"""Schema changes must not fail because child rows point at the parent."""

from odoo_migrator.db import database, init_db
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Module


def test_adding_a_column_keeps_child_rows(tmp_path):
    path = tmp_path / "schema.sqlite"
    init_db(path)
    created = create_migration(
        name="Acme",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path="/old",
        new_project_path="/new",
    )
    with database.connection_context():
        Module.create(migration=created["id"], technical_name="sale_custom")
        database.execute_sql("ALTER TABLE migration DROP COLUMN provider_override")
        database.execute_sql(
            "CREATE TABLE migration__tmp__ (id VARCHAR(255) NOT NULL PRIMARY KEY)"
        )
    init_db(path)
    with database.connection_context():
        assert Module.select().count() == 1
        names = {
            row[0]
            for row in database.execute_sql(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        columns = {column.name for column in database.get_columns("migration")}

    assert "migration__tmp__" not in names
    assert "provider_override" in columns
