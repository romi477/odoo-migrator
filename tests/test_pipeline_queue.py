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
    assert 'role="group" aria-label="Migration step to queue"' in page.text
    assert "runner-bar" in page.text
    assert "pipeline-selection-head" not in page.text
    assert ">Queue<" in page.text
    assert "FIFO · First in, first out" in page.text
    # A step is queued by its own chip; the only button is the one for them all.
    assert "Add the selected step to the queue" not in page.text
    assert ">Add all<" in page.text
    assert "requestSubmit()" in page.text
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
    # The steps the picked one waits on are marked, and the note names them.
    assert "is-blocker" in page.text
    assert "missing()" in page.text


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

    assert snapshot["steps"] == [
        {"step": 1, "whole": False, "removable": True, "why": ""}
    ]

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
    assert queue_snapshot(row.id)["steps"] == [
        {"step": 1, "whole": True, "removable": True, "why": ""}
    ]


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


def test_the_queue_card_says_whether_it_holds_anything(client):
    _prepared(client)

    empty = client.get("/run").text

    assert 'data-has-items="false"' in empty
    assert 'data-has-items="true"' not in empty

    client.post("/run/queue/add", data={"step": "1"}, headers={"Accept": "application/json"})

    assert 'data-has-items="true"' in client.get("/run").text


def test_the_log_is_the_last_thing_on_the_screen(client):
    _prepared(client)

    page = client.get("/run").text
    workspace = page.index('class="workspace"')
    log = page.index("data-run-log-panel")

    # Out of the workspace the run's card is in, and after it: it is the
    # canvas at the bottom, not a card among the others.
    assert log > workspace
    assert page.index("</article>", workspace) < log
    # One cable, on the left, out of the queue card into the run's card.
    assert page.count('class="runner-link"') == 1


def _stop_form(page):
    import re

    return re.search(r'<form[^>]*action="/run/queue/stop"[^>]*>', page).group(0)


def test_stop_is_there_only_while_the_queue_runs(client):
    from odoo_migrator.pipeline_queue import set_run_state

    row, _sale = _prepared(client)
    client.post("/run/queue/add", data={"step": "1"}, headers={"Accept": "application/json"})

    assert " hidden" in _stop_form(client.get("/run").text)

    set_run_state(row.id, "running")

    assert " hidden" not in _stop_form(client.get("/run").text)


def _unqueue(client, step):
    return client.post(
        "/run/queue/remove-step",
        data={"step": str(step)},
        headers={"Accept": "application/json"},
    )


def _flags(migration_id):

    return {
        item["step"]: (item["removable"], item["why"])
        for item in queue_snapshot(migration_id)["steps"]
    }


def test_a_queued_step_is_taken_out_whole(client):
    row = _queue_both_steps(client)

    removed = _unqueue(client, 2)

    assert removed.status_code == 200
    assert _queued_steps(row.id) == [1]
    assert {module["step"] for module in removed.json()["modules"]} == {1}


def test_a_step_a_queued_one_waits_on_cannot_be_taken_out_first(client):
    row = _queue_both_steps(client)

    refused = _unqueue(client, 1)

    assert refused.status_code == 400
    assert refused.json()["error"] == (
        "Step 2 waits for step 1: take step 2 out first."
    )
    assert _queued_steps(row.id) == [1, 2]
    removable, why = _flags(row.id)[1]
    assert removable is False
    assert why == "Step 2 waits for step 1: take step 2 out first."
    assert _flags(row.id)[2] == (True, "")


def test_a_step_that_has_started_stays(client):
    from odoo_migrator.models import Batch

    row = _queue_both_steps(client)
    with database.connection_context():
        Batch.create(migration=row.id, status="running", step=2)

    refused = _unqueue(client, 2)

    assert refused.status_code == 400
    assert refused.json()["error"] == "Step 2 has started: let it finish or stop it."
    assert _queued_steps(row.id) == [1, 2]
    assert _flags(row.id)[2][0] is False


