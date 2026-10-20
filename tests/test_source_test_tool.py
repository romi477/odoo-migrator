"""The one write the source side allows: a new test, in the module's own tests/."""

import pytest

from odoo_migrator.db import database, init_db
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Migration, Module
from odoo_migrator.runner import tools
from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext
from odoo_migrator.runner.tools import TOOL_SIDES, ToolRefused, _write_source_test, tools_for


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    init_db(tmp_path / "t.sqlite")
    source = tmp_path / "17"
    target = tmp_path / "18"
    (source / "sale_x" / "models").mkdir(parents=True)
    (source / "sale_x" / "__manifest__.py").write_text("{'name': 'X'}\n")
    target.mkdir()
    created = create_migration(
        name="Acme", accent="violet", source_version="17.0", target_version="18.0",
        old_project_path=str(source), new_project_path=str(target),
    )
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])
        module = Module.create(
            migration=migration, technical_name="sale_x",
            old_path=str(source / "sale_x"), new_path=str(target / "sale_x"),
        )
    # The branch is Task 4's; here the write is the thing under test.
    monkeypatch.setattr("odoo_migrator.source_git.ensure_branch", lambda migration: "migrator/x")

    return StepContext(
        job_id="j", attempt=1, migration=migration, module=module, cartridge=None,
        sheller=None, sessions={}, emit=lambda event: None,
    )


def test_a_test_is_written_under_tests_of_the_source_module(ctx, tmp_path):
    written = _write_source_test(ctx, "source", "sale_x/tests/test_x.py", "# t\n")

    assert (tmp_path / "17" / "sale_x" / "tests" / "test_x.py").read_text() == "# t\n"
    assert written.endswith("sale_x/tests/test_x.py")
    assert ctx.source_tests_written is True
    assert ctx.source_green is False



def test_a_written_test_is_unproven_until_it_is_settled(ctx, tmp_path):
    """A start of another module adds what the baseline missed; it must not
    take a test the agent is still writing for a person's."""
    import subprocess

    from odoo_migrator.source_git import unproven

    subprocess.run(["git", "init", "-q"], cwd=tmp_path / "17", check=True)

    _write_source_test(ctx, "source", "sale_x/tests/test_x.py", "# t\n")

    assert unproven(tmp_path / "17") == ["sale_x/tests"]


def test_a_path_outside_tests_is_refused_not_clamped(ctx, tmp_path):
    for bad in (
        "sale_x/models/x.py",
        "sale_x/__manifest__.py",
        "sale_x/tests/../models/x.py",
        "../x.py",
    ):
        with pytest.raises(ToolRefused):
            _write_source_test(ctx, "source", bad, "x\n")

    assert not (tmp_path / "17" / "sale_x" / "tests").exists()
    assert not (tmp_path / "17" / "sale_x" / "models" / "x.py").exists()
    assert ctx.source_tests_written is False


def test_a_module_that_already_has_tests_is_never_written_to(ctx, tmp_path):
    (tmp_path / "17" / "sale_x" / "tests").mkdir()
    (tmp_path / "17" / "sale_x" / "tests" / "test_old.py").write_text("# kept\n")

    with pytest.raises(ToolRefused, match="already has tests"):
        _write_source_test(ctx, "source", "sale_x/tests/test_new.py", "x\n")

    assert (tmp_path / "17" / "sale_x" / "tests" / "test_old.py").read_text() == "# kept\n"
    assert not (tmp_path / "17" / "sale_x" / "tests" / "test_new.py").exists()


def test_a_test_file_outside_tests_counts_as_a_test(ctx, tmp_path):
    (tmp_path / "17" / "sale_x" / "models" / "test_stray.py").write_text("# x\n")

    with pytest.raises(ToolRefused, match="already has tests"):
        _write_source_test(ctx, "source", "sale_x/tests/test_new.py", "x\n")


def test_a_step_may_write_several_files_and_rewrite_its_own(ctx, tmp_path):
    _write_source_test(ctx, "source", "sale_x/tests/__init__.py", "from . import test_x\n")
    _write_source_test(ctx, "source", "sale_x/tests/test_x.py", "v1\n")
    _write_source_test(ctx, "source", "sale_x/tests/test_x.py", "v2\n")

    assert (tmp_path / "17" / "sale_x" / "tests" / "test_x.py").read_text() == "v2\n"


