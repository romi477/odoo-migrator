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
Nothing here initialises git: a source project that is not a repository is
refused, not made into one.
"""

from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

from odoo_migrator.module_paths import module_root
from odoo_migrator.target_git import _EXCLUDED, _LOCK, AUTHOR, _git, _too_broad, _toplevel, _why

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
    """
    directory = module_root(migration, module, "source")
    if directory is None:

        return None
    tests = Path(directory) / "tests"
    if not tests.is_dir():

        return None
    name = module.technical_name
    if not green:
        gone = _remove(tests)

        return (
            f"the characterization tests of {name} did not pass on the source "
            f"version, so {gone}"
        )
    top = _toplevel(tests)
    if top is None or _too_broad(top):

        return f"the characterization tests of {name} could not be committed: no repository"
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
        )
    # From inside `tests/` with `.`, as `commit_module` does: git adds nothing
    # when an exclude pathspec sits beside the path of a directory it has
    # never seen, which is exactly what this is.
    with _LOCK:
        try:
            _git(tests, "add", "-A", "--", ".", *_EXCLUDED)
            staged = _git(tests, "diff", "--cached", "--quiet", "--", ".", check=False)
            if staged.returncode == 0:

                return None
            _git(
                tests, "commit", "-q", "--no-verify", "-m",
                _message(name, job_id, session, command),
                "--", ".",
            )
            head = _git(tests, "rev-parse", "--short", "HEAD", check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:

            return f"the characterization tests of {name} could not be committed: {_why(exc)}"

    return f"committed the characterization tests of {name} as {head}"


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
