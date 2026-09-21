"""Cartridges, smoke-run, model wrappers. Task 16.

The suite never talks to Docker or to a live n8n. Task 3b already
established the packages endpoint: multipart, not JSON (415), and a
missing archive is 400 rather than a missing route.
"""

import json
from pathlib import Path

import pytest
from conftest import API_KEY
from fastapi.testclient import TestClient
from test_batch import _group, _named, _prepared

from odoo_migrator.authkey import API_KEY_HEADER
from odoo_migrator.contract import CONTRACT_VERSION, ContractError
from odoo_migrator.db import database
from odoo_migrator.models import Batch, CartridgeInstall, Job, Migration
from odoo_migrator.provision import FakeComposeEngine
from odoo_migrator.sheller import FakeShellerClient

REPO = Path(__file__).resolve().parents[1]
STOCK = REPO / "n8n" / "stock"
WRAPPERS = ("migrate-module-openai", "migrate-module-claude")


def test_stock_manifest_speaks_this_contract():
    from odoo_migrator.cartridges import load_manifest

    manifest = load_manifest(STOCK / "manifest.json")

    assert manifest.id == "stock"
    assert manifest.contract_version == CONTRACT_VERSION
    assert manifest.webhook_path == "migrate-module"
    assert set(WRAPPERS) <= set(manifest.wrappers.values())


def test_a_cartridge_on_an_unknown_contract_is_refused():
    from odoo_migrator.cartridges import load_manifest

    path = STOCK / "manifest.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw["contract_version"] = "999"

    with pytest.raises(ContractError, match="contract_version"):
        load_manifest(raw)


def test_model_wrappers_are_thin_and_call_the_shared_loop():
    parent = json.loads((STOCK / "migrate-one-module.json").read_text(encoding="utf-8"))
    types = [node["type"] for node in parent["nodes"]]

    assert "n8n-nodes-base.executeWorkflowTrigger" in types
    for name in WRAPPERS:
        doc = json.loads((STOCK / f"{name}.json").read_text(encoding="utf-8"))
        node_types = [node["type"] for node in doc["nodes"]]
        assert "n8n-nodes-base.webhook" in node_types
        assert "n8n-nodes-base.executeWorkflow" in node_types
        assert all("langchain.agent" not in t for t in node_types)
        refs = []
        for node in doc["nodes"]:
            if node["type"] != "n8n-nodes-base.executeWorkflow":
                continue
            wid = node["parameters"]["workflowId"]
            refs.append(wid["value"] if isinstance(wid, dict) else wid)
        assert any("migrate-one-module" in str(ref) for ref in refs)


def test_publish_activates_wrappers_after_the_shared_parent():
    from odoo_migrator.n8n.fake import FakeN8nClient
    from odoo_migrator.n8n.stock import publish_stock

    n8n = FakeN8nClient()
    n8n.ready = True
    result = publish_stock(n8n)
    names = {n8n.workflows[wid]["name"] for wid in n8n.active}
    parent_at = n8n.activated.index(result["parent_id"])

    assert set(WRAPPERS).issubset(names)
    for name in WRAPPERS:
        wrapper_id = next(
            wid for wid, wf in n8n.workflows.items() if wf["name"] == name
        )
        assert n8n.activated.index(wrapper_id) > parent_at
    assert "migrate-module-openai" in n8n.webhooks
    assert "migrate-module-claude" in n8n.webhooks


def test_package_import_is_multipart_the_way_2_39_9_answered(tmp_path):
    from odoo_migrator.n8n.client import HttpN8nClient, N8nError

    captured = []

    class _Http:
        def request(self, method, path, **kwargs):
            captured.append({"method": method, "path": path, **kwargs})

            class _Resp:
                status_code = 200
                content = b"{}"
                text = "{}"
                reason_phrase = "OK"

                def json(self):

                    return {"ok": True}

            return _Resp()

    HttpN8nClient("http://n8n.example", api_key="k", http=_Http()).import_package(
        b"PK fake archive", filename="stock.n8np"
    )

    call = captured[-1]
    assert call["method"] == "POST"
    assert call["path"] == "/api/v1/n8n-packages/import"
    assert "files" in call
    assert "json" not in call
    with pytest.raises(N8nError, match="archive|400"):
        from odoo_migrator.n8n.fake import FakeN8nClient

        fake = FakeN8nClient()
        fake.import_package(b"not-a-zip")


