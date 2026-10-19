"""Judge the lap against the migration's strictness."""

from __future__ import annotations

import re

from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext, StepResult

# An Odoo log record, as odoo-bin writes it and Sheller passes it through:
# `2026-09-27 10:00:00,123 1 ERROR acme odoo.addons.sale: ...`. Anchored on
# the whole prefix: the bare word also turns up inside tracebacks and test
# output, and failing a green lap on one of those costs more than missing a
# record.
_LOG_ERROR = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \d+ (?:ERROR|CRITICAL) \S+ \S+: "
)
# A terminal's colours, in case a container runs odoo-bin with them on.
_ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Enough to recognise the problem; the whole log is in the run log already.
_SHOWN = 20


def note_log_errors(ctx: StepContext, lines) -> None:
    """Keep the ERROR and CRITICAL records a step's stderr carried."""
    for line in lines or ():
        text = _ANSI.sub("", str(line)).strip()
        if _LOG_ERROR.match(text):
            ctx.log_errors.append(text)


def run(step: Step, ctx: StepContext) -> StepResult:
    """Errors in the log: a finding at `basic`, a failed lap above it.

    The product spec's baseline is that Odoo logged no error while the module
    installed, upgraded and ran its tests. A view that fails to validate can
    log ERROR and still leave the module `installed`, and the lap used to
    pass on that silently. On a shared database the record is often another
    module's, so `basic` writes it down for the checkpoint instead of
    failing the lap; `enhanced` and `strict` fail it.
    """
    level = (ctx.module.strictness or ctx.migration.strictness_default or "basic").strip()
    if "behaviour_differs" in ctx.lap_flags:
        # At every level. For a module without tests this is the only
        # evidence of behaviour there is, and a module that behaves
        # differently is not ported however cleanly it installs.
        found = (ctx.prior_answers.get("probe") or "").strip() or "(the probe gave no detail)"

        return StepResult(
            ok=False,
            output=f"the probe found behaviour that differs between the versions:\n{found}",
        )
    if "button_broken" in ctx.lap_flags:
        # At every level, like a behaviour that differs: a button that works
        # on the source and raises on the target is a break a person meets on
        # the first click.
        found = (ctx.prior_answers.get("click") or "").strip() or "(the click step gave no detail)"

        return StepResult(
            ok=False,
            output=f"a button works on the source version and breaks on the target:\n{found}",
        )
    if ctx.log_errors:
        shown = "\n".join(ctx.log_errors[:_SHOWN])
        more = len(ctx.log_errors) - _SHOWN
        tail = f"\n(and {more} more)" if more > 0 else ""
        said = f"Odoo logged {len(ctx.log_errors)} error(s) during this lap:\n{shown}{tail}"
        if level != "basic":

            return StepResult(ok=False, output=said)
        ctx.findings.append({"text": said, "source": "verdict: odoo log"})
    if level == "strict" and not (ctx.prior_answers.get("probe") or "").strip():

        return StepResult(
            ok=False,
            output="strict wants a probe answer, and there is none",
        )

    return StepResult(ok=True, output=level)
