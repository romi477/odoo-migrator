"""Pipeline queue: add steps, dedupe, run/stop."""

from test_batch import _prepared
from test_tree import _open

from odoo_migrator.db import database
from odoo_migrator.models import PipelineQueueItem
from odoo_migrator.pipeline_queue import add_step, queue_snapshot, run_state


def test_add_step_dedupes_and_lists_modules(client):
    row, _sale = _prepared(client)

    first = client.post("/run/queue/add", data={"step": "1"}, follow_redirects=True)
    again = client.post("/run/queue/add", data={"step": "1"}, follow_redirects=True)

    assert first.status_code == 200
    assert again.status_code == 200
    with database.connection_context():
        assert PipelineQueueItem.select().where(PipelineQueueItem.migration == row.id).count() == 1
    snap = queue_snapshot(row.id)
    assert snap["run_state"] == "stopped"
    assert len(snap["modules"]) >= 1


def test_queue_run_page_has_selection_and_queue(client):
    _prepared(client)

    page = client.get("/run")

    assert ">Selection<" in page.text
    assert ">Queue<" in page.text
    assert "FIFO · First in, first out" in page.text
    assert ">Add to queue<" in page.text
    assert 'aria-label="Run the pipeline queue"' in page.text


def test_queue_modules_sort_by_migration_rank_not_add_order(client):
    row, _sale = _prepared(client)
    client.post("/run/queue/add", data={"step": "3"}, follow_redirects=True)
    client.post("/run/queue/add", data={"step": "1"}, follow_redirects=True)

    snap = queue_snapshot(row.id)
    ranks = [module["migration_rank"] for module in snap["modules"]]

    assert ranks == sorted(ranks)
    assert snap["modules"][0]["position"] == 1
    assert snap["modules"][0]["migration_rank"] == min(ranks)


def test_queue_run_sets_running(client):
    row, _sale = _prepared(client)
    client.post("/run/queue/add", data={"step": "1"}, follow_redirects=True)
    client.app.state.executor = type(
        "Hold", (), {"start": lambda self, payload: None}
    )()

    page = client.post("/run/queue/run", follow_redirects=True)

    assert page.status_code == 200
    assert run_state(row.id) == "running"
