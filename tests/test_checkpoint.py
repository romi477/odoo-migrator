"""End-of-batch review, continue/override, promote a finding. Task 12."""

from types import SimpleNamespace

import pytest
from test_batch import _group, _named, _prepared
from test_tree import _open

from odoo_migrator.db import database
from odoo_migrator.models import Batch, Finding, Job

FINDING = "tax mapping moved to account.tax"
DIFF = "- mapping = old\n+ mapping = new"
ASSUMPTION_Q = "which tax mapping?"
ASSUMPTION_A = "use the new tax mapping"


class ReviewExecutor:
    """Calls back through the app, the way a real executor does."""

    def __init__(self, app, status="passed", **extra):
        self.app = app
        self.status = status
        self.extra = extra
        self.started = []

    def start(self, payload):
        self.started.append(payload["module"]["technical_name"])
        body = {
            "job_id": payload["job_id"],
            "status": self.status,
            "attempts": [
                {
                    "iteration": 1,
                    "diff": DIFF,
                    "test_output": "FAILED" if self.status == "failed" else "ok",
                    "verdict": "fail" if self.status == "failed" else "pass",
                }
            ],
            "findings": [{"text": FINDING, "source": "understand"}],
            "assumptions": [{"question": ASSUMPTION_Q, "assumption": ASSUMPTION_A}],
        }
        body.update(self.extra)
        self.app.state.on_result(body)


def _failed_checkpoint(client):
    row, sale = _prepared(client)
    client.app.state.executor = ReviewExecutor(client.app, status="failed")
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)

    return row, sale


@pytest.fixture
def parked_job(client):
    question = "Which override do we drop?"
    _row, sale = _prepared(client)
    client.app.state.executor = ReviewExecutor(
        client.app, status="waiting_for_human", question=question
    )
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)

    return SimpleNamespace(question=question)


def test_a_checkpoint_shows_the_thread(client, parked_job):
    page = client.get("/run").text

    assert "checkpoint-thread" in page
    assert parked_job.question in page


def test_the_run_page_shows_the_checkpoint(client):
    _row, _sale = _failed_checkpoint(client)

    page = client.get("/run")

    assert page.status_code == 200
    assert "sale_custom" in page.text
    assert FINDING in page.text
    assert DIFF in page.text
    assert "stock" in page.text
    assert "1.0.0" in page.text
    assert ASSUMPTION_Q in page.text
    assert ASSUMPTION_A in page.text
    assert ">Continue<" in page.text
    assert "disabled" in page.text
    assert ">Override<" in page.text
    assert ">Dismiss<" in page.text
    assert ">Promote<" in page.text
    assert "Pick a version file" in page.text
    assert 'selected>' in page.text or 'value=""' in page.text


def test_continue_is_refused_while_a_failed_job_is_unread(client):
    _failed_checkpoint(client)

    page = client.post("/run/continue", follow_redirects=True)

    assert "Unread failed" in page.text or "waiting" in page.text.lower()
    with database.connection_context():
        assert Batch.select().get().status == "checkpoint"


def test_dismiss_then_continue_lets_the_next_batch_start(client):
    row, sale = _failed_checkpoint(client)
    stock = _named(row.id, "stock_custom")
    with database.connection_context():
        job = Job.select().where(Job.module == sale["id"]).get()

    client.post(f"/run/jobs/{job.id}/dismiss", follow_redirects=True)
    continued = client.post("/run/continue", follow_redirects=True)

    assert continued.status_code == 200
    with database.connection_context():
        assert Batch.select().where(Batch.migration == row.id).get().status == "idle"

    _group(client, sale, stock)
    client.app.state.executor = ReviewExecutor(client.app, status="passed")
    again = client.post("/tree/batch/run", data={"batch_group": "1"}, follow_redirects=True)

    assert "Review the last batch" not in again.text
    with database.connection_context():
        assert Batch.select().where(Batch.migration == row.id).count() == 2


def test_override_continues_without_dismissing(client):
    _failed_checkpoint(client)

    client.post("/run/continue", data={"override": "1"}, follow_redirects=True)

    with database.connection_context():
        batch = Batch.select().get()
        job = Job.select().get()
    assert batch.status == "idle"
    assert job.reviewed is False
    assert job.status == "failed"


def test_a_new_batch_is_refused_while_the_checkpoint_is_open(client):
    row, sale = _failed_checkpoint(client)
    stock = _named(row.id, "stock_custom")
    _group(client, sale, stock)

    page = client.post("/tree/batch/run", data={"batch_group": "1"}, follow_redirects=True)

    assert "Review the last batch" in page.text
    with database.connection_context():
        assert Batch.select().where(Batch.migration == row.id).count() == 1


def test_promote_without_a_version_is_refused(tmp_path, compose, fake_sheller):
    from fastapi.testclient import TestClient

    from odoo_migrator.api import create_app

    rules = tmp_path / "rules"
    rules.mkdir()
    for version in ("17.0", "18.0"):
        (rules / f"{version}.md").write_text(f"# {version}\n", encoding="utf-8")
    app = create_app(
        tmp_path / "cp.sqlite",
        compose=compose,
        sheller=fake_sheller,
        rules_dir=rules,
    )
    client = TestClient(app)
    _failed_checkpoint(client)
    with database.connection_context():
        finding = Finding.select().get()

    page = client.post(
        f"/run/findings/{finding.id}/promote",
        data={"version": ""},
        follow_redirects=True,
    )

    assert "Pick a version file" in page.text
    with database.connection_context():
        assert Finding.get_by_id(finding.id).promoted_to_version is None
    assert FINDING not in (rules / "18.0.md").read_text(encoding="utf-8")


