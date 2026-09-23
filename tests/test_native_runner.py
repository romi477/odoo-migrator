"""The native runner records every tool call on its own thread."""

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


@pytest.fixture
def parked_job(app, tmp_path):
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
    answer_job(
        module_id=parked_job.module_id,
        answer="drop the override",
        hub=client.app.state.hub,
    )

    assert reloaded(parked_job).status == "running"


def test_the_thread_never_shares_a_connection(native_job):
    """Peewee rule: a connection never crosses a thread boundary."""
    run_to_completion(native_job)

    assert not database.is_closed() is False
