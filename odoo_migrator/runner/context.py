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
    # The model client for agent steps. Deterministic steps never touch it.
    client: object = None
    # Seconds one exec may take before it is interrupted. None uses the
    # catalog's own ceiling.
    command_timeout: float | None = None
    # The last run_test payload, so test_result can answer without a second call.
    last_test: dict | None = None
    # Set by the native runner so a tool call lands on the step that made it.
    step_run_id: int | None = None
    record_tool: Callable | None = None
    # When set, ask_human parks, waits, and returns the human's answer.
    wait_for_human: Callable | None = None


@dataclass
class StepResult:
    ok: bool
    flags: set[str] = field(default_factory=set)
    answer: str = ""
    output: str = ""
    # What this turn spent, as the provider observed it. The runner does not
    # read a specific SDK's response object to find these.
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
