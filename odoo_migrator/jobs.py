"""Start a job, apply callbacks, reconcile after a crash."""

from __future__ import annotations

import json
import threading
import uuid

from odoo_migrator.contract import (
    ContractError,
    ProgressPayload,
    ResultPayload,
    build_trigger,
    stack_hash,
)
from odoo_migrator.db import database
from odoo_migrator.events import EventHub
from odoo_migrator.graph import (
    earlier_steps_blocking,
    modules_in_batch_order,
    modules_in_step_order,
    steps_for_migration,
)
from odoo_migrator.models import (
    Assumption,
    Attempt,
    Batch,
    Finding,
    Job,
    Migration,
    Module,
)
from odoo_migrator.rules import RulesError, append_finding
from odoo_migrator.settings import get_app_settings

# What a restart reconciles. A job parked on waiting_for_human is not a
# casualty of the restart — the human still owes it an answer — so it is
# deliberately absent here.
NON_TERMINAL = ("queued", "running")
# No further callback is expected after these. A progress that arrives
# anyway is a retry or an out-of-order delivery, not news.
FINISHED = ("passed", "failed")
# A module with a job in one of these already has an executor working on
# its files.
IN_FLIGHT = ("queued", "running", "waiting_for_human")
# The lamp is global: a stall on another migration must still be visible.
ATTENTION = ("failed", "waiting_for_human")


class UnknownJob(ContractError):
    """Callback for a job_id the app does not have."""


class AlreadyRunning(ContractError):
    """That module already has an executor working on it."""


def running_migration_count() -> int:
    with database.connection_context():

        return (
            Module.select(Module.migration)
            .where(Module.status == "running")
            .distinct()
            .count()
        )


def lamp_state() -> str:
    with database.connection_context():
        waiting = Module.select().where(Module.status.in_(ATTENTION)).exists()

    return "needs-attention" if waiting else "idle"


def attention_target() -> dict | None:
    """The stalled module the lamp should land on, whichever migration it is."""
    with database.connection_context():
        module = (
            Module.select()
            .where(Module.status.in_(ATTENTION))
            .order_by(Module.id)
            .first()
        )
        if module is None:

            return None

        return {
            "migration_id": str(module.migration_id),
            "module_id": module.id,
            "technical_name": module.technical_name,
        }


def _emit(hub: EventHub, job: Job, module: Module) -> None:
    hub.publish(
        "module",
        {
            "id": module.id,
            "status": module.status,
            "stage": job.current_stage,
            "attempt": job.attempt,
            "migration_id": str(module.migration_id),
            "question": job.question,
            "has_assumptions": bool(module.has_assumptions),
            "technical_name": module.technical_name,
            "current_session_id": job.current_session_id or "",
        },
    )
    hub.publish("running", {"count": running_migration_count()})
    hub.publish("lamp", {"state": lamp_state()})


def reconcile_jobs() -> None:
    """A callback that never arrived is ``unknown``, not a job still running.

    Modules that are merely queued — waiting to be migrated, no job yet —
    stay queued. Only a module that belonged to a non-terminal job is
    marked unknown, so a restart does not paint the whole tree as a crash.
    """
    with database.connection_context():
        module_ids = [
            job.module_id for job in Job.select().where(Job.status.in_(NON_TERMINAL))
        ]
        Job.update(status="unknown").where(Job.status.in_(NON_TERMINAL)).execute()
        if module_ids:
            Module.update(status="unknown").where(Module.id.in_(module_ids)).execute()
        # A batch whose runner died is not still running, and leaving it so
        # blocks every later batch on that migration behind "already running".
        # One parked on waiting_for_human is not dead — the human still owes
        # it an answer — so it is not marked unknown here.
        parked = [
            job.batch_id
            for job in Job.select().where(Job.status == "waiting_for_human")
        ]
        running = Batch.status == "running"
        if parked:
            running = running & Batch.id.not_in(parked)
        Batch.update(status="unknown").where(running).execute()


