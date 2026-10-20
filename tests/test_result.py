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

    assert "sale_<wbr>custom" in page
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

    assert "Reopen finished" not in page
    assert 'aria-label="Reopen sale_custom"' in page

    done = client.post(f"/result/modules/{module_id}/reopen", follow_redirects=True)

    assert "Reopen finished" not in done.text
    assert 'aria-label="Reopen sale_custom"' not in done.text
    with database.connection_context():
        row = Module.get_by_id(module_id)
        assert row.status == "queued"
        assert row.has_assumptions is False
        assert Job.get(Job.job_id == "job-kept").status == "passed"


def test_a_stopped_module_can_be_reopened(client):
    """A person's Stop leaves the module `stopped`, which is an outcome like
    any other. It was missing from the reopenable statuses: no Reopen on
    Result, a refusal from the route, and Reopen finished skipped it."""
    migration_id = _open(client)
    with database.connection_context():
        module = Module.create(
            migration=migration_id, technical_name="sale_custom", status="stopped"
        )
        module_id = module.id

    page = client.get("/result").text

    assert 'aria-label="Reopen sale_custom"' in page

    client.post(f"/result/modules/{module_id}/reopen", follow_redirects=True)

    with database.connection_context():
        assert Module.get_by_id(module_id).status == "queued"
        Module.update(status="stopped").where(Module.id == module_id).execute()

    client.post(f"/migrations/{migration_id}/reopen", follow_redirects=True)

    with database.connection_context():
        assert Module.get_by_id(module_id).status == "queued"


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

    page = client.post(f"/migrations/{migration_id}/reopen", follow_redirects=True)

    assert page.status_code == 200
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

    assert "Build graph on Tree first" in page
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

    # The instant, whole, with its offset: `stamp.js` renders it in the
    # reader's zone. Truncated here it read as local time while it was UTC.
    assert 'datetime="2026-09-28T08:17:00+00:00"' in page
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
    assert summary["finished_at"] == "2026-09-28T08:17:00+00:00"
    assert [step["id"] for step in summary["steps"]] == ["port", "tests"]
    assert summary["steps"][1]["ok"] is False
    assert [m["author"] for m in summary["messages"]] == ["agent", "human"]
    assert "malformed" in summary["messages"][0]["text"]
    assert summary["messages"][0]["at"]


def test_a_summary_says_what_the_model_spent(client):
    """The run log prints tokens per step as it goes and nothing added them
    up, so what a module cost was a sum done by hand over a log."""
    from odoo_migrator.models import Job, StepRun

    migration_id = _open(client)
    module = _ran(migration_id, "sale_custom")
    with database.connection_context():
        job = Job.get(Job.module == module.id)
        for step, tin, tout, cached in (("understand", 1000, 200, 5000), ("port", 300, 50, 0)):
            StepRun.create(
                job=job, attempt=1, step_id=step, kind="agent", ok=True,
                tokens_in=tin, tokens_out=tout, tokens_cached=cached,
            )

    summary = client.get(f"/api/result/modules/{module.id}/summary").json()

    assert summary["tokens"] == {"in": 1300, "out": 250, "cached": 5000}
    by_step = {step["id"]: step for step in summary["steps"]}
    assert by_step["understand"]["tokens"] == {"in": 1000, "out": 200}
    # A step that does not call the model spent nothing, and says so as None
    # so the panel prints nothing rather than "0+0".
    assert by_step["tests"]["tokens"] is None


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


def test_the_summary_says_what_happened_to_the_tests(client):
    """A step list that says `write_test` says a test was written and nothing
    about whether it ran. The pipeline's own `tests` step runs before that
    one, so for a module that shipped none the only run there is is the one
    the agent makes inside `write_test`."""
    from odoo_migrator.models import StepRun, ToolCall

    migration_id = _open(client)
    module = _ran(migration_id, "mig_test_account")
    with database.connection_context():
        step = StepRun.get(StepRun.step_id == "port")
        ToolCall.create(
            step_run=step, tool="list_tests", side="source", ok=True,
            preview="no tests on disk",
        )
        ToolCall.create(
            step_run=step, tool="write_file", side="target", ok=True,
            preview="/p/mig_test_account/tests/test_mig_test_account.py",
        )
        ToolCall.create(
            step_run=step, tool="write_file", side="target", ok=True,
            preview="/p/mig_test_account/models/account_move.py",
        )
        ToolCall.create(
            step_run=step, tool="run_test", side="target", ok=True,
            preview="passed: 3 test(s), 0 failure(s), 0 error(s)",
        )
        ToolCall.create(step_run=step, tool="exec", side="target", ok=True, preview="1")

    testing = client.get(f"/api/result/modules/{module.id}/summary").json()["testing"]

    assert [row["what"] for row in testing] == ["list_tests", "wrote", "run_test"]
    assert testing[1]["detail"].endswith("test_mig_test_account.py")
    assert "3 test(s)" in testing[2]["detail"]
    assert testing[0]["step"] == "port"


