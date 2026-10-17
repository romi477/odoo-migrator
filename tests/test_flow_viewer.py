"""The flow file, read as the app holds it: its text and an outline of it.

The outline is the part a later editor builds on, so every node carries the
path a write would address and the lines it covers in the text.
"""

import re
from pathlib import Path

import pytest

from odoo_migrator.cartridges import install_from_text
from odoo_migrator.db import database
from odoo_migrator.flowfile import flow_outline, flow_text
from odoo_migrator.models import Migration
from odoo_migrator.paths import static_dir

STOCK = Path("cartridges/stock.yaml").read_text(encoding="utf-8")


def _walk(nodes):
    for node in nodes:
        yield node
        yield from _walk(node["children"])


def _find(nodes, *path):
    for node in _walk(nodes):
        if node["path"] == list(path):

            return node
    raise AssertionError(f"no node at {path}")


def _lines(text, node):
    first, last = node["lines"]

    return text.splitlines()[first - 1 : last]


def test_the_stock_flow_is_read_from_its_file(client):
    assert flow_text("stock", "1.0.0") == STOCK


def test_an_installed_flow_is_read_from_what_was_imported(client):
    text = STOCK.replace("cartridge: stock", "cartridge: acme").replace("version: 1.0.0", "version: 2.1.0")
    install_from_text(text)

    assert flow_text("acme", "2.1.0") == text


def test_a_flow_nobody_installed_is_not_there(client):
    assert flow_text("nothing", "9.9.9") is None
    assert flow_text("stock", "9.9.9") is None


def test_the_outline_covers_the_top_level_and_names_each_step():
    outline = flow_outline(STOCK)

    assert [node["label"] for node in outline][:3] == ["cartridge", "version", "schema"]
    assert {"model", "pipeline", "on_failure"} <= {node["label"] for node in outline}
    steps = _find(outline, "pipeline")["children"]
    assert [step["label"] for step in steps][:3] == ["agent · understand", "agent · port", "lint"]
    assert steps[0]["path"] == ["pipeline", 0]


def test_a_nodes_lines_start_at_its_own_key_and_stop_before_the_next():
    outline = flow_outline(STOCK)
    port = _find(outline, "pipeline", 1)
    prompt = _find(outline, "pipeline", 1, "prompt")

    assert _lines(STOCK, port)[0].strip() == "- step: agent"
    assert "id: port" in "\n".join(_lines(STOCK, port))
    assert _lines(STOCK, prompt)[0].strip() == "prompt: |"
    # The prompt is a block of its own, and the step after it is not in it.
    assert "- step: lint" not in "\n".join(_lines(STOCK, port))
    assert _lines(STOCK, _find(outline, "pipeline", 2))[0].strip() == "- step: lint"
    # A block does not carry the blank lines that separate it from the next.
    assert _lines(STOCK, prompt)[-1].strip() != ""


def test_a_scalar_shows_a_short_preview_and_a_block_shows_its_first_line():
    outline = flow_outline(STOCK)

    assert _find(outline, "model", "name")["preview"] == "claude-sonnet-5-5"
    assert _find(outline, "pipeline", 1, "prompt")["preview"].startswith("Port the module")
    assert _find(outline, "model")["preview"] == ""


def test_a_file_that_is_not_yaml_has_an_empty_outline():
    assert flow_outline("a: [unclosed") == []


def test_the_api_returns_text_hash_and_outline(client):
    body = client.get("/api/flows/stock/1.0.0").json()

    assert body["text"] == STOCK
    assert re.fullmatch(r"[0-9a-f]{64}", body["sha256"])
    assert body["cartridge_id"] == "stock"
    assert body["outline"][0]["label"] == "cartridge"


def test_an_unknown_flow_is_a_404(client):
    assert client.get("/api/flows/nothing/9.9.9").status_code == 404


def test_every_page_carries_the_dialog_and_it_is_read_only(client):
    for path in ("/", "/flow"):
        page = client.get(path).text

        assert "data-flow-dialog" in page
        assert "read-only" in page
        assert 'class="flow-dialog"' in page
        assert "<textarea" not in page[page.index("data-flow-dialog") :][:3000]
        assert "contenteditable" not in page


def test_there_is_no_page_of_its_own_for_a_flow_file(client):
    """The file is read inside the app, in a dialog; a window of the browser's
    own was what was asked for and not wanted."""
    assert client.get("/flows/stock/1.0.0").status_code == 404


def test_the_flow_file_has_no_route_that_changes_anything(client):
    assert client.post("/api/flows/stock/1.0.0").status_code == 405
    assert client.put("/api/flows/stock/1.0.0").status_code == 405


def _card(client, tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    client.post(
        "/migrations",
        data={
            "name": "Acme",
            "accent": "violet",
            "source_version": "17.0",
            "target_version": "18.0",
            "old_project_path": str(old),
            "new_project_path": str(new),
        },
    )
    with database.connection_context():
        row = Migration.get(Migration.name == "Acme")
    client.post(f"/migrations/{row.id}/open", follow_redirects=True)

    return client.get(f"/?settings={row.id}").text


def test_the_flow_field_has_an_eye_that_opens_the_selected_flow(client, tmp_path):
    html = _card(client, tmp_path)

    assert 'data-flow-view data-flow-url="/api/flows/stock/1.0.0"' in html
    assert 'data-flow-name="stock 1.0.0"' in html
    assert 'aria-label="View the flow file"' in html
    # A button: nothing to follow, and nothing that leaves the page.
    eye = html[html.rindex("<", 0, html.index("data-flow-view")) :][:12]
    assert eye.startswith("<button")
    # It follows the select, so it sits in the same item as the select.
    item = html[html.index('name="cartridge_id"') :]
    assert item.index("data-flow-view") < item.index("</li>")


def test_the_script_opens_a_dialog_of_the_app_not_a_window_of_the_browser():
    text = (static_dir() / "flow_viewer.js").read_text(encoding="utf-8")

    assert ".showModal()" in text
    assert "window.open" not in text
    assert "data-flow-dialog" in text
    # A press on the backdrop is a press on the dialog itself.
    assert "event.target === dialog" in text


def test_the_components_page_shows_the_viewer(client):
    html = client.get("/components").text

    assert "flow-dialog" in html
    assert "flow-view-button" in html


@pytest.mark.parametrize("name", ["flow_viewer.js"])
def test_the_script_writes_no_markup(name):
    text = (static_dir() / name).read_text(encoding="utf-8")

    assert not re.search(r"\.innerHTML\s*=\s*(?![\"']{2})", text)


def test_the_dialog_is_a_tenth_narrower_than_the_card_it_opened_from():
    """At the card's own width its edges ran on into the dialog's, and the
    two read as one window."""
    script = (static_dir() / "flow_viewer.js").read_text(encoding="utf-8")
    sheet = (static_dir() / "style.css").read_text(encoding="utf-8")

    assert 'closest(".row-card")' in script
    assert "getBoundingClientRect().width * 0.9" in script
    assert 'setProperty("--flow-width"' in script
    block = sheet[sheet.index(".flow-dialog {") :].split("}", 1)[0]
    assert "var(--flow-width" in block
    # Never wider than the window it sits in.
    assert "100vw" in block


def test_the_page_behind_the_flow_dialog_is_blurred():
    sheet = (static_dir() / "style.css").read_text(encoding="utf-8")
    block = sheet[sheet.index(".flow-dialog::backdrop") :].split("}", 1)[0]

    assert "backdrop-filter: blur(" in block
