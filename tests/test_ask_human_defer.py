"""batch_defer records each question once, however many laps ask it."""

from types import SimpleNamespace

from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext
from odoo_migrator.runner.steps import ask_human


def test_the_same_question_on_every_lap_is_one_assumption():
    ctx = StepContext(
        job_id="job-1", attempt=1,
        migration=SimpleNamespace(ask_human_mode="batch_defer"),
        module=SimpleNamespace(technical_name="sale_x"),
        cartridge=None, sheller=None, sessions={}, emit=lambda _event: None,
    )
    ctx.last_answer = "Which currency does the shop report in?"

    for lap in (1, 2, 3):
        ctx.attempt = lap
        assert ask_human.run(Step(kind="ask_human"), ctx).ok

    assert len(ctx.assumptions) == 1
