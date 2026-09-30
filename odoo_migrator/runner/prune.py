"""Remove the old version's migration scripts from a ported module.

Odoo runs `migrations/<version>/` (and `upgrades/<version>/`) when a module
moves to that version. The old series' scripts were written for the old
framework. Worse, a short version such as `1.0.3` is read against the
module's own series, which after the port is the new one: the script would
run again, on a framework it was never written for. Only the target
series' directories are kept — scripts written for this migration.
"""

from __future__ import annotations

import shutil
from pathlib import Path

SCRIPT_DIRS = ("migrations", "upgrades")


def prune_old_migrations(module_dir: Path, target_version: str) -> list[str]:
    """Delete every version directory not of `target_version`'s series.

    Returns what was removed, relative to the module. A scripts directory
    left with nothing but an `__init__.py` goes too.
    """
    root = Path(module_dir)
    series = f"{target_version}."
    removed: list[str] = []
    for name in SCRIPT_DIRS:
        scripts = root / name
        if not scripts.is_dir() or scripts.is_symlink():
            continue
        for child in sorted(scripts.iterdir()):
            if not child.is_dir() or child.is_symlink() or child.name.startswith(series):
                continue
            if child.name == "__pycache__":
                continue
            shutil.rmtree(child)
            removed.append(f"{name}/{child.name}")
        left = [child for child in scripts.iterdir() if child.name not in ("__init__.py", "__pycache__")]
        if not left:
            shutil.rmtree(scripts)

    return removed
