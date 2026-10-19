"""Which test methods a directory defines, and what each one expects.

What the migrator proved green on the source has to arrive on the target, and
the way to see that it did is by name: a port may reshape a test's body for the
target version, and keeps the method it is. It keeps what the method expects,
too, unless it says why not: a port that turned an expected 'blue' into 'green'
made a red test green and proved nothing.
"""

from __future__ import annotations

import ast
from collections import Counter
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


MARKER = "# migrator: target-differs"


def test_expectations(directory) -> dict[str, dict]:
    """What each `test_*` method under `directory` expects, by method name.

    An expectation is an assertion reduced to what a port must not quietly
    change: which assertion it is and the literal values in it, in order —
    `assertEqual('blue')` for `self.assertEqual(order.colour, 'blue')`. The
    expressions around them are left out, so a field renamed for the target
    version is not a change; a value that became another one, or an assertion
    that went, is. `assertRaises` keeps the exception it names.

    Each method also carries the `# migrator: target-differs` lines in its
    body: a change the port explained.
    """
    root = Path(directory)
    if not root.is_dir():

        return {}
    found: dict[str, dict] = {}
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        try:
            text = path.read_text(encoding="utf-8")
            tree = ast.parse(text)
        except (SyntaxError, UnicodeDecodeError, OSError):
            continue
        lines = text.splitlines()
        for node in ast.walk(tree):
            if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                continue
            if not node.name.startswith("test_"):
                continue
            body = lines[node.lineno - 1 : node.end_lineno]
            found[node.name] = {
                "expects": Counter(_expectations(node)),
                "marked": [line.strip() for line in body if MARKER in line],
            }

    return found


def _expectations(function) -> list[str]:
    said = []
    for node in ast.walk(function):
        if isinstance(node, ast.Assert):
            said.append(f"assert({', '.join(_literals(node.test))})")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr.startswith("assert")
        ):
            parts = []
            if node.func.attr.startswith("assertRaises") and node.args:
                parts.append(ast.unparse(node.args[0]))
                rest = node.args[1:]
            else:
                rest = node.args
            for argument in rest:
                parts.extend(_literals(argument))
            said.append(f"{node.func.attr}({', '.join(parts)})")

    return said


def _literals(node) -> list[str]:

    return [
        repr(each.value)
        for each in ast.walk(node)
        if isinstance(each, ast.Constant) and each.value is not Ellipsis
    ]
