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
    prior_answers: dict[str, str] = field(default_factory=dict)
    previous_diff: str = ""
    last_failure: str = ""
    # The model client for agent steps. Deterministic steps never touch it.
    client: object = None
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
