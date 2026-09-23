"""What an agent step is allowed to see, and which model runs it.

The cartridge lists context items. This module renders only those, in the
closed order, so a rewrite cannot be handed the previous attempt by accident.
"""

from __future__ import annotations

from pathlib import Path

from odoo_migrator.rules import assemble_rules
from odoo_migrator.runner.agent import run_agent
from odoo_migrator.runner.cartridge import CONTEXT_ITEMS, Budget, Step
from odoo_migrator.runner.context import StepContext, StepResult

# Used when neither the step nor the migration card names a tighter budget.
_DEFAULT_BUDGET = Budget(tool_calls=40, tokens=200_000)


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
    if step.model is not None:

        return step.model.name
    override = (getattr(ctx.migration, "model_override", None) or "").strip()
    if override:

        return override

    return ctx.cartridge.model.name


def budget_for(step: Step, ctx: StepContext) -> Budget:
    """The step's budget when it names one, otherwise the default ceiling."""
    del ctx

    return step.budget or _DEFAULT_BUDGET


def run_agent_step(step: Step, ctx: StepContext) -> StepResult:
    """One agent step: render its context, then let the model report."""
    rendered = build_context(step, ctx)
    prompt = step.prompt
    if rendered:
        prompt = f"{rendered}\n\n{prompt}" if prompt else rendered
    running = step.model_copy(update={"prompt": prompt})
    result = run_agent(
        running,
        ctx,
        client=ctx.client,
        model=resolve_model(step, ctx),
        budget=budget_for(step, ctx),
    )
    if step.id and result.answer:
        ctx.prior_answers[step.id] = result.answer
    if not result.ok:
        ctx.last_failure = result.output or ""

    return result


def _render(item: str, ctx: StepContext) -> str:
    if item == "rules":

        return _rules(ctx)
    if item == "module_source":

        return _module_source(ctx)
    if item == "prior_answers":

        return _prior_answers(ctx)
    if item == "previous_diff":

        return ctx.previous_diff or "(none)"
    if item == "last_failure":

        return ctx.last_failure or "(none)"

    return ""


def _rules(ctx: StepContext) -> str:
    docs = assemble_rules(ctx.migration.source_version, ctx.migration.target_version)
    chunks = [f"### {doc.version} ({doc.role})\n{doc.text}" for doc in docs]

    return "\n\n".join(chunks)


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

    return "\n\n".join(chunks) or "(empty)"


def _old_root(ctx: StepContext) -> Path | None:
    if ctx.module.old_path:

        return Path(ctx.module.old_path)
    project = ctx.migration.old_project_path
    if not project:

        return None

    return Path(project) / ctx.module.technical_name


def _prior_answers(ctx: StepContext) -> str:
    if not ctx.prior_answers:

        return "(none)"
    lines = [f"{step_id}: {answer}" for step_id, answer in ctx.prior_answers.items()]

    return "\n".join(lines)
