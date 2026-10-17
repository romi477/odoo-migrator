"""Sheller client contract: the fake and the HTTP client pass the same tests."""

import hashlib
import json

import httpx2
import pytest
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from odoo_migrator.sheller import (
    FakeShellerClient,
    HttpShellerClient,
    ShellerError,
    ShellerNeedsHuman,
    ShellerNotOwner,
    ShellerSessionDead,
    ShellerTimeout,
    ShellerUnreachable,
    mcp_for_ui,
)
from odoo_migrator.sheller.client import SESSION_OPEN_TIMEOUT

# What Sheller 1.11.0 answers an agent's open with `allow_commit: true` on a
# container nobody switched on, word for word.
NEEDS_A_HUMAN = {
    "detail": {
        "error": "needs_a_human",
        "act": "open an agent's session with commit already granted",
        "recovery": (
            "open it without allow_commit; a human grants the right later, from the "
            "UI — or turns on 'agents may commit' for this container, and its "
            "sessions open with it"
        ),
    }
}


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
        if isinstance(exc, ShellerNeedsHuman):

            return HTTPException(status_code=403, detail=NEEDS_A_HUMAN["detail"])
        if isinstance(exc, ShellerNotOwner):

            return HTTPException(status_code=403, detail={"error": "not_owner"})

        return HTTPException(status_code=getattr(exc, "status", 400), detail=str(exc))

    @app.get("/health")
    def health():

        return fake.health()

    @app.post("/api/sessions")
    def open_session(body: OpenBody):
        try:

            return fake.open_session(
                container=body.container,
                database=body.database,
                odoo_bin=body.odoo_bin,
                owner=body.owner,
                allow_commit=body.allow_commit,
            )
        except ShellerError as exc:
            raise translate(exc) from None

    @app.get("/api/help")
    def help_topics():
        try:

            return fake.help()
        except ShellerError as exc:
            raise translate(exc) from None

    @app.get("/api/agent-commit")
    def agent_commit():
        try:

            return fake.agent_commit()
        except ShellerError as exc:
            raise translate(exc) from None

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

    @app.get("/api/sessions/{session_id}/history")
    def history(session_id: str, logs: bool = False, log_tail: int = 2000):
        try:

            return fake.feed(session_id, logs=logs, log_tail=log_tail)
        except ShellerError as exc:
            raise translate(exc) from None

    @app.post("/api/sessions/{session_id}/interrupt")
    def interrupt(
        session_id: str,
        x_os_session_key: str | None = Header(None),
        x_os_admin_key: str | None = Header(None),
    ):
        refuse_admin(x_os_admin_key)
        try:

            return fake.interrupt(session_id, write_key=x_os_session_key or "")
        except ShellerError as exc:
            raise translate(exc) from None

    @app.get("/api/journals/{session_id}")
    def journal_export(session_id: str):
        try:
            body = fake.journal(session_id)
        except ShellerError as exc:
            raise translate(exc) from None
        lines = [json.dumps(row) for row in body["records"]]

        return PlainTextResponse(
            ("\n".join(lines) + "\n") if lines else "",
            media_type="application/x-ndjson",
        )

    @app.get("/api/containers/{container}/tests")
    def container_tests(container: str, module: str = ""):

        return {
            "module": module,
            "path": f"/opt/project/{module}",
            "classes": [],
            "container": container,
        }

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


class _Response:
    def __init__(self, status: int, payload):
        self.status_code = status
        self.content = b"1"
        self._payload = payload

    def json(self):

        return self._payload


class _RecordingHttp:
    def __init__(self, response: _Response):
        self.response = response
        self.calls: list[tuple] = []

    def request(self, method, path, **kwargs):
        self.calls.append((method, path, kwargs))

        return self.response


def test_open_session_outlasts_a_slow_registry_load():
    """The daemon waits 90s for hello. The HTTP call has to stay past that."""
    http = _RecordingHttp(_Response(200, {"id": "s1", "write_key": "k"}))
    client = HttpShellerClient("http://127.0.0.1:8765", http=http)

    client.open_session(container="odoo-old", database="acme", odoo_bin="/opt/odoo/odoo-bin")

    assert http.calls[0][2]["timeout"] == SESSION_OPEN_TIMEOUT
    assert SESSION_OPEN_TIMEOUT > 90


