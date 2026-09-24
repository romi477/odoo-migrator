"""What a step did to the module's files on the target side.

The agent writes through the `write_file` tool, so nothing else knows a file
changed. Without this the `previous_diff` context item is always empty, and
`patch` — the step whose whole difference from `rewrite` is being handed the
previous attempt — is handed nothing.
"""

from __future__ import annotations

from pathlib import Path

from odoo_migrator.diffs import unified

# A module is source, not data, but a stray import or a build artefact in the
# tree should not turn one diff into megabytes.
MAX_FILE_BYTES = 512 * 1024
SKIP_PARTS = {"__pycache__", ".git", "node_modules"}


def snapshot(root: Path | None) -> dict[str, str]:
    """Every readable text file under ``root``, keyed by its relative path."""
    if root is None or not root.is_dir():

        return {}
    taken: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or SKIP_PARTS & set(path.parts):
            continue
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
