"""Which database each side runs on.

`db_name` in a container's odoo.conf was the only answer, and a config
without it — ordinary for an image that serves several databases — was a
hard refusal. The live machine's Postgres holds forty; nothing in the app
could say which one the migration is about.
"""

import pytest

from odoo_migrator.db import database
from odoo_migrator.migrations import create_migration, save_migration_settings
from odoo_migrator.models import Migration
from odoo_migrator.runner.native import NativeRunner


class _Probe:
    """Answers probe the way the daemon does, and nothing else."""

    def __init__(self, db_name, databases):
        self._reply = {"db_name": db_name, "databases": databases, "odoo_bin": "/opt/odoo/odoo-bin"}
        self.asked = []

    def probe(self, container):
        self.asked.append(container)

        return dict(self._reply)


def _migration(tmp_path, **extra):
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
        row.old_container = "mig17"
        row.new_container = "mig18"
        for key, value in extra.items():
            setattr(row, key, value)
        row.save()

    return row


def test_the_card_beats_the_container_config(client, tmp_path):
    del client
    migration = _migration(tmp_path, old_database="mig_test_17", new_database="mig_test_18")
    sheller = _Probe("integra_db_19_presta", ["mig_test_17", "mig_test_18", "integra_db_19_presta"])

    targets = NativeRunner(sheller=sheller, hub=None)._targets(migration)

    assert targets["source"]["database"] == "mig_test_17"
    assert targets["target"]["database"] == "mig_test_18"


def test_a_blank_card_still_uses_the_container_config(client, tmp_path):
    del client
    migration = _migration(tmp_path)
    sheller = _Probe("mig_test_17", ["mig_test_17"])

    targets = NativeRunner(sheller=sheller, hub=None)._targets(migration)

    assert targets["source"]["database"] == "mig_test_17"


def test_a_container_with_no_db_name_can_still_be_used(client, tmp_path):
    """This used to be a hard refusal with nothing the user could do."""
    del client
    migration = _migration(tmp_path, old_database="mig_test_17", new_database="mig_test_18")
    sheller = _Probe(None, ["mig_test_17", "mig_test_18"])

    targets = NativeRunner(sheller=sheller, hub=None)._targets(migration)

    assert targets["target"]["database"] == "mig_test_18"


def test_nothing_anywhere_says_so_and_lists_what_there_is(client, tmp_path):
    del client
    migration = _migration(tmp_path)
    sheller = _Probe(None, ["mig_test_17", "mig_test_18"])

    with pytest.raises(RuntimeError, match="names no database"):
        NativeRunner(sheller=sheller, hub=None)._targets(migration)


def test_a_name_that_is_not_there_is_refused_with_the_ones_that_are(client, tmp_path):
    del client
    migration = _migration(tmp_path, old_database="typo_17", new_database="mig_test_18")
    sheller = _Probe("mig_test_17", ["mig_test_17", "mig_test_18"])

    with pytest.raises(RuntimeError) as refused:
        NativeRunner(sheller=sheller, hub=None)._targets(migration)

    assert "typo_17" in str(refused.value)
    assert "mig_test_17" in str(refused.value)


def test_a_long_list_is_summarised_not_dumped(client, tmp_path):
    """A shared Postgres holds dozens; the whole list buries the sentence."""
    del client
    migration = _migration(tmp_path, old_database="typo", new_database="db00")
    sheller = _Probe("db00", [f"db{index:02d}" for index in range(40)])

    with pytest.raises(RuntimeError) as refused:
        NativeRunner(sheller=sheller, hub=None)._targets(migration)

    message = str(refused.value)

    assert "It has 40, among them" in message
    assert "db39" not in message


def test_the_settings_form_stores_both_names(client, tmp_path):
    del client
    migration = _migration(tmp_path)

    saved = save_migration_settings(
        migration.id,
        strictness_default="basic",
        retry_limit=5,
        ask_human_mode="sync",
        old_database="  mig_test_17  ",
        new_database="mig_test_18",
    )

    assert saved["old_database"] == "mig_test_17"
    assert saved["new_database"] == "mig_test_18"
