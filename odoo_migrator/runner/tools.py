"""Tool catalog. A cartridge picks from this and cannot extend it.

``run_test`` is allowed on the old session as well as the new one: the stock
flow lists it while reading the module on the source container.

Every tool takes ``side`` and never a container name. The container comes
from the migration row, which is what confines the agent to two of them.
"""

from __future__ import annotations

import contextlib
import json
import time
from pathlib import Path

from odoo_migrator.sheller.errors import ShellerError, ShellerTimeout

# One `search([])` over a large table is the realistic way a step hangs. The
# daemon cannot cap it, so the client does, and the command is interrupted.
COMMAND_TIMEOUT = 300.0

TOOL_SIDES = {
    "exec": ("old", "new"),
    "source": ("old", "new"),
    "journal": ("old", "new"),
    "history": ("old", "new"),
    "run_test": ("old", "new"),
    "list_tests": ("old", "new"),
    "test_result": ("new",),
    "interrupt": ("old", "new"),
    "rollback": ("old", "new"),
    "reopen": ("new",),
    "read_file": ("old", "new"),
    "write_file": ("new",),
}


class ToolRefused(Exception):
    """The step asked a tool to touch a session or path it did not declare."""


# JSON schemas live with the catalog. A provider translates them; it does
# not own them. Name + schema + callable is what an out-of-process agent
# can be handed.
TOOL_SCHEMAS = {
    "exec": {
        "type": "object",
        "properties": {
            "side": {"type": "string"},
            "code": {"type": "string"},
        },
        "required": ["side", "code"],
    },
    # Sheller's `os_source`: a file or directory by `path`, or the code a
    # model's method runs by `model` + `method`. `module` picks one link of
    # the override chain.
    "source": {
        "type": "object",
        "properties": {
            "side": {"type": "string"},
            "path": {"type": "string"},
            "first": {"type": "integer"},
            "last": {"type": "integer"},
            "model": {"type": "string"},
            "method": {"type": "string"},
            "module": {"type": "string"},
        },
        "required": ["side"],
    },
    "read_file": {
        "type": "object",
        "properties": {"side": {"type": "string"}, "path": {"type": "string"}},
        "required": ["side", "path"],
    },
    "write_file": {
        "type": "object",
        "properties": {
            "side": {"type": "string"},
            "path": {"type": "string"},
            "content": {"type": "string"},
        },
        "required": ["side", "path", "content"],
    },
    "run_test": {
        "type": "object",
        "properties": {"side": {"type": "string"}, "test": {"type": "string"}},
        "required": ["side", "test"],
    },
    "list_tests": {
        "type": "object",
        "properties": {"side": {"type": "string"}, "module": {"type": "string"}},
        "required": ["side"],
    },
}
# What the model is told each tool does. One generic line for all of them
# left `source` to be guessed at, and a guess is what it got.
TOOL_DESCRIPTIONS = {
    "exec": "Run Python in the live Odoo shell on this side; `env` is bound.",
    "source": (
        "Read code on this side. `path='sale/models/sale_order.py'` (with "
        "optional `first`/`last` lines) reads a file, a directory lists it; "
        "`model='sale.order'` with `method='action_confirm'` returns the code "
        "that runs and its override chain, and `module=` picks one link."
    ),
    "journal": "The session's journal: every command it ran and what came back.",
    "history": "The session's command history.",
    "run_test": "Run one test spec: 'module.TestClass' or 'module.TestClass.test_method'.",
    "list_tests": "The test classes a module declares, each with a ready spec.",
    "test_result": "The report of the last run_test.",
    "interrupt": "Stop the command this side's session is running.",
    "rollback": "Discard everything this side's session holds uncommitted.",
    "reopen": "Open a fresh session on this side; do it after changing Python files.",
    "read_file": "Read a file, by a path relative to this side's project root.",
    "write_file": "Write a file, by a path relative to the new project's root.",
}
_SIDE_ONLY = {
    "type": "object",
    "properties": {"side": {"type": "string"}},
    "required": ["side"],
}


class BoundTool:
    """A catalog tool closed over one step and the sessions it declared."""

    def __init__(self, name: str, fn):
        self.name = name
        self._fn = fn
        self.input_schema = TOOL_SCHEMAS.get(name, _SIDE_ONLY)
        self.description = TOOL_DESCRIPTIONS.get(name, f"Run {name} on one side this step declared.")

    def __call__(self, **kwargs) -> str:
        return self._fn(**kwargs)


class ReportTool:
    """The tool the model calls to end the step.

    Its schema is the step's declared flags plus a free-text answer, so a
    step cannot raise a flag the cartridge did not list.
    """

    name = "report"
    description = "End the step with an answer and the flags that are true."

    def __init__(self, step):
        properties: dict = {"answer": {"type": "string"}}
        for flag in step.raises:
            properties[flag] = {"type": "boolean"}
        self.input_schema = {"type": "object", "properties": properties}
        self._step = step

    def __call__(self, **payload) -> str:
        return json.dumps(payload)


def report_tool(step) -> ReportTool:
    return ReportTool(step)


def tools_for(step, ctx) -> list[BoundTool]:
    """The concrete callables for the tools this step declared."""
    bound = []
    for name in step.tools:
        if name not in TOOL_SIDES:
            raise ToolRefused(f"unknown tool {name}")
        bound.append(BoundTool(name, _bind(name, step, ctx)))

    return bound


