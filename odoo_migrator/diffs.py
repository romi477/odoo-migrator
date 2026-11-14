"""A unified diff of one file, before and after.

The native runner snapshots the module around each step and records what it
wrote on the job, per attempt (`Job.file_diffs`); the checkpoint shows it.
"""

from __future__ import annotations

from difflib import unified_diff


def unified(path: str, before: str, after: str) -> str:
    if before == after:

        return ""

    return "".join(
        unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )
