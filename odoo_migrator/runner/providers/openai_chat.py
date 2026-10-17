"""One tool loop over an OpenAI-style ``/chat/completions`` endpoint.

LM Studio, Ollama, llama.cpp and api.openai.com all answer it with tool
calls, so the loop is written once. What differs between them — where the
server is, whether it wants a key, what a refusal may say — stays with the
provider that owns the client.
"""

from __future__ import annotations

import json
from collections.abc import Callable

import httpx2

from odoo_migrator.runner.cartridge import Budget
from odoo_migrator.runner.context import StepResult
from odoo_migrator.runner.tools import BoundTool, ReportTool


def run_chat_step(
    client: httpx2.Client,
    *,
    system: str,
    prompt: str,
    tools: list[BoundTool],
    report: ReportTool,
    model: str,
    budget: Budget,
    stop_check: Callable[[], None] | None = None,
    server: str = "the local model server",
    refusal: Callable[[httpx2.Response], str] | None = None,
) -> StepResult:
    """Run one step to its report. The caller opens and closes ``client``.

    ``server`` names who failed when no answer came at all; ``refusal``
    words an HTTP error status, and a provider whose server quotes what it
    was sent uses it to leave that out.
    """
    words = refusal or _server_said
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

            return _failed(words(exc.response), usage, turns, said, tools)
        except (httpx2.HTTPError, ValueError) as exc:

            return _failed(f"{server} failed: {exc}", usage, turns, said, tools)
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
