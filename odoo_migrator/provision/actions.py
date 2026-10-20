"""up / restart / rebuild / stop. Ours to touch; someone else's is an error.

There is no attach: a side that runs is linked when it is seen (the
settings card, Build graph), which is all Attach did, and nobody pressed it.
"""

from dataclasses import dataclass
from pathlib import Path

from odoo_migrator.provision.compose_file import (
    build_inputs,
    find_compose_file,
    published_host_ports,
)
from odoo_migrator.provision.engine import (
    CONFIG_HASH_LABEL,
    SERVICE_LABEL,
    WORKING_DIR_LABEL,
    RunningContainer,
)
from odoo_migrator.provision.errors import ComposeMissing, NothingRunning, PortStolen


@dataclass(frozen=True)
class SideStatus:
    state: str
    container: str
    compose_file: str | None
    detail: str = ""
    # What changed after the running container, or its image, was made: the
    # files a Rebuild would take in. Empty when nothing did, or it cannot tell.
    stale: tuple[str, ...] = ()


def is_ours(container: RunningContainer, project_path: Path) -> bool:
    labeled = container.labels.get(WORKING_DIR_LABEL)
    if not labeled:

        return False

    return Path(labeled).resolve() == project_path.resolve()


def looks_like_odoo(container: RunningContainer) -> bool:
    """Only ever a hint, never a count.

    Matching "odoo" in the name or image calls a container named
    ``odoo-postgres`` an Odoo server, and on a real host it did. It stays
    only to order the candidates inside one compose project, where the
    alternative is picking arbitrarily.
    """

    return "odoo" in f"{container.name} {container.image}".lower()


@dataclass(frozen=True)
class ResourceCount:
    """What is running, split by what we can actually attribute.

    ``ours`` is exact: a container carries the compose working directory it
    was started from, and we compare it against the project paths of the
    migrations this app knows. ``total`` is every running container on the
    machine. Neither number guesses what software is inside — the previous
    count did, and reported a Postgres container as Odoo.
    """

    ours: int
    total: int


def count_running(engine, project_paths) -> ResourceCount:
    wanted = {Path(path).resolve() for path in project_paths if path}
    ours = 0
    total = 0
    for container in engine.list_containers():
        if container.status != "running":
            continue
        total += 1
        labeled = container.labels.get(WORKING_DIR_LABEL)
        if labeled and Path(labeled).resolve() in wanted:
            ours += 1

    return ResourceCount(ours=ours, total=total)


def describe_side(engine, project_path: Path, *, sibling_path: Path | None = None) -> SideStatus:
    compose = find_compose_file(project_path)
    if compose is None:

        return SideStatus(
            state="missing_compose",
            container="",
            compose_file=None,
            detail="No compose file in the project. The app does not generate one.",
        )
    running = _running_ours(engine, project_path)
    if running:
        picked = _pick_odoo(running)
        if picked is None:

            return SideStatus(
                state="running",
                container="",
                compose_file=str(compose),
                detail=(
                    "This project is up, but none of its containers is "
                    "recognisably Odoo. Name the Odoo service so it can be "
                    "told apart from the database."
                ),
            )

        return SideStatus(
            state="running",
            container=picked.name,
            compose_file=str(compose),
            stale=_stale(engine, picked, compose, project_path),
        )
    thief = _thief(engine, project_path, compose)
    if thief is not None:

        return SideStatus(
            state="port_stolen",
            container="",
            compose_file=str(compose),
            detail=_stolen_detail(thief, compose, sibling_path),
        )

    return SideStatus(
        state="stopped",
        container="",
        compose_file=str(compose),
        detail="Compose file found; nothing from this project is running.",
    )


def _stale(engine, container: RunningContainer, compose: Path, project_path: Path) -> tuple[str, ...]:
    """The build inputs newer than what they built: the compose file than the
    container, the Dockerfile and what it copies than the image. A container
    recreated from an old image is not fresh because it is new. A compose
    file that is newer and gives the service the hash the container carries
    was touched, not changed: an edited comment is not a reason to rebuild.
    Never raises: a file or a time that cannot be read says nothing."""
    try:
        inputs = build_inputs(compose)
    except Exception:  # noqa: BLE001 - a hint never breaks the card.

        return ()
    image_created = None
    if inputs["image"] and container.image_id and hasattr(engine, "image_created"):
        image_created = engine.image_created(container.image_id)
    built = {"container": container.created, "image": image_created}
    changed = []
    for kind, paths in inputs.items():
        if built[kind] is None:
            continue
        for path in paths:
            try:
                newer = path.stat().st_mtime > built[kind]
            except OSError:
                continue
            if newer and kind == "container" and _same_config(engine, container, compose, project_path):
                continue
            if newer:
                changed.append(_shown(path, project_path))

    return tuple(changed)


def _same_config(engine, container: RunningContainer, compose: Path, project_path: Path) -> bool:
    service = container.labels.get(SERVICE_LABEL)
    made_with = container.labels.get(CONFIG_HASH_LABEL)
    if not service or not made_with or not hasattr(engine, "config_hash"):

        return False

    return engine.config_hash(project_path, compose, service) == made_with


