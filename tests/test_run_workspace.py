"""Runner and Sheller are separate full-width screens. Task 13."""

from datetime import UTC, datetime

import pytest
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
    # The pipeline and its log, and nothing else. Counting panels was the
    # old way of saying this, and the log made the number wrong without
    # making the page wrong.
    assert page.text.count("data-pipeline\n") + page.text.count("data-pipeline ") == 1
    assert "data-run-log-panel" in page.text
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


@pytest.fixture
def live_job(client):
    _row, sale = _prepared(client)
    client.app.state.executor = ProgressingExecutor(client.app)
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)

    return sale


def test_the_pipeline_live_panel_is_a_ribbon_of_the_cartridge(client, live_job):
    del live_job
    page = client.get("/run").text

    assert "data-ribbon" in page
    assert page.count('class="ribbon-node') >= 7


def test_every_ribbon_block_links_to_its_own_step_on_flow(client, live_job):
    """The ribbon said where the run was and never what the step does. Flow
    holds that, and the ids match because both are `step.id or step.kind`."""
    from odoo_migrator.cartridges import pinned_for
    from odoo_migrator.db import database
    from odoo_migrator.models import Migration

    del live_job
    page = client.get("/run").text
    with database.connection_context():
        migration = Migration.select().where(Migration.archived_at.is_null()).first()
        names = [step.id or step.kind for step in pinned_for(migration).pipeline]

    assert names
    for name in names:
        assert f'href="/flow#{name}"' in page


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


def test_the_ribbon_says_how_each_step_went_not_only_where_the_run_is(client, live_job):
    """A highlighted `understand` said "the run is here" and nothing else,
    in a colour that read as a failure. Each block now carries the outcome
    of that step on this lap, and the steps that have not run say so."""
    from odoo_migrator.db import database
    from odoo_migrator.models import Job, StepRun

    with database.connection_context():
        job = Job.select().where(Job.module == live_job["id"]).order_by(Job.id.desc()).first()
        StepRun.create(
            job=job, attempt=job.attempt, step_id="understand", kind="agent",
            ok=True, finished_at=datetime.now(UTC),
        )
        StepRun.create(job=job, attempt=job.attempt, step_id="port", kind="agent")

    page = client.get("/run").text

    assert 'data-step="understand" data-state="done"' in page
    assert 'data-step="port" data-state="running"' in page
    assert 'data-step="verdict" data-state="pending"' in page


def test_a_failed_step_is_a_failed_block(client, live_job):
    from odoo_migrator.db import database
    from odoo_migrator.models import Job, StepRun

    with database.connection_context():
        job = Job.select().where(Job.module == live_job["id"]).order_by(Job.id.desc()).first()
        StepRun.create(
            job=job, attempt=job.attempt, step_id="understand", kind="agent",
            ok=False, finished_at=datetime.now(UTC),
        )

    page = client.get("/run").text

    assert 'data-step="understand" data-state="failed"' in page


def test_the_module_event_carries_the_same_states(client, live_job):
    """The page paints itself from these. Rebuilt from the stage line alone
    they would assume every earlier step passed."""
    from odoo_migrator.db import database
    from odoo_migrator.jobs import ribbon_states
    from odoo_migrator.models import Job, StepRun

    with database.connection_context():
        job = Job.select().where(Job.module == live_job["id"]).order_by(Job.id.desc()).first()
        StepRun.create(
            job=job, attempt=job.attempt, step_id="understand", kind="agent",
            ok=True, finished_at=datetime.now(UTC),
        )
        states = ribbon_states(job)

    assert states[0] == {"id": "understand", "state": "done"}
    assert {node["state"] for node in states[1:]} == {"pending"}


def test_the_pipeline_thread_is_this_module_and_this_run(client, live_job):
    """It was every checkpoint in the migration. Starting a second module put
    the first one's diagnosis under a run that had just begun."""
    from odoo_migrator.db import database
    from odoo_migrator.models import CheckpointMessage, Job, Module

    with database.connection_context():
        live = Job.select().where(Job.module == live_job["id"]).order_by(Job.id.desc()).first()
        migration_id = live.module.migration_id
        older = Module.create(
            migration=migration_id, technical_name="mig_test_base", depends="[]"
        )
        done = Job.create(batch=live.batch, module=older, status="passed")
        CheckpointMessage.create(job=done, author="agent", text="the spec was malformed")
        CheckpointMessage.create(job=live, author="agent", text="which override do we drop?")

    page = client.get("/run").text

    assert "which override do we drop?" in page
    assert "the spec was malformed" not in page


