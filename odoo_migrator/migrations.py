"""Active migrations. Each call opens its own connection_context."""

from datetime import UTC, datetime

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
