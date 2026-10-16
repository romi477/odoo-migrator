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


# What the page fetches for itself: a graph, a run log, a log line's body,
# one module's last outcome. These are read by the browser that is already
# showing the page, on loopback, and carry no key. Anything else under /api/
# would be a machine surface coming back.
PAGE_OWN = ("/api/flow", "/api/jobs", "/api/log", "/api/result")


def test_no_route_requires_a_machine_key(client):
    """The File API and the Sheller proxy were the only authenticated routes.

    They existed because a remote executor ran in a container and could not
    reach a daemon that publishes to loopback. What is left under /api/ is
    the page fetching its own data.
    """
    paths = [route.path for route in client.app.routes]

    assert not [p for p in paths if p.startswith("/api/files")]
    assert not [p for p in paths if p.startswith("/api/sheller")]
    assert not [p for p in paths if "webhook" in p or "resume" in p]
    assert not [
        p
        for p in paths
        if p.startswith("/api/") and not p.startswith(PAGE_OWN)
    ]


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


def test_a_saved_secret_is_dots_and_the_bin_removes_it(client):
    from odoo_migrator.settings import SECRET_MASK, provider_setting

    page = client.get("/settings").text

    assert re.search(
        r'name="provider_cursor_api_key"[^>]*value=""', page
    )
    assert 'aria-label="Remove Cursor API key"' in page
    assert 'aria-label="Remove Anthropic API key"' in page
    saved = client.post(
        "/settings",
        data={
            "sheller_base_url": "http://127.0.0.1:8765",
            "sheller_image_tag": "latest",
            "provider_cursor_api_key": "cursor-secret",
            "provider_anthropic_api_key": "anthropic-secret",
        },
    )
    saved = client.get("/settings")

    assert f'value="{SECRET_MASK}"' in saved.text
    assert saved.text.count(f'value="{SECRET_MASK}"') == 2
    assert "cursor-secret" not in saved.text
    assert "anthropic-secret" not in saved.text
    assert 'aria-label="Remove Cursor API key"' in saved.text
    assert 'aria-label="Remove Anthropic API key"' in saved.text
    assert provider_setting("cursor", "api_key") == "cursor-secret"
    kept = client.post(
        "/settings",
        data={
            "sheller_base_url": "http://127.0.0.1:8765",
            "sheller_image_tag": "latest",
            "provider_cursor_api_key": SECRET_MASK,
            "provider_anthropic_api_key": SECRET_MASK,
        },
    )

    assert provider_setting("cursor", "api_key") == "cursor-secret"
    assert provider_setting("anthropic", "api_key") == "anthropic-secret"
    cleared = client.post(
        "/settings",
        data={
            "sheller_base_url": "http://127.0.0.1:8765",
            "sheller_image_tag": "latest",
            "provider_cursor_api_key": SECRET_MASK,
            "provider_anthropic_api_key": SECRET_MASK,
            "clear_provider": "cursor__api_key",
        },
        follow_redirects=True,
    )

    assert provider_setting("cursor", "api_key") == ""
    assert provider_setting("anthropic", "api_key") == "anthropic-secret"
    assert re.search(
        r'name="provider_cursor_api_key"[^>]*value=""', cleared.text
    )
    assert 'aria-label="Remove Cursor API key"' in cleared.text
    assert 'aria-label="Remove Anthropic API key"' in cleared.text
    assert kept.status_code == 200


def test_sheller_base_url_saves_and_stays_off_other_pages(client):
    saved = client.post(
        "/settings",
        data={
            "sheller_base_url": "http://127.0.0.1:8765",
            "sheller_image_tag": "1.7.0",
        },
    )
    saved = client.get("/settings")

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


def test_the_card_picks_a_provider_and_only_its_models(client):
    _create(client, "Acme")
    with database.connection_context():
        row = Migration.get(Migration.name == "Acme")
    page = client.get(f"/?settings={row.id}").text

    assert 'name="provider_override"' in page
    assert 'value="anthropic" selected' in page
    assert 'value="claude-sonnet-5-5" data-provider="anthropic" selected' in page
    assert "composer-2.5" in page
    saved = client.post(
        f"/migrations/{row.id}/settings",
        data={
            "strictness_default": "basic",
            "retry_limit": "5",
            "ask_human_mode": "sync",
            "provider_override": "cursor",
            "model_override": "composer-2.5",
        },
        follow_redirects=True,
    )

    assert saved.status_code == 200
    assert 'value="cursor" selected' in saved.text
    assert 'value="composer-2.5" data-provider="cursor" selected' in saved.text
    with database.connection_context():
        row = Migration.get_by_id(row.id)
        assert row.provider_override == "cursor"
        assert row.model_override == "composer-2.5"
    refused = client.post(
        f"/migrations/{row.id}/settings",
        data={
            "strictness_default": "basic",
            "retry_limit": "5",
            "ask_human_mode": "sync",
            "provider_override": "cursor",
            "model_override": "claude-opus-5",
        },
        follow_redirects=True,
    )

    assert "Nothing saved." in refused.text
    with database.connection_context():
        row = Migration.get_by_id(row.id)
        assert row.model_override == "composer-2.5"


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


