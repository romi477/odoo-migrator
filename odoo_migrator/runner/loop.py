"""Walk one cartridge. A failing lap takes the failure lane, then resumes."""

from __future__ import annotations

from odoo_migrator.runner.cartridge import Cartridge
from odoo_migrator.runner.context import StepContext
from odoo_migrator.runner.registry import resolve as _default_resolve

_PARKED = "waiting_for_human"
_NO_PROGRESS = (
    "the failure lane changed nothing, so the next attempt would fail "
    "the same way"
)


def _repeats(failure: str) -> str:

    return f"{failure}\n\n({_NO_PROGRESS})".strip()


def run_job(
    cartridge: Cartridge,
    ctx: StepContext,
    *,
    retry_limit: int,
    resolve=_default_resolve,
) -> str:
    """``resolve`` is a parameter because the runner wraps every step.

    It used to be swapped on this module at runtime and restored in a
    ``finally``. Two jobs in flight — the queue and a Migrate on the tree can
    overlap — would instrument each other, and whichever finished first put
    the bare resolver back under the other one.
    """
    pipeline = list(cartridge.pipeline)
    names = [step.id or step.kind for step in pipeline]
    index = 0
    attempt = 0
    # What the last lap looked like when it failed. A lap that ends in the
    # same state as the one before it will end that way again.
    last_state: tuple[str, str, str] | None = None
    while True:
        attempt += 1
        if attempt > retry_limit:

            return "failed"
        ctx.attempt = attempt
        flags: set[str] = set()
        finished = True
        while index < len(pipeline):
            step = pipeline[index]
            index += 1
            if step.when and step.when not in flags:
                continue
            result = resolve(step.kind)(step, ctx)
            flags.update(result.flags)
            if _PARKED in result.flags:

                return _PARKED
            if result.ok:
                if step.id and result.answer:
                    ctx.prior_answers[step.id] = result.answer
                continue
            ctx.last_failure = result.output or ""
            failed_at = index - 1
            for lane in cartridge.on_failure.pipeline:
                if lane.when and lane.when not in flags:
                    continue
                handled = resolve(lane.kind)(lane, ctx)
                flags.update(handled.flags)
                if _PARKED in handled.flags:

                    return _PARKED
            # The lane is meant to change something: raise a flag that gates a
            # fix, or write files. When the state going into the retry is the
            # one that just failed, the retry is the same lap again. The live
            # run spent five attempts and 740K tokens proving that.
            state = (
                names[failed_at],
                ctx.last_failure,
                getattr(ctx, "previous_diff", "") or "",
            )
            if state == last_state:
                ctx.last_failure = _repeats(ctx.last_failure)

                return "failed"
            last_state = state
            # Never past the step that failed. `resume_at` says how far back
            # to rewind, and a cartridge picks a step late enough to skip the
            # expensive work — but a failure *before* that step means the
            # work was never done. The stock flow resumes at `lint`, so an
            # `understand` that failed used to retry a lint of a module
            # `port` had not written yet, five identical laps in a row.
            index = min(names.index(cartridge.on_failure.retry.resume_at), failed_at)
            finished = False
            break
        if finished:

            return "passed"
