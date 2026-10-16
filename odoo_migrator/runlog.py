"""The run log: what a job did, in order, with the reason attached.

A step's outcome was a boolean and a badge. Why it ended that way was
computed, returned, and dropped — so a run that stopped after two attempts
left nothing to read but two red rows. This is the missing half.

Writing a line must never be able to fail the job it describes, so every
call here swallows its own errors. A log that takes the run down with it is
worse than no log.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime

from odoo_migrator.db import database
from odoo_migrator.models import Job, RunLog

# One line, read at a glance in a scrolling panel. The body goes to
# `detail` and is fetched when someone opens the line.
MAX_TEXT = 300
# A prompt or a traceback, not a transcript. Past this it stops being a
# log line and starts being a file nobody reads.
MAX_DETAIL = 20_000


def write(
    job_id: str,
    *,
    source: str,
    text: str,
    detail: str = "",
    level: str = "info",
    hub=None,
    tool_call_id: int | None = None,
) -> None:
    """Record one line, and put it on the stream the page is watching."""
    clipped = _clip(text, MAX_TEXT)
    body = _clip(detail, MAX_DETAIL)
    row_id = 0
    module = ""
    migration_id = ""
    at = datetime.now(UTC)
    try:
        with database.connection_context():
            job = Job.get_or_none(Job.job_id == job_id)
            if job is None:

                return
            # Whose run this line belongs to. The page used to read the name
            # off the live panel above the log, and the first line of a job
            # arrives before the progress that renames that panel: for the
            # seconds in between, a new job wore the previous module's name.
            module = job.module.technical_name
            # Which migration, so a page shows only its own: two run side by
            # side, and a panel that followed every job hopped between them.
            migration_id = str(job.module.migration_id)
            row = RunLog.create(
                job=job,
                at=at,
                level=level,
                source=source,
                text=clipped,
                detail=body,
                tool_call=tool_call_id,
            )
            row_id = row.id
    except Exception:  # noqa: BLE001 - the log never fails the run.

        return
    if hub is None:

        return
    with contextlib.suppress(Exception):
        hub.publish(
            "log",
            {
                "id": row_id,
                "job_id": job_id,
                "migration_id": migration_id,
                "module": module,
                "at": at.isoformat(),
                "level": level,
                "source": source,
                "text": clipped,
                "has_detail": bool(body) or tool_call_id is not None,
            },
        )


def lines(job_id: str, *, after: int = 0, limit: int = 1000) -> list[dict]:
    """The log so far. `after` is the last id the page already has."""
    with database.connection_context():
        job = Job.get_or_none(Job.job_id == job_id)
        if job is None:

            return []
        rows = list(
            RunLog.select()
            .where((RunLog.job == job) & (RunLog.id > after))
            .order_by(RunLog.id)
            .limit(limit)
        )

        return [
            {
                "id": row.id,
                # ISO-8601, not `str()`: the space-separated form is not a
                # format `Date` is required to parse, and the page renders
                # these in the reader's own zone.
                "at": row.at.isoformat(),
                "level": row.level,
                "source": row.source,
                "text": row.text,
                # A tool call's body is not stored here: the line points at
                # the skeleton row that can fetch it from the journal.
                "has_detail": bool(row.detail) or row.tool_call_id is not None,
            }
            for row in rows
        ]


def clear(job_id: str) -> int:
    """Delete one job's log. Returns how many lines went."""
    with database.connection_context():
        job = Job.get_or_none(Job.job_id == job_id)
        if job is None:

            return 0

        return RunLog.delete().where(RunLog.job == job).execute()


def detail_of(line_id: int) -> str | None:
    with database.connection_context():
        row = RunLog.get_or_none(RunLog.id == line_id)

        return None if row is None else row.detail


def tool_call_of(line_id: int) -> tuple[str, str] | None:
    """The session and command a line's tool call ran as, or None.

    What turns a 120-character preview into the command's whole answer. The
    journal is the daemon's, and it is the only copy: a body stored here
    would be a shorter duplicate of something it already keeps.
    """
    with database.connection_context():
        row = RunLog.get_or_none(RunLog.id == line_id)
        if row is None or row.tool_call_id is None:

            return None
        call = row.tool_call

        return (call.session_id or "", call.command_id or "")


def _clip(text: str, limit: int) -> str:
    value = (text or "").strip()
    if len(value) <= limit:

        return value

    return f"{value[:limit]}… (+{len(value) - limit} characters)"
