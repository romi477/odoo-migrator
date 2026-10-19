"""The pipeline review: every place a live run broke that the doubles hid."""

from pathlib import Path
from types import SimpleNamespace

import httpx2
import pytest

from odoo_migrator.db import database, init_db
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Migration, Module
from odoo_migrator.runner import sessions as session_helpers
from odoo_migrator.runner.cartridge import Budget, CartridgeError, Step, load_cartridge
from odoo_migrator.runner.context import StepContext, StepResult
from odoo_migrator.runner.loop import run_job
from odoo_migrator.runner.steps import lint, tests, upgrade, verdict
from odoo_migrator.runner.tools import ToolRefused, tools_for
from odoo_migrator.sheller import FakeShellerClient, HttpShellerClient
from odoo_migrator.sheller.errors import ShellerBusy, ShellerSessionDead, ShellerTimeout

_MINIMAL = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-sonnet-5-5}
pipeline:
  - step: verdict
on_failure:
  pipeline: []
  retry:
    resume_at: verdict
"""


@pytest.fixture(autouse=True)
def no_waiting(monkeypatch):
    monkeypatch.setattr(session_helpers, "POLL_SECONDS", 0)


@pytest.fixture
def sheller():

    return FakeShellerClient()


@pytest.fixture
def ctx_for(tmp_path, sheller):
    init_db(tmp_path / "review.sqlite")

    def build(*, project="new", target_version="18.0"):
        new_root = tmp_path / project
        module_dir = new_root / "sale_custom"
        module_dir.mkdir(parents=True, exist_ok=True)
        created = create_migration(
            name="Acme",
            accent="violet",
            source_version="17.0",
            target_version=target_version,
            old_project_path=str(tmp_path / "old"),
            new_project_path=str(new_root),
        )
        with database.connection_context():
            migration = Migration.get_by_id(created["id"])
            module = Module.create(
                migration=migration, technical_name="sale_custom", new_path=str(module_dir)
            )
        targets = {
            side: {
                "container": container,
                "database": "acme",
                "odoo_bin": "/opt/odoo/odoo-bin",
                "owner": {"kind": "agent", "migration_id": migration.id},
            }
            for side, container in (("source", "odoo-old"), ("target", "odoo-new"))
        }
        ctx = StepContext(
            job_id="job-1",
            attempt=1,
            migration=migration,
            module=module,
            cartridge=load_cartridge(_MINIMAL),
            sheller=sheller,
            sessions={},
            session_targets=targets,
            emit=lambda _event: None,
        )
        for side in targets:
            session_helpers.open_side(ctx, side)

        return ctx

    return build


# -- tests: the daemon's report, read whole --------------------------------


def _seed_one_test(sheller):
    sheller.tests_for(
        "sale_custom", sheller.catalogue("sale_custom", {"TestSale": ["test_total"]})
    )


def test_a_failing_test_fails_the_step(ctx_for, sheller):
    """The daemon says red with `error: None` and `test.success: false`. The
    step looked for `ok` and `status`, which it never sends, and passed."""
    _seed_one_test(sheller)
    sheller.test_outcomes["sale_custom.TestSale.test_total"] = {
        "success": False,
        "failures": 1,
        "stderr": ["AssertionError: 10 != 12"],
    }

    result = tests.run(Step(kind="tests"), ctx_for())

    assert result.ok is False
    assert result.flags == {"tests_failed"}
    assert "1 failure(s)" in result.output
    assert "AssertionError: 10 != 12" in result.output


def test_a_run_of_zero_tests_is_not_a_pass(ctx_for, sheller):
    _seed_one_test(sheller)
    sheller.test_outcomes["sale_custom.TestSale.test_total"] = {"tests_run": 0}

    result = tests.run(Step(kind="tests"), ctx_for())

    assert result.ok is False
    assert "ran 0 tests" in result.output


def test_the_step_gives_a_test_time_to_run(ctx_for, sheller):
    _seed_one_test(sheller)

    assert tests.run(Step(kind="tests"), ctx_for()).ok is True
    assert sheller.tests_run[0]["timeout"] >= 300


def _http(handler) -> HttpShellerClient:

    return HttpShellerClient("http://sheller", transport=httpx2.MockTransport(handler))


def test_the_http_call_outlasts_the_test_it_waits_for(monkeypatch):
    """The client default was 30s against the daemon's 300."""
    seen = {}
    client = _http(lambda request: httpx2.Response(200, json={}))
    real = client._http.request

    def spy(method, path, **kwargs):
        seen.update(kwargs)

        return real(method, path, **kwargs)

    monkeypatch.setattr(client._http, "request", spy)
    client.run_test("s1", "m.TestX", write_key="k")
    assert seen["timeout"] > 300
    client.run_test("s1", "m.TestX", write_key="k", timeout=1800)
    assert seen["timeout"] > 1800


def test_busy_and_the_daemons_own_timeout_are_their_own_errors():
    busy = _http(lambda request: httpx2.Response(409, json={"detail": "session busy"}))
    with pytest.raises(ShellerBusy):
        busy.exec("s1", "1", write_key="k")
    late = _http(lambda request: httpx2.Response(504, json={"detail": "timed out"}))
    with pytest.raises(ShellerTimeout):
        late.exec("s1", "1", write_key="k")


# -- source: Sheller's snippet, not a dotted name sent as Python -------------


def test_source_runs_the_read_snippet_and_parses_it(ctx_for, sheller):
    ctx = ctx_for()
    step = Step(kind="agent", id="port", sessions=["target"], tools=["source"], prompt="p")
    (source,) = tools_for(step, ctx)

    answer = source(side="target", model="sale.order", method="action_confirm")

    sent = sheller.execs[-1]
    assert "_os_read()" in sent
    assert "type(env['sale.order'])" in sent
    assert "action_confirm" in answer
    assert "NameError" not in answer