def start_job(
    *,
    migration_id: str,
    module_id: int,
    executor,
    hub: EventHub,
    batch_id: int | None = None,
    smoke: bool = False,
) -> Job:
    settings = get_app_settings()
    with database.connection_context():
        module_row = Module.get_or_none(
            (Module.id == module_id) & (Module.migration == migration_id)
        )
        if module_row is None:
            raise ContractError("unknown module")
        if module_row.struck_out:
            raise ContractError(
                f"{module_row.technical_name} is struck out. Bring it back first."
            )
        name = module_row.technical_name
    step = steps_for_migration(migration_id).get(name)
    # A batch already passed the wave check to start. Re-checking here
    # stopped a mixed Group: stock (step 2) waited for every step-1
    # module on the tree, including ones the operator left out of the batch.
    blocked = None
    if not smoke and batch_id is None:
        blocked = earlier_steps_blocking(migration_id, step)
    if blocked:
        raise ContractError(blocked)
    with database.connection_context():
        # Two presses of Migrate used to mean two executors rewriting the same
        # files, and whichever answered last decided the module's fate.
        busy = (
            Job.select()
            .where((Job.module == module_id) & (Job.status.in_(IN_FLIGHT)))
            .exists()
        )
        if busy:
            raise AlreadyRunning(
                f"{name} is already being migrated."
            )
        if batch_id is None and Batch.select().where(
            (Batch.migration == migration_id) & (Batch.status == "checkpoint")
        ).exists():
            raise ContractError("Review the last batch before starting another.")
    job_id = str(uuid.uuid4())
    # Build first so a loopback app_base_url never leaves a queued row behind.
    payload = build_trigger(
        migration_id=migration_id,
        job_id=job_id,
        module_id=module_id,
        app_base_url=settings["app_base_url"],
    )
    with database.connection_context():
        migration = Migration.get_by_id(migration_id)
        module = Module.get_by_id(module_id)
        owns_batch = batch_id is None
        if owns_batch:
            batch = Batch.create(
                migration=migration,
                # A smoke-run is not a batch to review, and its verdict has
                # to be written when its result lands rather than read back
                # a millisecond after the trigger left.
                status="smoke" if smoke else "running",
                cartridge_id=migration.cartridge_id or "stock",
                cartridge_version=migration.cartridge_version or "1.0.0",
                model_webhook_url=(
                    migration.model_webhook_path or "migrate-module"
                ).strip("/"),
            )
            batch_id = batch.id
        else:
            batch = Batch.get_by_id(batch_id)
        webhook_path = (
            batch.model_webhook_url or migration.model_webhook_path or "migrate-module"
        ).strip("/")
        payload["webhook_path"] = webhook_path
        job = Job.create(
            job_id=job_id,
            batch=batch,
            module=module,
            status="queued",
            rules_hash=stack_hash(payload["rules"]),
            cartridge_id=migration.cartridge_id or "stock",
            cartridge_version=migration.cartridge_version or "1.0.0",
        )
        was = module.status
        module.status = "queued"
        # A flag from an earlier run of this module says nothing about this
        # one, and nothing else ever cleared it: the badge would have stayed
        # on a module that came back clean.
        module.has_assumptions = False
        module.save()
        _emit(hub, job, module)
        job.status = "running"
        job.save()
        module.status = "running"
        module.save()
        _emit(hub, job, module)

    try:
        executor.start(payload)
    except Exception as refused:
        # The trigger never reached the executor, so nothing is running.
        # This used to leave a job and a module stuck on "running" and a
        # batch stuck on "running" with it: Migrate then answered "already
        # being migrated", a new batch answered "already running", and only
        # a restart cleared it. With n8n down it was a 500 and a traceback.
        _undo_start(job_id, module_id, was, batch_id if owns_batch else None, hub)
        raise ContractError(f"The executor did not take the job: {refused}") from refused

    return job


