"""Docker Engine surface for Odoo compose projects. Fake for the suite."""

import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from odoo_migrator.provision.compose_file import published_host_ports
from odoo_migrator.provision.errors import ComposeFailed, DockerUnavailable

WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"
SERVICE_LABEL = "com.docker.compose.service"
# Compose's hash of the service's configuration when it made the container.
CONFIG_HASH_LABEL = "com.docker.compose.config-hash"


@dataclass
class RunningContainer:
    name: str
    image: str
    status: str
    labels: dict
    host_ports: list[int] = field(default_factory=list)
    # Bind mounts only, as {"host", "container", "read_only"}. Volumes have
    # no directory on this machine a person can open.
    mounts: list[dict] = field(default_factory=list)
    # When the container was created, as a Unix time, and the image it runs:
    # what a rebuild would change is compared with these.
    created: float | None = None
    image_id: str = ""


class FakeComposeEngine:
    """Records compose calls. The suite never talks to an Engine."""

    def __init__(self):
        self.containers: list[RunningContainer] = []
        self.ups: list[Path] = []
        self.restarts: list[Path] = []
        self.rebuilds: list[Path] = []
        self.stops: list[Path] = []
        # image id -> when it was built, as a Unix time.
        self.images: dict[str, float] = {}
        # service -> the hash its compose file gives it now.
        self.config_hashes: dict[str, str] = {}

    def add_container(
        self,
        *,
        name: str,
        image: str,
        status: str,
        labels: dict,
        host_ports: list[int],
        mounts: list[dict] | None = None,
        created: float | None = None,
        image_id: str = "",
    ) -> None:
        self.containers.append(
            RunningContainer(
                name=name,
                image=image,
                status=status,
                labels=labels,
                host_ports=list(host_ports),
                mounts=list(mounts or []),
                created=created,
                image_id=image_id,
            )
        )

    def list_containers(self) -> list[RunningContainer]:

        return list(self.containers)

    def image_created(self, image_id: str) -> float | None:

        return self.images.get(image_id)

    def config_hash(self, project_dir: Path, compose_file: Path, service: str) -> str | None:

        return self.config_hashes.get(service)

    def compose_up(self, project_dir: Path, compose_file: Path) -> None:
        resolved = project_dir.resolve()
        self.ups.append(resolved)
        ports = published_host_ports(compose_file) or [8069]
        self.containers.append(
            RunningContainer(
                name=f"{resolved.name}-odoo-1",
                image="odoo:17.0",
                status="running",
                labels={WORKING_DIR_LABEL: str(resolved)},
                host_ports=list(ports),
            )
        )

    def compose_restart(self, project_dir: Path, compose_file: Path) -> None:
        self.restarts.append(project_dir.resolve())

    def compose_rebuild(self, project_dir: Path, compose_file: Path) -> None:
        self.rebuilds.append(project_dir.resolve())

    def compose_stop(self, project_dir: Path, compose_file: Path) -> None:
        resolved = project_dir.resolve()
        self.stops.append(resolved)
        # Stopping keeps the container; only its status changes. A removed
        # container would answer describe_side the same way an Up never ran.
        for container in self.containers:
            if container.labels.get(WORKING_DIR_LABEL) == str(resolved):
                container.status = "exited"


class DockerComposeEngine:
    """List/inspect through the Docker SDK; up/restart/rebuild through Compose V2."""

    def __init__(self, client=None):
        self._docker = client

    def list_containers(self) -> list[RunningContainer]:
        try:
            found = []
            for container in self._client().containers.list(all=True):
                found.append(
                    RunningContainer(
                        name=container.name,
                        image=container.attrs.get("Config", {}).get("Image") or "",
                        status=container.status,
                        labels=dict(container.labels or {}),
                        host_ports=_host_ports(container),
                        mounts=_bind_mounts(container),
                        created=_unix_time(container.attrs.get("Created")),
                        image_id=container.attrs.get("Image") or "",
                    )
                )
        except Exception as exc:
            raise DockerUnavailable(
                f"Docker Engine is not reachable: {exc}"
            ) from exc

        return found

    def image_created(self, image_id: str) -> float | None:
        """When an image was built; None when it cannot be read."""
        try:
            image = self._client().images.get(image_id)
        except Exception:  # noqa: BLE001 - a hint never breaks the card.

            return None

        return _unix_time(image.attrs.get("Created"))

    def config_hash(self, project_dir: Path, compose_file: Path, service: str) -> str | None:
        """The hash compose gives the service's configuration now, which it
        stores on the container it makes; None when compose cannot say."""
        try:
            result = subprocess.run(
                ["docker", "compose", "-f", str(compose_file), "config", f"--hash={service}"],
                cwd=project_dir,
                capture_output=True,
                text=True,
                check=False,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):

            return None
        for line in result.stdout.splitlines():
            name, _, digest = line.strip().partition(" ")
            if name == service and digest:

                return digest.strip()

        return None

    def compose_up(self, project_dir: Path, compose_file: Path) -> None:
        _run_compose(project_dir, compose_file, ["up", "-d"])

    def compose_restart(self, project_dir: Path, compose_file: Path) -> None:
        _run_compose(project_dir, compose_file, ["restart"])

    def compose_rebuild(self, project_dir: Path, compose_file: Path) -> None:
        _run_compose(
            project_dir, compose_file, ["up", "-d", "--build", "--force-recreate"]
        )

    def compose_stop(self, project_dir: Path, compose_file: Path) -> None:
        # `stop`, not `down`: the container and its volume survive, so the new
        # side can be brought back up without losing the migration's state.
        _run_compose(project_dir, compose_file, ["stop"])

    def _client(self):
        if self._docker is None:
            import docker

            try:
                self._docker = docker.from_env()
            except Exception as exc:
                raise DockerUnavailable(
                    f"Docker Engine is not reachable: {exc}"
                ) from exc

        return self._docker


def _unix_time(stamp) -> float | None:
    """Docker's `2026-10-17T09:12:33.123456789Z`, which has more digits of a
    second than Python reads, as a Unix time; None for anything else."""
    if not isinstance(stamp, str) or not stamp:

        return None
    text = stamp.strip().replace("Z", "+00:00")
    head, dot, rest = text.partition(".")
    if dot:
        digits = rest[: len(rest) - len(rest.lstrip("0123456789"))]
        zone = rest[len(digits):]
        text = f"{head}.{digits[:6]}{zone}"
    try:

        return datetime.fromisoformat(text).timestamp()
    except ValueError:

        return None


def _bind_mounts(container) -> list[dict]:
    found = []
    for entry in container.attrs.get("Mounts") or []:
        if entry.get("Type") != "bind":
            continue
        host = entry.get("Source") or ""
        inside = entry.get("Destination") or ""
        if host and inside:
            found.append(
                {"host": host, "container": inside, "read_only": not entry.get("RW", True)}
            )

    return found


def _host_ports(container) -> list[int]:
    ports: list[int] = []
    for bindings in (container.ports or {}).values():
        if not bindings:
            continue
        for binding in bindings:
            host = (binding or {}).get("HostPort")
            if host:
                ports.append(int(host))

    return ports


def _run_compose(project_dir: Path, compose_file: Path, args: list[str]) -> None:
    cmd = ["docker", "compose", "-f", str(compose_file), *args]
    result = subprocess.run(
        cmd,
        cwd=project_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise ComposeFailed(result.stderr.strip() or result.stdout.strip() or "compose failed")
