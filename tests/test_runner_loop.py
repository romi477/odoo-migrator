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

    # Carries the fields the loop and the deterministic steps read. A
    # namespace missing one reads as a passing test right up until the real
    # context grows the attribute.
    return SimpleNamespace(
        attempt=1,
        job_id="job-1",
        script=script,
        emit=lambda _event: None,
        prior_answers={},
        last_answer="",
        previous_diff="",
        last_failure="",
        wait_for_human=None,
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

    outcome = run_job(cartridge, ctx, retry_limit=retry_limit, resolve=resolve)

    return outcome, ran


def test_a_gated_step_is_skipped_when_its_flag_is_not_raised():
    outcome, ran = run_recorded(CARTRIDGE_WITH_PROBE, loop_ctx(no_tests=False))

    assert "probe" not in ran
    assert outcome == "passed"


def test_a_lane_that_changes_nothing_stops_instead_of_spending_every_attempt():
    """`patch` runs, writes nothing, and the same `upgrade` fails again. The
    live run proved this shape five times over, at 740K tokens."""
    outcome, ran = run_recorded(ALWAYS_FAILS, loop_ctx(), retry_limit=5)

    assert ran.count("lint") == 2
    assert outcome == "failed"


def test_the_retry_loop_spends_its_attempts_while_the_lane_keeps_working():
    ran: list[str] = []
    cartridge = load_cartridge(ALWAYS_FAILS)
    ctx = loop_ctx()

    def resolve(kind: str):
        def runner(step, current):
            label = step.id or step.kind
            ran.append(label)
            if label == "patch":
                # A real patch writes files, which shows up as a new diff.
                current.previous_diff = f"diff {len(ran)}"

                return StepResult(ok=True)
            if label == "upgrade":

                return StepResult(ok=False, output="upgrade failed")

            return StepResult(ok=True)

        return runner

    outcome = run_job(cartridge, ctx, retry_limit=3, resolve=resolve)

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


STOCK_SHAPE = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-opus-5}
pipeline:
  - step: agent
    id: understand
    sessions: [old]
    tools: [exec]
    prompt: read
  - step: agent
    id: port
    sessions: [new]
    tools: [write_file]
    prompt: port
  - step: lint
  - step: upgrade
  - step: verdict
on_failure:
  pipeline:
    - step: agent
      id: diagnose
      sessions: [new]
      tools: [exec]
      prompt: why
  retry:
    resume_at: lint
"""


def run_with(text: str, ctx, fails, *, retry_limit: int = 3):
    """`fails` is a set of labels that fail the first time they run."""
    ran: list[str] = []
    seen: set[str] = set()
    cartridge = load_cartridge(text)

    def resolve(kind: str):
        def runner(step, current):
            del current
            label = step.id or step.kind
            ran.append(label)
            if label in fails and label not in seen:
                seen.add(label)

                return StepResult(ok=False, output=f"{label} failed")

            return StepResult(ok=True)

        return runner

    outcome = run_job(cartridge, ctx, retry_limit=retry_limit, resolve=resolve)

    return outcome, ran


def test_a_failure_before_resume_at_does_not_skip_the_step_that_does_the_work():
    """`port` writes the module. Resuming at `lint` after `understand` failed
    used to lint a directory `port` had never written."""
    outcome, ran = run_with(STOCK_SHAPE, loop_ctx(), fails={"understand"})

    assert ran[:2] == ["understand", "diagnose"]
    assert ran[2:] == ["understand", "port", "lint", "upgrade", "verdict"]
    assert outcome == "passed"


def test_a_failure_after_resume_at_still_rewinds_to_it():
    outcome, ran = run_with(STOCK_SHAPE, loop_ctx(), fails={"upgrade"})

    assert ran[:4] == ["understand", "port", "lint", "upgrade"]
    assert ran[4] == "diagnose"
    assert ran[5] == "lint"
    assert "port" not in ran[5:]
    assert outcome == "passed"


def test_a_failure_on_resume_at_itself_retries_that_step():
    outcome, ran = run_with(STOCK_SHAPE, loop_ctx(), fails={"lint"})

    assert ran[ran.index("diagnose") + 1] == "lint"
    assert outcome == "passed"


def test_an_agent_step_does_not_own_last_failure(monkeypatch):
    """`diagnose` reads `last_failure`, and so does the `patch` gated after it
    in the same lane. A diagnose that failed used to replace the upgrade's
    error with its own, and the patch then fixed the wrong thing. The loop
    sets this field; a step does not."""
    from types import SimpleNamespace

    from odoo_migrator.runner.cartridge import ModelRef, Step
    from odoo_migrator.runner.prompting import run_agent_step

    class _Provider:
        def ensure_ready(self):

            return None

        def run_step(self, **_kwargs):

            return StepResult(ok=False, output="diagnose crashed")

    monkeypatch.setattr("odoo_migrator.runner.providers.get", lambda _name: _Provider())
    monkeypatch.setattr(
        "odoo_migrator.runner.prompting.system_prompt", lambda _step, _ctx: "standing"
    )
    ctx = SimpleNamespace(
        migration=SimpleNamespace(provider_override="", model_override=""),
        module=SimpleNamespace(technical_name="sale_custom"),
        cartridge=SimpleNamespace(model=ModelRef(provider="anthropic", name="claude-opus-5")),
        provider=None,
        prior_answers={},
        last_answer="",
        last_failure="upgrade failed",
        previous_diff="",
        sessions={},
    )

    result = run_agent_step(
        Step(kind="agent", id="diagnose", sessions=["new"], tools=[], prompt="why"), ctx
    )

    assert not result.ok
    assert ctx.last_failure == "upgrade failed"
