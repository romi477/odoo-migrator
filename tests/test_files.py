"""File API: read/write under the migration's old and new roots only."""

import pytest

from odoo_migrator.db import database
from odoo_migrator.files import PathEscapes, resolve_in_root
from odoo_migrator.models import Migration


def _open(client, tmp_path, name="Acme"):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "sale_custom").mkdir()
    (old / "sale_custom" / "__init__.py").write_text("from . import models\n", encoding="utf-8")
    client.post(
        "/migrations",
        data={
            "name": name,
            "accent": "violet",
            "source_version": "17.0",
            "target_version": "18.0",
            "old_project_path": str(old),
            "new_project_path": str(new),
        },
        follow_redirects=True,
    )
    with database.connection_context():
        row = Migration.get(Migration.name == name)

    return row, old, new


def test_dotdot_is_refused_not_clamped(tmp_path):
    root = tmp_path / "new"
    root.mkdir()
    (tmp_path / "secret.txt").write_text("nope\n", encoding="utf-8")

    with pytest.raises(PathEscapes):
        resolve_in_root(root, "../secret.txt")
    with pytest.raises(PathEscapes):
        resolve_in_root(root, "sale_custom/../../secret.txt")
    with pytest.raises(PathEscapes):
        resolve_in_root(root, "/etc/passwd")


def test_write_inside_new_root_and_read_from_old(client, tmp_path):
    row, _old, new = _open(client, tmp_path)
    written = client.put(
        f"/api/migrations/{row.id}/files",
        json={"side": "new", "path": "sale_custom/__init__.py", "content": "from . import models\n"},
    )

    assert written.status_code == 200
    assert (new / "sale_custom" / "__init__.py").read_text(encoding="utf-8") == (
        "from . import models\n"
    )
    read = client.get(
        f"/api/migrations/{row.id}/files",
        params={"side": "old", "path": "sale_custom/__init__.py"},
    )

    assert read.status_code == 200
    assert read.json()["content"] == "from . import models\n"
    assert read.json()["side"] == "old"


def test_path_escape_is_403_and_writes_nothing(client, tmp_path):
    row, _old, new = _open(client, tmp_path)
    secret = tmp_path / "secret.txt"
    secret.write_text("keep\n", encoding="utf-8")

    escaped = client.put(
        f"/api/migrations/{row.id}/files",
        json={"side": "new", "path": "../secret.txt", "content": "stolen\n"},
    )

    assert escaped.status_code == 403
    assert escaped.json()["error"] == "path_escapes"
    assert secret.read_text(encoding="utf-8") == "keep\n"
    assert list(new.iterdir()) == []

    absolute = client.get(
        f"/api/migrations/{row.id}/files",
        params={"side": "old", "path": "/etc/passwd"},
    )

    assert absolute.status_code == 403
    nested = client.get(
        f"/api/migrations/{row.id}/files",
        params={"side": "new", "path": "sale_custom/../../secret.txt"},
    )

    assert nested.status_code == 403


def test_symlink_out_of_root_is_403(client, tmp_path):
    row, _old, new = _open(client, tmp_path)
    outside = tmp_path / "outside.py"
    outside.write_text("SECRET = 1\n", encoding="utf-8")
    (new / "hook.py").symlink_to(outside)

    leaked = client.get(
        f"/api/migrations/{row.id}/files",
        params={"side": "new", "path": "hook.py"},
    )

    assert leaked.status_code == 403
    assert leaked.json()["error"] == "path_escapes"


def test_missing_file_is_404(client, tmp_path):
    row, _old, _new = _open(client, tmp_path)

    missing = client.get(
        f"/api/migrations/{row.id}/files",
        params={"side": "old", "path": "no_such_module/__init__.py"},
    )

    assert missing.status_code == 404
    assert missing.json()["error"] == "not_found"


def test_unknown_migration_is_404(client):
    response = client.get(
        "/api/migrations/missing/files",
        params={"side": "old", "path": "a.py"},
    )

    assert response.status_code == 404


def test_a_file_that_is_not_text_is_415(client, tmp_path):
    """A project holds .mo, .png and the odd vendored blob. Reading one used
    to raise UnicodeDecodeError out of the route — a 500 and a traceback,
    which reads as the app being broken."""
    row, old, _new = _open(client, tmp_path)
    (old / "sale_custom" / "messages.mo").write_bytes(b"\xff\xfe\x00compiled")

    binary = client.get(
        f"/api/migrations/{row.id}/files",
        params={"side": "old", "path": "sale_custom/messages.mo"},
    )

    assert binary.status_code == 415
    assert binary.json()["error"] == "not_text"


def test_the_key_is_minted_once_and_kept_private(tmp_path):
    from odoo_migrator.authkey import load_or_create

    path = tmp_path / "api.key"
    minted = load_or_create(path)

    assert len(minted) >= 32
    assert load_or_create(path) == minted
    assert path.stat().st_mode & 0o077 == 0


def test_an_older_database_gains_the_columns_it_is_missing(tmp_path):
    """create_tables leaves an existing table alone, so every column added
    since a database was made is missing from it and the first query raises
    "no such column"."""
    from peewee import SqliteDatabase

    from odoo_migrator.db import database, init_db
    from odoo_migrator.models import Module

    path = tmp_path / "old.sqlite"
    older = SqliteDatabase(str(path))
    older.connect()
    older.execute_sql(
        "CREATE TABLE module ("
        " id INTEGER PRIMARY KEY, migration_id TEXT NOT NULL,"
        " technical_name TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'queued')"
    )
    older.execute_sql(
        "INSERT INTO module (migration_id, technical_name, status)"
        " VALUES ('m1', 'sale_custom', 'passed')"
    )
    older.close()

    init_db(path)

    with database.connection_context():
        columns = {column.name for column in database.get_columns("module")}
        kept = Module.get(Module.technical_name == "sale_custom")

    assert {"license", "author", "depends", "absent_from_odoo"} <= columns
    assert kept.status == "passed"
