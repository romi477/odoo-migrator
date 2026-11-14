"""A session's write key outlives the thread that opened it, so a restart can
close the shells a dead job left in the containers. A keyless close is 403."""

from types import SimpleNamespace

from odoo_migrator.db import database
from odoo_migrator.models import ShellerSession
from odoo_migrator.runner.sessions import close_side, open_side
from odoo_migrator.sheller.registry import close_left_sessions


def _ctx(fake):

    return SimpleNamespace(
        sheller=fake, sessions={}, write_keys={},
        migration=SimpleNamespace(id="mig-1"),
        session_targets={
            "target": {
                "container": "odoo-new", "database": "acme",
                "odoo_bin": "/opt/odoo/odoo-bin", "owner": {"kind": "agent"},
            }
        },
    )


def _rows():
    with database.connection_context():

        return {row.session_id: row.migration_id for row in ShellerSession.select()}


def test_an_opened_session_is_remembered_and_a_closed_one_forgotten(app, fake_sheller):
    ctx = _ctx(fake_sheller)

    sid = open_side(ctx, "target")

    assert _rows() == {sid: "mig-1"}
    close_side(ctx, "target")
    assert _rows() == {}


def test_a_restart_closes_the_sessions_a_dead_job_left_open(app, fake_sheller):
    ctx = _ctx(fake_sheller)
    sid = open_side(ctx, "target")
    # The job's thread died with the process: nothing closed it.

    close_left_sessions(fake_sheller)

    assert sid in fake_sheller.closes
    assert _rows() == {}


def test_a_session_the_daemon_no_longer_has_is_forgotten(app, fake_sheller):
    ctx = _ctx(fake_sheller)
    sid = open_side(ctx, "target")
    fake_sheller.forget_journal(sid)

    close_left_sessions(fake_sheller)

    assert _rows() == {}


def test_a_daemon_that_does_not_answer_keeps_the_keys_for_the_next_start(app, fake_sheller):
    from odoo_migrator.sheller import ShellerError

    ctx = _ctx(fake_sheller)
    sid = open_side(ctx, "target")

    def down(*_args, **_kwargs):
        raise ShellerError("connection refused")

    fake_sheller.close = down
    close_left_sessions(fake_sheller)

    assert sid in _rows()


def test_the_app_closes_them_when_it_starts(tmp_path, compose, fake_sheller):
    from odoo_migrator.api import create_app
    from odoo_migrator.sheller import registry

    first = create_app(tmp_path / "db.sqlite", compose=compose, sheller=fake_sheller)
    assert first.state.closing_sessions is None
    opened = fake_sheller.open_session(
        container="odoo-new", database="acme", odoo_bin="/opt/odoo/odoo-bin",
        owner={"kind": "agent"}, allow_commit=True,
    )
    registry.remember(opened["id"], opened["write_key"], "mig-1")

    again = create_app(tmp_path / "db.sqlite", compose=compose, sheller=fake_sheller)
    again.state.closing_sessions.join(timeout=5)

    assert opened["id"] in fake_sheller.closes
