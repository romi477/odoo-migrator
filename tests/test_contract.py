"""Webhook contract, stub executor, SSE row patches. Locked against the Task 3b spike."""

import threading
import time
from urllib.parse import urlparse

import pytest
from conftest import API_KEY
from fastapi.testclient import TestClient
from test_tree import _open

from odoo_migrator.api import create_app
from odoo_migrator.db import database
from odoo_migrator.models import Attempt, Finding, Job, Migration, Module
from odoo_migrator.provision import FakeComposeEngine
from odoo_migrator.sheller import FakeShellerClient

# Copied from docs_/n8n-spike-findings.md. The contract is locked against
# this JSON, not against the spec's camel_snake guess.
SPIKE_PROGRESS = {
    "job_id": "13fe80ab-fab3-4420-9757-47dbd4f87850",
    "stage": "understand",
    "attempt": 1,
    "resumeUrl": (
        "http://127.0.0.1:5679/webhook-waiting/1"
        "?signature=6afb719692c2466a203a511d362157e8f963b46c99332b42d3fa5b03c4aa57e4"
    ),
}
SPIKE_RESULT = {
    "job_id": "13fe80ab-fab3-4420-9757-47dbd4f87850",
    "status": "passed",
    "answer": "use the new tax mapping",
}

APP_BASE = "http://host.docker.internal:8000"
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def _reachable_settings(client):
    client.post(
        "/settings",
        data={
            "n8n_base_url": "http://n8n.example:5678",
            "n8n_api_key": "",
            "app_base_url": APP_BASE,
            "sheller_base_url": "http://127.0.0.1:8765",
            "sheller_image_tag": "",
            "min_sheller_version": "",
        },
        follow_redirects=True,
    )


def _sale_custom(migration_id):
    from odoo_migrator.graph import list_modules

    return next(m for m in list_modules(migration_id) if m["technical_name"] == "sale_custom")


def _prepared(client):
    row = _open(client)
    _reachable_settings(client)
    from odoo_migrator.migrations import save_side_container

    save_side_container(row.id, "old", "odoo-old")
    save_side_container(row.id, "new", "odoo-new")
    client.post("/tree/resolve", follow_redirects=True)

    return row, _sale_custom(row.id)


class RecordingExecutor:
    def __init__(self):
        self.payloads = []

    def start(self, payload):
        self.payloads.append(payload)


def test_progress_parses_the_spike_resumeUrl():
    from odoo_migrator.contract import ProgressPayload

    parsed = ProgressPayload.model_validate(SPIKE_PROGRESS)

    assert parsed.job_id == SPIKE_PROGRESS["job_id"]
    assert parsed.stage == "understand"
    assert parsed.attempt == 1
    assert parsed.resume_webhook_url == SPIKE_PROGRESS["resumeUrl"]
    assert "?signature=" in parsed.resume_webhook_url


def test_result_parses_the_spike_callback_including_answer():
    from odoo_migrator.contract import ResultPayload

    parsed = ResultPayload.model_validate(SPIKE_RESULT)

    assert parsed.job_id == SPIKE_RESULT["job_id"]
    assert parsed.status == "passed"
    assert parsed.findings == []


def test_trigger_urls_are_app_base_url_never_loopback(tmp_path):
    from odoo_migrator.contract import LoopbackInPayload, build_trigger
    from odoo_migrator.db import init_db
    from odoo_migrator.migrations import create_migration
    from odoo_migrator.models import Module as ModuleModel

    init_db(tmp_path / "payload.sqlite")
    created = create_migration(
        name="Acme",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path="/tmp/old",
        new_project_path="/tmp/new",
    )
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])
        module = ModuleModel.create(
            migration=migration,
            technical_name="sale_custom",
            old_path="/tmp/old/sale_custom",
            new_path="/tmp/new/sale_custom",
        )
        job_id = "job-no-loopback"

    payload = build_trigger(
        migration_id=created["id"],
        job_id=job_id,
        module_id=module.id,
        app_base_url=APP_BASE,
    )

    assert payload["contract_version"] == "1"
    assert payload["sheller_base_url"] == APP_BASE
    assert payload["callback_url"] == f"{APP_BASE}/api/jobs/{job_id}/result"
    assert payload["progress_url"] == f"{APP_BASE}/api/jobs/{job_id}/progress"
    # The daemon stays in AppSettings. Putting it here is the Linux-only bug
    # Docker Desktop hides: host.docker.internal forwards onto loopback.
    assert "8765" not in payload["sheller_base_url"]
    assert "/api/sheller" not in payload["sheller_base_url"]
    for key in ("sheller_base_url", "callback_url", "progress_url"):
        host = urlparse(payload[key]).hostname
        assert host not in LOOPBACK_HOSTS
    assert [entry["role"] for entry in payload["rules"]] == ["source", "target"]
    assert payload["system_prompt"]
    assert payload["cartridge"] == {"id": "stock", "version": "1.0.0"}
    assert "rules_hash" not in payload

    with pytest.raises(LoopbackInPayload):
        build_trigger(
            migration_id=created["id"],
            job_id=job_id,
            module_id=module.id,
            app_base_url="http://127.0.0.1:8000",
        )


