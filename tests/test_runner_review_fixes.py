"""What the first review of the runner found. One test per finding.

These are here rather than spread across the suites they belong to because
they are a set: every one of them passed its own task's tests while being
wrong, and most of them were wrong in the same way — a double that answered
more helpfully than the daemon, or an invariant nobody asserted.
"""

from types import SimpleNamespace

import pytest

from odoo_migrator.db import database
from odoo_migrator.models import Migration, Module
from odoo_migrator.runner.cartridge import Budget, Step, load_cartridge
from odoo_migrator.runner.context import StepContext
from odoo_migrator.runner.prompting import MAX_TOOL_CALLS, budget_for
from odoo_migrator.runner.providers.anthropic import MAX_OUTPUT_TOKENS, NoModelClient
from odoo_migrator.runner.snapshot import diff, snapshot
from odoo_migrator.runner.steps import ask_human
from odoo_migrator.runner.tools import ToolRefused, tools_for
from odoo_migrator.sheller.errors import ShellerError
from odoo_migrator.sheller.fake import FakeShellerClient


def _ctx(tmp_path, sheller, **extra) -> StepContext:
    old = tmp_path / "old"
    new = tmp_path / "new"
    (new / "sale_custom").mkdir(parents=True)
    old.mkdir(exist_ok=True)
    with database.connection_context():
        migration = Migration.create(
            name="Acme",
            accent_color="oklch(0.70 0.15 305)",
            source_version="17.0",
            target_version="18.0",
            old_project_path=str(old),
            new_project_path=str(new),
            old_container="odoo-old",
            new_container="odoo-new",
        )
        module = Module.create(
            migration=migration,
            technical_name="sale_custom",
            new_path=str(new / "sale_custom"),
        )
    opened = sheller.open_session(
        container="odoo-new",
        database="acme_17",
        odoo_bin="/opt/odoo/odoo-bin",
        owner={"kind": "agent"},
        allow_commit=True,
    )
    fields = {
        "job_id": "job-1",
        "attempt": 1,
        "migration": migration,
        "module": module,
        "cartridge": None,
        "sheller": sheller,
        "sessions": {"new": opened["id"]},
        "write_keys": {"new": opened["write_key"]},
        "session_targets": {
            "new": {
                "container": "odoo-new",
                "database": "acme_17",
                "odoo_bin": "/opt/odoo/odoo-bin",
                "owner": {"kind": "agent"},
            }
        },
        "emit": lambda _event: None,
    }
    fields.update(extra)

    return StepContext(**fields)


def test_the_fake_describes_a_session_the_way_the_daemon_does():
    """The daemon's `describe()` has no `odoo_bin`. The fake invented one.

    `reopen` was written against the fake and read `described["odoo_bin"]`,
    which is a KeyError on the first real container.
    """
    sheller = FakeShellerClient()
    opened = sheller.open_session(
        container="odoo-new",
        database="acme_17",
        odoo_bin="/opt/odoo/odoo-bin",
        owner={"kind": "agent"},
        allow_commit=True,
    )

    described = sheller.get_session(opened["id"])

    assert "odoo_bin" not in described
    assert "write_key" not in described
    assert described["container"] == "odoo-new"


def test_reopen_uses_the_target_the_runner_remembered(app, tmp_path):
    sheller = FakeShellerClient()
    ctx = _ctx(tmp_path, sheller)
    first = ctx.sessions["new"]
    step = Step(kind="agent", id="port", sessions=["new"], tools=["reopen"], prompt="x")
    reopen = next(tool for tool in tools_for(step, ctx) if tool.name == "reopen")

    reopen(side="new")

    assert ctx.sessions["new"] != first
    assert first in sheller.closes


def test_a_tool_call_records_the_session_it_actually_ran_in(app, tmp_path):
    """A reopen inside a step must not relabel the call that did it."""
    sheller = FakeShellerClient()
    seen = []
    ctx = _ctx(
        tmp_path,
        sheller,
        record_tool=lambda *row: seen.append(row),
    )
    before = ctx.sessions["new"]
    step = Step(kind="agent", id="port", sessions=["new"], tools=["reopen"], prompt="x")
    reopen = next(tool for tool in tools_for(step, ctx) if tool.name == "reopen")

    reopen(side="new")

    assert seen[0][2] == before