def test_another_job_on_this_module_is_not_the_current_thread(client, live_job):
    """Scoped to the job on screen, not to the module. A module carries every
    conversation it ever had; the panel is showing one run."""
    from odoo_migrator.db import database
    from odoo_migrator.models import CheckpointMessage, Job

    with database.connection_context():
        live = (
            Job.select()
            .where(Job.module == live_job["id"])
            .order_by(Job.id.desc())
            .first()
        )
        CheckpointMessage.create(job=live, author="agent", text="the question this run asked")
        # Newer by id and not in flight, so "the module's newest job" would
        # have picked this one and shown a conversation nobody is having.
        other = Job.create(batch=live.batch, module_id=live_job["id"], status="failed")
        CheckpointMessage.create(job=other, author="agent", text="a diagnosis from another run")

    page = client.get("/run").text

    assert "the question this run asked" in page
    assert "a diagnosis from another run" not in page


def test_the_log_always_follows_the_run_and_has_no_switch_for_it():
    """Nobody watching a run wants the log to stop. A new line scrolls into
    view while the list is at its foot, and is left alone when the reader has
    scrolled up; there is no control for it."""
    from odoo_migrator.paths import static_dir

    script = (static_dir() / "runlog.js").read_text(encoding="utf-8")
    css = (static_dir() / "style.css").read_text(encoding="utf-8")

    assert "data-run-log-follow" not in script
    assert "var stick = atBottom();" in script
    assert ".run-log-follow" not in css


def test_clear_empties_the_card_and_not_only_the_lines():
    """Cleared, the card kept the module and the job id of a log that was no
    longer there."""
    from odoo_migrator.paths import static_dir

    script = (static_dir() / "runlog.js").read_text(encoding="utf-8")
    clear = script.split("data-run-log-clear")[1]

    assert "forget()" in clear
    # And the next line to arrive puts the name back.
    assert "remember();" in script.split("function append")[1][:400]


def test_the_log_says_when_it_is_showing_a_finished_run(client, live_job):
    """The panel above says no module is running while this line names one.
    Without a word saying which, the finished run reads as the current one."""
    from odoo_migrator.db import database
    from odoo_migrator.models import Job, RunLog

    with database.connection_context():
        job = (
            Job.select()
            .where(Job.module == live_job["id"])
            .order_by(Job.id.desc())
            .first()
        )
        RunLog.create(job=job, level="info", source="port", text="agent step started")

    running = client.get("/run").text

    assert "last run" not in running

    with database.connection_context():
        job = Job.get_by_id(job.id)
        job.status = "passed"
        job.save()

    finished = client.get("/run").text

    assert "last run" in finished


def test_a_cleared_log_says_so_rather_than_showing_a_blank_box(client, live_job):
    """A job whose log was cleared and a migration that never ran are
    different things; the panel showed an empty box for both."""
    from odoo_migrator.db import database
    from odoo_migrator.models import Job, RunLog

    page = client.get("/run").text

    assert "log is empty" in page

    with database.connection_context():
        job = (
            Job.select()
            .where(Job.module == live_job["id"])
            .order_by(Job.id.desc())
            .first()
        )
        RunLog.create(job=job, level="info", source="port", text="agent step started")

    page = client.get("/run").text

    assert "log is empty" in page  # rendered, and hidden by the template
    assert "data-run-log-empty hidden" in page


def _attempts(job_module_id):
    """Attempt 1: understand, port, lint pass, upgrade fails, the lane runs.
    Attempt 2: the lane loops back to lint; lint and upgrade pass, tests fail,
    and patch is running."""
    from odoo_migrator.db import database
    from odoo_migrator.models import Job, StepRun

    now = datetime.now(UTC)
    with database.connection_context():
        job = Job.select().where(Job.module == job_module_id).order_by(Job.id.desc()).first()
        for step, ok in (("understand", True), ("port", True), ("lint", True),
                         ("upgrade", False), ("diagnose", True), ("patch", True)):
            StepRun.create(job=job, attempt=1, step_id=step, kind="agent", ok=ok, finished_at=now)
        for step, ok in (("lint", True), ("upgrade", True), ("tests", False), ("diagnose", True)):
            StepRun.create(job=job, attempt=2, step_id=step, kind="agent", ok=ok, finished_at=now)
        StepRun.create(job=job, attempt=2, step_id="patch", kind="agent")
        job.attempt = 2
        job.save()

        return job.job_id


def test_a_step_done_in_an_earlier_attempt_is_not_grey(client, live_job):
    """understand and port ran once, in attempt 1; the retry loops back to
    lint. Grey read as "never ran"."""
    _attempts(live_job["id"])

    page = client.get("/run").text

    assert 'data-step="understand" data-state="earlier"' in page
    assert "done in attempt 1" in page
    assert 'data-step="lint" data-state="done"' in page
    assert 'data-step="tests" data-state="failed"' in page
    assert 'data-step="verdict" data-state="pending"' in page


