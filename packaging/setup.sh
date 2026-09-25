#!/bin/sh
# Create the virtualenv and install this project into it.
#
#   packaging/setup.sh
#
# Uses uv and uv.lock when uv is on PATH. Otherwise python3 -m venv and
# pip install -e ".[dev]". Does not start Docker, Odoo, or the app.
# Start afterwards with packaging/serve.sh.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

die() { echo "$@" >&2; exit 1; }

[ "$#" -eq 0 ] || die "usage: packaging/setup.sh"

if command -v uv >/dev/null 2>&1; then
    exec uv sync
fi

python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3, 12) else 1)' \
    || die "Python 3.12 or newer is required"
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
echo "ready: .venv"
echo "start: packaging/serve.sh"