def test_the_stream_reads_the_database_off_the_event_loop(client, monkeypatch):
    """The hello's two queries ran on the event loop itself, so a slow or
    locked database stalled every open page and request, not just this one."""
    import asyncio

    on_loop = []

    def watched(name, real):
        def call():
            try:
                asyncio.get_running_loop()
                on_loop.append(name)
            except RuntimeError:
                pass

            return real()

        return call

    monkeypatch.setattr(api, "SSE_HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr(api, "SSE_MAX_SECONDS", 0.05)
    monkeypatch.setattr(
        api, "running_migration_count", watched("count", api.running_migration_count)
    )
    monkeypatch.setattr(api, "lamp_state", watched("lamp", api.lamp_state))

    assert "event: lamp" in client.get("/sse").text
    assert on_loop == []


def test_sse_window_is_bounded_in_production_defaults():
    """An endless generator only ends when it is cancelled, which makes both
    shutdown and the test suite hang on whatever sleep it was in."""

    assert api.SSE_MAX_SECONDS > api.SSE_HEARTBEAT_SECONDS
    assert api.SSE_MAX_SECONDS <= 600


def test_the_lamp_pulses_green_while_something_runs_and_amber_wins(client):
    """It sat grey beside "1 run": nothing said the work was going on."""
    from test_tree import _open

    from odoo_migrator.models import Module

    row = _open(client, "Busy")
    with database.connection_context():
        Module.create(migration=row.id, technical_name="sale_custom", status="running")

    running = client.get("/").text

    assert 'class="attention-lamp running"' in running
    assert "A migration is running." in running

    with database.connection_context():
        Module.create(migration=row.id, technical_name="stock_custom", status="failed")

    assert 'class="attention-lamp needs-attention"' in client.get("/").text


def test_styleguide_shows_attention_lamp(client):
    html = client.get("/components").text

    assert "attention-lamp" in html
    assert "Attention" in html


def _save(client, **extra):
    return client.post(
        "/settings",
        data={
            "sheller_base_url": "http://127.0.0.1:8765",
            "sheller_image_tag": "latest",
            **extra,
        },
        follow_redirects=False,
    )


def test_settings_remember_the_screen_they_were_opened_from(client):
    """Save and Close goes back where the person was, not to a fixed page."""
    page = client.get("/settings", headers={"referer": "http://127.0.0.1/result"}).text

    assert 'name="back" value="/result"' in page
    assert "Save and Close" in page


def test_settings_fall_back_to_overview_when_nothing_says_where_from(client):
    plain = client.get("/settings").text
    # A page of some other site is not a screen of this one.
    foreign = client.get("/settings", headers={"referer": "https://evil.example/result"}).text
    # Settings coming from settings is not where to return to.
    itself = client.get("/settings", headers={"referer": "http://127.0.0.1/settings"}).text

    for page in (plain, foreign, itself):
        assert 'name="back" value="/"' in page


def test_save_and_close_returns_to_the_screen_it_came_from(client):
    saved = _save(client, back="/tree")

    assert saved.status_code == 303
    assert saved.headers["location"] == "/tree"


def test_save_and_close_never_leaves_the_app(client):
    for hostile in ("https://evil.example/", "//evil.example/", "/settings", "/nowhere", ""):
        saved = _save(client, back=hostile)

        assert saved.headers["location"] == "/", hostile


def test_the_bin_stays_on_settings_and_remembers_where_to_go_back(client):
    """Removing a key is not leaving the page."""
    removed = _save(client, back="/result", clear_provider="cursor__api_key")

    assert removed.status_code == 303
    assert removed.headers["location"] == "/settings?back=%2Fresult"

    page = client.get("/settings?back=/result").text

    assert 'name="back" value="/result"' in page
