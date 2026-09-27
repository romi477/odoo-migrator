"""The run log: why a step ended, written down and readable afterwards.

A run that stopped after two attempts left two red rows and nothing else.
The reason each step ended was computed by the provider, returned in
`StepResult.output`, and dropped on the floor.
"""

import pytest

from odoo_migrator import runlog
from odoo_migrator.db import database
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Batch, Job, Migration, Module, RunLog, StepRun
from odoo_migrator.runner.context import StepResult
from odoo_migrator.runner.native import NativeRunner


@pytest.fixture
def job(client, tmp_path):
    del client
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

        return Job.create(batch=batch, module=module, status="running")


def test_a_line_is_stored_and_streamed(app, job):
    seen = []
    app.state.hub.publish = lambda event, data: seen.append((event, data))

    runlog.write(job.job_id, source="port", text="agent step started", hub=app.state.hub)

    lines = runlog.lines(job.job_id)

    assert lines[0]["source"] == "port"
    assert lines[0]["text"] == "agent step started"
    assert seen[0][0] == "log"
    assert seen[0][1]["job_id"] == job.job_id


def test_a_body_is_kept_apart_from_the_line(app, job):
    runlog.write(
        job.job_id,
        source="port",
        text="failed — the agent finished without calling report",
        detail="status: completed\nagent said: I would need the file first.",
    )
    line = runlog.lines(job.job_id)[0]

    assert line["has_detail"]
    assert "agent said" not in line["text"]
    assert "agent said" in runlog.detail_of(line["id"])


def test_the_page_fetches_a_body_only_when_asked(client, app, job):
    runlog.write(job.job_id, source="port", text="failed", detail="the whole prompt")
    line_id = runlog.lines(job.job_id)[0]["id"]

    listing = client.get(f"/api/jobs/{job.job_id}/log").json()

    assert listing["lines"][0]["has_detail"]
    assert "the whole prompt" not in str(listing)
    assert client.get(f"/api/log/{line_id}").text == "the whole prompt"


def test_after_returns_only_what_the_page_does_not_have(client, job):
    runlog.write(job.job_id, source="a", text="one")
    first = runlog.lines(job.job_id)[0]["id"]
    runlog.write(job.job_id, source="b", text="two")

    fresh = client.get(f"/api/jobs/{job.job_id}/log?after={first}").json()["lines"]

    assert [line["text"] for line in fresh] == ["two"]


def test_a_missing_body_is_a_404_not_a_blank_panel(client):

    assert client.get("/api/log/9999").status_code == 404


def test_writing_a_line_never_takes_the_run_down(app):
    """A log that can fail the job it describes is worse than no log."""

    runlog.write("no-such-job", source="port", text="x", hub=app.state.hub)


def test_a_step_keeps_the_reason_it_ended(app, job, fake_sheller):
    with database.connection_context():
        step_run = StepRun.create(job=job, attempt=1, step_id="port", kind="agent")

    runner = NativeRunner(sheller=fake_sheller, hub=app.state.hub)
    runner._close_step(
        step_run.id,
        StepResult(ok=False, output="the agent finished without calling report"),
    )

    with database.connection_context():
        assert "without calling report" in StepRun.get_by_id(step_run.id).output


def test_a_long_body_is_clipped_rather_than_stored_whole(job):
    runlog.write(job.job_id, source="port", text="x", detail="y" * (runlog.MAX_DETAIL * 2))
    line_id = runlog.lines(job.job_id)[0]["id"]

    assert len(runlog.detail_of(line_id)) < runlog.MAX_DETAIL * 2