def test_the_summary_names_the_sheller_sessions_the_run_worked_in(client):
    """Reading the daemon's own journal for a run meant grepping the log for
    the line that printed the ids. `ShellerSession` is no help: its row is
    deleted when the session closes, so a finished job has none."""
    from odoo_migrator.models import StepRun, ToolCall

    migration_id = _open(client)
    module = _ran(migration_id, "mig_test_account")
    with database.connection_context():
        port = StepRun.get(StepRun.step_id == "port")
        tests = StepRun.get(StepRun.step_id == "tests")
        ToolCall.create(
            step_run=port, tool="exec", side="source", session_id="old1", command_id="1", ok=True
        )
        ToolCall.create(
            step_run=port, tool="exec", side="target", session_id="new1", command_id="1", ok=True
        )
        # No session: these are the host's, not the daemon's.
        ToolCall.create(step_run=port, tool="write_file", side="target", ok=True)
        # Once stamped with one. A run that read eight files and wrote five
        # showed thirteen "calls" the daemon never heard of; its journal had
        # five commands and the summary said eighteen.
        for tool in ("read_file", "read_file", "write_file"):
            ToolCall.create(step_run=port, tool=tool, side="target", session_id="new1", ok=True)
        # A route, and a call the daemon refused: neither became a command.
        ToolCall.create(step_run=port, tool="list_tests", side="target", session_id="new1", ok=True)
        ToolCall.create(step_run=port, tool="exec", side="target", session_id="new1", ok=False)
        # The same side again, reopened after the rewrite.
        ToolCall.create(
            step_run=tests, tool="exec", side="target", session_id="new2", command_id="1", ok=True
        )
        ToolCall.create(
            step_run=tests, tool="run_test", side="target", session_id="new2", command_id="2", ok=True
        )
        ToolCall.create(
            step_run=tests, tool="read_file", side="target", session_id="new2", ok=True
        )

    sessions = client.get(f"/api/result/modules/{module.id}/summary").json()["sessions"]

    assert [(row["side"], row["session_id"]) for row in sessions] == [
        ("source", "old1"),
        ("target", "new1"),
        ("target", "new2"),
    ]
    assert sessions[1]["calls"] == 1
    assert sessions[2]["calls"] == 2
    assert sessions[2]["steps"] == ["tests"]


def test_a_module_that_opened_no_shell_says_so(client):
    migration_id = _open(client)
    module = _ran(migration_id, "mig_test_account")

    summary = client.get(f"/api/result/modules/{module.id}/summary").json()

    assert summary["sessions"] == []


def test_a_test_run_is_recorded_by_its_verdict_not_its_json():
    """`{"stdout": "", "stdout_truncated": false, "result": null` is the first
    120 characters of a test report, and says nothing about the test."""
    from odoo_migrator.runner.tools import _preview

    report = {
        "stdout": "",
        "error": None,
        "test": {"success": True, "tests_run": 3, "failures": 0, "errors": 0},
    }

    assert _preview("run_test", report, "{}") == "passed: 3 test(s), 0 failure(s), 0 error(s)"
    red = {"error": None, "test": {"success": False, "tests_run": 3, "failures": 1, "errors": 0}}
    assert _preview("run_test", red, "{}").startswith("failed: 3 test(s), 1 failure(s)")
    # A run of nothing is not a pass, and the line says so.
    none = {"error": None, "test": {"success": True, "tests_run": 0}}
    assert _preview("run_test", none, "{}").startswith("failed: 0 test(s)")
    assert _preview("list_tests", {"classes": []}, "{}") == "no tests on disk"


def _chain(migration_id):
    """base <- sale <- shop <- extra, and `side`, which depends on nothing.

    Returns the ids by name."""
    import json

    ids = {}
    with database.connection_context():
        for name, depends, status in (
            ("base", [], "passed"),
            ("sale", ["base"], "passed"),
            ("shop", ["sale"], "passed"),
            ("extra", ["shop"], "failed"),
            ("side", [], "passed"),
        ):
            ids[name] = Module.create(
                migration=migration_id,
                technical_name=name,
                status=status,
                depends=json.dumps(depends),
            ).id

    return ids


