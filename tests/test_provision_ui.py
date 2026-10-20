"""Project-setup screen: compose actions for the open migration's old and new sides."""

from odoo_migrator.db import database
from odoo_migrator.models import Migration


def _open_migration(client, name="Acme", *, old, new):
    created = client.post(
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

    return created, row


def _compose(project, port):
    project.mkdir(parents=True, exist_ok=True)
    (project / "compose.yaml").write_text(
        "services:\n"
        "  odoo:\n"
        "    image: odoo:17.0\n"
        "    ports:\n"
        f'      - "{port}:8069"\n',
        encoding="utf-8",
    )


def test_a_stopped_side_has_one_button_start_and_nothing_else(client, tmp_path):
    """Five buttons in a row read as five things to do. A stopped side has
    one: Start. The migration and the side are its accessible name."""
    old = tmp_path / "old"
    new = tmp_path / "new"
    _compose(old, 8069)
    _compose(new, 8070)
    _, row = _open_migration(client, old=old, new=new)
    page = client.get(f"/?settings={row.id}").text

    assert 'aria-label="Start the source container of Acme"' in page
    assert 'aria-label="Start the target container of Acme"' in page
    assert f'action="/migrations/{row.id}/containers/source/up"' in page
    for action in ("stop", "restart", "rebuild", "attach"):
        assert f"containers/source/{action}" not in page
    assert "side-more" not in page.split('class="migration-settings-cols"', 1)[1].split("</form>", 1)[0]
    assert "containers on this machine" in page
    # The card headings are the roles, not "source container" / "target container".
    assert ">source<" in page
    assert ">target<" in page


def test_a_running_side_has_no_button_and_its_actions_behind_a_menu(client, tmp_path, compose):
    old = tmp_path / "old"
    new = tmp_path / "new"
    _compose(old, 8069)
    _compose(new, 8070)
    _, row = _open_migration(client, old=old, new=new)
    _running(compose, old, "old-odoo-1", 8069)

    page = client.get(f"/?settings={row.id}").text
    source = page.split('<h3>source</h3>', 1)[1].split('<h3>target</h3>', 1)[0]

    assert 'containers/source/up"' not in source
    menu = source.split('<details class="side-more" data-menu>', 1)[1].split("</details>", 1)[0]
    for action, label in (("restart", "Restart"), ("rebuild", "Rebuild"), ("stop", "Stop")):
        assert f'containers/source/{action}"' in menu
        assert f'aria-label="{label} the source container of Acme"' in menu
    assert 'aria-label="More for the source container of Acme"' in menu
    assert '<script defer src="/static/menus.js"></script>' in page


def test_up_persists_the_container_name_on_the_migration(client, tmp_path, compose):
    old = tmp_path / "old"
    new = tmp_path / "new"
    _compose(old, 8069)
    _compose(new, 8070)
    _, row = _open_migration(client, old=old, new=new)

    page = client.post(
        f"/migrations/{row.id}/containers/source/up",
        follow_redirects=True,
    )

    assert page.status_code == 200
    assert "running" in page.text
    with database.connection_context():
        stored = Migration.get_by_id(row.id)
        assert stored.old_container
        assert "odoo" in stored.old_container.lower()
    assert compose.ups == [old.resolve()]


def test_up_says_so_when_the_port_belongs_to_someone_else(client, tmp_path, compose):
    old = tmp_path / "old"
    new = tmp_path / "new"
    _compose(old, 8069)
    _compose(new, 8070)
    _, row = _open_migration(client, old=old, new=new)
    compose.add_container(
        name="someone-elses-odoo",
        image="odoo:17.0",
        status="running",
        labels={},
        host_ports=[8069],
    )

    page = client.post(
        f"/migrations/{row.id}/containers/source/up",
        follow_redirects=True,
    )

    assert page.status_code == 200
    assert "not part of this project" in page.text
    with database.connection_context():
        assert Migration.get_by_id(Migration.get(Migration.name == "Acme").id).old_container == ""
    assert compose.ups == []
    assert [c.name for c in compose.containers] == ["someone-elses-odoo"]


def test_missing_compose_is_loud_not_silent(client, tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    _, row = _open_migration(client, old=old, new=new)

    page = client.post(
        f"/migrations/{row.id}/containers/source/up",
        follow_redirects=True,
    )

    assert "compose" in page.text.lower()
    assert "form-error" in page.text


def test_styleguide_shows_container_actions(client):
    html = client.get("/components").text

    assert ">Start</button>" in html and "side-more-menu" in html
    assert "Odoo containers running" in html
    assert "Container" in html


def test_actions_that_take_a_container_away_ask_first(client, tmp_path, compose):
    """Stop, Restart and Rebuild under a running job fail it at its next
    command, and one stray press did that with no question asked."""
    old = tmp_path / "old"
    new = tmp_path / "new"
    _compose(old, 8069)
    _compose(new, 8070)
    _, row = _open_migration(client, old=old, new=new)
    _running(compose, old, "old-odoo-1", 8069)
    page = client.get(f"/?settings={row.id}").text

    for action in ("stop", "restart", "rebuild"):
        form = page[page.index(f'containers/source/{action}"') :]
        assert "data-confirm-text" in form[: form.index(">")]
    start = page[page.index('containers/target/up"') :]
    assert "data-confirm-text" not in start[: start.index(">")]


def _running(compose, project, name, port):
    from odoo_migrator.provision.engine import WORKING_DIR_LABEL

    compose.add_container(
        name=name,
        image="odoo:17.0",
        status="running",
        labels={WORKING_DIR_LABEL: str(project.resolve())},
        host_ports=[port],
    )


def test_there_is_no_attach_a_running_side_is_linked_when_the_card_sees_it(
    client, tmp_path, compose
):
    """Attach recorded the container of a side already running, and nobody
    ever pressed it. The card that shows the side running records it."""
    old = tmp_path / "old"
    new = tmp_path / "new"
    _compose(old, 8069)
    _compose(new, 8070)
    _, row = _open_migration(client, old=old, new=new)
    _running(compose, old, "old-odoo-1", 8069)
    _running(compose, new, "new-odoo-1", 8070)

    page = client.get(f"/?settings={row.id}").text

    assert "containers/source/attach" not in page and ">Attach<" not in page
    with database.connection_context():
        stored = Migration.get_by_id(row.id)
        assert (stored.old_container, stored.new_container) == ("old-odoo-1", "new-odoo-1")
    refused = client.post(f"/migrations/{row.id}/containers/source/attach", follow_redirects=True)
    assert "That container action was rejected" in refused.text


def test_build_graph_links_both_running_sides(client, tmp_path, compose, monkeypatch):
    """The target was linked only by a button on another screen; a target
    started outside the app was not the one a run used."""
    from odoo_migrator import api

    old = tmp_path / "old"
    new = tmp_path / "new"
    _compose(old, 8069)
    _compose(new, 8070)
    _, row = _open_migration(client, old=old, new=new)
    _running(compose, old, "old-odoo-1", 8069)
    _running(compose, new, "new-odoo-1", 8070)
    monkeypatch.setattr(api, "resolve_graph", lambda client, migration_id: [])
    monkeypatch.setattr(api, "snapshot_mounts", lambda engine, migration_id: {})

    client.post("/tree/resolve", follow_redirects=True)

    with database.connection_context():
        stored = Migration.get_by_id(row.id)
        assert (stored.old_container, stored.new_container) == ("old-odoo-1", "new-odoo-1")


def test_build_graph_on_a_stopped_source_says_to_start_it(client, tmp_path, compose):
    old = tmp_path / "old"
    new = tmp_path / "new"
    _compose(old, 8069)
    _compose(new, 8070)
    _open_migration(client, old=old, new=new)

    page = client.post("/tree/resolve", follow_redirects=True).text

    assert "The source container is not running" in page


def test_a_running_side_whose_dockerfile_changed_has_rebuild_and_says_why(
    client, tmp_path, compose
):
    import os

    from odoo_migrator.provision.engine import WORKING_DIR_LABEL

    old = tmp_path / "old"
    new = tmp_path / "new"
    _compose(new, 8070)
    old.mkdir()
    (old / "compose.yaml").write_text(
        'services:\n  odoo:\n    build: .\n    ports:\n      - "8069:8069"\n', encoding="utf-8"
    )
    (old / "Dockerfile").write_text("FROM odoo:17.0\n", encoding="utf-8")
    os.utime(old / "compose.yaml", (1000, 1000))
    os.utime(old / "Dockerfile", (3000, 3000))
    _, row = _open_migration(client, old=old, new=new)
    compose.images["sha256:old"] = 2000
    compose.add_container(
        name="old-odoo-1", image="old-odoo", status="running",
        labels={WORKING_DIR_LABEL: str(old.resolve())}, host_ports=[8069],
        created=2000, image_id="sha256:old",
    )

    page = client.get(f"/?settings={row.id}").text
    source = page.split("<h3>source</h3>", 1)[1].split("<h3>target</h3>", 1)[0]
    actions = source.split('<div class="side-actions">', 1)[1].split("</div>", 1)[0]

    assert "Changed since this container was built" in source
    assert '<span class="mono">Dockerfile</span>' in source
    assert 'containers/source/rebuild"' in actions
    assert 'class="primary"' in actions and "data-confirm-text" in actions
