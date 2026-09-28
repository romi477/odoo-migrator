from odoo_migrator.db import database
from odoo_migrator.models import Module


def _create(client, name="Acme"):

    return client.post(
        "/migrations",
        data={
            "name": name,
            "accent": "violet",
            "source_version": "17.0",
            "target_version": "18.0",
            "old_project_path": f"/tmp/{name}/old",
            "new_project_path": f"/tmp/{name}/new",
        },
        follow_redirects=True,
    )


def _open(client, name="Acme"):
    created = _create(client, name)
    migration_id = created.text.split("/migrations/", 1)[1].split("/open", 1)[0]
    client.post(f"/migrations/{migration_id}/open", follow_redirects=True)

    return migration_id


def test_result_page_groups_modules_by_wave(client):
    migration_id = _open(client)
    with database.connection_context():
        Module.create(
            migration=migration_id,
            technical_name="sale_custom",
            status="passed",
            depends='["product"]',
        )
        Module.create(
            migration=migration_id,
            technical_name="stock_custom",
            status="queued",
            depends='["sale_custom"]',
        )

    page = client.get("/result").text

    assert "result-columns" in page
    assert "result-card" in page
    assert "sale_custom" in page
    assert "stock_custom" in page
    assert "wave" in page
    assert "1 of 2 modules" in page
    assert 'data-screen="result"' in page


def test_struck_modules_show_excluded_not_queued(client):
    migration_id = _open(client)
    with database.connection_context():
        Module.create(
            migration=migration_id,
            technical_name="product",
            status="passed",
        )
        Module.create(
            migration=migration_id,
            technical_name="sale_custom",
            status="queued",
            struck_out=True,
            depends='["product"]',
        )

    page = client.get("/result").text

    assert "sale_custom" in page
    assert 'result-corner-badge excluded' in page
    assert "excluded — skipped on Tree" in page


def test_result_paginates_waves_four_per_page(client):
    migration_id = _open(client)
    names = [f"wave_{i}" for i in range(10)]
    with database.connection_context():
        for i, name in enumerate(names):
            depends = "[]" if i == 0 else f'["{names[i - 1]}"]'
            Module.create(
                migration=migration_id,
                technical_name=name,
                status="queued",
                depends=depends,
            )

    page = client.get("/result").text

    assert "result-columns-panel" in page
    assert page.count('class="result-page-dot"') == 3
    assert 'data-result-wave-page="0"' in page
    assert 'data-result-wave-page="2"' in page
    assert "wave_9" in page


def test_reopen_puts_a_finished_module_back_to_queued(client):
    from odoo_migrator.models import Batch, Job, Migration

    migration_id = _open(client)
    with database.connection_context():
        module = Module.create(
            migration=migration_id,
            technical_name="sale_custom",
            status="passed",
            has_assumptions=True,
        )
        module_id = module.id
        batch = Batch.create(migration=Migration.get_by_id(migration_id), status="idle")
        Job.create(batch=batch, module=module, status="passed", job_id="job-kept")

    page = client.get("/result").text

    assert "Reopen finished" in page
    assert 'aria-label="Reopen sale_custom"' in page

    done = client.post(f"/result/modules/{module_id}/reopen", follow_redirects=True)

    assert "Reopen finished" not in done.text
    assert 'aria-label="Reopen sale_custom"' not in done.text
    with database.connection_context():
        row = Module.get_by_id(module_id)
        assert row.status == "queued"
        assert row.has_assumptions is False
        assert Job.get(Job.job_id == "job-kept").status == "passed"


def test_reopen_finished_clears_every_recorded_outcome(client):
    migration_id = _open(client)
    with database.connection_context():
        Module.create(migration=migration_id, technical_name="sale_custom", status="passed")
        Module.create(migration=migration_id, technical_name="stock_custom", status="failed")
        kept = Module.create(
            migration=migration_id,
            technical_name="purchase_skip",
            status="passed",
            struck_out=True,
        )
        kept_id = kept.id

    page = client.post("/result/reopen", follow_redirects=True)

    assert page.status_code == 200
    assert "Reopen finished" not in page.text
    with database.connection_context():
        by_name = {row.technical_name: row.status for row in Module.select()}
        assert by_name["sale_custom"] == "queued"
        assert by_name["stock_custom"] == "queued"
        assert Module.get_by_id(kept_id).status == "passed"


