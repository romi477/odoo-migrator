"""The clickbot step, against the double: no browser, no daemon."""

from types import SimpleNamespace

import pytest

from odoo_migrator.runner.cartridge import Step
from odoo_migrator.runner.context import StepContext, Stopped
from odoo_migrator.runner.steps import clickbot
from odoo_migrator.sheller import FakeShellerClient, ShellerError, ShellerTimeout

BROKEN = {
    "failed": [{"xmlid": "sale_custom.menu_a", "root": "sale_custom.menu_root", "reason": "boom"}],
    "roots": [{"xmlid": "sale_custom.menu_root", "success": False, "error": None}],
}


@pytest.fixture
def fake():
    return FakeShellerClient()


def _context(fake, sides=("source", "target")):
    sessions, keys, targets = {}, {}, {}
    for side in sides:
        container = "odoo-old" if side == "source" else "odoo-new"
        opened = fake.open_session(
            container=container, database="acme", odoo_bin="/opt/odoo/odoo-bin",
            owner={"kind": "agent"}, allow_commit=side == "target",
        )
        sessions[side], keys[side] = opened["id"], opened["write_key"]
        targets[side] = {
            "container": container, "database": "acme",
            "odoo_bin": "/opt/odoo/odoo-bin", "owner": {"kind": "agent"},
        }

    return StepContext(
        job_id="job-1", attempt=1, migration=None,
        module=SimpleNamespace(technical_name="sale_custom"), cartridge=None,
        sheller=fake, sessions=sessions, write_keys=keys, session_targets=targets,
        emit=lambda _event: None,
    )


@pytest.fixture
def ctx_for(fake):

    return lambda: _context(fake)


def _run(ctx):

    return clickbot.run(Step(kind="clickbot"), ctx)


def _findings(ctx):

    return [row["text"] for row in ctx.findings if row["source"] == "clickbot"]


def test_a_green_module_is_one_run_on_the_target_and_nothing_else(fake, ctx_for):
    ctx = ctx_for()

    result = _run(ctx)

    assert result.ok is True and result.flags == set()
    assert "menu(s) clicked" in result.output
    (run,) = fake.clicks
    assert run["container"] == "odoo-new" and run["module"] == "sale_custom"
    assert run["timeout"] == clickbot.CLICK_TIMEOUT == 900.0 and run["login"] is None
    assert ctx.findings == []


def test_the_run_is_a_line_in_the_run_log_under_the_tools_name(fake, ctx_for):
    calls = []
    ctx = ctx_for()
    ctx.record_tool = lambda *args: calls.append(args)

    _run(ctx)

    ((tool, side, session, command_id, _ms, ok, preview),) = calls
    assert (tool, side, ok) == ("click_module", "target", True)
    assert session == ctx.sessions["target"] and command_id
    assert "menu(s) clicked" in preview


def test_a_daemon_without_the_clickbot_is_a_finding_and_the_lap_goes_on():
    fake = FakeShellerClient(version="1.11.0")
    ctx = _context(fake, sides=("target",))

    result = _run(ctx)

    assert result.ok is True and fake.clicks == []
    assert any("needs Sheller 1.12.0" in text and "1.11.0" in text for text in _findings(ctx))
    assert "did not run" in result.output


def test_a_route_that_answers_404_is_the_same_finding(fake, ctx_for):
    fake.click_error["odoo-new"] = ShellerError("Sheller HTTP 404: Not Found", status=404)
    ctx = ctx_for()

    result = _run(ctx)

    assert result.ok is True
    assert any("no clickbot route" in text for text in _findings(ctx))


def test_a_container_that_cannot_run_a_browser_is_a_finding_with_its_reason(fake, ctx_for):
    fake.clickbot_capability["odoo-new"] = {"available": False, "reason": "chrome_not_found"}
    ctx = ctx_for()

    result = _run(ctx)

    assert result.ok is True and fake.clicks == []
    assert any("chrome_not_found" in text and "unproven" in text for text in _findings(ctx))


def test_odoo_15_and_16_cannot_say_so_and_the_run_decides(fake, ctx_for):
    fake.clickbot_capability["odoo-new"] = {"available": None, "reason": "chrome_not_checked"}

    result = _run(ctx_for())

    assert result.ok is True and len(fake.clicks) == 1


