"""The double answers a clickbot run the way Sheller 1.12.0 to 1.13.2 do."""

import pytest

from odoo_migrator.sheller import FakeShellerClient, ShellerError, ShellerTimeout


def _open(fake, container="odoo-new"):
    return fake.open_session(
        container=container, database="acme", odoo_bin="/opt/odoo/odoo-bin",
        owner={"kind": "agent"}, allow_commit=False,
    )


def _run(fake, opened, module="sale_custom"):
    return fake.click_module(opened["id"], module, write_key=opened["write_key"])


def test_a_green_run_clicked_something_and_failed_nothing():
    fake = FakeShellerClient()
    answer = _run(fake, _open(fake))

    assert answer["error"] is None
    assert answer["test"]["success"] is True and answer["test"]["tests_run"] > 0
    box = answer["clickbot"]
    assert box["module"] == "sale_custom" and box["debug"] is True
    assert box["clicked"] and box["failed"] == [] and box["restored"] is True
    assert all(root["success"] for root in box["roots"])


def test_the_session_says_whether_the_container_can_run_the_clickbot():
    fake = FakeShellerClient()
    opened = _open(fake)

    assert fake.get_session(opened["id"])["clickbot"] == {"available": True, "reason": None}

    fake.clickbot_capability["odoo-new"] = {"available": False, "reason": "chrome_not_found"}
    assert fake.get_session(opened["id"])["clickbot"]["reason"] == "chrome_not_found"


def test_a_daemon_before_1_12_has_no_clickbot_route_and_no_capability():
    fake = FakeShellerClient(version="1.11.0")
    opened = _open(fake)

    assert "clickbot" not in fake.get_session(opened["id"])
    with pytest.raises(ShellerError) as excinfo:
        _run(fake, opened)

    assert excinfo.value.status == 404


def test_a_failed_menu_makes_the_run_red():
    fake = FakeShellerClient()
    fake.clickbot_answers["odoo-new"] = {
        "failed": [{"xmlid": "sale_custom.menu_a", "root": "sale_custom.menu_root", "reason": "boom"}],
        "roots": [{"xmlid": "sale_custom.menu_root", "success": False, "error": None}],
    }
    answer = _run(fake, _open(fake))

    assert answer["error"] is None
    assert answer["test"]["success"] is False and answer["test"]["failures"] == 1
    assert answer["clickbot"]["failed"][0]["reason"] == "boom"


def test_a_refusal_is_the_error_and_ran_no_test():
    fake = FakeShellerClient()
    fake.clickbot_answers["odoo-new"] = {"refused": "no_visible_menus", "clicked": [], "roots": []}
    answer = _run(fake, _open(fake))

    assert answer["error"] == "no_visible_menus"
    assert answer["test"]["tests_run"] == 0 and answer["test"]["success"] is False


def test_a_skipped_run_is_js_tests_skipped_never_a_pass():
    fake = FakeShellerClient()
    fake.clickbot_answers["odoo-new"] = {"skipped": {"reason": "chrome_not_found"}, "clicked": []}
    answer = _run(fake, _open(fake))

    assert answer["error"] == "js_tests_skipped"
    assert answer["test"]["success"] is False
    assert answer["clickbot"]["skipped"]["reason"] == "chrome_not_found"


def test_a_menu_tree_that_could_not_be_put_back_outranks_what_ended_the_run():
    fake = FakeShellerClient()
    fake.clickbot_answers["odoo-new"] = {"restored": False, "error": "interrupted"}
    answer = _run(fake, _open(fake))

    assert answer["error"] == "clickbot_restore_failed"
    assert answer["clickbot"]["cause"] == "interrupted"


def test_a_run_needs_the_sessions_key_and_is_scriptable_per_container():
    fake = FakeShellerClient()
    new, old = _open(fake, "odoo-new"), _open(fake, "odoo-old")
    fake.clickbot_answers["odoo-old"] = {"refused": "no_menus", "clicked": [], "roots": []}

    assert _run(fake, new)["error"] is None
    assert _run(fake, old)["error"] == "no_menus"
    with pytest.raises(ShellerError):
        fake.click_module(new["id"], "sale_custom", write_key="not-the-key")
    assert [run["container"] for run in fake.clicks] == ["odoo-new", "odoo-old"]


def test_a_scripted_error_is_raised_as_the_client_would():
    fake = FakeShellerClient()
    fake.click_error["odoo-new"] = ShellerTimeout("no answer within the timeout")

    with pytest.raises(ShellerTimeout):
        _run(fake, _open(fake))
