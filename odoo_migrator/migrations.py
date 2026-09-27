"""Active migrations. Each call opens its own connection_context."""

from datetime import UTC, datetime
from pathlib import Path

from peewee import fn

from odoo_migrator.db import database
from odoo_migrator.models import ACCENTS, Migration, Module
from odoo_migrator.rules import check_versions


def _row(migration: Migration) -> dict:

    return {
        "id": migration.id,
        "name": migration.name,
        "accent_color": migration.accent_color,
        "source_version": migration.source_version,
        "target_version": migration.target_version,
        "old_project_path": migration.old_project_path,
        "new_project_path": migration.new_project_path,
        "strictness_default": migration.strictness_default,
        "retry_limit": migration.retry_limit,
        "ask_human_mode": migration.ask_human_mode,
        "status": migration.status,
        "old_container": migration.old_container,
        "new_container": migration.new_container,
        "cartridge_id": migration.cartridge_id or "stock",
        "cartridge_version": migration.cartridge_version or "1.0.0",
        "model_webhook_path": migration.model_webhook_path or "migrate-module",
        "old_database": migration.old_database or "",
        "new_database": migration.new_database or "",
        "provider_override": migration.provider_override or "",
        "model_override": migration.model_override or "",
    }


def _progress(counts: dict | None) -> dict:
    """The collapsed-row panel: how many modules moved, how many need a human,
    how many failed, and the two bar widths as percentages."""
    counts = counts or {}
    total = counts.get("total", 0)
    passed = counts.get("passed", 0)
    failed = counts.get("failed", 0)
    waiting = counts.get("waiting", 0)

    def pct(part: int) -> str:

        return f"{part / total * 100:.2f}%" if total else "0%"

    return {
        "total": total,
        "passed": passed,
        "failed": failed,
        "waiting": waiting,
        "reopenable": counts.get("reopenable", 0),
        "pct_done": pct(passed),
        "pct_bad": pct(failed),
        "has_modules": total > 0,
    }


def _module_counts() -> dict[str, dict]:
    """Per-migration module tallies in one grouped query, so a long list does
    not fan out into a query per row. The caller holds the connection."""
    counts: dict[str, dict] = {}
    query = (
        Module.select(
            Module.migration,
            Module.status,
            fn.COUNT(Module.id).alias("n"),
        )
        .where(~Module.struck_out)
        .group_by(Module.migration, Module.status)
    )
    for row in query:
        bucket = counts.setdefault(
            row.migration_id, {"total": 0, "passed": 0, "failed": 0, "waiting": 0}
        )
        bucket["total"] += row.n
        if row.status == "passed":
            bucket["passed"] += row.n
        elif row.status == "failed":
            bucket["failed"] += row.n
        elif row.status == "waiting_for_human":
            bucket["waiting"] += row.n

    return counts


def list_active() -> list[dict]:
    with database.connection_context():
        query = (
            Migration.select()
            .where(Migration.archived_at.is_null())
            .order_by(Migration.name)
        )
        rows = [_row(row) for row in query]
        counts = _module_counts()
    for row in rows:
        row["progress"] = _progress(counts.get(row["id"]))

    return rows


def _result_display_status(row: dict) -> str:
    if row.get("struck_out"):

        return "excluded"

    return row.get("status") or "queued"


def _result_corner(row: dict) -> str:
    """Corner badge on Result cards: done, waiting, or excluded."""
    if row.get("struck_out"):

        return "excluded"
    status = row.get("status") or "queued"
    if status == "passed":

        return "done"
    if status == "waiting_for_human":

        return "waiting"

    return ""


def _result_subtitle(row: dict) -> str:
    if row.get("struck_out"):

        return "excluded — skipped on Tree"
    deps = row.get("depends") or []
    if not deps:

        return "—"
    head = ", ".join(deps[:3])
    if len(deps) > 3:
        head += ", …"

    return head


def _result_module(row: dict) -> dict:
    step = row.get("step") or 1
    step_class = step if 1 <= step <= 8 else "more"

    return {
        **row,
        "subtitle": _result_subtitle(row),
        "step_class": step_class,
        "display_status": _result_display_status(row),
        "result_corner": _result_corner(row),
    }


RESULT_STEPS_PER_PAGE = 4


def migration_results(migration_id: str) -> dict:
    """Tallies and step columns for the Result dashboard."""
    from odoo_migrator.graph import list_modules

    listed = list_modules(migration_id)
    active = [row for row in listed if not row["struck_out"]]
    counts = {
        "total": len(active),
        "passed": 0,
        "failed": 0,
        "waiting": 0,
        "reopenable": 0,
    }
    for row in active:
        if row["status"] == "passed":
            counts["passed"] += 1
        elif row["status"] == "failed":
            counts["failed"] += 1
        elif row["status"] == "waiting_for_human":
            counts["waiting"] += 1
        if row["status"] in ("passed", "failed", "unknown"):
            counts["reopenable"] += 1
    by_step: dict[int, list[dict]] = {}
    for row in listed:
        enriched = _result_module(row)
        step = enriched.get("step") or 1
        by_step.setdefault(step, []).append(enriched)
    steps = [
        {
            "step": step,
            "modules": sorted(by_step[step], key=lambda row: row["technical_name"]),
        }
        for step in sorted(by_step)
    ]

    step_count = len(steps)
    if step_count:
        step_page_count = (
            step_count + RESULT_STEPS_PER_PAGE - 1
        ) // RESULT_STEPS_PER_PAGE
    else:
        step_page_count = 0

    return {
        "progress": _progress(counts),
        "steps": steps,
        "steps_per_page": RESULT_STEPS_PER_PAGE,
        "step_page_count": step_page_count,
    }


