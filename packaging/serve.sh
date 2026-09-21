#!/bin/sh
# Start the app.
#
#   packaging/serve.sh              loopback, the default
#   packaging/serve.sh --for-n8n    bind 0.0.0.0 so a container can reach it
#   packaging/serve.sh --reload     restart on Python changes (development)
#
# PORT overrides 8000. Extra arguments are passed to odoo-migrator.
#
# --for-n8n exists because a container cannot call loopback and Docker
# Desktop leaves no narrower address than 0.0.0.0 to bind. It is a separate
# flag rather than the default so that widening stays a decision: the pages
# have no authentication, and the key only guards the File API and the
# Sheller proxy. The app prints what it is exposing when you use it.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

die() { echo "$@" >&2; exit 1; }

host=127.0.0.1
args=""
for arg in "$@"; do
    case "$arg" in
        --for-n8n) host=0.0.0.0 ;;
        *) args="$args $arg" ;;
    esac
done

if command -v uv >/dev/null 2>&1; then
    set -- uv run odoo-migrator
elif [ -x .venv/bin/odoo-migrator ]; then
    set -- .venv/bin/odoo-migrator
else
    die "no uv and no .venv/bin/odoo-migrator. Run: uv sync"
fi

# shellcheck disable=SC2086
exec "$@" --host "$host" --port "${PORT:-8000}" $args
