"""Module graph from live Odoo, via Sheller — never by parsing manifests."""

import pytest

from odoo_migrator.db import database, init_db
from odoo_migrator.graph import GraphError, list_modules, resolve_graph, set_batch_group, set_struck
from odoo_migrator.migrations import create_migration, save_side_container
from odoo_migrator.sheller import FakeShellerClient


def _migration(tmp_path, *, old_container="odoo-old"):
    init_db(tmp_path / "graph.sqlite")
    created = create_migration(
        name="Acme",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path="/tmp/old",
        new_project_path="/tmp/new",
    )
    if old_container:
        save_side_container(created["id"], "old", old_container)

    return created


def test_probe_db_name_is_not_the_first_database_on_the_server():
    """The list is every database the server can see, including ones built
    by other Odoo majors. Opening a shell on databases[0] is the 410 that
    the live round-trip found. The fake has to keep that trap in place or
    the graph tests would pass while using the wrong key."""
    probed = FakeShellerClient().probe("odoo-old")

    assert probed["db_name"]
    assert probed["databases"][0] != probed["db_name"]
    assert probed["db_name"] in probed["databases"]


def test_resolve_opens_on_probe_db_name_not_databases_0(tmp_path):
    created = _migration(tmp_path)
    fake = FakeShellerClient()
    probed = fake.probe("odoo-old")

    resolve_graph(fake, created["id"])

    assert fake.opens[0]["database"] == probed["db_name"]
    assert fake.opens[0]["database"] != probed["databases"][0]
    assert fake.opens[0]["odoo_bin"] == probed["odoo_bin"]
    assert fake.opens[0]["container"] == "odoo-old"


def test_resolve_is_read_only_and_closes_the_session(tmp_path):
    created = _migration(tmp_path)
    fake = FakeShellerClient()

    resolve_graph(fake, created["id"])

    assert fake.opens[0]["allow_commit"] is False
    assert fake.commits == []
    assert fake.closes == [fake.opens[0]["id"]]
    assert fake._sessions == {}


def test_resolve_owner_is_the_agent_for_this_migration(tmp_path):
    created = _migration(tmp_path)
    fake = FakeShellerClient()

    resolve_graph(fake, created["id"])

    owner = fake.opens[0]["owner"]
    assert owner["kind"] == "agent"
    assert owner["migration_id"] == created["id"]


def test_resolve_asks_ir_module_module_not_manifests(tmp_path):
    created = _migration(tmp_path)
    fake = FakeShellerClient()

    resolve_graph(fake, created["id"])

    assert len(fake.execs) == 1
    code = fake.execs[0]
    assert "ir.module.module" in code
    assert "__manifest__" not in code


def test_resolve_persists_modules_with_parents_from_odoo(tmp_path):
    created = _migration(tmp_path)
    fake = FakeShellerClient()

    rows = resolve_graph(fake, created["id"])

    by_name = {row["technical_name"]: row for row in rows}
    # Core and Enterprise are out; the LGPL-3 module sitting in the
    # Enterprise directory stays, because hiding a module nobody notices is
    # worse than showing one that gets struck.
    assert set(by_name) == {"sale_custom", "stock_custom", "web_enterprise_helper"}
    # Every dependency inside the kept set, not one arbitrary parent.
    assert by_name["stock_custom"]["depends"] == ["sale_custom"]
    assert by_name["sale_custom"]["depends"] == []
    # Its manifest claims Odoo wrote it, and it is still the customer's.
    assert by_name["sale_custom"]["author"] == "Odoo S.A."
    assert by_name["sale_custom"]["status"] == "queued"
    assert by_name["sale_custom"]["addons_root"] == "/opt/project"
    listed = list_modules(created["id"])
    assert [row["technical_name"] for row in listed] == [
        "sale_custom",
        "stock_custom",
        "web_enterprise_helper",
    ]


def test_struck_out_survives_re_resolve(tmp_path):
    created = _migration(tmp_path)
    fake = FakeShellerClient()
    resolve_graph(fake, created["id"])
    custom = next(
        row for row in list_modules(created["id"]) if row["technical_name"] == "sale_custom"
    )
    set_struck(custom["id"], True)

    resolve_graph(fake, created["id"])

    again = next(
        row for row in list_modules(created["id"]) if row["technical_name"] == "sale_custom"
    )
    assert again["struck_out"] is True
    assert again["id"] == custom["id"]