def _undo_start(
    job_id: str, module_id: int, status: str, batch_id: int | None, hub: EventHub
) -> None:
    with database.connection_context():
        job = Job.get_or_none(Job.job_id == job_id)
        module = Module.get_by_id(module_id)
        module.status = status
        module.save()
        if job is not None:
            job.delete_instance()
        if batch_id is not None:
            batch = Batch.get_or_none(Batch.id == batch_id)
            if batch is not None and not batch.jobs.exists():
                batch.delete_instance()
        hub.publish(
            "module",
            {
                "id": module.id,
                "status": module.status,
                "stage": "",
                "attempt": 0,
                "migration_id": str(module.migration_id),
                "question": None,
                "has_assumptions": bool(module.has_assumptions),
                "technical_name": module.technical_name,
                "current_session_id": "",
            },
        )
        hub.publish("running", {"count": running_migration_count()})
        hub.publish("lamp", {"state": lamp_state()})


def start_batch(
    *,
    migration_id: str,
    executor,
    hub: EventHub,
    batch_group: int | None = None,
    step: int | None = None,
) -> Batch:
    if step is not None:
        ordered = modules_in_step_order(migration_id, step)
        record_group = step
    elif batch_group is not None:
        ordered = modules_in_batch_order(migration_id, batch_group)
        record_group = batch_group
    else:
        raise ContractError("No batch or step selected.")
    if not ordered:
        empty = (
            "That step has no modules in the plan."
            if step is not None
            else "That batch is empty. Group modules on the tree first."
        )
        raise ContractError(empty)
    ranks = steps_for_migration(migration_id)
    wave = min(ranks.get(row["technical_name"], 1) for row in ordered)
    blocked = earlier_steps_blocking(migration_id, wave)
    if blocked:
        raise ContractError(blocked)
    with database.connection_context():
        busy = (
            Batch.select()
            .where((Batch.migration == migration_id) & (Batch.status == "running"))
            .exists()
        )
        if busy:
            raise ContractError("A batch is already running on this migration.")
        if Batch.select().where(
            (Batch.migration == migration_id) & (Batch.status == "checkpoint")
        ).exists():
            raise ContractError("Review the last batch before starting another.")
        migration = Migration.get_by_id(migration_id)
    from odoo_migrator.cartridges import smoke_allows_batch

    if not smoke_allows_batch(migration):
        raise ContractError("A smoke-run must pass before a batch.")
    with database.connection_context():
        batch = Batch.create(
            migration=migration,
            status="running",
            batch_group=record_group,
            cartridge_id=migration.cartridge_id or "stock",
            cartridge_version=migration.cartridge_version or "1.0.0",
            model_webhook_url=(
                migration.model_webhook_path or "migrate-module"
            ).strip("/"),
        )
        batch_id = batch.id
    # One module. The next starts when this one's result arrives. A loop
    # here only looked sequential because the stub answers inline: a real
    # executor takes the trigger and returns, so the loop fired every
    # module at once and never saw a stop status.
    start_job(
        migration_id=migration_id,
        module_id=ordered[0]["id"],
        executor=executor,
        hub=hub,
        batch_id=batch_id,
    )
    with database.connection_context():

        return Batch.get_by_id(batch_id)


def close_batch(batch_id: int, hub: EventHub | None = None) -> None:
    """The sequential run is over. Review lives on this status; Task 12 reads it."""
    with database.connection_context():
        batch = Batch.get_by_id(batch_id)
        batch.status = "checkpoint"
        batch.save()
        migration_id = str(batch.migration_id)
    if hub is not None:
        # The run screen exists to be watched. Without this the batch ends,
        # the pipeline panel says nothing is running, and the review the
        # human is waiting for only appears if they think to reload.
        hub.publish("checkpoint", {"migration_id": migration_id, "batch_id": batch_id})


# The stub answers inside ``executor.start``, so advancing from a result
# re-enters this function one frame deeper per module, and a long batch
# would run out of stack. A nested call hands its work to the outermost
# one instead, which keeps the depth flat.
_advancing = threading.local()


