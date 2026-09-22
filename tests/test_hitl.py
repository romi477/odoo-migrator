"""HITL: module notes, wait/resume, batch_defer assumptions. Task 11."""

from test_batch import _group, _named, _prepared

from odoo_migrator.db import database
from odoo_migrator.executor import StubExecutor
from odoo_migrator.jobs import reconcile_jobs
from odoo_migrator.models import Assumption, Batch, Job, Module

RESUME_URL = (
    "http://n8n.example:5678/webhook-waiting/1"
    "?signature=6afb719692c2466a203a511d362157e8f963b46c99332b42d3fa5b03c4aa57e4"
)
QUESTION = "which tax mapping?"
ANSWER = "use the new tax mapping"


class RecordingResume:
    """The suite does not POST at n8n. A real resume is that POST."""

    def __init__(self):
        self.calls = []
        self.error = None

    def resume(self, url, answer):
        if self.error is not None:
            raise self.error
        self.calls.append({"url": url, "answer": answer})


class CaptureExecutor:
    """Records the trigger. A real executor cannot be inspected after the fact."""

    def __init__(self, app):
        self.app = app
        self.payloads = []

    def start(self, payload):
        self.payloads.append(payload)
        self.app.state.on_result({"job_id": payload["job_id"], "status": "passed"})


class AskingExecutor:
    """Parks the first start with a question, the way n8n's Wait node does.

    Later starts (the rest of the batch) pass. Continuations of the parked
    job are the app's own callback, not a second ``start``.
    """

    def __init__(self, app):
        self.app = app
        self.started = []

    def start(self, payload):
        self.started.append(payload["module"]["technical_name"])
        if len(self.started) == 1:
            self.app.state.on_result(
                {
                    "job_id": payload["job_id"],
                    "status": "waiting_for_human",
                    "question": QUESTION,
                    "resumeUrl": RESUME_URL,
                }
            )

            return
        self.app.state.on_result({"job_id": payload["job_id"], "status": "passed"})


class AssumptionExecutor:
    """A green result that still carried an assumption — batch_defer's shape."""

    def __init__(self, app):
        self.app = app

    def start(self, payload):
        self.app.state.on_result(
            {
                "job_id": payload["job_id"],
                "status": "passed",
                "assumptions": [{"question": QUESTION, "assumption": ANSWER}],
            }
        )


def _park(client, resume=None):
    row, sale = _prepared(client)
    stock = _named(row.id, "stock_custom")
    _group(client, sale, stock)
    if resume is not None:
        client.app.state.resume = resume
    client.app.state.executor = AskingExecutor(client.app)
    client.post("/tree/batch/run", data={"batch_group": "1"}, follow_redirects=True)

    return row, sale, stock


def test_saved_notes_are_injected_as_module_notes(client):
    _row, sale = _prepared(client)
    capture = CaptureExecutor(client.app)
    client.app.state.executor = capture

    saved = client.post(
        f"/tree/modules/{sale['id']}/notes",
        data={"notes": ANSWER},
        follow_redirects=True,
    )

    assert saved.status_code == 200
    assert ANSWER in saved.text
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)

    assert capture.payloads[0]["module_notes"] == ANSWER


def test_notes_survive_a_restart(tmp_path, compose, fake_sheller):
    from conftest import API_KEY
    from fastapi.testclient import TestClient

    from odoo_migrator.api import create_app
    from odoo_migrator.authkey import API_KEY_HEADER

    db = tmp_path / "notes.sqlite"
    first = create_app(
        db, compose=compose, sheller=fake_sheller, api_key=API_KEY
    )
    client = TestClient(first, headers={API_KEY_HEADER: API_KEY})
    _row, sale = _prepared(client)
    client.post(f"/tree/modules/{sale['id']}/notes", data={"notes": ANSWER})

    second = create_app(
        db, compose=compose, sheller=fake_sheller, api_key=API_KEY
    )
    again = TestClient(second, headers={API_KEY_HEADER: API_KEY})
    again.post(f"/migrations/{_row.id}/open", follow_redirects=True)

    assert ANSWER in again.get("/tree").text
    with database.connection_context():
        assert Module.get_by_id(sale["id"]).notes == ANSWER


