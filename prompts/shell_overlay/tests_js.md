### Browser tests (Hoot)

`list_tests` also answers with `js` for the module: the JS test files it
ships, `in_manifest` for each (is it in the bundle the run loads), and `run`,
the call that runs them. Beside those are `available`, whether this container
can run a browser suite at all, and `reason` when it cannot:
`chrome_not_found`, `websocket_client_missing` or `runner_not_supported` (15 to
17 use QUnit, which `run_test` does not drive). Read them before you spend a run.

Run the suite through the Python test that drives Hoot, and pass Hoot's filter
as `params`:

    run_test(side="target", test="web.WebSuite.test_unit_desktop", params="@my_module")

`params` is a module (`@my_module`), a suite or one test by its id
(`@my_module/path/to/the test name`), several joined by commas with no space
after them, a `-` in front to leave one out. A filter Hoot has nothing for does
not narrow the run, it widens it to every JS test there is, so the session
stops such a run and the answer names the filter in `hoot.unknown_filters`.
Check the id before you retry.

The answer's own counts describe the one Python test that ran Hoot. `hoot` has
Hoot's: `passed`, `failed`, `failed_tests` (ids; pass one back as `params` to
rerun just that test) and `reasons`, the failed assertions of each. A file with
`in_manifest: false` is not in the run, and the run passes over fewer tests
than the module has.

**A skipped suite is not a pass.** Without Chromium or `websocket-client` Odoo
skips the test and counts the skip as a pass. Here the answer says
`error: "js_tests_skipped"`, `test.success` is false and `hoot.skipped` is true,
with the `reason`. Report that the JS tests did not run; never that they passed.
