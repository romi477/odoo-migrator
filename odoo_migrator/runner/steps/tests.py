"""Run the module's own tests, Python and browser. No test classes is a flag,
not a failure."""

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
    # `retest` runs after `write_test`, in the lap that raised `no_tests`. The
    # browser suite already ran in it, and `write_test` writes Python only.
    retest = "no_tests" in ctx.lap_flags
    if retest:
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
    js = None if retest else _browser_files(catalogue)
    if js is not None:
        missing = _browser_tests_left_behind(ctx, js) or _outside_the_bundle(js)
        if missing:

            return StepResult(ok=False, flags={"tests_failed"}, output=missing)
    backend = _backend(ctx, name, catalogue.get("classes") or [])
    if not js:

        return backend
    browser = _browser(ctx, name, catalogue["js"], js)

    return StepResult(
        ok=backend.ok and browser.ok,
        flags=backend.flags | browser.flags,
        output=f"{backend.output}\n{browser.output}",
    )


def _backend(ctx: StepContext, name: str, classes: list) -> StepResult:
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

        return _expectations_changed(ctx, Path(directory) / "tests")

    return (
        f"The characterization tests did not reach the target: {len(missing)} of "
        f"{len(wanted)} test methods the source proved green are not in "
        f"{ctx.module.technical_name}/tests/ on the target: {', '.join(missing)}. "
        "Port them with the same method names, replacing a tests file of the "
        "same name that is already there."
    )


def _expectations_changed(ctx: StepContext, target_tests: Path) -> str:
    """Why the lap fails because the port changed what a test expects, or "".

    The tests arrived by name, and a name says nothing about the body: a port
    could turn an expected 'blue' into the 'green' its code returns, and the
    test the source proved went green on the target without proving anything.
    A `# migrator: target-differs` line explains the assertion under it and
    no other, so every value that went needs one of its own: a marked
    assertion the source did not have, or a marker where one was taken out.
    Explained, the change goes to the review as a finding; otherwise it fails
    the lap, and the failure lane puts the assertion back or explains it.
    """
    from odoo_migrator.source_git import characterization_dir
    from odoo_migrator.test_names import MARKER, test_expectations

    source_tests = characterization_dir(ctx.migration, ctx.module)
    if source_tests is None:

        return ""
    before = test_expectations(source_tests)
    after = test_expectations(target_tests)
    unexplained = []
    for key in sorted(before):
        other = _counterpart(key, before, after)
        if other is None:
            continue
        old, new = before[key], after[other]
        gone = old["expects"] - new["expects"]
        if not gone:
            continue
        came = new["expects"] - old["expects"]
        said = f"{key}: {', '.join(sorted(gone.elements()))} is gone"
        if came:
            said += f"; the target has {', '.join(sorted(came.elements()))}"
        explanations = sum((came & new["explained"]).values()) + new["free"]
        if explanations >= sum(gone.values()):
            finding = {
                "text": f"The port changed what {said}. It says why: {' '.join(new['marked'])}",
                "source": "tests: target-differs",
            }
            if finding not in ctx.findings:
                ctx.findings.append(finding)
        else:
            unexplained.append(said)
    if not unexplained:

        return ""

    return (
        f"The port changed what {len(unexplained)} of the characterization tests "
        f"expect, and did not say why. {'; '.join(unexplained)}. These are the "
        "values the source version proved. Put each assertion back as the source "
        "had it; a test that then fails is the port's to fix. Only when a rule of "
        "the target version changes the behaviour on purpose, keep the new "
        f"assertion and put `{MARKER} — <what changed, and the rule that says "
        "so>` on the line above it."
    )


# Where a module keeps its browser tests. Tours are run by a Python test and
# helpers by the tests that import them; neither is a suite of its own.
_JS_TESTS = "static/tests"
_NOT_SUITES = ("tours", "helpers")


def _browser_files(catalogue: dict) -> list[dict] | None:
    """The JS test files the target's listing names, or None when the daemon
    does not say (before 1.11.0): then nothing here is checked or run."""
    js = catalogue.get("js")
    if not isinstance(js, dict):

        return None

    return [entry for entry in js.get("files") or [] if isinstance(entry, dict)]


def _shipped_browser_tests(directory) -> list[str]:
    """The source module's JS test files, read on the host."""
    if directory is None:

        return []
    root = Path(directory) / _JS_TESTS
    if not root.is_dir():

        return []

    return sorted(
        path.relative_to(directory).as_posix()
        for path in root.rglob("*.js")
        if not set(path.relative_to(root).parts[:-1]) & set(_NOT_SUITES)
    )


