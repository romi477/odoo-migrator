"""What an agent step is allowed to see, and which model runs it.

The cartridge lists context items. This module renders only those, in the
closed order, so a rewrite cannot be handed the previous attempt by accident.
"""

from __future__ import annotations

from pathlib import Path

from odoo_migrator.module_paths import agent_path, module_root
from odoo_migrator.rules import (
    RuleDoc,
    assemble_rules,
    load_shell_notes,
    load_system_prompt,
    load_test_guide,
)
from odoo_migrator.runner.cartridge import CONTEXT_ITEMS, Budget, Step
from odoo_migrator.runner.context import StepContext, StepResult, check_stop
from odoo_migrator.runner.snapshot import module_files

# The ceiling a cartridge cannot raise. "A cartridge may narrow, never widen"
# is the one rule that keeps an uploaded file from granting itself something,
# and a budget is the cheapest thing in that file to inflate.
MAX_TOOL_CALLS = 60
MAX_TOKENS = 400_000
_DEFAULT_BUDGET = Budget(tool_calls=40, tokens=200_000)

# One module's source can be a megabyte of XML. Past this the prompt stops
# being context and starts being a bill. The diff of a first port is the same
# problem wearing a different hat.
MAX_SOURCE_CHARS = 120_000
MAX_DIFF_CHARS = 60_000


def build_context(step: Step, ctx: StepContext) -> str:
    """Render the declared context items, in ``CONTEXT_ITEMS`` order."""
    wanted = set(step.context)
    parts = []
    for item in CONTEXT_ITEMS:
        if item not in wanted:
            continue
        parts.append(f"## {item}\n{_render(item, ctx)}")

    return "\n\n".join(parts)


def resolve_model(step: Step, ctx: StepContext) -> str:
    """Step model, else the migration card, else the cartridge's model."""

    return _chosen(step, ctx)[1]


def resolve_provider(step: Step, ctx: StepContext) -> str:
    """Step provider, else the migration card, else the cartridge's provider."""

    return _chosen(step, ctx)[0]


def _chosen(step: Step, ctx: StepContext) -> tuple[str, str]:
    if step.model is not None:

        return step.model.provider, step.model.name
    provider = (getattr(ctx.migration, "provider_override", None) or "").strip()
    model = (getattr(ctx.migration, "model_override", None) or "").strip()
    if provider and model:

        return provider, model
    named = ctx.cartridge.model
    if model:

        return named.provider, model

    return named.provider, named.name


def budget_for(step: Step, ctx: StepContext) -> Budget:
    """The step's budget, clamped to the ceiling. Narrowing only.

    A cartridge that asks for a hundred thousand tool calls gets the ceiling,
    not the number it typed.
    """
    del ctx
    asked = step.budget or _DEFAULT_BUDGET
    calls = asked.tool_calls if asked.tool_calls is not None else MAX_TOOL_CALLS
    tokens = asked.tokens if asked.tokens is not None else MAX_TOKENS

    return Budget(tool_calls=min(calls, MAX_TOOL_CALLS), tokens=min(tokens, MAX_TOKENS))


def system_prompt(step: Step, ctx: StepContext) -> str:
    """The standing instruction, plus which module and versions this is.

    ``prompts/system.md`` is the part that never changes; the line under it
    is what a step cannot work without and the cartridge should not have to
    repeat in every prompt it writes. Without any of this the model answers
    in prose, never calls a tool, and the step ends with nothing reported.
    """
    standing = load_system_prompt().strip()
    shell = (getattr(ctx, "shell_notes", "") or load_shell_notes()).strip()
    facts = (
        f"## This run\n"
        f"Module: {ctx.module.technical_name}\n"
        f"Step: {step.id or step.kind}\n"
        f"\n"
        f"## The two sides\n"
        f"source: Odoo {ctx.migration.source_version} — the version this module is "
        f"ported FROM. Read it; the only thing you may write there is a "
        f"characterization test, by `write_source_test`, when your step has that tool.\n"
        f"target: Odoo {ctx.migration.target_version} — the version this module is "
        f"ported TO. This is where the port is written.\n"
        # The paths `read_file` and `write_file` take. A project that keeps
        # its addons in a subdirectory is the common shape, and a model told
        # only the module's name writes one level too high.
        f"Its directory on the source side: {agent_path(ctx.migration, ctx.module, 'source')}\n"
        f"Its directory on the target side: {agent_path(ctx.migration, ctx.module, 'target')}"
    )

    return f"{standing}\n\n{shell}\n\n{facts}"


