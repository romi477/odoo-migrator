"""Click through the menus of the ported module with Odoo's own clickbot.

Odoo validates views when it installs a module; it does not render them. A
filter on a field that is gone, an OWL component that crashes, a menu whose
action the user cannot read: each installs cleanly and breaks the first time a
person opens the menu. Sheller 1.12.0 runs Odoo's clickbot over one installed
module's menus; this step puts that run in the flow, after `tests`.

It runs only when the container can. A run that did not happen is never a
pass: an old daemon, a container with no browser, a skipped run and a module
with nothing to click each end the step with `ok=True`, and all of them but
the last leave a finding saying the menus are unproven. The container is a
person's to set up; nothing in the port would fix it, and the failure lane
would only spend laps on it. A menu the port broke fails the lap and goes to
the failure lane, as a red test does. Nothing is written: the clickbot trims
`load_menus` in memory and runs in a test transaction.
"""

from __future__ import annotations

import time

from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.clickbot_answer import Reading, menu_lines, read, summary
from odoo_migrator.runner.context import StepContext, StepResult, check_stop
from odoo_migrator.runner.sessions import reopen_side
from odoo_migrator.runner.tools import _elapsed, _record
from odoo_migrator.sheller.errors import ShellerError, ShellerTimeout

# Seconds the daemon lets one run take. Past it, the daemon stops the run.
CLICK_TIMEOUT = 900.0
# The first Sheller that has the route.
CLICKBOT_SINCE = (1, 12, 0)


def run(step: Step, ctx: StepContext) -> StepResult:
    del step
    name = ctx.module.technical_name
    if not ctx.sessions.get("target"):

        return StepResult(ok=False, output="no target session")
    blocked = _cannot_run(ctx, "target")
    if blocked:

        return _did_not_run(ctx, name, blocked)
    target, why = _click(ctx, "target", name)
    if target is None:

        return _did_not_run(ctx, name, why)
    if target.outcome == "no_menus":

        return StepResult(ok=True, output=f"{name} clickbot: no menu of its own to click")
    if target.outcome == "setup":

        return StepResult(
            ok=False,
            flags={"tests_failed"},
            output=f"{name} clickbot: the daemon answered {target.reason} after a green upgrade",
        )
    if target.outcome != "ran":

        return _did_not_run(ctx, name, target.reason)
    _note_what_was_not_proved(ctx, name, target)
    if not target.broken:
        said = f"{name} clickbot: {summary(target)}"
        if target.notes:
            said += f"; {'; '.join(target.notes)}"

        return StepResult(ok=True, output=said)

    return _failed(ctx, name, target)


def _failed(ctx: StepContext, name: str, target: Reading) -> StepResult:
    baseline, why = _baseline(ctx, name)
    broke_on_source: dict = {}
    if baseline is None:
        ours, both = list(target.broken), []
    else:
        broke_on_source = {entry.xmlid: entry for entry in baseline.broken}
        ours = [entry for entry in target.broken if entry.xmlid not in broke_on_source]
        both = [entry for entry in target.broken if entry.xmlid in broke_on_source]
    for entry in both:
        _finding(
            ctx,
            f"Menu {entry.xmlid} of {name} fails on the source too: "
            f"{broke_on_source[entry.xmlid].reason}",
        )
    if not ours:

        return StepResult(
            ok=True,
            output=f"{name} clickbot: {summary(target)}; {len(both)} menu(s) fail on the source too",
        )
    lines = [
        f"{name} clickbot: {len(ours)} menu(s) the port broke (as admin, debug on)",
        *menu_lines(ours),
    ]
    if both:
        lines.append(
            "not broken by the port, failing on the source too: "
            + ", ".join(entry.xmlid for entry in both)
        )
    if target.unreached:
        lines.append(f"unreached: {', '.join(target.unreached)}")
    if baseline is None:
        lines.append(
            f"no baseline was taken on the source ({why}): every broken menu is counted as the port's"
        )

    return StepResult(ok=False, flags={"tests_failed"}, output="\n".join(lines))


