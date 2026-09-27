"""Run one job on a background thread. The database is never shared with it."""

from __future__ import annotations

import contextlib
import json
import shutil
import threading
import traceback
from datetime import UTC, datetime
from pathlib import Path

from odoo_migrator import runlog
from odoo_migrator.db import database
from odoo_migrator.events import EventHub
from odoo_migrator.jobs import apply_progress, apply_result
from odoo_migrator.models import CheckpointMessage, Job, Migration, Module, StepRun, ToolCall
from odoo_migrator.module_paths import module_root
from odoo_migrator.runner.cartridge import load_cartridge
from odoo_migrator.runner.context import StepContext, StepResult
from odoo_migrator.runner.loop import run_job
from odoo_migrator.runner.registry import resolve
from odoo_migrator.runner.sessions import close_side, ensure_usable, open_side
from odoo_migrator.runner.snapshot import diff as diff_snapshots
from odoo_migrator.runner.snapshot import snapshot
from odoo_migrator.sheller.errors import ShellerError

# job_id -> event the human's answer releases. The thread that parked is
# the only one that waits; answer_job sets the event from the request thread.
# What the migration card says when it says nothing. Same default as the
# column, so a blank card does not quietly run a different number of laps.
_RETRY_LIMIT = 5

_WAITERS: dict[str, threading.Event] = {}
_ANSWERS: dict[str, str] = {}
_GUARD = threading.Lock()

# One job at a time on a target database. The queue runs one module at a
# time, but Migrate on the tree does not wait for it, and two jobs rebuilding
# one database's registry at once fight over `ir_module_module` — a lock
# wait, a serialization failure, or an install of the other job's module
# half-way through its own port. Held while a job runs, released while it is
# parked on a person.
_TARGETS: dict[tuple[str, str], threading.Lock] = {}


def _target_lock(ctx) -> threading.Lock | None:
    target = ctx.session_targets.get("new")
    if not target:

        return None
    key = (target["container"], target["database"])
    with _GUARD:

        return _TARGETS.setdefault(key, threading.Lock())


def resume_waiter(job_id: str, answer: str) -> bool:
    """Release a parked runner. False when nothing in this process is waiting.

    A parked job's loop lives in a thread, so a restart takes it with them.
    The caller has to know, or it marks a job running that nobody is running.
    """
    with _GUARD:
        event = _WAITERS.get(job_id)
        if event is None:

            return False
        _ANSWERS[job_id] = answer
    event.set()

    return True


