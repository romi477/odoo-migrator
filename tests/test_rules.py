from pathlib import Path

import pytest

from odoo_migrator.rules import (
    DowngradeError,
    MissingPromptError,
    MissingRulesError,
    RulesError,
    SameVersionError,
    assemble_rules,
    load_system_prompt,
)


def _write(rules_dir: Path, version: str, text: str) -> None:
    rules_dir.mkdir(parents=True, exist_ok=True)
    (rules_dir / f"{version}.md").write_text(text, encoding="utf-8")


def test_seventeen_to_eighteen_is_source_then_target(tmp_path):
    _write(tmp_path, "17.0", "coming from 17.0\n")
    _write(tmp_path, "18.0", "what 18.0 changed\n")
    docs = assemble_rules("17.0", "18.0", rules_dir=tmp_path)

    assert [(doc.version, doc.role, doc.text) for doc in docs] == [
        ("17.0", "source", "coming from 17.0\n"),
        ("18.0", "target", "what 18.0 changed\n"),
    ]


def test_sixteen_to_nineteen_is_four_docs_in_version_order(tmp_path):
    """16.0 → 19.0 has no pair document; it is an ordered stack of four."""
    _write(tmp_path, "16.0", "16")
    _write(tmp_path, "17.0", "17")
    _write(tmp_path, "18.0", "18")
    _write(tmp_path, "19.0", "19")
    docs = assemble_rules("16.0", "19.0", rules_dir=tmp_path)

    assert [(doc.version, doc.role) for doc in docs] == [
        ("16.0", "source"),
        ("17.0", "intermediate"),
        ("18.0", "intermediate"),
        ("19.0", "target"),
    ]
    assert [doc.text for doc in docs] == ["16", "17", "18", "19"]


def test_missing_intermediate_file_is_a_hard_error(tmp_path):
    """A gap is a missing document, not a shorter stack."""
    _write(tmp_path, "16.0", "16")
    _write(tmp_path, "17.0", "17")
    _write(tmp_path, "19.0", "19")

    with pytest.raises(MissingRulesError, match="18.0"):
        assemble_rules("16.0", "19.0", rules_dir=tmp_path)


def test_same_version_is_refused(tmp_path):
    _write(tmp_path, "18.0", "18")

    with pytest.raises(SameVersionError, match="version jump"):
        assemble_rules("18.0", "18.0", rules_dir=tmp_path)


def test_downgrade_is_refused(tmp_path):
    _write(tmp_path, "17.0", "17")
    _write(tmp_path, "18.0", "18")

    with pytest.raises(DowngradeError, match="downgrade"):
        assemble_rules("18.0", "17.0", rules_dir=tmp_path)


def test_a_minor_version_is_refused_not_rounded_down(tmp_path):
    """17.2 used to build 17.0's stack without saying so: the minor was parsed
    and then thrown away. Rules are kept per major, so the pair is refused."""
    _write(tmp_path, "17.0", "17")
    _write(tmp_path, "18.0", "18")

    with pytest.raises(RulesError, match="on-premise"):
        assemble_rules("17.2", "18.0", rules_dir=tmp_path)


def test_a_same_major_upgrade_is_refused_not_answered_with_a_lone_source(tmp_path):
    """17.0 → 17.4 used to return one document, roled 'source', with nothing
    describing the target. An agent would have been told where the code comes
    from and never what changed."""
    _write(tmp_path, "17.0", "17")

    with pytest.raises(RulesError, match="on-premise"):
        assemble_rules("17.0", "17.4", rules_dir=tmp_path)


def test_every_stack_has_a_source_and_a_target(tmp_path):
    for version in ("16.0", "17.0", "18.0", "19.0"):
        _write(tmp_path, version, version)

    for source, target in (("17.0", "18.0"), ("16.0", "19.0"), ("18.0", "19.0")):
        roles = [doc.role for doc in assemble_rules(source, target, rules_dir=tmp_path)]

        assert roles[0] == "source"
        assert roles[-1] == "target"
        assert len(roles) >= 2


def test_system_prompt_is_the_file_contents(tmp_path):
    (tmp_path / "system.md").write_text("You are the migrator.\n", encoding="utf-8")

    assert load_system_prompt(prompts_dir=tmp_path) == "You are the migrator.\n"


def test_missing_system_prompt_is_a_hard_error(tmp_path):
    with pytest.raises(MissingPromptError, match="system.md"):
        load_system_prompt(prompts_dir=tmp_path)


def test_a_common_file_is_sent_once_in_front_of_the_versions(tmp_path):
    _write(tmp_path, "17.0", "coming from 17.0\n")
    _write(tmp_path, "18.0", "what 18.0 changed\n")
    (tmp_path / "common.md").write_text("shared\n", encoding="utf-8")
    docs = assemble_rules("17.0", "18.0", rules_dir=tmp_path)

    assert [(doc.version, doc.role) for doc in docs] == [
        ("common", "context"),
        ("17.0", "source"),
        ("18.0", "target"),
    ]
    assert docs[0].text == "shared\n"


def test_shipped_rules_assemble_seventeen_to_eighteen():
    docs = assemble_rules("17.0", "18.0")

    assert [doc.version for doc in docs] == ["common", "17.0", "18.0"]
    assert docs[0].role == "context"
    assert docs[1].role == "source"
    assert docs[2].role == "target"
    assert "attrs" in docs[1].text
    assert "aggregator" in docs[2].text


def test_shipped_rules_say_when_the_list_root_changes():
    """16 and 17 keep <tree>. 18 is where the root becomes <list>."""
    sixteen = {doc.version: doc.text for doc in assemble_rules("16.0", "17.0")}
    eighteen = {doc.version: doc.text for doc in assemble_rules("17.0", "18.0")}
    nineteen = {doc.version: doc.text for doc in assemble_rules("18.0", "19.0")}

    assert "root tag is `tree`" in sixteen["16.0"]
    assert "root tag is still `tree`" in sixteen["17.0"]
    assert "root tag is `list`" in eighteen["18.0"]
    assert "<tree>` is not a valid root" in eighteen["18.0"]
    assert "root tag stays `list`" in nineteen["19.0"]
    assert "Python 3.10 or newer" in nineteen["19.0"]
    assert "Python 3.12" not in nineteen["19.0"]
    assert "tree" in sixteen["common"]
    assert "list" in sixteen["common"]


def test_shipped_rules_assemble_nineteen_to_twenty():
    docs = assemble_rules("19.0", "20.0")

    assert [doc.version for doc in docs] == ["common", "19.0", "20.0"]
    assert docs[-1].role == "target"
    assert "ir.access" in docs[-1].text
    assert "is_company" in docs[-1].text
