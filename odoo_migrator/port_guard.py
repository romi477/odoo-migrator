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

from odoo_migrator.contract import ContractError
from odoo_migrator.db import database
from odoo_migrator.models import Job, Migration, Module
from odoo_migrator.module_paths import module_root
from odoo_migrator.runner.snapshot import module_files
from odoo_migrator.target_git import person_commits

# Odoo imports a mounted module and leaves these beside the source. They
# are not anyone's work, and a repository that does not ignore them would
# otherwise read as changed on every module the target has loaded.
_NOISE = ("__pycache__",)
_NOISE_SUFFIXES = (".pyc", ".pyo")


def _noise(relative: str) -> bool:
    parts = Path(relative).parts

    return any(part in _NOISE for part in parts) or relative.endswith(_NOISE_SUFFIXES)


def fingerprint(directory: Path) -> str:
    """A hash of every file under `directory`, by relative path and content.

    Not the repository a module can hold (`.git`): every git command run
    there rewrites something in it, and the agent's own port read as a hand
    edit after the first `git status`.
    """
    digest = hashlib.sha256()
    root = Path(directory)
    if not root.is_dir():

        return ""
    for path in module_files(root):
        relative = path.relative_to(root).as_posix()
        if _noise(relative):
            continue
        try:
            content = path.read_bytes()
        except OSError:
            # The container can leave a file this user cannot read. It is
            # still there, and raising here failed the result it was for.
            content = b"(unreadable)"
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(content + b"\0")

    return digest.hexdigest()