def _shown(path: Path, project_path: Path) -> str:
    try:

        return path.resolve().relative_to(project_path.resolve()).as_posix()
    except ValueError:

        return str(path)


def describe_side_without_listing(project_path: Path, *, detail: str) -> SideStatus:
    """Filesystem only. Used when the Engine is down so the page still renders."""
    compose = find_compose_file(project_path)
    if compose is None:

        return SideStatus(
            state="missing_compose",
            container="",
            compose_file=None,
            detail="No compose file in the project. The app does not generate one.",
        )

    return SideStatus(
        state="unknown",
        container="",
        compose_file=str(compose),
        detail=detail,
    )


def up(engine, project_path: Path, *, sibling_path: Path | None = None) -> SideStatus:
    compose = _require_compose(project_path)
    _refuse_stolen(engine, project_path, compose, sibling_path)
    running = _running_ours(engine, project_path)
    if not running:
        engine.compose_up(project_path, compose)

    return describe_side(engine, project_path, sibling_path=sibling_path)


def restart(engine, project_path: Path, *, sibling_path: Path | None = None) -> SideStatus:
    compose = _require_compose(project_path)
    _refuse_stolen(engine, project_path, compose, sibling_path)
    if not _running_ours(engine, project_path):
        raise NothingRunning("Nothing from this project is running. Start it first.")
    engine.compose_restart(project_path, compose)

    return describe_side(engine, project_path, sibling_path=sibling_path)


def rebuild(engine, project_path: Path, *, sibling_path: Path | None = None) -> SideStatus:
    compose = _require_compose(project_path)
    _refuse_stolen(engine, project_path, compose, sibling_path)
    if not _running_ours(engine, project_path):
        raise NothingRunning("Nothing from this project is running. Start it first.")
    engine.compose_rebuild(project_path, compose)

    return describe_side(engine, project_path, sibling_path=sibling_path)


def stop(engine, project_path: Path, *, sibling_path: Path | None = None) -> SideStatus:
    """Stop this project's containers, keeping them and their volumes. A no-op
    when nothing of ours is running, so it never errors on an idle side, and
    it targets only this compose project, so a foreign container is untouched."""
    compose = _require_compose(project_path)
    if _running_ours(engine, project_path):
        engine.compose_stop(project_path, compose)

    return describe_side(engine, project_path, sibling_path=sibling_path)


def _require_compose(project_path: Path) -> Path:
    compose = find_compose_file(project_path)
    if compose is None:
        raise ComposeMissing(
            f"No compose file in {project_path}. "
            "The app does not generate one; add compose.yaml (or docker-compose.yml)."
        )

    return compose


def _running_ours(engine, project_path: Path) -> list[RunningContainer]:

    return [
        container
        for container in engine.list_containers()
        if container.status == "running" and is_ours(container, project_path)
    ]


def _pick_odoo(containers: list[RunningContainer]) -> RunningContainer | None:
    """The Odoo container of a compose project, or None.

    Returning an arbitrary container when nothing matches was worse than
    returning nothing: that name is stored as the migration's container and
    every later Sheller call is aimed at it, so a database container would
    be handed to ``odoo-bin shell``. The probe in task 7 can say for
    certain; until then, say we do not know.
    """
    for container in containers:
        if looks_like_odoo(container):

            return container

    return None


def _stolen_detail(
    thief: RunningContainer, compose_file: Path, sibling_path: Path | None
) -> str:
    """Name the real cause. The likeliest thief is this migration's own
    other side: two Odoo compose files copied from the same project both
    publish 8069, so the second side never comes up. Calling that "someone
    else's" sends the reader looking for a stranger.
    """
    shared = set(published_host_ports(compose_file)) & set(thief.host_ports)
    port = min(shared) if shared else None
    where = f"port {port}" if port is not None else "a published port"
    if sibling_path is not None and is_ours(thief, sibling_path):

        return (
            f"{thief.name} is the other side of this migration, and it uses "
            f"{where} too. Give the old and new compose files different "
            "host ports; both sides run at once."
        )

    return (
        f"{thief.name} is using {where} and is not part of this project. "
        "Stop it yourself or change the port — the app never takes a port "
        "from a container it did not start."
    )


def _thief(engine, project_path: Path, compose_file: Path) -> RunningContainer | None:
    wanted = set(published_host_ports(compose_file))
    if not wanted:

        return None
    for container in engine.list_containers():
        if container.status != "running":
            continue
        if is_ours(container, project_path):
            continue
        if wanted.intersection(container.host_ports):

            return container

    return None


def _refuse_stolen(
    engine, project_path: Path, compose_file: Path, sibling_path: Path | None = None
) -> None:
    thief = _thief(engine, project_path, compose_file)
    if thief is None:

        return
    raise PortStolen(_stolen_detail(thief, compose_file, sibling_path))