def test_a_step_that_is_not_queued_cannot_be_taken_out(client):
    row, _sale = _prepared(client)

    refused = _unqueue(client, 1)

    assert refused.status_code == 400
    assert refused.json()["error"] == "Step 1 is not in the queue."
    assert _queued_steps(row.id) == []


def test_taking_a_step_out_forgets_the_cards_taken_off_it(client):
    row = _queue_both_steps(client)
    _remove(client, _last(row.id)["id"])
    _remove(client, _last(row.id)["id"])

    _unqueue(client, 1)
    client.post("/run/queue/add", data={"step": "1"}, headers={"Accept": "application/json"})

    assert _cards(row.id) == ["sale_custom", "web_enterprise_helper"]


def test_the_chips_can_take_a_queued_step_out(client):
    _queue_both_steps(client)

    page = client.get("/run").text

    # A hidden submit button carries the second action, so a chip can queue a
    # step and, pressed again, take it out.
    assert 'formaction="/run/queue/remove-step"' in page
    assert "is-undo" in page


def _bar_count(client):
    html = client.get("/run").text
    bar = html.split('class="queue-count"', 1)[1].split("</span></span>", 1)[0]

    return " ".join(
        part.strip()
        for part in bar.replace("<span", "|<span").split("|")
        if part.strip()
    )


def _hold(client):
    client.app.state.executor = type("Hold", (), {"start": lambda self, payload: None})()


def test_the_bar_counts_zero_for_an_empty_queue(client):
    row, _sale = _prepared(client)

    count = queue_snapshot(row.id)["count"]

    assert (count["text"], count["unit"], count["queued"]) == ("0", "queued", 0)
    assert 'data-queued="0"' in client.get("/run").text


def test_the_bar_counts_the_modules_in_the_queue(client):
    row = _queue_both_steps(client)
    waiting = len(queue_snapshot(row.id)["modules"])

    count = queue_snapshot(row.id)["count"]

    assert waiting >= 2
    assert (count["text"], count["unit"]) == (str(waiting), "queued")
    assert f'data-queued="{waiting}"' in client.get("/run").text
    assert f">{waiting}</span> <span class=\"unit\">queued<" in client.get("/run").text


def test_a_running_queue_reads_the_place_out_of_the_total(client):
    row = _queue_both_steps(client)
    total = len(queue_snapshot(row.id)["modules"])
    _hold(client)

    client.post("/run/queue/run", follow_redirects=True)
    first = queue_snapshot(row.id)["count"]

    assert (first["text"], first["unit"]) == (f"1/{total}", "run")
    assert first["title"] == f"Module 1 of {total} in the queue."

    from odoo_migrator.models import Module

    one = queue_snapshot(row.id)["modules"][0]["id"]
    with database.connection_context():
        Module.update(status="passed").where(Module.id == one).execute()

    assert queue_snapshot(row.id)["count"]["text"] == f"2/{total}"
    assert f"2/{total}" in _bar_count(client)


def test_cards_added_to_a_running_queue_grow_the_total(client):
    row, _sale = _prepared(client)
    client.post("/run/queue/add", data={"step": "1"}, headers={"Accept": "application/json"})
    first_step = len(queue_snapshot(row.id)["modules"])
    _hold(client)
    client.post("/run/queue/run", follow_redirects=True)

    client.post("/run/queue/add", data={"step": "2"}, headers={"Accept": "application/json"})
    both = len(queue_snapshot(row.id)["modules"])

    assert both > first_step
    assert queue_snapshot(row.id)["count"]["text"] == f"1/{both}"

    client.post("/run/queue/remove-step", data={"step": "2"}, headers={"Accept": "application/json"})

    assert queue_snapshot(row.id)["count"]["text"] == f"1/{first_step}"