class NativeRunner:
    """``start`` returns at once. The job runs on its own thread."""

    def __init__(self, *, sheller, hub: EventHub, progress=None, result=None, provider=None):
        self.sheller = sheller
        self.hub = hub
        self._progress = progress or (lambda body: apply_progress(body, hub))
        self._result = result or (lambda body: apply_result(body, hub))
        # Only a test passes one. A live run asks the registry for the
        # provider the cartridge names, step by step.
        self.provider = provider
        self._threads: dict[str, threading.Thread] = {}
        self._errors: dict[str, BaseException] = {}

    def start(self, payload: dict) -> None:
        job_id = payload["job_id"]
        thread = threading.Thread(
            target=self._run,
            args=(payload,),
            name=f"migrate-{job_id}",
            daemon=True,
        )
        self._threads[job_id] = thread
        thread.start()

    def resume(self, job_id: str, answer: str) -> bool:
        """True when this process still has the thread that parked the job."""

        return resume_waiter(job_id, answer)

    def wait(self, job_id: str, timeout: float = 10) -> None:
        thread = self._threads[job_id]
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError(job_id)
        error = self._errors.get(job_id)
        if error is not None:
            raise error

    def _log(self, job_id: str, source: str, text: str, detail: str = "", level: str = "info") -> None:

        runlog.write(job_id, source=source, text=text, detail=detail, level=level, hub=self.hub)

    def _run(self, payload: dict) -> None:
        job_id = payload["job_id"]
        ctx = None
        seen_attempt = 0

        def resolve_step(kind: str):
            runner = resolve(kind)

            def wrapped(step, current):
                nonlocal seen_attempt
                if current.attempt != seen_attempt:
                    seen_attempt = current.attempt
                    self.hub.publish(
                        "attempt_started",
                        {"job_id": job_id, "attempt": current.attempt},
                    )
                    self._log(job_id, "runner", f"attempt {current.attempt}")
                step_run = self._open_step(current, step)
                current.step_run_id = step_run
                label = step.id or step.kind
                current.step_run_label = label
                self.hub.publish(
                    "step_started",
                    {"job_id": job_id, "step": label, "kind": step.kind, "attempt": current.attempt},
                )
                self._progress({"job_id": job_id, "stage": label, "attempt": current.attempt})
                self._log(job_id, label, f"{step.kind} step started")
                self._mend_sessions(current, label)
                writes = "write_file" in (step.tools or ())
                before = snapshot(_module_root(current)) if writes else {}
                try:
                    if step.fresh:
                        self._clear_module(current, label)
                    result = runner(step, current)
                except Exception as exc:
                    # The row is closed either way. It used to stay open with
                    # no outcome, and the trace showed a step still running
                    # for a job that had long failed.
                    self._close_step(
                        step_run,
                        StepResult(ok=False, output=f"{type(exc).__name__}: {exc}"),
                    )
                    raise
                if writes:
                    self._note_diff(current, before)
                self._close_step(step_run, result)
                self._log(
                    job_id,
                    label,
                    _outcome_line(result),
                    detail=result.detail or result.output,
                    level="info" if result.ok else "error",
                )
                self.hub.publish(
                    "step_finished",
                    {
                        "job_id": job_id,
                        "step": label,
                        "ok": result.ok,
                        "flags": sorted(result.flags),
                    },
                )

                return result

            return wrapped

        try:
            # Building the context is part of the run, not a prelude to it: a
            # container that is not up, a probe that fails, a cartridge that
            # will not load and a missing model key all raise here. Outside
            # this try they killed the thread with nobody told, and the job
            # sat queued until a restart called it unknown.
            self._log(job_id, "runner", "job started")
            cartridge = self._cartridge(payload)
            self._log(
                job_id,
                "runner",
                f"cartridge {cartridge.cartridge} {cartridge.version} "
                f"({cartridge.sha256[:12]}), model {cartridge.model.provider}/"
                f"{cartridge.model.name}",
            )
            ctx = self._context(payload, cartridge)
            self._log(job_id, "runner", _where_line(ctx), detail=_where_detail(ctx))
            # Opened here rather than inside `_context`, so `ctx` is already
            # bound when it happens. Opening the old side and then failing on
            # the new one used to leave `ctx` unassigned, and the `finally`
            # below closed nothing: a live odoo-bin stayed on the host.
            ctx.baseline_files = snapshot(_module_root(ctx))
            self._open_sessions(ctx)
            self._log(
                job_id,
                "runner",
                "sessions open: "
                + ", ".join(f"{side}={value}" for side, value in sorted(ctx.sessions.items())),
            )
            lock = _target_lock(ctx)
            ctx.target_lock = lock
            self._hold_target(ctx)
            try:
                outcome = run_job(
                    cartridge,
                    ctx,
                    retry_limit=(ctx.migration.retry_limit or _RETRY_LIMIT),
                    resolve=resolve_step,
                )
            finally:
                self._release_target(ctx)
        except Exception as exc:  # noqa: BLE001 - the job has to hear about every failure.
            self._errors[job_id] = exc
            self._log(
                job_id,
                "runner",
                f"{type(exc).__name__}: {exc}",
                detail="".join(traceback.format_exception(exc)),
                level="error",
            )
            self._fail(job_id, exc, attempt=ctx.attempt if ctx is not None else 1)

            return
        finally:
            # A job that ends, fails or is abandoned leaves no odoo-bin behind.
            # A parked one has already closed its own; closing twice is a 404
            # the daemon answers and this swallows.
            if ctx is not None:
                self._close_sessions(ctx)
        if outcome != "waiting_for_human":
            self._result(
                {
                    "job_id": job_id,
                    "status": outcome if outcome in ("passed", "failed") else "failed",
                    "attempts": [
                        {
                            "iteration": ctx.attempt,
                            "diff": ctx.previous_diff,
                            "verdict": outcome,
                        }
                    ],
                    "findings": list(ctx.findings),
                }
            )
        self._log(
            job_id,
            "runner",
            f"job {outcome}",
            detail=ctx.last_failure,
            level="error" if outcome == "failed" else "info",
        )
        self.hub.publish("job_finished", {"job_id": job_id, "status": outcome})

    def _fail(self, job_id: str, exc: BaseException, attempt: int = 1) -> None:
        """Tell the app the job failed, then say so on the stream.

        Publishing the event without the result left the row running while
        the screen said it had stopped.
        """
        with contextlib.suppress(Exception):
            self._result(
                {
                    "job_id": job_id,
                    "status": "failed",
                    "attempts": [
                        {"iteration": attempt, "verdict": f"{type(exc).__name__}: {exc}"}
                    ],
                }
            )
        self.hub.publish(
            "job_finished",
            {"job_id": job_id, "status": "failed", "error": str(exc)},
        )

    def _note_diff(self, ctx: StepContext, before: dict) -> None:
        """Record what the step wrote, for the checkpoint and the next prompt.

        The checkpoint keeps each step's own hunk, per attempt. The prompt
        gets the whole change since the job began.
        """
        after = snapshot(_module_root(ctx))
        hunk = diff_snapshots(before, after)
        if not hunk:

            return
        ctx.previous_diff = diff_snapshots(ctx.baseline_files, after)
        with database.connection_context():
            job = Job.get(Job.job_id == ctx.job_id)
            blob = json.loads(job.file_diffs or "{}")
            key = str(ctx.attempt)
            blob[key] = (blob.get(key) or "") + hunk
            job.file_diffs = json.dumps(blob)
            job.save()

    def _close_sessions(self, ctx: StepContext) -> None:
        # A session already gone is not a failure: the daemon answers 404 for
        # one whose process died, and closing twice is normal on the parked
        # path.
        for side in list(ctx.sessions):
            close_side(ctx, side)

    def _open_sessions(self, ctx: StepContext) -> None:
        for side in ctx.session_targets:
            if ctx.sessions.get(side):
                continue
            open_side(ctx, side)

    def _mend_sessions(self, ctx: StepContext, label: str) -> None:
        """A session that died or is stuck on an old command is fixed here.

        Nothing did this before: after one timeout or one crashed shell,
        every later step on that side answered `session_busy` or
        `session_dead`, and the failure lane diagnosed the session instead
        of the module.
        """
        for side in list(ctx.session_targets):
            if not ctx.sessions.get(side):
                continue
            try:
                done = ensure_usable(ctx, side)
            except ShellerError as exc:
                self._log(ctx.job_id, label, f"{side} session unusable: {exc}", level="warn")
                continue
            if done:
                self._log(ctx.job_id, label, done, level="warn")

    def _clear_module(self, ctx: StepContext, label: str) -> None:
        """Empty the module's directory on the new side, for a step that starts over.

        `rewrite` is told not to reconstruct the previous attempt, and then
        found it on disk: whatever it did not overwrite stayed in the module
        and was imported with the rest. Only a directory strictly inside the
        new project root, holding a manifest, is cleared.
        """
        root = _module_root(ctx)
        project = Path(ctx.migration.new_project_path or "").expanduser().resolve()
        if root is None or not root.is_dir() or not str(ctx.migration.new_project_path or "").strip():

            return
        resolved = root.resolve()
        if resolved == project or not resolved.is_relative_to(project):
            self._log(ctx.job_id, label, f"not clearing {root}: outside the new project", level="warn")

            return
        # A migration created before the roots were checked against each
        # other can still have them overlap, and the directory found under
        # the new root is then the old module itself.
        old_raw = str(ctx.migration.old_project_path or "").strip()
        if old_raw:
            old = Path(old_raw).expanduser().resolve()
            if resolved.is_relative_to(old) or old.is_relative_to(resolved):
                self._log(
                    ctx.job_id,
                    label,
                    f"not clearing {root}: it is inside the old project {old}",
                    level="error",
                )

                return
        if not (resolved / "__manifest__.py").is_file():

            return
        removed = 0
        for child in sorted(resolved.iterdir()):
            if child.is_dir() and not child.is_symlink():
                shutil.rmtree(child)
            else:
                child.unlink()
            removed += 1
        self._log(ctx.job_id, label, f"cleared {removed} entr(ies) from {root} before starting over")

    def _hold_target(self, ctx: StepContext) -> None:
        lock = getattr(ctx, "target_lock", None)
        if lock is None:

            return
        if not lock.acquire(blocking=False):
            target = ctx.session_targets["new"]
            self._log(
                ctx.job_id,
                "runner",
                f"waiting for {target['container']}/{target['database']}: "
                "another job is migrating on it",
                level="warn",
            )
            lock.acquire()
        ctx.target_held = True

    def _release_target(self, ctx: StepContext) -> None:
        lock = getattr(ctx, "target_lock", None)
        if lock is not None and getattr(ctx, "target_held", False):
            ctx.target_held = False
            lock.release()

    def _cartridge(self, payload: dict):
        """The cartridge this migration pinned, not always the stock file.

        The payload carries no text, so this used to load
        ``cartridges/stock.yaml`` whatever the card said — an imported
        cartridge was pinned, recorded on the job, drawn on Flow, and never
        run. ``pinned_for`` is the same lookup the screens use.
        """
        text = payload.get("cartridge_text")
        if text:

            return load_cartridge(text)
        from odoo_migrator.cartridges import pinned_for

        with database.connection_context():
            job = Job.get(Job.job_id == payload["job_id"])
            module = Module.get_by_id(job.module_id)
            migration = Migration.get_by_id(module.migration_id)

        return pinned_for(migration)

    def _context(self, payload: dict, cartridge) -> StepContext:
        with database.connection_context():
            job = Job.get(Job.job_id == payload["job_id"])
            module = Module.get_by_id(job.module_id)
            migration = Migration.get_by_id(module.migration_id)
        targets = self._targets(migration)

        def record(tool, side, session_id, command_id, duration_ms, ok, preview):
            self._log(
                payload["job_id"],
                ctx.step_run_label or "runner",
                f"{tool}({side}) {duration_ms}ms {'ok' if ok else 'failed'}: {preview}",
                level="info" if ok else "warn",
            )
            with database.connection_context():
                ToolCall.create(
                    step_run=ctx.step_run_id,
                    tool=tool,
                    side=side,
                    session_id=session_id or "",
                    command_id=command_id or "",
                    duration_ms=duration_ms,
                    ok=ok,
                    preview=preview or "",
                )
            self.hub.publish(
                "tool_called",
                {
                    "job_id": payload["job_id"],
                    "tool": tool,
                    "side": side,
                    "session_id": session_id,
                    "command_id": command_id,
                },
            )

        def wait_for_human(question: str) -> str:
            """Park, and stop holding two Odoo processes while a person thinks.

            A checkpoint can sit for a day. The sessions close here and open
            again on the answer — the deliberate exception to one session for
            the whole run, which holds for a job that is running, not one that
            is waiting on a person.
            """
            event = threading.Event()
            with _GUARD:
                _WAITERS[payload["job_id"]] = event
            with database.connection_context():
                parked = Job.get(Job.job_id == payload["job_id"])
                CheckpointMessage.create(job=parked, author="agent", text=question)
            self._close_sessions(ctx)
            # A person may take a day. Another job can use the database
            # meanwhile; this one takes it back before it touches it again.
            self._release_target(ctx)
            self._result(
                {
                    "job_id": payload["job_id"],
                    "status": "waiting_for_human",
                    "question": question,
                }
            )
            event.wait()
            with _GUARD:
                _WAITERS.pop(payload["job_id"], None)
                answer = _ANSWERS.pop(payload["job_id"], "")
            self._hold_target(ctx)
            self._open_sessions(ctx)

            return answer

        ctx = StepContext(
            job_id=payload["job_id"],
            attempt=1,
            migration=migration,
            module=module,
            cartridge=cartridge,
            sheller=self.sheller,
            sessions={},
            write_keys={},
            session_targets=targets,
            emit=lambda event: self.hub.publish(event.get("kind", "event"), event),
            provider=self.provider,
            record_tool=record,
            wait_for_human=wait_for_human,
        )

        return ctx

    def _targets(self, migration) -> dict[str, dict]:
        """Where each side runs, as ``open_session`` wants it.

        ``odoo_bin`` and the database name come from the daemon's probe, which
        runs inside the container with its own Python. They used to be two
        constants here, which is fine until a project is not the one they were
        typed for.
        """
        owner = {"kind": "agent", "migration_id": migration.id}
        targets = {}
        for side, container, chosen in (
            ("old", migration.old_container, migration.old_database),
            ("new", migration.new_container, migration.new_database),
        ):
            if not container:
                raise RuntimeError(f"the {side} container of this migration is not up")
            probed = self.sheller.probe(container)
            known = probed.get("databases") or []
            # The card first, then the container's own odoo.conf. A config
            # without `db_name` is ordinary and one Postgres commonly holds
            # dozens of databases, so the probe cannot be the only answer.
            database_name = (chosen or "").strip() or (probed.get("db_name") or "")
            if not database_name:
                raise RuntimeError(
                    f"the {side} container {container} names no database in its "
                    f"config, and the migration card names none either. "
                    f"{_choices(known)}"
                )
            # Opening a shell on a database that is not there starts a process
            # that dies at once, and all the job could record was session_dead.
            if known and database_name not in known:
                raise RuntimeError(
                    f"database {database_name!r} is not on the {side} container "
                    f"{container}. {_choices(known)}"
                )
            targets[side] = {
                "container": container,
                "database": database_name,
                "odoo_bin": probed.get("odoo_bin") or "",
                "owner": dict(owner),
            }

        return targets

    def _open_step(self, ctx: StepContext, step) -> int:
        with database.connection_context():
            job = Job.get(Job.job_id == ctx.job_id)
            row = StepRun.create(
                job=job,
                attempt=ctx.attempt,
                step_id=step.id or step.kind,
                kind=step.kind,
            )

            return row.id

    def _close_step(self, step_run_id: int, result) -> None:
        with database.connection_context():
            row = StepRun.get_by_id(step_run_id)
            row.finished_at = datetime.now(UTC)
            row.ok = result.ok
            row.flags = json.dumps(sorted(result.flags))
            row.tokens_in = result.tokens_in
            row.tokens_out = result.tokens_out
            row.tokens_cached = result.tokens_cached
            row.output = (result.output or "")[:4000]
            row.save()


