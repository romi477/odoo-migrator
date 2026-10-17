import re

from odoo_migrator.db import database
from odoo_migrator.models import ACCENTS, Migration, Module
from odoo_migrator.rules import supported_versions


def _create(client, name, accent="violet", source="17.0", target="18.0"):

    return client.post(
        "/migrations",
        data={
            "name": name,
            "accent": accent,
            "source_version": source,
            "target_version": target,
            "old_project_path": f"/tmp/{name}/old",
            "new_project_path": f"/tmp/{name}/new",
        },
        follow_redirects=True,
    )


def test_empty_overview_shows_empty_state(client):
    html = client.get("/").text

    assert "No migrations yet." in html
    assert "Create migration" in html
    # The button, not six fields: the form was taking most of the screen to
    # ask a question nobody had asked yet.
    assert 'name="old_project_path"' not in html
    assert 'href="/?create=1' in html

    asked = client.get("/?create=1").text

    assert 'name="old_project_path"' in asked
    css = client.get("/static/style.css").text
    rule = css.split(".create-migration {", 1)[1].split("}", 1)[0]
    assert "margin-inline: auto" in rule
    # Outlined in the chosen accent, with a cyan fallback for the specimen.
    assert "border-color: var(--accent, var(--cyan))" in rule


def test_create_two_migrations_lists_both(client):
    _create(client, "Acme")
    html = _create(client, "Beta").text

    assert "Acme" in html
    assert "Beta" in html
    assert "17.0" in html
    assert "18.0" in html
    assert "No migrations yet." not in html
    assert ">Settings<" not in html
    assert "row-toggle" in html
    assert "collapsed" in html
    assert 'aria-expanded="false"' in html


def test_collapsed_row_shows_module_progress_and_attention_counts(client):
    """The collapsed row carries a progress bar and the counts a human cares
    about, so the list reads without opening a card. Struck modules are not
    in the migration, so they are not in the total."""
    created = _create(client, "Acme")
    row_id = created.text.split('id="m-', 1)[1].split('"', 1)[0]
    with database.connection_context():
        Module.create(migration=row_id, technical_name="a", status="passed")
        Module.create(migration=row_id, technical_name="b", status="failed")
        Module.create(migration=row_id, technical_name="c", status="waiting_for_human")
        Module.create(migration=row_id, technical_name="d", status="queued")
        Module.create(
            migration=row_id, technical_name="e", status="queued", struck_out=True
        )
    html = client.get("/").text

    assert "progress-bar" in html
    assert "1 of 4 modules" in html
    assert "1 waiting for you" in html
    assert "1 failed" in html


def test_a_migration_with_no_modules_has_no_progress_bar(client):
    _create(client, "Acme")
    html = client.get("/").text

    assert "progress-bar" not in html


def test_list_row_sets_accent_as_custom_property(client):
    html = _create(client, "Acme", accent="violet").text

    assert 'style="--accent:' in html
    assert ACCENTS["violet"] in html


def test_unknown_accent_is_rejected(client):
    response = _create(client, "Acme", accent="cyan")

    assert response.status_code == 200
    assert "Pick an accent that is not a status colour." in response.text
    with database.connection_context():
        assert Migration.select().count() == 0


def test_archive_hides_migration_from_list_and_switcher(client):
    _create(client, "Acme")
    with database.connection_context():
        row = Migration.get(Migration.name == "Acme")
    html = client.post(f"/migrations/{row.id}/archive", follow_redirects=True).text

    assert "Acme" not in html
    with database.connection_context():
        assert Migration.get_by_id(row.id).archived_at is not None


def test_open_migration_changes_tree_context_only(client):
    _create(client, "Acme")
    _create(client, "Beta")
    with database.connection_context():
        acme = Migration.get(Migration.name == "Acme")
        beta = Migration.get(Migration.name == "Beta")
        acme_id, beta_id = acme.id, beta.id

    tree = client.post(f"/migrations/{acme_id}/open", follow_redirects=True)

    assert tree.url.path == "/tree"
    assert f'data-open-migration="{acme_id}"' in tree.text
    assert "Acme" in tree.text

    tree = client.post(f"/migrations/{beta_id}/open", follow_redirects=True)

    assert f'data-open-migration="{beta_id}"' in tree.text
    assert "Beta" in tree.text
    with database.connection_context():
        assert Migration.get_by_id(acme_id).status == "idle"
        assert Migration.get_by_id(beta_id).status == "idle"


def test_all_planned_models_create_tables(app):
    from odoo_migrator.models import (
        AppSettings,
        Assumption,
        Attempt,
        Batch,
        CartridgeInstall,
        Finding,
        Job,
        Module,
    )

    with database.connection_context():
        assert AppSettings.table_exists()
        assert CartridgeInstall.table_exists()
        assert Migration.table_exists()
        assert Module.table_exists()
        assert Batch.table_exists()
        assert Job.table_exists()
        assert Attempt.table_exists()
        assert Finding.table_exists()
        assert Assumption.table_exists()


