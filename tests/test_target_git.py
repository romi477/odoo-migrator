"""The target is kept under git, and every ported module is its own commit.

A target that was not a repository had no undo and no guard: the hand-edit
check reads git, so a re-run wrote over a person's edits without a word. The
app puts such a target under git with a baseline of the modules as they were,
and commits each module that passes, in its own directory and nothing else.
"""

import shutil
import subprocess

import pytest

from odoo_migrator.db import database, init_db
from odoo_migrator.events import EventHub
from odoo_migrator.executor import StubExecutor
from odoo_migrator.jobs import apply_result, start_job
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Batch, Job, Migration, Module
from odoo_migrator.port_guard import git_state
from odoo_migrator.target_git import commit_module, ensure_repository

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(cwd, *args) -> str:

    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout


def _log(root) -> list[str]:

    return _git(root, "log", "--format=%an|%s").splitlines()


@pytest.fixture
def plain(tmp_path):
    """A target that is no repository: two modules and a database volume."""
    init_db(tmp_path / "git.sqlite")
    root = tmp_path / "new"
    for name in ("sale_x", "stock_x"):
        (root / name).mkdir(parents=True)
        (root / name / "__manifest__.py").write_text(f"{{'name': '{name}'}}\n")
        (root / name / "models.py").write_text("OLD = 17\n")
    (root / "pgdata").mkdir()
    (root / "pgdata" / "PG_VERSION").write_text("16\n")
    created = create_migration(
        name="Acme", accent="violet", source_version="17.0", target_version="18.0",
        old_project_path=str(tmp_path / "old"), new_project_path=str(root),
    )
    with database.connection_context():
        for name in ("sale_x", "stock_x"):
            Module.create(
                migration=created["id"], technical_name=name, new_path=str(root / name),
            )

    return created, root


def _module(name):
    with database.connection_context():

        return Module.get(Module.technical_name == name)


def test_a_target_outside_git_gets_a_baseline_of_its_modules(plain):
    created, root = plain

    said = ensure_repository(created["id"])

    assert (root / ".git").is_dir()
    assert "git init" in said
    assert _log(root) == ["odoo-migrator|odoo-migrator: baseline before porting"]
    tracked = _git(root, "ls-files").split()
    assert "sale_x/models.py" in tracked and "stock_x/__manifest__.py" in tracked
    # Not the rest of the directory: a database volume is nobody's source.
    assert not any(path.startswith("pgdata") for path in tracked)
    assert git_state(root / "sale_x") == "clean"
    assert "__pycache__/" in (root / ".gitignore").read_text()


def test_a_target_already_in_git_is_left_as_it_is(plain):
    created, root = plain
    _git(root, "init", "-q")

    assert ensure_repository(created["id"]) is None
    assert not (root / ".gitignore").exists()


def test_a_module_that_is_its_own_repository_stays_out_of_the_baseline(plain):
    created, root = plain
    _git(root / "stock_x", "init", "-q")

    ensure_repository(created["id"])

    assert not any(path.startswith("stock_x") for path in _git(root, "ls-files").split())


def test_a_ported_module_is_its_own_commit(plain):
    created, root = plain
    ensure_repository(created["id"])
    (root / "sale_x" / "models.py").write_text("NEW = 18\n")
    (root / "sale_x" / "__manifest__.py").unlink()
    (root / "sale_x" / "__pycache__").mkdir()
    (root / "sale_x" / "__pycache__" / "models.cpython-312.pyc").write_bytes(b"\0")
    (root / "stock_x" / "models.py").write_text("SOMEBODY_ELSE = 1\n")

    said = commit_module(_module("sale_x"), job_id="job-1")

    assert "committed" in said
    assert _log(root)[0] == "odoo-migrator|odoo-migrator: port sale_x to 18.0"
    changed = _git(root, "show", "--name-status", "--format=", "HEAD").splitlines()
    assert sorted(changed) == ["D\tsale_x/__manifest__.py", "M\tsale_x/models.py"]
    assert "job-1" in _git(root, "log", "-1", "--format=%b")
    # The neighbour's change is not this module's, and stays uncommitted.
    assert git_state(root / "stock_x") == "dirty"


def test_a_port_that_changed_nothing_is_no_commit(plain):
    created, root = plain
    ensure_repository(created["id"])

    assert commit_module(_module("sale_x"), job_id="job-1") is None
    assert len(_log(root)) == 1


def test_a_commit_does_not_ask_for_a_signature_or_run_hooks(plain):
    """A repository that signs every commit, or runs a hook that formats
    files, would have refused the commit or rewritten the port."""
    created, root = plain
    ensure_repository(created["id"])
    _git(root, "config", "commit.gpgsign", "true")
    _git(root, "config", "gpg.program", "false")
    hook = root / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    (root / "sale_x" / "models.py").write_text("NEW = 18\n")

    assert "committed" in commit_module(_module("sale_x"), job_id="job-1")


def test_a_repository_at_home_or_above_gets_no_commit(plain, monkeypatch):
    """A home directory kept in git is somebody's dotfiles, not this
    project's history."""
    _created, root = plain
    _git(root, "init", "-q")
    monkeypatch.setenv("HOME", str(root))
    (root / "sale_x" / "models.py").write_text("NEW = 18\n")

    assert commit_module(_module("sale_x"), job_id="job-1") is None
    assert git_state(root / "sale_x") == "dirty"


def _stub():

    return StubExecutor(progress=lambda body: None, result=lambda body: None)


def test_a_start_puts_the_target_under_git_and_says_so(plain):
    from odoo_migrator.runlog import lines

    created, root = plain

    job = start_job(
        migration_id=created["id"], module_id=_module("sale_x").id,
        executor=_stub(), hub=EventHub(),
    )

    assert (root / ".git").is_dir()
    texts = [line["text"] for line in lines(job.job_id)]
    assert any("git init" in text for text in texts)
    assert not any("not in a git repository" in text for text in texts)


def _finish(created, name, status):
    module = _module(name)
    with database.connection_context():
        batch = Batch.create(migration=Migration.get_by_id(created["id"]), status="running")
        Job.create(batch=batch, module=module, status="running", job_id=f"job-{status}")
    apply_result({"job_id": f"job-{status}", "status": status}, EventHub())


def test_a_module_that_passes_is_committed(plain):
    created, root = plain
    ensure_repository(created["id"])
    (root / "sale_x" / "models.py").write_text("NEW = 18\n")

    _finish(created, "sale_x", "passed")

    assert _log(root)[0].endswith("port sale_x to 18.0")
    assert git_state(root / "sale_x") == "clean"


def test_a_module_that_fails_is_left_uncommitted(plain):
    created, root = plain
    ensure_repository(created["id"])
    (root / "sale_x" / "models.py").write_text("HALF = 18\n")

    _finish(created, "sale_x", "failed")

    assert len(_log(root)) == 1
    assert git_state(root / "sale_x") == "dirty"
