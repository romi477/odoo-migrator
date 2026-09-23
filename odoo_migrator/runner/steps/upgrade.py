"""Install or upgrade the module on the target session, then commit."""

from __future__ import annotations

from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext, StepResult
from odoo_migrator.sheller.errors import ShellerError


def run(step: Step, ctx: StepContext) -> StepResult:
    session_id = ctx.sessions.get("new")
    write_key = ctx.write_keys.get("new")
    if not session_id or not write_key:

        return StepResult(ok=False, output="no target session")
    name = ctx.module.technical_name
    quoted = repr(name)
    code = (
        f"mod = env['ir.module.module'].search([('name', '=', {quoted})], limit=1)\n"
        "if not mod:\n"
        f"    raise RuntimeError('module not found: {name}')\n"
        "if mod.state == 'installed':\n"
        "    mod.button_immediate_upgrade()\n"
        "else:\n"
        "    mod.button_immediate_install()\n"
    )
    try:
        executed = ctx.sheller.exec(session_id, code, write_key=write_key)
    except ShellerError as exc:

        return StepResult(ok=False, output=str(exc))
    if executed.get("error") or executed.get("ok") is False:
        detail = executed.get("error") or executed

        return StepResult(ok=False, output=str(detail))
    try:
        ctx.sheller.commit(session_id, write_key=write_key)
    except ShellerError as exc:

        return StepResult(ok=False, output=str(exc))
    ctx.emit({"kind": "upgrade", "module": name, "job_id": ctx.job_id})

    return StepResult(ok=True, output=f"upgraded {name}")
