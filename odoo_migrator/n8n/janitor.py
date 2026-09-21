"""Close sessions left behind by a run the app has given up on.

The write key is required: measured against the daemon, a close with an
empty key is 403 not_owner even for an agent session, and a session whose
process died is announced and drops out of the registry, so waiting for a
corpse to appear in a listing sweeps nothing. The key the proxy saw at open
time is what makes this work; without it the app could only ask the human
to close the session in Sheller's own UI.
"""

from odoo_migrator.db import database
from odoo_migrator.models import Job
from odoo_migrator.sheller.errors import ShellerError, ShellerUnreachable
from odoo_migrator.sheller.registry import forget, keys_for


def sweep_orphaned_sessions(sheller) -> list[str]:
    if sheller is None:

        return []
    with database.connection_context():
        given_up = {
            str(job.module.migration_id)
            for job in Job.select().where(Job.status == "unknown")
        }
    known = keys_for(given_up)
    if not known:

        return []
    try:
        sessions = sheller.list_sessions()
    except ShellerUnreachable:

        return []
    closed = []
    for session in sessions:
        session_id = session.get("id")
        write_key = known.get(session_id)
        if not write_key:
            continue
        owner = session.get("owner") or {}
        # Ours by key, but check anyway: a daemon we merely attached to is
        # shared, and closing a human's session is not ours to do.
        if owner.get("kind") != "agent":
            continue
        try:
            sheller.close(session_id, write_key=write_key)
        except ShellerError:
            continue
        forget(session_id)
        closed.append(session_id)
    # A session the daemon no longer lists is gone; the row is just litter.
    for session_id in known:
        if session_id not in {one.get("id") for one in sessions}:
            forget(session_id)

    return closed
