"""A row saved from one thread does not write back what another thread
changed meanwhile. `save()` wrote every column: Build graph during a run
read a module as `running`, a job finished, and the graph wrote `running`
back over `passed`."""

from odoo_migrator.db import database, init_db
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Module


def test_a_stale_row_saves_only_what_its_writer_changed(tmp_path):
    init_db(tmp_path / "db.sqlite")
    created = create_migration(
        name="Acme", accent="violet", source_version="17.0", target_version="18.0",
        old_project_path=str(tmp_path / "old"), new_project_path=str(tmp_path / "new"),
    )
    with database.connection_context():
        module = Module.create(migration=created["id"], technical_name="sale_custom", status="running")
        stale = Module.get_by_id(module.id)
        # Another thread: the job finished.
        Module.update(status="passed").where(Module.id == module.id).execute()
        stale.depends = '["sale"]'
        stale.save()

        fresh = Module.get_by_id(module.id)
    assert fresh.status == "passed" and fresh.depends == '["sale"]'