def test_source_refuses_what_the_daemon_would_refuse(ctx_for):
    ctx = ctx_for()
    step = Step(kind="agent", id="port", sessions=["target"], tools=["source"], prompt="p")
    (source,) = tools_for(step, ctx)

    assert "nothing_to_read" in source(side="target")
    assert "method_without_model" in source(side="target", path="sale", method="x")


def test_source_says_why_a_read_failed(ctx_for, sheller):
    sheller.exec_answers["_os_read()"] = {
        "error": {"type": "KeyError", "message": "'no.such.model'", "traceback": ""}
    }
    ctx = ctx_for()
    step = Step(kind="agent", id="port", sessions=["target"], tools=["source"], prompt="p")
    (source,) = tools_for(step, ctx)

    answer = source(side="target", model="no.such.model")

    assert "unreadable" in answer
    assert "KeyError" in answer


# -- upgrade: a fresh process, the list refreshed, a failure cleaned up ------


def test_upgrade_runs_in_a_fresh_session(ctx_for, sheller):
    """The session open since the job started imported the old Python."""
    ctx = ctx_for()
    before = ctx.sessions["target"]

    result = upgrade.run(Step(kind="upgrade"), ctx)

    assert result.ok is True
    assert ctx.sessions["target"] != before
    assert before in sheller.closes
    code = next(code for code in sheller.execs if "button_immediate" in code)
    assert "update_list()" in code
    assert code.index("update_list()") < code.index("button_immediate")
    assert ctx.sessions["target"] in sheller.commits


def test_upgrade_is_a_line_of_its_own_in_the_run_log(ctx_for, sheller):
    """It said only "ok", and a four-second upgrade read as nothing having
    happened. Now it is a tool call, the way a test run is: the time it took,
    what was done, and the command to open in Sheller's journal."""
    sheller.exec_answers["button_immediate"] = {"result": "('upgraded', 'installed')"}
    calls = []
    ctx = ctx_for()
    ctx.record_tool = lambda *args: calls.append(args)

    result = upgrade.run(Step(kind="upgrade"), ctx)

    assert result.ok is True
    assert result.output == "sale_custom: upgraded, was installed"
    ((tool, side, session_id, command_id, duration_ms, ok, preview),) = calls
    assert (tool, side, ok) == ("upgrade", "target", True)
    assert session_id == ctx.sessions["target"] and command_id
    assert duration_ms >= 0
    assert preview == "sale_custom · upgraded, was installed"


def test_a_failed_upgrade_is_a_failed_line_in_the_run_log(ctx_for, sheller):
    sheller.exec_answers["button_immediate"] = {
        "error": {"type": "ParseError", "message": "while parsing views.xml", "traceback": ""},
    }
    calls = []
    ctx = ctx_for()
    ctx.record_tool = lambda *args: calls.append(args)

    upgrade.run(Step(kind="upgrade"), ctx)

    assert calls[0][0] == "upgrade" and calls[0][5] is False
    assert "ParseError: while parsing views.xml" in calls[0][6]


def test_upgrade_drops_the_old_versions_migration_scripts_first(ctx_for, tmp_path):
    """Before Odoo reads the ported module: an old `migrations/17.0.*` would
    run on the new framework."""
    ctx = ctx_for()
    old_script = tmp_path / "new" / "sale_custom" / "migrations" / "17.0.1.0.3" / "pre-migrate.py"
    old_script.parent.mkdir(parents=True)
    old_script.write_text("def migrate(cr, version):\n    pass\n")

    result = upgrade.run(Step(kind="upgrade"), ctx)

    assert not (tmp_path / "new" / "sale_custom" / "migrations").exists()
    assert "migrations/17.0.1.0.3" in result.output


def test_upgrade_does_not_commit_what_the_agent_left_pending(ctx_for, sheller):
    """Closing the old session is what discards it; the upgrade's own commit
    then has nothing of the agent's to carry."""
    ctx = ctx_for()
    sheller.exec(ctx.sessions["target"], "env['res.partner'].create({})", write_key=ctx.write_keys["target"])
    stale = ctx.sessions["target"]

    upgrade.run(Step(kind="upgrade"), ctx)

    assert stale in sheller.closes
    assert stale not in sheller.commits


def test_a_failed_upgrade_puts_the_module_states_back(ctx_for, sheller):
    sheller.exec_answers["button_immediate"] = {
        "error": {
            "type": "ParseError",
            "message": "while parsing views.xml",
            "traceback": "Traceback\n  ...\nParseError: field 'x' does not exist",
        },
        "stderr": ["2026-09-27 10:00:00,001 7 ERROR acme odoo.modules.loading: boom"],
    }
    sheller.exec_answers["button_install_cancel"] = {"result": "['sale_custom']"}
    ctx = ctx_for()

    result = upgrade.run(Step(kind="upgrade"), ctx)

    assert result.ok is False
    assert "ParseError: field 'x' does not exist" in result.output
    assert "reset the states of: sale_custom" in result.output
    # The failed process is not the one diagnose gets.
    reset_session = ctx.sessions["target"]
    assert reset_session in sheller.commits
    assert ctx.log_errors


def test_an_upgrade_that_outlives_the_call_is_waited_for(ctx_for, sheller):
    """The daemon stops waiting at 300s; the upgrade carries on in the session."""
    sheller.hangs_on = "button_immediate"
    sheller.hangs_for = 3
    sheller.exec_answers["search_count"] = {"result": "('installed', 0)"}
    ctx = ctx_for()

    result = upgrade.run(Step(kind="upgrade"), ctx)

    assert result.ok is True
    assert "outlived" in result.output