def test_an_impossible_version_pair_is_refused_at_creation(client):
    """Not three screens later when the first job is assembled."""
    for source, target, expected in (
        ("18.0", "18.0", "not a version jump"),
        ("18.0", "17.0", "downgrade"),
        ("17.2", "18.0", "on-premise"),
    ):
        response = client.post(
            "/migrations",
            data={
                "name": "Acme",
                "accent": "violet",
                "source_version": source,
                "target_version": target,
                "old_project_path": "/tmp/old",
                "new_project_path": "/tmp/new",
            },
            follow_redirects=True,
        )

        assert response.status_code == 200, (source, target)
        assert expected in response.text, (source, target)

    with database.connection_context():
        assert Migration.select().count() == 0


def _checked_accent(html: str) -> str:
    match = re.search(r'value="([a-z]+)" checked', html)

    return match.group(1) if match else ""


def test_default_accent_moves_on_so_two_migrations_are_not_the_same_colour(client):
    """A fixed default would make every migration violet, which is exactly what
    the accent exists to prevent."""
    first = _checked_accent(client.get("/?create=1").text)
    _create(client, "Acme", accent=first)
    second = _checked_accent(client.get("/?create=1").text)
    _create(client, "Beta", accent=second)
    third = _checked_accent(client.get("/?create=1").text)

    assert len({first, second, third}) == 3


def test_archiving_frees_its_accent_for_reuse(client):
    first = _checked_accent(client.get("/?create=1").text)
    _create(client, "Acme", accent=first)
    with database.connection_context():
        row = Migration.get(Migration.name == "Acme")
    client.post(f"/migrations/{row.id}/archive", follow_redirects=True)

    assert _checked_accent(client.get("/?create=1").text) == first


def test_accent_options_are_not_status_colours(client):
    """Status hues carry meaning: green passed, red failed, amber attention,
    cyan active. A migration's identity colour must not speak that language."""
    status_hues = (" 155)", " 25)", " 62)", " 205)", " 82)")

    for value in ACCENTS.values():

        assert not value.endswith(status_hues)


def test_switcher_and_accent_dot_are_on_the_styleguide(client):
    html = client.get("/components").text

    assert "Accent" in html
    assert "accent-dot" in html
    assert "migration-switcher" in html


def test_a_refused_creation_keeps_the_form_open_with_what_was_typed(client):
    """Re-rendering an empty form is how someone loses two paths they
    pasted. The refusal keeps both, and the form itself."""
    refused = client.post(
        "/migrations",
        data={
            "name": "Same",
            "accent": "violet",
            "source_version": "18.0",
            "target_version": "18.0",
            "old_project_path": "/opt/projects/sandbox/18",
            "new_project_path": "/opt/projects/sandbox/18-new",
        },
        follow_redirects=True,
    )

    assert "form-error" in refused.text
    assert 'name="old_project_path" value="/opt/projects/sandbox/18"' in refused.text
    assert 'name="new_project_path" value="/opt/projects/sandbox/18-new"' in refused.text
    assert 'value="Same"' in refused.text


def test_supported_versions_are_the_rules_files():
    assert supported_versions() == ["16.0", "17.0", "18.0", "19.0", "20.0"]


def test_the_create_form_offers_versions_as_selects(client):
    html = client.get("/?create=1").text

    assert '<select class="mono" name="source_version"' in html
    assert '<select class="mono" name="target_version"' in html
    assert 'name="source_version" value=' not in html
    for version in supported_versions():
        assert f'value="{version}"' in html


def test_a_version_without_rules_is_refused_at_creation(client):
    response = _create(client, "Acme", target="21.0")

    assert response.status_code == 200
    assert "21.0 is not supported" in response.text
    with database.connection_context():
        assert Migration.select().count() == 0


def test_the_widest_supported_jump_is_accepted(client):
    response = _create(client, "Wide", source="16.0", target="20.0")

    assert response.status_code == 200
    with database.connection_context():
        assert Migration.get(Migration.name == "Wide").target_version == "20.0"


def test_a_card_keeps_its_thick_accent_edge_only_while_collapsed(client):
    """Open, the card is outlined all round in its accent, and a thick left
    edge beside three thin ones looked wrong. Collapsed, the thick edge is the
    marker; the one pixel it gives up open is put back as padding, so the
    header does not move when the card opens."""
    css = client.get("/static/style.css").text

    closed = css.split(".migration-list .row-card {", 1)[1].split("}", 1)[0]
    assert "border-left-width: 3px" in closed
    opened = css.split(".migration-list .row-card.expanded {", 1)[1].split("}", 1)[0]
    assert "border-left-width: 1px" in opened
    assert "padding-left: 2px" in opened
