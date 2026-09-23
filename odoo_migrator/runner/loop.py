"""Walk one cartridge. A failing lap takes the failure lane, then resumes."""

from __future__ import annotations

from odoo_migrator.runner.cartridge import Cartridge
from odoo_migrator.runner.context import StepContext
from odoo_migrator.runner.registry import resolve

_PARKED = "waiting_for_human"


def run_job(cartridge: Cartridge, ctx: StepContext, *, retry_limit: int) -> str:
    pipeline = list(cartridge.pipeline)
    names = [step.id or step.kind for step in pipeline]
    index = 0
    attempt = 0
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
            for lane in cartridge.on_failure.pipeline:
                if lane.when and lane.when not in flags:
                    continue
                handled = resolve(lane.kind)(lane, ctx)
                flags.update(handled.flags)
                if _PARKED in handled.flags:

                    return _PARKED
            index = names.index(cartridge.on_failure.retry.resume_at)
            finished = False
            break
        if finished:

            return "passed"
