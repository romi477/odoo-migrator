"""A job fetches the shell notes when it starts, keeps them, and records them."""

import hashlib

import pytest

from odoo_migrator import runlog, shell_guidance
from odoo_migrator.db import database
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Batch, Job, Migration, Module
from odoo_migrator.runner.context import StepResult
from odoo_migrator.runner.native import NativeRunner
from odoo_migrator.sheller import ShellerError

MANIFEST = "{'name': 'Sale Custom', 'version': '18.0.1.0.0', 'depends': ['sale']}"


@pytest.fixture(autouse=True)
def fresh():
    shell_guidance.forget()
    yield
    shell_guidance.forget()


@pytest.fixture
def job(app, tmp_path, fake_sheller):
    old_module = tmp_path / "17" / "project" / "sale_custom"
    old_module.mkdir(parents=True)
    (old_module / "__manifest__.py").write_text(MANIFEST, encoding="utf-8")
    (tmp_path / "18" / "project").mkdir(parents=True)
    created = create_migration(
        name="Sandbox", accent="violet", source_version="17.0", target_version="18.0",
        old_project_path=str(tmp_path / "17"), new_project_path=str(tmp_path / "18"),
    )
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])
        migration.old_container = "odoo-old"
        migration.new_container = "odoo-new"
        migration.save()
        module = Module.create(
            migration=migration, technical_name="sale_custom",
            old_path=str(old_module),
            new_path=str(tmp_path / "18" / "project" / "sale_custom"),
        )
        batch = Batch.create(migration=migration, status="running")

        return Job.create(batch=batch, module=module, status="queued")


class Capture:
    """Records the standing instruction of every step; writes the port."""

    name = "scripted"

    def __init__(self, during=None):
        self.systems: dict[str, str] = {}
        self.during = during

    def ensure_ready(self) -> None:

        return None

    def run_step(self, *, system, prompt, tools, report, model, budget, stop_check=None):
        step = system.split("Step: ", 1)[1].splitlines()[0].strip()
        self.systems[step] = system
        if self.during and step == "understand":
            self.during()
        if step == "port":
            by_name = {tool.name: tool for tool in tools}
            by_name["write_file"](
                side="target", path="project/sale_custom/__manifest__.py", content=MANIFEST
            )

        return StepResult(ok=True, answer=f"{step} done")


def _run(app, fake_sheller, job, script):
    runner = NativeRunner(sheller=fake_sheller, hub=app.state.hub, provider=script)
    runner.start({"job_id": job.job_id})
    runner.wait(job.job_id, timeout=30)


def _texts(job):

    return [line["text"] for line in runlog.lines(job.job_id)]


def test_every_step_is_briefed_with_the_notes_the_daemon_served(app, fake_sheller, job):
    script = Capture()

    _run(app, fake_sheller, job, script)

    assert set(script.systems) >= {"understand", "port"}
    for system in script.systems.values():
        assert "## What you cannot do, and must not attempt" in system
        assert "{{" not in system
        assert "os_" not in system


def test_the_job_records_what_it_was_told(app, fake_sheller, job):
    notes = shell_guidance.shell_notes(fake_sheller)
    shell_guidance.forget()

    _run(app, fake_sheller, job, Capture())

    with database.connection_context():
        row = Job.get_by_id(job.id)
    assert row.shell_notes_hash == notes.hash
    assert row.shell_notes_hash == hashlib.sha256(notes.text.encode("utf-8")).hexdigest()
    assert row.shell_notes_source == "daemon 1.11.0"


def test_the_log_says_where_the_notes_came_from(app, fake_sheller, job):
    _run(app, fake_sheller, job, Capture())

    line = next(text for text in _texts(job) if text.startswith("shell notes:"))
    assert "daemon 1.11.0" in line
    assert "overlaid: modules, tests" in line
    assert "left out:" in line


def test_a_sheller_upgraded_halfway_does_not_change_a_running_job(app, fake_sheller, job):
    def upgrade():
        fake_sheller.help_topics["orm"] = "## Writing idiomatic ORM code\n\nCHANGED MID-JOB.\n"
        fake_sheller._version = "1.12.0"

    script = Capture(during=upgrade)

    _run(app, fake_sheller, job, script)

    assert "CHANGED MID-JOB" not in script.systems["port"]
    assert script.systems["understand"].split("## This run")[0] == (
        script.systems["port"].split("## This run")[0]
    )


def test_a_daemon_without_the_route_still_briefs_the_agent_and_the_log_warns(
    app, fake_sheller, job
):
    def no_route():
        raise ShellerError("Sheller HTTP 404: Not Found", status=404)

    # A 1.10.0 fake would not open the target at all (a person must switch
    # commit on first); the route is the only thing that differs here.
    fake_sheller.help = no_route
    script = Capture()

    _run(app, fake_sheller, job, script)

    assert "## What you must not attempt" in script.systems["understand"]
    with database.connection_context():
        assert Job.get_by_id(job.id).shell_notes_source == "vendored"
    assert any("no guidance route" in text for text in _texts(job))
