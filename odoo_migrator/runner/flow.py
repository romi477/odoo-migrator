"""The drawing of a cartridge. Derived on read; nothing about it is stored."""

from __future__ import annotations

from itertools import pairwise

from odoo_migrator.runner.cartridge import Cartridge


def flow_graph(cartridge: Cartridge) -> dict:
    nodes: list[dict] = []
    edges: list[dict] = []
    _lane(cartridge.pipeline, "main", nodes, edges)
    failure = _lane(cartridge.on_failure.pipeline, "failure", nodes, edges)
    resume = cartridge.on_failure.retry.resume_at
    for node_id in failure:
        edges.append({"from": node_id, "to": resume, "kind": "retry"})

    return {"nodes": nodes, "edges": edges}


def _lane(steps, lane: str, nodes: list[dict], edges: list[dict]) -> list[str]:
    ids = []
    for step in steps:
        node_id = step.id or step.kind
        ids.append(node_id)
        nodes.append(
            {
                "id": node_id,
                "kind": step.kind,
                "lane": lane,
                "gated_on": step.when,
            }
        )
    for left, right in pairwise(ids):
        edges.append({"from": left, "to": right, "kind": "next"})

    return ids
