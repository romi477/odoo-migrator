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


def place(migration, name: str, container_path: str) -> dict:
    """Where module `name` is on each side, on the host and in the container.

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
        target = _new_container_path(migration, new_mounts, old_mount, container_path, name)
        new_mount = covering(new_mounts, target) if target else None
        if new_mount is None:
            placed["new_path"] = ""
            placed["path_problem"] = "no target directory"

            return placed
        if too_broad(new_mount["host"]):
            placed["new_path"] = ""
            placed["path_problem"] = "mount too broad"

            return placed
        placed["new_container_path"] = target
        placed["new_path"] = str(to_host(new_mounts, target))

    return placed


def _new_container_path(migration, new_mounts, old_mount, container_path, name) -> str:
    """Where the port goes inside the target container.

    An existing port under the new project wins. Then the same container
    directory, when the target mounts it. Then the new project's own mount,
    at the module's place relative to its old mount.
    """
    project = (migration.new_project_path or "").strip()
    if project:
        existing = find_module_dir(project, name)
        if existing is not None:
            inside = to_container(new_mounts, str(existing))
            if inside:

                return inside
    if container_path and covering(new_mounts, container_path) is not None:

        return container_path
    if not project:

        return ""
    root = to_container(new_mounts, str(Path(project).expanduser()))
    if root is None:

        return ""
    if old_mount is not None:
        relative = PurePosixPath(container_path).relative_to(old_mount["container"])
    else:
        relative = PurePosixPath(name)

    return str(PurePosixPath(root) / relative)
