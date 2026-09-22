"""HTTP for the migrator UI. Factory, same shape as odoo-sheller."""

import asyncio
import json
import queue
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import httpx2
from fastapi import Body, FastAPI, Form, Request
from typing import Annotated
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from odoo_migrator.authkey import API_KEY_HEADER, KEY_FILENAME, load_or_create
from odoo_migrator.cartridges import list_installs, smoke_run
from odoo_migrator.contract import ContractError, ProgressPayload, ResultPayload
from odoo_migrator.db import init_db
from odoo_migrator.events import CLOSED, EventHub
from odoo_migrator.executor import StubExecutor
from odoo_migrator.files import PathEscapes, read_text, resolve_in_root, write_text
from odoo_migrator.graph import (
    GraphError,
    list_modules,
    module_for_migration,
    resolve_graph,
    set_batch_group,
    leave_out_selection,
    restore_selection,
    skip_subtree,
)
from odoo_migrator.jobs import (
    UnknownJob,
    advance_batch,
    answer_job,
    apply_progress,
    apply_result,
    attention_target,
    batch_is_checkpoint,
    continue_batch,
    dismiss_job,
    lamp_state,
    latest_checkpoint,
    live_run,
    promote_finding,
    reconcile_jobs,
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
from odoo_migrator.n8n import HttpN8nClient, N8nExecutor, sweep_orphaned_sessions
from odoo_migrator.paths import rules_dir as default_rules_dir
from odoo_migrator.paths import static_dir, templates_dir
from odoo_migrator.provision import (
    ComposeError,
    DockerComposeEngine,
    DockerUnavailable,
    count_running,
    describe_side,
    describe_side_without_listing,
)
from odoo_migrator.provision.actions import attach, rebuild, restart, stop, up
from odoo_migrator.proxy import (
    AdminKeyForbidden,
    PathNotAllowed,
    allowed_path,
    normalized_path,
    outgoing_headers,
)
from odoo_migrator.resume import HttpResumeClient
from odoo_migrator.rules import RulesError, stack_versions
from odoo_migrator.settings import KEEP, clear_n8n_api_key, get_app_settings, save_app_settings
from odoo_migrator.sheller import HttpShellerClient
from odoo_migrator.sheller.errors import ShellerUnreachable
from odoo_migrator.sheller.registry import forget, remember

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


def app_version() -> str:
    from importlib.metadata import PackageNotFoundError, version

    try:

        return version("odoo-migrator")
    except PackageNotFoundError:

        return "unknown"


def create_app(
    db_path: Path | str | None = None,
    *,
    compose=None,
    sheller=None,
    sheller_http=None,
    api_key: str | None = None,
    executor=None,
    hub=None,
    resume=None,
    rules_dir=None,
    n8n=None,
) -> FastAPI:
    resolved_db = init_db(db_path)
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
    app.state.sheller_http = sheller_http
    # The machine-facing surface — File API and Sheller proxy — is the one
    # n8n needs, so it cannot stay on loopback like the pages do. Behind it
    # sits an API that writes host files and runs Python as SUPERUSER_ID,
    # which is a key's worth of protection, not none.
    if api_key is None and str(resolved_db) != ":memory:":
        api_key = load_or_create(resolved_db.parent / KEY_FILENAME)
    app.state.api_key = api_key or secrets.token_urlsafe(32)
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

    # The callback path, exposed so an executor double drives the app the
    # way a real one does — through here — instead of reaching past it into
    # the job layer, where it would miss everything a callback triggers.
    app.state.on_progress = _on_progress
    app.state.on_result = _on_result
    app.state.n8n = n8n
    if executor is None:
        n8n_url = get_app_settings()["n8n_base_url"].strip()
        if n8n_url:
            n8n_client = n8n or HttpN8nClient(
                n8n_url, api_key=get_app_settings()["n8n_api_key"]
            )
            app.state.n8n = n8n_client
            executor = N8nExecutor(n8n_client)
        else:
            executor = StubExecutor(progress=_on_progress, result=_on_result)
    app.state.executor = executor
    app.state.resume = resume if resume is not None else HttpResumeClient()
    app.state.rules_dir = Path(rules_dir) if rules_dir is not None else default_rules_dir()
    reconcile_jobs()
    try:
        daemon = app.state.sheller
        if daemon is None:
            daemon = HttpShellerClient(get_app_settings()["sheller_base_url"])
        sweep_orphaned_sessions(daemon)
    except ShellerUnreachable:
        pass

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

    def _wrong_key(request: Request) -> JSONResponse | None:
        presented = request.headers.get(API_KEY_HEADER, "")
        if secrets.compare_digest(presented, request.app.state.api_key):

            return None

        return JSONResponse({"error": "bad_api_key"}, status_code=403)

    def _file_root(migration_id: str, side: str) -> Path | JSONResponse:
        current = get_active(migration_id)
        if current is None:

            return JSONResponse({"error": "not_found"}, status_code=404)
        if side not in ("old", "new"):

            return JSONResponse({"error": "unknown_side"}, status_code=400)
        key = "old_project_path" if side == "old" else "new_project_path"

        return Path(current[key])

    def _note_session(method: str, path: str, upstream) -> None:
        """Keep the write key the daemon hands back through us.

        n8n reaches the daemon only through this proxy, so the key crosses
        this process at open time either way. Without keeping it the app can
        never close a session it did not open — a close with an empty key is
        403, and a session whose process dies leaves the daemon's registry
        rather than lingering as something to sweep.
        """
        if upstream.status_code >= 400:

            return
        if method == "DELETE" and path.startswith("/api/sessions/"):
            forget(path.rsplit("/", 1)[-1])

            return
        if not (method == "POST" and path.rstrip("/") == "/api/sessions"):

            return
        try:
            body = upstream.json()
        except ValueError:

            return
        if not isinstance(body, dict):

            return
        owner = body.get("owner") or {}
        remember(
            body.get("id") or "",
            body.get("write_key") or "",
            str(owner.get("migration_id") or ""),
        )

    async def _forward(request: Request, path: str):
        refused = _wrong_key(request)
        if refused is not None:

            return refused
        try:
            headers = outgoing_headers(dict(request.headers))
        except AdminKeyForbidden:

            return JSONResponse({"error": "admin_key_forbidden"}, status_code=403)
        try:
            path = normalized_path(path)
        except PathNotAllowed:

            return JSONResponse({"error": "proxy_denied"}, status_code=403)
        if not allowed_path(path):

            return JSONResponse({"error": "proxy_denied"}, status_code=403)
        body = await request.body()
        http = request.app.state.sheller_http
        owned = http is None
        if owned:
            http = httpx2.Client(
                base_url=get_app_settings()["sheller_base_url"].rstrip("/"),
                timeout=300.0,
            )
        kwargs: dict = {
            "headers": headers or None,
            "params": list(request.query_params.multi_items()),
        }
        if body:
            kwargs["content"] = body
        try:
            upstream = await run_in_threadpool(http.request, request.method, path, **kwargs)
        except httpx2.RequestError as unreachable:
            # A daemon that is not listening is an operational answer, not a
            # bug in the app. 500 told the executor to look at our traceback.

            return JSONResponse(
                {"error": "sheller_unreachable", "detail": str(unreachable)},
                status_code=502,
            )
        finally:
            if owned:
                http.close()
        _note_session(request.method, path, upstream)

        return Response(
            content=upstream.content,
            status_code=upstream.status_code,
            media_type=upstream.headers.get("content-type"),
        )

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

        return RedirectResponse("/tree", status_code=303)

    @app.post("/tree/modules/restore")
    def tree_restore(
        request: Request,
        module_ids: Annotated[list[int], Form()],
    ):
        """Include again for one or many modules: ancestors first, then downstream."""
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
        """Leave out for one or many modules: upward depends chains only."""
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
                resume=request.app.state.resume,
                hub=request.app.state.hub,
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
                    "Select one module. Migrating a whole batch lands with the "
                    "batch runner."
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
    def tree_batch_run(request: Request, batch_group: str = Form("")):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        if current is None:

            return RedirectResponse("/tree", status_code=303)
        if not batch_group.strip():

            return _tree_page(request, error="Select a batch to migrate.")
        try:
            batch = start_batch(
                migration_id=current["id"],
                batch_group=int(batch_group),
                executor=request.app.state.executor,
                hub=request.app.state.hub,
            )
        except (ContractError, ValueError) as refused:

            return _tree_page(request, error=str(refused))
        if batch.status == "checkpoint":

            return RedirectResponse("/run", status_code=303)

        return RedirectResponse("/tree", status_code=303)

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

    @app.get("/n8n")
    def n8n_page(request: Request):

        return _run_page(request, screen="n8n")

    @app.get("/sheller")
    def sheller_page(request: Request):

        return _run_page(request, screen="sheller")

    @app.get("/result")
    def result_page(request: Request):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        board = migration_results(current["id"]) if current else None

        return _page(
            request,
            "result.html",
            "result",
            {"board": board},
        )

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

    def _run_page(request: Request, error: str | None = None, screen: str = "pipeline"):
        current = get_active(request.cookies.get(OPEN_COOKIE))
        checkpoint = latest_checkpoint(current["id"]) if current else None
        live = live_run(current["id"]) if current else None
        settings = get_app_settings()
        versions = []
        if current:
            try:
                versions = stack_versions(
                    current["source_version"], current["target_version"]
                )
            except RulesError:
                versions = []

        return _page(
            request,
            "run.html",
            screen,
            {
                "screen": screen,
                "checkpoint": checkpoint,
                "live": live,
                "n8n_base_url": _browsable(settings["n8n_base_url"]),
                "sheller_web_url": _browsable(settings["sheller_base_url"], "/web"),
                "versions": versions,
                "error": error,
            },
        )

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
            {
                # The key is never echoed into HTML; the settings field shows a
                # password mask when one is stored.
                "settings": {**stored, "n8n_api_key": ""},
                "has_api_key": bool(stored["n8n_api_key"]),
            },
        )

    @app.post("/settings")
    def settings_save(
        request: Request,
        n8n_base_url: str = Form(""),
        n8n_api_key: str = Form(""),
        app_base_url: str = Form(""),
        sheller_base_url: str = Form(""),
        sheller_image_tag: str = Form(""),
    ):
        # Blank means "leave it alone", since the field arrives blank on every
        # load. Clearing a stored key is its own button.
        key = n8n_api_key if n8n_api_key.strip() else KEEP
        save_app_settings(
            n8n_base_url=n8n_base_url,
            n8n_api_key=key,
            app_base_url=app_base_url,
            sheller_base_url=sheller_base_url,
            sheller_image_tag=sheller_image_tag,
        )

        return RedirectResponse("/settings", status_code=303)

    @app.post("/settings/n8n-api-key/clear")
    def settings_clear_api_key():
        clear_n8n_api_key()

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
        ) is None:

            return _overview(
                request,
                expanded=migration_id,
                error="Those settings were rejected. Nothing saved.",
            )

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

    @app.get("/api/migrations/{migration_id}/files")
    def files_read(request: Request, migration_id: str, side: str, path: str):
        refused = _wrong_key(request)
        if refused is not None:

            return refused
        root = _file_root(migration_id, side)
        if isinstance(root, JSONResponse):

            return root
        try:
            target = resolve_in_root(root, path)
            content = read_text(target)
        except PathEscapes:

            return JSONResponse({"error": "path_escapes"}, status_code=403)
        except FileNotFoundError:

            return JSONResponse({"error": "not_found"}, status_code=404)
        except UnicodeDecodeError:
            # .mo, .png, a vendored wheel. Named, because a 500 here reads as
            # the app being broken rather than the file not being source.

            return JSONResponse({"error": "not_text"}, status_code=415)

        return {"side": side, "path": path, "content": content}

    @app.put("/api/migrations/{migration_id}/files")
    def files_write(
        request: Request,
        migration_id: str,
        side: str = Body(),
        path: str = Body(),
        content: str = Body(),
    ):
        refused = _wrong_key(request)
        if refused is not None:

            return refused
        root = _file_root(migration_id, side)
        if isinstance(root, JSONResponse):

            return root
        try:
            target = resolve_in_root(root, path)
            before = ""
            if target.is_file():
                try:
                    before = read_text(target)
                except UnicodeDecodeError:
                    before = ""
            write_text(target, content)
        except PathEscapes:

            return JSONResponse({"error": "path_escapes"}, status_code=403)
        if side == "new":
            from odoo_migrator.diffs import note_new_write

            note_new_write(migration_id, path, before, content)

        return {"side": side, "path": path, "written": True}

    @app.post("/api/jobs/{job_id}/progress")
    def job_progress(request: Request, job_id: str, payload: ProgressPayload):
        refused = _wrong_key(request)
        if refused is not None:

            return refused
        if payload.job_id != job_id:

            return JSONResponse({"error": "job_mismatch"}, status_code=400)
        try:
            apply_progress(payload, request.app.state.hub)
        except UnknownJob:

            return JSONResponse({"error": "not_found"}, status_code=404)

        return {"ok": True}

    @app.post("/api/jobs/{job_id}/result")
    def job_result(request: Request, job_id: str, payload: ResultPayload):
        refused = _wrong_key(request)
        if refused is not None:

            return refused
        if payload.job_id != job_id:

            return JSONResponse({"error": "job_mismatch"}, status_code=400)
        try:
            apply_result(payload, request.app.state.hub)
        except UnknownJob:

            return JSONResponse({"error": "not_found"}, status_code=404)

        return {"ok": True}

    @app.get("/health")
    async def proxy_health(request: Request):

        return await _forward(request, "/health")

    @app.api_route(
        "/api/{rest:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"]
    )
    async def proxy_api(request: Request, rest: str):

        return await _forward(request, "/api/" + rest)

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
