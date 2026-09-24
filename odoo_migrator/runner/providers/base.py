"""The provider protocol. One turn of one model; nothing about Odoo."""

from __future__ import annotations

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
        prompt: str,
        tools: list[BoundTool],
        report: ReportTool,
        model: str,
        budget: Budget,
    ) -> StepResult:
        """Run one turn. The step ends when the model calls ``report``."""