def test_an_attempt_can_be_read_back_with_its_retry_lane(client, live_job):
    job_id = _attempts(live_job["id"])

    first = client.get(f"/api/jobs/{job_id}/attempts/1").json()
    second = client.get(f"/api/jobs/{job_id}/attempts/2").json()

    assert first["attempt"] == 1 and first["attempts"] == 2
    states = {node["id"]: node["state"] for node in first["steps"]}
    assert states["upgrade"] == "failed"
    assert states["tests"] == "pending"
    assert [node["id"] for node in first["lane"]] == ["diagnose", "patch"]
    assert second["lane"] == [
        {"id": "diagnose", "state": "done"},
        {"id": "patch", "state": "running"},
    ]
    assert client.get(f"/api/jobs/{job_id}/attempts/3").status_code == 404
    assert client.get("/api/jobs/nope/attempts/1").status_code == 404


def test_the_page_offers_every_attempt_of_the_live_run(client, live_job):
    _attempts(live_job["id"])

    page = client.get("/run").text

    assert "data-attempt-picker" in page
    assert 'data-attempt="1"' in page
    assert 'data-attempt="2"' in page
    assert "data-ribbon-lane" in page


def test_a_run_parked_on_a_question_can_be_stopped(client):
    """Stop reaches a parked run now, so the card offers it there too.
    It showed only while the run was `running`: a question nobody meant to
    answer left the module in flight, with no way to end it from here."""
    _row, sale = _prepared(client)
    client.app.state.executor = SessionExecutor(client.app)
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)

    page = client.get("/run").text

    assert f'action="/run/modules/{sale["id"]}/stop"' in page


def test_a_page_opened_while_nothing_runs_holds_the_whole_live_panel(client):
    """The ribbon, Stop and the answer form were drawn only when a run was
    in flight at load. A page opened between two modules got the name and
    stage from the `module` event and an empty card under them: there was
    no ribbon for the event to paint, and no form for the question."""
    _row, _sale = _prepared(client)

    page = client.get("/run").text

    assert "data-ribbon" in page
    assert page.count('class="ribbon-node"') >= 7
    assert "data-pipeline-stop" in page
    assert "data-pipeline-ask" in page


def test_the_open_question_is_shown_once_in_its_answer_form(client):
    """The question is the newest message of the thread and the text of
    the answer form. Shown in both, it read twice down the card: once with
    its lines run together, once as written."""
    _row, sale = _prepared(client)
    client.app.state.executor = SessionExecutor(client.app)
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)

    page = client.get("/run").text

    panel = page.split("data-pipeline-ask", 1)[0].split("data-pipeline", 1)[1]
    assert QUESTION not in panel
    assert page.count(QUESTION) >= 1


def test_an_answered_question_folds_into_one_line(client):
    """Answered, the question stopped being the answer form's text and came
    back as the thread: the agent's whole diagnosis, open down the card, as
    if it were still waiting. It folds to the answer; a click reads it."""
    from test_hitl import AskingExecutor

    _row, sale = _prepared(client)
    client.app.state.executor = AskingExecutor(client.app)
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)
    client.post(
        f"/run/modules/{sale['id']}/answer", data={"answer": "use the new one"},
        follow_redirects=True,
    )

    page = client.get("/run").text

    assert '<details class="checkpoint-past"' in page
    assert "Answered: use the new one" in page
    assert "<details class=\"checkpoint-past\" open" not in page


class StoppableExecutor(SessionExecutor):
    """Parks, and hears a Stop the way the native runner does."""

    def __init__(self, app):
        super().__init__(app)
        self.asked: set[str] = set()

    def stop(self, job_id):
        self.asked.add(job_id)

        return True

    def stopping(self, job_id):

        return job_id in self.asked


def test_stop_on_a_module_no_longer_running_says_so_instead_of_crashing(client):
    """The job ended between the page drawing Stop and the press — or the
    page was reloaded after a Stop, which posts it again. stop_job refused,
    the route did not catch it, and the answer was a 500."""
    _row, sale = _prepared(client)

    page = client.post(f"/run/modules/{sale['id']}/stop", follow_redirects=False)

    assert page.status_code == 200
    assert "That module is not running." in page.text


def test_a_heard_stop_lands_on_run_and_says_it_is_stopping(client):
    """After the press the card looked exactly as before until the model's
    turn ended, which can take minutes, and a reload posted the Stop again."""
    _row, sale = _prepared(client)
    client.app.state.executor = StoppableExecutor(client.app)
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)

    pressed = client.post(f"/run/modules/{sale['id']}/stop", follow_redirects=False)

    assert pressed.status_code == 303
    assert pressed.headers["location"] == "/run"
    page = client.get("/run").text
    stop = page[page.index("data-pipeline-stop") :]
    stop = stop[: stop.index("</form>")]
    assert "Stopping…" in stop
    assert "disabled" in stop


def test_a_log_line_opens_its_body_from_an_eye_not_a_word():
    from odoo_migrator.paths import static_dir

    script = (static_dir() / "runlog.js").read_text(encoding="utf-8")

    assert 'open.textContent = "body"' not in script
    assert "eye()" in script
    assert "Show the body of this line" in script
