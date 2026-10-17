# Characterization tests on the source side — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An optional flow step writes a test into a test-less source module, runs it green on the source container, and lets the port carry it to the target; the two sides are named `source` and `target` everywhere an agent sees them.

**Architecture:** Five tasks, one commit each at least. (1) rename the side vocabulary, (2) a side legend the runner renders into every agent prompt, (3) a `write_source_test` tool with its guards and a green tracker on the step context, (4) `source_git.py`: the migration's branch in the source project and the commit-or-delete that settles a step, (5) wire it into the runner, the stock flow and the docs.

**Tech Stack:** Python, FastAPI, Peewee, pytest, ruff (`ruff check --fix`, never `ruff format`), git via subprocess.

**Spec:** `docs_/superpowers/specs/2026-10-17-characterization-tests-design.md`

## Global Constraints

- Code, comments, docs and commits in English.
- `return` gets a blank line before it. Run `ruff check --fix`, never `ruff format`.
- Inline `style=` only for custom properties. No new template or CSS is planned; if one appears, it gets a row on `/components`.
- The suite passes with no Docker, no Odoo, no daemon. A test double answers the way the real thing does (`FakeShellerClient` for the daemon; a real `git` in a `tmp_path` for git, as `tests/test_target_git.py` does).
- Never read `admin.key`, never touch `~/.odoo-sheller/`, never call the daemon's admin endpoints. Never stop a daemon on 8765.
- `read_file` and `write_file` stay under the configured project roots; a path that escapes is refused, not clamped. `write_source_test` is held tighter still: only `<module>/tests/` of the source module.
- No aliases and no compatibility for the old side words (spec, "Vocabulary"). Database columns and form fields named `old_*` / `new_*` keep their names.
- Before any `.py` edit in the user's checkout, check the job table read-only (`sqlite3.connect(f'file:{user_dir()}/migrator.sqlite?mode=ro', uri=True)`, statuses `queued`, `running`, `waiting_for_human`). The user's server on 8000 runs without `--reload`; never restart it.
- Commits end with `Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>`. Never push.
- One review gate at a time: commit the task, stop for review if the user asks for it.

## Review Focus

- A module that already has tests, with a model that raises `needs_tests` anyway: nothing is written and the source tree is untouched (Task 3 pins the guard in the tool, not in the prompt).
- A source project that is not a git repository: the write is refused with a reason and the lap carries on as the stock flow does today (Task 4, Task 5).
- A test that never goes green, or a step that is stopped or fails midway: `tests/` is deleted and nothing is committed; a red test must not reach the port (Task 4, Task 5).
- `port` and `rewrite` declare `sessions: [source, target]` and `write_file`: they must still be unable to write the source (Task 3).
- The source checkout carries uncommitted work of the person: the branch switch keeps it and the commit never includes it (Task 4).

---

### Task 1: `source` and `target` instead of `old` and `new`

**Files:**
- Modify: `odoo_migrator/runner/cartridge.py` (`SIDES`), `odoo_migrator/runner/tools.py` (`TOOL_SIDES`, `_require_side`, `_dispatch`, the host-path helpers), `odoo_migrator/runner/native.py`, `odoo_migrator/runner/sessions.py`, `odoo_migrator/runner/prompting.py`, `odoo_migrator/runner/steps/{upgrade,tests,lint}.py`
- Modify: `odoo_migrator/module_paths.py`, `odoo_migrator/port_guard.py`, `odoo_migrator/discard.py`, `odoo_migrator/target_git.py`, `odoo_migrator/jobs.py`, `odoo_migrator/graph.py`, `odoo_migrator/migrations.py` (`save_side_container`), `odoo_migrator/contract.py`, `odoo_migrator/api.py`, `odoo_migrator/web/templates/migration_settings.html`
- Modify: `cartridges/stock.yaml`, `prompts/sheller.md`, `docs/runner-design.md` (the `source(side='old', ...)` example and the prose that names a side), `tests/**` that name a side
- Test: `tests/test_sides.py` (new)

**Interfaces:**
- Produces: a side is the string `"source"` or `"target"` in every `side` parameter, `ctx.sessions`, `ctx.write_keys`, `ctx.session_targets`, `TOOL_SIDES`, `SIDES`, the `sessions:` field of a flow and the tool's `side` argument. `module_root(migration, module, side)`, `allowed_roots(migration, side)`, `agent_path(migration, module, side)`, `relative_to_root(migration, module, side)` take those two words. Columns (`Migration.old_project_path`, `Module.old_path`, ...) are unchanged.
- Produces: `_require_side` refuses with
  `side 'X' is not a session this step declared for TOOL; it declared: 'a', 'b'. The sides are 'source' (the version the module is ported from) and 'target' (the version it is ported to).`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sides.py`:

```python
"""A side is `source` or `target`. An agent must not have to guess which is which."""

import re
from pathlib import Path

import pytest

from odoo_migrator.runner.cartridge import SIDES, CartridgeError, Step, load_cartridge
from odoo_migrator.runner.tools import TOOL_SIDES, ToolRefused, _require_side

STOCK = Path("cartridges/stock.yaml").read_text(encoding="utf-8")


def test_the_sides_are_source_and_target():
    assert SIDES == frozenset({"source", "target"})
    assert {side for sides in TOOL_SIDES.values() for side in sides} == {"source", "target"}


def test_a_flow_that_says_old_is_refused_and_told_the_word_to_use():
    text = STOCK.replace("sessions: [source, target]", "sessions: [old, target]", 1)

    with pytest.raises(CartridgeError, match=r"unknown session 'old'.*'source' and 'target'"):
        load_cartridge(text)


def test_the_stock_flow_and_the_shell_notes_never_name_a_side_old_or_new():
    for name in ("cartridges/stock.yaml", "prompts/sheller.md"):
        text = Path(name).read_text(encoding="utf-8")

        assert not re.search(r"side\s*=\s*['\"](old|new)['\"]", text), name
        assert not re.search(r"sessions:\s*\[[^\]]*\b(old|new)\b", text), name


def test_a_side_the_step_did_not_declare_names_the_declared_ones():
    step = Step(step="agent", id="x", sessions=["source"], tools=["exec"], prompt="p")

    with pytest.raises(ToolRefused, match=r"declared: 'source'.*'source' \(the version"):
        _require_side(step, "exec", "target")
