"""Park the job until a human answers — or, at `batch_defer`, do not.

Without a waiter the loop stops and the job stays parked. The native runner
installs a waiter that blocks this step and then lets the loop continue.

A migration set to `batch_defer` records the question as an assumption and
carries on; the checkpoint is where a person reads it. The card offered the
mode for a long time before anything here read it.
"""

from __future__ import annotations

from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext, StepResult

DEFERRED = (
    "Not asked: this migration defers questions to the checkpoint "
    "(batch_defer). The run carried on without an answer."
)
# What the next step reads in place of a person's answer.
DEFERRED_ANSWER = (
    "No one was asked: this migration defers questions to the checkpoint "
    "(batch_defer). Carry on with your best judgement and say in your report "
    "what you assumed."
)


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
    mode = getattr(getattr(ctx, "migration", None), "ask_human_mode", "sync")
    if mode == "batch_defer":
        ctx.assumptions.append({"question": question, "assumption": DEFERRED})

        return StepResult(ok=True, answer=DEFERRED_ANSWER, output=question)
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
