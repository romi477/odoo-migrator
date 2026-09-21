"""n8n compose, stock deterministic cartridge, janitor. Task 14.

The suite never talks to Docker or to a live n8n. The fake answers the way
2.39.9 answered the spike: /healthz is 200 while starting, /rest/settings
JSON is ready, and a parent whose Execute Workflow points at an unpublished
child is 400.
"""

import json
from pathlib import Path

import pytest
import yaml
from conftest import API_KEY
from fastapi.testclient import TestClient
from test_batch import _prepared

from odoo_migrator.authkey import API_KEY_HEADER
from odoo_migrator.db import database, init_db
from odoo_migrator.executor import StubExecutor
from odoo_migrator.jobs import apply_result, reconcile_jobs
from odoo_migrator.models import Job, Module
from odoo_migrator.provision import FakeComposeEngine
from odoo_migrator.settings import save_app_settings
from odoo_migrator.sheller import FakeShellerClient, ShellerNotOwner

REPO = Path(__file__).resolve().parents[1]
COMPOSE = REPO / "n8n" / "compose.yml"
STOCK = REPO / "n8n" / "stock"
PINNED = "docker.n8n.io/n8nio/n8n:2.39.9"
CHILDREN = (
    "sheller-close-session",
    "sheller-open-session",
    "sheller-upgrade-module",
    "sheller-run-tests",
    "progress",
    "callback-result",
)
PARENT = "migrate-one-module"


def test_compose_pins_2_39_9_and_does_not_use_latest():
    text = COMPOSE.read_text(encoding="utf-8")
    doc = yaml.safe_load(text)
    image = doc["services"]["n8n"]["image"]

    assert image == PINNED
    assert ":latest" not in text
    assert "sandbox-runner" not in text


def test_compose_sets_n8n_external_url_from_env_not_a_baked_host():
    text = COMPOSE.read_text(encoding="utf-8")
    env = yaml.safe_load(text)["services"]["n8n"]["environment"]
    webhook = env.get("N8N_WEBHOOK_URL") or env.get("WEBHOOK_URL")

    assert webhook
    assert "${" in str(webhook)
    assert "127.0.0.1" not in str(webhook)
    assert "localhost:5678" not in str(webhook)


def test_compose_reaches_the_host_gateway_and_keeps_workflows():
    service = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["services"]["n8n"]
    volumes = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))["volumes"]

    assert "host.docker.internal:host-gateway" in service.get("extra_hosts", [])
    assert service.get("volumes")
    assert "n8n_data" in volumes


def test_stock_cartridge_is_deterministic_and_talks_to_the_app_proxy():
    parent_doc = json.loads((STOCK / f"{PARENT}.json").read_text(encoding="utf-8"))
    types = [node["type"] for node in parent_doc["nodes"]]

    assert "n8n-nodes-base.webhook" in types
    assert all("agent" not in node_type.lower() for node_type in types)
    assert parent_doc["settings"].get("errorWorkflow")
    refs = []
    for node in parent_doc["nodes"]:
        if node["type"] == "n8n-nodes-base.executeWorkflow":
            wid = node["parameters"]["workflowId"]
            refs.append(wid["value"] if isinstance(wid, dict) else wid)
    for name in (
        "sheller-open-session",
        "sheller-upgrade-module",
        "sheller-run-tests",
        "sheller-close-session",
        "callback-result",
    ):
        assert any(name in str(ref) for ref in refs), name
    blob = (STOCK / f"{PARENT}.json").read_text(encoding="utf-8")
    for child in CHILDREN:
        blob += (STOCK / f"{child}.json").read_text(encoding="utf-8")
    assert "127.0.0.1:8765" not in blob
    assert "$json.sheller_base_url" in blob
    assert "X-Odoo-Migrator-Key" in blob
    callback = json.loads((STOCK / "callback-result.json").read_text(encoding="utf-8"))
    http_nodes = [node for node in callback["nodes"] if "httpRequest" in node["type"]]
    assert any(node.get("retryOnFail") is True for node in http_nodes)
    assert any((node.get("maxTries") or 0) >= 2 for node in http_nodes)


def test_wait_until_ready_ignores_healthz_and_wants_rest_settings_json():
    from odoo_migrator.n8n.client import wait_until_ready

    hits = []

    def fetch():
        hits.append("settings")
        if len(hits) == 1:
            raise ValueError("n8n is starting up. Please wait")

        return {"ok": True}

    wait_until_ready(fetch, interval=0.01, timeout=1)

    assert hits == ["settings", "settings"]


def test_a_parent_does_not_activate_until_every_child_is_published():
    from odoo_migrator.n8n.fake import FakeN8nClient
    from odoo_migrator.n8n.stock import publish_stock

    n8n = FakeN8nClient()
    n8n.ready = True
    n8n.refuse_child = "sheller-run-tests"

    with pytest.raises(Exception, match="not published|not active"):
        publish_stock(n8n)

    parent = next(
        workflow for workflow in n8n.workflows.values() if workflow["name"] == PARENT
    )
    assert parent["id"] not in n8n.active