def test_a_run_writes_a_log_a_person_can_follow(app, tmp_path, fake_sheller):
    """The whole point: after a job, the log says what happened."""
    from test_stock_flow_end_to_end import StepScript

    old_module = tmp_path / "17" / "project" / "sale_custom"
    old_module.mkdir(parents=True)
    (old_module / "__manifest__.py").write_text(
        "{'name': 'x', 'depends': []}", encoding="utf-8"
    )
    (tmp_path / "18" / "project").mkdir(parents=True)
    created = create_migration(
        name="Sandbox",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path=str(tmp_path / "17"),
        new_project_path=str(tmp_path / "18"),
    )
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])
        migration.old_container = "odoo-old"
        migration.new_container = "odoo-new"
        migration.save()
        module = Module.create(migration=migration, technical_name="sale_custom")
        batch = Batch.create(migration=migration, status="running")
        started = Job.create(batch=batch, module=module, status="queued")

    runner = NativeRunner(
        sheller=fake_sheller, hub=app.state.hub, provider=StepScript(fails_once=["understand"])
    )
    runner.start({"job_id": started.job_id})
    runner.wait(started.job_id, timeout=30)

    lines = runlog.lines(started.job_id)
    text = "\n".join(f"{line['source']}: {line['text']}" for line in lines)

    assert "job started" in text
    assert "cartridge stock 1.0.0" in text
    assert "odoo-old/" in text
    assert "sessions open:" in text
    assert "understand: failed" in text
    assert "attempt 2" in text
    assert "job passed" in text


def test_the_log_survives_the_job_it_describes(client, app, tmp_path, fake_sheller):
    """A run that failed two minutes ago is the one someone wants to read."""
    from odoo_migrator.jobs import recent_job

    del app, fake_sheller
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
        module = Module.create(migration=migration, technical_name="sale_custom")
        batch = Batch.create(migration=migration, status="running")
        done = Job.create(batch=batch, module=module, status="failed")
    runlog.write(done.job_id, source="runner", text="job failed")

    shown = recent_job(created["id"])

    assert shown["job_id"] == done.job_id
    assert shown["live"] is False
    assert client.get(f"/api/jobs/{done.job_id}/log").json()["lines"]


def test_the_pipeline_page_carries_the_log_panel(client, tmp_path):
    del tmp_path
    from test_run_workspace import _open

    _open(client)

    page = client.get("/run").text

    assert "data-run-log-panel" in page
    assert "/static/runlog.js" in page


def test_nothing_is_logged_against_an_unknown_job():

    assert runlog.lines("no-such-job") == []
    assert RunLog.select().count() >= 0


def test_clearing_drops_the_lines_and_keeps_the_job(client, job):
    runlog.write(job.job_id, source="runner", text="job started")
    runlog.write(job.job_id, source="port", text="agent step started")

    gone = client.request("DELETE", f"/api/jobs/{job.job_id}/log").json()

    assert gone["deleted"] == 2
    assert runlog.lines(job.job_id) == []
    with database.connection_context():
        assert Job.get_by_id(job.id).status == "running"


def test_clearing_an_unknown_job_is_not_an_error(client):

    assert client.request("DELETE", "/api/jobs/no-such-job/log").json()["deleted"] == 0


def test_the_panel_offers_copy_and_clear(client):
    from test_run_workspace import _open

    _open(client)

    page = client.get("/run").text

    assert "data-run-log-copy" in page
    assert "data-run-log-body-copy" in page
    assert "data-run-log-clear" in page
    assert "data-run-log-body-close" in page
    # The glyph is an include, so a missing template is a 500 and not a
    # silently empty button.
    assert "copy-glyph" in page


def test_the_copy_button_is_on_the_component_catalogue(client):
    page = client.get("/components").text

    assert "copy-button" in page
    assert "run-log-body-panel" in page


def test_the_panel_names_the_job_so_it_can_be_referred_to(client, tmp_path):
    """A log that cannot be named is one nobody can point at."""
    from test_run_workspace import _open

    del tmp_path
    _open(client)

    page = client.get("/run").text

    assert "data-run-log-id" in page or "No job has run" in page


def test_the_job_id_is_shown_and_copyable(client, app, tmp_path, fake_sheller):
    from odoo_migrator.jobs import recent_job

    del app, fake_sheller
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
        module = Module.create(migration=migration, technical_name="sale_custom")
        batch = Batch.create(migration=migration, status="running")
        done = Job.create(batch=batch, module=module, status="failed")
    client.post(f"/migrations/{created['id']}/open", follow_redirects=False)

    page = client.get("/run").text

    assert recent_job(created["id"])["job_id"] == done.job_id
    assert f'data-run-log-id="{done.job_id}"' in page
    # Short in the button, whole in the title and on the clipboard.
    assert done.job_id[:8] in page
