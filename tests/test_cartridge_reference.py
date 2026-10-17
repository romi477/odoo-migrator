"""docs/cartridge-reference.md says what the code accepts, key for key.

The page is the lookup for anyone writing a flow, and a table that drifted from
the loader once told people that `read_file` was target-only. Each table is
compared with the thing it describes, both ways: a key the code has and the page
lacks, and a row on the page the code does not know.
"""

import re
from pathlib import Path

from odoo_migrator.runner.cartridge import (
    BUILTIN_FLAGS,
    BUILTIN_READS,
    CONTEXT_ITEMS,
    KINDS,
    SCHEMA_VERSION,
    Budget,
    Cartridge,
    FailureLane,
    ModelRef,
    Retry,
    Step,
    load_cartridge_file,
)
from odoo_migrator.runner.tools import TOOL_SCHEMAS, TOOL_SIDES

REFERENCE = Path("docs/cartridge-reference.md").read_text(encoding="utf-8")


def _sections():
    """Heading -> the names in the first column of the tables under it."""
    sections = {}
    current = None
    for line in REFERENCE.splitlines():
        if line.startswith("## "):
            current = line[3:].strip()
            sections[current] = []
        elif current and line.startswith("| `"):
            first = line.split("|")[1]
            sections[current].extend(re.findall(r"`([^`]+)`", first))

    return sections


def _aliases(model):
    return {field.alias or name for name, field in model.model_fields.items()}


def test_the_top_level_keys_are_the_cartridge_models_fields():
    documented = set(_sections()["Top-level keys"])
    in_code = _aliases(Cartridge) - {"sha256"}

    assert documented == in_code


def test_the_nested_keys_are_the_nested_models_fields():
    documented = set(_sections()["Nested keys"])
    in_code = (
        {f"model.{key}" for key in _aliases(ModelRef)}
        | {f"budget.{key}" for key in _aliases(Budget)}
        | {"on_failure.pipeline", "on_failure.retry.resume_at"}
    )

    assert documented == in_code
    assert _aliases(FailureLane) == {"pipeline", "retry"}
    assert _aliases(Retry) == {"resume_at"}


def test_the_step_keys_are_the_step_models_fields():
    documented = set(_sections()["Step keys"])

    assert documented == _aliases(Step)


def test_the_step_kinds_are_the_ones_the_loader_knows():
    assert set(_sections()["Step kinds"]) == set(KINDS)


def test_the_context_items_are_the_closed_vocabulary():
    assert set(_sections()["Context items"]) == set(CONTEXT_ITEMS)


def test_the_tools_are_the_catalog_and_each_row_names_its_sides_and_arguments():
    rows = {}
    for line in REFERENCE.split("## Tools", 1)[1].split("\n## ", 1)[0].splitlines():
        if line.startswith("| `"):
            cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
            rows[re.findall(r"`([^`]+)`", cells[0])[0]] = cells

    assert set(rows) == set(TOOL_SIDES)
    for tool, cells in rows.items():
        sides = {side.strip() for side in cells[1].split(",")}
        assert sides == set(TOOL_SIDES[tool]), tool
        arguments = set(re.findall(r"`([a-z_]+)\*?`", cells[2]))
        in_code = set(TOOL_SCHEMAS.get(tool, {"properties": {"side": 0}})["properties"])
        assert arguments == in_code, tool
        required = set(re.findall(r"`([a-z_]+)\*`", cells[2]))
        wanted = set(TOOL_SCHEMAS.get(tool, {}).get("required", ["side"]))
        assert required == wanted, tool


def test_the_built_in_flags_are_the_ones_the_steps_know():
    assert set(_sections()["Flags"]) == set(BUILTIN_FLAGS) | set(BUILTIN_READS)


def test_the_flags_the_stock_cartridge_declares_are_listed_with_who_raises_and_reads_them():
    flow = load_cartridge_file(Path("cartridges/stock.yaml"))
    raised = {flag for step in (*flow.pipeline, *flow.on_failure.pipeline) for flag in step.raises}
    rows = {}
    for line in REFERENCE.split("## Flags the stock cartridge declares", 1)[1].split("\n## ", 1)[0].splitlines():
        if line.startswith("| `"):
            cells = [cell.strip().strip("`") for cell in line.strip().strip("|").split("|")]
            rows[cells[0]] = cells

    assert set(rows) == raised
    steps = [*flow.pipeline, *flow.on_failure.pipeline]
    for flag, cells in rows.items():
        assert {step.id for step in steps if flag in step.raises} == {cells[1]}, flag
        readers = {step.id or step.kind for step in steps if step.when == flag}
        # `verdict` reads `behaviour_differs` without a `when`.
        assert cells[2] in readers | {"verdict"}, flag


def test_the_page_names_the_schema_the_build_implements():
    assert f"This build implements `{SCHEMA_VERSION}`" in REFERENCE


def test_the_smallest_cartridge_on_the_page_loads():
    block = REFERENCE.split("## A smallest cartridge", 1)[1].split("```yaml\n", 1)[1].split("```", 1)[0]

    from odoo_migrator.runner.cartridge import load_cartridge

    loaded = load_cartridge(block)

    assert loaded.cartridge == "mine"
