"""Build graph snapshots both containers' bind mounts, and reads the graph
from the container directories they name."""

import json

import pytest

from odoo_migrator.contract import ContractError
from odoo_migrator.db import database, init_db
from odoo_migrator.events import EventHub
from odoo_migrator.executor import StubExecutor
from odoo_migrator.graph import (
    build_graph_snippet,
    path_problems_blocking,
    resolve_graph,
    snapshot_mounts,
)
from odoo_migrator.jobs import start_batch, start_job
from odoo_migrator.migrations import create_migration, save_side_container
from odoo_migrator.models import Job, Migration, Module
from odoo_migrator.provision import FakeComposeEngine
from odoo_migrator.provision.errors import DockerUnavailable
from odoo_migrator.sheller import FakeShellerClient
from odoo_migrator.sheller.fake import PROJECT

OLD_MOUNTS = [
    {"host": "/opt/sources/project", "container": "/opt/project", "read_only": False},
    {"host": "/opt/sources/external", "container": "/opt/external-addons", "read_only": True},
]
NEW_MOUNTS = [
    {"host": "/opt/sources/project-18", "container": "/opt/project", "read_only": False},
]


def _migration(tmp_path):
    init_db(tmp_path / "mounts.sqlite")
    created = create_migration(
        name="Acme",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path="/tmp/old",
        new_project_path="/tmp/new",
    )
    save_side_container(created["id"], "source", "odoo-old")
    save_side_container(created["id"], "target", "odoo-new")

    return created


def _engine():
    engine = FakeComposeEngine()
    engine.add_container(name="odoo-old", image="odoo:17", status="running",
                         labels={}, host_ports=[], mounts=OLD_MOUNTS)
    engine.add_container(name="odoo-new", image="odoo:18", status="running",
                         labels={}, host_ports=[], mounts=NEW_MOUNTS)

    return engine


def test_build_graph_stores_both_sides_mounts(tmp_path):
    created = _migration(tmp_path)

    snapshot = snapshot_mounts(_engine(), created["id"])

    assert snapshot == {"source": OLD_MOUNTS, "target": NEW_MOUNTS}
    with database.connection_context():
        row = Migration.get_by_id(created["id"])
        assert json.loads(row.old_mounts) == OLD_MOUNTS
        assert json.loads(row.new_mounts) == NEW_MOUNTS


def test_an_unreachable_engine_stores_not_known(tmp_path):
    created = _migration(tmp_path)

    class Down:
        def list_containers(self):
            raise DockerUnavailable("Docker Engine is not reachable")

    assert snapshot_mounts(Down(), created["id"]) == {"source": [], "target": []}
    # And the graph still builds, from the project directory as before.
    rows = resolve_graph(FakeShellerClient(), created["id"])
    assert {row["technical_name"] for row in rows} >= {"sale_custom", "stock_custom"}
    assert all(row["path_problem"] == "" for row in rows)


def test_the_graph_snippet_reads_container_roots_when_mounts_are_known(tmp_path):
    """Only the mounts inside the old project: an external checkout holds
    dozens of uninstalled modules nobody asked for."""
    created = _migration(tmp_path)
    with database.connection_context():
        Migration.update(old_project_path="/opt/sources/project").where(
            Migration.id == created["id"]
        ).execute()
    snapshot_mounts(_engine(), created["id"])
    fake = FakeShellerClient()

    resolve_graph(fake, created["id"])

    snippet = fake.execs[-1]
    assert '["/opt/project"]' in snippet
    assert "/opt/external-addons" not in snippet
    assert "/opt/sources/project" not in snippet


def test_the_snippet_drops_a_root_that_overlaps_the_odoo_install():
    snippet = build_graph_snippet("/tmp/old", container_roots=["/opt/odoo", "/opt/project"])

    assert "install_root" in snippet
    assert "_overlaps_install" in snippet


def _graph_with(fake, *extra):
    """The default graph plus modules at the given container directories."""
    for name, root in extra:
        fake.graph_modules.append(
            {"name": name, "depends": ["sale"], "state": "installed",
             "addons_root": root, "path": f"{root}/{name}",
             "author": "Acme", "license": "OPL-1", "is_core": False}
        )

    return fake


