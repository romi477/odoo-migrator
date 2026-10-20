"""Deterministic steps: lint, the test catalogue, and the verdict."""

import pytest

from odoo_migrator.db import database, init_db
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Migration, Module
from odoo_migrator.runner.cartridge import Step, load_cartridge
from odoo_migrator.runner.context import StepContext
from odoo_migrator.runner.steps import lint, tests, verdict
from odoo_migrator.sheller import FakeShellerClient

_CARTRIDGE = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-sonnet-5-5}
pipeline:
  - step: verdict
on_failure:
  pipeline: []
  retry:
    resume_at: verdict
"""


@pytest.fixture
def sheller_fake(request):
    # A daemon that runs a whole module, unless a test asks for an older one.

    return FakeShellerClient(version=getattr(request, "param", MODULE_RUNS))


# Sheller 1.8.3 runs a bare module and lists what failed. One before it
# takes a class or a method only, and the step runs class by class there.
# 1.8.3 to 1.10 also refuse the target side's open with commit, so the
# daemon that runs a module here is the one that lets the target commit.
MODULE_RUNS = "1.11.0"
_older_daemon = pytest.mark.parametrize("sheller_fake", ["1.8.2"], indirect=True)


@pytest.fixture
def step_context(tmp_path, sheller_fake):
    init_db(tmp_path / "steps.sqlite")

    def build(*, new_module_path=None, strictness="basic", technical_name="sale_custom"):
        module_root = new_module_path or (tmp_path / "new" / technical_name)
        module_root.mkdir(parents=True, exist_ok=True)
        project = module_root.parent
        created = create_migration(
            name="Acme",
            accent="violet",
            source_version="17.0",
            target_version="18.0",
            old_project_path=str(tmp_path / "old"),
            new_project_path=str(project),
        )
        with database.connection_context():
            migration = Migration.get_by_id(created["id"])
            migration.strictness_default = strictness
            migration.save()
            module = Module.create(
                migration=migration,
                technical_name=technical_name,
                new_path=str(module_root),
            )
        opened = sheller_fake.open_session(
            container="odoo-new",
            database="acme",
            odoo_bin="/opt/odoo/odoo-bin",
            owner={"kind": "agent"},
            allow_commit=True,
        )

        return StepContext(
            job_id="job-1",
            attempt=1,
            migration=migration,
            module=module,
            cartridge=load_cartridge(_CARTRIDGE),
            sheller=sheller_fake,
            sessions={"source": opened["id"], "target": opened["id"]},
            write_keys={"source": opened["write_key"], "target": opened["write_key"]},
            emit=lambda _event: None,
        )

    return build


def test_lint_fails_a_manifest_that_is_not_python(tmp_path, step_context):
    # Not tmp_path itself: its parent would be the new project root, with the
    # old project inside it, and overlapping roots are refused.
    module_dir = tmp_path / "new" / "flat"
    module_dir.mkdir(parents=True)
    (module_dir / "__manifest__.py").write_text("{'name': 'x',", encoding="utf-8")
    ctx = step_context(new_module_path=module_dir)

    result = lint.run(Step(kind="lint"), ctx)

    assert result.ok is False
    assert "lint_failed" in result.flags
    assert "__manifest__.py" in result.output


def test_tests_raises_no_tests_when_the_module_declares_none(step_context, sheller_fake):
    sheller_fake.tests_for("sale_custom", [])
    ctx = step_context()

    result = tests.run(Step(kind="tests"), ctx)

    assert result.ok is True
    assert result.flags == {"no_tests"}


def test_verdict_is_basic_by_default_and_reads_the_migration(step_context):
    ctx = step_context(strictness="strict")

    result = verdict.run(Step(kind="verdict"), ctx)

    assert result.ok is False  # strict wants a probe answer, and there is none


@_older_daemon
def test_a_module_with_tests_runs_the_specs_the_daemon_gave(step_context, sheller_fake):
    """The runner used to rebuild specs from names and interpolate a mapping,
    so Sheller was handed `module.Class.{'name': ..., 'spec': ...}` and
    answered 400 invalid_test_spec. Every module with tests failed that way,
    and the run blamed the module."""
    sheller_fake.tests_for(
        "sale_custom",
        sheller_fake.catalogue(
            "sale_custom", {"TestSaleCustom": ["test_one", "test_two"]}
        ),
    )
    ctx = step_context()

    result = tests.run(Step(kind="tests"), ctx)

    ran = [row["test"] for row in sheller_fake.tests_run]

    assert ran == ["sale_custom.TestSaleCustom"]
    assert all("{" not in spec for spec in ran)
    assert result.ok is True
    assert "sale_custom.TestSaleCustom" in result.output


@_older_daemon
def test_a_green_class_is_one_run_not_one_per_method(step_context, sheller_fake):
    """One run per method set the class up again for every test: seventy
    methods of seven seconds each, ten minutes with nothing in the log."""
    sheller_fake.tests_for(
        "sale_custom",
        sheller_fake.catalogue(
            "sale_custom",
            {"TestA": ["test_one", "test_two", "test_three"], "TestB": ["test_four"]},
        ),
    )

    result = tests.run(Step(kind="tests"), step_context())

    assert [row["test"] for row in sheller_fake.tests_run] == [
        "sale_custom.TestA",
        "sale_custom.TestB",
    ]
    assert result.ok is True


@_older_daemon
def test_a_red_class_is_run_again_by_method_to_name_the_failing_test(
    step_context, sheller_fake
):
    sheller_fake.tests_for(
        "sale_custom",
        sheller_fake.catalogue(
            "sale_custom", {"TestA": ["test_one", "test_two"], "TestB": ["test_three"]}
        ),
    )
    sheller_fake.test_outcomes["sale_custom.TestA.test_two"] = {
        "success": False,
        "failures": 1,
        "stderr": ["AssertionError: 1 != 2"],
    }

    result = tests.run(Step(kind="tests"), step_context())

    assert [row["test"] for row in sheller_fake.tests_run] == [
        "sale_custom.TestA",
        "sale_custom.TestA.test_one",
        "sale_custom.TestA.test_two",
        "sale_custom.TestB",
    ]
    assert result.ok is False
    assert "sale_custom.TestA.test_two: 1 failure(s)" in result.output
    assert "sale_custom.TestA.test_one" not in result.output


@_older_daemon
def test_each_run_is_a_line_in_the_run_log(step_context, sheller_fake):
    """A tests step said nothing between "started" and its end, so a long
    one read as a hang."""
    sheller_fake.tests_for(
        "sale_custom",
        sheller_fake.catalogue("sale_custom", {"TestA": ["test_one"], "TestB": ["test_two"]}),
    )
    recorded = []
    ctx = step_context()
    ctx.record_tool = lambda *args: recorded.append(args)

    tests.run(Step(kind="tests"), ctx)

    assert [(call[0], call[1], call[5]) for call in recorded] == [
        ("run_test", "target", True),
        ("run_test", "target", True),
    ]
    assert recorded[0][6].startswith("1/2 sale_custom.TestA")
    assert recorded[1][6].startswith("2/2 sale_custom.TestB")


@_older_daemon
def test_a_class_with_no_methods_runs_as_the_class(step_context, sheller_fake):
    sheller_fake.tests_for(
        "sale_custom", sheller_fake.catalogue("sale_custom", {"TestSaleCustom": []})
    )

    tests.run(Step(kind="tests"), step_context())

    assert [row["test"] for row in sheller_fake.tests_run] == ["sale_custom.TestSaleCustom"]


def test_the_double_refuses_a_shape_the_daemon_never_returns(sheller_fake):
    """A double that accepts a looser shape hides the bug it just caught."""
    from odoo_migrator.sheller.errors import ShellerError

    with pytest.raises(ShellerError, match="as the daemon returns it"):
        sheller_fake.tests_for("sale_custom", [{"name": "TestSaleCustom"}])
    with pytest.raises(ShellerError, match="as the daemon returns it"):
        sheller_fake.tests_for(
            "sale_custom",
            [
                {
                    "name": "TestSaleCustom",
                    "spec": "sale_custom.TestSaleCustom",
                    "methods": [{"name": "test_one"}],
                }
            ],
        )


@_older_daemon
def test_a_stop_ends_the_tests_between_two_runs(step_context, sheller_fake):
    """A red class of seventy methods is seventy more runs. Stop was read
    before each step only, so it waited for every one of them."""
    from odoo_migrator.runner.context import Stopped

    sheller_fake.tests_for(
        "sale_custom",
        sheller_fake.catalogue("sale_custom", {"TestA": ["test_one"], "TestB": ["test_two"]}),
    )
    ctx = step_context()
    ctx.stop_asked = lambda: len(sheller_fake.tests_run) >= 1

    with pytest.raises(Stopped, match="during tests"):
        tests.run(Step(kind="tests"), ctx)

    assert [row["test"] for row in sheller_fake.tests_run] == ["sale_custom.TestA"]


def _with_target(ctx):
    # What the runner records, so a session can be freed or opened again.
    ctx.session_targets = {
        "target": {
            "container": "odoo-new",
            "database": "acme",
            "odoo_bin": "/opt/odoo/odoo-bin",
            "owner": {"kind": "agent"},
        }
    }

    return ctx


@_older_daemon
def test_a_run_that_outlives_the_call_does_not_leave_the_rest_busy(step_context, sheller_fake):
    """A class past the timeout kept running in the session, and every run
    after it answered `session_busy`: a page of lines blamed on the tests,
    and `diagnose` worked on those."""
    sheller_fake.tests_for(
        "sale_custom",
        sheller_fake.catalogue(
            "sale_custom", {"TestSlow": ["test_a", "test_b"], "TestFast": ["test_c"]}
        ),
    )
    sheller_fake.test_hangs.add("sale_custom.TestSlow")

    result = tests.run(Step(kind="tests"), _with_target(step_context()))

    assert result.ok is False
    assert "sale_custom.TestSlow: ran past" in result.output
    assert "session_busy" not in result.output
    assert sheller_fake.interrupts
    # Not again method by method: each of those can take as long.
    assert [row["test"] for row in sheller_fake.tests_run] == [
        "sale_custom.TestSlow",
        "sale_custom.TestFast",
    ]


@_older_daemon
def test_a_method_that_outlives_the_call_ends_the_reruns_of_its_class(
    step_context, sheller_fake
):
    sheller_fake.tests_for(
        "sale_custom",
        sheller_fake.catalogue("sale_custom", {"TestA": ["test_one", "test_two"]}),
    )
    sheller_fake.test_outcomes["sale_custom.TestA"] = {"success": False, "failures": 1}
    sheller_fake.test_hangs.add("sale_custom.TestA.test_one")

    result = tests.run(Step(kind="tests"), _with_target(step_context()))

    assert result.ok is False
    assert "sale_custom.TestA.test_one: ran past" in result.output
    assert [row["test"] for row in sheller_fake.tests_run] == [
        "sale_custom.TestA",
        "sale_custom.TestA.test_one",
    ]


def _two_classes(sheller_fake):
    sheller_fake.tests_for(
        "sale_custom",
        sheller_fake.catalogue(
            "sale_custom", {"TestA": ["test_one", "test_two"], "TestB": ["test_three"]}
        ),
    )


def test_the_double_runs_a_whole_module_and_lists_what_failed(step_context, sheller_fake):
    """Sheller 1.8.3: a bare module runs every class in it, and every answer
    carries `failed`, each entry a spec to pass straight back."""
    _two_classes(sheller_fake)
    sheller_fake.test_outcomes["sale_custom.TestA.test_two"] = {"success": False, "failures": 1}
    ctx = step_context()

    report = sheller_fake.run_test(
        ctx.sessions["target"], "sale_custom", write_key=ctx.write_keys["target"]
    )

    assert report["test"]["tests_run"] == 3
    assert report["test"]["success"] is False
    assert report["failed"] == [{"test": "sale_custom.TestA.test_two", "kind": "failure"}]


@_older_daemon
def test_the_double_before_module_runs_refuses_a_bare_module(step_context, sheller_fake):
    from odoo_migrator.sheller.errors import ShellerError

    _two_classes(sheller_fake)
    ctx = step_context()

    with pytest.raises(ShellerError, match="invalid_test_spec"):
        sheller_fake.run_test(ctx.sessions["target"], "sale_custom", write_key=ctx.write_keys["target"])
    report = sheller_fake.run_test(
        ctx.sessions["target"], "sale_custom.TestA", write_key=ctx.write_keys["target"]
    )
    assert "failed" not in report


def test_a_green_module_is_one_run(step_context, sheller_fake):
    """Every class used to be its own run, each loading the registry and
    setting its class up again. One module run covers them in one go."""
    _two_classes(sheller_fake)

    result = tests.run(Step(kind="tests"), step_context())

    assert [row["test"] for row in sheller_fake.tests_run] == ["sale_custom"]
    assert result.ok is True
    assert "sale_custom (3 tests)" in result.output


def test_a_module_run_gets_the_daemons_longest_ceiling(step_context, sheller_fake):
    _two_classes(sheller_fake)

    tests.run(Step(kind="tests"), step_context())

    assert sheller_fake.tests_run[0]["timeout"] == tests.MODULE_TEST_TIMEOUT == 3600.0


def test_a_red_module_runs_again_only_what_failed(step_context, sheller_fake):
    """A module run answers with counts and `failed`; the reason is in the
    log of each failed spec run on its own, which is what `diagnose` reads."""
    _two_classes(sheller_fake)
    sheller_fake.test_outcomes["sale_custom.TestA.test_two"] = {
        "success": False,
        "failures": 1,
        "stderr": ["AssertionError: 1 != 2"],
    }

    result = tests.run(Step(kind="tests"), step_context())

    assert [row["test"] for row in sheller_fake.tests_run] == [
        "sale_custom",
        "sale_custom.TestA.test_two",
    ]
    assert result.ok is False
    assert result.flags == {"tests_failed"}
    assert "sale_custom.TestA.test_two: 1 failure(s)" in result.output
    assert "AssertionError: 1 != 2" in result.output
    assert "sale_custom.TestA.test_one" not in result.output


def test_a_test_red_in_the_module_and_green_alone_says_so(step_context, sheller_fake):
    _two_classes(sheller_fake)
    sheller_fake.test_outcomes["sale_custom"] = {
        "success": False,
        "failures": 1,
        "failed": [{"test": "sale_custom.TestA.test_one", "kind": "failure"}],
    }

    result = tests.run(Step(kind="tests"), step_context())

    assert result.ok is False
    assert "sale_custom.TestA.test_one: red in the module run, green alone" in result.output


def test_a_test_file_that_failed_to_set_up_is_not_run_again(step_context, sheller_fake):
    """setUpModule failing is listed as the whole module: no tag names a file,
    and running the module again would fail the same way."""
    _two_classes(sheller_fake)
    sheller_fake.test_outcomes["sale_custom"] = {
        "success": False,
        "errors": 1,
        "failed": [{"test": "sale_custom", "kind": "error"}],
        "stderr": ["ERROR: setUpModule (odoo.addons.sale_custom.tests.test_a)"],
    }

    result = tests.run(Step(kind="tests"), step_context())

    assert [row["test"] for row in sheller_fake.tests_run] == ["sale_custom"]
    assert result.ok is False
    assert "sale_custom: a test file failed to set up" in result.output
    assert "setUpModule" in result.output


def test_a_module_run_past_its_timeout_frees_the_session(step_context, sheller_fake):
    _two_classes(sheller_fake)
    sheller_fake.test_hangs.add("sale_custom")

    result = tests.run(Step(kind="tests"), _with_target(step_context()))

    assert result.ok is False
    assert "sale_custom: ran past 3600s" in result.output
    assert sheller_fake.interrupts
    assert [row["test"] for row in sheller_fake.tests_run] == ["sale_custom"]


def test_a_module_with_no_standard_tests_runs_class_by_class(step_context, sheller_fake):
    """A module run leaves out `-standard` and `external` classes. When that
    is all a module has, the run finds nothing, and the classes on disk are
    still the module's tests."""
    _two_classes(sheller_fake)
    sheller_fake.test_outcomes["sale_custom"] = {"tests_run": 0}

    result = tests.run(Step(kind="tests"), step_context())

    assert [row["test"] for row in sheller_fake.tests_run] == [
        "sale_custom",
        "sale_custom.TestA",
        "sale_custom.TestB",
    ]
    assert result.ok is True


