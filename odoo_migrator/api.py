"""HTTP for the migrator UI. Factory, same shape as odoo-sheller."""

import asyncio
import json
import os
import queue
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import urlparse

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from odoo_migrator.cartridges import install_from_text, list_installs, smoke_run
from odoo_migrator.contract import ContractError
from odoo_migrator.db import database, init_db
from odoo_migrator.events import CLOSED, EventHub
from odoo_migrator.executor import StubExecutor
from odoo_migrator.graph import (
    GraphError,
    include_all,
    leave_out_all,
    leave_out_selection,
    list_modules,
    list_pipeline_steps,
    module_for_migration,
    resolve_graph,
    restore_selection,
    set_batch_group,
    skip_subtree,
)
from odoo_migrator.jobs import (
    advance_batch,
    answer_job,
    apply_progress,
    apply_result,
    attention_target,
    batch_is_checkpoint,
    checkpoint_thread,
    continue_batch,
    dismiss_job,
    lamp_state,
    latest_checkpoint,
    live_run,
    promote_finding,
    recent_job,
    reconcile_jobs,
    reopen_finished,
    reopen_module,
    running_migration_count,
    save_notes,
    start_batch,
    start_job,
)
from odoo_migrator.migrations import (
    archive_migration,
    create_migration,
    get_active,
    list_active,
    migration_results,
    save_migration_settings,
    save_side_container,
    suggest_accent,
)
from odoo_migrator.models import ACCENTS
from odoo_migrator.paths import rules_dir as default_rules_dir
from odoo_migrator.paths import static_dir, templates_dir
from odoo_migrator.pipeline_queue import add_all_steps as queue_add_all_steps
from odoo_migrator.pipeline_queue import add_step as queue_add_step
from odoo_migrator.pipeline_queue import clear as queue_clear
from odoo_migrator.pipeline_queue import queue_snapshot
from odoo_migrator.pipeline_queue import start as queue_start
from odoo_migrator.pipeline_queue import stop as queue_stop
from odoo_migrator.provision import (
    ComposeError,
    DockerComposeEngine,
    DockerUnavailable,
    count_running,
    describe_side,
    describe_side_without_listing,
)
from odoo_migrator.provision.actions import attach, rebuild, restart, stop, up
from odoo_migrator.rules import RulesError, stack_versions
from odoo_migrator.runner.cartridge import CartridgeError
from odoo_migrator.settings import get_app_settings, save_app_settings
from odoo_migrator.sheller import HttpShellerClient

STATIC = static_dir()
TEMPLATES = Jinja2Templates(directory=templates_dir())
NO_STORE = {"Cache-Control": "no-store"}
OPEN_COOKIE = "open_migration"
SSE_RETRY_MS = 3000
SSE_HEARTBEAT_SECONDS = 15
SSE_MAX_SECONDS = 300
PROVISION_ACTIONS = {
    "up": up,
    "attach": attach,
    "restart": restart,
    "rebuild": rebuild,
    "stop": stop,
}


class NoCacheStaticFiles(StaticFiles):
    """The UI is a local tool; a cached stylesheet lies about the current theme."""

    def file_response(self, full_path, stat_result, scope, status_code=200):
        response = FileResponse(
            full_path, status_code=status_code, stat_result=stat_result, headers=NO_STORE
        )

        return response


def _providers() -> list[dict]:
    """Selectable providers and the models each one lists. The scripted
    double has none, so it does not appear on the card."""
    from odoo_migrator.runner.providers import known_models

    rows = []
    for name, models in sorted(known_models().items()):
        if not models:
            continue
        rows.append({"name": name, "models": sorted(models)})

    return rows


def app_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:

        return version("odoo-migrator")
    except PackageNotFoundError:

        return "unknown"


def _choose_executor(*, sheller, hub, progress, result):
    """Stub by default. ``ODOO_MIGRATOR_RUNNER=native`` runs the in-process loop."""
    if os.environ.get("ODOO_MIGRATOR_RUNNER") == "native":
        from odoo_migrator.runner.native import NativeRunner

        # The pages build a client per request. The runner is chosen once,
        # at startup, and used to be handed None — the first probe then
        # died with AttributeError before a session existed.
        if sheller is None:
            sheller = HttpShellerClient(get_app_settings()["sheller_base_url"])

        return NativeRunner(
            sheller=sheller,
            hub=hub,
            progress=progress,
            result=result,
        )

    return StubExecutor(progress=progress, result=result)


