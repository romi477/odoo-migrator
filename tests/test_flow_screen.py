"""Flow draws the pinned cartridge. The graph is derived, never stored."""

import pytest

from odoo_migrator.migrations import create_migration


@pytest.fixture
def migration(client, tmp_path):
    del client
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()

    return create_migration(
        name="Acme",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path=str(old),
        new_project_path=str(new),
    )


def test_the_graph_is_derived_from_the_cartridge_not_stored(client, migration):
    graph = client.get(f"/api/flow/{migration['id']}/graph").json()

    by_id = {node["id"]: node for node in graph["nodes"]}
    assert by_id["understand"]["kind"] == "agent"
    assert "source Odoo" in by_id["understand"]["hint"]
    assert by_id["understand"]["actor"] == "agent"
    assert "read_file" in by_id["understand"]["tools"]
    assert "reading a module" in by_id["understand"]["prompt"]
    assert by_id["lint"]["actor"] == "function"
    assert by_id["lint"]["prompt"] == ""
    assert by_id["lint"]["tools"] == []
    assert "Parks the job" in by_id["ask_human"]["hint"]
    assert by_id["lane:pipeline"]["hint"] == ""
    assert by_id["understand"]["parent"] == "lane:pipeline"
    assert by_id["lint"]["kind"] == "lint"
    assert by_id["lint"]["gated_on"] is None
    assert by_id["probe"]["gated_on"] == "no_tests"
    assert by_id["write_test"]["gated_on"] == "no_tests"
    assert by_id["patch"]["gated_on"] == "small_fix"
    assert by_id["rewrite"]["gated_on"] == "start_over"
    assert by_id["ask_human"]["kind"] == "ask_human"
    assert by_id["ask_human"]["gated_on"] == "needs_human"
    assert by_id["diagnose"]["parent"] == "lane:on_failure"
    retries = [edge for edge in graph["edges"] if edge["kind"] == "retry"]
    assert retries == [
        {"from": "lane:on_failure", "to": "lint", "kind": "retry", "label": "retry"}
    ]
    assert {
        "from": "lane:pipeline",
        "to": "lane:on_failure",
        "kind": "failure",
        "label": "any step fails",
    } in graph["edges"]
    assert all("position" not in node for node in graph["nodes"])


def test_flow_draws_the_pinned_cartridge_with_nothing_running(client, migration):
    del migration
    page = client.get("/flow").text

    # The cartridge, once, on the bar under the tab. The heading that used to
    # say "Flow /" repeated the tab and is gone.
    assert 'class="flow-bar"' in page
    assert "Flow /" not in page
    assert "stock" in page and "1.0.0" in page
    assert 'id="flow-inspect" hidden' in page
    assert 'aria-label="Close the step"' in page
    assert "Agent. A model turn." in page
    assert "Function. The app runs it." in page