```

- [ ] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_sides.py -q`
Expected: 4 FAILED — `SIDES` is still `{"old", "new"}`, the `old` flow loads, `stock.yaml` still says `side='old'`, the refusal names no declared sides.

- [ ] **Step 3: Rename the vocabulary**

Work from the grep, not from memory — every line below is a side value, not a column:

```bash
grep -rnI "\"old\"\|\"new\"\|'old'\|'new'" odoo_migrator prompts cartridges | grep -v web/static
grep -rnI "provision\.old\|provision\.new\|\.old\b\|\.new\b" odoo_migrator/web/templates
```

Edit each hit so the word is `"source"` / `"target"`. The shape of the change, file by file:

- `runner/cartridge.py`: `SIDES = frozenset({"source", "target"})`. In `_refuse_invalid` the unknown-session error becomes `f"unknown session '{side}'; the sides are 'source' and 'target'"`.
- `runner/tools.py`: every `("old", "new")` in `TOOL_SIDES` becomes `("source", "target")`; `("new",)` becomes `("target",)`. `_project_root` and the other `side == "old"` tests become `side == "source"`. `_inside_module` and `_write_file` use `"target"` where they used `"new"`, and their user-facing strings say "target side". `_require_side`:

```python
def _require_side(step, name: str, side: str) -> None:
    if side not in TOOL_SIDES[name] or side not in step.sessions:
        declared = ", ".join(repr(one) for one in step.sessions) or "none"
        raise ToolRefused(
            f"side {side!r} is not a session this step declared for {name}; "
            f"it declared: {declared}. The sides are 'source' (the version the "
            "module is ported from) and 'target' (the version it is ported to)."
        )
```

- `runner/sessions.py:33`: `allow_commit=side == "target"`.
- `runner/native.py`: `session_targets["target"]` at the four places; `_targets` iterates `("source", migration.old_container, migration.old_database)` and `("target", migration.new_container, migration.new_database)`; the loop at `~997` iterates `("source", "target")`; `_module_root` uses `"target"`. The words "old container"/"new container" in its error strings become "source container"/"target container".
- `runner/steps/*.py`, `port_guard.py`, `discard.py`, `target_git.py`, `jobs.py`: `module_root(..., "new")` becomes `module_root(..., "target")`; `ctx.sessions["new"]` etc. become `["target"]`.
- `runner/prompting.py`: `_old_root` calls `module_root(..., "source")`; `agent_path(..., "source" / "target")` in `system_prompt` (Task 2 rewrites that block; here only the arguments change).
- `module_paths.py`: `side == "old"` becomes `side == "source"` in `module_root`, `relative_to_root`, the container-path column choice (`:300`), and `allowed_roots` iterates `("source", "target")` and returns `{"source": [], "target": []}` where it returned `{"old": [], "new": []}`.
- `graph.py:943-951`: `containers = {"source": migration.old_container, "target": migration.new_container}`; `snapshot["source"]`, `snapshot["target"]`.
- `contract.py:185`: `"containers": {"source": migration.old_container, "target": migration.new_container}`.
- `migrations.py` `save_side_container`: accepts `"source"` / `"target"`.
- `api.py`: `save_side_container(migration["id"], "source", ...)`; `describe_side` maps keyed `"source"` / `"target"` at `~1430-1439` and `~1507`; the route handler at `~1647` tests `side not in ("source", "target")` and `side == "source"`.
- `web/templates/migration_settings.html`: the loop becomes `[("source", "source", provision.source), ("target", "target", provision.target)]` (the second element was the label; keep the labels), `side == "source"` in the two conditions, and the form action `/containers/{{ side }}/{{ action }}` now carries the new word. Search `web/templates` and `web/static/*.js` for `/containers/old` and `/containers/new` and any `provision.old`.
- `cartridges/stock.yaml`: every `sessions: [old]` / `[new]` / `[old, new]` becomes the new word; `source(side='old', path=...)` becomes `source(side='source', path=...)`. `prompts/sheller.md` lines 88-92 likewise (`side="old"` -> `"source"`, `"new"` -> `"target"`).
- `docs/runner-design.md`: the `source(side='old', ...)` example (line ~410) and prose about "the old container" in the decisions list; the override list gets its item 6 in Task 5, not here.

Then update the tests. The same patterns are in about forty files; the ones that name a side are the hits of:

```bash
grep -rnlI "\"old\"\|\"new\"\|'old'\|'new'\|\[old\|old, new\|side=old\|side=new" tests
```

Change a side to its new word; leave `old_project_path=`, `old_container=`, `old_database=`, `old_path=` and the other column names alone. YAML in tests (`tests/cartridges/*.yaml` and inline strings) changes `sessions: [old]` the same way.

- [ ] **Step 4: Run the new tests, then the suite**

Run: `uv run pytest tests/test_sides.py -q`
Expected: 4 passed.

Run: `uv run pytest -q -x 2>&1 | tail -20`
Expected: every failure is a test or a source line still saying a side in the old words. Fix each, re-run, until `... passed`. Do not weaken a test to get there.

- [ ] **Step 5: Prove nothing in the code still names a side the old way**

Run:
```bash
grep -rnI "\"old\"\|\"new\"\|'old'\|'new'" odoo_migrator prompts cartridges | grep -v web/static
ruff check --fix odoo_migrator tests
```
Expected: the grep prints nothing; ruff prints no errors (accept the import-order fixes it makes).

- [ ] **Step 6: Commit**