def test_an_upgrade_that_outlives_the_call_and_did_not_finish_fails(ctx_for, sheller):
    sheller.hangs_on = "button_immediate"
    sheller.hangs_for = 1
    sheller.exec_answers["search_count"] = {"result": "('to upgrade', 1)"}

    result = upgrade.run(Step(kind="upgrade"), ctx_for())

    assert result.ok is False
    assert "did not finish" in result.output


# -- sessions: a dead or stuck one is mended before the next step ------------


def test_a_dead_session_is_reopened(ctx_for, sheller):
    ctx = ctx_for()
    dead = ctx.sessions["target"]
    sheller.kill(dead)
    with pytest.raises(ShellerSessionDead):
        sheller.exec(dead, "1", write_key=ctx.write_keys["target"])

    done = session_helpers.ensure_usable(ctx, "target")

    assert "dead" in done
    assert ctx.sessions["target"] != dead
    assert sheller.exec(ctx.sessions["target"], "1", write_key=ctx.write_keys["target"])["error"] is None


def test_a_busy_session_is_interrupted_and_kept(ctx_for, sheller):
    ctx = ctx_for()
    sheller.hangs_on = "search([])"
    with pytest.raises(ShellerTimeout):
        sheller.exec(ctx.sessions["target"], "env['x'].search([])", write_key=ctx.write_keys["target"])
    kept = ctx.sessions["target"]

    done = session_helpers.ensure_usable(ctx, "target")

    assert "interrupted" in done
    assert ctx.sessions["target"] == kept
    assert kept in sheller.interrupts


def test_a_ready_session_is_left_alone(ctx_for):
    ctx = ctx_for()
    kept = ctx.sessions["target"]

    assert session_helpers.ensure_usable(ctx, "target") == ""
    assert ctx.sessions["target"] == kept


# -- verdict: the baseline is no errors in the log ---------------------------


_RECORDS = [
    "2026-09-27 10:00:00,001 7 INFO acme odoo.modules.loading: loading",
    "2026-09-27 10:00:00,002 7 ERROR acme odoo.addons.base: view invalid",
]


@pytest.mark.parametrize("level", ["enhanced", "strict"])
def test_an_error_in_the_log_fails_the_lap_above_basic(ctx_for, level):
    ctx = ctx_for()
    ctx.migration.strictness_default = level
    ctx.prior_answers["probe"] = "same on both sides"
    verdict.note_log_errors(ctx, _RECORDS)

    result = verdict.run(Step(kind="verdict"), ctx)

    assert result.ok is False
    assert "view invalid" in result.output
    assert "loading" not in result.output
    assert ctx.findings == []


def test_at_basic_an_error_in_the_log_is_a_finding(ctx_for):
    """On a shared database the record is often another module's."""
    ctx = ctx_for()
    verdict.note_log_errors(ctx, _RECORDS)

    result = verdict.run(Step(kind="verdict"), ctx)

    assert result.ok is True
    assert len(ctx.findings) == 1
    assert "view invalid" in ctx.findings[0]["text"]
    assert ctx.findings[0]["source"] == "verdict: odoo log"


@pytest.mark.parametrize(
    "line",
    [
        "Traceback: psycopg2.errors.ERROR during the query",
        "    raise UserError('ERROR in the wizard')",
        "FAIL: test_total (ERROR expected) ",
        "odoo.sql_db: ERROR bad query",
    ],
)
def test_the_word_alone_is_not_a_log_record(ctx_for, line):
    ctx = ctx_for()

    verdict.note_log_errors(ctx, [line])

    assert ctx.log_errors == []


def test_a_coloured_record_still_counts(ctx_for):
    ctx = ctx_for()

    verdict.note_log_errors(
        ctx, ["2026-09-27 10:00:00,002 7 \x1b[1;31mERROR\x1b[0m acme odoo.sql_db: bad query"]
    )

    assert len(ctx.log_errors) == 1


def test_a_clean_lap_passes(ctx_for):

    assert verdict.run(Step(kind="verdict"), ctx_for()).ok is True


# -- lint --------------------------------------------------------------------


def test_lint_reports_a_file_that_is_not_utf8(ctx_for):
    from pathlib import Path

    ctx = ctx_for()
    root = ctx.module.new_path
    (Path(root) / "__manifest__.py").write_text("{'name': 'x', 'depends': []}", encoding="utf-8")
    (Path(root) / "legacy.py").write_bytes(b"# \xff\xfe latin-1\n")

    result = lint.run(Step(kind="lint"), ctx)

    assert result.ok is False
    assert "legacy.py: not UTF-8" in result.output


@pytest.mark.parametrize(
    ("manifest", "expected"),
    [
        ("{'name': 'x', 'depends': [], 'installable': False}", "installable is False"),
        ("{'name': 'x', 'depends': [], 'version': '17.0.1.0.0'}", "is for 17.0"),
    ],
)
def test_lint_reads_what_odoo_will_refuse_in_a_manifest(ctx_for, manifest, expected):
    from pathlib import Path

    ctx = ctx_for()
    (Path(ctx.module.new_path) / "__manifest__.py").write_text(manifest, encoding="utf-8")

    result = lint.run(Step(kind="lint"), ctx)

    assert result.ok is False
    assert expected in result.output


def test_lint_accepts_the_target_series(ctx_for):
    from pathlib import Path

    ctx = ctx_for()
    (Path(ctx.module.new_path) / "__manifest__.py").write_text(
        "{'name': 'x', 'depends': [], 'version': '18.0.1.0.0'}", encoding="utf-8"
    )

    assert lint.run(Step(kind="lint"), ctx).ok is True


