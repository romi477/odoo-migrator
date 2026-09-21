"""Project-root file access for the rewrite stage. Escape is 403, never a clamp."""

from pathlib import Path


class PathEscapes(Exception):
    """The path left the configured old or new project root."""


def resolve_in_root(root: Path, relative: str) -> Path:
    """Resolve ``relative`` under ``root``, or raise :class:`PathEscapes`.

    A path that would leave the root is refused, not silently clipped back
    inside. ``..`` in the request is enough, even when the resolved location
    would still have been under the root — clamping ``a/../b`` into ``b`` is
    how an escape becomes a successful write.
    """
    if relative is None:
        raise PathEscapes("")
    text = relative.strip()
    if text.startswith(("/", "\\")):
        raise PathEscapes(relative)
    parts = Path(text).parts
    if ".." in parts:
        raise PathEscapes(relative)
    root_resolved = root.resolve()
    candidate = (root_resolved / text).resolve()
    if not candidate.is_relative_to(root_resolved):
        raise PathEscapes(relative)

    return candidate


def read_text(path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)

    return path.read_text(encoding="utf-8")


def write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
