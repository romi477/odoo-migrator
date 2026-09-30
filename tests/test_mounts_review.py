"""Findings of the whole-branch review of addons-from-mounts, pinned.

Each case is a layout a real compose file has: one checkout mounted by both
containers, an Enterprise checkout per version, a symlinked host directory.
"""

import json
import os
from pathlib import Path

import pytest

from odoo_migrator.db import database, init_db
from odoo_migrator.graph import resolve_graph, snapshot_mounts
from odoo_migrator.migrations import create_migration, save_side_container
from odoo_migrator.models import Migration, Module
from odoo_migrator.mounts import too_broad
from odoo_migrator.provision import FakeComposeEngine
from odoo_migrator.runner.tools import TOOL_DESCRIPTIONS, ToolRefused, _write_file
from odoo_migrator.sheller import FakeShellerClient

PROJECT_17 = {"host": "/opt/sandbox/17/project", "container": "/opt/project", "read_only": False}
PROJECT_18 = {"host": "/opt/sandbox/18/project", "container": "/opt/project", "read_only": False}


def _migration(tmp_path, old_mounts, new_mounts, *, old_project="/opt/sandbox/17"):
    init_db(tmp_path / "review.sqlite")
    created = create_migration(
        name="Acme",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path=old_project,
        new_project_path="/opt/sandbox/18",
    )
    save_side_container(created["id"], "old", "odoo-old")
    save_side_container(created["id"], "new", "odoo-new")
    engine = FakeComposeEngine()
    engine.add_container(name="odoo-old", image="odoo:17", status="running", labels={},
                         host_ports=[], mounts=old_mounts)
    engine.add_container(name="odoo-new", image="odoo:18", status="running", labels={},
                         host_ports=[], mounts=new_mounts)
    snapshot_mounts(engine, created["id"])

    return created


def _module(name):
    with database.connection_context():

        return Module.get(Module.technical_name == name)


def _with(fake, *rows):
    for name, root, extra in rows:
        fake.graph_modules.append(
            {"name": name, "depends": ["sale"], "state": "installed",
             "addons_root": root, "path": f"{root}/{name}",
             "author": "Acme", "license": "OPL-1", "is_core": False, **extra}
        )

    return fake


def test_a_directory_both_sides_mount_from_one_checkout_is_not_ported_in_place(tmp_path):
    shared = {"host": "/opt/sources/external", "container": "/opt/external-addons", "read_only": False}
    created = _migration(tmp_path, [PROJECT_17, shared], [PROJECT_18, shared])

    resolve_graph(_with(FakeShellerClient(), ("queue_x", "/opt/external-addons", {})), created["id"])

    queue = _module("queue_x")
    assert queue.path_problem == "shared with the old side"
    assert queue.new_path == ""


def test_an_lgpl_module_in_enterprise_is_not_ported_into_the_enterprise_checkout(tmp_path):
    enterprise_17 = {"host": "/opt/odoo-src/enterprise17", "container": "/opt/enterprise", "read_only": False}
    enterprise_18 = {"host": "/opt/odoo-src/enterprise18", "container": "/opt/enterprise", "read_only": False}
    created = _migration(tmp_path, [PROJECT_17, enterprise_17], [PROJECT_18, enterprise_18])

    # The default graph holds account_reports (OEEL, Odoo's own) and
    # web_enterprise_helper (LGPL-3, kept) both under /opt/enterprise.
    resolve_graph(FakeShellerClient(), created["id"])

    helper = _module("web_enterprise_helper")
    assert "enterprise18" not in helper.new_path
    assert helper.new_path == "/opt/sandbox/18/project/web_enterprise_helper"
    assert helper.new_container_path == "/opt/project/web_enterprise_helper"


def test_resolve_sends_only_the_mounts_under_the_old_project(tmp_path):
    external = {"host": "/opt/sources/external", "container": "/opt/external-addons", "read_only": True}
    created = _migration(tmp_path, [PROJECT_17, external], [PROJECT_18])
    fake = FakeShellerClient()

    resolve_graph(fake, created["id"])

    assert '["/opt/project"]' in fake.execs[-1]
    assert "/opt/external-addons" not in fake.execs[-1]


def test_a_relative_write_cannot_reach_a_guessed_directory_when_mounts_are_known(tmp_path):
    new_project = tmp_path / "new"
    ext18 = tmp_path / "ext18"
    (ext18 / "sale_x").mkdir(parents=True)
    (new_project / "sale_x").mkdir(parents=True)
    init_db(tmp_path / "write.sqlite")
    created = create_migration(
        name="Acme", accent="violet", source_version="17.0", target_version="18.0",
        old_project_path=str(tmp_path / "old"), new_project_path=str(new_project),
    )
    new_mounts = [
        {"host": str(new_project), "container": "/opt/project", "read_only": False},
        {"host": str(ext18), "container": "/opt/external-addons", "read_only": False},
    ]
    with database.connection_context():
        Migration.update(new_mounts=json.dumps(new_mounts)).where(Migration.id == created["id"]).execute()
        migration = Migration.get_by_id(created["id"])
        module = Module.create(
            migration=migration, technical_name="sale_x",
            new_path=str(ext18 / "sale_x"), new_container_path="/opt/external-addons/sale_x",
        )

    class Ctx:
        pass

    ctx = Ctx()
    ctx.migration, ctx.module = migration, module

    with pytest.raises(ToolRefused):
        _write_file(ctx, "new", "sale_x/models/x.py", "")
    assert not (new_project / "sale_x" / "models").exists()
    _write_file(ctx, "new", "/opt/external-addons/sale_x/models/x.py", "X = 1\n")
    assert (ext18 / "sale_x" / "models" / "x.py").read_text() == "X = 1\n"


