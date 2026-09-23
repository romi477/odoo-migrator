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


def test_the_script_is_executable_and_parses():
    path = _script("serve.sh")

    assert path.is_file()
    assert path.stat().st_mode & stat.S_IXUSR, "serve.sh is not executable"
    # sh -n is a syntax check that runs nothing.
    assert subprocess.run(["sh", "-n", str(path)], check=False).returncode == 0


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
