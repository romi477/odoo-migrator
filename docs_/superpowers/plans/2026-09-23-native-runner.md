# Native runner implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace n8n with a runner inside this app that executes one module
at a time from a declarative YAML cartridge, and draw that cartridge live on
a `Flow` screen.

**Architecture:** `Executor.start(payload)` stays the seam; a `NativeRunner`
joins `StubExecutor` behind it and runs one job in one background thread. The
flow is data: a cartridge lists five deterministic step kinds plus a generic
`agent` step it configures with id, sessions, tools, context, raises and
prompt. Agent steps drive two live Odoo containers through odoo-sheller with
the Anthropic SDK's tool runner. The app stores the skeleton of a run;
Sheller's journal keeps the bodies.

**Tech Stack:** FastAPI, Jinja, Alpine, Peewee/SQLite, Pydantic v2, PyYAML,
`anthropic` (new dependency), Cytoscape.js (already vendored), httpx2.

**Spec:** `docs_/superpowers/specs/2026-09-23-native-runner-design.md` — read
it before Task 1 and keep it open. The plan argues from the spec; where they
differ, the spec wins.

**Branch:** `native-runner`. Do not merge to `main` until Task 13 passes.

## Global constraints

Copied from `AGENTS.md` and the spec. Every task's requirements include
these, and no task may quietly relax one.

- **Code, comments, docs and commits in English.** Discussion with the human
  happens in Russian.
- **One task at a time.** Implement the current task with tests, commit,
  stop. Do not begin task N+1 in the turn that commits N.
- **`return` gets a blank line before it.** Run `ruff check`, never
  `ruff format` — the formatter strips it.
- **Line length 100** (`[tool.ruff]` in `pyproject.toml`).
- **Peewee discipline:** every operation inside its own
  `db.connection_context()`; a connection never crosses a thread boundary.
  Schema changes use `playhouse.migrate`, never Alembic.
- **Every external boundary ships a test double in the task that introduces
  it, and a double must answer the way the real thing answers.** The suite
  must pass with no Docker, no Odoo, no Sheller daemon and no network.
- **Appearance lives in the stylesheet.** Inline `style=` only for custom
  properties such as `--accent`. A test enforces this; do not widen it.
- **Every new component gets a row on `/components`** in the task that adds
  it.
- **Templates live in `odoo_migrator/web/templates/`, assets in
  `web/static/`.** Only `static/` is mounted.
- **Never read `admin.key`, never touch `~/.odoo-sheller/`, never call the
  daemon's admin endpoints.** A daemon answering on 8765 may not be ours:
  use it, never stop it.
- **The app binds loopback.** After Task 1 there is no flag that widens it.
- **The source container never commits.** `allow_commit: false` for the whole
  run. The target container opens with `allow_commit: true`.
- **A cartridge may narrow, never widen.** Tools come from the code catalog,
  budgets are capped by the migration card, sessions restrict reach.
- **Model IDs:** default `claude-opus-5`; the cheaper alternative is
  `claude-sonnet-5`. Never append a date suffix.

---

### Task 1: Remove the n8n transport

Everything n8n needed to reach a loopback daemon from inside a container goes
first, so nothing later is built on top of it. The app keeps working on
`StubExecutor` throughout.

**Files:**
- Delete: `odoo_migrator/proxy.py`, `odoo_migrator/files.py`,
  `odoo_migrator/authkey.py`, `odoo_migrator/resume.py`,
  `odoo_migrator/n8n/` (whole package), `n8n/stock/`, `n8n/.env.example`,
  `packaging/n8n.sh`, `packaging/publish_stock.py`
- Delete: `tests/test_sheller_proxy.py`, `tests/test_files.py`,
  `tests/test_n8n_stock.py`, `tests/test_n8n_agents.py`,
  `tests/test_contract.py`, `tests/test_cartridges.py`
- Modify: `odoo_migrator/api.py` (drop the File API routes, the proxy routes,
  the progress/result webhooks, the resume route, the API-key dependency and
  the startup warning), `odoo_migrator/__main__.py` (drop `--host`),
  `odoo_migrator/settings.py` and `odoo_migrator/models.py` (drop
  `n8n_base_url`, `n8n_api_key`, `app_base_url` from `AppSettings`),
  `odoo_migrator/jobs.py` (`answer_job` no longer posts a resume URL),
  `odoo_migrator/web/templates/settings.html`, `run.html`, `base.html`,
  `styleguide.html`, `components.html`
- Test: `tests/test_settings.py`, `tests/test_hitl.py`, `tests/test_batch.py`,
  `tests/test_checkpoint.py`, `tests/test_packaging.py` (trim the parts that
  assert removed behaviour)

**Interfaces:**
- Consumes: nothing.
- Produces: `AppSettings` without the three n8n fields; `answer_job(*,
  module_id: int, answer: str, hub: EventHub) -> Job` (the `resume` argument
  is gone); an app with no authenticated routes and no `--host`.

