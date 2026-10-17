"""Discard: forget one module's run, as if it had never started.

Dismiss only marks a failed run as read. Discard deletes the run — its jobs,
logs and findings — puts the module's directory in the new project back to
its last commit, and queues the module again. It never touches a module that
passed, because the app has committed that port.
"""

import shutil

import pytest
import test_port_guard as guard
from test_checkpoint import _failed_checkpoint

from odoo_migrator.contract import ContractError
from odoo_migrator.db import database
from odoo_migrator.discard import DISCARDABLE, discard_run
from odoo_migrator.models import (
    Assumption,
    Attempt,
    Batch,
    CheckpointMessage,
    Finding,
    Job,
    Module,
    RunLog,
    StepRun,
    ToolCall,
)
from odoo_migrator.paths import static_dir

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

# The target checkout and its migration are test_port_guard's.
repo = guard.repo
migration = guard.migration
_module = guard._module


class Hub:
    def __init__(self):
        self.events = []

    def publish(self, topic, payload):
        self.events.append((topic, payload))


def _run_of(name, *, status="stopped", batch=None, job_id="job-1"):
    """One module's run, with a row in every table a run writes to."""
    with database.connection_context():
        module = Module.get(Module.technical_name == name)
        if batch is None:
            batch = Batch.create(migration=module.migration, status="checkpoint")
        job = Job.create(batch=batch, module=module, status=status, job_id=job_id)
        Attempt.create(job=job, iteration=1, verdict="fail")
        Finding.create(job=job, text="a finding", source="understand")
        Assumption.create(job=job, question="q", assumption="a")
        step = StepRun.create(job=job, step_id="understand", kind="agent")
        call = ToolCall.create(step_run=step, tool="exec")
        RunLog.create(job=job, text="a line", tool_call=call)
        CheckpointMessage.create(job=job, author="agent", text="hello")
        module.status = status
        module.has_assumptions = True
        module.needs_review = True
        module.port_fingerprint = "abc"
        module.kept_commit = "def"
        module.save()

        return batch.id


def _in_git(sale, tmp_path, *, changed=False):
    """Make the module's directory in the new project a git checkout, the way
    the target of a real migration is."""
    directory = tmp_path / "target" / "sale_custom"
    directory.mkdir(parents=True)
    (directory / "__manifest__.py").write_text("{'name': 'sale_custom'}\n")
    guard._git(directory, "init", "-q")
    guard._git(directory, "add", ".")
    guard._git(directory, "commit", "-qm", "19.0 code")
    if changed:
        (directory / "models.py").write_text("PORTED = 20\n")
    with database.connection_context():
        Module.update(new_path=str(directory)).where(Module.id == sale["id"]).execute()

    return directory


def _rows():
    with database.connection_context():

        return {
            model.__name__: model.select().count()
            for model in (Job, Attempt, Finding, Assumption, StepRun, ToolCall, RunLog, CheckpointMessage, Batch)
        }


def test_the_statuses_that_can_be_discarded_are_the_ones_that_did_not_pass():
    assert set(DISCARDABLE) == {"stopped", "failed", "unknown"}


def test_a_run_is_deleted_whole_and_its_batch_with_it(migration):
    _run_of("sale_x")

    discard_run(_module("sale_x").id, migration["id"])

    assert set(_rows().values()) == {0}


def test_the_module_is_queued_again_and_remembers_nothing(migration):
    _run_of("sale_x")

    discard_run(_module("sale_x").id, migration["id"])

    module = _module("sale_x")
    assert module.status == "queued"
    assert not module.has_assumptions
    assert not module.needs_review
    assert module.port_fingerprint == ""
    assert module.kept_commit == ""


def test_every_run_of_that_module_goes_not_only_the_last(migration):
    _run_of("sale_x", job_id="job-old")
    _run_of("sale_x", job_id="job-new")

    discard_run(_module("sale_x").id, migration["id"])

    assert _rows()["Job"] == 0


def test_a_batch_that_still_holds_another_module_stays(migration):
    batch = _run_of("sale_x")
    _run_of("stock_x", status="failed", batch=Batch.get_by_id(batch), job_id="job-2")

    discard_run(_module("sale_x").id, migration["id"])

    assert _rows()["Batch"] == 1
    assert _rows()["Job"] == 1
    assert _module("stock_x").status == "failed"


def test_the_modules_files_go_back_to_the_last_commit_and_no_other_module_is_touched(migration, repo):
    _run_of("sale_x")
    (repo / "sale_x" / "models.py").write_text("PORTED = 20\n")
    (repo / "sale_x" / "views.xml").write_text("<odoo/>\n")
    (repo / "stock_x" / "models.py").write_text("MINE = 1\n")

    discard_run(_module("sale_x").id, migration["id"])

    assert (repo / "sale_x" / "models.py").read_text() == "OLD = 19\n"
    assert not (repo / "sale_x" / "views.xml").exists()
    assert (repo / "stock_x" / "models.py").read_text() == "MINE = 1\n"


def test_a_module_that_passed_is_refused_and_nothing_is_touched(migration, repo):
    _run_of("sale_x", status="passed")
    (repo / "sale_x" / "models.py").write_text("PORTED = 20\n")

    with pytest.raises(ContractError, match="passed"):
        discard_run(_module("sale_x").id, migration["id"])

    assert _rows()["Job"] == 1
    assert (repo / "sale_x" / "models.py").read_text() == "PORTED = 20\n"