def test_migrate_one_module_queued_running_passed_with_stub(client):
    _row, custom = _prepared(client)

    page = client.post("/tree/migrate", data={"module_id": custom["id"]}, follow_redirects=True)

    assert page.status_code == 200
    with database.connection_context():
        job = Job.select().join(Module).where(Module.id == custom["id"]).get()
        module = Module.get_by_id(custom["id"])
        statuses = [event[1]["status"] for event in client.app.state.hub.published if event[0] == "module"]

    assert job.status == "passed"
    assert job.rules_hash
    assert module.status == "passed"
    compacted = [
        status for i, status in enumerate(statuses) if i == 0 or status != statuses[i - 1]
    ]
    assert compacted == ["queued", "running", "passed"]
    assert "form-error" not in page.text


def test_progress_persists_resume_url_from_the_first_callback_including_signature(client):
    _row, custom = _prepared(client)
    recording = RecordingExecutor()
    client.app.state.executor = recording
    client.post("/tree/migrate", data={"module_id": custom["id"]}, follow_redirects=True)
    with database.connection_context():
        job = Job.select().join(Module).where(Module.id == custom["id"]).get()
        job_id = job.job_id

    first = (
        "http://n8n.example:5678/webhook-waiting/1"
        "?signature=6afb719692c2466a203a511d362157e8f963b46c99332b42d3fa5b03c4aa57e4"
    )
    posted = client.post(
        f"/api/jobs/{job_id}/progress",
        json={
            "job_id": job_id,
            "stage": "understand",
            "attempt": 1,
            "resumeUrl": first,
        },
    )

    assert posted.status_code == 200
    later = client.post(
        f"/api/jobs/{job_id}/progress",
        json={
            "job_id": job_id,
            "stage": "rewrite",
            "attempt": 1,
            "resumeUrl": "http://n8n.example:5678/webhook-waiting/2?signature=other",
        },
    )

    assert later.status_code == 200
    with database.connection_context():
        stored = Job.get(Job.job_id == job_id)
        module = Module.get_by_id(custom["id"])

    assert stored.resume_webhook_url == first
    assert "?signature=" in stored.resume_webhook_url
    assert stored.current_stage == "rewrite"
    assert stored.attempt == 1
    assert module.status == "running"


def test_result_stores_findings_and_attempts(client):
    _row, custom = _prepared(client)
    client.app.state.executor = RecordingExecutor()
    client.post("/tree/migrate", data={"module_id": custom["id"]}, follow_redirects=True)
    with database.connection_context():
        job_id = Job.select().join(Module).where(Module.id == custom["id"]).get().job_id

    posted = client.post(
        f"/api/jobs/{job_id}/result",
        json={
            "job_id": job_id,
            "status": "passed",
            "current_session_id": "sess-new",
            "attempts": [
                {
                    "iteration": 1,
                    "diff": "--- a\n+++ b\n",
                    "test_output": "ok",
                    "verdict": "pass",
                }
            ],
            "findings": [{"text": "tax mapping moved", "source": "understand"}],
        },
    )

    assert posted.status_code == 200
    with database.connection_context():
        job = Job.get(Job.job_id == job_id)
        module = Module.get_by_id(custom["id"])
        findings = list(Finding.select().where(Finding.job == job))
        attempts = list(Attempt.select().where(Attempt.job == job))

    assert job.status == "passed"
    assert job.current_session_id == "sess-new"
    assert module.status == "passed"
    assert findings[0].text == "tax mapping moved"
    assert findings[0].source == "understand"
    assert attempts[0].iteration == 1
    assert attempts[0].verdict == "pass"


def test_machine_facing_callbacks_require_the_api_key(tmp_path, compose, fake_sheller):
    app = create_app(
        tmp_path / "key.sqlite",
        compose=compose,
        sheller=fake_sheller,
        api_key=API_KEY,
    )
    bare = TestClient(app)

    progress = bare.post("/api/jobs/missing/progress", json=SPIKE_PROGRESS)
    result = bare.post("/api/jobs/missing/result", json=SPIKE_RESULT)

    assert progress.status_code == 403
    assert progress.json() == {"error": "bad_api_key"}
    assert result.status_code == 403
    assert result.json() == {"error": "bad_api_key"}


