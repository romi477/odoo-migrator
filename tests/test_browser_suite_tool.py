"""The agent runs a module's browser suite through `run_test` with `params`."""

from types import SimpleNamespace

import pytest

from odoo_migrator.runner.tools import TOOL_SCHEMAS, _dispatch, _preview
from odoo_migrator.sheller import FakeShellerClient

SUITE = "web.WebSuite.test_unit_desktop"


@pytest.fixture
def ctx():
    fake = FakeShellerClient()
    opened = fake.open_session(
        container="odoo-new", database="acme", odoo_bin="/opt/odoo/odoo-bin",
        owner={"kind": "agent"}, allow_commit=False,
    )

    return SimpleNamespace(
        sheller=fake,
        sessions={"target": opened["id"]},
        write_keys={"target": opened["write_key"]},
        last_test=None,
    )


def test_the_tool_offers_a_filter():
    assert "params" in TOOL_SCHEMAS["run_test"]["properties"]
    assert "params" not in TOOL_SCHEMAS["run_test"]["required"]


def test_the_filter_is_handed_to_the_daemon(ctx):
    _dispatch("run_test", ctx, "target", {"test": SUITE, "params": "@sale_custom"})

    assert ctx.sheller.tests_run[-1]["params"] == "@sale_custom"


def test_a_run_with_no_filter_sends_none_not_an_empty_one(ctx):
    _dispatch("run_test", ctx, "target", {"test": "sale_custom"})
    _dispatch("run_test", ctx, "target", {"test": SUITE, "params": ""})

    assert [run["params"] for run in ctx.sheller.tests_run] == [None, None]


def test_a_skipped_suite_reads_as_skipped_in_the_log_and_never_as_passed(ctx):
    ctx.sheller.hoot_answers[SUITE] = {"skipped": True, "reason": "chrome_not_found"}
    answer = _dispatch("run_test", ctx, "target", {"test": SUITE, "params": "@sale_custom"})

    line = _preview("run_test", answer, "")

    assert line.startswith("error: js_tests_skipped")
    assert "chrome_not_found" in line
    assert "passed" not in line


def test_the_counts_of_a_browser_run_are_in_the_log_line():
    answer = {
        "error": None,
        "test": {"success": False, "tests_run": 1, "failures": 1, "errors": 0},
        "failed": [{"test": SUITE, "kind": "failure"}],
        "hoot": {"passed": 12, "failed": 2, "failed_tests": ["@m/a", "@m/b"]},
    }

    line = _preview("run_test", answer, "")

    assert "hoot 12 passed, 2 failed" in line
