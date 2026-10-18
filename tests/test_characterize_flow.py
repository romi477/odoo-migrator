"""characterize: a test written on the source, proved there, ported with the module."""

import shutil
import subprocess
from pathlib import Path

import pytest

from odoo_migrator.cartridges import install_from_text
from odoo_migrator.db import database
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Batch, Job, Migration, Module
from odoo_migrator.runner.cartridge import load_cartridge_file
from odoo_migrator.runner.context import StepResult
from odoo_migrator.runner.native import NativeRunner
from odoo_migrator.runner.tools import ToolRefused

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

STOCK = Path("cartridges/stock.yaml").read_text(encoding="utf-8")
MANIFEST = "{'name': 'Sale Custom', 'version': '18.0.1.0.0', 'depends': ['sale']}"
TEST = (
    "from odoo.tests import TransactionCase\n\n\n"
    "class TestX(TransactionCase):\n    def test_x(self):\n        self.assertTrue(True)\n"
)


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


def make_repo(source):
    git(source, "init", "-q", "-b", "main")
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "seed")

    return git(source, "rev-parse", "main")


@pytest.fixture
def sandbox(app, tmp_path, fake_sheller):
    """A project whose addons sit in `project/`, as the real one does."""
    old_module = tmp_path / "17" / "project" / "sale_custom"
    old_module.mkdir(parents=True)
    (old_module / "__manifest__.py").write_text(MANIFEST, encoding="utf-8")
    (old_module / "models.py").write_text("X = 0\n", encoding="utf-8")
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
        job = Job.create(batch=batch, module=module, status="queued")

    return job, tmp_path


def _run(app, fake_sheller, sandbox, script):
    job, _ = sandbox
    runner = NativeRunner(sheller=fake_sheller, hub=app.state.hub, provider=script)
    runner.start({"job_id": job.job_id})
    runner.wait(job.job_id, timeout=30)

    return job


class CharacterizeScript:
    """`understand` raises (or not) `needs_tests`; `characterize` writes and runs."""

    name = "scripted"

    def __init__(self, *, raise_needs_tests=True, run_it=True, carry_tests=True):
        self.raise_needs_tests = raise_needs_tests
        self.run_it = run_it
        self.carry_tests = carry_tests
        self.ran: list[str] = []
        self.port_prompt = ""
        self.refusal = ""

    def ensure_ready(self) -> None:

        return None

    def run_step(self, *, system, prompt, tools, report, model, budget, stop_check=None):
        step = system.split("Step: ", 1)[1].splitlines()[0].strip()
        self.ran.append(step)
        by_name = {tool.name: tool for tool in tools}
        flags = set()
        if step == "understand" and self.raise_needs_tests:
            flags.add("needs_tests")
        if step == "characterize":
            try:
                for name, body in (("__init__.py", "from . import test_x\n"), ("test_x.py", TEST)):
                    by_name["write_source_test"](
                        side="source", path=f"project/sale_custom/tests/{name}", content=body
                    )
            except ToolRefused as exc:
                # The provider loop hands a refusal back to the model as text,
                # and a model that cannot write ends the step.
                self.refusal = str(exc)

                return StepResult(ok=True, answer="could not write a test")
            if self.run_it:
                by_name["run_test"](side="source", test="sale_custom.TestX")
        if step == "port":
            self.port_prompt = prompt
            by_name["write_file"](
                side="target", path="project/sale_custom/__manifest__.py", content=MANIFEST
            )
            if self.carry_tests and "### tests/test_x.py" in prompt:
                for name, body in (("__init__.py", "from . import test_x\n"), ("test_x.py", TEST)):
                    by_name["write_file"](
                        side="target", path=f"project/sale_custom/tests/{name}", content=body
                    )

        return StepResult(ok=True, answer=f"{step} done", flags=flags)


def _source(sandbox):
    _, tmp_path = sandbox

    return tmp_path / "17"


def _status(job):
    with database.connection_context():

        return Job.get_by_id(job.id).status


