"""HTTP client for a Sheller daemon, and the helper that must not invent an MCP command."""

import json
from typing import Protocol

import httpx2

from odoo_migrator.sheller.errors import (
    ShellerBusy,
    ShellerError,
    ShellerNeedsHuman,
    ShellerNotOwner,
    ShellerSessionDead,
    ShellerTimeout,
    ShellerUnreachable,
)

SESSION_KEY_HEADER = "X-OS-Session-Key"
# The daemon blocks POST /api/sessions until the shell says hello, and it
# waits 90s for that frame. A shorter HTTP timeout cancels the open while
# the session is still starting: the UI has already shown it, then the
# process is killed. Stay past that wait so a slow registry load can finish
# and a real death still comes back as the daemon's own reason.
SESSION_OPEN_TIMEOUT = 120.0
# The daemon's own ceiling for a test run when the body names none. The HTTP
# call has to outlast whichever ceiling applies: the client default is 30s,
# and a test class that ran longer came back as a timeout while it kept the
# session busy, so every spec after it answered `session_busy`.
DAEMON_TEST_TIMEOUT = 300.0
# Room for the daemon to notice its own timeout and say so, before ours fires.
HTTP_MARGIN = 30.0


def mcp_for_ui(health: dict) -> dict | None:
    """The MCP spawn command for *this* daemon, or None.

    ``"mcp": null`` means the daemon cannot say. Inventing ``docker exec``
    on its behalf would only fail later; hide it instead.
    """
    mcp = health.get("mcp")
    if mcp is None:

        return None

    return mcp


def with_js_capability(answer: dict, session: dict) -> dict:
    """Put whether this session can run a browser suite beside the module's JS.

    The listing says which JS test files a module ships and the call that runs
    them; the session says whether its container has Chromium to run them
    with. An agent reads both in one answer, before it spends a run. A daemon
    older than 1.11.0 has neither, and its answer is left as it is.
    """
    js = answer.get("js")
    capability = session.get("js_tests")
    if not isinstance(js, dict) or not isinstance(capability, dict):

        return answer
    merged = dict(js)
    for key in ("available", "reason", "tested"):
        merged[key] = capability.get(key)

    return {**answer, "js": merged}


class ShellerClient(Protocol):
    def health(self) -> dict: ...

    def help(self) -> dict: ...

    def list_containers(self) -> list: ...

    def probe(self, container: str) -> dict: ...

    def agent_commit(self) -> list: ...

    def open_session(
        self,
        *,
        container: str,
        database: str,
        odoo_bin: str,
        owner: dict | None = None,
        allow_commit: bool | None = None,
    ) -> dict: ...

    def list_sessions(self) -> list: ...

    def get_session(self, session_id: str) -> dict: ...

    def exec(
        self, session_id: str, code: str, *, write_key: str, timeout: float | None = None
    ) -> dict: ...

    def run_test(
        self,
        session_id: str,
        test: str,
        *,
        write_key: str,
        timeout: float | None = None,
        params: str | None = None,
    ) -> dict: ...

    def commit(self, session_id: str, *, write_key: str) -> dict: ...

    def rollback(self, session_id: str, *, write_key: str) -> dict: ...

    def close(self, session_id: str, *, write_key: str) -> dict: ...

    def source(self, session_id: str, *, write_key: str, **target) -> dict: ...

    def journal(self, session_id: str) -> dict: ...

    def feed(
        self, session_id: str, *, logs: bool = False, log_tail: int | None = None
    ) -> dict: ...

    def list_tests(self, session_id: str, *, module: str, write_key: str) -> dict: ...

    def interrupt(self, session_id: str, *, write_key: str) -> dict: ...


