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


def test_n8n_refuses_before_the_key_exists(tmp_path):
    """`die` inside a command substitution kills the subshell and nothing
    else, so a missing key file printed its complaint and the script carried
    on to hand docker a mangled command line."""
    env = {**os.environ, "HOME": str(tmp_path)}

    refused = subprocess.run(
        [str(_script("n8n.sh")), "up"],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )

    assert refused.returncode != 0
    assert "Start the app once" in refused.stderr
    assert "docker" not in refused.stdout


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
