"""The standing instruction reaches the model.

Without it the agent was never told that a tool is the only way to see
anything, or that a step ends with `report`. It answered in prose, called
nothing, and every agent step came back `ok=False` with an empty report —
which is what the first live run did, five laps in a row.
"""

import sys
import types
from types import SimpleNamespace

import pytest

from odoo_migrator.runner.cartridge import Budget, ModelRef, Step
from odoo_migrator.runner.prompting import run_agent_step, system_prompt
from odoo_migrator.runner.providers.anthropic import AnthropicProvider
from odoo_migrator.runner.providers.cursor import CursorProvider
from odoo_migrator.runner.tools import report_tool


def _step(**extra) -> Step:
    fields = {"kind": "agent", "id": "port", "sessions": ["new"], "tools": [], "prompt": "port it"}
    fields.update(extra)

    return Step(**fields)


def _ctx(**extra):
    fields = {
        "job_id": "job-1",
        "attempt": 1,
        "migration": SimpleNamespace(
            source_version="17.0",
            target_version="18.0",
            provider_override="",
            model_override="",
            old_project_path="",
            new_project_path="",
        ),
        "module": SimpleNamespace(technical_name="sale_custom", old_path="", new_path=""),
        "cartridge": SimpleNamespace(model=ModelRef(provider="anthropic", name="claude-opus-5")),
        "client": None,
        "prior_answers": {},
        "last_answer": "",
        "last_failure": "",
        "previous_diff": "",
        "sessions": {},
    }
    fields.update(extra)

    return SimpleNamespace(**fields)


def test_the_system_prompt_says_how_a_step_ends():
    text = system_prompt(_step(), _ctx())

    assert "report" in text
    assert "guess" in text


def test_the_system_prompt_names_this_module_and_both_versions():
    text = system_prompt(_step(), _ctx())

    assert "sale_custom" in text
    assert "17.0" in text and "18.0" in text
    assert "Step: port" in text


def test_an_agent_step_hands_the_system_prompt_to_its_provider(monkeypatch):
    seen = {}

    class Recorder:
        name = "anthropic"

        def ensure_ready(self):

            return None

        def run_step(self, **kwargs):
            from odoo_migrator.runner.context import StepResult

            seen.update(kwargs)

            return StepResult(ok=True, answer="done")

    monkeypatch.setattr("odoo_migrator.runner.providers.get", lambda _name: Recorder())

    run_agent_step(_step(), _ctx())

    assert "report" in seen["system"]
    assert seen["prompt"] == "port it"


def test_anthropic_sends_it_as_the_system_parameter(monkeypatch):
    kwargs_seen = {}

    class _Messages:
        def tool_runner(self, **kwargs):
            kwargs_seen.update(kwargs)

            return iter(())

    class _Client:
        def __init__(self, api_key=None):
            self.beta = SimpleNamespace(messages=_Messages())

    import anthropic

    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    monkeypatch.setattr("odoo_migrator.runner.providers.anthropic._key", lambda: "test-key")

    result = AnthropicProvider().run_step(
        system="standing orders",
        prompt="port it",
        tools=[],
        report=report_tool(_step()),
        model="claude-opus-5",
        budget=Budget(tool_calls=3),
    )

    assert kwargs_seen["system"] == "standing orders"
    assert not result.ok


def test_cursor_has_no_system_field_so_it_leads_with_it(monkeypatch):
    """The SDK's ``AgentOptions`` has no system parameter. Dropping the text
    would leave this provider the only agent never told how a step ends."""
    captured = {}

    class _Result:
        usage = None

    class _Run:
        def wait(self):
            captured["custom"]["report"].execute({"answer": "ported"}, None)

            return _Result()

    class _Agent:
        @classmethod
        def create(cls, options):

            return cls()

        def send(self, prompt):
            captured["prompt"] = prompt

            return _Run()

        def close(self):

            return None

    sdk = types.ModuleType("cursor_sdk")
    sdk.Agent = _Agent
    sdk.AgentOptions = lambda **kwargs: SimpleNamespace(**kwargs)
    sdk.CustomTool = lambda execute, description=None, input_schema=None: SimpleNamespace(
        execute=execute
    )

    def _local(cwd, custom_tools):
        captured["custom"] = custom_tools

        return SimpleNamespace(custom_tools=custom_tools)

    sdk.LocalAgentOptions = _local
    errors = types.ModuleType("cursor_sdk.errors")
    errors.CursorAgentError = type("CursorAgentError", (Exception,), {})
    monkeypatch.setitem(sys.modules, "cursor_sdk", sdk)
    monkeypatch.setitem(sys.modules, "cursor_sdk.errors", errors)
    monkeypatch.setattr("odoo_migrator.runner.providers.cursor._key", lambda: "test-key")

    result = CursorProvider().run_step(
        system="standing orders",
        prompt="port it",
        tools=[],
        report=report_tool(_step()),
        model="composer-2.5",
        budget=Budget(tool_calls=3),
    )

    assert captured["prompt"].startswith("standing orders")
    assert "port it" in captured["prompt"]
    assert result.ok


def test_a_missing_system_prompt_file_is_not_silently_skipped(monkeypatch, tmp_path):
    from odoo_migrator.rules import MissingPromptError

    monkeypatch.setattr("odoo_migrator.runner.prompting.load_system_prompt",
                        lambda: (_ for _ in ()).throw(MissingPromptError("gone")))
    with pytest.raises(MissingPromptError):
        system_prompt(_step(), _ctx())
