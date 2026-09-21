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


def test_resolve_button_visible_text_is_the_verb(client):
    row = _open(client)
    save_side_container(row.id, "old", "odoo-old")
    html = client.get("/tree").text

    assert ">Resolve<" in html
    assert 'aria-label="Resolve the module graph from the old container"' in html


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


def test_styleguide_has_a_graph_node_and_a_struck_node(client):
    html = client.get("/styleguide").text

    assert "Module graph" in html
    assert "graph-node" in html
    assert "struck" in html
    assert "sale_custom" in html


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
