"""Read what Sheller's clickbot answered, and say it in words.

The step decides from a `Reading`; the agent's tool shows the words. Both
come from one reading of the answer, so what `diagnose` sees is what the lap
saw. Nothing that clicked nothing is a pass: an answer with no report, or one
that clicked no menu and failed none, is the environment's, not a green run.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# The module has no menu of its own that a person sees: nothing to prove.
NO_MENUS = ("no_menus", "no_visible_menus")
# After a green `upgrade` the module is installed and Odoo knows it.
SETUP = ("module_not_installed", "unknown_module")
# How Sheller words a browser or devtools failure on one root:
# `f"{type(exc).__name__}: {exc}"`. An assertion's own text carries no prefix.
_DRIVER = re.compile(r"^[\w.]*(?:Error|Exception)\b: ")


@dataclass(frozen=True)
class Broken:
    xmlid: str
    root: str
    reason: str


@dataclass(frozen=True)
class Reading:
    # ran | no_menus | skipped | setup | environment
    outcome: str
    reason: str = ""
    broken: tuple[Broken, ...] = ()
    unreached: tuple[str, ...] = ()
    # What kept part of the run from being a proof: a root past the daemon's
    # deadline, a browser that fell over. The environment's, not the port's.
    notes: tuple[str, ...] = ()
    hidden: int = 0
    clicked: int = 0
    counts: dict = field(default_factory=dict)
    restore_failed: bool = False


def read(answer: dict) -> Reading:
    box = answer.get("clickbot") if isinstance(answer, dict) else None
    if not isinstance(box, dict):

        return Reading("environment", "the daemon's answer carries no clickbot report")
    error = _error(answer.get("error"))
    shared = {
        "unreached": _names(box.get("unreached")),
        "hidden": _count(box.get("hidden")),
        "clicked": _count(box.get("clicked")),
        "counts": {
            key: box[key] for key in ("views", "filters", "modals") if isinstance(box.get(key), int)
        },
    }
    if error == "clickbot_restore_failed":
        cause = box.get("cause")
        why = "Odoo's menus could not be put back after the run"
        if cause:
            why += f" (the run itself ended: {cause})"

        return Reading("environment", why, restore_failed=True, **shared)
    if error in NO_MENUS:

        return Reading("no_menus", error, **shared)
    if error in SETUP:

        return Reading("setup", error, **shared)
    if error == "js_tests_skipped":
        skipped = box.get("skipped") if isinstance(box.get("skipped"), dict) else {}

        return Reading(
            "skipped",
            str(skipped.get("reason") or "Odoo skipped the run and gave no reason"),
            **shared,
        )
    if error:

        return Reading("environment", error, **shared)
    broken = [
        Broken(
            str(entry.get("xmlid") or ""),
            str(entry.get("root") or ""),
            str(entry.get("reason") or ""),
        )
        for entry in box.get("failed") or []
        if isinstance(entry, dict)
    ]
    notes: list[str] = []
    for root in box.get("roots") or []:
        if not isinstance(root, dict) or root.get("success") is not False:
            continue
        xmlid = str(root.get("xmlid") or "")
        why = str(root.get("error") or "")
        if why == "timeout":
            notes.append(f"the run ran out of time under {xmlid}")
        elif any(entry.root == xmlid for entry in broken):
            continue
        elif _DRIVER.match(why):
            notes.append(f"the browser failed under {xmlid}: {why}")
        else:
            broken.append(Broken(xmlid, xmlid, why or "the root failed and the run says no more"))
    if not broken and not notes and not shared["clicked"]:

        return Reading("environment", "the run clicked nothing and reported no failure", **shared)

    return Reading("ran", broken=tuple(broken), notes=tuple(notes), **shared)


def summary(reading: Reading) -> str:
    """The counts Sheller gives, in one line."""
    parts = [f"{reading.clicked} menu(s) clicked"]
    for key in ("views", "filters", "modals"):
        if key in reading.counts:
            parts.append(f"{reading.counts[key]} {key[:-1]}(s)")

    return ", ".join(parts)


def menu_lines(broken) -> list[str]:
    """One line per broken menu, each with its xmlid: the agent finds the
    menu, its action and its views from it."""

    return [f"{entry.xmlid} (under {entry.root}): {entry.reason}" for entry in broken]


def render(answer: dict) -> str:
    """The answer as words for the agent. A run that did not happen says so in
    words that cannot read as a pass."""
    reading = read(answer)
    if reading.outcome == "no_menus":

        return (
            "clickbot: the module has no menu of its own that this user sees "
            f"({reading.reason}); nothing was clicked"
        )
    if reading.outcome != "ran":
        said = reading.reason
        if reading.outcome == "setup":
            said += "; the module is not installed or Odoo does not know it"

        return f"clickbot did not run ({said}): nothing was clicked, so this is not a pass"
    lines = [f"clickbot: {summary(reading)}, {len(reading.broken)} failed"]
    lines += menu_lines(reading.broken)
    if reading.unreached:
        lines.append(f"unreached: {', '.join(reading.unreached)}")
    lines += [f"note: {note}" for note in reading.notes]

    return "\n".join(lines)


def _error(value) -> str:
    if isinstance(value, dict):

        return f"{value.get('type') or 'Error'}: {value.get('message') or ''}".strip()

    return str(value or "")


def _names(value) -> tuple[str, ...]:

    return tuple(str(item) for item in value) if isinstance(value, list) else ()


def _count(value) -> int:
    if isinstance(value, list):

        return len(value)

    return value if isinstance(value, int) else 0
