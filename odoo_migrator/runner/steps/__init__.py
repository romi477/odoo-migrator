"""Deterministic steps. An agent step is not one of these."""

from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext, StepResult
from odoo_migrator.runner.steps import ask_human, lint, smoke, tests, upgrade, verdict

_RUNNERS = {
    "lint": lint.run,
    "upgrade": upgrade.run,
    "tests": tests.run,
    "smoke": smoke.run,
    "verdict": verdict.run,
    "ask_human": ask_human.run,
}


def run_step(step: Step, ctx: StepContext) -> StepResult:
    runner = _RUNNERS.get(step.kind)
    if runner is None:
        raise KeyError(step.kind)

    return runner(step, ctx)
