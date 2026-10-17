"""Sequential batch runner, stop rules, global attention. Task 10."""

import pytest
from test_tree import _open

from odoo_migrator.db import database
from odoo_migrator.migrations import save_side_container
from odoo_migrator.models import Batch, Job, Module


def _named(migration_id, name):
    from odoo_migrator.graph import list_modules

    return next(m for m in list_modules(migration_id) if m["technical_name"] == name)


def _prepared(client, name="Acme"):
    row = _open(client, name)
    save_side_container(row.id, "source", "odoo-old")
    save_side_container(row.id, "target", "odoo-new")
    client.post("/tree/resolve", follow_redirects=True)
    helper = _named(row.id, "web_enterprise_helper")
    # Resolve puts the helper on step 1 with sale_custom. Tests that then
    # group sale+stock as a two-module batch would otherwise pick it up.
    client.post(f"/tree/modules/{helper['id']}/batch", data={"batch_group": ""})

    return row, _named(row.id, "sale_custom")


def _group(client, *modules, group=1):
    for module in modules:
        client.post(
            f"/tree/modules/{module['id']}/batch",
            data={"batch_group": str(group)},
        )


class OutcomeExecutor:
    """Calls back with a per-module status, through the app's own callback.

    It used to call ``apply_result`` directly, which is the one thing a real
    executor cannot do: a remote executor reaches the app through the callback. Going
    around it meant the double skipped everything a result sets off — the
    batch advancing, above all — and the runner looked sequential while it
    was not.
    """

    def __init__(self, outcomes, app):
        self.outcomes = outcomes
        self.app = app
        self.started = []

    def start(self, payload):
        name = payload["module"]["technical_name"]
        self.started.append(name)
        self.app.state.on_result(
            {
                "job_id": payload["job_id"],
                "status": self.outcomes.get(name, "passed"),
                "attempts": [{"iteration": 1, "verdict": "done"}],
            }
        )


def test_a_failed_job_stops_the_batch_before_the_next_module(client):
    """stock_custom depends on sale_custom, so the order is not a guess."""
    row, sale = _prepared(client)
    stock = _named(row.id, "stock_custom")
    _group(client, sale, stock)
    failer = OutcomeExecutor({"sale_custom": "failed"}, client.app)
    client.app.state.executor = failer

    page = client.post("/tree/batch/run", data={"batch_group": "1"}, follow_redirects=True)

    assert page.status_code == 200
    assert failer.started == ["sale_custom"]
    with database.connection_context():
        jobs = list(Job.select().join(Module).where(Module.migration == row.id))
        batch = Batch.select().where(Batch.migration == row.id).get()
        sale_row = Module.get_by_id(sale["id"])
        stock_row = Module.get_by_id(stock["id"])

    assert len(jobs) == 1
    assert jobs[0].status == "failed"
    assert sale_row.status == "failed"
    assert stock_row.status == "queued"
    assert batch.status == "checkpoint"


def test_waiting_for_human_stops_the_batch_the_same_way(client):
    row, sale = _prepared(client)
    stock = _named(row.id, "stock_custom")
    _group(client, sale, stock)
    client.app.state.executor = OutcomeExecutor(
        {"sale_custom": "waiting_for_human"}, client.app
    )

    client.post("/tree/batch/run", data={"batch_group": "1"}, follow_redirects=True)

    with database.connection_context():
        assert Job.select().join(Module).where(Module.migration == row.id).count() == 1
        assert Module.get_by_id(stock["id"]).status == "queued"


def test_a_passing_batch_runs_dependents_after_their_parents(client):
    row, sale = _prepared(client)
    stock = _named(row.id, "stock_custom")
    _group(client, sale, stock)
    runner = OutcomeExecutor({}, client.app)
    client.app.state.executor = runner

    client.post("/tree/batch/run", data={"batch_group": "1"}, follow_redirects=True)

    assert runner.started == ["sale_custom", "stock_custom"]
    with database.connection_context():
        assert Module.get_by_id(sale["id"]).status == "passed"
        assert Module.get_by_id(stock["id"]).status == "passed"
        assert Batch.select().where(Batch.migration == row.id).get().status == "checkpoint"
        assert Job.select().join(Module).where(Module.migration == row.id).count() == 2


