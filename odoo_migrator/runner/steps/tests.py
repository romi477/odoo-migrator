"""Run the module's own test classes. No classes is a flag, not a failure."""

from __future__ import annotations

from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext, StepResult
from odoo_migrator.sheller.errors import ShellerError


def run(step: Step, ctx: StepContext) -> StepResult:
    session_id = ctx.sessions.get("new")
    write_key = ctx.write_keys.get("new", "")
    name = ctx.module.technical_name
    if not session_id:

        return StepResult(ok=False, output="no target session")
    try:
        catalogue = ctx.sheller.list_tests(session_id, module=name, write_key=write_key)
    except ShellerError as exc:

        return StepResult(ok=False, flags={"tests_failed"}, output=str(exc))
    classes = catalogue.get("classes") or []
    if not classes:

        return StepResult(ok=True, flags={"no_tests"}, output=f"{name} declares no tests")
    failed: list[str] = []
    for spec in _specs(name, classes):
        try:
            report = ctx.sheller.run_test(session_id, spec, write_key=write_key)
        except ShellerError as exc:
            failed.append(f"{spec}: {exc}")
            continue
        if report.get("ok") is False or report.get("error") or report.get("status") == "fail":
            failed.append(f"{spec}: {report.get('error') or report.get('status') or 'failed'}")
    if failed:

        return StepResult(ok=False, flags={"tests_failed"}, output="\n".join(failed))

    return StepResult(ok=True, output=f"{len(classes)} test class(es) passed")


def _specs(module: str, classes: list) -> list[str]:
    specs: list[str] = []
    for item in classes:
        if isinstance(item, str):
            specs.append(item if "." in item else f"{module}.{item}")
            continue
        class_name = item.get("name") or item.get("class") or ""
        methods = item.get("methods") or []
        if methods:
            specs.extend(f"{module}.{class_name}.{method}" for method in methods)
        elif class_name:
            specs.append(f"{module}.{class_name}")

    return specs
