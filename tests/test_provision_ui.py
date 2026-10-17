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


def test_project_setup_names_the_migration_on_every_container_action(client, tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    _compose(old, 8069)
    _compose(new, 8070)
    _, row = _open_migration(client, old=old, new=new)
    page = client.get(f"/?settings={row.id}").text

    # The name is the accessible name, not the visible label: as text on all
    # eight buttons it wrapped every one of them over two lines, repeating
    # what the card heading and the page heading already say.
    for action in ("Up", "Stop", "Attach", "Restart", "Rebuild"):

        assert f'aria-label="{action} the source container of Acme"' in page
        assert f">{action}<" in page

    assert 'aria-label="Up the target container of Acme"' in page
    assert 'aria-label="Stop the target container of Acme"' in page
    assert "containers on this machine" in page
    # The card headings are the roles, not "source container" / "target container".
    assert ">source<" in page
    assert ">target<" in page


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

    assert "Up Acme old" in html
    assert "Odoo containers running" in html
    assert "Container" in html


def test_actions_that_take_a_container_away_ask_first(client, tmp_path):
    """Stop, Restart and Rebuild under a running job fail it at its next
    command, and one stray press did that with no question asked."""
    old = tmp_path / "old"
    new = tmp_path / "new"
    _compose(old, 8069)
    _compose(new, 8070)
    _, row = _open_migration(client, old=old, new=new)
    page = client.get(f"/?settings={row.id}").text

    for action in ("stop", "restart", "rebuild"):
        form = page[page.index(f'containers/source/{action}"') :]
        assert "data-confirm-text" in form[: form.index(">")]
    up = page[page.index('containers/source/up"') :]
    assert "data-confirm-text" not in up[: up.index(">")]