```bash
git add -A odoo_migrator prompts cartridges docs tests
git commit -m "refactor: a side is source or target, never old or new

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 2: A side legend in every agent prompt

**Files:**
- Modify: `odoo_migrator/runner/prompting.py` (`system_prompt`)
- Test: `tests/test_system_prompt.py` (existing; add cases)

**Interfaces:**
- Consumes: Task 1's `agent_path(migration, module, side)`.
- Produces: `system_prompt(step, ctx)` always contains the block below, rendered by the runner, whatever the flow's own prompts say.

```
## The two sides
source: Odoo 17.0 — the version this module is ported FROM. Read it; the only thing you may write there is a characterization test, by `write_source_test`, when your step has that tool.
target: Odoo 18.0 — the version this module is ported TO. This is where the port is written.
Its directory on the source side: <path>
Its directory on the target side: <path>
```

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_system_prompt.py` (reuse the file's existing `ctx`/`step` fixtures or helpers; if it builds them inline, copy that pattern):

```python
def test_the_system_prompt_says_which_side_is_which(ctx, step):
    text = system_prompt(step, ctx)

    assert "## The two sides" in text
    assert "source: Odoo 17.0" in text
    assert "ported FROM" in text
    assert "target: Odoo 18.0" in text
    assert "ported TO" in text


def test_the_system_prompt_names_no_side_old_or_new(ctx, step):
    text = system_prompt(step, ctx)

    assert "(old)" not in text
    assert "(new)" not in text
    assert "old side" not in text
    assert "new side" not in text
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/test_system_prompt.py -q`
Expected: the two new tests FAIL (the block does not exist; the facts still say "(old) to ... (new)").

- [ ] **Step 3: Implement**

In `system_prompt`, replace the `facts` string:

```python
    facts = (
        f"## This run\n"
        f"Module: {ctx.module.technical_name}\n"
        f"Step: {step.id or step.kind}\n"
        f"\n"
        f"## The two sides\n"
        f"source: Odoo {ctx.migration.source_version} — the version this module is "
        f"ported FROM. Read it; the only thing you may write there is a "
        f"characterization test, by `write_source_test`, when your step has that tool.\n"
        f"target: Odoo {ctx.migration.target_version} — the version this module is "
        f"ported TO. This is where the port is written.\n"
        # The paths `read_file` and `write_file` take. A project that keeps
        # its addons in a subdirectory is the common shape, and a model told
        # only the module's name writes one level too high.
        f"Its directory on the source side: {agent_path(ctx.migration, ctx.module, 'source')}\n"
        f"Its directory on the target side: {agent_path(ctx.migration, ctx.module, 'target')}"
    )
```

`StepScript` in `tests/test_stock_flow_end_to_end.py` finds the step with `system.rsplit("Step: ", 1)[1].strip()`. With `Step:` no longer last, that breaks: change the helper to `system.split("Step: ", 1)[1].splitlines()[0].strip()`. Search `tests/` for other `rsplit("Step: "` users and change them the same way.

- [ ] **Step 4: Run, then the suite**

Run: `uv run pytest tests/test_system_prompt.py tests/test_stock_flow_end_to_end.py -q`, then `uv run pytest -q 2>&1 | tail -5`
Expected: all pass.

- [ ] **Step 5: Commit**

```bash
ruff check --fix odoo_migrator tests
git add -A odoo_migrator tests
git commit -m "feat: every agent prompt says which side is the source and which the target

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 3: The `write_source_test` tool

**Files:**
- Modify: `odoo_migrator/runner/tools.py`, `odoo_migrator/runner/context.py`
- Test: `tests/test_source_test_tool.py` (new)

**Interfaces:**
- Consumes: `module_root(migration, module, "source")`, `_inside(ctx, side, given)` (resolves a container path or a project-relative path to a host `Path`, refusing escapes), `ToolRefused`.
- Produces (tools.py): `TOOL_SIDES["write_source_test"] = ("source",)`; `TOOL_SIDES["reopen"] = ("source", "target")`; `"write_source_test"` in `HOST_TOOLS`; description and schema entries (same arguments as `write_file`: `side`, `path`, `content`); `_write_source_test(ctx, side, relative, content) -> str`; `_note_source_run(ctx, name, side, value)` called from `_bind.call` after a tool returns.
- Produces (context.py): on `StepContext`: `source_tests_had: bool | None = None`, `source_tests_written: bool = False`, `source_green: bool = False`. The runner resets all three at the start of a step that has the tool (Task 5).
- Consumes from Task 4: `odoo_migrator.source_git.ensure_branch(migration)` and `SourceGitError`. To keep this task testable alone, the tool imports them lazily and the tests monkeypatch `ensure_branch`; Task 4 provides the real one.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_source_test_tool.py`:

```python
"""The one write the source side allows: a new test, in the module's own tests/."""

import pytest

from odoo_migrator.db import database, init_db
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Migration, Module
from odoo_migrator.runner import tools
from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext
from odoo_migrator.runner.tools import TOOL_SIDES, ToolRefused, _write_source_test, tools_for


@pytest.fixture
def ctx(tmp_path, monkeypatch):
    init_db(tmp_path / "t.sqlite")
    source = tmp_path / "17"
    target = tmp_path / "18"
    (source / "sale_x" / "models").mkdir(parents=True)
    (source / "sale_x" / "__manifest__.py").write_text("{'name': 'X'}\n")
    target.mkdir()
    created = create_migration(
        name="Acme", accent="violet", source_version="17.0", target_version="18.0",
        old_project_path=str(source), new_project_path=str(target),
    )
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])
        module = Module.create(
            migration=migration, technical_name="sale_x",
            old_path=str(source / "sale_x"), new_path=str(target / "sale_x"),
        )
    monkeypatch.setattr("odoo_migrator.source_git.ensure_branch", lambda migration: "migrator/x", raising=False)

    return StepContext(
        job_id="j", attempt=1, migration=migration, module=module, cartridge=None,
        sheller=None, sessions={}, emit=lambda event: None,
    )


def test_a_test_is_written_under_tests_of_the_source_module(ctx, tmp_path):
    written = _write_source_test(ctx, "source", "sale_x/tests/test_x.py", "# t\n")

    assert (tmp_path / "17" / "sale_x" / "tests" / "test_x.py").read_text() == "# t\n"
    assert written.endswith("sale_x/tests/test_x.py")
    assert ctx.source_tests_written is True
    assert ctx.source_green is False


def test_a_path_outside_tests_is_refused_not_clamped(ctx, tmp_path):
    for bad in ("sale_x/models/x.py", "sale_x/__manifest__.py", "sale_x/tests/../models/x.py", "../x.py"):
        with pytest.raises(ToolRefused):
            _write_source_test(ctx, "source", bad, "x\n")

    assert not (tmp_path / "17" / "sale_x" / "tests").exists()
    assert not (tmp_path / "17" / "sale_x" / "models" / "x.py").exists()


def test_a_module_that_already_has_tests_is_never_written_to(ctx, tmp_path):
    (tmp_path / "17" / "sale_x" / "tests").mkdir()
    (tmp_path / "17" / "sale_x" / "tests" / "test_old.py").write_text("# kept\n")

    with pytest.raises(ToolRefused, match="already has tests"):
        _write_source_test(ctx, "source", "sale_x/tests/test_new.py", "x\n")

    assert (tmp_path / "17" / "sale_x" / "tests" / "test_old.py").read_text() == "# kept\n"
    assert not (tmp_path / "17" / "sale_x" / "tests" / "test_new.py").exists()


def test_a_test_file_outside_tests_counts_as_a_test(ctx, tmp_path):
    (tmp_path / "17" / "sale_x" / "models" / "test_stray.py").write_text("# x\n")

    with pytest.raises(ToolRefused, match="already has tests"):
        _write_source_test(ctx, "source", "sale_x/tests/test_new.py", "x\n")


def test_a_step_may_write_several_files_and_rewrite_its_own(ctx, tmp_path):
    _write_source_test(ctx, "source", "sale_x/tests/__init__.py", "from . import test_x\n")
    _write_source_test(ctx, "source", "sale_x/tests/test_x.py", "v1\n")
    _write_source_test(ctx, "source", "sale_x/tests/test_x.py", "v2\n")

    assert (tmp_path / "17" / "sale_x" / "tests" / "test_x.py").read_text() == "v2\n"


def test_a_refused_branch_refuses_the_write(ctx, tmp_path, monkeypatch):
    from odoo_migrator.source_git import SourceGitError

    def refuse(migration):
        raise SourceGitError("the source project is not a git repository")

    monkeypatch.setattr("odoo_migrator.source_git.ensure_branch", refuse)

    with pytest.raises(ToolRefused, match="not a git repository"):
        _write_source_test(ctx, "source", "sale_x/tests/test_x.py", "x\n")

    assert not (tmp_path / "17" / "sale_x" / "tests").exists()


def test_the_target_side_is_not_a_place_to_write_a_source_test(ctx):
    assert TOOL_SIDES["write_source_test"] == ("source",)
    step = Step(step="agent", id="c", sessions=["target"], tools=["write_source_test"], prompt="p")

    with pytest.raises(ToolRefused):
        tools_for(step, ctx)[0](side="target", path="sale_x/tests/test_x.py", content="x\n")


def test_port_and_rewrite_still_cannot_write_the_source(ctx, tmp_path):
    step = Step(step="agent", id="port", sessions=["source", "target"], tools=["write_file"], prompt="p")
    write = tools_for(step, ctx)[0]

    with pytest.raises(ToolRefused):
        write(side="source", path="sale_x/models/x.py", content="x\n")

    assert not (tmp_path / "17" / "sale_x" / "models" / "x.py").exists()


def test_a_green_run_on_the_source_marks_the_tests_green(ctx):
    ctx.source_tests_written = True
    green = {"test": {"success": True, "tests_run": 2, "failures": 0, "errors": 0}}

    tools._note_source_run(ctx, "run_test", "source", green)

    assert ctx.source_green is True


def test_a_run_that_ran_nothing_or_failed_is_not_green(ctx):
    ctx.source_tests_written = True
    for report in (
        {"test": {"success": True, "tests_run": 0}},
        {"test": {"success": False, "tests_run": 1, "failures": 1}},
        {"error": "boom"},
    ):
        ctx.source_green = True

        tools._note_source_run(ctx, "run_test", "source", report)

        assert ctx.source_green is False


def test_a_run_on_the_target_or_before_any_write_changes_nothing(ctx):
    green = {"test": {"success": True, "tests_run": 2}}

    tools._note_source_run(ctx, "run_test", "source", green)
    ctx.source_tests_written = True
    tools._note_source_run(ctx, "run_test", "target", green)

    assert ctx.source_green is False
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/test_source_test_tool.py -q`
Expected: collection error / FAIL — `_write_source_test` and `_note_source_run` do not exist, `StepContext` has no `source_tests_written`.

- [ ] **Step 3: Implement**

`odoo_migrator/runner/context.py`, on `StepContext` beside `last_test`:

```python
    # What a step that writes a characterization test has done. The runner
    # clears these when such a step starts; `write_source_test` fills them
    # and `source_git.settle_tests` reads them when it ends.
    source_tests_had: bool | None = None
    source_tests_written: bool = False
    source_green: bool = False
```

`odoo_migrator/runner/tools.py`:

```python
TOOL_SIDES = {
    ...
    "reopen": ("source", "target"),
    ...
    "write_file": ("target",),
    "write_source_test": ("source",),
}

HOST_TOOLS = frozenset({"read_file", "write_file", "write_source_test"})
```

In `TOOL_SCHEMAS` add `"write_source_test"` with the same schema object as `"write_file"`. In `TOOL_DESCRIPTIONS`:

```python
    "write_source_test": (
        "Write a characterization test for a module that has no tests, into "
        "`tests/` of the module on the source side. Nothing else on the source "
        "side can be written, and a module that already has tests refuses. "
        "Reopen the source session after writing, then run the test there."
    ),
```

In `_dispatch`, beside the `write_file` branch:

```python
    if name == "write_source_test":

        return _write_source_test(ctx, side, kwargs.get("path", ""), kwargs.get("content", ""))
```

In `_bind.call`, right after `value = _dispatch(...)` succeeds (before `command_id`), add `_note_source_run(ctx, name, side, value)`.

New functions:

```python
def _write_source_test(ctx, side: str, relative: str, content: str) -> str:
    """The one write the source side allows: a new test in the module's tests/.

    Refused when the module already had a test when the step began, so the
    prompt is not the only thing between the model and a test someone wrote.
    """
    from odoo_migrator.module_paths import module_root
    from odoo_migrator.source_git import SourceGitError, ensure_branch

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
            f"{ctx.module.technical_name} already has tests on the source side. "
            "Nothing is written next to them."
        )
    try:
        ensure_branch(ctx.migration)
    except SourceGitError as exc:
        raise ToolRefused(str(exc)) from None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    ctx.source_tests_written = True
    ctx.source_green = False

    return str(path)


def _has_tests(root: Path) -> bool:
    if (root / "tests").exists():

        return True

    return any(
        "__pycache__" not in found.parts for found in root.rglob("test_*.py")
    )


def _note_source_run(ctx, name: str, side: str, value) -> None:
    """Whether the tests this step wrote are green, from the run that proves it.

    A run counts when it was on the source side, after a write, and ran at
    least one test and none failed. A later write clears it again.
    """
    if name != "run_test" or side != "source" or not ctx.source_tests_written:

        return
    outcome = value.get("test") if isinstance(value, dict) else None
    ctx.source_green = bool(
        isinstance(outcome, dict)
        and not value.get("error")
        and outcome.get("success")
        and (outcome.get("tests_run") or 0) > 0
    )
```

`source_git.py` does not exist yet; the tests monkeypatch `ensure_branch` with `raising=False`, but `from odoo_migrator.source_git import ...` needs the module to import. Create a stub file in this task that Task 4 fills:

```python
"""The migration's branch in the source project. See Task 4."""

from __future__ import annotations


class SourceGitError(Exception):
    """The source project cannot take a characterization test."""


def ensure_branch(migration) -> str:
    raise SourceGitError("not implemented")
```

- [ ] **Step 4: Run, then the suite**

Run: `uv run pytest tests/test_source_test_tool.py -q`, then `uv run pytest -q 2>&1 | tail -5`
Expected: all pass. (Note `_inside` for a project-relative `sale_x/tests/test_x.py` resolves under the source project root; the fixture's module sits directly in it.)

- [ ] **Step 5: Commit**

```bash
ruff check --fix odoo_migrator tests
git add -A odoo_migrator tests
git commit -m "feat: write_source_test, the one write the source side allows

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 4: `source_git.py` — the branch, and commit-or-delete

**Files:**
- Modify: `odoo_migrator/source_git.py` (replace the stub)
- Test: `tests/test_source_git.py` (new)

**Interfaces:**
- Consumes: from `odoo_migrator.target_git`: `_git(cwd, *args, check=True)`, `_toplevel(directory)`, `_too_broad(top)`, `_LOCK`, `_why(exc)`, `AUTHOR`, `_EXCLUDED`.
- Produces:
  - `SourceGitError(Exception)`.
  - `branch_name(migration) -> str`: `migrator/<slug>-<id8>`; slug is the migration name lowercased, runs of anything outside `[a-z0-9]` collapsed to `-`, stripped, cut at 30, `migration` when empty.
  - `ensure_branch(migration) -> str`: the branch name, with the source project's checkout on it.
  - `settle_tests(migration, module, job_id, green) -> str | None`: a line for the run log, or None when `tests/` is absent.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_source_git.py`:

```python
"""The source project gets a branch of its own, and a green test is committed there."""

import subprocess
from pathlib import Path

import pytest

from odoo_migrator.db import database, init_db
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Migration, Module
from odoo_migrator.source_git import SourceGitError, branch_name, ensure_branch, settle_tests


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    init_db(tmp_path / "t.sqlite")
    source = tmp_path / "17"
    (source / "sale_x").mkdir(parents=True)
    (source / "sale_x" / "__manifest__.py").write_text("{'name': 'X'}\n")
    git(source, "init", "-q", "-b", "main")
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "seed")
    target = tmp_path / "18"
    target.mkdir()
    created = create_migration(
        name="Acme Corp 17→18", accent="violet", source_version="17.0", target_version="18.0",
        old_project_path=str(source), new_project_path=str(target),
    )
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])
        module = Module.create(
            migration=migration, technical_name="sale_x",
            old_path=str(source / "sale_x"), new_path=str(target / "sale_x"),
        )

    return source, migration, module


def test_the_branch_name_is_safe_and_names_the_migration(repo):
    _, migration, _ = repo

    name = branch_name(migration)

    assert name.startswith("migrator/acme-corp-17-18-")
    assert name.endswith(str(migration.id)[:8])
    git(".", "check-ref-format", "--branch", name)


def test_the_branch_is_made_from_head_and_the_default_branch_is_left_alone(repo):
    source, migration, _ = repo
    head = git(source, "rev-parse", "main")

    name = ensure_branch(migration)

    assert git(source, "branch", "--show-current") == name
    assert git(source, "rev-parse", name) == head
    assert git(source, "rev-parse", "main") == head


def test_a_second_call_reuses_the_branch(repo):
    source, migration, _ = repo

    first = ensure_branch(migration)
    second = ensure_branch(migration)

    assert first == second
    assert git(source, "branch", "--list", "migrator/*").count("migrator/") == 1


def test_uncommitted_work_travels_with_the_switch_and_stays_uncommitted(repo):
    source, migration, module = repo
    (source / "sale_x" / "notes.txt").write_text("mine\n")

    ensure_branch(migration)
    (source / "sale_x" / "tests").mkdir()
    (source / "sale_x" / "tests" / "test_x.py").write_text("# t\n")
    settle_tests(migration, module, "job1", green=True)

    assert (source / "sale_x" / "notes.txt").read_text() == "mine\n"
    assert "notes.txt" not in git(source, "show", "--name-only", "--format=", "HEAD")
    assert "sale_x/tests/test_x.py" in git(source, "show", "--name-only", "--format=", "HEAD")
    assert "?? sale_x/notes.txt" in git(source, "status", "--short")


def test_a_project_that_is_not_a_repository_is_refused(tmp_path):
    init_db(tmp_path / "t.sqlite")
    source = tmp_path / "17"
    source.mkdir()
    created = create_migration(
        name="Plain", accent="violet", source_version="17.0", target_version="18.0",
        old_project_path=str(source), new_project_path=str(tmp_path / "18"),
    )
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])

    with pytest.raises(SourceGitError, match="not a git repository"):
        ensure_branch(migration)

    assert not (source / ".git").exists()


def test_a_green_test_is_committed_with_only_its_own_files(repo):
    source, migration, module = repo
    ensure_branch(migration)
    tests = source / "sale_x" / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("from . import test_x\n")
    (tests / "test_x.py").write_text("# t\n")
    (tests / "__pycache__").mkdir()
    (tests / "__pycache__" / "x.pyc").write_bytes(b"\0")

    line = settle_tests(migration, module, "job1", green=True)

    changed = git(source, "show", "--name-only", "--format=%s", "HEAD").splitlines()
    assert changed[0] == "odoo-migrator: characterization tests for sale_x"
    assert sorted(changed[1:]) == ["sale_x/tests/__init__.py", "sale_x/tests/test_x.py"]
    assert "committed" in line


def test_a_test_that_is_not_green_is_deleted_and_nothing_is_committed(repo):
    source, migration, module = repo
    ensure_branch(migration)
    before = git(source, "rev-parse", "HEAD")
    tests = source / "sale_x" / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text("# red\n")

    line = settle_tests(migration, module, "job1", green=False)

    assert not tests.exists()
    assert git(source, "rev-parse", "HEAD") == before
    assert git(source, "status", "--short") == ""
    assert "removed" in line


def test_nothing_written_is_nothing_to_settle(repo):
    _, migration, module = repo

    assert settle_tests(migration, module, "job1", green=False) is None
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/test_source_git.py -q`
Expected: FAIL — `branch_name` and `settle_tests` do not exist; `ensure_branch` raises "not implemented".

- [ ] **Step 3: Implement**

Replace the stub `odoo_migrator/source_git.py`:

```python
"""Give the source project a branch of its own, and keep characterization
tests on it.

The source side is read-only to the agent with one exception: a test for a
module that ships none, written by `write_source_test`. That file must not
land on the branch the person works on, so the first write of a migration
puts the source checkout on `migrator/<name>-<id>`, made from where it was.
A green test is committed there, one commit per module; a test that is not
green is deleted, so a red one never reaches the port.

Same conventions as `target_git`: a fixed author, no hooks, no signing, one
lock per process, and never a repository at or above the home directory.
Nothing here initialises git: a source project that is not a repository is
refused, not made into one.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from odoo_migrator.module_paths import module_root
from odoo_migrator.target_git import _EXCLUDED, _LOCK, _git, _toplevel, _too_broad, _why


class SourceGitError(Exception):
    """The source project cannot take a characterization test."""


def branch_name(migration) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(migration.name).lower()).strip("-")[:30].strip("-")

    return f"migrator/{slug or 'migration'}-{str(migration.id)[:8]}"


def ensure_branch(migration) -> str:
    """The source checkout is on this migration's branch when this returns."""
    root = Path(migration.old_project_path or "").expanduser()
    top = _toplevel(root) if root.is_dir() else None
    if top is None or _too_broad(top):
        raise SourceGitError(
            "the source project is not a git repository, so a characterization "
            "test cannot be kept on a branch of its own. Nothing was written."
        )
    name = branch_name(migration)
    with _LOCK:
        try:
            current = _git(top, "branch", "--show-current", check=False).stdout.strip()
            if current == name:

                return name
            exists = _git(
                top, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False
            ).returncode == 0
            done = _git(top, "switch", *(() if exists else ("-c",)), name, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SourceGitError(f"git failed in the source project: {_why(exc)}") from None
    if done.returncode != 0:
        raise SourceGitError(
            f"the source project could not be switched to {name}: {done.stderr.strip()}"
        )

    return name


def settle_tests(migration, module, job_id: str, green: bool) -> str | None:
    """Commit the tests a step wrote when they pass; delete them when they do not.

    `tests/` did not exist before the step (the write is refused otherwise),
    so deleting it removes only what the step made. None when there is none.
    """
    directory = module_root(migration, module, "source")
    if directory is None:

        return None
    tests = Path(directory) / "tests"
    if not tests.is_dir():

        return None
    name = module.technical_name
    if not green:
        shutil.rmtree(tests, ignore_errors=True)

        return (
            f"the characterization tests of {name} did not pass on the source "
            "version, so they were removed"
        )
    top = _toplevel(tests)
    if top is None or _too_broad(top):

        return f"the characterization tests of {name} could not be committed: no repository"
    with _LOCK:
        try:
            _git(top, "add", "-A", "--", str(tests), *_EXCLUDED)
            staged = _git(top, "diff", "--cached", "--quiet", "--", str(tests), check=False)
            if staged.returncode == 0:

                return None
            _git(
                top, "commit", "-q", "--no-verify", "-m",
                f"odoo-migrator: characterization tests for {name}\n\nJob {job_id}.",
                "--", str(tests),
            )
            head = _git(top, "rev-parse", "--short", "HEAD", check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:

            return f"the characterization tests of {name} could not be committed: {_why(exc)}"

    return f"committed the characterization tests of {name} as {head}"
```

Note: the `_EXCLUDED` pathspecs are `:(exclude,glob)**/__pycache__/**`-style and apply to the `add`. If `git add -A -- <abs path> <exclude...>` complains about the mix of absolute and magic pathspecs, run the add with `cwd=tests` and `"."` instead of `str(tests)`, as `commit_module` does with its directory.

- [ ] **Step 4: Run, then the suite**

Run: `uv run pytest tests/test_source_git.py tests/test_source_test_tool.py -q`, then `uv run pytest -q 2>&1 | tail -5`
Expected: all pass. A `module_paths` import cycle (`source_git` -> `module_paths` -> ... -> `tools` -> `source_git`) is avoided because `tools.py` imports `source_git` lazily inside `_write_source_test`.

- [ ] **Step 5: Commit**

```bash
ruff check --fix odoo_migrator tests
git add -A odoo_migrator tests
git commit -m "feat: the source project gets a branch for the migration; green tests are committed there

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

### Task 5: The step, the flag, the runner hook, the docs

**Files:**
- Modify: `cartridges/stock.yaml`, `odoo_migrator/runner/native.py` (`wrapped` in `_run_job`; new `_reset_source_tests` / `_settle_source_tests`)
- Modify: `docs/runner-design.md`, `docs/security.md`, `docs/architecture.md`, `docs/ui-guide.md`, `docs/faq.md`, `docs/faq-ru.md`, `README.md`
- Test: `tests/test_characterize_flow.py` (new)

**Interfaces:**
- Consumes: `StepContext.source_tests_had/written/green` (Task 3), `settle_tests(migration, module, job_id, green)` (Task 4), `system_prompt` (Task 2).
- Produces: a stock flow in which `understand` raises `needs_tests`, `characterize` (`when: needs_tests`) runs between `understand` and `port`, and `port`'s prompt carries the source's tests.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_characterize_flow.py`. It copies `MANIFEST`, the `sandbox` fixture and `_run` from `tests/test_stock_flow_end_to_end.py` (the source project there is `tmp_path / "17"`, the module `project/sale_custom`), makes the source a git repository, and drives `characterize` with a scripted provider.

```python
"""characterize: a test written on the source, proved there, ported with the module."""

from pathlib import Path
import subprocess

import pytest

from odoo_migrator.db import database
from odoo_migrator.models import Job
from odoo_migrator.runner.cartridge import load_cartridge_file
from odoo_migrator.runner.context import StepResult
from odoo_migrator.runner.native import NativeRunner
from odoo_migrator.runner.tools import ToolRefused
# MANIFEST, sandbox, _run: copy them from tests/test_stock_flow_end_to_end.py

TEST = (
    "from odoo.tests import TransactionCase\n\n\n"
    "class TestX(TransactionCase):\n    def test_x(self):\n        self.assertTrue(True)\n"
)


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


def make_repo(source):
    git(source, "init", "-q", "-b", "main")
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "seed")

    return git(source, "rev-parse", "main")


class CharacterizeScript:
    """`understand` raises (or not) `needs_tests`; `characterize` writes and runs."""

    name = "scripted"

    def __init__(self, *, raise_needs_tests=True, run_it=True):
        self.raise_needs_tests = raise_needs_tests
        self.run_it = run_it
        self.ran: list[str] = []
        self.port_prompt = ""
        self.refusal = ""

    def ensure_ready(self) -> None:

        return None

    def run_step(self, *, system, prompt, tools, report, model, budget, stop_check=None):
        step = system.split("Step: ", 1)[1].splitlines()[0].strip()
        self.ran.append(step)
        by_name = {tool.name: tool for tool in tools}
        flags = set()
        if step == "understand" and self.raise_needs_tests:
            flags.add("needs_tests")
        if step == "characterize":
            try:
                for name, body in (("__init__.py", "from . import test_x\n"), ("test_x.py", TEST)):
                    by_name["write_source_test"](
                        side="source", path=f"project/sale_custom/tests/{name}", content=body
                    )
            except ToolRefused as exc:
                # The provider loop hands a refusal back to the model as text,
                # and a model that cannot write ends the step.
                self.refusal = str(exc)

                return StepResult(ok=True, answer="could not write a test")
            if self.run_it:
                by_name["run_test"](side="source", test="sale_custom.TestX")
        if step == "port":
            self.port_prompt = prompt
            by_name["write_file"](
                side="target", path="project/sale_custom/__manifest__.py", content=MANIFEST
            )

        return StepResult(ok=True, answer=f"{step} done", flags=flags)


def _source(sandbox):
    _, tmp_path = sandbox

    return tmp_path / "17"


def _status(job):
    with database.connection_context():

        return Job.get_by_id(job.id).status


def test_a_testless_module_gets_a_green_test_on_a_branch_and_the_port_sees_it(app, fake_sheller, sandbox):
    source = _source(sandbox)
    seed = make_repo(source)
    fake_sheller.tests_for("sale_custom", fake_sheller.catalogue("sale_custom", {"TestX": ["test_x"]}))
    script = CharacterizeScript()

    job = _run(app, fake_sheller, sandbox, script)

    assert script.ran.index("characterize") < script.ran.index("port")
    assert git(source, "branch", "--show-current").startswith("migrator/sandbox-")
    assert git(source, "rev-parse", "main") == seed
    assert "project/sale_custom/tests/test_x.py" in git(source, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert "[sheller:" in git(source, "log", "-1", "--format=%s")
    assert "Sheller-session: " in git(source, "log", "-1", "--format=%B")
    assert "### tests/test_x.py" in script.port_prompt
    assert _status(job) == "passed"


def test_a_module_that_needs_no_tests_skips_characterize(app, fake_sheller, sandbox):
    source = _source(sandbox)
    make_repo(source)
    script = CharacterizeScript(raise_needs_tests=False)

    job = _run(app, fake_sheller, sandbox, script)

    assert "characterize" not in script.ran
    assert not (source / "project" / "sale_custom" / "tests").exists()
    assert git(source, "branch", "--show-current") == "main"
    assert _status(job) == "passed"


def test_a_test_that_never_goes_green_is_removed_and_the_lap_carries_on(app, fake_sheller, sandbox):
    source = _source(sandbox)
    seed = make_repo(source)
    fake_sheller.test_outcomes["sale_custom.TestX"] = {"success": False, "failures": 1, "tests_run": 1}
    script = CharacterizeScript()

    job = _run(app, fake_sheller, sandbox, script)

    assert not (source / "project" / "sale_custom" / "tests").exists()
    assert git(source, "rev-parse", "HEAD") == seed
    assert git(source, "status", "--short") == ""
    assert "tests/test_x.py" not in script.port_prompt
    assert "port" in script.ran
    assert _status(job) == "passed"


def test_a_test_that_was_never_run_is_not_green(app, fake_sheller, sandbox):
    source = _source(sandbox)
    make_repo(source)

    job = _run(app, fake_sheller, sandbox, CharacterizeScript(run_it=False))

    assert not (source / "project" / "sale_custom" / "tests").exists()
    assert _status(job) == "passed"


def test_a_source_project_that_is_not_a_repository_costs_the_lap_nothing(app, fake_sheller, sandbox):
    source = _source(sandbox)
    script = CharacterizeScript()

    job = _run(app, fake_sheller, sandbox, script)

    assert "not a git repository" in script.refusal
    assert not (source / "project" / "sale_custom" / "tests").exists()
    assert not (source / ".git").exists()
    assert "port" in script.ran
    assert _status(job) == "passed"
```

Also add, in the same file:

```python
def test_the_stock_flow_wires_the_step():
    flow = load_cartridge_file(Path("cartridges/stock.yaml"))
    ids = [step.id or step.kind for step in flow.pipeline]
    understand = flow.step_by_id("understand")
    characterize = flow.step_by_id("characterize")

    assert "needs_tests" in understand.raises
    assert ids.index("understand") < ids.index("characterize") < ids.index("port")
    assert characterize.when == "needs_tests"
    assert characterize.sessions == ["source"]
    assert "write_source_test" in characterize.tools
    assert "write_source_test" not in flow.step_by_id("port").tools
    assert "write_source_test" not in flow.step_by_id("rewrite").tools
```

- [ ] **Step 2: Run and watch it fail**

Run: `uv run pytest tests/test_characterize_flow.py -q`
Expected: FAIL — `characterize` is not in the flow, `understand` raises nothing.

- [ ] **Step 3: The flow file**

`cartridges/stock.yaml`:

1. `understand` gains `raises: [needs_tests]` after `context:` and these paragraphs at the end of its prompt:

```
      Last, decide whether this module needs tests written for it. Call
      `list_tests` on the source side first. If it lists any test class, the
      module has tests: leave `needs_tests` unset and say nothing about
      tests. If it has none, raise `needs_tests` when the module has Python
      logic worth pinning (methods, computed fields, constraints, onchanges,
      state transitions). Leave it unset for a module of data, views,
      security files or translations only, and say so in your answer.
```

2. After `understand` and before `port`:

```yaml
  - step: agent
    id: characterize
    when: needs_tests
    sessions: [source]
    tools: [exec, source, run_test, list_tests, read_file, write_source_test, reopen]
    context: [rules, module_source, prior_answers]
    prompt: |
      This module has no tests. Write the smallest test that pins the
      behaviour the understand step described, built from the recipe it
      proved, into `tests/` of the module on the source side with
      `write_source_test`: a `tests/__init__.py` that imports the test file,
      and the test file. Read a test of a module that has one with `source`
      before writing yours: the base class and the helpers are this
      version's, not the ones you remember.

      Then `reopen` the source session, so Odoo sees the new package, and
      `run_test` it there. Fix the test until it passes on the source. A
      test that cannot be made to pass here is not evidence of anything: say
      so and finish; the runner removes what you wrote.

      This test is the oracle for the port. It must assert what this
      version does, not what you think it ought to do.
```

3. `port`'s prompt gains, after its first paragraph:

```
      If the module's `tests/` directory is in the source you were given, it
      holds characterization tests this run wrote and proved green on the
      source version. Port them with the rest, and keep what they assert:
      change only what the target version forces.
```

- [ ] **Step 4: The runner hook**

In `odoo_migrator/runner/native.py`, in `wrapped` (inside `_run_job`), around the existing `writes`/`before` lines:

```python
                settles = "write_source_test" in (step.tools or ())
                if settles:
                    current.source_tests_had = None
                    current.source_tests_written = False
                    current.source_green = False
```

and make the `try:` that calls `runner(step, current)` end with:

```python
                finally:
                    if settles:
                        self._settle_source_tests(current, label)
```

(Python allows `try / except / finally`; the existing `except Exception as exc:` block stays. A stop raises `Stopped` through the `finally`, which is what is wanted: the tests are settled either way.)

New method on the runner class:

```python
    def _settle_source_tests(self, ctx: StepContext, label: str) -> None:
        """Keep what a green step wrote and delete what is not green."""
        from odoo_migrator.source_git import settle_tests

        if not ctx.source_tests_written:

            return
        try:
            line = settle_tests(
                ctx.migration, ctx.module, ctx.job_id, ctx.source_green,
                ctx.source_green_session, ctx.source_green_command,
            )
        except Exception as exc:  # a git failure must not fail the lap
            line = f"the characterization tests could not be settled: {exc}"
        if line:
            self._log(ctx.job_id, label, line, level="info" if ctx.source_green else "warn")
```

- [ ] **Step 5: Run the new tests, then the whole suite**

Run: `uv run pytest tests/test_characterize_flow.py -q`, then `uv run pytest -q 2>&1 | tail -8`
Expected: all pass. If the end-to-end `StepScript` helper in `test_stock_flow_end_to_end.py` now sees `understand` raising `needs_tests` only when scripted, its existing tests are unchanged (they never raise it).

- [ ] **Step 6: Docs**

- `docs/runner-design.md`: in "What does not change", the sessions bullet says "the old container is read-only for the whole run" → "the source container is read-only for the whole run, with one exception: `write_source_test` (see Overrides, item 6)". In "Overrides vs the 2026-09-20 spec" add:

  `6. **The source side can take a characterization test.** One tool, `write_source_test`, creates files in the module's own `tests/` on the source side, only for a module that has no tests, only on a branch the app made in the source project, and a test that is not green is deleted. Everything else on the source side stays read-only.`

  Add the `characterize` step and the `needs_tests` flag to the stock flow's description; the file's `understand`/`port` walk-through is the place.
- `docs/security.md`: the sentence "The old container is read-only for the run" (line ~75) gets the same exception, and a short section "Writing a test on the source side" with the five guards: own tool, only `tests/`, only when the module had none, create-only inside the step, branch of its own, and delete-if-red.
- `docs/architecture.md`: step list gets `characterize`; mention `source_git.py` beside `target_git.py`.
- `docs/ui-guide.md`, `docs/faq.md`, `docs/faq-ru.md`: why a `migrator/…` branch appears in the source project, and why the source checkout stays on it while a migration runs.
- `README.md`: `source_git.py` in the module table.

- [ ] **Step 7: Final suite, lint, commit**

Run: `ruff check --fix odoo_migrator tests && uv run pytest -q 2>&1 | tail -5`
Expected: ruff clean; all tests pass.

```bash
git add -A cartridges docs README.md odoo_migrator tests
git commit -m "feat: an optional characterize step writes a test on the source before the port

Co-Authored-By: Claude Sonnet 5.5 <noreply@anthropic.com>"
```

---

## Self-review

**Spec coverage.** Vocabulary (spec "Vocabulary") → Task 1; the legend and its test (spec bullets under Vocabulary) → Task 2 and Task 1's tests; the decision (`needs_tests`) and the step → Task 5; the tool and its guards (own tool, tests/ only, had-tests snapshot, create-only inside the step, `reopen` on source) → Task 3; the branch, commit and delete → Task 4; the runner hook and the not-green path → Task 5; docs → Task 5; failure modes table → Task 3 (refusals), Task 4 (not a repository, red test), Task 5 (stopped step via `finally`). The "runner dies between the write and settle" row is a documented gap in the spec, not a task.

**Placeholders.** Task 1's rename is specified by the grep that finds every hit plus the per-file shape, because the edit is mechanical and the suite is the checker.

**Type consistency.** `ensure_branch(migration) -> str`, `settle_tests(migration, module, job_id, green) -> str | None`, `StepContext.source_tests_had/written/green`, `TOOL_SIDES["write_source_test"] = ("source",)`, `_write_source_test(ctx, side, relative, content)`, `_note_source_run(ctx, name, side, value)` are used with the same names in Tasks 3, 4 and 5.

**Open ruling carried from the spec.** `stock` stays at version `1.0.0` (the shipped file is the only text `flow_text` resolves for `stock`; a bump would orphan migrations pinned to it).
