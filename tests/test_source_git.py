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


# -- a source outside git is put under it at the start ----------------------------


@pytest.fixture
def plain(tmp_path):
    """A source project that is no repository: two modules and a database volume."""
    init_db(tmp_path / "t.sqlite")
    source = tmp_path / "17"
    for name in ("sale_x", "stock_x"):
        (source / name).mkdir(parents=True)
        (source / name / "__manifest__.py").write_text(f"{{'name': '{name}'}}\n")
        (source / name / "models.py").write_text("OLD = 17\n")
    (source / "pgdata").mkdir()
    (source / "pgdata" / "PG_VERSION").write_text("16\n")
    created = create_migration(
        name="Plain", accent="violet", source_version="17.0", target_version="18.0",
        old_project_path=str(source), new_project_path=str(tmp_path / "18"),
    )
    with database.connection_context():
        for name in ("sale_x", "stock_x"):
            Module.create(migration=created["id"], technical_name=name, old_path=str(source / name))

    return created, source


def _log(root):
    return git(root, "log", "--format=%an|%s").splitlines()


def test_a_source_outside_git_gets_a_baseline_of_its_modules(plain):
    from odoo_migrator.source_git import ensure_repository

    created, source = plain

    said = ensure_repository(created["id"])

    assert (source / ".git").is_dir()
    assert "git init" in said
    assert "source" in said
    assert _log(source) == ["odoo-migrator|odoo-migrator: baseline of the source before porting"]
    tracked = git(source, "ls-files").split()
    assert "sale_x/models.py" in tracked and "stock_x/__manifest__.py" in tracked
    # Not the rest of the directory: a database volume is nobody's source.
    assert not any(path.startswith("pgdata") for path in tracked)
    assert "__pycache__/" in (source / ".gitignore").read_text()
    assert git(source, "status", "--short", "--", "sale_x") == ""


def test_a_source_already_in_git_is_left_as_it_is(plain):
    from odoo_migrator.source_git import ensure_repository

    created, source = plain
    git(source, "init", "-q")

    assert ensure_repository(created["id"]) is None
    assert not (source / ".gitignore").exists()


def test_a_module_that_is_its_own_repository_stays_out_of_the_source_baseline(plain):
    from odoo_migrator.source_git import ensure_repository

    created, source = plain
    git(source / "stock_x", "init", "-q")

    ensure_repository(created["id"])

    assert not any(path.startswith("stock_x") for path in git(source, "ls-files").split())


def test_a_project_inside_somebody_elses_repository_is_not_given_one(plain, tmp_path):
    from odoo_migrator.source_git import ensure_repository

    created, source = plain
    git(tmp_path, "init", "-q")

    assert ensure_repository(created["id"]) is None
    assert not (source / ".git").exists()


def test_a_baselined_source_takes_a_branch_and_a_green_test(plain):
    """The point of the baseline: the branch starts from a commit that has the
    code in it, and the commit of the test holds only the test."""
    from odoo_migrator.source_git import ensure_repository

    created, source = plain
    ensure_repository(created["id"])
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])
        module = Module.get(Module.technical_name == "sale_x")
    ensure_branch(migration)
    (source / "sale_x" / "tests").mkdir()
    (source / "sale_x" / "tests" / "test_x.py").write_text("# t\n")

    settle_tests(migration, module, "job1", green=True, session=SESSION)

    assert git(source, "branch", "--show-current").startswith("migrator/plain-")
    assert git(source, "show", "--name-only", "--format=", "HEAD").splitlines() == [
        "sale_x/tests/test_x.py"
    ]
    assert "sale_x/models.py" in git(source, "ls-files").split()


def test_a_start_puts_the_source_under_git_and_says_so(plain):
    from odoo_migrator.events import EventHub
    from odoo_migrator.executor import StubExecutor
    from odoo_migrator.jobs import start_job
    from odoo_migrator.runlog import lines

    created, source = plain
    with database.connection_context():
        module_id = Module.get(Module.technical_name == "sale_x").id

    job = start_job(
        migration_id=created["id"], module_id=module_id,
        executor=StubExecutor(progress=lambda body: None, result=lambda body: None),
        hub=EventHub(),
    )

    assert (source / ".git").is_dir()
    texts = [line["text"] for line in lines(job.job_id)]
    assert any("source" in text and "git init" in text for text in texts)



