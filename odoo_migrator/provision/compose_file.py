"""Locate and read the compose file the user already prepared."""

from pathlib import Path

import yaml

# Compose's own default order: compose.yaml preferred, docker-compose.* last.
COMPOSE_FILENAMES = (
    "compose.yaml",
    "compose.yml",
    "docker-compose.yaml",
    "docker-compose.yml",
)


def find_compose_file(project_path: Path) -> Path | None:
    for name in COMPOSE_FILENAMES:
        candidate = project_path / name
        if candidate.is_file():

            return candidate

    return None


def published_host_ports(compose_file: Path) -> list[int]:
    data = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
    ports: list[int] = []
    for service in (data.get("services") or {}).values():
        if not isinstance(service, dict):
            continue
        for entry in service.get("ports") or []:
            ports.extend(_host_port(entry))

    return ports


def _host_port(entry) -> list[int]:
    """Published host ports for one compose ``ports`` entry.

    Returns a list because a range publishes several, and an empty list for
    entries that publish nothing predictable. Every case here is legal
    compose that a user's file really contains:

    - ``"8069"`` — short syntax with no host side. Compose picks an
      ephemeral host port, so this publishes *no* known port. Reading it as
      host port 8069 invented a clash that was not there.
    - ``"8000-8010:8000-8010"`` — a range. ``int()`` on it raised
      ValueError, uncaught, and the settings page answered 500.
    """
    if isinstance(entry, int):

        return []
    if isinstance(entry, dict):
        published = entry.get("published")
        if published is None:

            return []

        return _expand(str(published))
    text = str(entry).split("/", 1)[0]
    parts = text.split(":")
    if len(parts) == 1:

        return []

    return _expand(parts[-2])


def _expand(value: str) -> list[int]:
    start, dash, end = value.partition("-")
    try:
        if not dash:

            return [int(start)]
        first, last = int(start), int(end)
    except ValueError:

        return []
    if last < first or last - first > 1000:

        return []

    return list(range(first, last + 1))


def build_inputs(compose_file: Path) -> dict[str, list[Path]]:
    """What a rebuild reads, by what it is compared with.

    ``container``: the compose file, which a container is created from.
    ``image``: each service's Dockerfile, and the files its COPY and ADD
    lines take from the build context or from a local additional context.
    A directory is left out: `COPY project/ …` would name every file the
    port edits. So is a source with a wildcard, a URL, or a stage or image
    of `--from`. Only what exists is listed.
    """
    found: dict[str, list[Path]] = {"container": [compose_file], "image": []}
    data = yaml.safe_load(compose_file.read_text(encoding="utf-8")) or {}
    base = compose_file.parent
    for service in (data.get("services") or {}).values():
        build = service.get("build") if isinstance(service, dict) else None
        if not build:
            continue
        if isinstance(build, str):
            build = {"context": build}
        if not isinstance(build, dict):
            continue
        context = _local(base, build.get("context") or ".")
        if context is None:
            continue
        extra = {}
        contexts = build.get("additional_contexts") or {}
        if isinstance(contexts, list):
            contexts = dict(entry.split("=", 1) for entry in contexts if "=" in str(entry))
        for name, where in contexts.items():
            local = _local(base, str(where))
            if local is not None:
                extra[str(name)] = local
        dockerfile = context / (build.get("dockerfile") or "Dockerfile")
        if not dockerfile.is_file():
            continue
        found["image"].append(dockerfile)
        for source in _copied(dockerfile, context, extra):
            if source.is_file() and source not in found["image"]:
                found["image"].append(source)

    return found


def _local(base: Path, where: str) -> Path | None:
    """A build context on this machine, or None for a URL or a git remote."""
    if "://" in where or where.startswith("git@"):

        return None

    return (base / Path(where).expanduser()).resolve()


def _copied(dockerfile: Path, context: Path, extra: dict[str, Path]) -> list[Path]:
    text = dockerfile.read_text(encoding="utf-8").replace("\\\n", " ")
    sources: list[Path] = []
    for line in text.splitlines():
        words = line.strip().split()
        if not words or words[0].upper() not in ("COPY", "ADD"):
            continue
        root = context
        rest = []
        for word in words[1:]:
            if word.startswith("--from="):
                root = extra.get(word.split("=", 1)[1])
            elif not word.startswith("--"):
                rest.append(word)
        if root is None or len(rest) < 2 or rest[0].startswith("["):
            continue
        for source in rest[:-1]:
            if "://" in source or any(mark in source for mark in "*?["):
                continue
            sources.append(root / source.lstrip("/"))

    return sources
