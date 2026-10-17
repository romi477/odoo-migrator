"""Which test methods a directory defines.

What the migrator proved green on the source has to arrive on the target, and
the way to see that it did is by name: a port may reshape a test's body for the
target version, and keeps the method it is.
"""

from __future__ import annotations

import ast
from pathlib import Path


def test_method_names(directory) -> set[str]:
    """Every function named `test_*` in the `.py` files under `directory`.

    A file that does not parse has no names and does not stop the others: it
    cannot be run either, so what it would have defined is missing.
    """
    root = Path(directory)
    if not root.is_dir():

        return set()
    found: set[str] = set()
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        found.update(
            node.name
            for node in ast.walk(tree)
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name.startswith("test_")
        )

    return found
