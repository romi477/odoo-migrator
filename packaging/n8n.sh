#!/bin/sh
# Bring the stock n8n up, and publish the cartridge into it.
#
#   packaging/n8n.sh up         start it and wait until it is really ready
#   packaging/n8n.sh publish    create and activate the stock workflows
#   packaging/n8n.sh status     what compose thinks is running
#   packaging/n8n.sh logs       follow
#   packaging/n8n.sh down       stop, keeping the volume
#
# N8N_HOST_PORT overrides 5678, which is often already taken. The address
# n8n generates its webhook URLs from follows it, so the two cannot drift.
#
# `down` never passes -v. The workflows live in n8n's own database inside
# that volume, not in this repo, and removing it throws away everything
# publish created plus any hand edit made on the canvas.
set -eu

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

COMPOSE_FILE=n8n/compose.yml
KEY_FILE="$HOME/.odoo-migrator/api.key"
PORT="${N8N_HOST_PORT:-5678}"
BASE_URL="${N8N_BASE_URL:-http://127.0.0.1:$PORT}"

die() { echo "$@" >&2; exit 1; }

# Only `up` needs the key. `die` inside a command substitution kills the
# subshell and nothing else, so it is resolved here as its own statement
# and checked — otherwise a missing key file printed its complaint and the
# script carried on to hand docker a mangled command line.
key=""
require_key() {
    # Mint it here if the app has not run yet, through the app's own
    # function rather than a second implementation: same path, same 0600,
    # same value the app will read back. Otherwise this script would only
    # work after someone had started the app once, which is an ordering
    # rule for no reason.
    if [ ! -f "$KEY_FILE" ]; then
        python_run -c 'from odoo_migrator.authkey import KEY_FILENAME, load_or_create
from odoo_migrator.db import default_db_path
load_or_create(default_db_path().parent / KEY_FILENAME)' >/dev/null
    fi
    [ -f "$KEY_FILE" ] || die "could not mint $KEY_FILE. Run: uv sync"
    # Read, never print: this tool gets demonstrated on a projector.
    key="$(cat "$KEY_FILE")"
    [ -n "$key" ] || die "$KEY_FILE is empty. Delete it and start the app again."
}

python_run() {
    if command -v uv >/dev/null 2>&1; then
        uv run python "$@"
    elif [ -x .venv/bin/python ]; then
        .venv/bin/python "$@"
    else
        die "no uv and no .venv/bin/python. Run: uv sync"
    fi
}

compose() {
    command -v docker >/dev/null 2>&1 || die "docker not found"
    N8N_HOST_PORT="$PORT" \
    N8N_WEBHOOK_URL="$BASE_URL" \
    MIGRATOR_API_KEY="$key" \
        docker compose -f "$COMPOSE_FILE" "$@"
}

# /healthz answers 200 while n8n is still starting up and serving the string
# "n8n is starting up. Please wait". /rest/settings returning JSON is the
# first moment the API will actually answer.
wait_ready() {
    command -v curl >/dev/null 2>&1 || { echo "no curl; skipping the readiness wait" >&2; return 0; }
    i=0
    while [ "$i" -lt 120 ]; do
        body="$(curl -fsS "$BASE_URL/rest/settings" 2>/dev/null || true)"
        case "$body" in
            \{*) echo "n8n ready at $BASE_URL"; return 0 ;;
        esac
        i=$((i + 1))
        sleep 1
    done
    die "n8n did not become ready at $BASE_URL. Try: packaging/n8n.sh logs"
}

# Refuse before touching anything. `up` recreates the container and only
# then fails on the bind, so a port clash leaves the stack in a worse
# state than it started in. On this machine 5678 is an Odoo container's.
port_is_free() {
    if command -v lsof >/dev/null 2>&1; then
        ! lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1
    elif command -v nc >/dev/null 2>&1; then
        ! nc -z 127.0.0.1 "$PORT" >/dev/null 2>&1
    else
        return 0
    fi
}

require_port() {
    port_is_free && return 0
    # Ours already serving there is not a clash: `up` is idempotent.
    if [ -n "$(compose ps -q n8n 2>/dev/null)" ] && compose ps n8n 2>/dev/null | grep -q "Up "; then

        return 0
    fi
    die "port $PORT is already listening, and it is not our n8n.
  Pick another: N8N_HOST_PORT=5679 $0 up
  The address n8n builds its webhook URLs from follows it, so the two cannot drift."
}

case "${1:-up}" in
    up)
        require_key
        require_port
        compose up -d
        wait_ready
        echo
        echo "Next: create the owner account and an API key in n8n's UI,"
        echo "then: N8N_API_KEY=... packaging/n8n.sh publish"
        ;;
    publish)
        [ -n "${N8N_API_KEY:-}" ] || die "N8N_API_KEY is unset. n8n mints it in its own UI (Settings > n8n API)."
        wait_ready
        # Exported, not an assignment prefix: a prefix on a function call
        # is not portable and leaks into the shell in some of them.
        N8N_BASE_URL="$BASE_URL"
        export N8N_BASE_URL
        python_run packaging/publish_stock.py
        ;;
    status) compose ps ;;
    logs)   compose logs -f ;;
    down)   compose down ;;
    *) die "usage: $0 [up|publish|status|logs|down]" ;;
esac
