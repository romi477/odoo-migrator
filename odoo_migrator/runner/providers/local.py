"""A model on this machine, over the OpenAI-compatible chat API.

LM Studio, Ollama and llama.cpp's server all answer ``/v1/chat/completions``
with tool calls, so one provider serves any of them. It runs its own tool
loop: there is no SDK runner to hand the tools to.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx2

from odoo_migrator.runner.cartridge import Budget
from odoo_migrator.runner.context import StepResult
from odoo_migrator.runner.providers.base import SettingField
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
        by_name = {tool.name: tool for tool in tools}
        offered = [_spec(tool) for tool in tools] + [_spec(report)]
        messages: list[dict] = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": prompt or ""})
        usage = {"in": 0, "out": 0, "cached": 0}
        context = 0
        calls = 0
        turns = 0
        said = ""
        with self._client(_TIMEOUT) as client:
            while True:
                # Before each turn, after the tools of the last one ran: what
                # they wrote is on disk and in the trace, as when a step ends.
                if stop_check is not None and turns:
                    stop_check()
                if budget.tokens is not None and usage["in"] + usage["out"] > budget.tokens:

                    return _failed("stopped at the token budget", usage, turns, said, tools)
                try:
                    response = client.post(
                        "/chat/completions",
                        json={"model": model, "messages": messages, "tools": offered},
                    )
                    response.raise_for_status()
                    body = response.json()
                except httpx2.HTTPStatusError as exc:

                    return _failed(_server_said(exc.response), usage, turns, said, tools)
                except (httpx2.HTTPError, ValueError) as exc:

                    return _failed(f"the local model server failed: {exc}", usage, turns, said, tools)
                turns += 1
                context = _add_usage(body, usage, context)
                message = ((body.get("choices") or [{}])[0]).get("message") or {}
                said = str(message.get("content") or "").strip() or said
                asked = message.get("tool_calls") or []
                if not asked:

                    return _failed(
                        f"the step ended without a report, after {turns} turn(s)",
                        usage,
                        turns,
                        said,
                        tools,
                    )
                messages.append(
                    {"role": "assistant", "content": message.get("content") or "", "tool_calls": asked}
                )
                for call in asked:
                    function = call.get("function") or {}
                    name = str(function.get("name") or "")
                    payload, broken = _arguments(function.get("arguments"))
                    if broken:
                        answer = broken
                    elif name == report.name:
                        # A refused report goes back to the model, as on the
                        # other providers: the runner never picks for it.
                        answer = report.refusal(payload)
                        if not answer:

                            return _from_report(report, payload, usage, _account(turns, said, tools))
                    elif name in by_name:
                        if budget.tool_calls is not None and calls >= budget.tool_calls:

                            return _failed("stopped at the tool-call budget", usage, turns, said, tools)
                        calls += 1
                        answer = _call(by_name[name], payload)
                    else:
                        answer = f"there is no tool named {name!r}"
                    messages.append(
                        {"role": "tool", "tool_call_id": call.get("id") or "", "content": answer}
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


def _spec(tool) -> dict:

    return {
        "type": "function",
        "function": {
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        },
    }


def _arguments(raw) -> tuple[dict, str]:
    """The call's arguments, or the words that tell the model they were bad.

    A local model writes a broken JSON string more often than a hosted one.
    Failing the step on it throws the turn away; telling it lets it retry.
    """
    if isinstance(raw, dict):

        return raw, ""
    try:
        parsed = json.loads(raw or "{}")
    except ValueError:

        return {}, "refused: the arguments are not valid JSON. Call the tool again."
    if not isinstance(parsed, dict):

        return {}, "refused: the arguments must be a JSON object. Call the tool again."

    return parsed, ""


def _call(tool: BoundTool, payload: dict) -> str:
    """Run one tool; whatever it raises is the model's to read.

    The Anthropic SDK's runner does the same with an exception from a tool.
    A wrong argument name is a TypeError here, and the model can fix it.
    """
    try:

        return str(tool(**payload))
    except Exception as exc:  # noqa: BLE001 - the model reads it and calls again.

        return f"refused: {exc}"


def _add_usage(body: dict, usage: dict, context: int) -> int:
    """Count one turn and return the length of the conversation after it.

    Every turn resends the whole conversation, and the server keeps its
    prefix cached. When it does not report the cached part, the previous
    turn's prompt and answer are taken as cached: counting the full prompt
    every turn grew the budget with the square of the turns.
    """
    counted = body.get("usage") or {}
    prompt = int(counted.get("prompt_tokens") or 0)
    completion = int(counted.get("completion_tokens") or 0)
    details = counted.get("prompt_tokens_details") or {}
    reported = details.get("cached_tokens")
    cached = int(reported) if reported is not None else min(context, prompt)
    usage["in"] += prompt - cached
    usage["cached"] += cached
    usage["out"] += completion

    return prompt + completion


def _server_said(response) -> str:
    try:
        error = response.json().get("error")
    except ValueError:
        error = response.text
    if isinstance(error, dict):
        error = error.get("message") or error

    return f"the local model server answered {response.status_code}: {error}"


def _counts(usage: dict) -> dict:

    return {
        "tokens_in": usage["in"],
        "tokens_out": usage["out"],
        "tokens_cached": usage["cached"],
    }


def _account(turns: int, said: str, tools: list) -> str:
    rows = [
        f"turns: {turns}",
        f"tools offered: {', '.join(tool.name for tool in tools)}",
        "",
        "last text from the model:",
        said or "(none)",
    ]

    return "\n".join(rows)


def _failed(output: str, usage: dict, turns: int, said: str, tools: list) -> StepResult:

    return StepResult(ok=False, output=output, detail=_account(turns, said, tools), **_counts(usage))


def _from_report(report: ReportTool, payload: dict, usage: dict, detail: str) -> StepResult:
    flags = {
        flag
        for flag in report.input_schema["properties"]
        if flag != "answer" and payload.get(flag)
    }

    return StepResult(
        ok=True,
        flags=flags,
        answer=str(payload.get("answer") or ""),
        detail=detail,
        **_counts(usage),
    )
