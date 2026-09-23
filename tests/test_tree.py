"""Tree screen: Cytoscape graph, strike-out, marquee, batch groups."""

import hashlib
from pathlib import Path

from odoo_migrator.db import database
from odoo_migrator.migrations import save_side_container
from odoo_migrator.models import Migration


def _open(client, name="Acme"):
    client.post(
        "/migrations",
        data={
            "name": name,
            "accent": "violet",
            "source_version": "17.0",
            "target_version": "18.0",
            "old_project_path": f"/tmp/{name}/old",
            "new_project_path": f"/tmp/{name}/new",
        },
        follow_redirects=True,
    )
    with database.connection_context():
        row = Migration.get(Migration.name == name)
    client.post(f"/migrations/{row.id}/open", follow_redirects=True)

    return row


def test_tree_without_a_migration_asks_to_open_one(client):
    html = client.get("/tree").text

    assert "Open a migration to load its dependency graph." in html
    assert "Resolve" not in html


def test_resolve_from_the_tree_shows_modules_from_odoo(client):
    row = _open(client)
    save_side_container(row.id, "old", "odoo-old")

    page = client.post("/tree/resolve", follow_redirects=True)

    assert page.status_code == 200
    assert "sale_custom" in page.text
    assert 'id="module-graph"' in page.text
    assert "/static/vendor/cytoscape.min.js" in page.text
    assert "/static/tree.js" in page.text
    assert 'data-box-select="true"' in page.text


def test_resolve_without_old_container_is_loud_on_the_tree(client):
    _open(client)

    page = client.post("/tree/resolve", follow_redirects=True)

    assert page.status_code == 200
    assert "form-error" in page.text
    assert "old container" in page.text


def _open_with_projects(client, tmp_path, name="Linked", *, compose=True):
    old = tmp_path / "old"
    new = tmp_path / "new"
    for path in (old, new):
        path.mkdir()
        if compose:
            (path / "compose.yaml").write_text(
                'services:\n  odoo:\n    image: odoo:17.0\n'
                '    ports:\n      - "8069:8069"\n',
                encoding="utf-8",
            )
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
    client.post(f"/migrations/{row.id}/open", follow_redirects=True)

    return row, old


def test_resolve_auto_links_a_running_source_container(client, tmp_path):
    """Resolve links to a source container already up, so the human does not
    press Attach on another screen first. Only Up starts one."""
    from odoo_migrator.provision.engine import WORKING_DIR_LABEL

    row, old = _open_with_projects(client, tmp_path)
    # Ours, running, but the app was never told to Attach it.
    client.app.state.compose.add_container(
        name="odoo-old",
        image="odoo:17.0",
        status="running",
        labels={WORKING_DIR_LABEL: str(old.resolve())},
        host_ports=[8069],
    )

    page = client.post("/tree/resolve", follow_redirects=True)

    assert page.status_code == 200
    # Resolved, not refused: the graph rendered and nothing asked for Up.
    assert "sale_custom" in page.text
    assert "can't be linked" not in page.text
    assert "not running yet" not in page.text
    with database.connection_context():
        assert Migration.get_by_id(row.id).old_container == "odoo-old"


def test_resolve_when_source_is_down_points_to_the_card(client, tmp_path):
    """Nothing running: the error names Up and links to the migration's card,
    rather than the old cryptic 'Attach the old container first.'"""
    row, _old = _open_with_projects(client, tmp_path)

    page = client.post("/tree/resolve", follow_redirects=True)

    assert page.status_code == 200
    assert "form-error" in page.text
    assert "not running yet" in page.text
    assert f"/?settings={row.id}" in page.text
    with database.connection_context():
        assert Migration.get_by_id(row.id).old_container == ""


