"""A side is `source` or `target`. An agent must not have to guess which is which."""

import re
from pathlib import Path

import pytest

from odoo_migrator.runner.cartridge import SIDES, CartridgeError, Step, load_cartridge
from odoo_migrator.runner.tools import TOOL_DESCRIPTIONS, TOOL_SIDES, ToolRefused, _require_side

STOCK = Path("cartridges/stock.yaml").read_text(encoding="utf-8")


def test_the_sides_are_source_and_target():
    assert SIDES == frozenset({"source", "target"})
    assert {side for sides in TOOL_SIDES.values() for side in sides} == {"source", "target"}


def test_a_flow_that_says_old_is_refused_and_told_the_word_to_use():
    text = STOCK.replace("sessions: [source, target]", "sessions: [old, target]", 1)

    with pytest.raises(CartridgeError, match=r"unknown session 'old'.*'source' and 'target'"):
        load_cartridge(text)


def test_the_stock_flow_and_the_shell_notes_never_name_a_side_old_or_new():
    for name in ("cartridges/stock.yaml", "prompts/sheller.md"):
        text = Path(name).read_text(encoding="utf-8")

        assert not re.search(r"side\s*=\s*['\"](old|new)['\"]", text), name
        assert not re.search(r"sessions:\s*\[[^\]]*\b(old|new)\b", text), name


def test_a_side_the_step_did_not_declare_names_the_declared_ones():
    step = Step(step="agent", id="x", sessions=["source"], tools=["exec"], prompt="p")

    with pytest.raises(ToolRefused, match=r"declared: 'source'.*'source' \(the version"):
        _require_side(step, "exec", "target")


# A side named the old way, in any of the shapes the prompts used: a bold
# label, a bracketed tag, `side=old`, or the word before a noun that is a side.
_OLD_WORDS = re.compile(
    r"\*\*(?:old|new)\*\*"
    r"|\((?:old|new)\)"
    r"|\bside\s*[=:]\s*['\"]?(?:old|new)\b"
    r"|\b(?:old|new)\s+(?:side|container|session|project|version|Odoo|module|one)\b",
    re.IGNORECASE,
)


def _what_an_agent_reads():
    texts = {
        "prompts/system.md": Path("prompts/system.md").read_text(encoding="utf-8"),
        "prompts/sheller.md": Path("prompts/sheller.md").read_text(encoding="utf-8"),
        "prompts/test_guide.md": Path("prompts/test_guide.md").read_text(encoding="utf-8"),
    }
    flow = load_cartridge(STOCK)
    for step in (*flow.pipeline, *flow.on_failure.pipeline):
        texts[f"stock.yaml step {step.id or step.kind}"] = step.prompt
    for name, text in TOOL_DESCRIPTIONS.items():
        texts[f"tool description {name}"] = text

    return texts


def test_nothing_an_agent_reads_names_a_side_old_or_new():
    found = {
        where: match.group(0)
        for where, text in _what_an_agent_reads().items()
        for match in _OLD_WORDS.finditer(text)
    }

    assert found == {}
