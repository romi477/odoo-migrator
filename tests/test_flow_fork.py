"""Forking a flow: the only way to write one from the viewer.

A fork is a new flow id and version. It never replaces a flow that is
already there, whatever its text says, because the stock file ships with the
app and a pin on another flow would change under a running migration.
"""

import html
import re
from pathlib import Path

import pytest

from odoo_migrator.cartridges import (
    FlowTaken,
    fork_from_text,
    install_from_text,
    next_version,
)
from odoo_migrator.db import database
from odoo_migrator.flowfile import flow_text
from odoo_migrator.models import CartridgeInstall
from odoo_migrator.paths import static_dir
from odoo_migrator.runner.cartridge import CartridgeError

STOCK = Path("cartridges/stock.yaml").read_text(encoding="utf-8")


def _as(cartridge: str, version: str) -> str:
    text = STOCK.replace("cartridge: stock", f"cartridge: {cartridge}", 1)

    return text.replace("version: 1.0.0", f'version: "{version}"', 1)


# -- the next free version ------------------------------------------------


def test_the_next_version_of_stock_is_the_patch_after_it(client):
    assert next_version("stock", "1.0.0") == "1.0.1"


def test_a_version_that_is_taken_is_skipped(client):
    install_from_text(_as("stock", "1.0.1"))
    install_from_text(_as("stock", "1.0.2"))

    assert next_version("stock", "1.0.0") == "1.0.3"


def test_a_shorter_version_counts_its_last_number(client):
    assert next_version("acme", "2.3") == "2.4"
    assert next_version("acme", "7") == "8"


def test_a_version_that_does_not_end_in_a_number_gets_a_dot_one(client):
    assert next_version("acme", "2026-10") == "2026-10.1"
    assert next_version("acme", "1.0.0-rc") == "1.0.0-rc.1"


def test_the_version_is_free_for_the_id_it_is_asked_about_only(client):
    install_from_text(_as("acme", "1.0.1"))

    assert next_version("stock", "1.0.0") == "1.0.1"


# -- saving a fork ---------------------------------------------------------


def test_a_fork_is_stored_under_the_id_and_version_its_text_names(client):
    text = _as("stock", "1.0.1")

    row = fork_from_text(text)

    assert (row.cartridge_id, row.version) == ("stock", "1.0.1")
    assert flow_text("stock", "1.0.1") == text
    assert row.smoke_status == "untested"


def test_a_fork_may_start_a_flow_of_its_own(client):
    fork_from_text(_as("acme", "1.0.0"))

    assert flow_text("acme", "1.0.0") is not None


def test_the_shipped_stock_flow_is_never_replaced(client):
    with pytest.raises(FlowTaken, match=r"stock@1\.0\.0"):
        fork_from_text(STOCK.replace("model:", "# edited\nmodel:", 1))

    assert flow_text("stock", "1.0.0") == STOCK
    with database.connection_context():
        assert not CartridgeInstall.select().exists()


def test_an_installed_flow_is_never_replaced(client):
    original = _as("acme", "1.0.0")
    install_from_text(original)

    with pytest.raises(FlowTaken):
        fork_from_text(original.replace("model:", "# edited\nmodel:", 1))

    assert flow_text("acme", "1.0.0") == original


def test_a_smoke_row_without_a_body_still_holds_its_name(client):
    with database.connection_context():
        CartridgeInstall.create(cartridge_id="ghost", version="1.0.0", smoke_status="passed")

    with pytest.raises(FlowTaken):
        fork_from_text(_as("ghost", "1.0.0"))

    with database.connection_context():
        assert CartridgeInstall.get(CartridgeInstall.cartridge_id == "ghost").smoke_status == "passed"


def test_a_file_that_is_not_a_flow_is_refused(client):
    with pytest.raises(CartridgeError):
        fork_from_text("cartridge: acme\nversion: '1.0.1'\n")


@pytest.mark.parametrize(
    "cartridge, version",
    [
        ("acme@x", "1.0.1"),
        ("ac/me", "1.0.1"),
        ("acme", "1.0/1"),
        ("acme", "1.0 1"),
        ("acme", "a@b"),
        ("acme", "1" * 41),
        ("-acme", "1.0.1"),
    ],
)
def test_an_id_or_version_the_address_cannot_carry_is_refused(client, cartridge, version):
    with pytest.raises(CartridgeError, match="may hold only"):
        fork_from_text(_as(cartridge, version))

    with database.connection_context():
        assert not CartridgeInstall.select().exists()


