"""The test double, as a provider. It can fail to converge."""

from __future__ import annotations

from odoo_migrator.runner.cartridge import Budget
from odoo_migrator.runner.context import StepResult
from odoo_migrator.runner.providers.base import SettingField
from odoo_migrator.runner.tools import BoundTool, ReportTool, ToolRefused


class ScriptedProvider:
    """Replays a ``ModelDouble`` through the same ``run_step`` live providers use."""

    name = "scripted"
    models: frozenset[str] = frozenset()
    settings: tuple[SettingField, ...] = ()

    def __init__(self, client):
        self._client = client

    def accepts_model(self, name: str) -> bool:
        del name

        return False

    def ensure_ready(self) -> None:

        return None

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
        del system, prompt, model
        by_name = {tool.name: tool for tool in tools}
        limit = budget.tool_calls
        while True:
            if limit is not None and self._client.calls >= limit:

                return StepResult(ok=False, output="stopped at the tool-call budget")
            action = self._client.next_action()
            if action is None:

                return StepResult(ok=False, output="the model returned nothing useful")
            name = action.get("name") or action.get("tool")
            payload = action.get("input") or {}
            if name == "report":

                return _from_report(report, payload)
            tool = by_name.get(name)
            if tool is None:

                return StepResult(ok=False, output=f"unknown tool {name}")
            try:
                tool(**payload)
            except ToolRefused as refused:

                return StepResult(ok=False, output=str(refused))


def _from_report(report: ReportTool, payload: dict) -> StepResult:
    flags = {
        flag
        for flag in report.input_schema["properties"]
        if flag != "answer" and payload.get(flag)
    }

    return StepResult(ok=True, flags=flags, answer=str(payload.get("answer") or ""))