- [x] **Step 1: Write the failing test**

Add to `tests/test_settings.py`:

```python
def test_no_route_requires_a_machine_key(client):
    """The File API and the Sheller proxy were the only authenticated routes.

    They existed because n8n ran in a container and could not reach a daemon
    that publishes to loopback. Nothing machine-facing is left.
    """
    paths = [route.path for route in client.app.routes]

    assert not [p for p in paths if p.startswith("/api/files")]
    assert not [p for p in paths if p.startswith("/api/sheller")]
    assert not [p for p in paths if "webhook" in p or "resume" in p]
```

- [x] **Step 2: Run it and watch it fail**

Run: `uv run pytest tests/test_settings.py::test_no_route_requires_a_machine_key -v`
Expected: FAIL — the routes are still registered.

- [x] **Step 3: Delete the modules and their tests**

```bash
git rm -r odoo_migrator/n8n n8n/stock packaging/n8n.sh packaging/publish_stock.py
git rm odoo_migrator/proxy.py odoo_migrator/files.py odoo_migrator/authkey.py odoo_migrator/resume.py
git rm tests/test_sheller_proxy.py tests/test_files.py tests/test_n8n_stock.py \
       tests/test_n8n_agents.py tests/test_contract.py tests/test_cartridges.py
```

- [x] **Step 4: Cut the routes and settings**

In `api.py` remove every route that mounted the deleted modules, the
`X-Odoo-Migrator-Key` dependency, and the startup warning about a widened
bind. In `__main__.py` remove `--host` and pass `host="127.0.0.1"` to
`uvicorn.run`. In `models.py` drop `n8n_base_url`, `n8n_api_key` and
`app_base_url` from `AppSettings`, and write a `playhouse.migrate` step that
drops those columns. In `settings.html` remove their fields and hints.

`jobs.answer_job` currently calls a resume callable; make it store the answer
and return the job. Nothing resumes it yet — Task 8 wires that — so a test
that asserted the job left `waiting_for_human` on answer is changed to assert
the answer is stored and the job stays parked.

- [x] **Step 5: Run the whole suite**

Run: `uv run pytest`
Expected: PASS. Roughly a hundred tests are gone with their subjects.

- [x] **Step 6: Check the tree for leftovers**

Run: `grep -rn "n8n\|X-Odoo-Migrator-Key\|app_base_url" odoo_migrator/ tests/ packaging/ README.md`
Expected: no hits outside `docs_/`. Any hit is either a deletion you missed
or a thing that must be renamed — nothing kept may need the word.

