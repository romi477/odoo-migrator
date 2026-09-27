"""packaging/ holds the commands nobody should have to remember.

Same shape as odoo-sheller's: a shell entry point at the top, and the
desktop build slotting in later without moving anything.
"""

import os
import stat
import subprocess
from pathlib import Path

PACKAGING = Path(__file__).resolve().parents[1] / "packaging"


def _script(name: str) -> Path:

    return PACKAGING / name


def test_env_file_fills_unset_names_and_the_shell_wins(tmp_path, monkeypatch):
    from odoo_migrator.__main__ import load_env_file

    path = tmp_path / ".env"
    path.write_text(
        '# comment\nexport ODOO_MIGRATOR_RUNNER=native\nQUOTED="keep spaces"\n'
        "ALREADY=from-file\nnot a pair\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("ODOO_MIGRATOR_RUNNER", raising=False)
    monkeypatch.delenv("QUOTED", raising=False)
    monkeypatch.setenv("ALREADY", "from-shell")

    load_env_file(path)

    assert os.environ["ODOO_MIGRATOR_RUNNER"] == "native"
    assert os.environ["QUOTED"] == "keep spaces"
    assert os.environ["ALREADY"] == "from-shell"


def test_a_missing_env_file_changes_nothing(tmp_path, monkeypatch):
    from odoo_migrator.__main__ import load_env_file

    monkeypatch.delenv("ODOO_MIGRATOR_RUNNER", raising=False)

    load_env_file(tmp_path / ".env")

    assert "ODOO_MIGRATOR_RUNNER" not in os.environ


def test_the_scripts_are_executable_and_parse():
    for name in ("setup.sh", "serve.sh"):
        path = _script(name)

        assert path.is_file()
        assert path.stat().st_mode & stat.S_IXUSR, f"{name} is not executable"
        # sh -n is a syntax check that runs nothing.
        assert subprocess.run(["sh", "-n", str(path)], check=False).returncode == 0


def test_setup_syncs_with_uv_and_does_not_touch_docker(tmp_path):
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "uv").write_text('#!/bin/sh\necho "cwd=$(pwd)"\necho "$*"\n', encoding="utf-8")
    (stub / "uv").chmod(0o755)
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"}

    ran = subprocess.run(
        [str(_script("setup.sh"))], capture_output=True, text=True, env=env, check=True
    )

    assert "sync" in ran.stdout
    assert f"cwd={PACKAGING.parent}" in ran.stdout
    assert "docker" not in ran.stdout
    refused = subprocess.run(
        [str(_script("setup.sh")), "extra"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert refused.returncode != 0


def test_serve_passes_the_port_and_has_no_widen_flag(tmp_path):
    """The pages have no authentication. Nothing is left that needs a
    wider bind, so the script must not grow a flag that opens one."""
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "uv").write_text('#!/bin/sh\necho "$*"\n', encoding="utf-8")
    (stub / "uv").chmod(0o755)
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"}

    plain = subprocess.run(
        [str(_script("serve.sh"))], capture_output=True, text=True, env=env, check=True
    )
    reloaded = subprocess.run(
        [str(_script("serve.sh")), "--reload"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    assert "--port 8000" in plain.stdout
    assert "--host" not in plain.stdout
    assert "--reload" in reloaded.stdout
    assert "--host" not in reloaded.stdout


def _pyproject() -> dict:
    import tomllib

    return tomllib.loads((PACKAGING.parent / "pyproject.toml").read_text(encoding="utf-8"))


def test_every_package_in_the_tree_is_listed():
    """The list is explicit so data directories can be mapped into the
    package; a new subpackage left off it would be missing from a wheel."""
    root = PACKAGING.parent
    listed = set(_pyproject()["tool"]["setuptools"]["packages"])
    found = {
        ".".join(init.parent.relative_to(root).parts)
        for init in (root / "odoo_migrator").rglob("__init__.py")
        if "__pycache__" not in init.parts
    }

    assert found <= listed, f"not in pyproject.toml: {sorted(found - listed)}"


def test_rules_prompts_and_cartridges_ship_inside_the_package():
    config = _pyproject()["tool"]["setuptools"]
    mapped = config["package-dir"]

    assert mapped["odoo_migrator.bundled_rules"] == "rules"
    assert mapped["odoo_migrator.bundled_prompts"] == "prompts"
    assert mapped["odoo_migrator.bundled_cartridges"] == "cartridges"
    for name in mapped:
        assert name in config["packages"]
        assert config["package-data"][name]


def test_an_installed_build_reads_the_bundled_copies(tmp_path, monkeypatch):
    """No checkout next to the package: the copies inside it are used."""
    from odoo_migrator import paths

    package = tmp_path / "site-packages" / "odoo_migrator"
    for name in ("rules", "prompts", "cartridges"):
        (package / f"bundled_{name}").mkdir(parents=True)
    monkeypatch.setattr(paths, "package_dir", lambda: package)

    assert paths.rules_dir() == package / "bundled_rules"
    assert paths.prompts_dir() == package / "bundled_prompts"
    assert paths.cartridges_dir() == package / "bundled_cartridges"


def test_a_checkout_reads_its_own_directories():
    from odoo_migrator import paths

    assert paths.rules_dir() == PACKAGING.parent / "rules"
    assert paths.cartridges_dir() == PACKAGING.parent / "cartridges"


def test_an_installed_build_promotes_into_the_user_directory(tmp_path, monkeypatch):
    """No checkout, so the rules come from the wheel. That directory belongs
    to the install and is replaced by the next one."""
    from odoo_migrator import paths

    monkeypatch.setattr(paths, "repo_dir", lambda: tmp_path / "site-packages")
    monkeypatch.setattr(paths, "user_dir", lambda: tmp_path / "home")

    assert paths.rules_overlay_dir() == tmp_path / "home" / "rules"


def test_a_checkout_writes_its_rules_where_they_are_reviewed(tmp_path, monkeypatch):
    from odoo_migrator import paths

    checkout = tmp_path / "checkout"
    (checkout / "rules").mkdir(parents=True)
    monkeypatch.setattr(paths, "repo_dir", lambda: checkout)

    assert paths.rules_overlay_dir() is None
    assert paths.rules_dir() == checkout / "rules"