def _baseline(ctx: StepContext, name: str) -> tuple[Reading | None, str]:
    """The same run on the source, to tell what the port broke from what was
    already broken. A clickbot cannot click one menu alone, so it is the whole
    module again, and only a lap with broken menus pays for it.

    None, and why, when the source cannot give one: erring toward repair, the
    lap is retried and a person reads the review either way.
    """
    if not ctx.sessions.get("source"):

        return None, "no source session"
    blocked = _cannot_run(ctx, "source")
    if blocked:

        return None, blocked
    reading, why = _click(ctx, "source", name)
    if reading is None:

        return None, why
    if reading.outcome != "ran":

        return None, reading.reason

    return reading, ""


def _cannot_run(ctx: StepContext, side: str) -> str:
    """Why the run cannot happen here, or "". Read before the run is spent."""
    version = _daemon_version(ctx)
    if version and version < CLICKBOT_SINCE:

        return (
            f"the clickbot needs Sheller 1.12.0; this one is "
            f"{'.'.join(str(piece) for piece in version)}"
        )
    try:
        session = ctx.sheller.get_session(ctx.sessions[side])
    except ShellerError:
        # Not knowing is not a reason to skip: the run itself will say.

        return ""
    box = session.get("clickbot")
    if isinstance(box, dict) and box.get("available") is False:

        return f"the container cannot run a browser ({box.get('reason') or 'no reason given'})"

    return ""


def _daemon_version(ctx: StepContext) -> tuple[int, ...]:
    try:
        text = str(ctx.sheller.health().get("version") or "")
    except ShellerError:

        return ()

    return tuple(int(piece) for piece in text.split(".") if piece.isdigit())


def _click(ctx: StepContext, side: str, name: str) -> tuple[Reading | None, str]:
    """One run on ``side``. A reading, or None and why there is none.

    A Stop is heard here, before each run. A run past the call is still going
    in its session; the runner mends both sessions before the next step.
    """
    check_stop(ctx, "during the clickbot")
    session_id = ctx.sessions.get(side, "")
    started = time.perf_counter()
    try:
        answer = ctx.sheller.click_module(
            session_id, name, write_key=ctx.write_keys.get(side, ""), timeout=CLICK_TIMEOUT
        )
    except ShellerTimeout:
        why = f"the run ran past {CLICK_TIMEOUT:.0f}s"
        _record(ctx, "click_module", side, session_id, "", _elapsed(started), False, f"{name} · {why}")

        return None, why
    except ShellerError as exc:
        why = (
            "this daemon has no clickbot route (Sheller below 1.12.0)"
            if exc.status == 404
            else str(exc)
        )
        _record(ctx, "click_module", side, session_id, "", _elapsed(started), False, f"{name} · {why}")

        return None, why
    reading = read(answer)
    _record(
        ctx, "click_module", side, session_id, str(answer.get("id") or ""), _elapsed(started),
        reading.outcome == "ran" and not reading.broken,
        f"{name} · {summary(reading) if reading.outcome == 'ran' else reading.reason}",
    )
    if reading.restore_failed:
        _reopen_after_restore_failed(ctx, side, name, reading)

    return reading, ""


def _reopen_after_restore_failed(ctx: StepContext, side: str, name: str, reading: Reading) -> None:
    """The shell is left answering menus through Sheller's wrapper. Open a
    fresh one before anything else uses this side."""
    try:
        reopen_side(ctx, side)
        said = f"the {side} session was opened again"
    except ShellerError as exc:
        said = f"the {side} session could not be opened again: {exc}"
    _finding(ctx, f"The clickbot of {name} on the {side}: {reading.reason}; {said}.")


def _did_not_run(ctx: StepContext, name: str, reason: str) -> StepResult:
    _finding(
        ctx,
        f"The clickbot of {name} did not run on the target ({reason}): its menus are unproven.",
    )

    return StepResult(ok=True, output=f"{name} clickbot did not run: {reason}")


def _note_what_was_not_proved(ctx: StepContext, name: str, target: Reading) -> None:
    if target.unreached:
        _finding(ctx, f"The clickbot of {name} did not reach: {', '.join(target.unreached)}.")
    for note in target.notes:
        _finding(ctx, f"The clickbot of {name}: {note}.")


def _finding(ctx: StepContext, text: str) -> None:
    finding = {"text": text, "source": "clickbot"}
    if finding not in ctx.findings:
        ctx.findings.append(finding)
