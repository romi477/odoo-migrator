"""Errors the Sheller client raises."""


class ShellerError(Exception):
    """A Sheller call failed. Subclasses are the cases a caller must distinguish."""

    def __init__(self, message: str, *, status: int | None = None, detail=None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class ShellerUnreachable(ShellerError):
    """Nothing answered at the daemon URL."""


class ShellerTimeout(ShellerError):
    """The call did not come back in time. The command is still running.

    The daemon takes no timeout for `exec` — `ExecBody` is `{code}` and
    nothing else — so this is ours, on the HTTP call. The session stays busy
    with the command afterwards, which is what `interrupt` is for.
    """


class ShellerBusy(ShellerError):
    """HTTP 409: the session is still running an earlier command.

    Usually the tail of a timeout: the call gave up, the command did not.
    Wait for the session to be ready, or interrupt it.
    """


class ShellerNotOwner(ShellerError):
    """Wrong or missing X-OS-Session-Key. Never retry this with an admin key."""


class ShellerNeedsHuman(ShellerError):
    """HTTP 403 `needs_a_human`: the act is a person's, not a wrong key.

    Since Sheller 1.8.3 an agent's session that opens with `allow_commit:
    true` is refused unless a person has switched its container on for
    agents (1.11.0, *agents may commit*). This used to come back as
    `not_owner`, like every 403, and a person was told about a key when the
    fix was a switch on Sheller's container card. The message is the
    daemon's own: what was refused and what to do about it.
    """


class ShellerSessionDead(ShellerError):
    """HTTP 410: the session's process is gone.

    Its own case because the answer is specific and unlike any other error:
    open a fresh session. The retry loop needs to tell "this session died"
    apart from "the call was wrong", and a shell started against a database
    built by a different Odoo major dies exactly this way.
    """
