"""The native runner records every tool call on its own thread."""

import threading
import time

import pytest
from doubles.model import ModelDouble

from odoo_migrator.db import database
from odoo_migrator.events import EventHub
from odoo_migrator.jobs import answer_job
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Batch, Job, Migration, Module, ToolCall
from odoo_migrator.runner.native import NativeRunner
from odoo_migrator.runner.providers.scripted import ScriptedProvider

_CARTRIDGE = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-opus-5}
pipeline:
  - step: agent
    id: port
    sessions: [new]
    tools: [exec, reopen]
    prompt: port
on_failure:
  pipeline: []
  retry:
    resume_at: port
"""


def _module(tmp_path):
    old_mod = tmp_path / "old" / "sale_custom"
    new_mod = tmp_path / "new" / "sale_custom"
    old_mod.mkdir(parents=True)
    new_mod.mkdir(parents=True)
    created = create_migration(
        name="Acme",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path=str(tmp_path / "old"),
        new_project_path=str(tmp_path / "new"),
    )
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])
        # The runner probes both containers for the database and odoo_bin, so
        # a migration whose containers are not up is a loud refusal.
        migration.old_container = "odoo-old"
        migration.new_container = "odoo-new"
        migration.save()
        module = Module.create(
            migration=migration,
            technical_name="sale_custom",
            old_path=str(old_mod),
            new_path=str(new_mod),
        )
        batch = Batch.create(migration=migration, status="running")
        job = Job.create(batch=batch, module=module, status="queued")

        return job


@pytest.fixture
def native_job(app, fake_sheller, tmp_path):
    job = _module(tmp_path)
    model = ModelDouble(
        turns=[
            {"name": "exec", "input": {"side": "new", "code": "1"}},
            {"name": "reopen", "input": {"side": "new"}},
            {"name": "exec", "input": {"side": "new", "code": "2"}},
            {"name": "report", "input": {"answer": "ported"}},
        ]
    )
    runner = NativeRunner(sheller=fake_sheller, hub=app.state.hub, provider=ScriptedProvider(model))
    runner.start({"job_id": job.job_id, "cartridge_text": _CARTRIDGE})

    return job, runner


_ASKS = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-opus-5}
pipeline:
  - step: agent
    id: diagnose
    sessions: [new]
    tools: [exec]
    raises: [needs_human]
    prompt: diagnose
  - step: ask_human
    when: needs_human
on_failure:
  pipeline: []
  retry:
    resume_at: diagnose
"""


@pytest.fixture
def parked_job(app, fake_sheller, tmp_path):
    """Parked by the runner itself, so a thread is really waiting on it."""
    job = _module(tmp_path)
    model = ModelDouble(
        turns=[
            {"name": "report", "input": {"answer": "which override?", "needs_human": True}},
            {"name": "report", "input": {"answer": "done"}},
        ]
    )
    runner = NativeRunner(sheller=fake_sheller, hub=app.state.hub, provider=ScriptedProvider(model))
    runner.start({"job_id": job.job_id, "cartridge_text": _ASKS})
    for _ in range(200):
        if reloaded(job).status == "waiting_for_human":
            break
        time.sleep(0.01)

    return job, runner


@pytest.fixture
def abandoned_job(app, tmp_path):
    """Parked with nothing waiting: the app restarted while a human thought."""
    del app
    job = _module(tmp_path)
    with database.connection_context():
        job.status = "waiting_for_human"
        job.question = "Which override do we drop?"
        job.save()

    return job


def run_to_completion(native_job):
    job, runner = native_job
    runner.wait(job.job_id)


def reloaded(job):
    with database.connection_context():

        return Job.get_by_id(job.id)


def test_a_run_records_the_session_of_every_tool_call(client, native_job):
    """A trace line finds its journal entry by session id and command id.

    A reopen mid-step makes a new session, so this cannot be stored per step.
    """
    del client
    run_to_completion(native_job)

    with database.connection_context():
        calls = list(ToolCall.select().order_by(ToolCall.id))

    new_side = {c.session_id for c in calls if c.side == "new"}
    assert new_side and "" not in new_side
    assert len(new_side) >= 2
    assert all(c.command_id for c in calls if c.tool == "exec")


def test_an_answer_resumes_a_parked_job(client, parked_job):
    job, runner = parked_job

    answer_job(
        module_id=job.module_id,
        answer="drop the override",
        hub=client.app.state.hub,
        resume=runner.resume,
    )
    runner.wait(job.job_id)

    assert reloaded(job).status == "passed"


