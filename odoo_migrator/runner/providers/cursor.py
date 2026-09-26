"""Cursor, via its Python SDK and in-process custom tools.

``LocalAgentOptions.custom_tools`` takes a ``CustomTool`` whose ``execute``
is a Python callable. The catalog is handed over directly, so this provider
does not start an MCP server. Built-in tools stay off: ``tools=[]``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from odoo_migrator.runner.cartridge import Budget
from odoo_migrator.runner.context import StepResult
from odoo_migrator.runner.providers.base import SettingField
from odoo_migrator.runner.tools import BoundTool, ReportTool, ToolRefused

# What the settings page lists. ``accepts_model`` is wider: ``auto-smart``
# and the same name with parameters are models, not members of this set.
_LISTED = frozenset({"composer-2.5", "composer-2", "auto"})


class CursorProvider:
    name = "cursor"
    models = _LISTED
    settings = (SettingField("api_key", "Cursor API key"),)

    def accepts_model(self, name: str) -> bool:
        bare = name.split("?", 1)[0].strip()

        return bare in self.models or bare.startswith("auto")

    def ensure_ready(self) -> None:
        if not _key():
            raise RuntimeError(
                "no Cursor API key is set — set it on the settings page. "
                "The SDK reads CURSOR_API_KEY; team admin keys are not accepted."
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
    ) -> StepResult:
        self.ensure_ready()
        from cursor_sdk import Agent, AgentOptions, CustomTool, LocalAgentOptions
        from cursor_sdk.errors import CursorAgentError
        captured: dict = {}
        spent = {"calls": 0}

        def execute(bound: BoundTool):
            def run(arguments, _context) -> str:
                if budget.tool_calls is not None and spent["calls"] >= budget.tool_calls:

                    return "stopped at the tool-call budget"
                spent["calls"] += 1
                try:

                    return bound(**dict(arguments or {}))
                except ToolRefused as refused:

                    return f"refused: {refused}"

            return run

        def report_execute(arguments, _context) -> str:
            captured.clear()
            captured.update(dict(arguments or {}))

            return "reported"

        custom = {
            tool.name: CustomTool(
                execute=execute(tool),
                description=tool.description,
                input_schema=tool.input_schema,
            )
            for tool in tools
        }
        custom[report.name] = CustomTool(
            execute=report_execute,
            description=report.description,
            input_schema=report.input_schema,
        )
        # Built-in tools are off, so this directory is only a cwd the SDK
        # requires. The agent reaches the project through the catalog.
        with tempfile.TemporaryDirectory(prefix="cursor-step-") as cwd:
            try:
                agent = Agent.create(
                    AgentOptions(
                        model=model,
                        api_key=_key(),
                        tools=[],
                        local=LocalAgentOptions(
                            cwd=Path(cwd),
                            custom_tools=custom,
                        ),
                    )
                )
                try:
                    # ``AgentOptions`` has no system field, so the standing
                    # instruction rides at the head of the first message.
                    # Dropping it would leave this provider's agent the only
                    # one that was never told how a step ends.
                    opening = f"{system}\n\n{prompt}" if system else (prompt or "")
                    result = agent.send(opening).wait()
                finally:
                    agent.close()
            except CursorAgentError as exc:

                return StepResult(ok=False, output=str(exc))
        usage = getattr(result, "usage", None)
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
        tokens_cached = int(getattr(usage, "cache_read_tokens", 0) or 0)
        if budget.tokens is not None and tokens_in + tokens_out > budget.tokens:

            return StepResult(
                ok=False,
                output="stopped at the token budget",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                tokens_cached=tokens_cached,
            )
        if not captured:

            return StepResult(
                ok=False,
                output="the model returned nothing useful",
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                tokens_cached=tokens_cached,
            )
        flags = {
            flag
            for flag in report.input_schema["properties"]
            if flag != "answer" and captured.get(flag)
        }

        return StepResult(
            ok=True,
            flags=flags,
            answer=str(captured.get("answer") or ""),
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_cached=tokens_cached,
        )


def _key() -> str:
    from odoo_migrator.settings import provider_setting

    return provider_setting("cursor", "api_key")
