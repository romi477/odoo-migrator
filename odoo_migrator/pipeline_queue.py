"""Pipeline queue: ordered steps, run/stop, sequential batches."""

from __future__ import annotations

from odoo_migrator.contract import ContractError
from odoo_migrator.db import database
from odoo_migrator.events import EventHub
from odoo_migrator.graph import DONE, list_modules, modules_in_step_order
from odoo_migrator.jobs import continue_batch, live_run, start_batch
from odoo_migrator.models import (
    Batch,
    Job,
    PipelineQueueItem,
    PipelineQueueSkip,
    PipelineQueueState,
)

FINISHED_OK = set(DONE)


def _state_row(migration_id: str) -> PipelineQueueState:
    with database.connection_context():
        row, _ = PipelineQueueState.get_or_create(migration=migration_id)

    return row


def run_state(migration_id: str) -> str:
    with database.connection_context():
        row = PipelineQueueState.get_or_none(PipelineQueueState.migration == migration_id)
        if row is None:

            return "stopped"

        return row.run_state


def set_run_state(migration_id: str, state: str) -> None:
    with database.connection_context():
        row, _ = PipelineQueueState.get_or_create(migration=migration_id)
        row.run_state = state
        row.save()


def _skipped(migration_id: str) -> set[int]:
    """Modules taken off the queue while the step they are on stays."""
    with database.connection_context():
        return {
            row.module_id
            for row in PipelineQueueSkip.select().where(
                PipelineQueueSkip.migration == migration_id
            )
        }


def _partial_steps(migration_id: str) -> set[int]:
    """Steps that have a module taken off them."""
    skipped = _skipped(migration_id)

    return {
        module.get("step") or 1
        for module in list_modules(migration_id)
        if module["id"] in skipped
    }


def _forget_skips(migration_id: str, step: int) -> int:
    """Put the step's removed modules back; returns how many there were."""
    ids = [module["id"] for module in modules_in_step_order(migration_id, step)]
    if not ids:

        return 0
    with database.connection_context():

        return (
            PipelineQueueSkip.delete()
            .where(
                (PipelineQueueSkip.migration == migration_id)
                & (PipelineQueueSkip.module.in_(ids))
            )
            .execute()
        )


def step_blocked_by(migration_id: str, step: int, queued: set[int]) -> int | None:
    """The earliest earlier step the modules of `step` hang on that is not queued.

    Only a step whose modules depend, through modules not yet migrated, on
    that step counts: an unrelated module on an earlier step holds nothing.
    """
    from odoo_migrator.graph import list_pipeline_steps

    for row in list_pipeline_steps(migration_id):
        if row["step"] == step:
            for needed in row["needs"]:
                if needed not in queued:

                    return needed

    return None


def add_step(migration_id: str, step: int) -> bool:
    """Append a step if it is not already queued. Returns False when deduped.

    A step is not queued ahead of an earlier step its modules depend on, while
    that one has modules to migrate and is not queued itself: raises ValueError.
    """
    with database.connection_context():
        exists = (
            PipelineQueueItem.select()
            .where(
                (PipelineQueueItem.migration == migration_id)
                & (PipelineQueueItem.step == step)
            )
            .exists()
        )
        if exists:
            # Queued again: whatever was taken off it comes back.

            return _forget_skips(migration_id, step) > 0
        # A step with a module taken off it is not queued as far as the steps
        # after it go: they may hang on that very module.
        partial = _partial_steps(migration_id)
        queued = {
            item.step
            for item in PipelineQueueItem.select().where(
                PipelineQueueItem.migration == migration_id
            )
            if item.step not in partial
        }
        blocker = step_blocked_by(migration_id, step, queued)
        if blocker is not None:
            raise ValueError(
                f"Step {step} cannot be queued before step {blocker}: "
                "add it first, or migrate it."
            )
        last = (
            PipelineQueueItem.select()
            .where(PipelineQueueItem.migration == migration_id)
            .order_by(PipelineQueueItem.position.desc())
            .first()
        )
        position = (last.position + 1) if last else 0
        PipelineQueueItem.create(migration=migration_id, step=step, position=position)

    return True


