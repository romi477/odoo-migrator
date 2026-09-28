"""A tool call is a skeleton row. Its body comes from the journal, when asked.

The run log on Pipeline lists the calls; opening one fetches the command's
whole record from Sheller. Nothing is stored twice — the daemon allows a
megabyte of stdout per command and keeps the only full copy.
"""

import pytest

from odoo_migrator.db import database
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Batch, Job, Migration, Module, StepRun, ToolCall


@pytest.fixture
def sheller_fake(fake_sheller):

    return fake_sheller


def _job(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    created = create_migration(
        name="Acme",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path=str(old),
        new_project_path=str(new),
    )
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])
        module = Module.create(migration=migration, technical_name="sale_custom")
        batch = Batch.create(migration=migration, status="running")
        job = Job.create(batch=batch, module=module, status="passed")

        return job


@pytest.fixture
def tool_call(client, sheller_fake, tmp_path):
    del client
    opened = sheller_fake.open_session(
        container="odoo-new",
        database="acme",
        odoo_bin="/opt/odoo/odoo-bin",
        owner={"kind": "agent"},
        allow_commit=True,
    )
    sheller_fake.exec(opened["id"], "1", write_key=opened["write_key"])
    job = _job(tmp_path)
    with database.connection_context():
        step = StepRun.create(job=job, attempt=1, step_id="port", kind="agent", ok=True)
        call = ToolCall.create(
            step_run=step,
            tool="exec",
            side="new",
            session_id=opened["id"],
            command_id="1",
            preview="1",
            ok=True,
        )

        return call


@pytest.fixture
def finished_job(client, tmp_path):
    del client
    job = _job(tmp_path)
    with database.connection_context():
        step = StepRun.create(job=job, attempt=1, step_id="port", kind="agent", ok=True)
        ToolCall.create(
            step_run=step,
            tool="exec",
            side="new",
            session_id="sess-new",
            command_id="7",
            preview="env['res.partner'].search([])",
            ok=True,
        )

        return job


def _line(job, call):
    from odoo_migrator.models import RunLog

    with database.connection_context():
        return RunLog.create(
            job=job,
            level="info",
            source="port",
            text=f"{call.tool}({call.side}) 12ms ok: {call.preview}",
            tool_call=call,
        )


def test_a_log_line_opens_the_command_the_journal_kept(client, tool_call):
    with database.connection_context():
        job = tool_call.step_run.job
    line = _line(job, tool_call)

    body = client.get(f"/api/log/{line.id}")

    assert body.status_code == 200
    assert "not available" not in body.text
    assert tool_call.command_id in body.text


def test_a_log_line_survives_a_journal_that_is_gone(client, tool_call, sheller_fake):
    """The journal is the daemon's and can be removed. The line stays."""
    with database.connection_context():
        job = tool_call.step_run.job
    line = _line(job, tool_call)
    sheller_fake.forget_journal(tool_call.session_id)

    body = client.get(f"/api/log/{line.id}")

    assert body.status_code == 200
    assert "not available" in body.text


def test_the_line_stores_no_body_of_its_own(client, tool_call):
    """A `detail` capped at 20k would be a shorter copy of what the daemon
    already keeps whole, and a second place for the customer's source to sit."""
    from odoo_migrator import runlog
    from odoo_migrator.models import RunLog

    del client
    with database.connection_context():
        job = tool_call.step_run.job
    line = _line(job, tool_call)
    with database.connection_context():
        stored = RunLog.get_by_id(line.id)

    assert stored.detail == ""
    # The page still offers to open it: the pointer is what says there is
    # something to read.
    assert runlog.lines(job.job_id)[-1]["has_detail"] is True


def test_the_flow_page_is_the_picture_and_not_a_second_log(client, finished_job):
    """One job was read on two screens. The calls are the run log's."""
    with database.connection_context():
        migration_id = str(finished_job.module.migration_id)
    client.cookies.set("open_migration", migration_id)

    page = client.get("/flow").text

    assert "flow-canvas" in page
    assert "flow-trace" not in page
