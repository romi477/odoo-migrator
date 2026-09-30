"""Start a job, apply callbacks, reconcile after a crash."""

from __future__ import annotations

import json
import threading
import uuid
from datetime import UTC

from peewee import fn

from odoo_migrator import runlog
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
    DONE,
    earlier_steps_blocking,
    modules_in_batch_order,
    modules_in_step_order,
    path_problems_blocking,
    steps_for_migration,
)
from odoo_migrator.models import (
    Assumption,
    Attempt,
    Batch,
    CheckpointMessage,
    Finding,
    Job,
    Migration,
    Module,
    StepRun,
)
from odoo_migrator.module_paths import module_root
from odoo_migrator.port_guard import git_state, hand_edits_blocking, record_fingerprint
from odoo_migrator.rules import RulesError, append_finding

# What a restart reconciles. A job parked on waiting_for_human is not a
# casualty of the restart — the human still owes it an answer — so it is
# deliberately absent here.
NON_TERMINAL = ("queued", "running")
# No further callback is expected after these. A progress that arrives
# anyway is a retry or an out-of-order delivery, not news.
FINISHED = ("passed", "failed")
# A recorded outcome the human can put back to queued. A parked question
# is not one of these: that thread is still open.
REOPENABLE = ("passed", "failed", "unknown", "migrated")
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
    """`needs-attention` beats `running`: a stall is what the lamp is for, and
    a run somewhere else must not hide it."""
    with database.connection_context():
        waiting = Module.select().where(Module.status.in_(ATTENTION)).exists()
        running = Module.select().where(Module.status == "running").exists()

    if waiting:

        return "needs-attention"

    return "running" if running else "idle"


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


