import re

from odoo_migrator import api
from odoo_migrator.db import database
from odoo_migrator.models import AppSettings, Migration


def _create(client, name="Acme"):

    return client.post(
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


def test_no_route_requires_a_machine_key(client):
    """The File API and the Sheller proxy were the only authenticated routes.

    They existed because a remote executor ran in a container and could not
    reach a daemon that publishes to loopback. Nothing machine-facing is left.
    """
    paths = [route.path for route in client.app.routes]

    assert not [p for p in paths if p.startswith("/api/files")]
    assert not [p for p in paths if p.startswith("/api/sheller")]
    assert not [p for p in paths if "webhook" in p or "resume" in p]
    assert not [p for p in paths if p.startswith("/api/")]


def test_app_settings_are_a_gear_and_a_migration_settings_live_in_its_row(client):
    """App-wide settings are behind the gear. A migration's own settings
    belong to that migration, so they open inside its row on Overview and
    act on its id — not on whatever the switcher is showing."""
    _create(client, "Acme")
    with database.connection_context():
        row = Migration.get(Migration.name == "Acme")

    app_page = client.get("/settings").text

    assert 'name="sheller_base_url"' in app_page
    assert 'name="strictness_default"' not in app_page
    assert "/settings/migration" not in app_page

    nav = client.get("/").text

    assert 'class="gear' in nav
    assert 'aria-label="App settings"' in nav
    assert 'name="strictness_default"' not in nav

    expanded = client.get(f"/?settings={row.id}").text

    assert 'name="strictness_default"' in expanded
    assert f'action="/migrations/{row.id}/settings"' in expanded
    assert f'action="/migrations/{row.id}/containers/old/up"' in expanded
    assert 'name="sheller_base_url"' not in expanded
    assert ">Settings<" not in expanded
    assert "row-card expanded" in expanded
    assert 'aria-expanded="true"' in expanded


def test_sheller_base_url_saves_and_stays_off_other_pages(client):
    saved = client.post(
        "/settings",
        data={
            "sheller_base_url": "http://127.0.0.1:8765",
            "sheller_image_tag": "1.7.0",
        },
        follow_redirects=True,
    )

    assert saved.status_code == 200
    assert 'value="http://127.0.0.1:8765"' in saved.text
    assert "1.7.0" not in client.get("/").text
    assert "1.7.0" not in client.get("/tree").text
    with database.connection_context():
        stored = AppSettings.get()
        assert stored.sheller_base_url == "http://127.0.0.1:8765"
        assert stored.sheller_image_tag == "1.7.0"


def test_migration_settings_save_on_the_open_migration(client):
    _create(client, "Acme")
    with database.connection_context():
        row = Migration.get(Migration.name == "Acme")
    client.post(f"/migrations/{row.id}/open", follow_redirects=True)
    page = client.post(
        f"/migrations/{row.id}/settings",
        data={
            "strictness_default": "enhanced",
            "retry_limit": "8",
            "ask_human_mode": "batch_defer",
        },
        follow_redirects=True,
    )

    assert page.status_code == 200
    assert "Acme" in page.text
    assert 'value="enhanced" selected' in page.text
    with database.connection_context():
        row = Migration.get_by_id(row.id)
        assert row.strictness_default == "enhanced"
        assert row.retry_limit == 8
        assert row.ask_human_mode == "batch_defer"


def test_archive_button_names_the_migration_accessibly(client):
    """Archive is a bin at the foot of the open card, not a labelled button on
    the collapsed row. The name is its accessible name and tooltip; a bin
    needs no visible text, and the row already shows the name a line above."""
    created = _create(client, "Acme")
    row_id = created.text.split('id="m-', 1)[1].split('"', 1)[0]
    collapsed = client.get("/").text

    assert "Archive Acme" not in collapsed

    html = client.get(f"/?settings={row_id}").text

    assert 'aria-label="Archive Acme"' in html
    assert 'title="Archive Acme"' in html
    assert "bin-icon" in html
    assert ">Archive<" not in html


def test_migration_settings_say_when_nothing_was_saved(client):
    """Re-rendering the old values with no message looks exactly like success."""
    unknown = client.post(
        "/migrations/nobody/settings",
        data={"strictness_default": "basic", "retry_limit": "5", "ask_human_mode": "sync"},
        follow_redirects=True,
    )

    assert "No such migration." in unknown.text

    _create(client, "Acme")
    with database.connection_context():
        row = Migration.get(Migration.name == "Acme")
    client.post(f"/migrations/{row.id}/open", follow_redirects=True)

    not_a_number = client.post(
        f"/migrations/{row.id}/settings",
        data={"strictness_default": "basic", "retry_limit": "soon", "ask_human_mode": "sync"},
        follow_redirects=True,
    )

    assert "Retry limit must be a whole number." in not_a_number.text

    rejected = client.post(
        f"/migrations/{row.id}/settings",
        data={"strictness_default": "paranoid", "retry_limit": "5", "ask_human_mode": "sync"},
        follow_redirects=True,
    )

    assert "Nothing saved." in rejected.text
    with database.connection_context():
        assert Migration.get_by_id(row.id).strictness_default == "basic"
        assert Migration.get_by_id(row.id).retry_limit == 5


def test_attention_lamp_and_running_count_are_rendered_from_the_database(client):
    """They used to be hardcoded idle/0 and moved only on an SSE event, so a
    reload while a module waited for an answer showed a calm nav bar."""
    from test_tree import _open

    from odoo_migrator.models import Module

    html = client.get("/").text

    assert 'class="attention-lamp idle"' in html
    assert 'data-running="0"' in html

    row = _open(client, "Waiting")
    with database.connection_context():
        Module.create(migration=row.id, technical_name="sale_custom", status="running")
        Module.create(migration=row.id, technical_name="stock_custom", status="waiting_for_human")
    busy = client.get("/").text

    assert 'class="attention-lamp needs-attention"' in busy
    assert 'data-running="1"' in busy
    assert 'id="sse-hook"' in html
    assert "/static/sse.js" in html
    assert "/sse" in html


def test_sse_stream_stays_open_heartbeats_and_reconnects_promptly(client, monkeypatch):
    """A stream that closes after one event and sets a long retry looks like
    "SSE worked once and then stopped": the browser waits out the retry before
    trying again. This one holds the connection, heartbeats, and asks for a
    quick reconnect when it does close.

    The window is shortened here rather than waiting five minutes; the point
    under test is that there is a heartbeat and that the window ends.
    """
    monkeypatch.setattr(api, "SSE_HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr(api, "SSE_MAX_SECONDS", 0.1)

    response = client.get("/sse")

    assert response.status_code == 200
    assert "text/event-stream" in response.headers["content-type"]
    # The hello carries the current state, so a reconnect re-syncs a nav
    # bar that missed events while the stream was down.
    assert "event: running" in response.text
    assert "event: lamp" in response.text
    assert ": ping" in response.text

    retry_ms = int(re.search(r"retry:\s*(\d+)", response.text).group(1))

    assert retry_ms <= 10_000


def test_sse_window_is_bounded_in_production_defaults():
    """An endless generator only ends when it is cancelled, which makes both
    shutdown and the test suite hang on whatever sleep it was in."""

    assert api.SSE_MAX_SECONDS > api.SSE_HEARTBEAT_SECONDS
    assert api.SSE_MAX_SECONDS <= 600


def test_styleguide_shows_attention_lamp(client):
    html = client.get("/components").text

    assert "attention-lamp" in html
    assert "Attention" in html
