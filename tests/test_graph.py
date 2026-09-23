"""Module graph from live Odoo, via Sheller — never by parsing manifests."""

import pytest

from odoo_migrator.db import database, init_db
from odoo_migrator.graph import (
    GraphError,
    earlier_steps_blocking,
    include_all,
    leave_out_all,
    leave_out_selection,
    list_modules,
    persist_graph,
    resolve_graph,
    restore_selection,
    set_batch_group,
    set_struck,
    skip_subtree,
    topological_steps,
)
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


def _custom(name, depends):

    return {
        "name": name,
        "depends": depends,
        "state": "installed",
        "addons_root": "/opt/project",
        "author": "Acme",
        "license": "OPL-1",
        "is_core": False,
    }


def test_step_is_edges_from_a_root_plus_one():
    """The visual row: 0 edges is step 1, one hop is step 2.

    A shortcut to the root does not push the node down; breadthfirst
    paints it on the nearer row, and the step follows that.
    """
    assert topological_steps({"root": [], "child": ["root"]}) == {
        "root": 1,
        "child": 2,
    }
    assert topological_steps(
        {
            "a": [],
            "b": ["a"],
            "c": ["a", "b"],
        }
    ) == {"a": 1, "b": 2, "c": 2}


def test_topological_steps_match_four_waves_on_a_real_tree():
    """The screenshot tree: base, then four, then four, then ui."""
    steps = topological_steps(
        {
            "mig_test_base": [],
            "mig_test_account": ["mig_test_base"],
            "mig_test_partner": ["mig_test_base"],
            "mig_test_product": ["mig_test_base"],
            "mig_test_settings": ["mig_test_base"],
            "mig_test_crm": ["mig_test_partner"],
            "mig_test_purchase": ["mig_test_partner"],
            "mig_test_sale": ["mig_test_partner", "mig_test_product"],
            "mig_test_stock": ["mig_test_product", "mig_test_settings"],
            "mig_test_ui": ["mig_test_sale"],
        }
    )

    assert steps["mig_test_base"] == 1
    assert {steps[name] for name in (
        "mig_test_account",
        "mig_test_partner",
        "mig_test_product",
        "mig_test_settings",
    )} == {2}
    assert {steps[name] for name in (
        "mig_test_crm",
        "mig_test_purchase",
        "mig_test_sale",
        "mig_test_stock",
    )} == {3}
    assert steps["mig_test_ui"] == 4


def test_resolve_groups_modules_into_those_steps(tmp_path):
    """Resolve is when the waves are drawn. Grouping by hand after the
    fact is still allowed; starting from an ungrouped tree is not."""
    created = _migration(tmp_path)
    persist_graph(
        created["id"],
        [
            _custom("mig_test_base", []),
            _custom("mig_test_account", ["mig_test_base"]),
            _custom("mig_test_partner", ["mig_test_base"]),
            _custom("mig_test_product", ["mig_test_base"]),
            _custom("mig_test_settings", ["mig_test_base"]),
            _custom("mig_test_crm", ["mig_test_partner"]),
            _custom("mig_test_purchase", ["mig_test_partner"]),
            _custom("mig_test_sale", ["mig_test_partner", "mig_test_product"]),
            _custom("mig_test_stock", ["mig_test_product", "mig_test_settings"]),
            _custom("mig_test_ui", ["mig_test_sale"]),
        ],
    )
    by_name = {row["technical_name"]: row for row in list_modules(created["id"])}

    assert by_name["mig_test_base"]["batch_group"] == 1
    assert by_name["mig_test_partner"]["batch_group"] == 2
    assert by_name["mig_test_sale"]["batch_group"] == 3
    assert by_name["mig_test_ui"]["batch_group"] == 4