def advance_batch(job_id: str, executor, hub: EventHub) -> None:
    """Start the batch's next module, or close the batch. Driven by results."""
    queued = getattr(_advancing, "queued", None)
    if queued is not None:
        queued.append(job_id)

        return
    _advancing.queued = [job_id]
    try:
        while _advancing.queued:
            _advance_one(_advancing.queued.pop(0), executor, hub)
    finally:
        _advancing.queued = None


def _advance_one(job_id: str, executor, hub: EventHub) -> None:
    with database.connection_context():
        job = Job.get_or_none(Job.job_id == job_id)
        if job is None:

            return
        batch = job.batch
        smoke_result = None
        if batch.status == "smoke":
            smoke_result = "passed" if job.status == "passed" else "failed"
        elif batch.status != "running":

            return
        batch_id = batch.id
        group = batch.batch_group
        migration_id = str(batch.migration_id)
        stopped = job.status == "failed"
        waiting = job.status == "waiting_for_human"
        done = {row.module_id for row in Job.select().where(Job.batch == batch_id)}
    if waiting:
        # The human still owes an answer. Closing would hide the rest of
        # the group behind a checkpoint they cannot resume.

        return
    if smoke_result is not None:
        from odoo_migrator.cartridges import record_smoke

        record_smoke(migration_id, smoke_result)

        return
    if stopped or group is None:
        # A stop is the batch's answer, and a batch of one has nothing next.
        close_batch(batch_id, hub)
        _pipeline_after_batch(migration_id, batch_id, executor, hub)

        return
    remaining = [
        module
        for module in modules_in_batch_order(migration_id, group)
        if module["id"] not in done
    ]
    if not remaining:
        close_batch(batch_id, hub)
        _pipeline_after_batch(migration_id, batch_id, executor, hub)

        return
    from odoo_migrator.pipeline_queue import should_advance_modules

    if not should_advance_modules(migration_id):
        close_batch(batch_id, hub)
        _pipeline_after_batch(migration_id, batch_id, executor, hub)

        return
    try:
        start_job(
            migration_id=migration_id,
            module_id=remaining[0]["id"],
            executor=executor,
            hub=hub,
            batch_id=batch_id,
        )
    except ContractError:
        # Struck out since the batch began, or already in flight from a
        # separate Migrate. Stop rather than skip past it silently; the
        # checkpoint screen is where the human sees what did not run.
        close_batch(batch_id, hub)
        _pipeline_after_batch(migration_id, batch_id, executor, hub)


def _pipeline_after_batch(
    migration_id: str,
    batch_id: int,
    executor,
    hub: EventHub,
) -> None:
    from odoo_migrator.pipeline_queue import after_batch_checkpoint

    after_batch_checkpoint(migration_id, batch_id, executor, hub)


def apply_progress(body, hub: EventHub) -> None:
    payload = body if isinstance(body, ProgressPayload) else ProgressPayload.model_validate(body)
    with database.connection_context():
        job = Job.get_or_none(Job.job_id == payload.job_id)
        if job is None:
            raise UnknownJob(payload.job_id)
        if job.status in FINISHED or job.status == "waiting_for_human":
            # A late progress used to set the job back to running, and the
            # module with it: a green module went back to in-flight with
            # nothing left to send a result. The same rewrite would un-park
            # a Wait before the human had answered.

            return
        job.current_stage = payload.stage
        job.attempt = payload.attempt
        if payload.resume_webhook_url and not job.resume_webhook_url:
            job.resume_webhook_url = payload.resume_webhook_url
        job.status = "running"
        job.save()
        module = job.module
        module.status = "running"
        module.save()
        _emit(hub, job, module)


