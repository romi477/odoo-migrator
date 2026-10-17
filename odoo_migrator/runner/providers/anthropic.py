"""Anthropic, via the SDK's beta tool runner."""

from __future__ import annotations

from collections.abc import Callable

from odoo_migrator.runner.cartridge import Budget
from odoo_migrator.runner.context import StepResult
from odoo_migrator.runner.providers.base import SettingField
from odoo_migrator.runner.tools import BoundTool, ReportTool

_EFFORT = "high"
# The cap on one response, which is not the budget for the step. The models
# top out at 128K, so passing a 200K run budget as `max_tokens` is a 400.
MAX_OUTPUT_TOKENS = 64_000


class NoModelClient(RuntimeError):
    """An agent step ran and this provider had no key."""


class AnthropicProvider:
    name = "anthropic"
    # The request below sends adaptive thinking, effort `high` and no
    # `tool_choice` or sampling parameters, which every model here accepts:
    # Sonnet 5.5 refuses a forced tool choice and non-default sampling, and
    # Sonnet 4.6 has no `xhigh`.
    models = frozenset(
        {
            "claude-sonnet-5-5",
            "claude-sonnet-5",
            "claude-sonnet-4-6",
            "claude-haiku-5-5",
        }
    )
    settings = (SettingField("api_key", "Anthropic API key"),)

    def accepts_model(self, name: str) -> bool:

        return name in self.models

    def ensure_ready(self) -> None:
        if not _key():
            raise NoModelClient(
                "no Anthropic API key is set — the agent steps of a cartridge "
                "cannot run. Set it on the settings page."
            )

    def run_step(
        self,
        *,
        system: str,
        prompt: str,
        tools: list[BoundTool],
        report: ReportTool,
        model: str,
        budget: Budget,
        stop_check: Callable[[], None] | None = None,
    ) -> StepResult:
        import anthropic
        from anthropic import beta_tool

        self.ensure_ready()
        captured: dict = {}
        usage = {"in": 0, "out": 0, "cached": 0}
        runnable = [_as_beta(beta_tool, tool) for tool in tools]
        runnable.append(_report_beta(beta_tool, report, captured))
        kwargs = {
            "max_tokens": min(budget.tokens or MAX_OUTPUT_TOKENS, MAX_OUTPUT_TOKENS),
            "model": model,
            "tools": runnable,
            "messages": [{"role": "user", "content": prompt or ""}],
            "thinking": {"type": "adaptive"},
            "output_config": {"effort": _EFFORT},
            "stream": True,
            # Every turn resends the whole conversation: the rules, the module
            # source, every tool result so far. Uncached, a 40K-token prompt
            # re-billed on each of a port's writes crossed a 200K budget by
            # the fifth tool call. The top-level breakpoint moves with the
            # conversation, so each turn reads the one before it from cache.
            "cache_control": {"type": "ephemeral"},
        }
        if system:
            kwargs["system"] = system
        if budget.tool_calls is not None:
            kwargs["max_iterations"] = budget.tool_calls
        client = anthropic.Anthropic(api_key=_key())
        runner = client.beta.messages.tool_runner(**kwargs)
        overspent = False
        turns = 0
        said = ""
        try:
            # Every turn, not the last one. `until_done()` hands back the
            # final message, and reading usage off that dropped everything
            # the tool loop spent getting there — which on a long step is
            # nearly all of it. The token budget has nowhere else to be
            # enforced either: the SDK caps iterations, not tokens.
            for turn in runner:
                message = turn.get_final_message()
                turns += 1
                said = _text_of(message) or said
                _add_usage(message, usage)
                # The step ends at `report`. Left to the runner, the call was
                # executed on the next iteration — which then asked the model
                # for one more turn it had no use for, and paid for it.
                reported = _report_input(message, report)
                if reported is not None:
                    captured.clear()
                    captured.update(reported)
                    break
                if budget.tokens is not None and _spent(usage) > budget.tokens:
                    overspent = True
                    break
                # The runner calls the tools of this turn before the next
                # one is asked for, so stopping here leaves nothing half run.
                if stop_check is not None:
                    stop_check()
        except anthropic.APIError as exc:
            account = _account(turns, said, tools)
            if captured:

                return _from_report(report, captured, usage, account)

            return StepResult(ok=False, output=str(exc), detail=account, **_counts(usage))
        account = _account(turns, said, tools)
        if captured:

            return _from_report(report, captured, usage, account)
        if overspent:

            return StepResult(
                ok=False,
                output="stopped at the token budget",
                detail=account,
                **_counts(usage),
            )

        return StepResult(
            ok=False,
            output=f"the step ended without a report, after {turns} turn(s)",
            detail=account,
            **_counts(usage),
        )


