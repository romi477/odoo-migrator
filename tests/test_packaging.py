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