def test_the_reruns_stop_at_a_limit(step_context, sheller_fake):
    """Seventy red tests are seventy runs that each set their class up. The
    first few name the reason; the rest are listed, and the agent can run
    any of them."""
    methods = [f"test_{index:02d}" for index in range(tests.RERUN_LIMIT + 3)]
    sheller_fake.tests_for(
        "sale_custom", sheller_fake.catalogue("sale_custom", {"TestA": methods})
    )
    for method in methods:
        sheller_fake.test_outcomes[f"sale_custom.TestA.{method}"] = {
            "success": False,
            "failures": 1,
        }

    result = tests.run(Step(kind="tests"), step_context())

    assert len(sheller_fake.tests_run) == 1 + tests.RERUN_LIMIT
    last = f"sale_custom.TestA.{methods[-1]}"
    assert f"{last}: failure in the module run, not run again" in result.output


def test_a_stop_ends_the_reruns(step_context, sheller_fake):
    from odoo_migrator.runner.context import Stopped

    _two_classes(sheller_fake)
    sheller_fake.test_outcomes["sale_custom.TestA.test_two"] = {"success": False, "failures": 1}
    ctx = step_context()
    ctx.stop_asked = lambda: len(sheller_fake.tests_run) >= 1

    with pytest.raises(Stopped, match="during tests"):
        tests.run(Step(kind="tests"), ctx)

    assert [row["test"] for row in sheller_fake.tests_run] == ["sale_custom"]


