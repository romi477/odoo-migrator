"""An OpenAI-compatible server, as LM Studio, Ollama and llama.cpp answer.

Mounted as an ``httpx2.MockTransport``: the provider speaks HTTP to it the
way it speaks to the real server, and every request it sent is kept.
"""

import json

import httpx2


def tool_call(name: str, arguments, call_id: str = "call_1") -> dict:
    """One entry of ``message.tool_calls``. Arguments travel as a JSON string."""
    text = arguments if isinstance(arguments, str) else json.dumps(arguments)

    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": text}}


def reply(*calls: dict, content: str = "", prompt: int = 0, completion: int = 0, cached=None) -> dict:
    """One chat completion: the assistant's text, its tool calls, its usage."""
    message: dict = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = list(calls)
    usage: dict = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
    }
    if cached is not None:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}

    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if calls else "stop",
            }
        ],
        "usage": usage,
    }


class OpenAIServer:
    """Answers ``/v1/chat/completions`` from a script and ``/v1/models`` from a list."""

    def __init__(
        self,
        replies=(),
        *,
        models=("qwen/qwen3.8-27b",),
        status: int = 200,
        error="",
        api_key: str | None = None,
    ):
        # A server that wants a key answers a missing or wrong one with 401,
        # and its message quotes the start and end of what it was sent, as
        # api.openai.com does. The local servers ask for none.
        self.api_key = api_key
        self.replies = list(replies)
        self.models = list(models)
        self.status = status
        self.error = error
        self.requests: list[dict] = []

    def transport(self) -> httpx2.MockTransport:

        return httpx2.MockTransport(self._handle)

    def _handle(self, request: httpx2.Request) -> httpx2.Response:
        if self.api_key is not None:
            sent = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
            if sent != self.api_key:
                shown = f"{sent[:7]}***{sent[-4:]}" if sent else "(none)"
                error = {
                    "message": f"Incorrect API key provided: {shown}.",
                    "type": "invalid_request_error",
                    "code": "invalid_api_key",
                }

                return httpx2.Response(401, json={"error": error})
        if request.url.path.endswith("/models"):
            data = [{"id": name, "object": "model", "owned_by": "organization_owner"} for name in self.models]

            return httpx2.Response(200, json={"object": "list", "data": data})
        self.requests.append(json.loads(request.content))
        if self.status != 200:

            return httpx2.Response(self.status, json={"error": self.error})
        if not self.replies:

            return httpx2.Response(200, json=reply(content="I am done."))

        return httpx2.Response(200, json=self.replies.pop(0))
