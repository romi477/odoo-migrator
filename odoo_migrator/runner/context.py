"""What every step receives, and what it hands back."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from odoo_migrator.models import Migration, Module
from odoo_migrator.runner.cartridge import Cartridge


class Stopped(Exception):
    """A person stopped this run. Not a failure of the module.

    The message says where the run was when it heard: "before lint",
    "during tests", "while waiting for an answer".
    """


def check_stop(ctx, where: str) -> None:
    """Raise `Stopped` if a person has asked this run to end.

    The runner asks before every step. A step or a wait that can outlast a
    person's patience asks again inside: between two test runs, or while it
    waits for an answer or for a database another job holds.
    """
    asked = getattr(ctx, "stop_asked", None)
    if callable(asked) and asked():
        raise Stopped(where)


@dataclass
class StepContext:
    job_id: str
    attempt: int
    migration: Migration
    module: Module
    cartridge: Cartridge
    sheller: object
    sessions: dict[str, str]
    emit: Callable[[dict], None]
    write_keys: dict[str, str] = field(default_factory=dict)
    # container, database and odoo_bin per side, as `open_session` wants them.
    # The daemon does not give `odoo_bin` back, so reopening a session means
    # remembering what it was opened with.
    session_targets: dict[str, dict] = field(default_factory=dict)
    prior_answers: dict[str, str] = field(default_factory=dict)
    # The most recent agent answer. `ask_human` asks with it, so the human
    # reads the diagnosis instead of a constant.
    last_answer: str = ""
    # The module now against the module as the job found it. Not the last
    # step's diff: after `write_test` that was the test, and `patch` could
    # not see the port it was asked to fix.
    previous_diff: str = ""
    # The target side's module files when the job began; `previous_diff` is
    # measured from here.
    baseline_files: dict[str, str] = field(default_factory=dict)
    last_failure: str = ""
    # A provider to use instead of the one the cartridge names. Only a test
    # sets this, and what it sets is a real `Provider`, so the step runs the
    # same code a live turn does.
    provider: object = None
    # Seconds one exec may take before it is interrupted. None uses the
    # catalog's own ceiling.
    command_timeout: float | None = None
    # The last run_test payload, so test_result can answer without a second call.
    last_test: dict | None = None
    # What a step that writes a characterization test has done. The runner
    # clears these when such a step starts; `write_source_test` fills them
    # and `source_git.settle_tests` reads them when it ends.
    source_tests_had: bool | None = None
    source_tests_written: bool = False
    source_green: bool = False
    # Test methods in each file the step wrote, by path: a run proves the tests
    # green only when it ran at least this many.
    source_test_methods: dict[str, int] = field(default_factory=dict)
    # What is wrong with each file the step wrote that no run can show: a test
    # method that asserts nothing, or a file that does not parse.
    source_test_flaws: dict[str, list[str]] = field(default_factory=dict)
    # The Sheller session and command that proved the tests green. They go in
    # the commit, so a test can be traced to the run that vouched for it. The
    # session's write key never does: it is a secret, and the id is not.
    source_green_session: str = ""
    source_green_command: str = ""
    # ERROR and CRITICAL records Odoo logged during this lap's upgrade and
    # tests. `verdict` fails a lap that has any; the loop clears it per lap.
    log_errors: list[str] = field(default_factory=list)
    # The flags raised so far in this lap. The loop owns the set; `verdict`
    # reads the ones it gives a meaning to.
    lap_flags: set[str] = field(default_factory=set)
    # What the job reports besides its verdict: `{"text", "source"}`, as the
    # result payload's findings. The checkpoint shows them.
    findings: list[dict] = field(default_factory=list)
    # Questions a `batch_defer` migration did not stop for:
    # `{"question", "assumption"}`, as the result payload's assumptions.
    assumptions: list[dict] = field(default_factory=list)
    # Set by the native runner so a tool call lands on the step that made it.
    step_run_id: int | None = None
    # The same step, by name, for the run log.
    step_run_label: str = ""
    record_tool: Callable | None = None
    # When set, ask_human parks, waits, and returns the human's answer.
    wait_for_human: Callable | None = None
    # The lock on the target database this job holds while it runs.
    target_lock: object = None
    target_held: bool = False
    # Whether a person has asked this run to stop. Read through `check_stop`.
    stop_asked: Callable[[], bool] | None = None


@dataclass
class StepResult:
    ok: bool
    flags: set[str] = field(default_factory=set)
    answer: str = ""
    output: str = ""
    # The provider's own account of the turn: how many rounds, what the model
    # said, why it stopped. One line of `output` is not enough to tell a
    # model that refused from a model that was never asked, and that is the
    # question a failed step leaves behind.
    detail: str = ""
    # What this turn spent, as the provider observed it. The runner does not
    # read a specific SDK's response object to find these.
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
