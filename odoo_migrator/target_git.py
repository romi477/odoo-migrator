"""Keep the target under git, and commit each module the agent ports.

Git is the review and the undo of a port written in place, and the hand-edit
guard reads it. A target that was no repository had neither: a re-run wrote
over a person's edits without a word. So a target outside git is put under
it on the first start, with a baseline of the plan's modules as they were,
and every module that passes is committed on its own — one module, one
commit, in its own directory and nothing beside it.

Commits are the app's, not the person's: a fixed author, no signature asked
for, no hooks run. A hook that formats files would rewrite the port the
tests just passed, and a signing prompt would hang a background thread.
"""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from odoo_migrator.db import database
from odoo_migrator.models import Migration, Module
from odoo_migrator.module_paths import module_root

AUTHOR = ("odoo-migrator", "odoo-migrator@localhost")
BASELINE_MESSAGE = "odoo-migrator: baseline before porting"
# What Odoo leaves beside a module it imported. Not anyone's work.
IGNORED = ("__pycache__/", "*.py[co]")
_EXCLUDED = tuple(f":(exclude,glob)**/{pattern}**" if pattern.endswith("/") else
                  f":(exclude,glob)**/{pattern}" for pattern in IGNORED)
# Two modules of one batch can finish together, and git takes one index lock
# per repository. Commits wait their turn here rather than fail on it.
_LOCK = threading.Lock()


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:

    return subprocess.run(
        [
            "git",
            "-c", f"user.name={AUTHOR[0]}",
            "-c", f"user.email={AUTHOR[1]}",
            "-c", "commit.gpgsign=false",
            *args,
        ],
        cwd=cwd,
        capture_output=True,
        encoding="utf-8",
        errors="replace",
        timeout=60,
        check=check,
    )


def _toplevel(directory: Path) -> Path | None:
    try:
        answer = _git(directory, "rev-parse", "--show-toplevel", check=False)
    except (OSError, subprocess.TimeoutExpired):

        return None
    if answer.returncode != 0:

        return None

    return Path(answer.stdout.strip())


def ensure_repository(migration_id: str) -> str | None:
    """Put the new project directory under git if nothing holds it yet.

    The baseline holds the plan's module directories only. The rest of the
    directory — a database volume, a filestore, files this user cannot read
    — is not the project's source, and stays out. A module that is its own
    repository stays out too: it already has its history. Returns what was
    done, for the run log, or None when there was nothing to do.
    """
    with database.connection_context():
        migration = Migration.get_or_none(Migration.id == migration_id)
        if migration is None:

            return None
        modules = list(
            Module.select().where(
                (Module.migration == migration_id) & (Module.struck_out == False)
            )
        )
        directories = [module_root(migration, module, "new") for module in modules]
        raw = (migration.new_project_path or "").strip()
    if not raw:

        return None
    root = Path(raw).expanduser()
    if not root.is_dir() or _toplevel(root) is not None:

        return None
    inside = sorted(
        directory.relative_to(root).as_posix()
        for directory in directories
        if directory is not None
        and directory.is_dir()
        and directory.is_relative_to(root)
        and not (directory / ".git").exists()
    )
    with _LOCK:
        try:
            _git(root, "init", "-q")
            ignore = root / ".gitignore"
            if not ignore.exists():
                ignore.write_text("\n".join(IGNORED) + "\n", encoding="utf-8")
            _git(root, "add", "--", ".gitignore")
            if inside:
                _git(root, "add", "-A", "--", *inside, *_EXCLUDED)
            _git(root, "commit", "-q", "--no-verify", "--allow-empty", "-m", BASELINE_MESSAGE)
        except (OSError, subprocess.SubprocessError) as exc:

            return f"git init in {root} did not finish: {_why(exc)}"

    return (
        f"{root} had no repository: git init, and a baseline commit "
        f"of {len(inside)} module director{'y' if len(inside) == 1 else 'ies'} "
        "as they were. Each module that passes is committed on its own."
    )


def commit_module(module: Module, *, job_id: str) -> str | None:
    """Commit what the port left in this module's directory, and only there.

    None when there is nothing to commit, or no repository this app should
    write history into: one at the home directory or above is somebody's
    dotfiles, not this project.
    """
    with database.connection_context():
        migration = Migration.get_by_id(module.migration_id)
        directory = module_root(migration, module, "new")
        target = migration.target_version
    if directory is None or not Path(directory).is_dir():

        return None
    directory = Path(directory)
    top = _toplevel(directory)
    if top is None or _too_broad(top):

        return None
    with _LOCK:
        try:
            _git(directory, "add", "-A", "--", ".", *_EXCLUDED)
            staged = _git(directory, "diff", "--cached", "--quiet", "--", ".", check=False)
            if staged.returncode == 0:

                return None
            message = (
                f"odoo-migrator: port {module.technical_name} to {target}\n\n"
                f"Job {job_id}."
            )
            # `-- .`: the module's paths only. Whatever else a person staged
            # stays staged, and out of this commit.
            _git(directory, "commit", "-q", "--no-verify", "-m", message, "--", ".")
        except (OSError, subprocess.SubprocessError) as exc:

            return f"the port of {module.technical_name} could not be committed: {_why(exc)}"
        head = _git(directory, "rev-parse", "--short", "HEAD", check=False).stdout.strip()

    return f"committed the port of {module.technical_name} as {head}"


def _too_broad(top: Path) -> bool:
    home = Path.home().resolve()
    resolved = top.resolve()

    return resolved == home or resolved in home.parents


def _why(exc: Exception) -> str:
    stderr = getattr(exc, "stderr", "") or ""

    return stderr.strip() or str(exc)
