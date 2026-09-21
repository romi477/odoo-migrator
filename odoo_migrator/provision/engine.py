"""Docker Engine surface for Odoo compose projects. Fake for the suite."""

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from odoo_migrator.provision.compose_file import published_host_ports
from odoo_migrator.provision.errors import ComposeFailed, DockerUnavailable

WORKING_DIR_LABEL = "com.docker.compose.project.working_dir"


@dataclass
class RunningContainer:
    name: str
    image: str
    status: str
    labels: dict
    host_ports: list[int] = field(default_factory=list)


class FakeComposeEngine:
    """Records compose calls. The suite never talks to an Engine."""

    def __init__(self):
        self.containers: list[RunningContainer] = []
        self.ups: list[Path] = []
        self.restarts: list[Path] = []
        self.rebuilds: list[Path] = []

    def add_container(
        self,
        *,
        name: str,
        image: str,
        status: str,
        labels: dict,
        host_ports: list[int],
    ) -> None:
        self.containers.append(
            RunningContainer(
                name=name,
                image=image,
                status=status,
                labels=labels,
                host_ports=list(host_ports),
            )
        )

    def list_containers(self) -> list[RunningContainer]:

        return list(self.containers)

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
                    )
                )
        except Exception as exc:
            raise DockerUnavailable(
                f"Docker Engine is not reachable: {exc}"
            ) from exc

        return found

    def compose_up(self, project_dir: Path, compose_file: Path) -> None:
        _run_compose(project_dir, compose_file, ["up", "-d"])

    def compose_restart(self, project_dir: Path, compose_file: Path) -> None:
        _run_compose(project_dir, compose_file, ["restart"])

    def compose_rebuild(self, project_dir: Path, compose_file: Path) -> None:
        _run_compose(
            project_dir, compose_file, ["up", "-d", "--build", "--force-recreate"]
        )

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