def add_all_steps(migration_id: str) -> int:
    """Append every wave that still has in-plan, not-yet-passed modules.

    Returns how many queue entries were added.
    """
    from odoo_migrator.graph import list_pipeline_steps

    added = 0
    for row in list_pipeline_steps(migration_id, pending_only=True):
        if add_step(migration_id, row["step"]):
            added += 1

    return added


def remove_finished_steps(migration_id: str) -> None:
    """Drop queue entries whose modules are all passed."""
    with database.connection_context():
        items = list(
            PipelineQueueItem.select()
            .where(PipelineQueueItem.migration == migration_id)
            .order_by(PipelineQueueItem.position)
        )
        for item in items:
            modules = modules_in_step_order(migration_id, item.step)
            if not modules:
                item.delete_instance()
                _forget_skips(migration_id, item.step)

                continue
            if all(module["status"] in FINISHED_OK for module in modules):
                item.delete_instance()
                _forget_skips(migration_id, item.step)


def _pending_modules_sorted(migration_id: str) -> list[dict]:
    """Pending modules from queued steps, one row each, parents first."""
    with database.connection_context():
        items = list(
            PipelineQueueItem.select()
            .where(PipelineQueueItem.migration == migration_id)
        )
    seen: set[int] = set(_skipped(migration_id))
    pending: list[dict] = []
    for item in items:
        for module in modules_in_step_order(migration_id, item.step):
            if module["status"] in FINISHED_OK:

                continue
            if module["id"] in seen:

                continue
            seen.add(module["id"])
            pending.append(module)
    pending.sort(
        key=lambda row: (row.get("migration_rank") or 0, row["technical_name"])
    )

    return pending


def queue_snapshot(migration_id: str) -> dict:
    """Pending modules in migration order for the Pipeline panel."""
    live = live_run(migration_id)
    active_id = live["module_id"] if live else None
    with database.connection_context():
        items = list(
            PipelineQueueItem.select()
            .where(PipelineQueueItem.migration == migration_id)
            .order_by(PipelineQueueItem.position)
        )
    modules: list[dict] = []
    for rank, module in enumerate(_pending_modules_sorted(migration_id), start=1):
        modules.append(
            {
                "id": module["id"],
                "technical_name": module["technical_name"],
                "step": module.get("step") or 1,
                "migration_rank": module.get("migration_rank"),
                "status": module["status"],
                "active": module["id"] == active_id,
                "position": rank,
            }
        )

    # Only the end of the queue can be taken back: a card in the middle may be
    # what a later one waits for. A module in flight stays.
    for module in modules:
        module["removable"] = False
    if modules:
        modules[-1]["removable"] = not modules[-1]["active"]
    partial = _partial_steps(migration_id)

    state = run_state(migration_id)

    return {
        "run_state": state,
        "idle": state != "running" and live is None,
        "steps": [
            {"step": item.step, "whole": item.step not in partial} for item in items
        ],
        "modules": modules,
    }


def remove_last_module(migration_id: str, module_id: int) -> None:
    """Take the last card out of the queue. Raises ValueError when it cannot go.

    The step stays queued with the cards it has left, and leaves with the last
    of them. `module_id` is the card the person saw last; if the queue moved on
    since, it is refused rather than removing a different one.
    """
    modules = queue_snapshot(migration_id)["modules"]
    chosen = next((module for module in modules if module["id"] == module_id), None)
    if chosen is None:
        raise ValueError("That module is not in the queue.")
    name = chosen["technical_name"]
    if modules[-1]["id"] != module_id:
        raise ValueError(f"{name} is not the last in the queue: remove from the end.")
    if not chosen["removable"]:
        raise ValueError(f"{name} is running: stop it first.")
    step = chosen["step"]
    with database.connection_context():
        PipelineQueueSkip.get_or_create(migration=migration_id, module=module_id)
    if not any(module["step"] == step for module in modules[:-1]):
        with database.connection_context():
            PipelineQueueItem.delete().where(
                (PipelineQueueItem.migration == migration_id)
                & (PipelineQueueItem.step == step)
            ).execute()
        _forget_skips(migration_id, step)


def publish_queue(hub: EventHub, migration_id: str) -> None:
    hub.publish("queue", {"migration_id": migration_id, **queue_snapshot(migration_id)})