class HttpShellerClient:
    """Talks to a live daemon. Never reads admin.key, never sends X-OS-Admin-Key."""

    def __init__(
        self,
        base_url: str,
        *,
        transport=None,
        timeout: float = 30.0,
        http=None,
    ):
        if http is not None:
            self._http = http

            return
        kwargs: dict = {"base_url": base_url.rstrip("/"), "timeout": timeout}
        if transport is not None:
            kwargs["transport"] = transport
        self._http = httpx2.Client(**kwargs)

    def health(self) -> dict:

        return self._request("GET", "/health")

    def help(self) -> dict:
        """``GET /api/help`` (Sheller 1.11.0): the guidance its MCP clients get.

        Every topic with its hash, and the tool names left as ``{{marker}}``
        for the caller to replace with its own. An older daemon answers 404.
        """

        return self._request("GET", "/api/help")

    def list_containers(self) -> list:

        return self._request("GET", "/api/containers")

    def agent_commit(self) -> list:
        """``GET /api/agent-commit``: the containers a person switched on for agents.

        Sheller 1.11.0. No key to read it; turning one on or off takes the
        admin key, which this app never sends, so there is no call for that.
        """

        return self._request("GET", "/api/agent-commit")

    def probe(self, container: str) -> dict:
        """Where ``odoo_bin`` and the database list come from.

        The probe runs inside the target container using its own Python, so
        the daemon never needs network access to that project's Postgres.
        """

        return self._request("POST", "/api/probe", json={"container": container})

    def open_session(
        self,
        *,
        container: str,
        database: str,
        odoo_bin: str,
        owner: dict | None = None,
        allow_commit: bool | None = None,
        **extra,
    ) -> dict:
        """``odoo_bin`` is not optional.

        The daemon answers 422 "container, database and odoo_bin are
        required" without it — take the value from :meth:`probe`.
        """
        payload = {
            "container": container,
            "database": database,
            "odoo_bin": odoo_bin,
            "owner": owner,
            "allow_commit": allow_commit,
            **extra,
        }

        return self._request(
            "POST",
            "/api/sessions",
            json={key: value for key, value in payload.items() if value is not None},
            timeout=SESSION_OPEN_TIMEOUT,
        )

    def list_sessions(self) -> list:

        return self._request("GET", "/api/sessions")

    def get_session(self, session_id: str) -> dict:

        return self._request("GET", f"/api/sessions/{session_id}")

    def exec(
        self, session_id: str, code: str, *, write_key: str, timeout: float | None = None
    ) -> dict:
        """``timeout`` is ours, on the HTTP call: the daemon's ExecBody has

        no such field. On expiry the command keeps running in the session,
        so a caller that cares calls ``interrupt``.
        """
        extra = {} if timeout is None else {"timeout": timeout}

        return self._request(
            "POST",
            f"/api/sessions/{session_id}/exec",
            json={"code": code},
            write_key=write_key,
            **extra,
        )

    def run_test(
        self,
        session_id: str,
        test: str,
        *,
        write_key: str,
        timeout: float | None = None,
        params: str | None = None,
    ) -> dict:
        body: dict = {"test": test}
        if timeout is not None:
            body["timeout"] = timeout
        if params is not None:
            # Sheller 1.10.0: goes on the test tag in brackets, and Hoot reads
            # it as a filter. The daemon refuses a bracket it cannot pair.
            body["params"] = params

        return self._request(
            "POST",
            f"/api/sessions/{session_id}/run_test",
            json=body,
            write_key=write_key,
            timeout=(timeout or DAEMON_TEST_TIMEOUT) + HTTP_MARGIN,
        )

    def commit(self, session_id: str, *, write_key: str) -> dict:

        return self._request(
            "POST", f"/api/sessions/{session_id}/commit", write_key=write_key
        )

    def rollback(self, session_id: str, *, write_key: str) -> dict:

        return self._request(
            "POST", f"/api/sessions/{session_id}/rollback", write_key=write_key
        )

    def close(self, session_id: str, *, write_key: str) -> dict:

        return self._request(
            "DELETE", f"/api/sessions/{session_id}", write_key=write_key
        )

    def source(self, session_id: str, *, write_key: str, **target) -> dict:
        """Read through ``exec``, with the snippet Sheller's MCP tool runs.

        ``target`` is ``path`` (with ``first``/``last``) or ``model`` (with
        ``method`` and ``module``). The daemon has no source route.
        """
        from odoo_migrator.sheller.source import read_source

        return read_source(self, session_id, write_key=write_key, **target)

    def journal(self, session_id: str) -> dict:
        """``GET /api/journals/{id}`` is ndjson, not a JSON object."""
        try:
            response = self._http.request("GET", f"/api/journals/{session_id}")
        except httpx2.RequestError as exc:
            raise ShellerUnreachable(
                f"Sheller daemon at {self._http.base_url} did not answer"
            ) from exc
        if response.status_code >= 400:
            _raise_http(response)

        return {"records": _records(response.text)}

    def feed(
        self, session_id: str, *, logs: bool = False, log_tail: int | None = None
    ) -> dict:
        """``GET /api/sessions/{id}/history``. A closed session still answers."""
        params: dict = {"logs": logs}
        if log_tail is not None:
            params["log_tail"] = log_tail

        return self._request(
            "GET", f"/api/sessions/{session_id}/history", params=params
        )

    def list_tests(self, session_id: str, *, module: str, write_key: str) -> dict:
        """``GET /api/containers/{container}/tests``. The catalogue is not session-keyed."""
        session = self.get_session(session_id)
        answer = self._request(
            "GET",
            f"/api/containers/{session['container']}/tests",
            params={"module": module},
            write_key=write_key,
        )

        return with_js_capability(answer, session)

    def interrupt(self, session_id: str, *, write_key: str) -> dict:

        return self._request(
            "POST", f"/api/sessions/{session_id}/interrupt", write_key=write_key
        )

    def _request(self, method: str, path: str, *, write_key: str | None = None, **kwargs):
        headers = dict(kwargs.pop("headers", None) or {})
        if write_key is not None:
            headers[SESSION_KEY_HEADER] = write_key
        try:
            response = self._http.request(
                method, path, headers=headers or None, **kwargs
            )
        except httpx2.TimeoutException as exc:
            # Not "the daemon did not answer": it is answering, the command
            # is simply still running. The caller may want to interrupt it.
            raise ShellerTimeout(f"no answer within the timeout for {path}") from exc
        except httpx2.RequestError as exc:
            raise ShellerUnreachable(
                f"Sheller daemon at {self._http.base_url} did not answer"
            ) from exc
        if response.status_code >= 400:
            _raise_http(response)
        if not response.content:

            return {}
        try:

            return response.json()
        except ValueError:
            # A proxy's error page, a daemon half-way through a restart.
            raise ShellerError(
                f"Sheller answered {path} with something that is not JSON: "
                f"{response.text[:200]}",
                status=response.status_code,
            ) from None


