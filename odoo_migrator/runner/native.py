"""Run one job on a background thread. The database is never shared with it."""

from __future__ import annotations

import contextlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from odoo_migrator.db import database
from odoo_migrator.events import EventHub
from odoo_migrator.jobs import apply_progress, apply_result
from odoo_migrator.models import CheckpointMessage, Job, Migration, Module, StepRun, ToolCall
from odoo_migrator.paths import repo_dir
from odoo_migrator.runner.cartridge import load_cartridge, load_cartridge_file
from odoo_migrator.runner.context import StepContext
from odoo_migrator.runner.loop import run_job
from odoo_migrator.runner.registry import resolve
from odoo_migrator.runner.snapshot import diff as diff_snapshots
from odoo_migrator.runner.snapshot import snapshot

# job_id -> event the human's answer releases. The thread that parked is
# the only one that waits; answer_job sets the event from the request thread.
# What the migration card says when it says nothing. Same default as the
# column, so a blank card does not quietly run a different number of laps.
_RETRY_LIMIT = 5

_WAITERS: dict[str, threading.Event] = {}
_ANSWERS: dict[str, str] = {}
_GUARD = threading.Lock()


def resume_waiter(job_id: str, answer: str) -> None:
    """Release a parked runner, if this process is the one that parked it."""
    with _GUARD:
        _ANSWERS[job_id] = answer
        event = _WAITERS.get(job_id)
    if event is not None:
        event.set()


class NativeRunner:
    """``start`` returns at once. The job runs on its own thread."""

    def __init__(self, *, sheller, hub: EventHub, progress=None, result=None, client=None):
        self.sheller = sheller
        self.hub = hub
        self._progress = progress or (lambda body: apply_progress(body, hub))
        self._result = result or (lambda body: apply_result(body, hub))
        self.client = client
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

    def wait(self, job_id: str, timeout: float = 10) -> None:
        thread = self._threads[job_id]
        thread.join(timeout)
        if thread.is_alive():
            raise TimeoutError(job_id)
        error = self._errors.get(job_id)
        if error is not None:
            raise error

    def _run(self, payload: dict) -> None:
        job_id = payload["job_id"]
        cartridge = self._cartridge(payload)
        ctx = self._context(payload, cartridge)
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
                step_run = self._open_step(current, step)
                current.step_run_id = step_run
                label = step.id or step.kind
                self.hub.publish(
                    "step_started",
                    {"job_id": job_id, "step": label, "kind": step.kind, "attempt": current.attempt},
                )
                self._progress({"job_id": job_id, "stage": label, "attempt": current.attempt})
                writes = "write_file" in (step.tools or ())
                before = snapshot(_module_root(current)) if writes else {}
                result = runner(step, current)
                if writes:
                    self._note_diff(current, before)
                self._close_step(step_run, result)
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
            outcome = run_job(
                cartridge,
                ctx,
                retry_limit=ctx.migration.retry_limit or _RETRY_LIMIT,
                resolve=resolve_step,
            )
        except Exception as exc:  # noqa: BLE001 - wait() re-raises this on the caller thread.
            self._errors[job_id] = exc
            self.hub.publish("job_finished", {"job_id": job_id, "status": "failed"})

            return
        finally:
            # A job that ends, fails or is abandoned leaves no odoo-bin behind.
            # A parked one has already closed its own; closing twice is a 404
            # the daemon answers and this swallows.
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
                }
            )
        self.hub.publish("job_finished", {"job_id": job_id, "status": outcome})

    def _note_diff(self, ctx: StepContext, before: dict) -> None:
        """Record what the step wrote, for the next prompt and the checkpoint."""
        hunk = diff_snapshots(before, snapshot(_module_root(ctx)))
        if not hunk:

            return
        ctx.previous_diff = hunk
        with database.connection_context():
            job = Job.get(Job.job_id == ctx.job_id)
            blob = json.loads(job.file_diffs or "{}")
            key = str(ctx.attempt)
            blob[key] = (blob.get(key) or "") + hunk
            job.file_diffs = json.dumps(blob)
            job.save()

    def _close_sessions(self, ctx: StepContext) -> None:
        for side, session_id in list(ctx.sessions.items()):
            if not session_id:
                continue
            with contextlib.suppress(Exception):
                # A session already gone is not a failure: the daemon answers
                # 404 for one whose process died, and closing twice is normal
                # on the parked path.
                self.sheller.close(session_id, write_key=ctx.write_keys.get(side, ""))
            ctx.sessions[side] = ""

    def _open_sessions(self, ctx: StepContext) -> None:
        for side, target in ctx.session_targets.items():
            if ctx.sessions.get(side):
                continue
            opened = self.sheller.open_session(
                container=target["container"],
                database=target["database"],
                odoo_bin=target["odoo_bin"],
                owner=target["owner"],
                allow_commit=side == "new",
            )
            ctx.sessions[side] = opened["id"]
            ctx.write_keys[side] = opened["write_key"]

    def _cartridge(self, payload: dict):
        text = payload.get("cartridge_text")
        if text:

            return load_cartridge(text)

        return load_cartridge_file(repo_dir() / "cartridges" / "stock.yaml")

    def _model_client(self, cartridge):
        """Resolve the client, but only for a cartridge that has agent steps.

        ``client`` may be a factory: building it reads the key from settings
        and refuses when there is none, and a cartridge of deterministic steps
        should not be stopped by a missing key it never needed.
        """
        lanes = list(cartridge.pipeline) + list(cartridge.on_failure.pipeline)
        if not any(step.kind == "agent" for step in lanes):

            return None
        if callable(self.client):

            return self.client()

        return self.client

    def _context(self, payload: dict, cartridge) -> StepContext:
        with database.connection_context():
            job = Job.get(Job.job_id == payload["job_id"])
            module = Module.get_by_id(job.module_id)
            migration = Migration.get_by_id(module.migration_id)
        targets = self._targets(migration)

        def record(tool, side, session_id, command_id, duration_ms, ok, preview):
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
            client=self._model_client(cartridge),
            record_tool=record,
            wait_for_human=wait_for_human,
        )
        self._open_sessions(ctx)

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
        for side, container in (
            ("old", migration.old_container),
            ("new", migration.new_container),
        ):
            if not container:
                raise RuntimeError(f"the {side} container of this migration is not up")
            probed = self.sheller.probe(container)
            targets[side] = {
                "container": container,
                "database": probed.get("db_name") or "",
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
            row.save()


def _module_root(ctx: StepContext) -> Path | None:
    """Where this module lives inside the target project."""
    if ctx.module.new_path:

        return Path(ctx.module.new_path)
    project = ctx.migration.new_project_path
    if not project:

        return None

    return Path(project) / ctx.module.technical_name
