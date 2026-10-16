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
from odoo_migrator.jobs import abandon_job, apply_progress, apply_result, lamp_state
from odoo_migrator.models import CheckpointMessage, Job, Migration, Module, StepRun, ToolCall
from odoo_migrator.module_paths import module_root
from odoo_migrator.runner.cartridge import load_cartridge
from odoo_migrator.runner.context import StepContext, StepResult, Stopped, check_stop
from odoo_migrator.runner.loop import run_job
from odoo_migrator.runner.registry import resolve
from odoo_migrator.runner.sessions import (
    close_side,
    ensure_usable,
    open_side,
    open_sides_patiently,
)
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

# Jobs a person asked to stop. The loop reads this between steps: the one in
# flight finishes, because an agent turn cannot be taken back and the step
# row would be left open on a thread that is still writing to it.
_STOPPING: set[str] = set()

# How often a job waiting for a database another job holds looks up to see
# whether it was stopped meanwhile.
_TARGET_POLL = 0.25

# Finished jobs whose thread and error `wait` can still be asked about.
_KEPT_FINISHED = 16


def ask_to_stop(job_id: str) -> None:
    """Flag the run, and wake it if it is parked on a person.

    A parked thread waits for an answer with no end of its own. Without the
    wake the flag sat unread until someone answered the question, which is
    the opposite of what pressing Stop on a parked job asks for.
    """
    with _GUARD:
        _STOPPING.add(job_id)
        parked = _WAITERS.get(job_id)
    if parked is not None:
        parked.set()


def stopping(job_id: str) -> bool:
    with _GUARD:

        return job_id in _STOPPING


def _forget_stop(job_id: str) -> None:
    with _GUARD:
        _STOPPING.discard(job_id)


def _target_lock(ctx) -> threading.Lock | None:
    target = ctx.session_targets.get("new")
    if not target:

        return None

    return _target_lock_for(target["container"], target["database"])


