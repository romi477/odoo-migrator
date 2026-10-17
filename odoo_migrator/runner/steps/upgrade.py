"""Install or upgrade the module on the target session, then commit."""

from __future__ import annotations

import ast
import contextlib
import time

from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext, StepResult
from odoo_migrator.runner.prune import prune_old_migrations
from odoo_migrator.runner.sessions import reopen_side, wait_ready
from odoo_migrator.runner.steps.verdict import note_log_errors
from odoo_migrator.sheller.errors import ShellerError, ShellerTimeout

# The daemon stops waiting for an exec at 300s and says so with a 504. The
# HTTP call has to outlast that, or ours fires first and the daemon's reason
# is lost. The client default of 30s timed out nearly every real upgrade,
# which kept running in the session while the lap was marked failed.
UPGRADE_TIMEOUT = 330.0
# An upgrade that outlives the daemon's ceiling is still running in the
# session. How long to wait for it before calling it lost.
UPGRADE_WAIT = 3600.0
# The traceback's end and the log's end are where the reason is.
_TAIL = 40

# `update_list` first: a module this database has never seen is not in
# `ir.module.module` until the addons paths are scanned, and a manifest the
# port changed is not read again without it. `to upgrade` is upgraded, not
# installed: `button_install` does not take that state.
_UPGRADE = """\
Module = env['ir.module.module']
Module.update_list()
mod = Module.search([('name', '=', {name!r})], limit=1)
if not mod:
    raise RuntimeError('module not found on the addons path: ' + {name!r})
if mod.state == 'uninstallable':
    raise RuntimeError({name!r} + ' is uninstallable: its manifest says installable False, '
                       'or its dependencies are not on the addons path')
if mod.state in ('installed', 'to upgrade'):
    mod.button_immediate_upgrade()
else:
    mod.button_immediate_install()
"""

# `button_immediate_*` commits the states it marks before it rebuilds the
# registry. When the rebuild fails they stay committed, and the next
# upgrade — this module's or any other's — tries to install them again.
# 19.0 dropped the three `button_*_cancel` for one `button_reset_state`.
_RESET = """\
Module = env['ir.module.module']
stuck = Module.search([('state', 'in', ('to install', 'to upgrade', 'to remove'))])
names = sorted(stuck.mapped('name'))
if hasattr(Module, 'button_reset_state'):
    Module.button_reset_state()
else:
    stuck.filtered(lambda m: m.state == 'to install').button_install_cancel()
    stuck.filtered(lambda m: m.state == 'to upgrade').button_upgrade_cancel()
    stuck.filtered(lambda m: m.state == 'to remove').button_uninstall_cancel()
names
"""

_STATE = """\
Module = env['ir.module.module']
mod = Module.search([('name', '=', {name!r})], limit=1)
stuck = Module.search_count([('state', 'in', ('to install', 'to upgrade', 'to remove'))])
(mod.state if mod else 'missing', stuck)
"""


def run(step: Step, ctx: StepContext) -> StepResult:
    """Drop the source version's migration scripts, then upgrade.

    Here, and not in a step of its own: this is the moment the ported module
    is read by Odoo, and it runs again after every patch.
    """
    from odoo_migrator.module_paths import module_root

    directory = module_root(ctx.migration, ctx.module, "target")
    removed = []
    if directory is not None:
        removed = prune_old_migrations(directory, ctx.migration.target_version)
    result = _upgrade(step, ctx)
    if removed:
        note = "removed old migration scripts: " + ", ".join(removed)
        result.output = f"{note}\n{result.output}" if result.output else note

    return result


