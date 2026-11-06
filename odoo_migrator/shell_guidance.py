"""What the agent is told the Odoo shell can do, taken from the daemon.

Sheller teaches its own MCP clients through a server's instructions and
`os_help(topic)`. Since 1.11.0 the same texts are served to an HTTP client by
`GET /api/help`, with the tool names left as `{{markers}}`. A job asks for
them when it starts, replaces each marker with this app's name for the tool,
and sends that to every step.

Three kinds of topic (docs_/superpowers/specs/2026-10-17-shell-guidance-from-the-daemon.md):

- not ours: about opening, owning and committing sessions, which the runner
  does, and the rules of an Odoo major, which `rules/<version>.md` has said in
  its own way. Left out.
- owned: `tests` and `modules`. The daemon words them for a client that opens a
  session of its own per run and reaches `reopen` by other tools; this app runs
  a test in the session a step holds, waits for it, and has `reopen`. The
  overlay in `prompts/shell_overlay/` replaces the daemon's text, and records
  the hash of the text it was written against so a changed one is seen.
- taken: the rest, a topic Sheller adds tomorrow included. A list item that
  names a tool this app has no equivalent of is dropped, and a topic whose
  prose does is left out; the log says which, so no agent is told to call
  something that is not there.

`prompts/sheller.md`, the copy taken by hand at 1.10.0, is the fallback for a
daemon without the route or one that does not answer.
"""

from __future__ import annotations

import hashlib
import re
import threading
from dataclasses import dataclass, field
from pathlib import Path

from odoo_migrator.paths import prompts_dir as default_prompts_dir
from odoo_migrator.rules import load_shell_notes
from odoo_migrator.sheller.errors import ShellerError

# The markers whose action the agent has under the same name.
MAPPED = frozenset(
    {
        "exec", "source", "journal", "history", "run_test", "test_result",
        "list_tests", "interrupt", "rollback", "click_module",
    }
)
# Left out because the runner does what they say (sessions, ownership, commit)
# or because another file says it (the rules of a major).
NOT_OURS = frozenset({"sessions", "ownership", "commit", "remote_server", "watching"})
RULES_OF_A_MAJOR = re.compile(r"^odoo_\d+$")
# The order they read in. A topic not named here follows, by name.
ORDER = ("limits", "orm", "code", "log", "records", "modules", "jobs", "tests")
# The daemon's text of each topic this app words itself, by sha256 as
# `GET /api/help` reported it at Sheller 1.13.2. A different hash means
# Sheller changed the topic after the overlay was written. 1.12.0 added
# `click_module` to `tests`. The notes go to every agent step of a job and only
# `diagnose` has that tool, so what to know about it is in the tool's own
# description and not here; 1.13.2 added the rollback before Odoo's own install
# and upgrade to `modules`, which the overlay now says.
OVERLAID = {
    "modules": "48007152491b74fd069267d2924f05a73ac27a36e45e2a6405e0113095577070",
    "tests": "79ad51fddff721cc20555f829f0949fe9e65962bcd3eff33aa434299f8fc755d",
}
_MARKER = re.compile(r"\{\{(\w+)\}\}")
_HELP_CALL = re.compile(r"\{\{help\}\}\('(\w+)'\)")


@dataclass(frozen=True)
class ShellNotes:
    text: str
    hash: str
    # `daemon 1.11.0`, or `vendored` for the copy in prompts/sheller.md.
    source: str
    taken: list[str] = field(default_factory=list)
    overlaid: list[str] = field(default_factory=list)
    # topic -> why it is not in the text
    left_out: dict[str, str] = field(default_factory=dict)
    # topic -> why some of its list items are not
    trimmed: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)


_CACHE: dict[tuple[str, str], ShellNotes] = {}
_LOCK = threading.Lock()


def forget() -> None:
    """Drop what was fetched. A test's way to start from nothing."""
    with _LOCK:
        _CACHE.clear()


def default_overlay_dir() -> Path:

    return default_prompts_dir() / "shell_overlay"


def _sha(text: str) -> str:

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def shell_notes(client, *, prompts_dir: Path | None = None) -> ShellNotes:
    """The notes for a job that starts now.

    Asks the daemon for its version, which is cheap, and for the guidance only
    when that version has not been seen: a batch asks once, an upgraded Sheller
    is noticed by the next job. A fallback is never remembered, so a daemon
    that was down is asked again.
    """
    directory = Path(prompts_dir) if prompts_dir is not None else default_prompts_dir()
    try:
        version = str(client.health()["version"])
    except (ShellerError, KeyError, TypeError) as exc:

        return _vendored(directory, f"the daemon did not say its version ({exc})")
    key = (version, str(directory))
    with _LOCK:
        known = _CACHE.get(key)
    if known is not None:

        return known
    try:
        answer = client.help()
    except ShellerError as exc:
        if getattr(exc, "status", None) == 404:

            return _vendored(
                directory,
                f"Sheller {version} has no guidance route (it has one from 1.11.0)",
            )

        return _vendored(directory, f"the guidance could not be fetched ({exc})")
    notes = _assemble(answer, version, directory)
    if notes is None:

        return _vendored(directory, f"Sheller {version} served no topic this app can use")
    with _LOCK:
        _CACHE[key] = notes

    return notes


def _vendored(directory: Path, why: str) -> ShellNotes:
    text = load_shell_notes(prompts_dir=directory)

    return ShellNotes(
        text=text,
        hash=_sha(text),
        source="vendored",
        warnings=[f"{why}: the copy in prompts/sheller.md is used"],
    )