# The browser suite: the call the target's listing hands back, run after the
# module's Python tests.
SUITE = "web.WebSuite.test_unit_desktop"


def _browser_tests(tmp_path, sheller_fake, *, files=None, classes=True, ship=True):
    """A module whose source ships one Hoot file, and a target listing that
    names `files` (the one file, in the bundle, by default)."""
    source = tmp_path / "old" / "sale_custom"
    (source / "static" / "tests").mkdir(parents=True, exist_ok=True)
    (source / "__manifest__.py").write_text("{'name': 'Sale custom'}", encoding="utf-8")
    if ship:
        (source / "static" / "tests" / "badge.test.js").write_text("", encoding="utf-8")
    sheller_fake.tests_for(
        "sale_custom",
        sheller_fake.catalogue("sale_custom", {"TestA": ["test_one"]}) if classes else [],
    )
    if files is None:
        files = [{"path": "static/tests/badge.test.js", "in_manifest": True, "tests": []}]
    sheller_fake.js_listing["sale_custom"] = {
        "runner": "hoot",
        "files": files,
        "run": {"test": SUITE, "params": "@sale_custom"} if files else None,
    }


def test_the_browser_suite_runs_after_the_python_tests(tmp_path, step_context, sheller_fake):
    """It used to be the agent's to remember. The step ran Python only, and a
    module's JS tests were never run by anything the run recorded."""
    _browser_tests(tmp_path, sheller_fake)
    sheller_fake.hoot_answers[SUITE] = {"passed": 5, "failed": 0, "failed_tests": []}

    result = tests.run(Step(kind="tests"), step_context())

    assert [(row["test"], row["params"]) for row in sheller_fake.tests_run] == [
        ("sale_custom", None),
        (SUITE, "@sale_custom"),
    ]
    assert result.ok is True
    assert "sale_custom browser tests: 5 passed" in result.output


