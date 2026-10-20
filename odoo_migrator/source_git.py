"""Give the source project a branch of its own, and keep characterization
tests on it.

The source side is read-only to the agent with one exception: a test for a
module that ships none, written by `write_source_test`. That file must not
land on the branch the person works on, so the first write of a migration
puts the source checkout on `migrator/<name>-<id>`, made from where it was.
A green test is committed there, one commit per module; a test that is not
green is deleted, so a red one never reaches the port.

Same conventions as `target_git`: a fixed author, no hooks, no signing, one
lock per process, and never a repository at or above the home directory.
A source project that is not a repository is put under git when a job starts
(`ensure_repository`), as the target is: no remote, one baseline commit of the
plan's modules as they are. A project inside somebody else's repository is
left alone, and the write that needs a branch is then refused.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from odoo_migrator.db import database
from odoo_migrator.models import Migration, Module
from odoo_migrator.module_paths import module_root
from odoo_migrator.target_git import (
    _EXCLUDED,
    _LOCK,
    AUTHOR,
    _git,
    _too_broad,
    _toplevel,
    _why,
    catch_up,
    init_with_baseline,
    made_here,
)
from odoo_migrator.test_names import test_method_names

# The title of a commit of tests the migrator wrote. The session the green run
# happened in is in the brackets, short enough to read and long enough to find
# in Sheller's journal; the whole id is in the body. A person can type the same
# title, so the author is checked as well (see `migrator_test_commits`).
TITLE = re.compile(
    r"^odoo-migrator: characterization tests for (?P<module>\S+)"
    r"(?: \[sheller:(?P<session>[0-9A-Za-z_-]+)\])?$"
)
SESSION_SHOWN = 12


class SourceGitError(Exception):
    """The source project cannot take a characterization test."""


BASELINE_MESSAGE = "odoo-migrator: baseline of the source before porting"


def ensure_repository(migration_id: str) -> str | None:
    """Put the source project under git if nothing holds it yet.

    A characterization test goes on a branch of its own, which needs a
    repository to branch in. The source is the person's code, so what goes in
    is the least that does the job: a repository with no remote, a
    `.gitignore` for what Odoo leaves behind, and one baseline commit of the
    plan's module directories as they are. Their history is not rewritten
    because there is none, and a project that sits inside another repository
    is left alone. In a repository this app made, each start adds what the
    baseline missed (`catch_up`). Returns what was done, for the run log, or
    None.
    """
    with database.connection_context():
        migration = Migration.get_or_none(Migration.id == migration_id)
        if migration is None:

            return None
        modules = list(
            Module.select().where((Module.migration == migration_id) & (Module.struck_out == False))
        )
        directories = [module_root(migration, module, "source") for module in modules]
        raw = (migration.old_project_path or "").strip()
    if not raw:

        return None
    root = Path(raw).expanduser()
    if not root.is_dir():

        return None
    if _toplevel(root) is not None:

        return catch_up(root, directories, modules=True, keep_out=unproven(root)) or None
    inside, failure = init_with_baseline(root, directories, BASELINE_MESSAGE)
    if failure:

        return failure

    return (
        f"{root} (source) had no repository: git init, and a baseline commit "
        f"of {len(inside)} module director{'y' if len(inside) == 1 else 'ies'} as they "
        "were. A characterization test, if one is written, goes on a branch of its own."
    )


# Tests the agent wrote that no green run has proved yet, one path per line,
# relative to the top of the repository. A start adds what the baseline missed
# and must not take these for a person's code: another module of the batch may
# be writing them, or a run that was killed left them behind. In the git
# directory, so it survives a restart and travels with no branch.
UNPROVEN = "odoo-migrator-unproven"


def _unproven_file(top: Path) -> Path:
    answer = _git(top, "rev-parse", "--git-path", UNPROVEN, check=False)

    return top / answer.stdout.strip()


def unproven(top: Path) -> list[str]:
    try:
        text = _unproven_file(top).read_text(encoding="utf-8")
    except OSError:

        return []

    return [line for line in text.splitlines() if line]


def mark_unproven(tests: Path) -> None:
    """`tests` holds what the agent is writing; a start leaves it untracked."""
    top = _toplevel(tests.parent)
    if top is None:

        return
    relative = tests.resolve().relative_to(top.resolve()).as_posix()
    with _LOCK:
        listed = unproven(top)
        if relative not in listed:
            _unproven_file(top).write_text("\n".join([*listed, relative]) + "\n", encoding="utf-8")


def _clear_unproven(top: Path, relative: str) -> None:
    listed = [line for line in unproven(top) if line != relative]
    target = _unproven_file(top)
    try:
        if listed:
            target.write_text("\n".join(listed) + "\n", encoding="utf-8")
        else:
            target.unlink(missing_ok=True)
    except OSError:
        pass


def branch_name(migration) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(migration.name).lower()).strip("-")[:30].strip("-")

    return f"migrator/{slug or 'migration'}-{str(migration.id)[:8]}"


def ensure_branch(migration) -> str:
    """The source checkout is on this migration's branch when this returns."""
    root = Path(migration.old_project_path or "").expanduser()
    top = _toplevel(root) if root.is_dir() else None
    if top is None or _too_broad(top):
        raise SourceGitError(
            "the source project is not a git repository, so a characterization "
            "test cannot be kept on a branch of its own. Nothing was written."
        )
    name = branch_name(migration)
    with _LOCK:
        try:
            current = _git(top, "branch", "--show-current", check=False).stdout.strip()
            if current == name:

                return name
            exists = (
                _git(
                    top, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False
                ).returncode
                == 0
            )
            if exists:
                # The first write made this branch and moved the checkout onto
                # it. A checkout that is somewhere else now was moved by a
                # person, who may have committed fixes since: switching back
                # would put older code under the source container and under
                # every port after it. Another migration's branch is the same.
                raise SourceGitError(
                    f"the source project is on {current or 'a detached HEAD'!r}, not on "
                    f"{name}, the branch of this migration. Switching back would put "
                    "older code under the source container, so nothing was written. "
                    "Switch to it (or merge it) yourself and start the module again."
                )
            done = _git(top, "switch", "-c", name, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SourceGitError(f"git failed in the source project: {_why(exc)}") from None
    if done.returncode != 0:
        raise SourceGitError(
            f"the source project could not be switched to {name}: {done.stderr.strip()}"
        )

    return name


def _remove(tests: Path) -> str:
    """Delete `tests/`, and say what happened. A `__pycache__` a container
    made may not be the app's to delete, and the directory then stays."""
    shutil.rmtree(tests, ignore_errors=True)
    if tests.exists():

        return f"they could not be removed: delete {tests} by hand"

    return "they were removed"


def settle_tests(
    migration, module, job_id: str, green: bool, session: str = "", command: str = ""
) -> str | None:
    """Commit the tests a step wrote when they pass; delete them when they do not.

    `tests/` did not exist before the step (the write is refused otherwise),
    so deleting it removes only what the step made. None when there is none.
    Either way they are no longer unproven (`mark_unproven`); tests that could
    not be committed stay so.
    """
    directory = module_root(migration, module, "source")
    if directory is None:

        return None
    tests = Path(directory) / "tests"
    if not tests.is_dir():

        return None
    top = _toplevel(tests)
    relative = tests.resolve().relative_to(top.resolve()).as_posix() if top else ""
    line, settled = _settle(migration, module, job_id, green, session, command, tests, top, relative)
    if settled and top is not None:
        with _LOCK:
            _clear_unproven(top, relative)

    return line


def _settle(migration, module, job_id, green, session, command, tests, top, relative):
    name = module.technical_name
    if not green:
        gone = _remove(tests)

        return (
            f"the characterization tests of {name} did not pass on the source "
            f"version, so {gone}"
        ), True
    if top is None or _too_broad(top):

        return f"the characterization tests of {name} could not be committed: no repository", False
    expected = branch_name(migration)
    current = _git(top, "branch", "--show-current", check=False).stdout.strip()
    if current != expected:
        # Committing here would put the tests on a branch that is not this
        # migration's, the person's own included. Left in the tree they would
        # be untracked files on it, so they go.
        gone = _remove(tests)

        return (
            f"the characterization tests of {name} were not committed: the source "
            f"project is not on its branch ({expected}) but on "
            f"{current or 'a detached HEAD'!r}, so {gone}"
        ), True
    # The tests go on the branch beside the code they test. A module the
    # baseline missed is added first, in a commit of its own.
    caught = ""
    if made_here(top):
        keep_out = [*unproven(top), relative]
        caught = catch_up(top, [tests.parent], modules=True, keep_out=keep_out)
    # From inside `tests/` with `.`, as `commit_module` does: git adds nothing
    # when an exclude pathspec sits beside the path of a directory it has
    # never seen, which is exactly what this is.
    with _LOCK:
        try:
            _git(tests, "add", "-A", "--", ".", *_EXCLUDED)
            staged = _git(tests, "diff", "--cached", "--quiet", "--", ".", check=False)
            if staged.returncode == 0:

                return (caught or None), True
            _git(
                tests, "commit", "-q", "--no-verify", "-m",
                _message(name, job_id, session, command),
                "--", ".",
            )
            head = _git(tests, "rev-parse", "--short", "HEAD", check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:

            return (
                f"the characterization tests of {name} could not be committed: {_why(exc)}"
            ), False
    done = f"committed the characterization tests of {name} as {head}"

    return (f"{caught}; {done}" if caught else done), True


def _message(name: str, job_id: str, session: str, command: str) -> str:
    """The title says who and in which session; the body says it all."""
    title = f"odoo-migrator: characterization tests for {name}"
    if session:
        title += f" [sheller:{session[:SESSION_SHOWN]}]"
    lines = [title, "", f"Job {job_id}."]
    if session:
        lines.append(f"Sheller-session: {session}")
    if command:
        lines.append(f"Sheller-command: {command}")

    return "\n".join(lines)


def migrator_test_commits(directory) -> list[dict]:
    """The commits of characterization tests the migrator made in `directory`,
    newest first: sha, module, and the short session id if the title has one.

    Both halves must hold: the author the migrator commits as, and its title.
    Either alone is easy to write by hand.
    """
    directory = Path(directory)
    if not directory.is_dir():

        return []
    answer = _git(
        directory, "log", f"--author={AUTHOR[1]}", "--format=%H%x1f%s", "--", ".", check=False
    )
    if answer.returncode != 0:

        return []
    found = []
    for line in answer.stdout.splitlines():
        sha, _, subject = line.partition("\x1f")
        matched = TITLE.match(subject)
        if matched:
            found.append(
                {"sha": sha, "module": matched["module"], "session": matched["session"] or ""}
            )

    return found


def characterization_dir(migration, module) -> Path | None:
    """The source's `tests/` of this module when the migrator wrote it, else
    None: a port may drop, rename or change a test a person wrote, for a
    reason of theirs."""
    directory = module_root(migration, module, "source")
    if directory is None:

        return None
    tests = Path(directory) / "tests"
    if not migrator_test_commits(tests):

        return None

    return tests


def characterization_names(migration, module) -> list[str]:
    """The test methods the migrator proved green on the source, which a port
    must carry to the target. Empty when the source's tests are not its own.
    """
    tests = characterization_dir(migration, module)
    if tests is None:

        return []

    return sorted(test_method_names(tests))