def test_the_live_app_hands_the_runner_a_sheller_client(tmp_path, monkeypatch):
    """create_app() is how the process starts: no client is injected."""
    from odoo_migrator.api import create_app
    from odoo_migrator.sheller import HttpShellerClient

    monkeypatch.setenv("ODOO_MIGRATOR_RUNNER", "native")
    app = create_app(tmp_path / "native.sqlite")

    assert isinstance(app.state.executor, NativeRunner)
    assert isinstance(app.state.executor.sheller, HttpShellerClient)


def test_an_answer_to_a_job_whose_runner_is_gone_does_not_pretend(client, abandoned_job):
    """The loop lived in a thread, and a restart took it. Saying "running"
    would be a row nobody is running."""
    runner = NativeRunner(sheller=None, hub=client.app.state.hub)

    answered = answer_job(
        module_id=abandoned_job.module_id,
        answer="drop the override",
        hub=client.app.state.hub,
        resume=runner.resume,
    )

    assert answered.answer == "drop the override"
    assert reloaded(abandoned_job).status == "unknown"
    # The batch it belonged to is not running either. `reconcile_jobs` spares
    # a batch whose job is parked, because a person still owes it an answer;
    # once that answer has nowhere to go, a batch left `running` blocks every
    # later one on the migration and no screen can clear it.
    with database.connection_context():
        assert Batch.get_by_id(abandoned_job.batch_id).status == "unknown"


def test_an_answer_that_resumes_leaves_its_batch_running(client, parked_job):
    """The runner is still on it; the batch is not over."""
    job, runner = parked_job

    answer_job(
        module_id=job.module_id,
        answer="drop the override",
        hub=client.app.state.hub,
        resume=runner.resume,
    )
    runner.wait(job.job_id)

    with database.connection_context():
        assert Batch.get_by_id(job.batch_id).status == "running"


def test_the_thread_never_shares_a_connection(native_job):
    """Peewee rule: a connection never crosses a thread boundary."""
    run_to_completion(native_job)

    assert not database.is_closed() is False


def test_a_missing_target_database_is_refused_before_a_session(app, tmp_path):
    job = _module(tmp_path)

    class Probe:
        def probe(self, container):
            return {
                "db_name": "mig_test_18",
                "odoo_bin": "/opt/odoo/odoo-bin",
                "databases": ["some_other_db"],
            }

        def open_session(self, **kwargs):
            raise AssertionError(kwargs)

    runner = NativeRunner(sheller=Probe(), hub=app.state.hub)
    with database.connection_context():
        module = Module.get_by_id(job.module_id)
        migration = Migration.get_by_id(module.migration_id)

    with pytest.raises(RuntimeError, match="mig_test_18"):
        runner._targets(migration)


def test_the_runner_asks_the_daemon_where_each_side_runs(client, native_job, fake_sheller):
    """The database and odoo_bin used to be two constants typed into the runner."""
    del client
    run_to_completion(native_job)

    assert [opened["container"] for opened in fake_sheller.opens][:2] == [
        "odoo-old",
        "odoo-new",
    ]
    assert {opened["database"] for opened in fake_sheller.opens} == {"acme_17"}
    assert {opened["odoo_bin"] for opened in fake_sheller.opens} == {"/opt/odoo/odoo-bin"}


def test_the_source_side_never_gets_commit(client, native_job, fake_sheller):
    del client
    run_to_completion(native_job)

    by_container = {opened["container"]: opened for opened in fake_sheller.opens}
    assert by_container["odoo-old"]["allow_commit"] is False
    assert by_container["odoo-new"]["allow_commit"] is True


def test_a_finished_job_leaves_no_session_behind(client, native_job, fake_sheller):
    """Two odoo-bin processes per job, held forever, is how a machine dies."""
    del client
    run_to_completion(native_job)

    opened = {row["id"] for row in fake_sheller.opens}
    assert opened
    assert opened <= set(fake_sheller.closes)


def test_the_log_names_the_model_that_will_run(client, tmp_path):
    """The line read to know what a run is about to spend printed the
    cartridge's own model. A card overriding Opus with Cursor still said
    Opus — the one thing someone watching this line is trying to avoid."""
    from odoo_migrator.runner.cartridge import load_cartridge
    from odoo_migrator.runner.context import StepContext
    from odoo_migrator.runner.native import _model_line

    del client
    job = _module(tmp_path)
    with database.connection_context():
        module = Module.get_by_id(job.module_id)
        migration = Migration.get_by_id(module.migration_id)
        migration.provider_override = "cursor"
        migration.model_override = "composer-2.5"
        migration.save()
    cartridge = load_cartridge(_CARTRIDGE)
    ctx = StepContext(
        job_id=job.job_id,
        attempt=1,
        migration=migration,
        module=module,
        cartridge=cartridge,
        sheller=None,
        sessions={},
        emit=lambda _event: None,
    )

    line = _model_line(cartridge, ctx)

    assert line.startswith("cursor/composer-2.5")
    assert "anthropic/claude-opus-5" in line, "the file's own choice is worth saying"

    migration.provider_override = ""
    migration.model_override = ""

    assert _model_line(cartridge, ctx) == "anthropic/claude-opus-5"


