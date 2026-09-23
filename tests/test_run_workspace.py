"""Pipeline and Sheller are separate full-width screens. Task 13."""

from test_batch import _prepared
from test_checkpoint import DIFF, FINDING, _failed_checkpoint
from test_hitl import QUESTION, RESUME_URL
from test_tree import _open

SESSION = "sess-new-observer"


class ProgressingExecutor:
    """Leaves the job running after a stage, the way a live executor posts progress."""

    def __init__(self, app):
        self.app = app
        self.job_id = None

    def start(self, payload):
        self.job_id = payload["job_id"]
        self.app.state.on_progress(
            {
                "job_id": payload["job_id"],
                "stage": "rewrite",
                "attempt": 2,
            }
        )


class SessionExecutor:
    """Parks with a live session id. Result is where the contract carries it."""

    def __init__(self, app):
        self.app = app

    def start(self, payload):
        self.app.state.on_result(
            {
                "job_id": payload["job_id"],
                "status": "waiting_for_human",
                "question": QUESTION,
                "resumeUrl": RESUME_URL,
                "current_session_id": SESSION,
            }
        )


def _save_urls(client, *, sheller="http://sheller.test:8765"):
    client.post(
        "/settings",
        data={
            "sheller_base_url": sheller,
            "sheller_image_tag": "",
        },
        follow_redirects=True,
    )


def _iframe_src(html: str) -> str:
    start = html.find("<iframe")
    assert start != -1
    chunk = html[start : html.find(">", start)]

    return chunk


def test_run_page_is_the_pipeline_only(client):
    _open(client)

    page = client.get("/run")

    assert page.status_code == 200
    assert 'class="workspace"' in page.text
    assert page.text.count("workspace-panel") == 1
    assert "data-pipeline" in page.text
    assert "data-observer" not in page.text
    assert "No module is running." in page.text
    assert 'data-screen="pipeline"' in page.text
    assert 'data-screen="sheller"' in page.text


def test_sheller_page_is_the_iframe_without_extra_chrome(client):
    _open(client)
    _save_urls(client)

    page = client.get("/sheller").text

    assert 'class="sheller-embed"' in page
    assert 'class="workspace"' not in page
    assert "No live session yet." not in page
    assert "Observer only" not in page


def test_sheller_iframe_is_observer_web_without_a_write_key_or_session_query(client):
    """Sheller's /web has no session deep-link — only ``?app`` for the desktop
    frame. Observer mode is this browser not holding a write key. Inventing
    ``?session=`` would look like we picked one; Sheller would ignore it."""
    _open(client)
    _save_urls(client)

    page = client.get("/sheller")
    tag = _iframe_src(page.text)

    assert 'title="Sheller observer"' in page.text
    assert 'src="http://sheller.test:8765/web"' in tag
    assert "write_key" not in page.text
    assert "session=" not in tag
    assert "X-OS-Session-Key" not in page.text


def test_pipeline_shows_live_stage_and_attempt(client):
    row, sale = _prepared(client)
    client.app.state.executor = ProgressingExecutor(client.app)

    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)
    page = client.get("/run")

    assert "sale_custom" in page.text
    assert "rewrite" in page.text
    assert "attempt 2" in page.text
    assert f'data-migration-id="{row.id}"' in page.text
    assert f'data-module-id="{sale["id"]}"' in page.text
    assert "data-pipeline-empty hidden" in page.text


def test_sheller_still_does_not_deep_link_or_show_session_id(client):
    _row, sale = _prepared(client)
    _save_urls(client)
    client.app.state.executor = SessionExecutor(client.app)

    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)
    page = client.get("/sheller")
    tag = _iframe_src(page.text)

    assert SESSION not in page.text
    assert 'src="http://sheller.test:8765/web"' in tag
    assert "session=" not in tag


def test_progress_sse_carries_what_the_pipeline_needs(client):
    row, sale = _prepared(client)
    client.app.state.executor = ProgressingExecutor(client.app)

    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)
    payloads = [event[1] for event in client.app.state.hub.published if event[0] == "module"]
    last = payloads[-1]

    assert last["id"] == sale["id"]
    assert last["technical_name"] == "sale_custom"
    assert last["stage"] == "rewrite"
    assert last["attempt"] == 2
    assert last["migration_id"] == str(row.id)
    assert "current_session_id" in last


def test_sse_client_patches_the_pipeline_without_replacing_the_page(client):
    html = client.get("/run").text
    script = client.get("/static/sse.js").text

    assert "/static/sse.js" in html
    assert "[data-pipeline]" in script
    assert "data-migration-id" in script
    assert "technical_name" in script
    assert "current_session_id" in script
    assert "location.reload" not in script
    assert "document.body.innerHTML" not in script


def test_checkpoint_renders_on_the_pipeline_screen(client):
    _failed_checkpoint(client)

    page = client.get("/run")

    assert "data-pipeline" in page.text
    assert 'class="checkpoint"' in page.text
    assert 'class="checkpoint"' not in client.get("/sheller").text
    assert FINDING in page.text
    assert DIFF in page.text
    assert ">Continue<" in page.text


def test_styleguide_shows_the_run_workspace(client):
    html = client.get("/components").text

    assert 'class="workspace"' in html
    assert "data-pipeline" in html
    assert 'title="Sheller observer"' in html
    assert "rewrite · attempt 2" in html


def test_the_end_of_a_batch_reaches_the_open_run_screen(client):
    """The run screen exists to be watched. Nothing was published when a
    batch closed, so the pipeline panel went quiet and the review the human
    was waiting for appeared only if they thought to reload."""
    from test_batch import DeferredExecutor, _group, _named, _prepared

    row, sale = _prepared(client)
    stock = _named(row.id, "stock_custom")
    _group(client, sale, stock)
    executor = DeferredExecutor(client.app)
    client.app.state.executor = executor
    client.post("/tree/batch/run", data={"batch_group": "1"}, follow_redirects=True)
    executor.answer("failed")

    published = [event for event in client.app.state.hub.published if event[0] == "checkpoint"]

    assert published
    assert published[-1][1]["migration_id"] == row.id
    page = client.get("/run").text
    assert "data-checkpoint-ready" in page
    script = client.get("/static/sse.js").text
    assert 'addEventListener("checkpoint"' in script


def test_a_settings_url_that_is_not_http_never_becomes_a_frame_src(client):
    """The operator sets these, so this is a typo surface rather than an
    attack surface — but a javascript: iframe src runs on our origin."""
    from test_tree import _open

    _open(client)
    client.post(
        "/settings",
        data={
            "sheller_base_url": "javascript:alert(2)",
            "sheller_image_tag": "",
        },
        follow_redirects=True,
    )

    pipeline = client.get("/run").text
    sheller = client.get("/sheller").text

    assert "javascript:alert" not in pipeline
    assert "javascript:alert" not in sheller
    # An empty src is not an empty frame — the browser resolves it to the
    # current document and loads this page inside itself.
    assert 'src=""' not in sheller
    assert "Set the Sheller base URL in settings" in sheller
