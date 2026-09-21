"""Talk to n8n's public API. Ready means /rest/settings returns JSON."""

from __future__ import annotations

import time

import httpx2


class N8nError(Exception):
    def __init__(self, message: str, *, status: int | None = None):
        super().__init__(message)
        self.status = status


class N8nNotReady(N8nError):
    """/healthz is 200 while n8n still serves 'starting up'. Wait for JSON."""


def wait_until_ready(fetch, *, timeout: float = 60.0, interval: float = 0.5):
    """``fetch`` must return a dict. Anything else is still starting."""
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            body = fetch()
            if isinstance(body, dict):

                return body
            last = body
        except N8nNotReady as exc:
            last = exc
        except ValueError as exc:
            last = exc
        time.sleep(interval)
    raise N8nNotReady(f"n8n /rest/settings did not return JSON: {last!r}")


class HttpN8nClient:
    def __init__(self, base_url: str, *, api_key: str = "", timeout: float = 30.0, http=None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self._http = http or httpx2.Client(base_url=self.base_url, timeout=timeout)

    def wait_until_ready(self, timeout: float = 60.0) -> dict:

        return wait_until_ready(self.rest_settings, timeout=timeout)

    def rest_settings(self) -> dict:
        response = self._http.get("/rest/settings")
        ctype = response.headers.get("content-type", "")
        if "json" not in ctype:
            raise N8nNotReady(response.text[:200])
        try:

            return response.json()
        except ValueError as exc:
            raise N8nNotReady(response.text[:200]) from exc

    def create_workflow(self, body: dict) -> dict:

        return self._request("POST", "/api/v1/workflows", json=body)

    def activate(self, workflow_id: str) -> dict:

        return self._request("POST", f"/api/v1/workflows/{workflow_id}/activate")

    def trigger(self, path: str, payload: dict) -> dict:
        slug = path.strip("/")

        return self._request("POST", f"/webhook/{slug}", json=payload, keyed=False)

    def import_package(
        self, archive: bytes, *, filename: str = "cartridge.n8np", policy: str = "update"
    ) -> dict:
        files = {"file": (filename, archive, "application/octet-stream")}
        data = {"workflowConflictPolicy": policy}

        return self._request(
            "POST", "/api/v1/n8n-packages/import", files=files, data=data
        )

    def _request(self, method: str, path: str, *, keyed: bool = True, **kwargs) -> dict:
        headers = dict(kwargs.pop("headers", None) or {})
        if keyed and self.api_key:
            headers["X-N8N-API-KEY"] = self.api_key
        try:
            response = self._http.request(method, path, headers=headers or None, **kwargs)
        except httpx2.RequestError as exc:
            raise N8nError(f"n8n at {self.base_url} did not answer") from exc
        if response.status_code >= 400:
            raise N8nError(
                response.text[:500] or response.reason_phrase,
                status=response.status_code,
            )
        if not response.content:

            return {}

        return response.json()
