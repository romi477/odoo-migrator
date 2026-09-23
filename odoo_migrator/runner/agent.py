"""One agent step: tools, then a report, or the budget.

The live client is the Anthropic SDK's beta tool runner. Tests pass a
``ModelDouble`` instead, which replays scripted turns and is allowed to
never converge.
"""

from __future__ import annotations

from odoo_migrator.runner.cartridge import Budget, Step
from odoo_migrator.runner.context import StepContext, StepResult
from odoo_migrator.runner.tools import ToolRefused, report_tool, tools_for


def run_agent(
    step: Step,
    ctx: StepContext,
    *,
    client,
    model: str,
    budget: Budget,
) -> StepResult:
    if getattr(client, "scripted", False):
        return _scripted(step, ctx, client, budget)

    return _anthropic(step, ctx, client, model, budget)


def _scripted(step: Step, ctx: StepContext, client, budget: Budget) -> StepResult:
    """Replay the double until it reports, or until the tool-call budget."""
    tools = {tool.name: tool for tool in tools_for(step, ctx)}
    limit = budget.tool_calls
    while True:
        if limit is not None and client.calls >= limit:
            return StepResult(ok=False, output="stopped at the tool-call budget")
        action = client.next_action()
        if action is None:
            return StepResult(ok=False, output="the model returned nothing useful")
        name = action.get("name") or action.get("tool")
        payload = action.get("input") or {}
        if name == "report":
            return _from_report(step, payload)
        tool = tools.get(name)
        if tool is None:
            return StepResult(ok=False, output=f"unknown tool {name}")
        try:
            tool(**payload)
        except ToolRefused as refused:
            return StepResult(ok=False, output=str(refused))


def _from_report(step: Step, payload: dict) -> StepResult:
    flags = {flag for flag in step.raises if payload.get(flag)}

    return StepResult(ok=True, flags=flags, answer=str(payload.get("answer") or ""))


def _anthropic(step: Step, ctx: StepContext, client, model: str, budget: Budget) -> StepResult:
    """Drive ``client.beta.messages.tool_runner`` until ``report`` or the budget.

    Thinking is adaptive. Effort comes from the migration when it has one.
    Streaming is on so a long turn is not one buffered response.
    """
    import anthropic
    from anthropic import beta_tool

    captured: dict = {}
    runnable = [_as_beta(beta_tool, tool) for tool in tools_for(step, ctx)]
    runnable.append(_report_beta(beta_tool, step, captured))
    kwargs = {
        "max_tokens": budget.tokens or 8192,
        "model": model,
        "tools": runnable,
        "messages": [{"role": "user", "content": step.prompt or ""}],
        "thinking": {"type": "adaptive"},
        "output_config": {"effort": _effort(ctx)},
        "stream": True,
    }
    if budget.tool_calls is not None:
        kwargs["max_iterations"] = budget.tool_calls
    runner = client.beta.messages.tool_runner(**kwargs)
    try:
        runner.until_done()
    except anthropic.APIError as exc:
        if captured:
            return _from_report(step, captured)

        return StepResult(ok=False, output=str(exc))
    if not captured:
        return StepResult(ok=False, output="stopped at the tool-call budget")

    return _from_report(step, captured)


def _effort(ctx: StepContext) -> str:
    raw = getattr(ctx.migration, "model_effort", None)

    return raw or "high"


_TOOL_SCHEMAS = {
    "exec": {
        "type": "object",
        "properties": {
            "side": {"type": "string"},
            "code": {"type": "string"},
        },
        "required": ["side", "code"],
    },
    "source": {
        "type": "object",
        "properties": {
            "side": {"type": "string"},
            "dotted": {"type": "string"},
        },
        "required": ["side", "dotted"],
    },
    "read_file": {
        "type": "object",
        "properties": {"side": {"type": "string"}, "path": {"type": "string"}},
        "required": ["side", "path"],
    },
    "write_file": {
        "type": "object",
        "properties": {
            "side": {"type": "string"},
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["side", "path", "content"],
    },
    "run_test": {
        "type": "object",
        "properties": {"side": {"type": "string"}, "test": {"type": "string"}},
        "required": ["side", "test"],
    },
    "list_tests": {
        "type": "object",
        "properties": {"side": {"type": "string"}, "module": {"type": "string"}},
        "required": ["side"],
    },
}


def _as_beta(beta_tool, tool):
    """Wrap a bound catalog tool so the SDK can advertise and call it."""
    schema = _TOOL_SCHEMAS.get(tool.name) or {
        "type": "object",
        "properties": {"side": {"type": "string"}},
        "required": ["side"],
    }

    def call(**payload: object) -> str:
        """Run this tool on a side the step declared."""
        return tool(**payload)

    return beta_tool(
        call,
        name=tool.name,
        description=f"Run {tool.name} on one side this step declared.",
        input_schema=schema,
    )


def _report_beta(beta_tool, step: Step, captured: dict):
    schema = report_tool(step).input_schema

    def report(**payload: object) -> str:
        """End the step. Set a flag only when it is true."""
        captured.clear()
        captured.update(payload)

        return "reported"

    return beta_tool(
        report,
        name="report",
        description="End the step with an answer and the flags that are true.",
        input_schema=schema,
    )