- [x] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor: remove the n8n transport and the surface it needed"
```

---

### Task 2: The cartridge language

**Files:**
- Create: `odoo_migrator/runner/__init__.py`,
  `odoo_migrator/runner/cartridge.py`, `cartridges/stock.yaml`
- Create: `tests/test_cartridge.py`,
  `tests/cartridges/` (one file per refusal)
- Modify: `pyproject.toml` (`[tool.setuptools.package-data]` must ship
  `cartridges/*.yaml`)

**Interfaces:**
- Consumes: nothing.
- Produces:
  - `class CartridgeError(Exception)`
  - `class Step(BaseModel)` — `kind: str`, `id: str | None`, `when: str |
    None`, `sessions: list[str]`, `tools: list[str]`, `context: list[str]`,
    `raises: list[str]`, `prompt: str`, `model: ModelRef | None`,
    `budget: Budget | None`
  - `class Cartridge(BaseModel)` — `cartridge: str`, `version: str`,
    `schema_version: int`, `model: ModelRef`, `pipeline: list[Step]`,
    `on_failure: FailureLane`, plus `sha256: str` and
    `def step_by_id(self, name: str) -> Step | None`
  - `def load_cartridge(text: str) -> Cartridge` — parses YAML **or** JSON
  - `def load_cartridge_file(path: Path) -> Cartridge`
  - `DETERMINISTIC = ("lint", "upgrade", "tests", "verdict", "ask_human")`
  - `BUILTIN_FLAGS = {"no_tests": "tests", "tests_failed": "tests",
    "lint_failed": "lint"}`
  - `CONTEXT_ITEMS = ("rules", "module_source", "prior_answers",
    "previous_diff", "last_failure")`

- [x] **Step 1: Write the failing tests**

`tests/test_cartridge.py`:

```python
import pytest

from odoo_migrator.runner.cartridge import CartridgeError, load_cartridge

MINIMAL = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-opus-5}
pipeline:
  - step: agent
    id: port
    sessions: [new]
    tools: [write_file]
    context: [rules]
    prompt: write it
  - step: verdict
on_failure:
  pipeline:
    - step: agent
      id: diagnose
      sessions: [new]
      tools: [exec]
      context: [last_failure]
      raises: [small_fix]
      prompt: why
    - step: agent
      id: patch
      when: small_fix
      sessions: [new]
      tools: [write_file]
      context: [previous_diff]
      prompt: fix it
  retry:
    resume_at: verdict
"""


def test_a_valid_cartridge_loads_and_carries_its_hash():
    loaded = load_cartridge(MINIMAL)

    assert loaded.cartridge == "t"
    assert [step.id or step.kind for step in loaded.pipeline] == ["port", "verdict"]
    assert len(loaded.sha256) == 64


def test_json_is_accepted_too():
    """yaml.safe_load parses JSON, so an uploaded .json file is not a refusal."""
    as_json = load_cartridge(
        '{"cartridge":"t","version":"1.0.0","schema":1,'
        '"model":{"provider":"anthropic","name":"claude-opus-5"},'
        '"pipeline":[{"step":"verdict"}],"on_failure":{"pipeline":[],'
        '"retry":{"resume_at":"verdict"}}}'
    )

    assert as_json.cartridge == "t"


@pytest.mark.parametrize(
    "broken, says",
    [
        ("unknown_step", "step"),
        ("unknown_flag", "flag"),
        ("unraised_flag", "raise"),
        ("unread_flag", "read"),
        ("unknown_tool", "tool"),
        ("tool_without_session", "session"),
        ("unknown_context", "context"),
        ("duplicate_id", "id"),
        ("agent_without_prompt", "prompt"),
        ("prompt_on_deterministic", "prompt"),
        ("resume_at_nowhere", "resume_at"),
        ("unknown_provider", "provider"),
        ("future_schema", "schema"),
    ],
)
def test_every_refusal_names_what_is_wrong(broken, says, cartridge_text):
    with pytest.raises(CartridgeError) as refused:
        load_cartridge(cartridge_text(broken))

    assert says in str(refused.value).lower()
```

Add to `tests/conftest.py`:

```python
@pytest.fixture
def cartridge_text():
    root = Path(__file__).parent / "cartridges"

    def read(name: str) -> str:

        return (root / f"{name}.yaml").read_text(encoding="utf-8")

    return read
```

- [x] **Step 2: Write the thirteen broken fixtures**

One file per row of the parametrize list, each a copy of `MINIMAL` with
exactly one thing wrong. For example `tests/cartridges/unread_flag.yaml`
declares `raises: [small_fix, start_over]` on `diagnose` while no `when:`
reads `start_over`; `tests/cartridges/tool_without_session.yaml` gives a step
`sessions: [new]` and `tools: [exec]` with `exec` aimed at `old`. Keep each
file minimal — a reviewer should see the defect by reading the file alone.

- [x] **Step 3: Run the tests and watch them fail**

Run: `uv run pytest tests/test_cartridge.py -v`
Expected: FAIL — `odoo_migrator.runner.cartridge` does not exist.

- [x] **Step 4: Implement the loader**

`odoo_migrator/runner/cartridge.py` validates with Pydantic and then runs the
cross-field rules that Pydantic cannot express: the flag vocabulary is
`BUILTIN_FLAGS` plus every flag any step declares in `raises`; a flag that is
declared and never read is a refusal, and so is one read and never raised.
The tool catalog is imported from `odoo_migrator/runner/tools.py`, which in
this task is a module holding only the names and the sides each tool is
allowed on:

```python
TOOL_SIDES = {
    "exec": ("old", "new"),
    "source": ("old", "new"),
    "journal": ("old", "new"),
    "history": ("old", "new"),
    "run_test": ("new",),
    "list_tests": ("old", "new"),
    "test_result": ("new",),
    "interrupt": ("old", "new"),
    "rollback": ("old", "new"),
    "reopen": ("new",),
    "read_file": ("old", "new"),
    "write_file": ("new",),
}
```

`sha256` is the digest of the file text as given, before parsing — the
identity of what the human uploaded, not of what Pydantic made of it.

- [x] **Step 5: Run the tests and watch them pass**

Run: `uv run pytest tests/test_cartridge.py -v`
Expected: PASS.

- [x] **Step 6: Write `cartridges/stock.yaml`**

Copy it verbatim from the spec's "The stock cartridge" section. Do not
reword the prompts: the spec is where they are reviewed.

- [x] **Step 7: Test that the shipped cartridge loads**

```python
def test_the_shipped_stock_cartridge_loads():
    from odoo_migrator.paths import repo_dir
    from odoo_migrator.runner.cartridge import load_cartridge_file

    loaded = load_cartridge_file(repo_dir() / "cartridges" / "stock.yaml")

    assert loaded.cartridge == "stock"
    assert loaded.step_by_id("understand") is not None
    assert loaded.on_failure.retry.resume_at == "lint"
```

- [x] **Step 8: Run the suite and commit**

```bash
uv run pytest && uv run ruff check
git add -A && git commit -m "feat: the cartridge language, its validation and the stock flow"
```

---

### Task 3: Sheller client verbs and the fake that answers like the daemon

**Files:**
- Modify: `odoo_migrator/sheller/client.py`, `odoo_migrator/sheller/fake.py`
- Test: `tests/test_sheller_client.py`

**Interfaces:**
- Consumes: nothing.
- Produces, on both the protocol and the fake:
  - `source(self, session_id: str, *, dotted: str, write_key: str) -> dict`
  - `journal(self, session_id: str) -> dict`
  - `feed(self, session_id: str, *, logs: bool = False) -> dict`
  - `list_tests(self, session_id: str, *, module: str, write_key: str) -> dict`
  - `interrupt(self, session_id: str, *, write_key: str) -> dict`

- [x] **Step 1: Write the failing tests**

```python
def test_the_feed_of_a_closed_session_still_answers(sheller):
    """Sheller journals outlive their session; the trace depends on it."""
    opened = sheller.open_session(
        container="c", database="d", odoo_bin="/odoo/odoo-bin",
        owner={"kind": "agent"}, allow_commit=False,
    )
    sheller.exec(opened["session_id"], "1 + 1", write_key=opened["write_key"])
    sheller.close(opened["session_id"], write_key=opened["write_key"])

    feed = sheller.feed(opened["session_id"])

    assert [entry["code"] for entry in feed["commands"]] == ["1 + 1"]


def test_interrupt_needs_the_write_key(sheller):
    opened = sheller.open_session(
        container="c", database="d", odoo_bin="/odoo/odoo-bin",
        owner={"kind": "agent"}, allow_commit=False,
    )

    with pytest.raises(ShellerError) as refused:
        sheller.interrupt(opened["session_id"], write_key="not-the-key")

    assert refused.value.status == 403
```

- [x] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_sheller_client.py -k "feed or interrupt" -v`
Expected: FAIL — the methods do not exist.

- [x] **Step 3: Implement the client methods**

Against the daemon's real routes: `GET /api/journals/{session_id}`, the
command feed endpoint with its `logs` and `log_tail` query parameters,
`POST /api/sessions/{id}/source`, `.../tests`, `.../interrupt`. Read
`/opt/projects/odoo-sheller/docs/container.md` and the daemon's `api.py`
for the exact paths and status codes before writing any of them; do not
invent a route.

- [x] **Step 4: Teach the fake to answer the same way**

The fake keeps a journal per session that survives `close`, refuses a wrong
or missing write key with 403 `not_owner` — measured against the live daemon
— and returns the same envelope shapes as the real routes. Where you are not
certain what the daemon returns, run it: there is a `live` pytest marker for
tests that talk to a real daemon, and the answer belongs in the fake, not in
a guess.

- [x] **Step 5: Run the suite and commit**

```bash
uv run pytest && uv run ruff check
git add -A && git commit -m "feat: source, journal, feed, list_tests and interrupt on the Sheller client"
```

---

### Task 4: The deterministic steps

**Files:**
- Create: `odoo_migrator/runner/steps/__init__.py`,
  `steps/lint.py`, `steps/upgrade.py`, `steps/tests.py`, `steps/verdict.py`,
  `steps/ask_human.py`
- Create: `odoo_migrator/runner/context.py` (the `StepContext` every step
  receives)
- Test: `tests/test_steps_deterministic.py`

**Interfaces:**
- Consumes: `Cartridge`, `Step` (Task 2); the Sheller client (Task 3).
- Produces:
  - `@dataclass class StepContext` — `job_id: str`, `attempt: int`,
    `migration: Migration`, `module: Module`, `cartridge: Cartridge`,
    `sheller`, `sessions: dict[str, str]`, `emit: Callable[[dict], None]`
  - `@dataclass class StepResult` — `ok: bool`, `flags: set[str]`,
    `answer: str = ""`, `output: str = ""`
  - `def run_step(step: Step, ctx: StepContext) -> StepResult` per module

- [ ] **Step 1: Write the failing tests**

```python
def test_lint_fails_a_manifest_that_is_not_python(tmp_path, step_context):
    (tmp_path / "__manifest__.py").write_text("{'name': 'x',", encoding="utf-8")
    ctx = step_context(new_module_path=tmp_path)

    result = lint.run(Step(kind="lint"), ctx)

    assert result.ok is False
    assert "lint_failed" in result.flags
    assert "__manifest__.py" in result.output


def test_tests_raises_no_tests_when_the_module_declares_none(step_context, sheller_fake):
    sheller_fake.tests_for("sale_custom", [])
    ctx = step_context()

    result = tests.run(Step(kind="tests"), ctx)

    assert result.ok is True
    assert result.flags == {"no_tests"}


def test_verdict_is_basic_by_default_and_reads_the_migration(step_context):
    ctx = step_context(strictness="strict")

    result = verdict.run(Step(kind="verdict"), ctx)

    assert result.ok is False  # strict wants a probe answer, and there is none
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_steps_deterministic.py -v`
Expected: FAIL — the package does not exist.

- [ ] **Step 3: Implement the five steps**

`lint` reads the written module with `ast.parse` for `.py` and
`xml.etree.ElementTree` for `.xml`, plus the manifest keys Odoo requires. It
touches no container — that is the point of running it before `upgrade`.

`upgrade` calls `exec` on the target session to install or upgrade the
module, then commits. `tests` uses `list_tests` and `run_test`, raising
`no_tests` when the list is empty and `tests_failed` when the report is red.
`verdict` reads `Module.strictness or Migration.strictness_default`.
`ask_human` writes the checkpoint and returns a result that parks the job —
the loop in Task 5 interprets that.

- [ ] **Step 4: Run and commit**

```bash
uv run pytest && uv run ruff check
git add -A && git commit -m "feat: lint, upgrade, tests, verdict and ask_human as code steps"
```

---

### Task 5: The loop

**Files:**
- Create: `odoo_migrator/runner/loop.py`, `odoo_migrator/runner/registry.py`
- Test: `tests/test_runner_loop.py`

**Interfaces:**
- Consumes: Tasks 2 and 4.
- Produces:
  - `def resolve(kind: str)` in `registry.py` — deterministic kinds only for
    now; `agent` raises `NotImplementedError` until Task 7
  - `def run_job(cartridge: Cartridge, ctx: StepContext, *, retry_limit: int)
    -> str` returning `"passed" | "failed" | "waiting_for_human"`

- [ ] **Step 1: Write the failing tests**

```python
def test_a_gated_step_is_skipped_when_its_flag_is_not_raised(loop_ctx):
    outcome, ran = run_recorded(CARTRIDGE_WITH_PROBE, loop_ctx(no_tests=False))

    assert "probe" not in ran
    assert outcome == "passed"


def test_the_retry_loop_actually_loops_and_stops_at_the_limit(loop_ctx):
    outcome, ran = run_recorded(ALWAYS_FAILS, loop_ctx(), retry_limit=3)

    assert ran.count("lint") == 3
    assert outcome == "failed"


def test_retry_resumes_where_resume_at_says(loop_ctx):
    outcome, ran = run_recorded(ALWAYS_FAILS, loop_ctx(), retry_limit=2)

    assert ran[:3] == ["port", "lint", "upgrade"]
    assert ran[ran.index("patch") + 1] == "lint"


def test_a_parked_job_stops_the_loop_without_spending_an_attempt(loop_ctx):
    outcome, ran = run_recorded(ASKS_A_HUMAN, loop_ctx(needs_human=True))

    assert outcome == "waiting_for_human"
    assert ran.count("diagnose") == 1
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_runner_loop.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement the loop**

Walk `pipeline` in order, skipping a step whose `when` flag is not set. On a
failing result, walk `on_failure.pipeline` the same way, then return to
`retry.resume_at` and increment the attempt, until `retry_limit`. A parked
step ends the loop immediately and leaves the job for Task 8 to resume.
Flags live for one lap and are cleared when an attempt starts, except that
`ask_human` parking does not clear anything.

- [ ] **Step 4: Run and commit**

```bash
uv run pytest && uv run ruff check
git add -A && git commit -m "feat: the runner loop, its branch and its retry edge"
```

---

### Task 6: The model client and the agent's tools

**Files:**
- Create: `odoo_migrator/runner/agent.py`, `odoo_migrator/runner/tools.py`
  (grown from Task 2's name table into real tool functions)
- Create: `tests/doubles/model.py`
- Modify: `pyproject.toml` — add `anthropic>=0.40`
- Modify: `odoo_migrator/models.py`, `odoo_migrator/settings.py` — an
  `anthropic_api_key` field on `AppSettings`
- Test: `tests/test_agent.py`, `tests/test_tools.py`

**Interfaces:**
- Consumes: Tasks 2–4.
- Produces:
  - `def tools_for(step: Step, ctx: StepContext) -> list` — the concrete
    callables, each bound to the sessions the step declared
  - `def report_tool(step: Step)` — schema is one boolean per declared flag
    plus `answer: str`
  - `def run_agent(step: Step, ctx: StepContext, *, client, model: str,
    budget: Budget) -> StepResult`
  - `class ModelDouble` in `tests/doubles/model.py` — replays scripted turns

- [ ] **Step 1: Write the failing tests**

```python
def test_a_tool_cannot_reach_a_session_the_step_did_not_declare(step_context):
    step = Step(kind="agent", id="probe", sessions=["new"], tools=["exec"],
                prompt="x")
    tools = tools_for(step, step_context())
    exec_tool = next(t for t in tools if t.name == "exec")

    with pytest.raises(ToolRefused) as refused:
        exec_tool(side="old", code="env.cr.execute('select 1')")

    assert "old" in str(refused.value)


def test_the_report_tool_schema_is_the_declared_flags():
    step = Step(kind="agent", id="diagnose", sessions=["new"], tools=["exec"],
                raises=["needs_human", "small_fix"], prompt="x")

    schema = report_tool(step).input_schema

    assert set(schema["properties"]) == {"answer", "needs_human", "small_fix"}


def test_a_step_that_never_converges_ends_at_the_budget(step_context):
    """A double that always asks for another tool call must not hang a run."""
    model = ModelDouble(always_calls="exec")

    result = run_agent(AGENT_STEP, step_context(), client=model,
                       model="claude-opus-5", budget=Budget(tool_calls=5))

    assert result.ok is False
    assert "budget" in result.output.lower()
    assert model.calls == 5
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_agent.py tests/test_tools.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement the tools**

Each tool takes `side` and never a container name: the container comes from
the migration row, which is what confines the agent to two of them. A tool
whose side was not declared by the step raises `ToolRefused` before any HTTP
happens. `write_file` and `read_file` resolve inside the target project root
and refuse a path that escapes it — a 403-shaped refusal, not a clamp.
`reopen` closes the target session and opens a fresh one, recording the new
id on the context.

- [ ] **Step 4: Implement the agent turn**

Use the SDK's beta tool runner: `@beta_tool` on the tool callables and
`client.beta.messages.tool_runner(...)`, `runner.until_done()`. Adaptive
thinking (`thinking={"type": "adaptive"}`), effort from settings, streaming
for long turns. The step ends when the model calls `report`; it also ends,
unsuccessfully, at the budget. Read `python/claude-api/README.md` from the
`claude-api` skill before writing this file, and do not guess SDK names.

- [ ] **Step 5: Write the model double**

`ModelDouble` replays scripted turns: a turn that calls a tool, a turn that
calls `report`, a turn that returns nothing useful, and a mode that never
converges. It must be able to fail — a double that always succeeds proves the
loop compiles, not that it terminates.

- [ ] **Step 6: Run and commit**

```bash
uv run pytest && uv run ruff check
git add -A && git commit -m "feat: the agent turn, its tool catalog and a model double that can fail"
```

---

### Task 7: The generic agent step

**Files:**
- Modify: `odoo_migrator/runner/registry.py` (resolve `agent`),
  `odoo_migrator/runner/context.py` (build the prompt context)
- Create: `odoo_migrator/runner/prompting.py`
- Test: `tests/test_agent_step.py`

**Interfaces:**
- Consumes: Tasks 2, 5, 6.
- Produces: `def build_context(step: Step, ctx: StepContext) -> str` —
  renders only the declared `context` items, in the order of `CONTEXT_ITEMS`.

- [ ] **Step 1: Write the failing tests**

```python
def test_rewrite_is_not_given_the_previous_attempt(agent_ctx):
    """The patch/rewrite distinction is the cartridge's, not the code's."""
    step = Step(kind="agent", id="rewrite", sessions=["old", "new"],
                tools=["write_file"], context=["rules", "module_source"],
                prompt="again")

    rendered = build_context(step, agent_ctx(previous_diff="--- a/models.py"))

    assert "--- a/models.py" not in rendered
    assert "module_source" in rendered.lower() or "models.py" in rendered


def test_patch_is_given_it(agent_ctx):
    step = Step(kind="agent", id="patch", sessions=["new"],
                tools=["write_file"], context=["previous_diff"], prompt="fix")

    rendered = build_context(step, agent_ctx(previous_diff="--- a/models.py"))

    assert "--- a/models.py" in rendered
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_agent_step.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`rules` renders the assembled per-version stack the app already builds.
`prior_answers` renders each earlier agent step's `id` and answer for this
job. The model for a step is resolved by the spec's precedence: the step's
own `model`, else the migration card's override, else the cartridge's.

- [ ] **Step 4: Run the stock cartridge end to end against doubles**

```python
def test_the_stock_cartridge_runs_to_passed_against_doubles(loop_ctx):
    outcome, ran = run_recorded_file("cartridges/stock.yaml", loop_ctx())

    assert outcome == "passed"
    assert ran[:4] == ["understand", "port", "lint", "upgrade"]
```

- [ ] **Step 5: Run and commit**

```bash
uv run pytest && uv run ruff check
git add -A && git commit -m "feat: the cartridge's agent steps run"
```

---

### Task 8: `NativeRunner`, persistence and events

**Files:**
- Create: `odoo_migrator/runner/native.py`
- Modify: `odoo_migrator/models.py` (`StepRun`, `ToolCall`; `Job.answer`
  becomes a thread), `odoo_migrator/jobs.py` (`answer_job` resumes),
  `odoo_migrator/api.py` (choose the executor)
- Test: `tests/test_native_runner.py`, `tests/test_hitl.py`

**Interfaces:**
- Consumes: Tasks 2–7.
- Produces:
  - `class NativeRunner` with `start(self, payload: dict) -> None`, running
    the job in a `threading.Thread`
  - `class StepRun(BaseModel)` — job, attempt, step id, kind, started_at,
    finished_at, ok, flags, tokens_in, tokens_out, cost_cents
  - `class ToolCall(BaseModel)` — step_run, tool, side, session_id,
    command_id, duration_ms, ok, preview
  - SSE events named `step_started`, `step_finished`, `tool_called`,
    `attempt_started`, `job_finished`

- [ ] **Step 1: Write the failing tests**

```python
def test_a_run_records_the_session_of_every_tool_call(client, native_job):
    """A trace line finds its journal entry by session id and command id.

    A reopen mid-step makes a new session, so this cannot be stored per step.
    """
    run_to_completion(native_job)

    with database.connection_context():
        calls = list(ToolCall.select().order_by(ToolCall.id))

    assert {c.session_id for c in calls if c.side == "new"} > {""}
    assert len({c.session_id for c in calls if c.side == "new"}) >= 2
    assert all(c.command_id for c in calls if c.tool == "exec")


def test_an_answer_resumes_a_parked_job(client, parked_job):
    answer_job(module_id=parked_job.module_id, answer="drop the override",
               hub=hub)

    assert reloaded(parked_job).status == "running"


def test_the_thread_never_shares_a_connection(native_job):
    """Peewee rule: a connection never crosses a thread boundary."""
    run_to_completion(native_job)

    assert not database.is_closed() is False  # main thread untouched
```

- [ ] **Step 2: Run them and watch them fail**

Run: `uv run pytest tests/test_native_runner.py -v`
Expected: FAIL.

- [ ] **Step 3: Implement**

`NativeRunner.start` spawns one thread per job. Inside it, every database
operation opens its own `connection_context()`. Progress reaches the app
through the same `apply_progress` / `apply_result` functions, called
directly. The schema change is a `playhouse.migrate` step.

- [ ] **Step 4: Make the parked answer a thread**

`Job.answer` (a string) becomes `CheckpointMessage` rows — author
(`agent` | `human`), text, created_at. The checkpoint screen renders the
thread. Only one human message is written for now; the shape is what matters.

- [ ] **Step 5: Run and commit**

```bash
uv run pytest && uv run ruff check
git add -A && git commit -m "feat: the native runner, its records and its events"
```

---

### Task 9: Cartridge identity, import and smoke run

**Files:**
- Modify: `odoo_migrator/cartridges.py` (rewritten for YAML),
  `odoo_migrator/models.py` (`CartridgeInstall`: drop `webhook_path` and
  `contract_version`, add `sha256` and `schema_version`),
  `odoo_migrator/web/templates/migration_settings.html`
- Test: `tests/test_cartridge_install.py`

**Interfaces:**
- Produces: `def install_from_text(text: str) -> CartridgeInstall`,
  `def pinned_for(migration) -> Cartridge`, `def smoke_run(migration, module)`

- [ ] **Step 1: Write the failing test**

```python
def test_a_run_records_the_cartridge_it_used(client, migration):
    install = install_from_text(STOCK_TEXT)
    job = start_job(migration, module, cartridge=install)

    assert job.cartridge_id == "stock"
    assert job.cartridge_version == "1.0.0"
    assert job.cartridge_sha256 == install.sha256


def test_an_invalid_upload_is_refused_before_it_is_stored(client, migration):
    page = client.post(f"/migrations/{migration.id}/cartridge",
                       files={"file": ("bad.yaml", b"pipeline: [{step: nope}]")},
                       follow_redirects=True)

    assert "step" in page.text
    with database.connection_context():
        assert CartridgeInstall.select().count() == 0
```

- [ ] **Steps 2–5:** fail, implement, pass, commit as in previous tasks.

```bash
git commit -m "feat: import, pin and smoke-run a YAML cartridge"
```

---

### Task 10: The `Flow` screen — the drawing

**Files:**
- Create: `odoo_migrator/web/templates/flow.html`,
  `odoo_migrator/web/static/flow.js`
- Modify: `api.py` (`/flow` replaces `/n8n`), `base.html`, `style.css`,
  `components.html`, `styleguide.html`
- Test: `tests/test_flow_screen.py`

**Interfaces:**
- Produces: `GET /flow` and `GET /api/flow/{migration_id}/graph` returning
  `{nodes: [{id, kind, lane, gated_on}], edges: [{from, to, kind}]}`

- [ ] **Step 1: Write the failing test**

```python
def test_the_graph_is_derived_from_the_cartridge_not_stored(client, migration):
    graph = client.get(f"/api/flow/{migration.id}/graph").json()

    assert [n["id"] for n in graph["nodes"]][:3] == ["understand", "port", "lint"]
    assert {"from": "patch", "to": "lint", "kind": "retry"} in graph["edges"]
    assert all("position" not in node for node in graph["nodes"])


def test_flow_draws_the_pinned_cartridge_with_nothing_running(client, migration):
    page = client.get("/flow").text

    assert "stock" in page and "1.0.0" in page
```

- [ ] **Steps 2–5:** fail, implement with Cytoscape and a computed preset
  layout, pass, add the `/components` row, commit.

```bash
git commit -m "feat: the Flow screen draws the pinned cartridge"
```

---

### Task 11: The `Flow` screen — the painting and the trace

**Files:**
- Modify: `flow.html`, `flow.js`, `style.css`, `api.py`
- Test: `tests/test_flow_live.py`

**Interfaces:**
- Produces: `GET /api/flow/{job_id}/trace?attempt=N` (the skeleton rows) and
  `GET /api/flow/trace/{tool_call_id}/body` (fetches the cell from Sheller's
  journal on demand)

- [ ] **Step 1: Write the failing tests**

```python
def test_a_trace_line_survives_a_journal_that_is_gone(client, tool_call, sheller_fake):
    sheller_fake.forget_journal(tool_call.session_id)

    body = client.get(f"/api/flow/trace/{tool_call.id}/body")

    assert body.status_code == 200
    assert "not available" in body.text


def test_the_trace_is_the_skeleton_and_not_the_bodies(client, finished_job):
    rows = client.get(f"/api/flow/{finished_job.job_id}/trace").json()

    assert rows[0]["session_id"]
    assert len(rows[0]["preview"]) <= 120
    assert "stdout" not in rows[0]
```

- [ ] **Steps 2–5:** fail, implement, pass, commit. The body expands on an
  explicit click only — journals are unmasked, and this tool is demonstrated
  on a projector.

```bash
git commit -m "feat: the live trace, with bodies fetched from Sheller's journal"
```

---

### Task 12: The Pipeline ribbon and the checkpoint thread

**Files:**
- Modify: `run.html`, `pipeline.js`, `style.css`, `result.html`,
  `components.html`
- Test: `tests/test_run_workspace.py`, `tests/test_checkpoint.py`

- [ ] **Step 1: Write the failing tests**

```python
def test_the_pipeline_live_panel_is_a_ribbon_of_the_cartridge(client, live_job):
    page = client.get("/run").text

    assert 'data-ribbon' in page
    assert page.count('class="ribbon-node') >= 7


def test_a_checkpoint_shows_the_thread(client, parked_job):
    page = client.get("/run").text

    assert "checkpoint-thread" in page
    assert parked_job.question in page
```

- [ ] **Steps 2–5:** fail, implement, pass, add the `/components` rows,
  commit.

```bash
git commit -m "feat: the Pipeline ribbon and the checkpoint thread"
```

---

### Task 13: One real module, end to end

This task has no unit test. It is the gate that lets the branch merge.

- [ ] **Step 1: Bring up both containers**

Use the migration card's compose actions for the source and target
containers of a real project. Confirm on `/` that both report running.

- [ ] **Step 2: Resolve the module graph and queue one leaf module**

Pick a module with no dependants and no tests — it exercises `probe` and
`write_test`, the two steps that only run when there are none.

- [ ] **Step 3: Watch `Flow`**

Expected, and each is a thing to write down rather than assume:
- the canvas paints in order and the active node is obvious;
- the trace shows `exec` calls with both side chips;
- a trace body opens from Sheller's journal;
- the attempt switcher appears if there is a second attempt.

- [ ] **Step 4: Check the two invariants by hand**

```bash
curl -s http://127.0.0.1:8765/api/sessions | python3 -m json.tool
```

Expected: the source session shows `"allow_commit": false`, the target
session `true`, both `"owner": {"kind": "agent"}` carrying the migration id.

- [ ] **Step 5: Confirm the module actually works on the new version**

Open the target container and exercise the module by hand — the recipe from
`understand` is in the attempt, so replay it. The runner's own verdict is
not evidence for this gate; a human looking at the result is.

- [ ] **Step 6: Report, do not merge**

Write what happened — including what was wrong — and stop. Merging to `main`
is the human's decision, and the spec says `main` stays the working n8n build
until this passes.

---

## Self-review notes

**Spec coverage.** Every section of the spec has a task: the language and
validation (2), the generic agent step (6, 7), the tool catalog and
containment (6), the fixture recipe (it is prompt text in `cartridges/
stock.yaml`, Task 2), sessions and commit policy (4, 6, 13), the checkpoint
thread (8), the screens (10, 11, 12), the storage split (8, 11), demolition
(1), testing doubles (3, 6).

**Deliberately not in this plan**, and listed in the spec's open questions:
assisted answers, the node inspector, a two-column `probe` reading, a canvas
wider than one row, a cartridge editor, and the policy for what a prompt may
carry off the machine.

**Known ordering risk.** Task 1 leaves `answer_job` storing an answer that
nothing resumes until Task 8. That intermediate state is intentional and
tested as such; do not "fix" it in Task 1 by inventing a resume path.
