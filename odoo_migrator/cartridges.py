"""Import a YAML cartridge, pin it to a migration, smoke-run one module."""

from __future__ import annotations

import re

import yaml

from odoo_migrator.contract import ContractError
from odoo_migrator.db import database
from odoo_migrator.models import CartridgeInstall, Job, Migration
from odoo_migrator.paths import cartridges_dir
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
            # A smoke-run passed one text. Another text under the same id and
            # version is not what it tried.
            if row.sha256 != loaded.sha256:
                row.smoke_status = "untested"
            row.sha256 = loaded.sha256
            row.schema_version = loaded.schema_version
            row.body = text
            row.save()

        return row


# What an id and a version may be made of. The select on a card carries
# `id@version`, split at the first `@`, and the viewer's address is
# `/api/flows/<id>/<version>`: an `@` or a `/` would break either.
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,39}")


class FlowTaken(CartridgeError):
    """A flow by that id and version is already there, and stays as it is."""

    def __init__(self, cartridge_id: str, version: str):
        self.cartridge_id = cartridge_id
        self.version = version
        super().__init__(
            f"{cartridge_id}@{version} already exists. A fork is saved under a new "
            "version or a new id; it never replaces a flow."
        )


def _held(cartridge_id: str, version: str) -> bool:
    """A row by that name, with or without a body. Needs an open connection."""

    return (
        CartridgeInstall.select()
        .where(
            (CartridgeInstall.cartridge_id == cartridge_id)
            & (CartridgeInstall.version == version)
        )
        .exists()
    )


def flow_taken(cartridge_id: str, version: str) -> bool:
    """Whether a flow answers to this id and version.

    A row counts even without a body: a smoke verdict is recorded against a
    pin, and a fork may not take over a name that holds one. The stock file
    that ships with the app has no row at all.
    """
    from odoo_migrator.flowfile import flow_text

    with database.connection_context():
        held = _held(cartridge_id, version)

    return held or flow_text(cartridge_id, version) is not None


def next_version(cartridge_id: str, version: str) -> str:
    """The first version after this one that no flow of the id holds.

    The last number after a dot goes up by one; a version that does not end
    in one gets `.1` after it. It is a suggestion: the check that counts is
    the one `fork_from_text` makes when the fork is saved.
    """
    candidate = version
    while True:
        head, dot, last = candidate.rpartition(".")
        candidate = f"{head}{dot}{int(last) + 1}" if last.isdigit() else f"{candidate}.1"
        if not flow_taken(cartridge_id, candidate):

            return candidate


def fork_from_text(text: str) -> CartridgeInstall:
    """Store a flow under the id and version its own text names, as a new row.

    Never replaces: a name that is taken is refused, even for the same text.
    That is what `install_from_text` does on purpose for an import, and what a
    fork made from a viewer must not do to the flow it was copied from.
    """
    try:
        loaded = load_cartridge(text)
    except CartridgeError as exc:
        kind = _unknown_step(text)
        if kind:
            raise CartridgeError(f"unknown step '{kind}'") from exc
        raise
    from odoo_migrator.flowfile import flow_text

    for label, value in (("id", loaded.cartridge), ("version", loaded.version)):
        if not _NAME.fullmatch(value):
            raise CartridgeError(
                f"The flow {label} {value!r} is not allowed: it may hold only letters, "
                "digits and . _ + -, start with a letter or digit, and be at most "
                "40 characters."
            )
    taken = FlowTaken(loaded.cartridge, loaded.version)
    if flow_text(loaded.cartridge, loaded.version) is not None:
        raise taken
    with database.connection_context(), database.atomic():
        if _held(loaded.cartridge, loaded.version):
            raise taken

        return CartridgeInstall.create(
            cartridge_id=loaded.cartridge,
            version=loaded.version,
            sha256=loaded.sha256,
            schema_version=loaded.schema_version,
            body=text,
            smoke_status="untested",
        )


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

    return load_cartridge_file(cartridges_dir() / "stock.yaml")


def unreadable_flow(cartridge_id: str, version: str) -> str:
    """Why a stored flow no longer loads, or "" when it loads or is not there.

    A flow is validated when it is stored, and the rules for a valid one move
    on. Pinning one that no longer loads was accepted, and every page that
    reads the pinned flow then answered 500.
    """
    from odoo_migrator.flowfile import flow_text

    text = flow_text(cartridge_id, version)
    if text is None:

        return ""
    try:
        load_cartridge(text)
    except CartridgeError as exc:

        return str(exc)

    return ""


def list_installs() -> list[dict]:
    """The flows a card can pin: the one that ships first, then what is stored.

    The shipped file has no row of its own, so it is added unless a row holds
    its name already (a smoke verdict makes one). Without it, storing any flow
    took `stock` out of the Flow select, and a card pinned to it showed
    another flow as its choice.
    """
    with database.connection_context():
        rows = list(CartridgeInstall.select().order_by(CartridgeInstall.cartridge_id))
    found = [
        {
            "cartridge_id": row.cartridge_id,
            "version": row.version,
            "sha256": row.sha256,
            "schema_version": row.schema_version,
            "smoke_status": row.smoke_status,
        }
        for row in rows
    ]
    shipped = load_cartridge_file(cartridges_dir() / "stock.yaml")
    if any((r["cartridge_id"], r["version"]) == (shipped.cartridge, shipped.version) for r in found):

        return found

    return [
        {
            "cartridge_id": shipped.cartridge,
            "version": shipped.version,
            "sha256": shipped.sha256,
            "schema_version": shipped.schema_version,
            "smoke_status": "shipped",
        },
        *found,
    ]


def smoke_allows_batch(migration: Migration) -> bool:
    """A pin nobody has smoke-run does not get to run two hundred modules.

    With nothing installed at all the app is on its own stub and there is
    no cartridge to smoke-run, so that case stays open. Once a cartridge
    has been imported, a pin that matches no install is a mistake worth
    stopping on rather than the one case that skips the gate — except the
    stock flow, which ships with the app and has no install row: asking for
    one locked every migration on stock out the moment any flow was
    imported.
    """
    cartridge_id = migration.cartridge_id or "stock"
    with database.connection_context():
        if not CartridgeInstall.select().exists():

            return True
        row = CartridgeInstall.get_or_none(
            (CartridgeInstall.cartridge_id == cartridge_id)
            & (CartridgeInstall.version == (migration.cartridge_version or "1.0.0"))
        )
        if row is None:

            return cartridge_id == "stock"

        return row.smoke_status == "passed"


def smoke_state(migration) -> dict:
    """What the card says about the pin's smoke-run, and whether one is owed."""
    cartridge_id = migration.get("cartridge_id") or "stock"
    version = migration.get("cartridge_version") or "1.0.0"
    with database.connection_context():
        row = CartridgeInstall.get_or_none(
            (CartridgeInstall.cartridge_id == cartridge_id)
            & (CartridgeInstall.version == version)
        )
    status = row.smoke_status if row is not None else ("shipped" if cartridge_id == "stock" else "missing")

    return {"status": status, "owed": status not in ("passed", "shipped", "running")}


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