def test_displayed_step_follows_depends_not_a_stale_batch_group(tmp_path):
    """A leftover Group of 1 on a second-wave module used to paint it as
    step 1, so the tree looked like two waves instead of four."""
    created = _migration(tmp_path)
    persist_graph(
        created["id"],
        [
            _custom("mig_test_base", []),
            _custom("mig_test_partner", ["mig_test_base"]),
            _custom("mig_test_sale", ["mig_test_partner"]),
            _custom("mig_test_ui", ["mig_test_sale"]),
        ],
    )
    partner = next(
        row
        for row in list_modules(created["id"])
        if row["technical_name"] == "mig_test_partner"
    )
    set_batch_group(partner["id"], 1)
    listed = {
        row["technical_name"]: row for row in list_modules(created["id"])
    }

    assert listed["mig_test_base"]["step"] == 1
    assert listed["mig_test_partner"]["step"] == 2
    assert listed["mig_test_sale"]["step"] == 3
    assert listed["mig_test_ui"]["step"] == 4
    assert listed["mig_test_partner"]["batch_group"] == 1


def test_earlier_steps_block_on_computed_rank_not_batch_group(tmp_path):
    """Grouping the second wave into batch 1 used to let it start as step 1."""
    created = _migration(tmp_path)
    persist_graph(
        created["id"],
        [
            _custom("mig_test_base", []),
            _custom("mig_test_partner", ["mig_test_base"]),
        ],
    )
    partner = next(
        row
        for row in list_modules(created["id"])
        if row["technical_name"] == "mig_test_partner"
    )
    set_batch_group(partner["id"], 1)

    assert earlier_steps_blocking(created["id"], 2)


def test_skipping_a_module_takes_its_descendants_out_with_it(tmp_path):
    """A step-2 module left in the plan while its parent is out cannot
    be migrated. Skip removes the whole downstream tree."""
    created = _migration(tmp_path)
    persist_graph(
        created["id"],
        [
            _custom("mig_test_base", []),
            _custom("mig_test_partner", ["mig_test_base"]),
            _custom("mig_test_product", ["mig_test_base"]),
            _custom("mig_test_sale", ["mig_test_partner", "mig_test_product"]),
            _custom("mig_test_ui", ["mig_test_sale"]),
        ],
    )
    rows = {row["technical_name"]: row for row in list_modules(created["id"])}

    skip_subtree(rows["mig_test_partner"]["id"], True)
    after = {row["technical_name"]: row for row in list_modules(created["id"])}

    assert after["mig_test_partner"]["struck_out"] is True
    assert after["mig_test_sale"]["struck_out"] is True
    assert after["mig_test_ui"]["struck_out"] is True
    assert after["mig_test_base"]["struck_out"] is False
    assert after["mig_test_product"]["struck_out"] is False


def test_restoring_a_module_brings_descendants_back(tmp_path):
    created = _migration(tmp_path)
    persist_graph(
        created["id"],
        [
            _custom("mig_test_base", []),
            _custom("mig_test_partner", ["mig_test_base"]),
            _custom("mig_test_sale", ["mig_test_partner"]),
        ],
    )
    rows = {row["technical_name"]: row for row in list_modules(created["id"])}
    skip_subtree(rows["mig_test_partner"]["id"], True)

    skip_subtree(rows["mig_test_partner"]["id"], False)
    after = {row["technical_name"]: row for row in list_modules(created["id"])}

    assert after["mig_test_partner"]["struck_out"] is False
    assert after["mig_test_sale"]["struck_out"] is False


