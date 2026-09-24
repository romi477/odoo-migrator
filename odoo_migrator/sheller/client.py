"""HTTP client for a Sheller daemon, and the helper that must not invent an MCP command."""

import json
from typing import Protocol

import httpx2

from odoo_migrator.sheller.errors import (
    ShellerError,
    ShellerNotOwner,
    ShellerSessionDead,
    ShellerTimeout,
    ShellerUnreachable,
)

SESSION_KEY_HEADER = "X-OS-Session-Key"


def mcp_for_ui(health: dict) -> dict | None:
    """The MCP spawn command for *this* daemon, or None.

    ``"mcp": null`` means the daemon cannot say. Inventing ``docker exec``
    on its behalf would only fail later; hide it instead.
    """
    mcp = health.get("mcp")
    if mcp is None:

        return None

    return mcp


class ShellerClient(Protocol):
    def health(self) -> dict: ...

    def list_containers(self) -> list: ...

    def probe(self, container: str) -> dict: ...

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
    ) -> dict: ...

    def commit(self, session_id: str, *, write_key: str) -> dict: ...

    def rollback(self, session_id: str, *, write_key: str) -> dict: ...

    def close(self, session_id: str, *, write_key: str) -> dict: ...

    def source(self, session_id: str, *, dotted: str, write_key: str) -> dict: ...

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

    def list_containers(self) -> list:

        return self._request("GET", "/api/containers")

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
    ) -> dict:
        body: dict = {"test": test}
        if timeout is not None:
            body["timeout"] = timeout

        return self._request(
            "POST",
            f"/api/sessions/{session_id}/run_test",
            json=body,
            write_key=write_key,
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

    def source(self, session_id: str, *, dotted: str, write_key: str) -> dict:
        """Read through ``exec``. The daemon has no source route; its MCP tool is this call."""

        return self.exec(session_id, dotted, write_key=write_key)

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
        records = [
            json.loads(line) for line in response.text.splitlines() if line.strip()
        ]

        return {"records": records}

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

        return self._request(
            "GET",
            f"/api/containers/{session['container']}/tests",
            params={"module": module},
            write_key=write_key,
        )

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

        return response.json()


def _raise_http(response: httpx2.Response) -> None:
    try:
        detail = response.json()
    except ValueError:
        detail = response.text
    if response.status_code == 403:
        raise ShellerNotOwner("not_owner", status=403, detail=detail)
    if response.status_code == 410:
        raise ShellerSessionDead("session_dead", status=410, detail=detail)
    raise ShellerError(
        f"Sheller HTTP {response.status_code}: {detail}",
        status=response.status_code,
        detail=detail,
    )
