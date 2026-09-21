"""Remember the write key n8n was handed, so a crashed run can be cleaned up.

The proxy is the only route n8n has to the daemon, so the key passes through
this process at open time whether we record it or not. Recording it is what
makes the session janitor possible at all: measured against the daemon, a
close with an empty key is 403 not_owner even for an agent session, and a
session whose process dies is announced and drops out of the registry, so
there is no dead one left in a listing to sweep instead.
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