# -- tools -------------------------------------------------------------------


def test_a_project_path_with_a_tilde_is_the_home_directory(ctx_for, monkeypatch, tmp_path):
    home = tmp_path / "home"
    (home / "proj").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    ctx = ctx_for()
    ctx.migration.new_project_path = "~/proj"
    step = Step(kind="agent", id="port", sessions=["target"], tools=["write_file"], prompt="p")
    (write,) = tools_for(step, ctx)

    written = write(side="target", path="sale_custom/x.py", content="x = 1\n")

    assert written == str((home / "proj" / "sale_custom" / "x.py").resolve())


def test_our_own_value_error_is_not_handed_to_the_model(ctx_for, monkeypatch):
    ctx = ctx_for()
    step = Step(kind="agent", id="port", sessions=["target"], tools=["exec"], prompt="p")
    (run,) = tools_for(step, ctx)

    def broken(*_args, **_kwargs):
        raise ValueError("a bug in the runner")

    monkeypatch.setattr("odoo_migrator.runner.tools._dispatch", broken)

    with pytest.raises(ValueError, match="a bug in the runner"):
        run(side="target", code="1")


def test_reading_a_binary_file_is_an_answer_not_a_crash(ctx_for):
    from pathlib import Path

    ctx = ctx_for()
    (Path(ctx.module.new_path) / "logo.png").write_bytes(b"\x89PNG\xff\xfe")
    step = Step(kind="agent", id="port", sessions=["target"], tools=["read_file"], prompt="p")
    (read,) = tools_for(step, ctx)

    assert read(side="target", path="sale_custom/logo.png").startswith("error:")


# -- the loop: a person's answer, the lane's failures, repeats ---------------

_ASKS = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-sonnet-5-5}
pipeline:
  - step: agent
    id: port
    sessions: [target]
    tools: [exec]
    context: [prior_answers]
    prompt: port
on_failure:
  pipeline:
    - step: agent
      id: diagnose
      sessions: [target]
      tools: [exec]
      context: [prior_answers]
      raises: [needs_human]
      prompt: why
    - step: ask_human
      when: needs_human
  retry:
    resume_at: port
"""


class _Scripted:
    """Answers each step in turn, and keeps every prompt it was given."""

    def __init__(self, results):
        self.results = list(results)
        self.prompts: list[str] = []

    def ensure_ready(self):

        return None

    def run_step(self, *, prompt, **_kwargs):
        self.prompts.append(prompt)

        return self.results.pop(0)


def test_a_persons_answer_reaches_the_next_prompt(ctx_for):
    ctx = ctx_for()
    ctx.cartridge = load_cartridge(_ASKS)
    ctx.wait_for_human = lambda question: "use the v18 tax API"
    provider = _Scripted(
        [
            StepResult(ok=False, output="port failed"),
            StepResult(ok=True, answer="which API?", flags={"needs_human"}),
            StepResult(ok=True, answer="ported"),
        ]
    )
    ctx.provider = provider

    outcome = run_job(ctx.cartridge, ctx, retry_limit=3)

    assert outcome == "passed"
    assert ctx.prior_answers["ask_human"] == "use the v18 tax API"
    assert "use the v18 tax API" in provider.prompts[-1]


def test_the_same_failure_with_new_addresses_is_still_the_same(ctx_for):
    """A traceback never repeats byte for byte; the check never fired."""
    lane = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-sonnet-5-5}
pipeline:
  - step: agent
    id: port
    sessions: [target]
    tools: [exec]
    prompt: port
on_failure:
  pipeline:
    - step: agent
      id: diagnose
      sessions: [target]
      tools: [exec]
      prompt: why
  retry:
    resume_at: port
"""
    ctx = ctx_for()
    ctx.cartridge = load_cartridge(lane)
    ctx.provider = _Scripted(
        [
            StepResult(ok=False, output="<Record object at 0x10a2b3c4> took 12.5s"),
            StepResult(ok=True, answer="no idea"),
            StepResult(ok=False, output="<Record object at 0x7f00ee11> took 13.1s"),
            StepResult(ok=True, answer="no idea"),
            StepResult(ok=False, output="must not get this far"),
        ]
    )

    outcome = run_job(ctx.cartridge, ctx, retry_limit=5)

    assert outcome == "failed"
    assert ctx.attempt == 2
    assert "changed nothing" in ctx.last_failure


def test_a_lane_step_that_failed_says_so_in_the_outcome(ctx_for):
    lane = _ASKS.replace("    - step: ask_human\n      when: needs_human\n", "")
    ctx = ctx_for()
    ctx.cartridge = load_cartridge(lane.replace("raises: [needs_human]\n      ", ""))
    ctx.provider = _Scripted(
        [
            StepResult(ok=False, output="port failed"),
            StepResult(ok=False, output="stopped at the token budget"),
            StepResult(ok=False, output="port failed"),
            StepResult(ok=False, output="stopped at the token budget"),
        ]
    )

    assert run_job(ctx.cartridge, ctx, retry_limit=5) == "failed"
    assert "diagnose failed: stopped at the token budget" in ctx.last_failure


# -- providers ---------------------------------------------------------------


def _anthropic(monkeypatch, turns):
    import anthropic

    from odoo_migrator.runner.providers.anthropic import AnthropicProvider

    seen = {}

    class _Messages:
        def tool_runner(self, **kwargs):
            seen.update(kwargs)

            return iter(turns)

    class _Client:
        def __init__(self, api_key=None):
            self.beta = SimpleNamespace(messages=_Messages())

    monkeypatch.setattr(anthropic, "Anthropic", _Client)
    monkeypatch.setattr("odoo_migrator.runner.providers.anthropic._key", lambda: "k")

    return AnthropicProvider(), seen


