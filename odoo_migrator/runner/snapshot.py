"""What a step did to the module's files on the target side.

The agent writes through the `write_file` tool, so nothing else knows a file
changed. Without this the `previous_diff` context item is always empty, and
`patch` — the step whose whole difference from `rewrite` is being handed the
previous attempt — is handed nothing.
"""

from __future__ import annotations

import os
from pathlib import Path

from odoo_migrator.diffs import unified

# A module is source, not data, but a stray import or a build artefact in the
# tree should not turn one diff into megabytes.
MAX_FILE_BYTES = 512 * 1024
SKIP_PARTS = {"__pycache__", ".git", "node_modules"}


def module_files(root: Path) -> list[Path]:
    """Every file under ``root``, sorted, without the directories above.

    Pruned, not filtered: a `.git` inside the module holds thousands of
    objects, and walking each one to drop it cost more than the module. The
    names are matched below ``root`` only; matched against the whole path, a
    project kept under a directory called `node_modules` had no files at all.
    """
    found: list[Path] = []
    for directory, subdirectories, files in os.walk(root):
        subdirectories[:] = [name for name in subdirectories if name not in SKIP_PARTS]
        found.extend(
            path for path in (Path(directory) / name for name in files) if path.is_file()
        )

    return sorted(found)


def snapshot(root: Path | None) -> dict[str, str]:
    """Every readable text file under ``root``, keyed by its relative path."""
    if root is None or not root.is_dir():

        return {}
    taken: dict[str, str] = {}
    for path in module_files(root):
        try:
            if path.stat().st_size > MAX_FILE_BYTES:
                continue
            taken[path.relative_to(root).as_posix()] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue

    return taken


def diff(before: dict[str, str], after: dict[str, str]) -> str:
    """A unified diff over both snapshots, including files added or removed."""
    hunks = []
    for name in sorted(set(before) | set(after)):
        hunk = unified(name, before.get(name, ""), after.get(name, ""))
        if hunk:
            hunks.append(hunk)

    return "".join(hunks)
