"""The drawing of a cartridge. Derived on read; nothing about it is stored."""

from __future__ import annotations

from itertools import pairwise

from odoo_migrator.runner.cartridge import Cartridge

MAIN_LANE = "lane:pipeline"
FAILURE_LANE = "lane:on_failure"

# What a hover says. Stock ids are the flow people see; another cartridge
# falls back to the kind, because its prompt is too long for a tooltip.
_HINTS = {
    "understand": (
        "Reads the module on the old Odoo through a live session. Describes "
        "current behaviour and what will break, then builds a fixture recipe "
        "and rolls it back. Later steps replay that recipe."
    ),
    "port": (
        "Writes the ported module into the new project. Both sessions stay "
        "open. It sees the version rules, the module source, and the "
        "understand answer."
    ),
    "lint": (
        "No container. Checks that the new module's Python parses, its XML "
        "parses, and its manifest is a dict with name and depends."
    ),
    "upgrade": (
        "Installs the module on the new container, or upgrades it if it is "
        "already installed, then commits. The old container is never committed."
    ),
    "tests": (
        "Runs the module's own test classes on the new container. No classes "
        "is a success that raises no_tests. A failing test leaves the pipeline."
    ),
    "probe": (
        "Only when the module has no tests. Replays the understand recipe on "
        "both versions and reports every difference."
    ),
    "write_test": (
        "Only when the module has no tests. Turns the replayed recipe into a "
        "real test in the ported module and runs it."
    ),
    "verdict": (
        "Judges the lap. At strictness strict it fails when probe left no "
        "answer. basic and enhanced add no check of their own."
    ),
    "diagnose": (
        "Works out why the lap failed, from the log, the journal, the source, "
        "and a live session on the new container. The flag it raises opens "
        "one block below: ask a human, look at the old version again, patch, "
        "or rewrite."
    ),
    "ask_human": (
        "Parks the job. The question is the diagnosis. Both sessions close "
        "while it waits. Answer on Tree, and the same run continues."
    ),
    "reprobe": (
        "Goes back to the old version with the question the diagnosis raised."
    ),
    "patch": (
        "Applies the named fix on the new side. It has the previous diff and "
        "changes what is wrong, leaving the rest."
    ),
    "rewrite": (
        "Writes the module again from the old version. Its directory on the "
        "new side is emptied first, and it is not given the previous attempt."
    ),
}
_KIND_HINTS = {
    "agent": "A model turn. It uses this step's tools, then reports an answer.",
    "lint": _HINTS["lint"],
    "upgrade": _HINTS["upgrade"],
    "tests": _HINTS["tests"],
    "verdict": _HINTS["verdict"],
    "ask_human": _HINTS["ask_human"],
}


def flow_graph(cartridge: Cartridge) -> dict:
    """Two lanes, and the two edges between them.

    The failure lane is entered when *any* pipeline step fails, and it is
    left once, after the whole lane has run — so both edges belong to the
    lanes and not to their steps. This used to emit one retry edge per
    failure step, which drew five arrows into `lint` for a jump the loop
    makes once, and drew no way into the failure lane at all.
    """
    nodes: list[dict] = [_lane(MAIN_LANE, "pipeline", "main")]
    edges: list[dict] = []
    lane = cartridge.on_failure.pipeline
    if lane:
        nodes.append(_lane(FAILURE_LANE, "on failure", "failure"))
    _steps(cartridge.pipeline, "main", MAIN_LANE, nodes, edges)
    if not lane:

        return {"nodes": nodes, "edges": edges}
    _steps(lane, "failure", FAILURE_LANE, nodes, edges)
    edges.append(
        {
            "from": MAIN_LANE,
            "to": FAILURE_LANE,
            "kind": "failure",
            "label": "any step fails",
        }
    )
    edges.append(
        {
            "from": FAILURE_LANE,
            "to": cartridge.on_failure.retry.resume_at,
            "kind": "retry",
            "label": "retry",
        }
    )

    return {"nodes": nodes, "edges": edges}


def _hint(step) -> str:
    named = _HINTS.get(step.id or "")
    if named:

        return named
    text = _KIND_HINTS.get(step.kind, "This step runs as part of the flow.")
    if step.when:

        return f"Runs only if {step.when} was raised. {text}"

    return text


def _lane(node_id: str, label: str, lane: str) -> dict:

    return {
        "id": node_id,
        "label": label,
        "kind": "lane",
        "lane": lane,
        "parent": None,
        "gated_on": None,
        "hint": "",
        "actor": "",
        "sessions": [],
        "tools": [],
        "context": [],
        "raises": [],
        "prompt": "",
    }


def _steps(steps, lane: str, parent: str, nodes: list[dict], edges: list[dict]) -> None:
    """One node per step, and the edge to the step after it.

    A gated step keeps its flag on the node: drawn without it, `probe` reads
    as part of every run and `patch` reads as something that happens before
    `rewrite` rather than instead of it.
    """
    ids = []
    for step in steps:
        node_id = step.id or step.kind
        ids.append(node_id)
        nodes.append(
            {
                "id": node_id,
                "label": node_id,
                "kind": step.kind,
                "lane": lane,
                "parent": parent,
                "gated_on": step.when,
                "hint": _hint(step),
                "actor": "agent" if step.kind == "agent" else "function",
                "sessions": list(step.sessions),
                "tools": list(step.tools),
                "context": list(step.context),
                "raises": list(step.raises),
                "prompt": step.prompt.strip(),
            }
        )
    for left, right in pairwise(ids):
        edges.append({"from": left, "to": right, "kind": "next"})
