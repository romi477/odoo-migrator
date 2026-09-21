"""The checkpoint diff is what the rewrite wrote, not what the cartridge sent.

The agent writes through the File API. Until this task nobody compared
before and after, so the block that exists to show what happened to the
module stayed empty on a live run. The cartridge may still send a diff;
an empty one is filled from the writes.
"""

import uuid

from test_batch import _named, _prepared

from odoo_migrator.db import database
from odoo_migrator.models import Attempt, Job


class Hold:
    def start(self, payload):
        self.job_id = payload["job_id"]


def _running(client, sale):
    holder = Hold()
    client.app.state.executor = holder
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)

    return holder


def test_a_new_side_write_fills_an_empty_attempt_diff(client):
    row, sale = _prepared(client)
    holder = _running(client, sale)
    marker = f"x = {uuid.uuid4().hex}\n"
    client.put(
        f"/api/migrations/{row.id}/files",
        json={
            "side": "new",
            "path": "sale_custom/models.py",
            "content": marker,
        },
    )
    client.app.state.on_result(
        {
            "job_id": holder.job_id,
            "status": "failed",
            "attempts": [{"iteration": 1, "verdict": "fail", "diff": ""}],
        }
    )

    with database.connection_context():
        attempt = Attempt.select().join(Job).where(Job.job_id == holder.job_id).get()
    assert f"+{marker.rstrip()}" in attempt.diff
    assert "sale_custom/models.py" in attempt.diff
    assert marker.rstrip() in client.get("/run").text


def test_a_cartridge_diff_is_kept_when_it_sent_one(client):
    row, sale = _prepared(client)
    holder = _running(client, sale)
    client.put(
        f"/api/migrations/{row.id}/files",
        json={
            "side": "new",
            "path": "sale_custom/models.py",
            "content": f"x = {uuid.uuid4().hex}\n",
        },
    )
    client.app.state.on_result(
        {
            "job_id": holder.job_id,
            "status": "failed",
            "attempts": [
                {
                    "iteration": 1,
                    "verdict": "fail",
                    "diff": "- mapping = old\n+ mapping = new",
                }
            ],
        }
    )

    with database.connection_context():
        attempt = Attempt.select().join(Job).where(Job.job_id == holder.job_id).get()
    assert attempt.diff == "- mapping = old\n+ mapping = new"


def test_an_old_side_write_is_not_the_module_diff(client):
    row, sale = _prepared(client)
    holder = _running(client, sale)
    client.put(
        f"/api/migrations/{row.id}/files",
        json={
            "side": "old",
            "path": "sale_custom/models.py",
            "content": "should_not_appear\n",
        },
    )
    client.app.state.on_result(
        {
            "job_id": holder.job_id,
            "status": "failed",
            "attempts": [{"iteration": 1, "verdict": "fail", "diff": ""}],
        }
    )

    with database.connection_context():
        attempt = Attempt.select().join(Job).where(Job.job_id == holder.job_id).get()
    assert "should_not_appear" not in attempt.diff


def test_a_write_to_another_module_stays_off_this_job(client):
    row, sale = _prepared(client)
    _named(row.id, "stock_custom")
    holder = _running(client, sale)
    client.put(
        f"/api/migrations/{row.id}/files",
        json={
            "side": "new",
            "path": "stock_custom/__init__.py",
            "content": "from . import models\n",
        },
    )
    client.app.state.on_result(
        {
            "job_id": holder.job_id,
            "status": "failed",
            "attempts": [{"iteration": 1, "verdict": "fail", "diff": ""}],
        }
    )

    with database.connection_context():
        attempt = Attempt.select().join(Job).where(Job.job_id == holder.job_id).get()
    assert "stock_custom" not in attempt.diff


def test_writes_after_a_later_progress_land_on_that_attempt(client):
    row, sale = _prepared(client)
    holder = _running(client, sale)
    first = f"first = {uuid.uuid4().hex}\n"
    second = f"second = {uuid.uuid4().hex}\n"
    client.put(
        f"/api/migrations/{row.id}/files",
        json={
            "side": "new",
            "path": "sale_custom/first.py",
            "content": first,
        },
    )
    client.app.state.on_progress(
        {"job_id": holder.job_id, "stage": "rewrite", "attempt": 2}
    )
    client.put(
        f"/api/migrations/{row.id}/files",
        json={
            "side": "new",
            "path": "sale_custom/second.py",
            "content": second,
        },
    )
    client.app.state.on_result(
        {
            "job_id": holder.job_id,
            "status": "failed",
            "attempts": [
                {"iteration": 1, "verdict": "retry", "diff": ""},
                {"iteration": 2, "verdict": "fail", "diff": ""},
            ],
        }
    )

    with database.connection_context():
        rows = list(
            Attempt.select()
            .join(Job)
            .where(Job.job_id == holder.job_id)
            .order_by(Attempt.iteration)
        )
    assert first.rstrip() in rows[0].diff
    assert second.rstrip() not in rows[0].diff
    assert second.rstrip() in rows[1].diff
    assert first.rstrip() not in rows[1].diff


def test_a_module_that_passes_first_time_still_shows_its_diff(client):
    """The stock canvas appended an attempt when it retried or failed and
    none when it passed, and the app only filled diffs into attempts the
    cartridge had sent. So the outcome most worth reading — the rewrite
    that worked — recorded its diff and showed it to nobody."""
    row, sale = _prepared(client)
    holder = _running(client, sale)
    marker = f"x = {uuid.uuid4().hex}\n"
    client.put(
        f"/api/migrations/{row.id}/files",
        json={"side": "new", "path": "sale_custom/models.py", "content": marker},
    )

    client.app.state.on_result(
        {"job_id": holder.job_id, "status": "passed", "attempts": []}
    )

    with database.connection_context():
        stored = list(Attempt.select())
    assert len(stored) == 1
    assert stored[0].verdict == "passed"
    assert marker.strip() in stored[0].diff
    assert "sale_custom/models.py" in client.get("/run").text


def test_the_stock_canvas_logs_the_attempt_that_passed():
    import json
    from pathlib import Path

    from odoo_migrator.n8n.stock import PARENT

    parent = json.loads(
        (Path("n8n/stock") / f"{PARENT}.json").read_text(encoding="utf-8")
    )
    node = next(one for one in parent["nodes"] if one["name"] == "Mark passed")

    assert node["type"] == "n8n-nodes-base.code"
    assert "attempts" in node["parameters"]["jsCode"]
    assert "'passed'" in node["parameters"]["jsCode"]


def test_a_module_nested_under_addons_is_still_attributed(client):
    """A prefix test only matched a module sitting at the root of the new
    project. addons/<module>/... is an ordinary layout, and its diff was
    dropped with nothing said."""
    row, sale = _prepared(client)
    holder = _running(client, sale)
    marker = f"y = {uuid.uuid4().hex}\n"
    client.put(
        f"/api/migrations/{row.id}/files",
        json={
            "side": "new",
            "path": "addons/sale_custom/models/sale.py",
            "content": marker,
        },
    )

    client.app.state.on_result(
        {"job_id": holder.job_id, "status": "passed", "attempts": []}
    )

    with database.connection_context():
        stored = Attempt.select().get()
    assert marker.strip() in stored.diff