def test_what_sits_beside_the_modules_is_ignored_so_status_is_clean(plain):
    """A compose file, a mounted filestore with Odoo's sessions: not source,
    and not left in `git status` as work nobody committed."""
    from odoo_migrator.source_git import ensure_repository

    created, source = plain
    (source / "compose.yaml").write_text("services: {}\n")
    (source / "filestore" / "sessions").mkdir(parents=True)
    (source / "filestore" / "sessions" / "werkzeug_x.sess").write_text("token\n")

    ensure_repository(created["id"])

    ignore = (source / ".gitignore").read_text().splitlines()
    assert {"/compose.yaml", "/filestore/", "/pgdata/"} <= set(ignore)
    assert git(source, "status", "--short") == ""


def test_a_module_the_baseline_missed_is_added_at_the_next_start(plain):
    from odoo_migrator.source_git import ensure_repository

    created, source = plain
    with database.connection_context():
        Module.update(path_problem="not mounted").where(Module.technical_name == "stock_x").execute()
    ensure_repository(created["id"])
    assert not any(path.startswith("stock_x") for path in git(source, "ls-files").split())
    with database.connection_context():
        Module.update(path_problem="").where(Module.technical_name == "stock_x").execute()

    said = ensure_repository(created["id"])

    assert "stock_x" in said and "the baseline missed" in said
    assert _log(source)[0] == (
        "odoo-migrator|odoo-migrator: baseline of 1 more module directory as they were"
    )
    assert "stock_x/models.py" in git(source, "ls-files").split()
    assert git(source, "status", "--short") == ""
    # Nothing left to add: the next start says nothing.
    assert ensure_repository(created["id"]) is None


def test_the_catch_up_leaves_unproven_tests_and_a_persons_edits_alone(plain):
    """Tests the agent is still writing are committed only once green, and a
    change a person made to a tracked file is theirs to commit."""
    from odoo_migrator.source_git import ensure_repository, mark_unproven

    created, source = plain
    ensure_repository(created["id"])
    (source / "sale_x" / "models.py").write_text("OLD = 17  # a person's fix\n")
    mark_unproven(source / "sale_x" / "tests")
    (source / "sale_x" / "tests").mkdir()
    (source / "sale_x" / "tests" / "test_x.py").write_text("# red\n")
    (source / "sale_x" / "wizard.py").write_text("NEW = 1\n")

    ensure_repository(created["id"])

    assert git(source, "show", "--name-only", "--format=", "HEAD").splitlines() == [
        "sale_x/wizard.py"
    ]
    assert git(source, "diff", "--name-only") == "sale_x/models.py"
    assert git(source, "ls-files", "--others", "--exclude-standard") == "sale_x/tests/test_x.py"


def test_tests_a_missed_module_shipped_with_go_in_with_its_code(plain):
    """A person's tests are the module's code, not the agent's unproven ones."""
    from odoo_migrator.source_git import ensure_repository

    created, source = plain
    with database.connection_context():
        Module.update(path_problem="not mounted").where(Module.technical_name == "stock_x").execute()
    ensure_repository(created["id"])
    with database.connection_context():
        Module.update(path_problem="").where(Module.technical_name == "stock_x").execute()
    (source / "stock_x" / "tests").mkdir()
    (source / "stock_x" / "tests" / "test_own.py").write_text("# a person's\n")

    ensure_repository(created["id"])

    assert "stock_x/tests/test_own.py" in git(source, "ls-files").split()


def test_a_settled_test_is_no_longer_unproven(plain):
    from odoo_migrator.source_git import ensure_repository, mark_unproven, unproven

    created, source = plain
    ensure_repository(created["id"])
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])
        module = Module.get(Module.technical_name == "sale_x")
    ensure_branch(migration)
    mark_unproven(source / "sale_x" / "tests")
    (source / "sale_x" / "tests").mkdir()
    (source / "sale_x" / "tests" / "test_x.py").write_text("# t\n")
    assert unproven(source) == ["sale_x/tests"]

    settle_tests(migration, module, "job1", green=True, session=SESSION)

    assert unproven(source) == []


