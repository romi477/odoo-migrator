"""What the rewrite did to the module. Taken from File API writes.

The agent writes through the File API. The cartridge callback's
``attempt.diff`` is often empty, so the checkpoint block that exists to
show the change would stay blank unless the app compares before and after
itself.
"""

from __future__ import annotations

import json
from difflib import unified_diff

from odoo_migrator.db import database
from odoo_migrator.models import Job, Module

# Same set as jobs.IN_FLIGHT: these still have an executor on the files.
_IN_FLIGHT = ("queued", "running", "waiting_for_human")


def unified(path: str, before: str, after: str) -> str:
    if before == after:

        return ""

    return "".join(
        unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def note_new_write(migration_id: str, relative: str, before: str, after: str) -> None:
    hunk = unified(relative, before, after)
    if not hunk:

        return
    with database.connection_context():
        jobs = (
            Job.select()
            .join(Module)
            .where((Module.migration == migration_id) & (Job.status.in_(_IN_FLIGHT)))
        )
        for job in jobs:
            name = job.module.technical_name
            # A module does not always sit at the root of the new project:
            # addons/sale_custom/models.py is an ordinary layout, and a
            # prefix test dropped its diff without saying anything.
            folders = relative.split("/")[:-1]
            if relative != name and name not in folders:
                continue
            key = str(job.attempt or 1)
            blob = json.loads(job.file_diffs or "{}")
            blob[key] = (blob.get(key) or "") + hunk
            job.file_diffs = json.dumps(blob)
            job.save()

            return
