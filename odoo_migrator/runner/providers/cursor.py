"""Cursor, via its Python SDK and in-process custom tools.

``LocalAgentOptions.custom_tools`` takes a ``CustomTool`` whose ``execute``
is a Python callable. The catalog is handed over directly, so this provider
does not start an MCP server. Built-in tools stay off: ``tools=[]``.
"""

from __future__ import annotations

import tempfile
from pathlib import Path
from urllib.parse import parse_qsl

from odoo_migrator.runner.cartridge import Budget
from odoo_migrator.runner.context import StepResult
from odoo_migrator.runner.providers.base import SettingField
from odoo_migrator.runner.tools import BoundTool, ReportTool, ToolRefused

# What the settings page lists. ``accepts_model`` is wider: ``auto-smart``
# and the same name with parameters are models, not members of this set.
_LISTED = frozenset({"composer-2.5", "composer-2", "auto"})

# Cursor's built-in tools that could touch this machine, denied by name.
#
# Not `tools=[]`. That reads like "offer no built-in tools" and also takes
# away `getMcpTools` and `mcp`, which are how an agent reaches a custom
# tool at all: with it, every tool in our catalog came back "Tool not
# available" and the agent wrote prose instead of working. Measured on a
# one-tool probe — called without it, refused with it.
#
# These are the API's own names, not the display names the agent recites,
# and it rejects one it does not know. A Cursor release that renames one
# therefore fails the step with a message naming every valid tool, which
# is a loud, one-run fix rather than a silently widened agent.
#
# Reads are left alone: the workspace is an empty temporary directory, so
# there is nothing in it to read. What is denied is the host and the
# network — shell, edits, fetches, the screen — and control over other
# agents and this one's own mode.
DENIED_BUILTINS = (
    "adopt",
    "applyAgentDiff",
    "await",
    "computerUse",
    "connectScm",
    "createAgent",
    "createPlan",
    "delete",
    "edit",
    "editPrLabels",
    "fetch",
    "generateImage",
    "piBash",
    "piEdit",
    "piWrite",
    "prManagement",
    "recordScreen",
    "replaceEnv",
    "sendToAgent",
    "setupVmEnvironment",
    "shell",
    "startGrindExecution",
    "startGrindPlanning",
    "stopAgent",
    "switchMode",
    "task",
    "webFetch",
    "webSearch",
    "writeCanvas",
    "writeShellStdin",
)