def test_a_refused_branch_refuses_the_write(ctx, tmp_path, monkeypatch):
    from odoo_migrator.source_git import SourceGitError

    def refuse(migration):
        raise SourceGitError("the source project is not a git repository")

    monkeypatch.setattr("odoo_migrator.source_git.ensure_branch", refuse)

    with pytest.raises(ToolRefused, match="not a git repository"):
        _write_source_test(ctx, "source", "sale_x/tests/test_x.py", "x\n")

    assert not (tmp_path / "17" / "sale_x" / "tests").exists()
    assert ctx.source_tests_written is False


def test_the_target_side_is_not_a_place_to_write_a_source_test(ctx):
    assert TOOL_SIDES["write_source_test"] == ("source",)
    step = Step(step="agent", id="c", sessions=["target"], tools=["write_source_test"], prompt="p")

    with pytest.raises(ToolRefused):
        tools_for(step, ctx)[0](side="target", path="sale_x/tests/test_x.py", content="x\n")


def test_port_and_rewrite_still_cannot_write_the_source(ctx, tmp_path):
    step = Step(
        step="agent", id="port", sessions=["source", "target"], tools=["write_file"], prompt="p"
    )
    write = tools_for(step, ctx)[0]

    with pytest.raises(ToolRefused):
        write(side="source", path="sale_x/models/x.py", content="x\n")

    assert not (tmp_path / "17" / "sale_x" / "models" / "x.py").exists()


def test_reopen_may_restart_either_side(ctx):
    assert TOOL_SIDES["reopen"] == ("source", "target")


TWO_TESTS = (
    "class TestX(TransactionCase):\n"
    "    def test_a(self):\n        self.assertTrue(1)\n\n"
    "    def test_b(self):\n        self.assertTrue(1)\n"
)


def _written(ctx, content=TWO_TESTS):
    ctx.sessions = {"source": "sess-source", "target": "sess-target"}
    _write_source_test(ctx, "source", "sale_x/tests/__init__.py", "from . import test_x\n")
    _write_source_test(ctx, "source", "sale_x/tests/test_x.py", content)


def _ran(ctx, spec, run=2, success=True, side="source", **extra):
    report = {"test": {"success": success, "tests_run": run}, **extra}
    tools._note_source_run(ctx, "run_test", side, report, spec)


def test_a_run_of_the_module_that_ran_every_test_marks_them_green(ctx):
    _written(ctx)

    _ran(ctx, "sale_x", run=2)

    assert ctx.source_green is True


def test_a_class_run_that_ran_every_test_counts_too(ctx):
    _written(ctx)

    _ran(ctx, "sale_x.TestX", run=2)

    assert ctx.source_green is True


def test_a_run_of_another_module_is_not_green(ctx):
    """The tests were committed as proved because *a* run on the source
    passed, whatever it ran."""
    _written(ctx)

    _ran(ctx, "other_module", run=40)
    assert ctx.source_green is False

    _ran(ctx, "other_module.TestY", run=40)
    assert ctx.source_green is False


def test_a_module_whose_name_starts_like_ours_is_not_ours(ctx):
    _written(ctx)

    _ran(ctx, "sale_x_extra", run=2)

    assert ctx.source_green is False


def test_a_run_of_one_method_does_not_prove_the_other(ctx):
    """A failing method was fixed and only it was run again."""
    _written(ctx)

    _ran(ctx, "sale_x.TestX.test_a", run=1)

    assert ctx.source_green is False


def test_a_run_of_a_class_that_is_not_ours_to_the_full_count_is_not_green(ctx):
    _written(ctx)

    _ran(ctx, "sale_x.TestOther", run=1)

    assert ctx.source_green is False


def test_a_test_file_with_no_test_method_can_never_be_green(ctx):
    _written(ctx, "class TestX(TransactionCase):\n    pass\n")

    _ran(ctx, "sale_x", run=5)

    assert ctx.source_green is False


def test_a_run_that_ran_nothing_or_failed_is_not_green(ctx):
    _written(ctx)
    for report in (
        {"test": {"success": True, "tests_run": 0}},
        {"test": {"success": False, "tests_run": 2, "failures": 1}},
        {"test": {"success": True, "tests_run": 2}, "error": "boom"},
    ):
        ctx.source_green = True

        tools._note_source_run(ctx, "run_test", "source", report, "sale_x")

        assert ctx.source_green is False


