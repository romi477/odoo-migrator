"""The guidance an agent is given about the Odoo shell comes from the daemon.

`prompts/sheller.md` is the copy taken by hand at 1.10.0; it is the fallback.
What a job sends is `shell_guidance.shell_notes`: the daemon's topics with the
tool names this app gives the agent, the two topics this app words itself, and
nothing about a flow it does not have.
"""

import hashlib

import pytest

from odoo_migrator import shell_guidance
from odoo_migrator.rules import load_shell_notes
from odoo_migrator.sheller import FakeShellerClient, ShellerUnreachable


@pytest.fixture(autouse=True)
def fresh(monkeypatch):
    shell_guidance.forget()
    # The fake's topics are cut short, so the hashes the overlays were written
    # against (the real 1.11.0 ones) are not theirs. Record the fake's.
    fake = FakeShellerClient()
    monkeypatch.setattr(
        shell_guidance,
        "OVERLAID",
        {
            topic: hashlib.sha256(fake.help_topics[topic].encode("utf-8")).hexdigest()
            for topic in ("tests", "modules")
        },
    )
    yield
    shell_guidance.forget()


def _sha(text: str) -> str:

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def test_a_taken_topic_carries_this_apps_tool_names():
    notes = shell_guidance.shell_notes(FakeShellerClient())

    assert "run_test does\n  not on 13" in notes.text
    assert 'source(path="integration_shopify")' in notes.text
    assert "{{" not in notes.text
    assert "os_" not in notes.text
    assert notes.source == "daemon 1.11.0"


def test_a_topic_about_a_flow_this_app_does_not_have_is_left_out():
    notes = shell_guidance.shell_notes(FakeShellerClient())

    assert "Commit is a right the human grants" not in notes.text
    assert "opens one and" not in notes.text
    assert "Use source before you rely" not in notes.text
    assert set(notes.left_out) >= {"sessions", "commit", "odoo_17"}


def test_the_two_topics_this_app_words_itself_replace_the_daemons():
    notes = shell_guidance.shell_notes(FakeShellerClient())

    assert notes.overlaid == ["modules", "tests"]
    assert "not a fresh one" in notes.text
    assert "call `reopen`, then `run_test`" in notes.text
    # The daemon's wording is for an MCP client: a session it opens itself.
    assert "do not call close_session" not in notes.text
    assert "the run opens a session of its own" not in notes.text


def test_the_notes_open_with_the_head_and_follow_a_fixed_order():
    notes = shell_guidance.shell_notes(FakeShellerClient())
    text = notes.text

    assert text.startswith("# Working inside the Odoo shell")
    order = [
        text.index("## What you cannot do"),
        text.index("## Writing idiomatic ORM code"),
        text.index("## Reading the code you are debugging"),
        text.index("## Seeing what Odoo logged"),
        text.index("## Installing and updating a module"),
        text.index("## Running a test"),
    ]
    assert order == sorted(order)


def test_a_topic_sheller_adds_tomorrow_is_taken_without_a_change_here():
    fake = FakeShellerClient()
    fake.help_topics["profiling"] = "## Profiling\n\nWrap it in {{exec}}, read {{journal}}.\n"
    notes = shell_guidance.shell_notes(fake)

    assert "## Profiling\n\nWrap it in exec, read journal." in notes.text
    assert "profiling" in notes.taken


def test_a_new_topic_that_names_a_tool_the_agent_lacks_is_left_out_and_said_so():
    fake = FakeShellerClient()
    fake.help_topics["handover"] = "## Handover\n\nCall {{attach_session}} with the key.\n"
    notes = shell_guidance.shell_notes(fake)

    assert "Handover" not in notes.text
    assert "attach_session" in notes.left_out["handover"]


def test_a_pointer_to_a_topic_that_is_not_in_the_notes_leaves_the_topic_out():
    fake = FakeShellerClient()
    fake.help_topics["records"] = "## Records\n\nSee {{help}}('commit') first.\n"
    notes = shell_guidance.shell_notes(fake)

    assert "## Records" not in notes.text
    assert "records" in notes.left_out


def test_a_pointer_to_a_topic_that_is_in_the_notes_reads_as_a_sentence():
    notes = shell_guidance.shell_notes(FakeShellerClient())

    assert "leaves its log in the journal: the notes on tests." in notes.text


