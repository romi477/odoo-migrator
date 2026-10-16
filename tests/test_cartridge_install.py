"""A YAML cartridge is imported, pinned, and recorded on the job that used it."""

from pathlib import Path

import pytest

from odoo_migrator.cartridges import install_from_text
from odoo_migrator.db import database
from odoo_migrator.jobs import start_job
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import CartridgeInstall, Migration, Module

STOCK_TEXT = (Path("cartridges/stock.yaml")).read_text(encoding="utf-8")


@pytest.fixture
def migration(client, tmp_path):
    del client
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()

    return create_migration(
        name="Acme",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path=str(old),
        new_project_path=str(new),
    )


def test_a_run_records_the_cartridge_it_used(client, migration):
    install = install_from_text(STOCK_TEXT)
    with database.connection_context():
        module = Module.create(migration=migration["id"], technical_name="sale_custom")
        module_id = module.id
    job = start_job(
        migration_id=migration["id"],
        module_id=module_id,
        executor=client.app.state.executor,
        hub=client.app.state.hub,
        cartridge=install,
    )

    assert job.cartridge_id == "stock"
    assert job.cartridge_version == "1.0.0"
    assert job.cartridge_sha256 == install.sha256
    # The card has not chosen, so the job records the flow's default.
    assert job.provider == "anthropic"
    assert job.model_name == "claude-sonnet-5-5"


def test_a_run_records_the_pair_the_card_saved(client, migration):
    install_from_text(STOCK_TEXT)
    with database.connection_context():
        row = Migration.get_by_id(migration["id"])
        row.provider_override = "cursor"
        row.model_override = "composer-2.5"
        row.save()
        module = Module.create(migration=migration["id"], technical_name="sale_custom")
        module_id = module.id
    job = start_job(
        migration_id=migration["id"],
        module_id=module_id,
        executor=client.app.state.executor,
        hub=client.app.state.hub,
    )

    assert job.provider == "cursor"
    assert job.model_name == "composer-2.5"


def test_an_invalid_upload_is_refused_before_it_is_stored(client, migration):
    page = client.post(
        f"/migrations/{migration['id']}/cartridge",
        files={"file": ("bad.yaml", b"pipeline: [{step: nope}]")},
        follow_redirects=True,
    )

    assert page.status_code == 200
    assert "step" in page.text
    with database.connection_context():
        assert CartridgeInstall.select().count() == 0


def _migration_with(tmp_path, cartridge_id: str, version: str):
    from odoo_migrator.db import database
    from odoo_migrator.migrations import create_migration
    from odoo_migrator.models import Migration

    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir(exist_ok=True)
    new.mkdir(exist_ok=True)
    created = create_migration(
        name="Acme",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path=str(old),
        new_project_path=str(new),
    )
    with database.connection_context():
        row = Migration.get_by_id(created["id"])
        row.cartridge_id = cartridge_id
        row.cartridge_version = version
        row.save()

    return row


def test_a_job_records_the_hash_of_the_stock_file_it_runs(client, tmp_path):
    """An empty sha256 was on every job. Pinning exists to say which text
    ran, and the stock file is a real cartridge with a real hash."""
    del client
    from odoo_migrator.cartridges import pinned_for
    from odoo_migrator.jobs import _pinned_identity

    migration = _migration_with(tmp_path, "stock", "1.0.0")

    cartridge_id, version, sha = _pinned_identity(migration, None)

    assert (cartridge_id, version) == ("stock", "1.0.0")
    assert sha == pinned_for(migration).sha256
    assert len(sha) == 64


def test_the_runner_loads_the_cartridge_the_card_pinned(client, tmp_path, fake_sheller):
    """The payload carries no text, so the runner used to load the stock file
    whatever the card said: an imported cartridge was pinned, recorded and
    drawn, and never run."""
    del client
    from odoo_migrator.cartridges import install_from_text
    from odoo_migrator.db import database
    from odoo_migrator.models import Batch, Job, Module
    from odoo_migrator.runner.native import NativeRunner

    text = """
cartridge: house
version: 2.0.0
schema: 1
model: {provider: anthropic, name: claude-opus-5}
pipeline:
  - step: verdict
on_failure:
  pipeline: []
  retry:
    resume_at: verdict
"""
    install_from_text(text)
    migration = _migration_with(tmp_path, "house", "2.0.0")
    with database.connection_context():
        module = Module.create(migration=migration, technical_name="sale_custom")
        batch = Batch.create(migration=migration, status="running")
        job = Job.create(batch=batch, module=module, status="queued")

    runner = NativeRunner(sheller=fake_sheller, hub=None)
    loaded = runner._cartridge({"job_id": job.job_id})

    assert loaded.cartridge == "house"
    assert [step.kind for step in loaded.pipeline] == ["verdict"]


def _variant(cartridge_id: str, version: str = "1.0.0", note: str = "") -> str:
    text = STOCK_TEXT.replace("cartridge: stock", f"cartridge: {cartridge_id}", 1)
    text = text.replace("version: 1.0.0", f"version: {version}", 1)

    return text + (f"\n# {note}\n" if note else "")


def test_importing_a_flow_does_not_lock_the_stock_flow_out(client, migration):
    """The stock file ships with the app and has no install row. Once any
    flow was imported, the gate asked for that missing row and every
    migration on stock answered "A smoke-run must pass before a batch"."""
    from odoo_migrator.cartridges import smoke_allows_batch

    install_from_text(_variant("acme_flow"))

    with database.connection_context():
        on_stock = Migration.get_by_id(migration["id"])

    assert smoke_allows_batch(on_stock)


def test_the_card_offers_a_smoke_run_for_a_pin_that_has_not_passed(client, migration):
    """The gate asks for a passed smoke-run, and nothing on any screen
    started one: an imported flow could never run."""
    client.post(
        f"/migrations/{migration['id']}/cartridge",
        files={"file": ("acme.yaml", _variant("acme_flow").encode())},
    )

    card = client.get(f"/?settings={migration['id']}").text

    assert f'action="/migrations/{migration["id"]}/smoke"' in card
    assert "untested" in card


def test_import_pins_the_flow_it_imported(client, migration):
    """The newest row by id is not the one just imported when that flow was
    imported before: the row is updated in place and keeps its old id."""
    install_from_text(_variant("first_flow"))
    install_from_text(_variant("second_flow"))

    client.post(
        f"/migrations/{migration['id']}/cartridge",
        files={"file": ("first.yaml", _variant("first_flow", note="again").encode())},
    )

    with database.connection_context():
        pinned = Migration.get_by_id(migration["id"])
    assert pinned.cartridge_id == "first_flow"


def test_a_changed_text_needs_a_new_smoke_run(client, migration):
    """A smoke-run passed one text. The same id and version with another
    text is not what was tried."""
    row = install_from_text(_variant("acme_flow"))
    with database.connection_context():
        CartridgeInstall.update(smoke_status="passed").where(
            CartridgeInstall.id == row.id
        ).execute()

    again = install_from_text(_variant("acme_flow", note="edited"))

    assert again.smoke_status == "untested"