def test_startup_marks_non_terminal_jobs_unknown(tmp_path, compose, fake_sheller):
    db = tmp_path / "recon.sqlite"
    create_app(db, compose=compose, sheller=fake_sheller, api_key=API_KEY)
    from odoo_migrator.migrations import create_migration
    from odoo_migrator.models import Batch

    created = create_migration(
        name="Acme",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path="/tmp/old",
        new_project_path="/tmp/new",
    )
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])
        module = Module.create(
            migration=migration, technical_name="sale_custom", status="running"
        )
        batch = Batch.create(migration=migration, status="running")
        Job.create(batch=batch, module=module, status="running", current_stage="rewrite")

    create_app(db, compose=compose, sheller=fake_sheller, api_key=API_KEY)

    with database.connection_context():
        assert Job.select().get().status == "unknown"
        assert Module.select().get().status == "unknown"


def test_thirty_module_rows_update_over_one_sse_connection(client, monkeypatch):
    from odoo_migrator import api as api_mod

    monkeypatch.setattr(api_mod, "SSE_HEARTBEAT_SECONDS", 0.05)
    monkeypatch.setattr(api_mod, "SSE_MAX_SECONDS", 0.8)
    row, _custom = _prepared(client)
    with database.connection_context():
        migration = Migration.get_by_id(row.id)
        ids = [
            Module.create(migration=migration, technical_name=f"mod_{index}").id
            for index in range(30)
        ]

    def emit():
        time.sleep(0.15)
        for module_id in ids:
            client.app.state.hub.publish(
                "module",
                {
                    "id": module_id,
                    "status": "running",
                    "stage": "understand",
                    "attempt": 1,
                },
            )

    threading.Thread(target=emit, daemon=True).start()
    response = client.get("/sse")

    assert response.status_code == 200
    assert response.text.count("event: module") == 30


def test_sse_client_patches_named_events_and_never_replaces_the_page(client):
    html = client.get("/tree").text
    js = client.get("/static/sse.js")

    assert "/static/sse.js" in html
    assert js.status_code == 200
    assert 'addEventListener("module"' in js.text
    assert 'addEventListener("lamp"' in js.text
    assert 'addEventListener("running"' in js.text
    assert "location.reload" not in js.text
    assert "hx-swap" not in js.text
    assert "document.body.innerHTML" not in js.text


def test_migrate_button_visible_text_is_the_verb(client):
    row = _open(client)
    html = client.get("/tree").text

    assert ">Migrate<" in html
    assert 'aria-label="Migrate the selected module"' in html
    assert row.id


def test_loopback_app_base_url_is_loud_on_migrate(client):
    _row, custom = _prepared(client)
    client.post(
        "/settings",
        data={
            "n8n_base_url": "",
            "n8n_api_key": "",
            "app_base_url": "http://127.0.0.1:8000",
            "sheller_base_url": "http://127.0.0.1:8765",
            "sheller_image_tag": "",
            "min_sheller_version": "",
        },
        follow_redirects=True,
    )

    page = client.post("/tree/migrate", data={"module_id": custom["id"]}, follow_redirects=True)

    assert page.status_code == 200
    assert "form-error" in page.text
    assert "loopback" in page.text.lower() or "127.0.0.1" in page.text
    with database.connection_context():
        assert Job.select().count() == 0


def test_tree_rows_carry_module_id_for_sse_patches(client):
    _row, custom = _prepared(client)
    html = client.get("/tree").text

    assert f'data-module-id="{custom["id"]}"' in html
    assert "badge queued" in html


def test_styleguide_shows_a_patchable_module_row(client):
    html = client.get("/styleguide").text

    assert "data-module-id" in html
    assert ">Migrate<" in html


def test_executor_is_injected_so_tests_do_not_need_n8n(tmp_path):
    recording = RecordingExecutor()
    app = create_app(
        tmp_path / "exec.sqlite",
        compose=FakeComposeEngine(),
        sheller=FakeShellerClient(),
        api_key=API_KEY,
        executor=recording,
    )

    assert app.state.executor is recording


