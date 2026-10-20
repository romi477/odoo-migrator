"""The lamp: amber while a module waits for a person, red while a failure is
unread, off once Dismiss has read it."""

import re

from test_checkpoint import _failed_checkpoint, parked_job  # noqa: F401

from odoo_migrator.db import database
from odoo_migrator.jobs import attention_target, lamp_state
from odoo_migrator.models import Job, Module


def _dismiss(client):
    with database.connection_context():
        job = Job.get()
    client.post(f"/run/jobs/{job.id}/dismiss")

    return job


def test_an_unread_failure_flashes_the_lamp_red(client):
    _failed_checkpoint(client)

    page = client.get("/").text

    assert lamp_state() == "failed"
    assert 'class="attention-lamp failed"' in page
    assert "A module has failed. Open it." in page


def test_dismiss_turns_the_lamp_off_and_says_so(client, monkeypatch):
    _failed_checkpoint(client)
    heard = []
    publish = client.app.state.hub.publish
    monkeypatch.setattr(
        client.app.state.hub, "publish", lambda name, data=None, *a, **k: (heard.append((name, data)), publish(name, data, *a, **k))[1]
    )

    _dismiss(client)

    assert lamp_state() == "idle"
    assert ("lamp", {"state": "idle"}) in heard
    assert 'class="attention-lamp idle"' in client.get("/").text
    with database.connection_context():
        assert Module.select().where(Module.status == "failed").exists()


def test_a_dismissed_failure_is_not_where_the_lamp_leads(client):
    _failed_checkpoint(client)
    assert attention_target() is not None

    _dismiss(client)

    assert attention_target() is None


def test_a_waiting_module_stays_amber_over_a_failure(client, parked_job):  # noqa: F811
    with database.connection_context():
        migration = Module.get().migration_id
        Module.create(migration=migration, technical_name="stock_custom", status="failed")

    assert lamp_state() == "needs-attention"


def test_the_failed_lamp_is_red_and_flashes(client):
    css = client.get("/static/style.css").text
    failed = css.split(".attention-lamp.failed .lamp-dot {")[1].split("}")[0]

    assert "background: var(--red)" in failed
    assert "animation: attention-lamp" in failed
    assert "  .attention-lamp.needs-attention .lamp-dot,\n  .attention-lamp.failed .lamp-dot," in css
    script = client.get("/static/sse.js").text
    assert re.search(r'"failed"|\'failed\'|=== "failed"', script)