def test_a_red_browser_test_fails_the_lap_with_its_assertions(
    tmp_path, step_context, sheller_fake
):
    _browser_tests(tmp_path, sheller_fake)
    sheller_fake.test_outcomes[SUITE] = {"success": False, "failures": 1}
    sheller_fake.hoot_answers[SUITE] = {
        "passed": 4,
        "failed": 1,
        "failed_tests": ["@sale_custom/badge/renders the badge"],
        "reasons": {"@sale_custom/badge/renders the badge": "expected 'blue', got 'green'"},
    }

    result = tests.run(Step(kind="tests"), step_context())

    assert result.ok is False
    assert result.flags == {"tests_failed"}
    assert "@sale_custom/badge/renders the badge: expected 'blue', got 'green'" in result.output


def test_a_skipped_browser_suite_is_a_finding_and_never_a_pass(
    tmp_path, step_context, sheller_fake
):
    """No Chromium in the target container is a person's to fix, and nothing
    the port does would. It does not fail the lap; it is not a pass either."""
    _browser_tests(tmp_path, sheller_fake)
    sheller_fake.hoot_answers[SUITE] = {"skipped": True, "reason": "chrome_not_found"}
    ctx = step_context()

    result = tests.run(Step(kind="tests"), ctx)

    assert result.ok is True
    assert "browser tests did not run: chrome_not_found" in result.output
    assert "passed" not in result.output.splitlines()[-1]
    assert [finding["source"] for finding in ctx.findings] == ["tests: browser"]