def run_agent_step(step: Step, ctx: StepContext) -> StepResult:
    """One agent step: render its context, then let the model report."""
    rendered = build_context(step, ctx)
    prompt = step.prompt
    if rendered:
        prompt = f"{rendered}\n\n{prompt}" if prompt else rendered
    from odoo_migrator.runner.providers import get
    from odoo_migrator.runner.tools import report_tool, tools_for

    # `ctx.provider` is an injected `Provider`, never a client object to be
    # sniffed for a `scripted` attribute. The branch that did that was the
    # one place a test drove a different loop than a live run.
    provider = ctx.provider or get(resolve_provider(step, ctx))
    provider.ensure_ready()
    result = provider.run_step(
        system=system_prompt(step, ctx),
        prompt=prompt,
        tools=tools_for(step, ctx),
        report=report_tool(step),
        model=resolve_model(step, ctx),
        budget=budget_for(step, ctx),
        stop_check=lambda: check_stop(ctx, f"during {step.id or step.kind}"),
    )
    if step.id and result.answer:
        ctx.prior_answers[step.id] = result.answer
    if result.answer:
        # What `ask_human` asks with. Without it a checkpoint showed a
        # constant string while the diagnosis sat in prior_answers unread.
        ctx.last_answer = result.answer
    # `last_failure` belongs to the loop. Setting it here as well meant a
    # `diagnose` that failed overwrote the failure it was called to explain,
    # and the `patch` gated after it in the same lane read the diagnosis's
    # own error as the thing to fix.

    return result


def _render(item: str, ctx: StepContext) -> str:
    if item == "rules":

        return _rules(ctx)
    if item == "project_notes":

        return project_notes(ctx)
    if item == "module_source":

        return _module_source(ctx)
    if item == "prior_answers":

        return _prior_answers(ctx)
    if item == "previous_diff":

        return _clipped(ctx.previous_diff, MAX_DIFF_CHARS) or "(none)"
    if item == "hand_changes":

        return _hand_changes(ctx)
    if item == "last_failure":

        return ctx.last_failure or "(none)"
    if item == "test_guide":

        return load_test_guide()

    return ""


# What a project says about itself, for the step that first reads a module.
# Matched without regard to case: `README.md` found `readme.md` on a Mac and
# not on Linux. In this order, within one directory.
NOTE_NAMES = ("agents.md", "claude.md", "readme.md", "readme.rst", "readme.txt", "readme")
# A module's own documentation directories. `readme/` is OCA's: README.rst is
# built from it, and not every checkout has the built one.
NOTE_DIRS = ("readme", "docs", "doc")
NOTE_SUFFIXES = (".md", ".rst", ".txt")
# The `readme/` fragments that say what a module does, in reading order.
# CONTRIBUTORS, CREDITS and HISTORY name people and releases, not behaviour.
OCA_FRAGMENTS = ("description", "context", "usage", "configure", "install", "roadmap")
MAX_NOTE_CHARS = 8000
MAX_NOTES_CHARS = 24000
# The module's own notes have a budget of their own. Under one budget, broad
# to narrow, a project's long READMEs used it up and the notes about this
# module were the ones cut.
MAX_MODULE_NOTES_CHARS = 24000


def project_notes(ctx: StepContext) -> str:
    """AGENTS.md, CLAUDE.md and READMEs on the source side, broad to narrow.

    The project directory (where the compose file is), then every addons
    directory the plan's modules are in, then the module's own directory with
    its `readme/`, `docs/` or `doc/`. Each file is clipped, and so is each
    of the two groups: a README can be a book.
    """
    from odoo_migrator.db import database
    from odoo_migrator.models import Module

    root_raw = (ctx.migration.old_project_path or "").strip()
    root = Path(root_raw).expanduser() if root_raw else None
    places: list[Path] = []
    if root is not None:
        places.append(root)
    with database.connection_context():
        paths = [
            row.old_path
            for row in Module.select(Module.old_path).where(
                (Module.migration == ctx.migration.id)
                & (Module.struck_out == False)
                & (Module.old_path != "")
            )
        ]
    places.extend(sorted({Path(path).parent for path in paths}))
    seen: set[Path] = set()
    sections: list[str] = []
    broad = [path for place in places for path in _note_files(place)]
    _collect(broad, root, seen, sections, MAX_NOTES_CHARS)
    module_dir = _old_root(ctx)
    if module_dir is not None:
        _collect(module_notes(Path(module_dir)), root, seen, sections, MAX_MODULE_NOTES_CHARS)

    return "\n\n".join(sections) if sections else "(no project notes found)"


def _note_files(place: Path) -> list[Path]:
    """The note files directly in `place`, in NOTE_NAMES order."""
    try:
        entries = [entry for entry in place.iterdir() if entry.name.lower() in NOTE_NAMES]
    except OSError:

        return []
    found = [entry for entry in entries if entry.is_file()]

    return sorted(found, key=lambda entry: (NOTE_NAMES.index(entry.name.lower()), entry.name))


def module_notes(module_dir: Path) -> list[Path]:
    """A module's own notes: its README and the like, then its `readme/`
    fragments, then whatever `docs/` or `doc/` holds as text."""
    found = _note_files(module_dir)
    try:
        directories = [
            entry
            for entry in module_dir.iterdir()
            if entry.name.lower() in NOTE_DIRS and entry.is_dir()
        ]
    except OSError:

        return found
    for directory in sorted(directories, key=lambda entry: NOTE_DIRS.index(entry.name.lower())):
        texts = [
            path
            for path in module_files(directory)
            if path.suffix.lower() in NOTE_SUFFIXES
        ]
        if directory.name.lower() == "readme":
            texts = sorted(
                (path for path in texts if path.stem.lower() in OCA_FRAGMENTS),
                key=lambda path: OCA_FRAGMENTS.index(path.stem.lower()),
            )
        found.extend(texts)

    return found