def _upgrade(step: Step, ctx: StepContext) -> StepResult:
    """Always in a fresh process, which also drops what the agent left pending.

    The session open since the job started imported the module's Python as
    it was then; an upgrade in it ran the old code, so a patch never reached
    the database and every retry failed the same way. Closing it discards
    the uncommitted writes the agent's own `exec` calls left behind, which
    `button_immediate_*` would otherwise commit along with the upgrade.
    """
    if "target" not in ctx.session_targets:

        return StepResult(ok=False, output="no target session")
    name = ctx.module.technical_name
    try:
        reopen_side(ctx, "target")
    except ShellerError as exc:

        return StepResult(ok=False, output=f"could not open a fresh target session: {exc}")
    session_id = ctx.sessions["target"]
    write_key = ctx.write_keys["target"]
    try:
        executed = ctx.sheller.exec(
            session_id, _UPGRADE.format(name=name), write_key=write_key, timeout=UPGRADE_TIMEOUT
        )
    except ShellerTimeout:

        return _outlived(ctx, name)
    except ShellerError as exc:

        return _failed(ctx, str(exc))
    note_log_errors(ctx, executed.get("stderr"))
    if executed.get("error"):

        return _failed(ctx, _describe(executed))
    try:
        committed = ctx.sheller.commit(session_id, write_key=write_key)
    except ShellerError as exc:

        return _failed(ctx, f"commit: {exc}")
    if isinstance(committed, dict) and committed.get("error"):

        return _failed(ctx, f"commit: {committed['error']}")
    ctx.emit({"kind": "upgrade", "module": name, "job_id": ctx.job_id})

    return StepResult(ok=True, output=f"upgraded {name}")


def _outlived(ctx: StepContext, name: str) -> StepResult:
    """The call stopped waiting; the upgrade did not. Wait for it, then look.

    Its own answer is lost — the daemon abandons the result frame — so the
    module's state is the only account left of how it went.
    """
    state = wait_ready(ctx, "target", time.monotonic() + UPGRADE_WAIT)
    if state != "ready":
        with contextlib.suppress(ShellerError):
            ctx.sheller.interrupt(ctx.sessions["target"], write_key=ctx.write_keys["target"])

        return _failed(
            ctx,
            f"the upgrade of {name} ran past {UPGRADE_WAIT:.0f}s and the session "
            f"is {state}; it was interrupted",
        )
    try:
        executed = ctx.sheller.exec(
            ctx.sessions["target"], _STATE.format(name=name), write_key=ctx.write_keys["target"]
        )
        found, stuck = ast.literal_eval(executed.get("result") or "('unknown', 0)")
    except (ShellerError, ValueError, SyntaxError, TypeError) as exc:

        return _failed(ctx, f"the upgrade of {name} outlived the call, and its state is unreadable: {exc}")
    if found != "installed" or stuck:

        return _failed(
            ctx,
            f"the upgrade of {name} outlived the call and did not finish: "
            f"{name} is {found}, {stuck} module(s) still marked to install or upgrade",
        )
    ctx.emit({"kind": "upgrade", "module": name, "job_id": ctx.job_id})

    return StepResult(
        ok=True,
        output=f"upgraded {name} (it outlived the daemon's exec ceiling; checked its state after)",
    )


def _failed(ctx: StepContext, reason: str) -> StepResult:
    """Leave the target the way the next step can use it, then say why.

    A failed rebuild can leave the session's transaction aborted, and
    `diagnose` — the step that reads this — then got `InFailedSqlTransaction`
    on every query and diagnosed that. A fresh session, and the module
    states the attempt marked put back.
    """
    notes = []
    try:
        reopen_side(ctx, "target")
        reset = ctx.sheller.exec(ctx.sessions["target"], _RESET, write_key=ctx.write_keys["target"])
        if reset.get("error"):
            notes.append(f"could not reset module states: {_describe(reset)}")
        else:
            ctx.sheller.commit(ctx.sessions["target"], write_key=ctx.write_keys["target"])
            names = ast.literal_eval(reset.get("result") or "[]") or []
            if names:
                notes.append(f"reset the states of: {', '.join(names)}")
    except (ShellerError, ValueError, SyntaxError) as exc:
        notes.append(f"could not clean up the target session: {exc}")
    suffix = "\n\n" + "\n".join(notes) if notes else ""

    return StepResult(ok=False, output=f"{reason}{suffix}")


def _describe(executed: dict) -> str:
    error = executed.get("error")
    if isinstance(error, dict):
        head = f"{error.get('type') or 'Error'}: {error.get('message') or ''}".strip()
        trace = str(error.get("traceback") or "").strip().splitlines()[-_TAIL:]
        body = "\n".join(trace)
    else:
        head = str(error)
        body = ""
    log = [str(line) for line in (executed.get("stderr") or [])][-_TAIL:]
    parts = [head]
    if body:
        parts.append(body)
    if log:
        parts.append("log:\n" + "\n".join(log))

    return "\n".join(parts)
