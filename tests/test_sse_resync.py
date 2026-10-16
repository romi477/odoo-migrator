"""What a page is told when its stream (re)opens, and what it is sent at all.

The app restarts on every edit during development. EventSource reconnects
on its own, but whatever happened while it was down was lost: the hello
carried the lamp and the running count, and the pipeline card, the queue
and the statuses on Tree and Result stayed as they were until a reload.
"""

import json
import queue
import re

from odoo_migrator import api
from odoo_migrator.db import database
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Batch, Job, Migration, Module


def _migration(tmp_path):
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


def _events(text: str) -> list[tuple[str, dict]]:
    found = []
    for event, data in re.findall(r"event: (\w+)\ndata: (.*)\n", text):
        found.append((event, json.loads(data)))

    return found


def _short(monkeypatch):
    monkeypatch.setattr(api, "SSE_HEARTBEAT_SECONDS", 0.02)
    monkeypatch.setattr(api, "SSE_MAX_SECONDS", 0.05)


def test_the_hello_tells_a_page_what_its_migration_looks_like_now(client, tmp_path, monkeypatch):
    _short(monkeypatch)
    created = _migration(tmp_path)
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])
        Module.create(migration=migration, technical_name="base_custom", status="passed")
        Module.create(migration=migration, technical_name="sale_custom", status="queued")

    events = dict(_events(client.get(f"/sse?migration={created['id']}").text))

    assert events["queue"]["migration_id"] == created["id"]
    assert events["pipeline_idle"]["migration_id"] == created["id"]
    states = {row["technical_name"]: row["status"] for row in events["module_states"]["modules"]}
    assert states == {"base_custom": "passed", "sale_custom": "queued"}


def test_the_hello_repaints_a_run_that_is_still_going(client, tmp_path, monkeypatch):
    _short(monkeypatch)
    created = _migration(tmp_path)
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])
        module = Module.create(migration=migration, technical_name="sale_custom", status="running")
        batch = Batch.create(migration=migration, status="running")
        job = Job.create(batch=batch, module=module, status="running", current_stage="port", attempt=1)

    events = _events(client.get(f"/sse?migration={created['id']}").text)

    live = [data for event, data in events if event == "module"]
    assert live and live[0]["job_id"] == job.job_id
    assert live[0]["status"] == "running"
    assert "pipeline_idle" not in dict(events)


def test_a_stream_without_a_migration_says_only_what_the_nav_bar_needs(client, monkeypatch):
    _short(monkeypatch)

    names = [event for event, _data in _events(client.get("/sse").text)]

    assert sorted(names) == ["lamp", "running"]


def test_only_what_a_page_listens_to_goes_down_the_stream(client, monkeypatch):
    """Step, tool and job events are for the runner's own record and the
    tests. Sent to every open page, each tool call was one more message to
    every tab, with nothing listening for it."""
    _short(monkeypatch)
    hub = client.app.state.hub
    primed: queue.Queue = queue.Queue()
    primed.put(("tool_called", {"job_id": "x"}))
    primed.put(("step_started", {"job_id": "x"}))
    primed.put(("lamp", {"state": "idle"}))
    monkeypatch.setattr(hub, "subscribe", lambda: primed)

    text = client.get("/sse").text

    assert "tool_called" not in text
    assert "step_started" not in text
    assert text.count("event: lamp") == 2