def _key() -> str:
    """The stored key. `provider_setting` falls back to the old column itself."""
    from odoo_migrator.settings import provider_setting

    return provider_setting("anthropic", "api_key")


def _as_beta(beta_tool, tool: BoundTool):
    def call(**payload: object) -> str:
        """Run this tool on a side the step declared."""

        return tool(**payload)

    return beta_tool(
        call,
        name=tool.name,
        description=tool.description,
        input_schema=tool.input_schema,
    )


def _report_beta(beta_tool, report: ReportTool, captured: dict):
    def submit(**payload: object) -> str:
        """End the step. Set a flag only when it is true."""
        refused = report.refusal(payload)
        if refused:

            return refused
        captured.clear()
        captured.update(payload)

        return "reported"

    return beta_tool(
        submit,
        name=report.name,
        description=report.description,
        input_schema=report.input_schema,
    )


def _add_usage(message, usage: dict) -> None:
    counted = getattr(message, "usage", None)
    if counted is None:

        return
    # Input is what was billed at full rate or above: uncached tokens plus
    # the ones written to the cache. Reads from the cache are counted apart.
    usage["in"] += int(getattr(counted, "input_tokens", 0) or 0)
    usage["in"] += int(getattr(counted, "cache_creation_input_tokens", 0) or 0)
    usage["out"] += int(getattr(counted, "output_tokens", 0) or 0)
    usage["cached"] += int(getattr(counted, "cache_read_input_tokens", 0) or 0)


def _spent(usage: dict) -> int:
    """What the budget holds a step to: new input and output, not cache reads.

    A cache read is the same prefix again at a tenth of the price. Counting
    it as new input made the budget grow with the square of the turns, so
    it measured how long a conversation was rather than what it cost.
    """

    return usage["in"] + usage["out"]


def _report_input(message, report: ReportTool) -> dict | None:
    """The report this turn ended with, or None — a refused one included.

    A refused report is not the end of the step: left to stop the loop here,
    the model would never hear why it was refused, and the runner would act
    on a contradiction it was told to reject.
    """
    for block in getattr(message, "content", None) or []:
        if getattr(block, "type", "") == "tool_use" and getattr(block, "name", "") == report.name:
            payload = getattr(block, "input", None)
            payload = dict(payload) if isinstance(payload, dict) else {}
            if report.refusal(payload):

                return None

            return payload

    return None


def _counts(usage: dict) -> dict:

    return {
        "tokens_in": usage["in"],
        "tokens_out": usage["out"],
        "tokens_cached": usage["cached"],
    }


def _text_of(message) -> str:
    blocks = getattr(message, "content", None) or []
    parts = [str(getattr(block, "text", "")) for block in blocks]

    return "\n".join(part for part in parts if part).strip()


def _account(turns: int, said: str, tools: list) -> str:
    rows = [
        f"turns: {turns}",
        f"tools offered: {', '.join(tool.name for tool in tools)}",
        "",
        "last text from the model:",
        said or "(none)",
    ]

    return "\n".join(rows)


def _from_report(report: ReportTool, payload: dict, usage: dict, detail: str = "") -> StepResult:
    flags = {
        flag
        for flag in report.input_schema["properties"]
        if flag != "answer" and payload.get(flag)
    }

    return StepResult(
        ok=True,
        flags=flags,
        answer=str(payload.get("answer") or ""),
        detail=detail,
        **_counts(usage),
    )
