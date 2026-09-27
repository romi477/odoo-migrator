"""Walk one cartridge. A failing lap takes the failure lane, then resumes."""

from __future__ import annotations

import re

from odoo_migrator.runner.cartridge import Cartridge
from odoo_migrator.runner.context import StepContext
from odoo_migrator.runner.registry import resolve as _default_resolve

_PARKED = "waiting_for_human"
_NO_PROGRESS = (
    "the failure lane changed nothing, so the next attempt would fail "
    "the same way"
)


# What changes between two runs of the same failure without meaning anything:
# object addresses, ids, timestamps, durations. Compared verbatim, a traceback
# never repeats, and the check below never fired.
_NOISE = (
    re.compile(r"0x[0-9a-fA-F]+"),
    re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"),
    re.compile(r"\b[0-9a-fA-F]{16,}\b"),
    re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}(?:[.,]\d+)?"),
    re.compile(r"\b\d+(?:\.\d+)?\s?(?:ms|s)\b"),
)


def _normalised(failure: str) -> str:
    for pattern in _NOISE:
        failure = pattern.sub("_", failure)

    return failure


def _repeats(failure: str, lane_failures: list[str]) -> str:
    notes = [f"({_NO_PROGRESS})", *lane_failures]

    return f"{failure}\n\n" + "\n".join(notes)


def _label(step) -> str:

    return step.id or step.kind


def _keep_answer(ctx: StepContext, step, result) -> None:
    """Under the step's name, so a later prompt can read it.

    An agent step stores its own. A deterministic one did not, and
    `ask_human` is one: the answer a person wrote went nowhere, the retry ran
    with the same context, and it failed the way the lap before it had.
    """
    if result.ok and result.answer:
        ctx.prior_answers[_label(step)] = result.answer
        ctx.last_answer = result.answer


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
    names = [_label(step) for step in pipeline]
    index = 0
    attempt = 0
    # What the last lap looked like when it failed. A lap that ends in the
    # same state as the one before it will end that way again.
    last_state: tuple[str, str, str, str] | None = None
    while True:
        attempt += 1
        if attempt > retry_limit:

            return "failed"
        ctx.attempt = attempt
        ctx.log_errors = []
        flags: set[str] = set()
        ctx.lap_flags = flags
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
                _keep_answer(ctx, step, result)
                continue
            ctx.last_failure = result.output or ""
            failed_at = index - 1
            # A lane step that fails raised nothing, so what it was meant to
            # decide goes undecided. Its reason used to show in the run log
            # only, and the job ended on the original failure as if the lane
            # had looked and found nothing.
            lane_failures: list[str] = []
            for lane in cartridge.on_failure.pipeline:
                if lane.when and lane.when not in flags:
                    continue
                handled = resolve(lane.kind)(lane, ctx)
                flags.update(handled.flags)
                if _PARKED in handled.flags:

                    return _PARKED
                if handled.ok:
                    _keep_answer(ctx, lane, handled)
                else:
                    first = (handled.output or "failed").strip().splitlines()
                    lane_failures.append(f"{_label(lane)} failed: {first[0] if first else 'failed'}")
            # The lane is meant to change something: raise a flag that gates a
            # fix, or write files. When the state going into the retry is the
            # one that just failed, the retry is the same lap again. The live
            # run spent five attempts and 740K tokens proving that.
            # A human answer is a change too: the retry reads it.
            state = (
                names[failed_at],
                _normalised(ctx.last_failure),
                getattr(ctx, "previous_diff", "") or "",
                ctx.prior_answers.get("ask_human", ""),
            )
            if state == last_state:
                ctx.last_failure = _repeats(ctx.last_failure, lane_failures)

                return "failed"
            if lane_failures and attempt >= retry_limit:
                ctx.last_failure = f"{ctx.last_failure}\n\n" + "\n".join(lane_failures)
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