def test_batch_group_survives_re_resolve(tmp_path):
    created = _migration(tmp_path)
    fake = FakeShellerClient()
    resolve_graph(fake, created["id"])
    custom = next(
        row for row in list_modules(created["id"]) if row["technical_name"] == "sale_custom"
    )
    set_batch_group(custom["id"], 2)

    resolve_graph(fake, created["id"])

    again = next(
        row for row in list_modules(created["id"]) if row["technical_name"] == "sale_custom"
    )
    assert again["batch_group"] == 2


def test_a_vanished_module_that_carries_human_work_is_flagged_not_deleted(tmp_path):
    """Deleting it threw away notes, status, batch group and assumptions.

    A resolve against the wrong database would have wiped every one of them
    at once, which is not a state anyone can get back.
    """
    created = _migration(tmp_path)
    fake = FakeShellerClient()
    resolve_graph(fake, created["id"])
    custom = next(
        row for row in list_modules(created["id"]) if row["technical_name"] == "sale_custom"
    )
    set_struck(custom["id"], True)
    fake.graph_modules = [
        {"name": "stock_custom", "depends": [], "state": "installed",
         "addons_root": "/opt/project", "is_core": False},
    ]

    resolve_graph(fake, created["id"])

    kept = next(
        row for row in list_modules(created["id"]) if row["technical_name"] == "sale_custom"
    )

    assert kept["struck_out"] is True
    assert kept["absent_from_odoo"] is True


def test_a_vanished_module_nobody_touched_is_dropped(tmp_path):
    created = _migration(tmp_path)
    fake = FakeShellerClient()
    resolve_graph(fake, created["id"])
    fake.graph_modules = [
        {"name": "sale_custom", "depends": [], "state": "installed",
         "addons_root": "/opt/project", "is_core": False},
    ]

    resolve_graph(fake, created["id"])

    assert {row["technical_name"] for row in list_modules(created["id"])} == {"sale_custom"}


def test_resolve_without_old_container_is_loud(tmp_path):
    created = _migration(tmp_path, old_container="")
    fake = FakeShellerClient()

    with pytest.raises(GraphError, match="old container"):
        resolve_graph(fake, created["id"])

    assert fake.opens == []


def test_resolve_closes_the_session_even_when_exec_fails(tmp_path):
    created = _migration(tmp_path)
    fake = FakeShellerClient()
    fake.exec_error = RuntimeError("session blew up")

    with pytest.raises(GraphError):
        resolve_graph(fake, created["id"])

    assert fake.closes == [fake.opens[0]["id"]]
    with database.connection_context():
        pass  # connection must still be usable; the failure did not leak one


@pytest.mark.live
def test_live_graph_from_old_odoo(tmp_path):
    """Review gate: the tree appears from a running old Odoo. Skip if none."""
    from odoo_migrator.sheller import HttpShellerClient, ShellerUnreachable

    client = HttpShellerClient("http://127.0.0.1:8765", timeout=180.0)
    try:
        client.health()
    except ShellerUnreachable:
        pytest.skip("no daemon on 8765")

    containers = client.list_containers()
    if not containers:
        pytest.skip("no containers visible to the daemon")

    probed = None
    for entry in containers:
        candidate = client.probe(entry["name"])
        if candidate.get("ok") and candidate.get("db_name"):
            probed = (entry["name"], candidate)
            break
    if probed is None:
        pytest.skip("no Odoo container to open a session on")

    name, _details = probed
    created = _migration(tmp_path, old_container=name)
    before = {entry["id"] for entry in client.list_sessions()}
    rows = resolve_graph(client, created["id"])

    # Core is filtered out, so the tree holds only the project's own addons.
    # On this host that was 3 of 128 installed modules.
    assert all(not row["addons_root"].endswith("/odoo/addons") for row in rows)
    after = {entry["id"] for entry in client.list_sessions()}
    # What this is about is that resolve_graph leaves no session behind.
    # Equality also failed when a session belonging to someone else ended
    # during the run — the daemon reaps dead ones — which is not our bug
    # and made the suite flaky against a live daemon.
    assert after - before == set()
