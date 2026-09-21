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
