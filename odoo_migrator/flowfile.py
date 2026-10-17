"""A flow file, as text and as an outline of it.

The viewer reads this, and an editor will write through it: every outline
node carries the path a write would address and the lines it covers, so the
two never have to agree on anything but those.
"""

from __future__ import annotations

import yaml

from odoo_migrator.db import database
from odoo_migrator.models import CartridgeInstall
from odoo_migrator.paths import cartridges_dir

_PREVIEW = 60


def flow_text(cartridge_id: str, version: str) -> str | None:
    """The file as the app holds it, or None when there is no such flow.

    An installed flow is the text that was imported. The stock flow is the
    file that ships with the app, and is there while nothing is installed
    under its name, as `pinned_for` takes it.
    """
    with database.connection_context():
        row = CartridgeInstall.get_or_none(
            (CartridgeInstall.cartridge_id == cartridge_id)
            & (CartridgeInstall.version == version)
        )
        body = row.body if row is not None else ""
    if body:

        return body
    path = cartridges_dir() / "stock.yaml"
    if cartridge_id != "stock" or not path.is_file():

        return None
    text = path.read_text(encoding="utf-8")
    try:
        shipped = yaml.safe_load(text)
    except yaml.YAMLError:

        return None
    if not isinstance(shipped, dict) or str(shipped.get("version")) != version:

        return None

    return text


def flow_outline(text: str) -> list[dict]:
    """The top-level keys of the file as a tree, empty when it is not YAML."""
    try:
        root = yaml.compose(text)
    except yaml.YAMLError:

        return []
    if not isinstance(root, yaml.MappingNode):

        return []

    return _pairs(root, [], text.splitlines())


def _pairs(node: yaml.MappingNode, path: list, lines: list[str]) -> list[dict]:

    return [
        _node(value, [*path, key.value], str(key.value), key.start_mark.line, lines)
        for key, value in node.value
    ]


def _node(value, path: list, label: str, first: int, lines: list[str]) -> dict:
    kind = "scalar"
    children: list[dict] = []
    preview = ""
    if isinstance(value, yaml.ScalarNode):
        preview = _short(value.value)
    elif isinstance(value, yaml.MappingNode):
        kind = "map"
        children = _pairs(value, path, lines)
    else:
        kind = "seq"
        if all(isinstance(item, yaml.ScalarNode) for item in value.value):
            preview = _short(", ".join(str(item.value) for item in value.value))
        else:
            children = [_item(item, [*path, index], index, lines) for index, item in enumerate(value.value)]

    return {
        "path": path,
        "label": label,
        "kind": kind,
        "preview": preview,
        "lines": [first + 1, _last(value, first, lines)],
        "children": children,
    }


def _item(item, path: list, index: int, lines: list[str]) -> dict:
    node = _node(item, path, _item_label(item, index), item.start_mark.line, lines)

    return node


def _item_label(item, index: int) -> str:
    """A pipeline step is named by its kind and id; anything else by place."""
    if isinstance(item, yaml.MappingNode):
        fields = {
            key.value: value.value
            for key, value in item.value
            if isinstance(value, yaml.ScalarNode)
        }
        if "step" in fields:

            return f"{fields['step']} · {fields['id']}" if fields.get("id") else str(fields["step"])

    return f"[{index}]"


def _last(value, first: int, lines: list[str]) -> int:
    """The last line the value covers, 1-based and inclusive.

    A block ends where the next token starts, so the blank lines and the
    comments that lead into the next key would otherwise belong to this one.
    """
    end = value.end_mark
    # An end mark with only indentation before it is the start of the next
    # token's line, so that line is not this value's.
    at_line_start = end.line >= len(lines) or not lines[end.line][: end.column].strip()
    last = end.line if at_line_start else end.line + 1
    last = min(max(last, first + 1), len(lines))
    while last > first + 1 and _is_gap(lines[last - 1]):
        last -= 1

    return last


def _is_gap(line: str) -> bool:
    stripped = line.strip()

    return not stripped or stripped.startswith("#")


def _short(text: str) -> str:
    line = next((row.strip() for row in str(text).splitlines() if row.strip()), "")

    return line if len(line) <= _PREVIEW else line[: _PREVIEW - 1] + "…"