def _turn(*, inp=0, out=0, read=0, written=0, content=()):
    message = SimpleNamespace(
        content=list(content),
        usage=SimpleNamespace(
            input_tokens=inp,
            output_tokens=out,
            cache_read_input_tokens=read,
            cache_creation_input_tokens=written,
        ),
    )

    return SimpleNamespace(get_final_message=lambda: message)


def _run(provider, budget=None):
    from odoo_migrator.runner.tools import report_tool

    step = Step(kind="agent", id="port", sessions=["target"], raises=[], prompt="p")

    return provider.run_step(
        system="s", prompt="p", tools=[], report=report_tool(step),
        model="claude-sonnet-5-5", budget=budget or Budget(),
    )


def test_the_conversation_is_cached(monkeypatch):
    provider, seen = _anthropic(monkeypatch, [_turn(inp=10)])

    _run(provider)

    assert seen["cache_control"] == {"type": "ephemeral"}


def test_cache_reads_do_not_spend_the_budget(monkeypatch):
    """Five turns re-reading a 40K prefix is not 200K of new work."""
    turns = [_turn(inp=500, written=500, out=100, read=40_000) for _ in range(5)]
    provider, _ = _anthropic(monkeypatch, turns)

    result = _run(provider, Budget(tokens=10_000))

    assert "token budget" not in result.output
    assert result.tokens_in == 5000
    assert result.tokens_cached == 200_000


def test_the_step_ends_at_report(monkeypatch):
    report = SimpleNamespace(type="tool_use", name="report", input={"answer": "ported"})
    turns = [_turn(inp=10, content=[report]), _turn(inp=99_999)]
    provider, _ = _anthropic(monkeypatch, turns)

    result = _run(provider)

    assert result.ok is True
    assert result.answer == "ported"
    assert result.tokens_in == 10


# -- cartridge: a step that starts over --------------------------------------


def test_fresh_is_only_for_a_step_that_writes():
    text = _MINIMAL.replace("  - step: verdict\n", "  - step: verdict\n    fresh: true\n")
    with pytest.raises(CartridgeError, match="fresh"):
        load_cartridge(text)


def test_the_stock_rewrite_starts_from_an_empty_directory():
    from pathlib import Path

    from odoo_migrator.runner.cartridge import load_cartridge_file

    stock = load_cartridge_file(Path("cartridges/stock.yaml"))

    assert stock.step_by_id("rewrite").fresh is True
    assert stock.step_by_id("patch").fresh is False


# -- the native runner ---------------------------------------------------------


def _runner(sheller):
    from odoo_migrator.events import EventHub
    from odoo_migrator.runner.native import NativeRunner

    return NativeRunner(sheller=sheller, hub=EventHub())


def test_a_fresh_step_starts_from_an_empty_module(ctx_for, sheller):
    from pathlib import Path

    ctx = ctx_for()
    root = Path(ctx.module.new_path)
    (root / "__manifest__.py").write_text("{'name': 'x', 'depends': []}", encoding="utf-8")
    (root / "models").mkdir()
    (root / "models" / "old_attempt.py").write_text("x = 1\n", encoding="utf-8")

    _runner(sheller)._clear_module(ctx, "rewrite")

    assert root.is_dir()
    assert list(root.iterdir()) == []


def test_a_fresh_step_keeps_the_repository_the_module_is(ctx_for, sheller):
    """A module that is its own repository — or a submodule, whose `.git`
    is a file — lost its history to a start-over, and with it the undo the
    hand-edit guard sends people to. Odoo loads nothing hidden, so nothing
    hidden belongs to the attempt being cleared."""
    from pathlib import Path

    ctx = ctx_for()
    root = Path(ctx.module.new_path)
    (root / "__manifest__.py").write_text("{'name': 'x', 'depends': []}", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "HEAD").write_text("ref: refs/heads/18.0\n", encoding="utf-8")
    (root / ".gitignore").write_text("*.pyc\n", encoding="utf-8")
    (root / "models.py").write_text("x = 1\n", encoding="utf-8")

    _runner(sheller)._clear_module(ctx, "rewrite")

    assert sorted(child.name for child in root.iterdir()) == [".git", ".gitignore"]
    assert (root / ".git" / "HEAD").is_file()


def test_a_fresh_step_keeps_a_submodule_link(ctx_for, sheller):
    from pathlib import Path

    ctx = ctx_for()
    root = Path(ctx.module.new_path)
    (root / "__manifest__.py").write_text("{'name': 'x', 'depends': []}", encoding="utf-8")
    (root / ".git").write_text("gitdir: ../.git/modules/sale_custom\n", encoding="utf-8")

    _runner(sheller)._clear_module(ctx, "rewrite")

    assert [child.name for child in root.iterdir()] == [".git"]


def test_a_fresh_step_never_clears_the_project_root(ctx_for, sheller):
    from pathlib import Path

    ctx = ctx_for()
    project = Path(ctx.migration.new_project_path)
    (project / "__manifest__.py").write_text("{'name': 'x', 'depends': []}", encoding="utf-8")
    ctx.module.new_path = str(project)

    _runner(sheller)._clear_module(ctx, "rewrite")

    assert (project / "__manifest__.py").is_file()


def test_two_jobs_on_one_database_share_one_lock(ctx_for, tmp_path):
    from odoo_migrator.runner.native import _target_lock

    first = ctx_for(project="a")
    second = ctx_for(project="b")
    other = ctx_for(project="c")
    other.session_targets["target"] = {**other.session_targets["target"], "database": "elsewhere"}

    assert _target_lock(first) is _target_lock(second)
    assert _target_lock(first) is not _target_lock(other)


