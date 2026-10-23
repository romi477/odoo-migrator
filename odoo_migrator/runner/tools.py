"""Tool catalog. A cartridge picks from this and cannot extend it.

``run_test`` is allowed on the source session as well as the target one: the stock
flow lists it while reading the module on the source container.

Every tool takes ``side`` and never a container name. The container comes
from the migration row, which is what confines the agent to two of them.
"""

from __future__ import annotations

import ast
import contextlib
import json
import re
import time
from pathlib import Path

from odoo_migrator.sheller.errors import ShellerError, ShellerTimeout

# One `search([])` over a large table is the realistic way a step hangs. The
# daemon cannot cap it, so the client does, and the command is interrupted.
COMMAND_TIMEOUT = 300.0
# How much of a file `read_file` hands back at once: the module source the
# prompt carries is clipped at the same size. A data file of megabytes went
# into the conversation whole, and spent the step's budget in one answer.
READ_FILE_CHARS = 120_000
# A whole module's run, as Sheller's own tool times one. A class or a method
# keeps the daemon's default.
MODULE_TEST_TIMEOUT = 1800.0

TOOL_SIDES = {
    "exec": ("source", "target"),
    "source": ("source", "target"),
    "journal": ("source", "target"),
    "history": ("source", "target"),
    "run_test": ("source", "target"),
    "list_tests": ("source", "target"),
    "test_result": ("target",),
    "interrupt": ("source", "target"),
    "rollback": ("source", "target"),
    "reopen": ("source", "target"),
    "read_file": ("source", "target"),
    "write_file": ("target",),
    # The one write the source side allows. A tool of its own, because
    # `write_file` on the source would let `port` and `rewrite`, which open
    # both sides and declare it, write the project they are porting from.
    "write_source_test": ("source",),
}


# Flags that cannot both be true in one report. The diagnosis picks one
# repair; raising both ran the whole failure lane, and `rewrite` — which
# starts from an empty module directory — deleted the fix `patch` had just
# written, in the same lap and at the cost of two agent steps.
EXCLUSIVE_FLAGS = (("small_fix", "start_over"),)


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
        "properties": {
            "side": {"type": "string"},
            "path": {"type": "string"},
            "first": {"type": "integer"},
            "last": {"type": "integer"},
        },
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
        "properties": {
            "side": {"type": "string"},
            "test": {"type": "string"},
            "params": {"type": "string"},
        },
        "required": ["side", "test"],
    },
    "list_tests": {
        "type": "object",
        "properties": {"side": {"type": "string"}, "module": {"type": "string"}},
        "required": ["side"],
    },
}
TOOL_SCHEMAS["write_source_test"] = TOOL_SCHEMAS["write_file"]
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
    "run_test": (
        "Run tests: 'module' (its standard tests), 'module.TestClass' or "
        "'module.TestClass.test_method'. `failed` lists each failing test as "
        "a spec to run again on its own, which answers with its log. A "
        "module's browser (Hoot) tests run as 'web.WebSuite.test_unit_desktop' "
        "with `params='@module'`, Hoot's filter; `list_tests` has the exact "
        "call. A suite Odoo skipped answers `js_tests_skipped`: it did not pass."
    ),
    "list_tests": (
        "The test classes a module declares, each with a ready spec. Under `js`, "
        "its browser test files, each `in_manifest` when the test bundle loads it, "
        "and `run`, the call that runs them."
    ),
    "test_result": "The report of the last run_test.",
    "interrupt": "Stop the command this side's session is running.",
    "rollback": "Discard everything this side's session holds uncommitted.",
    "reopen": "Open a fresh session on this side; do it after changing Python files.",
    "read_file": (
        "Read a file on the host, by its path inside this side's container "
        "(as a traceback names it). A path relative to the project root works "
        "where no container path was given. `first`/`last` (1-based lines) "
        "read a part; a long file comes back clipped."
    ),
    "write_file": (
        "Write a file inside the module's directory, by its path inside the "
        "target container. A path relative to the target project's root works where "
        "no container path was given."
    ),
    "write_source_test": (
        "Write a characterization test for a module that has no tests, into "
        "`tests/` of the module on the source side. Nothing else on the source "
        "side can be written, and a module that already has tests refuses. "
        "Reopen the source session after writing, then run the test there."
    ),
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

    def refusal(self, payload: dict) -> str:
        """Why this report cannot be accepted, in words for the model, or "".

        A refused report does not end the step: the model is told what is
        wrong with it and reports again. The alternative is the runner
        choosing for it, which hides that the diagnosis contradicted itself.
        """
        for pair in EXCLUSIVE_FLAGS:
            raised = [flag for flag in pair if payload.get(flag)]
            if len(raised) > 1:

                return (
                    f"{' and '.join(raised)} cannot both be true: they ask for "
                    "different repairs, and running both undoes the first. "
                    "Report again with the one you mean."
                )

        return ""

    def __call__(self, **payload) -> str:
        refused = self.refusal(payload)
        if refused:

            return refused

        return json.dumps(payload)


