"""Tool callables stay inside the sessions and the project the step declared."""

import pytest

from odoo_migrator.db import database, init_db
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Migration, Module
from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext
from odoo_migrator.runner.tools import ToolRefused, report_tool, tools_for
from odoo_migrator.sheller.fake import FakeShellerClient


@pytest.fixture
def step_context(tmp_path):
    init_db(tmp_path / "tools.sqlite")
    sheller = FakeShellerClient()
    module_root = tmp_path / "new" / "sale_custom"
    module_root.mkdir(parents=True)
    (tmp_path / "old").mkdir()
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
            sessions={"old": opened["id"], "new": opened["id"]},
            write_keys={"old": opened["write_key"], "new": opened["write_key"]},
            emit=lambda _event: None,
        )

    return build


def test_a_tool_cannot_reach_a_session_the_step_did_not_declare(step_context):
    step = Step(kind="agent", id="probe", sessions=["new"], tools=["exec"], prompt="x")
    tools = tools_for(step, step_context())
    exec_tool = next(t for t in tools if t.name == "exec")

    with pytest.raises(ToolRefused) as refused:
        exec_tool(side="old", code="env.cr.execute('select 1')")

    assert "old" in str(refused.value)


def test_the_report_tool_schema_is_the_declared_flags():
    step = Step(
        kind="agent",
        id="diagnose",
        sessions=["new"],
        tools=["exec"],
        raises=["needs_human", "small_fix"],
        prompt="x",
    )

    schema = report_tool(step).input_schema

    assert set(schema["properties"]) == {"answer", "needs_human", "small_fix"}


def test_write_file_refuses_a_path_that_escapes_the_project(step_context):
    step = Step(kind="agent", id="port", sessions=["new"], tools=["write_file"], prompt="x")
    tools = tools_for(step, step_context())
    write = next(t for t in tools if t.name == "write_file")

    with pytest.raises(ToolRefused) as refused:
        write(side="new", path="../outside.py", content="x = 1\n")

    assert "escapes" in str(refused.value)


def test_a_report_cannot_ask_for_a_patch_and_a_rewrite_at_once():
    """The spec calls them mutually exclusive. Both raised, the whole failure
    lane ran: `patch` wrote a fix and `rewrite`, which starts from an empty
    module directory, deleted it in the same lap."""
    diagnose = Step(
        kind="agent",
        id="diagnose",
        prompt="why",
        raises=["needs_human", "small_fix", "start_over"],
    )
    report = report_tool(diagnose)

    refused = report.refusal({"small_fix": True, "start_over": True})

    assert "small_fix and start_over cannot both be true" in refused
    assert "Report again" in refused
    assert report(small_fix=True, start_over=True) == refused


def test_one_repair_at_a_time_is_accepted():
    diagnose = Step(
        kind="agent",
        id="diagnose",
        prompt="why",
        raises=["needs_human", "small_fix", "start_over"],
    )
    report = report_tool(diagnose)

    assert report.refusal({"small_fix": True, "answer": "a field moved"}) == ""
    assert report.refusal({"start_over": True, "needs_human": True}) == ""
    assert '"small_fix": true' in report(small_fix=True).replace("True", "true")
