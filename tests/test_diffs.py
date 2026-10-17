"""The checkpoint diff is what the rewrite wrote, not what the cartridge sent.

Writes are noted on the in-flight job. Until this task nobody compared
before and after, so the block that exists to show what happened to the
module stayed empty on a live run. The cartridge may still send a diff;
an empty one is filled from the writes.
"""

import uuid

from test_batch import _named, _prepared

from odoo_migrator.db import database
from odoo_migrator.diffs import note_new_write
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
    note_new_write(str(row.id), "sale_custom/models.py", "", marker)
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
    note_new_write(str(row.id), "sale_custom/models.py", "", f"x = {uuid.uuid4().hex}\n")
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


def test_a_write_to_another_module_stays_off_this_job(client):
    row, sale = _prepared(client)
    _named(row.id, "stock_custom")
    holder = _running(client, sale)
    note_new_write(str(row.id), "stock_custom/__init__.py", "", "from . import models\n")
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
    note_new_write(str(row.id), "sale_custom/first.py", "", first)
    client.app.state.on_progress(
        {"job_id": holder.job_id, "stage": "rewrite", "attempt": 2}
    )
    note_new_write(str(row.id), "sale_custom/second.py", "", second)
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
    """A cartridge that reports no attempt still rewrote the module. The
    outcome most worth reading — the rewrite that worked — must show its diff."""
    row, sale = _prepared(client)
    holder = _running(client, sale)
    marker = f"x = {uuid.uuid4().hex}\n"
    note_new_write(str(row.id), "sale_custom/models.py", "", marker)

    client.app.state.on_result(
        {"job_id": holder.job_id, "status": "passed", "attempts": []}
    )

    with database.connection_context():
        stored = list(Attempt.select())
    assert len(stored) == 1
    assert stored[0].verdict == "passed"
    assert marker.strip() in stored[0].diff
    assert "sale_custom/models.py" in client.get("/run").text


def test_a_module_nested_under_addons_is_still_attributed(client):
    """A prefix test only matched a module sitting at the root of the new
    project. addons/<module>/... is an ordinary layout, and its diff was
    dropped with nothing said."""
    row, sale = _prepared(client)
    holder = _running(client, sale)
    marker = f"y = {uuid.uuid4().hex}\n"
    note_new_write(str(row.id), "addons/sale_custom/models/sale.py", "", marker)

    client.app.state.on_result(
        {"job_id": holder.job_id, "status": "passed", "attempts": []}
    )

    with database.connection_context():
        stored = Attempt.select().get()
    assert marker.strip() in stored.diff


def test_an_earlier_attempt_of_a_passed_job_is_not_recorded_as_passed(client):
    """The runner reports no attempts, so the rows come from what was written.
    A lap that was followed by another failed; only the last one has the job's
    outcome."""
    row, sale = _prepared(client)
    holder = _running(client, sale)
    note_new_write(str(row.id), "sale_custom/first.py", "", f"a = {uuid.uuid4().hex}\n")
    client.app.state.on_progress({"job_id": holder.job_id, "stage": "lint", "attempt": 2})
    note_new_write(str(row.id), "sale_custom/second.py", "", f"b = {uuid.uuid4().hex}\n")

    client.app.state.on_result({"job_id": holder.job_id, "status": "passed", "attempts": []})

    with database.connection_context():
        rows = list(
            Attempt.select().join(Job).where(Job.job_id == holder.job_id).order_by(Attempt.iteration)
        )
    assert [(one.iteration, one.verdict) for one in rows] == [(1, "failed"), (2, "passed")]


def test_the_card_holds_an_older_attempt_until_the_reader_follows(client):
    """Reading attempt 1 while the next module starts used to swap the card, and
    read as the run starting over. The page says so instead, and Follow goes on."""
    static = client.get("/static/sse.js").text
    attempts = client.get("/static/attempts.js").text
    components = client.get("/components").text

    assert 'pipe.hasAttribute("data-reading")' in static
    assert "card-held" in static and "card-follow" in static
    assert "data-attempt-follow" in attempts
    assert "attempt-moved" in components