def test_a_dead_session_reports_the_daemons_reason():
    http = _RecordingHttp(_Response(410, "session did not start: loading registry"))
    client = HttpShellerClient("http://127.0.0.1:8765", http=http)

    with pytest.raises(ShellerSessionDead) as dead:
        client.open_session(
            container="odoo-old", database="acme", odoo_bin="/opt/odoo/odoo-bin"
        )

    assert "loading registry" in str(dead.value)


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

    # The daemon's shapes: `error` is None on success, and a test run's
    # outcome is under `test`. Neither answer carries an `ok`.
    executed = sheller.exec(sid, "1 + 1", write_key=key)
    assert executed["error"] is None
    tested = sheller.run_test(sid, "sale.TestSale.test_ok", write_key=key)
    assert tested["error"] is None
    assert tested["test"]["success"] is True
    assert sheller.commit(sid, write_key=key)["ok"] is True
    assert sheller.rollback(sid, write_key=key)["ok"] is True
    assert sheller.close(sid, write_key=key)["ok"] is True


def test_the_feed_of_a_closed_session_still_answers(sheller):
    """The history route answers after close. The array is ``entries``."""
    opened = sheller.open_session(
        container="c",
        database="d",
        odoo_bin="/odoo/odoo-bin",
        owner={"kind": "agent"},
        allow_commit=False,
    )
    sheller.exec(opened["id"], "1 + 1", write_key=opened["write_key"])
    sheller.close(opened["id"], write_key=opened["write_key"])

    feed = sheller.feed(opened["id"])

    assert [entry["code"] for entry in feed["entries"]] == ["1 + 1"]


def test_interrupt_needs_the_write_key(sheller):
    opened = sheller.open_session(
        container="c",
        database="d",
        odoo_bin="/odoo/odoo-bin",
        owner={"kind": "agent"},
        allow_commit=False,
    )

    with pytest.raises(ShellerError) as refused:
        sheller.interrupt(opened["id"], write_key="not-the-key")

    assert refused.value.status == 403


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

    assert client.exec(opened["id"], "1", write_key=opened["write_key"])["error"] is None
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
    """Review gate: a daemon already on 8765 is usable. Skip if none is.

    2s was the timeout, and a daemon busy with someone's session answered
    later than that: a timeout is not ShellerUnreachable, so the test failed
    instead of skipping. Something other than Sheller on the port is a skip
    too, not a verdict on the client.
    """
    client = HttpShellerClient("http://127.0.0.1:8765", timeout=15.0)
    try:
        health = client.health()
    except ShellerUnreachable:
        pytest.skip("no daemon on 8765")
    except ShellerTimeout:
        pytest.skip("the daemon on 8765 did not answer /health within 15s")
    except (ShellerError, ValueError) as exc:
        pytest.skip(f"8765 is not answering like a Sheller daemon: {exc}")
    if not isinstance(health, dict) or "version" not in health:
        pytest.skip("8765 is not answering like a Sheller daemon")

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
        # The probe reports the database the container is configured for,
        # which is not the same as one that exists: opening a shell on a
        # missing database starts a process that dies at once (410).
        if (
            candidate.get("ok")
            and candidate.get("db_name")
            and candidate["db_name"] in (candidate.get("databases") or [])
        ):
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


def _answering(route: str, body: str) -> HttpShellerClient:
    from fastapi import FastAPI

    app = FastAPI()

    @app.get(route)
    def answer(session_id: str):
        del session_id

        return PlainTextResponse(body)

    return HttpShellerClient("http://testserver", http=TestClient(app))


def test_a_journal_line_cut_short_does_not_lose_the_rest():
    """The journal is appended to while it is read, so its last line can be
    half written. One such line raised ValueError past the tool's error
    handling, and a read of the journal failed the whole step."""
    client = _answering("/api/journals/{session_id}", '{"id": 1, "kind": "exec"}\n{"id": 2, "ki')

    assert client.journal("s1") == {"records": [{"id": 1, "kind": "exec"}]}


def test_an_answer_that_is_not_json_is_a_sheller_error():
    """A proxy's error page, or a daemon mid-restart, answered 200 with HTML.
    `response.json()` raised ValueError, which nothing on the way up
    expected from a Sheller call."""
    client = _answering("/api/sessions/{session_id}", "<html>Bad gateway</html>")

    with pytest.raises(ShellerError, match="not JSON"):
        client.get_session("s1")



def test_a_refusal_that_needs_a_person_is_not_a_wrong_key():
    """Every 403 used to read `not_owner`. The runner's target side was
    refused this way from Sheller 1.8.3 on, and the person was told about a
    key when what they had to do was turn a switch on."""
    client = HttpShellerClient(
        "http://127.0.0.1:8765",
        transport=httpx2.MockTransport(lambda request: httpx2.Response(403, json=NEEDS_A_HUMAN)),
    )

    with pytest.raises(ShellerNeedsHuman) as refused:
        client.open_session(
            container="mig18",
            database="d",
            odoo_bin="/odoo/odoo-bin",
            owner={"kind": "agent"},
            allow_commit=True,
        )

    assert not isinstance(refused.value, ShellerNotOwner)
    assert refused.value.status == 403
    assert "commit already granted" in str(refused.value)
    assert "agents may commit" in str(refused.value)


