"""The trace is a skeleton. The journal body is fetched only when asked."""

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


def test_a_trace_line_survives_a_journal_that_is_gone(client, tool_call, sheller_fake):
    sheller_fake.forget_journal(tool_call.session_id)

    body = client.get(f"/api/flow/trace/{tool_call.id}/body")

    assert body.status_code == 200
    assert "not available" in body.text


def test_the_trace_is_the_skeleton_and_not_the_bodies(client, finished_job):
    rows = client.get(f"/api/flow/{finished_job.job_id}/trace").json()

    assert rows[0]["session_id"]
    assert len(rows[0]["preview"]) <= 120
    assert "stdout" not in rows[0]


def test_a_trace_row_says_which_step_and_lap_made_it(client, finished_job):
    """Attempt after attempt ran into the next with nothing between them."""
    rows = client.get(f"/api/flow/{finished_job.job_id}/trace").json()

    assert rows[0]["step"] == "port"
    assert rows[0]["attempt"] == 1


def test_the_flow_page_names_the_job_it_traces(client, finished_job):
    """The page takes the newest job on the open migration. Named nowhere,
    the list was "whatever ran last" once a batch had run."""
    with database.connection_context():
        migration_id = str(finished_job.module.migration_id)
    client.cookies.set("open_migration", migration_id)

    page = client.get("/flow").text

    assert "sale_custom" in page
    assert finished_job.job_id[:8] in page
    assert "attempt" in page


def test_the_flow_page_says_when_nothing_has_run(client, tmp_path):
    created = create_migration(
        name="Empty",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path=str(tmp_path / "o"),
        new_project_path=str(tmp_path / "n"),
    )
    client.cookies.set("open_migration", created["id"])

    page = client.get("/flow").text

    assert "No job has run on this migration yet." in page