def test_a_stopped_queue_keeps_its_place_while_a_module_finishes(client):
    """Stop lets the module in flight finish, so the run is not over yet."""
    row = _queue_both_steps(client)
    total = len(queue_snapshot(row.id)["modules"])
    _hold(client)
    client.post("/run/queue/run", follow_redirects=True)
    client.post("/run/queue/stop", follow_redirects=True)

    count = queue_snapshot(row.id)["count"]

    assert (count["text"], count["unit"]) == (f"1/{total}", "run")


def test_the_cable_to_the_run_carries_a_pulse_while_the_queue_runs(client):
    _prepared(client)
    page = client.get("/run").text
    css = client.get("/static/style.css").text
    link = page.split('class="runner-link"', 1)[1].split("</span></span>", 1)[0]

    assert 'class="runner-link-pulse"' in link
    assert "opacity: 0" in css.split(".runner-link-pulse {")[1].split("}")[0]
    assert 'data-working="true"]) .runner-link-pulse' in css
    # Down the cable, again and again, and still under reduced motion.
    assert "link-pulse 1.6s linear infinite" in css
    assert "translateY(34px)" in css.split("@keyframes link-pulse")[1].split("@media")[0]
    reduced = css.split("@keyframes link-pulse")[1].split("@media (prefers-reduced-motion: reduce) {")[1]

    assert "animation: none" in reduced.split("}\n}")[0]


def test_the_cable_to_the_run_stays_cyan_and_only_its_pulse_turns_amber(client):
    """The card it enters has a cyan edge, so the cable is one colour. What it
    carries is paler and takes the amber of the work on the way."""
    _prepared(client)
    page = client.get("/run").text
    css = client.get("/static/style.css").text

    assert "runner-link-wire" not in page and "runner-link-wire" not in css
    frames = css.split("@keyframes link-pulse")[1].split("@media")[0]

    assert "var(--cyan) 50%, white" in frames
    assert "var(--amber) 50%, white" in frames


def _play(client, module_id):
    return client.post(
        "/run/queue/run-module",
        data={"module": str(module_id)},
        headers={"Accept": "application/json"},
    )


def _statuses(migration_id):
    from odoo_migrator.models import Module

    with database.connection_context():
        return {
            row.technical_name: row.status
            for row in Module.select().where(Module.migration == migration_id)
        }


def test_a_play_button_runs_that_module_and_nothing_after_it(client):
    row = _queue_both_steps(client)
    _hold(client)
    first = queue_snapshot(row.id)["modules"][0]
    others = [module["technical_name"] for module in queue_snapshot(row.id)["modules"][1:]]
    before = _statuses(row.id)

    started = _play(client, first["id"])

    assert started.status_code == 200
    after = _statuses(row.id)
    assert after[first["technical_name"]] == "running"
    # The queue itself did not start: nothing after this module moved.
    assert run_state(row.id) == "stopped"
    assert all(after[name] == before[name] for name in others)
    assert started.json()["modules"][0]["active"] is True


def test_play_is_off_with_a_reason_while_something_else_is_running(client):
    row = _queue_both_steps(client)
    _hold(client)
    modules = queue_snapshot(row.id)["modules"]

    assert modules[0]["play"] == ""
    client.post("/run/queue/run", follow_redirects=True)
    running = queue_snapshot(row.id)["modules"]

    assert all("queue is running" in module["play"] for module in running)
    refused = _play(client, running[-1]["id"])

    assert refused.status_code == 400
    assert "queue is running" in refused.json()["error"]


def test_play_on_a_module_of_a_later_step_waits_for_the_earlier_ones(client):
    row = _queue_both_steps(client)
    _hold(client)
    later = next(m for m in queue_snapshot(row.id)["modules"] if m["step"] == 2)

    assert later["play"]
    refused = _play(client, later["id"])

    assert refused.status_code == 400
    assert refused.json()["error"] == later["play"]
    assert _statuses(row.id)[later["technical_name"]] != "running"