def create_app(
    db_path: Path | str | None = None,
    *,
    compose=None,
    sheller=None,
    executor=None,
    hub=None,
    rules_dir=None,
) -> FastAPI:
    init_db(db_path)
    @asynccontextmanager
    async def lifespan(running: FastAPI):
        yield
        # Shutdown. Every open SSE stream is parked on a blocking read and
        # would otherwise hold the process up for its whole window: SIGTERM
        # freed the port and the process stayed alive, which reads as a
        # hang and ends in SIGKILL.
        running.state.hub.close()

    app = FastAPI(
        title="odoo-migrator",
        version=app_version(),
        docs_url=None,
        lifespan=lifespan,
    )
    app.state.compose = compose if compose is not None else DockerComposeEngine()
    app.state.sheller = sheller
    app.state.hub = hub if hub is not None else EventHub()

    def _on_progress(body):
        apply_progress(body, app.state.hub)

    def _on_result(body):
        finished = apply_result(body, app.state.hub)
        if finished is not None:
            # The batch moves on its results, not on a loop in the request
            # that started it. app.state.executor is read now, not captured,
            # so a test that swaps the executor mid-batch is honoured.
            advance_batch(finished, app.state.executor, app.state.hub)
            from odoo_migrator.db import database
            from odoo_migrator.models import Job
            from odoo_migrator.pipeline_queue import on_module_finished

            with database.connection_context():
                job = Job.get_or_none(Job.job_id == finished)
                if job is not None:
                    on_module_finished(str(job.batch.migration_id), app.state.hub)

    # The callback path, exposed so an executor double drives the app the
    # way a real one does — through here — instead of reaching past it into
    # the job layer, where it would miss everything a callback triggers.
    app.state.on_progress = _on_progress
    app.state.on_result = _on_result
    if executor is None:
        executor = _choose_executor(
            sheller=app.state.sheller,
            hub=app.state.hub,
            progress=_on_progress,
            result=_on_result,
        )
    app.state.executor = executor
    app.state.rules_dir = Path(rules_dir) if rules_dir is not None else default_rules_dir()
    reconcile_jobs()

    def _chrome(request: Request) -> dict:
        current = get_active(request.cookies.get(OPEN_COOKIE))

        return {
            "version": app_version(),
            "accents": ACCENTS,
            "suggested_accent": suggest_accent(),
            "migrations": list_active(),
            "current": current,
            # Rendered, not hardcoded. The lamp and the counter only ever
            # moved on an SSE event, so opening a page — or reloading one —
            # while a module waited for an answer showed a calm nav bar.
            "running_count": running_migration_count(),
            "lamp": lamp_state(),
            "providers": _providers(),
        }

    def _page(request: Request, template: str, active: str, extra: dict | None = None):
        context = {"active": active, **_chrome(request)}
        if extra:
            context.update(extra)

        return TEMPLATES.TemplateResponse(
            request,
            template,
            context,
            headers=NO_STORE,
        )

    def _sheller(request: Request):
        if request.app.state.sheller is not None:

            return request.app.state.sheller

        return HttpShellerClient(get_app_settings()["sheller_base_url"])

    def _tree_page(
        request: Request,
        error: str | None = None,
        error_settings: str | None = None,
    ):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        modules = list_modules(current["id"]) if current is not None else []
        focus = request.query_params.get("focus")
        selected = None
        if focus:
            selected = next(
                (row for row in modules if str(row["id"]) == focus),
                None,
            )
        if selected is None:
            selected = next(
                (row for row in modules if row.get("status") == "waiting_for_human"),
                None,
            )

        return _page(
            request,
            "tree.html",
            "tree",
            {
                "modules": modules,
                "error": error,
                # When the error is about the old container, the fix is on the
                # migration's card; the template turns this into a link there.
                "error_settings": error_settings,
                "selected": selected,
                "graph_updated": request.query_params.get("graph") == "updated",
            },
        )

    def _link_old_container(request: Request, migration: dict) -> str | None:
        """Record the old container the app will talk to, from the one already
        running for its project. Resolve never *starts* a container — that is
        Up on the card — so a stopped side is an error the human resolves
        there, not silent magic here. Returns an error message, or None."""
        old = Path(migration["old_project_path"])
        new = Path(migration["new_project_path"])
        try:
            side = describe_side(request.app.state.compose, old, sibling_path=new)
        except DockerUnavailable as exc:

            return f"The old container can't be linked; Docker is not reachable: {exc}"
        if side.state == "running" and side.container:
            save_side_container(migration["id"], "old", side.container)

            return None
        if side.state == "stopped":

            return (
                "The old container is not running yet. Press Up on its card, "
                "then Resolve."
            )

        return f"The old container can't be linked: {side.detail or side.state}"

    @app.get("/")
    def overview(request: Request, settings: str = "", create: str = ""):

        return _overview(
            request, expanded=settings or None, creating=create == "1"
        )

    @app.post("/migrations")
    def migrations_create(
        request: Request,
        name: str = Form(),
        accent: str = Form(),
        source_version: str = Form(),
        target_version: str = Form(),
        old_project_path: str = Form(),
        new_project_path: str = Form(),
    ):
        typed = {
            "name": name,
            "accent": accent,
            "source_version": source_version,
            "target_version": target_version,
            "old_project_path": old_project_path,
            "new_project_path": new_project_path,
        }
        # A refusal leaves the form open with what was typed still in it.
        # Re-rendering it empty is how someone loses two paths they pasted.
        refused_with = {"creating": True, "typed": typed}
        try:
            created = create_migration(**typed)
        except RulesError as refused:

            return _overview(request, error=str(refused), **refused_with)
        if created is None:

            return _overview(
                request,
                error="Pick an accent that is not a status colour.",
                **refused_with,
            )

        return RedirectResponse("/", status_code=303)

    @app.post("/migrations/{migration_id}/archive")
    def migrations_archive(request: Request, migration_id: str):
        archive_migration(migration_id)
        response = RedirectResponse("/", status_code=303)
        if request.cookies.get(OPEN_COOKIE) == migration_id:
            response.delete_cookie(OPEN_COOKIE)

        return response

    @app.post("/migrations/{migration_id}/open")
    def migrations_open(migration_id: str):
        current = get_active(migration_id)
        if current is None:

            return RedirectResponse("/", status_code=303)
        response = RedirectResponse("/tree", status_code=303)
        response.set_cookie(OPEN_COOKIE, current["id"], httponly=True, samesite="lax")

        return response

    @app.get("/tree")
    def tree(request: Request):

        return _tree_page(request)

    @app.post("/tree/resolve")
    def tree_resolve(request: Request):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/tree", status_code=303)
        # Link to the already-running old container instead of making the
        # human press Attach on another screen first. resolve_graph re-reads
        # the recorded container from the row.
        if not current["old_container"]:
            unlinked = _link_old_container(request, current)
            if unlinked is not None:

                return _tree_page(
                    request, error=unlinked, error_settings=current["id"]
                )
        try:
            resolve_graph(_sheller(request), current["id"])
        except GraphError as refused:

            return _tree_page(request, error=str(refused))

        return RedirectResponse("/tree?graph=updated", status_code=303)

    @app.post("/tree/modules/restore")
    def tree_restore(
        request: Request,
        module_ids: Annotated[list[int], Form()],
    ):
        """Include for one or many modules: ancestors first, then downstream."""
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/tree", status_code=303)
        wants_json = "application/json" in request.headers.get("accept", "")
        if not module_ids:
            if wants_json:

                return JSONResponse({"error": "no modules selected"}, status_code=400)

            return _tree_page(request, error="no modules selected")
        try:
            for module_id in module_ids:
                module_for_migration(current["id"], module_id)
            updated = restore_selection(module_ids)
        except GraphError as refused:
            if wants_json:

                return JSONResponse({"error": str(refused)}, status_code=400)

            return _tree_page(request, error=str(refused))
        if wants_json:

            return JSONResponse(
                {
                    "modules": [
                        {"id": row["id"], "struck_out": row["struck_out"]}
                        for row in updated
                    ]
                }
            )

        return RedirectResponse("/tree", status_code=303)

    @app.post("/tree/modules/leave-out")
    def tree_leave_out(
        request: Request,
        module_ids: Annotated[list[int], Form()],
    ):
        """Exclude for one or many modules: upward depends chains only."""
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/tree", status_code=303)
        wants_json = "application/json" in request.headers.get("accept", "")
        if not module_ids:
            if wants_json:

                return JSONResponse({"error": "no modules selected"}, status_code=400)

            return _tree_page(request, error="no modules selected")
        try:
            for module_id in module_ids:
                module_for_migration(current["id"], module_id)
            updated = leave_out_selection(module_ids)
        except GraphError as refused:
            if wants_json:

                return JSONResponse({"error": str(refused)}, status_code=400)

            return _tree_page(request, error=str(refused))
        if wants_json:

            return JSONResponse(
                {
                    "modules": [
                        {"id": row["id"], "struck_out": row["struck_out"]}
                        for row in updated
                    ]
                }
            )

        return RedirectResponse("/tree", status_code=303)

    @app.post("/tree/modules/leave-out-all")
    def tree_leave_out_all(request: Request):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/tree", status_code=303)
        wants_json = "application/json" in request.headers.get("accept", "")
        try:
            updated = leave_out_all(current["id"])
        except GraphError as refused:
            if wants_json:

                return JSONResponse({"error": str(refused)}, status_code=400)

            return _tree_page(request, error=str(refused))
        if wants_json:

            return JSONResponse(
                {
                    "modules": [
                        {"id": row["id"], "struck_out": row["struck_out"]}
                        for row in updated
                    ]
                }
            )

        return RedirectResponse("/tree", status_code=303)

    @app.post("/tree/modules/include-all")
    def tree_include_all(request: Request):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/tree", status_code=303)
        wants_json = "application/json" in request.headers.get("accept", "")
        try:
            updated = include_all(current["id"])
        except GraphError as refused:
            if wants_json:

                return JSONResponse({"error": str(refused)}, status_code=400)

            return _tree_page(request, error=str(refused))
        if wants_json:

            return JSONResponse(
                {
                    "modules": [
                        {"id": row["id"], "struck_out": row["struck_out"]}
                        for row in updated
                    ]
                }
            )

        return RedirectResponse("/tree", status_code=303)

    @app.post("/tree/modules/{module_id}/strike")
    def tree_strike(request: Request, module_id: int, struck: str = Form("")):
        """``struck`` is explicit. Toggling per module made one press of the
        button do two opposite things to a mixed selection: the already
        struck came back while the rest went out. The caller decides once
        for the whole selection, so pressing again undoes the press."""
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/tree", status_code=303)
        wanted = None if not struck.strip() else struck.strip() == "1"
        wants_json = "application/json" in request.headers.get("accept", "")
        try:
            module_for_migration(current["id"], module_id)
            updated = skip_subtree(module_id, wanted)
        except GraphError as refused:
            if wants_json:

                return JSONResponse({"error": str(refused)}, status_code=400)

            return _tree_page(request, error=str(refused))
        if wants_json:

            return JSONResponse(
                {
                    "modules": [
                        {"id": row["id"], "struck_out": row["struck_out"]}
                        for row in updated
                    ]
                }
            )

        return RedirectResponse("/tree", status_code=303)

    @app.post("/tree/modules/{module_id}/batch")
    def tree_batch(request: Request, module_id: int, batch_group: str = Form("")):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/tree", status_code=303)
        try:
            module_for_migration(current["id"], module_id)
            value = int(batch_group) if batch_group.strip() else None
            set_batch_group(module_id, value)
        except (GraphError, ValueError) as refused:

            return _tree_page(request, error=str(refused))

        return RedirectResponse("/tree", status_code=303)

    @app.post("/tree/modules/{module_id}/notes")
    def tree_notes(request: Request, module_id: int, notes: str = Form("")):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/tree", status_code=303)
        try:
            module_for_migration(current["id"], module_id)
            save_notes(module_id, notes)
        except (GraphError, ContractError) as refused:

            return _tree_page(request, error=str(refused))

        return RedirectResponse("/tree", status_code=303)

    @app.post("/tree/modules/{module_id}/answer")
    def tree_answer(request: Request, module_id: int, answer: str = Form("")):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/tree", status_code=303)
        try:
            module_for_migration(current["id"], module_id)
            answer_job(
                module_id=module_id,
                answer=answer,
                hub=request.app.state.hub,
                resume=getattr(request.app.state.executor, "resume", None),
            )
        except (GraphError, ContractError) as refused:

            return _tree_page(request, error=str(refused))

        return RedirectResponse("/tree", status_code=303)

    @app.post("/tree/migrate")
    def tree_migrate(
        request: Request, module_id: str = Form(""), selected: str = Form("0")
    ):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/tree", status_code=303)
        if not module_id.strip():
            # The canvas sends how many were selected, so a marquee over five
            # modules is told what it did instead of quietly migrating one.
            many = selected.strip().isdigit() and int(selected) > 1

            return _tree_page(
                request,
                error=(
                    "Select one module. Migrating a whole batch starts on "
                    "Pipeline."
                    if many
                    else "Select a module to migrate."
                ),
            )
        try:
            job = start_job(
                migration_id=current["id"],
                module_id=int(module_id),
                executor=request.app.state.executor,
                hub=request.app.state.hub,
            )
        except (ContractError, ValueError) as refused:

            return _tree_page(request, error=str(refused))
        if batch_is_checkpoint(job.job_id):

            return RedirectResponse("/run", status_code=303)

        return RedirectResponse("/tree", status_code=303)

    @app.post("/tree/batch/run")
    def tree_batch_run(
        request: Request,
        batch_group: str = Form(""),
        step: str = Form(""),
    ):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/tree", status_code=303)
        if not step.strip() and not batch_group.strip():

            return _run_page(request, error="Select a step to migrate.")
        try:
            if step.strip():
                start_batch(
                    migration_id=current["id"],
                    step=int(step),
                    executor=request.app.state.executor,
                    hub=request.app.state.hub,
                )
            else:
                start_batch(
                    migration_id=current["id"],
                    batch_group=int(batch_group),
                    executor=request.app.state.executor,
                    hub=request.app.state.hub,
                )
        except (ContractError, ValueError) as refused:

            return _run_page(request, error=str(refused))

        return RedirectResponse("/run", status_code=303)

    @app.get("/attention")
    def attention():
        """The lamp is global. Clicking it opens the stalled migration."""
        target = attention_target()
        if target is None:

            return RedirectResponse("/", status_code=303)
        response = RedirectResponse(
            f"/tree?focus={target['module_id']}", status_code=303
        )
        response.set_cookie(
            OPEN_COOKIE, target["migration_id"], httponly=True, samesite="lax"
        )

        return response

    @app.get("/run")
    def run(request: Request):

        return _run_page(request, screen="pipeline")

    @app.get("/sheller")
    def sheller_page(request: Request):

        return _run_page(request, screen="sheller")

    def _result_page(request: Request, error: str | None = None):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        board = migration_results(current["id"]) if current else None
        thread = checkpoint_thread(current["id"]) if current else []

        return _page(
            request,
            "result.html",
            "result",
            {"board": board, "thread": thread, "error": error},
        )

    @app.get("/result")
    def result_page(request: Request):

        return _result_page(request)

    @app.post("/result/reopen")
    def result_reopen(request: Request):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/result", status_code=303)
        reopen_finished(current["id"], request.app.state.hub)

        return RedirectResponse("/result", status_code=303)

    @app.post("/result/modules/{module_id}/reopen")
    def result_reopen_one(request: Request, module_id: int):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/result", status_code=303)
        try:
            reopen_module(module_id, current["id"], request.app.state.hub)
        except ContractError as refused:

            return _result_page(request, error=str(refused))

        return RedirectResponse("/result", status_code=303)

    def _browsable(value: str, suffix: str = "") -> str:
        """A settings string is about to become an href and an iframe src.

        Only the operator can set it, so this is not an attack surface so
        much as a typo surface — but `javascript:` in an iframe src runs on
        our origin, and a scheme check costs nothing.
        """
        base = (value or "").strip().rstrip("/")
        if not base:

            return ""
        if urlparse(base).scheme not in ("http", "https"):

            return ""

        return base + suffix

    def _ribbon(current: dict) -> list[str]:
        from odoo_migrator.cartridges import pinned_for
        from odoo_migrator.models import Migration

        with database.connection_context():
            migration = Migration.get_or_none(Migration.id == current["id"])
        if migration is None:

            return []
        cartridge = pinned_for(migration)

        return [step.id or step.kind for step in cartridge.pipeline]

    def _run_page(request: Request, error: str | None = None, screen: str = "pipeline"):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        checkpoint = latest_checkpoint(current["id"]) if current else None
        live = live_run(current["id"]) if current else None
        ribbon = _ribbon(current) if current and live else []
        thread = checkpoint_thread(current["id"]) if current else []
        settings = get_app_settings()
        versions = []
        pipeline_steps = []
        if current:
            try:
                versions = stack_versions(
                    current["source_version"], current["target_version"]
                )
            except RulesError:
                versions = []
            pipeline_steps = list_pipeline_steps(current["id"])
        pipeline_queue = queue_snapshot(current["id"]) if current else None
        log_job = recent_job(current["id"]) if current else None

        return _page(
            request,
            "run.html",
            screen,
            {
                "screen": screen,
                "checkpoint": checkpoint,
                "live": live,
                "ribbon": ribbon,
                "thread": thread,
                "pipeline_steps": pipeline_steps,
                "pipeline_queue": pipeline_queue,
                "log_job": log_job,
                "sheller_web_url": _browsable(settings["sheller_base_url"], "/web"),
                "versions": versions,
                "error": error,
            },
        )

    @app.post("/run/queue/add")
    def run_queue_add(request: Request, step: str = Form("")):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        wants_json = "application/json" in request.headers.get("accept", "")
        if current is None:
            if wants_json:

                return JSONResponse({"error": "no open migration"}, status_code=400)

            return RedirectResponse("/run", status_code=303)
        if not step.strip():
            if wants_json:

                return JSONResponse({"error": "Pick a step to queue."}, status_code=400)

            return _run_page(request, error="Pick a step to queue.")
        try:
            chosen = int(step)
        except ValueError:
            if wants_json:

                return JSONResponse({"error": "Pick a step to queue."}, status_code=400)

            return _run_page(request, error="Pick a step to queue.")
        waves = {row["step"]: row for row in list_pipeline_steps(current["id"])}
        wave = waves.get(chosen)
        if wave is None:
            refused = "That step has no modules left in the plan."
        elif not wave["pending"]:
            refused = f"Step {chosen} is already passed."
        else:
            refused = ""
        if refused:
            if wants_json:

                return JSONResponse({"error": refused}, status_code=400)

            return _run_page(request, error=refused)
        try:
            queue_add_step(current["id"], chosen)
        except ValueError as refused:
            if wants_json:

                return JSONResponse({"error": str(refused)}, status_code=400)

            return _run_page(request, error=str(refused))
        from odoo_migrator.pipeline_queue import publish_queue

        publish_queue(request.app.state.hub, current["id"])
        if wants_json:

            return JSONResponse(
                {"migration_id": current["id"], **queue_snapshot(current["id"])}
            )

        return RedirectResponse("/run", status_code=303)

    @app.post("/run/queue/add-all")
    def run_queue_add_all(request: Request):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        wants_json = "application/json" in request.headers.get("accept", "")
        if current is None:
            if wants_json:

                return JSONResponse({"error": "no open migration"}, status_code=400)

            return RedirectResponse("/run", status_code=303)
        queue_add_all_steps(current["id"])
        from odoo_migrator.pipeline_queue import publish_queue

        publish_queue(request.app.state.hub, current["id"])
        if wants_json:

            return JSONResponse(
                {"migration_id": current["id"], **queue_snapshot(current["id"])}
            )

        return RedirectResponse("/run", status_code=303)

    @app.post("/run/queue/run")
    def run_queue_run(request: Request):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/run", status_code=303)
        try:
            queue_start(
                current["id"],
                request.app.state.executor,
                request.app.state.hub,
            )
        except ContractError as refused:

            return _run_page(request, error=str(refused))

        return RedirectResponse("/run", status_code=303)

    @app.post("/run/queue/stop")
    def run_queue_stop(request: Request):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/run", status_code=303)
        queue_stop(current["id"], request.app.state.hub)

        return RedirectResponse("/run", status_code=303)

    @app.post("/run/queue/clear")
    def run_queue_clear(request: Request):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/run", status_code=303)
        if not queue_clear(current["id"], request.app.state.hub):

            return _run_page(
                request,
                error="The queue is still working. Stop it and wait for the current module.",
            )

        return RedirectResponse("/run", status_code=303)

    @app.post("/run/continue")
    def run_continue(request: Request, override: str = Form("")):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/run", status_code=303)
        try:
            continue_batch(current["id"], override=override.strip() == "1")
        except ContractError as refused:

            return _run_page(request, error=str(refused))

        return RedirectResponse("/run", status_code=303)

    @app.post("/run/jobs/{job_pk}/dismiss")
    def run_dismiss(request: Request, job_pk: int):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/run", status_code=303)
        try:
            dismiss_job(job_pk, current["id"])
        except ContractError as refused:

            return _run_page(request, error=str(refused))

        return RedirectResponse("/run", status_code=303)

    @app.post("/run/findings/{finding_id}/promote")
    def run_promote(request: Request, finding_id: int, version: str = Form("")):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/run", status_code=303)
        try:
            promote_finding(
                finding_id,
                version,
                current["id"],
                rules_dir=request.app.state.rules_dir,
            )
        except ContractError as refused:

            return _run_page(request, error=str(refused))

        return RedirectResponse("/run", status_code=303)

    @app.get("/settings")
    def settings(request: Request):
        stored = get_app_settings()

        return _page(
            request,
            "settings.html",
            "settings",
            {"settings": stored},
        )

    @app.post("/settings")
    async def settings_save(request: Request):
        form = await request.form()
        values = {
            key.removeprefix("provider_").replace("_", "__", 1): str(form.get(key) or "")
            for key in form
            if key.startswith("provider_")
        }
        cleared = form.get("clear_provider")
        save_app_settings(
            sheller_base_url=str(form.get("sheller_base_url") or ""),
            sheller_image_tag=str(form.get("sheller_image_tag") or ""),
            provider_values=values,
            cleared={str(cleared)} if cleared else set(),
        )

        return RedirectResponse("/settings", status_code=303)

    def _known_project_paths() -> list[str]:
        """Every active migration's two project directories.

        The count of what is running is attributed against these, rather
        than guessed from container names — a host here really does run one
        called ``odoo-postgres``.
        """
        paths = []
        for migration in list_active():
            paths.append(migration["old_project_path"])
            paths.append(migration["new_project_path"])

        return paths

    def _migration_provision(request: Request, migration: dict) -> tuple[dict, str | None]:
        """Container state for one migration's two sides.

        This reaches Docker, so it is built for the row that is expanded and
        no other: a list of twenty migrations would otherwise probe forty
        compose projects to draw a page nobody asked to expand.
        """
        engine = request.app.state.compose
        old_path = Path(migration["old_project_path"])
        new_path = Path(migration["new_project_path"])
        try:

            return {
                "old": describe_side(engine, old_path, sibling_path=new_path),
                "new": describe_side(engine, new_path, sibling_path=old_path),
                "resources": count_running(engine, _known_project_paths()),
            }, None
        except DockerUnavailable as exc:
            detail = str(exc)

            return {
                "old": describe_side_without_listing(old_path, detail=detail),
                "new": describe_side_without_listing(new_path, detail=detail),
                "resources": None,
            }, detail

    def _flow_default(migration: dict | None) -> dict:
        """The cartridge's model, shown until the card saves its own pair."""
        if migration is None:

            return {"provider": "", "model": ""}
        from odoo_migrator.cartridges import pinned_for
        from odoo_migrator.runner.cartridge import CartridgeError

        try:
            named = pinned_for(migration).model
        except CartridgeError:
            named = None
        if named is None:

            return {"provider": "", "model": ""}

        return {"provider": named.provider, "model": named.name}

    def _overview(
        request: Request,
        *,
        expanded: str | None = None,
        creating: bool = False,
        typed: dict | None = None,
        error: str | None = None,
    ):
        """Overview, with one migration's settings open inside its own row.

        Which migration is *open* and which row is *expanded* are different
        questions. Settings belong to the migration they are for, so they
        are reached from its row and act on its id, not on whatever the
        switcher happens to be showing.
        """
        migration = get_active(expanded) if expanded else None
        provision = None
        if migration is not None:
            provision, unreachable = _migration_provision(request, migration)
            if error is None:
                error = unreachable

        return _page(
            request,
            "overview.html",
            "overview",
            {
                "expanded": migration,
                "flow_default": _flow_default(migration),
                "provision": provision,
                "cartridges": list_installs() if migration else [],
                # The form is asked for, not permanently open. It stays open
                # with what was typed still in it when the answer is a
                # refusal — re-rendering an empty form is how someone loses
                # two paths they pasted.
                "creating": creating,
                "typed": typed or {},
                "error": error,
            },
        )

    @app.post("/migrations/{migration_id}/settings")
    def migration_settings_save(
        request: Request,
        migration_id: str,
        strictness_default: str = Form(),
        retry_limit: str = Form(),
        ask_human_mode: str = Form(),
        cartridge_id: str | None = Form(None),
        cartridge_version: str | None = Form(None),
        model_webhook_path: str | None = Form(None),
        old_database: str | None = Form(None),
        new_database: str | None = Form(None),
        provider_override: str | None = Form(None),
        model_override: str | None = Form(None),
    ):
        """Every rejection says so. Silently discarding what someone typed
        into a settings form and re-rendering the old values is the worst
        failure this screen can have: it looks exactly like success."""
        if get_active(migration_id) is None:

            return _overview(request, error="No such migration.")
        try:
            limit = int(retry_limit)
        except ValueError:

            return _overview(
                request,
                expanded=migration_id,
                error="Retry limit must be a whole number.",
            )
        if cartridge_id and "@" in cartridge_id:
            # The picker carries id and version together. It used to post the
            # id alone beside a hidden field holding whatever version was
            # already pinned, so choosing a different cartridge — or a second
            # version of the same one — kept the old number.
            cartridge_id, _, cartridge_version = cartridge_id.partition("@")
        if save_migration_settings(
            migration_id,
            strictness_default=strictness_default,
            retry_limit=limit,
            ask_human_mode=ask_human_mode,
            cartridge_id=cartridge_id,
            cartridge_version=cartridge_version,
            model_webhook_path=model_webhook_path,
            old_database=old_database,
            new_database=new_database,
            provider_override=provider_override,
            model_override=model_override,
        ) is None:

            return _overview(
                request,
                expanded=migration_id,
                error="Those settings were rejected. Nothing saved.",
            )

        return RedirectResponse(f"/?settings={migration_id}", status_code=303)

    @app.post("/migrations/{migration_id}/cartridge")
    async def migration_cartridge(
        request: Request,
        migration_id: str,
        file: Annotated[UploadFile, File()],
    ):
        if get_active(migration_id) is None:

            return _overview(request, error="No such migration.")
        raw = await file.read()
        try:
            text = raw.decode("utf-8")
            install_from_text(text)
        except (CartridgeError, UnicodeError) as exc:

            return _overview(request, expanded=migration_id, error=str(exc))
        with database.connection_context():
            from odoo_migrator.models import CartridgeInstall as Install
            from odoo_migrator.models import Migration

            row = Install.select().order_by(Install.id.desc()).first()
            migration = Migration.get_or_none(Migration.id == migration_id)
            if row is not None and migration is not None:
                migration.cartridge_id = row.cartridge_id
                migration.cartridge_version = row.version
                migration.save()

        return RedirectResponse(f"/?settings={migration_id}", status_code=303)

    @app.post("/migrations/{migration_id}/smoke")
    def migration_smoke(request: Request, migration_id: str):
        if get_active(migration_id) is None:

            return _overview(request, error="No such migration.")
        try:
            status = smoke_run(
                migration_id, request.app.state.executor, request.app.state.hub
            )
        except ContractError as exc:

            return _overview(request, expanded=migration_id, error=str(exc))
        if status not in ("passed", "failed"):
            # A real cartridge answers minutes later. Calling that a failure
            # was how every batch ended up blocked for ever.

            return _overview(
                request,
                expanded=migration_id,
                error=(
                    "Smoke-run started. A batch will not start until its "
                    "result arrives and passes."
                ),
            )
        if status != "passed":

            return _overview(
                request,
                expanded=migration_id,
                error="Smoke-run failed. A batch will not start until it passes.",
            )

        return RedirectResponse(f"/?settings={migration_id}", status_code=303)

    @app.post("/migrations/{migration_id}/containers/{side}/{action}")
    def provision_container(
        request: Request, migration_id: str, side: str, action: str
    ):
        migration = get_active(migration_id)
        if migration is None:

            return _overview(request, error="No such migration.")
        handler = PROVISION_ACTIONS.get(action)
        if side not in ("old", "new") or handler is None:

            return _overview(
                request,
                expanded=migration_id,
                error="That container action was rejected. Nothing changed.",
            )
        path = Path(
            migration["old_project_path"]
            if side == "old"
            else migration["new_project_path"]
        )
        sibling = Path(
            migration["new_project_path"]
            if side == "old"
            else migration["old_project_path"]
        )
        try:
            result = handler(request.app.state.compose, path, sibling_path=sibling)
        except ComposeError as exc:

            return _overview(request, expanded=migration_id, error=str(exc))
        save_side_container(migration_id, side, result.container)

        return RedirectResponse(f"/?settings={migration_id}", status_code=303)

    @app.get("/sse")
    async def sse(request: Request):
        """One stream per page, held open.

        ``async def`` on purpose, and the one endpoint that must be: a sync
        handler is run in the threadpool, so every open page would hold a
        thread for as long as it is open. Nothing here touches Peewee, so the
        rule about connections and threads does not apply.

        The stream stays open and sends a comment heartbeat rather than
        closing between events. A closed stream plus a long ``retry`` would
        leave the browser waiting that long before reconnecting, which looks
        exactly like "SSE worked once and then stopped".

        It is held open for a bounded window rather than for ever. EventSource
        reconnects on its own, so an endless generator buys nothing and costs
        a task that only ends when it is cancelled — which makes shutdown hang
        on whatever sleep it was in.
        """

        async def events():
            subscribed = request.app.state.hub.subscribe()
            try:
                yield f"retry: {SSE_RETRY_MS}\n\n"
                # Hello with the current state, not a fixed zero: the stream
                # closes on its own every few minutes, and anything that
                # happened during the reconnect would otherwise be lost.
                yield f'event: running\ndata: {{"count": {running_migration_count()}}}\n\n'
                yield f'event: lamp\ndata: {{"state": "{lamp_state()}"}}\n\n'
                deadline = time.monotonic() + SSE_MAX_SECONDS
                hub = request.app.state.hub
                while time.monotonic() < deadline and not hub.closed:
                    if await request.is_disconnected():
                        break
                    try:
                        item = await asyncio.to_thread(
                            subscribed.get, True, SSE_HEARTBEAT_SECONDS
                        )
                    except queue.Empty:
                        yield ": ping\n\n"
                        continue
                    if item is CLOSED:
                        # The app is going down. The browser reconnects on
                        # its own; holding the stream open only keeps the
                        # process alive.
                        break
                    event, data = item
                    yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
            finally:
                request.app.state.hub.unsubscribe(subscribed)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={**NO_STORE, "X-Accel-Buffering": "no"},
        )

    @app.get("/flow")
    def flow(request: Request):
        from odoo_migrator.paths import repo_dir
        from odoo_migrator.runner.cartridge import load_cartridge_file
        from odoo_migrator.runner.flow import flow_graph

        current = get_active(request.cookies.get(OPEN_COOKIE))
        name = current["cartridge_id"] if current else "stock"
        version = current["cartridge_version"] if current else "1.0.0"
        migration_id = current["id"] if current else ""
        job_id = ""
        if current:
            from odoo_migrator.models import Job, Module

            with database.connection_context():
                latest = (
                    Job.select()
                    .join(Module)
                    .where(Module.migration == current["id"])
                    .order_by(Job.id.desc())
                    .first()
                )
                if latest is not None:
                    job_id = latest.job_id
        embedded = None
        if not migration_id:
            embedded = flow_graph(
                load_cartridge_file(repo_dir() / "cartridges" / "stock.yaml")
            )

        return _page(
            request,
            "flow.html",
            "flow",
            {
                "flow_name": name,
                "flow_version": version,
                "flow_migration_id": migration_id,
                "flow_job_id": job_id,
                "flow_embedded": embedded,
            },
        )

    @app.get("/api/flow/{migration_id}/graph")
    def flow_graph_route(migration_id: str):
        from odoo_migrator.models import Migration
        from odoo_migrator.runner.flow import flow_graph

        with database.connection_context():
            row = Migration.get_or_none(Migration.id == migration_id)
        if row is None:

            return JSONResponse({"error": "unknown migration"}, status_code=404)
        from odoo_migrator.cartridges import pinned_for

        return JSONResponse(flow_graph(pinned_for(row)))

    @app.get("/api/flow/{job_id}/trace")
    def flow_trace(job_id: str, attempt: int | None = None):
        from odoo_migrator.models import Job, StepRun, ToolCall

        with database.connection_context():
            job = Job.get_or_none(Job.job_id == job_id)
            if job is None:

                return JSONResponse([], status_code=404)
            runs = StepRun.select().where(StepRun.job == job)
            if attempt is not None:
                runs = runs.where(StepRun.attempt == attempt)
            run_ids = [run.id for run in runs]
            calls = list(
                ToolCall.select()
                .where(ToolCall.step_run.in_(run_ids))
                .order_by(ToolCall.id)
            )
            rows = [
                {
                    "id": call.id,
                    "tool": call.tool,
                    "side": call.side,
                    "session_id": call.session_id,
                    "command_id": call.command_id,
                    "preview": (call.preview or "")[:120],
                    "ok": call.ok,
                    "duration_ms": call.duration_ms,
                }
                for call in calls
            ]

        return JSONResponse(rows)

    @app.get("/api/jobs/{job_id}/log")
    def job_log(job_id: str, after: int = 0):
        """The run log so far. `after` is the last line the page holds."""
        from odoo_migrator import runlog

        return JSONResponse({"job_id": job_id, "lines": runlog.lines(job_id, after=after)})

    @app.get("/api/log/{line_id}")
    def job_log_detail(line_id: int):
        """One line's body, fetched when it is opened and not before.

        Prompts and model answers carry the customer's source. They are a
        click away, the way a Sheller journal body is: this tool gets
        demonstrated on a projector.
        """
        from fastapi.responses import PlainTextResponse

        from odoo_migrator import runlog

        body = runlog.detail_of(line_id)
        if body is None:

            return PlainTextResponse("not available", status_code=404)

        return PlainTextResponse(body or "(empty)")

    @app.get("/api/flow/trace/{tool_call_id}/body")
    def flow_trace_body(request: Request, tool_call_id: int):
        from fastapi.responses import PlainTextResponse

        from odoo_migrator.models import ToolCall
        from odoo_migrator.sheller.errors import ShellerError

        with database.connection_context():
            call = ToolCall.get_or_none(ToolCall.id == tool_call_id)
            session_id = call.session_id if call is not None else ""
            command_id = call.command_id if call is not None else ""
        if not session_id:

            return PlainTextResponse("not available")
        try:
            journal = request.app.state.sheller.journal(session_id)
        except ShellerError:

            return PlainTextResponse("not available")
        records = journal.get("records") or []
        matched = [
            record for record in records if str(record.get("id", "")) == str(command_id)
        ]
        chosen = matched[0] if matched else {"records": records}

        return PlainTextResponse(json.dumps(chosen, default=str))

    @app.get("/styleguide")
    def styleguide(request: Request):

        return _page(request, "styleguide.html", "styleguide")

    @app.get("/components")
    def components(request: Request):

        return _page(request, "components.html", "components")

    @app.get("/favicon.ico", include_in_schema=False)
    def favicon():

        return FileResponse(STATIC / "logo.svg", media_type="image/svg+xml", headers=NO_STORE)

    app.mount("/static", NoCacheStaticFiles(directory=STATIC), name="static")

    return app
