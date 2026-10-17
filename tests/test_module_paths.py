"""Where a module's source is on the host.

The live run had `old_project_path` pointing at the repository and the
addons one level down in `project/`. Three separate lookups all answered
`<root>/<name>`, so the prompt carried "(no module source)", lint reported
"no module directory", and the rewrite's diff was empty — while the graph
scan had found all fourteen modules and looked fine.
"""

from types import SimpleNamespace

from odoo_migrator.module_paths import find_module_dir, host_paths, module_root, relative_to_root


def _module(root, name: str):
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "__manifest__.py").write_text("{'name': 'x', 'depends': []}", encoding="utf-8")

    return directory


def _migration(old="", new=""):

    return SimpleNamespace(old_project_path=str(old), new_project_path=str(new))


def test_a_module_in_a_subdirectory_is_found(tmp_path):
    _module(tmp_path / "project", "sale_custom")

    found = find_module_dir(tmp_path, "sale_custom")

    assert found == tmp_path / "project" / "sale_custom"


def test_a_directory_without_a_manifest_is_not_the_module(tmp_path):
    (tmp_path / "sale_custom").mkdir()

    assert find_module_dir(tmp_path, "sale_custom") is None


def test_the_root_itself_wins_over_a_deeper_copy(tmp_path):
    _module(tmp_path, "sale_custom")
    _module(tmp_path / "vendor", "sale_custom")

    assert find_module_dir(tmp_path, "sale_custom") == tmp_path / "sale_custom"


def test_the_new_side_mirrors_where_the_old_one_sits(tmp_path):
    """The port has to be written somewhere that does not exist yet."""
    old = tmp_path / "17"
    new = tmp_path / "18"
    _module(old / "project", "sale_custom")
    (new / "project").mkdir(parents=True)

    old_path, new_path = host_paths(_migration(old, new), "sale_custom")

    assert old_path == str(old / "project" / "sale_custom")
    assert new_path == str(new / "project" / "sale_custom")


def test_an_existing_module_on_the_new_side_is_used_as_it_is(tmp_path):
    old = tmp_path / "17"
    new = tmp_path / "18"
    _module(old / "project", "sale_custom")
    _module(new / "addons", "sale_custom")

    _, new_path = host_paths(_migration(old, new), "sale_custom")

    assert new_path == str(new / "addons" / "sale_custom")


def test_nothing_found_falls_back_to_the_root(tmp_path):
    old = tmp_path / "17"
    new = tmp_path / "18"
    old.mkdir()
    new.mkdir()

    old_path, new_path = host_paths(_migration(old, new), "sale_custom")

    assert old_path == ""
    assert new_path == str(new / "sale_custom")


def test_a_stored_path_is_not_searched_for_again(tmp_path):
    module = SimpleNamespace(
        technical_name="sale_custom",
        old_path=str(tmp_path / "somewhere" / "sale_custom"),
        new_path="",
    )

    assert module_root(_migration(tmp_path, tmp_path), module, "source") == (
        tmp_path / "somewhere" / "sale_custom"
    )


def test_the_prompt_gets_the_path_write_file_wants(tmp_path):
    """`write_file` refuses an absolute path, so the model needs this one."""
    old = tmp_path / "17"
    _module(old / "project", "sale_custom")
    module = SimpleNamespace(technical_name="sale_custom", old_path="", new_path="")

    assert relative_to_root(_migration(old, old), module, "source") == "project/sale_custom"


def test_the_scan_stores_both_paths(app, tmp_path):
    from odoo_migrator.db import database
    from odoo_migrator.graph import persist_graph
    from odoo_migrator.migrations import create_migration
    from odoo_migrator.models import Module

    old = tmp_path / "17"
    new = tmp_path / "18"
    _module(old / "project", "sale_custom")
    new.mkdir()
    created = create_migration(
        name="Acme",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path=str(old),
        new_project_path=str(new),
    )

    persist_graph(created["id"], [{"name": "sale_custom", "depends": [], "addons_root": "/opt/project"}])

    with database.connection_context():
        stored = Module.get(Module.technical_name == "sale_custom")

    assert stored.old_path == str(old / "project" / "sale_custom")
    assert stored.new_path == str(new / "project" / "sale_custom")
    # The container's own path is kept, and is not a host path.
    assert stored.addons_root == "/opt/project"