def test_the_module_in_flight_has_an_amber_square_that_stops_it_now(client):
    row = _queue_both_steps(client)
    _hold(client)
    first = queue_snapshot(row.id)["modules"][0]
    _play(client, first["id"])
    page = client.get("/run").text
    square = page.split(f'data-queue-stop data-module="{first["id"]}"')[1].split("</form>")[0]

    # Its own run, stopped now: the same route as the card's Stop always was.
    assert f'action="/run/modules/{first["id"]}/stop"' in square
    assert "disabled" not in square.split(">", 2)[1]
    assert f'Stop {first["technical_name"]} now' in square
    assert "<rect" in square
    assert f'data-queue-play data-module="{first["id"]}"' not in page
    css = client.get("/static/style.css").text

    assert "color: var(--amber)" in css.split(".pipeline-queue-active .queue-play,")[1].split("}")[0]
    assert "var(--red)" in css.split(".queue-stop button.queue-play:hover:not(:disabled),")[1].split("}")[0]
    script = client.get("/static/pipeline.js").text

    assert "function stopSquare" in script
    assert "mod.active && IN_WORK[mod.status]" in script


def test_every_row_has_a_play_button_and_it_is_an_outlined_triangle_that_gives_when_pressed(client):
    from odoo_migrator.paths import static_dir

    row = _queue_both_steps(client)
    page = client.get("/run").text
    script = client.get("/static/pipeline.js").text
    css = client.get("/static/style.css").text
    cards = len(queue_snapshot(row.id)["modules"])
    play = css.split(".queue-play {\n  order: 2;")[1].split("}")[0]

    # The collapsed first row and the list: the first card twice.
    assert page.count("data-queue-play") == cards + 1
    assert 'd="M10.41 6.10 Q7.00 4.00 7.00 8.00 L7.00 16.00 Q7.00 20.00 10.41 17.90 L16.59 14.10 Q20.00 12.00 16.59 9.90 Z"' in page
    # Outlined, not filled, and the corners are rounded by the stroke's joins.
    glyph = page.split('class="play-glyph"')[1].split("</svg>")[0]
    assert 'fill="none"' in glyph and 'stroke-linejoin="round"' in glyph
    # The corners are curves in the path, three of them, not sharp vertices.
    assert glyph.count(" Q") == 3
    # A real target, white; amber and a plate under the pointer; it gives when
    # pressed, as a player's button does.
    assert "width: 26px" in play and "height: 26px" in play
    assert "var(--text)" in play
    hover = css.split("button.queue-play:hover:not(:disabled),")[1].split("}")[0]
    assert "color: var(--amber)" in hover and "background:" in hover
    assert "transform: scale(0.9)" in css.split("button.queue-play:active:not(:disabled) {")[1].split("}")[0]
    assert "animation" not in play
    # The page script draws the same one for a row it builds, and the lamp and
    # the ball in a ring are gone.
    assert "function playButton" in script and 'setAttribute("fill", "none")' in script.split("function playButton")[1]
    assert "pipeline-lamp" not in css and "pipeline-lamp" not in page
    assert not (static_dir() / "lamp-ball.js").exists()
    assert "has-ball" not in css


def _running_page(client):
    row = _queue_both_steps(client)
    _hold(client)
    first = queue_snapshot(row.id)["modules"][0]
    _play(client, first["id"])

    return row, first, client.get("/run").text


def test_the_cables_pulse_for_any_work_in_hand_not_only_a_running_queue(client):
    row, _first, page = _running_page(client)
    card = page.split("data-pipeline-queue-panel", 1)[1].split(">", 1)[0]
    css = client.get("/static/style.css").text

    # One module run on its own: the queue is stopped, and work is going on.
    assert 'data-running="false"' in card
    assert 'data-working="true"' in card
    assert queue_snapshot(row.id)["idle"] is False
    assert 'data-running="true"]) .runner-hook::after' not in css
    assert 'data-working="true"]) .runner-hook::after' in css
    assert 'data-working="true"]) .runner-link-pulse' in css