def test_promote_writes_the_file_the_human_picked_not_the_target(
    tmp_path, compose, fake_sheller
):
    """The app cannot infer which version caused the issue. Picking 17.0
    on a 17→18 migration is the user's call, even if 18.0 looks obvious."""
    from fastapi.testclient import TestClient

    from odoo_migrator.api import create_app

    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "17.0.md").write_text("# 17.0\ncoming from 17\n", encoding="utf-8")
    (rules / "18.0.md").write_text("# 18.0\nwhat 18 changed\n", encoding="utf-8")
    app = create_app(
        tmp_path / "cp.sqlite",
        compose=compose,
        sheller=fake_sheller,
        rules_dir=rules,
    )
    client = TestClient(app)
    _failed_checkpoint(client)
    with database.connection_context():
        finding = Finding.select().get()

    client.post(
        f"/run/findings/{finding.id}/promote",
        data={"version": "17.0"},
        follow_redirects=True,
    )

    seventeen = (rules / "17.0.md").read_text(encoding="utf-8")
    eighteen = (rules / "18.0.md").read_text(encoding="utf-8")
    assert FINDING in seventeen
    assert FINDING not in eighteen
    with database.connection_context():
        assert Finding.get_by_id(finding.id).promoted_to_version == "17.0"


def test_promote_to_eighteen_writes_rules_eighteen(tmp_path, compose, fake_sheller):
    from fastapi.testclient import TestClient

    from odoo_migrator.api import create_app

    rules = tmp_path / "rules"
    rules.mkdir()
    (rules / "17.0.md").write_text("# 17.0\n", encoding="utf-8")
    (rules / "18.0.md").write_text("# 18.0\nstub\n", encoding="utf-8")
    app = create_app(
        tmp_path / "cp.sqlite",
        compose=compose,
        sheller=fake_sheller,
        rules_dir=rules,
    )
    client = TestClient(app)
    _failed_checkpoint(client)
    with database.connection_context():
        finding = Finding.select().get()

    client.post(
        f"/run/findings/{finding.id}/promote",
        data={"version": "18.0"},
        follow_redirects=True,
    )

    assert FINDING in (rules / "18.0.md").read_text(encoding="utf-8")
    with database.connection_context():
        assert Finding.get_by_id(finding.id).promoted_to_version == "18.0"


def test_styleguide_shows_checkpoint_actions(client):
    html = client.get("/components").text

    assert ">Continue<" in html
    assert ">Override<" in html
    assert ">Promote<" in html
    assert ">Dismiss<" in html
    assert "Pick a version file" in html


def _rules_app(tmp_path, compose, fake_sheller):
    from fastapi.testclient import TestClient

    from odoo_migrator.api import create_app

    rules = tmp_path / "rules"
    rules.mkdir()
    for version in ("17.0", "18.0"):
        (rules / f"{version}.md").write_text(f"# {version}\ncurated\n", encoding="utf-8")
    app = create_app(
        tmp_path / "cp.sqlite",
        compose=compose,
        sheller=fake_sheller,
        rules_dir=rules,
    )

    return TestClient(app), rules


def test_a_promotion_says_where_it_came_from(tmp_path, compose, fake_sheller):
    """These files are curated by hand and read in review. An anonymous
    paragraph at the bottom is indistinguishable from a rule someone wrote
    on purpose, and nothing said which run produced it."""
    client, rules = _rules_app(tmp_path, compose, fake_sheller)
    _failed_checkpoint(client)
    with database.connection_context():
        finding = Finding.select().get()
        job_id = finding.job.job_id

    client.post(
        f"/run/findings/{finding.id}/promote",
        data={"version": "18.0"},
        follow_redirects=True,
    )

    written = (rules / "18.0.md").read_text(encoding="utf-8")

    assert FINDING in written
    assert "<!-- promoted from sale_custom on " in written
    assert job_id in written
    # A comment, so the model reading the stack does not get a heading that
    # says nothing about Odoo.
    assert "\n## " not in written


def test_a_finding_from_another_migration_cannot_be_promoted(
    tmp_path, compose, fake_sheller
):
    """The screen only lists the open migration's findings, so an id from
    somewhere else is not something the human can have meant. Dismiss was
    scoped this way and promote was not."""
    client, rules = _rules_app(tmp_path, compose, fake_sheller)
    _failed_checkpoint(client)
    with database.connection_context():
        stranger = Finding.select().get()
    other = _open(client, "Beta")
    client.post(f"/migrations/{other.id}/open", follow_redirects=True)

    refused = client.post(
        f"/run/findings/{stranger.id}/promote",
        data={"version": "18.0"},
        follow_redirects=True,
    )

    assert "unknown finding" in refused.text
    assert FINDING not in (rules / "18.0.md").read_text(encoding="utf-8")
    with database.connection_context():
        assert Finding.get_by_id(stranger.id).promoted_to_version is None
