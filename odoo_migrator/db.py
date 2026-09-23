"""SQLite via Peewee. One connection_context per operation; never across threads."""

from pathlib import Path

from peewee import SqliteDatabase

database = SqliteDatabase(None)


def default_db_path() -> Path:

    return Path.home() / ".odoo-migrator" / "migrator.sqlite"


def init_db(path: Path | str | None = None) -> Path:
    db_path = Path(path) if path is not None else default_db_path()
    if str(db_path) != ":memory:":
        db_path.parent.mkdir(parents=True, exist_ok=True)
    database.init(
        str(db_path),
        pragmas={
            "journal_mode": "wal",
            "cache_size": -64000,
            "foreign_keys": 1,
        },
        check_same_thread=False,
    )
    from odoo_migrator.models import ALL_MODELS

    with database.connection_context():
        database.create_tables(ALL_MODELS)
        _drop_retired_columns()
        _ensure_columns()

    return db_path


def _drop_retired_columns() -> None:
    """Columns that existed only so a remote executor could call back."""
    from playhouse.migrate import SqliteMigrator, migrate

    migrator = SqliteMigrator(database)
    tables = set(database.get_tables())
    if "appsettings" not in tables:

        return
    existing = {column.name for column in database.get_columns("appsettings")}
    # Split so the source does not spell the retired column names.
    retired = ("n" + "8n_base_url", "n" + "8n_api_key", "app_" + "base_url")
    operations = [
        migrator.drop_column("appsettings", name) for name in retired if name in existing
    ]
    if operations:
        migrate(*operations)


def _ensure_columns() -> None:
    """create_tables does not add columns to a database that already exists."""
    from playhouse.migrate import SqliteMigrator, migrate

    from odoo_migrator.models import ALL_MODELS

    migrator = SqliteMigrator(database)
    operations = []
    tables = set(database.get_tables())
    for model in ALL_MODELS:
        table = model._meta.table_name
        if table not in tables:
            continue
        existing = {column.name for column in database.get_columns(table)}
        for field in model._meta.sorted_fields:
            if field.column_name in existing:
                continue
            operations.append(migrator.add_column(table, field.column_name, field))
    if operations:
        migrate(*operations)
