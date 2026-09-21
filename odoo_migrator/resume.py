"""POST ``{"answer": "..."}`` to n8n's Wait-node resume URL."""

from __future__ import annotations

import httpx2

from odoo_migrator.contract import ContractError


class ResumeError(ContractError):
    """n8n did not accept the answer. The job stays waiting."""


class FakeResumeClient:
    """Records the POST. The suite does not need n8n running."""

    def __init__(self):
        self.calls: list[dict] = []
        self.error: ResumeError | None = None

    def resume(self, url: str, answer: str) -> None:
        if self.error is not None:
            raise self.error
        self.calls.append({"url": url, "answer": answer})


class HttpResumeClient:
    def resume(self, url: str, answer: str) -> None:
        try:
            with httpx2.Client(timeout=30.0) as client:
                response = client.post(url, json={"answer": answer})
                response.raise_for_status()
        except httpx2.HTTPError as exc:
            raise ResumeError(f"n8n did not accept the answer: {exc}") from exc
