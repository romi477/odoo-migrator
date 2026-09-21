"""Sheller client contract: the fake and the HTTP client pass the same tests."""

import httpx2
import pytest
from fastapi.testclient import TestClient

from odoo_migrator.sheller import (
    FakeShellerClient,
    HttpShellerClient,
    ShellerError,
    ShellerNotOwner,
    ShellerUnreachable,
    mcp_for_ui,
)


def _stub_app(fake: FakeShellerClient):
    """ASGI double of the Sheller routes the migrator actually calls."""
    from fastapi import FastAPI, Header, HTTPException
    from pydantic import BaseModel

    app = FastAPI()

    class ExecBody(BaseModel):
        code: str

    class RunTestBody(BaseModel):
        test: str
        timeout: float | None = None

    class OpenBody(BaseModel):
        container: str | None = None
        database: str | None = None
        odoo_bin: str | None = None
        owner: dict | None = None
        allow_commit: bool | None = None

    class ProbeBody(BaseModel):
        container: str

    def translate(exc: ShellerError) -> HTTPException:
        if isinstance(exc, ShellerNotOwner):

            return HTTPException(status_code=403, detail={"error": "not_owner"})

        return HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc))

    @app.get("/health")
    def health():

        return fake.health()

    @app.post("/api/sessions")
    def open_session(body: OpenBody):

        return fake.open_session(
            container=body.container,
            database=body.database,
            odoo_bin=body.odoo_bin,
            owner=body.owner,
            allow_commit=body.allow_commit,
        )

    @app.get("/api/containers")
    def list_containers():

        return fake.list_containers()

    @app.post("/api/probe")
    def probe(body: ProbeBody):

        return fake.probe(body.container)

    @app.get("/api/sessions")
    def list_sessions():

        return fake.list_sessions()

    @app.get("/api/sessions/{session_id}")
    def get_session(session_id: str):
        try:

            return fake.get_session(session_id)
        except ShellerError as exc:
            raise translate(exc) from None

    def refuse_admin(x_os_admin_key: str | None):
        if x_os_admin_key is not None:
            raise HTTPException(status_code=500, detail="admin key must never be sent")

    @app.post("/api/sessions/{session_id}/exec")
    def exec_code(
        session_id: str,
        body: ExecBody,
        x_os_session_key: str | None = Header(None),
        x_os_admin_key: str | None = Header(None),
    ):
        refuse_admin(x_os_admin_key)
        try:

            return fake.exec(session_id, body.code, write_key=x_os_session_key or "")
        except ShellerError as exc:
            raise translate(exc) from None

    @app.post("/api/sessions/{session_id}/run_test")
    def run_test(
        session_id: str,
        body: RunTestBody,
        x_os_session_key: str | None = Header(None),
        x_os_admin_key: str | None = Header(None),
    ):
        refuse_admin(x_os_admin_key)
        try:

            return fake.run_test(
                session_id,
                body.test,
                write_key=x_os_session_key or "",
                timeout=body.timeout,
            )
        except ShellerError as exc:
            raise translate(exc) from None

    @app.post("/api/sessions/{session_id}/commit")
    def commit(
        session_id: str,
        x_os_session_key: str | None = Header(None),
        x_os_admin_key: str | None = Header(None),
    ):
        refuse_admin(x_os_admin_key)
        try:

            return fake.commit(session_id, write_key=x_os_session_key or "")
        except ShellerError as exc:
            raise translate(exc) from None

    @app.post("/api/sessions/{session_id}/rollback")
    def rollback(
        session_id: str,
        x_os_session_key: str | None = Header(None),
        x_os_admin_key: str | None = Header(None),
    ):
        refuse_admin(x_os_admin_key)
        try:

            return fake.rollback(session_id, write_key=x_os_session_key or "")
        except ShellerError as exc:
            raise translate(exc) from None

    @app.delete("/api/sessions/{session_id}")
    def close(
        session_id: str,
        x_os_session_key: str | None = Header(None),
        x_os_admin_key: str | None = Header(None),
    ):
        refuse_admin(x_os_admin_key)
        try:

            return fake.close(session_id, write_key=x_os_session_key or "")
        except ShellerError as exc:
            raise translate(exc) from None

    return app


def _http_client(fake: FakeShellerClient) -> HttpShellerClient:
    # httpx2.ASGITransport is async-only; Starlette's TestClient is the sync
    # portal onto the same stub the HTTP client would hit on the wire.
    return HttpShellerClient(
        "http://testserver",
        http=TestClient(_stub_app(fake)),
    )


@pytest.fixture(params=["fake", "http"])
def sheller(request):
    fake = FakeShellerClient()
    if request.param == "fake":

        return fake

    return _http_client(fake)


def _open(sheller, **kwargs):
    defaults = {
        "container": "odoo-old",
        "database": "acme",
        "odoo_bin": "/opt/odoo/odoo-bin",
        "owner": {"kind": "agent", "label": "odoo-migrator", "migration_id": "m1"},
        "allow_commit": False,
    }
    defaults.update(kwargs)

    return sheller.open_session(**defaults)


def test_health_shape(sheller):
    health = sheller.health()

    assert health["ok"] is True
    assert "version" in health
    assert "container" in health
    assert "mcp" in health


