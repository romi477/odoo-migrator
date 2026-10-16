"""The provider protocol. One turn of one model; nothing about Odoo."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from odoo_migrator.runner.cartridge import Budget
from odoo_migrator.runner.context import StepResult
from odoo_migrator.runner.tools import BoundTool, ReportTool


@dataclass(frozen=True)
class SettingField:
    """One value the settings page asks for. Secrets are never re-rendered."""

    key: str
    label: str
    secret: bool = True


class Provider(Protocol):
    name: str
    models: frozenset[str]
    settings: tuple[SettingField, ...]

    def accepts_model(self, name: str) -> bool:
        """Whether a cartridge may name this model.

        Not a set lookup: a later provider names models the loader has
        never seen, including ones that carry parameters.
        """

    def ensure_ready(self) -> None:
        """Attach to, or start, whatever this provider needs."""

    def run_step(
        self,
        *,
        system: str,
        prompt: str,
        tools: list[BoundTool],
        report: ReportTool,
        model: str,
        budget: Budget,
        stop_check: Callable[[], None] | None = None,
    ) -> StepResult:
        """Run one turn. The step ends when the model calls ``report``.

        ``system`` is the standing instruction for every step of every
        cartridge: what the agent is, that a tool is the only way to see
        anything, and that a step ends with ``report``. A provider whose
        SDK has no system field prepends it to ``prompt`` rather than
        dropping it.

        ``stop_check`` raises `Stopped` when a person has pressed Stop. A
        provider that owns its loop calls it between model turns: the run
        otherwise heard Stop only when the step ended, which on a local
        model was the better part of an hour away.
        """
