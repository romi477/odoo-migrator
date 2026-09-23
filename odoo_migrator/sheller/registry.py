"""Remember the write key a session was opened with, so a crashed run can close it.

Measured against the daemon, a close with an empty key is 403 not_owner even
for an agent session, and a session whose process dies drops out of the
registry on its own.
"""

from odoo_migrator.db import database
from odoo_migrator.models import ShellerSession


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
