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
        def __init__(self, cwd, custom_tools, sandbox_options=None):
            self.custom_tools = custom_tools
            self.sandbox_options = sandbox_options

    class AgentOptions:
        def __init__(self, model, api_key, local, mode=None, disallowed_tools=()):
            captured["model"] = model
            captured["denied"] = tuple(disallowed_tools)
            captured["mode"] = mode
            captured["custom"] = local.custom_tools
            captured["sandbox"] = local.sandbox_options

    class _Result:
        usage = None

    class _Run:
        # The reports the agent makes in one run, in order. More than one is
        # how a refused report is replayed: the tool answers, the agent tries
        # again inside the same run.
        reports = ({"answer": "ported"},)

        def wait(self):
            captured["custom"]["exec"].execute({"side": "new", "code": "1"}, None)
            captured["answers"] = [
                captured["custom"]["report"].execute(dict(payload), None)
                for payload in _Run.reports
            ]

            return _Result()

    class _Agent:
        def __init__(self, options=None):
            captured["options"] = options

        def send(self, prompt):
            captured["prompt"] = prompt

            return _Run()

        def close(self):

            return None

    sdk = types.ModuleType("cursor_sdk")
    class _Client:
        # `Client.launch_bridge` is the owned-bridge path the provider must
        # take: the default client does not own its bridge, and custom tools
        # then register on a callback server the bridge never heard of.
        @classmethod
        def launch_bridge(cls, workspace=None):
            captured["workspace"] = workspace

            return cls()

        def create_agent(self, options):

            return _Agent(options)

        def close(self):
            captured["client_closed"] = True

    sdk.Client = _Client
    sdk.Agent = _Agent
    sdk.AgentOptions = AgentOptions
    sdk.CustomTool = CustomTool
    sdk.LocalAgentOptions = LocalAgentOptions
    sdk.SandboxOptions = lambda enabled=None: {"enabled": enabled}
    sdk.ModelSelection = lambda id, params=(): {"id": id, "params": list(params)}
    sdk.ModelParameterValue = lambda id, value: {"id": id, "value": value}
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

    # `tools=[]` is never passed: it also removes the two meta-tools an
    # agent needs to call a custom tool, and the whole catalog then came
    # back "Tool not available". The dangerous built-ins go by name.
    assert "tools" not in captured
    assert "shell" in captured["denied"]
    assert "piWrite" in captured["denied"]
    # The two the agent needs to reach a custom tool are never denied.
    assert "getMcpTools" not in captured["denied"]
    assert "mcp" not in captured["denied"]
    assert captured["sandbox"] == {"enabled": True}
    assert captured["mode"] == "agent"
    # The bridge is ours. With the default client it is not, and every
    # custom tool came back "Tool not available" from the agent.
    assert captured["workspace"]
    assert captured["client_closed"]
    assert "exec" in captured["custom"]
    assert result.ok
    assert result.answer == "ported"

    # Over budget, but the agent had already reported. The tokens are spent
    # either way; discarding the answer sent the lap into the failure lane.
    _Result.usage = types.SimpleNamespace(input_tokens=5000, output_tokens=10)
    over = CursorProvider().run_step(
        system="",
        prompt="port it",
        tools=[BoundTool("exec", _exec)],
        report=report_tool(step),
        model="composer-2.5",
        budget=Budget(tool_calls=5, tokens=1000),
    )

    assert over.ok
    assert over.answer == "ported"
    assert over.tokens_in == 5000
    assert captured["model"] == {"id": "composer-2.5", "params": []}

    # Parameters written into the model name are the SDK's params, not part
    # of its id. As one string the run reported `ModelSelection(id='default')`.
    _Result.model = types.SimpleNamespace(id="default")
    routed = CursorProvider().run_step(
        system="",
        prompt="port it",
        tools=[BoundTool("exec", _exec)],
        report=report_tool(step),
        model="auto-smart?effort=high",
        budget=Budget(tool_calls=5),
    )

    assert captured["model"] == {
        "id": "auto-smart",
        "params": [{"id": "effort", "value": "high"}],
    }
    assert "model mismatch: asked for auto-smart?effort=high, the run reports default" in (
        routed.detail
    )

    # `small_fix` and `start_over` at once is a refused report: the tool says
    # why and the agent reports again. Accepted, the failure lane ran `patch`
    # and then `rewrite`, which starts from an empty module directory and
    # deleted the fix `patch` had just written.
    diagnose = _step(id="diagnose", raises=["small_fix", "start_over"])
    _Run.reports = (
        {"small_fix": True, "start_over": True},
        {"answer": "a field moved", "small_fix": True},
    )
    decided = CursorProvider().run_step(
        system="",
        prompt="why",
        tools=[BoundTool("exec", _exec)],
        report=report_tool(diagnose),
        model="composer-2.5",
        budget=Budget(tool_calls=5),
    )

    assert "cannot both be true" in captured["answers"][0]
    assert captured["answers"][1] == "reported"
    assert decided.flags == {"small_fix"}
    assert decided.answer == "a field moved"


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


