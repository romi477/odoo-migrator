"""The prompts of the stock cartridge say what the cartridge now does.

Most of these pin a decision, not a wording: that a step is told something at
all, that guidance lives in one place, that a name the code does not know is
gone. They cannot show a prompt is good; a live run does that.
"""

from pathlib import Path
from types import SimpleNamespace

import pytest

from odoo_migrator.runner.cartridge import CONTEXT_ITEMS, load_cartridge_file
from odoo_migrator.runner.prompting import _render

FLOW = load_cartridge_file(Path("cartridges/stock.yaml"))


def _prompt(step_id):
    return " ".join(FLOW.step_by_id(step_id).prompt.split())


def _all_steps():
    return [*FLOW.pipeline, *FLOW.on_failure.pipeline]


# -- one guide for the steps that write tests --------------------------------------


def test_the_test_guide_is_a_context_item_of_the_steps_that_write_tests_and_no_other():
    assert "test_guide" in CONTEXT_ITEMS
    holders = {step.id for step in _all_steps() if "test_guide" in step.context}

    assert holders == {"characterize", "write_test"}


def test_the_guide_renders_from_its_file():
    text = _render("test_guide", SimpleNamespace())

    assert text == Path("prompts/test_guide.md").read_text(encoding="utf-8").strip()


def test_the_guide_holds_the_choices_about_what_to_pin_and_assert():
    guide = " ".join(Path("prompts/test_guide.md").read_text(encoding="utf-8").split())

    assert "eight at most" in guide
    assert "check its class" in guide
    assert "Every test method needs an assertion" in guide
    assert "setUpClass" in guide
    assert "TransactionCase" in guide
    assert "Name each test method" in guide
    # Pinned whether or not the target is expected to change it: the port is
    # the one with the rules, and records a deliberate difference.
    assert "pin it anyway" in guide
    assert "Do not pin something you know the target version changes" not in guide


# -- understand ---------------------------------------------------------------------


def test_understand_says_what_the_recipe_is_for_now():
    prompt = _prompt("understand")

    assert "judged by replaying" not in prompt
    assert "characterize" in prompt
    assert "probe" in prompt


def test_understand_leaves_a_list_of_behaviours_for_the_step_that_writes_the_test():
    prompt = _prompt("understand")

    assert "list the behaviours" in prompt
    assert "one line each" in prompt


# -- port and rewrite ----------------------------------------------------------------


def test_port_tells_the_migrators_tests_from_the_ones_the_module_had():
    prompt = _prompt("port")

    assert "the `characterize` answer" in prompt
    assert "tests of its own" in prompt
    assert "like any other file" in prompt


def test_rewrite_carries_the_tests_too_because_it_starts_from_an_empty_directory():
    """`fresh` empties the module's directory on the target, tests included,
    and the tests step then failed the lap for tests the rewrite never wrote."""
    prompt = _prompt("rewrite")

    assert "characterize" in prompt
    assert "same method names" in prompt
    assert "tests/" in prompt


# -- the failure lane ----------------------------------------------------------------


def test_the_flag_is_named_for_the_source_not_the_old_version():
    diagnose = FLOW.step_by_id("diagnose")

    assert "reexamine_source" in diagnose.raises
    assert FLOW.step_by_id("reprobe").when == "reexamine_source"
    assert "reexamine_old" not in Path("cartridges/stock.yaml").read_text(encoding="utf-8")


def test_diagnose_is_told_what_each_flag_decides():
    prompt = _prompt("diagnose")

    for flag in ("needs_human", "reexamine_source", "small_fix", "start_over"):
        assert f"`{flag}`" in prompt
    assert "never both" in prompt


def test_diagnose_treats_a_proved_test_as_right_and_the_port_as_wrong():
    prompt = _prompt("diagnose")

    assert "the `characterize` answer" in prompt
    assert "port is wrong" in prompt
    assert "did not reach the target" in prompt


def test_reprobe_answers_with_what_it_ran_and_changes_nothing():
    prompt = _prompt("reprobe")

    assert "what you ran" in prompt
    assert "do not guess" in prompt


def test_patch_does_not_edit_a_proved_assertion_to_make_it_pass():
    prompt = _prompt("patch")

    assert "characterize" in prompt
    assert "target-differs" in prompt


# -- the fallback and the probe -------------------------------------------------------


def test_probe_no_longer_claims_to_be_the_only_evidence():
    prompt = _prompt("probe")

    assert "ships no tests" not in prompt
    assert "no tests to show" in prompt


# -- the standing prompt --------------------------------------------------------------


def test_the_standing_prompt_knows_the_one_write_the_source_allows():
    text = Path("prompts/system.md").read_text(encoding="utf-8")
    flat = " ".join(text.split())

    assert "write_source_test" in flat
    assert "Read-only." not in flat
    assert "restarts this side's session" in flat


@pytest.mark.parametrize("step_id", ["characterize", "write_test", "port", "rewrite", "diagnose"])
def test_every_step_that_touches_tests_has_a_prompt_that_fits_in_a_screen_or_two(step_id):
    """A prompt that grows without bound stops being read by the model. Not a
    quality test: a tripwire for the next addition."""
    assert len(FLOW.step_by_id(step_id).prompt.split()) < 520


# -- browser tests ---------------------------------------------------------------


def test_the_port_carries_the_browser_tests_and_can_see_whether_they_are_in_the_bundle():
    """The port was told about `tests/` only, and a module's JS tests stayed
    behind or landed outside the bundle no run loads."""
    port = FLOW.step_by_id("port")

    assert "static/tests/" in _prompt("port")
    assert "in_manifest: true" in _prompt("port")
    assert "list_tests" in port.tools
    assert "rules" in port.context


def test_the_migrator_writes_python_tests_only():
    guide = " ".join(Path("prompts/test_guide.md").read_text(encoding="utf-8").split())

    assert "Write Python tests only." in guide
    assert "not browser ones" in _prompt("write_test")
    assert "browser tests, not test classes" in _prompt("understand")


def test_the_18_rules_say_how_a_qunit_test_becomes_a_hoot_one():
    rules = Path("rules/18.0.md").read_text(encoding="utf-8")

    assert "## Browser tests" in rules
    assert "web.assets_unit_tests" in rules
    assert ".test.js" in rules
    assert "@odoo/hoot" in rules


def test_the_standing_prompt_says_a_port_carries_and_runs_the_browser_tests():
    flat = " ".join(Path("prompts/system.md").read_text(encoding="utf-8").split())

    assert "`static/tests/`" in flat
    assert "runs them on the target" in flat
