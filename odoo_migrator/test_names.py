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
    """What each `test_*` method under `directory` expects, by `Class.method`.

    An expectation is an assertion reduced to what a port must not quietly
    change: which assertion it is and the literal values in it, in order —
    `assertEqual('blue')` for `self.assertEqual(order.colour, 'blue')`. The
    expressions around them are left out, and so are the strings that name
    things rather than expect them: a field in a domain or a subscript, a key
    of an `assertRecordValues` row, the argument of `mapped` or `ref`. A field
    renamed for the target version is not a change; a value that became
    another one, or an assertion that went, is. `assertRaises` keeps the
    exception it names.

    Keyed by class and method, so two classes with a `test_create` each are
    two entries; `name` is the method's own. A `# migrator: target-differs`
    line explains the assertion under it and no other: `explained` holds what
    those assertions expect. One that stands over no assertion, where the port
    took one out, explains one that went: `free` counts them. `marked` is the
    lines, for the review.
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
        for owner, node in _test_methods(tree):
            key = f"{owner}.{node.name}" if owner else node.name
            found[key] = {"name": node.name, **_read_method(node, lines)}

    return found


def _test_methods(tree, owner: str = ""):
    """`(class name, function)` for each `test_*` method, classes nested or not."""
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.ClassDef):
            inner = f"{owner}.{node.name}" if owner else node.name
            yield from _test_methods(node, inner)
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
            "test_"
        ):
            yield owner, node


def _read_method(function, lines: list[str]) -> dict:
    expects: Counter = Counter()
    explained: Counter = Counter()
    used: set[int] = set()
    for node, said in _expectations(function):
        expects[said] += 1
        marker = _marker_over(node.lineno, lines)
        if marker is not None:
            explained[said] += 1
            used.add(marker)
    # A comment after the last statement is the method's too, when it is
    # indented under the `def`: where the port took out its last assertion.
    end = function.end_lineno or function.lineno
    while (
        end < len(lines)
        and lines[end].strip().startswith("#")
        and len(lines[end]) - len(lines[end].lstrip()) > function.col_offset
    ):
        end += 1
    body = range(function.lineno - 1, end)
    markers = [index for index in body if index < len(lines) and MARKER in lines[index]]

    return {
        "expects": expects,
        "explained": explained,
        "free": len([index for index in markers if index not in used]),
        "marked": [lines[index].strip() for index in markers],
    }


def _marker_over(lineno: int, lines: list[str]) -> int | None:
    """The index of the marker on the assertion's first line, or in the
    comment block right above it; None when there is none."""
    index = lineno - 1
    if index < len(lines) and MARKER in lines[index]:

        return index
    index -= 1
    while index >= 0 and lines[index].strip().startswith("#"):
        if MARKER in lines[index]:

            return index
        index -= 1

    return None


def _expectations(function) -> list[tuple[ast.AST, str]]:
    said = []
    for node in ast.walk(function):
        if isinstance(node, ast.Assert):
            said.append((node, f"assert({', '.join(_literals(node.test))})"))
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
            said.append((node, f"{node.func.attr}({', '.join(parts)})"))

    return said


# A domain leaf's operator: the string before it is a field, not a value.
_OPERATORS = frozenset(
    {
        "=", "!=", "<>", "<", ">", "<=", ">=", "in", "not in", "like", "ilike",
        "not like", "not ilike", "=like", "=ilike", "child_of", "parent_of", "=?",
        "any", "not any",
    }
)
# Methods whose string arguments name fields, paths or records.
_NAMING = frozenset(
    {
        "ref", "mapped", "filtered", "sorted", "read", "search_read", "read_group",
        "_read_group", "fields_get", "default_get", "grouped",
    }
)


def _literals(node) -> list[str]:
    found: list[str] = []
    _collect(node, found)

    return found


def _collect(node, found: list[str]) -> None:
    if isinstance(node, ast.Constant):
        if node.value is not Ellipsis:
            found.append(repr(node.value))

        return
    if isinstance(node, ast.Subscript):
        # `env['res.partner']`, `record['state']`, `records[0]`: what is
        # looked up, not what is expected.
        _collect(node.value, found)

        return
    if isinstance(node, ast.Dict):
        for value in node.values:
            _collect(value, found)

        return
    if (
        isinstance(node, ast.Tuple | ast.List)
        and len(node.elts) == 3
        and isinstance(node.elts[1], ast.Constant)
        and node.elts[1].value in _OPERATORS
    ):
        _collect(node.elts[2], found)

        return
    if isinstance(node, ast.Call):
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
        if name in _NAMING:
            if isinstance(func, ast.Attribute):
                _collect(func.value, found)
            for argument in node.args:
                if not (isinstance(argument, ast.Constant) and isinstance(argument.value, str)):
                    _collect(argument, found)
            for keyword in node.keywords:
                _collect(keyword.value, found)

            return
    for child in ast.iter_child_nodes(node):
        _collect(child, found)