def test_a_repository_a_person_made_gets_nothing_added(repo):
    from odoo_migrator.source_git import ensure_repository

    source, migration, _module = repo
    (source / "sale_x" / "wizard.py").write_text("NEW = 1\n")
    (source / "compose.yaml").write_text("services: {}\n")

    assert ensure_repository(migration.id) is None
    assert git(source, "log", "--format=%s").splitlines() == ["seed"]
    assert not (source / ".gitignore").exists()


def test_a_green_test_of_a_module_the_baseline_missed_lands_beside_its_code(plain):
    """The branch held tests with no code beside them: the baseline had taken
    one module of fifteen, and only `tests/` was committed after it."""
    from odoo_migrator.source_git import ensure_repository

    created, source = plain
    with database.connection_context():
        Module.update(path_problem="not mounted").where(Module.technical_name == "stock_x").execute()
    ensure_repository(created["id"])
    with database.connection_context():
        Module.update(path_problem="").where(Module.technical_name == "stock_x").execute()
        migration = Migration.get_by_id(created["id"])
        module = Module.get(Module.technical_name == "stock_x")
    ensure_branch(migration)
    (source / "stock_x" / "tests").mkdir()
    (source / "stock_x" / "tests" / "test_x.py").write_text("# t\n")

    line = settle_tests(migration, module, "job1", green=True, session=SESSION)

    assert "the baseline missed (stock_x)" in line
    assert "committed the characterization tests of stock_x" in line
    assert git(source, "show", "--name-only", "--format=", "HEAD").splitlines() == [
        "stock_x/tests/test_x.py"
    ]
    assert git(source, "show", "--name-only", "--format=", "HEAD~1").splitlines() == [
        "stock_x/__manifest__.py", "stock_x/models.py",
    ]


def test_the_environment_keeps_modules_outside_the_plan_in_sight(tmp_path):
    from odoo_migrator.target_git import environment

    root = tmp_path / "17"
    for path in ("project/sale_x", "project/other_x", "oca/web/web_x"):
        (root / path).mkdir(parents=True)
        (root / path / "__manifest__.py").write_text("{}\n")
    (root / "project" / "requirements.txt").write_text("x\n")
    (root / "odoo.conf").write_text("[options]\n")
    (root / ".env").write_text("SECRET=1\n")

    found = environment(root, [root / "project" / "sale_x"])

    assert found == ["/.env", "/odoo.conf", "/project/requirements.txt"]


# -- which tests are the migrator's to see carried -------------------------------


def _commit_tests(source, migration, module, body):
    ensure_branch(migration)
    tests = source / "sale_x" / "tests"
    tests.mkdir()
    (tests / "__init__.py").write_text("from . import test_x\n")
    (tests / "test_x.py").write_text(body)
    settle_tests(migration, module, "job1", green=True, session=SESSION)


def test_the_migrators_tests_name_the_methods_a_port_must_carry(repo):
    from odoo_migrator.source_git import characterization_names

    source, migration, module = repo
    _commit_tests(
        source, migration, module,
        "class T:\n    def test_a(self): assert 1\n    def test_b(self): assert 1\n",
    )

    assert characterization_names(migration, module) == ["test_a", "test_b"]


def test_a_module_with_no_tests_of_the_migrators_names_nothing(repo):
    from odoo_migrator.source_git import characterization_names

    _, migration, module = repo

    assert characterization_names(migration, module) == []


def test_tests_a_person_wrote_are_not_the_migrators_to_demand(repo):
    """A port may drop or rename a test someone wrote, for a reason of theirs.
    Only the migrator's own, proved green on the source, must arrive."""
    from odoo_migrator.source_git import characterization_names

    source, migration, module = repo
    (source / "sale_x" / "tests").mkdir()
    (source / "sale_x" / "tests" / "test_mine.py").write_text("class T:\n    def test_mine(self): assert 1\n")
    git(source, "add", "-A")
    git(source, "commit", "-q", "-m", "odoo-migrator: characterization tests for sale_x [sheller:ffffffffffff]")

    assert characterization_names(migration, module) == []
