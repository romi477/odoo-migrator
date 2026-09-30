"""read_file and write_file take container paths when the mounts are known."""

import json

import pytest

from odoo_migrator.db import database, init_db
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Migration, Module
from odoo_migrator.module_paths import agent_path, allowed_roots
from odoo_migrator.runner.tools import ToolRefused, _read_file, _write_file


class Ctx:
    def __init__(self, migration, module):
        self.migration = migration
        self.module = module


@pytest.fixture
def ctx(tmp_path):
    init_db(tmp_path / "tools.sqlite")
    old_host = tmp_path / "old-src"
    ext_host = tmp_path / "old-ext"
    conf_host = tmp_path / "conf"
    new_host = tmp_path / "new-src"
    for directory in (old_host / "sale_x" / "models", ext_host / "queue_x", conf_host, new_host / "sale_x"):
        directory.mkdir(parents=True)
    (old_host / "sale_x" / "models" / "sale.py").write_text("OLD = 1\n")
    (conf_host / "odoo.conf").write_text("admin_passwd = secret\n")
    (tmp_path / "outside.txt").write_text("nope\n")
    created = create_migration(
        name="Acme", accent="violet", source_version="17.0", target_version="18.0",
        old_project_path=str(old_host), new_project_path=str(new_host),
    )
    old_mounts = [
        {"host": str(old_host), "container": "/opt/project", "read_only": False},
        {"host": str(ext_host), "container": "/opt/external-addons", "read_only": True},
        {"host": str(conf_host), "container": "/etc/odoo", "read_only": True},
    ]
    new_mounts = [{"host": str(new_host), "container": "/opt/project", "read_only": False}]
    with database.connection_context():
        Migration.update(old_mounts=json.dumps(old_mounts), new_mounts=json.dumps(new_mounts)).where(
            Migration.id == created["id"]
        ).execute()
        migration = Migration.get_by_id(created["id"])
        module = Module.create(
            migration=migration, technical_name="sale_x",
            old_path=str(old_host / "sale_x"), new_path=str(new_host / "sale_x"),
            old_container_path="/opt/project/sale_x", new_container_path="/opt/project/sale_x",
        )

    return Ctx(migration, module)


def test_a_traceback_path_reads_the_host_file(ctx):
    assert _read_file(ctx, "old", "/opt/project/sale_x/models/sale.py") == "OLD = 1\n"


def test_a_mount_with_no_plan_module_is_not_a_root(ctx):
    roots = [mount["container"] for mount in allowed_roots(ctx.migration, "old")]

    assert roots == ["/opt/project"]
    with pytest.raises(ToolRefused, match="source"):
        _read_file(ctx, "old", "/etc/odoo/odoo.conf")
    with pytest.raises(ToolRefused):
        _read_file(ctx, "old", "/opt/external-addons/queue_x/__init__.py")


def test_dot_dot_cannot_climb_out_of_a_root(ctx):
    with pytest.raises(ToolRefused):
        _read_file(ctx, "old", "/opt/project/../../outside.txt")
    with pytest.raises(ToolRefused):
        _read_file(ctx, "old", "/opt/project/sale_x/../../../outside.txt")


def test_a_write_lands_in_the_module_on_the_new_side(ctx, tmp_path):
    written = _write_file(ctx, "new", "/opt/project/sale_x/models/sale.py", "NEW = 1\n")

    assert (tmp_path / "new-src" / "sale_x" / "models" / "sale.py").read_text() == "NEW = 1\n"
    assert written.endswith("sale_x/models/sale.py")
    with pytest.raises(ToolRefused):
        _write_file(ctx, "new", "/opt/project/other_x/x.py", "")


def test_a_relative_path_keeps_working(ctx):
    assert _read_file(ctx, "old", "sale_x/models/sale.py") == "OLD = 1\n"


def test_the_prompt_names_the_container_path(ctx):
    assert agent_path(ctx.migration, ctx.module, "old") == "/opt/project/sale_x"
    assert agent_path(ctx.migration, ctx.module, "new") == "/opt/project/sale_x"


def test_without_mounts_the_prompt_keeps_the_relative_path(ctx):
    with database.connection_context():
        Migration.update(old_mounts="[]", new_mounts="[]").where(Migration.id == ctx.migration.id).execute()
        ctx.migration = Migration.get_by_id(ctx.migration.id)

    assert agent_path(ctx.migration, ctx.module, "old") == "sale_x"
