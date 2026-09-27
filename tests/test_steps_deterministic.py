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
    # Not tmp_path itself: its parent would be the new project root, with the
    # old project inside it, and overlapping roots are refused.
    module_dir = tmp_path / "new" / "flat"
    module_dir.mkdir(parents=True)
    (module_dir / "__manifest__.py").write_text("{'name': 'x',", encoding="utf-8")
    ctx = step_context(new_module_path=module_dir)

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


def test_a_module_with_tests_runs_the_specs_the_daemon_gave(step_context, sheller_fake):
    """The runner used to rebuild specs from names and interpolate a mapping,
    so Sheller was handed `module.Class.{'name': ..., 'spec': ...}` and
    answered 400 invalid_test_spec. Every module with tests failed that way,
    and the run blamed the module."""
    sheller_fake.tests_for(
        "sale_custom",
        sheller_fake.catalogue(
            "sale_custom", {"TestSaleCustom": ["test_one", "test_two"]}
        ),
    )
    ctx = step_context()

    result = tests.run(Step(kind="tests"), ctx)

    ran = [row["test"] for row in sheller_fake.tests_run]

    assert ran == [
        "sale_custom.TestSaleCustom.test_one",
        "sale_custom.TestSaleCustom.test_two",
    ]
    assert all("{" not in spec for spec in ran)
    assert result.ok is True


def test_a_class_with_no_methods_runs_as_the_class(step_context, sheller_fake):
    sheller_fake.tests_for(
        "sale_custom", sheller_fake.catalogue("sale_custom", {"TestSaleCustom": []})
    )

    tests.run(Step(kind="tests"), step_context())

    assert [row["test"] for row in sheller_fake.tests_run] == ["sale_custom.TestSaleCustom"]


def test_the_double_refuses_a_shape_the_daemon_never_returns(sheller_fake):
    """A double that accepts a looser shape hides the bug it just caught."""
    from odoo_migrator.sheller.errors import ShellerError

    with pytest.raises(ShellerError, match="as the daemon returns it"):
        sheller_fake.tests_for("sale_custom", [{"name": "TestSaleCustom"}])
    with pytest.raises(ShellerError, match="as the daemon returns it"):
        sheller_fake.tests_for(
            "sale_custom",
            [
                {
                    "name": "TestSaleCustom",
                    "spec": "sale_custom.TestSaleCustom",
                    "methods": [{"name": "test_one"}],
                }
            ],
        )
