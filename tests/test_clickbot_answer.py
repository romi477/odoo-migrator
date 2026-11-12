"""What Sheller's clickbot answered, read the way the step and the agent need it."""

from odoo_migrator.runner.clickbot_answer import read, render, summary


def _answer(error=None, **box):
    base = {
        "module": "m", "login": "admin", "debug": True,
        "roots": [{"xmlid": "m.root", "success": True, "error": None}],
        "clicked": ["m.a", "m.b"], "failed": [], "unreached": [], "hidden": [],
        "refused": None, "restored": True, "filters": 4, "views": 5, "modals": 1,
    }
    base.update(box)

    return {"error": error, "clickbot": base, "test": {"success": error is None}}


def test_a_green_run_clicked_menus_and_broke_none():
    reading = read(_answer())

    assert reading.outcome == "ran"
    assert reading.broken == () and reading.clicked == 2
    assert summary(reading) == "2 menu(s) clicked, 5 view(s), 4 filter(s), 1 modal(s)"


def test_a_failed_menu_is_broken_with_its_root_and_reason():
    reading = read(
        _answer(
            failed=[{"xmlid": "m.a", "root": "m.root", "reason": "boom"}],
            roots=[{"xmlid": "m.root", "success": False, "error": None}],
        )
    )

    assert [(b.xmlid, b.root, b.reason) for b in reading.broken] == [("m.a", "m.root", "boom")]


def test_a_failing_root_with_no_menu_named_is_broken_under_the_roots_xmlid():
    reading = read(
        _answer(roots=[{"xmlid": "sale.sale_menu_root", "success": False, "error": "the view crashed"}])
    )
    assertion = read(
        _answer(roots=[{"xmlid": "sale.sale_menu_root", "success": False, "error": "x is not defined"}])
    )

    assert reading.broken[0].xmlid == "sale.sale_menu_root"
    assert reading.broken[0].reason == "the view crashed"
    # An assertion's own text carries no exception-class prefix: the page's, not the driver's.
    assert assertion.broken


def test_a_root_the_daemon_ran_out_of_time_on_is_a_note_and_never_broken():
    reading = read(
        _answer(
            roots=[{"xmlid": "m.root", "success": False, "error": "timeout"}],
            unreached=["m.c", "m.d"],
        )
    )

    assert reading.outcome == "ran" and reading.broken == ()
    assert reading.unreached == ("m.c", "m.d")
    assert any("m.root" in note and "time" in note for note in reading.notes)


def test_a_server_or_page_error_with_no_menu_named_is_broken_not_a_crashed_browser():
    """Sheller sets a root's `error` to the first server error, worded
    `ValueError: ...`, and then the page's console line when it cannot blame one
    menu. Both read like an exception, and both are the module's."""
    for text in (
        "ValueError: Invalid field 'x_old' on model 'sale.order'",
        "TypeError: Cannot read properties of undefined",
        "odoo.exceptions.MissingError: record does not exist",
    ):
        reading = read(_answer(roots=[{"xmlid": "m.root", "success": False, "error": text}]))

        assert [b.xmlid for b in reading.broken] == ["m.root"], text
        assert reading.broken[0].reason == text
        assert reading.notes == ()


def test_a_browser_that_fell_over_is_a_note_and_never_broken():
    reading = read(
        _answer(
            roots=[{"xmlid": "m.root", "success": False, "error": "ConnectionRefusedError: devtools"}]
        )
    )

    assert reading.broken == ()
    assert any("ConnectionRefusedError" in note for note in reading.notes)


def test_a_root_that_failed_under_a_named_menu_with_no_error_of_its_own_adds_nothing():
    reading = read(
        _answer(
            failed=[{"xmlid": "m.a", "root": "m.root", "reason": "boom"}],
            roots=[{"xmlid": "m.root", "success": False, "error": None}],
        )
    )

    assert [b.xmlid for b in reading.broken] == ["m.a"]


