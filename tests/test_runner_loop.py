"""The runner loop: gates, the failure lane, and the retry edge."""

from types import SimpleNamespace

from odoo_migrator.runner.cartridge import load_cartridge
from odoo_migrator.runner.context import StepResult
from odoo_migrator.runner.loop import run_job
from odoo_migrator.runner.steps import ask_human

ALWAYS_FAILS = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-opus-5}
pipeline:
  - step: agent
    id: port
    sessions: [new]
    tools: [exec]
    prompt: port
  - step: lint
  - step: upgrade
on_failure:
  pipeline:
    - step: agent
      id: patch
      sessions: [new]
      tools: [write_file]
      prompt: patch
  retry:
    resume_at: lint
"""

CARTRIDGE_WITH_PROBE = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-opus-5}
pipeline:
  - step: agent
    id: port
    sessions: [new]
    tools: [exec]
    prompt: port
  - step: tests
  - step: agent
    id: probe
    when: no_tests
    sessions: [new]
    tools: [exec]
    prompt: probe
  - step: verdict
on_failure:
  pipeline: []
  retry:
    resume_at: verdict
"""

ASKS_A_HUMAN = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-opus-5}
pipeline:
  - step: upgrade
on_failure:
  pipeline:
    - step: agent
      id: diagnose
      sessions: [new]
      tools: [exec]
      context: [last_failure]
      raises: [needs_human]
      prompt: why
    - step: ask_human
      when: needs_human
  retry:
    resume_at: upgrade
"""


def loop_ctx(**script):

    return SimpleNamespace(
        attempt=1, job_id="job-1", script=script, emit=lambda _event: None
    )


def run_recorded(text: str, ctx, *, retry_limit: int = 3):
    ran: list[str] = []
    cartridge = load_cartridge(text)

    def resolve(kind: str):
        def runner(step, current):
            label = step.id or step.kind
            ran.append(label)
            if step.kind == "ask_human":

                return ask_human.run(step, current)
            if label == "upgrade":

                return StepResult(ok=False, output="upgrade failed")
            if label == "diagnose" and current.script.get("needs_human"):

                return StepResult(ok=True, flags={"needs_human"})
            if step.kind == "tests" and current.script.get("no_tests"):

                return StepResult(ok=True, flags={"no_tests"})

            return StepResult(ok=True)

        return runner

    from odoo_migrator.runner import loop as loop_mod

    loop_original = loop_mod.resolve
    loop_mod.resolve = resolve
    try:
        outcome = run_job(cartridge, ctx, retry_limit=retry_limit)
    finally:
        loop_mod.resolve = loop_original

    return outcome, ran


def test_a_gated_step_is_skipped_when_its_flag_is_not_raised():
    outcome, ran = run_recorded(CARTRIDGE_WITH_PROBE, loop_ctx(no_tests=False))

    assert "probe" not in ran
    assert outcome == "passed"


def test_the_retry_loop_actually_loops_and_stops_at_the_limit():
    outcome, ran = run_recorded(ALWAYS_FAILS, loop_ctx(), retry_limit=3)

    assert ran.count("lint") == 3
    assert outcome == "failed"


def test_retry_resumes_where_resume_at_says():
    outcome, ran = run_recorded(ALWAYS_FAILS, loop_ctx(), retry_limit=2)

    assert ran[:3] == ["port", "lint", "upgrade"]
    assert ran[ran.index("patch") + 1] == "lint"
    assert outcome == "failed"


def test_a_parked_job_stops_the_loop_without_spending_an_attempt():
    outcome, ran = run_recorded(ASKS_A_HUMAN, loop_ctx(needs_human=True))

    assert outcome == "waiting_for_human"
    assert ran.count("diagnose") == 1