def test_the_tool_descriptions_name_container_paths():
    assert "container" in TOOL_DESCRIPTIONS["read_file"]
    assert "container" in TOOL_DESCRIPTIONS["write_file"]


def test_a_symlink_to_the_home_directory_is_too_broad(tmp_path):
    link = tmp_path / "link"
    os.symlink(Path.home(), link)

    assert too_broad(str(link)) is not None


def test_a_module_deeper_than_its_mount_goes_straight_into_the_new_project(tmp_path):
    """/opt/external-addons/integration-queue is an addons_path entry of its
    own. Keeping `integration-queue/` under the new project put the port
    where no addons_path entry reaches."""
    external = {"host": "/opt/sources/external", "container": "/opt/external-addons", "read_only": True}
    created = _migration(tmp_path, [PROJECT_17, external], [PROJECT_18])

    resolve_graph(
        _with(FakeShellerClient(), ("q_x", "/opt/external-addons/integration-queue", {})),
        created["id"],
    )

    queued = _module("q_x")
    assert queued.new_container_path == "/opt/project/q_x"
    assert queued.new_path == "/opt/sandbox/18/project/q_x"


def test_the_card_names_addons_directories_not_mount_points(tmp_path):
    from odoo_migrator.module_paths import addon_directories

    aggregate_17 = {"host": "/opt/int/19/odoo-sh", "container": "/opt/odoo-sh", "read_only": False}
    aggregate_18 = {"host": "/opt/int/20/odoo-sh", "container": "/opt/odoo-sh", "read_only": False}
    created = _migration(tmp_path, [PROJECT_17, aggregate_17], [PROJECT_18, aggregate_18])
    resolve_graph(
        _with(FakeShellerClient(), ("conn_x", "/opt/odoo-sh/connectors-extensions", {})),
        created["id"],
    )

    found = addon_directories(created["id"])

    assert "/opt/int/19/odoo-sh/connectors-extensions" in found["old"]
    assert "/opt/int/19/odoo-sh" not in found["old"]
    assert "/opt/sandbox/17/project" in found["old"]
    assert "/opt/int/20/odoo-sh/connectors-extensions" in found["new"]


def test_a_module_the_target_does_not_load_is_stopped_at_build_graph(tmp_path):
    """Two jobs met this live: the port was written where the target mounts
    it, but that directory is not on the target's addons_path. One agent
    asked; the other copied the module into /opt/project through exec, and
    the two copies drifted apart. It is found here now, before any job."""
    from odoo_migrator.graph import mark_migrated_from_target

    aggregate_17 = {"host": "/opt/int/19/odoo-sh", "container": "/opt/odoo-sh", "read_only": False}
    aggregate_18 = {"host": "/opt/int/20/odoo-sh", "container": "/opt/odoo-sh", "read_only": False}
    created = _migration(tmp_path, [PROJECT_17, aggregate_17], [PROJECT_18, aggregate_18])
    fake = _with(FakeShellerClient(), ("conn_x", "/opt/odoo-sh/connectors-extensions", {}))
    fake.addons_path_by_container["odoo-new"] = ["/opt/odoo/addons", "/opt/project"]
    resolve_graph(fake, created["id"])

    outcome = mark_migrated_from_target(fake, created["id"])

    connector = _module("conn_x")
    assert connector.path_problem == "not on the target's addons_path"
    assert _module("sale_custom").path_problem == ""
    assert outcome["off_path"] == 1


def test_a_target_that_loads_the_directory_raises_nothing(tmp_path):
    from odoo_migrator.graph import mark_migrated_from_target

    aggregate_17 = {"host": "/opt/int/19/odoo-sh", "container": "/opt/odoo-sh", "read_only": False}
    aggregate_18 = {"host": "/opt/int/20/odoo-sh", "container": "/opt/odoo-sh", "read_only": False}
    created = _migration(tmp_path, [PROJECT_17, aggregate_17], [PROJECT_18, aggregate_18])
    fake = _with(FakeShellerClient(), ("conn_x", "/opt/odoo-sh/connectors-extensions", {}))
    fake.addons_path_by_container["odoo-new"] = [
        "/opt/odoo/addons", "/opt/project", "/opt/odoo-sh/connectors-extensions",
    ]
    resolve_graph(fake, created["id"])

    assert mark_migrated_from_target(fake, created["id"])["off_path"] == 0
    assert _module("conn_x").path_problem == ""
