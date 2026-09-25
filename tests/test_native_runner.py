"""The native runner records every tool call on its own thread."""

import time

import pytest
from doubles.model import ModelDouble

from odoo_migrator.db import database
from odoo_migrator.jobs import answer_job
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Batch, Job, Migration, Module, ToolCall
from odoo_migrator.runner.native import NativeRunner

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
    runner = NativeRunner(sheller=fake_sheller, hub=app.state.hub, client=model)
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
    runner = NativeRunner(sheller=fake_sheller, hub=app.state.hub, client=model)
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