def ribbon_states(job: Job) -> list[dict]:
    """Every pipeline step of this job's cartridge, with how this lap went.

    The ribbon used to mark the current step and nothing else, so a
    highlighted `understand` said only "this is where the run is" — not
    whether it had failed, and the highlight's colour read as an alarm.
    The failure lane is not here: its steps are not the pipeline, and the
    line above the ribbon already names the one running.

    Caller holds the connection.
    """
    from odoo_migrator.cartridges import pinned_for

    runs = {
        (run.step_id or run.kind): run
        for run in StepRun.select().where(
            (StepRun.job == job) & (StepRun.attempt == job.attempt)
        )
    }
    states = []
    for step in pinned_for(job.module.migration).pipeline:
        name = step.id or step.kind
        run = runs.get(name)
        if run is None:
            state = "pending"
        elif run.finished_at is None:
            state = "running"
        else:
            state = "done" if run.ok else "failed"
        states.append({"id": name, "state": state})

    return states


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
            "needs_review": bool(module.needs_review),
            "technical_name": module.technical_name,
            "current_session_id": job.current_session_id or "",
            # The ribbon paints itself from this. Rebuilding it from the
            # stage alone meant guessing that every earlier step passed,
            # which is the question the ribbon is there to answer.
            "steps": ribbon_states(job),
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
    cartridge=None,
) -> Job:
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
    unplaced = path_problems_blocking(migration_id, [module_id])
    if unplaced:
        raise ContractError(unplaced)
    edited = hand_edits_blocking(migration_id, [module_id])
    if edited:
        raise ContractError(edited)
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
    payload = build_trigger(
        migration_id=migration_id,
        job_id=job_id,
        module_id=module_id,
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
        pinned = _pinned_identity(migration, cartridge)
        provider, model_name = recorded_pair(migration)
        job = Job.create(
            job_id=job_id,
            batch=batch,
            module=module,
            status="queued",
            rules_hash=stack_hash(payload["rules"]),
            cartridge_id=pinned[0],
            cartridge_version=pinned[1],
            cartridge_sha256=pinned[2],
            provider=provider,
            model_name=model_name,
        )
        was = module.status
        module.status = "queued"
        # A flag from an earlier run of this module says nothing about this
        # one, and nothing else ever cleared it: the badge would have stayed
        # on a module that came back clean.
        module.has_assumptions = False
        module.needs_review = False
        module.save()
        _emit(hub, job, module)
        job.status = "running"
        job.save()
        module.status = "running"
        module.save()
        _emit(hub, job, module)
        target_dir = module_root(migration, module, "new")
    # A port is written in place, and git is its undo. Without it, say so
    # where the run is read; it is not a reason to refuse.
    if target_dir is not None and git_state(target_dir) == "not_git":
        runlog.write(
            job_id,
            source="runner",
            text=(
                f"{target_dir} is not in a git repository: nothing can undo "
                "what this run writes there."
            ),
            level="warn",
            hub=hub,
        )

    try:
        executor.start(payload)
    except Exception as refused:
        # The trigger never reached the executor, so nothing is running.
        # This used to leave a job and a module stuck on "running" and a
        # batch stuck on "running" with it: Migrate then answered "already
        # being migrated", a new batch answered "already running", and only
        # a restart cleared it. A refused start was a 500 and a traceback.
        _undo_start(job_id, module_id, was, batch_id if owns_batch else None, hub)
        raise ContractError(f"The executor did not take the job: {refused}") from refused

    return job


def _pinned_identity(migration, cartridge) -> tuple[str, str, str]:
    """Id, version and sha of the cartridge this job will record."""
    if cartridge is not None:

        return (cartridge.cartridge_id, cartridge.version, cartridge.sha256 or "")
    from odoo_migrator.models import CartridgeInstall

    wanted_id = migration.cartridge_id or "stock"
    wanted_version = migration.cartridge_version or "1.0.0"
    row = CartridgeInstall.get_or_none(
        (CartridgeInstall.cartridge_id == wanted_id)
        & (CartridgeInstall.version == wanted_version)
    )
    if row is not None:

        return (row.cartridge_id, row.version, row.sha256 or "")
    # No install row means the stock file, which is a real cartridge with a
    # real hash. Recording an empty one left every job unable to say which
    # text it ran — the one thing pinning exists for.
    from odoo_migrator.cartridges import pinned_for
    from odoo_migrator.runner.cartridge import CartridgeError

    try:
        loaded = pinned_for(migration)
    except CartridgeError:

        return (wanted_id, wanted_version, "")

    return (wanted_id, wanted_version, loaded.sha256 or "")


def recorded_pair(migration) -> tuple[str, str]:
    """The card's provider and model, or the flow's default.

    An empty card means the cartridge's ``model`` block, which is what the
    settings form shows until someone saves a different pair. The job stores
    this pair, so the review names what ran, not a default the card replaced.
    """
    from odoo_migrator.cartridges import pinned_for
    from odoo_migrator.runner.cartridge import CartridgeError

    provider = (migration.provider_override or "").strip()
    name = (migration.model_override or "").strip()
    if provider and name:

        return provider, name
    try:
        named = pinned_for(migration).model
    except CartridgeError:
        named = None
    if named is None:

        return provider, name
    if name:

        return named.provider, name
    if provider:

        return provider, named.name

    return named.provider, named.name


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
                "needs_review": bool(module.needs_review),
                "technical_name": module.technical_name,
                "current_session_id": "",
            },
        )
        hub.publish("running", {"count": running_migration_count()})
        hub.publish("lamp", {"state": lamp_state()})


def _still_to_run(modules: list[dict]) -> list[dict]:
    """The ones a batch has work to do on.

    A wave is started because something in it is still queued, and the
    Pipeline queue shows exactly those. The runner did not filter, so a
    wave with one module left re-ran the three beside it that had already
    passed — and a module that had been green for an hour came back as
    `failed` the moment the shell it needed went down. Running a module
    again is what Reopen is for, and it is the human who asks.
    """

    return [module for module in modules if module["status"] not in DONE]