def suggest_accent() -> str:
    """The accent least used by active migrations.

    A fixed default would make every migration the same colour, which is the
    one thing the accent exists to prevent: telling two open migrations apart
    at a glance. Least-used rather than random so the first six are distinct.
    """
    with database.connection_context():
        taken = [row.accent_color for row in Migration.select().where(Migration.archived_at.is_null())]

    return min(ACCENTS, key=lambda key: (taken.count(ACCENTS[key]), list(ACCENTS).index(key)))


def get_active(migration_id: str | None) -> dict | None:
    if not migration_id:

        return None
    with database.connection_context():
        row = Migration.get_or_none(
            (Migration.id == migration_id) & (Migration.archived_at.is_null())
        )
        if row is None:

            return None

        return _row(row)


class ProjectPathsError(ValueError):
    """The two project roots overlap. The old one is the only copy of the source."""


def check_project_paths(old: str, new: str) -> None:
    """Refuse two roots that are the same directory, or where one holds the other.

    Nothing compared them while nothing wrote to the old side. A step marked
    `fresh` empties the module's directory on the new side, and the module is
    looked up under the new root: with equal roots, or a new root above the
    old one, that directory is the old module — the only copy of its source.
    `write_file` on the new side lands in it just the same.
    """
    old, new = (old or "").strip(), (new or "").strip()
    if not old or not new:

        return
    old_root = Path(old).expanduser().resolve()
    new_root = Path(new).expanduser().resolve()
    if old_root == new_root:
        raise ProjectPathsError("The old and the new project are the same directory.")
    if old_root.is_relative_to(new_root) or new_root.is_relative_to(old_root):
        raise ProjectPathsError(
            "One project directory is inside the other. The port writes under the "
            "new one, and must not be able to reach the old module's source."
        )


def create_migration(
    *,
    name: str,
    accent: str,
    source_version: str,
    target_version: str,
    old_project_path: str,
    new_project_path: str,
) -> dict | None:
    if accent not in ACCENTS:

        return None
    # Refuse an impossible version pair here, where someone typed it, rather
    # than when the first job is assembled several screens later.
    check_versions(source_version.strip(), target_version.strip())
    check_project_paths(old_project_path, new_project_path)
    with database.connection_context():
        row = Migration.create(
            name=name.strip(),
            accent_color=ACCENTS[accent],
            source_version=source_version.strip(),
            target_version=target_version.strip(),
            old_project_path=old_project_path.strip(),
            new_project_path=new_project_path.strip(),
        )

        return _row(row)


def archive_migration(migration_id: str) -> bool:
    with database.connection_context():
        row = Migration.get_or_none(
            (Migration.id == migration_id) & (Migration.archived_at.is_null())
        )
        if row is None:

            return False
        row.archived_at = datetime.now(UTC)
        row.save()

        return True


def save_migration_settings(
    migration_id: str,
    *,
    strictness_default: str,
    retry_limit: int,
    ask_human_mode: str,
    cartridge_id: str | None = None,
    cartridge_version: str | None = None,
    model_webhook_path: str | None = None,
    old_database: str | None = None,
    new_database: str | None = None,
    provider_override: str | None = None,
    model_override: str | None = None,
) -> dict | None:
    if strictness_default not in ("basic", "enhanced"):

        return None
    if ask_human_mode not in ("sync", "batch_defer"):

        return None
    if retry_limit < 1:

        return None
    with database.connection_context():
        row = Migration.get_or_none(
            (Migration.id == migration_id) & (Migration.archived_at.is_null())
        )
        if row is None:

            return None
        row.strictness_default = strictness_default
        row.retry_limit = retry_limit
        row.ask_human_mode = ask_human_mode
        if cartridge_id is not None:
            row.cartridge_id = cartridge_id.strip() or None
        if cartridge_version is not None:
            row.cartridge_version = cartridge_version.strip() or None
        if model_webhook_path is not None:
            row.model_webhook_path = model_webhook_path.strip()
        # A blank field means the container's own config, which is what the
        # hint under it says. There is nothing to validate here: whether the
        # database exists is answered by the probe, at the start of a run.
        if old_database is not None:
            row.old_database = old_database.strip()
        if new_database is not None:
            row.new_database = new_database.strip()
        if provider_override is not None or model_override is not None:
            provider = (provider_override or "").strip()
            name = (model_override if model_override is not None else row.model_override).strip()
            if provider or name:
                from odoo_migrator.runner.providers import get

                try:
                    chosen = get(provider)
                except KeyError:

                    return None
                if not name or not chosen.accepts_model(name):

                    return None
            row.provider_override = provider
            row.model_override = name
        row.save()

        return _row(row)


def save_side_container(migration_id: str, side: str, container: str) -> None:
    if side not in ("old", "new"):

        return
    with database.connection_context():
        row = Migration.get_or_none(
            (Migration.id == migration_id) & (Migration.archived_at.is_null())
        )
        if row is None:

            return
        if side == "old":
            row.old_container = container
        else:
            row.new_container = container
        row.save()
