"""A model on this machine, over the OpenAI-compatible chat API.

LM Studio, Ollama and llama.cpp's server all answer ``/v1/chat/completions``
with tool calls, so one provider serves any of them. The tool loop is the
one OpenAI's own provider runs: see ``openai_chat``.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx2

from odoo_migrator.runner.cartridge import Budget
from odoo_migrator.runner.context import StepResult
from odoo_migrator.runner.providers.base import SettingField
from odoo_migrator.runner.providers.openai_chat import run_chat_step
from odoo_migrator.runner.tools import BoundTool, ReportTool

# Reading a 40K-token prompt on a laptop takes minutes, and that is one
# turn. The read timeout is the longest a single turn may take; a hung
# server still fails the step instead of holding the job forever.
_TIMEOUT = httpx2.Timeout(5.0, read=900.0)
# The card asks for the model list on every render. A server that is down
# costs this much, once per page.
_LIST_TIMEOUT = httpx2.Timeout(1.0)


class LocalProvider:
    name = "local"
    settings = (SettingField("base_url", "Local model server URL", secret=False),)

    def __init__(self, *, transport=None, base_url: str | None = None):
        # Both for the tests: a fake server, and a URL that is not a setting.
        self._transport = transport
        self._base_url = base_url

    @property
    def models(self) -> frozenset[str]:
        """What the server has loaded, asked each time.

        Whatever is loaded in LM Studio now is what can run; a list kept
        here would name models that are gone. Embedding models answer the
        same endpoint and cannot chat, so they are left out.
        """
        url = self._url()
        if not url:

            return frozenset()
        try:
            with self._client(_LIST_TIMEOUT) as client:
                response = client.get("/models")
                response.raise_for_status()
                rows = response.json().get("data") or []
        except (httpx2.HTTPError, ValueError):

            return frozenset()
        names = {str(row.get("id") or "") for row in rows if isinstance(row, dict)}

        return frozenset(name for name in names if name and "embed" not in name)

    def accepts_model(self, name: str) -> bool:
        # Not the loaded list: a cartridge may name a model loaded later,
        # and the server says so itself when it is not.

        return bool(name.strip())

    def ensure_ready(self) -> None:
        if not self._url():
            raise RuntimeError(_NO_URL)

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
        if not self._url():

            return StepResult(ok=False, output=_NO_URL)
        with self._client(_TIMEOUT) as client:

            return run_chat_step(
                client,
                system=system,
                prompt=prompt,
                tools=tools,
                report=report,
                model=model,
                budget=budget,
                stop_check=stop_check,
            )

    def _url(self) -> str:
        if self._base_url is not None:

            return self._base_url.strip().rstrip("/")
        from odoo_migrator.settings import provider_setting

        return provider_setting("local", "base_url").strip().rstrip("/")

    def _client(self, timeout) -> httpx2.Client:

        return httpx2.Client(base_url=self._url(), timeout=timeout, transport=self._transport)


_NO_URL = (
    "no local model server is set — set its URL on the settings page, "
    "e.g. http://127.0.0.1:1234/v1 for LM Studio."
)