def start_batch(
    *,
    migration_id: str,
    executor,
    hub: EventHub,
    batch_group: int | None = None,
    step: int | None = None,
) -> Batch:
    if step is not None:
        listed = modules_in_step_order(migration_id, step)
    elif batch_group is not None:
        listed = modules_in_batch_order(migration_id, batch_group)
    else:
        raise ContractError("No batch or step selected.")
    if not listed:
        empty = (
            "That step has no modules in the plan."
            if step is not None
            else "That batch is empty. Group modules on the tree first."
        )
        raise ContractError(empty)
    ordered = _still_to_run(listed)
    if not ordered:
        raise ContractError(
            "Every module here has already passed. Reopen one to run it again."
        )
    unplaced = path_problems_blocking(migration_id, [row["id"] for row in ordered])
    if unplaced:
        raise ContractError(unplaced)
    edited = hand_edits_blocking(migration_id, [row["id"] for row in ordered])
    if edited:
        raise ContractError(edited)
    # The guard reads the wave off everything selected, not off what is left
    # to do. Dropping a passed parent from the list would raise the wave and
    # block on an earlier step the batch was never waiting for.
    ranks = steps_for_migration(migration_id)
    wave = min(ranks.get(row["technical_name"], 1) for row in listed)
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
            # Each in its own column. A step recorded in `batch_group` was
            # read back as a group, so a step batch advanced through the
            # group with the same number.
            batch_group=batch_group,
            step=step,
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
        wave = batch.step
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
    if stopped or (group is None and wave is None):
        # A stop is the batch's answer, and a batch of one has nothing next.
        close_batch(batch_id, hub)
        _pipeline_after_batch(migration_id, batch_id, executor, hub)

        return
    # The same selection the batch started from, re-read so it survives a
    # restart mid-batch.
    if wave is not None:
        selected = modules_in_step_order(migration_id, wave)
    else:
        selected = modules_in_batch_order(migration_id, group)
    remaining = _still_to_run(
        [module for module in selected if module["id"] not in done]
    )
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
        # What this run left, so the next start can tell its own uncommitted
        # port from a person's edit.
        record_fingerprint(module)
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


def _publish_module(hub: EventHub | None, module: Module) -> None:
    if hub is None:

        return
    hub.publish(
        "module",
        {
            "id": module.id,
            "status": module.status,
            "stage": "",
            "attempt": 0,
            "migration_id": str(module.migration_id),
            "question": "",
            "has_assumptions": bool(module.has_assumptions),
            "needs_review": bool(module.needs_review),
            "technical_name": module.technical_name,
            "current_session_id": "",
        },
    )


def reopen_module(module_id: int, migration_id: str, hub: EventHub | None = None) -> Module:
    """Put one finished module back to queued.

    The jobs that produced the outcome stay. This does not touch files in
    the new project: a stub pass never wrote any, and a real rewrite is
    not something this button can undo.
    """
    with database.connection_context():
        module = Module.get_or_none(Module.id == module_id)
        if module is None or str(module.migration_id) != migration_id:
            raise ContractError("unknown module")
        if module.struck_out:
            raise ContractError("An excluded module stays out of the plan.")
        if module.status not in REOPENABLE:
            raise ContractError("Only a finished module can be reopened.")
        busy = (
            Job.select()
            .where((Job.module == module) & (Job.status.in_(IN_FLIGHT)))
            .exists()
        )
        if busy:
            raise ContractError("That module is still running.")
        module.status = "queued"
        module.has_assumptions = False
        # It is about to run again, which is the review.
        module.needs_review = False
        module.save()
        marked = _mark_dependents_for_review(module)
    _publish_module(hub, module)
    for dependent in marked:
        _publish_module(hub, dependent)

    return module


def _mark_dependents_for_review(module: Module) -> list[Module]:
    """Note, on every passed module downstream of this one, that it passed
    against something that has since been reopened.

    Transitive: a module that reaches this one through others has passed
    against its result just the same. Only `passed` modules are marked — one
    that failed or is queued has nothing left to be wrong about — and an
    excluded one is not in the plan at all. The caller holds the connection.
    """
    from odoo_migrator.graph import dependents_by_name

    rows = list(Module.select().where(Module.migration == module.migration_id))
    listed = [
        {"technical_name": row.technical_name, "depends": json.loads(row.depends or "[]")}
        for row in rows
    ]
    below = dependents_by_name(listed).get(module.technical_name, set())
    marked = []
    for row in rows:
        if row.technical_name not in below:
            continue
        if row.struck_out or row.status != "passed" or row.needs_review:
            continue
        row.needs_review = True
        row.save()
        marked.append(row)

    return marked


def keep_reviewed(module_id: int, migration_id: str, hub: EventHub | None = None) -> Module:
    """A person looked and the module is fine as it is: drop the note."""
    with database.connection_context():
        module = Module.get_or_none(Module.id == module_id)
        if module is None or str(module.migration_id) != migration_id:
            raise ContractError("unknown module")
        module.needs_review = False
        module.save()
    _publish_module(hub, module)

    return module


