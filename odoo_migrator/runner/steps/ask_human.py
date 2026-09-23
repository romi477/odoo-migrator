"""Park the job. The loop stops; nothing here resumes it."""

from __future__ import annotations

from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext, StepResult


def run(step: Step, ctx: StepContext) -> StepResult:
    question = (step.prompt or "").strip() or "A checkpoint is open."
    ctx.emit(
        {
            "kind": "ask_human",
            "job_id": ctx.job_id,
            "question": question,
        }
    )

    return StepResult(
        ok=False,
        flags={"waiting_for_human"},
        answer=question,
        output=question,
    )