def test_the_stub_can_emit_waiting_for_human_and_a_resume_url():
    results = []
    stub = StubExecutor(
        progress=lambda _body: None,
        result=results.append,
        ask=QUESTION,
    )

    stub.start({"job_id": "job-1", "module": {"technical_name": "sale_custom"}})

    assert results[-1]["status"] == "waiting_for_human"
    assert results[-1]["question"] == QUESTION
    assert results[-1]["resumeUrl"] == RESUME_URL
    assert "?signature=" in results[-1]["resumeUrl"]


def test_waiting_for_human_parks_the_batch_until_a_human_answers(client):
    row, sale, stock = _park(client)

    page = client.get("/tree")
    with database.connection_context():
        batch = Batch.select().where(Batch.migration == row.id).get()
        job = Job.select().join(Module).where(Module.id == sale["id"]).get()
        stock_row = Module.get_by_id(stock["id"])

    assert QUESTION in page.text
    assert ">Answer<" in page.text
    assert 'aria-label="Answer sale_custom"' in page.text
    assert batch.status == "running"
    assert job.status == "waiting_for_human"
    assert job.resume_webhook_url == RESUME_URL
    assert stock_row.status == "queued"
    assert client.app.state.executor.started == ["sale_custom"]


def test_answering_posts_the_spike_body_to_the_resume_url_and_the_job_continues(
    client,
):
    resume = RecordingResume()
    row, sale, stock = _park(client, resume)

    answered = client.post(
        f"/tree/modules/{sale['id']}/answer",
        data={"answer": ANSWER},
        follow_redirects=True,
    )

    assert answered.status_code == 200
    assert resume.calls == [{"url": RESUME_URL, "answer": ANSWER}]
    with database.connection_context():
        job = Job.select().join(Module).where(Module.id == sale["id"]).get()
        module = Module.get_by_id(sale["id"])
        batch = Batch.select().where(Batch.migration == row.id).get()
    assert job.status == "running"
    assert module.status == "running"
    assert batch.status == "running"
    assert client.app.state.executor.started == ["sale_custom"]

    client.app.state.on_result({"job_id": job.job_id, "status": "passed"})

    assert client.app.state.executor.started == ["sale_custom", "stock_custom"]
    with database.connection_context():
        assert Module.get_by_id(sale["id"]).status == "passed"
        assert Module.get_by_id(stock["id"]).status == "passed"
        assert Batch.select().where(Batch.migration == row.id).get().status == "checkpoint"


def test_an_empty_answer_is_refused(client):
    resume = RecordingResume()
    _row, sale, _stock = _park(client, resume)

    page = client.post(
        f"/tree/modules/{sale['id']}/answer",
        data={"answer": "   "},
        follow_redirects=True,
    )

    assert "Write an answer." in page.text
    assert resume.calls == []
    with database.connection_context():
        assert Module.get_by_id(sale["id"]).status == "waiting_for_human"


def test_a_failed_resume_stays_loud_and_keeps_the_job_waiting(client):
    from odoo_migrator.resume import ResumeError

    resume = RecordingResume()
    resume.error = ResumeError("n8n did not accept the answer: connection refused")
    _row, sale, _stock = _park(client, resume)

    page = client.post(
        f"/tree/modules/{sale['id']}/answer",
        data={"answer": ANSWER},
        follow_redirects=True,
    )

    assert "n8n did not accept the answer" in page.text
    with database.connection_context():
        assert Module.get_by_id(sale["id"]).status == "waiting_for_human"


def test_a_restart_does_not_forget_a_job_waiting_for_a_human(client):
    row, sale, _stock = _park(client)

    reconcile_jobs()

    with database.connection_context():
        job = Job.select().join(Module).where(Module.id == sale["id"]).get()
        batch = Batch.select().where(Batch.migration == row.id).get()
        module = Module.get_by_id(sale["id"])
    assert job.status == "waiting_for_human"
    assert module.status == "waiting_for_human"
    assert batch.status == "running"


