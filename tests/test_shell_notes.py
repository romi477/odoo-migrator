"""What the agent is told the Odoo shell can do.

The text is odoo-sheller's own `os_help` topics, vendored: this app talks to
the daemon over HTTP and there is no route that serves them. Copied text can
drift, so the topics are checked against the sibling checkout when it is
there.
"""

import ast
import hashlib
from pathlib import Path

import pytest

from odoo_migrator.rules import MissingPromptError, load_shell_notes

_SHELLER_MCP = Path("/opt/projects/odoo-sheller/odoo_sheller/mcp.py")
# The topics taken, as they read when they were taken. A changed hash is not
# a failure of this app: it means someone improved the guidance next door and
# a person should read the new version before copying it over.
VENDORED = {
    "limits": "988d9939e69331431ecacf8239e1ae24a74fbce63287dc6a8a3664d9a872e5bd",
    "orm": "29ce479be88e26e3c260c056cc2e71c2001be5069a1de0e8c6ef308a4aefd031",
    "code": "9695582a4342042586018028a87e617a9adffe8333083f67a3b4326914a14638",
    "log": "43c3218353d967e7acfe5def9db2fd771a990a930e348f865ba90b9188d2525c",
    "records": "5b2c65b96dd3ba9f796619631e74644d1be58469e92cb2db1c8085c56db054a5",
    "modules": "0955570535534c2d4e6279884172dab9a8452c3fcab5dfcdd753f10856eecbca",
    "jobs": "c853c98be14913bfc4e739cbcaae82491f034aa509f136a30954ed6d5580f37f",
    "tests": "34e478ea86ebc128aea2692c1805962d30b67dcea80a5936d57635b8459aa782",
}
# Left out on purpose: the runner opens, closes, owns and commits the
# sessions, so an agent told how to ask for commit rights would be told to do
# something it cannot do.
NOT_OURS = ("sessions", "ownership", "commit", "remote_server", "watching")


def _their_help() -> dict:
    tree = ast.parse(_SHELLER_MCP.read_text(encoding="utf-8"))
    found = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.AnnAssign) and getattr(node.target, "id", "") == "HELP"
        ),
        None,
    )
    assert found is not None, "odoo-sheller no longer has a HELP dict"

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


def test_the_vendored_topics_have_not_changed_next_door():
    if not _SHELLER_MCP.is_file():
        pytest.skip("no odoo-sheller checkout next to this one")
    theirs = _their_help()
    stale = [
        topic
        for topic, digest in VENDORED.items()
        if hashlib.sha256(theirs[topic].encode("utf-8")).hexdigest() != digest
    ]

    assert not stale, f"odoo-sheller rewrote {stale}: read it, then re-vendor prompts/sheller.md"


def test_the_topics_the_runner_owns_are_left_out():
    """A person reading this file should see the choice, not guess it."""
    if not _SHELLER_MCP.is_file():
        pytest.skip("no odoo-sheller checkout next to this one")
    theirs = _their_help()

    assert set(VENDORED) | set(NOT_OURS) == set(theirs), (
        "odoo-sheller has a topic this app has never decided about"
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