def apply_result(body, hub: EventHub) -> str | None:
    payload = body if isinstance(body, ResultPayload) else ResultPayload.model_validate(body)
    with database.connection_context():
        job = Job.get_or_none(Job.job_id == payload.job_id)
        if job is None:
            raise UnknownJob(payload.job_id)
        if job.status in FINISHED:
            # A redelivered result would append its attempts and findings a
            # second time.

            return None
        job.status = payload.status
        if payload.current_session_id:
            job.current_session_id = payload.current_session_id
        if payload.resume_webhook_url and not job.resume_webhook_url:
            job.resume_webhook_url = payload.resume_webhook_url
        if payload.question:
            job.question = payload.question
        job.save()
        module = job.module
        module.status = payload.status
        if payload.assumptions:
            # batch_defer: the module may still be green. The flag is how
            # the checkpoint knows it carried an unreviewed call.
            module.has_assumptions = True
        module.save()
        # A batch of three would have flipped to passed after the first job
        # and hidden a later fail. The runner stamps checkpoint when it stops.
        logged = json.loads(job.file_diffs or "{}")
        for attempt in payload.attempts:
            Attempt.create(
                job=job,
                iteration=attempt.iteration,
                diff=attempt.diff or logged.get(str(attempt.iteration), ""),
                test_output=attempt.test_output,
                verdict=attempt.verdict,
                diagnosis=attempt.diagnosis,
            )
        # A cartridge that reports no attempt still rewrote the module, and
        # we watched it do it through the File API. Without this the diff of
        # a module that passed first time — the outcome most worth reading —
        # is recorded and then shown to nobody.
        reported = {str(attempt.iteration) for attempt in payload.attempts}
        for iteration in sorted(set(logged) - reported, key=lambda one: int(one)):
            Attempt.create(
                job=job,
                iteration=int(iteration),
                diff=logged[iteration],
                verdict=payload.status,
            )
        for finding in payload.findings:
            Finding.create(job=job, text=finding.text, source=finding.source)
        for assumption in payload.assumptions:
            Assumption.create(
                job=job,
                question=assumption.question,
                assumption=assumption.assumption,
            )
        _emit(hub, job, module)

    return payload.job_id


def save_notes(module_id: int, notes: str) -> None:
    with database.connection_context():
        module = Module.get_or_none(Module.id == module_id)
        if module is None:
            raise ContractError("unknown module")
        module.notes = notes
        module.save()


def answer_job(*, module_id: int, answer: str, resume, hub: EventHub) -> Job:
    """POST the spike body to the Wait URL, then mark the job running again."""
    text = (answer or "").strip()
    if not text:
        raise ContractError("Write an answer.")
    with database.connection_context():
        job = (
            Job.select()
            .where((Job.module == module_id) & (Job.status == "waiting_for_human"))
            .order_by(Job.id.desc())
            .first()
        )
        if job is None:
            raise ContractError("That module is not waiting for an answer.")
        url = job.resume_webhook_url
        if not url:
            raise ContractError("This job has no resume URL.")
        job_id = job.job_id
    # The POST is the resume. Holding the SQLite connection across it
    # would pin a writer for however long n8n takes to answer.
    resume.resume(url, text)
    with database.connection_context():
        job = Job.get(Job.job_id == job_id)
        if job.status != "waiting_for_human":
            # n8n resumed, ran to the end and posted its result before the
            # resume call returned. Writing "running" over that put a
            # finished module back in flight, on a batch that had already
            # closed, so nothing would ever move it again.

            return job
        job.status = "running"
        job.answer = text
        job.save()
        module = job.module
        module.status = "running"
        module.save()
        _emit(hub, job, module)

    return job


def live_run(migration_id: str) -> dict | None:
    """The in-flight job the run workspace should show, if any."""
    with database.connection_context():
        job = (
            Job.select()
            .join(Module)
            .where((Module.migration == migration_id) & (Job.status.in_(IN_FLIGHT)))
            .order_by(Job.id.desc())
            .first()
        )
        if job is None:

            return None

        return {
            "module_id": job.module_id,
            "module": job.module.technical_name,
            "status": job.status,
            "stage": job.current_stage or "",
            "attempt": job.attempt,
            "session_id": job.current_session_id or "",
        }