def test_a_roots_own_error_beside_a_named_menu_is_a_second_failure_and_kept():
    """Sheller sets a root's `error` only when a run failed and no menu could
    be blamed (15 to 19), or no menu was named at all (20): next to a `failed`
    entry it is another failure, never the same one said again."""
    reading = read(
        _answer(
            failed=[{"xmlid": "m.a", "root": "m.root", "reason": "boom"}],
            roots=[{"xmlid": "m.root", "success": False, "error": "later: the next start failed"}],
        )
    )

    assert [(b.xmlid, b.reason) for b in reading.broken] == [
        ("m.a", "boom"), ("m.root", "later: the next start failed"),
    ]


def test_a_roots_own_timeout_beside_a_named_menu_is_still_a_note():
    reading = read(
        _answer(
            failed=[{"xmlid": "m.a", "root": "m.root", "reason": "boom"}],
            roots=[{"xmlid": "m.root", "success": False, "error": "timeout"}],
        )
    )

    assert [b.xmlid for b in reading.broken] == ["m.a"]
    assert any("time" in note for note in reading.notes)


def test_a_module_with_no_menus_of_its_own_is_not_a_failure_and_not_a_run():
    for refusal in ("no_menus", "no_visible_menus"):
        reading = read(_answer(error=refusal, refused=refusal, clicked=[], roots=[]))

        assert reading.outcome == "no_menus"


def test_a_module_that_is_not_there_after_a_green_upgrade_is_a_setup_problem():
    for refusal in ("module_not_installed", "unknown_module"):
        assert read(_answer(error=refusal, refused=refusal)).outcome == "setup"


def test_a_skip_names_its_reason_or_says_there_is_none():
    named = read(_answer(error="js_tests_skipped", skipped={"reason": "chrome_not_found"}, clicked=[]))
    bare = read(_answer(error="js_tests_skipped", skipped={"reason": None}, clicked=[]))

    assert named.outcome == "skipped" and named.reason == "chrome_not_found"
    assert bare.outcome == "skipped" and "no reason" in bare.reason


def test_an_unknown_login_and_any_other_error_are_the_environments():
    assert read(_answer(error="unknown_login", refused="unknown_login")).outcome == "environment"
    other = read(_answer(error="workers must be 0"))

    assert other.outcome == "environment" and "workers must be 0" in other.reason


def test_a_menu_tree_that_could_not_be_put_back_says_so_and_asks_for_a_new_session():
    reading = read(_answer(error="clickbot_restore_failed", restored=False, cause="interrupted"))

    assert reading.outcome == "environment" and reading.restore_failed is True
    assert "interrupted" in reading.reason


def test_an_answer_with_no_clickbot_report_is_never_a_pass():
    assert read({"error": None, "test": {"success": True}}).outcome == "environment"
    assert read({}).outcome == "environment"


def test_a_run_that_clicked_nothing_and_failed_nothing_is_never_a_pass():
    reading = read(_answer(clicked=[], roots=[]))

    assert reading.outcome == "environment"
    assert "nothing" in reading.reason


def test_the_words_for_the_agent_cannot_read_as_a_pass_when_nothing_ran():
    skipped = render(_answer(error="js_tests_skipped", skipped={"reason": "chrome_not_found"}, clicked=[]))

    assert "did not run" in skipped and "chrome_not_found" in skipped
    assert "not a pass" in skipped


def test_the_words_for_the_agent_name_each_broken_menu_then_what_was_not_reached():
    text = render(
        _answer(
            failed=[{"xmlid": "m.a", "root": "m.root", "reason": "boom"}],
            roots=[{"xmlid": "m.root", "success": False, "error": None}],
            unreached=["m.z"],
        )
    )
    lines = text.splitlines()

    assert lines[0].startswith("clickbot: 2 menu(s) clicked, 5 view(s)")
    assert "1 failed" in lines[0]
    assert "m.a (under m.root): boom" in lines
    assert "unreached: m.z" in lines