def test_a_module_that_is_running_is_refused(migration):
    _run_of("sale_x", status="running")

    with pytest.raises(ContractError):
        discard_run(_module("sale_x").id, migration["id"])

    assert _rows()["Job"] == 1


def test_a_directory_outside_git_is_refused_before_anything_is_deleted(migration, repo):
    """Its files could not be put back, and half a discard is the one outcome
    worse than none."""
    _run_of("sale_x")
    shutil.rmtree(repo / ".git")

    with pytest.raises(ContractError, match="git"):
        discard_run(_module("sale_x").id, migration["id"])

    assert _rows()["Job"] == 1
    assert _module("sale_x").status == "stopped"


def test_another_migrations_module_is_unknown(migration):
    _run_of("sale_x")

    with pytest.raises(ContractError, match="unknown module"):
        discard_run(_module("sale_x").id, "not-this-one")

    assert _rows()["Job"] == 1


def test_the_page_hears_that_the_module_is_queued_again(migration):
    _run_of("sale_x")
    hub = Hub()

    discard_run(_module("sale_x").id, migration["id"], hub)

    assert [(topic, body["status"]) for topic, body in hub.events] == [("module", "queued")]


def test_the_checkpoint_offers_discard_on_a_failed_run_and_says_what_it_does(client, tmp_path):
    _row, sale = _failed_checkpoint(client)
    _in_git(sale, tmp_path, changed=True)

    page = client.get("/run").text

    assert "/discard-run" in page
    assert 'data-confirm-title="Discard the run of' in page
    assert "1 changed file in its directory" in page
    assert "put back to the last commit" in page
    assert ">Discard run<" in page


def test_a_directory_outside_git_shows_the_button_off_and_says_why(client, tmp_path):
    _row, sale = _failed_checkpoint(client)
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "models.py").write_text("PORTED = 20\n")
    with database.connection_context():
        Module.update(new_path=str(plain)).where(Module.id == sale["id"]).execute()

    page = client.get("/run").text

    assert "is not a git checkout, so its files cannot be put back" in page
    assert "/discard-run" not in page


def test_the_discard_button_is_beside_dismiss_and_does_not_replace_it(client, tmp_path):
    _row, sale = _failed_checkpoint(client)
    _in_git(sale, tmp_path)

    page = client.get("/run").text

    assert ">Dismiss<" in page
    assert page.index(">Dismiss<") < page.index(">Discard run<")


def test_pressing_discard_clears_the_checkpoint(client, tmp_path):
    _row, sale = _failed_checkpoint(client)
    directory = _in_git(sale, tmp_path, changed=True)

    answer = client.post(f"/run/modules/{sale['id']}/discard-run", follow_redirects=False)

    assert answer.status_code == 303
    assert answer.headers["location"] == "/run"
    with database.connection_context():
        assert Job.select().count() == 0
        assert Module.get_by_id(sale["id"]).status == "queued"
    assert not (directory / "models.py").exists()
    assert "Continue" not in client.get("/run").text


def test_a_refused_discard_says_why_on_the_page(client, tmp_path):
    _row, sale = _failed_checkpoint(client)
    _in_git(sale, tmp_path)
    with database.connection_context():
        Module.update(status="passed").where(Module.id == sale["id"]).execute()

    page = client.post(f"/run/modules/{sale['id']}/discard-run")

    assert "passed" in page.text


def _actions(page):
    """The row of buttons on a job card."""
    start = page.index('class="row job-review-actions"')

    return page[start : page.index("</div>", start)]


def test_the_card_carries_continue_next_to_discard_run(client, tmp_path):
    """Continue was a single button above every card, outside the one a person
    was reading."""
    _row, sale = _failed_checkpoint(client)
    _in_git(sale, tmp_path)

    row = _actions(client.get("/run").text)

    assert 'action="/run/continue"' in row
    assert row.index(">Continue<") < row.index(">Discard run<")


def test_continue_waits_for_the_run_to_be_read_like_the_one_above(client, tmp_path):
    _row, sale = _failed_checkpoint(client)
    _in_git(sale, tmp_path)

    unread = _actions(client.get("/run").text)

    assert "disabled" in unread.split("Continue")[0].rsplit("<button", 1)[1]
    with database.connection_context():
        job = Job.get()
    client.post(f"/run/jobs/{job.id}/dismiss")
    read = _actions(client.get("/run").text)

    assert "disabled" not in read.split("Continue")[0].rsplit("<button", 1)[1]


def test_continue_is_a_plain_button_and_discard_run_is_the_orange_one(client, tmp_path):
    """Orange means a deletion, so nothing that only moves on is orange."""
    _row, sale = _failed_checkpoint(client)
    _in_git(sale, tmp_path)

    page = client.get("/run").text
    start = page.index('class="checkpoint"')
    checkpoint = page[start : page.index("data-confirm-dialog", start)]
    sheet = (static_dir() / "style.css").read_text(encoding="utf-8")
    block = sheet[sheet.index(".discard-run {") :].split("}", 1)[0]

    assert 'class="primary"' not in checkpoint
    assert 'class="discard-run"' in _actions(page)
    assert "color: var(--amber)" in block
    assert "border-color: var(--amber)" in block


def test_a_module_that_never_got_a_directory_is_forgotten_without_files(client):
    """The fixture's paths do not exist on disk: there is nothing to put back,
    which is not the same as a directory git does not know."""
    _row, sale = _failed_checkpoint(client)

    answer = client.post(f"/run/modules/{sale['id']}/discard-run", follow_redirects=False)

    assert answer.status_code == 303
    with database.connection_context():
        assert Job.select().count() == 0
