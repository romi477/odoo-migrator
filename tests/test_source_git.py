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


def test_a_checkout_the_person_moved_off_the_branch_is_not_switched_back(repo):
    """They went back to main and committed a fix there. Switching to the
    migration's branch would roll the source's code back under the container
    and every port after it."""
    source, migration, _ = repo
    name = ensure_branch(migration)
    git(source, "switch", "main")
    (source / "sale_x" / "fix.py").write_text("FIXED = 1\n")
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "a fix on main")
    head = git(source, "rev-parse", "HEAD")

    with pytest.raises(SourceGitError, match=rf"on 'main'.*{name}"):
        ensure_branch(migration)

    assert git(source, "branch", "--show-current") == "main"
    assert git(source, "rev-parse", "HEAD") == head
    assert (source / "sale_x" / "fix.py").read_text() == "FIXED = 1\n"


def test_a_second_migration_does_not_pull_the_first_one_off_its_branch(repo):
    """17→18 and 17→19 from one source project: each has a branch, and a write
    for the first while the second is checked out is refused."""
    source, first, _ = repo
    name = ensure_branch(first)
    with database.connection_context():
        second = Migration.get_by_id(
            create_migration(
                name="Second", accent="violet", source_version="17.0", target_version="19.0",
                old_project_path=str(source), new_project_path=str(source.parent / "19"),
            )["id"]
        )
    ensure_branch(second)

    with pytest.raises(SourceGitError, match=name):
        ensure_branch(first)

    assert git(source, "branch", "--show-current") == branch_name(second)


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


# -- who wrote a test, and in which Sheller session ----------------------------

SESSION = "0123456789abcdef0123456789abcdef"


def _body(source):
    return git(source, "log", "-1", "--format=%B")


def test_the_title_of_a_green_commit_names_the_sheller_session(repo):
    source, migration, module = repo
    ensure_branch(migration)
    tests = source / "sale_x" / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text("# t\n")

    settle_tests(migration, module, "job1", green=True, session=SESSION, command="12")

    assert git(source, "log", "-1", "--format=%s") == (
        "odoo-migrator: characterization tests for sale_x [sheller:0123456789ab]"
    )
    assert git(source, "log", "-1", "--format=%ae") == "odoo-migrator@localhost"
    body = _body(source)
    assert f"Sheller-session: {SESSION}" in body
    assert "Sheller-command: 12" in body
    assert "Job job1." in body


def test_a_commit_without_a_command_id_leaves_that_line_out(repo):
    source, migration, module = repo
    ensure_branch(migration)
    (source / "sale_x" / "tests").mkdir()
    (source / "sale_x" / "tests" / "test_x.py").write_text("# t\n")

    settle_tests(migration, module, "job1", green=True, session=SESSION)

    assert "Sheller-command" not in _body(source)


def test_the_migrators_test_commits_are_told_from_everyone_elses(repo):
    from odoo_migrator.source_git import migrator_test_commits

    source, migration, module = repo
    ensure_branch(migration)
    (source / "sale_x" / "tests").mkdir()
    (source / "sale_x" / "tests" / "test_x.py").write_text("# t\n")
    settle_tests(migration, module, "job1", green=True, session=SESSION, command="12")
    # A person copying the title does not make the commit the migrator's.
    (source / "sale_x" / "tests" / "test_y.py").write_text("# mine\n")
    git(source, "add", "-A")
    git(
        source, "commit", "-q", "-m",
        "odoo-migrator: characterization tests for sale_x [sheller:ffffffffffff]",
    )
    # Nor is another commit of the migrator's a test commit.
    (source / "sale_x" / "notes.txt").write_text("n\n")
    git(source, "add", "-A")
    git(
        source, "-c", "user.name=odoo-migrator", "-c", "user.email=odoo-migrator@localhost",
        "commit", "-q", "-m", "odoo-migrator: port sale_x to 18.0",
    )

    found = migrator_test_commits(source / "sale_x" / "tests")

    assert [(one["module"], one["session"]) for one in found] == [("sale_x", "0123456789ab")]
    assert len(found[0]["sha"]) == 40


def test_no_commit_of_the_migrators_means_an_empty_answer(repo):
    from odoo_migrator.source_git import migrator_test_commits

    source, _, _ = repo

    assert migrator_test_commits(source / "sale_x") == []
    assert migrator_test_commits(source / "missing") == []


def test_a_module_deeper_in_the_project_is_committed_too(repo):
    """git adds nothing for a directory it has never seen when an exclude
    pathspec sits beside its path, which only showed at this depth."""
    source, migration, _ = repo
    deep = source / "project" / "deep_x"
    deep.mkdir(parents=True)
    (deep / "__manifest__.py").write_text("{'name': 'D'}\n")
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "a module in project/")
    with database.connection_context():
        module = Module.create(migration=migration, technical_name="deep_x", old_path=str(deep))
    ensure_branch(migration)
    (deep / "tests").mkdir()
    (deep / "tests" / "__init__.py").write_text("from . import test_x\n")
    (deep / "tests" / "test_x.py").write_text("# t\n")

    line = settle_tests(migration, module, "job1", green=True, session=SESSION)

    assert line.startswith("committed the characterization tests of deep_x as ")
    assert sorted(git(source, "show", "--name-only", "--format=", "HEAD").splitlines()) == [
        "project/deep_x/tests/__init__.py",
        "project/deep_x/tests/test_x.py",
    ]


def test_a_green_test_is_not_committed_to_a_branch_that_is_not_the_migrations(repo):
    """settle_tests ran with the checkout on main: the commit would have gone
    to the person's own branch."""
    source, migration, module = repo
    ensure_branch(migration)
    tests = source / "sale_x" / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text("# t\n")
    git(source, "switch", "main")
    head = git(source, "rev-parse", "main")

    line = settle_tests(migration, module, "job1", green=True, session=SESSION)

    assert git(source, "rev-parse", "main") == head
    assert git(source, "log", "--format=%s", "-3").count("characterization") == 0
    assert not tests.exists()
    assert "not on its branch" in line
    assert "main" in line


def test_a_tests_directory_that_cannot_be_removed_is_not_reported_removed(repo, monkeypatch):
    """A `__pycache__` the container made may not be the app's to delete."""
    source, migration, module = repo
    ensure_branch(migration)
    tests = source / "sale_x" / "tests"
    tests.mkdir()
    (tests / "test_x.py").write_text("# t\n")
    monkeypatch.setattr("odoo_migrator.source_git.shutil.rmtree", lambda *a, **k: None)

    line = settle_tests(migration, module, "job1", green=False)

    assert "could not be removed" in line
    assert str(tests) in line
    assert "were removed" not in line
