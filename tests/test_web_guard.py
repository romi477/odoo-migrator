"""Binding loopback keeps other machines out, not other pages in the browser.

Any site the operator has open can submit a form to http://127.0.0.1:8000,
and a name an attacker controls can be rebound to 127.0.0.1 after its page
loaded. These pin the two checks that close that: a request that changes
something must come from this app's own pages, and every request must name
this machine in Host.
"""

from fastapi.testclient import TestClient

from odoo_migrator.settings import get_app_settings

ELSEWHERE = "http://192.0.2.10:8765"


def _save_sheller(client, **headers):

    return client.post(
        "/settings",
        data={"sheller_base_url": ELSEWHERE, "sheller_image_tag": ""},
        headers=headers,
        follow_redirects=False,
    )


def test_a_form_posted_from_another_site_is_refused(client):
    answer = _save_sheller(
        client, **{"Sec-Fetch-Site": "cross-site", "Origin": "https://pages.example"}
    )

    assert answer.status_code == 403
    assert get_app_settings()["sheller_base_url"] != ELSEWHERE


def test_a_page_on_another_port_of_this_machine_is_refused(client):
    # Same site, not same origin: whatever else listens on loopback.
    answer = _save_sheller(
        client, **{"Sec-Fetch-Site": "same-site", "Origin": "http://127.0.0.1:5173"}
    )

    assert answer.status_code == 403
    assert get_app_settings()["sheller_base_url"] != ELSEWHERE


def test_an_origin_that_is_not_this_app_is_refused_without_fetch_metadata(client):
    answer = _save_sheller(client, Origin="https://pages.example")

    assert answer.status_code == 403


def test_a_null_origin_is_refused(client):
    # A sandboxed frame, a file:// page, a data: URL.
    answer = _save_sheller(client, Origin="null")

    assert answer.status_code == 403


def test_the_apps_own_page_may_post(client):
    answer = _save_sheller(
        client, **{"Sec-Fetch-Site": "same-origin", "Origin": "http://127.0.0.1"}
    )

    assert answer.status_code == 303
    assert get_app_settings()["sheller_base_url"] == ELSEWHERE


def test_a_client_that_is_not_a_browser_may_post(client):
    # curl and the suite send neither header; a forged form cannot omit them.
    answer = _save_sheller(client)

    assert answer.status_code == 303


def test_a_host_name_that_is_not_this_machine_is_refused(app):
    # DNS rebinding: the attacker's own name, now pointing at 127.0.0.1.
    rebound = TestClient(app, base_url="http://pages.example:8000")

    assert rebound.get("/").status_code == 400
    assert rebound.get("/api/log/1").status_code == 400


def test_every_loopback_name_is_this_machine(client):
    for host in ("127.0.0.1:8000", "localhost:8000", "[::1]:8000", "LOCALHOST"):
        answer = client.get("/", headers={"host": host}, follow_redirects=False)

        assert answer.status_code != 400, host
