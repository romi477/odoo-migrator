"""Which code runs a step kind."""

from __future__ import annotations

from odoo_migrator.runner.prompting import run_agent_step
from odoo_migrator.runner.steps import ask_human, lint, tests, upgrade, verdict

_RUNNERS = {
    "agent": run_agent_step,
    "lint": lint.run,
    "upgrade": upgrade.run,
    "tests": tests.run,
    "verdict": verdict.run,
    "ask_human": ask_human.run,
}


def resolve(kind: str):
    runner = _RUNNERS.get(kind)
    if runner is None:
        raise NotImplementedError(kind)

    return runner
