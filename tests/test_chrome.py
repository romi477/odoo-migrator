import re

import pytest

PAGES = (
    ("/", "overview", "Migrations"),
    ("/tree", "tree", "Module tree"),
    ("/run", "run", "Run"),
    ("/settings", "settings", "App settings"),
    ("/styleguide", "styleguide", "Styleguide"),
)

INLINE_STYLE = re.compile(r'style="([^"]*)"')


def body_of(html: str) -> str:

    return html.split("<body", 1)[1]


@pytest.mark.parametrize("path,active,heading", PAGES)
def test_page_returns_ok_with_screen_heading(client, path, active, heading):
    response = client.get(path)

    assert response.status_code == 200
    assert heading in response.text


@pytest.mark.parametrize("path,active,heading", PAGES)
def test_chrome_brand_bar_is_present(client, path, active, heading):
    html = client.get(path).text

    assert 'id="top"' in html
    assert 'class="brand"' in html
    assert "odoo-migrator." in html
    assert 'class="nav-tabs"' in html
    assert 'class="nav-end"' in html
    assert f'href="{path}" class="active"' in html
    assert f'data-screen="{active}"' in html


def test_stylesheet_is_linked_and_carries_sheller_tokens(client):
    html = client.get("/").text

    assert "/static/style.css" in html

    css = client.get("/static/style.css")

    assert css.status_code == 200
    assert css.text.count("--background: oklch(0.11 0.03 290)") == 1
    assert "--panel: oklch(0.17 0.04 290)" in css.text
    assert "--cyan: oklch(0.72 0.09 205)" in css.text
    assert "--amber: oklch(0.72 0.12 62)" in css.text
    assert "color-scheme: dark" in css.text


@pytest.mark.parametrize("path,active,heading", PAGES)
def test_inline_styles_carry_only_custom_properties(client, path, active, heading):
    """The mockup is 499 inline declarations and no classes; none of that is
    copied here — appearance belongs in the stylesheet.

    Custom properties are the exception, because some values are per-row data
    rather than styling: a migration's accent colour arrives as
    ``style="--accent: ..."`` and has nowhere else to live. Anything that is
    not a ``--`` declaration is a rule that escaped the stylesheet.
    """
    offenders = [
        declaration.strip()
        for block in INLINE_STYLE.findall(body_of(client.get(path).text))
        for declaration in block.split(";")
        if declaration.strip() and not declaration.strip().startswith("--")
    ]

    assert offenders == []


@pytest.mark.parametrize(
    "name",
    ("base.html", "styleguide.html", "settings.html", "settings_migration.html"),
)
def test_templates_are_not_served_as_static_files(client, name):
    """A mount over the whole of web/ handed out Jinja source at /static/*.html."""

    assert client.get(f"/static/{name}").status_code == 404
