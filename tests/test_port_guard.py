"""A module edited by hand since the agent last wrote it does not start.

The target is often a git checkout of the new branch, still holding the old
code, and the port is written over it in place. Git is the undo. What must
not happen is the agent overwriting — or clearing — work a person did there
and has not committed. The agent's own uncommitted port, from an earlier run
of the same module, is not that: a fingerprint of what the last run left
tells the two apart. Only the module's own directory is looked at.
"""

import shutil
import subprocess

import pytest

from odoo_migrator.contract import ContractError
from odoo_migrator.db import database, init_db
from odoo_migrator.events import EventHub
from odoo_migrator.executor import StubExecutor
from odoo_migrator.jobs import apply_result, start_job
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Batch, Job, Migration, Module
from odoo_migrator.port_guard import fingerprint, git_state, hand_edits_blocking

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    """A target checkout on branch 20.0 with two modules, committed."""
    root = tmp_path / "new"
    for name in ("sale_x", "stock_x"):
        (root / name).mkdir(parents=True)
        (root / name / "__manifest__.py").write_text(f"{{'name': '{name}'}}\n")
        (root / name / "models.py").write_text("OLD = 19\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "19.0 code on the 20.0 branch")

    return root


@pytest.fixture
def migration(tmp_path, repo):
    init_db(tmp_path / "guard.sqlite")
    created = create_migration(
        name="Acme", accent="violet", source_version="19.0", target_version="20.0",
        old_project_path=str(tmp_path / "old"), new_project_path=str(repo),
    )
    with database.connection_context():
        for name in ("sale_x", "stock_x"):
            Module.create(
                migration=created["id"], technical_name=name,
                new_path=str(repo / name),
            )

    return created


def _module(name):
    with database.connection_context():

        return Module.get(Module.technical_name == name)


def test_git_state_reads_only_the_module_directory(repo):
    (repo / "stock_x" / "models.py").write_text("NEW = 20\n")

    assert git_state(repo / "sale_x") == "clean"
    assert git_state(repo / "stock_x") == "dirty"


def test_a_directory_outside_git_is_not_a_repository(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    assert git_state(plain) == "not_git"
    assert git_state(tmp_path / "missing") == "missing"


def test_a_clean_module_starts(migration):
    assert hand_edits_blocking(migration["id"], [_module("sale_x").id]) is None


def test_an_uncommitted_port_of_another_module_does_not_stop_this_one(migration, repo):
    (repo / "stock_x" / "models.py").write_text("PORTED = 20\n")

    assert hand_edits_blocking(migration["id"], [_module("sale_x").id]) is None


def test_our_own_uncommitted_port_does_not_stop_a_rerun(migration, repo):
    (repo / "sale_x" / "models.py").write_text("PORTED = 20\n")
    with database.connection_context():
        Module.update(port_fingerprint=fingerprint(repo / "sale_x")).where(
            Module.technical_name == "sale_x"
        ).execute()

    assert hand_edits_blocking(migration["id"], [_module("sale_x").id]) is None


def test_a_hand_edit_after_the_agent_stops_the_module(migration, repo):
    (repo / "sale_x" / "models.py").write_text("PORTED = 20\n")
    with database.connection_context():
        Module.update(port_fingerprint=fingerprint(repo / "sale_x")).where(
            Module.technical_name == "sale_x"
        ).execute()
    (repo / "sale_x" / "models.py").write_text("PORTED = 20\nFIXED_BY_HAND = 1\n")

    message = hand_edits_blocking(migration["id"], [_module("sale_x").id])

    assert "sale_x" in message
    assert "edited by hand" in message
    with pytest.raises(ContractError, match="edited by hand"):
        start_job(
            migration_id=migration["id"], module_id=_module("sale_x").id,
            executor=StubExecutor(progress=lambda body: None, result=lambda body: None),
            hub=EventHub(),
        )


def test_a_hand_edit_before_any_run_stops_the_module(migration, repo):
    (repo / "sale_x" / "models.py").write_text("MINE = 1\n")

    assert "edited by hand" in hand_edits_blocking(migration["id"], [_module("sale_x").id])


def test_a_finished_run_records_what_it_left(migration, repo):
    module = _module("sale_x")
    with database.connection_context():
        batch = Batch.create(migration=Migration.get_by_id(migration["id"]), status="running")
        Job.create(batch=batch, module=module, status="running", job_id="job-1")
    (repo / "sale_x" / "models.py").write_text("PORTED = 20\n")

    apply_result({"job_id": "job-1", "status": "passed"}, EventHub())

    assert _module("sale_x").port_fingerprint == fingerprint(repo / "sale_x")
    assert hand_edits_blocking(migration["id"], [module.id]) is None


def test_fingerprint_ignores_bytecode(repo):
    before = fingerprint(repo / "sale_x")
    (repo / "sale_x" / "__pycache__").mkdir()
    (repo / "sale_x" / "__pycache__" / "models.cpython-312.pyc").write_bytes(b"\x00")

    assert fingerprint(repo / "sale_x") == before


def test_bytecode_odoo_left_in_the_checkout_is_not_a_change(repo):
    """The target imports the mounted module and leaves __pycache__ behind.
    Unless the repository ignores it, git lists it as untracked."""
    cache = repo / "sale_x" / "__pycache__"
    cache.mkdir()
    (cache / "models.cpython-312.pyc").write_bytes(b"\x00")

    assert git_state(repo / "sale_x") == "clean"


def test_a_module_outside_git_starts_with_a_warning_in_its_log(tmp_path):
    from odoo_migrator.runlog import lines

    init_db(tmp_path / "plain.sqlite")
    plain = tmp_path / "plain"
    (plain / "sale_x").mkdir(parents=True)
    created = create_migration(
        name="Plain", accent="violet", source_version="19.0", target_version="20.0",
        old_project_path=str(tmp_path / "old"), new_project_path=str(plain),
    )
    with database.connection_context():
        module = Module.create(
            migration=created["id"], technical_name="sale_x", new_path=str(plain / "sale_x"),
        )

    job = start_job(
        migration_id=created["id"], module_id=module.id,
        executor=StubExecutor(progress=lambda body: None, result=lambda body: None),
        hub=EventHub(),
    )

    texts = [line["text"] for line in lines(job.job_id)]
    assert any("not in a git repository" in text for text in texts)
