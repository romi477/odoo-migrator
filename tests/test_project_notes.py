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


def _module(ctx):
    from pathlib import Path

    return Path(ctx.module.old_path)


def test_a_readme_is_found_whatever_its_case_and_suffix(ctx):
    """`README.md` was looked up by that exact name. On a Mac that finds
    `readme.md` too, and on Linux it does not; `README` and `README.txt`
    were never looked for."""
    module = _module(ctx)
    (module / "README.rst").unlink()
    (module / "readme.md").write_text("Lower-case readme.\n")
    (module / "README").write_text("No suffix at all.\n")
    (module / "README.txt").write_text("Plain text readme.\n")

    notes = project_notes(ctx)

    assert "sale_custom/readme.md" in notes and "Lower-case readme." in notes
    assert "No suffix at all." in notes
    assert "Plain text readme." in notes


def test_an_oca_readme_directory_is_read(ctx):
    """OCA modules keep their description in `readme/`; README.rst is built
    from it, and not every checkout has the built one."""
    readme = _module(ctx) / "readme"
    readme.mkdir()
    (readme / "USAGE.rst").write_text("Open a shop, set its margin.\n")
    (readme / "DESCRIPTION.md").write_text("Margins per shop, on sale orders.\n")
    (readme / "CONTRIBUTORS.md").write_text("* Somebody <somebody@example.com>\n")

    notes = project_notes(ctx)

    assert "sale_custom/readme/DESCRIPTION.md" in notes
    assert notes.index("Margins per shop") < notes.index("Open a shop")
    assert "Somebody" not in notes


def test_a_docs_directory_is_read(ctx):
    docs = _module(ctx) / "docs"
    (docs / "guide").mkdir(parents=True)
    (docs / "index.rst").write_text("How the margins are computed.\n")
    (docs / "guide" / "setup.md").write_text("Set the margin on the shop.\n")
    (docs / "diagram.png").write_bytes(b"\x89PNG\r\n\x1a\n\x00\xff")

    notes = project_notes(ctx)

    assert "How the margins are computed." in notes
    assert "Set the margin on the shop." in notes
    assert "PNG" not in notes


def test_the_project_does_not_crowd_out_the_modules_own_notes(ctx):
    """Broad to narrow under one budget: three long project files used it up,
    and the module's own README — the one about this module — was cut."""
    from pathlib import Path

    old = Path(ctx.migration.old_project_path)
    for name in ("AGENTS.md", "CLAUDE.md", "README.md"):
        (old / name).write_text("p" * MAX_NOTE_CHARS)

    notes = project_notes(ctx)

    assert "margins per shop" in notes


def _source(ctx) -> str:

    return build_context(Step(step="agent", id="port", context=["module_source"]), ctx)


def test_translations_are_left_out_of_the_module_source(ctx):
    """`i18n/` sorts before `models/`. A module with a few .po files spent the
    whole source budget on translations and the model never saw its code."""
    module = _module(ctx)
    (module / "i18n").mkdir()
    (module / "i18n" / "fr.po").write_text('msgid "Margin"\nmsgstr "Marge"\n' * 10000)
    (module / "i18n" / "sale_custom.pot").write_text('msgid "Margin"\n')
    (module / "models").mkdir()
    (module / "models" / "shop.py").write_text("class Shop: margin = 1\n")

    source = _source(ctx)

    assert "class Shop: margin = 1" in source
    assert "msgstr" not in source and "sale_custom.pot" not in source


def test_the_module_source_puts_the_code_first(ctx):
    module = _module(ctx)
    for relative, text in {
        "static/src/js/shop.js": "// js",
        "data/shops.csv": "id,name",
        "views/shop.xml": "<odoo/>",
        "models/shop.py": "class Shop: pass",
        "__init__.py": "from . import models",
    }.items():
        path = module / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)

    source = _source(ctx)
    order = [
        source.index(f"### {name}")
        for name in (
            "__manifest__.py",
            "__init__.py",
            "models/shop.py",
            "views/shop.xml",
            "data/shops.csv",
            "README.rst",
            "static/src/js/shop.js",
        )
    ]

    assert order == sorted(order)