def test_a_run_on_the_target_or_before_any_write_changes_nothing(ctx):
    _ran(ctx, "sale_x", run=2)
    assert ctx.source_green is False

    _written(ctx)
    _ran(ctx, "sale_x", run=2, side="target")

    assert ctx.source_green is False


def test_a_write_after_a_green_run_makes_the_tests_unproven_again(ctx):
    _written(ctx)
    _ran(ctx, "sale_x", run=2)
    assert ctx.source_green is True

    _write_source_test(ctx, "source", "sale_x/tests/test_x.py", TWO_TESTS + "# changed\n")

    assert ctx.source_green is False


def test_rewriting_a_file_replaces_its_count_it_does_not_add_to_it(ctx):
    _written(ctx)
    _write_source_test(ctx, "source", "sale_x/tests/test_x.py", TWO_TESTS)

    _ran(ctx, "sale_x", run=2)

    assert ctx.source_green is True


def test_the_green_run_remembers_the_session_and_the_command_that_proved_it(ctx):
    _written(ctx)
    ctx.write_keys = {"source": "KEY-SECRET"}

    _ran(ctx, "sale_x", run=2, id=7)

    assert ctx.source_green_session == "sess-source"
    assert ctx.source_green_command == "7"
    assert "KEY-SECRET" not in (ctx.source_green_session + ctx.source_green_command)


def test_a_run_that_is_not_green_forgets_the_proof(ctx):
    _written(ctx)
    _ran(ctx, "sale_x", run=2, id=7)

    _ran(ctx, "sale_x", run=2, success=False, id=8)

    assert ctx.source_green_session == ""
    assert ctx.source_green_command == ""


def test_a_write_forgets_the_proof_too(ctx):
    _written(ctx)
    _ran(ctx, "sale_x", run=2, id=7)

    _write_source_test(ctx, "source", "sale_x/tests/test_x.py", TWO_TESTS + "# v2\n")

    assert ctx.source_green_session == ""


def test_the_model_is_told_what_the_tool_is_for():
    description = tools.TOOL_DESCRIPTIONS["write_source_test"]

    assert "tests/" in description
    assert "source" in description
    assert tools.TOOL_SCHEMAS["write_source_test"]["required"] == ["side", "path", "content"]


# -- a test that asserts nothing proves nothing --------------------------------

NO_ASSERT = (
    "class TestX(TransactionCase):\n"
    "    def test_a(self):\n        self.env['res.partner'].create({'name': 'x'})\n"
)


def test_a_test_method_without_an_assertion_is_never_green(ctx):
    _written(ctx, NO_ASSERT)

    _ran(ctx, "sale_x", run=1)

    assert ctx.source_green is False


def test_the_write_says_which_method_asserts_nothing(ctx):
    written = _write_source_test(ctx, "source", "sale_x/tests/test_x.py", NO_ASSERT)

    assert "test_a" in written
    assert "asserts nothing" in written


def test_a_good_write_answers_with_the_path_alone(ctx):
    written = _write_source_test(ctx, "source", "sale_x/tests/test_x.py", TWO_TESTS)

    assert "\n" not in written


@pytest.mark.parametrize(
    "body",
    [
        "        self.assertEqual(1, 1)\n",
        "        with self.assertRaises(ValueError):\n            int('x')\n",
        "        assert 1 == 1\n",
        "        self._assert_total(10)\n",
        "        self.assertTrue(1)\n",
    ],
)
def test_every_ordinary_way_to_assert_counts(ctx, body):
    _written(ctx, f"class TestX(TransactionCase):\n    def test_a(self):\n{body}")

    _ran(ctx, "sale_x", run=1)

    assert ctx.source_green is True


def test_one_bare_method_among_asserting_ones_keeps_the_tests_unproven(ctx):
    _written(ctx, TWO_TESTS + "    def test_c(self):\n        self.env['res.partner'].search([])\n")

    _ran(ctx, "sale_x", run=3)

    assert ctx.source_green is False


def test_a_file_that_does_not_parse_is_never_green(ctx):
    _written(ctx, "class TestX(TransactionCase:\n    def test_a(self):\n")

    _ran(ctx, "sale_x", run=1)

    assert ctx.source_green is False


def test_fixing_the_method_that_asserted_nothing_makes_the_tests_provable(ctx):
    _written(ctx, NO_ASSERT)
    _write_source_test(ctx, "source", "sale_x/tests/test_x.py", TWO_TESTS)

    _ran(ctx, "sale_x", run=2)

    assert ctx.source_green is True
