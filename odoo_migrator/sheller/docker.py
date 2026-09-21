"""Docker Engine surface the resolver needs. Fake for tests; subprocess for later."""

import subprocess
from dataclasses import dataclass
from pathlib import Path

from odoo_migrator.sheller.errors import (
    DockerPullError,
    PortClash,
    UnsupportedDockerHost,
)

SHELLER_LABEL = "io.github.romi477.odoo-sheller"
SHELLER_IMAGE_REPO = "ghcr.io/romi477/odoo-sheller"
PUBLISH_IP = "127.0.0.1"
DAEMON_PORT = 8765
SOCKET_IN_CONTAINER = "/var/run/docker.sock"
STATE_IN_CONTAINER = "/data/.odoo-sheller"
DEFAULT_CONTAINER_NAME = "odoo-migrator-sheller"


@dataclass(frozen=True)
class DockerRun:
    image: str
    volumes: list[tuple[str, str]]
    env: dict[str, str]
    publish: tuple[str, int, int]
    name: str = DEFAULT_CONTAINER_NAME
    detach: bool = True


def host_docker_socket(
    *,
    xdg_runtime_dir: str | None = None,
    docker_host: str | None = None,
) -> Path:
    """Host side of the socket mount. Inside the container the path never changes."""
    if docker_host:
        if not docker_host.startswith("unix://"):
            raise UnsupportedDockerHost(
                "Windows is not supported, and neither is a DOCKER_HOST "
                "that is not a unix socket."
            )

        return Path(docker_host.removeprefix("unix://"))
    if xdg_runtime_dir:
        rootless = Path(xdg_runtime_dir) / "docker.sock"
        if rootless.exists():

            return rootless

    return Path("/var/run/docker.sock")


class FakeDocker:
    """Records Engine calls. The suite never talks to a real daemon or socket."""

    def __init__(self):
        self.labeled: list[str] = []
        self.started: list[str] = []
        self.stopped: list[str] = []
        self.removed: list[str] = []
        self.runs: list[DockerRun] = []
        self.pulls: list[str] = []
        self.inspect_calls = 0
        self.health_polls_until_healthy = 1
        self.immediately_healthy = False
        self.fail_run_once: PortClash | None = None
        self.pull_error: Exception | None = None
        self.run_error: Exception | None = None
        self._health_countdown: dict[str, int] = {}

    def add_labeled(self, container_id: str, running: bool = False) -> None:
        self.labeled.append(container_id)
        if running:
            self._health_countdown[container_id] = 0

    def list_labeled(self) -> list[str]:

        return list(self.labeled)

    def start(self, container_id: str) -> None:
        self.started.append(container_id)
        self._health_countdown[container_id] = self.health_polls_until_healthy

    def inspect_health(self, container_id: str) -> str:
        self.inspect_calls += 1
        if self.immediately_healthy:

            return "healthy"
        remaining = self._health_countdown.get(container_id, 0)
        if remaining <= 0:

            return "healthy"
        self._health_countdown[container_id] = remaining - 1

        return "starting"

    def pull(self, image: str) -> None:
        if self.pull_error is not None:
            raise self.pull_error
        self.pulls.append(image)

    def run(self, spec: DockerRun) -> str:
        self.runs.append(spec)
        if self.fail_run_once is not None:
            clash = self.fail_run_once
            self.fail_run_once = None
            raise clash
        if self.run_error is not None:
            raise self.run_error
        container_id = f"run-{len(self.runs)}"
        self._health_countdown[container_id] = (
            0 if self.immediately_healthy else self.health_polls_until_healthy
        )

        return container_id

    def remove(self, container_id: str, force: bool = False) -> None:
        self.removed.append(container_id)

    def stop(self, container_id: str) -> None:
        self.stopped.append(container_id)


class SubprocessDocker:
    """The real Engine, via the docker CLI. Not used by the suite."""

    def list_labeled(self) -> list[str]:
        result = subprocess.run(
            ["docker", "ps", "-aq", "--filter", f"label={SHELLER_LABEL}"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:

            return []

        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def start(self, container_id: str) -> None:
        subprocess.run(
            ["docker", "start", container_id],
            capture_output=True,
            text=True,
            check=True,
        )

    def inspect_health(self, container_id: str) -> str:
        result = subprocess.run(
            [
                "docker",
                "inspect",
                "--format",
                "{{.State.Health.Status}}",
                container_id,
            ],
            capture_output=True,
            text=True,
            check=False,
        )

        return (result.stdout or "").strip() or "starting"

    def pull(self, image: str) -> None:
        result = subprocess.run(
            ["docker", "pull", image], capture_output=True, text=True, check=False
        )
        if result.returncode != 0:
            raise DockerPullError(result.stderr or result.stdout)

    def run(self, spec: DockerRun) -> str:
        args = ["docker", "run", "-d", "--name", spec.name]
        for host, container in spec.volumes:
            args.extend(["-v", f"{host}:{container}"])
        for key, value in spec.env.items():
            args.extend(["-e", f"{key}={value}"])
        host_ip, host_port, container_port = spec.publish
        args.extend(["-p", f"{host_ip}:{host_port}:{container_port}"])
        args.append(spec.image)
        result = subprocess.run(args, capture_output=True, text=True, check=False)
        if result.returncode != 0:
            message = result.stderr or result.stdout
            leftover = _created_leftover()
            if leftover and "address already in use" in message:
                raise PortClash(created_id=leftover, message=message)
            raise RuntimeError(message)

        return result.stdout.strip()

    def remove(self, container_id: str, force: bool = False) -> None:
        args = ["docker", "rm"]
        if force:
            args.append("-f")
        args.append(container_id)
        subprocess.run(args, capture_output=True, text=True, check=False)

    def stop(self, container_id: str) -> None:
        subprocess.run(
            ["docker", "stop", container_id],
            capture_output=True,
            text=True,
            check=False,
        )


def _created_leftover() -> str | None:
    result = subprocess.run(
        [
            "docker",
            "ps",
            "-aq",
            "--filter",
            "status=created",
            "--filter",
            f"label={SHELLER_LABEL}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not ids:

        return None

    return ids[0]
