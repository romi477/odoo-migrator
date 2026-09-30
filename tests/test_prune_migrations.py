"""A port does not carry the old version's migration scripts.

`migrations/<version>/` and `upgrades/<version>/` hold scripts Odoo runs when
a module moves to that version. The old series' scripts were written for
the old framework, and a short version such as `1.0.3` is read against the
module's series — after the port, the new one — so an old script would run
again, on the new framework. Only the target series' scripts stay: they
are the ones written for this migration.
"""

from odoo_migrator.runner.prune import prune_old_migrations


def _script(root, *parts):
    path = root.joinpath(*parts)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("def migrate(cr, version):\n    pass\n")


def test_old_series_migrations_go_and_the_target_series_stays(tmp_path):
    module = tmp_path / "sale_x"
    _script(module, "migrations", "17.0.1.0.3", "pre-migrate.py")
    _script(module, "migrations", "1.0.4", "post-migrate.py")
    _script(module, "migrations", "0.0.0", "end-migrate.py")
    _script(module, "migrations", "18.0.1.0.0", "pre-migrate.py")
    _script(module, "upgrades", "17.0.2.0.0", "post-migrate.py")

    removed = prune_old_migrations(module, "18.0")

    assert sorted(removed) == [
        "migrations/0.0.0",
        "migrations/1.0.4",
        "migrations/17.0.1.0.3",
        "upgrades/17.0.2.0.0",
    ]
    assert (module / "migrations" / "18.0.1.0.0" / "pre-migrate.py").is_file()
    assert not (module / "upgrades").exists()


def test_a_directory_left_empty_goes_too(tmp_path):
    module = tmp_path / "sale_x"
    _script(module, "migrations", "17.0.1.0.3", "pre-migrate.py")
    (module / "migrations" / "__init__.py").write_text("")

    prune_old_migrations(module, "18.0")

    assert not (module / "migrations").exists()


def test_a_module_without_migrations_is_left_alone(tmp_path):
    module = tmp_path / "sale_x"
    module.mkdir()
    (module / "__manifest__.py").write_text("{}\n")

    assert prune_old_migrations(module, "18.0") == []
    assert (module / "__manifest__.py").is_file()


def test_nothing_outside_the_module_is_touched(tmp_path):
    assert prune_old_migrations(tmp_path / "missing", "18.0") == []
