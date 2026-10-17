"""OpenAI, over its chat completions API.

The loop is the one a local model uses; what differs is the key, the fixed
address and what a refusal is allowed to say.
"""

import pytest
from doubles.openai_server import OpenAIServer, reply, tool_call

from odoo_migrator.runner.cartridge import Budget, Step
from odoo_migrator.runner.providers import get, known_models, model_allowed
from odoo_migrator.runner.providers.openai import OpenAIProvider
from odoo_migrator.runner.tools import BoundTool, report_tool

KEY = "sk-proj-abcdefghijklmnopqrstuvwxyz0123456789"
MODEL = "gpt-6-sol"


def _step(**extra) -> Step:
    fields = {"kind": "agent", "id": "port", "sessions": ["target"], "tools": ["exec"], "prompt": "port"}
    fields.update(extra)

    return Step(**fields)


def _run(server: OpenAIServer, *, key=KEY, tools=(), model=MODEL):
    provider = OpenAIProvider(transport=server.transport(), api_key=key)

    return provider.run_step(
        system="be an agent",
        prompt="port sale_custom",
        tools=list(tools),
        report=report_tool(_step()),
        model=model,
        budget=Budget(tool_calls=10),
    )


def test_a_step_runs_a_tool_and_ends_at_report():
    seen = []

    def exec_(**payload):
        seen.append(payload)

        return "42"

    server = OpenAIServer(
        [
            reply(tool_call("exec", {"side": "target", "code": "6 * 7"}, "call_a")),
            reply(tool_call("report", {"answer": "ported"}, "call_b")),
        ],
        api_key=KEY,
    )

    result = _run(server, tools=[BoundTool("exec", exec_)])

    assert result.ok
    assert result.answer == "ported"
    assert seen == [{"side": "target", "code": "6 * 7"}]
    assert server.requests[0]["model"] == MODEL


def test_the_key_goes_in_the_authorization_header_and_nowhere_else():
    server = OpenAIServer([reply(tool_call("report", {"answer": "ok"}))], api_key=KEY)

    _run(server)

    assert server.requests
    assert KEY not in str(server.requests)


def test_a_rejected_key_fails_the_step_without_repeating_any_of_the_key():
    server = OpenAIServer(api_key=KEY)

    result = _run(server, key="sk-proj-wrongwrongwrongwrong")

    assert not result.ok
    assert "settings page" in result.output
    assert "sk-proj" not in result.output
    assert "wrong" not in result.output
    assert "sk-proj" not in (result.detail or "")


def test_no_key_means_no_step_and_no_request():
    server = OpenAIServer(api_key=KEY)
    provider = OpenAIProvider(transport=server.transport(), api_key="")

    with pytest.raises(RuntimeError, match="settings page"):
        provider.ensure_ready()
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
    assert server.requests == []


def test_any_other_server_error_still_says_what_it_said():
    server = OpenAIServer(status=400, error="context length exceeded", api_key=KEY)

    result = _run(server)

    assert not result.ok
    assert "context length exceeded" in result.output


def test_the_models_are_a_fixed_list_and_need_no_key_to_show():
    provider = OpenAIProvider(api_key="")

    assert provider.models == frozenset({"gpt-6-sol"})
    assert provider.accepts_model("gpt-6-sol")
    assert not provider.accepts_model("claude-sonnet-5-5")


def test_a_cartridge_may_name_it(client):
    assert known_models()["openai"] == frozenset({"gpt-6-sol"})
    assert model_allowed("gpt-6-sol")


def test_the_registry_has_it_and_its_key_is_a_secret_setting():
    provider = get("openai")

    assert provider.name == "openai"
    assert [(field.key, field.secret) for field in provider.settings] == [("api_key", True)]


def test_the_key_is_stored_and_never_shown_back(client):
    page = client.post(
        "/settings",
        data={
            "sheller_base_url": "http://127.0.0.1:8765",
            "sheller_image_tag": "latest",
            "provider_openai_api_key": KEY,
        },
        follow_redirects=False,
    )

    assert page.status_code == 303
    settings = client.get("/settings").text
    assert KEY not in settings
    assert 'name="provider_openai_api_key"' in settings
    from odoo_migrator.runner.providers.openai import OpenAIProvider as Fresh

    assert Fresh()._key() == KEY


def test_a_stop_ends_the_step_between_two_turns():
    from odoo_migrator.runner.context import Stopped

    server = OpenAIServer(
        [reply(tool_call("exec", {"side": "target", "code": "1"}, f"c{n}")) for n in range(3)],
        api_key=KEY,
    )

    def stop_check():
        if server.requests:
            raise Stopped("during port")

    provider = OpenAIProvider(transport=server.transport(), api_key=KEY)
    with pytest.raises(Stopped):
        provider.run_step(
            system="",
            prompt="x",
            tools=[BoundTool("exec", lambda **_kw: "ok")],
            report=report_tool(_step()),
            model=MODEL,
            budget=Budget(),
            stop_check=stop_check,
        )

    assert len(server.requests) == 1


def test_settings_list_the_providers_anthropic_openai_cursor_local(client):
    from odoo_migrator.settings import get_app_settings

    order = []
    for field in get_app_settings()["provider_fields"]:
        if field["provider"] not in order:
            order.append(field["provider"])
    page = client.get("/settings").text
    places = [page.index(f'name="provider_{name}_') for name in order]

    assert order == ["anthropic", "openai", "cursor", "local"]
    assert places == sorted(places)
