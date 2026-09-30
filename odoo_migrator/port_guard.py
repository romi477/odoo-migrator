"""Do not write over a module a person has changed and not committed.

A port is written in place: the target is typically a git checkout of the
new branch, still holding the old code, and git is how a person reviews
and undoes what the agent wrote. That makes one case dangerous: work done
by hand in the module's directory since the agent last touched it, not yet
committed. Starting the module again would overwrite it, and a start-over
clears the directory first.

The agent's own uncommitted port — an earlier run of the same module — is
not that. What each run leaves is fingerprinted on the module, and a
directory that still matches it is the agent's. Only the module's own
directory is looked at, so an uncommitted port of the module beside it
stops nothing.
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path

from odoo_migrator.db import database
from odoo_migrator.models import Migration, Module
from odoo_migrator.module_paths import module_root

# Odoo imports a mounted module and leaves these beside the source. They
# are not anyone's work, and a repository that does not ignore them would
# otherwise read as changed on every module the target has loaded.
_NOISE = ("__pycache__",)
_NOISE_SUFFIXES = (".pyc", ".pyo")


def _noise(relative: str) -> bool:
    parts = Path(relative).parts

    return any(part in _NOISE for part in parts) or relative.endswith(_NOISE_SUFFIXES)


def fingerprint(directory: Path) -> str:
    """A hash of every file under `directory`, by relative path and content."""
    digest = hashlib.sha256()
    root = Path(directory)
    if not root.is_dir():

        return ""
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if not path.is_file() or _noise(relative):
            continue
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")

    return digest.hexdigest()


def git_state(directory: Path) -> str:
    """"clean", "dirty", "not_git" or "missing", for this directory only.

    `git status --porcelain -- .` from inside it: the stable, script-facing
    format, limited to this path. Bytecode Odoo left behind is not a change.
    """
    root = Path(directory)
    if not root.is_dir():

        return "missing"
    try:
        answer = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all", "--", "."],
            cwd=root,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):

        return "not_git"
    if answer.returncode != 0:

        return "not_git"
    for line in answer.stdout.splitlines():
        # "XY path", or "XY old -> new" for a rename.
        relative = line[3:].split(" -> ")[-1].strip().strip('"')
        if relative and not _noise(relative):

            return "dirty"

    return "clean"


def hand_edits_blocking(migration_id: str, module_ids: list[int]) -> str | None:
    """Modules whose directory holds uncommitted work that is not the agent's."""
    if not module_ids:

        return None
    with database.connection_context():
        migration = Migration.get_or_none(Migration.id == migration_id)
        modules = list(
            Module.select().where(
                (Module.migration == migration_id)
                & (Module.id.in_(module_ids))
                & (Module.struck_out == False)
            )
        )
    if migration is None:

        return None
    edited = []
    for module in sorted(modules, key=lambda row: row.technical_name):
        directory = module_root(migration, module, "new")
        if directory is None or git_state(directory) != "dirty":
            continue
        if module.port_fingerprint and fingerprint(directory) == module.port_fingerprint:
            continue
        edited.append(f"{module.technical_name} ({directory})")
    if not edited:

        return None

    return (
        "These modules were edited by hand since the agent last wrote them, "
        f"and the changes are not committed: {', '.join(edited)}. Commit or "
        "discard them first; starting again would write over them."
    )


def record_fingerprint(module: Module) -> None:
    """Remember what this run left in the module's directory. The caller
    holds the connection."""
    directory = module_root(module.migration, module, "new")
    module.port_fingerprint = fingerprint(directory) if directory is not None else ""