def test_two_migrations_run_independently_and_the_lamp_shows_on_the_other_screen(client):
    """A fail on Acme must not stop Beta, and must still light the lamp
    while the operator is looking at Beta."""
    acme, acme_sale = _prepared(client, name="Acme")
    acme_stock = _named(acme.id, "stock_custom")
    _group(client, acme_sale, acme_stock)
    beta, beta_sale = _prepared(client, name="Beta")
    beta_stock = _named(beta.id, "stock_custom")
    _group(client, beta_sale, beta_stock)

    failer = OutcomeExecutor({"sale_custom": "failed"}, client.app)
    client.app.state.executor = failer
    client.post(f"/migrations/{acme.id}/open", follow_redirects=True)
    client.post("/tree/batch/run", data={"batch_group": "1"}, follow_redirects=True)

    passer = OutcomeExecutor({}, client.app)
    client.app.state.executor = passer
    client.post(f"/migrations/{beta.id}/open", follow_redirects=True)
    client.post("/tree/batch/run", data={"batch_group": "1"}, follow_redirects=True)

    other = client.get("/tree")

    assert "Beta" in other.text
    assert 'class="attention-lamp needs-attention"' in other.text
    with database.connection_context():
        acme_jobs = Job.select().join(Module).where(Module.migration == acme.id).count()
        beta_jobs = Job.select().join(Module).where(Module.migration == beta.id).count()
        assert Module.get_by_id(acme_sale["id"]).status == "failed"
        assert Module.get_by_id(acme_stock["id"]).status == "queued"
        assert Module.get_by_id(beta_sale["id"]).status == "passed"
        assert Module.get_by_id(beta_stock["id"]).status == "passed"
    assert acme_jobs == 1
    assert beta_jobs == 2

    jumped = client.get("/attention", follow_redirects=True)

    assert jumped.status_code == 200
    assert "Acme" in jumped.text
    assert f'data-module-id="{acme_sale["id"]}"' in jumped.text


def test_run_batch_control_lives_on_pipeline(client):
    _prepared(client)

    tree = client.get("/tree").text
    run = client.get("/run").text

    assert ">Run batch<" not in tree
    assert ">Add to queue<" in run
    assert ">Queue<" in run
    assert "Step 1" in run
    assert "pipeline-step-modules" in run


def test_an_empty_or_missing_batch_is_loud(client):
    _prepared(client)

    empty = client.post(
        "/tree/batch/run", data={"batch_group": "9"}, follow_redirects=True
    )

    assert "form-error" in empty.text
    assert "batch" in empty.text.lower()
    with database.connection_context():
        assert Job.select().count() == 0


def test_styleguide_shows_the_pipeline_queue(client):
    html = client.get("/components").text

    assert ">Add to queue<" in html
    assert "pipeline-queue-list" in html


class DeferredExecutor:
    """Takes the trigger and answers later — the only shape a real executor has.

    The runner used to loop over the whole group calling ``start`` and then
    read the job status it had just created, which is still ``running``
    until a callback arrives. Against an executor that answers inline that
    read like a sequential batch; against this one it fired every module at
    once and never saw a stop.
    """

    def __init__(self, app):
        self.app = app
        self.started = []
        self.pending = []

    def start(self, payload):
        self.started.append(payload["module"]["technical_name"])
        self.pending.append(payload["job_id"])

    def answer(self, status="passed"):
        self.app.state.on_result({"job_id": self.pending.pop(0), "status": status})


def test_the_batch_waits_for_a_callback_before_starting_the_next_module(client):
    row, sale = _prepared(client)
    stock = _named(row.id, "stock_custom")
    helper = _named(row.id, "web_enterprise_helper")
    _group(client, sale, stock, helper)
    executor = DeferredExecutor(client.app)
    client.app.state.executor = executor

    client.post("/tree/batch/run", data={"batch_group": "1"}, follow_redirects=True)

    assert executor.started == ["sale_custom"]
    with database.connection_context():
        assert Batch.select().where(Batch.migration == row.id).get().status == "running"

    executor.answer("passed")

    assert executor.started == ["sale_custom", "stock_custom"]

    executor.answer("failed")

    assert executor.started == ["sale_custom", "stock_custom"]
    with database.connection_context():
        batch = Batch.select().where(Batch.migration == row.id).get()
        assert batch.status == "checkpoint"
        assert Module.get_by_id(helper["id"]).status == "queued"