def test_a_changed_topic_this_app_words_itself_is_a_warning_not_a_surprise():
    fake = FakeShellerClient()
    fake.help_topics["tests"] += "\nSheller changed this.\n"
    notes = shell_guidance.shell_notes(fake)

    assert any("tests" in warning and "overlay" in warning for warning in notes.warnings)
    assert "Sheller changed this." not in notes.text


def test_nothing_is_warned_when_the_overlays_are_current():
    assert shell_guidance.shell_notes(FakeShellerClient()).warnings == []


def test_the_hash_is_of_the_text_the_agent_gets():
    notes = shell_guidance.shell_notes(FakeShellerClient())

    assert notes.hash == _sha(notes.text)


def test_a_batch_asks_the_daemon_once_and_a_new_version_asks_again():
    fake = FakeShellerClient()
    asked = []
    real = fake.help
    fake.help = lambda: asked.append(1) or real()

    shell_guidance.shell_notes(fake)
    shell_guidance.shell_notes(fake)
    shell_guidance.shell_notes(fake)

    assert len(asked) == 1

    fake._version = "1.12.0"
    notes = shell_guidance.shell_notes(fake)

    assert len(asked) == 2
    assert notes.source == "daemon 1.12.0"


def test_a_daemon_without_the_route_falls_back_to_the_copy_and_says_so():
    notes = shell_guidance.shell_notes(FakeShellerClient(version="1.10.0"))

    assert notes.source == "vendored"
    assert notes.text == load_shell_notes()
    assert any("1.10.0" in warning or "no guidance route" in warning for warning in notes.warnings)


def test_a_daemon_that_does_not_answer_falls_back_to_the_copy():
    fake = FakeShellerClient()

    def down():
        raise ShellerUnreachable("no daemon")

    fake.health = down
    notes = shell_guidance.shell_notes(fake)

    assert notes.source == "vendored"
    assert notes.text == load_shell_notes()


def test_an_answer_with_no_usable_topic_falls_back_to_the_copy():
    fake = FakeShellerClient()
    fake.help_topics = {"sessions": "## Sessions\n\n{{open_session}}\n"}
    notes = shell_guidance.shell_notes(fake)

    assert notes.source == "vendored"


def test_a_fallback_is_not_remembered():
    """A daemon that was down once must be asked again by the next job."""
    fake = FakeShellerClient()
    real = fake.health

    def down():
        raise ShellerUnreachable("no daemon")

    fake.health = down
    assert shell_guidance.shell_notes(fake).source == "vendored"
    fake.health = real

    assert shell_guidance.shell_notes(fake).source == "daemon 1.11.0"


def test_the_overlays_are_the_sections_of_the_copy_they_stand_for():
    """The fallback and the overlay must say the same thing about a test run
    and about installing a module, or an old daemon and a new one brief the
    agent differently."""
    vendored = load_shell_notes()
    root = shell_guidance.default_overlay_dir()
    for name in ("modules", "tests"):
        assert (root / f"{name}.md").read_text(encoding="utf-8").strip() in vendored


def test_a_topic_about_sessions_is_left_out_even_when_it_names_no_tool():
    """`watching` and `ownership` carry no marker at all, so only the list of
    what the runner does itself keeps them out."""
    fake = FakeShellerClient()
    fake.help_topics["watching"] = "## Being watched\n\nEverything is journalled.\n"
    fake.help_topics["ownership"] = "## Ownership\n\nA session has one owner.\n"
    notes = shell_guidance.shell_notes(fake)

    assert "Being watched" not in notes.text
    assert "A session has one owner." not in notes.text
    assert "runner" in notes.left_out["watching"]
    assert "runner" in notes.left_out["ownership"]


def test_an_unusable_answer_is_not_remembered_either():
    fake = FakeShellerClient()
    good = dict(fake.help_topics)
    fake.help_topics = {"sessions": "## Sessions\n\n{{open_session}}\n"}
    assert shell_guidance.shell_notes(fake).source == "vendored"
    fake.help_topics = good

    assert shell_guidance.shell_notes(fake).source == "daemon 1.11.0"
