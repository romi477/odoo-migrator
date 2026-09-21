"""Three-level daemon resolver. Fake Docker — the suite never talks to an Engine."""

from pathlib import Path

import pytest

from odoo_migrator.sheller import (
    DockerLoginRequired,
    DockerPullError,
    FakeDocker,
    PortClash,
    ResolveConfig,
    ResolveFailed,
    ShellerTooOld,
    UnsupportedDockerHost,
    mcp_for_ui,
    resolve_daemon,
)

MCP = {
    "command": "docker",
    "args": ["exec", "-i", "9f31c2ab77e1", "python", "-m", "odoo_sheller.mcp"],
}


def _health(*, version="1.7.0", container=False, mcp=None):

    return {"ok": True, "version": version, "container": container, "mcp": mcp}


def _config(tmp_path: Path | None = None, **kwargs) -> ResolveConfig:
    home = tmp_path if tmp_path is not None else Path("/home/me")
    values = {
        "base_url": "http://127.0.0.1:8765",
        "min_version": "1.7.0",
        "image_tag": "1.7.0",
        "uid": 501,
        "gid": 20,
        "home": home,
        "wait_timeout": 1.0,
        "poll_interval": 0.0,
        "sleeper": lambda _seconds: None,
    }
    values.update(kwargs)

    return ResolveConfig(**values)


def _resolve(*, probe, docker=None, find_binary=None, spawn=None, config=None):

    return resolve_daemon(
        probe=probe,
        docker=docker if docker is not None else FakeDocker(),
        find_binary=find_binary or (lambda: None),
        spawn=spawn or (lambda _path: None),
        config=config or _config(),
    )


def test_health_answer_attaches_and_does_not_touch_docker():
    docker = FakeDocker()
    result = _resolve(probe=lambda: _health(mcp=MCP), docker=docker)

    assert result.level == "attach"
    assert result.owned is False
    assert result.mcp == MCP
    assert docker.started == []
    assert docker.stopped == []
    assert docker.removed == []
    assert docker.runs == []


def test_too_old_attached_daemon_is_refused_not_replaced():
    docker = FakeDocker()

    with pytest.raises(ShellerTooOld, match="1.6.0"):
        _resolve(probe=lambda: _health(version="1.6.0"), docker=docker)

    assert docker.stopped == []
    assert docker.runs == []
    assert docker.started == []


def test_stopped_labeled_container_is_started_and_waited():
    docker = FakeDocker()
    docker.add_labeled("abc123", running=False)
    docker.health_polls_until_healthy = 2
    answers = [_health(container=True, mcp=MCP)]

    def probe():
        if docker.started:

            return answers[0]

        return None

    result = _resolve(probe=probe, docker=docker)

    assert result.level == "start_container"
    assert result.owned is False
    assert docker.started == ["abc123"]
    assert docker.inspect_calls >= 2
    assert docker.runs == []


def test_local_binary_is_spawned_when_nothing_is_serving():
    spawned: list[str] = []

    def probe():
        if spawned:

            return _health(container=False, mcp=None)

        return None

    result = _resolve(
        probe=probe,
        docker=FakeDocker(),
        find_binary=lambda: "/usr/bin/odoo-sheller",
        spawn=lambda path: spawned.append(path),
    )

    assert result.level == "spawn_local"
    assert result.owned is True
    assert spawned == ["/usr/bin/odoo-sheller"]
    assert result.mcp is None


def test_docker_run_uses_all_five_flags(tmp_path):
    docker = FakeDocker()
    docker.immediately_healthy = True
    runtime = tmp_path / "run"
    runtime.mkdir()
    sock = runtime / "docker.sock"
    sock.write_text("")

    def probe():
        if docker.runs:

            return _health(container=True, mcp=MCP)

        return None

    result = _resolve(
        probe=probe,
        docker=docker,
        config=_config(tmp_path, xdg_runtime_dir=str(runtime)),
    )

    assert result.level == "docker_run"
    assert result.owned is True
    spec = docker.runs[0]
    assert spec.image == "ghcr.io/romi477/odoo-sheller:1.7.0"
    volumes = dict(spec.volumes)
    assert volumes[str(sock)] == "/var/run/docker.sock"
    assert volumes[str(tmp_path / ".odoo-sheller")] == "/data/.odoo-sheller"
    assert spec.env["ODOO_SHELLER_UID"] == "501"
    assert spec.env["ODOO_SHELLER_GID"] == "20"
    assert spec.publish == ("127.0.0.1", 8765, 8765)


def test_publish_is_loopback_only(tmp_path):
    docker = FakeDocker()
    docker.immediately_healthy = True

    def probe():
        if docker.runs:

            return _health(container=True)

        return None

    _resolve(probe=probe, docker=docker, config=_config(tmp_path))

    host_ip, _, _ = docker.runs[0].publish
    assert host_ip == "127.0.0.1"
    assert host_ip != "0.0.0.0"


def test_tcp_docker_host_is_refused_before_run(tmp_path):
    with pytest.raises(UnsupportedDockerHost, match="unix socket"):
        _resolve(
            probe=lambda: None,
            docker=FakeDocker(),
            config=_config(tmp_path, docker_host="tcp://1.2.3.4:2375"),
        )


def test_created_leftover_is_removed_before_retry(tmp_path):
    docker = FakeDocker()
    docker.immediately_healthy = True
    docker.fail_run_once = PortClash(
        created_id="leftover1",
        message="bind: address already in use",
    )

    def probe():
        if docker.runs and docker.removed:

            return _health(container=True)

        return None

    result = _resolve(probe=probe, docker=docker, config=_config(tmp_path))

    assert result.level == "docker_run"
    assert docker.removed == ["leftover1"]
    assert len(docker.runs) == 2


def test_private_ghcr_pull_tells_the_user_to_login(tmp_path):
    docker = FakeDocker()
    docker.pull_error = DockerPullError("pull access denied")

    with pytest.raises(DockerLoginRequired, match="docker login ghcr.io"):
        _resolve(probe=lambda: None, docker=docker, config=_config(tmp_path))


def test_empty_image_tag_does_not_pull_latest(tmp_path):
    docker = FakeDocker()

    with pytest.raises(ResolveFailed, match="sheller_image_tag"):
        _resolve(
            probe=lambda: None,
            docker=docker,
            config=_config(tmp_path, image_tag=""),
        )

    assert docker.pulls == []
    assert docker.runs == []


def test_none_of_the_three_fails_loud(tmp_path):
    docker = FakeDocker()
    docker.run_error = RuntimeError("Cannot connect to the Docker daemon")

    with pytest.raises(ResolveFailed, match="None of the three"):
        _resolve(
            probe=lambda: None,
            docker=docker,
            find_binary=lambda: None,
            config=_config(tmp_path),
        )


def test_null_mcp_on_resolved_daemon_is_not_invented():
    result = _resolve(probe=lambda: _health(mcp=None))

    assert result.mcp is None
    assert mcp_for_ui(result.health) is None