def test_leave_out_selection_on_a_leaf_strikes_only_that_module(tmp_path):
    created = _migration(tmp_path)
    persist_graph(
        created["id"],
        [
            _custom("mig_test_base", []),
            _custom("mig_test_partner", ["mig_test_base"]),
            _custom("mig_test_product", ["mig_test_base"]),
            _custom("mig_test_crm", ["mig_test_partner"]),
            _custom("mig_test_purchase", ["mig_test_partner"]),
            _custom("mig_test_sale", ["mig_test_partner", "mig_test_product"]),
            _custom("mig_test_ui", ["mig_test_sale"]),
        ],
    )
    rows = {row["technical_name"]: row for row in list_modules(created["id"])}

    leave_out_selection([rows["mig_test_ui"]["id"]])
    after = {row["technical_name"]: row for row in list_modules(created["id"])}

    assert after["mig_test_ui"]["struck_out"] is True
    assert after["mig_test_sale"]["struck_out"] is False
    assert after["mig_test_partner"]["struck_out"] is False
    assert after["mig_test_product"]["struck_out"] is False


def test_leave_out_selection_on_a_whole_step_strikes_downstream(tmp_path):
    created = _migration(tmp_path)
    persist_graph(
        created["id"],
        [
            _custom("mig_test_base", []),
            _custom("mig_test_account", ["mig_test_base"]),
            _custom("mig_test_partner", ["mig_test_base"]),
            _custom("mig_test_product", ["mig_test_base"]),
            _custom("mig_test_settings", ["mig_test_base"]),
            _custom("mig_test_crm", ["mig_test_partner"]),
            _custom("mig_test_purchase", ["mig_test_partner"]),
            _custom("mig_test_sale", ["mig_test_partner", "mig_test_product"]),
            _custom("mig_test_stock", ["mig_test_product", "mig_test_settings"]),
            _custom("mig_test_ui", ["mig_test_sale"]),
        ],
    )
    rows = {row["technical_name"]: row for row in list_modules(created["id"])}
    step_two = [
        rows["mig_test_account"]["id"],
        rows["mig_test_partner"]["id"],
        rows["mig_test_product"]["id"],
        rows["mig_test_settings"]["id"],
    ]

    leave_out_selection(step_two)
    after = {row["technical_name"]: row for row in list_modules(created["id"])}

    assert after["mig_test_base"]["struck_out"] is False
    assert after["mig_test_partner"]["struck_out"] is True
    assert after["mig_test_sale"]["struck_out"] is True
    assert after["mig_test_ui"]["struck_out"] is True
    assert after["mig_test_crm"]["struck_out"] is True


def test_restore_selection_ui_leaves_sibling_branches_out(tmp_path):
    created = _migration(tmp_path)
    persist_graph(
        created["id"],
        [
            _custom("mig_test_base", []),
            _custom("mig_test_partner", ["mig_test_base"]),
            _custom("mig_test_product", ["mig_test_base"]),
            _custom("mig_test_crm", ["mig_test_partner"]),
            _custom("mig_test_purchase", ["mig_test_partner"]),
            _custom("mig_test_sale", ["mig_test_partner", "mig_test_product"]),
            _custom("mig_test_ui", ["mig_test_sale"]),
        ],
    )
    rows = {row["technical_name"]: row for row in list_modules(created["id"])}
    for name in rows:
        skip_subtree(rows[name]["id"], True)

    restore_selection([rows["mig_test_ui"]["id"]])
    after = {row["technical_name"]: row for row in list_modules(created["id"])}

    assert after["mig_test_ui"]["struck_out"] is False
    assert after["mig_test_sale"]["struck_out"] is False
    assert after["mig_test_partner"]["struck_out"] is False
    assert after["mig_test_product"]["struck_out"] is False
    assert after["mig_test_base"]["struck_out"] is False
    assert after["mig_test_crm"]["struck_out"] is True
    assert after["mig_test_purchase"]["struck_out"] is True


