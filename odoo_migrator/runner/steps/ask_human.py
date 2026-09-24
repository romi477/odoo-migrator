"""Park the job until a human answers.

Without a waiter the loop stops and the job stays parked. The native runner
installs a waiter that blocks this step and then lets the loop continue.
"""

from __future__ import annotations

from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext, StepResult


def run(step: Step, ctx: StepContext) -> StepResult:
    """Ask with what the last agent step said.

    `ask_human` is deterministic, so validation refuses a prompt on it and
    the cartridge has nothing to write here. The question the human needs is
    the diagnosis that raised `needs_human`, which is the answer of the step
    that ran just before this one.
    """
    question = (ctx.last_answer or "").strip() or "A checkpoint is open."
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