def test_a_failed_smoke_run_blocks_a_batch(client):
    from odoo_migrator.cartridges import smoke_run
    from odoo_migrator.jobs import start_batch

    row, sale = _prepared(client)
    stock = _named(row.id, "stock_custom")
    _group(client, sale, stock)
    with database.connection_context():
        CartridgeInstall.create(
            cartridge_id="stock",
            version="1.0.0",
            contract_version=CONTRACT_VERSION,
            webhook_path="migrate-module",
            smoke_status="untested",
        )

    class Fail:
        def start(self, payload):
            client.app.state.on_result({"job_id": payload["job_id"], "status": "failed"})

    smoke_run(row.id, Fail(), client.app.state.hub)

    with database.connection_context():
        assert CartridgeInstall.get().smoke_status == "failed"
    with pytest.raises(ContractError, match="smoke"):
        start_batch(
            migration_id=row.id,
            batch_group=1,
            executor=Fail(),
            hub=client.app.state.hub,
        )


def test_swapping_the_cartridge_does_not_rewrite_past_jobs(client):
    _row, sale = _prepared(client)
    client.app.state.executor = type(
        "Hold", (), {"start": lambda self, payload: None}
    )()
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)
    with database.connection_context():
        job = Job.select().get()
        assert job.cartridge_id == "stock"
        assert job.cartridge_version == "1.0.0"
        Migration.update(cartridge_id="other", cartridge_version="9.9.9").execute()
        job = Job.get_by_id(job.id)
        batch = Batch.get_by_id(job.batch_id)

    assert job.cartridge_id == "stock"
    assert job.cartridge_version == "1.0.0"
    assert batch.cartridge_id == "stock"


def test_the_executor_fires_the_wrapper_webhook_pinned_on_the_batch(
    tmp_path, compose, fake_sheller
):
    from odoo_migrator.api import create_app
    from odoo_migrator.n8n.executor import N8nExecutor
    from odoo_migrator.n8n.fake import FakeN8nClient
    from odoo_migrator.n8n.stock import publish_stock
    from odoo_migrator.settings import save_app_settings

    n8n = FakeN8nClient()
    n8n.ready = True
    publish_stock(n8n)
    init_path = tmp_path / "wrap.sqlite"
    from odoo_migrator.db import init_db

    init_db(init_path)
    save_app_settings(
        n8n_base_url="http://n8n.example:5678",
        n8n_api_key="k",
        app_base_url="http://host.docker.internal:8000",
        sheller_base_url="http://127.0.0.1:8765",
        sheller_image_tag="",
        min_sheller_version="",
    )
    app = create_app(
        init_path,
        compose=FakeComposeEngine(),
        sheller=FakeShellerClient(),
        api_key=API_KEY,
        n8n=n8n,
        executor=N8nExecutor(n8n),
    )
    client = TestClient(app, headers={API_KEY_HEADER: API_KEY})
    row, sale = _prepared(client)
    with database.connection_context():
        Migration.update(
            cartridge_id="stock",
            cartridge_version="1.0.0",
            model_webhook_path="migrate-module-claude",
        ).where(Migration.id == row.id).execute()

    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)

    assert n8n.triggers[-1]["path"] == "migrate-module-claude"


def test_migration_settings_pin_the_cartridge_and_the_wrapper(client):
    from test_migrations import _create

    _create(client, "Acme")
    with database.connection_context():
        row = Migration.get(Migration.name == "Acme")
    client.post(f"/migrations/{row.id}/open", follow_redirects=True)
    with database.connection_context():
        CartridgeInstall.create(
            cartridge_id="stock",
            version="1.0.0",
            contract_version=CONTRACT_VERSION,
            webhook_path="migrate-module",
            smoke_status="passed",
        )
    page = client.post(
        "/settings/migration",
        data={
            "strictness_default": "basic",
            "retry_limit": "5",
            "ask_human_mode": "sync",
            "cartridge_id": "stock",
            "cartridge_version": "1.0.0",
            "model_webhook_path": "migrate-module-openai",
        },
        follow_redirects=True,
    )

    assert page.status_code == 200
    with database.connection_context():
        stored = Migration.get_by_id(row.id)
    assert stored.cartridge_id == "stock"
    assert stored.model_webhook_path == "migrate-module-openai"
    assert 'name="cartridge_id"' in client.get("/settings/migration").text
    assert "smoke" in client.get("/styleguide").text.lower()


class DeferredExecutor:
    """Takes the trigger and answers later, which is all n8n ever does."""

    def __init__(self, app):
        self.app = app
        self.pending = []
        self.started = []

    def start(self, payload):
        self.started.append(payload["module"]["technical_name"])
        self.pending.append(payload["job_id"])

    def answer(self, status="passed"):
        self.app.state.on_result({"job_id": self.pending.pop(0), "status": status})


