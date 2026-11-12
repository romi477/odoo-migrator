"""The agent's `click_module`: the same run and the same reading as the step."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from odoo_migrator.runner.cartridge import CartridgeError, load_cartridge, load_cartridge_file
from odoo_migrator.runner.tools import (
    TOOL_DESCRIPTIONS,
    TOOL_SCHEMAS,
    TOOL_SIDES,
    ToolRefused,
    tools_for,
)
from odoo_migrator.sheller import FakeShellerClient, ShellerTimeout


@pytest.fixture
def fake():
    return FakeShellerClient()


def _ctx(fake):
    opened = fake.open_session(
        container="odoo-new", database="acme", odoo_bin="/opt/odoo/odoo-bin",
        owner={"kind": "agent"}, allow_commit=True,
    )
    calls = []

    return SimpleNamespace(
        sheller=fake, sessions={"target": opened["id"]},
        write_keys={"target": opened["write_key"]},
        module=SimpleNamespace(technical_name="sale_custom"),
        session_targets={
            "target": {
                "container": "odoo-new", "database": "acme",
                "odoo_bin": "/opt/odoo/odoo-bin", "owner": {"kind": "agent"},
            }
        },
        last_test=None, record_tool=lambda *args: calls.append(args), calls=calls,
    )


def _tool(ctx, sessions=("target",)):
    step = SimpleNamespace(
        tools=["click_module"], sessions=list(sessions), kind="agent", id="diagnose"
    )

    return tools_for(step, ctx)[0]


def test_the_tool_is_in_the_catalog_for_both_sides_with_a_module_argument():
    assert TOOL_SIDES["click_module"] == ("source", "target")
    assert set(TOOL_SCHEMAS["click_module"]["properties"]) == {"side", "module"}
    assert TOOL_SCHEMAS["click_module"]["required"] == ["side"]
    assert "login" not in TOOL_SCHEMAS["click_module"]["properties"]
    assert "never a pass" in TOOL_DESCRIPTIONS["click_module"]


def test_the_agent_clicks_as_the_step_does_and_reads_the_words(fake):
    fake.clickbot_answers["odoo-new"] = {
        "failed": [{"xmlid": "sale_custom.menu_a", "root": "sale_custom.menu_root", "reason": "boom"}],
        "roots": [{"xmlid": "sale_custom.menu_root", "success": False, "error": None}],
    }
    ctx = _ctx(fake)

    text = _tool(ctx)(side="target")

    assert "sale_custom.menu_a (under sale_custom.menu_root): boom" in text
    (run,) = fake.clicks
    assert run["module"] == "sale_custom" and run["login"] is None and run["debug"] is True
    assert run["timeout"] == 900.0


def test_a_module_may_be_named_and_the_run_is_in_the_log_under_the_tools_name(fake):
    ctx = _ctx(fake)

    _tool(ctx)(side="target", module="other_module")

    assert fake.clicks[0]["module"] == "other_module"
    assert ctx.calls[0][0] == "click_module" and ctx.calls[0][1] == "target"
    assert ctx.calls[0][6].startswith("clickbot: ")


def test_a_run_that_did_not_happen_reads_as_one_in_the_words_and_the_log(fake):
    fake.clickbot_answers["odoo-new"] = {"skipped": {"reason": "chrome_not_found"}, "clicked": []}
    ctx = _ctx(fake)

    text = _tool(ctx)(side="target")

    assert "did not run" in text and "not a pass" in text
    assert ctx.calls[0][6].startswith("clickbot did not run")


def test_a_run_past_the_call_is_interrupted_and_said(fake):
    fake.click_error["odoo-new"] = ShellerTimeout("no answer within the timeout")
    ctx = _ctx(fake)

    # A Sheller error is the agent's to read, as for every other tool.
    before = ctx.sessions["target"]

    text = _tool(ctx)(side="target")

    assert text.startswith("error: ") and "opened again" in text
    assert before in fake.closes and ctx.sessions["target"] != before


def test_the_tool_is_refused_on_a_side_the_step_did_not_declare(fake):
    ctx = _ctx(fake)

    with pytest.raises(ToolRefused):
        _tool(ctx, sessions=("source",))(side="target")


def test_diagnose_has_it_on_the_target_and_the_other_agents_do_not():
    stock = load_cartridge_file(Path("cartridges/stock.yaml"))

    assert "click_module" in stock.step_by_id("diagnose").tools
    assert stock.step_by_id("diagnose").sessions == ["target"]
    for other in ("patch", "port", "rewrite", "reprobe", "understand"):
        assert "click_module" not in stock.step_by_id(other).tools


def test_a_flow_cannot_hand_click_module_to_a_step_with_no_session():
    text = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-sonnet-5-5}
pipeline:
  - step: agent
    id: a
    tools: [click_module]
    prompt: x
on_failure:
  pipeline: []
  retry: {resume_at: a}
"""
    with pytest.raises(CartridgeError, match="not allowed on session"):
        load_cartridge(text)


def test_a_menu_tree_that_could_not_be_put_back_reopens_the_session_and_says_so(fake):
    fake.clickbot_answers["odoo-new"] = {"restored": False}
    ctx = _ctx(fake)
    before = ctx.sessions["target"]

    text = _tool(ctx)(side="target")

    assert "put back" in text and "opened again" in text
    assert before in fake.closes and ctx.sessions["target"] != before


def test_the_description_carries_what_only_a_step_with_the_tool_needs_to_know():
    """The shell notes go to every agent step of a job. Only `diagnose` has this
    tool, so what to know about it is said here, where only it is read."""
    text = TOOL_DESCRIPTIONS["click_module"]

    assert "cannot click one menu" in text
    assert "not in the database until" in text and "upgrade" in text


def test_what_the_run_rolled_back_is_said(fake):
    """The run rolls back whatever the session held: a record `exec` made to
    open a view with data is gone, and `run_test` shows the field."""
    ctx = _ctx(fake)
    fake.exec(ctx.sessions["target"], "env['res.partner'].create({'name': 'x'})",
              write_key=ctx.write_keys["target"])

    text = _tool(ctx)(side="target")

    assert "rolled back" in text


def test_the_log_line_carries_the_runs_command_id_and_its_outcome(fake):
    ctx = _ctx(fake)
    _tool(ctx)(side="target")
    fake.clickbot_answers["odoo-new"] = {"skipped": {"reason": "chrome_not_found"}, "clicked": []}
    _tool(ctx)(side="target")

    (green, skipped) = ctx.calls
    assert green[3] and green[5] is True
    assert skipped[3] and skipped[5] is False
