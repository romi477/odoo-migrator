"""Run the module's own test classes. No classes is a flag, not a failure."""

from __future__ import annotations

from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext, StepResult
from odoo_migrator.runner.sessions import reopen_side
from odoo_migrator.sheller.errors import ShellerError

# Seconds one spec may run. Past the daemon's default of 300: a module's own
# test class on a real database is not a unit test.
TEST_TIMEOUT = 1800.0
# The end of the log is where the traceback and the summary are. `diagnose`
# reads this as `last_failure`; "1 failure" alone gives it nothing to work on.
_LOG_TAIL = 60

def run(step: Step, ctx: StepContext) -> StepResult:
    name = ctx.module.technical_name
    if not ctx.sessions.get("new"):

        return StepResult(ok=False, output="no target session")
    if "no_tests" in ctx.lap_flags:
        # A test was written after this session opened, and the process
        # imported the module's `tests` package when it did. Without a fresh
        # one the run finds the set that was there before and says nothing
        # about the case that was just added.
        try:
            reopen_side(ctx, "new")
        except ShellerError as exc:

            return StepResult(ok=False, output=f"could not reopen the target session: {exc}")
    session_id = ctx.sessions["new"]
    write_key = ctx.write_keys.get("new", "")
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
            report = ctx.sheller.run_test(
                session_id, spec, write_key=write_key, timeout=TEST_TIMEOUT
            )
        except ShellerError as exc:
            failed.append(f"{spec}: {exc}")
            continue
        _note_log_errors(ctx, report)
        problem = report_failure(report)
        if problem:
            failed.append(f"{spec}: {problem}{_tail(report)}")
    if failed:

        return StepResult(ok=False, flags={"tests_failed"}, output="\n".join(failed))

    return StepResult(ok=True, output=f"{len(classes)} test class(es) passed")


def report_failure(report: dict) -> str:
    """Why a run is red, or "" when it is green. The daemon's shape, read whole.

    A red run is not an error: the daemon answers `error: None` and puts the
    outcome in `test`. This used to look for `ok` and `status`, which the
    daemon never sends, so a failing test passed. A run of zero tests is red
    too — the spec matched nothing, and "nothing failed" is not a pass.
    """
    error = report.get("error")
    if error:
        if isinstance(error, dict):

            return f"{error.get('type') or 'Error'}: {error.get('message') or ''}".strip()

        return str(error)
    outcome = report.get("test")
    if not isinstance(outcome, dict):

        return "the daemon returned no test report"
    if outcome.get("success") is not True:

        return (
            f"{outcome.get('failures') or 0} failure(s), "
            f"{outcome.get('errors') or 0} error(s) "
            f"in {outcome.get('tests_run') or 0} test(s)"
        )
    if not outcome.get("tests_run"):

        return "ran 0 tests: the runner found nothing under this spec"

    return ""


def _tail(report: dict) -> str:
    lines = [str(line) for line in (report.get("stderr") or [])]
    stdout = str(report.get("stdout") or "").strip()
    if stdout:
        lines.extend(stdout.splitlines())
    if not lines:

        return ""

    return "\n" + "\n".join(lines[-_LOG_TAIL:])


def _note_log_errors(ctx: StepContext, report: dict) -> None:
    from odoo_migrator.runner.steps.verdict import note_log_errors

    note_log_errors(ctx, report.get("stderr") or [])


def _specs(module: str, classes: list) -> list[str]:
    """The specs the daemon already composed, not ones rebuilt from names.

    `list_tests` gives every class and every method a ready `spec`. This
    used to ignore them and interpolate `f"{module}.{class}.{method}"`,
    where `method` is a mapping — so Sheller was handed
    `mig_test_base.TestMigTestBase.{'name': 'test_x', 'spec': '...'}` and
    answered 400 `invalid_test_spec`. Every module with tests failed that
    way, and the run blamed the module.
    """
    specs: list[str] = []
    for item in classes:
        if isinstance(item, str):
            specs.append(item if "." in item else f"{module}.{item}")
            continue
        class_spec = _spec_of(item) or f"{module}.{item.get('name') or item.get('class') or ''}"
        methods = item.get("methods") or []
        if not methods:
            specs.append(class_spec)
            continue
        for method in methods:
            found = _method_spec(class_spec, method)
            if found:
                specs.append(found)

    return specs


def _spec_of(item) -> str:
    if not isinstance(item, dict):

        return ""

    return str(item.get("spec") or "").strip()


def _method_spec(class_spec: str, method) -> str:
    if isinstance(method, str):

        return f"{class_spec}.{method}"
    ready = _spec_of(method)
    if ready:

        return ready
    name = str((method or {}).get("name") or "").strip() if isinstance(method, dict) else ""

    return f"{class_spec}.{name}" if name else ""
