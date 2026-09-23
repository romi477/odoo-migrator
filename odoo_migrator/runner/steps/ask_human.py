"""Park the job until a human answers.

Without a waiter the loop stops and the job stays parked. The native runner
installs a waiter that blocks this step and then lets the loop continue.
"""

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
    waiter = getattr(ctx, "wait_for_human", None)
    if callable(waiter):
        answer = waiter(question)

        return StepResult(ok=True, answer=answer or "", output=question)

    return StepResult(
        ok=False,
        flags={"waiting_for_human"},
        answer=question,
        output=question,
    )