class CursorProvider:
    name = "cursor"
    models = _LISTED
    settings = (SettingField("api_key", "Cursor API key"),)

    def accepts_model(self, name: str) -> bool:
        bare = name.split("?", 1)[0].strip()

        return bare in self.models or bare.startswith("auto")

    def ensure_ready(self) -> None:
        if not _key():
            raise RuntimeError(
                "no Cursor API key is set — set it on the settings page. "
                "The SDK reads CURSOR_API_KEY; team admin keys are not accepted."
            )

    def run_step(
        self,
        *,
        system: str,
        prompt: str,
        tools: list[BoundTool],
        report: ReportTool,
        model: str,
        budget: Budget,
    ) -> StepResult:
        self.ensure_ready()
        from cursor_sdk import (
            AgentOptions,
            Client,
            CustomTool,
            LocalAgentOptions,
            ModelParameterValue,
            ModelSelection,
            SandboxOptions,
        )
        from cursor_sdk.errors import CursorAgentError
        captured: dict = {}
        spent = {"calls": 0}

        def execute(bound: BoundTool):
            def run(arguments, _context) -> str:
                if budget.tool_calls is not None and spent["calls"] >= budget.tool_calls:

                    return "stopped at the tool-call budget"
                spent["calls"] += 1
                try:

                    return bound(**dict(arguments or {}))
                except ToolRefused as refused:

                    return f"refused: {refused}"

            return run

        def report_execute(arguments, _context) -> str:
            payload = dict(arguments or {})
            refused = report.refusal(payload)
            if refused:

                return refused
            captured.clear()
            captured.update(payload)

            return "reported"

        custom = {
            tool.name: CustomTool(
                execute=execute(tool),
                description=tool.description,
                input_schema=tool.input_schema,
            )
            for tool in tools
        }
        custom[report.name] = CustomTool(
            execute=report_execute,
            description=report.description,
            input_schema=report.input_schema,
        )
        # An empty workspace: neither project root is inside it, so what
        # is left of the built-in toolset sees nothing of the migration. The
        # catalog is the only route to the projects and the containers, and
        # the sandbox is Cursor's own containment for the rest.
        with tempfile.TemporaryDirectory(prefix="cursor-step-") as cwd:
            # `Client.launch_bridge` and not the module-level `Agent.create`.
            # The default client launches a bridge and then connects to it as
            # a stranger, so it does not own it: registering custom tools
            # falls into connect mode, which starts a *second* callback
            # server in this process and repoints the bridge at it. The
            # agent kept answering `Tool not available` for every one of
            # them. An owned bridge registers the handlers on the server it
            # was launched pointing at.
            #
            # One bridge per step. The provider is a registry singleton
            # shared by every job, and a step already takes tens of seconds.
            client = Client.launch_bridge(workspace=cwd)
            try:
                agent = client.create_agent(
                    AgentOptions(
                        model=_selection(model, ModelSelection, ModelParameterValue),
                        api_key=_key(),
                        # `mode` is 'agent' or 'plan', and unset takes
                        # whatever the service defaults to. A step of this
                        # runner always wants the agent to act: `plan`
                        # answers in prose and calls nothing, which is what
                        # a step with no report looks like from here.
                        mode="agent",
                        # Named denials, not `tools=[]`. That reads like
                        # "offer no built-in tools" and also takes away the
                        # two the agent needs to call a custom tool at all.
                        # Measured on a one-tool probe: called without it,
                        # "Tool not available" with it.
                        disallowed_tools=DENIED_BUILTINS,
                        local=LocalAgentOptions(
                            cwd=Path(cwd),
                            custom_tools=custom,
                            sandbox_options=SandboxOptions(enabled=True),
                        ),
                    )
                )
                try:
                    # ``AgentOptions`` has no system field, so the standing
                    # instruction rides at the head of the first message.
                    # Dropping it would leave this provider's agent the only
                    # one that was never told how a step ends.
                    opening = f"{system}\n\n{prompt}" if system else (prompt or "")
                    result = agent.send(opening).wait()
                finally:
                    agent.close()
            except CursorAgentError as exc:

                return StepResult(ok=False, output=str(exc))
            finally:
                client.close()
        usage = getattr(result, "usage", None)
        tokens_in = int(getattr(usage, "input_tokens", 0) or 0)
        tokens_out = int(getattr(usage, "output_tokens", 0) or 0)
        tokens_cached = int(getattr(usage, "cache_read_tokens", 0) or 0)
        detail = _account(result, spent["calls"], sorted(custom), asked=model)
        status = str(getattr(result, "status", "") or "")
        # A run that did not complete is not "the model returned nothing
        # useful": the SDK knows why and puts it here. Reading only
        # `captured` reported the symptom and threw the cause away.
        if status and status not in ("completed", "finished", "success"):

            return StepResult(
                ok=False,
                output=f"the agent run ended {status}",
                detail=detail,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                tokens_cached=tokens_cached,
            )
        # Checked after the run, so a report the agent already made stands: the
        # tokens are spent either way, and throwing the answer away with them
        # sent the lap into the failure lane with nothing to show for it.
        if not captured and budget.tokens is not None and tokens_in + tokens_out > budget.tokens:

            return StepResult(
                ok=False,
                output="stopped at the token budget",
                detail=detail,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                tokens_cached=tokens_cached,
            )
        if not captured:

            return StepResult(
                ok=False,
                output=(
                    f"the agent finished without calling report "
                    f"({spent['calls']} tool call(s) made)"
                ),
                detail=detail,
                tokens_in=tokens_in,
                tokens_out=tokens_out,
                tokens_cached=tokens_cached,
            )
        flags = {
            flag
            for flag in report.input_schema["properties"]
            if flag != "answer" and captured.get(flag)
        }

        return StepResult(
            ok=True,
            flags=flags,
            answer=str(captured.get("answer") or ""),
            detail=detail,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            tokens_cached=tokens_cached,
        )


def _selection(model: str, selection_type, parameter_type):
    """``auto-smart?effort=high`` as the SDK wants it: an id and its params.

    The cartridge writes a model with its parameters in one string. Handed
    over as that string, the SDK made it the id — `auto-smart?effort=high`,
    no params — and the run reported `ModelSelection(id='default')`: the
    model the job recorded was not the model that ran.
    """
    name, _, query = model.partition("?")
    params = tuple(
        parameter_type(id=key, value=value)
        for key, value in parse_qsl(query, keep_blank_values=True)
    )

    return selection_type(id=name.strip(), params=params)


def _ran_model(result) -> str:
    chosen = getattr(result, "model", None)

    return str(getattr(chosen, "id", chosen) or "")


def _account(result, calls: int, offered: list[str], asked: str = "") -> str:
    """What the SDK reported, for the run log.

    `RunResult` carries `status`, the agent's own `result` text, the model
    and a duration. None of it was read, so a step that ended with no
    report looked identical whether the agent refused, crashed, or was
    never given a tool it could call.
    """
    text = str(getattr(result, "result", "") or "").strip()
    ran = _ran_model(result)
    wanted = asked.partition("?")[0].strip()
    rows = [
        f"status: {getattr(result, 'status', '')}",
        f"model: {ran or '(not reported)'}",
        "mode: agent",
        f"duration_ms: {getattr(result, 'duration_ms', '')}",
        f"tool calls made: {calls}",
        f"tools offered: {', '.join(offered)}",
        f"built-ins denied: {len(DENIED_BUILTINS)}",
        "",
        "agent said:",
        text or "(nothing)",
    ]
    if ran and wanted and ran != wanted:
        # Not a failure: the SDK may name a routed model differently. But the
        # job records the one it asked for, and a reader has to know.
        rows.insert(0, f"model mismatch: asked for {asked}, the run reports {ran}")

    return "\n".join(rows)


def _key() -> str:
    from odoo_migrator.settings import provider_setting

    return provider_setting("cursor", "api_key")
