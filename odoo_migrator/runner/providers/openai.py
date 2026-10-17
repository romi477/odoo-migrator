"""OpenAI, over its chat completions API.

The tool loop is the one a local model server runs; see ``openai_chat``.
What is here is the part that is OpenAI's: the key, the fixed address, and
a refusal worded so that no part of the key is ever repeated.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx2

from odoo_migrator.runner.cartridge import Budget
from odoo_migrator.runner.context import StepResult
from odoo_migrator.runner.providers.base import SettingField
from odoo_migrator.runner.providers.openai_chat import _server_said, run_chat_step
from odoo_migrator.runner.tools import BoundTool, ReportTool

BASE_URL = "https://api.openai.com/v1"
# A turn on a long prompt can take minutes. The read timeout is the longest
# one turn may take; a hung connection still fails the step.
_TIMEOUT = httpx2.Timeout(10.0, read=900.0)

_NO_KEY = (
    "no OpenAI API key is set — the agent steps of a cartridge cannot run. "
    "Set it on the settings page."
)
_REJECTED = "OpenAI rejected the API key — check it on the settings page."


class OpenAIProvider:
    name = "openai"
    models = frozenset({"gpt-6-sol"})
    settings = (SettingField("api_key", "OpenAI API key"),)

    def __init__(self, *, transport=None, api_key: str | None = None):
        # Both for the tests: a fake server, and a key that is not a setting.
        self._transport = transport
        self._api_key = api_key

    def accepts_model(self, name: str) -> bool:

        return name in self.models

    def ensure_ready(self) -> None:
        if not self._key():
            raise RuntimeError(_NO_KEY)

    def run_step(
        self,
        *,
        system: str,
        prompt: str,
        tools: list[BoundTool],
        report: ReportTool,
        model: str,
        budget: Budget,
        stop_check: Callable[[], None] | None = None,
    ) -> StepResult:
        if not self._key():

            return StepResult(ok=False, output=_NO_KEY)
        with self._client() as client:

            return run_chat_step(
                client,
                system=system,
                prompt=prompt,
                tools=tools,
                report=report,
                model=model,
                budget=budget,
                stop_check=stop_check,
                server="OpenAI",
                refusal=_refusal,
            )

    def _key(self) -> str:
        if self._api_key is not None:

            return self._api_key.strip()
        from odoo_migrator.settings import provider_setting

        return provider_setting("openai", "api_key").strip()

    def _client(self) -> httpx2.Client:

        return httpx2.Client(
            base_url=BASE_URL,
            timeout=_TIMEOUT,
            transport=self._transport,
            headers={"Authorization": f"Bearer {self._key()}"},
        )


def _refusal(response: httpx2.Response) -> str:
    """OpenAI's words for an error, except a 401: that message quotes the key."""
    if response.status_code == 401:

        return _REJECTED

    return _server_said(response)