def latest_checkpoint(migration_id: str) -> dict | None:
    """The open review, if this migration has one."""
    with database.connection_context():
        batch = (
            Batch.select()
            .where((Batch.migration == migration_id) & (Batch.status == "checkpoint"))
            .order_by(Batch.id.desc())
            .first()
        )
        if batch is None:

            return None
        unread = False
        jobs = []
        for job in Job.select().where(Job.batch == batch.id).order_by(Job.id):
            stalled = job.status in ATTENTION and not job.reviewed
            unread = unread or stalled
            jobs.append(
                {
                    "id": job.id,
                    "job_id": job.job_id,
                    "module": job.module.technical_name,
                    "status": job.status,
                    "reviewed": bool(job.reviewed),
                    "question": job.question,
                    "answer": job.answer,
                    "rules_hash": job.rules_hash,
                    "stalled": stalled,
                    "attempts": [
                        {
                            "iteration": attempt.iteration,
                            "diff": attempt.diff,
                            "test_output": attempt.test_output,
                            "verdict": attempt.verdict,
                            "diagnosis": attempt.diagnosis,
                        }
                        for attempt in job.attempts
                    ],
                    "findings": [
                        {
                            "id": finding.id,
                            "text": finding.text,
                            "source": finding.source,
                            "promoted_to_version": finding.promoted_to_version,
                        }
                        for finding in job.findings
                    ],
                    "assumptions": [
                        {
                            "question": row.question,
                            "assumption": row.assumption,
                        }
                        for row in job.assumptions
                    ],
                }
            )

        return {
            "id": batch.id,
            "cartridge_id": batch.cartridge_id,
            "cartridge_version": batch.cartridge_version,
            "unread": unread,
            "jobs": jobs,
        }


def continue_batch(migration_id: str, *, override: bool = False) -> Batch:
    with database.connection_context():
        batch = (
            Batch.select()
            .where((Batch.migration == migration_id) & (Batch.status == "checkpoint"))
            .order_by(Batch.id.desc())
            .first()
        )
        if batch is None:
            raise ContractError("No batch is waiting for review.")
        unread = (
            Job.select()
            .where(
                (Job.batch == batch.id)
                & (Job.status.in_(ATTENTION))
                & (Job.reviewed == False)
            )
            .exists()
        )
        if unread and not override:
            raise ContractError(
                "Unread failed or waiting modules. Dismiss them, or override."
            )
        batch.status = "idle"
        batch.save()

        return batch


def dismiss_job(job_pk: int, migration_id: str) -> Job:
    with database.connection_context():
        job = Job.get_or_none(Job.id == job_pk)
        if job is None or str(job.batch.migration_id) != migration_id:
            raise ContractError("unknown job")
        if job.status not in ATTENTION:
            raise ContractError("Only a failed or waiting job can be dismissed.")
        job.reviewed = True
        job.save()

        return job


def promote_finding(
    finding_id: int, version: str, migration_id: str, *, rules_dir
) -> Finding:
    named = (version or "").strip()
    if not named:
        raise ContractError("Pick a version file.")
    with database.connection_context():
        finding = Finding.get_or_none(Finding.id == finding_id)
        # Scoped like dismiss_job and the File API: the screen only lists
        # this migration's findings, so an id from another one is not a
        # thing the human can have meant.
        if finding is None or str(finding.job.batch.migration_id) != migration_id:
            raise ContractError("unknown finding")
        if finding.promoted_to_version:
            raise ContractError(
                f"Already promoted to {finding.promoted_to_version}."
            )
        # The finding belongs to a job on this machine; the file write is
        # the promotion. Holding the SQLite connection across it is the
        # same mistake answer_job used to make with n8n.
        text = finding.text
        came_from = finding.job.module.technical_name
        job_ref = finding.job.job_id
    try:
        append_finding(
            named, text, rules_dir=rules_dir, module=came_from, job_id=job_ref
        )
    except RulesError as refused:
        raise ContractError(str(refused)) from refused
    with database.connection_context():
        finding = Finding.get_by_id(finding_id)
        finding.promoted_to_version = named
        finding.save()

    return finding


def batch_is_checkpoint(job_id: str) -> bool:
    with database.connection_context():
        job = Job.get_or_none(Job.job_id == job_id)
        if job is None:

            return False

        return job.batch.status == "checkpoint"