def test_a_run_odoo_skipped_is_a_finding_and_never_a_pass(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = {"skipped": {"reason": "chrome_not_found"}, "clicked": []}
    ctx = ctx_for()

    result = _run(ctx)

    assert result.ok is True
    assert "did not run" in result.output and "menu(s) clicked" not in result.output
    assert any("chrome_not_found" in text for text in _findings(ctx))


def test_a_module_with_no_menus_of_its_own_is_neither_a_finding_nor_a_failure(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = {"refused": "no_menus", "clicked": [], "roots": []}
    ctx = ctx_for()

    result = _run(ctx)

    assert result.ok is True and ctx.findings == []
    assert "no menu" in result.output


@pytest.mark.parametrize(
    "script",
    [
        {"refused": "unknown_login", "clicked": [], "roots": []},
        {"error": "workers must be 0", "clicked": [], "roots": []},
    ],
)
def test_what_the_environment_refuses_is_a_finding_and_the_lap_goes_on(fake, ctx_for, script):
    fake.clickbot_answers["odoo-new"] = script
    ctx = ctx_for()

    result = _run(ctx)

    assert result.ok is True and len(_findings(ctx)) == 1


def test_a_run_past_the_call_is_a_finding_and_the_lap_goes_on(fake, ctx_for):
    fake.click_error["odoo-new"] = ShellerTimeout("no answer within the timeout")
    ctx = ctx_for()

    result = _run(ctx)

    assert result.ok is True
    assert any("900" in text for text in _findings(ctx))


def test_a_module_the_daemon_does_not_find_after_a_green_upgrade_fails_the_step(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = {"refused": "module_not_installed", "clicked": [], "roots": []}

    result = _run(ctx_for())

    assert result.ok is False and result.flags == {"tests_failed"}
    assert "module_not_installed" in result.output


def test_menus_a_root_ran_out_of_time_on_are_a_finding_listing_them_not_a_failure(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = {
        "roots": [{"xmlid": "sale_custom.menu_root", "success": False, "error": "timeout"}],
        "unreached": ["sale_custom.menu_c"],
    }
    ctx = ctx_for()

    result = _run(ctx)

    assert result.ok is True
    assert any("sale_custom.menu_c" in text for text in _findings(ctx))


def test_menus_a_clean_run_never_reached_are_a_finding(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = {"unreached": ["sale_custom.menu_url"]}
    ctx = ctx_for()

    result = _run(ctx)

    assert result.ok is True
    assert any("sale_custom.menu_url" in text for text in _findings(ctx))


def test_a_menu_tree_that_could_not_be_put_back_reopens_that_side_and_says_so(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = {"restored": False}
    ctx = ctx_for()
    before = ctx.sessions["target"]

    result = _run(ctx)

    assert result.ok is True
    assert before in fake.closes and ctx.sessions["target"] != before
    assert any("put back" in text for text in _findings(ctx))
    assert "put back" in result.output


def test_a_menu_the_port_broke_fails_the_lap_and_names_it(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = dict(BROKEN)

    result = _run(ctx_for())

    assert result.ok is False and result.flags == {"tests_failed"}
    lines = result.output.splitlines()
    assert lines[0] == "sale_custom clickbot: 1 menu(s) the port broke (as admin, debug on)"
    assert "sale_custom.menu_a (under sale_custom.menu_root): boom" in lines


def test_a_stop_is_heard_before_the_run(fake, ctx_for):
    ctx = ctx_for()
    ctx.stop_asked = lambda: True

    with pytest.raises(Stopped):
        _run(ctx)

    assert fake.clicks == []


def test_the_same_finding_is_not_added_twice_when_the_lap_runs_again(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = {"unreached": ["sale_custom.menu_url"]}
    ctx = ctx_for()

    _run(ctx)
    _run(ctx)

    assert len(_findings(ctx)) == 1


def test_a_step_with_no_target_session_fails_the_way_tests_does(fake, ctx_for):
    ctx = ctx_for()
    ctx.sessions["target"] = ""

    result = _run(ctx)

    assert result.ok is False and result.output == "no target session"


SOURCE_BROKEN_A = {
    "failed": [{"xmlid": "sale_custom.menu_a", "root": "sale_custom.menu_root", "reason": "old boom"}],
    "roots": [{"xmlid": "sale_custom.menu_root", "success": False, "error": None}],
}


def test_the_source_is_not_clicked_when_the_target_is_green(fake, ctx_for):
    _run(ctx_for())

    assert [run["container"] for run in fake.clicks] == ["odoo-new"]


def test_a_menu_that_works_on_the_source_and_not_on_the_target_is_the_ports(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = dict(BROKEN)

    result = _run(ctx_for())

    assert result.ok is False and result.flags == {"tests_failed"}
    assert [run["container"] for run in fake.clicks] == ["odoo-new", "odoo-old"]
    assert result.output.splitlines()[0].startswith("sale_custom clickbot: 1 menu(s) the port broke")
    assert "no baseline" not in result.output


def test_a_menu_that_fails_on_the_source_too_is_a_finding_and_the_lap_goes_on(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = dict(BROKEN)
    fake.clickbot_answers["odoo-old"] = dict(SOURCE_BROKEN_A)
    ctx = ctx_for()

    result = _run(ctx)

    assert result.ok is True and result.flags == set()
    assert any(
        "sale_custom.menu_a" in text and "fails on the source too" in text and "old boom" in text
        for text in _findings(ctx)
    )
    assert "fail on the source too" in result.output


def test_only_what_the_port_broke_fails_the_lap_when_some_fail_on_both(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = {
        "failed": [
            {"xmlid": "sale_custom.menu_a", "root": "sale_custom.menu_root", "reason": "boom"},
            {"xmlid": "sale_custom.menu_b", "root": "sale_custom.menu_root", "reason": "bang"},
        ],
        "roots": [{"xmlid": "sale_custom.menu_root", "success": False, "error": None}],
    }
    fake.clickbot_answers["odoo-old"] = dict(SOURCE_BROKEN_A)

    result = _run(ctx_for())

    lines = result.output.splitlines()
    assert result.ok is False
    assert lines[0] == "sale_custom clickbot: 1 menu(s) the port broke (as admin, debug on)"
    assert "sale_custom.menu_b (under sale_custom.menu_root): bang" in lines
    assert "not broken by the port, failing on the source too: sale_custom.menu_a" in lines


def test_a_menu_the_source_never_reached_is_the_ports(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = dict(BROKEN)
    fake.clickbot_answers["odoo-old"] = {
        "clicked": [], "unreached": ["sale_custom.menu_a"],
        "roots": [{"xmlid": "sale_custom.menu_root", "success": False, "error": "timeout"}],
    }

    result = _run(ctx_for())

    assert result.ok is False


def test_a_root_failing_with_no_menu_named_on_both_sides_is_broken_on_both(fake, ctx_for):
    root = {"roots": [{"xmlid": "sale.sale_menu_root", "success": False, "error": "the view crashed"}]}
    fake.clickbot_answers["odoo-new"] = dict(root)
    fake.clickbot_answers["odoo-old"] = dict(root)
    ctx = ctx_for()

    result = _run(ctx)

    assert result.ok is True
    assert any("sale.sale_menu_root" in text and "source too" in text for text in _findings(ctx))


def test_a_source_that_cannot_run_the_clickbot_gives_no_baseline_and_says_so(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = dict(BROKEN)
    fake.clickbot_capability["odoo-old"] = {"available": False, "reason": "chrome_not_found"}

    result = _run(ctx_for())

    assert result.ok is False and result.flags == {"tests_failed"}
    assert "no baseline was taken on the source" in result.output
    assert "chrome_not_found" in result.output
    assert [run["container"] for run in fake.clicks] == ["odoo-new"]


@pytest.mark.parametrize(
    "script",
    [{"refused": "no_menus", "clicked": [], "roots": []}, {"skipped": {"reason": "x"}, "clicked": []}],
)
def test_a_source_run_that_ran_nothing_gives_no_baseline(fake, ctx_for, script):
    fake.clickbot_answers["odoo-new"] = dict(BROKEN)
    fake.clickbot_answers["odoo-old"] = script

    result = _run(ctx_for())

    assert result.ok is False and "no baseline was taken on the source" in result.output


def test_a_dead_source_session_gives_no_baseline(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = dict(BROKEN)
    ctx = ctx_for()
    fake.kill(ctx.sessions["source"])

    result = _run(ctx)

    assert result.ok is False and "no baseline was taken on the source" in result.output


def test_a_job_with_no_source_session_gives_no_baseline(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = dict(BROKEN)
    ctx = ctx_for()
    ctx.sessions["source"] = ""

    result = _run(ctx)

    assert result.ok is False and "no baseline was taken on the source" in result.output


def test_a_menu_tree_the_source_could_not_put_back_reopens_the_source(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = dict(BROKEN)
    fake.clickbot_answers["odoo-old"] = {"restored": False}
    ctx = ctx_for()
    before = ctx.sessions["source"]

    result = _run(ctx)

    assert result.ok is False and "no baseline was taken on the source" in result.output
    assert before in fake.closes and ctx.sessions["source"] != before
    assert ctx.sessions["target"] not in fake.closes


def test_a_stop_is_heard_before_the_baseline_too(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = dict(BROKEN)
    ctx = ctx_for()
    ctx.stop_asked = lambda: len(fake.clicks) >= 1

    with pytest.raises(Stopped):
        _run(ctx)

    assert [run["container"] for run in fake.clicks] == ["odoo-new"]


def test_the_findings_of_a_baseline_are_not_added_twice_when_the_lap_runs_again(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = dict(BROKEN)
    fake.clickbot_answers["odoo-old"] = dict(SOURCE_BROKEN_A)
    ctx = ctx_for()

    _run(ctx)
    _run(ctx)

    assert len(_findings(ctx)) == 1


def test_every_step_kind_the_loader_accepts_has_code_to_run_it():
    from odoo_migrator.runner.cartridge import KINDS
    from odoo_migrator.runner.registry import resolve
    from odoo_migrator.runner.steps import _RUNNERS

    for kind in KINDS:
        assert callable(resolve(kind)), kind
    assert set(_RUNNERS) == set(KINDS) - {"agent"}


def test_a_run_past_the_call_frees_the_session_and_the_output_says_so(fake, ctx_for):
    fake.click_hangs.add("odoo-new")
    ctx = ctx_for()
    sid = ctx.sessions["target"]
    assert fake.get_session(sid)["state"] == "ready"

    result = _run(ctx)

    assert result.ok is True
    assert fake.get_session(ctx.sessions["target"])["state"] == "ready"
    assert sid in fake.interrupts
    assert "interrupted" in result.output
    assert any("interrupted" in text for text in _findings(ctx))


def test_a_baseline_run_past_the_call_frees_the_source_session(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = dict(BROKEN)
    fake.click_hangs.add("odoo-old")
    ctx = ctx_for()
    source = ctx.sessions["source"]

    result = _run(ctx)

    assert result.ok is False and "no baseline was taken on the source" in result.output
    assert source in fake.interrupts
    assert fake.get_session(ctx.sessions["source"])["state"] == "ready"


def test_the_daemon_is_asked_its_version_once_however_many_sides_run(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = dict(BROKEN)
    asked = []
    real = fake.health
    fake.health = lambda: asked.append(1) or real()

    _run(ctx_for())

    assert len(fake.clicks) == 2 and len(asked) == 1


def _quiet(ctx):

    return {row["text"]: row.get("quiet", False) for row in ctx.findings}


def test_what_the_environment_kept_from_running_is_a_quiet_finding(fake, ctx_for):
    fake.clickbot_capability["odoo-new"] = {"available": False, "reason": "chrome_not_found"}
    ctx = ctx_for()

    _run(ctx)

    assert ctx.findings and all(_quiet(ctx).values())


def test_menus_never_reached_and_notes_are_quiet_findings(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = {
        "roots": [{"xmlid": "sale_custom.menu_root", "success": False, "error": "timeout"}],
        "unreached": ["sale_custom.menu_url"],
    }
    ctx = ctx_for()

    _run(ctx)

    assert len(ctx.findings) == 2 and all(_quiet(ctx).values())


def test_a_menu_that_fails_on_the_source_too_is_a_finding_that_stops_for_reading(fake, ctx_for):
    fake.clickbot_answers["odoo-new"] = dict(BROKEN)
    fake.clickbot_answers["odoo-old"] = dict(BROKEN)
    ctx = ctx_for()

    _run(ctx)

    (text,) = [text for text in _findings(ctx) if "source too" in text]
    assert _quiet(ctx)[text] is False