def test_publish_activates_children_before_the_parent_and_stores_the_production_path():
    from odoo_migrator.n8n.fake import FakeN8nClient
    from odoo_migrator.n8n.stock import WEBHOOK_PATH, publish_stock

    n8n = FakeN8nClient()
    n8n.ready = True
    result = publish_stock(n8n)

    parent_at = n8n.activated.index(result["parent_id"])
    for name in CHILDREN:
        child_id = next(
            wid for wid, workflow in n8n.workflows.items() if workflow["name"] == name
        )
        assert n8n.activated.index(child_id) < parent_at
    assert set(CHILDREN).issubset({n8n.workflows[wid]["name"] for wid in n8n.active})
    assert result["webhook_path"] == WEBHOOK_PATH
    assert WEBHOOK_PATH in n8n.webhooks


def test_n8n_executor_posts_the_production_webhook_and_the_stub_stays_the_fallback(
    tmp_path, compose, fake_sheller
):
    from odoo_migrator.api import create_app
    from odoo_migrator.n8n.executor import N8nExecutor
    from odoo_migrator.n8n.fake import FakeN8nClient
    from odoo_migrator.n8n.stock import publish_stock

    app = create_app(
        tmp_path / "stub.sqlite",
        compose=compose,
        sheller=fake_sheller,
        api_key=API_KEY,
    )
    assert isinstance(app.state.executor, StubExecutor)

    n8n = FakeN8nClient()
    n8n.ready = True
    publish_stock(n8n)
    init_db(tmp_path / "n8n.sqlite")
    save_app_settings(
        n8n_base_url="http://n8n.example:5678",
        n8n_api_key="n8n_test_key",
        app_base_url="http://host.docker.internal:8000",
        sheller_base_url="http://127.0.0.1:8765",
        sheller_image_tag="",
        min_sheller_version="",
    )
    keyed = create_app(
        tmp_path / "n8n.sqlite",
        compose=FakeComposeEngine(),
        sheller=FakeShellerClient(),
        api_key=API_KEY,
        n8n=n8n,
    )
    assert isinstance(keyed.state.executor, N8nExecutor)

    client = TestClient(keyed, headers={API_KEY_HEADER: API_KEY})
    _row, sale = _prepared(client)
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)

    assert n8n.triggers
    assert n8n.triggers[-1]["path"].endswith("migrate-module")
    assert n8n.triggers[-1]["payload"]["module"]["technical_name"] == "sale_custom"


def test_a_result_that_arrives_after_reconcile_is_not_lost(client):
    _row, sale = _prepared(client)
    client.app.state.executor = type(
        "Hold",
        (),
        {"start": lambda self, payload: setattr(self, "job_id", payload["job_id"])},
    )()
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)
    job_id = client.app.state.executor.job_id
    reconcile_jobs()
    apply_result({"job_id": job_id, "status": "passed"}, client.app.state.hub)

    with database.connection_context():
        assert Job.select().get().status == "passed"
        assert Module.get_by_id(sale["id"]).status == "passed"


def test_janitor_closes_a_stranded_session_with_the_key_the_proxy_saw(
    tmp_path, compose, fake_sheller
):
    """Measured against the daemon on 8765: closing with an empty key is 403
    not_owner even for an agent session we had just opened, and a session
    whose process dies is announced and leaves the registry — so there is no
    corpse in a listing to sweep instead. The key n8n was handed is the only
    thing that closes one, and it passes through our proxy on the way out.
    """
    from test_sheller_client import _stub_app

    from odoo_migrator.api import create_app
    from odoo_migrator.n8n.janitor import sweep_orphaned_sessions

    stub = TestClient(_stub_app(fake_sheller))
    app = create_app(
        tmp_path / "j.sqlite",
        compose=compose,
        sheller=fake_sheller,
        sheller_http=stub,
        api_key=API_KEY,
    )
    client = TestClient(app, headers={API_KEY_HEADER: API_KEY})
    row, sale = _prepared(client)
    app.state.executor = type("Hold", (), {"start": lambda self, payload: None})()
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)

    # n8n opens through the proxy, the only route it has to the daemon.
    opened = client.post(
        "/api/sessions",
        json={
            "container": "odoo-new",
            "database": "acme_17",
            "odoo_bin": "/odoo-bin",
            "owner": {"kind": "agent", "label": "n8n", "migration_id": row.id},
            "allow_commit": True,
        },
    ).json()
    human = fake_sheller.open_session(
        container="odoo-new",
        database="acme_17",
        odoo_bin="/odoo-bin",
        owner={"kind": "human", "label": "operator"},
        allow_commit=True,
    )
    reconcile_jobs()

    closed = sweep_orphaned_sessions(fake_sheller)

    assert closed == [opened["id"]]
    ids = {session["id"] for session in fake_sheller.list_sessions()}
    assert opened["id"] not in ids
    assert human["id"] in ids


