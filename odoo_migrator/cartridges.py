"""Import a YAML cartridge, pin it to a migration, smoke-run one module."""

from __future__ import annotations

import yaml

from odoo_migrator.contract import ContractError
from odoo_migrator.db import database
from odoo_migrator.models import CartridgeInstall, Job, Migration
from odoo_migrator.paths import repo_dir
from odoo_migrator.runner.cartridge import (
    KINDS,
    Cartridge,
    CartridgeError,
    load_cartridge,
    load_cartridge_file,
)


def install_from_text(text: str) -> CartridgeInstall:
    """Validate, then store. An invalid file never becomes a row."""
    try:
        loaded = load_cartridge(text)
    except CartridgeError as exc:
        kind = _unknown_step(text)
        if kind:
            raise CartridgeError(f"unknown step '{kind}'") from exc
        raise
    with database.connection_context():
        row = CartridgeInstall.get_or_none(
            (CartridgeInstall.cartridge_id == loaded.cartridge)
            & (CartridgeInstall.version == loaded.version)
        )
        if row is None:
            row = CartridgeInstall.create(
                cartridge_id=loaded.cartridge,
                version=loaded.version,
                sha256=loaded.sha256,
                schema_version=loaded.schema_version,
                body=text,
                smoke_status="untested",
            )
        else:
            row.sha256 = loaded.sha256
            row.schema_version = loaded.schema_version
            row.body = text
            row.save()

        return row


def pinned_for(migration) -> Cartridge:
    """The cartridge this migration pinned, or the stock file."""
    if isinstance(migration, dict):
        cartridge_id = migration.get("cartridge_id") or "stock"
        cartridge_version = migration.get("cartridge_version") or "1.0.0"
    else:
        cartridge_id = migration.cartridge_id or "stock"
        cartridge_version = migration.cartridge_version or "1.0.0"
    with database.connection_context():
        row = CartridgeInstall.get_or_none(
            (CartridgeInstall.cartridge_id == cartridge_id)
            & (CartridgeInstall.version == cartridge_version)
        )
        body = row.body if row is not None else ""
    if body:

        return load_cartridge(body)

    return load_cartridge_file(repo_dir() / "cartridges" / "stock.yaml")


def list_installs() -> list[dict]:
    with database.connection_context():
        rows = list(CartridgeInstall.select().order_by(CartridgeInstall.cartridge_id))

    return [
        {
            "cartridge_id": row.cartridge_id,
            "version": row.version,
            "sha256": row.sha256,
            "schema_version": row.schema_version,
            "smoke_status": row.smoke_status,
        }
        for row in rows
    ]


def smoke_allows_batch(migration: Migration) -> bool:
    """A pin nobody has smoke-run does not get to run two hundred modules.

    With nothing installed at all the app is on its own stub and there is
    no cartridge to smoke-run, so that case stays open. Once a cartridge
    has been imported, a pin that matches no install is a mistake worth
    stopping on rather than the one case that skips the gate.
    """
    with database.connection_context():
        if not CartridgeInstall.select().exists():

            return True
        row = CartridgeInstall.get_or_none(
            (CartridgeInstall.cartridge_id == (migration.cartridge_id or "stock"))
            & (CartridgeInstall.version == (migration.cartridge_version or "1.0.0"))
        )
        if row is None:

            return False

        return row.smoke_status == "passed"


def record_smoke(migration_id: str, status: str) -> None:
    """Write the verdict against the cartridge this migration has pinned."""
    with database.connection_context():
        migration = Migration.get_by_id(migration_id)
        cid = migration.cartridge_id or "stock"
        cver = migration.cartridge_version or "1.0.0"
        row = CartridgeInstall.get_or_none(
            (CartridgeInstall.cartridge_id == cid) & (CartridgeInstall.version == cver)
        )
        if row is None:
            CartridgeInstall.create(
                cartridge_id=cid,
                version=cver,
                smoke_status=status,
            )
        else:
            row.smoke_status = status
            row.save()


def smoke_run(migration_id: str, executor, hub, *, module_id: int | None = None) -> str:
    """Start the smoke-run. The verdict is whatever the job ends as."""
    from odoo_migrator.graph import list_modules
    from odoo_migrator.jobs import start_job

    if module_id is None:
        modules = [row for row in list_modules(migration_id) if not row.get("struck_out")]
        if not modules:
            raise ContractError("No module to smoke-run.")
        module_id = min(modules, key=lambda row: row["technical_name"])["id"]
    record_smoke(migration_id, "running")
    job = start_job(
        migration_id=migration_id,
        module_id=module_id,
        executor=executor,
        hub=hub,
        smoke=True,
    )
    with database.connection_context():

        return Job.get(Job.job_id == job.job_id).status


def _unknown_step(text: str) -> str | None:
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError:

        return None
    if not isinstance(raw, dict):

        return None
    found = None
    for key in ("pipeline",):
        for item in raw.get(key) or []:
            if not isinstance(item, dict):
                continue
            kind = item.get("step") or item.get("kind")
            if kind and kind not in KINDS:
                found = str(kind)
    lane = raw.get("on_failure") or {}
    if isinstance(lane, dict):
        for item in lane.get("pipeline") or []:
            if isinstance(item, dict):
                kind = item.get("step") or item.get("kind")
                if kind and kind not in KINDS:
                    found = str(kind)

    return found
