"""Which code runs a step kind. Agent steps arrive in a later task."""

from __future__ import annotations

from odoo_migrator.runner.steps import ask_human, lint, tests, upgrade, verdict

_RUNNERS = {
    "lint": lint.run,
    "upgrade": upgrade.run,
    "tests": tests.run,
    "verdict": verdict.run,
    "ask_human": ask_human.run,
}


def resolve(kind: str):
    if kind == "agent":
        raise NotImplementedError(kind)
    runner = _RUNNERS.get(kind)
    if runner is None:
        raise NotImplementedError(kind)

    return runner
