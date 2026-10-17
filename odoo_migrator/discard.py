"""Forget one module's run, as if it had never started.

Dismiss marks a failed run as read and keeps all of it. This deletes the
run: every job of the module with the logs, findings and thread that hang
off them, the module's directory in the target project back to its last
commit, and the module queued again. A module that passed is refused: the
app committed that port, and taking a commit back is not this button's job
(Reopen is).

The target container's database is not touched. A run that got as far as
committing an upgrade there leaves it.
"""

from __future__ import annotations

from pathlib import Path

from odoo_migrator.contract import ContractError
from odoo_migrator.db import database
from odoo_migrator.events import EventHub
from odoo_migrator.jobs import IN_FLIGHT, _publish_module
from odoo_migrator.models import (
    Assumption,
    Attempt,
    Batch,
    CheckpointMessage,
    Finding,
    Job,
    Migration,
    Module,
    RunLog,
    StepRun,
    ToolCall,
)
from odoo_migrator.module_paths import module_root
from odoo_migrator.port_guard import changed_files, discard_hand_edits

# The runs that did not pass: nothing of theirs is committed.
DISCARDABLE = ("stopped", "failed", "unknown")


def discard_preview(module: Module) -> int | None:
    """How many files in the module's directory a discard would put back, for
    the question asked first. None when it cannot say (not a git checkout)."""
    with database.connection_context():
        migration = Migration.get_by_id(module.migration_id)
        directory = module_root(migration, module, "target")
    if not _exists(directory):

        return 0
    files = changed_files(directory)

    return None if files is None else len(files)


def discard_run(module_id: int, migration_id: str, hub: EventHub | None = None) -> Module:
    """Delete every run of this module and queue it again. Returns the module."""
    with database.connection_context():
        module = Module.get_or_none(Module.id == module_id)
        if module is None or str(module.migration_id) != migration_id:
            raise ContractError("unknown module")
        if module.status not in DISCARDABLE:
            raise ContractError(
                f"{module.technical_name} is {module.status}; only a stopped, failed or "
                "unknown run can be discarded. A passed module is Reopen's."
            )
        busy = (
            Job.select()
            .where((Job.module == module) & (Job.status.in_(IN_FLIGHT)))
            .exists()
        )
        if busy:
            raise ContractError(f"{module.technical_name} is running. Stop it first.")
        migration = Migration.get_by_id(migration_id)
        directory = module_root(migration, module, "target")
    # The files first: it is the step that can refuse, and a refusal must
    # leave the run exactly as it was.
    if _exists(directory):
        files = changed_files(directory)
        if files is None:
            raise ContractError(
                f"{module.technical_name} is not in a git repository, so its files cannot "
                "be put back. Nothing was discarded."
            )
        if files:
            discard_hand_edits(migration_id, module_id)
    with database.atomic():
        _delete_runs(module)
        module.status = "queued"
        module.has_assumptions = False
        module.needs_review = False
        module.port_fingerprint = ""
        module.kept_commit = ""
        module.save()
    _publish_module(hub, module)

    return module


def _exists(directory) -> bool:
    """A module that never got a directory has no files to put back."""

    return directory is not None and Path(directory).is_dir()


def _delete_runs(module: Module) -> None:
    jobs = list(Job.select().where(Job.module == module))
    batches = {job.batch_id for job in jobs}
    for job in jobs:
        steps = list(StepRun.select().where(StepRun.job == job))
        # A log line points at its tool call, so the lines go first.
        RunLog.delete().where(RunLog.job == job).execute()
        if steps:
            ToolCall.delete().where(ToolCall.step_run.in_(steps)).execute()
        for table in (StepRun, Attempt, Finding, Assumption, CheckpointMessage):
            table.delete().where(table.job == job).execute()
        job.delete_instance()
    for batch_id in batches:
        if not Job.select().where(Job.batch == batch_id).exists():
            Batch.delete().where(Batch.id == batch_id).execute()
