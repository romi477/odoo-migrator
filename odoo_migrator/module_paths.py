"""Where one module's source lives on the host, on each side.

Three places used to answer this separately, and all three answered
``<project root>/<technical name>``. That is right only when the project
root is itself the addons directory. A repository that keeps its addons in
``project/`` or ``addons/`` — the common shape — resolved to a directory
that does not exist, so the prompt carried ``(no module source)``, lint
reported ``no module directory``, and the diff of a rewrite was empty.

``Module.addons_root`` cannot settle it: the graph scan reads it inside the
container, so it is a container path (``/opt/project``) and says nothing
about the bind mount on this machine.
"""

from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

from odoo_migrator.mounts import covering, load, to_container, to_host, too_broad

MANIFEST = "__manifest__.py"
# A search deep enough for repo/addons/<mod> and repo/project/<mod>, and
# shallow enough not to walk a checkout of Odoo itself.
MAX_DEPTH = 3
SKIP = {".git", ".hg", ".idea", ".vscode", "__pycache__", "node_modules", ".venv", "venv"}


def find_module_dir(root: Path | str, name: str) -> Path | None:
    """The directory of module ``name`` under ``root``, or None.

    A directory counts only when it holds a manifest: a stray folder with
    the module's name is not the module.
    """
    base = Path(root).expanduser()
    if not base.is_dir():

        return None
    for directory in _candidates(base, name):
        if (directory / MANIFEST).is_file():

            return directory

    return None


def _candidates(base: Path, name: str):
    """``base/name`` first, then one level down, then two. Breadth first,
    so ``repo/<mod>`` wins over ``repo/vendor/<mod>``."""
    level = [base]
    for _ in range(MAX_DEPTH):
        for parent in level:
            candidate = parent / name
            if candidate.is_dir():

                yield candidate
        level = [child for parent in level for child in _children(parent)]
        if not level:

            return


def _children(parent: Path) -> list[Path]:
    try:

        return sorted(
            child
            for child in parent.iterdir()
            if child.is_dir() and child.name not in SKIP and not child.name.startswith(".")
        )
    except OSError:

        return []


def host_paths(migration, technical_name: str) -> tuple[str, str]:
    """Where this module is on the old side, and where it belongs on the new.

    The new side is a decision, not a discovery: on a first port nothing is
    there yet. It mirrors the old module's position relative to its own
    root, so a project that keeps addons in ``project/`` gets the port
    written to ``project/`` and not beside it.
    """
    old_root = (migration.old_project_path or "").strip()
    new_root = (migration.new_project_path or "").strip()
    found = find_module_dir(old_root, technical_name) if old_root else None
    old_path = str(found) if found is not None else ""
    if not new_root:

        return old_path, ""
    existing = find_module_dir(new_root, technical_name)
    if existing is not None:

        return old_path, str(existing)
    if found is not None:
        relative = found.relative_to(Path(old_root).expanduser())

        return old_path, str(Path(new_root).expanduser() / relative)

    return old_path, str(Path(new_root).expanduser() / technical_name)


def module_root(migration, module, side: str) -> Path | None:
    """The one answer the runner, lint and the prompt all use."""
    if getattr(module, "path_problem", ""):

        return None
    stored = (module.old_path if side == "old" else module.new_path) or ""
    if stored:

        return Path(stored)
    old_path, new_path = host_paths(migration, module.technical_name)
    chosen = old_path if side == "old" else new_path

    return Path(chosen) if chosen else None


def relative_to_root(migration, module, side: str) -> str:
    """The module's path as ``write_file`` wants it: relative to the root.

    The tools refuse an absolute path, so a model told only the module's
    name would write to the wrong place whenever the addons sit in a
    subdirectory.
    """
    root = (migration.old_project_path if side == "old" else migration.new_project_path) or ""
    directory = module_root(migration, module, side)
    if directory is None or not root:

        return module.technical_name
    try:

        return directory.relative_to(Path(root).expanduser()).as_posix()
    except ValueError:

        return module.technical_name


def place(
    migration, name: str, container_path: str, framework: frozenset[str] = frozenset()
) -> dict:
    """Where module `name` is on each side, on the host and in the container.

    `framework` holds the old side's mount points that carry Odoo's own code
    (Enterprise, a mounted Odoo). A module kept from one of them is never
    ported into the same mount on the target — that is Odoo's checkout for
    the new version — but into the new project.

    A side whose mounts are not known answers as `host_paths` does, and
    raises no problem. A side whose mounts are known and do not place the
    module sets `path_problem`, and that side's host path is left empty:
    a guessed directory is where a port would be written and never loaded.
    """
    old_mounts = load(migration.old_mounts)
    new_mounts = load(migration.new_mounts)
    fallback_old, fallback_new = host_paths(migration, name)
    placed = {
        "old_path": fallback_old,
        "new_path": fallback_new,
        "old_container_path": "",
        "new_container_path": "",
        "path_problem": "",
    }
    old_mount = covering(old_mounts, container_path) if old_mounts and container_path else None
    if old_mounts:
        placed["old_container_path"] = container_path
        if old_mount is None:
            placed["old_path"] = ""
            placed["path_problem"] = "not mounted"

            return placed
        if too_broad(old_mount["host"]):
            placed["old_path"] = ""
            placed["path_problem"] = "mount too broad"

            return placed
        placed["old_path"] = str(to_host(old_mounts, container_path))
    if new_mounts:
        reuse = old_mount is None or old_mount["container"] not in framework
        target = _new_container_path(
            migration, new_mounts, old_mount, container_path, name, reuse=reuse
        )
        new_mount = covering(new_mounts, target) if target else None
        if new_mount is None:
            placed["new_path"] = ""
            placed["path_problem"] = "no target directory"

            return placed
        if too_broad(new_mount["host"]):
            placed["new_path"] = ""
            placed["path_problem"] = "mount too broad"

            return placed
        new_path = str(to_host(new_mounts, target))
        # One checkout mounted by both containers: porting "into the target"
        # would rewrite the source the old side runs, and a start-over would
        # empty it first.
        if placed["old_path"] and _overlap(placed["old_path"], new_path):
            placed["new_path"] = ""
            placed["path_problem"] = "shared with the old side"

            return placed
        placed["new_container_path"] = target
        placed["new_path"] = new_path

    return placed