def test_reopen_refuses_a_module_that_is_not_finished(client):
    migration_id = _open(client)
    with database.connection_context():
        module = Module.create(
            migration=migration_id,
            technical_name="sale_custom",
            status="queued",
        )
        module_id = module.id

    page = client.post(f"/result/modules/{module_id}/reopen", follow_redirects=True)

    assert "Only a finished module can be reopened." in page.text
    with database.connection_context():
        assert Module.get_by_id(module_id).status == "queued"


def test_result_without_modules_explains_resolve(client):
    _open(client)

    page = client.get("/result").text

    assert "Resolve the graph on Tree" in page
    assert "result-columns" not in page


def _ran(migration_id, name, *, status="passed", question="", answer=""):
    """A module with one finished job behind it."""
    from datetime import UTC, datetime

    from odoo_migrator.models import Batch, CheckpointMessage, Job, StepRun

    with database.connection_context():
        module = Module.create(
            migration=migration_id, technical_name=name, status=status, depends="[]"
        )
        batch = Batch.create(migration=migration_id, status="idle")
        job = Job.create(batch=batch, module=module, status=status, attempt=2)
        StepRun.create(
            job=job, attempt=2, step_id="port", kind="agent", ok=True,
            finished_at=datetime(2026, 9, 28, 8, 16, tzinfo=UTC), output="",
        )
        StepRun.create(
            job=job, attempt=2, step_id="tests", kind="tests", ok=False,
            finished_at=datetime(2026, 9, 28, 8, 17, tzinfo=UTC),
            output="1 failure",
        )
        if question:
            CheckpointMessage.create(job=job, author="agent", text=question)
        if answer:
            CheckpointMessage.create(job=job, author="human", text=answer)

        return module


def test_a_card_says_when_its_run_ended(client):
    """`passed` and nothing else. The first question about a result read a
    day later is when it happened."""
    migration_id = _open(client)
    _ran(migration_id, "sale_custom")

    page = client.get("/result").text

    assert "2026-09-28 08:17" in page
    assert "result-summary-open" in page


def test_a_card_opens_what_that_run_said(client):
    """The checkpoint thread was one flat list for the whole migration: a
    paragraph about one module above thirty cards, tied to none of them."""
    migration_id = _open(client)
    module = _ran(
        migration_id,
        "sale_custom",
        status="failed",
        question="## diagnose\nThe spec was malformed.",
        answer="Harness bug, fixed.",
    )

    page = client.get("/result").text
    summary = client.get(f"/api/result/modules/{module.id}/summary").json()

    # Not on the screen until someone opens that card.
    assert "The spec was malformed." not in page
    assert summary["status"] == "failed"
    assert summary["attempt"] == 2
    assert summary["finished_at"] == "2026-09-28 08:17"
    assert [step["id"] for step in summary["steps"]] == ["port", "tests"]
    assert summary["steps"][1]["ok"] is False
    assert [m["author"] for m in summary["messages"]] == ["agent", "human"]
    assert "malformed" in summary["messages"][0]["text"]
    assert summary["messages"][0]["at"]


def test_a_module_that_never_ran_has_no_summary_to_open(client):
    migration_id = _open(client)
    with database.connection_context():
        module = Module.create(
            migration=migration_id, technical_name="sale_custom", depends="[]"
        )

    page = client.get("/result").text
    answer = client.get(f"/api/result/modules/{module.id}/summary")

    assert "result-summary-open" not in page
    assert answer.status_code == 404


def test_a_summary_of_another_migrations_module_is_refused(client):
    """The open migration is the one this screen is showing."""
    other = _create(client, "Other")
    other_id = other.text.split("/migrations/", 1)[1].split("/open", 1)[0]
    theirs = _ran(other_id, "not_mine")
    # Opened last, so this is the migration the screen is showing.
    _open(client)

    answer = client.get(f"/api/result/modules/{theirs.id}/summary")

    assert answer.status_code == 404
