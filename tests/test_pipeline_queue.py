"""Pipeline queue: add steps, dedupe, run/stop."""

from test_batch import _prepared

from odoo_migrator.db import database
from odoo_migrator.models import PipelineQueueItem
from odoo_migrator.pipeline_queue import queue_snapshot, run_state


def test_queue_add_json_avoids_redirect(client):
    row, _sale = _prepared(client)

    response = client.post(
        "/run/queue/add",
        data={"step": "1"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["migration_id"] == row.id
    assert payload["modules"]
    assert payload["run_state"] == "stopped"


def test_add_step_dedupes_and_lists_modules(client):
    row, _sale = _prepared(client)

    first = client.post("/run/queue/add", data={"step": "1"}, follow_redirects=True)
    again = client.post("/run/queue/add", data={"step": "1"}, follow_redirects=True)

    assert first.status_code == 200
    assert again.status_code == 200
    with database.connection_context():
        assert PipelineQueueItem.select().where(PipelineQueueItem.migration == row.id).count() == 1
    snap = queue_snapshot(row.id)
    assert snap["run_state"] == "stopped"
    assert len(snap["modules"]) >= 1


def test_queue_run_page_has_selection_and_queue(client):
    _prepared(client)

    page = client.get("/run")

    # Selection is a bar, not a card that folds: the step chips are always there.
    assert 'role="radiogroup" aria-label="Migration step to queue"' in page.text
    assert "runner-bar" in page.text
    assert "pipeline-selection-head" not in page.text
    assert ">Queue<" in page.text
    assert "FIFO · First in, first out" in page.text
    assert "Add step" in page.text
    assert ">Add all steps<" in page.text
    assert 'aria-label="Clear the pipeline queue"' in page.text
    assert 'aria-label="Run the pipeline queue"' in page.text


def _queued_steps(migration_id):
    with database.connection_context():
        return [
            item.step
            for item in PipelineQueueItem.select()
            .where(PipelineQueueItem.migration == migration_id)
            .order_by(PipelineQueueItem.position)
        ]


def test_a_step_is_not_queued_over_an_earlier_one_that_is_not_migrated(client):
    row, _sale = _prepared(client)

    refused = client.post(
        "/run/queue/add",
        data={"step": "2"},
        headers={"Accept": "application/json"},
    )

    assert refused.status_code == 400
    assert refused.json()["error"] == (
        "Step 2 cannot be queued before step 1: add it first, or migrate it."
    )
    assert _queued_steps(row.id) == []


def test_a_step_is_queued_once_every_earlier_one_is_queued(client):
    row, _sale = _prepared(client)

    for step in ("1", "2"):
        added = client.post(
            "/run/queue/add", data={"step": step}, headers={"Accept": "application/json"}
        )
        assert added.status_code == 200

    assert _queued_steps(row.id) == [1, 2]


def test_a_step_is_queued_over_earlier_ones_that_are_migrated(client):
    row, _sale = _prepared(client)
    from odoo_migrator.graph import list_modules
    from odoo_migrator.models import Module

    earlier = [m["id"] for m in list_modules(row.id) if m["step"] == 1 and not m["struck_out"]]
    with database.connection_context():
        Module.update(status="passed").where(Module.id.in_(earlier)).execute()

    added = client.post(
        "/run/queue/add", data={"step": "2"}, headers={"Accept": "application/json"}
    )

    assert added.status_code == 200
    assert _queued_steps(row.id) == [2]


def test_the_selection_marks_a_step_that_cannot_be_queued_yet(client):
    _prepared(client)

    page = client.get("/run")

    assert "blockedBy(" in page.text


def test_queue_modules_sort_by_migration_rank(client):
    row, _sale = _prepared(client)
    client.post("/run/queue/add", data={"step": "1"}, follow_redirects=True)
    client.post("/run/queue/add", data={"step": "2"}, follow_redirects=True)

    snap = queue_snapshot(row.id)
    ranks = [module["migration_rank"] for module in snap["modules"]]

    assert ranks == sorted(ranks)
    assert snap["modules"][0]["position"] == 1
    assert snap["modules"][0]["migration_rank"] == min(ranks)


def test_add_all_steps_queues_every_wave(client):
    row, _sale = _prepared(client)
    from odoo_migrator.graph import list_pipeline_steps

    waves = list_pipeline_steps(row.id, pending_only=True)
    page = client.post("/run/queue/add-all", follow_redirects=True)

    assert page.status_code == 200
    with database.connection_context():
        queued = list(
            PipelineQueueItem.select()
            .where(PipelineQueueItem.migration == row.id)
            .order_by(PipelineQueueItem.position)
        )
    assert len(queued) == len(waves)
    assert [item.step for item in queued] == [wave["step"] for wave in waves]


def test_add_all_steps_dedupes(client):
    row, _sale = _prepared(client)
    from odoo_migrator.graph import list_pipeline_steps

    waves = list_pipeline_steps(row.id, pending_only=True)
    client.post("/run/queue/add", data={"step": str(waves[0]["step"])}, follow_redirects=True)
    client.post("/run/queue/add-all", follow_redirects=True)
    with database.connection_context():
        count = PipelineQueueItem.select().where(PipelineQueueItem.migration == row.id).count()

    assert count == len(waves)


def test_a_passed_wave_stays_visible_and_is_not_queued(client):
    row, _sale = _prepared(client)
    from odoo_migrator.graph import list_modules, list_pipeline_steps
    from odoo_migrator.models import Module

    listed = list_modules(row.id)
    roots = [module for module in listed if module["step"] == 1 and not module["struck_out"]]
    with database.connection_context():
        Module.update(status="passed").where(
            Module.id.in_([module["id"] for module in roots])
        ).execute()

    page = client.get("/run")
    waves = list_pipeline_steps(row.id)
    root = next(wave for wave in waves if wave["step"] == 1)

    # The page carries every wave, the passed one with nothing pending; the
    # chip for it is drawn in the browser, marked passed and not queueable.
    assert '"pending": 0' in page.text
    assert root["modules"]
    assert '"step": 2' in page.text
    assert root["pending"] == 0
    refused = client.post("/run/queue/add", data={"step": "1"}, follow_redirects=True)
    assert "Step 1 is already passed." in refused.text
    with database.connection_context():
        assert (
            PipelineQueueItem.select().where(PipelineQueueItem.migration == row.id).count()
            == 0
        )


def test_add_all_steps_skips_passed_modules(client):
    row, _sale = _prepared(client)
    from odoo_migrator.graph import list_modules, list_pipeline_steps
    from odoo_migrator.models import Module

    done = next(m for m in list_modules(row.id) if m["technical_name"] == "sale_custom")
    with database.connection_context():
        Module.update(status="passed").where(Module.id == done["id"]).execute()
    pending = list_pipeline_steps(row.id, pending_only=True)
    client.post("/run/queue/add-all", follow_redirects=True)
    with database.connection_context():
        queued = [
            item.step
            for item in PipelineQueueItem.select()
            .where(PipelineQueueItem.migration == row.id)
            .order_by(PipelineQueueItem.position)
        ]
    pending_names = {name for wave in pending for name in wave["modules"]}

    assert queued == [wave["step"] for wave in pending]
    assert "sale_custom" not in pending_names


def test_clear_drops_the_queue_when_nothing_is_running(client):
    row, _sale = _prepared(client)
    client.post("/run/queue/add", data={"step": "1"}, follow_redirects=True)

    page = client.post("/run/queue/clear", follow_redirects=True)

    assert page.status_code == 200
    assert "still working" not in page.text
    with database.connection_context():
        assert (
            PipelineQueueItem.select().where(PipelineQueueItem.migration == row.id).count()
            == 0
        )


def test_clear_refuses_while_the_pipe_is_running(client):
    row, _sale = _prepared(client)
    client.post("/run/queue/add", data={"step": "1"}, follow_redirects=True)
    from odoo_migrator.pipeline_queue import set_run_state

    set_run_state(row.id, "running")

    page = client.post("/run/queue/clear", follow_redirects=True)

    assert "still working" in page.text
    with database.connection_context():
        assert (
            PipelineQueueItem.select().where(PipelineQueueItem.migration == row.id).count()
            == 1
        )


def test_queue_run_sets_running(client):
    row, _sale = _prepared(client)
    client.post("/run/queue/add", data={"step": "1"}, follow_redirects=True)
    client.app.state.executor = type(
        "Hold", (), {"start": lambda self, payload: None}
    )()

    page = client.post("/run/queue/run", follow_redirects=True)

    assert page.status_code == 200
    assert run_state(row.id) == "running"


def test_queue_run_on_an_empty_queue_says_so(client):
    row, _sale = _prepared(client)

    page = client.post("/run/queue/run", follow_redirects=True)

    assert page.status_code == 200
    assert "Nothing is queued." in page.text
    assert run_state(row.id) == "stopped"
    form = page.text[page.text.index('action="/run/queue/run"') :]
    assert "disabled" in form[: form.index("</form>")]


def test_a_queue_row_says_its_modules_status(client):
    """The lamp of the module in work spins while it runs and stands still
    while it waits on a person; the stylesheet can only tell the two apart
    from the row."""
    row, _sale = _prepared(client)
    client.post("/run/queue/add", data={"step": "1"}, follow_redirects=True)
    first = queue_snapshot(row.id)["modules"][0]

    page = client.get("/run").text

    assert f'data-module-id="{first["id"]}" data-status="{first["status"]}"' in page


def test_a_wave_the_queue_carries_on_from_is_not_announced_for_review(client):
    """The batch closed to `checkpoint` and said so; the queue then approved
    it and started the next wave. Runner showed "This batch has finished.
    Reload to review it." at every wave, with nothing left to review."""
    from test_batch import OutcomeExecutor

    from odoo_migrator.models import Batch

    row, _sale = _prepared(client)
    client.post("/run/queue/add-all", follow_redirects=True)
    client.app.state.executor = OutcomeExecutor({}, client.app)

    client.post("/run/queue/run", follow_redirects=True)

    with database.connection_context():
        statuses = {batch.status for batch in Batch.select().where(Batch.migration == row.id)}
    assert "checkpoint" not in statuses
    announced = [event for event in client.app.state.hub.published if event[0] == "checkpoint"]
    assert announced == []


def _remove(client, module_id):
    return client.post(
        "/run/queue/remove",
        data={"module": str(module_id)},
        headers={"Accept": "application/json"},
    )


def _queue_both_steps(client):
    row, _sale = _prepared(client)
    for step in ("1", "2"):
        client.post("/run/queue/add", data={"step": step}, headers={"Accept": "application/json"})

    return row


def _cards(migration_id):
    return [module["technical_name"] for module in queue_snapshot(migration_id)["modules"]]


def _last(migration_id):

    return queue_snapshot(migration_id)["modules"][-1]


def test_the_last_card_leaves_the_queue_alone(client):
    row = _queue_both_steps(client)
    before = _cards(row.id)

    removed = _remove(client, _last(row.id)["id"])

    assert removed.status_code == 200
    assert _cards(row.id) == before[:-1]
    assert [module["technical_name"] for module in removed.json()["modules"]] == before[:-1]


def test_a_step_with_cards_left_stays_in_the_queue(client):
    row = _queue_both_steps(client)
    _remove(client, _last(row.id)["id"])

    assert _queued_steps(row.id) == [1]

    _remove(client, _last(row.id)["id"])

    assert _cards(row.id) == ["sale_custom"]
    assert _queued_steps(row.id) == [1]


def test_a_step_whose_cards_are_all_removed_leaves_the_queue(client):
    row = _queue_both_steps(client)

    _remove(client, _last(row.id)["id"])

    assert _queued_steps(row.id) == [1]
    assert 2 not in {module["step"] for module in queue_snapshot(row.id)["modules"]}


def test_a_card_that_is_not_the_last_stays(client):
    row = _queue_both_steps(client)
    first = queue_snapshot(row.id)["modules"][0]

    refused = _remove(client, first["id"])

    assert refused.status_code == 400
    assert refused.json()["error"] == (
        "sale_custom is not the last in the queue: remove from the end."
    )
    assert len(_cards(row.id)) == 3


def test_a_module_that_is_not_queued_is_refused(client):
    _row, sale = _prepared(client)

    refused = _remove(client, sale["id"])

    assert refused.status_code == 400
    assert refused.json()["error"] == "That module is not in the queue."


def test_a_card_in_flight_stays(client):
    from odoo_migrator.models import Batch, Job

    row = _queue_both_steps(client)
    last = _last(row.id)
    with database.connection_context():
        batch = Batch.create(migration=row.id, status="running")
        Job.create(batch=batch, module=last["id"], status="running", job_id="flying-1")

    refused = _remove(client, last["id"])

    assert refused.status_code == 400
    assert refused.json()["error"] == "stock_custom is running: stop it first."
    assert len(_cards(row.id)) == 3
    assert _last(row.id)["removable"] is False


def test_only_the_last_card_can_be_removed(client):
    row = _queue_both_steps(client)

    modules = queue_snapshot(row.id)["modules"]

    assert [module["removable"] for module in modules] == [False, False, True]


def test_the_page_draws_the_remove_button_on_the_last_card_only(client):
    row = _queue_both_steps(client)
    last = _last(row.id)

    page = client.get("/run").text

    assert page.count("data-queue-remove") == 1
    assert f'data-queue-remove data-module="{last["id"]}"' in page


def test_a_step_missing_a_card_is_not_whole_for_the_steps_after_it(client):
    row = _queue_both_steps(client)
    _remove(client, _last(row.id)["id"])
    _remove(client, _last(row.id)["id"])
    snapshot = queue_snapshot(row.id)

    assert snapshot["steps"] == [{"step": 1, "whole": False}]

    refused = client.post(
        "/run/queue/add", data={"step": "2"}, headers={"Accept": "application/json"}
    )

    assert refused.status_code == 400
    assert refused.json()["error"] == (
        "Step 2 cannot be queued before step 1: add it first, or migrate it."
    )


def test_adding_a_step_again_brings_its_removed_cards_back(client):
    row = _queue_both_steps(client)
    _remove(client, _last(row.id)["id"])
    _remove(client, _last(row.id)["id"])

    added = client.post(
        "/run/queue/add", data={"step": "1"}, headers={"Accept": "application/json"}
    )

    assert added.status_code == 200
    assert _cards(row.id) == ["sale_custom", "web_enterprise_helper"]
    assert queue_snapshot(row.id)["steps"] == [{"step": 1, "whole": True}]


def test_a_removed_card_is_not_run_with_its_step(client):
    from odoo_migrator.models import Job, Module

    row = _queue_both_steps(client)
    _remove(client, _last(row.id)["id"])
    _remove(client, _last(row.id)["id"])

    client.post("/run/queue/run", follow_redirects=True)

    with database.connection_context():
        ran = {
            job.module.technical_name
            for job in Job.select().join(Module).where(Module.migration == row.id)
        }
    assert ran == {"sale_custom"}


def test_clear_forgets_what_was_removed(client):
    row = _queue_both_steps(client)
    _remove(client, _last(row.id)["id"])
    _remove(client, _last(row.id)["id"])

    client.post("/run/queue/clear", follow_redirects=True)
    client.post("/run/queue/add", data={"step": "1"}, headers={"Accept": "application/json"})

    assert _cards(row.id) == ["sale_custom", "web_enterprise_helper"]
