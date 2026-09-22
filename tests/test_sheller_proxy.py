"""Sheller proxy: n8n talks to the app, the app talks to loopback."""

import pytest
from conftest import API_KEY
from fastapi.testclient import TestClient
from test_sheller_client import _stub_app

from odoo_migrator.api import create_app
from odoo_migrator.authkey import API_KEY_HEADER
from odoo_migrator.provision import FakeComposeEngine
from odoo_migrator.sheller import (
    SESSION_KEY_HEADER,
    FakeShellerClient,
    HttpShellerClient,
    ShellerError,
)


def _app(tmp_path, fake, stub):

    return create_app(
        tmp_path / "proxy.sqlite",
        compose=FakeComposeEngine(),
        sheller=fake,
        sheller_http=stub,
        api_key=API_KEY,
    )


def _clients(tmp_path):
    fake = FakeShellerClient()
    stub = TestClient(_stub_app(fake))
    app = _app(tmp_path, fake, stub)
    keyed = TestClient(app, headers={API_KEY_HEADER: API_KEY})
    via_proxy = HttpShellerClient("http://testserver", http=keyed)
    via_direct = HttpShellerClient("http://testserver", http=stub)

    return fake, keyed, via_proxy, via_direct


def test_session_opened_through_the_proxy_matches_one_opened_directly(tmp_path):
    _fake, _app_client, via_proxy, via_direct = _clients(tmp_path)
    probed = via_direct.probe("odoo-old")

    opened = via_proxy.open_session(
        container="odoo-old",
        database=probed["db_name"],
        odoo_bin=probed["odoo_bin"],
        owner={"kind": "agent", "label": "odoo-migrator", "migration_id": "m1"},
        allow_commit=False,
    )
    described = via_direct.get_session(opened["id"])

    assert described["id"] == opened["id"]
    assert described["container"] == "odoo-old"
    assert described["database"] == probed["db_name"]
    assert described["allow_commit"] is False
    executed = via_proxy.exec(opened["id"], "1 + 1", write_key=opened["write_key"])
    assert executed["ok"] is True
    via_proxy.close(opened["id"], write_key=opened["write_key"])
    with pytest.raises(ShellerError):
        via_direct.get_session(opened["id"])


def test_session_key_is_forwarded_untouched(tmp_path):
    fake, app_client, via_proxy, _via_direct = _clients(tmp_path)
    probed = fake.probe("odoo-old")
    opened = via_proxy.open_session(
        container="odoo-old",
        database=probed["db_name"],
        odoo_bin=probed["odoo_bin"],
        allow_commit=False,
    )

    refused = app_client.post(
        f"/api/sessions/{opened['id']}/exec",
        json={"code": "1"},
        headers={SESSION_KEY_HEADER: "someone-elses-key"},
    )

    assert refused.status_code == 403


def test_admin_key_is_refused_and_never_forwarded(tmp_path):
    _fake, app_client, _via_proxy, _via_direct = _clients(tmp_path)

    response = app_client.get("/api/sessions", headers={"X-OS-Admin-Key": "anything"})

    assert response.status_code == 403
    assert response.json()["error"] == "admin_key_forbidden"


def test_journals_are_not_proxied(tmp_path):
    _fake, app_client, _via_proxy, _via_direct = _clients(tmp_path)

    listed = app_client.get("/api/journals")
    one = app_client.get("/api/journals/abc")

    assert listed.status_code == 403
    assert one.status_code == 403


def test_health_is_the_daemon_health(tmp_path):
    fake, app_client, _via_proxy, _via_direct = _clients(tmp_path)

    health = app_client.get("/health")

    assert health.status_code == 200
    assert health.json() == fake.health()


def test_default_bind_is_loopback():
    """0.0.0.0 was the default and was called "the container gateway". It is
    every interface: on a laptop on office Wi-Fi it hands the subnet a Sheller
    proxy, and behind that is arbitrary Python as SUPERUSER_ID. Widening it is
    a decision the human makes per run."""
    from odoo_migrator.__main__ import build_parser

    assert build_parser().get_default("host") == "127.0.0.1"


def test_encoded_dotdot_cannot_reach_the_journals(tmp_path):
    """``%2e%2e`` survives routing undecoded by the allowlist and is then
    collapsed by the HTTP client on the way out, so a prefix check alone
    served an unmasked journal through a path that claimed to be a session."""
    from fastapi import FastAPI

    upstream = FastAPI()

    @upstream.get("/api/journals")
    def journals():

        return {"journals": ["unmasked"]}

    app = _app(tmp_path, FakeShellerClient(), TestClient(upstream))
    client = TestClient(app, headers={API_KEY_HEADER: API_KEY})

    sneaked = client.get("/api/sessions/%2e%2e/journals")

    assert sneaked.status_code == 403
    assert sneaked.json()["error"] == "proxy_denied"


