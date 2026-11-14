"""Remember the write key a session was opened with, so a crashed run can close it.

Measured against the daemon, a close with an empty key is 403 not_owner even
for an agent session, and a session whose process dies drops out of the
registry on its own. A job's keys lived only in its thread: a restart left
both `odoo-bin shell` processes running in the containers until a person
closed them in Sheller.
"""

from odoo_migrator.db import database
from odoo_migrator.models import ShellerSession
from odoo_migrator.sheller.errors import ShellerError


def remember(session_id: str, write_key: str, migration_id: str = "") -> None:
    if not session_id or not write_key:

        return
    with database.connection_context():
        ShellerSession.insert(
            session_id=session_id,
            write_key=write_key,
            migration_id=migration_id or "",
        ).on_conflict(
            conflict_target=[ShellerSession.session_id],
            update={
                ShellerSession.write_key: write_key,
                ShellerSession.migration_id: migration_id or "",
            },
        ).execute()


def forget(session_id: str) -> None:
    if not session_id:

        return
    with database.connection_context():
        ShellerSession.delete().where(
            ShellerSession.session_id == session_id
        ).execute()


def keys_for(migration_ids) -> dict[str, str]:
    wanted = {str(one) for one in migration_ids}
    if not wanted:

        return {}
    with database.connection_context():

        return {
            row.session_id: row.write_key
            for row in ShellerSession.select().where(
                ShellerSession.migration_id.in_(list(wanted))
            )
        }


def close_left_sessions(sheller) -> list[str]:
    """Close every session this process opened and no job closed.

    Called when the app starts, after the jobs that were running are marked
    unknown: none of them holds a session any more. A job parked on a person
    closed its sessions when it parked. A session the daemon no longer has is
    forgotten; one it could not be asked about is kept for the next start.
    Returns the ids closed.
    """
    with database.connection_context():
        rows = [(row.session_id, row.write_key) for row in ShellerSession.select()]
    closed = []
    for session_id, write_key in rows:
        try:
            sheller.close(session_id, write_key=write_key)
        except ShellerError as exc:
            if getattr(exc, "status", None) not in (404, 410):
                continue
        else:
            closed.append(session_id)
        forget(session_id)

    return closed
