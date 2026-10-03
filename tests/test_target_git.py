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


def _person_commits(root, name, text, subject="fix by hand"):
    (root / name / "models.py").write_text(text)
    subprocess.run(
        ["git", "-c", "user.name=Ann", "-c", "user.email=ann@example.com",
         "commit", "-qam", subject],
        cwd=root, check=True, capture_output=True,
    )


def _ported(created, root, name="sale_x"):
    ensure_repository(created["id"])
    (root / name / "models.py").write_text("NEW = 18\n")
    commit_module(_module(name), job_id="job-1")


def test_a_persons_commit_after_the_port_is_listed(plain):
    from odoo_migrator.target_git import person_commits

    created, root = plain
    _ported(created, root)
    _person_commits(root, "sale_x", "NEW = 18\nFIXED = 1\n")
    _person_commits(root, "stock_x", "ELSEWHERE = 1\n", subject="not this module")

    listed = person_commits(root / "sale_x")

    assert [(row["author"], row["subject"]) for row in listed] == [("Ann", "fix by hand")]
    assert len(listed[0]["short"]) >= 7


def test_commits_before_any_port_are_somebody_elses_history(tmp_path):
    """A repository the person brought has commits by people all the way
    down. With no commit of the app's in this directory, there is no port
    for them to come after."""
    from odoo_migrator.target_git import person_commits

    root = tmp_path / "checkout"
    (root / "sale_x").mkdir(parents=True)
    (root / "sale_x" / "models.py").write_text("OLD = 17\n")
    _git(root, "init", "-q")
    subprocess.run(
        ["git", "-c", "user.name=Ann", "-c", "user.email=ann@example.com", "add", "."],
        cwd=root, check=True,
    )
    subprocess.run(
        ["git", "-c", "user.name=Ann", "-c", "user.email=ann@example.com",
         "commit", "-qm", "theirs"],
        cwd=root, check=True, capture_output=True,
    )

    assert person_commits(root / "sale_x") == []


def test_a_rerun_over_a_persons_commit_does_not_start(plain):
    """The directory is clean, so the uncommitted-changes check let it
    through, and a re-run wrote the fix out of the working copy."""
    from odoo_migrator.port_guard import HandEdits

    created, root = plain
    _ported(created, root)
    _person_commits(root, "sale_x", "NEW = 18\nFIXED = 1\n")

    with pytest.raises(HandEdits, match="committed by hand") as refused:
        start_job(
            migration_id=created["id"], module_id=_module("sale_x").id,
            executor=_stub(), hub=EventHub(),
        )

    (entry,) = refused.value.modules
    assert entry["files"] == []
    assert [row["subject"] for row in entry["commits"]] == ["fix by hand"]


def test_keep_lets_the_rerun_start_until_the_next_commit(plain):
    from odoo_migrator.port_guard import hand_edits_blocking, keep_hand_edits

    created, root = plain
    _ported(created, root)
    _person_commits(root, "sale_x", "NEW = 18\nFIXED = 1\n")
    module_id = _module("sale_x").id

    keep_hand_edits(created["id"], module_id)

    assert hand_edits_blocking(created["id"], [module_id]) is None
    _person_commits(root, "sale_x", "NEW = 18\nFIXED = 2\n", subject="another fix")
    assert "committed by hand" in hand_edits_blocking(created["id"], [module_id])


def test_the_port_is_given_what_a_person_committed_since_the_last_one(plain):
    from odoo_migrator.paths import cartridges_dir
    from odoo_migrator.runner.cartridge import Step, load_cartridge_file
    from odoo_migrator.runner.context import StepContext
    from odoo_migrator.runner.prompting import build_context

    created, root = plain
    _ported(created, root)
    _person_commits(root, "sale_x", "NEW = 18\nFIXED = 1\n")
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])
    ctx = StepContext(
        job_id="job-2", attempt=1, migration=migration, module=_module("sale_x"),
        cartridge=load_cartridge_file(cartridges_dir() / "stock.yaml"),
        sheller=None, sessions={}, emit=lambda _event: None,
    )
    step = Step(step="agent", id="port", context=["hand_changes"])

    rendered = build_context(step, ctx)

    assert "fix by hand" in rendered and "Ann" in rendered
    assert "+FIXED = 1" in rendered
    _person_commits(root, "stock_x", "ELSEWHERE = 1\n", subject="not this module")
    assert "ELSEWHERE" not in build_context(step, ctx)


def test_without_a_persons_commit_the_port_is_told_there_is_none(plain):
    from odoo_migrator.target_git import person_changes

    created, root = plain
    _ported(created, root)

    assert person_changes(root / "sale_x") == ""


def test_the_stock_port_and_rewrite_carry_a_persons_changes():
    from odoo_migrator.paths import cartridges_dir
    from odoo_migrator.runner.cartridge import load_cartridge_file

    stock = load_cartridge_file(cartridges_dir() / "stock.yaml")

    for step_id in ("port", "rewrite"):
        step = stock.step_by_id(step_id)
        assert "hand_changes" in step.context
        assert "carry them" in " ".join(step.prompt.split())
