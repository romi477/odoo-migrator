"""Load and publish the stock cartridge."""

from __future__ import annotations

import json
from pathlib import Path

from odoo_migrator.paths import repo_dir

WEBHOOK_PATH = "migrate-module"
CHILDREN = (
    "sheller-close-session",
    "sheller-open-session",
    "sheller-upgrade-module",
    "sheller-run-tests",
    "progress",
    "callback-result",
    "agent-understand",
    "agent-rewrite",
    "agent-diagnose",
    "agent-reprobe",
)
PARENT = "migrate-one-module"
WRAPPERS = (
    "migrate-module-openai",
    "migrate-module-claude",
)


def stock_dir() -> Path:

    return repo_dir() / "n8n" / "stock"


def load_named(name: str) -> dict:
    path = stock_dir() / f"{name}.json"

    return json.loads(path.read_text(encoding="utf-8"))


def _bind(value, ids: dict[str, str]):
    if isinstance(value, str) and value.startswith("stock:"):
        name = value.split(":", 1)[1]

        return ids[name]
    if isinstance(value, dict):

        return {key: _bind(item, ids) for key, item in value.items()}
    if isinstance(value, list):

        return [_bind(item, ids) for item in value]

    return value


def publish_stock(n8n) -> dict:
    """Create every sub-workflow, then activate children, the shared parent,
    and the model wrappers last.

    On 2.39.9 a parent whose Execute Workflow points at an unpublished child
    is 400 and the production webhook stays 404. Wrappers execute the parent,
    so they activate after it.
    """
    n8n.wait_until_ready()
    ids: dict[str, str] = {}
    for name in CHILDREN:
        created = n8n.create_workflow(_bind(load_named(name), ids))
        ids[name] = created["id"]
    parent = _bind(load_named(PARENT), ids)
    created = n8n.create_workflow(parent)
    ids[PARENT] = created["id"]
    for name in WRAPPERS:
        created = n8n.create_workflow(_bind(load_named(name), ids))
        ids[name] = created["id"]
    for name in (*CHILDREN, PARENT, *WRAPPERS):
        n8n.activate(ids[name])

    return {
        "parent_id": ids[PARENT],
        "webhook_path": WEBHOOK_PATH,
        "ids": ids,
    }