def test_a_parked_job_lets_another_use_the_database(ctx_for, sheller):
    from odoo_migrator.runner.native import _target_lock

    ctx = ctx_for()
    runner = _runner(sheller)
    ctx.target_lock = _target_lock(ctx)
    runner._hold_target(ctx)
    assert ctx.target_lock.locked()

    runner._release_target(ctx)

    assert not ctx.target_lock.locked()
    runner._release_target(ctx)  # twice is harmless: it is not held


def test_a_step_that_raises_still_closes_its_row(app, fake_sheller, tmp_path):
    """The trace used to show a step running for a job long failed."""
    from test_native_runner import _module

    from odoo_migrator.models import StepRun

    job = _module(tmp_path)
    runner = _runner(fake_sheller)
    cartridge = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-sonnet-5-5}
pipeline:
  - step: agent
    id: port
    sessions: [target]
    tools: [exec]
    prompt: port
on_failure:
  pipeline: []
  retry:
    resume_at: port
"""

    class _Boom:
        def ensure_ready(self):

            return None

        def run_step(self, **_kwargs):
            raise RuntimeError("provider fell over")

    runner.provider = _Boom()
    runner.start({"job_id": job.job_id, "cartridge_text": cartridge})
    with pytest.raises(RuntimeError):
        runner.wait(job.job_id)

    with database.connection_context():
        rows = list(StepRun.select().where(StepRun.job == job.id))
    assert rows and rows[0].finished_at is not None
    assert rows[0].ok is False
    assert "provider fell over" in rows[0].output


# -- the two project roots ---------------------------------------------------


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("repo", "repo"),
        ("repo/v17", "repo"),
        ("repo", "repo/v18"),
        ("repo/./v17/..", "repo"),
    ],
)
def test_overlapping_project_roots_are_refused(tmp_path, old, new):
    """With a new root at or above the old one, the module found under the
    new root is the old module, and `fresh` empties it."""
    from odoo_migrator.migrations import ProjectPathsError

    init_db(tmp_path / "roots.sqlite")
    with pytest.raises(ProjectPathsError):
        create_migration(
            name="Acme",
            accent="violet",
            source_version="17.0",
            target_version="18.0",
            old_project_path=str(tmp_path / old),
            new_project_path=str(tmp_path / new),
        )


def test_siblings_are_fine(tmp_path):
    init_db(tmp_path / "roots.sqlite")

    assert create_migration(
        name="Acme",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path=str(tmp_path / "repo" / "v17"),
        new_project_path=str(tmp_path / "repo" / "v18"),
    )


def test_the_form_says_why_it_refused_the_roots(client, tmp_path):
    response = client.post(
        "/migrations",
        data={
            "name": "Acme",
            "accent": "violet",
            "source_version": "17.0",
            "target_version": "18.0",
            "old_project_path": str(tmp_path / "repo"),
            "new_project_path": str(tmp_path / "repo"),
        },
        follow_redirects=False,
    )

    assert response.status_code == 200
    assert "same directory" in response.text


def test_a_fresh_step_never_clears_the_old_module(ctx_for, sheller, tmp_path):
    """A migration saved before the roots were checked can still overlap."""
    from pathlib import Path

    ctx = ctx_for()
    old_module = tmp_path / "shared" / "sale_custom"
    old_module.mkdir(parents=True)
    (old_module / "__manifest__.py").write_text("{'name': 'x', 'depends': []}", encoding="utf-8")
    ctx.migration.old_project_path = str(tmp_path / "shared")
    ctx.migration.new_project_path = str(tmp_path / "shared")
    ctx.module.new_path = str(old_module)

    _runner(sheller)._clear_module(ctx, "rewrite")

    assert (Path(old_module) / "__manifest__.py").is_file()


# -- the read snippet, against the one Sheller runs ----------------------------

_SHELLER_MCP = "/opt/projects/odoo-sheller/odoo_sheller/mcp.py"


def _code_lines(snippet: str) -> list[str]:

    return [
        line.rstrip()
        for line in snippet.splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]


@pytest.mark.parametrize(
    "args",
    [
        ("sale/models/sale_order.py", None, None, 10, 40, None),
        ("sale", None, None, None, None, None),
        (None, "sale.order", "action_confirm", None, None, "sale"),
        (None, "sale.order", None, None, None, None),
    ],
)
def test_the_snippet_is_still_the_one_sheller_runs(args):
    """Copied, so it can drift. Checked when the sibling checkout is here."""
    import ast
    from pathlib import Path

    from odoo_migrator.sheller.source import source_snippet

    path = Path(_SHELLER_MCP)
    if not path.is_file():
        pytest.skip("no odoo-sheller checkout next to this one")
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = next(
        (node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == "_source_snippet"),
        None,
    )
    assert found is not None, "odoo-sheller no longer has _source_snippet"
    namespace: dict = {}
    exec(compile(ast.Module(body=[found], type_ignores=[]), _SHELLER_MCP, "exec"), namespace)  # noqa: S102
    theirs = namespace["_source_snippet"](*args)

    assert _code_lines(source_snippet(*args)) == _code_lines(theirs)


# -- previous_diff: everything since the job began -----------------------------


def test_patch_sees_the_port_and_the_test_not_only_the_last_step(ctx_for, sheller):
    """After `write_test` it used to be the test's diff alone, and `patch`
    was asked to fix a port it could not see."""
    from pathlib import Path

    from odoo_migrator.models import Batch, Job
    from odoo_migrator.runner.snapshot import snapshot

    ctx = ctx_for()
    with database.connection_context():
        batch = Batch.create(migration=ctx.migration, status="running")
        job = Job.create(batch=batch, module=ctx.module, status="running")
    ctx.job_id = job.job_id
    root = Path(ctx.module.new_path)
    (root / "models.py").write_text("class Sale:\n    pass\n", encoding="utf-8")
    ctx.baseline_files = snapshot(root)
    runner = _runner(sheller)

    before = snapshot(root)
    (root / "models.py").write_text("class Sale:\n    _name = 'sale.custom'\n", encoding="utf-8")
    runner._note_diff(ctx, before)
    before = snapshot(root)
    (root / "tests").mkdir()
    (root / "tests" / "test_sale.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    runner._note_diff(ctx, before)

    assert "_name = 'sale.custom'" in ctx.previous_diff
    assert "def test_x" in ctx.previous_diff
    # The checkpoint still keeps each step's own hunk.
    with database.connection_context():
        logged = Job.get_by_id(job.id).file_diffs
    assert "_name = 'sale.custom'" in logged and "def test_x" in logged


def test_the_baseline_is_the_module_as_the_job_found_it(app, fake_sheller, tmp_path):
    from test_native_runner import _module

    job = _module(tmp_path)
    module_dir = tmp_path / "new" / "sale_custom"
    (module_dir / "models.py").write_text("x = 1\n", encoding="utf-8")
    cartridge = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-sonnet-5-5}
pipeline:
  - step: agent
    id: port
    sessions: [target]
    tools: [write_file]
    prompt: port
on_failure:
  pipeline: []
  retry:
    resume_at: port
"""
    from doubles.model import ModelDouble

    from odoo_migrator.runner.providers.scripted import ScriptedProvider

    model = ModelDouble(
        turns=[
            {"name": "write_file", "input": {"side": "target", "path": "sale_custom/models.py", "content": "x = 2\n"}},
            {"name": "report", "input": {"answer": "ported"}},
        ]
    )
    runner = _runner(fake_sheller)
    runner.provider = ScriptedProvider(model)
    runner.start({"job_id": job.job_id, "cartridge_text": cartridge})
    runner.wait(job.job_id)

    from odoo_migrator.models import Attempt

    with database.connection_context():
        diff = Attempt.select().where(Attempt.job == job.id).first().diff
    assert "-x = 1" in diff and "+x = 2" in diff