def _bind(name: str, step, ctx):
    def call(**kwargs) -> str:
        side = kwargs.get("side")
        if not isinstance(side, str):
            raise ToolRefused(f"{name} requires side")
        _require_side(step, name, side)
        started = time.perf_counter()
        # The session this command runs against, read before dispatch:
        # `reopen` replaces it, and a trace line recorded with the new id
        # would look for its body in a journal that never saw the command.
        ran_in = ctx.sessions.get(side, "")
        try:
            value = _dispatch(name, ctx, side, kwargs)
        except ToolRefused:
            raise
        # A binary file under the module is an answer to give the model, not
        # a crash of the step. Named, not ValueError: that would hand our own
        # bugs to the model as if they were its mistakes.
        except (ShellerError, OSError, UnicodeDecodeError) as exc:
            _record(ctx, name, side, ran_in, "", _elapsed(started), False, str(exc))

            return f"error: {exc}"
        command_id = ""
        if isinstance(value, dict) and value.get("id") is not None:
            command_id = str(value["id"])
        text = _text(value)
        _record(ctx, name, side, ran_in, command_id, _elapsed(started), True, text)

        return text

    return call


def _elapsed(started: float) -> int:

    return max(0, int((time.perf_counter() - started) * 1000))


def _record(
    ctx,
    name: str,
    side: str,
    session_id: str,
    command_id: str,
    duration_ms: int,
    ok: bool,
    preview: str,
):
    recorder = getattr(ctx, "record_tool", None)
    if not callable(recorder):

        return
    recorder(
        name,
        side,
        session_id,
        command_id,
        duration_ms,
        ok,
        preview[:120],
    )


def _require_side(step, name: str, side: str) -> None:
    if side not in TOOL_SIDES[name] or side not in step.sessions:
        raise ToolRefused(f"side {side!r} is not a session this step declared for {name}")


def _dispatch(name: str, ctx, side: str, kwargs: dict):
    session_id = ctx.sessions.get(side, "")
    write_key = ctx.write_keys.get(side, "")
    sheller = ctx.sheller
    if name == "exec":

        return _exec(ctx, session_id, write_key, kwargs.get("code", ""))
    if name == "source":
        target = {
            key: kwargs[key]
            for key in ("path", "first", "last", "model", "method", "module")
            if kwargs.get(key) not in (None, "")
        }

        return sheller.source(session_id, write_key=write_key, **target)
    if name == "journal":
        return sheller.journal(session_id)
    if name == "history":
        return sheller.feed(session_id)
    if name == "list_tests":
        return sheller.list_tests(
            session_id,
            module=kwargs.get("module") or ctx.module.technical_name,
            write_key=write_key,
        )
    if name == "run_test":
        result = sheller.run_test(
            session_id,
            test=kwargs.get("test", ""),
            write_key=write_key,
        )
        ctx.last_test = result if isinstance(result, dict) else {"raw": result}

        return ctx.last_test
    if name == "test_result":
        return ctx.last_test or {}
    if name == "interrupt":
        return sheller.interrupt(session_id, write_key=write_key)
    if name == "rollback":
        return sheller.rollback(session_id, write_key=write_key)
    if name == "reopen":
        return _reopen(ctx, side, session_id, write_key)
    if name == "read_file":
        return _read_file(ctx, side, kwargs.get("path", ""))
    if name == "write_file":
        return _write_file(ctx, side, kwargs.get("path", ""), kwargs.get("content", ""))

    raise ToolRefused(f"unknown tool {name}")


def _exec(ctx, session_id: str, write_key: str, code: str):
    """Run code, and do not let one query hold the step for ever.

    The daemon has no timeout for `exec`, so this one is on the HTTP call.
    When it expires the command is still running in the session, which is
    why the next thing to do is interrupt it — otherwise the session stays
    busy and every later call answers `session_busy`.
    """
    limit = getattr(ctx, "command_timeout", None) or COMMAND_TIMEOUT
    try:

        return ctx.sheller.exec(session_id, code, write_key=write_key, timeout=limit)
    except ShellerTimeout:
        with contextlib.suppress(ShellerError):
            ctx.sheller.interrupt(session_id, write_key=write_key)

        raise ShellerTimeout(
            f"the command ran longer than {limit:.0f}s and was interrupted"
        ) from None


def _reopen(ctx, side: str, session_id: str, write_key: str) -> str:
    """Close this side's session and open a fresh one on the same target.

    The target comes from `ctx.session_targets`, not from the daemon: its
    `describe()` answers container, database and owner but never `odoo_bin`,
    and `open_session` refuses without it.
    """
    del session_id, write_key
    from odoo_migrator.runner.sessions import reopen_side

    if not ctx.session_targets.get(side):
        raise ToolRefused(f"no target recorded for side {side!r}")

    return reopen_side(ctx, side)


def _project_root(ctx, side: str) -> Path:
    migration = ctx.migration
    raw = migration.old_project_path if side == "old" else migration.new_project_path

    # `~` as typed on the card. `module_paths` expands it, so without this
    # the prompt named one directory and the tools wrote under `./~`.
    return Path(raw).expanduser().resolve()


def _inside(ctx, side: str, relative: str) -> Path:
    """Resolve ``relative`` under the side's project root, or refuse it.

    A path that escapes is a refusal, not a clamp: the agent does not get
    a rewritten path that still writes somewhere.
    """
    root = _project_root(ctx, side)
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ToolRefused(f"path escapes the project root: {relative}")

    return candidate


def _read_file(ctx, side: str, relative: str) -> str:
    path = _inside(ctx, side, relative)
    if not path.is_file():
        raise ToolRefused(f"not a file: {relative}")

    return path.read_text(encoding="utf-8")


def _write_file(ctx, side: str, relative: str, content: str) -> str:
    path = _inside(ctx, side, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    return str(path)


def _text(value) -> str:
    if isinstance(value, str):
        return value

    return json.dumps(value, default=str)
