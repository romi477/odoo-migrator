#!/bin/sh
# Start the app on loopback.
#
#   packaging/serve.sh           the usual start
#   packaging/serve.sh --reload  restart on Python changes (development)
#
# PORT overrides 8000. Extra arguments are passed to odoo-migrator.
# The bind is loopback. There is no flag that widens it: the pages have
# no authentication, and nothing machine-facing is left to protect.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

die() { echo "$@" >&2; exit 1; }

user_args="$*"

if command -v uv >/dev/null 2>&1; then
    set -- uv run odoo-migrator
elif [ -x .venv/bin/odoo-migrator ]; then
    set -- .venv/bin/odoo-migrator
else
    die "no uv and no .venv/bin/odoo-migrator. Run: uv sync"
fi

# shellcheck disable=SC2086
exec "$@" --port "${PORT:-8000}" $user_args