def test_a_stopped_run_ends_at_its_next_step(client, tmp_path):
    """Stop is not a failure of the module: nothing was found wrong with it.
    The step in flight finishes — a model turn cannot be called back — and
    the job ends before the next one opens."""
    from odoo_migrator.jobs import stop_job
    from odoo_migrator.runner.native import NativeRunner, stopping

    del client
    job = _module(tmp_path)
    runner = NativeRunner(sheller=None, hub=EventHub())
    runner._threads[job.job_id] = threading.current_thread()

    asked = stop_job(module_id=job.module_id, hub=runner.hub, stop=runner.stop)

    assert asked is True
    assert stopping(job.job_id) is True
    assert reloaded(job).status != "failed"


def test_a_stopped_run_closes_its_batch_and_keeps_its_diff(app, tmp_path):
    """The stop used to write `stopped` on the job and nothing else. The
    batch stayed `running` for ever, so Run refused every later start with
    "A batch is already running", and the diff the run had written was on
    disk but in no attempt anyone could read."""
    from odoo_migrator.models import Attempt
    from odoo_migrator.pipeline_queue import _batch_block_reason

    job = _module(tmp_path)
    with database.connection_context():
        row = Job.get_by_id(job.id)
        row.status = "running"
        row.attempt = 1
        row.file_diffs = '{"1": "--- a/models.py\\n+++ b/models.py\\n"}'
        row.save()
    runner = NativeRunner(sheller=None, hub=app.state.hub, result=app.state.on_result)

    runner._stopped(job.job_id, "before probe", attempt=1)

    assert reloaded(job).status == "stopped"
    with database.connection_context():
        assert Module.get_by_id(job.module_id).status == "stopped"
        assert Batch.get_by_id(job.batch_id).status == "checkpoint"
        diffs = [attempt.diff for attempt in Attempt.select().where(Attempt.job == job.id)]
    assert diffs == ["--- a/models.py\n+++ b/models.py\n"]
    migration_id = str(job.module.migration_id)
    assert _batch_block_reason(migration_id) == "Review the last batch before starting another."


def test_a_late_progress_does_not_bring_a_stopped_job_back(app, tmp_path):
    from odoo_migrator.jobs import apply_progress

    job = _module(tmp_path)
    with database.connection_context():
        Job.update(status="stopped").where(Job.id == job.id).execute()

    apply_progress({"job_id": job.job_id, "stage": "port", "attempt": 1}, app.state.hub)

    assert reloaded(job).status == "stopped"


def test_stopping_a_job_no_thread_is_running_does_not_pretend(client, tmp_path):
    """The same lie `answer_job` refuses: a restart took the thread with it."""
    from odoo_migrator.jobs import stop_job
    from odoo_migrator.runner.native import NativeRunner

    del client
    job = _module(tmp_path)
    runner = NativeRunner(sheller=None, hub=EventHub())

    asked = stop_job(module_id=job.module_id, hub=runner.hub, stop=runner.stop)

    assert asked is False
    assert reloaded(job).status == "unknown"


def test_stopping_a_module_that_is_not_running_is_refused(client, tmp_path):
    from odoo_migrator.contract import ContractError
    from odoo_migrator.jobs import stop_job
    from odoo_migrator.models import Job

    del client
    job = _module(tmp_path)
    with database.connection_context():
        row = Job.get_by_id(job.id)
        row.status = "passed"
        row.save()

    with pytest.raises(ContractError, match="not running"):
        stop_job(module_id=job.module_id, hub=EventHub(), stop=lambda _id: True)


def test_stop_reaches_a_job_parked_on_a_person(client, parked_job):
    """A parked thread waits for an answer with no timeout. Stop set a flag
    it never read: the job stayed parked until someone answered it, and only
    then did the next step see the stop."""
    del client
    job, runner = parked_job
    assert reloaded(job).status == "waiting_for_human"

    assert runner.stop(job.job_id) is True
    runner.wait(job.job_id, timeout=5)

    assert reloaded(job).status == "stopped"
    with database.connection_context():
        verdicts = [row.verdict for row in Job.get_by_id(job.id).attempts]
    assert verdicts == ["stopped while waiting for an answer"]


