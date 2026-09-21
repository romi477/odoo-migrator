import re
from pathlib import Path

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


def test_app_settings_and_migration_settings_are_separate_screens(client):
    app_page = client.get("/settings").text
    mig_page = client.get("/settings/migration").text

    assert "App settings" in app_page
    assert 'name="n8n_base_url"' in app_page
    assert "This migration" in app_page
    assert 'name="strictness_default"' not in app_page

    assert "This migration" in mig_page
    assert "Open a migration" in mig_page
    assert 'name="n8n_base_url"' not in mig_page


def test_n8n_base_url_saves_and_stays_off_other_pages(client):
    saved = client.post(
        "/settings",
        data={
            "n8n_base_url": "http://127.0.0.1:5678",
            "n8n_api_key": "n8n_test_key",
            "app_base_url": "http://host.docker.internal:8000",
            "sheller_base_url": "http://127.0.0.1:8765",
            "sheller_image_tag": "1.7.0",
            "min_sheller_version": "1.7.0",
        },
        follow_redirects=True,
    )

    assert saved.status_code == 200
    assert 'value="http://127.0.0.1:5678"' in saved.text
    assert "http://127.0.0.1:5678" not in client.get("/").text
    assert "http://127.0.0.1:5678" not in client.get("/tree").text
    with database.connection_context():
        assert AppSettings.get().n8n_base_url == "http://127.0.0.1:5678"


def test_n8n_base_url_is_not_hardcoded_in_the_app():
    """The URL lives in settings. A host baked into templates or Python is a
    second source of truth and the thing the later reverse-proxy move would
    have to hunt down."""
    root = Path(__file__).resolve().parents[1] / "odoo_migrator"
    offenders = []
    for path in root.rglob("*"):
        if path.suffix not in {".py", ".html", ".js", ".css"}:
            continue
        if "vendor" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        for needle in ("n8n.io", "localhost:5678", "127.0.0.1:5678"):
            if needle in text:
                offenders.append(f"{path.relative_to(root)}: {needle}")

    assert offenders == []


def test_migration_settings_save_on_the_open_migration(client):
    _create(client, "Acme")
    with database.connection_context():
        row = Migration.get(Migration.name == "Acme")
    client.post(f"/migrations/{row.id}/open", follow_redirects=True)
    page = client.post(
        "/settings/migration",
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
    """The name is the button's accessible name and tooltip. As visible text it
    wraps a list row over three lines while repeating what the row already
    shows; actions away from their row still spell it out."""
    html = _create(client, "Acme").text

    assert 'aria-label="Archive Acme"' in html
    assert 'title="Archive Acme"' in html
    assert ">Archive<" in html


def test_migration_settings_say_when_nothing_was_saved(client):
    """Re-rendering the old values with no message looks exactly like success."""
    no_migration_open = client.post(
        "/settings/migration",
        data={"strictness_default": "basic", "retry_limit": "5", "ask_human_mode": "sync"},
        follow_redirects=True,
    )

    assert "Open a migration before changing its settings." in no_migration_open.text

    _create(client, "Acme")
    with database.connection_context():
        row = Migration.get(Migration.name == "Acme")
    client.post(f"/migrations/{row.id}/open", follow_redirects=True)

    not_a_number = client.post(
        "/settings/migration",
        data={"strictness_default": "basic", "retry_limit": "soon", "ask_human_mode": "sync"},
        follow_redirects=True,
    )

    assert "Retry limit must be a whole number." in not_a_number.text

    rejected = client.post(
        "/settings/migration",
        data={"strictness_default": "paranoid", "retry_limit": "5", "ask_human_mode": "sync"},
        follow_redirects=True,
    )

    assert "Nothing saved." in rejected.text
    with database.connection_context():
        assert Migration.get_by_id(row.id).strictness_default == "basic"
        assert Migration.get_by_id(row.id).retry_limit == 5


def test_api_key_is_stored_but_never_rendered(client):
    client.post(
        "/settings",
        data={
            "n8n_base_url": "http://127.0.0.1:5678",
            "n8n_api_key": "n8n_secret_value",
            "app_base_url": "",
            "sheller_base_url": "http://127.0.0.1:8765",
            "sheller_image_tag": "",
            "min_sheller_version": "",
        },
        follow_redirects=True,
    )
    page = client.get("/settings").text

    assert "n8n_secret_value" not in page
    assert "stored — leave blank to keep" in page
    with database.connection_context():
        assert AppSettings.get().n8n_api_key == "n8n_secret_value"


def test_saving_with_a_blank_key_keeps_the_stored_one(client):
    client.post(
        "/settings",
        data={
            "n8n_base_url": "http://127.0.0.1:5678",
            "n8n_api_key": "n8n_secret_value",
            "app_base_url": "",
            "sheller_base_url": "http://127.0.0.1:8765",
            "sheller_image_tag": "",
            "min_sheller_version": "",
        },
        follow_redirects=True,
    )
    client.post(
        "/settings",
        data={
            "n8n_base_url": "http://127.0.0.1:5679",
            "n8n_api_key": "",
            "app_base_url": "",
            "sheller_base_url": "http://127.0.0.1:8765",
            "sheller_image_tag": "",
            "min_sheller_version": "",
        },
        follow_redirects=True,
    )

    with database.connection_context():
        stored = AppSettings.get()

        assert stored.n8n_api_key == "n8n_secret_value"
        assert stored.n8n_base_url == "http://127.0.0.1:5679"

    client.post("/settings/n8n-api-key/clear", follow_redirects=True)

    with database.connection_context():
        assert AppSettings.get().n8n_api_key == ""


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
    html = client.get("/styleguide").text

    assert "attention-lamp" in html
    assert "Attention" in html
