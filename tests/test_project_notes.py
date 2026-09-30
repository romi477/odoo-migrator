"""The understand step reads what the project says about itself.

AGENTS.md, CLAUDE.md and READMEs say what a project is and how its modules
fit together. The app finds them on the old side and hands them to the step
that declares `project_notes`; a model told to go and look would guess at
names with a tool that cannot list a directory.
"""

import pytest

from odoo_migrator.db import database, init_db
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Migration, Module
from odoo_migrator.paths import cartridges_dir
from odoo_migrator.runner.cartridge import Step, load_cartridge_file
from odoo_migrator.runner.context import StepContext
from odoo_migrator.runner.prompting import MAX_NOTE_CHARS, build_context, project_notes
from odoo_migrator.sheller.fake import FakeShellerClient


@pytest.fixture
def ctx(tmp_path):
    init_db(tmp_path / "notes.sqlite")
    old = tmp_path / "old"
    addons = old / "project"
    module_dir = addons / "sale_custom"
    module_dir.mkdir(parents=True)
    (module_dir / "__manifest__.py").write_text("{'name': 'x'}\n")
    (old / "AGENTS.md").write_text("# Acme\nA connector suite for shops.\n")
    (addons / "README.md").write_text("Addons of the Acme suite.\n")
    (module_dir / "README.rst").write_text("Sale custom: margins per shop.\n")
    created = create_migration(
        name="Acme", accent="violet", source_version="17.0", target_version="18.0",
        old_project_path=str(old), new_project_path=str(tmp_path / "new"),
    )
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])
        module = Module.create(
            migration=migration, technical_name="sale_custom", old_path=str(module_dir),
        )

    return StepContext(
        job_id="job-1", attempt=1, migration=migration, module=module,
        cartridge=load_cartridge_file(cartridges_dir() / "stock.yaml"),
        sheller=FakeShellerClient(), sessions={}, emit=lambda _event: None,
    )


def test_notes_come_from_the_project_its_addons_and_the_module(ctx):
    notes = project_notes(ctx)

    assert "AGENTS.md" in notes and "A connector suite for shops." in notes
    assert "project/README.md" in notes and "Addons of the Acme suite." in notes
    assert "sale_custom/README.rst" in notes and "margins per shop" in notes
    # The project first, the module last: broad to narrow.
    assert notes.index("A connector suite") < notes.index("Addons of the Acme") < notes.index("margins")


def test_a_long_note_is_clipped_and_says_so(ctx):
    root = ctx.migration.old_project_path
    with open(f"{root}/AGENTS.md", "w") as handle:
        handle.write("x" * (MAX_NOTE_CHARS + 500))

    notes = project_notes(ctx)

    assert "x" * (MAX_NOTE_CHARS + 1) not in notes
    assert "clipped" in notes


def test_no_notes_is_said_plainly(tmp_path, ctx):
    for name in ("AGENTS.md",):
        (tmp_path / "old" / name).unlink()
    (tmp_path / "old" / "project" / "README.md").unlink()
    (tmp_path / "old" / "project" / "sale_custom" / "README.rst").unlink()

    assert project_notes(ctx) == "(no project notes found)"


def test_only_a_step_that_declares_them_gets_them(ctx):
    with_notes = Step(step="agent", id="understand", context=["project_notes"])
    without = Step(step="agent", id="port", context=["rules"])

    assert "A connector suite for shops." in build_context(with_notes, ctx)
    assert "A connector suite for shops." not in build_context(without, ctx)


def test_the_stock_understand_step_reads_them_first():
    stock = load_cartridge_file(cartridges_dir() / "stock.yaml")
    understand = next(step for step in stock.pipeline if step.id == "understand")

    assert "project_notes" in understand.context
    assert "project notes" in understand.prompt.lower()