def test_a_single_module_batch_stays_open_until_its_result(client):
    """start_job stamped the batch checkpoint the moment it had handed the
    trigger over, so Task 12 would open a review of a job still running."""
    row, sale = _prepared(client)
    executor = DeferredExecutor(client.app)
    client.app.state.executor = executor

    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)

    with database.connection_context():
        assert Batch.select().where(Batch.migration == row.id).get().status == "running"

    executor.answer("passed")

    with database.connection_context():
        assert Batch.select().where(Batch.migration == row.id).get().status == "checkpoint"


def test_a_restart_does_not_leave_a_batch_running_for_ever(client):
    """"A batch is already running on this migration" would have been the
    answer to every batch from then on."""
    from odoo_migrator.jobs import reconcile_jobs

    row, sale = _prepared(client)
    stock = _named(row.id, "stock_custom")
    _group(client, sale, stock)
    client.app.state.executor = DeferredExecutor(client.app)
    client.post("/tree/batch/run", data={"batch_group": "1"}, follow_redirects=True)

    reconcile_jobs()

    with database.connection_context():
        assert Batch.select().where(Batch.migration == row.id).get().status == "unknown"
    again = client.post(
        "/tree/batch/run", data={"batch_group": "1"}, follow_redirects=True
    )

    assert "already running" not in again.text


def test_a_later_step_cannot_run_until_earlier_steps_are_done(client):
    """Resolve already grouped sale_custom as step 1 and stock_custom as
    step 2. Starting the second wave while the first is still queued is
    the thing grouping exists to prevent."""
    row, _sale = _prepared(client)
    stock = _named(row.id, "stock_custom")

    page = client.post(
        "/tree/batch/run", data={"batch_group": "2"}, follow_redirects=True
    )

    assert page.status_code == 200
    assert "cannot start until step" in page.text.lower()
    with database.connection_context():
        assert Job.select().join(Module).where(Module.id == stock["id"]).count() == 0


def test_the_lamp_points_at_the_stalled_module_not_only_its_migration(client):
    """Opening the right migration and leaving the human to find one stalled
    node among a hundred is not opening the module."""
    _row, sale = _prepared(client)
    client.app.state.executor = OutcomeExecutor({"sale_custom": "failed"}, client.app)
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)

    jumped = client.get("/attention", follow_redirects=False)

    assert jumped.headers["location"] == f"/tree?focus={sale['id']}"
    script = client.get("/static/tree.js").text
    assert 'get("focus")' in script


def test_a_second_run_skips_the_modules_that_already_passed(client):
    """A wave is started because something in it is still queued. The runner
    took the whole wave, so the three modules beside it were migrated again
    — and one of them, green for an hour, came back `failed` the moment the
    shell it needed went down."""
    row, sale = _prepared(client)
    stock = _named(row.id, "stock_custom")
    _group(client, sale, stock)
    first = OutcomeExecutor({"stock_custom": "failed"}, client.app)
    client.app.state.executor = first
    client.post("/tree/batch/run", data={"batch_group": "1"}, follow_redirects=True)
    client.post("/run/continue", data={"override": "1"}, follow_redirects=True)

    again = OutcomeExecutor({}, client.app)
    client.app.state.executor = again
    client.post("/tree/batch/run", data={"batch_group": "1"}, follow_redirects=True)

    assert first.started == ["sale_custom", "stock_custom"]
    assert again.started == ["stock_custom"]
    with database.connection_context():
        assert Module.get_by_id(sale["id"]).status == "passed"
        assert Module.get_by_id(stock["id"]).status == "passed"


def test_a_batch_with_nothing_left_to_do_says_so_rather_than_redoing_it(client):
    row, sale = _prepared(client)
    stock = _named(row.id, "stock_custom")
    _group(client, sale, stock)
    client.app.state.executor = OutcomeExecutor({}, client.app)
    client.post("/tree/batch/run", data={"batch_group": "1"}, follow_redirects=True)
    client.post("/run/continue", data={"override": "1"}, follow_redirects=True)

    again = OutcomeExecutor({}, client.app)
    client.app.state.executor = again
    page = client.post("/tree/batch/run", data={"batch_group": "1"}, follow_redirects=True)

    assert again.started == []
    # The refusal, not the queue snapshot that also carries the word.
    assert "Every module here has already passed" in page.text