def _module(name):
    with database.connection_context():

        return Module.get(Module.technical_name == name)


def _placed(tmp_path, engine, *extra):
    created = _migration(tmp_path)
    snapshot_mounts(engine, created["id"])
    resolve_graph(_graph_with(FakeShellerClient(), *extra), created["id"])

    return created


def test_two_source_directories_land_in_their_matching_targets(tmp_path):
    engine = FakeComposeEngine()
    engine.add_container(name="odoo-old", image="odoo:17", status="running", labels={}, host_ports=[],
                         mounts=OLD_MOUNTS)
    engine.add_container(name="odoo-new", image="odoo:18", status="running", labels={}, host_ports=[],
                         mounts=NEW_MOUNTS + [{"host": "/opt/sources/external-18",
                                               "container": "/opt/external-addons", "read_only": False}])
    _placed(tmp_path, engine, ("queue_x", "/opt/external-addons"))

    project = _module("sale_custom")
    assert project.old_container_path == f"{PROJECT}/sale_custom"
    assert project.old_path == "/opt/sources/project/sale_custom"
    assert project.new_container_path == "/opt/project/sale_custom"
    assert project.new_path == "/opt/sources/project-18/sale_custom"
    external = _module("queue_x")
    assert external.old_path == "/opt/sources/external/queue_x"
    assert external.new_container_path == "/opt/external-addons/queue_x"
    assert external.new_path == "/opt/sources/external-18/queue_x"
    assert external.path_problem == ""


def test_a_directory_the_target_lacks_goes_under_the_new_project(tmp_path):
    new_mounts = [{"host": "/tmp/new", "container": "/mnt/new-project", "read_only": False}]
    engine = FakeComposeEngine()
    engine.add_container(name="odoo-old", image="odoo:17", status="running", labels={}, host_ports=[],
                         mounts=OLD_MOUNTS)
    engine.add_container(name="odoo-new", image="odoo:18", status="running", labels={}, host_ports=[],
                         mounts=new_mounts)
    _placed(tmp_path, engine, ("queue_x", "/opt/external-addons"))

    external = _module("queue_x")
    assert external.new_container_path == "/mnt/new-project/queue_x"
    assert external.new_path == "/tmp/new/queue_x"


def test_a_module_under_no_mount_is_not_mounted(tmp_path):
    _placed(tmp_path, _engine(), ("baked_x", "/opt/odoo-sh/connectors-extensions"))

    baked = _module("baked_x")
    assert baked.path_problem == "not mounted"
    assert baked.old_path == ""


def test_a_mount_of_the_home_directory_is_too_broad(tmp_path):
    from pathlib import Path

    old_mounts = OLD_MOUNTS + [{"host": str(Path.home()), "container": "/mnt/home", "read_only": True}]
    engine = FakeComposeEngine()
    engine.add_container(name="odoo-old", image="odoo:17", status="running", labels={}, host_ports=[],
                         mounts=old_mounts)
    engine.add_container(name="odoo-new", image="odoo:18", status="running", labels={}, host_ports=[],
                         mounts=NEW_MOUNTS)
    _placed(tmp_path, engine, ("home_x", "/mnt/home"))

    assert _module("home_x").path_problem == "mount too broad"


def test_no_target_mount_for_the_module_or_project_is_no_target_directory(tmp_path):
    engine = FakeComposeEngine()
    engine.add_container(name="odoo-old", image="odoo:17", status="running", labels={}, host_ports=[],
                         mounts=OLD_MOUNTS)
    engine.add_container(name="odoo-new", image="odoo:18", status="running", labels={}, host_ports=[],
                         mounts=[{"host": "/opt/other", "container": "/opt/other", "read_only": False}])
    _placed(tmp_path, engine)

    assert _module("sale_custom").path_problem == "no target directory"