# Enough to recognise the one that was meant; a shared Postgres can hold
# dozens, and the whole list would bury the sentence in front of it.
_SHOWN = 12


def _choices(known: list) -> str:
    if not known:

        return "It reports no databases at all."
    listed = ", ".join(str(name) for name in known[:_SHOWN])
    if len(known) > _SHOWN:

        return f"It has {len(known)}, among them: {listed}."

    return f"It has: {listed}."


def _outcome_line(result) -> str:
    """One line a person can scan. The body goes to the line's detail."""
    head = "ok" if result.ok else "failed"
    flags = ", ".join(sorted(result.flags))
    spent = ""
    if result.tokens_in or result.tokens_out:
        spent = f", {result.tokens_in}+{result.tokens_out} tokens"
    reason = (result.output or "").strip().splitlines()
    tail = f" — {reason[0]}" if reason else ""

    return f"{head}{f' [{flags}]' if flags else ''}{spent}{tail}"


def _where_line(ctx: StepContext) -> str:
    sides = ctx.session_targets

    return " · ".join(
        f"{side}: {target['container']}/{target['database']}"
        for side, target in sorted(sides.items())
    ) or "no containers"


def _where_detail(ctx: StepContext) -> str:
    """The facts a run goes wrong over, written down once, in one place."""
    from odoo_migrator.module_paths import module_root

    rows = [f"module: {ctx.module.technical_name}"]
    for side in ("old", "new"):
        target = ctx.session_targets.get(side) or {}
        rows.append(
            f"{side}: container={target.get('container', '')} "
            f"database={target.get('database', '')} "
            f"odoo_bin={target.get('odoo_bin', '')} "
            f"source={module_root(ctx.migration, ctx.module, side)}"
        )

    return "\n".join(rows)


def _module_root(ctx: StepContext) -> Path | None:
    """Where this module lives inside the target project."""

    return module_root(ctx.migration, ctx.module, "new")
