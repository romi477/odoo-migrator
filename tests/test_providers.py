"""The provider registry, the scripted double, and Cursor's in-process tools."""

import sys
import types

import pytest
from doubles.model import ModelDouble

from odoo_migrator.runner.cartridge import Budget, Step, load_cartridge
from odoo_migrator.runner.context import StepContext
from odoo_migrator.runner.providers import get, known_models
from odoo_migrator.runner.providers.cursor import CursorProvider
from odoo_migrator.runner.providers.scripted import ScriptedProvider
from odoo_migrator.runner.tools import BoundTool, ToolRefused, report_tool, tools_for


def _step(**extra) -> Step:
    fields = {
        "kind": "agent",
        "id": "port",
        "sessions": ["new"],
        "tools": ["exec"],
        "prompt": "port it",
    }
    fields.update(extra)

    return Step(**fields)


def _ctx() -> StepContext:

    return StepContext(
        job_id="job-1",
        attempt=1,
        migration=None,
        module=None,
        cartridge=None,
        sheller=None,
        sessions={"new": "sess"},
        emit=lambda _event: None,
    )


def test_the_registry_is_how_a_provider_is_found():
    assert get("anthropic").name == "anthropic"
    assert "claude-opus-5" in known_models()["anthropic"]
    assert "composer-2.5" in known_models()["cursor"]
    with pytest.raises(KeyError):
        get("no-such-provider")


def test_cursor_accepts_a_parameterised_auto_model():
    provider = CursorProvider()

    assert provider.accepts_model("auto-smart?effort=high")
    assert not provider.accepts_model("claude-opus-5")


def test_a_cartridge_may_name_cursor_without_the_loader_knowing_the_model():
    text = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: cursor, name: "auto-smart?effort=high"}
pipeline:
  - step: verdict
on_failure:
  pipeline: []
  retry:
    resume_at: verdict
"""

    loaded = load_cartridge(text)

    assert loaded.model.provider == "cursor"


def test_scripted_converges_on_report():
    client = ModelDouble(turns=[{"name": "report", "input": {"answer": "done", "needs_human": True}}])
    step = _step(raises=["needs_human"])

    result = ScriptedProvider(client).run_step(
        system="",
        prompt="x",
        tools=tools_for(step, _ctx()),
        report=report_tool(step),
        model="claude-opus-5",
        budget=Budget(tool_calls=5),
    )

    assert result.ok
    assert result.answer == "done"
    assert result.flags == {"needs_human"}


def test_scripted_can_fail_to_converge():
    result = ScriptedProvider(ModelDouble(turns=[])).run_step(
        system="",
        prompt="x",
        tools=[],
        report=report_tool(_step()),
        model="claude-opus-5",
        budget=Budget(tool_calls=5),
    )

    assert not result.ok
    assert "nothing useful" in result.output


def test_scripted_stops_at_the_tool_budget():
    result = ScriptedProvider(ModelDouble(always_calls="exec")).run_step(
        system="",
        prompt="x",
        tools=[BoundTool("exec", lambda **_kwargs: "ok")],
        report=report_tool(_step()),
        model="claude-opus-5",
        budget=Budget(tool_calls=2),
    )

    assert not result.ok
    assert "budget" in result.output


def test_scripted_returns_a_refused_tool():
    step = _step(sessions=["new"], tools=["exec"])

    def refuse(**_payload):
        raise ToolRefused("side old is not declared")

    tools = tools_for(step, _ctx())
    tools[0]._fn = refuse
    client = ModelDouble(turns=[{"name": "exec", "input": {"side": "old", "code": "1"}}])

    result = ScriptedProvider(client).run_step(
        system="",
        prompt="x",
        tools=tools,
        report=report_tool(step),
        model="claude-opus-5",
        budget=Budget(tool_calls=5),
    )

    assert not result.ok
    assert "old" in result.output


def test_a_bound_tool_carries_its_schema():
    tool = tools_for(_step(), _ctx())[0]

    assert tool.name == "exec"
    assert "code" in tool.input_schema["properties"]


def test_cursor_hands_the_catalog_to_custom_tools_and_reports(monkeypatch):
    captured = {}

    class CustomTool:
        def __init__(self, execute, description=None, input_schema=None):
            self.execute = execute
            self.description = description
            self.input_schema = input_schema

    class LocalAgentOptions:
        def __init__(self, cwd, custom_tools):
            self.custom_tools = custom_tools

    class AgentOptions:
        def __init__(self, model, api_key, tools, local):
            captured["tools"] = tools
            captured["custom"] = local.custom_tools

    class _Result:
        usage = None

    class _Run:
        def wait(self):
            captured["custom"]["exec"].execute({"side": "new", "code": "1"}, None)
            captured["custom"]["report"].execute({"answer": "ported"}, None)

            return _Result()

    class _Agent:
        @classmethod
        def create(cls, options):
            captured["options"] = options

            return cls()

        def send(self, prompt):
            captured["prompt"] = prompt

            return _Run()

        def close(self):

            return None

    sdk = types.ModuleType("cursor_sdk")
    sdk.Agent = _Agent
    sdk.AgentOptions = AgentOptions
    sdk.CustomTool = CustomTool
    sdk.LocalAgentOptions = LocalAgentOptions
    errors = types.ModuleType("cursor_sdk.errors")

    class CursorAgentError(Exception):
        pass

    errors.CursorAgentError = CursorAgentError
    monkeypatch.setitem(sys.modules, "cursor_sdk", sdk)
    monkeypatch.setitem(sys.modules, "cursor_sdk.errors", errors)
    monkeypatch.setattr(
        "odoo_migrator.runner.providers.cursor._key",
        lambda: "test-key",
    )
    step = _step()
    called = []

    def _exec(**kwargs):
        called.append(kwargs)

        return "ok"

    result = CursorProvider().run_step(
        system="",
        prompt="port it",
        tools=[BoundTool("exec", _exec)],
        report=report_tool(step),
        model="composer-2.5",
        budget=Budget(tool_calls=5, tokens=1000),
    )

    assert captured["tools"] == []
    assert "exec" in captured["custom"]
    assert result.ok
    assert result.answer == "ported"


def test_cursor_without_a_key_does_not_call_the_sdk(monkeypatch):
    monkeypatch.setattr("odoo_migrator.runner.providers.cursor._key", lambda: "")
    with pytest.raises(RuntimeError, match="Cursor API key"):
        CursorProvider().run_step(
            system="",
            prompt="x",
            tools=[],
            report=report_tool(_step()),
            model="composer-2.5",
            budget=Budget(),
        )