# -- the route -------------------------------------------------------------


def test_the_flow_api_offers_the_next_version(client):
    body = client.get("/api/flows/stock/1.0.0").json()

    assert body["next_version"] == "1.0.1"


def test_posting_a_fork_stores_it_and_says_where_it_is(client):
    text = _as("stock", "1.0.1")

    response = client.post("/api/flows/fork", json={"text": text})

    assert response.status_code == 201
    body = response.json()
    assert (body["cartridge_id"], body["version"]) == ("stock", "1.0.1")
    assert re.fullmatch(r"[0-9a-f]{64}", body["sha256"])
    assert client.get("/api/flows/stock/1.0.1").json()["text"] == text


def test_a_name_that_is_taken_is_a_409_and_nothing_is_written(client):
    response = client.post("/api/flows/fork", json={"text": STOCK})

    assert response.status_code == 409
    assert "stock@1.0.0" in response.json()["error"]
    assert response.json()["next_version"] == "1.0.1"
    assert client.get("/api/flows/stock/1.0.0").json()["text"] == STOCK


def test_a_text_that_is_not_a_flow_is_a_400_with_the_reason(client):
    response = client.post("/api/flows/fork", json={"text": "cartridge: [\n"})

    assert response.status_code == 400
    assert response.json()["error"]


def test_a_body_without_text_is_a_400(client):
    assert client.post("/api/flows/fork", json={}).status_code == 400
    assert client.post("/api/flows/fork", json={"text": 5}).status_code == 400
    assert client.post("/api/flows/fork", content=b"not json").status_code == 400


def test_a_huge_text_is_refused_before_it_is_read_as_yaml(client):
    response = client.post("/api/flows/fork", json={"text": "# x\n" * 100_000})

    assert response.status_code == 413


def test_a_page_of_another_site_cannot_post_a_fork(client):
    response = client.post(
        "/api/flows/fork",
        json={"text": _as("acme", "1.0.0")},
        headers={"Origin": "http://evil.example"},
    )

    assert response.status_code == 403
    with database.connection_context():
        assert not CartridgeInstall.select().exists()


def test_the_routes_that_read_a_flow_still_change_nothing(client):
    assert client.post("/api/flows/stock/1.0.0").status_code == 405
    assert client.put("/api/flows/stock/1.0.0").status_code == 405
    assert client.delete("/api/flows/stock/1.0.0").status_code == 405


def test_the_import_route_still_replaces_a_flow_of_the_same_name(client):
    """Import is a person handing over a file on purpose. It is not the
    viewer's path and keeps its behaviour."""
    first = _as("acme", "1.0.0")
    install_from_text(first)
    second = first.replace("model:", "# second\nmodel:", 1)

    install_from_text(second)

    assert flow_text("acme", "1.0.0") == second


# -- the page --------------------------------------------------------------


def test_the_dialog_starts_read_only_and_its_edit_area_is_hidden(client):
    page = client.get("/").text
    dialog = page[page.index("data-flow-dialog") :]
    dialog = dialog[: dialog.index("</dialog>")]

    assert "data-flow-fork" in dialog
    assert "data-flow-save" in dialog
    area = dialog[dialog.index("<textarea") :].split(">", 1)[0]
    assert "hidden" in area
    assert "data-flow-edit" in area
    # The fields live in the bar, which starts hidden. Fork, Save fork and
    # Cancel sit together at the right of the head, and Save fork and Cancel
    # are hidden until a fork is open.
    for hidden in ("data-flow-bar", "data-flow-fork", "data-flow-save", "data-flow-cancel"):
        tag = dialog[dialog.rindex("<", 0, dialog.index(hidden)) :].split(">", 1)[0]
        assert "hidden" in tag, hidden
    bar = dialog[dialog.index("data-flow-bar") :].split("</div>", 1)[0]
    for inside in ("data-flow-id", "data-flow-version"):
        assert inside in bar
    for outside in ("data-flow-save", "data-flow-cancel"):
        assert outside not in bar


