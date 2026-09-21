"""Sheller HTTP client, in-memory fake, and the three-level daemon resolver."""

from odoo_migrator.sheller.client import (
    SESSION_KEY_HEADER,
    HttpShellerClient,
    ShellerClient,
    mcp_for_ui,
)
from odoo_migrator.sheller.docker import (
    DockerRun,
    FakeDocker,
    SubprocessDocker,
    host_docker_socket,
)
from odoo_migrator.sheller.errors import (
    DockerLoginRequired,
    DockerPullError,
    PortClash,
    ResolveFailed,
    ShellerError,
    ShellerNotOwner,
    ShellerSessionDead,
    ShellerTooOld,
    ShellerUnreachable,
    UnsupportedDockerHost,
)
from odoo_migrator.sheller.fake import FakeShellerClient
from odoo_migrator.sheller.resolve import (
    ResolveConfig,
    ResolvedDaemon,
    find_local_binary,
    resolve_daemon,
    spawn_local_daemon,
)

__all__ = [
    "SESSION_KEY_HEADER",
    "DockerLoginRequired",
    "DockerPullError",
    "DockerRun",
    "FakeDocker",
    "FakeShellerClient",
    "HttpShellerClient",
    "PortClash",
    "ResolveConfig",
    "ResolveFailed",
    "ResolvedDaemon",
    "ShellerClient",
    "ShellerError",
    "ShellerNotOwner",
    "ShellerSessionDead",
    "ShellerTooOld",
    "ShellerUnreachable",
    "SubprocessDocker",
    "UnsupportedDockerHost",
    "find_local_binary",
    "host_docker_socket",
    "mcp_for_ui",
    "resolve_daemon",
    "spawn_local_daemon",
]
