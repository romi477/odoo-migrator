"""The source project gets a branch of its own, and a green test is committed there."""

import shutil
import subprocess

import pytest

from odoo_migrator.db import database, init_db
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Migration, Module
from odoo_migrator.source_git import SourceGitError, branch_name, ensure_branch, settle_tests

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    init_db(tmp_path / "t.sqlite")
    source = tmp_path / "17"
    (source / "sale_x").mkdir(parents=True)
    (source / "sale_x" / "__manifest__.py").write_text("{'name': 'X'}\n")
    git(source, "init", "-q", "-b", "main")
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "seed")
    target = tmp_path / "18"
    target.mkdir()
    created = create_migration(
        name="Acme Corp 17→18", accent="violet", source_version="17.0", target_version="18.0",
        old_project_path=str(source), new_project_path=str(target),
    )
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])
        module = Module.create(
            migration=migration, technical_name="sale_x",
            old_path=str(source / "sale_x"), new_path=str(target / "sale_x"),
        )

    return source, migration, module


def test_the_branch_name_is_safe_and_names_the_migration(repo):
    source, migration, _ = repo

    name = branch_name(migration)

    assert name.startswith("migrator/acme-corp-17-18-")
    assert name.endswith(str(migration.id)[:8])
    git(source, "check-ref-format", "--branch", name)


def test_a_name_with_nothing_usable_still_makes_a_branch(repo):
    source, migration, _ = repo
    migration.name = "→→→"

    name = branch_name(migration)

    assert name.startswith("migrator/migration-")
    git(source, "check-ref-format", "--branch", name)


def test_the_branch_is_made_from_head_and_the_default_branch_is_left_alone(repo):
    source, migration, _ = repo
    head = git(source, "rev-parse", "main")

    name = ensure_branch(migration)

    assert git(source, "branch", "--show-current") == name
    assert git(source, "rev-parse", name) == head
    assert git(source, "rev-parse", "main") == head


def test_a_second_call_reuses_the_branch(repo):
    source, migration, _ = repo

    first = ensure_branch(migration)
    second = ensure_branch(migration)

    assert first == second
    assert git(source, "branch", "--list", "migrator/*").count("migrator/") == 1


def test_the_branch_is_found_again_after_the_person_switched_away(repo):
    source, migration, _ = repo
    name = ensure_branch(migration)
    git(source, "switch", "main")

    assert ensure_branch(migration) == name
    assert git(source, "branch", "--show-current") == name


def test_uncommitted_work_travels_with_the_switch_and_stays_uncommitted(repo):
    source, migration, module = repo
    (source / "sale_x" / "notes.txt").write_text("mine\n")

    ensure_branch(migration)
    (source / "sale_x" / "tests").mkdir()
    (source / "sale_x" / "tests" / "test_x.py").write_text("# t\n")
    settle_tests(migration, module, "job1", green=True)

    committed = git(source, "show", "--name-only", "--format=", "HEAD").splitlines()
    assert (source / "sale_x" / "notes.txt").read_text() == "mine\n"
    assert committed == ["sale_x/tests/test_x.py"]
    assert "?? sale_x/notes.txt" in git(source, "status", "--short")


def test_a_project_that_is_not_a_repository_is_refused(tmp_path):
    init_db(tmp_path / "t.sqlite")
    source = tmp_path / "17"
    source.mkdir()
    created = create_migration(
        name="Plain", accent="violet", source_version="17.0", target_version="18.0",
        old_project_path=str(source), new_project_path=str(tmp_path / "18"),
    )
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])

    with pytest.raises(SourceGitError, match="not a git repository"):
        ensure_branch(migration)

    assert not (source / ".git").exists()


def test_a_switch_git_refuses_refuses_the_write_with_gits_words(repo):
    source, migration, _ = repo
    name = ensure_branch(migration)
    (source / "sale_x" / "conflict.txt").write_text("on the migration branch\n")
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "on branch")
    git(source, "switch", "main")
    (source / "sale_x" / "conflict.txt").write_text("a different, uncommitted version\n")

    with pytest.raises(SourceGitError, match=name):
        ensure_branch(migration)

    assert git(source, "branch", "--show-current") == "main"
    assert (source / "sale_x" / "conflict.txt").read_text() == "a different, uncommitted version\n"


def test_a_green_test_is_committed_with_only_its_own_files(repo):
    source, migration, module = repo
    ensure_branch(migration)
    tests = source / "sale_x" / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("from . import test_x\n")
    (tests / "test_x.py").write_text("# t\n")
    (tests / "__pycache__").mkdir()
    (tests / "__pycache__" / "x.pyc").write_bytes(b"\0")

    line = settle_tests(migration, module, "job1", green=True)

    changed = [
        line for line in git(source, "show", "--name-only", "--format=%s", "HEAD").splitlines() if line
    ]
    assert changed[0] == "odoo-migrator: characterization tests for sale_x"
    assert sorted(changed[1:]) == ["sale_x/tests/__init__.py", "sale_x/tests/test_x.py"]
    assert line.startswith("committed the characterization tests of sale_x as ")


def test_a_test_that_is_not_green_is_deleted_and_nothing_is_committed(repo):
    source, migration, module = repo
    ensure_branch(migration)
    before = git(source, "rev-parse", "HEAD")
    tests = source / "sale_x" / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text("# red\n")

    line = settle_tests(migration, module, "job1", green=False)

    assert not tests.exists()
    assert git(source, "rev-parse", "HEAD") == before
    assert git(source, "status", "--short") == ""
    assert "were removed" in line


def test_nothing_written_is_nothing_to_settle(repo):
    _, migration, module = repo

    assert settle_tests(migration, module, "job1", green=False) is None
    assert settle_tests(migration, module, "job1", green=True) is None


def test_a_repository_at_the_home_directory_is_not_written_into(repo, tmp_path, monkeypatch):
    source, migration, _ = repo
    monkeypatch.setattr("odoo_migrator.target_git.Path.home", lambda: source)

    with pytest.raises(SourceGitError, match="not a git repository"):
        ensure_branch(migration)