def test_a_budget_larger_than_the_ceiling_is_clamped():
    """"A cartridge may narrow, never widen" was not enforced anywhere."""
    step = Step(
        kind="agent",
        id="greedy",
        sessions=["new"],
        tools=["exec"],
        prompt="x",
        budget=Budget(tool_calls=100_000, tokens=10_000_000),
    )

    clamped = budget_for(step, None)

    assert clamped.tool_calls == MAX_TOOL_CALLS
    assert clamped.tokens <= 400_000


def test_a_smaller_budget_is_honoured():
    step = Step(
        kind="agent",
        id="modest",
        sessions=["new"],
        tools=["exec"],
        prompt="x",
        budget=Budget(tool_calls=3),
    )

    assert budget_for(step, None).tool_calls == 3


def test_the_output_cap_is_not_the_run_budget():
    """200_000 as `max_tokens` is a 400: the models cap one answer at 128K."""
    assert MAX_OUTPUT_TOKENS <= 128_000


def test_an_agent_step_with_no_key_says_so(app, tmp_path, monkeypatch):
    """There is no client object to be missing any more: a provider that is
    not ready refuses before it spends anything."""
    from odoo_migrator.runner.providers.anthropic import AnthropicProvider

    monkeypatch.setattr("odoo_migrator.runner.providers.anthropic._key", lambda: "")

    with pytest.raises(NoModelClient, match="settings page"):
        AnthropicProvider().ensure_ready()


def test_ask_human_asks_with_the_last_agent_answer(app, tmp_path):
    sheller = FakeShellerClient()
    ctx = _ctx(tmp_path, sheller, last_answer="the override is gone in 18.0 — keep it?")

    result = ask_human.run(Step(kind="ask_human"), ctx)

    assert "keep it?" in result.output
    assert result.output != "A checkpoint is open."


def test_a_write_is_visible_as_a_diff(tmp_path):
    root = tmp_path / "sale_custom"
    root.mkdir()
    (root / "models.py").write_text("x = 1\n", encoding="utf-8")
    before = snapshot(root)
    (root / "models.py").write_text("x = 2\n", encoding="utf-8")

    hunk = diff(before, snapshot(root))

    assert "-x = 1" in hunk
    assert "+x = 2" in hunk


def test_the_loop_takes_its_resolver_as_an_argument():
    """It used to be swapped on the module, which two jobs would fight over."""
    from odoo_migrator.runner.loop import run_job

    text = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-opus-5}
pipeline:
  - step: verdict
on_failure:
  pipeline: []
  retry:
    resume_at: verdict
