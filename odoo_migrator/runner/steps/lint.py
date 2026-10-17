"""Manifest, Python and XML checks. No container: that is why this runs first."""

from __future__ import annotations

import ast
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from odoo_migrator.module_paths import module_root
from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext, StepResult

# A manifest Odoo will not load is missing its name. `depends` is how the
# graph is read; a file without it is not a module manifest.
_REQUIRED = ("name", "depends")
# `<series>.x.y.z`, the five-part form. Odoo prepends the running series to a
# version that does not start with it, so `17.0.1.0.0` on 18.0 loads as
# `18.0.17.0.1.0.0` — and migration scripts compare against that.
_SERIES_VERSION = re.compile(r"^(\d+\.\d+)\.\d+\.\d+\.\d+$")


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
            problems.extend(_python(path, relative, ctx.migration.target_version or ""))
        elif path.suffix == ".xml":
            problems.extend(_xml(path, relative))
    if problems:

        return StepResult(ok=False, flags={"lint_failed"}, output="\n".join(problems))

    return StepResult(ok=True)


def _module_root(ctx: StepContext) -> Path | None:

    return module_root(ctx.migration, ctx.module, "target")


def _python(path: Path, relative: str, target: str = "") -> list[str]:
    # A file that is not UTF-8 is a finding. It used to raise out of the
    # step and fail the whole job instead of the lap.
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:

        return [f"{relative}: not UTF-8 ({exc.reason} at byte {exc.start})"]
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
    problems = []
    if value.get("installable") is False:
        problems.append(f"{relative}: installable is False, so Odoo will not install it")
    found = _SERIES_VERSION.match(str(value.get("version") or ""))
    if target and found and found.group(1) != target:
        problems.append(
            f"{relative}: version {value['version']} is for {found.group(1)}; "
            f"the target is {target}"
        )

    return problems


def _xml(path: Path, relative: str) -> list[str]:
    try:
        ET.parse(path)
    except ET.ParseError as exc:

        return [f"{relative}: {exc}"]

    return []
