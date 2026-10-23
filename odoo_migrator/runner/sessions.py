"""Open, reopen and look after the two sessions a job holds.

Four places used to do parts of this on their own: the runner opened, the
`reopen` tool reopened, and nothing noticed a session that had died or was
still busy with a command an earlier call gave up on. Every step after that
answered `session_dead` or `session_busy` and the lap was spent on it.
"""

from __future__ import annotations

import contextlib
import time

from odoo_migrator.runner.context import check_stop
from odoo_migrator.sheller.errors import ShellerError

# How often to ask whether a busy session is free again, and for how long
# before giving up on the command it is running.
POLL_SECONDS = 2.0
BUSY_GRACE = 60.0


def open_side(ctx, side: str) -> str:
    """Open ``side`` on the target recorded for it. The id is returned."""
    target = ctx.session_targets.get(side)
    if not target:
        raise ShellerError(f"no target recorded for side {side!r}")
    opened = ctx.sheller.open_session(
        container=target["container"],
        database=target["database"],
        odoo_bin=target["odoo_bin"],
        owner=target.get("owner") or {"kind": "agent"},
        allow_commit=side == "target",
    )
    ctx.sessions[side] = opened["id"]
    ctx.write_keys[side] = opened["write_key"]

    return opened["id"]


# After a person's answer the containers are often still coming back — "I
# restarted it" is a common answer. How long to keep trying before parking
# the job again, and how often.
REOPEN_WAIT = 180.0
REOPEN_POLL = 5.0


def open_sides_patiently(
    ctx, *, wait: float = REOPEN_WAIT, poll: float = REOPEN_POLL, clock=time
) -> str | None:
    """Open every side not open yet, retrying until ``wait`` has passed.

    Returns None when both are open, or the last reason when they are not.
    Never raises for the daemon's answer: the caller decides what a person
    is told. A Stop pressed meanwhile ends the wait (`Stopped`).
    """
    deadline = clock.monotonic() + wait
    reason = ""
    while True:
        try:
            for side in ctx.session_targets:
                if not ctx.sessions.get(side):
                    open_side(ctx, side)

            return None
        except ShellerError as exc:
            reason = str(exc) or type(exc).__name__
        if clock.monotonic() >= deadline:

            return reason
        check_stop(ctx, "while opening the sessions again")
        clock.sleep(poll)


def close_side(ctx, side: str) -> None:
    """Close ``side`` if it is open. A session already gone is not a failure."""
    session_id = ctx.sessions.get(side)
    if not session_id:

        return
    with contextlib.suppress(Exception):
        ctx.sheller.close(session_id, write_key=ctx.write_keys.get(side, ""))
    ctx.sessions[side] = ""


def reopen_side(ctx, side: str) -> str:
    """A fresh process on the same target.

    The running one imported the module's Python when it opened and will not
    import it again: Odoo's loader skips a package already in `sys.modules`,
    so an upgrade in it runs the code as it was, not as the step left it.
    Closing also discards whatever the session held uncommitted.
    """
    close_side(ctx, side)

    return open_side(ctx, side)


def wait_ready(ctx, side: str, deadline: float) -> str:
    """Poll until ``side`` is not busy, or ``deadline`` (monotonic) passes.

    Returns the last state seen: ``ready``, ``busy``, ``dead``, or ``gone``.
    A Stop pressed meanwhile ends the wait (`Stopped`): the upgrade that
    outlived its call waits here for up to an hour.
    """
    state = "busy"
    while True:
        state = session_state(ctx, side)
        if state != "busy" or time.monotonic() >= deadline:

            return state
        check_stop(ctx, f"while waiting for the {side} session")
        time.sleep(POLL_SECONDS)


def session_state(ctx, side: str) -> str:
    session_id = ctx.sessions.get(side)
    if not session_id:

        return "gone"
    try:
        described = ctx.sheller.get_session(session_id)
    except ShellerError:

        return "gone"

    return str(described.get("state") or "gone")


def ensure_usable(ctx, side: str) -> str:
    """Make ``side`` able to take a command. Returns what had to be done, or "".

    A dead or vanished session is reopened. A busy one is the tail of a
    command a call stopped waiting for: it is interrupted and given a grace
    period, then reopened if it still will not come back.
    """
    if side not in ctx.session_targets:

        return ""
    state = session_state(ctx, side)
    if state in ("ready", "starting"):

        return ""
    if state == "busy":
        with contextlib.suppress(ShellerError):
            ctx.sheller.interrupt(ctx.sessions[side], write_key=ctx.write_keys.get(side, ""))
        state = wait_ready(ctx, side, time.monotonic() + BUSY_GRACE)
        if state == "ready":

            return f"{side} session was busy; interrupted it"
    reopen_side(ctx, side)

    return f"{side} session was {state}; opened a fresh one"