def test_batch_defer_flags_a_green_module_that_carried_assumptions(client):
    row, sale = _prepared(client)
    client.post(
        f"/migrations/{row.id}/settings",
        data={
            "strictness_default": "basic",
            "retry_limit": "3",
            "ask_human_mode": "batch_defer",
        },
        follow_redirects=True,
    )
    deferrer = AssumptionExecutor(client.app)
    client.app.state.executor = deferrer
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)

    page = client.get("/tree")
    with database.connection_context():
        module = Module.get_by_id(sale["id"])
        rows = list(Assumption.select())
    assert module.status == "passed"
    assert module.has_assumptions is True
    assert rows[0].question == QUESTION
    assert rows[0].assumption == ANSWER
    assert "assumptions" in page.text


def test_the_trigger_carries_batch_defer(client):
    row, sale = _prepared(client)
    client.post(
        f"/migrations/{row.id}/settings",
        data={
            "strictness_default": "basic",
            "retry_limit": "3",
            "ask_human_mode": "batch_defer",
        },
        follow_redirects=True,
    )
    capture = CaptureExecutor(client.app)
    client.app.state.executor = capture
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)

    assert capture.payloads[0]["ask_human_mode"] == "batch_defer"


def test_sse_carries_the_question_so_the_row_can_show_it_without_a_reload(client):
    _park(client)
    questions = [
        event[1].get("question")
        for event in client.app.state.hub.published
        if event[0] == "module"
    ]

    assert QUESTION in questions
    script = client.get("/static/sse.js").text
    assert '".ask"' in script
    assert "waiting_for_human" in script


def test_styleguide_shows_notes_and_answer(client):
    html = client.get("/components").text

    assert ">Save<" in html
    assert ">Answer<" in html
    assert 'name="notes"' in html
    assert 'name="answer"' in html
    assert "assumptions" in html



class FinishingResume:
    """n8n resumes, runs to the end and posts its result before the resume
    call returns. Nothing makes that impossible — the Wait node hands control
    straight back to the workflow."""

    def __init__(self, app):
        self.app = app
        self.job_id = None
        self.calls = []

    def resume(self, url, answer):
        self.calls.append({"url": url, "answer": answer})
        self.app.state.on_result({"job_id": self.job_id, "status": "passed"})


def test_an_answer_does_not_overwrite_a_job_that_finished_meanwhile(client):
    """Writing "running" over a result that already landed left a finished
    module in flight on a batch that had already closed, so nothing would
    ever move it again."""
    _row, sale = _prepared(client)
    resume = FinishingResume(client.app)
    client.app.state.resume = resume
    client.app.state.executor = StubExecutor(
        progress=client.app.state.on_progress,
        result=client.app.state.on_result,
        ask=QUESTION,
    )
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)
    with database.connection_context():
        resume.job_id = Job.get(Job.module == sale["id"]).job_id

    client.post(
        f"/tree/modules/{sale['id']}/answer",
        data={"answer": ANSWER},
        follow_redirects=True,
    )

    with database.connection_context():
        assert Job.get(Job.module == sale["id"]).status == "passed"
        assert Module.get_by_id(sale["id"]).status == "passed"


def test_a_clean_re_run_clears_the_assumptions_badge(client):
    """Nothing cleared the flag, so a module that carried one assumption once
    wore the badge for the rest of the project."""
    _row, sale = _prepared(client)
    client.app.state.executor = CaptureExecutor(client.app)
    with database.connection_context():
        module = Module.get_by_id(sale["id"])
        module.has_assumptions = True
        module.save()

    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)

    with database.connection_context():
        assert Module.get_by_id(sale["id"]).has_assumptions is False


def test_the_answer_is_kept_beside_the_question(client):
    """The question was persisted and the reply was not, so the half that
    unblocked the job lived only in n8n's own execution log."""
    _row, sale = _prepared(client)
    resume = RecordingResume()
    client.app.state.resume = resume
    client.app.state.executor = StubExecutor(
        progress=client.app.state.on_progress,
        result=client.app.state.on_result,
        ask=QUESTION,
    )
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)

    client.post(
        f"/tree/modules/{sale['id']}/answer",
        data={"answer": ANSWER},
        follow_redirects=True,
    )

    with database.connection_context():
        job = Job.get(Job.module == sale["id"])

    assert job.question == QUESTION
    assert job.answer == ANSWER