def test_a_smoke_run_waits_for_its_result_before_judging(client):
    """It used to read the job status straight after handing the trigger
    over — "running" until a callback arrives — and write that down as
    failed. Against any real executor every smoke-run failed, and since a
    batch needs a passed one, every batch was blocked for ever."""
    from odoo_migrator.cartridges import list_installs

    _row, sale = _prepared(client)
    _group(client, sale)
    executor = DeferredExecutor(client.app)
    client.app.state.executor = executor

    page = client.post("/settings/migration/smoke", follow_redirects=True)

    assert "Smoke-run failed" not in page.text
    assert [install["smoke_status"] for install in list_installs()] == ["running"]
    blocked = client.post(
        "/tree/batch/run", data={"batch_group": "1"}, follow_redirects=True
    )
    assert "A smoke-run must pass before a batch." in blocked.text

    executor.answer("passed")

    assert [install["smoke_status"] for install in list_installs()] == ["passed"]
    with database.connection_context():
        # The smoke batch is not a review: it must not block the real one.
        assert [batch.status for batch in Batch.select()] == ["smoke"]
    allowed = client.post(
        "/tree/batch/run", data={"batch_group": "1"}, follow_redirects=True
    )
    assert "A smoke-run must pass before a batch." not in allowed.text


def test_a_smoke_run_that_fails_late_still_blocks_the_batch(client):
    from odoo_migrator.cartridges import list_installs

    _row, sale = _prepared(client)
    _group(client, sale)
    executor = DeferredExecutor(client.app)
    client.app.state.executor = executor
    client.post("/settings/migration/smoke", follow_redirects=True)

    executor.answer("failed")

    assert [install["smoke_status"] for install in list_installs()] == ["failed"]
    blocked = client.post(
        "/tree/batch/run", data={"batch_group": "1"}, follow_redirects=True
    )
    assert "A smoke-run must pass before a batch." in blocked.text


def test_the_picker_pins_a_version_not_just_a_name(client):
    """The option posted the id alone next to a hidden field holding the
    version already pinned, so choosing a second version of the same
    cartridge — or a different cartridge — kept the old number."""
    from odoo_migrator.cartridges import CartridgeManifest, record_install

    _prepared(client)
    record_install(
        CartridgeManifest(
            id="stock",
            version="1.0.0",
            contract_version=CONTRACT_VERSION,
            webhook_path="migrate-module",
        )
    )
    record_install(
        CartridgeManifest(
            id="stock",
            version="1.1.0",
            contract_version=CONTRACT_VERSION,
            webhook_path="migrate-module",
        )
    )
    page = client.get("/settings/migration").text

    assert 'value="stock@1.1.0"' in page

    client.post(
        "/settings/migration",
        data={
            "strictness_default": "basic",
            "retry_limit": "5",
            "ask_human_mode": "sync",
            "cartridge_id": "stock@1.1.0",
            "model_webhook_path": "migrate-module",
        },
        follow_redirects=True,
    )

    with database.connection_context():
        pinned = Migration.select().where(Migration.archived_at.is_null()).get()
    assert (pinned.cartridge_id, pinned.cartridge_version) == ("stock", "1.1.0")


def test_a_pin_that_matches_no_install_does_not_skip_the_gate(client):
    """An unknown pin used to be the one case that ran unsmoked."""
    from odoo_migrator.cartridges import (
        CartridgeManifest,
        record_install,
        smoke_allows_batch,
    )

    row, _sale = _prepared(client)
    record_install(
        CartridgeManifest(
            id="stock",
            version="1.0.0",
            contract_version=CONTRACT_VERSION,
            webhook_path="migrate-module",
        )
    )
    with database.connection_context():
        migration = Migration.get_by_id(row.id)
        migration.cartridge_id = "nobody"
        migration.cartridge_version = "9.9.9"
        migration.save()

        assert smoke_allows_batch(migration) is False


def test_the_manifest_only_claims_what_the_stock_canvas_does():
    """A capability list is data the app validates cartridges against. It
    claimed `strictness`, and no node on the canvas reads
    `strictness_level` — the field travels and nothing branches on it."""
    import json as _json
    from pathlib import Path as _Path

    manifest = _json.loads((_Path("n8n/stock/manifest.json")).read_text(encoding="utf-8"))
    canvas = "".join(
        path.read_text(encoding="utf-8") for path in _Path("n8n/stock").glob("*.json")
    )

    for capability in manifest["capabilities"]:
        assert capability in canvas, f"{capability} is declared and unused"
