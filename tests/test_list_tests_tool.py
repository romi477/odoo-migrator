"""`list_tests` early in a port: the target has no directory for the module yet."""

from test_runner_review_fixes import _ctx

from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.tools import tools_for
from odoo_migrator.sheller.fake import FakeShellerClient


def _list_tests(ctx):
    step = Step(kind="agent", id="port", sessions=["target"], tools=["list_tests"], prompt="x")

    return next(tool for tool in tools_for(step, ctx) if tool.name == "list_tests")


def test_a_module_not_on_the_target_yet_is_an_answer_not_a_failed_call(app, tmp_path):
    sheller = FakeShellerClient()
    sheller.absent_modules.add("sale_custom")
    seen = []
    ctx = _ctx(tmp_path, sheller, record_tool=lambda *row: seen.append(row))

    answer = _list_tests(ctx)(side="target")

    assert not answer.startswith("error:")
    assert "not on this container yet" in answer
    assert "Write its files first" in answer
    assert seen[0][5] is True
    assert "not on this container yet" in seen[0][6]


def test_any_other_failure_of_list_tests_is_still_an_error(app, tmp_path):
    sheller = FakeShellerClient()
    ctx = _ctx(tmp_path, sheller)
    sheller.close(ctx.sessions["target"], write_key=ctx.write_keys["target"])

    answer = _list_tests(ctx)(side="target")

    assert answer.startswith("error:")