def _target_lock_for(container: str, database_name: str) -> threading.Lock:
    with _GUARD:

        return _TARGETS.setdefault((container, database_name), threading.Lock())


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

    def __init__(
        self,
        *,
        sheller,
        hub: EventHub,
        progress=None,
        result=None,
        provider=None,
        sheller_for_job=None,
    ):
        # The daemon a job talks to. A test hands one in. The live app hands
        # `sheller_for_job`, asked as each job starts: a client built once at
        # startup kept the old URL after Settings saved a new one, and the
        # screens and the run talked to two daemons. A job keeps the one it
        # started with, because its sessions live there.
        self.sheller = sheller
        self._sheller_for_job = sheller_for_job
        self.hub = hub
        self._progress = progress or (lambda body: apply_progress(body, hub))
        self._result = result or (lambda body: apply_result(body, hub))
        # Only a test passes one. A live run asks the registry for the
        # provider the cartridge names, step by step.
        self.provider = provider
        self._threads: dict[str, threading.Thread] = {}
        self._errors: dict[str, BaseException] = {}
        # `start` is called from requests and from the thread of the job
        # before, when its result starts the next module.
        self._table = threading.Lock()

    def start(self, payload: dict) -> None:
        job_id = payload["job_id"]
        thread = threading.Thread(
            target=self._run,
            args=(payload,),
            name=f"migrate-{job_id}",
            daemon=True,
        )
        with self._table:
            self._forget_finished()
            self._threads[job_id] = thread
        thread.start()

    def job_sheller(self):
        """The Sheller client for a job starting now."""
        if self._sheller_for_job is not None:

            return self._sheller_for_job()

        return self.sheller

    def stop(self, job_id: str) -> bool:
        """Ask a running job to end at its next step. True when it is ours.

        The step in flight finishes: an agent turn cannot be taken back, and
        cutting the thread would leave a step row open and a session holding
        a command nobody is waiting for. A thread that has ended is not ours
        to stop, whatever its row says.
        """
        thread = self._threads.get(job_id)
        if thread is None or not thread.is_alive():

            return False
        ask_to_stop(job_id)

        return True

    def stopping(self, job_id: str) -> bool:
        """A Stop was asked for and the run has not reached it yet."""
        thread = self._threads.get(job_id)

        return thread is not None and thread.is_alive() and stopping(job_id)

    def resume(self, job_id: str, answer: str) -> bool:
        """True when this process still has the thread that parked the job."""

        return resume_waiter(job_id, answer)

    def _forget_finished(self) -> None:
        """Drop all but the last few finished jobs. The caller holds the table.

        Every thread and error stayed for the life of the process, and an
        error keeps the frames of the step that raised it: the step context,
        with the module's whole source in it.
        """
        finished = [job_id for job_id, thread in self._threads.items() if not thread.is_alive()]
        for job_id in finished[:-_KEPT_FINISHED]:
            self._threads.pop(job_id, None)
            self._errors.pop(job_id, None)

    def wait(self, job_id: str, timeout: float = 10) -> None:
        thread = self._threads[job_id]
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError(job_id)
        error = self._errors.get(job_id)
        if error is not None:
            raise error

    def _log(
        self,
        job_id: str,
        source: str,
        text: str,
        detail: str = "",
        level: str = "info",
        tool_call_id: int | None = None,
    ) -> None:

        runlog.write(
            job_id,
            source=source,
            text=text,
            detail=detail,
            level=level,
            hub=self.hub,
            tool_call_id=tool_call_id,
        )

    def _run(self, payload: dict) -> None:
        job_id = payload["job_id"]
        try:
            self._run_job(payload)
        finally:
            # Forgotten when the run is over, not when it starts: emptied at
            # the start, a Stop pressed before the thread got going was lost,
            # and nothing emptied it after.
            _forget_stop(job_id)

    def _run_job(self, payload: dict) -> None:
        job_id = payload["job_id"]
        ctx = None
        seen_attempt = 0

        def resolve_step(kind: str):
            runner = resolve(kind)

            def wrapped(step, current):
                nonlocal seen_attempt
                # Before the step is opened, so nothing is left running in
                # the trace and no session is asked for more work.
                check_stop(current, f"before {step.id or step.kind}")
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
            if stopping(job_id):
                raise Stopped("before it started")
            self._log(job_id, "runner", "job started")
            cartridge = self._cartridge(payload)
            ctx = self._context(payload, cartridge, self.job_sheller())
            self._log(
                job_id,
                "runner",
                f"cartridge {cartridge.cartridge} {cartridge.version} "
                f"({cartridge.sha256[:12]}), model {_model_line(cartridge, ctx)}",
            )
            self._log(job_id, "runner", _where_line(ctx), detail=_where_detail(ctx))
            # Opened here rather than inside `_context`, so `ctx` is already
            # bound when it happens. Opening the old side and then failing on
            # the new one used to leave `ctx` unassigned, and the `finally`
            # below closed nothing: a live odoo-bin stayed on the host.
            ctx.baseline_files = snapshot(_module_root(ctx))
            self._await_commit_switch(ctx)
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
        except Stopped as where:
            # Not a failure of the module: nothing was found wrong with it.
            # The step that had started finished first, so whatever it wrote
            # is on disk and in the trace, and the module is where that step
            # left it.
            self._log(
                job_id,
                "runner",
                f"stopped by a person, {where}",
                level="warn",
            )
            self._stopped(
                job_id, str(where), attempt=ctx.attempt if ctx is not None else 1
            )

            return
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
        recorded = outcome == "waiting_for_human" or self._report(
            job_id,
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
                "assumptions": list(ctx.assumptions),
            },
        )
        self._log(
            job_id,
            "runner",
            f"job {outcome}" if recorded else f"job {outcome}, but unknown to the app",
            detail=ctx.last_failure,
            level="error" if outcome == "failed" or not recorded else "info",
        )
        self.hub.publish(
            "job_finished", {"job_id": job_id, "status": outcome if recorded else "unknown"}
        )

    def _report(self, job_id: str, body: dict) -> bool:
        """Hand the outcome to the app. False when that failed.

        The result path does more than write a row: it advances the batch and
        can start the next module, in this thread. An exception there ended
        the thread with nothing said — no closing line, no `job_finished` —
        and the batch stayed `running`, refusing every later Run. Now the job
        is left the way a restart leaves one, `unknown`, and the run log says
        why.
        """
        try:
            self._result(body)
        except Exception as exc:  # noqa: BLE001 - nothing else in the process can say it.
            self._errors[job_id] = exc
            self._log(
                job_id,
                "runner",
                f"the result could not be recorded: {type(exc).__name__}: {exc}",
                detail="".join(traceback.format_exception(exc)),
                level="error",
            )
            with contextlib.suppress(Exception):
                abandon_job(job_id, self.hub)

            return False

        return True

    def _stopped(self, job_id: str, where: str, attempt: int = 1) -> None:
        """Tell the app a person ended this run, and say where it got to.

        Through the result, the way a failure is reported. Writing the status
        straight onto the row left the batch `running` for ever — every later
        Run was refused as "already running" — and the diff the run had
        written reached no attempt. ``where`` is the `Stopped` message:
        "before lint", "during tests".
        """
        self._report(
            job_id,
            {
                "job_id": job_id,
                "status": "stopped",
                "attempts": [{"iteration": attempt, "verdict": f"stopped {where}"}],
            },
        )
        self.hub.publish(
            "job_finished", {"job_id": job_id, "status": "stopped", "where": where}
        )
        with contextlib.suppress(Exception):
            self.hub.publish("lamp", {"state": lamp_state()})

    def _fail(self, job_id: str, exc: BaseException, attempt: int = 1) -> None:
        """Tell the app the job failed, then say so on the stream.

        Publishing the event without the result left the row running while
        the screen said it had stopped.
        """
        self._report(
            job_id,
            {
                "job_id": job_id,
                "status": "failed",
                "attempts": [
                    {"iteration": attempt, "verdict": f"{type(exc).__name__}: {exc}"}
                ],
            },
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

    def _await_commit_switch(self, ctx: StepContext) -> None:
        """Park until a person has opened the target container to agents' commits.

        Sheller 1.8.3 refuses an agent's session that opens with the right to
        commit, and the runner commits on the target after every upgrade. A
        job started anyway was refused at its first open and failed with
        `not_owner`, which told the person about a key. Sheller 1.11.0 lets a
        person switch the container on (*agents may commit*) and lists the
        switched-on containers without a key, so this asks before opening
        anything, and says what to do.

        The answer reopens the sessions (`wait_for_human`); a switch still off
        refuses the target there, and the job parks again with the daemon's
        own words.
        """
        if "new" not in ctx.session_targets:

            return
        container = ctx.session_targets["new"]["container"]
        while True:
            question = _commit_switch_question(ctx.sheller, container)
            if question is None:

                return
            self._log(ctx.job_id, "runner", question, level="warn")
            ctx.wait_for_human(question)

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
        new project root, holding a manifest, is cleared, and what is hidden
        in it stays.
        """
        from odoo_migrator.module_paths import allowed_roots

        root = _module_root(ctx)
        # The new side's addons directories when its mounts are known, and the
        # new project otherwise. Strictly inside one of them, never the root.
        roots = [
            Path(mount["host"]).expanduser().resolve()
            for mount in allowed_roots(ctx.migration, "new")
        ]
        if not roots and str(ctx.migration.new_project_path or "").strip():
            roots = [Path(ctx.migration.new_project_path).expanduser().resolve()]
        if root is None or not root.is_dir() or not roots:

            return
        resolved = root.resolve()
        if not any(resolved != one and resolved.is_relative_to(one) for one in roots):
            self._log(
                ctx.job_id,
                label,
                f"not clearing {root}: outside the new addons directories",
                level="warn",
            )

            return
        # A directory the old container mounts is the source it runs, even
        # when the new one mounts it too. Emptying it would delete the 17.0
        # code the port is read from.
        from odoo_migrator.mounts import load as load_mounts

        for mount in load_mounts(getattr(ctx.migration, "old_mounts", "[]")):
            old_host = Path(mount["host"]).expanduser().resolve()
            if resolved == old_host or resolved.is_relative_to(old_host) or old_host.is_relative_to(resolved):
                self._log(
                    ctx.job_id,
                    label,
                    f"not clearing {root}: the old container mounts {old_host}",
                    level="error",
                )

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
            # Hidden entries stay. Odoo loads none of them, so none is part
            # of the attempt being thrown away — and one can be the module's
            # own repository (`.git`, a directory or a submodule's file),
            # the undo for everything a run writes here.
            if child.name.startswith("."):
                continue
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
            where = f"{target['container']}/{target['database']}"
            self._log(
                ctx.job_id,
                "runner",
                f"waiting for {where}: another job is migrating on it",
                level="warn",
            )
            # In short waits, not one: the other job can take an hour, and a
            # Stop pressed meanwhile has to reach this one.
            while not lock.acquire(timeout=_TARGET_POLL):
                check_stop(ctx, f"while waiting for {where}")
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

    def _context(self, payload: dict, cartridge, sheller) -> StepContext:
        with database.connection_context():
            job = Job.get(Job.job_id == payload["job_id"])
            module = Module.get_by_id(job.module_id)
            migration = Migration.get_by_id(module.migration_id)
        targets = self._targets(migration, sheller)

        def record(tool, side, session_id, command_id, duration_ms, ok, preview):
            # The skeleton row first: the log line points at it, and that
            # pointer is what turns a 120-character preview into the whole
            # answer the daemon's journal kept.
            with database.connection_context():
                call = ToolCall.create(
                    step_run=ctx.step_run_id,
                    tool=tool,
                    side=side,
                    session_id=session_id or "",
                    command_id=command_id or "",
                    duration_ms=duration_ms,
                    ok=ok,
                    preview=preview or "",
                )
            self._log(
                payload["job_id"],
                ctx.step_run_label or "runner",
                f"{tool}({side}) {duration_ms}ms {'ok' if ok else 'failed'}: {preview}",
                level="info" if ok else "warn",
                # Only a command the daemon ran has a body to open. `read_file`
                # and `write_file` are the host's, `list_tests` and `journal`
                # are routes and not commands: offering a body for those
                # opened the session's whole journal, which begins with the
                # metadata of a session and answers nothing that was asked.
                tool_call_id=call.id if command_id else None,
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

        def park(question: str) -> str:
            """Close the sessions, hand the question to a person, wait.

            Stop wakes the wait as an answer does; the stop wins.
            """
            event = threading.Event()
            with _GUARD:
                _WAITERS[payload["job_id"]] = event
                # A Stop that came before the waiter existed had no one to
                # wake. Under the same lock, so it is either seen here or
                # sees the waiter.
                if payload["job_id"] in _STOPPING:
                    event.set()
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
            check_stop(ctx, "while waiting for an answer")
            self._hold_target(ctx)

            return answer

        def wait_for_human(question: str) -> str:
            """Park, and stop holding two Odoo processes while a person thinks.

            A checkpoint can sit for a day. The sessions close here and open
            again on the answer — the deliberate exception to one session for
            the whole run, which holds for a job that is running, not one that
            is waiting on a person.

            The answer is often "I restarted the container", sent while it is
            still coming back. Opening the sessions is retried for a while;
            if they still do not open, the job parks again and says why,
            rather than failing a run a person was in the middle of fixing.
            Every answer given along the way reaches the next step.
            """
            answers = []
            while True:
                answers.append(park(question))
                failure = open_sides_patiently(ctx)
                if failure is None:

                    return "\n\n".join(answer for answer in answers if answer)
                self._log(
                    payload["job_id"],
                    "ask_human",
                    f"the sessions did not open again after the answer: {failure}",
                    level="warn",
                )
                question = (
                    "The sessions could not be opened again after your answer: "
                    f"{failure}. Check that both containers are up, then answer "
                    "again; what you wrote before is kept."
                )

        ctx = StepContext(
            job_id=payload["job_id"],
            attempt=1,
            migration=migration,
            module=module,
            cartridge=cartridge,
            sheller=sheller,
            sessions={},
            write_keys={},
            session_targets=targets,
            emit=lambda event: self.hub.publish(event.get("kind", "event"), event),
            provider=self.provider,
            record_tool=record,
            wait_for_human=wait_for_human,
            stop_asked=lambda: stopping(payload["job_id"]),
        )

        return ctx

    def _targets(self, migration, sheller=None) -> dict[str, dict]:
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
            probed = (sheller or self.sheller).probe(container)
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


def _commit_switch_question(sheller, container: str) -> str | None:
    """What a person has to do before the target can commit, or None."""
    try:
        switched = {entry["container"] for entry in sheller.agent_commit()}
    except ShellerError as exc:
        if exc.status != 404:
            raise

        return (
            f"This Sheller has no 'agents may commit' switch, and since 1.8.3 it "
            f"lets no agent commit on {container}. Update Sheller to 1.11.0 or "
            f"later, turn the switch on for {container}, then answer here."
        )
    if container in switched:

        return None

    return (
        f"The target container {container} is not open to agents' commits, and "
        f"the run commits there after every upgrade. In Sheller, turn on 'agents "
        f"may commit' on the {container} card, then answer here."
    )


def _model_line(cartridge, ctx) -> str:
    """What will actually run, and what the file asked for when they differ.

    This line is read to know what a run is about to spend. It printed the
    cartridge's own model, so a card overriding `anthropic/claude-opus-5`
    with Cursor still said Opus — the one thing a person watching this line
    is trying to avoid.
    """
    from odoo_migrator.runner.cartridge import Step
    from odoo_migrator.runner.prompting import resolve_model, resolve_provider

    step = Step(kind="agent", id="", prompt="")
    chosen = f"{resolve_provider(step, ctx)}/{resolve_model(step, ctx)}"
    named = f"{cartridge.model.provider}/{cartridge.model.name}"
    if chosen == named:

        return chosen

    return f"{chosen} (the card's; the cartridge names {named})"


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
