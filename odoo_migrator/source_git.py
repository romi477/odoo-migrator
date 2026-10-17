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
from odoo_migrator.target_git import _EXCLUDED, _LOCK, _git, _too_broad, _toplevel, _why


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
            done = _git(top, "switch", *(() if exists else ("-c",)), name, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise SourceGitError(f"git failed in the source project: {_why(exc)}") from None
    if done.returncode != 0:
        raise SourceGitError(
            f"the source project could not be switched to {name}: {done.stderr.strip()}"
        )

    return name


def settle_tests(migration, module, job_id: str, green: bool) -> str | None:
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
        shutil.rmtree(tests, ignore_errors=True)

        return (
            f"the characterization tests of {name} did not pass on the source "
            "version, so they were removed"
        )
    top = _toplevel(tests)
    if top is None or _too_broad(top):

        return f"the characterization tests of {name} could not be committed: no repository"
    # Git reports its top level with symlinks resolved; the module's path may
    # not be (`/var` is `/private/var` on a Mac).
    relative = tests.resolve().relative_to(top.resolve()).as_posix()
    with _LOCK:
        try:
            _git(top, "add", "-A", "--", relative, *_EXCLUDED)
            staged = _git(top, "diff", "--cached", "--quiet", "--", relative, check=False)
            if staged.returncode == 0:

                return None
            _git(
                top, "commit", "-q", "--no-verify", "-m",
                f"odoo-migrator: characterization tests for {name}\n\nJob {job_id}.",
                "--", relative,
            )
            head = _git(top, "rev-parse", "--short", "HEAD", check=False).stdout.strip()
        except (OSError, subprocess.SubprocessError) as exc:

            return f"the characterization tests of {name} could not be committed: {_why(exc)}"

    return f"committed the characterization tests of {name} as {head}"
