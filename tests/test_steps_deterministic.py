"""Deterministic steps: lint, the test catalogue, and the verdict."""

import pytest

from odoo_migrator.db import database, init_db
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Migration, Module
from odoo_migrator.runner.cartridge import Step, load_cartridge
from odoo_migrator.runner.context import StepContext
from odoo_migrator.runner.steps import lint, tests, verdict
from odoo_migrator.sheller import FakeShellerClient

_CARTRIDGE = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-opus-5}
pipeline:
  - step: verdict
on_failure:
  pipeline: []
  retry:
    resume_at: verdict
"""


@pytest.fixture
def sheller_fake():

    return FakeShellerClient()


@pytest.fixture
def step_context(tmp_path, sheller_fake):
    init_db(tmp_path / "steps.sqlite")

    def build(*, new_module_path=None, strictness="basic", technical_name="sale_custom"):
        module_root = new_module_path or (tmp_path / "new" / technical_name)
        module_root.mkdir(parents=True, exist_ok=True)
        project = module_root.parent
        created = create_migration(
            name="Acme",
            accent="violet",
            source_version="17.0",
            target_version="18.0",
            old_project_path=str(tmp_path / "old"),
            new_project_path=str(project),
        )
        with database.connection_context():
            migration = Migration.get_by_id(created["id"])
            migration.strictness_default = strictness
            migration.save()
            module = Module.create(
                migration=migration,
                technical_name=technical_name,
                new_path=str(module_root),
            )
        opened = sheller_fake.open_session(
            container="odoo-new",
            database="acme",
            odoo_bin="/opt/odoo/odoo-bin",
            owner={"kind": "agent"},
            allow_commit=True,
        )

        return StepContext(
            job_id="job-1",
            attempt=1,
            migration=migration,
            module=module,
            cartridge=load_cartridge(_CARTRIDGE),
            sheller=sheller_fake,
            sessions={"old": opened["id"], "new": opened["id"]},
            write_keys={"old": opened["write_key"], "new": opened["write_key"]},
            emit=lambda _event: None,
        )

    return build


def test_lint_fails_a_manifest_that_is_not_python(tmp_path, step_context):
    (tmp_path / "__manifest__.py").write_text("{'name': 'x',", encoding="utf-8")
    ctx = step_context(new_module_path=tmp_path)

    result = lint.run(Step(kind="lint"), ctx)

    assert result.ok is False
    assert "lint_failed" in result.flags
    assert "__manifest__.py" in result.output


def test_tests_raises_no_tests_when_the_module_declares_none(step_context, sheller_fake):
    sheller_fake.tests_for("sale_custom", [])
    ctx = step_context()

    result = tests.run(Step(kind="tests"), ctx)

    assert result.ok is True
    assert result.flags == {"no_tests"}


def test_verdict_is_basic_by_default_and_reads_the_migration(step_context):
    ctx = step_context(strictness="strict")

    result = verdict.run(Step(kind="verdict"), ctx)

    assert result.ok is False  # strict wants a probe answer, and there is none