def _collect(
    paths: list[Path], root: Path | None, seen: set[Path], sections: list[str], limit: int
) -> None:
    total = 0
    for path in paths:
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        if len(text) > MAX_NOTE_CHARS:
            text = text[:MAX_NOTE_CHARS] + "\n… (clipped)"
        if total + len(text) > limit:
            sections.append("… (further notes clipped)")

            return
        total += len(text)
        sections.append(f"--- {_note_label(path, root)} ---\n{text}\n--- end ---")


def _note_label(path: Path, root: Path | None) -> str:
    if root is not None:
        try:

            return path.relative_to(root).as_posix()
        except ValueError:
            pass

    return str(path)


def _rules(ctx: StepContext) -> str:
    docs = assemble_rules(ctx.migration.source_version, ctx.migration.target_version)

    return "\n\n".join(_rule_doc(doc) for doc in docs)


def _rule_doc(doc: RuleDoc) -> str:
    """One rules file, delimited by name rather than by heading depth.

    These files carry their own `#` and `##` headings, so a `### 18.0`
    wrapper is outranked by the text it is meant to contain: a 17.0 → 20.0
    stack renders as four unattributed `## ORM` sections, and 17.0's "do not
    rename it to `aggregator`" ends up beside 18.0's "`group_operator` is now
    `aggregator`" with nothing saying which version each belongs to. The
    marker lines do not depend on what the file puts inside.
    """
    name = "common.md" if doc.version == "common" else f"{doc.version}.md"
    role = _ROLES.get(doc.role, doc.role)
    body = doc.text.strip()

    return f"--- rules/{name} — {role} ---\n{body}\n--- end rules/{name} ---"


# What a role means, in words. "source" and "target" are the app's names for
# the ends of the stack; a model given only the label has no reason to know
# that the source file describes what the module may stop doing, while the
# target file is the one it has to satisfy.
_ROLES = {
    "context": "shared context, ahead of the version files",
    "source": "the version this module is written for today",
    "intermediate": "passed through on the way, not landed on",
    "target": "the version to land on; satisfy this one",
}


# Translations: a .po file is tens of thousands of lines nobody ports, and
# `i18n/` sorts before `models/`, so a few of them used the whole budget.
SOURCE_SKIPPED_SUFFIXES = (".po", ".pot")


def _module_source(ctx: StepContext) -> str:
    """The module's text files, the code first.

    Alphabetical, the code came after `data/` and `i18n/`, and a module with
    a large data file was clipped before the model saw a single model. The
    manifest, Python, XML, the rest, the module's own notes, then `static/`.
    """
    root = _old_root(ctx)
    if root is None or not root.is_dir():

        return "(no module source)"
    notes = {path.relative_to(root).as_posix() for path in module_notes(root)}
    files = []
    for path in module_files(root):
        if path.suffix.lower() in SOURCE_SKIPPED_SUFFIXES:
            continue
        relative = path.relative_to(root).as_posix()
        files.append((_source_rank(relative, notes), relative, path))
    chunks = []
    for _rank, relative, path in sorted(files):
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        chunks.append(f"### {relative}\n{text}")
    rendered = "\n\n".join(chunks) or "(empty)"

    return _clipped(rendered, MAX_SOURCE_CHARS)


def _source_rank(relative: str, notes: set[str]) -> int:
    if relative == "__manifest__.py":

        return 0
    if relative.split("/", 1)[0] == "static":

        return 5
    if relative in notes:

        return 4
    if relative.endswith(".py"):

        return 1
    if relative.endswith(".xml"):

        return 2

    return 3


def _hand_changes(ctx: StepContext) -> str:
    """What a person committed in the target since the agent's last port.

    A re-run writes the module again from the source version, and a fix someone
    committed after the last port is not in the source version: without this it
    was written out of the working copy.
    """
    from odoo_migrator.target_git import person_changes

    directory = module_root(ctx.migration, ctx.module, "target")
    text = person_changes(directory) if directory is not None else ""

    return _clipped(text, MAX_DIFF_CHARS) or "(none)"


def _clipped(text: str, limit: int) -> str:
    if len(text) <= limit:

        return text

    return f"{text[:limit]}\n\n(truncated at {limit} characters; read the rest with read_file)"


def _old_root(ctx: StepContext) -> Path | None:

    return module_root(ctx.migration, ctx.module, "source")


# A person's answer should read as one, not as the name of a step kind.
_ANSWER_NAMES = {"ask_human": "ask_human (the answer a person gave to the question above)"}


def _prior_answers(ctx: StepContext) -> str:
    if not ctx.prior_answers:

        return "(none)"
    lines = [
        f"{_ANSWER_NAMES.get(step_id, step_id)}: {answer}"
        for step_id, answer in ctx.prior_answers.items()
    ]

    return "\n".join(lines)
