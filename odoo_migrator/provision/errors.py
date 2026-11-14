"""Errors compose provisioning raises. Silent skip is the failure this avoids."""


class ComposeError(Exception):
    """The project containers could not be provisioned."""


class ComposeMissing(ComposeError):
    """The project path has no compose file. The app does not generate one."""


class ComposeUnreadable(ComposeError):
    """The compose file is not YAML this app can read: saved half-edited, or
    not a mapping at the top. The message names the file."""


class PortStolen(ComposeError):
    """The published port is held by a container that is not this project."""


class NothingRunning(ComposeError):
    """No container from this compose project is running: Up first, never steal."""


class ComposeFailed(ComposeError):
    """docker compose itself refused."""


class DockerUnavailable(ComposeError):
    """The Engine was not reachable. The suite never takes this path."""