def test_a_container_that_cannot_run_a_browser_is_not_asked_to(
    tmp_path, step_context, sheller_fake
):
    _browser_tests(tmp_path, sheller_fake)
    sheller_fake.js_tests = {"available": False, "reason": "websocket_client_missing"}
    ctx = step_context()

    result = tests.run(Step(kind="tests"), ctx)

    assert [row["test"] for row in sheller_fake.tests_run] == ["sale_custom"]
    assert result.ok is True
    assert "websocket_client_missing" in ctx.findings[0]["text"]


def test_a_test_file_outside_the_bundle_fails_the_lap_before_any_run(
    tmp_path, step_context, sheller_fake
):
    """A file no bundle loads is a test the run passes over in silence."""
    _browser_tests(
        tmp_path,
        sheller_fake,
        files=[{"path": "static/tests/badge.test.js", "in_manifest": False, "tests": []}],
    )

    result = tests.run(Step(kind="tests"), step_context())

    assert sheller_fake.tests_run == []
    assert result.ok is False
    assert "static/tests/badge.test.js" in result.output
    assert "web.assets_unit_tests" in result.output


def test_browser_tests_the_port_left_behind_fail_the_lap(tmp_path, step_context, sheller_fake):
    """The source ships them and the target has none its version runs: left
    out, or kept as QUnit files that 18.0 does not look for."""
    _browser_tests(tmp_path, sheller_fake, files=[])

    result = tests.run(Step(kind="tests"), step_context())

    assert sheller_fake.tests_run == []
    assert result.ok is False
    assert result.flags == {"tests_failed"}
    assert "did not reach the target" in result.output
    assert "static/tests/badge.test.js" in result.output


