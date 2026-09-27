"""Judge the lap against the migration's strictness."""

from __future__ import annotations

import re

from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext, StepResult

# An Odoo log record: `<date> <time> <pid> ERROR <db> <logger>: <message>`.
_LOG_ERROR = re.compile(r"\s(ERROR|CRITICAL)\s")
# Enough to recognise the problem; the whole log is in the run log already.
_SHOWN = 20


def note_log_errors(ctx: StepContext, lines) -> None:
    """Keep the ERROR and CRITICAL records a step's stderr carried."""
    for line in lines or ():
        text = str(line)
        if _LOG_ERROR.search(text):
            ctx.log_errors.append(text.strip())


def run(step: Step, ctx: StepContext) -> StepResult:
    """Baseline at every level: no errors in the log.

    The product spec's done criteria put it in the baseline — the module
    installs, upgrades, its tests pass, and Odoo logged no error doing it. A
    view that fails to validate or a data file that raises during an upgrade
    can log ERROR and still leave the module `installed`, and the lap used to
    pass on that.
    """
    if ctx.log_errors:
        shown = "\n".join(ctx.log_errors[:_SHOWN])
        more = len(ctx.log_errors) - _SHOWN
        tail = f"\n(and {more} more)" if more > 0 else ""

        return StepResult(
            ok=False,
            output=f"Odoo logged {len(ctx.log_errors)} error(s) during this lap:\n{shown}{tail}",
        )
    level = (ctx.module.strictness or ctx.migration.strictness_default or "basic").strip()
    if level == "strict" and not (ctx.prior_answers.get("probe") or "").strip():

        return StepResult(
            ok=False,
            output="strict wants a probe answer, and there is none",
        )

    return StepResult(ok=True, output=level)