def _records(text: str) -> list:
    """A journal's lines. One cut short — the journal is appended to while
    it is read — is left out rather than losing the rest with it."""
    records = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except ValueError:
            continue

    return records


def _session_dead_message(detail) -> str:
    """The daemon's reason, not the bare status name.

    An open that dies puts the cause in the body: a string from the start
    (``session did not start`` plus the shell's stderr) or ``session_gone``
    with ``reason`` once the session had an id. The checkpoint shows this
    string and nothing else.
    """
    if isinstance(detail, dict):
        error = str(detail.get("error") or "session_dead")
        reason = str(detail.get("reason") or detail.get("message") or "").strip()
        if reason and reason not in error:

            return f"{error}: {reason}"

        return error
    if isinstance(detail, str) and detail.strip():

        return detail.strip()

    return "session_dead"


def _needs_a_human_message(refusal: dict) -> str:
    """The daemon's ``act`` and ``recovery``, which is what a person acts on."""
    parts = [f"needs_a_human: {refusal.get('act') or 'this needs a person'}"]
    if refusal.get("recovery"):
        parts.append(str(refusal["recovery"]))

    return " — ".join(parts)


def _raise_http(response: httpx2.Response) -> None:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    if response.status_code == 403:
        refusal = detail.get("detail") if isinstance(detail, dict) else None
        if isinstance(refusal, dict) and refusal.get("error") == "needs_a_human":
            raise ShellerNeedsHuman(_needs_a_human_message(refusal), status=403, detail=detail)
        raise ShellerNotOwner("not_owner", status=403, detail=detail)
    if response.status_code == 409:
        raise ShellerBusy(
            f"session_busy: {detail}", status=409, detail=detail
        )
    if response.status_code == 504:
        # The daemon's own ceiling (300s on exec) expired. As with ours, the
        # command is still running in the session.
        raise ShellerTimeout(
            f"the daemon gave up waiting: {detail}", status=504, detail=detail
        )
    if response.status_code == 410:
        raise ShellerSessionDead(
            _session_dead_message(detail), status=410, detail=detail
        )
    raise ShellerError(
        f"Sheller HTTP {response.status_code}: {detail}",
        status=response.status_code,
        detail=detail,
    )
