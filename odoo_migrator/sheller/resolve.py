"""Attach, start, spawn, or docker-run a Sheller daemon. Never kill one we attached to."""

import shutil
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlparse

from odoo_migrator.sheller.client import mcp_for_ui
from odoo_migrator.sheller.docker import (
    DAEMON_PORT,
    DEFAULT_CONTAINER_NAME,
    PUBLISH_IP,
    SHELLER_IMAGE_REPO,
    SOCKET_IN_CONTAINER,
    STATE_IN_CONTAINER,
    DockerRun,
    host_docker_socket,
)
from odoo_migrator.sheller.errors import (
    DockerLoginRequired,
    DockerPullError,
    PortClash,
    ResolveFailed,
    ShellerTooOld,
)


@dataclass
class ResolveConfig:
    base_url: str = "http://127.0.0.1:8765"
    min_version: str = ""
    image_tag: str = ""
    uid: int = 0
    gid: int = 0
    home: Path = field(default_factory=Path.home)
    xdg_runtime_dir: str | None = None
    docker_host: str | None = None
    wait_timeout: float = 30.0
    poll_interval: float = 0.2
    sleeper: Callable[[float], None] = time.sleep
    container_name: str = DEFAULT_CONTAINER_NAME


@dataclass(frozen=True)
class ResolvedDaemon:
    level: str
    base_url: str
    health: dict
    owned: bool
    mcp: dict | None
    container_id: str | None = None


def resolve_daemon(
    *,
    probe: Callable[[], dict | None],
    docker,
    find_binary: Callable[[], str | None],
    spawn: Callable[[str], object],
    config: ResolveConfig,
) -> ResolvedDaemon:
    """Three levels, in order. Fail explicitly if none of them produce a daemon."""
    health = probe()
    if health is not None:
        _assert_min_version(health, config.min_version)

        return _resolved("attach", health, config, owned=False)

    socket = host_docker_socket(
        xdg_runtime_dir=config.xdg_runtime_dir,
        docker_host=config.docker_host,
    )
    labeled = docker.list_labeled()
    if labeled:
        container_id = labeled[0]
        docker.start(container_id)
        _wait_healthy(docker, container_id, config)
        health = probe()
        if health is None:
            raise ResolveFailed(
                "started the labeled Sheller container but /health did not answer"
            )
        _assert_min_version(health, config.min_version)

        return _resolved(
            "start_container", health, config, owned=False, container_id=container_id
        )

    binary = find_binary()
    if binary:
        spawn(binary)
        health = _wait_probe(probe, config, "spawned odoo-sheller but /health never answered")
        _assert_min_version(health, config.min_version)

        return _resolved("spawn_local", health, config, owned=True)

    if not config.image_tag:
        raise ResolveFailed(
            "Set sheller_image_tag in app settings before provisioning "
            "the published Sheller image. Refusing to pull :latest."
        )

    image = f"{SHELLER_IMAGE_REPO}:{config.image_tag}"
    try:
        docker.pull(image)
    except DockerPullError as exc:
        raise DockerLoginRequired(
            "The Sheller image is private on GHCR. "
            "Run `docker login ghcr.io` with a token that has read:packages, "
            "then retry."
        ) from exc

    spec = DockerRun(
        image=image,
        volumes=[
            (str(socket), SOCKET_IN_CONTAINER),
            (str(config.home / ".odoo-sheller"), STATE_IN_CONTAINER),
        ],
        env={
            "ODOO_SHELLER_UID": str(config.uid),
            "ODOO_SHELLER_GID": str(config.gid),
        },
        publish=(PUBLISH_IP, _port(config.base_url), _port(config.base_url)),
        name=config.container_name,
    )
    try:
        container_id = _run_once_removing_created_leftover(docker, spec)
    except Exception as exc:
        raise ResolveFailed(
            "None of the three Sheller daemon resolutions worked: "
            f"nothing answered on the port, no labeled container to start, "
            f"no local odoo-sheller binary, and docker run failed: {exc}"
        ) from exc

    _wait_healthy(docker, container_id, config)
    health = probe()
    if health is None:
        raise ResolveFailed(
            "docker-run Sheller container became healthy but /health did not answer"
        )
    _assert_min_version(health, config.min_version)

    return _resolved(
        "docker_run", health, config, owned=True, container_id=container_id
    )


def find_local_binary() -> str | None:

    return shutil.which("odoo-sheller")


def spawn_local_daemon(path: str) -> subprocess.Popen:
    """Headless core. The resolver owns this process; an attached daemon it does not."""

    return subprocess.Popen(
        [path],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def _resolved(
    level: str,
    health: dict,
    config: ResolveConfig,
    *,
    owned: bool,
    container_id: str | None = None,
) -> ResolvedDaemon:

    return ResolvedDaemon(
        level=level,
        base_url=config.base_url,
        health=health,
        owned=owned,
        mcp=mcp_for_ui(health),
        container_id=container_id,
    )


def _assert_min_version(health: dict, minimum: str) -> None:
    if not minimum:

        return
    have = str(health.get("version") or "0")
    if _version_parts(have) < _version_parts(minimum):
        raise ShellerTooOld(
            f"Sheller {have} is below min_sheller_version {minimum}. "
            "A daemon that answers may not be ours; it is not stopped."
        )


def _version_parts(version: str) -> tuple[int, ...]:

    return tuple(int(piece) for piece in version.split(".") if piece.isdigit())


def _port(base_url: str) -> int:
    parsed = urlparse(base_url)

    return parsed.port or DAEMON_PORT


def _wait_healthy(docker, container_id: str, config: ResolveConfig) -> None:
    deadline = time.monotonic() + config.wait_timeout
    while True:
        status = docker.inspect_health(container_id)
        if status == "healthy":

            return
        if status == "unhealthy":
            raise ResolveFailed(f"Sheller container {container_id} is unhealthy")
        if time.monotonic() >= deadline:
            raise ResolveFailed(
                f"Timed out waiting for Health.Status=healthy on {container_id}"
            )
        config.sleeper(config.poll_interval)


def _wait_probe(
    probe: Callable[[], dict | None], config: ResolveConfig, message: str
) -> dict:
    deadline = time.monotonic() + config.wait_timeout
    while True:
        health = probe()
        if health is not None:

            return health
        if time.monotonic() >= deadline:
            raise ResolveFailed(message)
        config.sleeper(config.poll_interval)


def _run_once_removing_created_leftover(docker, spec: DockerRun) -> str:
    try:

        return docker.run(spec)
    except PortClash as clash:
        docker.remove(clash.created_id, force=True)

        return docker.run(spec)