def test_list_pipeline_steps_matches_tree_waves(tmp_path):
    created = _migration(tmp_path)
    persist_graph(
        created["id"],
        [
            _custom("mig_test_base", []),
            _custom("mig_test_partner", ["mig_test_base"]),
            _custom("mig_test_sale", ["mig_test_partner"]),
            _custom("mig_test_ui", ["mig_test_sale"]),
        ],
    )
    from odoo_migrator.graph import list_pipeline_steps, modules_in_step_order

    steps = list_pipeline_steps(created["id"])

    assert steps == [
        {"step": 1, "count": 1, "modules": ["mig_test_base"]},
        {"step": 2, "count": 1, "modules": ["mig_test_partner"]},
        {"step": 3, "count": 1, "modules": ["mig_test_sale"]},
        {"step": 4, "count": 1, "modules": ["mig_test_ui"]},
    ]
    assert [row["technical_name"] for row in modules_in_step_order(created["id"], 2)] == [
        "mig_test_partner"
    ]


def test_leave_out_all_and_include_all_round_trip(tmp_path):
    created = _migration(tmp_path)
    persist_graph(
        created["id"],
        [
            _custom("mig_test_base", []),
            _custom("mig_test_partner", ["mig_test_base"]),
            _custom("mig_test_sale", ["mig_test_partner"]),
        ],
    )

    leave_out_all(created["id"])
    after_out = {row["technical_name"]: row for row in list_modules(created["id"])}

    assert all(row["struck_out"] for row in after_out.values())

    include_all(created["id"])
    after_in = {row["technical_name"]: row for row in list_modules(created["id"])}

    assert all(not row["struck_out"] for row in after_in.values())


def test_restore_selection_brings_struck_ancestors_first(tmp_path):
    created = _migration(tmp_path)
    persist_graph(
        created["id"],
        [
            _custom("mig_test_base", []),
            _custom("mig_test_partner", ["mig_test_base"]),
            _custom("mig_test_crm", ["mig_test_partner"]),
        ],
    )
    rows = {row["technical_name"]: row for row in list_modules(created["id"])}
    skip_subtree(rows["mig_test_partner"]["id"], True)

    restore_selection([rows["mig_test_crm"]["id"]])
    after = {row["technical_name"]: row for row in list_modules(created["id"])}

    assert after["mig_test_partner"]["struck_out"] is False
    assert after["mig_test_crm"]["struck_out"] is False


def test_a_descendant_stays_out_while_another_parent_is_skipped(tmp_path):
    created = _migration(tmp_path)
    persist_graph(
        created["id"],
        [
            _custom("mig_test_base", []),
            _custom("mig_test_partner", ["mig_test_base"]),
            _custom("mig_test_product", ["mig_test_base"]),
            _custom("mig_test_sale", ["mig_test_partner", "mig_test_product"]),
        ],
    )
    rows = {row["technical_name"]: row for row in list_modules(created["id"])}
    skip_subtree(rows["mig_test_partner"]["id"], True)
    skip_subtree(rows["mig_test_product"]["id"], True)

    skip_subtree(rows["mig_test_partner"]["id"], False)
    after = {row["technical_name"]: row for row in list_modules(created["id"])}

    assert after["mig_test_partner"]["struck_out"] is False
    assert after["mig_test_product"]["struck_out"] is True
    assert after["mig_test_sale"]["struck_out"] is True


def test_migration_rank_is_global_order_not_node_id(tmp_path):
    """The badge is #rank/step: rank is when you migrate, step is the wave."""
    created = _migration(tmp_path)
    persist_graph(
        created["id"],
        [
            _custom("mig_test_account", ["mig_test_base"]),
            _custom("mig_test_base", []),
        ],
    )
    by_name = {row["technical_name"]: row for row in list_modules(created["id"])}

    assert by_name["mig_test_base"]["node_id"] == 2
    assert by_name["mig_test_account"]["node_id"] == 1
    assert by_name["mig_test_base"]["migration_rank"] == 1
    assert by_name["mig_test_account"]["migration_rank"] == 2
    assert by_name["mig_test_base"]["step"] == 1
    assert by_name["mig_test_account"]["step"] == 2