def _browser_tests_left_behind(ctx: StepContext, files: list[dict]) -> str:
    """Why the lap fails before it runs anything, or "".

    A module's browser tests are its authors', and the migrator writes none of
    its own: the ones the source ships are the only ones there will be. A port
    that leaves them behind, or writes them where the target version does not
    look (`*.test.js` from 18.0), has nothing for the suite to run, and the
    step used to say nothing about it. Counted by the target's listing, not by
    name: across 18.0 a QUnit file is rewritten for Hoot and may be split.
    """
    from odoo_migrator.module_paths import module_root

    shipped = _shipped_browser_tests(module_root(ctx.migration, ctx.module, "source"))
    if not shipped or files:

        return ""

    return (
        f"The browser tests did not reach the target: the source ships "
        f"{len(shipped)} JS test file(s) under {_JS_TESTS}/ ({', '.join(shipped)}), "
        "and the target has none its version runs. Port them, as the rules for "
        "the target version say: from 18.0 a browser test is a Hoot `*.test.js` "
        "file under static/tests/, in the manifest's `web.assets_unit_tests` bundle."
    )


def _outside_the_bundle(files: list[dict]) -> str:
    """Why the lap fails on a test file no run loads, or "".

    A file outside the manifest's test bundle is not in the run, and the run
    passes over fewer tests than the module has. `in_manifest: null` is a
    manifest the daemon could not read as data: not known, so not failed.
    """
    outside = [str(entry.get("path")) for entry in files if entry.get("in_manifest") is False]
    if not outside:

        return ""

    return (
        f"{len(outside)} browser test file(s) are not in the module's test bundle, "
        f"so no run loads them: {', '.join(outside)}. Add them to the manifest's "
        "`assets`, under `web.assets_unit_tests` (`web.qunit_suite_tests` before 18.0)."
    )


def _browser(ctx: StepContext, name: str, js: dict, files: list[dict]) -> StepResult:
    """Run the module's browser suite the way the listing says to.

    A container that cannot run one (no Chromium, no `websocket-client`, or a
    version Sheller does not drive) is a finding, not a failure: the
    container is a person's to set up, and nothing in the port would fix it.
    It is never a pass either.
    """
    call = js.get("run") if isinstance(js.get("run"), dict) else None
    reason = js.get("reason") if js.get("available") is False else None
    if call is None and reason is None:
        reason = f"no Hoot runner for {js.get('runner') or 'this version'}"
    if reason is None:
        report, problem, _outlived = _run_one(
            ctx, str(call.get("test")), "browser", params=call.get("params") or None
        )
        hoot = report.get("hoot") if isinstance(report.get("hoot"), dict) else {}
        if hoot.get("skipped"):
            reason = hoot.get("reason") or "skipped"
        elif problem:

            return StepResult(
                ok=False,
                flags={"tests_failed"},
                output=f"{name} browser tests: {problem}{_hoot_failures(hoot)}{_tail(report)}",
            )
        else:

            return StepResult(
                ok=True, output=f"{name} browser tests: {hoot.get('passed') or 0} passed"
            )
    finding = {
        "text": (
            f"The browser tests of {name} did not run on the target ({reason}): "
            f"{len(files)} JS test file(s) are unproven."
        ),
        "source": "tests: browser",
    }
    if finding not in ctx.findings:
        ctx.findings.append(finding)

    return StepResult(ok=True, output=f"{name} browser tests did not run: {reason}")


def _hoot_failures(hoot: dict) -> str:
    """Each failed browser test with its failed assertions. They are logged
    deep in the run, and the log tail never reaches them."""
    reasons = hoot.get("reasons") if isinstance(hoot.get("reasons"), dict) else {}
    lines = [
        f"{test}: {reasons.get(test) or 'failed'}" for test in hoot.get("failed_tests") or []
    ]

    return "\n" + "\n".join(lines) if lines else ""


def _counterpart(key: str, before: dict, after: dict) -> str | None:
    """The target's entry for the source's `Class.method`: the same key, or,
    when the port renamed the class, the one method of that name on both
    sides. None when there is no telling which."""
    if key in after:

        return key
    name = before[key]["name"]
    there = [other for other, entry in after.items() if entry["name"] == name]
    here = [other for other, entry in before.items() if entry["name"] == name]
    if len(there) == 1 and len(here) == 1:

        return there[0]

    return None


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
    ctx: StepContext,
    spec: str,
    where: str,
    *,
    timeout: float = TEST_TIMEOUT,
    params: str | None = None,
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
        report = ctx.sheller.run_test(
            session_id, spec, write_key=write_key, timeout=timeout, params=params
        )
    except ShellerTimeout:
        report, outlived = {}, True
        problem = f"ran past {timeout:.0f}s" + _free(ctx)
    except ShellerError as exc:
        report, problem = {}, str(exc)
    else:
        _note_log_errors(ctx, report)
        problem = report_failure(report)
    said = f"{_count(report)} passed" if not problem else problem
    hoot = report.get("hoot") if isinstance(report.get("hoot"), dict) else {}
    if "passed" in hoot or "failed" in hoot:
        # One Python test drove the suite; the browser's counts are Hoot's.
        said += f" · hoot {hoot.get('passed') or 0} passed, {hoot.get('failed') or 0} failed"
    named = f"{spec} [{params}]" if params else spec
    _record(
        ctx, "run_test", "target", session_id, str(report.get("id") or ""),
        _elapsed(started), not problem, f"{where} {named} · {said}".lstrip(),
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