def test_the_runner_card_does_not_say_the_module_or_its_state_again(client):
    _row, first, page = _running_page(client)
    live = page.split('class="pipeline-live"', 1)[1].split('class="ribbon"', 1)[0]
    machine = page.split('class="machine"', 1)[1].split("</article>", 1)[0]

    assert first["technical_name"] in machine
    # Neither the name, nor a badge with the status, below the machine.
    assert "data-pipeline-module" not in page
    assert "data-pipeline-status" not in page
    assert first["technical_name"] not in live
    assert 'class="badge' not in live
    assert "pipeline-head-who" not in page


def test_a_queued_run_is_stopped_from_its_card_and_the_header_stop_is_the_queues(client):
    _row, first, page = _running_page(client)
    controls = page.split('class="pipeline-queue-controls"', 1)[1].split("</header>", 1)[0]
    run = controls.split('action="/run/queue/run"')[1].split(">", 1)[0]
    now = controls.split("data-pipeline-stop")[1].split(">", 1)[0]
    live = page.split('class="pipeline-live"', 1)[1].split("</article>", 1)[0]

    assert "hidden" in run
    # The module has a card in the queue, and its square stops it now; the
    # header does not say it a second time.
    assert "hidden" in now
    assert f'data-queue-stop data-module="{first["id"]}"' in page
    assert "data-pipeline-stop" not in live
    # The header's Stop is the queue's: after this module. One word, and it
    # is there only while the queue runs.
    queue = controls.split('action="/run/queue/stop"')[1].split("</form>", 1)[0]
    assert ">Stop</button>" in queue
    assert "Stop after this module" not in controls
    css = client.get("/static/style.css").text

    assert ".pipeline-stop button::before" in css
    assert "border-radius: 2px" in css.split(".pipeline-stop button::before {")[1].split("}")[0]

    script = client.get("/static/sse.js").text
    assert 'document.querySelector("[data-pipeline-stop]")' in script
    assert "run.hidden = !!LIVE[data.status]" in script
    assert "stop.hidden = !LIVE[data.status] || !!carded" in script


def test_a_module_that_came_out_rests_in_cyan_not_green(client):
    css = client.get("/static/style.css").text
    rule = css.split('.machine[data-machine="done"] {')[1].split("}")[0]

    assert "--machine: var(--cyan)" in rule
    assert "green" not in rule


def test_the_steps_lead_from_the_input_slot_with_one_small_arrow_that_turns_amber_at_work(client):
    _row, _first, page = _running_page(client)
    ribbon = page.split('class="ribbon" data-ribbon>', 1)[1].split("</ol>", 1)[0]
    css = client.get("/static/style.css").text

    # The versions are the machine's slots; said again as blocks they pushed
    # the steps onto a second line.
    assert "ribbon-end" not in ribbon
    assert ">17.0<" not in ribbon and ">18.0<" not in ribbon
    # Every arrow is one rule, so one size; the first step's included.
    arrow = css.split(".ribbon:not(.ribbon-lane) .ribbon-node::before,\n.ribbon-node + .ribbon-node::before {")[1].split("}")[0]

    assert "font-size: 12px" in arrow
    assert css.count('content: "→"') == 1
    amber = css.split('.workspace-panel:has(.machine[data-machine="run"]) .ribbon:not(.ribbon-lane) .ribbon-node::before {')[1].split("}")[0]

    assert "color: var(--amber)" in amber