def test_a_testless_module_gets_a_green_test_on_a_branch_and_the_port_sees_it(
    app, fake_sheller, sandbox
):
    source = _source(sandbox)
    seed = make_repo(source)
    fake_sheller.tests_for(
        "sale_custom", fake_sheller.catalogue("sale_custom", {"TestX": ["test_x"]})
    )
    script = CharacterizeScript()

    job = _run(app, fake_sheller, sandbox, script)

    assert script.ran.index("characterize") < script.ran.index("port")
    assert git(source, "branch", "--show-current").startswith("migrator/sandbox-")
    assert git(source, "rev-parse", "main") == seed
    committed = git(source, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert "project/sale_custom/tests/test_x.py" in committed
    assert "[sheller:" in git(source, "log", "-1", "--format=%s")
    assert "Sheller-session: " in git(source, "log", "-1", "--format=%B")
    assert "### tests/test_x.py" in script.port_prompt
    assert _status(job) == "passed"


def test_a_module_that_needs_no_tests_skips_characterize(app, fake_sheller, sandbox):
    source = _source(sandbox)
    make_repo(source)
    script = CharacterizeScript(raise_needs_tests=False)

    job = _run(app, fake_sheller, sandbox, script)

    assert "characterize" not in script.ran
    assert not (source / "project" / "sale_custom" / "tests").exists()
    assert git(source, "branch", "--show-current") == "main"
    assert _status(job) == "passed"


def test_a_test_that_never_goes_green_is_removed_and_the_lap_carries_on(
    app, fake_sheller, sandbox
):
    source = _source(sandbox)
    seed = make_repo(source)
    fake_sheller.test_outcomes["sale_custom.TestX"] = {
        "success": False, "failures": 1, "tests_run": 1,
    }
    script = CharacterizeScript()

    job = _run(app, fake_sheller, sandbox, script)

    assert not (source / "project" / "sale_custom" / "tests").exists()
    assert git(source, "rev-parse", "HEAD") == seed
    assert git(source, "status", "--short") == ""
    assert "tests/test_x.py" not in script.port_prompt
    assert "port" in script.ran
    assert _status(job) == "passed"


def test_a_test_that_was_never_run_is_not_green(app, fake_sheller, sandbox):
    source = _source(sandbox)
    make_repo(source)

    job = _run(app, fake_sheller, sandbox, CharacterizeScript(run_it=False))

    assert not (source / "project" / "sale_custom" / "tests").exists()
    assert _status(job) == "passed"


def test_a_source_project_that_is_not_a_repository_costs_the_lap_nothing(
    app, fake_sheller, sandbox
):
    source = _source(sandbox)
    script = CharacterizeScript()

    job = _run(app, fake_sheller, sandbox, script)

    assert "not a git repository" in script.refusal
    assert not (source / "project" / "sale_custom" / "tests").exists()
    assert not (source / ".git").exists()
    assert "port" in script.ran
    assert _status(job) == "passed"


def test_a_step_that_dies_after_writing_leaves_the_source_as_it_was(app, fake_sheller, sandbox):
    """The settle runs in a `finally`: a step that raises is not green."""
    source = _source(sandbox)
    seed = make_repo(source)

    class Dies(CharacterizeScript):
        def run_step(self, **kwargs):
            if kwargs["system"].split("Step: ", 1)[1].startswith("characterize"):
                kwargs["tools"][[t.name for t in kwargs["tools"]].index("write_source_test")](
                    side="source", path="project/sale_custom/tests/test_x.py", content=TEST
                )
                raise RuntimeError("the provider fell over")

            return super().run_step(**kwargs)

    # The test harness's `wait` re-raises what ended the job's thread.
    with pytest.raises(RuntimeError, match="fell over"):
        _run(app, fake_sheller, sandbox, Dies())

    assert not (source / "project" / "sale_custom" / "tests").exists()
    assert git(source, "rev-parse", "HEAD") == seed


def test_a_second_run_of_the_step_finds_the_tests_the_first_one_committed(
    app, fake_sheller, sandbox
):
    """A flow that retries from `understand` runs `characterize` again in the
    same job. The snapshot of "had tests" is taken per step, so the second run
    sees what the first committed and writes nothing next to it."""
    source = _source(sandbox)
    make_repo(source)
    retrying = STOCK.replace("cartridge: stock", "cartridge: retrying", 1).replace(
        "resume_at: lint", "resume_at: understand", 1
    )
    install_from_text(retrying)
    job, _ = sandbox
    with database.connection_context():
        Migration.update(cartridge_id="retrying", cartridge_version="1.0.0").where(
            Migration.id == Job.get_by_id(job.id).module.migration_id
        ).execute()
    fake_sheller.tests_for(
        "sale_custom", fake_sheller.catalogue("sale_custom", {"TestX": ["test_x"]})
    )

    class PortFailsOnce(CharacterizeScript):
        failed = False

        def run_step(self, **kwargs):
            step = kwargs["system"].split("Step: ", 1)[1].splitlines()[0].strip()
            if step == "port" and not self.failed:
                self.failed = True
                self.ran.append(step)

                return StepResult(ok=False, output="port could not finish")

            return super().run_step(**kwargs)

    script = PortFailsOnce()

    _run(app, fake_sheller, sandbox, script)

    assert script.ran.count("characterize") == 2
    assert "already has tests" in script.refusal
    assert git(source, "log", "--format=%s").count("characterization tests for sale_custom") == 1


def test_the_stock_flow_wires_the_step():
    flow = load_cartridge_file(Path("cartridges/stock.yaml"))
    ids = [step.id or step.kind for step in flow.pipeline]
    understand = flow.step_by_id("understand")
    characterize = flow.step_by_id("characterize")

    assert "needs_tests" in understand.raises
    assert ids.index("understand") < ids.index("characterize") < ids.index("port")
    assert characterize.when == "needs_tests"
    assert characterize.sessions == ["source"]
    assert "write_source_test" in characterize.tools
    assert "write_source_test" not in flow.step_by_id("port").tools
    assert "write_source_test" not in flow.step_by_id("rewrite").tools


def test_characterize_does_not_read_the_rules_of_the_target():
    """They describe what 18.0 changes, and a test written to them runs on 17.0
    only by luck."""
    step = load_cartridge_file(Path("cartridges/stock.yaml")).step_by_id("characterize")

    assert set(step.context) == {"module_source", "prior_answers", "test_guide"}


def test_the_characterize_prompt_holds_what_was_decided():
    step = load_cartridge_file(Path("cartridges/stock.yaml")).step_by_id("characterize")
    prompt = " ".join(step.prompt.split())

    assert "what this module does today" in prompt
    assert "recipe" in prompt
    assert "list of behaviours" in prompt
    assert "write_source_test" in prompt
    assert "`reopen`" in prompt
    assert "whole module" in prompt
    assert "every test you wrote" in prompt
    assert "pin it and say so" in prompt
    assert "the module is right by definition" in prompt
    assert "what you chose not to pin" in prompt
    # The choices about what to pin live in the guide, once.
    assert "eight at most" not in prompt
    # A double pass was considered and ruled out.
    assert "twice" not in prompt


# -- deliberate differences are recorded, in the test, where a person reads them --

MARKER = "# migrator: target-differs — "


def _flat(step_id):
    flow = load_cartridge_file(Path("cartridges/stock.yaml"))

    return " ".join(flow.step_by_id(step_id).prompt.split())


def test_the_port_marks_an_assertion_it_changes_because_the_target_differs():
    prompt = _flat("port")

    assert MARKER in prompt
    assert "one line each" in prompt
    # A difference nobody can trace to a rule is not a deliberate one.
    assert "cannot trace to a rule" in prompt
    assert "do not edit the assertion" in prompt


def test_write_test_is_the_fallback_and_reads_both_sides_and_the_rules():
    flow = load_cartridge_file(Path("cartridges/stock.yaml"))
    step = flow.step_by_id("write_test")
    prompt = _flat("write_test")

    assert step.when == "no_tests"
    assert set(step.sessions) == {"source", "target"}
    assert {"rules", "module_source", "prior_answers", "test_guide"} <= set(step.context)
    # The source is the oracle: it is asked first, and the target is written to match.
    assert "oracle" in prompt
    assert "exec" in prompt
    assert "write nothing" in prompt
    assert MARKER in prompt
    assert "`reopen`" in prompt
    assert "twice" not in prompt


def test_write_test_never_writes_the_source():
    flow = load_cartridge_file(Path("cartridges/stock.yaml"))

    assert "write_source_test" not in flow.step_by_id("write_test").tools


# -- the tests the migrator proved on the source must arrive on the target -------


def _log_texts():
    from odoo_migrator.models import RunLog

    with database.connection_context():
        return [str(row.text) for row in RunLog.select()]


def test_a_port_that_leaves_the_characterization_tests_behind_fails_the_tests_step(
    app, fake_sheller, sandbox
):
    """Job 43 of the first live run: the target already had a tests file of its
    own, the port did not touch it, and the step ran those four and passed. The
    seven the source had proved were nowhere near."""
    source = _source(sandbox)
    make_repo(source)
    fake_sheller.tests_for(
        "sale_custom", fake_sheller.catalogue("sale_custom", {"TestOld": ["test_old"]})
    )
    script = CharacterizeScript(carry_tests=False)

    job = _run(app, fake_sheller, sandbox, script)

    assert _status(job) != "passed"
    said = " ".join(_log_texts())
    assert "did not reach the target" in said
    assert "test_x" in said


def test_a_port_that_carries_them_passes_the_tests_step(app, fake_sheller, sandbox):
    source = _source(sandbox)
    make_repo(source)
    fake_sheller.tests_for(
        "sale_custom", fake_sheller.catalogue("sale_custom", {"TestX": ["test_x"]})
    )

    job = _run(app, fake_sheller, sandbox, CharacterizeScript())
    _, tmp_path = sandbox

    assert "did not reach the target" not in " ".join(_log_texts())
    assert "def test_x" in (tmp_path / "18" / "project" / "sale_custom" / "tests" / "test_x.py").read_text()
    assert _status(job) == "passed"


def test_a_module_the_migrator_wrote_no_tests_for_is_not_held_to_any(app, fake_sheller, sandbox):
    source = _source(sandbox)
    make_repo(source)

    job = _run(app, fake_sheller, sandbox, CharacterizeScript(raise_needs_tests=False))

    assert "did not reach the target" not in " ".join(_log_texts())
    assert _status(job) == "passed"


def test_the_port_is_told_to_keep_the_method_names_and_to_replace_a_file_there():
    prompt = _flat("port")

    assert "same method names" in prompt
    assert "replacing a tests file of the same name" in prompt


# -- the source's migration scripts do not cross a major version --


def test_the_port_prompt_forbids_carrying_the_sources_migration_scripts():
    """Job 7265f4c7 copied `migrations/17.0.1.1.0/post-migration.py` to
    `18.0.1.1.0/`: relabelled with the target series, the old script escapes
    the pruning and runs on 18.0."""
    prompt = _flat("port")

    assert "Do not carry the source's migration scripts" in prompt
    assert "renaming its directory" in prompt
    assert "writing the same code under a target version" in prompt
    assert "only for what this port itself changes" in prompt
