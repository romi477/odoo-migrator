"""Where a container's paths are on this machine, from its bind mounts.

The only translation between a path inside a container and one on the host.
Build graph, the file tools and the prompt all go through here, so a path
from a traceback and a path on disk never disagree about which file they
mean. Nothing here touches the filesystem; only `container_mounts` asks
the compose engine.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath


def load(raw: str | None) -> list[dict]:
    """Mounts as stored on a migration. Anything malformed is no mount."""
    try:
        value = json.loads(raw or "[]")
    except ValueError:

        return []
    if not isinstance(value, list):

        return []

    return [
        {
            "host": str(entry["host"]),
            "container": str(entry["container"]),
            "read_only": bool(entry.get("read_only", False)),
        }
        for entry in value
        if isinstance(entry, dict) and entry.get("host") and entry.get("container")
    ]


def _under(path: PurePosixPath, root: PurePosixPath) -> bool:
    # Parts, not strings: /opt/project2 is not under /opt/project.

    return path == root or root in path.parents


def covering(mounts: list[dict], container_path: str) -> dict | None:
    """The mount a container path is under; the deepest one when nested."""
    path = PurePosixPath(container_path)
    best = None
    for mount in mounts:
        root = PurePosixPath(mount["container"])
        if not _under(path, root):
            continue
        if best is None or len(root.parts) > len(PurePosixPath(best["container"]).parts):
            best = mount

    return best


def to_host(mounts: list[dict], container_path: str) -> Path | None:
    mount = covering(mounts, container_path)
    if mount is None:

        return None
    relative = PurePosixPath(container_path).relative_to(mount["container"])

    return Path(mount["host"]) / relative


def to_container(mounts: list[dict], host_path: str) -> str | None:
    path = Path(host_path)
    best = None
    for mount in mounts:
        root = Path(mount["host"])
        if not (path == root or root in path.parents):
            continue
        if best is None or len(root.parts) > len(Path(best["host"]).parts):
            best = mount
    if best is None:

        return None
    relative = path.relative_to(best["host"]).as_posix()

    return str(PurePosixPath(best["container"]) / relative)


def too_broad(
    host: str, *, home: Path | None = None, data_dir: Path | None = None
) -> str | None:
    """Why a mount's host directory must not become a root the agent can read.

    One line in someone else's compose file — `- /:/mnt` or `- ~:/src` —
    would otherwise hand the model the whole disk or this app's own data.
    """
    from odoo_migrator.paths import user_dir

    # Real paths on all three: a host directory that is a symlink to the
    # home directory is the home directory once the tools resolve it.
    home = Path(os.path.realpath(home or Path.home()))
    data_dir = Path(os.path.realpath(data_dir or user_dir()))
    path = Path(os.path.realpath(host))
    if path == Path("/"):

        return "it mounts the whole disk"
    if path == home or path in home.parents:

        return "it mounts the home directory"
    if path == data_dir or path in data_dir.parents:

        return "it holds this app's own data"

    return None


def container_mounts(engine, name: str) -> list[dict] | None:
    """The bind mounts of container `name`, or None when that is not known."""
    from odoo_migrator.provision.errors import DockerUnavailable

    try:
        containers = engine.list_containers()
    except DockerUnavailable:

        return None
    for container in containers:
        if container.name == name:

            return [dict(mount) for mount in container.mounts]

    return None
