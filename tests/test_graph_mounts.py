"""Build graph snapshots both containers' bind mounts, and reads the graph
from the container directories they name."""

import json

from odoo_migrator.db import database, init_db
from odoo_migrator.graph import build_graph_snippet, resolve_graph, snapshot_mounts
from odoo_migrator.migrations import create_migration, save_side_container
from odoo_migrator.models import Migration
from odoo_migrator.provision import FakeComposeEngine
from odoo_migrator.provision.errors import DockerUnavailable
from odoo_migrator.sheller import FakeShellerClient

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
    save_side_container(created["id"], "old", "odoo-old")
    save_side_container(created["id"], "new", "odoo-new")

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

    assert snapshot == {"old": OLD_MOUNTS, "new": NEW_MOUNTS}
    with database.connection_context():
        row = Migration.get_by_id(created["id"])
        assert json.loads(row.old_mounts) == OLD_MOUNTS
        assert json.loads(row.new_mounts) == NEW_MOUNTS


def test_an_unreachable_engine_stores_not_known(tmp_path):
    created = _migration(tmp_path)

    class Down:
        def list_containers(self):
            raise DockerUnavailable("Docker Engine is not reachable")

    assert snapshot_mounts(Down(), created["id"]) == {"old": [], "new": []}
    # And the graph still builds, from the project directory as before.
    rows = resolve_graph(FakeShellerClient(), created["id"])
    assert {row["technical_name"] for row in rows} >= {"sale_custom", "stock_custom"}
    assert all(row["path_problem"] == "" for row in rows)


def test_the_graph_snippet_reads_container_roots_when_mounts_are_known(tmp_path):
    created = _migration(tmp_path)
    snapshot_mounts(_engine(), created["id"])
    fake = FakeShellerClient()

    resolve_graph(fake, created["id"])

    snippet = fake.execs[-1]
    assert '"/opt/project"' in snippet
    assert '"/opt/external-addons"' in snippet
    assert "/tmp/old" not in snippet


def test_the_snippet_drops_a_root_that_overlaps_the_odoo_install():
    snippet = build_graph_snippet("/tmp/old", container_roots=["/opt/odoo", "/opt/project"])

    assert "install_root" in snippet
    assert "_overlaps_install" in snippet
