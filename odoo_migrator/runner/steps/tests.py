"""Run the module's own tests. No test classes is a flag, not a failure."""

from __future__ import annotations

import time
from pathlib import Path

from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext, StepResult, check_stop
from odoo_migrator.runner.sessions import ensure_usable, reopen_side
from odoo_migrator.runner.tools import _elapsed, _record
from odoo_migrator.sheller.errors import ShellerError, ShellerTimeout

# Seconds one spec may run. Past the daemon's default of 300: a module's own
# test class on a real database is not a unit test.
TEST_TIMEOUT = 1800.0
# A whole module: the most the daemon allows.
MODULE_TEST_TIMEOUT = 3600.0
# Failed tests run again one by one, for the log that says why. Each sets its
# class up again; past this many the rest are named and left to the agent.
RERUN_LIMIT = 10
# The first Sheller that runs a bare module and lists what failed.
MODULE_RUNS_SINCE = (1, 8, 3)
# The end of the log is where the traceback and the summary are. `diagnose`
# reads this as `last_failure`; "1 failure" alone gives it nothing to work on.
_LOG_TAIL = 60


def run(step: Step, ctx: StepContext) -> StepResult:
    name = ctx.module.technical_name
    if not ctx.sessions.get("target"):

        return StepResult(ok=False, output="no target session")
    missing = _tests_left_behind(ctx)
    if missing:

        return StepResult(ok=False, flags={"tests_failed"}, output=missing)
    if "no_tests" in ctx.lap_flags:
        # A test was written after this session opened, and the process
        # imported the module's `tests` package when it did. Without a fresh
        # one the run finds the set that was there before and says nothing
        # about the case that was just added.
        try:
            reopen_side(ctx, "target")
        except ShellerError as exc:

            return StepResult(ok=False, output=f"could not reopen the target session: {exc}")
    try:
        catalogue = ctx.sheller.list_tests(
            ctx.sessions["target"], module=name, write_key=ctx.write_keys.get("target", "")
        )
    except ShellerError as exc:

        return StepResult(ok=False, flags={"tests_failed"}, output=str(exc))
    classes = catalogue.get("classes") or []
    if not classes:

        return StepResult(ok=True, flags={"no_tests"}, output=f"{name} declares no tests")
    if _runs_modules(ctx):
        result = _by_module(ctx, name)
        if result is not None:

            return result

    return _by_class(ctx, name, classes)


def _tests_left_behind(ctx: StepContext) -> str:
    """Why the lap fails before it runs anything, or "".

    The tests the migrator proved green on the source are the oracle for the
    port, and a port that did not write them leaves the step to run whatever
    the target already had: the first live run passed four old tests while the
    seven that mattered were not there. Checked by method name, so a port may
    reshape a body and may not drop the test.
    """
    from odoo_migrator.module_paths import module_root
    from odoo_migrator.source_git import characterization_names
    from odoo_migrator.test_names import test_method_names

    wanted = characterization_names(ctx.migration, ctx.module)
    if not wanted:

        return ""
    directory = module_root(ctx.migration, ctx.module, "target")
    present = test_method_names(Path(directory) / "tests") if directory else set()
    missing = [name for name in wanted if name not in present]
    if not missing:

        return ""

    return (
        f"The characterization tests did not reach the target: {len(missing)} of "
        f"{len(wanted)} test methods the source proved green are not in "
        f"{ctx.module.technical_name}/tests/ on the target: {', '.join(missing)}. "
        "Port them with the same method names, replacing a tests file of the "
        "same name that is already there."
    )


def _runs_modules(ctx: StepContext) -> bool:
    """Whether this daemon takes a bare module. One before 1.8.3 answers
    `invalid_test_spec`, and lists nothing as `failed`."""
    try:
        version = str(ctx.sheller.health().get("version") or "")
    except ShellerError:

        return False

    return tuple(int(piece) for piece in version.split(".") if piece.isdigit()) >= MODULE_RUNS_SINCE


def _by_module(ctx: StepContext, name: str) -> StepResult | None:
    """One run of the module's standard tests, then each failed spec alone.

    A module run is one registry load and one set-up per class, and it
    answers with `failed`: every failing test as a spec. Run alone, a spec
    answers with its log tail, which is where `diagnose` finds the reason.
    None when the run found no tests: a module run leaves out `-standard`
    and `external` classes, and when those are all a module has, its
    classes on disk are still its tests.
    """
    report, problem, outlived = _run_one(ctx, name, "", timeout=MODULE_TEST_TIMEOUT)
    outcome = report.get("test") if isinstance(report.get("test"), dict) else {}
    if not report.get("error") and outcome and not outcome.get("tests_run"):

        return None
    if not problem:

        return StepResult(ok=True, output=f"{name} ({_count(report)}) passed")
    listed = [
        entry for entry in report.get("failed") or []
        if isinstance(entry, dict) and entry.get("test")
    ]
    if outlived or not listed:

        return StepResult(
            ok=False, flags={"tests_failed"}, output=f"{name}: {problem}{_tail(report)}"
        )
    failed: list[str] = []
    reruns = [entry for entry in listed if entry["test"] != name]
    if len(reruns) < len(listed):
        # setUpModule: no tag names a file, and the module would fail again.
        failed.append(f"{name}: a test file failed to set up{_tail(report)}")
    stopped = False
    for index, entry in enumerate(reruns, start=1):
        spec, kind = str(entry["test"]), entry.get("kind") or "failure"
        if stopped or index > RERUN_LIMIT:
            failed.append(f"{spec}: {kind} in the module run, not run again")
            continue
        report, problem, outlived = _run_one(ctx, spec, f"{index}/{len(reruns)}")
        if problem:
            failed.append(f"{spec}: {problem}{_tail(report)}")
        else:
            failed.append(f"{spec}: red in the module run, green alone")
        # Each of the rest can take as long.
        stopped = outlived

    return StepResult(ok=False, flags={"tests_failed"}, output="\n".join(failed))