# -- probe: a difference in behaviour fails the lap ----------------------------


@pytest.mark.parametrize("level", ["basic", "enhanced", "strict"])
def test_a_difference_the_probe_found_fails_the_lap(ctx_for, level):
    ctx = ctx_for()
    ctx.migration.strictness_default = level
    ctx.prior_answers["probe"] = "amount_total is 110 on 17.0 and 100 on 18.0"
    ctx.lap_flags = {"no_tests", "behaviour_differs"}

    result = verdict.run(Step(kind="verdict"), ctx)

    assert result.ok is False
    assert "amount_total is 110" in result.output


def test_the_stock_probe_raises_the_flag_the_verdict_reads():
    from pathlib import Path

    from odoo_migrator.runner.cartridge import load_cartridge_file

    stock = load_cartridge_file(Path("cartridges/stock.yaml"))

    assert "behaviour_differs" in stock.step_by_id("probe").raises
    assert "rules" in stock.step_by_id("probe").context


def test_a_flag_only_verdict_reads_needs_a_verdict_in_the_file():
    text = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-sonnet-5-5}
pipeline:
  - step: agent
    id: probe
    sessions: [target]
    tools: [exec]
    raises: [behaviour_differs]
    prompt: probe
%s
on_failure:
  pipeline: []
  retry:
    resume_at: probe
"""
    assert load_cartridge(text % "  - step: verdict")
    with pytest.raises(CartridgeError, match="never read"):
        load_cartridge(text % "")


def test_the_loop_hands_the_verdict_this_laps_flags(ctx_for):
    lap = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-sonnet-5-5}
pipeline:
  - step: agent
    id: probe
    sessions: [target]
    tools: [exec]
    raises: [behaviour_differs]
    prompt: probe
  - step: verdict
on_failure:
  pipeline: []
  retry:
    resume_at: probe
"""
    ctx = ctx_for()
    ctx.cartridge = load_cartridge(lap)
    ctx.provider = _Scripted(
        [
            StepResult(ok=True, answer="totals differ", flags={"behaviour_differs"}),
            StepResult(ok=True, answer="same on both sides"),
        ]
    )

    outcome = run_job(ctx.cartridge, ctx, retry_limit=3)

    # Lap one fails on the flag; lap two raised none, so it passes.
    assert outcome == "passed"
    assert ctx.attempt == 2


def test_the_spec_shows_the_stock_cartridge_as_it_is():
    """The design of record quotes the whole file. It had drifted: the spec's
    copy gave `understand` no rules and `diagnose` none either."""
    from pathlib import Path

    spec = Path("docs/runner-design.md").read_text(
        encoding="utf-8"
    )
    start = spec.index("```yaml", spec.index("### The stock cartridge")) + len("```yaml\n")
    quoted = spec[start : spec.index("```", start)]

    assert quoted == Path("cartridges/stock.yaml").read_text(encoding="utf-8")


def test_diagnose_reads_the_rules():
    """The step that decides what is wrong did not see what the target changed."""
    from pathlib import Path

    from odoo_migrator.runner.cartridge import load_cartridge_file

    stock = load_cartridge_file(Path("cartridges/stock.yaml"))

    assert "rules" in stock.step_by_id("diagnose").context