def test_strike_json_skips_downstream_modules(client):
    """Exclude on the API side is skip_subtree: dependents go out too."""
    migration_id, modules = _resolved(client)
    base = modules["sale_custom"]
    stock = modules["stock_custom"]
    response = client.post(
        f"/tree/modules/{base['id']}/strike",
        data={"struck": "1"},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    payload = response.json()
    struck_ids = {row["id"] for row in payload["modules"] if row["struck_out"]}

    assert base["id"] in struck_ids
    assert stock["id"] in struck_ids


def test_restore_json_brings_ancestors_before_dependents(client):
    migration_id, modules = _resolved(client)
    partner = modules["sale_custom"]
    stock = modules["stock_custom"]
    client.post(
        f"/tree/modules/{partner['id']}/strike",
        data={"struck": "1"},
        headers={"Accept": "application/json"},
    )
    response = client.post(
        "/tree/modules/restore",
        data={"module_ids": [stock["id"]]},
        headers={"Accept": "application/json"},
    )

    assert response.status_code == 200
    payload = response.json()
    restored = {row["id"]: row["struck_out"] for row in payload["modules"]}

    assert restored[partner["id"]] is False
    assert restored[stock["id"]] is False


def test_strike_persists_across_a_reload(client):
    row = _open(client)
    save_side_container(row.id, "old", "odoo-old")
    client.post("/tree/resolve", follow_redirects=True)
    tree = client.get("/tree").text
    # The graph JSON is the source of truth the canvas reads; a struck flag
    # that only lives in Cytoscape state is gone on the next page load.
    assert '"struck_out": false' in tree or '"struck_out":false' in tree
    from odoo_migrator.graph import list_modules

    custom = next(m for m in list_modules(row.id) if m["technical_name"] == "sale_custom")

    struck = client.post(f"/tree/modules/{custom['id']}/strike", follow_redirects=True)

    assert '"struck_out": true' in struck.text or '"struck_out":true' in struck.text
    reloaded = client.get("/tree").text
    assert '"struck_out": true' in reloaded or '"struck_out":true' in reloaded


def test_batch_group_assignment_persists(client):
    row = _open(client)
    save_side_container(row.id, "old", "odoo-old")
    client.post("/tree/resolve", follow_redirects=True)
    from odoo_migrator.graph import list_modules

    custom = next(m for m in list_modules(row.id) if m["technical_name"] == "sale_custom")

    grouped = client.post(
        f"/tree/modules/{custom['id']}/batch",
        data={"batch_group": "1"},
        follow_redirects=True,
    )

    assert '"batch_group": 1' in grouped.text or '"batch_group":1' in grouped.text


def test_build_graph_button_visible_text(client):
    row = _open(client)
    save_side_container(row.id, "old", "odoo-old")
    html = client.get("/tree").text

    assert ">Build graph<" in html
    assert 'aria-label="Build the module graph from the old container"' in html


def test_cytoscape_is_vendored_with_a_hash(client):
    js = client.get("/static/vendor/cytoscape.min.js")

    assert js.status_code == 200
    assert b"Cytoscape" in js.content
    readme = (
        Path(__file__).resolve().parents[1]
        / "odoo_migrator"
        / "web"
        / "static"
        / "vendor"
        / "README.md"
    ).read_text(encoding="utf-8")
    assert "cytoscape.min.js" in readme
    digest = hashlib.sha256(js.content).hexdigest()
    assert digest in readme


def test_tree_script_enables_marquee_and_batch_drag(client):
    script = client.get("/static/tree.js")

    assert script.status_code == 200
    assert "boxSelectionEnabled" in script.text
    assert "dragfree" in script.text
    assert "batch_group" in script.text


def test_tree_script_keeps_breadthfirst_and_colours_steps(client):
    """Compound boxes made the tree unreadable. The old layout stays;
    each step gets its own colour from the stylesheet."""
    script = client.get("/static/tree.js")
    css = client.get("/static/style.css")

    assert script.status_code == 200
    assert "breadthfirst" in script.text
    assert "preset" not in script.text
    assert ":parent" not in script.text
    assert "step_color" in script.text
    # Cytoscape's colour parser drops oklch(); the script must resolve to rgb/hex.
    assert "oklch" in script.text
    assert "rgb(1, 2, 3)" in script.text
    assert "--step-1" in css.text
    assert "--step-2" in css.text
    assert "--step-3" in css.text
    assert "--step-4" in css.text
    assert "step-legend" in script.text
    assert "data-step" in script.text
    assert "aria-pressed" in script.text
    assert "node_id" in script.text
    assert "graph-node-index" in script.text
    assert " · step " not in script.text
    assert "colourForStep(row.step" in script.text
    assert "migration_rank" in script.text
    assert "module_id || row.id" in script.text
    assert "rank + \"/\" +" in script.text
    assert ".graph-node-index" in css.text
    assert "stepsFromDepends" in script.text
    assert "node-card" in script.text
    assert "node-card-include" in script.text
    assert 'data-selection="many"' in script.text or "data-selection" in script.text
    assert "INDEX_ZOOM" not in script.text
    assert "skip_subtree" not in script.text
    assert "Accept" in script.text
    assert "application/json" in script.text
    assert "/tree/modules/restore" in script.text
    assert "/tree/modules/leave-out" in script.text
    assert "/tree/modules/leave-out-all" in script.text
    assert "/tree/modules/include-all" in script.text


def test_tree_json_carries_node_id_and_step(client):
    _resolved(client)
    html = client.get("/tree").text

    assert '"node_id": 1' in html or '"node_id":1' in html
    assert '"step": 1' in html or '"step":1' in html
    assert '"step": 2' in html or '"step":2' in html
    assert 'id="graph-badges"' in html
    assert "graph-stage" in html
    assert "module-rows" not in html
    assert 'id="node-card"' in html
    assert "Exclude" in html
    assert ">Exclude all<" in html
    assert ">Include all<" in html


def test_leave_out_all_and_include_all_json(client):
    migration_id, _modules = _resolved(client)
    from odoo_migrator.graph import list_modules

    out = client.post(
        "/tree/modules/leave-out-all",
        headers={"Accept": "application/json"},
    )
    assert out.status_code == 200
    payload = out.json()
    assert len(payload["modules"]) == len(list_modules(migration_id))

    back = client.post(
        "/tree/modules/include-all",
        headers={"Accept": "application/json"},
    )
    assert back.status_code == 200
    assert all(not row["struck_out"] for row in back.json()["modules"])


def test_styleguide_has_a_graph_node_and_a_struck_node(client):
    html = client.get("/components").text

    assert "Module graph" in html
    assert "graph-node" in html
    assert "struck" in html
    assert "mig_test_base" in html
    assert "graph-node step-1" in html
    assert "graph-node step-2" in html
    assert "step-legend" in html
    assert "graph-node-index" in html
    assert "#1/1" in html
    assert "Exclude" in html
    assert 'id="node-card"' in html or "node-card" in html


def test_focus_opens_the_node_card_for_that_module(client):
    """The lamp used to land on a hundred-row list. The graph is the
    list; focus fills the one card for that module."""
    _id, modules = _resolved(client)
    custom = modules["sale_custom"]
    html = client.get(f"/tree?focus={custom['id']}").text

    assert f'data-module-id="{custom["id"]}"' in html
    assert "sale_custom" in html
    assert "Exclude" in html


def _resolved(client):
    row = _open(client)
    save_side_container(row.id, "old", "odoo-old")
    client.post("/tree/resolve", follow_redirects=True)
    from odoo_migrator.graph import list_modules

    return row.id, {m["technical_name"]: m for m in list_modules(row.id)}


def _reload(migration_id, module_id):
    from odoo_migrator.graph import list_modules

    return next(row for row in list_modules(migration_id) if row["id"] == module_id)


def test_strike_is_its_own_undo_for_a_mixed_selection(client):
    """Toggling each module made one press of Strike do two opposite things:
    modules struck by an earlier mistake came back while the rest went out.
    Bringing back one wrongly struck module is the whole point, so a press
    decides one way for everything selected.
    """
    migration_id, modules = _resolved(client)
    first, second = modules["sale_custom"], modules["stock_custom"]
    client.post(f"/tree/modules/{first['id']}/strike", data={"struck": "1"})

    for module in (first, second):
        client.post(f"/tree/modules/{module['id']}/strike", data={"struck": "1"})

    assert _reload(migration_id, first["id"])["struck_out"] is True
    assert _reload(migration_id, second["id"])["struck_out"] is True

    for module in (first, second):
        client.post(f"/tree/modules/{module['id']}/strike", data={"struck": "0"})

    assert _reload(migration_id, first["id"])["struck_out"] is False
    assert _reload(migration_id, second["id"])["struck_out"] is False


def test_a_batch_group_can_be_cleared(client):
    """Group only ever assigned the next number, so a wrong grouping could not
    be undone — regrouping climbed instead of fixing."""
    migration_id, modules = _resolved(client)
    module = modules["sale_custom"]
    client.post(f"/tree/modules/{module['id']}/batch", data={"batch_group": "3"})

    assert _reload(migration_id, module["id"])["batch_group"] == 3

    client.post(f"/tree/modules/{module['id']}/batch", data={"batch_group": ""})

    assert _reload(migration_id, module["id"])["batch_group"] is None
