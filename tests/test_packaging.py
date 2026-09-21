"""packaging/ holds the commands nobody should have to remember.

Same shape as odoo-sheller's: shell entry points at the top, a small Python
entry beside them, and the desktop build slotting in later without moving
anything.
"""

import os
import stat
import subprocess
from pathlib import Path

PACKAGING = Path(__file__).resolve().parents[1] / "packaging"


def _script(name: str) -> Path:

    return PACKAGING / name


def test_the_scripts_are_executable_and_parse():
    for name in ("serve.sh", "n8n.sh"):
        path = _script(name)
        assert path.is_file(), name
        assert path.stat().st_mode & stat.S_IXUSR, f"{name} is not executable"
        # sh -n is a syntax check that runs nothing.
        assert subprocess.run(["sh", "-n", str(path)], check=False).returncode == 0


def test_serve_defaults_to_loopback_and_widens_only_when_asked(tmp_path):
    """Widening is a decision: the pages have no authentication, and the key
    only guards the File API and the proxy."""
    stub = tmp_path / "bin"
    stub.mkdir()
    (stub / "uv").write_text('#!/bin/sh\necho "$*"\n', encoding="utf-8")
    (stub / "uv").chmod(0o755)
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"}

    plain = subprocess.run(
        [str(_script("serve.sh"))], capture_output=True, text=True, env=env, check=True
    )
    widened = subprocess.run(
        [str(_script("serve.sh")), "--for-n8n"],
        capture_output=True,
        text=True,
        env=env,
        check=True,
    )

    assert "--host 127.0.0.1" in plain.stdout
    assert "--host 0.0.0.0" in widened.stdout
    assert "--for-n8n" not in widened.stdout


def test_n8n_mints_the_key_when_the_app_has_not_run(tmp_path):
    """It used to demand that the app had been started once. Two notes on
    this test: HOME is what the key path is built from, and overriding it
    also hides docker's CLI plugins, so the compose step after this fails —
    which is fine, the key is minted before anything reaches docker."""
    env = {**os.environ, "HOME": str(tmp_path)}

    subprocess.run(
        [str(_script("n8n.sh")), "up"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    minted = tmp_path / ".odoo-migrator" / "api.key"

    assert minted.is_file()
    assert minted.stat().st_mode & 0o077 == 0
    assert len(minted.read_text(encoding="utf-8").strip()) >= 32


def test_down_never_removes_the_volume():
    """The workflows live in n8n's own database in that volume, not in this
    repo: `down -v` throws away everything publish created."""
    body = _script("n8n.sh").read_text(encoding="utf-8")

    assert "compose down" in body
    assert "down -v" not in body


def test_the_key_is_never_echoed():
    body = _script("n8n.sh").read_text(encoding="utf-8")

    assert "echo $key" not in body
    assert 'echo "$key"' not in body
    assert "cat \"$KEY_FILE\"" in body


def test_up_refuses_a_taken_port_before_touching_the_stack():
    """`docker compose up` recreates the container and only then fails on
    the bind, so a clash left the stack worse than it started. On the
    machine this was written on, 5678 belongs to an Odoo container."""
    body = _script("n8n.sh").read_text(encoding="utf-8")

    assert "require_port" in body
    assert body.index("require_port\n") < body.index("compose up -d")
    assert "N8N_HOST_PORT" in body


def test_the_key_is_minted_rather_than_demanded(tmp_path):
    """Requiring the app to have been started once was an ordering rule for
    no reason: the key is a file, and the app's own function makes it."""
    body = _script("n8n.sh").read_text(encoding="utf-8")

    assert "load_or_create" in body

    from odoo_migrator.authkey import KEY_FILENAME, load_or_create

    minted = load_or_create(tmp_path / KEY_FILENAME)

    assert (tmp_path / KEY_FILENAME).stat().st_mode & 0o077 == 0
    assert load_or_create(tmp_path / KEY_FILENAME) == minted