def test_a_wrong_key_is_still_not_owner():
    client = HttpShellerClient(
        "http://127.0.0.1:8765",
        transport=httpx2.MockTransport(
            lambda request: httpx2.Response(403, json={"detail": {"error": "not_owner"}})
        ),
    )

    with pytest.raises(ShellerNotOwner):
        client.exec("abc", "1", write_key="someone-elses-key")


@pytest.fixture(params=["fake", "http"])
def switched(request):
    """A daemon where a person switched `odoo-new` on for agents, and only it.

    The fake is handed back too: flipping the switch is a person's act with
    the admin key, which this app has no call for.
    """
    fake = FakeShellerClient(agent_commit={"odoo-new"})
    client = fake if request.param == "fake" else _http_client(fake)

    return client, fake


def test_the_switched_on_containers_can_be_read_before_anything_opens(switched):
    client, _fake = switched

    assert [entry["container"] for entry in client.agent_commit()] == ["odoo-new"]


def test_the_target_opens_with_commit_where_a_person_switched_it_on(switched):
    client, _fake = switched

    opened = _open(client, container="odoo-new", allow_commit=True)

    assert opened["allow_commit"] is True
    assert opened["commit_grant"] == "container"
    assert client.commit(opened["id"], write_key=opened["write_key"])


def test_the_target_is_refused_where_nobody_switched_it_on(switched):
    """Sheller 1.8.3 and on. The double used to accept it."""
    client, _fake = switched

    with pytest.raises(ShellerNeedsHuman, match="agents may commit"):
        _open(client, container="odoo-old", allow_commit=True)


def test_the_source_side_stays_read_only_on_a_switched_on_container(switched):
    """`allow_commit: false` is an opt-out the switch honours, at open and
    when a person turns it on later. The source side relies on it."""
    client, fake = switched
    fake.set_agent_commit("odoo-old", True)

    opened = _open(client, container="odoo-old", allow_commit=False)
    fake.set_agent_commit("odoo-old", True)

    assert opened["allow_commit"] is False
    assert client.get_session(opened["id"])["allow_commit"] is False
    with pytest.raises(ShellerError) as refused:
        client.commit(opened["id"], write_key=opened["write_key"])
    assert refused.value.status == 423


def test_turning_the_switch_off_takes_the_right_from_an_open_session(switched):
    client, fake = switched
    opened = _open(client, container="odoo-new", allow_commit=True)

    fake.set_agent_commit("odoo-new", False)

    described = client.get_session(opened["id"])
    assert described["allow_commit"] is False
    assert described["commit_grant"] is None
    with pytest.raises(ShellerError) as refused:
        client.commit(opened["id"], write_key=opened["write_key"])
    assert refused.value.status == 423


def test_a_daemon_before_the_switch_refuses_the_target_everywhere():
    """1.8.3 to 1.10: no switch to read, and no way for an agent to commit."""
    fake = FakeShellerClient(version="1.10.0")

    with pytest.raises(ShellerError) as missing:
        fake.agent_commit()
    with pytest.raises(ShellerNeedsHuman):
        _open(fake, container="odoo-new", allow_commit=True)
    assert missing.value.status == 404


def test_the_guidance_is_served_as_data_with_its_markers_unresolved(sheller):
    """Sheller 1.11.0: `GET /api/help`. A client that gives its agent its own
    tools substitutes the markers; the daemon never spells a tool."""
    answer = sheller.help()

    assert answer["version"] == sheller.health()["version"]
    assert answer["instructions"]
    assert "run_test" in answer["markers"]
    limits = answer["topics"]["limits"]
    assert hashlib.sha256(limits["text"].encode()).hexdigest() == limits["hash"]
    assert "{{run_test}}" in limits["text"]
    assert "os_" not in limits["text"]


def test_a_daemon_before_the_help_route_has_none():
    """1.10.0 and older answer 404, and the caller falls back to its own copy."""
    fake = FakeShellerClient(version="1.10.0")
    with pytest.raises(ShellerError) as excinfo:
        fake.help()

    assert excinfo.value.status == 404
    with pytest.raises(ShellerError) as excinfo:
        _http_client(fake).help()

    assert excinfo.value.status == 404