def _by_class(ctx: StepContext, name: str, classes: list) -> StepResult:
    """A daemon before 1.8.3, or a module with no standard tests."""
    # One run per class, not per method. Every run sets its class up again
    # — the integration, its crons, its users — and per method that was
    # seventy runs of seven seconds, ten minutes for a green module. A red
    # class is run again by method, so the failure still names its test.
    plan = _plan(name, classes)
    failed: list[str] = []
    covered: list[str] = []
    for index, (class_spec, methods) in enumerate(plan, start=1):
        where = f"{index}/{len(plan)}"
        report, problem, outlived = _run_one(ctx, class_spec, where)
        if not problem:
            covered.append(f"{class_spec} ({_count(report)})")
            continue
        if outlived:
            # Not again method by method: each of those can take as long.
            failed.append(f"{class_spec}: {problem}")
            continue
        if len(methods) < 2:
            failed.append(f"{methods[0] if methods else class_spec}: {problem}{_tail(report)}")
            continue
        named = False
        for method_spec in methods:
            report, problem, outlived = _run_one(ctx, method_spec, where)
            if problem:
                named = True
                failed.append(f"{method_spec}: {problem}{_tail(report)}")
            if outlived:
                break
        if not named:
            # Red as a class, green one by one: an order the methods depend
            # on, or a set-up that breaks only when they share it.
            failed.append(f"{class_spec}: red as a class, green method by method")
    if failed:

        return StepResult(ok=False, flags={"tests_failed"}, output="\n".join(failed))
    # Which specs, not how many. "1 test class(es) passed" is the same
    # sentence whether the run covered the module's own case or a class that
    # came in with a dependency, and the outcome is the one line anyone reads
    # about a green run.

    return StepResult(
        ok=True,
        output=f"{len(covered)} test class(es) passed: {', '.join(covered)}"
        if covered
        else "nothing to run",
    )


def _run_one(
    ctx: StepContext, spec: str, where: str, *, timeout: float = TEST_TIMEOUT
) -> tuple[dict, str, bool]:
    """Run one spec, and put it in the run log as it ends: a step that said
    nothing between "started" and its end read as a hang.

    Returns the report, why it is red ("" when green), and whether the run
    outlived the call. A Stop is heard here, between two runs. Read before
    each step only, it waited for every method of a red class to run again.
    """
    check_stop(ctx, "during tests")
    # Read for every run: freeing a session after a timeout can open a new one.
    session_id = ctx.sessions.get("target", "")
    write_key = ctx.write_keys.get("target", "")
    started = time.perf_counter()
    outlived = False
    try:
        report = ctx.sheller.run_test(session_id, spec, write_key=write_key, timeout=timeout)
    except ShellerTimeout:
        report, outlived = {}, True
        problem = f"ran past {timeout:.0f}s" + _free(ctx)
    except ShellerError as exc:
        report, problem = {}, str(exc)
    else:
        _note_log_errors(ctx, report)
        problem = report_failure(report)
    said = f"{_count(report)} passed" if not problem else problem
    _record(
        ctx, "run_test", "target", session_id, str(report.get("id") or ""),
        _elapsed(started), not problem, f"{where} {spec} · {said}".lstrip(),
    )

    return report, problem, outlived


def _free(ctx: StepContext) -> str:
    """A run past the call is still going in the session. Interrupt it, or
    open a fresh one, before the next run: left alone, every run after it
    answered `session_busy`, and the step blamed the tests for each."""
    try:
        done = ensure_usable(ctx, "target")
    except ShellerError as exc:

        return f"; the session could not be freed: {exc}"

    return f"; {done}" if done else ""


def _count(report: dict) -> str:
    outcome = report.get("test") if isinstance(report.get("test"), dict) else {}
    ran = outcome.get("tests_run") or 0

    return f"{ran} test{'s' if ran != 1 else ''}"


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


def _plan(module: str, classes: list) -> list[tuple[str, list[str]]]:
    """Each class's spec with its methods' specs: the ones the daemon composed,
    not ones rebuilt from names.

    `list_tests` gives every class and every method a ready `spec`. This
    used to ignore them and interpolate `f"{module}.{class}.{method}"`,
    where `method` is a mapping — so Sheller was handed
    `mig_test_base.TestMigTestBase.{'name': 'test_x', 'spec': '...'}` and
    answered 400 `invalid_test_spec`. Every module with tests failed that
    way, and the run blamed the module.
    """
    plan: list[tuple[str, list[str]]] = []
    for item in classes:
        if isinstance(item, str):
            plan.append((item if "." in item else f"{module}.{item}", []))
            continue
        class_spec = _spec_of(item) or f"{module}.{item.get('name') or item.get('class') or ''}"
        methods = [
            found
            for found in (_method_spec(class_spec, method) for method in item.get("methods") or [])
            if found
        ]
        plan.append((class_spec, methods))

    return plan


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