def report_tool(step) -> ReportTool:
    return ReportTool(step)


# Tools this process answers itself, on the host's disk. The daemon never
# hears of them, so they belong to no Sheller session.
HOST_TOOLS = frozenset({"read_file", "write_file", "write_source_test"})


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
        # The host's own file tools run in no session: stamping one on them
        # made a run that read eight files look like eight daemon calls.
        ran_in = "" if name in HOST_TOOLS else ctx.sessions.get(side, "")
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
        _note_source_run(ctx, name, side, value, kwargs.get("test", ""))
        command_id = ""
        if isinstance(value, dict) and value.get("id") is not None:
            command_id = str(value["id"])
        text = _text(value)
        # What the line says, which is not what the model is handed. A test
        # run's answer begins with empty stdout and a null result, so the
        # first 120 characters of it said nothing about the test.
        _record(
            ctx, name, side, ran_in, command_id, _elapsed(started), True,
            _preview(name, value, text),
        )

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
        declared = ", ".join(repr(one) for one in step.sessions) or "none"
        raise ToolRefused(
            f"side {side!r} is not a session this step declared for {name}; "
            f"it declared: {declared}. The sides are 'source' (the version the "
            "module is ported from) and 'target' (the version it is ported to)."
        )


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
        module = kwargs.get("module") or ctx.module.technical_name
        try:
            return sheller.list_tests(session_id, module=module, write_key=write_key)
        except ShellerError as exc:
            if not _module_missing(exc):
                raise

            # Early in a port the target has no directory for the module yet:
            # an answer to read, not a failed call.
            return {
                "module": module,
                "classes": [],
                "not_on_this_side": True,
                "note": (
                    f"{module} is not on this container yet: it has no directory there. "
                    "Write its files first, then list its tests again."
                ),
            }
    if name == "run_test":
        result = _run_test(
            ctx, session_id, write_key, kwargs.get("test", ""), kwargs.get("params") or ""
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
        return _read_file(
            ctx, side, kwargs.get("path", ""), kwargs.get("first"), kwargs.get("last")
        )
    if name == "write_file":
        return _write_file(ctx, side, kwargs.get("path", ""), kwargs.get("content", ""))
    if name == "write_source_test":

        return _write_source_test(ctx, side, kwargs.get("path", ""), kwargs.get("content", ""))

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


def _run_test(ctx, session_id: str, write_key: str, test: str, params: str = ""):
    """A run past the call is interrupted, as an `exec` is: left running, it
    kept the session busy, and the agent's next command on that side
    answered `session_busy`.

    A whole module answers with the count of its log lines, not the lines:
    thousands of them, in the conversation, for an answer `failed` already
    gives. They stay in the journal, as Sheller's own tool leaves them.
    """
    whole = "." not in test
    # A browser suite is a whole module's tests too, behind one Python test
    # and a filter: at the daemon's default it was cut off at 300s, while the
    # tests step gives the same run half an hour.
    extra = {"timeout": MODULE_TEST_TIMEOUT} if whole or params else {}
    if params:
        extra["params"] = params
    try:
        report = ctx.sheller.run_test(session_id, test=test, write_key=write_key, **extra)
    except ShellerTimeout:
        with contextlib.suppress(ShellerError):
            ctx.sheller.interrupt(session_id, write_key=write_key)

        raise ShellerTimeout(
            f"the test {test} outlived the call and was interrupted"
        ) from None
    if whole and isinstance(report, dict) and "stderr" in report:
        report = dict(report)
        report["stderr_lines"] = len(report.pop("stderr") or [])

    return report


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
    raw = (migration.old_project_path if side == "source" else migration.new_project_path) or ""
    if not raw.strip():
        # `Path("")` is the server's working directory, which is no project.
        raise ToolRefused(f"this migration names no {side} project directory")

    # `~` as typed on the card. `module_paths` expands it, so without this
    # the prompt named one directory and the tools wrote under `./~`.
    return Path(raw.strip()).expanduser().resolve()


def _inside(ctx, side: str, given: str) -> Path:
    """An absolute path is a container path; a relative one is under the
    project directory, as before mounts were known."""
    if given.startswith("/"):

        return _inside_mounts(ctx, side, given)

    return _inside_project(ctx, side, given)


def _inside_mounts(ctx, side: str, given: str) -> Path:
    """Translate a container path to the host, inside an allowed root, or refuse.

    Resolved before the check, so `..` and a symlink cannot climb out of the
    root they appear to be under.
    """
    from odoo_migrator.module_paths import allowed_roots
    from odoo_migrator.mounts import covering, to_host

    roots = allowed_roots(ctx.migration, side)
    if not roots:
        raise ToolRefused(
            f"no addons directories are known on the {side} side, so {given} "
            "cannot be read from disk. Read it with `source` from the container."
        )
    mount = covering(roots, given)
    if mount is None:
        listed = ", ".join(root["container"] for root in roots)
        raise ToolRefused(
            f"{given} is outside the addons directories of the {side} side "
            f"({listed}). Read it with `source` from the container."
        )
    root = Path(mount["host"]).expanduser().resolve()
    candidate = to_host([mount], given).resolve()
    if not candidate.is_relative_to(root):
        raise ToolRefused(f"path escapes its addons directory: {given}")

    return candidate


def _inside_project(ctx, side: str, relative: str) -> Path:
    """Resolve ``relative`` under the side's project root, or refuse it.

    A path that escapes is a refusal, not a clamp: the agent does not get
    a rewritten path that still writes somewhere.
    """
    root = _project_root(ctx, side)
    candidate = (root / relative).resolve()
    if not candidate.is_relative_to(root):
        raise ToolRefused(f"path escapes the project root: {relative}")

    return candidate


def _read_file(
    ctx, side: str, relative: str, first: int | None = None, last: int | None = None
) -> str:
    """The file, or lines `first` to `last` of it, clipped to READ_FILE_CHARS."""
    path = _inside(ctx, side, relative)
    if not path.is_file():
        raise ToolRefused(f"not a file: {relative}")
    text = path.read_text(encoding="utf-8")
    start = (_line_number(first, "first") or 1) - 1
    end = _line_number(last, "last")
    if start or end is not None:
        text = "".join(text.splitlines(keepends=True)[start:end])
    if len(text) <= READ_FILE_CHARS:

        return text
    shown = text[:READ_FILE_CHARS]
    reached = start + shown.count("\n")

    return (
        f"{shown}\n\n(clipped at {READ_FILE_CHARS} of {len(text)} characters, after "
        f"line {reached}; read on with first={reached + 1} and last)"
    )


def _line_number(value, name: str) -> int | None:
    if value in (None, ""):

        return None
    try:

        return max(int(value), 1)
    except (TypeError, ValueError):
        raise ToolRefused(f"{name} is a line number, not {value!r}") from None


def _inside_module(ctx, path: Path, relative: str) -> None:
    """A write lands in the module's own directory, or nowhere.

    The project root is not tight enough. A project that keeps its addons in
    `project/` has the module at `project/<name>`, and a model that took the
    module's technical name for its path wrote a whole port to `<name>/` —
    beside the addons directory, not in it. The container never saw a line of
    it, `upgrade` and `tests` ran on what was already installed, and the lap
    passed without touching anything the port had written.
    """
    from odoo_migrator.module_paths import (
        agent_path,
        allowed_roots,
        host_paths,
        module_root,
    )

    # The directory recorded on the module, and the one the migration's
    # current root implies. They are the same in a live run; they differ when
    # the root was changed after the tree was read, and a write that is right
    # by either of them is not the mistake this guards against.
    # With the target side's mounts known, the placed directory is the only
    # answer: the project-root guess is where a port lands and is never
    # loaded. With none placed, nothing is writable.
    mounted = bool(allowed_roots(ctx.migration, "target"))
    allowed = [module_root(ctx.migration, ctx.module, "target")]
    if not mounted:
        allowed.append(host_paths(ctx.migration, ctx.module.technical_name)[1] or None)
    roots = [Path(one).expanduser().resolve() for one in allowed if one]
    if not roots:
        # Mounted or not: with no directory to hold the write to, the write
        # used to go wherever the path said.
        raise ToolRefused(
            f"{relative}: the app does not know where this module is on the "
            "target side, so nothing is written for it."
        )
    if any(path.is_relative_to(one) for one in roots):

        return
    wanted = agent_path(ctx.migration, ctx.module, "target")
    raise ToolRefused(
        f"{relative} is outside the module's directory. This module is "
        f"{wanted} on the target side: write {wanted}/{Path(relative).name}, "
        "and everything else under that directory."
    )


def _write_source_test(ctx, side: str, relative: str, content: str) -> str:
    """The one write the source side allows: a new test in the module's tests/.

    Refused when the module already had a test when the step began, so the
    prompt is not the only thing between the model and a test someone wrote.
    """
    from odoo_migrator.module_paths import module_root
    from odoo_migrator.source_git import SourceGitError, ensure_branch, mark_unproven

    root = module_root(ctx.migration, ctx.module, "source")
    if root is None:
        raise ToolRefused(
            "the app does not know where this module is on the source side, "
            "so nothing is written for it."
        )
    root = Path(root).expanduser().resolve()
    path = _inside(ctx, side, relative)
    if not path.is_relative_to(root / "tests"):
        raise ToolRefused(
            f"{relative} is outside the module's tests/ directory. A "
            f"characterization test goes in {ctx.module.technical_name}/tests/ and "
            "nowhere else on the source side."
        )
    if ctx.source_tests_had is None:
        ctx.source_tests_had = _has_tests(root)
    if ctx.source_tests_had:
        raise ToolRefused(
            f"{ctx.module.technical_name} already has tests on the source side "
            "(a tests/ directory or a test_*.py file). Nothing is written next to them."
        )
    try:
        ensure_branch(ctx.migration)
    except SourceGitError as exc:
        raise ToolRefused(str(exc)) from None
    # Before the file exists: a start of another module in the batch adds
    # what the baseline missed, and must not take this for a person's test.
    mark_unproven(root / "tests")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    ctx.source_tests_written = True
    ctx.source_test_methods[str(path)] = len(_TEST_METHOD.findall(content))
    flaws = _test_flaws(content)
    ctx.source_test_flaws[str(path)] = flaws
    ctx.source_green = False
    ctx.source_green_session = ""
    ctx.source_green_command = ""
    if flaws:
        # A run cannot show this: a test that asserts nothing passes.
        return f"{path}\nnote: {'; '.join(flaws)}. Fix it before you run it; it cannot count as proved."

    return str(path)


def _has_tests(root: Path) -> bool:
    if (root / "tests").exists():

        return True

    return any("__pycache__" not in found.parts for found in root.rglob("test_*.py"))


def _test_flaws(content: str) -> list[str]:
    """What is wrong with a test file that a green run would not show.

    A test method with no assertion passes whatever the module does, and a
    file that does not parse is no test at all. Read from the syntax tree, not
    with a pattern: a name that merely contains "test_" in a string is not a
    test.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError as exc:

        return [f"the file does not parse (line {exc.lineno})"]
    bare = [
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name.startswith("test_")
        and not _asserts(node)
    ]

    return [f"{name} asserts nothing" for name in bare]


def _asserts(function) -> bool:
    """An `assert`, or a call whose name says it asserts (`assertEqual`,
    `assertRaises`, a helper called `_assert_total`)."""
    for node in ast.walk(function):
        if isinstance(node, ast.Assert):

            return True
        if isinstance(node, ast.Call):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
            if "assert" in name.lower():

                return True

    return False


_TEST_METHOD = re.compile(r"^[ \t]*def test_\w*\(", re.MULTILINE)


def _note_source_run(ctx, name: str, side: str, value, spec: str = "") -> None:
    """Whether the tests this step wrote are green, from the run that proves it.

    A run counts when it was on the source side, after a write, of this
    module, and it ran at least as many tests as the step wrote and none
    failed. Any other run on the source says nothing about them: another
    module passing used to mark them proved, and so did a rerun of the one
    method that had just been fixed. A later write clears it again.
    """
    if name != "run_test" or side != "source" or not ctx.source_tests_written:

        return
    module = ctx.module.technical_name
    ours = spec == module or spec.startswith(f"{module}.")
    wanted = sum(ctx.source_test_methods.values())
    outcome = value.get("test") if isinstance(value, dict) else None
    ctx.source_green = bool(
        ours
        and wanted > 0
        and not any(ctx.source_test_flaws.values())
        and isinstance(outcome, dict)
        and not value.get("error")
        and outcome.get("success")
        and (outcome.get("tests_run") or 0) >= wanted
    )
    ran = value.get("id") if isinstance(value, dict) else None
    ctx.source_green_session = ctx.sessions.get("source", "") if ctx.source_green else ""
    ctx.source_green_command = str(ran) if ctx.source_green and ran is not None else ""


def _write_file(ctx, side: str, relative: str, content: str) -> str:
    path = _inside(ctx, side, relative)
    _inside_module(ctx, path, relative)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")

    return str(path)


def _preview(name: str, value, text: str) -> str:
    """One line about what came back, for the run log and the summary.

    The raw answer is kept in the journal and is what the model reads. This
    is the line a person scans, so for the two tools whose answer is a report
    it is the report's verdict rather than the head of its JSON.
    """
    if not isinstance(value, dict):

        return text
    if name == "run_test":
        outcome = value.get("test")
        hoot = value.get("hoot") if isinstance(value.get("hoot"), dict) else {}
        if value.get("error"):
            if hoot.get("skipped"):
                # Odoo skips a browser test it cannot run and counts the skip
                # as a pass; the line says what it is.
                return (
                    f"error: {value['error']} ({hoot.get('reason') or 'no reason given'}): "
                    "the browser suite did not run"
                )

            return f"error: {value['error']}"
        if isinstance(outcome, dict):
            ran = outcome.get("tests_run") or 0
            verdict = "passed" if outcome.get("success") and ran else "failed"

            failed = [
                str(entry.get("test"))
                for entry in value.get("failed") or []
                if isinstance(entry, dict) and entry.get("test")
            ]
            named = f" · failed: {', '.join(failed)}" if failed else ""
            counted = (
                f" · hoot {hoot.get('passed') or 0} passed, {hoot.get('failed') or 0} failed"
                if "passed" in hoot or "failed" in hoot
                else ""
            )

            return (
                f"{verdict}: {ran} test(s), "
                f"{outcome.get('failures') or 0} failure(s), "
                f"{outcome.get('errors') or 0} error(s){counted}{named}"
            )
    if name == "list_tests":
        if isinstance(value, dict) and value.get("not_on_this_side"):

            return f"{value.get('module')} is not on this container yet"
        classes = value.get("classes")
        if isinstance(classes, list):
            named = [
                item.get("spec") or item.get("name")
                for item in classes
                if isinstance(item, dict)
            ]
            js = value.get("js") if isinstance(value.get("js"), dict) else {}
            files = [entry for entry in js.get("files") or [] if isinstance(entry, dict)]
            # A module with browser tests only said "no tests on disk".
            browser = f" · {len(files)} JS test file(s)" if files else ""
            if not classes:

                return f"no Python test classes{browser}" if files else "no tests on disk"

            return f"{len(classes)} class(es): {', '.join(filter(None, named))}{browser}"

    return text


def _module_missing(exc: ShellerError) -> bool:

    return exc.status == 404 and "module_not_found" in f"{exc.detail} {exc}"


def _text(value) -> str:
    if isinstance(value, str):
        return value

    return json.dumps(value, default=str)
