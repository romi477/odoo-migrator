"""Rules every page script keeps, read off the files themselves."""

import re

from odoo_migrator.paths import static_dir


def _scripts() -> dict[str, str]:

    return {
        path.name: path.read_text(encoding="utf-8")
        for path in static_dir().glob("*.js")
    }


def test_only_sse_js_opens_a_stream():
    """One EventSource per page. Runner opened a second: two connections
    and two server threads per tab, and three tabs used the browser's six
    connections to the host, so the fourth page's requests waited."""
    opening = [name for name, text in _scripts().items() if "new EventSource(" in text]

    assert opening == ["sse.js"]


def test_no_script_writes_markup_into_the_page():
    """Rows are built from nodes. A module's name is data from the project
    on disk, and the queue rows put it through innerHTML."""
    writing = [
        name
        for name, text in _scripts().items()
        if re.search(r"\.innerHTML\s*=\s*(?![\"']{2})", text)
    ]

    assert writing == []
