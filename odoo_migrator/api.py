"""HTTP for the migrator UI. Factory, same shape as odoo-sheller."""

import asyncio
import json
import os
import queue
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlencode, urlparse

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape

from odoo_migrator.cartridges import (
    install_from_text,
    list_installs,
    smoke_run,
    smoke_state,
    unreadable_flow,
)
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
    mark_migrated_from_target,
    module_for_migration,
    resolve_graph,
    restore_selection,
    set_batch_group,
    skip_subtree,
    snapshot_mounts,
)
from odoo_migrator.jobs import (
    REOPENABLE,
    advance_batch,
    answer_job,
    apply_progress,
    apply_result,
    attempt_view,
    attention_target,
    batch_is_checkpoint,
    checkpoint_thread,
    continue_batch,
    dismiss_job,
    keep_reviewed,
    lamp_state,
    latest_checkpoint,
    live_run,
    promote_finding,
    recent_job,
    reconcile_jobs,
    reopen_finished,
    reopen_module,
    save_notes,
    start_batch,
    start_job,
    stop_job,
    stream_state,
)
from odoo_migrator.migrations import (
    ProjectPathsError,
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
from odoo_migrator.module_paths import addon_directories
from odoo_migrator.paths import rules_dir as default_rules_dir
from odoo_migrator.paths import static_dir, templates_dir
from odoo_migrator.pipeline_queue import add_all_steps as queue_add_all_steps
from odoo_migrator.pipeline_queue import add_step as queue_add_step
from odoo_migrator.pipeline_queue import clear as queue_clear
from odoo_migrator.pipeline_queue import nav_count, queue_snapshot
from odoo_migrator.pipeline_queue import remove_last_module as queue_remove_last_module
from odoo_migrator.pipeline_queue import remove_step as queue_remove_step
from odoo_migrator.pipeline_queue import run_one as queue_run_one
from odoo_migrator.pipeline_queue import run_state as queue_state
from odoo_migrator.pipeline_queue import start as queue_start
from odoo_migrator.pipeline_queue import stop as queue_stop
from odoo_migrator.port_guard import HandEdits, discard_hand_edits, keep_hand_edits
from odoo_migrator.provision import (
    ComposeError,
    DockerComposeEngine,
    DockerUnavailable,
    count_running,
    describe_side,
    describe_side_without_listing,
)
from odoo_migrator.provision.actions import rebuild, restart, stop, up
from odoo_migrator.rules import RulesError, stack_versions, supported_versions
from odoo_migrator.runner.cartridge import CartridgeError
from odoo_migrator.settings import get_app_settings, save_app_settings
from odoo_migrator.sheller import HttpShellerClient
from odoo_migrator.web_guard import LocalOnly

STATIC = static_dir()
TEMPLATES = Jinja2Templates(directory=templates_dir())


def breakable(name) -> Markup:
    """A technical name that may wrap after each underscore, and nowhere else.

    `<wbr>` offers a line break without adding a character, so a copied name
    is still the name. Escaped first: the name reaches here as data.
    """

    return Markup("_<wbr>").join(escape(part) for part in str(name).split("_"))


TEMPLATES.env.filters["breakable"] = breakable
# One list of what Reopen takes, for the route and the button alike.
TEMPLATES.env.globals["reopenable"] = REOPENABLE
NO_STORE = {"Cache-Control": "no-store"}
# A flow file is a few kilobytes. Past this a body is not one.
FLOW_LIMIT = 256 * 1024
OPEN_COOKIE = "open_migration"
SSE_RETRY_MS = 3000
SSE_HEARTBEAT_SECONDS = 15
SSE_MAX_SECONDS = 300
# What the pages listen to. The runner also publishes its steps, tool calls
# and job ends, for its own record and the tests; sent to every open page,
# each tool call was one more message to every tab with nothing listening.
PAGE_EVENTS = frozenset({"module", "checkpoint", "lamp", "log", "queue"})
PROVISION_ACTIONS = {
    "up": up,
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


def _count_param(request: Request, name: str) -> int:
    """A non-negative count from the query string; anything else is 0."""
    raw = request.query_params.get(name) or ""

    return int(raw) if raw.isdigit() else 0


def _providers(migration: dict | None = None) -> list[dict]:
    """Selectable providers and the models each one lists.

    The scripted double has no models and no settings, so it does not
    appear on the card. A provider with settings stays even when it lists
    nothing — the local one lists what its server has loaded, which is
    nothing while the server is off — and the pair the card saved is always
    among the options. Dropped, the select fell to another provider, every
    model was disabled and not sent, and Save was refused.
    """
    from odoo_migrator.runner.providers import REGISTRY, known_models

    saved_provider = (migration or {}).get("provider_override") or ""
    saved_model = (migration or {}).get("model_override") or ""
    rows = []
    for name, models in sorted(known_models().items()):
        listed = set(models)
        if name == saved_provider and saved_model:
            listed.add(saved_model)
        if not listed and not REGISTRY[name].settings:
            continue
        rows.append({"name": name, "models": sorted(listed)})

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

        # The pages build a client per request, from the URL Settings has
        # now. The runner does the same per job: handed None it died on the
        # first probe, and handed one client at startup it never saw a URL
        # saved after.

        def current_sheller():

            return HttpShellerClient(get_app_settings()["sheller_base_url"])

        return NativeRunner(
            sheller=sheller,
            hub=hub,
            progress=progress,
            result=result,
            sheller_for_job=None if sheller is not None else current_sheller,
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
    # Loopback keeps other machines out; this keeps other pages out.
    app.add_middleware(LocalOnly)
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
            # while a module waited for an answer showed a calm nav bar. The
            # counter is the open migration's queue.
            "queue_count": nav_count(current["id"] if current else None),
            "lamp": lamp_state(),
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
                # When the error is about the source container, the fix is on the
                # migration's card; the template turns this into a link there.
                "error_settings": error_settings,
                "selected": selected,
                "graph_updated": request.query_params.get("graph") == "updated",
                "migrated_marked": _count_param(request, "migrated"),
                "unplaced": _count_param(request, "unplaced"),
                "target_error": request.query_params.get("target_error") or "",
            },
        )

    def _link_containers(request: Request, migration: dict) -> str | None:
        """Record the containers the app will talk to, from the ones running
        for the two projects: both sides, every Build graph, so a side started
        outside the app, or brought up again, is the one a run uses. Build graph
        never *starts* a container — that is Start on the card — so a source
        that is not running is an error the human resolves there, not silent
        magic here. The target is linked when it runs and not asked for: what
        reads it says so. Returns an error message, or None."""
        old = Path(migration["old_project_path"])
        new = Path(migration["new_project_path"])
        engine = request.app.state.compose
        try:
            sides = {
                "source": describe_side(engine, old, sibling_path=new),
                "target": describe_side(engine, new, sibling_path=old),
            }
        except DockerUnavailable as exc:
            if migration["old_container"]:

                return None

            return f"The source container can't be linked; Docker is not reachable: {exc}"
        _link_running(migration, sides)
        side = sides["source"]
        # One recorded before stays: a container started without compose is
        # not one the card can see, and the probe says if it is gone.
        if (side.state == "running" and side.container) or migration["old_container"]:

            return None
        if side.state == "stopped":

            return (
                "The source container is not running yet. Press Start on its card, "
                "then Build graph."
            )

        return f"The source container can't be linked: {side.detail or side.state}"

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
        except (RulesError, ProjectPathsError) as refused:

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
        # Link to the containers already running instead of making the human
        # press a button on another screen first. resolve_graph re-reads the
        # recorded container from the row.
        unlinked = _link_containers(request, current)
        if unlinked is not None:

            return _tree_page(request, error=unlinked, error_settings=current["id"])
        # Where each container mounts its addons from. Read now, once; a run
        # uses this snapshot. A changed compose file needs Build graph again.
        snapshot_mounts(request.app.state.compose, current["id"])
        try:
            resolve_graph(_sheller(request), current["id"])
        except GraphError as refused:

            return _tree_page(request, error=str(refused))
        # A half-ported project: what the target already has installed is
        # marked, not queued again. The same read finds modules the target
        # would not load. A target that cannot be read is said on the page;
        # the graph from the source stands either way.
        target = mark_migrated_from_target(_sheller(request), current["id"])
        unplaced = sum(
            1
            for row in list_modules(current["id"])
            if row.get("path_problem") and not row.get("struck_out")
        )
        query = {
            "graph": "updated",
            "migrated": target["marked"],
            "unplaced": unplaced,
        }
        if target["error"]:
            query["target_error"] = target["error"]

        return RedirectResponse(f"/tree?{urlencode(query)}", status_code=303)

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
        """Exclude for one or many modules. A leaf goes alone; a module with
        dependents, or a multi-select, takes everything downstream with it."""
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
                    "Runner."
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

        return _page(
            request,
            "result.html",
            "result",
            {"board": board, "error": error},
        )

    @app.get("/result")
    def result_page(request: Request):

        return _result_page(request)

    @app.get("/api/jobs/{job_id}/attempts/{attempt}")
    def job_attempt(job_id: str, attempt: int):
        """One attempt's ribbon and retry lane, for the attempt picker."""
        view = attempt_view(job_id, attempt)
        if view is None:

            return JSONResponse({"error": "no such attempt"}, status_code=404)

        return JSONResponse(view)

    @app.get("/api/result/progress")
    def result_progress(request: Request):
        """The board's tallies now, for the counters over the cards."""
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return JSONResponse({"error": "no migration"}, status_code=404)
        progress = migration_results(current["id"])["progress"]

        return JSONResponse({"migration_id": current["id"], **progress})

    @app.get("/api/result/modules/{module_id}/summary")
    def result_module_summary(request: Request, module_id: int):
        """What the last run on this module did, opened from its card.

        The checkpoint thread used to be one flat list at the top of Result:
        a paragraph about one module above thirty cards, with nothing saying
        which one it was about or when it was written.
        """
        from odoo_migrator.jobs import module_outcome
        from odoo_migrator.models import Module

        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return JSONResponse({"error": "no migration"}, status_code=404)
        with database.connection_context():
            owned = (
                Module.select()
                .where((Module.id == module_id) & (Module.migration == current["id"]))
                .exists()
            )
        if not owned:

            return JSONResponse({"error": "unknown module"}, status_code=404)
        outcome = module_outcome(module_id)
        if outcome is None:

            return JSONResponse({"error": "never ran"}, status_code=404)

        return JSONResponse(outcome)

    @app.post("/migrations/{migration_id}/reopen")
    def migration_reopen(request: Request, migration_id: str):
        if get_active(migration_id) is None:

            return _overview(request, error="No such migration.")
        reopen_finished(migration_id, request.app.state.hub)

        return RedirectResponse(f"/?settings={migration_id}", status_code=303)

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

    @app.post("/result/modules/{module_id}/keep")
    def result_keep_one(request: Request, module_id: int):
        """Drop the review note: someone looked, and the module stands."""
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/result", status_code=303)
        try:
            keep_reviewed(module_id, current["id"], request.app.state.hub)
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

    def _ribbon(current: dict, live: dict | None) -> dict:
        """The pipeline, each step with how this lap went. Same shape as SSE."""
        from odoo_migrator.cartridges import pinned_for
        from odoo_migrator.jobs import IN_FLIGHT, lane_states, ribbon_states
        from odoo_migrator.models import Job, Migration

        with database.connection_context():
            migration = Migration.get_or_none(Migration.id == current["id"])
            if migration is None:

                return []
            job = None
            if live:
                job = (
                    Job.select()
                    .where(
                        (Job.module == live["module_id"])
                        & (Job.status.in_(IN_FLIGHT))
                    )
                    .order_by(Job.id.desc())
                    .first()
                )
            if job is not None:

                return {
                    "steps": ribbon_states(job),
                    "lane": lane_states(job),
                    "attempts": max(job.attempt, 1),
                    "job_id": job.job_id,
                }
            pipeline = pinned_for(migration).pipeline

        return {
            "steps": [{"id": step.id or step.kind, "state": "pending"} for step in pipeline],
            "lane": [],
            "attempts": 0,
            "job_id": "",
        }

    def _run_page(
        request: Request,
        error: str | None = None,
        screen: str = "pipeline",
        hand_edits: list[dict] | None = None,
    ):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        checkpoint = latest_checkpoint(current["id"]) if current else None
        live = live_run(current["id"]) if current else None
        if live:
            # A Stop this process has heard and the run has not reached yet:
            # the turn in flight finishes first, which can take minutes.
            asked = getattr(request.app.state.executor, "stopping", None)
            live["stopping"] = bool(asked(live["job_id"])) if callable(asked) else False
        # Drawn whether or not a run is in flight: a page opened between two
        # modules has nothing else for the next one's events to paint.
        ribbon = _ribbon(current, live) if current else None
        # The module this panel is showing, not the migration: the thread
        # used to carry every checkpoint ever written here, so a diagnosis of
        # the module before this one appeared under a run that had just
        # started.
        thread = (
            checkpoint_thread(current["id"], job_id=live["job_id"])
            if current and live
            else []
        )
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
                "hand_edits": hand_edits or [],
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

    @app.post("/run/queue/remove")
    def run_queue_remove(request: Request, module: str = Form("")):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        wants_json = "application/json" in request.headers.get("accept", "")
        if current is None:
            if wants_json:

                return JSONResponse({"error": "no open migration"}, status_code=400)

            return RedirectResponse("/run", status_code=303)
        message = ""
        if not module.strip().isdigit():
            message = "Pick a module to remove."
        else:
            try:
                queue_remove_last_module(current["id"], int(module))
            except ValueError as refused:
                message = str(refused)
        if message:
            if wants_json:

                return JSONResponse({"error": message}, status_code=400)

            return _run_page(request, error=message)
        from odoo_migrator.pipeline_queue import publish_queue

        publish_queue(request.app.state.hub, current["id"])
        if wants_json:

            return JSONResponse(
                {"migration_id": current["id"], **queue_snapshot(current["id"])}
            )

        return RedirectResponse("/run", status_code=303)

    @app.post("/run/queue/run-module")
    def run_queue_run_module(request: Request, module: str = Form("")):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        wants_json = "application/json" in request.headers.get("accept", "")
        if current is None:
            if wants_json:

                return JSONResponse({"error": "no open migration"}, status_code=400)

            return RedirectResponse("/run", status_code=303)
        message = ""
        if not module.strip().isdigit():
            message = "Pick a module to run."
        else:
            try:
                queue_run_one(
                    current["id"],
                    int(module),
                    request.app.state.executor,
                    request.app.state.hub,
                )
            except ValueError as refused:
                message = str(refused)
        if message:
            if wants_json:

                return JSONResponse({"error": message}, status_code=400)

            return _run_page(request, error=message)
        if wants_json:

            return JSONResponse(
                {"migration_id": current["id"], **queue_snapshot(current["id"])}
            )

        return RedirectResponse("/run", status_code=303)

    @app.post("/run/queue/remove-step")
    def run_queue_remove_step(request: Request, step: str = Form("")):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        wants_json = "application/json" in request.headers.get("accept", "")
        if current is None:
            if wants_json:

                return JSONResponse({"error": "no open migration"}, status_code=400)

            return RedirectResponse("/run", status_code=303)
        message = ""
        if not step.strip().isdigit():
            message = "Pick a step to take out."
        else:
            try:
                queue_remove_step(current["id"], int(step))
            except ValueError as refused:
                message = str(refused)
        if message:
            if wants_json:

                return JSONResponse({"error": message}, status_code=400)

            return _run_page(request, error=message)
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

    def _announce_queue(request: Request, current: dict) -> None:
        """Tell the open pages the queue moved: a review answered, a run read."""
        from odoo_migrator.pipeline_queue import publish_queue

        publish_queue(request.app.state.hub, current["id"])

    def _start_queue(request: Request, current: dict):
        try:
            queue_start(
                current["id"],
                request.app.state.executor,
                request.app.state.hub,
            )
        except HandEdits as refused:
            # Shown on the run card with a way out beside each module, not as
            # a line over Selection that sends someone to their IDE.

            return _run_page(request, hand_edits=refused.modules)
        except ContractError as refused:

            return _run_page(request, error=str(refused))

        return RedirectResponse("/run", status_code=303)

    @app.post("/run/queue/run")
    def run_queue_run(request: Request):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/run", status_code=303)

        return _start_queue(request, current)

    @app.post("/run/modules/{module_id}/discard")
    def run_discard(request: Request, module_id: int):
        """Put the module's target directory back to its last commit, then Run."""
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/run", status_code=303)
        try:
            discard_hand_edits(current["id"], module_id)
        except ContractError as refused:

            return _run_page(request, error=str(refused))

        return _start_queue(request, current)

    @app.post("/run/modules/{module_id}/keep")
    def run_keep(request: Request, module_id: int):
        """Take the module's files as they are, then Run."""
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/run", status_code=303)
        try:
            keep_hand_edits(current["id"], module_id)
        except ContractError as refused:

            return _run_page(request, error=str(refused))

        return _start_queue(request, current)

    @app.post("/run/queue/stop")
    def run_queue_stop(request: Request):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/run", status_code=303)
        queue_stop(current["id"], request.app.state.hub)

        return RedirectResponse("/run", status_code=303)

    # What a restart leaves behind: a job whose thread died with the process.
    _NO_RUNNER = (
        "Nothing in this process is running that job — it was started before "
        "the last restart. It is marked unknown; start it again when you want it."
    )

    @app.post("/run/modules/{module_id}/stop")
    def run_stop(request: Request, module_id: int):
        """Stop this run, and stop the queue behind it.

        The step in flight finishes — an agent turn cannot be called back —
        and the job ends there rather than at the end of the module. The
        queue stop on its own lets the module finish first, which is not
        what someone watching a run spend tokens is asking for.
        """
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/run", status_code=303)
        # Scoped like an answer: a stale page of another migration stopped that
        # one's job, and this one's queue with it.
        try:
            module_for_migration(current["id"], module_id)
        except GraphError as refused:

            return _run_page(request, error=str(refused))
        queue_stop(current["id"], request.app.state.hub)
        try:
            stopped = stop_job(
                module_id=module_id,
                hub=request.app.state.hub,
                stop=getattr(request.app.state.executor, "stop", None),
            )
        except ContractError as refused:
            # Ended between the page drawing Stop and the press, or a reload
            # of the page a Stop answered, which posts it again.

            return _run_page(request, error=str(refused))
        if not stopped:

            return _run_page(request, error=_NO_RUNNER)

        # Back to the screen, not a page under the POST: reloading that one
        # pressed Stop again.
        return RedirectResponse("/run", status_code=303)

    @app.post("/run/modules/{module_id}/answer")
    def run_answer(request: Request, module_id: int, answer: str = Form("")):
        """The same answer as on Tree, from the screen that watches the run."""
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/run", status_code=303)
        try:
            module_for_migration(current["id"], module_id)
            answer_job(
                module_id=module_id,
                answer=answer,
                hub=request.app.state.hub,
                resume=getattr(request.app.state.executor, "resume", None),
            )
        except (GraphError, ContractError) as refused:

            return _run_page(request, error=str(refused))

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
    def run_continue(request: Request, override: str = Form(""), run: str = Form("")):
        """Close the review. From a queue card it also runs the queue again:
        a person who presses Continue there means the work to go on, and a
        review that closed with nothing moving read as a button that did
        nothing."""
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/run", status_code=303)
        try:
            continue_batch(current["id"], override=override.strip() == "1")
        except ContractError as refused:
            # A second press of the same button: the first closed the review
            # and started the queue, and that is what was asked for.
            if run.strip() == "1" and queue_state(current["id"]) == "running":

                return RedirectResponse("/run", status_code=303)

            return _run_page(request, error=str(refused))
        _announce_queue(request, current)
        if run.strip() == "1":

            return _start_queue(request, current)

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
        # A read failure no longer flashes the lamp.
        request.app.state.hub.publish("lamp", {"state": lamp_state()})
        _announce_queue(request, current)

        return RedirectResponse("/run", status_code=303)

    @app.post("/run/modules/{module_id}/discard-run")
    def run_discard_run(request: Request, module_id: int):
        """Forget the module's runs: not the hand-edit Discard above, which
        only puts files back and then runs."""
        from odoo_migrator.discard import discard_run

        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/run", status_code=303)
        try:
            discard_run(module_id, current["id"], request.app.state.hub)
        except ContractError as refused:

            return _run_page(request, error=str(refused))
        _announce_queue(request, current)

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

    # The screens Save and Close may return to. A path from this list and
    # nothing else, whatever the request carried: the value comes from a
    # header and a form field, and a redirect to either is an open redirect.
    _RETURN_SCREENS = frozenset(
        {"/", "/tree", "/flow", "/run", "/sheller", "/result", "/styleguide"}
    )

    def _return_screen(request: Request, value: str | None) -> str | None:
        """`value` as one of this app's screens, or None. Query and fragment
        are dropped: coming back to Overview with `?settings=` would reopen a
        migration's settings panel."""
        if not value:

            return None
        parsed = urlparse(value)
        if parsed.netloc and parsed.netloc != request.url.netloc:

            return None
        if parsed.scheme not in ("", "http", "https"):

            return None

        return parsed.path if parsed.path in _RETURN_SCREENS else None

    @app.get("/settings")
    def settings(request: Request):
        stored = get_app_settings()
        # Where the person was: told by the page that sent them, else by the
        # browser, else Overview.
        back = (
            _return_screen(request, request.query_params.get("back"))
            or _return_screen(request, request.headers.get("referer"))
            or "/"
        )

        return _page(
            request,
            "settings.html",
            "settings",
            {"settings": stored, "back": back},
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
        # A field the form did not carry is kept: the bins post from a form
        # of their own, with nothing in it but the key to remove.
        stored = get_app_settings()

        def sent(name: str) -> str:

            return str(form.get(name) or "") if name in form else stored[name]

        save_app_settings(
            sheller_base_url=sent("sheller_base_url"),
            sheller_image_tag=sent("sheller_image_tag"),
            provider_values=values,
            cleared={str(cleared)} if cleared else set(),
        )

        back = _return_screen(request, str(form.get("back") or "")) or "/"
        if cleared:
            # Removing a key is not leaving the page.
            return RedirectResponse(f"/settings?back={quote(back, safe='')}", status_code=303)

        return RedirectResponse(back, status_code=303)

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

    def _link_running(migration: dict, sides: dict) -> None:
        """A side the card shows running is the one this migration talks to.
        Attach did this by hand, and nobody pressed it: everything worked
        without it until a container started outside the app was not the
        one recorded."""
        saved = {"source": migration["old_container"], "target": migration["new_container"]}
        for side, status in sides.items():
            if status.state == "running" and status.container and status.container != saved[side]:
                save_side_container(migration["id"], side, status.container)

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
            sides = {
                "source": describe_side(engine, old_path, sibling_path=new_path),
                "target": describe_side(engine, new_path, sibling_path=old_path),
            }
            _link_running(migration, sides)

            return {**sides, "resources": count_running(engine, _known_project_paths())}, None
        except DockerUnavailable as exc:
            detail = str(exc)

            return {
                "source": describe_side_without_listing(old_path, detail=detail),
                "target": describe_side_without_listing(new_path, detail=detail),
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
                "smoke": smoke_state(migration) if migration else None,
                # Only here: the local provider's list is a request to its
                # server, and every page used to make it for a select only
                # this card has.
                "providers": _providers(migration) if migration else [],
                # The form is asked for, not permanently open. It stays open
                # with what was typed still in it when the answer is a
                # refusal — re-rendering an empty form is how someone loses
                # two paths they pasted.
                "creating": creating,
                "typed": typed or {},
                "versions": supported_versions(),
                "addons_roots": (
                    addon_directories(migration["id"])
                    if migration
                    else {"source": [], "target": []}
                ),
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
        if cartridge_id:
            unreadable = unreadable_flow(cartridge_id, cartridge_version or "1.0.0")
            if unreadable:

                return _overview(
                    request,
                    expanded=migration_id,
                    error=(
                        f"{cartridge_id}@{cartridge_version or '1.0.0'} cannot be pinned: "
                        f"{unreadable}. Nothing saved."
                    ),
                )
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
            refused = "Those settings were rejected. Nothing saved."
            if (provider_override or "").strip() and not (model_override or "").strip():
                # The model select sends nothing when the provider lists
                # nothing, which for the local one means its server is off.
                refused = (
                    f"Pick a model for {provider_override.strip()}. Nothing saved. "
                    "A provider that lists none may have its server off."
                )

            return _overview(request, expanded=migration_id, error=refused)

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
            row = install_from_text(text)
        except (CartridgeError, UnicodeError) as exc:

            return _overview(request, expanded=migration_id, error=str(exc))
        with database.connection_context():
            from odoo_migrator.models import Migration

            # The row just imported. The newest by id is another flow when
            # this one was imported before: it is updated in place.
            migration = Migration.get_or_none(Migration.id == migration_id)
            if migration is not None:
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
        if side not in ("source", "target") or handler is None:

            return _overview(
                request,
                expanded=migration_id,
                error="That container action was rejected. Nothing changed.",
            )
        named = (
            migration["old_project_path"]
            if side == "source"
            else migration["new_project_path"]
        )
        if not (named or "").strip():
            # `Path("")` is the server's working directory, not a project.

            return _overview(
                request,
                expanded=migration_id,
                error=f"This migration names no {side} project directory. Nothing changed.",
            )
        path = Path(named)
        other = (
            migration["new_project_path"]
            if side == "source"
            else migration["old_project_path"]
        )
        sibling = Path(other) if (other or "").strip() else None
        try:
            result = handler(request.app.state.compose, path, sibling_path=sibling)
        except ComposeError as exc:

            return _overview(request, expanded=migration_id, error=str(exc))
        save_side_container(migration_id, side, result.container)

        return RedirectResponse(f"/?settings={migration_id}", status_code=303)

    def _stream_hello(migration_id: str) -> list[tuple[str, dict]]:
        if get_active(migration_id) is None:

            return []

        return [
            *stream_state(migration_id),
            ("queue", {"migration_id": migration_id, **queue_snapshot(migration_id)}),
        ]

    @app.get("/sse")
    async def sse(request: Request, migration: str = ""):
        """One stream per page, held open.

        ``async def`` on purpose, and the one endpoint that must be: a sync
        handler is run in the threadpool, so every open page would hold a
        thread for as long as it is open. What it reads from the database —
        the hello — runs in a worker thread with its own connection: on the
        loop, a slow or locked database stalled every page at once.

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
                lamp = await asyncio.to_thread(lamp_state)
                yield f'event: lamp\ndata: {{"state": "{lamp}"}}\n\n'
                # The bar's counter is the open migration's queue. A page that
                # names no migration of its own still has one in its bar.
                if not migration:
                    opened = await asyncio.to_thread(
                        get_active, request.cookies.get(OPEN_COOKIE)
                    )
                    if opened:
                        told = await asyncio.to_thread(nav_count, opened["id"])
                        yield (
                            "event: queue\ndata: "
                            + json.dumps({"migration_id": opened["id"], "count": told})
                            + "\n\n"
                        )
                # The page's own migration as it is now. Whatever was
                # published while the stream was down is gone, and a page
                # that missed a run ending kept showing it running.
                if migration:
                    told = await asyncio.to_thread(_stream_hello, migration)
                    for event, data in told:
                        yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
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
                    if event not in PAGE_EVENTS:
                        continue
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
        from odoo_migrator.paths import cartridges_dir
        from odoo_migrator.runner.cartridge import load_cartridge_file
        from odoo_migrator.runner.flow import flow_graph

        current = get_active(request.cookies.get(OPEN_COOKIE))
        name = current["cartridge_id"] if current else "stock"
        version = current["cartridge_version"] if current else "1.0.0"
        migration_id = current["id"] if current else ""
        job_id = ""
        # Whose trace is under the picture. The page takes the newest job on
        # the open migration, and said so nowhere: after a batch of thirty
        # modules the list was "whatever ran last", with nothing naming it.
        traced = None
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
                    traced = {
                        "module": latest.module.technical_name,
                        "job_id": job_id,
                        "short": job_id[:8],
                        "attempt": latest.attempt,
                        "status": latest.status,
                    }
        embedded = None
        if not migration_id:
            embedded = flow_graph(
                load_cartridge_file(cartridges_dir() / "stock.yaml")
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
                "flow_traced": traced,
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

    # The flow file itself, read-only. The dialog on every page reads it from
    # the Flow field on a migration's card, and an editor will write through
    # the same address, so the JSON carries the hash a write would have to
    # quote.
    @app.get("/api/flows/{cartridge_id}/{version}")
    def flow_file(cartridge_id: str, version: str):
        import hashlib

        from odoo_migrator.cartridges import next_version
        from odoo_migrator.flowfile import flow_outline, flow_text

        text = flow_text(cartridge_id, version)
        if text is None:

            return JSONResponse({"error": "unknown flow"}, status_code=404)

        return JSONResponse(
            {
                "cartridge_id": cartridge_id,
                "version": version,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "text": text,
                "outline": flow_outline(text),
                # What a fork of this file would be called. A suggestion; the
                # check that counts is the one the save makes.
                "next_version": next_version(cartridge_id, version),
            },
            headers=NO_STORE,
        )

    # The one way the viewer writes: a fork, stored under the id and version
    # its own text names. It is a new row or an error, never a replacement.
    @app.post("/api/flows/fork")
    async def flow_fork(request: Request):
        import hashlib
        import json

        from odoo_migrator.cartridges import FlowTaken, fork_from_text, next_version

        raw = await request.body()
        if len(raw) > FLOW_LIMIT:

            return JSONResponse({"error": "That flow file is too large."}, status_code=413)
        try:
            text = json.loads(raw).get("text")
        except (ValueError, AttributeError):
            text = None
        if not isinstance(text, str):

            return JSONResponse({"error": "The body must be JSON with a text."}, status_code=400)
        try:
            row = fork_from_text(text)
        except FlowTaken as exc:

            return JSONResponse(
                {"error": str(exc), "next_version": next_version(exc.cartridge_id, exc.version)},
                status_code=409,
            )
        except CartridgeError as exc:

            return JSONResponse({"error": str(exc)}, status_code=400)

        return JSONResponse(
            {
                "cartridge_id": row.cartridge_id,
                "version": row.version,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
            },
            status_code=201,
            headers=NO_STORE,
        )

    @app.get("/api/jobs/{job_id}/log")
    def job_log(job_id: str, after: int = 0):
        """The run log so far. `after` is the last line the page holds."""
        from odoo_migrator import runlog

        return JSONResponse({"job_id": job_id, "lines": runlog.lines(job_id, after=after)})

    @app.delete("/api/jobs/{job_id}/log")
    def job_log_clear(job_id: str):
        """Drop this job's log. The job, its steps and its result stay.

        The log is a reading aid, not a record of the run: the outcome
        lives on the job and its `StepRun` rows. Clearing is how a long
        session stops drowning the next run.
        """
        from odoo_migrator import runlog

        return JSONResponse({"job_id": job_id, "deleted": runlog.clear(job_id)})

    def _journal_body(request: Request, session_id: str, command_id: str) -> str:
        """One command's record from Sheller's journal, whole.

        The daemon allows a megabyte of stdout per command, so this is not
        something to copy into our own row: a stored body would be a shorter
        duplicate of the only full copy there is. The journal can be removed,
        which is what "not available" means.
        """
        from odoo_migrator.sheller.errors import ShellerError

        if not session_id:

            return "not available"
        try:
            # `_sheller`, not `app.state.sheller`: that slot holds the double a
            # test injects and is None in a live process, so reaching for it
            # directly answered every body with a 500.
            journal = _sheller(request).journal(session_id)
        except ShellerError:

            return "not available"
        records = journal.get("records") or []
        matched = [
            record for record in records if str(record.get("id", "")) == str(command_id)
        ]
        if not matched:
            # Never the whole journal. That begins with the session's own
            # metadata, which is not what anyone opened a line to read.

            return "not available: that command is no longer in the journal"
        # A command and its outcome share an id — `run_test` writes the
        # request and then the result — and the outcome is the half worth
        # opening a line for.

        return json.dumps(matched[0] if len(matched) == 1 else matched, default=str)

    @app.get("/api/log/{line_id}")
    def job_log_detail(request: Request, line_id: int):
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
        if not body:
            # A line about a tool call stores no body of its own. The whole
            # answer is in the daemon's journal, which is where the trace
            # under the flow used to be the only way to read it.
            ran = runlog.tool_call_of(line_id)
            if ran is not None:

                return PlainTextResponse(_journal_body(request, *ran))

        return PlainTextResponse(body or "(empty)")

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
