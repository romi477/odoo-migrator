"""One agent step. The turn itself belongs to a provider.

Tests still call ``run_agent`` with a ``ModelDouble``. That double is a
``ScriptedProvider``, so the loop under test is the one a live provider runs.
"""

from __future__ import annotations

from odoo_migrator.runner.cartridge import Budget, Step
from odoo_migrator.runner.context import StepContext, StepResult
from odoo_migrator.runner.tools import report_tool, tools_for

# The cap on one response, which is not the budget for the step. The models
# top out at 128K. Passing a 200K run budget as ``max_tokens`` is a 400.
MAX_OUTPUT_TOKENS = 64_000


class NoModelClient(RuntimeError):
    """An agent step ran with no model client behind it."""


def run_agent(
    step: Step,
    ctx: StepContext,
    *,
    client,
    model: str,
    budget: Budget,
) -> StepResult:
    if client is None:
        raise NoModelClient(
            f"step {step.id or step.kind!r} is an agent step and there is no "
            "model client"
        )
    if not getattr(client, "scripted", False):
        raise NoModelClient(
            "a live turn is run by the provider registry, not by a client "
            "object passed in"
        )
    from odoo_migrator.runner.providers.scripted import ScriptedProvider

    return ScriptedProvider(client).run_step(
        system="",
        prompt=step.prompt or "",
        tools=tools_for(step, ctx),
        report=report_tool(step),
        model=model,
        budget=budget,
    )