def reopen_finished(migration_id: str, hub: EventHub | None = None) -> int:
    """Reopen every finished module still in the plan. Returns how many."""
    with database.connection_context():
        rows = list(
            Module.select().where(
                (Module.migration == migration_id)
                & (Module.struck_out == False)
                & (Module.status.in_(REOPENABLE))
            )
        )
        ids = [row.id for row in rows]
    opened = 0
    for module_id in ids:
        try:
            reopen_module(module_id, migration_id, hub)
        except ContractError:
            continue
        opened += 1

    return opened


def save_notes(module_id: int, notes: str) -> None:
    with database.connection_context():
        module = Module.get_or_none(Module.id == module_id)
        if module is None:
            raise ContractError("unknown module")
        module.notes = notes
        module.save()


def answer_job(*, module_id: int, answer: str, hub: EventHub, resume=None) -> Job:
    """Append the human's reply and let the parked runner continue.

    ``resume`` is the executor's own ``resume(job_id, answer) -> bool``.
    """
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
        job.answer = text
        job.save()
        CheckpointMessage.create(job=job, author="human", text=text)
        job_id = job.job_id
    # Only the executor knows whether it can carry this job on. The native
    # runner keeps a parked loop in a thread, and a restart takes it with it;
    # saying "running" then is a row nobody is running.
    resumed = bool(resume(job_id, text)) if callable(resume) else False
    with database.connection_context():
        job = Job.get(Job.job_id == job_id)
        job.status = "running" if resumed else "unknown"
        job.save()
        module = job.module
        module.status = job.status
        module.save()
        if not resumed:
            # Nothing is left to finish this batch. `reconcile_jobs` leaves a
            # batch `running` while one of its jobs is parked, because a
            # person still owes it an answer; this is where that stops being
            # true. Left running it blocks every later batch on the migration
            # behind "A batch is already running on this migration", and no
            # screen can clear that — review works off `checkpoint`.
            Batch.update(status="unknown").where(
                (Batch.id == job.batch_id) & (Batch.status == "running")
            ).execute()
        _emit(hub, job, module)

    return job


def stop_job(*, module_id: int, hub: EventHub, stop=None) -> bool:
    """Ask the run on this module to end at its next step.

    ``stop`` is the executor's own ``stop(job_id) -> bool``. Only it knows
    whether the thread that is running this job is in this process; a restart
    takes those with it, and saying "stopping" about a job nobody is running
    is the same lie `answer_job` refuses to tell.
    """
    with database.connection_context():
        job = (
            Job.select()
            .where((Job.module == module_id) & (Job.status.in_(IN_FLIGHT)))
            .order_by(Job.id.desc())
            .first()
        )
        if job is None:
            raise ContractError("That module is not running.")
        job_id = job.job_id
    asked = bool(stop(job_id)) if callable(stop) else False
    if not asked:
        # Nothing is running it here. Mark it the way a restart would, so the
        # row does not sit `running` for ever.
        with database.connection_context():
            job = Job.get(Job.job_id == job_id)
            job.status = "unknown"
            job.save()
            module = job.module
            module.status = "unknown"
            module.save()
            _emit(hub, job, module)

    return asked


def checkpoint_thread(migration_id: str, *, job_id: str = "") -> list[dict]:
    """The human and the agent, in order. A parked question counts even
    before its row is written.

    With ``job_id`` it is that one job's conversation. The unscoped thread is
    every message in the migration, which on a screen watching one run meant
    the diagnosis of the module before it — or of this module's previous
    attempt — sitting under a run that had just started.
    """
    with database.connection_context():
        rows = (
            CheckpointMessage.select()
            .join(Job)
            .join(Module)
            .where(Module.migration == migration_id)
            .order_by(CheckpointMessage.id)
        )
        waiting = (
            Job.select()
            .join(Module)
            .where(
                (Module.migration == migration_id)
                & (Job.status == "waiting_for_human")
                & (Job.question.is_null(False))
                & (Job.question != "")
            )
            .order_by(Job.id.desc())
        )
        if job_id:
            current = Job.get_or_none(Job.job_id == job_id)
            if current is None:

                return []
            rows = rows.where(CheckpointMessage.job == current)
            waiting = waiting.where(Job.id == current.id)
        messages = [{"author": row.author, "text": row.text} for row in rows]
        parked = waiting.first()
        if parked is not None and not any(
            item["text"] == parked.question for item in messages
        ):
            messages.append({"author": "agent", "text": parked.question})

        return messages