def test_stop_reaches_a_job_waiting_for_its_database(app, fake_sheller, tmp_path):
    """Another job migrating on the same database holds it, and this one
    waited for the lock with no end. Stop had to wait for that other job."""
    from odoo_migrator import runlog
    from odoo_migrator.runner import native

    job = _module(tmp_path)
    held = native._target_lock_for("odoo-new", "acme_17")
    held.acquire()
    try:
        model = ModelDouble(turns=[{"name": "report", "input": {"answer": "ported"}}])
        runner = NativeRunner(
            sheller=fake_sheller, hub=app.state.hub, provider=ScriptedProvider(model)
        )
        runner.start({"job_id": job.job_id, "cartridge_text": _CARTRIDGE})
        for _ in range(300):
            if any("another job is migrating" in line["text"] for line in runlog.lines(job.job_id)):
                break
            time.sleep(0.01)

        runner.stop(job.job_id)
        runner.wait(job.job_id, timeout=5)
    finally:
        held.release()

    assert reloaded(job).status == "stopped"


def test_a_result_that_cannot_be_recorded_does_not_strand_its_batch(app, fake_sheller, tmp_path):
    """The result path writes the outcome, advances the batch and can start
    the next module, all in this thread. An exception there ended the thread
    with nothing said: no closing line, no `job_finished`, and a batch left
    `running` that refused every later Run. It ends the way a restart leaves
    a job instead: `unknown`, and said so."""
    from odoo_migrator import runlog
    from odoo_migrator.models import Module

    job = _module(tmp_path)

    def unrecordable(body):
        raise RuntimeError("database is locked")

    model = ModelDouble(turns=[{"name": "report", "input": {"answer": "ported"}}])
    hub = app.state.hub
    heard = hub.subscribe()
    runner = NativeRunner(
        sheller=fake_sheller, hub=hub, provider=ScriptedProvider(model), result=unrecordable
    )
    runner.start({"job_id": job.job_id, "cartridge_text": _CARTRIDGE})

    with pytest.raises(RuntimeError, match="database is locked"):
        runner.wait(job.job_id)

    assert reloaded(job).status == "unknown"
    with database.connection_context():
        assert Module.get_by_id(job.module_id).status == "unknown"
        assert Batch.get_by_id(job.batch_id).status == "unknown"
    texts = [line["text"] for line in runlog.lines(job.job_id)]
    assert any("could not be recorded" in text for text in texts)
    events = []
    while not heard.empty():
        events.append(heard.get_nowait())
    assert any(name == "job_finished" for name, _data in events)


def test_stopping_a_job_whose_thread_has_ended_does_not_pretend(client, tmp_path):
    """A thread that died is still in the runner's table. Stop said
    "stopping" about it, and the row stayed in flight for ever."""
    from odoo_migrator.jobs import stop_job

    del client
    job = _module(tmp_path)
    runner = NativeRunner(sheller=None, hub=EventHub())
    ended = threading.Thread(target=lambda: None)
    ended.start()
    ended.join()
    runner._threads[job.job_id] = ended

    asked = stop_job(module_id=job.module_id, hub=runner.hub, stop=runner.stop)

    assert asked is False
    assert reloaded(job).status == "unknown"


def test_the_runner_forgets_old_finished_jobs():
    """Every job's thread and error stayed for the life of the process, and
    an error keeps the frames of the step that raised it — the context with
    the module's whole source among them."""
    from odoo_migrator.runner.native import _KEPT_FINISHED

    runner = NativeRunner(sheller=None, hub=EventHub())
    for number in range(_KEPT_FINISHED + 5):
        ended = threading.Thread(target=lambda: None)
        ended.start()
        ended.join()
        runner._threads[f"job-{number}"] = ended
        runner._errors[f"job-{number}"] = RuntimeError("x")

    runner._forget_finished()

    kept = list(runner._threads)
    assert len(kept) == _KEPT_FINISHED
    assert kept[-1] == f"job-{_KEPT_FINISHED + 4}"
    assert set(runner._errors) == set(kept)


def test_an_answer_does_not_undo_what_the_resumed_run_already_recorded(client, abandoned_job):
    """The answer woke the runner, then wrote `running` on the job. A run
    that finished in between — or parked again — went back to `running`,
    with nothing left to send it a result."""
    from odoo_migrator.jobs import apply_result

    hub = client.app.state.hub

    def resume_and_finish(job_id, answer):
        del answer
        apply_result({"job_id": job_id, "status": "passed"}, hub)

        return True

    answer_job(
        module_id=abandoned_job.module_id, answer="drop it", hub=hub, resume=resume_and_finish
    )

    assert reloaded(abandoned_job).status == "passed"
    with database.connection_context():
        assert Module.get_by_id(abandoned_job.module_id).status == "passed"