def test_scripted_sends_a_contradictory_report_back_to_the_model():
    """Live providers refuse it and let the model report again; the double
    has to take the same path, or the tests agree with a loop that cannot
    happen against a real model."""
    client = ModelDouble(
        turns=[
            {"name": "report", "input": {"small_fix": True, "start_over": True}},
            {"name": "report", "input": {"answer": "a field moved", "small_fix": True}},
        ]
    )
    step = _step(id="diagnose", raises=["small_fix", "start_over"])

    result = ScriptedProvider(client).run_step(
        system="",
        prompt="why",
        tools=tools_for(step, _ctx()),
        report=report_tool(step),
        model="claude-opus-5",
        budget=Budget(tool_calls=5),
    )

    assert result.ok is True
    assert result.flags == {"small_fix"}
    assert result.answer == "a field moved"


def test_anthropic_does_not_end_the_step_on_a_contradictory_report():
    from odoo_migrator.runner.providers.anthropic import _report_beta, _report_input

    step = _step(id="diagnose", raises=["small_fix", "start_over"])
    report = report_tool(step)
    both = types.SimpleNamespace(
        content=[
            types.SimpleNamespace(
                type="tool_use",
                name="report",
                input={"small_fix": True, "start_over": True},
            )
        ]
    )
    one = types.SimpleNamespace(
        content=[
            types.SimpleNamespace(type="tool_use", name="report", input={"start_over": True})
        ]
    )

    assert _report_input(both, report) is None
    assert _report_input(one, report) == {"start_over": True}

    captured: dict = {}
    submitted = []
    _report_beta(lambda fn, **kw: submitted.append(fn) or fn, report, captured)
    submit = submitted[0]

    assert "cannot both be true" in submit(small_fix=True, start_over=True)
    assert captured == {}
    assert submit(answer="done", small_fix=True) == "reported"
    assert captured == {"answer": "done", "small_fix": True}


def test_anthropic_reads_a_key_saved_in_either_place(tmp_path):
    """Keys used to live in a column on the app settings. A key saved there
    before `ProviderSetting` existed still has to be the one a step uses."""
    from odoo_migrator.db import database, init_db
    from odoo_migrator.models import AppSettings
    from odoo_migrator.runner.providers.anthropic import _key
    from odoo_migrator.settings import save_app_settings

    init_db(tmp_path / "keys.sqlite")
    with database.connection_context():
        AppSettings.create(anthropic_api_key="sk-legacy")

    assert _key() == "sk-legacy"

    save_app_settings(
        sheller_base_url="", sheller_image_tag="", provider_values={"anthropic__api_key": "sk-new"}
    )

    assert _key() == "sk-new"


def test_anthropic_takes_both_sonnets_a_cartridge_may_name():
    provider = get("anthropic")

    assert provider.accepts_model("claude-sonnet-5-5")
    assert provider.accepts_model("claude-sonnet-4-6")


def test_scripted_hears_a_stop_between_two_actions():
    """As a live provider does: the double that skipped the check would let
    a test pass on a loop that never stops."""
    from odoo_migrator.runner.context import Stopped

    client = ModelDouble(always_calls="exec")

    def stop_check():
        if client.calls >= 2:
            raise Stopped("during port")

    with pytest.raises(Stopped):
        ScriptedProvider(client).run_step(
            system="",
            prompt="x",
            tools=[BoundTool("exec", lambda **_kwargs: "ok")],
            report=report_tool(_step()),
            model="claude-opus-5",
            budget=Budget(tool_calls=10),
            stop_check=stop_check,
        )

    assert client.calls == 2


def test_anthropic_offers_the_current_models():
    """The card lists only what the provider names, and the newest Opus and
    Haiku were not among them."""
    from odoo_migrator.runner.providers import get

    anthropic = get("anthropic")

    assert anthropic.accepts_model("claude-opus-5-5")
    assert anthropic.accepts_model("claude-haiku-5-5")
