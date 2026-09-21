"""Cartridge manifests, smoke-run, pin. Task 16."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from odoo_migrator.contract import CONTRACT_VERSION, ContractError
from odoo_migrator.db import database
from odoo_migrator.models import CartridgeInstall, Job, Migration


class CartridgeManifest(BaseModel):
    model_config = ConfigDict(extra="ignore")
    id: str
    version: str
    contract_version: str
    webhook_path: str
    wrappers: dict[str, str] = {}
    capabilities: dict = {}


def load_manifest(source: Path | dict) -> CartridgeManifest:
    data = source if isinstance(source, dict) else json.loads(
        Path(source).read_text(encoding="utf-8")
    )
    manifest = CartridgeManifest.model_validate(data)
    if manifest.contract_version != CONTRACT_VERSION:
        raise ContractError(
            f"This app speaks contract_version {CONTRACT_VERSION}; "
            f"the cartridge declares {manifest.contract_version}."
        )

    return manifest


def record_install(manifest: CartridgeManifest) -> CartridgeInstall:
    with database.connection_context():
        row = CartridgeInstall.get_or_none(
            (CartridgeInstall.cartridge_id == manifest.id)
            & (CartridgeInstall.version == manifest.version)
        )
        if row is None:
            row = CartridgeInstall.create(
                cartridge_id=manifest.id,
                version=manifest.version,
                contract_version=manifest.contract_version,
                webhook_path=manifest.webhook_path,
                capabilities=json.dumps(manifest.capabilities),
                smoke_status="untested",
            )
        else:
            row.contract_version = manifest.contract_version
            row.webhook_path = manifest.webhook_path
            row.capabilities = json.dumps(manifest.capabilities)
            row.save()

        return row


def list_installs() -> list[dict]:
    with database.connection_context():
        rows = list(CartridgeInstall.select().order_by(CartridgeInstall.cartridge_id))

    return [
        {
            "cartridge_id": row.cartridge_id,
            "version": row.version,
            "contract_version": row.contract_version,
            "webhook_path": row.webhook_path,
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
                contract_version=CONTRACT_VERSION,
                webhook_path="migrate-module",
                smoke_status=status,
            )
        else:
            row.smoke_status = status
            row.save()


def smoke_run(migration_id: str, executor, hub) -> str:
    """Start the smoke-run. The verdict is whatever the job ends as.

    It used to read the job's status straight after handing the trigger
    over, which is ``running`` until a callback arrives — so against any
    real executor the smoke-run recorded ``failed`` every time, and since
    a batch needs ``passed`` that blocked every batch for ever. The stub
    answers inline and was the only thing this ever worked against.
    """
    from odoo_migrator.graph import list_modules
    from odoo_migrator.jobs import start_job

    modules = [row for row in list_modules(migration_id) if not row.get("struck_out")]
    if not modules:
        raise ContractError("No module to smoke-run.")
    module = min(modules, key=lambda row: row["technical_name"])
    record_smoke(migration_id, "running")
    job = start_job(
        migration_id=migration_id,
        module_id=module["id"],
        executor=executor,
        hub=hub,
        smoke=True,
    )
    with database.connection_context():

        return Job.get(Job.job_id == job.job_id).status