"""
    ran = []

    def resolve(kind):
        def runner(step, ctx):
            from odoo_migrator.runner.context import StepResult

            ran.append(kind)

            return StepResult(ok=True)

        return runner

    ctx = SimpleNamespace(attempt=1, prior_answers={}, last_answer="", last_failure="")
    outcome = run_job(load_cartridge(text), ctx, retry_limit=1, resolve=resolve)

    assert outcome == "passed"
    assert ran == ["verdict"]


def test_a_tool_still_refuses_a_side_the_step_did_not_declare(app, tmp_path):
    sheller = FakeShellerClient()
    ctx = _ctx(tmp_path, sheller)
    step = Step(kind="agent", id="probe", sessions=["new"], tools=["exec"], prompt="x")
    exec_tool = next(tool for tool in tools_for(step, ctx) if tool.name == "exec")

    with pytest.raises(ToolRefused):
        exec_tool(side="old", code="1 + 1")


def test_a_step_this_build_never_heard_of_can_report_progress():
    """The generic agent step is pointless if progress refuses its name."""
    from odoo_migrator.contract import ProgressPayload

    payload = ProgressPayload.model_validate(
        {"job_id": "job-1", "stage": "check_reports", "attempt": 1}
    )

    assert payload.stage == "check_reports"


def test_progress_still_refuses_something_that_is_not_a_label():
    from odoo_migrator.contract import ProgressPayload

    with pytest.raises(ValueError, match="unusable stage"):
        ProgressPayload.model_validate({"job_id": "job-1", "stage": "  ", "attempt": 1})


def test_a_setup_failure_fails_the_job_instead_of_vanishing(app, tmp_path, fake_sheller):
    """A container that is not up killed the thread with nobody told."""
    from odoo_migrator.models import Batch, Job
    from odoo_migrator.runner.native import NativeRunner

    with database.connection_context():
        migration = Migration.create(
            name="NoContainers",
            accent_color="oklch(0.70 0.15 305)",
            source_version="17.0",
            target_version="18.0",
            old_project_path=str(tmp_path / "old"),
            new_project_path=str(tmp_path / "new"),
        )
        module = Module.create(migration=migration, technical_name="sale_custom")
        batch = Batch.create(migration=migration, status="running")
        job = Job.create(batch=batch, module=module, status="queued")

    runner = NativeRunner(sheller=fake_sheller, hub=app.state.hub)
    runner.start({"job_id": job.job_id})
    with pytest.raises(RuntimeError, match="not up"):
        runner.wait(job.job_id)

    with database.connection_context():
        assert Job.get_by_id(job.id).status == "failed"


def test_a_command_that_outlives_its_timeout_is_interrupted(app, tmp_path):
    sheller = FakeShellerClient()
    sheller.hangs_on = "search([])"
    ctx = _ctx(tmp_path, sheller, command_timeout=0.5)
    step = Step(kind="agent", id="probe", sessions=["new"], tools=["exec"], prompt="x")
    exec_tool = next(tool for tool in tools_for(step, ctx) if tool.name == "exec")

    answer = exec_tool(side="new", code="env['res.partner'].search([])")

    assert "interrupted" in answer
    assert sheller.interrupts


def test_a_huge_diff_does_not_go_into_the_prompt_whole(app, tmp_path):
    from odoo_migrator.runner.prompting import MAX_DIFF_CHARS, build_context

    sheller = FakeShellerClient()
    ctx = _ctx(tmp_path, sheller, previous_diff="+" * (MAX_DIFF_CHARS * 2))
    step = Step(
        kind="agent",
        id="patch",
        sessions=["new"],
        tools=["write_file"],
        context=["previous_diff"],
        prompt="fix",
    )

    rendered = build_context(step, ctx)

    assert len(rendered) < MAX_DIFF_CHARS * 2
    assert "truncated" in rendered


def test_a_session_opened_before_a_failed_open_is_still_closed(app, tmp_path, fake_sheller):
    """`old` opens, `new` refuses. The runner used to leave the old shell
    running: `ctx` was never assigned, so the `finally` closed nothing."""
    from odoo_migrator.models import Batch, Job
    from odoo_migrator.runner.native import NativeRunner

    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    with database.connection_context():
        migration = Migration.create(
            name="Leak",
            accent_color="oklch(0.70 0.15 305)",
            source_version="17.0",
            target_version="18.0",
            old_project_path=str(old),
            new_project_path=str(new),
            old_container="odoo-old",
            new_container="odoo-new",
        )
        module = Module.create(migration=migration, technical_name="sale_custom")
        batch = Batch.create(migration=migration, status="running")
        job = Job.create(batch=batch, module=module, status="queued")

    real_open = fake_sheller.open_session
    opened: list[str] = []

    def open_once(**kwargs):
        if opened:
            raise ShellerError("the new container refused a session", status=409)
        row = real_open(**kwargs)
        opened.append(row["id"])

        return row

    fake_sheller.open_session = open_once

    runner = NativeRunner(sheller=fake_sheller, hub=app.state.hub)
    runner.start({"job_id": job.job_id})
    with pytest.raises(ShellerError):
        runner.wait(job.job_id)

    assert opened
    assert opened[0] in fake_sheller.closes


def test_a_project_kept_under_a_skipped_name_is_still_read(tmp_path):
    """The skip list was checked against the whole path, so a project kept
    anywhere under a directory called `node_modules` snapshot as empty, and
    every diff of it was blank."""
    root = tmp_path / "node_modules" / "project" / "sale_custom"
    root.mkdir(parents=True)
    (root / "models.py").write_text("x = 1\n", encoding="utf-8")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("[core]\n", encoding="utf-8")

    assert snapshot(root) == {"models.py": "x = 1\n"}


def test_a_test_run_that_outlives_its_call_is_interrupted(app, tmp_path):
    """`exec` interrupted its own timeout; `run_test` did not, and the agent's
    next command on that side answered `session_busy`."""
    sheller = FakeShellerClient()
    sheller.tests_for("sale_custom", sheller.catalogue("sale_custom", {"TestSlow": ["test_a"]}))
    sheller.test_hangs.add("sale_custom.TestSlow")
    ctx = _ctx(tmp_path, sheller)
    step = Step(kind="agent", id="probe", sessions=["new"], tools=["run_test", "exec"], prompt="x")
    bound = {tool.name: tool for tool in tools_for(step, ctx)}

    answer = bound["run_test"](side="new", test="sale_custom.TestSlow")

    assert "interrupted" in answer
    assert sheller.interrupts
    after = bound["exec"](side="new", code="1")
    assert not after.startswith("error:"), after
