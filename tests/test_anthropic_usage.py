"""What a step spent, and the budget it is held to.

Usage was read off the message `until_done()` returns — the last turn of
the tool loop. On a step that called ten tools, nine turns were dropped,
so the recorded cost of the expensive steps was the cost of their cheapest
one. The token budget a cartridge declares had nowhere to be enforced
either: the SDK caps iterations, not tokens.
"""

from types import SimpleNamespace

import pytest

from odoo_migrator.runner.cartridge import Budget, Step
from odoo_migrator.runner.providers.anthropic import AnthropicProvider
from odoo_migrator.runner.tools import report_tool


def _turn(inp: int, out: int, cached: int = 0):
    message = SimpleNamespace(
        usage=SimpleNamespace(
            input_tokens=inp, output_tokens=out, cache_read_input_tokens=cached
        )
    )

    return SimpleNamespace(get_final_message=lambda: message)


def _step() -> Step:

    return Step(kind="agent", id="port", sessions=["new"], tools=[], prompt="port")


def _provider(monkeypatch, turns, *, reports=None):
    import anthropic

    class _Messages:
        def tool_runner(self, **kwargs):
            if reports is not None:
                for tool in kwargs["tools"]:
                    if getattr(tool, "name", "") == "report":
                        tool.call(reports)

            return iter(turns)

    class _Client:
        def __init__(self, api_key=None):
            self.beta = SimpleNamespace(messages=_Messages())

    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    monkeypatch.setattr("odoo_migrator.runner.providers.anthropic._key", lambda: "test-key")

    return AnthropicProvider()


def _run(provider, budget):

    return provider.run_step(
        system="s",
        prompt="p",
        tools=[],
        report=report_tool(_step()),
        model="claude-opus-5",
        budget=budget,
    )


def test_every_turn_is_counted_not_only_the_last(monkeypatch):
    provider = _provider(monkeypatch, [_turn(100, 10, 5), _turn(200, 20, 7), _turn(300, 30)])

    result = _run(provider, Budget())

    assert result.tokens_in == 600
    assert result.tokens_out == 60
    assert result.tokens_cached == 12


def test_the_token_budget_stops_the_loop(monkeypatch):
    provider = _provider(monkeypatch, [_turn(400, 0), _turn(400, 0), _turn(400, 0)])

    result = _run(provider, Budget(tokens=500))

    assert not result.ok
    assert "token budget" in result.output
    # Stopped after the turn that crossed it, not after every turn ran.
    assert result.tokens_in == 800


def test_a_step_that_never_reports_says_so(monkeypatch):
    provider = _provider(monkeypatch, [_turn(10, 1)])

    result = _run(provider, Budget())

    assert not result.ok
    assert "without a report" in result.output
    assert result.tokens_in == 10


def test_a_reported_step_carries_the_whole_bill(monkeypatch):
    provider = _provider(
        monkeypatch, [_turn(100, 10), _turn(100, 10)], reports={"answer": "ported"}
    )

    result = _run(provider, Budget())

    assert result.ok
    assert result.answer == "ported"
    assert result.tokens_in == 200


def test_an_api_error_before_any_report_is_a_failure_with_its_bill(monkeypatch):
    import anthropic

    def _boom():
        yield _turn(50, 5)
        raise anthropic.APIError("upstream said no", request=None, body=None)

    provider = _provider(monkeypatch, _boom())

    result = _run(provider, Budget())

    assert not result.ok
    assert result.tokens_in == 50


def test_the_step_run_stores_the_cached_count(app, tmp_path, fake_sheller):
    """Cache reads are most of a long step's input and are billed apart."""
    from odoo_migrator.db import database
    from odoo_migrator.models import Batch, Job, Migration, Module, StepRun
    from odoo_migrator.runner.context import StepResult
    from odoo_migrator.runner.native import NativeRunner

    with database.connection_context():
        migration = Migration.create(
            name="Acme",
            accent_color="oklch(0.70 0.15 305)",
            source_version="17.0",
            target_version="18.0",
            old_project_path=str(tmp_path / "old"),
            new_project_path=str(tmp_path / "new"),
        )
        module = Module.create(migration=migration, technical_name="sale_custom")
        batch = Batch.create(migration=migration, status="running")
        job = Job.create(batch=batch, module=module, status="running")
        step_run = StepRun.create(job=job, attempt=1, step_id="port", kind="agent")

    runner = NativeRunner(sheller=fake_sheller, hub=app.state.hub)
    runner._close_step(
        step_run.id, StepResult(ok=True, tokens_in=10, tokens_out=2, tokens_cached=99)
    )

    with database.connection_context():
        assert StepRun.get_by_id(step_run.id).tokens_cached == 99


@pytest.mark.parametrize("budget", [Budget(), Budget(tokens=10_000_000)])
def test_no_budget_means_no_early_stop(monkeypatch, budget):
    provider = _provider(monkeypatch, [_turn(1000, 100) for _ in range(4)])

    result = _run(provider, budget)

    assert result.tokens_in == 4000