def changed_files(directory: Path) -> list[str] | None:
    """What git lists as changed in this directory only, as "XY path"
    relative to it. None when it is not a git checkout.

    `git status --porcelain -z -- .` from inside it: the stable,
    script-facing format, limited to this path. `-z` because without it a
    name that is not plain ASCII comes back quoted and escaped, and a rename
    as "old -> new". Bytecode Odoo left behind is not a change.
    """
    root = Path(directory)
    try:
        prefix = subprocess.run(
            ["git", "rev-parse", "--show-prefix"],
            cwd=root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        answer = subprocess.run(
            ["git", "status", "--porcelain", "-z", "--untracked-files=all", "--", "."],
            cwd=root,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):

        return None
    if prefix.returncode != 0 or answer.returncode != 0:

        return None
    # Porcelain paths are relative to the repository, whatever the cwd.
    inside = prefix.stdout.rstrip("\n")
    changed = []
    fields = iter(answer.stdout.split("\0"))
    for entry in fields:
        if len(entry) < 4:
            continue
        # "XY path"; a rename or a copy is followed by the path it came from.
        status, relative = entry[:2], entry[3:]
        if "R" in status or "C" in status:
            next(fields, None)
        relative = relative.removeprefix(inside)
        if relative and not _noise(relative):
            changed.append(f"{status.strip()} {relative}")

    return changed


def git_state(directory: Path) -> str:
    """"clean", "dirty", "not_git" or "missing", for this directory only."""
    root = Path(directory)
    if not root.is_dir():

        return "missing"
    changed = changed_files(root)
    if changed is None:

        return "not_git"

    return "dirty" if changed else "clean"


class HandEdits(ContractError):
    """A start refused over hand edits, with the modules it was refused over,
    so the screen can offer a way out beside each one."""

    def __init__(self, message: str, modules: list[dict]):
        super().__init__(message)
        self.modules = modules


def hand_edits(migration_id: str, module_ids: list[int]) -> list[dict]:
    """Modules whose directory holds work that is not the agent's: id,
    technical_name, directory, the uncommitted files git lists, and the
    commits a person made there since the agent's last one.

    A commit leaves the directory clean, and a clean directory used to pass:
    a re-run then wrote a person's committed fix out of the working copy.
    """
    if not module_ids:

        return []
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

        return []
    edited = []
    for module in sorted(modules, key=lambda row: row.technical_name):
        directory = module_root(migration, module, "new")
        if directory is None:
            continue
        files: list[str] = []
        if git_state(directory) == "dirty" and not (
            module.port_fingerprint and fingerprint(directory) == module.port_fingerprint
        ):
            files = changed_files(directory) or []
        commits = person_commits(directory)
        if commits and commits[0]["sha"] == module.kept_commit:
            commits = []
        if not files and not commits:
            continue
        edited.append(
            {
                "id": module.id,
                "technical_name": module.technical_name,
                "directory": str(directory),
                "files": files,
                "commits": commits,
            }
        )

    return edited


def hand_edits_blocking(migration_id: str, module_ids: list[int]) -> str | None:
    """The sentence a refused start shows, or None."""
    edited = hand_edits(migration_id, module_ids)
    if not edited:

        return None
    sentences = []
    changed = [row for row in edited if row["files"]]
    if changed:
        named = ", ".join(f"{row['technical_name']} ({row['directory']})" for row in changed)
        sentences.append(
            "These modules were edited by hand since the agent last wrote them, "
            f"and the changes are not committed: {named}. Commit or "
            "discard them first; starting again would write over them."
        )
    committed = [row for row in edited if row["commits"]]
    if committed:
        named = ", ".join(
            f"{row['technical_name']} ({', '.join(c['short'] for c in row['commits'])})"
            for row in committed
        )
        sentences.append(
            "These modules were committed by hand since the agent last ported "
            f"them: {named}. A new port is written from the old version; keep "
            "the commits to run anyway, and the agent is given them to carry."
        )

    return " ".join(sentences)


def raise_hand_edits(migration_id: str, module_ids: list[int]) -> None:
    edited = hand_edits(migration_id, module_ids)
    if edited:
        raise HandEdits(hand_edits_blocking(migration_id, module_ids), edited)


def _editable(migration_id: str, module_id: int) -> tuple[Module, Path]:
    """The module and its target directory, when nothing is running it."""
    with database.connection_context():
        module = Module.get_or_none(
            (Module.id == module_id) & (Module.migration == migration_id)
        )
        if module is None:
            raise ContractError("unknown module")
        busy = (
            Job.select()
            .where(
                (Job.module == module)
                & (Job.status.in_(("queued", "running", "waiting_for_human")))
            )
            .exists()
        )
        if busy:
            raise ContractError(f"{module.technical_name} is running. Stop it first.")
        directory = module_root(module.migration, module, "new")
    if directory is None or not Path(directory).is_dir():
        raise ContractError(f"{module.technical_name} has no target directory.")

    return module, Path(directory)


def discard_hand_edits(migration_id: str, module_id: int) -> None:
    """Put the module's directory back to the last commit, and nothing else.

    Both commands are limited to `.` from inside the module's directory, so
    a module beside it keeps whatever it holds. `restore` covers what is
    changed or staged; `clean` removes the files git does not know, which a
    checkout alone leaves behind — and then the directory still reads as
    edited. Ignored files stay.
    """
    module, directory = _editable(migration_id, module_id)
    if changed_files(directory) is None:
        raise ContractError(
            f"{module.technical_name} is not in a git repository; there is no "
            "commit to go back to."
        )
    for command in (
        ["git", "restore", "--source=HEAD", "--staged", "--worktree", "--", "."],
        ["git", "clean", "-fdq", "--", "."],
    ):
        answer = subprocess.run(
            command, cwd=directory, capture_output=True, text=True, timeout=60, check=False
        )
        if answer.returncode != 0:
            raise ContractError(
                f"{' '.join(command[:2])} failed in {directory}: {answer.stderr.strip()}"
            )


def keep_hand_edits(migration_id: str, module_id: int) -> None:
    """Take the directory as it is: the next run starts from these files,
    and is handed what a person committed since the last port."""
    module, directory = _editable(migration_id, module_id)
    commits = person_commits(directory)
    with database.connection_context():
        record_fingerprint(module)
        module.kept_commit = commits[0]["sha"] if commits else module.kept_commit
        module.save()


def record_fingerprint(module: Module) -> None:
    """Remember what this run left in the module's directory. The caller
    holds the connection."""
    directory = module_root(module.migration, module, "new")
    module.port_fingerprint = fingerprint(directory) if directory is not None else ""