def test_open_without_odoo_bin_is_refused(sheller):
    """The daemon answers 422 "container, database and odoo_bin are required".

    This was found by pointing the client at a real daemon: both the stub and
    the fake happily opened a session the daemon would have refused, so the
    contract suite agreed with a client that could not open one. A double has
    to refuse what the real thing refuses.
    """
    with pytest.raises(ShellerError) as refused:
        sheller.open_session(container="odoo-old", database="acme", odoo_bin="")

    assert refused.value.status == 422


def test_probe_supplies_the_odoo_bin_that_open_requires(sheller):
    probed = sheller.probe("odoo-old")

    assert probed["odoo_bin"]
    assert probed["databases"]

    opened = _open(sheller, odoo_bin=probed["odoo_bin"])

    assert opened["id"]


def test_open_returns_write_key_once(sheller):
    opened = _open(sheller)

    assert opened["write_key"]
    assert opened["id"]
    listed = sheller.list_sessions()
    assert listed[0]["id"] == opened["id"]
    assert "write_key" not in listed[0]
    described = sheller.get_session(opened["id"])
    assert "write_key" not in described


def test_exec_run_test_commit_rollback_close(sheller):
    opened = _open(sheller, allow_commit=True)
    sid = opened["id"]
    key = opened["write_key"]

    executed = sheller.exec(sid, "1 + 1", write_key=key)
    assert executed["ok"] is True
    tested = sheller.run_test(sid, "sale.TestSale.test_ok", write_key=key)
    assert tested["ok"] is True
    assert sheller.commit(sid, write_key=key)["ok"] is True
    assert sheller.rollback(sid, write_key=key)["ok"] is True
    assert sheller.close(sid, write_key=key)["ok"] is True


def test_wrong_write_key_is_not_owner(sheller):
    opened = _open(sheller)

    with pytest.raises(ShellerNotOwner):
        sheller.exec(opened["id"], "1", write_key="someone-elses-key")


def test_null_mcp_is_hidden_not_invented():
    health = {"ok": True, "version": "1.7.0", "container": True, "mcp": None}

    assert mcp_for_ui(health) is None


def test_mcp_object_is_returned_verbatim():
    mcp = {
        "command": "docker",
        "args": ["exec", "-i", "9f31c2ab77e1", "python", "-m", "odoo_sheller.mcp"],
    }
    health = {"ok": True, "version": "1.7.0", "container": True, "mcp": mcp}

    assert mcp_for_ui(health) is mcp


def test_http_client_never_sends_an_admin_key():
    """The stub 500s if X-OS-Admin-Key is present; a clean 200 is the proof."""
    client = _http_client(FakeShellerClient())
    opened = _open(client, allow_commit=True)

    assert client.exec(opened["id"], "1", write_key=opened["write_key"])["ok"] is True
    assert client.commit(opened["id"], write_key=opened["write_key"])["ok"] is True
    assert client.rollback(opened["id"], write_key=opened["write_key"])["ok"] is True
    assert client.close(opened["id"], write_key=opened["write_key"])["ok"] is True


def test_health_on_a_dead_transport_is_unreachable():
    class Boom(httpx2.BaseTransport):
        def handle_request(self, request):
            raise httpx2.ConnectError("Connection refused")

    client = HttpShellerClient("http://127.0.0.1:8765", transport=Boom())

    with pytest.raises(ShellerUnreachable):
        client.health()


@pytest.mark.live
def test_attach_to_a_live_daemon():
    """Review gate: a daemon already on 8765 is usable. Skip if none is."""
    client = HttpShellerClient("http://127.0.0.1:8765", timeout=2.0)
    try:
        health = client.health()
    except ShellerUnreachable:
        pytest.skip("no daemon on 8765")

    assert health["ok"] is True
    assert health["version"]


@pytest.mark.live
def test_live_session_round_trip():
    """Health alone proved nothing about the session calls — the mismatch that
    prompted this test (``odoo_bin`` is required) lived one endpoint deeper.

    Read-only: ``allow_commit`` is false and the session is rolled back and
    closed, so nothing is written to whatever database this host has.
    """
    client = HttpShellerClient("http://127.0.0.1:8765", timeout=180.0)
    try:
        client.health()
    except ShellerUnreachable:
        pytest.skip("no daemon on 8765")

    containers = client.list_containers()
    if not containers:
        pytest.skip("no containers visible to the daemon")

    probed = None
    for entry in containers:
        candidate = client.probe(entry["name"])
        # db_name, not databases[0]: the list is every database the server can
        # see, including ones built by other Odoo majors. Opening a shell on
        # one of those starts a process that dies immediately (HTTP 410,
        # "process ended"). db_name is the database this container is
        # configured for.
        if candidate.get("ok") and candidate.get("db_name"):
            probed = (entry["name"], candidate)
            break
    if probed is None:
        pytest.skip("no Odoo container to open a session on")

    name, details = probed
    opened = client.open_session(
        container=name,
        database=details["db_name"],
        odoo_bin=details["odoo_bin"],
        owner={"kind": "agent", "label": "odoo-migrator", "migration_id": "live-test"},
        allow_commit=False,
    )
    session_id, write_key = opened["id"], opened["write_key"]
    try:

        assert opened["allow_commit"] is False
        assert client.exec(session_id, "1 + 1", write_key=write_key)
        assert client.get_session(session_id)["id"] == session_id
        client.rollback(session_id, write_key=write_key)
    finally:
        client.close(session_id, write_key=write_key)
