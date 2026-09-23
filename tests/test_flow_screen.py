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

    assert [n["id"] for n in graph["nodes"]][:3] == ["understand", "port", "lint"]
    assert {"from": "patch", "to": "lint", "kind": "retry"} in graph["edges"]
    assert all("position" not in node for node in graph["nodes"])


def test_flow_draws_the_pinned_cartridge_with_nothing_running(client, migration):
    del migration
    page = client.get("/flow").text

    assert "stock" in page and "1.0.0" in page
