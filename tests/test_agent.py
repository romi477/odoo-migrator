"""The agent turn ends on report, or on the budget when the model never does.

The double is a provider, so these run the same `run_step` a live turn does.
"""

import pytest
from doubles.model import ModelDouble

from odoo_migrator.db import database, init_db
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Migration, Module
from odoo_migrator.runner.cartridge import Budget, Step
from odoo_migrator.runner.context import StepContext
from odoo_migrator.runner.providers.scripted import ScriptedProvider
from odoo_migrator.runner.tools import report_tool, tools_for
from odoo_migrator.sheller.fake import FakeShellerClient


def _run(step, ctx, model, budget):

    return ScriptedProvider(model).run_step(
        system="standing orders",
        prompt=step.prompt,
        tools=tools_for(step, ctx),
        report=report_tool(step),
        model="claude-opus-5",
        budget=budget,
    )


AGENT_STEP = Step(
    kind="agent",
    id="probe",
    sessions=["new"],
    tools=["exec"],
    prompt="x",
)


@pytest.fixture
def step_context(tmp_path):
    init_db(tmp_path / "agent.sqlite")
    sheller = FakeShellerClient()
    module_root = tmp_path / "new" / "sale_custom"
    module_root.mkdir(parents=True)
    created = create_migration(
        name="Acme",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path=str(tmp_path / "old"),
        new_project_path=str(tmp_path / "new"),
    )
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])
        module = Module.create(
            migration=migration,
            technical_name="sale_custom",
            new_path=str(module_root),
        )
    opened = sheller.open_session(
        container="odoo-new",
        database="acme",
        odoo_bin="/opt/odoo/odoo-bin",
        owner={"kind": "agent"},
        allow_commit=True,
    )

    def build():
        return StepContext(
            job_id="job-1",
            attempt=1,
            migration=migration,
            module=module,
            cartridge=None,
            sheller=sheller,
            sessions={"new": opened["id"]},
            write_keys={"new": opened["write_key"]},
            emit=lambda _event: None,
        )

    return build


def test_a_step_that_never_converges_ends_at_the_budget(step_context):
    """A double that always asks for another tool call must not hang a run."""
    model = ModelDouble(always_calls="exec")

    result = _run(AGENT_STEP, step_context(), model, Budget(tool_calls=5))

    assert result.ok is False
    assert "budget" in result.output.lower()
    assert model.calls == 5


def test_a_report_ends_the_step_with_its_flags(step_context):
    model = ModelDouble(
        turns=[
            {"name": "exec", "input": {"side": "new", "code": "1"}},
            {"name": "report", "input": {"answer": "done", "small_fix": True}},
        ]
    )
    step = Step(
        kind="agent",
        id="diagnose",
        sessions=["new"],
        tools=["exec"],
        raises=["small_fix"],
        prompt="x",
    )

    result = _run(step, step_context(), model, Budget())

    assert result.ok is True
    assert result.answer == "done"
    assert result.flags == {"small_fix"}
