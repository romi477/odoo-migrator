"""Judge the lap against the migration's strictness."""

from __future__ import annotations

from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext, StepResult


def run(step: Step, ctx: StepContext) -> StepResult:
    level = (ctx.module.strictness or ctx.migration.strictness_default or "basic").strip()
    if level == "strict" and not (ctx.prior_answers.get("probe") or "").strip():

        return StepResult(
            ok=False,
            output="strict wants a probe answer, and there is none",
        )

    return StepResult(ok=True, output=level)
