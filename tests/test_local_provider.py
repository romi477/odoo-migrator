"""A model on this machine, over the OpenAI-compatible API.

LM Studio, Ollama and llama.cpp's server all answer ``/v1/chat/completions``
with tool calls. A model there costs nothing per token, which is what makes
it the one to debug the pipeline with.
"""

from doubles.openai_server import OpenAIServer, reply, tool_call

from odoo_migrator.runner.cartridge import Budget, Step
from odoo_migrator.runner.providers import get
from odoo_migrator.runner.providers.local import LocalProvider
from odoo_migrator.runner.tools import BoundTool, ToolRefused, report_tool

URL = "http://127.0.0.1:1234/v1"
MODEL = "qwen/qwen3.8-27b"


def _step(**extra) -> Step:
    fields = {"kind": "agent", "id": "port", "sessions": ["new"], "tools": ["exec"], "prompt": "port"}
    fields.update(extra)

    return Step(**fields)


def _run(server: OpenAIServer, *, tools=(), step=None, budget=None, system="be an agent"):
    provider = LocalProvider(transport=server.transport(), base_url=URL)

    return provider.run_step(
        system=system,
        prompt="port sale_custom",
        tools=list(tools),
        report=report_tool(step or _step()),
        model=MODEL,
        budget=budget or Budget(tool_calls=10),
    )


def test_a_step_runs_a_tool_and_ends_at_report():
    seen = []

    def exec_(**payload):
        seen.append(payload)

        return "42"

    server = OpenAIServer(
        [
            reply(tool_call("exec", {"side": "new", "code": "6 * 7"}, "call_a")),
            reply(tool_call("report", {"answer": "ported", "needs_human": True}, "call_b")),
        ]
    )

    result = _run(server, tools=[BoundTool("exec", exec_)], step=_step(raises=["needs_human"]))

    assert result.ok
    assert result.answer == "ported"
    assert result.flags == {"needs_human"}
    assert seen == [{"side": "new", "code": "6 * 7"}]
    first, second = server.requests
    assert first["model"] == MODEL
    assert first["messages"][0] == {"role": "system", "content": "be an agent"}
    assert first["messages"][1] == {"role": "user", "content": "port sale_custom"}
    offered = [tool["function"]["name"] for tool in first["tools"]]
    assert offered == ["exec", "report"]
    assert second["messages"][-1] == {"role": "tool", "tool_call_id": "call_a", "content": "42"}
    assert second["messages"][-2]["tool_calls"][0]["id"] == "call_a"


def test_a_refused_report_goes_back_to_the_model():
    step = _step(raises=["small_fix", "start_over"])
    server = OpenAIServer(
        [
            reply(tool_call("report", {"answer": "x", "small_fix": True, "start_over": True})),
            reply(tool_call("report", {"answer": "fix it", "small_fix": True}, "call_2")),
        ]
    )

    result = _run(server, step=step)

    assert result.ok
    assert result.flags == {"small_fix"}
    told = server.requests[1]["messages"][-1]
    assert told["role"] == "tool"
    assert "cannot both be true" in told["content"]


def test_a_refused_tool_is_told_to_the_model_not_the_end_of_the_step():
    def refuse(**_payload):
        raise ToolRefused("side old is not declared")

    server = OpenAIServer(
        [
            reply(tool_call("exec", {"side": "old", "code": "1"})),
            reply(tool_call("report", {"answer": "done"}, "call_2")),
        ]
    )

    result = _run(server, tools=[BoundTool("exec", refuse)])

    assert result.ok
    assert "side old is not declared" in server.requests[1]["messages"][-1]["content"]


def test_arguments_that_are_not_json_are_told_to_the_model():
    server = OpenAIServer(
        [
            reply(tool_call("exec", "{side: new")),
            reply(tool_call("report", {"answer": "done"}, "call_2")),
        ]
    )

    result = _run(server, tools=[BoundTool("exec", lambda **_kw: "ok")])

    assert result.ok
    assert "not valid JSON" in server.requests[1]["messages"][-1]["content"]


def test_a_step_stops_at_the_tool_call_budget():
    calls = [reply(tool_call("exec", {"side": "new", "code": "1"}, f"c{n}")) for n in range(5)]
    server = OpenAIServer(calls)

    result = _run(server, tools=[BoundTool("exec", lambda **_kw: "ok")], budget=Budget(tool_calls=2))

    assert not result.ok
    assert "tool-call budget" in result.output


def test_an_answer_without_report_fails_the_step_and_keeps_what_it_said():
    server = OpenAIServer([reply(content="I think the module is fine.")])

    result = _run(server)

    assert not result.ok
    assert "without a report" in result.output
    assert "I think the module is fine." in result.detail


def test_a_server_error_fails_the_step_with_the_servers_words():
    server = OpenAIServer(status=400, error="context length exceeded")

    result = _run(server)

    assert not result.ok
    assert "context length exceeded" in result.output


def test_usage_counts_only_what_is_new_each_turn():
    """Each turn resends the whole conversation. The server keeps the prefix
    in its cache, so counting the full prompt every turn would grow the
    budget with the square of the turns, as it once did for Anthropic."""
    server = OpenAIServer(
        [
            reply(tool_call("exec", {"side": "new", "code": "1"}), prompt=1000, completion=50),
            reply(tool_call("report", {"answer": "done"}, "call_2"), prompt=1100, completion=20),
        ]
    )

    result = _run(server, tools=[BoundTool("exec", lambda **_kw: "ok")])

    assert result.tokens_in == 1000 + 50
    assert result.tokens_cached == 1050
    assert result.tokens_out == 70


def test_a_server_that_reports_its_cache_is_believed():
    server = OpenAIServer(
        [reply(tool_call("report", {"answer": "done"}), prompt=1000, completion=10, cached=800)]
    )

    result = _run(server)

    assert result.tokens_in == 200
    assert result.tokens_cached == 800


def test_the_models_are_the_ones_the_server_has_loaded():
    server = OpenAIServer(models=[MODEL, "text-embedding-nomic-embed-text-v1.5"])
    provider = LocalProvider(transport=server.transport(), base_url=URL)

    assert provider.models == frozenset({MODEL})
    assert provider.accepts_model(MODEL)


def test_no_server_url_means_no_models_and_a_step_that_says_why():
    provider = LocalProvider(base_url="")

    assert provider.models == frozenset()
    result = provider.run_step(
        system="",
        prompt="x",
        tools=[],
        report=report_tool(_step()),
        model=MODEL,
        budget=Budget(),
    )

    assert not result.ok
    assert "settings page" in result.output


def test_the_server_url_is_a_setting_shown_as_written(client):
    page = client.post(
        "/settings",
        data={
            "sheller_base_url": "http://127.0.0.1:8765",
            "sheller_image_tag": "latest",
            "provider_local_base_url": URL,
        },
        # Where it lands may list the providers' models, which would ask
        # whatever answers on that URL on this machine.
        follow_redirects=False,
    )

    assert page.status_code == 303
    settings = client.get("/settings").text
    assert f'value="{URL}"' in settings
    assert get("local").name == "local"