def test_the_janitor_leaves_a_session_whose_job_is_still_alive(
    tmp_path, compose, fake_sheller
):
    from test_sheller_client import _stub_app

    from odoo_migrator.api import create_app
    from odoo_migrator.n8n.janitor import sweep_orphaned_sessions

    stub = TestClient(_stub_app(fake_sheller))
    app = create_app(
        tmp_path / "alive.sqlite",
        compose=compose,
        sheller=fake_sheller,
        sheller_http=stub,
        api_key=API_KEY,
    )
    client = TestClient(app, headers={API_KEY_HEADER: API_KEY})
    row, sale = _prepared(client)
    app.state.executor = type("Hold", (), {"start": lambda self, payload: None})()
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)
    opened = client.post(
        "/api/sessions",
        json={
            "container": "odoo-new",
            "database": "acme_17",
            "odoo_bin": "/odoo-bin",
            "owner": {"kind": "agent", "migration_id": row.id},
        },
    ).json()

    # No reconcile: the job is still running, so nothing has been given up on.
    assert sweep_orphaned_sessions(fake_sheller) == []
    assert opened["id"] in {session["id"] for session in fake_sheller.list_sessions()}


def test_the_proxy_keeps_the_key_only_while_the_session_lives(
    tmp_path, compose, fake_sheller
):
    from test_sheller_client import _stub_app

    from odoo_migrator.api import create_app
    from odoo_migrator.models import ShellerSession

    stub = TestClient(_stub_app(fake_sheller))
    app = create_app(
        tmp_path / "k.sqlite",
        compose=compose,
        sheller=fake_sheller,
        sheller_http=stub,
        api_key=API_KEY,
    )
    client = TestClient(app, headers={API_KEY_HEADER: API_KEY})

    opened = client.post(
        "/api/sessions",
        json={
            "container": "odoo-new",
            "database": "acme_17",
            "odoo_bin": "/odoo-bin",
            "owner": {"kind": "agent", "migration_id": "m1"},
        },
    ).json()

    with database.connection_context():
        stored = ShellerSession.get(ShellerSession.session_id == opened["id"])
    assert stored.write_key == opened["write_key"]
    assert stored.migration_id == "m1"

    client.request(
        "DELETE",
        f"/api/sessions/{opened['id']}",
        headers={"X-OS-Session-Key": opened["write_key"]},
    )

    with database.connection_context():
        assert (
            ShellerSession.get_or_none(ShellerSession.session_id == opened["id"])
            is None
        )


def test_a_live_session_still_refuses_a_keyless_close(fake_sheller):
    """The fake let a session marked "dead" close without a key. The daemon
    refuses that too — and never lists a dead session in the first place."""
    opened = fake_sheller.open_session(
        container="c", database="db", odoo_bin="/odoo-bin", owner={"kind": "agent"}
    )

    with pytest.raises(ShellerNotOwner):
        fake_sheller.close(opened["id"], write_key="")

    assert fake_sheller.list_sessions()


def test_compose_states_the_env_access_the_cartridge_depends_on():
    """Every node that calls the app reads $env.MIGRATOR_API_KEY. n8n's docs
    give N8N_BLOCK_ENV_ACCESS_IN_NODE as false and its v20 breaking changes
    flip it to true; on the wrong side of that every header goes out empty
    and the app answers 403 to the whole cartridge."""
    compose = yaml.safe_load((Path("n8n/compose.yml")).read_text(encoding="utf-8"))
    environment = compose["services"]["n8n"]["environment"]

    assert environment["N8N_BLOCK_ENV_ACCESS_IN_NODE"] == "false"

    keyed = [
        path
        for path in sorted(Path("n8n/stock").glob("*.json"))
        if "$env.MIGRATOR_API_KEY" in path.read_text(encoding="utf-8")
    ]
    assert keyed, "no stock workflow presents the app key"


def test_a_trigger_the_executor_refuses_leaves_nothing_running(client):
    """n8n down was a 500 and a traceback, and the module stayed running for
    ever: Migrate answered "already being migrated", a new batch answered
    "already running", and only a restart cleared it."""
    from odoo_migrator.models import Batch

    _row, sale = _prepared(client)

    class Refusing:
        def start(self, payload):
            raise RuntimeError("n8n did not answer")

    client.app.state.executor = Refusing()

    page = client.post(
        "/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True
    )

    assert "The executor did not take the job" in page.text
    with database.connection_context():
        assert Job.select().count() == 0
        assert Batch.select().count() == 0
        assert Module.get_by_id(sale["id"]).status == "queued"
