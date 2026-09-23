"""Run one job on a background thread. The database is never shared with it."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime

from odoo_migrator.db import database
from odoo_migrator.events import EventHub
from odoo_migrator.jobs import apply_progress, apply_result
from odoo_migrator.models import CheckpointMessage, Job, Migration, Module, StepRun, ToolCall
from odoo_migrator.paths import repo_dir
from odoo_migrator.runner.cartridge import load_cartridge, load_cartridge_file
from odoo_migrator.runner.context import StepContext
from odoo_migrator.runner.loop import run_job
from odoo_migrator.runner.registry import resolve

# job_id -> event the human's answer releases. The thread that parked is
# the only one that waits; answer_job sets the event from the request thread.
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
                self._progress(
                    {"job_id": job_id, "stage": _stage(label), "attempt": current.attempt}
                )
                result = runner(step, current)
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

        from odoo_migrator.runner import loop as loop_mod

        original = loop_mod.resolve
        loop_mod.resolve = resolve_step
        try:
            try:
                outcome = run_job(
                    cartridge,
                    ctx,
                    retry_limit=ctx.migration.retry_limit or 3,
                )
            except Exception as exc:  # noqa: BLE001 - wait() re-raises this on the caller thread.
                self._errors[job_id] = exc
                self.hub.publish("job_finished", {"job_id": job_id, "status": "failed"})

                return
        finally:
            loop_mod.resolve = original
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

    def _cartridge(self, payload: dict):
        text = payload.get("cartridge_text")
        if text:

            return load_cartridge(text)

        return load_cartridge_file(repo_dir() / "cartridges" / "stock.yaml")

    def _context(self, payload: dict, cartridge) -> StepContext:
        with database.connection_context():
            job = Job.get(Job.job_id == payload["job_id"])
            module = Module.get_by_id(job.module_id)
            migration = Migration.get_by_id(module.migration_id)
        sessions, keys = self._sessions(migration)

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
            event = threading.Event()
            with _GUARD:
                _WAITERS[payload["job_id"]] = event
            with database.connection_context():
                parked = Job.get(Job.job_id == payload["job_id"])
                CheckpointMessage.create(job=parked, author="agent", text=question)
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

                return _ANSWERS.pop(payload["job_id"], "")

        ctx = StepContext(
            job_id=payload["job_id"],
            attempt=1,
            migration=migration,
            module=module,
            cartridge=cartridge,
            sheller=self.sheller,
            sessions=sessions,
            write_keys=keys,
            emit=lambda event: self.hub.publish(event.get("kind", "event"), event),
            client=self.client,
            record_tool=record,
            wait_for_human=wait_for_human,
        )

        return ctx

    def _sessions(self, migration) -> tuple[dict, dict]:
        owner = {"kind": "agent", "migration_id": migration.id}
        old = self.sheller.open_session(
            container=migration.old_container or "odoo-old",
            database="acme",
            odoo_bin="/opt/odoo/odoo-bin",
            owner=owner,
            allow_commit=False,
        )
        new = self.sheller.open_session(
            container=migration.new_container or "odoo-new",
            database="acme",
            odoo_bin="/opt/odoo/odoo-bin",
            owner=owner,
            allow_commit=True,
        )

        return (
            {"old": old["id"], "new": new["id"]},
            {"old": old["write_key"], "new": new["write_key"]},
        )

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


def _stage(label: str) -> str:
    from odoo_migrator.contract import STAGES

    if label in STAGES:

        return label

    return "understand"
