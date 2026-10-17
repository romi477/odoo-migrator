"""The stock cartridge, the real runner, one module ported.

The suite was green while the product could not migrate anything: 332
tests passed and `port` had never run once on a live job. Every piece was
covered on its own — the loop, the tools, the providers, lint — and no
test walked `cartridges/stock.yaml` through `NativeRunner` with a step
that fails first, which is what every real run does.
"""

import pytest

from odoo_migrator.db import database
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Batch, Job, Migration, Module, StepRun
from odoo_migrator.runner.context import StepResult
from odoo_migrator.runner.native import NativeRunner

MANIFEST = "{'name': 'Sale Custom', 'version': '18.0.1.0.0', 'depends': ['sale']}"


class StepScript:
    """A provider that answers per step.

    The step's id is in the system prompt — the runner puts it there so the
    model knows which step it is on — and that is what this keys off. It
    calls the same `BoundTool` objects a live provider is handed.
    """

    name = "scripted"

    def __init__(self, fails_once=()):
        self.fails_once = set(fails_once)
        self.ran: list[str] = []
        self.wrote: list[str] = []

    def ensure_ready(self) -> None:

        return None

    def run_step(self, *, system, prompt, tools, report, model, budget, stop_check=None) -> StepResult:
        del prompt, model, budget, stop_check
        step = system.rsplit("Step: ", 1)[1].strip()
        self.ran.append(step)
        if step in self.fails_once:
            self.fails_once.discard(step)

            return StepResult(ok=False, output=f"{step} could not finish")
        if step == "port":
            write = next(tool for tool in tools if tool.name == "write_file")
            for name, body in (("__manifest__.py", MANIFEST), ("models.py", "X = 1\n")):
                self.wrote.append(write(side="target", path=f"project/sale_custom/{name}", content=body))

        return StepResult(ok=True, answer=f"{step} done")


@pytest.fixture
def sandbox(app, tmp_path, fake_sheller):
    """A project whose addons sit in `project/`, as the real one does."""
    old_module = tmp_path / "17" / "project" / "sale_custom"
    old_module.mkdir(parents=True)
    (old_module / "__manifest__.py").write_text(MANIFEST, encoding="utf-8")
    (old_module / "models.py").write_text("X = 0\n", encoding="utf-8")
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
        module = Module.create(
            migration=migration,
            technical_name="sale_custom",
            old_path=str(old_module),
            new_path=str(tmp_path / "18" / "project" / "sale_custom"),
        )
        batch = Batch.create(migration=migration, status="running")
        job = Job.create(batch=batch, module=module, status="queued")

    return job, tmp_path


def _run(app, fake_sheller, sandbox, script):
    job, _ = sandbox
    runner = NativeRunner(sheller=fake_sheller, hub=app.state.hub, provider=script)
    runner.start({"job_id": job.job_id})
    runner.wait(job.job_id, timeout=30)

    return job


def test_the_stock_flow_ports_a_module(app, fake_sheller, sandbox):
    script = StepScript()

    job = _run(app, fake_sheller, sandbox, script)
    _, tmp_path = sandbox

    assert "port" in script.ran
    assert (tmp_path / "18" / "project" / "sale_custom" / "__manifest__.py").is_file()
    with database.connection_context():
        assert Job.get_by_id(job.id).status == "passed"


def test_a_first_step_that_fails_does_not_cost_the_port(app, fake_sheller, sandbox):
    """`resume_at` is `lint`, which sits after `port`. An `understand` that
    failed used to send the run to a lint of a module nobody had written,
    and every later lap failed the same way."""
    script = StepScript(fails_once=["understand"])

    job = _run(app, fake_sheller, sandbox, script)
    _, tmp_path = sandbox

    assert script.ran[:2] == ["understand", "diagnose"]
    assert "port" in script.ran
    assert (tmp_path / "18" / "project" / "sale_custom" / "models.py").read_text() == "X = 1\n"
    with database.connection_context():
        assert Job.get_by_id(job.id).status == "passed"


def test_the_written_files_are_recorded_as_a_diff(app, fake_sheller, sandbox):
    job = _run(app, fake_sheller, sandbox, StepScript())

    with database.connection_context():
        assert "models.py" in (Job.get_by_id(job.id).file_diffs or "")


def test_every_step_is_recorded_against_the_job(app, fake_sheller, sandbox):
    job = _run(app, fake_sheller, sandbox, StepScript())

    with database.connection_context():
        steps = [row.step_id for row in StepRun.select().where(StepRun.job == job.id)]

    assert steps[:3] == ["understand", "port", "lint"]
    assert "verdict" in steps


def test_a_module_the_port_never_writes_fails_without_looping_for_ever(
    app, fake_sheller, sandbox
):
    """`port` reports success and writes nothing, so lint keeps failing and
    the lane keeps not fixing it. That is a stop, not five laps."""

    class Idle(StepScript):
        def run_step(self, **kwargs):
            step = kwargs["system"].rsplit("Step: ", 1)[1].strip()
            self.ran.append(step)

            return StepResult(ok=True, answer="nothing to do")

    script = Idle()
    job = _run(app, fake_sheller, sandbox, script)

    assert script.ran.count("lint") <= 2
    with database.connection_context():
        assert Job.get_by_id(job.id).status == "failed"