def _batch_block_reason(migration_id: str) -> str | None:
    """Why the next step cannot start, or None when the pipe is free."""
    with database.connection_context():
        if (
            Batch.select()
            .where((Batch.migration == migration_id) & (Batch.status == "running"))
            .exists()
        ):

            return "A batch is already running on this migration."
        if (
            Batch.select()
            .where((Batch.migration == migration_id) & (Batch.status == "checkpoint"))
            .exists()
        ):

            return "Review the last batch before starting another."

    return None


def _batch_blocking(migration_id: str) -> bool:

    return _batch_block_reason(migration_id) is not None


def _next_pending_step(migration_id: str) -> int | None:
    pending = _pending_modules_sorted(migration_id)
    if not pending:

        return None
    first = pending[0]

    return first.get("step") or 1


def kick(migration_id: str, executor, hub: EventHub) -> None:
    """Start the next queued step when the pipeline is running.

    A block or an empty queue raises ``ContractError``. The auto-continue
    path catches that and stops; Run shows the same sentence on the page.
    """
    if run_state(migration_id) != "running":

        return
    remove_finished_steps(migration_id)
    reason = _batch_block_reason(migration_id)
    if reason == "Review the last batch before starting another.":
        set_run_state(migration_id, "stopped")
        publish_queue(hub, migration_id)
    if reason is not None:
        raise ContractError(reason)
    step = _next_pending_step(migration_id)
    if step is None:
        set_run_state(migration_id, "stopped")
        publish_queue(hub, migration_id)
        raise ContractError("Nothing is queued.")
    start_batch(
        migration_id=migration_id,
        step=step,
        executor=executor,
        hub=hub,
    )
    publish_queue(hub, migration_id)


def start(migration_id: str, executor, hub: EventHub) -> None:
    set_run_state(migration_id, "running")
    try:
        kick(migration_id, executor, hub)
    except ContractError:
        if run_state(migration_id) == "running" and not _batch_blocking(migration_id):
            set_run_state(migration_id, "stopped")
            publish_queue(hub, migration_id)
        raise
    publish_queue(hub, migration_id)


def stop(migration_id: str, hub: EventHub) -> None:
    set_run_state(migration_id, "stopped")
    publish_queue(hub, migration_id)


def clear(migration_id: str, hub: EventHub) -> bool:
    """Drop every queued step when nothing is in flight.

    A running pipe, or a module still finishing after Stop, keeps the list.
    """
    if run_state(migration_id) == "running" or live_run(migration_id) is not None:

        return False
    with database.connection_context():
        PipelineQueueItem.delete().where(
            PipelineQueueItem.migration == migration_id
        ).execute()
        PipelineQueueSkip.delete().where(
            PipelineQueueSkip.migration == migration_id
        ).execute()
    publish_queue(hub, migration_id)

    return True


def should_advance_modules(migration_id: str) -> bool:
    """False when the queue was stopped mid-run — finish the current job only."""
    with database.connection_context():
        queued = (
            PipelineQueueItem.select()
            .where(PipelineQueueItem.migration == migration_id)
            .exists()
        )
        if not queued:

            return True
        row = PipelineQueueState.get_or_none(PipelineQueueState.migration == migration_id)
        if row is None:

            return True

        return row.run_state == "running"


def after_batch_checkpoint(
    migration_id: str,
    batch_id: int,
    executor,
    hub: EventHub,
) -> None:
    """Auto-continue through the queue when a step batch finishes cleanly."""
    if run_state(migration_id) != "running":

        return
    with database.connection_context():
        jobs = list(Job.select().where(Job.batch == batch_id))
    if any(job.status in ("failed", "waiting_for_human") for job in jobs):
        set_run_state(migration_id, "stopped")
        publish_queue(hub, migration_id)

        return
    remove_finished_steps(migration_id)
    try:
        continue_batch(migration_id, override=True)
    except ContractError:

        return
    try:
        kick(migration_id, executor, hub)
    except ContractError:

        return


def on_module_finished(migration_id: str, hub: EventHub) -> None:
    remove_finished_steps(migration_id)
    publish_queue(hub, migration_id)