def test_a_second_build_graph_replaces_the_placement(tmp_path):
    created = _placed(tmp_path, _engine(), ("baked_x", "/opt/odoo-sh/connectors-extensions"))
    assert _module("baked_x").path_problem == "not mounted"
    fixed = FakeComposeEngine()
    fixed.add_container(name="odoo-old", image="odoo:17", status="running", labels={}, host_ports=[],
                        mounts=OLD_MOUNTS + [{"host": "/opt/sources/connectors",
                                              "container": "/opt/odoo-sh/connectors-extensions",
                                              "read_only": True}])
    fixed.add_container(name="odoo-new", image="odoo:18", status="running", labels={}, host_ports=[],
                        mounts=NEW_MOUNTS + [{"host": "/opt/sources/connectors-18",
                                              "container": "/opt/odoo-sh/connectors-extensions",
                                              "read_only": False}])

    snapshot_mounts(fixed, created["id"])
    resolve_graph(_graph_with(FakeShellerClient(), ("baked_x", "/opt/odoo-sh/connectors-extensions")),
                  created["id"])

    baked = _module("baked_x")
    assert baked.path_problem == ""
    assert baked.new_path == "/opt/sources/connectors-18/baked_x"


def test_a_step_with_an_unplaced_module_is_refused_whole(tmp_path):
    created = _placed(tmp_path, _engine(), ("baked_x", "/opt/odoo-sh/connectors-extensions"))
    baked = _module("baked_x")

    message = path_problems_blocking(created["id"], [baked.id, _module("sale_custom").id])

    assert "baked_x (not mounted)" in message
    assert "sale_custom" not in message
    with pytest.raises(ContractError, match="baked_x"):
        start_batch(migration_id=created["id"], executor=StubExecutor(progress=lambda body: None, result=lambda body: None), hub=EventHub(),
                    step=baked.batch_group)
    with pytest.raises(ContractError, match="baked_x"):
        start_job(migration_id=created["id"], module_id=baked.id,
                  executor=StubExecutor(progress=lambda body: None, result=lambda body: None), hub=EventHub())
    with database.connection_context():
        assert Job.select().count() == 0


def test_an_excluded_unplaced_module_does_not_block(tmp_path):
    created = _placed(tmp_path, _engine(), ("baked_x", "/opt/odoo-sh/connectors-extensions"))
    with database.connection_context():
        Module.update(struck_out=True).where(Module.technical_name == "baked_x").execute()

    assert path_problems_blocking(created["id"], [_module("baked_x").id]) is None


def test_the_card_names_the_addons_directories_found(client, tmp_path):
    client.post("/migrations", data={
        "name": "Acme", "accent": "violet", "source_version": "17.0", "target_version": "18.0",
        "old_project_path": "/tmp/Acme/old", "new_project_path": "/tmp/Acme/new",
    }, follow_redirects=True)
    with database.connection_context():
        row = Migration.get(Migration.name == "Acme")
    client.post(f"/migrations/{row.id}/open", follow_redirects=True)
    save_side_container(row.id, "source", "odoo-old")
    save_side_container(row.id, "target", "odoo-new")
    engine = client.app.state.compose
    engine.add_container(name="odoo-old", image="odoo:17", status="running", labels={}, host_ports=[],
                         mounts=OLD_MOUNTS)
    engine.add_container(name="odoo-new", image="odoo:18", status="running", labels={}, host_ports=[],
                         mounts=NEW_MOUNTS)
    client.app.state.sheller.graph_modules.append(
        {"name": "baked_x", "depends": ["sale"], "state": "installed",
         "addons_root": "/opt/odoo-sh/x", "path": "/opt/odoo-sh/x/baked_x",
         "author": "Acme", "license": "OPL-1", "is_core": False}
    )

    tree = client.post("/tree/resolve", follow_redirects=True).text
    card = client.get(f"/?settings={row.id}").text

    # baked_x, and web_enterprise_helper from the default graph: it lives in
    # /opt/enterprise, which these mounts do not cover.
    assert "2 modules have no known path on disk" in tree
    assert "Addons found in" in card
    assert "/opt/sources/project" in card
    assert "/opt/sources/project-18" in card