def test_a_port_cannot_be_written_beside_the_addons_directory(ctx_for, tmp_path):
    """The live gate's first run passed without testing anything it wrote.

    The project keeps its addons in `project/`, so the module is
    `project/mig_test_base`. The agent took the module's technical name for
    its path and wrote the whole port to `mig_test_base/` — inside the
    project root, so nothing refused it, and outside the directory the
    container mounts, so Odoo never saw a line of it. `upgrade` and `tests`
    ran on the copy that was already installed and the lap went green.
    """
    ctx = ctx_for()
    root = Path(ctx.migration.new_project_path)
    inside = root / "project" / "mig_test_base"
    inside.mkdir(parents=True, exist_ok=True)
    (inside / "__manifest__.py").write_text("{'name': 'x', 'depends': []}", encoding="utf-8")
    with database.connection_context():
        module = Module.get_by_id(ctx.module.id)
        module.technical_name = "mig_test_base"
        module.new_path = str(inside)
        module.save()
    ctx.module = module
    step = Step(kind="agent", id="port", sessions=["target"], tools=["write_file"], prompt="p")
    (write,) = tools_for(step, ctx)

    with pytest.raises(ToolRefused) as refused:
        write(side="target", path="mig_test_base/__manifest__.py", content="{}")

    assert "project/mig_test_base" in str(refused.value)

    written = write(
        side="target", path="project/mig_test_base/models/x.py", content="x = 1\n"
    )

    assert written == str(inside / "models" / "x.py")


# -- a written test is run by the runner, not only by the agent ---------------


def test_the_stock_flow_runs_the_test_it_just_wrote():
    """`tests` runs before `write_test`, so for a module that shipped none the
    only run was the one the agent made inside its own step — its outcome was
    a tool call and never a step anyone could fail on."""
    from pathlib import Path

    from odoo_migrator.runner.cartridge import load_cartridge_file

    stock = load_cartridge_file(Path("cartridges/stock.yaml"))
    names = [step.id or step.kind for step in stock.pipeline]

    assert names.index("retest") == names.index("write_test") + 1
    assert names.index("retest") < names.index("verdict")
    retest = stock.step_by_id("retest")
    assert retest.kind == "tests"
    assert retest.when == "no_tests"


def test_write_test_can_read_the_target_versions_own_tests():
    """It had `read_file` and no `source`: it could open the module's files on
    the host and not the base class it was writing against."""
    from pathlib import Path

    from odoo_migrator.runner.cartridge import load_cartridge_file

    stock = load_cartridge_file(Path("cartridges/stock.yaml"))
    tools = stock.step_by_id("write_test").tools

    assert "source" in tools
    assert "list_tests" in tools


def test_a_rerun_after_a_write_opens_a_fresh_session(ctx_for, sheller):
    """The process imported the module's `tests` package when the session
    opened. A run before a reopen finds the set that was there before."""
    ctx = ctx_for()
    ctx.lap_flags = {"no_tests"}
    before = ctx.sessions["target"]
    name = ctx.module.technical_name
    sheller.tests_for(name, sheller.catalogue(name, {"TestThing": ["test_one"]}))

    result = tests.run(Step(kind="tests", id="retest"), ctx)

    assert result.ok is True
    assert ctx.sessions["target"] != before
    assert before in sheller.closes


def test_the_first_run_of_a_lap_does_not_reopen(ctx_for, sheller):
    """`upgrade` opened this session a moment ago. Another odoo-bin start per
    lap buys nothing."""
    ctx = ctx_for()
    before = ctx.sessions["target"]
    name = ctx.module.technical_name
    sheller.tests_for(name, sheller.catalogue(name, {"TestThing": ["test_one"]}))

    tests.run(Step(kind="tests"), ctx)

    assert ctx.sessions["target"] == before


def test_a_source_read_can_be_opened_in_the_journal(ctx_for, sheller):
    """The snippet runs as a command, so the journal has it. `read_source`
    parsed the answer and dropped the id with the envelope, so a source line
    was the only one of the daemon's commands with nothing to open."""
    ctx = ctx_for()
    step = Step(kind="agent", id="port", sessions=["target"], tools=["source"], prompt="p")
    (read,) = tools_for(step, ctx)
    seen = []
    ctx.record_tool = lambda *args: seen.append(args)

    read(side="target", path="sale/models/sale_order.py")

    # (tool, side, session, command_id, ...)
    assert seen[0][0] == "source"
    assert seen[0][3], "a source read recorded no command id"


def test_a_green_run_says_which_specs_it_covered(ctx_for, sheller):
    """"1 test class(es) passed" is the same sentence whether the run covered
    the module's own case or a class that came in with a dependency. A
    module run names the module and the count."""
    ctx = ctx_for()
    name = ctx.module.technical_name
    sheller.tests_for(
        name, sheller.catalogue(name, {"TestThing": ["test_one", "test_two"]})
    )

    result = tests.run(Step(kind="tests"), ctx)

    assert result.ok is True
    assert f"{name} (2 tests) passed" in result.output


def test_the_failure_lane_can_run_the_test_that_failed():
    """`diagnose` and `patch` had no `run_test`: the diagnosis read a log tail
    and could not run the failing spec again, and a fix waited a whole lap —
    lint, upgrade, every test — to learn whether it worked."""
    from pathlib import Path

    from odoo_migrator.runner.cartridge import load_cartridge_file

    stock = load_cartridge_file(Path("cartridges/stock.yaml"))
    diagnose = stock.step_by_id("diagnose")
    patch = stock.step_by_id("patch")

    assert {"run_test", "list_tests"} <= set(diagnose.tools)
    assert {"run_test", "reopen"} <= set(patch.tools)
    assert "run the spec that failed" in " ".join(patch.prompt.split())