def _assemble(answer: dict, version: str, directory: Path) -> ShellNotes | None:
    try:
        served = {name: entry["text"] for name, entry in answer["topics"].items()}
        hashes = {name: entry["hash"] for name, entry in answer["topics"].items()}
    except (KeyError, TypeError, AttributeError):

        return None
    overlay_dir = directory / "shell_overlay"
    left_out: dict[str, str] = {}
    warnings: list[str] = []
    candidates: set[str] = set()
    for name in served:
        if name in OVERLAID:
            candidates.add(name)
        elif name in NOT_OURS:
            left_out[name] = "the runner opens, owns and commits the sessions"
        elif RULES_OF_A_MAJOR.match(name):
            left_out[name] = "rules/<version>.md says what a version changes"
        else:
            candidates.add(name)
    for name, recorded in OVERLAID.items():
        candidates.add(name)
        if name not in served:
            warnings.append(
                f"the daemon has no `{name}` topic now; the overlay prompts/shell_overlay/"
                f"{name}.md is used on its own"
            )
        elif hashes.get(name) != recorded:
            warnings.append(
                f"the daemon's `{name}` topic is not the one the overlay "
                f"prompts/shell_overlay/{name}.md was written against: read it, update "
                "the overlay and OVERLAID in shell_guidance.py"
            )
    # A topic that points at one that is not in the notes, or names a tool the
    # agent lacks, is left out; leaving one out can strand a pointer to it, so
    # go round until nothing changes.
    rendered: dict[str, str] = {}
    trimmed: dict[str, str] = {}
    changed = True
    while changed:
        changed = False
        rendered.clear()
        trimmed.clear()
        for name in sorted(candidates - set(OVERLAID)):
            text, problem = _render(served[name], candidates)
            if problem:
                left_out[name] = problem
                candidates.discard(name)
                changed = True
            else:
                rendered[name] = text
                dropped = _trim(served[name], candidates)[1]
                if dropped:
                    trimmed[name] = "; ".join(dropped)
    taken = [name for name in rendered]
    if not taken:

        return None
    ordered = [name for name in ORDER if name in candidates]
    ordered += sorted(name for name in candidates if name not in ORDER)
    sections = [(overlay_dir / "head.md").read_text(encoding="utf-8").strip()]
    for name in ordered:
        if name in OVERLAID:
            sections.append(_overlay(overlay_dir, name))
        else:
            sections.append(rendered[name].strip())
    text = "\n\n".join(sections) + "\n"

    return ShellNotes(
        text=text,
        hash=_sha(text),
        source=f"daemon {version}",
        taken=sorted(taken, key=ordered.index),
        overlaid=sorted(OVERLAID),
        left_out=left_out,
        trimmed=trimmed,
        warnings=warnings,
    )


def _overlay(overlay_dir: Path, name: str) -> str:
    parts = [(overlay_dir / f"{name}.md").read_text(encoding="utf-8").strip()]
    extra = overlay_dir / f"{name}_js.md"
    if extra.is_file():
        parts.append(extra.read_text(encoding="utf-8").strip())

    return "\n\n".join(parts)


def _blocks(text: str) -> list[tuple[bool, str]]:
    """The text as list items and the rest, in order.

    An item is a line that starts with `- ` and the lines indented under it.
    """
    blocks: list[tuple[bool, str]] = []
    for line in text.split("\n"):
        if line.startswith("- "):
            blocks.append((True, line))
        elif blocks and blocks[-1][0] and line.startswith("  ") and line.strip():
            blocks[-1] = (True, f"{blocks[-1][1]}\n{line}")
        else:
            blocks.append((False, line))

    return blocks


def _trim(text: str, present: set[str]) -> tuple[str, list[str]]:
    """Drop the list items that cannot be told to this agent.

    Sheller 1.13.0 added a line about `psql` to `limits`, and leaving the whole
    topic out for it took every other rule of that list away from the agent.
    An item stands on its own; a paragraph does not, and stays for `_render`
    to judge with the topic.
    """
    kept: list[str] = []
    dropped: list[str] = []
    for item, block in _blocks(text):
        problem = _render(block, present, trim=False)[1] if item else ""
        if problem:
            dropped.append(problem)
        else:
            kept.append(block)

    return "\n".join(kept), list(dict.fromkeys(dropped))


def _render(text: str, present: set[str], *, trim: bool = True) -> tuple[str, str]:
    """Put this app's tool names where the daemon left markers.

    Returns the text and, when the topic cannot be told to this agent, what is
    wrong with it: a marker for something it has no equivalent of, or a
    pointer to a topic that is not part of the notes. A list item with such a
    marker is dropped first (`_trim`); only the rest can leave a topic out.
    """
    if trim:
        text = _trim(text, present)[0]
    missing: list[str] = []

    def pointer(found: re.Match) -> str:
        if found.group(1) in present:

            return f"the notes on {found.group(1)}"
        missing.append(f"points at the topic `{found.group(1)}`")

        return ""

    text = _HELP_CALL.sub(pointer, text)

    def tool(found: re.Match) -> str:
        name = found.group(1)
        if name in MAPPED:

            return name
        missing.append(f"names `{name}`, which the agent does not have")

        return found.group(0)

    text = _MARKER.sub(tool, text)
    if missing:

        return text, "; ".join(dict.fromkeys(missing))

    return text, ""