def outcomes_for(migration_id: str) -> dict[int, dict]:
    """Per module, when its last job ended and whether it left anything to read.

    Three queries for a board of thirty cards, not one per card. A card said
    `passed` and nothing else — not when, which is the first thing anyone
    asks of a result they are looking at a day later.
    """
    with database.connection_context():
        last: dict[int, Job] = {}
        for job in (
            Job.select(Job.id, Job.module, Job.job_id, Job.status)
            .join(Module)
            .where(Module.migration == migration_id)
            .order_by(Job.id)
        ):
            last[job.module_id] = job
        ids = [job.id for job in last.values()]
        if not ids:

            return {}
        ended = {
            row.job_id: row.last
            for row in StepRun.select(
                StepRun.job, fn.MAX(StepRun.finished_at).alias("last")
            )
            .where(StepRun.job.in_(ids))
            .group_by(StepRun.job)
        }
        said = {
            row.job_id: row.count
            for row in CheckpointMessage.select(
                CheckpointMessage.job, fn.COUNT(CheckpointMessage.id).alias("count")
            )
            .where(CheckpointMessage.job.in_(ids))
            .group_by(CheckpointMessage.job)
        }

    return {
        module_id: {
            "finished_at": _stamp(ended.get(job.id)),
            "messages": said.get(job.id, 0),
            "job": job.job_id[:8],
        }
        for module_id, job in last.items()
    }


def module_outcome(module_id: int) -> dict | None:
    """The last job on one module: when it ended, how, and what it said.

    A card said `passed` and nothing else — not when, and not what the run
    had made of it. The checkpoint thread was on the Result screen as one
    flat list for the whole migration, so a paragraph about one module sat
    above thirty cards with nothing tying it to any of them.
    """
    with database.connection_context():
        job = (
            Job.select()
            .where(Job.module == module_id)
            .order_by(Job.id.desc())
            .first()
        )
        if job is None:

            return None
        runs = list(
            StepRun.select()
            .where(StepRun.job == job)
            .order_by(StepRun.id)
        )
        messages = [
            {
                "author": row.author,
                "text": row.text,
                "at": _stamp(row.created_at),
            }
            for row in CheckpointMessage.select()
            .where(CheckpointMessage.job == job)
            .order_by(CheckpointMessage.id)
        ]
    ended = [run.finished_at for run in runs if run.finished_at]
    finished = max(ended) if ended else None
    steps = [
        {
            "id": run.step_id or run.kind,
            "attempt": run.attempt,
            "ok": run.ok,
            "output": run.output or "",
            "tokens": (
                {"in": run.tokens_in, "out": run.tokens_out}
                if run.tokens_in or run.tokens_out
                else None
            ),
        }
        for run in runs
    ]

    return {
        "job_id": job.job_id,
        "short": job.job_id[:8],
        "status": job.status,
        "attempt": job.attempt,
        "stage": job.current_stage or "",
        "finished_at": _stamp(finished),
        "steps": steps,
        "tokens": {
            "in": sum(run.tokens_in for run in runs),
            "out": sum(run.tokens_out for run in runs),
            "cached": sum(run.tokens_cached for run in runs),
        },
        "sessions": _sessions(runs),
        "testing": _testing(runs),
        "messages": messages,
    }


