"""Assemble per-version rules into the ordered stack a migration sends."""

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from odoo_migrator.paths import prompts_dir as default_prompts_dir
from odoo_migrator.paths import rules_dir as default_rules_dir


class RulesError(Exception):
    """The stack could not be built. Silent skip is the failure this avoids."""


class SameVersionError(RulesError):
    """source == target. The migrator is a version jump, not a no-op."""


class DowngradeError(RulesError):
    """source > target. Without this check the stack would be empty."""


class MissingRulesError(RulesError):
    """A version in the stack has no file. That is a hole, not a shorter list."""


class MissingPromptError(RulesError):
    """prompts/system.md is missing. The payload cannot invent a prompt."""


@dataclass(frozen=True)
class RuleDoc:
    version: str
    role: str
    text: str


def _parse(version: str) -> int:
    """An on-premise Odoo version: ``N.0``.

    The minor is refused rather than ignored. Odoo Online has 17.2 and 17.4,
    and the stack is keyed by major only — so accepting "17.2" would quietly
    hand back 17.0's document, and "17.0 → 17.4" would build a one-document
    stack whose single entry is the *source*, with nothing saying what
    changed. Both are exactly the silent hole the rest of this module exists
    to prevent.
    """
    major, dot, minor = version.partition(".")
    if not dot or not major.isdigit() or not minor.isdigit():
        raise RulesError(f"Not an Odoo version: {version!r}")
    if minor != "0":
        raise RulesError(
            f"{version!r} is not an on-premise Odoo version. "
            "Rules are kept per major version (17.0, 18.0); "
            "Odoo Online minors such as 17.2 are not supported."
        )

    return int(major)


def _versions(source: str, target: str) -> list[str]:
    start = _parse(source)
    end = _parse(target)
    if start == end:
        raise SameVersionError(
            f"{source} → {target} is not a version jump: source and target are the same."
        )
    if start > end:
        raise DowngradeError(f"{source} → {target} is a downgrade.")

    return [f"{major}.0" for major in range(start, end + 1)]


def stack_versions(source: str, target: str) -> list[str]:
    """The version files a migration's stack touches. Promote asks; it does not pick."""

    return _versions(source, target)


def _role(index: int, last: int) -> str:
    # A one-entry stack would take this branch and never produce a target,
    # so the stack is guaranteed to hold at least two versions upstream.
    if index == 0:

        return "source"
    if index == last:

        return "target"

    return "intermediate"


def check_versions(source: str, target: str) -> None:
    """Raise if this pair can never assemble. Called where a human types it.

    Refusing at assembly time only is too late: nothing would object until a
    job was built for the first module, long after the migration was created.
    """
    _versions(source, target)


def assemble_rules(
    source: str, target: str, *, rules_dir: Path | None = None
) -> list[RuleDoc]:
    """Source document, then every version after it through the target.

    17.0 → 18.0 is two entries (source, target). 16.0 → 19.0 is four
    (source, two intermediate, target). A missing file raises; it is not
    skipped, because a hole in the stack would travel downstream unnoticed.
    """
    directory = Path(rules_dir) if rules_dir is not None else default_rules_dir()
    versions = _versions(source, target)
    last = len(versions) - 1
    docs = []
    for index, version in enumerate(versions):
        path = directory / f"{version}.md"
        if not path.is_file():
            raise MissingRulesError(f"No rules file for {version}: {path}")
        docs.append(
            RuleDoc(version=version, role=_role(index, last), text=path.read_text(encoding="utf-8"))
        )

    return docs


def load_system_prompt(*, prompts_dir: Path | None = None) -> str:
    directory = Path(prompts_dir) if prompts_dir is not None else default_prompts_dir()
    path = directory / "system.md"
    if not path.is_file():
        raise MissingPromptError(f"No system prompt at {path} (system.md).")

    return path.read_text(encoding="utf-8")


def append_finding(
    version: str,
    text: str,
    *,
    rules_dir: Path | None = None,
    module: str = "",
    job_id: str = "",
) -> Path:
    """Append to the file the human named. The app does not pick the version.

    Each promotion carries where it came from. These files are curated by
    hand and read in review; an anonymous paragraph at the bottom is
    indistinguishable from a rule someone wrote on purpose, and six months
    on nobody can tell which run produced it. A comment, so it does not
    become a heading in the prompt the model reads.
    """
    directory = Path(rules_dir) if rules_dir is not None else default_rules_dir()
    _parse(version)
    body = (text or "").strip()
    if not body:
        raise RulesError("That finding has no text to promote.")
    path = directory / f"{version}.md"
    if not path.is_file():
        raise MissingRulesError(f"No rules file for {version}: {path}")
    current = path.read_text(encoding="utf-8")
    if current and not current.endswith("\n"):
        current += "\n"
    where = module or "a run"
    stamp = datetime.now(UTC).date().isoformat()
    origin = f"<!-- promoted from {where} on {stamp}"
    if job_id:
        origin += f", job {job_id}"
    origin += " -->"
    path.write_text(current + "\n" + origin + "\n" + body + "\n", encoding="utf-8")

    return path
