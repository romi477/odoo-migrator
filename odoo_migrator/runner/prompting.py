"""What an agent step is allowed to see, and which model runs it.

The cartridge lists context items. This module renders only those, in the
closed order, so a rewrite cannot be handed the previous attempt by accident.
"""

from __future__ import annotations

from pathlib import Path

from odoo_migrator.module_paths import module_root, relative_to_root
from odoo_migrator.rules import RuleDoc, assemble_rules, load_system_prompt
from odoo_migrator.runner.cartridge import CONTEXT_ITEMS, Budget, Step
from odoo_migrator.runner.context import StepContext, StepResult

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
    facts = (
        f"## This run\n"
        f"Module: {ctx.module.technical_name}\n"
        f"Odoo {ctx.migration.source_version} (old) to "
        f"{ctx.migration.target_version} (new)\n"
        # The paths `read_file` and `write_file` take. A project that keeps
        # its addons in a subdirectory is the common shape, and a model told
        # only the module's name writes one level too high.
        f"Its directory on the old side: {relative_to_root(ctx.migration, ctx.module, 'old')}\n"
        f"Its directory on the new side: {relative_to_root(ctx.migration, ctx.module, 'new')}\n"
        f"Step: {step.id or step.kind}"
    )

    return f"{standing}\n\n{facts}"


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
    if item == "module_source":

        return _module_source(ctx)
    if item == "prior_answers":

        return _prior_answers(ctx)
    if item == "previous_diff":

        return _clipped(ctx.previous_diff, MAX_DIFF_CHARS) or "(none)"
    if item == "last_failure":

        return ctx.last_failure or "(none)"

    return ""


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


def _module_source(ctx: StepContext) -> str:
    root = _old_root(ctx)
    if root is None or not root.is_dir():

        return "(no module source)"
    chunks = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        relative = path.relative_to(root).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        chunks.append(f"### {relative}\n{text}")
    rendered = "\n\n".join(chunks) or "(empty)"

    return _clipped(rendered, MAX_SOURCE_CHARS)


def _clipped(text: str, limit: int) -> str:
    if len(text) <= limit:

        return text

    return f"{text[:limit]}\n\n(truncated at {limit} characters; read the rest with read_file)"


def _old_root(ctx: StepContext) -> Path | None:

    return module_root(ctx.migration, ctx.module, "old")


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