def test_tours_and_helpers_are_not_browser_tests_left_behind(
    tmp_path, step_context, sheller_fake
):
    _browser_tests(tmp_path, sheller_fake, files=[], ship=False)
    for kept in ("tours/sale_tour.js", "helpers/mock.js"):
        path = tmp_path / "old" / "sale_custom" / "static" / "tests" / kept
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("", encoding="utf-8")

    result = tests.run(Step(kind="tests"), step_context())

    assert result.ok is True
    assert [row["test"] for row in sheller_fake.tests_run] == ["sale_custom"]


def test_a_module_with_browser_tests_only_still_raises_no_tests(
    tmp_path, step_context, sheller_fake
):
    """`no_tests` is about Python: `write_test` writes Python, and the migrator
    writes no browser tests of its own. The suite runs all the same."""
    _browser_tests(tmp_path, sheller_fake, classes=False)

    result = tests.run(Step(kind="tests"), step_context())

    assert result.flags == {"no_tests"}
    assert [row["test"] for row in sheller_fake.tests_run] == [SUITE]


def test_retest_does_not_run_the_browser_suite_a_second_time(
    tmp_path, step_context, sheller_fake
):
    _browser_tests(tmp_path, sheller_fake, classes=False)
    ctx = _with_target(step_context())
    ctx.lap_flags = {"no_tests"}

    tests.run(Step(kind="tests", id="retest", when="no_tests"), ctx)

    assert SUITE not in [row["test"] for row in sheller_fake.tests_run]
