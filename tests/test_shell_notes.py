"""The copy of Sheller's guidance in `prompts/sheller.md`, and what it is for.

A job takes its shell notes from the daemon (`shell_guidance`, and its own
tests). This file is what is used when the daemon has no `/api/help` route or
does not answer: odoo-sheller's `os_help` topics as they read at 1.10.0, copied
by hand with the tool names rewritten. It is allowed to age; the checks against
the sibling checkout are about the two things that must not drift silently, the
hashes the overlays were written against and the set of tool markers.
"""

import ast
import hashlib
from pathlib import Path

import pytest

from odoo_migrator.rules import MissingPromptError, load_shell_notes
from odoo_migrator.shell_guidance import MAPPED, OVERLAID

_SHELLER_GUIDANCE = Path("/opt/projects/odoo-sheller/odoo_sheller/guidance.py")
# Markers Sheller has that the agent has no tool for. A new marker has to be
# put in one list or the other, on purpose.
UNMAPPED = {
    "open_session", "close_session", "commit", "session", "attach_session",
    "help", "list_sessions", "list_containers",
}


def _their(name: str):
    tree = ast.parse(_SHELLER_GUIDANCE.read_text(encoding="utf-8"))
    found = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.AnnAssign | ast.Assign)
            and (
                getattr(node.target, "id", "") if isinstance(node, ast.AnnAssign)
                else getattr(node.targets[0], "id", "")
            ) == name
        ),
        None,
    )
    assert found is not None, f"odoo-sheller's guidance.py no longer has {name}"

    return ast.literal_eval(found.value)


def test_the_notes_say_what_the_tools_can_do():
    text = load_shell_notes()

    assert "records.mapped('name')" in text
    assert "which override actually wins" in text
    assert "~/.odoo-sheller/" in text


def test_the_notes_name_our_tools_and_not_the_mcp_ones():
    """The agent has `source` and `exec`, not `os_source` and `os_exec`.
    Naming a tool it cannot call is worse than saying nothing."""
    text = load_shell_notes()

    assert "os_source" not in text
    assert "os_exec" not in text
    assert "os_run_test" not in text
    assert "source(side=" in text


def test_missing_notes_are_a_hard_error(tmp_path):
    with pytest.raises(MissingPromptError, match="sheller.md"):
        load_shell_notes(prompts_dir=tmp_path)


def test_the_overlays_were_written_against_the_topics_sheller_has_now():
    """A topic this app words itself is shadowed by the overlay. If Sheller
    changed the topic since, a person should read it and decide, which is what
    the job's `warn` line tells them at run time and this tells them sooner."""
    if not _SHELLER_GUIDANCE.is_file():
        pytest.skip("no odoo-sheller checkout next to this one")
    theirs = _their("HELP")

    for topic, recorded in OVERLAID.items():
        assert hashlib.sha256(theirs[topic].encode("utf-8")).hexdigest() == recorded, (
            f"odoo-sheller changed its `{topic}` topic: read it, update "
            f"prompts/shell_overlay/{topic}.md and OVERLAID in shell_guidance.py"
        )


def test_every_tool_marker_sheller_has_is_decided():
    if not _SHELLER_GUIDANCE.is_file():
        pytest.skip("no odoo-sheller checkout next to this one")
    markers = set(_their("MARKERS"))

    assert markers == set(MAPPED) | UNMAPPED, (
        "odoo-sheller has a tool marker this app has not decided about: map it to "
        "a tool the agent has, or list it as unmapped here"
    )


def test_the_test_section_describes_our_transport_and_not_the_mcp_tool():
    """odoo-sheller's own `tests` topic is about `os_run_test`, which opens a
    session per run and hands back a job to poll. The daemon's route — what
    this app calls — runs in the session the step is holding and waits, so
    the copy would have been wrong in the two places that matter: whose
    transaction is rolled back, and whether there is anything to poll."""
    text = load_shell_notes()

    assert "not a fresh one" in text
    assert "discarded_pending" in text
    assert "nothing to poll" in text
    # A test written this step is invisible until the process re-imports.
    assert "call `reopen`, then `run_test`" in text
    assert "invalid_test_spec" in text


def test_the_test_section_offers_a_module_and_what_failed():
    """Sheller 1.8.3: a bare module runs its standard tests, and every answer
    lists what failed as specs to run again. The notes said one class per
    call, in turn, and the agent ran a module class by class."""
    text = load_shell_notes()

    assert "`run_test(side, test)` takes three forms" in text
    assert "`failed`" in text
    assert "stderr_lines" in text
    assert "Run one class per call, in turn." not in text
