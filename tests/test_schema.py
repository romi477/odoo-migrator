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


_OLD_CARTRIDGEINSTALL = (
    'CREATE TABLE "cartridgeinstall" ("id" INTEGER NOT NULL PRIMARY KEY, '
    '"cartridge_id" VARCHAR(255) NOT NULL, "version" VARCHAR(255) NOT NULL, '
    '"contract_version" VARCHAR(255) NOT NULL, "webhook_path" TEXT NOT NULL, '
    '"capabilities" TEXT NOT NULL, "smoke_status" VARCHAR(255) NOT NULL, '
    '"sha256" VARCHAR(255) NOT NULL, "schema_version" INTEGER NOT NULL, '
    '"body" TEXT NOT NULL)'
)


def _with_old_cartridge_table(path):
    """A database made before the remote executor went: its flow table still
    has two NOT NULL columns the model no longer knows, with no default."""
    init_db(path)
    with database.connection_context():
        database.execute_sql('DROP TABLE "cartridgeinstall"')
        database.execute_sql(_OLD_CARTRIDGEINSTALL)
        database.execute_sql(
            "INSERT INTO cartridgeinstall (cartridge_id, version, contract_version, "
            "webhook_path, capabilities, smoke_status, sha256, schema_version, body) "
            "VALUES ('kept', '1.0.0', '1', 'hook', '{}', 'passed', 'abc', 1, 'text')"
        )


def test_a_flow_can_be_stored_in_a_database_that_kept_the_retired_columns(tmp_path):
    """Importing a new flow, or saving a fork, was a NOT NULL error there."""
    from pathlib import Path

    from odoo_migrator.cartridges import fork_from_text

    path = tmp_path / "old.sqlite"
    _with_old_cartridge_table(path)

    init_db(path)
    text = Path("cartridges/stock.yaml").read_text(encoding="utf-8")
    row = fork_from_text(text.replace("version: 1.0.0", 'version: "1.0.1"', 1))

    assert (row.cartridge_id, row.version) == ("stock", "1.0.1")
    with database.connection_context():
        columns = {column.name for column in database.get_columns("cartridgeinstall")}
    assert "contract_version" not in columns
    assert "webhook_path" not in columns


def test_the_rows_of_that_table_survive_losing_its_retired_columns(tmp_path):
    from odoo_migrator.models import CartridgeInstall

    path = tmp_path / "old.sqlite"
    _with_old_cartridge_table(path)

    init_db(path)

    with database.connection_context():
        row = CartridgeInstall.get(CartridgeInstall.cartridge_id == "kept")
    assert (row.version, row.smoke_status, row.sha256, row.body) == ("1.0.0", "passed", "abc", "text")