def test_the_hash_sits_by_the_title_and_the_buttons_at_the_right_edge(client):
    page = client.get("/").text
    head = page[page.index("flow-dialog-head") :].split("</header>", 1)[0]
    sheet = (static_dir() / "style.css").read_text(encoding="utf-8")

    order = [
        head.index(mark)
        for mark in (
            "data-flow-title",
            "data-flow-mode",
            "data-flow-hash",
            "data-flow-fork",
            "data-flow-save",
            "data-flow-cancel",
            "data-flow-close",
        )
    ]
    assert order == sorted(order)
    actions = sheet[sheet.index(".flow-dialog-actions {") :].split("}", 1)[0]
    assert "margin-left: auto" in actions
    hash_rule = sheet[sheet.index(".flow-viewer-hash {") :].split("}", 1)[0]
    assert "margin-left" not in hash_rule


def test_the_script_sends_the_text_and_nothing_else(client):
    script = (static_dir() / "flow_viewer.js").read_text(encoding="utf-8")

    assert '"/api/flows/fork"' in script
    assert 'method: "POST"' in script
    assert "JSON.stringify({ text:" in script


def test_the_components_page_shows_the_fork_controls(client):
    html = client.get("/components").text

    assert "Fork" in html
    assert "Save fork" in html


# -- the Flow select keeps the shipped flow ---------------------------------


def test_the_flows_on_offer_always_include_the_one_that_ships(client):
    from odoo_migrator.cartridges import list_installs

    assert [(row["cartridge_id"], row["version"]) for row in list_installs()] == [("stock", "1.0.0")]

    fork_from_text(_as("stock", "1.0.1"))

    assert [(row["cartridge_id"], row["version"]) for row in list_installs()] == [
        ("stock", "1.0.0"),
        ("stock", "1.0.1"),
    ]


def test_the_card_offers_the_shipped_flow_beside_a_fork_and_keeps_the_pin(client, tmp_path):
    fork_from_text(_as("stock", "1.0.1"))
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    client.post(
        "/migrations",
        data={
            "name": "Acme",
            "accent": "violet",
            "source_version": "17.0",
            "target_version": "18.0",
            "old_project_path": str(tmp_path / "old"),
            "new_project_path": str(tmp_path / "new"),
        },
    )
    with database.connection_context():
        from odoo_migrator.models import Migration

        row = Migration.get(Migration.name == "Acme")
    client.post(f"/migrations/{row.id}/open", follow_redirects=True)

    html = client.get(f"/?settings={row.id}").text
    select = html[html.index('name="cartridge_id"') :].split("</select>", 1)[0]

    assert 'value="stock@1.0.0" selected' in select
    assert 'value="stock@1.0.1"' in select
    assert "selected" not in select.split('value="stock@1.0.1"', 1)[1]


def test_the_script_reads_an_answer_that_is_not_json(client):
    """A server that fails says so in plain text. The dialog must report that,
    not the parser's complaint about its first character."""
    script = (static_dir() / "flow_viewer.js").read_text(encoding="utf-8")

    assert "response.text()" in script
    assert "The server answered " in script


# -- a stored flow that no longer loads cannot be pinned ---------------------


def test_a_stored_flow_in_the_old_side_words_is_refused_when_pinned(client, tmp_path):
    """A flow saved before the sides were renamed no longer loads. Pinning it
    used to be accepted, and every page that reads the pinned flow then
    answered 500."""
    (tmp_path / "old").mkdir()
    (tmp_path / "new").mkdir()
    client.post(
        "/migrations",
        data={
            "name": "Acme",
            "accent": "violet",
            "source_version": "17.0",
            "target_version": "18.0",
            "old_project_path": str(tmp_path / "old"),
            "new_project_path": str(tmp_path / "new"),
        },
    )
    with database.connection_context():
        from odoo_migrator.models import Migration

        row = Migration.get(Migration.name == "Acme")
        CartridgeInstall.create(
            cartridge_id="legacy",
            version="1.0.0",
            body=STOCK.replace("sessions: [source, target]", "sessions: [old, new]", 1),
            smoke_status="untested",
        )

    response = client.post(
        f"/migrations/{row.id}/settings",
        data={
            "strictness_default": "basic",
            "retry_limit": "3",
            "ask_human_mode": "sync",
            "cartridge_id": "legacy@1.0.0",
        },
    )

    assert response.status_code == 200
    page = html.unescape(response.text)
    assert "legacy@1.0.0 cannot be pinned" in page
    assert "'source' and 'target'" in page
    with database.connection_context():
        assert Migration.get_by_id(row.id).cartridge_id in (None, "", "stock")