def test_a_step_batch_runs_its_wave_and_not_the_group_with_the_same_number(client):
    """`Batch` recorded a step in `batch_group`, and the runner read that
    column back as a group. Nothing showed because Resolve sets every
    module's group to its own wave — until a human regroups one on Tree,
    which is what that control is for. Here `stock_custom` is moved into
    group 1 while staying on wave 2: a batch on step 1 used to pick it up
    and migrate it before its parent's wave was finished."""
    row, sale = _prepared(client)
    stock = _named(row.id, "stock_custom")
    helper = _named(row.id, "web_enterprise_helper")
    _group(client, stock, group=1)
    runner = OutcomeExecutor({}, client.app)
    client.app.state.executor = runner

    client.post("/tree/batch/run", data={"step": "1"}, follow_redirects=True)

    assert sorted(runner.started) == sorted(
        [sale["technical_name"], helper["technical_name"]]
    )
    with database.connection_context():
        assert Module.get_by_id(stock["id"]).status == "queued"
        batch = Batch.select().where(Batch.migration == row.id).get()

    assert batch.step == 1
    assert batch.batch_group is None


def test_a_result_is_recorded_whole_or_not_at_all(client, monkeypatch):
    """A failure half-way through left the job `passed` and its module
    `running`: the lamp lit for a run nobody was running."""
    from odoo_migrator.jobs import apply_result, start_job
    from odoo_migrator.models import Attempt

    class Silent:
        def start(self, payload):
            pass

    row, sale = _prepared(client)
    job = start_job(
        migration_id=str(row.id), module_id=sale["id"], executor=Silent(), hub=client.app.state.hub
    )

    def broken(**_fields):
        raise RuntimeError("disk full")

    monkeypatch.setattr(Attempt, "create", broken)
    with pytest.raises(RuntimeError, match="disk full"):
        apply_result(
            {
                "job_id": job.job_id,
                "status": "passed",
                "attempts": [{"iteration": 1, "diff": "", "verdict": "pass"}],
            },
            client.app.state.hub,
        )

    with database.connection_context():
        assert Job.get_by_id(job.id).status == "running"
        assert Module.get_by_id(sale["id"]).status == "running"


def test_two_starts_at_once_make_one_job(client, monkeypatch):
    """Two presses of Migrate are two requests on two threads. Both passed
    the in-flight check before either had written its job, and two
    executors ported the same directory; the last answer decided."""
    import threading

    from odoo_migrator import jobs as jobs_module
    from odoo_migrator.jobs import AlreadyRunning, start_job

    class Silent:
        def start(self, payload):
            pass

    row, sale = _prepared(client)
    both = threading.Barrier(2, timeout=5)
    real = jobs_module.build_trigger

    def side_by_side(**fields):
        # Both requests are past the early checks before either writes.
        both.wait()

        return real(**fields)

    monkeypatch.setattr(jobs_module, "build_trigger", side_by_side)
    outcomes = []

    def press():
        try:
            start_job(
                migration_id=str(row.id),
                module_id=sale["id"],
                executor=Silent(),
                hub=client.app.state.hub,
            )
            outcomes.append("started")
        except AlreadyRunning:
            outcomes.append("refused")

    presses = [threading.Thread(target=press) for _ in range(2)]
    for one in presses:
        one.start()
    for one in presses:
        one.join(10)

    assert sorted(outcomes) == ["refused", "started"]
    with database.connection_context():
        assert Job.select().where(Job.module == sale["id"]).count() == 1


def test_a_batch_whose_first_module_cannot_start_leaves_no_batch(client, monkeypatch):
    """A smoke-run still on the module: start_batch wrote its batch, then
    start_job refused the module. The batch stayed `running` with no job in
    it, and every later Run answered that a batch was already running."""
    from odoo_migrator import cartridges
    from odoo_migrator.contract import ContractError
    from odoo_migrator.jobs import start_batch

    row, sale = _prepared(client)
    monkeypatch.setattr(cartridges, "smoke_allows_batch", lambda _migration: True)
    with database.connection_context():
        smoke = Batch.create(migration=row.id, status="smoke")
        Job.create(batch=smoke, module=sale["id"], status="running", job_id="smoke-1")
    step = _named(row.id, "sale_custom")["step"]

    with pytest.raises(ContractError, match="already being migrated"):
        start_batch(
            migration_id=str(row.id),
            executor=OutcomeExecutor({}, client.app),
            hub=client.app.state.hub,
            step=step,
        )

    with database.connection_context():
        assert not Batch.select().where(
            (Batch.migration == row.id) & (Batch.status == "running")
        ).exists()
