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
) -> None:
    """Record one line, and put it on the stream the page is watching."""
    clipped = _clip(text, MAX_TEXT)
    body = _clip(detail, MAX_DETAIL)
    row_id = 0
    at = datetime.now(UTC)
    try:
        with database.connection_context():
            job = Job.get_or_none(Job.job_id == job_id)
            if job is None:

                return
            row = RunLog.create(
                job=job, at=at, level=level, source=source, text=clipped, detail=body
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
                "at": at.isoformat(),
                "level": level,
                "source": source,
                "text": clipped,
                "has_detail": bool(body),
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
                "at": str(row.at),
                "level": row.level,
                "source": row.source,
                "text": row.text,
                "has_detail": bool(row.detail),
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


def _clip(text: str, limit: int) -> str:
    value = (text or "").strip()
    if len(value) <= limit:

        return value

    return f"{value[:limit]}… (+{len(value) - limit} characters)"