def test_a_module_run_on_its_own_does_not_stop_for_review_when_it_passed(client):
    """The play button asked for one module. A review screen with a diff and a
    Continue button, and every other play barred until it is pressed, is the
    wrong end to it."""
    from odoo_migrator.models import Batch

    row = _queue_both_steps(client)
    first = queue_snapshot(row.id)["modules"][0]
    # An executor that answers at once, through the app's own callback.
    from test_batch import OutcomeExecutor

    client.app.state.executor = OutcomeExecutor({}, client.app)
    started = _play(client, first["id"])

    assert started.status_code == 200
    with database.connection_context():
        batches = list(Batch.select().where(Batch.migration == row.id))
    assert batches and all(batch.status != "checkpoint" for batch in batches)
    assert _statuses(row.id)[first["technical_name"]] == "passed"
    page = client.get("/run").text

    assert 'aria-label="Continue ' not in page
    assert "Review the last batch" not in page
    # And the next module's play is not barred behind a review.
    after = queue_snapshot(row.id)["modules"]
    assert all("Review the last batch" not in module["play"] for module in after)


def test_a_module_run_on_its_own_that_failed_still_waits_for_review(client):
    from odoo_migrator.models import Batch, Job

    row = _queue_both_steps(client)
    _hold(client)
    first = queue_snapshot(row.id)["modules"][0]
    _play(client, first["id"])
    with database.connection_context():
        job = Job.select().order_by(Job.id.desc()).get()
        job.status = "failed"
        job.save()
        batch = Batch.get_by_id(job.batch_id)
    from odoo_migrator.jobs import close_batch
    from odoo_migrator.pipeline_queue import after_batch_checkpoint

    close_batch(batch.id)
    after_batch_checkpoint(row.id, batch.id, client.app.state.executor, client.app.state.hub)
    with database.connection_context():
        assert Batch.get_by_id(batch.id).status == "checkpoint"


def _stopped_in_review(client):
    """A module run on its own and stopped: its batch waits for review, and
    the module stays in the queue."""
    from test_batch import OutcomeExecutor

    row = _queue_both_steps(client)
    first = queue_snapshot(row.id)["modules"][0]
    client.app.state.executor = OutcomeExecutor({first["technical_name"]: "stopped"}, client.app)
    _play(client, first["id"])

    return row, first


def test_a_run_in_review_is_answered_on_its_queue_card_and_run_waits_for_it(client):
    row, first = _stopped_in_review(client)
    snapshot = queue_snapshot(row.id)
    card = next(module for module in snapshot["modules"] if module["id"] == first["id"])

    assert snapshot["review"] is True
    assert card["review"] is not None and card["review"]["unread"] is False
    page = client.get("/run").text
    review = page.split("data-queue-review", 1)[1].split("</div>", 1)[0]

    # Continue and Discard in the play button's place, on the module's card.
    assert 'action="/run/continue"' in review
    assert f'action="/run/modules/{first["id"]}/discard-run"' in review
    assert "data-confirm-title" in review
    # Said once: the review under the card has neither, and no second Continue.
    assert page.count('action="/run/continue"') == page.count("data-queue-review")
    assert ">Discard run</button>" not in page
    # Run stays off until the review is answered.
    controls = page.split('class="pipeline-queue-controls"', 1)[1].split("</header>", 1)[0]
    run = controls.split('action="/run/queue/run"')[1].split("</form>", 1)[0]
    assert "disabled" in run
    assert "waits for review" in run


def test_discard_on_the_queue_card_frees_run(client):
    row, first = _stopped_in_review(client)

    client.post(f"/run/modules/{first['id']}/discard-run", follow_redirects=True)

    snapshot = queue_snapshot(row.id)
    assert snapshot["review"] is False
    assert all(module["review"] is None for module in snapshot["modules"])
    page = client.get("/run").text
    assert "data-queue-review" not in page
    run = page.split('action="/run/queue/run"')[1].split("</form>", 1)[0]
    assert "disabled" not in run


def test_the_page_script_draws_the_review_pair_and_keeps_run_off(client):
    script = client.get("/static/pipeline.js").text

    assert "function reviewButtons" in script
    assert "if (mod.review)" in script
    assert "|| !!data.review" in script
