"""Errors the Sheller client and daemon resolver raise."""


class ShellerError(Exception):
    """A Sheller call failed. Subclasses are the cases a caller must distinguish."""

    def __init__(self, message: str, *, status: int | None = None, detail=None):
        super().__init__(message)
        self.status = status
        self.detail = detail


class ShellerUnreachable(ShellerError):
    """Nothing answered at the daemon URL. The resolver's cue to try the next level."""


class ShellerNotOwner(ShellerError):
    """Wrong or missing X-OS-Session-Key. Never retry this with an admin key."""


class ShellerSessionDead(ShellerError):
    """HTTP 410: the session's process is gone.

    Its own case because the answer is specific and unlike any other error:
    open a fresh session. The retry loop needs to tell "this session died"
    apart from "the call was wrong", and a shell started against a database
    built by a different Odoo major dies exactly this way.
    """


class ShellerTooOld(ShellerError):
    """The daemon's /health.version is below min_sheller_version. Do not replace it."""


class ResolveFailed(ShellerError):
    """None of the three resolution levels produced a daemon. Never a silent None."""


class DockerLoginRequired(ResolveFailed):
    """GHCR refused the pull. The image is private; the user must docker login ghcr.io."""


class UnsupportedDockerHost(ResolveFailed):
    """DOCKER_HOST is not a unix socket. The container contract does not support that."""


class DockerPullError(Exception):
    """The Engine could not pull the image. The resolver maps this to DockerLoginRequired."""


class PortClash(Exception):
    """docker run exited because 8765 was taken, and left a container in created state."""

    def __init__(self, created_id: str, message: str = ""):
        super().__init__(message)
        self.created_id = created_id