def test_a_late_progress_does_not_revive_a_finished_job(client):
    """The stub finishes the job; n8n then redelivers a progress it had
    queued. That used to set the job — and the module — back to running,
    with nothing left to ever send a result."""
    _row, module = _prepared(client)
    client.post("/tree/migrate", data={"module_id": str(module["id"])})
    with database.connection_context():
        job = Job.get(Job.module == module["id"])

    assert job.status == "passed"

    late = client.post(
        f"/api/jobs/{job.job_id}/progress",
        json={"job_id": job.job_id, "stage": "rewrite", "attempt": 1},
    )

    assert late.status_code == 200
    with database.connection_context():
        assert Job.get(Job.job_id == job.job_id).status == "passed"
        assert Module.get_by_id(module["id"]).status == "passed"


def test_a_redelivered_result_does_not_duplicate_its_rows(client):
    _row, module = _prepared(client)
    client.post("/tree/migrate", data={"module_id": str(module["id"])})
    with database.connection_context():
        job = Job.get(Job.module == module["id"])
        before = Attempt.select().where(Attempt.job == job).count()

    again = client.post(
        f"/api/jobs/{job.job_id}/result",
        json={
            "job_id": job.job_id,
            "status": "passed",
            "attempts": [{"iteration": 1, "verdict": "pass"}],
            "findings": [{"text": "again", "source": "verdict"}],
        },
    )

    assert again.status_code == 200
    with database.connection_context():
        assert Attempt.select().where(Attempt.job == job).count() == before
        assert Finding.select().where(Finding.job == job).count() == 0


def test_a_cartridge_cannot_invent_a_status_or_a_stage(client):
    """The batch runner reads module status to decide whether to stop, so a
    status it does not know reads as "not failed" and marches on."""
    _row, module = _prepared(client)
    client.post("/tree/migrate", data={"module_id": str(module["id"])})
    with database.connection_context():
        job = Job.get(Job.module == module["id"])

    junk_status = client.post(
        f"/api/jobs/{job.job_id}/result",
        json={"job_id": job.job_id, "status": "banana"},
    )
    junk_stage = client.post(
        f"/api/jobs/{job.job_id}/progress",
        json={"job_id": job.job_id, "stage": "<script>", "attempt": 1},
    )
    zero_attempt = client.post(
        f"/api/jobs/{job.job_id}/progress",
        json={"job_id": job.job_id, "stage": "rewrite", "attempt": 0},
    )

    assert junk_status.status_code == 422
    assert junk_stage.status_code == 422
    assert zero_attempt.status_code == 422
    with database.connection_context():
        assert Module.get_by_id(module["id"]).status == "passed"


def test_migrating_a_module_twice_is_refused_while_it_is_in_flight(client):
    """Two presses meant two executors rewriting the same files, and the one
    that answered last decided the module's fate."""
    _row, module = _prepared(client)
    hub = client.app.state.hub
    parked = _ParkedExecutor()
    client.app.state.executor = parked

    first = client.post(
        "/tree/migrate", data={"module_id": str(module["id"])}, follow_redirects=True
    )
    second = client.post(
        "/tree/migrate", data={"module_id": str(module["id"])}, follow_redirects=True
    )

    assert "already being migrated" not in first.text
    assert len(parked.payloads) == 1
    assert "already being migrated" in second.text
    with database.connection_context():
        assert Job.select().where(Job.module == module["id"]).count() == 1
    assert hub is client.app.state.hub


def test_a_struck_module_is_not_migrated(client):
    _row, module = _prepared(client)
    client.post(f"/tree/modules/{module['id']}/strike", data={"struck": "1"})

    refused = client.post(
        "/tree/migrate", data={"module_id": str(module["id"])}, follow_redirects=True
    )

    assert "struck out" in refused.text
    with database.connection_context():
        assert Job.select().where(Job.module == module["id"]).count() == 0


def test_a_selection_of_several_says_so_instead_of_migrating_one(client):
    _prepared(client)

    many = client.post(
        "/tree/migrate", data={"module_id": "", "selected": "5"}, follow_redirects=True
    )
    none = client.post(
        "/tree/migrate", data={"module_id": "", "selected": "0"}, follow_redirects=True
    )

    assert "Select one module" in many.text
    assert "Select a module to migrate." in none.text
    with database.connection_context():
        assert Job.select().count() == 0


def test_the_event_ring_does_not_grow_without_end(client):
    """It was a list appended to for the lifetime of the process, and nothing
    ever read an old entry."""
    from odoo_migrator.events import RECENT, EventHub

    hub = EventHub()
    for index in range(RECENT + 50):
        hub.publish("module", {"id": index})

    assert len(hub.published) == RECENT
    assert hub.published[-1] == ("module", {"id": RECENT + 49})


class _ParkedExecutor:
    """Takes the trigger and never calls back — a job that is still running."""

    def __init__(self):
        self.payloads = []

    def start(self, payload: dict) -> None:
        self.payloads.append(payload)