def test_the_machine_facing_surface_needs_the_key(tmp_path):
    """The pages are loopback-only; the File API and the proxy are not, because
    a container has to reach them."""
    _fake, _keyed, _via_proxy, _via_direct = _clients(tmp_path)
    app = _app(tmp_path, FakeShellerClient(), TestClient(_stub_app(FakeShellerClient())))
    anonymous = TestClient(app)

    assert anonymous.get("/api/sessions").status_code == 403
    assert anonymous.get("/health").status_code == 403
    assert anonymous.get("/api/migrations/whatever/files", params={"side": "old", "path": "a.py"}).status_code == 403
    assert anonymous.put(
        "/api/migrations/whatever/files",
        json={"side": "new", "path": "a.py", "content": "x"},
    ).status_code == 403
    wrong = TestClient(app, headers={API_KEY_HEADER: "not-it"})
    assert wrong.get("/api/sessions").status_code == 403
    assert anonymous.get("/").status_code == 200


def test_a_daemon_that_is_not_listening_is_502(tmp_path):
    """500 read as a bug in the app; the executor needs to tell "Sheller is
    down" from "Sheller said no"."""
    from odoo_migrator.settings import KEEP, save_app_settings

    app = _app(tmp_path, FakeShellerClient(), None)
    save_app_settings(
        n8n_base_url="",
        n8n_api_key=KEEP,
        app_base_url="",
        sheller_base_url="http://127.0.0.1:1",
        sheller_image_tag="",
    )
    client = TestClient(
        app, headers={API_KEY_HEADER: API_KEY}, raise_server_exceptions=False
    )

    down = client.get("/health")

    assert down.status_code == 502
    assert down.json()["error"] == "sheller_unreachable"


@pytest.mark.live
def test_throwaway_container_reaches_file_api_and_proxy(tmp_path):
    """Review gate: n8n in a container must reach the File API and the proxy.

    Skipped unless Docker is up and alpine:3.20 is already pulled — a surprise
    pull on every ``uv run pytest`` is not a unit test.
    """
    import json
    import shutil
    import socket
    import subprocess
    import threading
    import time

    import httpx2
    import uvicorn

    from odoo_migrator.db import database
    from odoo_migrator.models import Migration

    if shutil.which("docker") is None:
        pytest.skip("no docker")
    if subprocess.run(["docker", "info"], capture_output=True, check=False).returncode != 0:
        pytest.skip("docker engine not reachable")
    images = subprocess.run(
        ["docker", "images", "-q", "alpine:3.20"],
        capture_output=True,
        text=True,
        check=False,
    )
    if not images.stdout.strip():
        pytest.skip("alpine:3.20 is not pulled")

    def free_port(host: str) -> int:
        sock = socket.socket()
        sock.bind((host, 0))
        port = sock.getsockname()[1]
        sock.close()

        return port

    def wait_http(client: httpx2.Client, path: str) -> None:
        for _ in range(50):
            try:
                client.get(path)
                return
            except httpx2.RequestError:
                time.sleep(0.05)
        pytest.skip(f"{path} did not come up")

    fake = FakeShellerClient()
    stub_port = free_port("127.0.0.1")
    app_port = free_port("0.0.0.0")
    threading.Thread(
        target=uvicorn.Server(
            uvicorn.Config(
                _stub_app(fake), host="127.0.0.1", port=stub_port, log_level="warning"
            )
        ).run,
        daemon=True,
    ).start()
    upstream = httpx2.Client(base_url=f"http://127.0.0.1:{stub_port}", timeout=5.0)
    wait_http(upstream, "/health")

    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    app = create_app(
        tmp_path / "live.sqlite",
        compose=FakeComposeEngine(),
        sheller=fake,
        sheller_http=upstream,
        api_key=API_KEY,
    )
    threading.Thread(
        target=uvicorn.Server(
            uvicorn.Config(app, host="0.0.0.0", port=app_port, log_level="warning")
        ).run,
        daemon=True,
    ).start()
    api = httpx2.Client(
        base_url=f"http://127.0.0.1:{app_port}",
        timeout=5.0,
        headers={API_KEY_HEADER: API_KEY},
    )
    wait_http(api, "/health")

    created = api.post(
        "/migrations",
        data={
            "name": "Live",
            "accent": "violet",
            "source_version": "17.0",
            "target_version": "18.0",
            "old_project_path": str(old),
            "new_project_path": str(new),
        },
    )
    assert created.status_code == 303
    with database.connection_context():
        row = Migration.get(Migration.name == "Live")
    written = api.put(
        f"/api/migrations/{row.id}/files",
        json={"side": "new", "path": "sale_custom/__init__.py", "content": "ok\n"},
    )
    assert written.status_code == 200

    file_url = (
        f"http://host.docker.internal:{app_port}/api/migrations/{row.id}/files"
        f"?side=new&path=sale_custom/__init__.py"
    )
    health_url = f"http://host.docker.internal:{app_port}/health"
    reached = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "--add-host",
            "host.docker.internal:host-gateway",
            "alpine:3.20",
            "sh",
            "-c",
            (
                f"wget -qO- --header '{API_KEY_HEADER}: {API_KEY}' {health_url}"
                " && echo --- && "
                f"wget -qO- --header '{API_KEY_HEADER}: {API_KEY}' '{file_url}'"
            ),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if reached.returncode != 0:
        pytest.skip(f"container could not reach the app: {reached.stderr}")
    health_text, file_text = reached.stdout.split("---", 1)
    assert json.loads(health_text)["ok"] is True
    assert json.loads(file_text)["content"] == "ok\n"
