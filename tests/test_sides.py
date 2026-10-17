"""A side is `source` or `target`. An agent must not have to guess which is which."""

import re
from pathlib import Path

import pytest

from odoo_migrator.runner.cartridge import SIDES, CartridgeError, Step, load_cartridge
from odoo_migrator.runner.tools import TOOL_SIDES, ToolRefused, _require_side

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
