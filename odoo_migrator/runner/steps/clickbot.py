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
the failure lane, as a red test does. So do menus admin no longer sees on the
target (`no_visible_menus`) when the source clicked them. The source is clicked
only when the target needs comparing, once for the whole job. Nothing is
written: the clickbot trims `load_menus` in memory and runs in a test
transaction.
"""

from __future__ import annotations

import time

from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.clickbot_answer import Reading, menu_lines, read, summary
from odoo_migrator.runner.context import StepContext, StepResult, check_stop
from odoo_migrator.runner.sessions import ensure_usable, reopen_side
from odoo_migrator.runner.tools import _elapsed, _record
from odoo_migrator.sheller.errors import ShellerError, ShellerTimeout
from odoo_migrator.sheller.version import CLICKBOT_SINCE, daemon_version

# Seconds the daemon lets one run take. Past it, the daemon stops the run.
CLICK_TIMEOUT = 900.0


def run(step: Step, ctx: StepContext) -> StepResult:
    del step
    name = ctx.module.technical_name
    if not ctx.sessions.get("target"):

        return StepResult(ok=False, output="no target session")
    # The daemon's version cannot differ between the sides: asked once.
    version = daemon_version(ctx.sheller)
    blocked = _cannot_run(ctx, "target", version)
    if blocked:

        return _did_not_run(ctx, name, blocked)
    target, why = _click(ctx, "target", name, version)
    if target is None:

        return _did_not_run(ctx, name, why)
    if target.outcome == "no_menus":

        return StepResult(ok=True, output=f"{name} clickbot: no menu of its own to click")
    if target.outcome == "none_visible":

        return _none_visible(ctx, name, target, version)
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

    return _failed(ctx, name, target, version)


def _none_visible(
    ctx: StepContext, name: str, target: Reading, version: tuple[int, ...]
) -> StepResult:
    """The module has menus and admin sees none with an action. What the source
    clicked, the port lost; nothing on either side is nothing to prove."""
    source, why = _baseline(ctx, name, version)
    hidden = f"{target.hidden} hidden from admin"
    if source is None:
        _finding(
            ctx,
            f"The clickbot of {name} found menus on the target and none visible to admin "
            f"({hidden}); the source could not be asked ({why}): they are unproven.",
            quiet=True,
        )

        return StepResult(ok=True, output=f"{name} clickbot: none visible to admin ({hidden})")
    if source.outcome != "ran" or not source.clicked_names:

        return StepResult(
            ok=True,
            output=f"{name} clickbot: none visible to admin on either side ({hidden})",
        )
    head = (
        f"{name} clickbot: admin sees no menu of the module on the target ({hidden}), "
        f"and the source clicked {len(source.clicked_names)}: the port lost them "
        "(an action gone, a group admin is not in)"
    )
    lines = [head, *source.clicked_names]

    return StepResult(ok=False, flags={"tests_failed"}, output="\n".join(lines))


def _failed(
    ctx: StepContext, name: str, target: Reading, version: tuple[int, ...]
) -> StepResult:
    baseline, why = _baseline(ctx, name, version)
    broke_on_source: dict = {}
    if baseline is None or baseline.outcome != "ran":
        # No baseline, or no menu of the module on the source to break: every
        # broken one is the port's.
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


def _baseline(
    ctx: StepContext, name: str, version: tuple[int, ...]
) -> tuple[Reading | None, str]:
    """The same run on the source, to tell what the port broke from what was
    already broken. A clickbot cannot click one menu alone, so it is the whole
    module again, and only a lap with broken menus pays for it.

    None, and why, when the source cannot give one: erring toward repair, the
    lap is retried and a person reads the review either way. A reading is
    `ran`, `no_menus` or `none_visible`, and is kept for the rest of the job:
    the source does not change between laps.
    """
    if isinstance(ctx.clickbot_source, Reading):

        return ctx.clickbot_source, ""
    if not ctx.sessions.get("source"):

        return None, "no source session"
    blocked = _cannot_run(ctx, "source", version)
    if blocked:

        return None, blocked
    reading, why = _click(ctx, "source", name, version)
    if reading is None:

        return None, why
    if reading.outcome not in ("ran", "no_menus", "none_visible"):

        return None, reading.reason
    ctx.clickbot_source = reading

    return reading, ""


def _cannot_run(ctx: StepContext, side: str, version: tuple[int, ...]) -> str:
    """Why the run cannot happen here, or "". Read before the run is spent."""
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


def _click(
    ctx: StepContext, side: str, name: str, version: tuple[int, ...], *, again: bool = False
) -> tuple[Reading | None, str]:
    """One run on ``side``. A reading, or None and why there is none.

    A Stop is heard here, before each run. A run past the call is still going
    in its session, and this side gets a fresh one.
    """
    check_stop(ctx, "during the clickbot")
    session_id = ctx.sessions.get(side, "")
    started = time.perf_counter()
    try:
        answer = ctx.sheller.click_module(
            session_id, name, write_key=ctx.write_keys.get(side, ""), timeout=CLICK_TIMEOUT
        )
    except ShellerTimeout:
        # Still running in the session, with `load_menus` patched. An interrupt
        # is what Sheller names as the likely cause of a menu tree it could not
        # put back, and it says so only in its journal: the shell is not
        # trusted again, a fresh one is opened.
        why = f"the run ran past {CLICK_TIMEOUT:.0f}s{_fresh(ctx, side)}"
        _record(ctx, "click_module", side, session_id, "", _elapsed(started), False, f"{name} · {why}")

        return None, why
    except ShellerError as exc:
        has_route = bool(version) and version >= CLICKBOT_SINCE
        if exc.status == 404 and has_route and not again:
            # The route is there, so the 404 is `session_or_404`: the session
            # went away (a daemon restarted). Once, on a fresh one.
            try:
                mended = ensure_usable(ctx, side)
            except ShellerError:
                mended = ""
            if mended:

                return _click(ctx, side, name, version, again=True)
        why = (
            "this daemon has no clickbot route (Sheller below 1.12.0)"
            if exc.status == 404 and not has_route
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


def _fresh(ctx: StepContext, side: str) -> str:
    """A fresh session on ``side``, as words after a reason."""
    try:
        reopen_side(ctx, side)
    except ShellerError as exc:

        return f"; the {side} session could not be opened again: {exc}"

    return f"; the {side} session was opened again"


def _reopen_after_restore_failed(ctx: StepContext, side: str, name: str, reading: Reading) -> None:
    """The shell is left answering menus through Sheller's wrapper. Open a
    fresh one before anything else uses this side."""
    try:
        reopen_side(ctx, side)
        said = f"the {side} session was opened again"
    except ShellerError as exc:
        said = f"the {side} session could not be opened again: {exc}"
    _finding(ctx, f"The clickbot of {name} on the {side}: {reading.reason}; {said}.", quiet=True)


def _did_not_run(ctx: StepContext, name: str, reason: str) -> StepResult:
    _finding(
        ctx,
        f"The clickbot of {name} did not run on the target ({reason}): its menus are unproven.",
        quiet=True,
    )

    return StepResult(ok=True, output=f"{name} clickbot did not run: {reason}")


def _note_what_was_not_proved(ctx: StepContext, name: str, target: Reading) -> None:
    if target.unreached:
        _finding(
            ctx, f"The clickbot of {name} did not reach: {', '.join(target.unreached)}.", quiet=True
        )
    for note in target.notes:
        _finding(ctx, f"The clickbot of {name}: {note}.", quiet=True)


def _finding(ctx: StepContext, text: str, *, quiet: bool = False) -> None:
    """A finding with `clickbot` as its source. A quiet one is what the
    environment kept from being proved: it is said of every module alike, so
    it does not stop a running queue for its review (`Finding.quiet`)."""
    finding = {"text": text, "source": "clickbot", "quiet": quiet}
    if finding not in ctx.findings:
        ctx.findings.append(finding)