def _flagged():
    with database.connection_context():
        return {
            row.technical_name
            for row in Module.select().where(Module.needs_review == True)
        }


def test_reopening_a_module_marks_what_depends_on_it_for_review(client):
    """The children passed against the old parent. Green on the board says
    they are checked, and after the parent changes that is no longer true."""
    migration_id = _open(client)
    ids = _chain(migration_id)

    client.post(f"/result/modules/{ids['base']}/reopen", follow_redirects=True)

    # Transitive: shop depends on base only through sale.
    assert _flagged() == {"sale", "shop"}
    with database.connection_context():
        # Still passed. The flag is a note, not a state, so the queue and the
        # counts are untouched.
        assert Module.get_by_id(ids["sale"]).status == "passed"
        assert Module.get_by_id(ids["base"]).status == "queued"
        # A failed module has nothing to be told; an unrelated one, nothing to fear.
        assert Module.get_by_id(ids["extra"]).needs_review is False
        assert Module.get_by_id(ids["side"]).needs_review is False


def test_an_excluded_dependent_is_not_marked(client):
    migration_id = _open(client)
    ids = _chain(migration_id)
    with database.connection_context():
        Module.update(struck_out=True).where(Module.id == ids["sale"]).execute()

    client.post(f"/result/modules/{ids['base']}/reopen", follow_redirects=True)

    assert "sale" not in _flagged()


def test_reopening_the_marked_module_itself_clears_its_flag(client):
    """It is going to run again, which is the review."""
    migration_id = _open(client)
    ids = _chain(migration_id)
    client.post(f"/result/modules/{ids['base']}/reopen", follow_redirects=True)

    client.post(f"/result/modules/{ids['sale']}/reopen", follow_redirects=True)

    with database.connection_context():
        assert Module.get_by_id(ids["sale"]).needs_review is False
    # ...and it, in turn, tells its own dependents.
    assert _flagged() == {"shop"}


def test_a_person_can_keep_a_marked_module_as_it_is(client):
    migration_id = _open(client)
    ids = _chain(migration_id)
    client.post(f"/result/modules/{ids['base']}/reopen", follow_redirects=True)

    page = client.get("/result").text
    assert "data-result-review" in page

    client.post(f"/result/modules/{ids['sale']}/keep", follow_redirects=True)

    assert _flagged() == {"shop"}
    with database.connection_context():
        assert Module.get_by_id(ids["sale"]).status == "passed"


def test_the_reopen_dialog_says_how_many_modules_it_will_mark(client):
    migration_id = _open(client)
    ids = _chain(migration_id)

    page = client.get("/result").text

    # base has two passed dependents (sale, shop); the failed one is not counted.
    assert "2 modules that depend on it will be marked for review." in page
    # A module nothing depends on says nothing about it.
    # base: sale and shop. sale: shop, and the singular says so. shop's only
    # dependent failed, so it is told nothing.
    assert "1 module that depends on it will be marked for review." in page
    assert page.count("will be marked for review.") == 2
    assert f'action="/result/modules/{ids["side"]}/reopen"' in page


def test_a_long_module_name_breaks_at_underscores_not_out_of_its_card(client):
    """A name as wide as the card ran out of it. It may wrap, but only
    between words, and copying it still gives the name: <wbr> adds no text."""
    migration_id = _open(client)
    with database.connection_context():
        Module.create(
            migration=migration_id,
            technical_name="integration_shopify_payouts",
            status="queued",
            depends='["integration_shopify"]',
        )
        Module.create(migration=migration_id, technical_name="integration_shopify", status="passed")

    page = client.get("/result").text

    assert "integration_<wbr>shopify_<wbr>payouts" in page
    assert "needs integration_<wbr>shopify" in page


def test_the_counters_over_the_board_can_be_read_again(client):
    """The cards followed the run over SSE and the figures above them stayed
    as the page loaded: "3 of 30" while the tenth module passed."""
    migration_id = _open(client)
    with database.connection_context():
        Module.create(migration=migration_id, technical_name="sale_custom", status="passed")
        Module.create(migration=migration_id, technical_name="stock_custom", status="failed")

    progress = client.get("/api/result/progress").json()

    assert progress["migration_id"] == migration_id
    assert progress["done"] == 1
    assert progress["failed"] == 1
    assert progress["total"] == 2
    page = client.get("/result").text
    assert "data-result-progress" in page
    assert "data-progress-waiting" in page
    script = client.get("/static/result.js").text
    assert "/api/result/progress" in script
