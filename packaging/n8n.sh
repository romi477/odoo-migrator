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
    [ -f "$KEY_FILE" ] || die "no $KEY_FILE yet. Start the app once (packaging/serve.sh) — it mints the key on first run."
    # Read, never print: this tool gets demonstrated on a projector.
    key="$(cat "$KEY_FILE")"
    [ -n "$key" ] || die "$KEY_FILE is empty. Delete it and start the app again."
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

case "${1:-up}" in
    up)
        require_key
        compose up -d
        wait_ready
        echo
        echo "Next: create the owner account and an API key in n8n's UI,"
        echo "then: N8N_API_KEY=... packaging/n8n.sh publish"
        ;;
    publish)
        [ -n "${N8N_API_KEY:-}" ] || die "N8N_API_KEY is unset. n8n mints it in its own UI (Settings > n8n API)."
        wait_ready
        if command -v uv >/dev/null 2>&1; then
            N8N_BASE_URL="$BASE_URL" uv run python packaging/publish_stock.py
        else
            N8N_BASE_URL="$BASE_URL" .venv/bin/python packaging/publish_stock.py
        fi
        ;;
    status) compose ps ;;
    logs)   compose logs -f ;;
    down)   compose down ;;
    *) die "usage: $0 [up|publish|status|logs|down]" ;;
esac
