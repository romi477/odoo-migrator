"""A YAML cartridge is imported, pinned, and recorded on the job that used it."""

from pathlib import Path

import pytest

from odoo_migrator.cartridges import install_from_text
from odoo_migrator.db import database
from odoo_migrator.jobs import start_job
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import CartridgeInstall, Module

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
