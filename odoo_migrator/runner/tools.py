"""Tool catalog. A cartridge picks from this and cannot extend it.

``run_test`` is allowed on the old session as well as the new one: the stock
flow lists it while reading the module on the source container.

Every tool takes ``side`` and never a container name. The container comes
from the migration row, which is what confines the agent to two of them.
"""

from __future__ import annotations

import json
from pathlib import Path

from odoo_migrator.sheller.errors import ShellerError

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


class BoundTool:
    """A catalog tool closed over one step and the sessions it declared."""

    def __init__(self, name: str, fn):
        self.name = name
        self._fn = fn

    def __call__(self, **kwargs) -> str:
        return self._fn(**kwargs)


class ReportTool:
    """The tool the model calls to end the step.

    Its schema is the step's declared flags plus a free-text answer, so a
    step cannot raise a flag the cartridge did not list.
    """

    name = "report"

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
        try:
            return _text(_dispatch(name, ctx, side, kwargs))
        except ToolRefused:
            raise
        except (ShellerError, OSError) as exc:
            return f"error: {exc}"

    return call


def _require_side(step, name: str, side: str) -> None:
    if side not in TOOL_SIDES[name] or side not in step.sessions:
        raise ToolRefused(f"side {side!r} is not a session this step declared for {name}")


def _dispatch(name: str, ctx, side: str, kwargs: dict):
    session_id = ctx.sessions.get(side, "")
    write_key = ctx.write_keys.get(side, "")
    sheller = ctx.sheller
    if name == "exec":
        return sheller.exec(session_id, kwargs.get("code", ""), write_key=write_key)
    if name == "source":
        return sheller.source(session_id, dotted=kwargs.get("dotted", ""), write_key=write_key)
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


def _reopen(ctx, side: str, session_id: str, write_key: str) -> str:
    described = ctx.sheller.get_session(session_id)
    ctx.sheller.close(session_id, write_key=write_key)
    opened = ctx.sheller.open_session(
        container=described["container"],
        database=described["database"],
        odoo_bin=described["odoo_bin"],
        owner=described.get("owner") or {"kind": "agent"},
        allow_commit=side == "new",
    )
    ctx.sessions[side] = opened["id"]
    ctx.write_keys[side] = opened["write_key"]

    return opened["id"]


def _project_root(ctx, side: str) -> Path:
    migration = ctx.migration
    raw = migration.old_project_path if side == "old" else migration.new_project_path

    return Path(raw).resolve()


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
