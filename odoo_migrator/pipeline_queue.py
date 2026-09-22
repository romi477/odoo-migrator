"""Pipeline queue: ordered steps, run/stop, sequential batches."""

from __future__ import annotations

from odoo_migrator.contract import ContractError
from odoo_migrator.db import database
from odoo_migrator.events import EventHub
from odoo_migrator.graph import modules_in_step_order
from odoo_migrator.jobs import continue_batch, live_run, start_batch
from odoo_migrator.models import Batch, Job, PipelineQueueItem, PipelineQueueState

FINISHED_OK = {"passed"}


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


def add_step(migration_id: str, step: int) -> bool:
    """Append a step if it is not already queued. Returns False when deduped."""
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

            return False
        last = (
            PipelineQueueItem.select()
            .where(PipelineQueueItem.migration == migration_id)
            .order_by(PipelineQueueItem.position.desc())
            .first()
        )
        position = (last.position + 1) if last else 0
        PipelineQueueItem.create(migration=migration_id, step=step, position=position)

    return True


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

                continue
            if all(module["status"] in FINISHED_OK for module in modules):
                item.delete_instance()


def _pending_modules_sorted(migration_id: str) -> list[dict]:
    """Pending modules from queued steps, one row each, parents first."""
    with database.connection_context():
        items = list(
            PipelineQueueItem.select()
            .where(PipelineQueueItem.migration == migration_id)
        )
    seen: set[int] = set()
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

    return {
        "run_state": run_state(migration_id),
        "steps": [{"step": item.step} for item in items],
        "modules": modules,
    }


def publish_queue(hub: EventHub, migration_id: str) -> None:
    hub.publish("queue", {"migration_id": migration_id, **queue_snapshot(migration_id)})


def _batch_blocking(migration_id: str) -> bool:
    with database.connection_context():
        running = (
            Batch.select()
            .where((Batch.migration == migration_id) & (Batch.status == "running"))
            .exists()
        )
        checkpoint = (
            Batch.select()
            .where((Batch.migration == migration_id) & (Batch.status == "checkpoint"))
            .exists()
        )

    return running or checkpoint


def _next_pending_step(migration_id: str) -> int | None:
    pending = _pending_modules_sorted(migration_id)
    if not pending:

        return None
    first = pending[0]

    return first.get("step") or 1


def kick(migration_id: str, executor, hub: EventHub) -> None:
    """Start the next queued step when the pipeline is running."""
    if run_state(migration_id) != "running":

        return
    remove_finished_steps(migration_id)
    if _batch_blocking(migration_id):

        return
    step = _next_pending_step(migration_id)
    if step is None:
        set_run_state(migration_id, "stopped")
        publish_queue(hub, migration_id)

        return
    start_batch(
        migration_id=migration_id,
        step=step,
        executor=executor,
        hub=hub,
    )
    publish_queue(hub, migration_id)


def start(migration_id: str, executor, hub: EventHub) -> None:
    set_run_state(migration_id, "running")
    kick(migration_id, executor, hub)
    publish_queue(hub, migration_id)


def stop(migration_id: str, hub: EventHub) -> None:
    set_run_state(migration_id, "stopped")
    publish_queue(hub, migration_id)


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