def _testing(runs: list) -> list[dict]:
    """What happened to the tests, in the order it happened.

    A summary that lists `write_test` says a test was written and nothing
    about whether it ran. The pipeline's own `tests` step runs before that
    one, so for a module that shipped none the only run there ever is is the
    one the agent makes inside `write_test` — and it was recorded as a tool
    call nobody reads.

    Caller holds the connection.
    """
    from odoo_migrator.models import ToolCall

    ids = [run.id for run in runs]
    if not ids:

        return []
    by_run = {run.id: run for run in runs}
    told = []
    for call in (
        ToolCall.select()
        .where(ToolCall.step_run.in_(ids))
        .order_by(ToolCall.id)
    ):
        wrote_test = call.tool == "write_file" and "/tests/" in (call.preview or "")
        if call.tool not in ("run_test", "list_tests") and not wrote_test:
            continue
        step = by_run.get(call.step_run_id)
        told.append(
            {
                "step": (step.step_id or step.kind) if step else "",
                "attempt": step.attempt if step else 0,
                "what": "wrote" if wrote_test else call.tool,
                "detail": call.preview or "",
                "ok": call.ok,
            }
        )

    return told


def _sessions(runs: list) -> list[dict]:
    """The Sheller sessions this job worked in, in the order it opened them.

    The summary named the steps and never where they ran, so reading the
    daemon's own journal for a run meant grepping the log for the line that
    printed the ids. `ShellerSession` is no help: a row is deleted when the
    session closes, so a finished job has none.

    A side has more than one when a session was reopened, which is ordinary:
    a rewrite invalidates the session on the new container, and a checkpoint
    closes both. So these are listed, not folded into one per side.

    What is counted is what became a command in that session's journal: a
    call the daemon gave an id. The host's file tools never touch it, the
    routes (`list_tests`, `journal`, `history`) are not commands, and a call
    the daemon refused — `session_busy` — never became one. Counting every
    row made a run that read eight files and wrote five show eighteen calls
    beside a journal of five commands. Rows written before the runner stopped
    stamping a session on the file tools carry one still, and are skipped by
    the missing id all the same.

    Caller holds the connection.
    """
    from odoo_migrator.models import ToolCall

    ids = [run.id for run in runs]
    if not ids:

        return []
    by_run = {run.id: run for run in runs}
    seen: dict[tuple[str, str], dict] = {}
    for call in (
        ToolCall.select()
        .where(ToolCall.step_run.in_(ids))
        .order_by(ToolCall.id)
    ):
        if not call.session_id or not call.command_id:
            continue
        key = (call.side, call.session_id)
        row = seen.get(key)
        if row is None:
            row = {
                "side": call.side,
                "session_id": call.session_id,
                "calls": 0,
                "steps": [],
            }
            seen[key] = row
        row["calls"] += 1
        step = by_run.get(call.step_run_id)
        label = (step.step_id or step.kind) if step else ""
        if label and label not in row["steps"]:
            row["steps"].append(label)

    return list(seen.values())


def _stamp(value) -> str:
    """The instant, in ISO-8601 with its offset. The browser makes it local.

    This used to truncate to minutes here, which threw the offset away and
    left every date on Result reading as if it were the reader's own clock
    when it was UTC. Only the browser knows the reader's zone, so the
    formatting moved there and what crosses is the unambiguous instant.

    A naive value is read as UTC, because that is what `models._now` writes.
    """
    if not value:

        return ""
    if isinstance(value, str):

        return value
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)

    return value.isoformat()


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
            "job_id": job.job_id,
            "module_id": job.module_id,
            "module": job.module.technical_name,
            "status": job.status,
            "stage": job.current_stage or "",
            "attempt": job.attempt,
            "session_id": job.current_session_id or "",
            "question": job.question or "",
        }


def recent_job(migration_id: str) -> dict | None:
    """The job the run log should show: the one in flight, else the last one.

    Not the same question as `live_run`. A run that failed two minutes ago
    is exactly the one someone wants to read, and it is no longer live.
    """
    with database.connection_context():
        job = (
            Job.select()
            .join(Module)
            .where(Module.migration == migration_id)
            .order_by(Job.id.desc())
            .first()
        )
        if job is None:

            return None

        # Whether there is anything to show. A job whose log was cleared and
        # a migration that has never run are different things, and the panel
        # showed an empty box for both.
        from odoo_migrator.models import RunLog

        lines = RunLog.select().where(RunLog.job == job).count()

        return {
            "job_id": job.job_id,
            "module": job.module.technical_name,
            "status": job.status,
            "live": job.status in IN_FLIGHT,
            "lines": lines,
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
                    "provider": job.provider or "",
                    "model": job.model_name or "",
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
        # same mistake a long write used to make while a callback was in flight.
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

