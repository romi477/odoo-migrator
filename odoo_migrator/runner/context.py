"""What every step receives, and what it hands back."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from odoo_migrator.models import Migration, Module
from odoo_migrator.runner.cartridge import Cartridge


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
    previous_diff: str = ""
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
    # ERROR and CRITICAL records Odoo logged during this lap's upgrade and
    # tests. `verdict` fails a lap that has any; the loop clears it per lap.
    log_errors: list[str] = field(default_factory=list)
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
