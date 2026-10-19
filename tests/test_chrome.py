import re

import pytest

PAGES = (
    ("/", "overview", "Migrations"),
    ("/tree", "tree", "dependency graph"),
    ("/run", "pipeline", "Runner"),
    ("/sheller", "sheller", "Sheller"),
    ("/result", "result", "Result"),
    ("/settings", "settings", "App settings"),
    ("/styleguide", "styleguide", "Guide"),
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
    assert 'class="nav-tabs nav-steps"' in html
    assert 'class="nav-end"' in html
    assert 'data-screen="overview"' in html
    # The gear is the last control. Attention and the running count sit
    # to its left; a migration's own screens are the row under the switcher.
    end = html.split('class="nav-end"', 1)[1].split("</div>", 1)[0]
    assert end.rfind("App settings") > end.rfind("attention-lamp")
    assert end.rfind("App settings") > end.rfind("queue-count")
    # The anchor, not a fixed attribute order: app settings are a gear with
    # its own class now, and the question is only whether the screen you are
    # on is marked as the current one.
    # Found by data-screen, not by href: the brand is also a link to "/",
    # and it is not the nav item for the screen you are on.
    anchor = re.search(rf'<a[^>]*data-screen="{active}"[^>]*>', html)

    assert anchor is not None, f"no nav link for {active}"
    assert f'href="{path}"' in anchor.group(0)
    assert "active" in anchor.group(0)
    assert f'data-screen="{active}"' in html


def test_steps_hook_follows_the_open_migration(client):
    """Overview is the list. The second row, and the hook into it, belong
    to whichever migration is open."""
    bare = client.get("/").text

    assert "step-hook" not in bare

    created = client.post(
        "/migrations",
        data={
            "name": "Acme",
            "accent": "violet",
            "source_version": "17.0",
            "target_version": "18.0",
            "old_project_path": "/tmp/acme/old",
            "new_project_path": "/tmp/acme/new",
        },
        follow_redirects=True,
    )
    migration_id = created.text.split("/migrations/", 1)[1].split("/open", 1)[0]
    client.post(f"/migrations/{migration_id}/open", follow_redirects=True)
    html = client.get("/tree").text

    assert "step-hook" in html
    assert "step-bridge" in html
    steps = html.split('class="nav-tabs nav-steps"', 1)[1].split("</div>", 1)[0]
    assert steps.count("step-dot") == 2
    order = re.findall(
        r'data-screen="(tree|flow|sheller|pipeline|result)"',
        steps,
    )
    # Runner, Flow and Sheller follow Tree. An arrow leads into them from Tree,
    # dots separate them, an arrow of the same length leads into Result.
    assert order == ["tree", "pipeline", "flow", "sheller", "result"]
    assert "nav-runner" not in html
    first = steps.index("step-bridge")

    assert steps.index('data-screen="tree"') < first < steps.index('data-screen="pipeline"')
    # Two arrows, and neither is a variant: they are one length.
    assert steps.count('class="step-bridge"') == 2
    assert "step-bridge-small" not in html
    assert "step-hook-end" not in html
    assert "switcher-versions" in html
    anchor = re.search(r'<a[^>]*data-screen="tree"[^>]*>', html)

    assert anchor is not None
    assert "active" in anchor.group(0)


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


def test_guide_lists_the_menu_steps_in_order(client):
    """The guide is the four steps under the migration, numbered like the
    menu. The component catalog is a different page."""
    html = client.get("/styleguide").text
    steps = html.split('class="guide-steps"', 1)[1].split("</ol>", 1)[0]

    assert re.findall(r"<h2>(.*?)</h2>", steps) == [
        "Tree",
        "Runner",
        "Sheller",
        "Result",
    ]
    assert "Status badges" not in html
    assert "Create migration" not in html
    # Flow is described after the four steps, block by block. It is not a
    # fifth item in the numbered list.
    flow = html.split('class="guide-flow"', 1)[1]
    assert "<h2>Flow</h2>" in flow
    for block in (
        "understand",
        "port",
        "lint",
        "upgrade",
        "tests",
        "probe",
        "write_test",
        "verdict",
        "diagnose",
        "ask_human",
        "reprobe",
        "patch",
        "rewrite",
    ):
        assert f"<strong>{block}</strong>" in flow


def test_components_page_renders_the_catalog(client):
    html = client.get("/components").text

    assert "Components" in html
    offenders = [
        declaration.strip()
        for block in INLINE_STYLE.findall(body_of(html))
        for declaration in block.split(";")
        if declaration.strip() and not declaration.strip().startswith("--")
    ]

    assert offenders == []


@pytest.mark.parametrize(
    "name",
    (
        "base.html",
        "styleguide.html",
        "components.html",
        "settings.html",
        "settings_migration.html",
    ),
)
def test_templates_are_not_served_as_static_files(client, name):
    """A mount over the whole of web/ handed out Jinja source at /static/*.html."""

    assert client.get(f"/static/{name}").status_code == 404


def test_the_timestamp_formatter_loads_ahead_of_every_screen_script(client):
    """Only the browser knows the reader's zone, so the server sends the
    instant and this formats it. Deferred scripts run in document order: a
    screen script that loaded first would call a helper that is not there."""
    html = client.get("/result").text
    stamp = html.index("/static/stamp.js")

    assert stamp < html.index("/static/result.js")


def test_the_guide_reads_as_one_column_in_the_middle(client):
    """It hugged the left edge with the rest of a wide screen empty."""
    page = client.get("/styleguide").text
    css = client.get("/static/style.css").text
    rule = css.split(".screen-guide > .guide-flow {")[1].split("}")[0]

    assert 'class="screen screen-guide"' in page
    assert "margin-left: auto" in rule and "margin-right: auto" in rule
    assert "max-width" in rule


def test_the_lamp_flashes_amber_whichever_migration_is_open(client):
    """It is global: a stall on another migration must read the same, so it
    carries no colour of the open one."""
    from test_batch import _prepared

    _prepared(client)
    html = client.get("/run").text

    lamp = re.search(r'<a href="/attention"[^>]*>', html).group(0)
    css = client.get("/static/style.css").text
    flashing = css.split(".attention-lamp.needs-attention .lamp-dot {")[1].split("}")[0]

    assert "--accent" not in lamp
    assert "background: var(--amber)" in flashing
    assert "--accent" not in css.split(".attention-lamp.running .lamp-dot {")[1].split("}")[0]
