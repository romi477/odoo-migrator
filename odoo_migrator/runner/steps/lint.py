"""Manifest, Python and XML checks. No container: that is why this runs first."""

from __future__ import annotations

import ast
import xml.etree.ElementTree as ET
from pathlib import Path

from odoo_migrator.module_paths import module_root
from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext, StepResult

# A manifest Odoo will not load is missing its name. `depends` is how the
# graph is read; a file without it is not a module manifest.
_REQUIRED = ("name", "depends")


def run(step: Step, ctx: StepContext) -> StepResult:
    root = _module_root(ctx)
    if root is None or not root.is_dir():

        return StepResult(ok=False, flags={"lint_failed"}, output=f"no module directory at {root}")
    problems: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        if path.suffix == ".py":
            problems.extend(_python(path, relative))
        elif path.suffix == ".xml":
            problems.extend(_xml(path, relative))
    if problems:

        return StepResult(ok=False, flags={"lint_failed"}, output="\n".join(problems))

    return StepResult(ok=True)


def _module_root(ctx: StepContext) -> Path | None:

    return module_root(ctx.migration, ctx.module, "new")


def _python(path: Path, relative: str) -> list[str]:
    text = path.read_text(encoding="utf-8")
    try:
        ast.parse(text, filename=relative)
    except SyntaxError as exc:

        return [f"{relative}: {exc.msg}"]
    if path.name != "__manifest__.py":

        return []
    try:
        value = ast.literal_eval(text)
    except (SyntaxError, ValueError) as exc:

        return [f"{relative}: {exc}"]
    if not isinstance(value, dict):

        return [f"{relative}: manifest must be a dict"]
    missing = [key for key in _REQUIRED if key not in value]
    if missing:

        return [f"{relative}: missing {', '.join(missing)}"]

    return []


def _xml(path: Path, relative: str) -> list[str]:
    try:
        ET.parse(path)
    except ET.ParseError as exc:

        return [f"{relative}: {exc}"]

    return []
