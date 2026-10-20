"""The bundle check in the smoke script matches a manifest entry the way Odoo does."""

import re

import pytest

from odoo_migrator.runner.steps import smoke


def _helper(name):
    script = smoke._SCRIPT.format(name="mod")
    source = re.search(rf"def {name}\(.*?\n\n\n", script, re.DOTALL).group(0)
    scope = {"_re": re}
    exec(source, scope)  # noqa: S102 - the script's own helper, nothing else

    return scope[name]


def _matches():

    return _helper("_matches")


@pytest.mark.parametrize(
    ("path", "pattern", "expected"),
    [
        ("mod/static/src/js/a.js", "mod/static/src/js/**/*", True),
        ("mod/static/src/js/deep/er/a.js", "mod/static/src/js/**/*", True),
        ("mod/static/tests/a.test.js", "mod/static/tests/**/*", True),
        ("mod/static/src/xml/a.xml", "mod/static/src/js/**/*", False),
        ("mod/static/src/js/a.js", "mod/static/src/js/*.js", True),
        ("mod/static/src/js/deep/a.js", "mod/static/src/js/*.js", False),
        ("mod/static/src/js/a.js", "mod/static/src/js/a.js", True),
        ("mod/static/src/js/a.js", "mod/static/src/js/b.js", False),
        ("mod/static/src/js/a.js", "mod/static/src/**/a.js", True),
    ],
)
def test_a_manifest_entry_matches_the_way_odoo_expands_it(path, pattern, expected):
    assert _matches()(path, pattern) is expected


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ("mod/static/src/js/**/*", "mod/static/src/js/**/*"),
        (("include", "mod/static/src/js/**/*"), "mod/static/src/js/**/*"),
        (("after", "web/a.js", "mod/static/src/b.js"), "mod/static/src/b.js"),
        (("replace", "web/a.js", "mod/static/src/b.js"), "mod/static/src/b.js"),
        # Takes files out: nothing is meant to match.
        (("remove", "mod/static/tests/tours/**/*"), ""),
        ((), ""),
    ],
)
def test_a_remove_entry_adds_no_file_for_the_check_to_look_for(entry, expected):
    assert _helper("_listed")(entry) == expected
