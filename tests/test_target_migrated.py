"""A module already installed on the target is marked `migrated` on Resolve.

A project often arrives half-ported: some modules are already in the new
Odoo. Resolve reads the target's installed modules and marks the ones the
app has not touched, so the queue does not run them again unless a person
reopens them.
"""

from odoo_migrator.db import database, init_db
from odoo_migrator.graph import (
    earlier_steps_blocking,
    list_pipeline_steps,
    mark_migrated_from_target,
    resolve_graph,
)
from odoo_migrator.jobs import reopen_module
from odoo_migrator.migrations import create_migration, migration_results, save_side_container
from odoo_migrator.models import Batch, Job, Migration, Module
from odoo_migrator.sheller import FakeShellerClient
from odoo_migrator.sheller.errors import ShellerUnreachable


def _migration(tmp_path, *, new_container="odoo-new"):
    init_db(tmp_path / "target.sqlite")
    created = create_migration(
        name="Acme",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path="/tmp/old",
        new_project_path="/tmp/new",
    )
    save_side_container(created["id"], "source", "odoo-old")
    if new_container:
        save_side_container(created["id"], "target", new_container)

    return created


def _statuses(migration_id):
    with database.connection_context():

        return {
            row.technical_name: row.status
            for row in Module.select().where(Module.migration == migration_id)
        }


def test_a_module_installed_on_the_target_is_marked_migrated(tmp_path):
    created = _migration(tmp_path)
    fake = FakeShellerClient()
    fake.installed_by_container["odoo-new"] = ["base", "sale", "sale_custom"]
    resolve_graph(fake, created["id"])

    outcome = mark_migrated_from_target(fake, created["id"])

    assert outcome == {"marked": 1, "error": None, "off_path": 0}
    statuses = _statuses(created["id"])
    assert statuses["sale_custom"] == "migrated"
    assert statuses["stock_custom"] == "queued"


def test_the_target_is_read_only_and_its_session_closed(tmp_path):
    created = _migration(tmp_path)
    fake = FakeShellerClient()
    resolve_graph(fake, created["id"])

    mark_migrated_from_target(fake, created["id"])

    target_open = fake.opens[-1]
    assert target_open["container"] == "odoo-new"
    assert target_open["allow_commit"] is False
    assert target_open["owner"]["migration_id"] == created["id"]
    assert fake.closes[-1] == target_open["id"]
    assert fake.rollbacks[-1] == target_open["id"]


def test_the_database_on_the_card_wins_over_the_probe(tmp_path):
    created = _migration(tmp_path)
    with database.connection_context():
        Migration.update(new_database="other_major_db").where(
            Migration.id == created["id"]
        ).execute()
    fake = FakeShellerClient()
    resolve_graph(fake, created["id"])

    mark_migrated_from_target(fake, created["id"])

    assert fake.opens[-1]["database"] == "other_major_db"


def test_a_module_with_work_of_ours_keeps_its_status(tmp_path):
    created = _migration(tmp_path)
    fake = FakeShellerClient()
    fake.installed_by_container["odoo-new"] = ["sale_custom", "stock_custom"]
    resolve_graph(fake, created["id"])
    with database.connection_context():
        failed = Module.get(Module.technical_name == "sale_custom")
        failed.status = "failed"
        failed.save()
        # Queued again, but it has run before: its jobs are ours.
        rerun = Module.get(Module.technical_name == "stock_custom")
        batch = Batch.create(migration=Migration.get_by_id(created["id"]), status="idle")
        Job.create(batch=batch, module=rerun, status="passed", job_id="job-ran")

    outcome = mark_migrated_from_target(fake, created["id"])

    assert outcome["marked"] == 0
    statuses = _statuses(created["id"])
    assert statuses["sale_custom"] == "failed"
    assert statuses["stock_custom"] == "queued"


def test_an_excluded_module_is_not_marked(tmp_path):
    created = _migration(tmp_path)
    fake = FakeShellerClient()
    fake.installed_by_container["odoo-new"] = ["sale_custom"]
    resolve_graph(fake, created["id"])
    with database.connection_context():
        Module.update(struck_out=True).where(
            Module.technical_name == "sale_custom"
        ).execute()

    assert mark_migrated_from_target(fake, created["id"])["marked"] == 0
    assert _statuses(created["id"])["sale_custom"] == "queued"


def test_no_target_container_marks_nothing_and_says_so(tmp_path):
    created = _migration(tmp_path, new_container="")
    fake = FakeShellerClient()
    resolve_graph(fake, created["id"])
    opened = len(fake.opens)

    outcome = mark_migrated_from_target(fake, created["id"])

    assert outcome["marked"] == 0
    assert "target container" in outcome["error"]
    assert len(fake.opens) == opened


def test_a_target_that_does_not_answer_leaves_the_graph_alone(tmp_path):
    created = _migration(tmp_path)
    fake = FakeShellerClient()
    resolve_graph(fake, created["id"])
    fake.exec_error = ShellerUnreachable("daemon went away")

    outcome = mark_migrated_from_target(fake, created["id"])

    assert outcome["marked"] == 0
    assert "daemon went away" in outcome["error"]
    assert set(_statuses(created["id"]).values()) == {"queued"}


def test_a_migrated_module_does_not_hold_back_the_next_step(tmp_path):
    created = _migration(tmp_path)
    fake = FakeShellerClient()
    # Every step-1 module of the default graph.
    fake.installed_by_container["odoo-new"] = ["sale_custom", "web_enterprise_helper"]
    resolve_graph(fake, created["id"])
    mark_migrated_from_target(fake, created["id"])

    assert earlier_steps_blocking(created["id"], 2) is None
    steps = list_pipeline_steps(created["id"], pending_only=True)
    assert all("sale_custom" not in step["modules"] for step in steps)


def test_reopen_puts_a_migrated_module_back_in_the_queue(tmp_path):
    created = _migration(tmp_path)
    fake = FakeShellerClient()
    fake.installed_by_container["odoo-new"] = ["sale_custom"]
    resolve_graph(fake, created["id"])
    mark_migrated_from_target(fake, created["id"])
    with database.connection_context():
        module_id = Module.get(Module.technical_name == "sale_custom").id

    reopen_module(module_id, created["id"])

    assert _statuses(created["id"])["sale_custom"] == "queued"


def test_the_result_board_counts_migrated_apart_from_passed(tmp_path):
    created = _migration(tmp_path)
    fake = FakeShellerClient()
    fake.installed_by_container["odoo-new"] = ["sale_custom"]
    resolve_graph(fake, created["id"])
    mark_migrated_from_target(fake, created["id"])

    progress = migration_results(created["id"])["progress"]

    assert progress["migrated"] == 1
    assert progress["passed"] == 0
    assert progress["pct_done"] != "0%"


def test_resolve_on_the_tree_reports_what_it_marked(client):
    client.post(
        "/migrations",
        data={
            "name": "Acme",
            "accent": "violet",
            "source_version": "17.0",
            "target_version": "18.0",
            "old_project_path": "/tmp/Acme/old",
            "new_project_path": "/tmp/Acme/new",
        },
        follow_redirects=True,
    )
    with database.connection_context():
        row = Migration.get(Migration.name == "Acme")
    client.post(f"/migrations/{row.id}/open", follow_redirects=True)
    save_side_container(row.id, "source", "odoo-old")
    save_side_container(row.id, "target", "odoo-new")
    client.app.state.sheller.installed_by_container["odoo-new"] = ["sale_custom"]

    page = client.post("/tree/resolve", follow_redirects=True)

    assert page.status_code == 200
    assert "1 module already installed on the target" in page.text