def test_modules_get_a_stable_node_id_on_first_persist(tmp_path):
    created = _migration(tmp_path)
    persist_graph(
        created["id"],
        [_custom("mig_test_base", []), _custom("mig_test_partner", ["mig_test_base"])],
    )
    first = {row["technical_name"]: row["node_id"] for row in list_modules(created["id"])}

    assert first["mig_test_base"] == 1
    assert first["mig_test_partner"] == 2

    persist_graph(
        created["id"],
        [_custom("mig_test_base", []), _custom("mig_test_partner", ["mig_test_base"])],
    )
    again = {row["technical_name"]: row["node_id"] for row in list_modules(created["id"])}

    assert again == first


def test_a_new_module_does_not_reuse_a_deleted_node_id(tmp_path):
    """The serial is max+1, never compacted. A hole is how you know the
    number belonged to a module that left."""
    created = _migration(tmp_path)
    persist_graph(
        created["id"],
        [
            _custom("mig_test_base", []),
            _custom("mig_test_partner", ["mig_test_base"]),
            _custom("mig_test_sale", ["mig_test_partner"]),
        ],
    )
    persist_graph(
        created["id"],
        [_custom("mig_test_base", []), _custom("mig_test_sale", ["mig_test_base"])],
    )
    persist_graph(
        created["id"],
        [
            _custom("mig_test_base", []),
            _custom("mig_test_sale", ["mig_test_base"]),
            _custom("mig_test_ui", ["mig_test_sale"]),
        ],
    )
    by_name = {row["technical_name"]: row["node_id"] for row in list_modules(created["id"])}

    assert by_name["mig_test_base"] == 1
    assert by_name["mig_test_sale"] == 3
    assert by_name["mig_test_ui"] == 4
    assert "mig_test_partner" not in by_name


def test_list_modules_backfills_a_missing_node_id(tmp_path):
    """A database from before this column would otherwise show #0 until
    the next Resolve."""
    created = _migration(tmp_path)
    with database.connection_context():
        from odoo_migrator.models import Module

        Module.create(migration=created["id"], technical_name="sale_custom")
    rows = list_modules(created["id"])

    assert rows[0]["node_id"] == 1


def test_resolve_assigns_steps_on_the_default_graph(tmp_path):
    created = _migration(tmp_path)
    rows = resolve_graph(FakeShellerClient(), created["id"])
    by_name = {row["technical_name"]: row for row in rows}

    assert by_name["sale_custom"]["batch_group"] == 1
    assert by_name["web_enterprise_helper"]["batch_group"] == 1
    assert by_name["stock_custom"]["batch_group"] == 2


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


def test_resolve_adds_new_modules_on_a_second_run(tmp_path):
    created = _migration(tmp_path)
    fake = FakeShellerClient()
    resolve_graph(fake, created["id"])
    before = {row["technical_name"] for row in list_modules(created["id"])}

    fake.graph_modules = list(fake.graph_modules) + [
        _custom("mig_test_new", ["sale_custom"]),
    ]
    resolve_graph(fake, created["id"])
    after = {row["technical_name"] for row in list_modules(created["id"])}

    assert "mig_test_new" in after
    assert before <= after


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


def test_resolve_redraws_step_groups(tmp_path):
    """A hand-drawn grouping is a cut of this snapshot. Resolve asks Odoo
    again and redraws the waves from the live graph."""
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
    assert again["batch_group"] == 1
    assert again["id"] == custom["id"]


def test_a_vanished_module_that_carries_human_work_is_flagged_not_deleted(tmp_path):
    """Deleting it threw away notes, status and assumptions.

    A resolve against the wrong database would have wiped every one of them
    at once, which is not a state anyone can get back. Batch groups are
    redrawn from the graph, so they are not a reason to keep the row.
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
        # The probe reports the database the container is configured for,
        # which is not the same as one that exists: opening a shell on a
        # missing database starts a process that dies at once (410).
        if (
            candidate.get("ok")
            and candidate.get("db_name")
            and candidate["db_name"] in (candidate.get("databases") or [])
        ):
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