def _project_point(mounts: list[dict], project: str) -> str | None:
    """Where the new project's addons are inside the target container.

    The project directory holds the compose file, and the addons are usually
    a directory under it (`./project:/opt/project`), so a mount *inside* the
    project counts; the one directly on it, or above it, too.
    """
    root = Path(os.path.realpath(Path(project).expanduser()))
    # Real paths on both sides: /tmp is /private/tmp on macOS, and a mount
    # typed one way would not match a project typed the other.
    mounts = [{**mount, "host": os.path.realpath(mount["host"])} for mount in mounts]
    above = to_container(mounts, str(root))
    if above is not None:

        return above
    inside = [
        mount
        for mount in mounts
        if root in Path(os.path.realpath(mount["host"])).parents
    ]
    if not inside:

        return None
    # The shallowest one: `project/` rather than `project/vendor/`.
    inside.sort(key=lambda mount: len(Path(mount["host"]).parts))

    return inside[0]["container"]


def _overlap(one: str, other: str) -> bool:
    first = Path(os.path.realpath(one))
    second = Path(os.path.realpath(other))

    return first == second or first in second.parents or second in first.parents


def _new_container_path(
    migration, new_mounts, old_mount, container_path, name, *, reuse: bool = True
) -> str:
    """Where the port goes inside the target container.

    An existing port under the new project wins. Then the same container
    directory, when the target mounts it. Then directly inside the new
    project's own mount.
    """
    project = (migration.new_project_path or "").strip()
    if project:
        existing = find_module_dir(project, name)
        if existing is not None:
            inside = to_container(new_mounts, str(existing))
            if inside:

                return inside
    if reuse and container_path and covering(new_mounts, container_path) is not None:

        return container_path
    if not project:

        return ""
    root = _project_point(new_mounts, project)
    if root is None:

        return ""
    # Straight under the project's addons directory. Addons directories are
    # flat: a module sits directly inside an addons_path entry, so keeping an
    # intermediate directory from the old side (integration-queue/) would put
    # the port where no entry reaches it.

    return str(PurePosixPath(root) / name)


def _side_mounts(migration, side: str) -> list[dict]:
    # getattr: a migration read before these columns existed, or a stand-in
    # in a test, has no mounts, which is "not known".

    return load(getattr(migration, f"{side}_mounts", "[]"))


def allowed_roots(migration, side: str) -> list[dict]:
    """The mounts the file tools may reach on `side`.

    Only the ones that hold a module of the plan. A mount of odoo.conf, the
    filestore or the logs is on the container too, and none of it is the
    agent's to read through a host tool.
    """
    from odoo_migrator.db import database
    from odoo_migrator.models import Module

    mounts = _side_mounts(migration, side)
    if not mounts:

        return []
    column = Module.old_container_path if side == "old" else Module.new_container_path
    with database.connection_context():
        paths = [
            value
            for (value,) in Module.select(column)
            .where(
                (Module.migration == migration.id)
                & (Module.struck_out == False)
                & (column != "")
            )
            .tuples()
        ]
    roots: list[dict] = []
    for path in paths:
        mount = covering(mounts, path)
        if mount is None or too_broad(mount["host"]) or mount in roots:
            continue
        roots.append(mount)

    return roots


def agent_path(migration, module, side: str) -> str:
    """The module's directory as the file tools take it on `side`.

    A container path when that side's mounts are known — the path Odoo and
    its tracebacks use — and the project-relative path otherwise.
    """
    stored = getattr(module, f"{side}_container_path", "")
    if stored and _side_mounts(migration, side):

        return stored

    return relative_to_root(migration, module, side)


def addon_directories(migration_id: str) -> dict[str, list[str]]:
    """What the card shows: the addons directories the plan's modules are in.

    The directory holding each module, which is an addons_path entry — not
    the mount it came through. A mount can aggregate several of them
    (`odoo-sh/` holding `connectors-extensions/`), and naming the mount
    read as though the modules were there.
    """
    from odoo_migrator.db import database
    from odoo_migrator.models import Migration, Module

    with database.connection_context():
        migration = Migration.get_or_none(Migration.id == migration_id)
        if migration is None:

            return {"old": [], "new": []}
        modules = list(
            Module.select().where(
                (Module.migration == migration_id) & (Module.struck_out == False)
            )
        )
    found: dict[str, list[str]] = {}
    for side in ("old", "new"):
        if not _side_mounts(migration, side):
            found[side] = []
            continue
        parents = {
            str(Path(path).parent)
            for path in (
                module.old_path if side == "old" else module.new_path for module in modules
            )
            if path
        }
        found[side] = sorted(parents)

    return found
