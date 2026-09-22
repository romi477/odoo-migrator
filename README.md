# odoo-migrator

Automatic migration of Odoo addon projects between framework versions
(17.0 → 18.0, and multi-version jumps). Input: a project with dozens or
hundreds of custom modules. Output: the same project ported, module by
module, verified against two live Odoo containers — the old version to
learn from, the new one to land in.

The app owns the queue, the batches, the rules and the review. n8n is a
replaceable cartridge that executes one module. odoo-sheller is how both
sides talk to a live Odoo.

```
Browser (Jinja + htmx + Alpine + SSE)
 │
FastAPI app ── SQLite: queue, batches, notes, findings, assembled rules
 │
 ├─ POST webhook ──────────> n8n (containerized, pure executor)
 │ │ AI Agent stages + deterministic stages
 │ <── progress / result ───────┤
 │ <── File API ────────────────┤
 │ <── Sheller proxy ───────────┘
 │
 └─> Sheller daemon 127.0.0.1:8765 ──> old container / new container
```

Per module: understand the old version, rewrite, install the upgrade on the
new container, test, judge, retry. The retry loop is a real loop on the n8n
canvas, not a model looping inside one node.

## Table of Contents

- [Background](#background)
- [Security](#security)
- [Requirements](#requirements)
- [Install](#install)
- [Usage](#usage)
- [n8n](#n8n)
- [Tests](#tests)
- [API](#api)
- [Development](#development)
- [Troubleshooting](#troubleshooting)

## Background

odoo-sheller already gives a live, persistent `odoo-bin shell` against one
running Odoo. This tool solves a different problem: taking a custom project
through a whole framework version, with a human able to check in and correct
course without babysitting every module.

Rules are one file per version (`rules/18.0.md`), never per pair. The app
assembles the ordered stack for a migration — that is also what makes
16.0 → 19.0 possible at all. Prompts and rules travel in the webhook payload
as resolved text, never as file paths inside an n8n node.

Full documentation: [docs/](docs/README.md) — architecture, the UI guide,
the security model, the n8n cartridge, and an FAQ in
[English](docs/faq.md) and [Russian](docs/faq-ru.md). The design of record
that the implementation was built against lives under
[`docs_/`](docs_/superpowers/specs/2026-09-20-odoo-migrator-design.md).

## Security

This pipeline executes arbitrary code as `SUPERUSER_ID` against real
databases, and edits source files on the host.

- The pages have **no authentication**. The app binds **loopback** by
  default. `--host` widens it only because a container cannot reach
  loopback — Docker Desktop leaves no narrower address than `0.0.0.0`.
  Widening is per run, never a default.
- The machine-facing surface — File API and Sheller proxy — requires
  `X-Odoo-Migrator-Key`, minted into `~/.odoo-migrator/api.key` on first
  start. The process prints the **path**, never the value: this tool gets
  demonstrated on a projector.
- The File API is scoped to the configured old and new project roots. A
  path that escapes them is a **403**, not a clamp.
- The Sheller proxy allowlist runs on the *collapsed* path. n8n never
  talks to `127.0.0.1:8765` itself — inside a container that address is
  the container. The app proxies. Never read `admin.key`, never touch
  `~/.odoo-sheller/`, never call the daemon's admin endpoints.
- Never publish Sheller's `8765` beyond loopback. A daemon that already
  answers on 8765 may not be ours: use it, never stop it.

The reasoning behind each of these is in [docs/security.md](docs/security.md).

## Requirements

| | |
|---|---|
| OS | macOS or Linux |
| Python | 3.12 or newer |
| Package manager | [uv](https://docs.astral.sh/uv/) (preferred) or pip + venv |
| Docker | Docker Engine, for the two Odoo compose projects and (optionally) n8n |
| Odoo | two live containers — old version and new — each with its own compose project |
| [odoo-sheller](https://github.com/romi477/odoo-sheller) | a daemon on `127.0.0.1:8765`, or one the app can spawn |

n8n is optional. With no `n8n_base_url` the app uses an in-process stub
executor that speaks the same webhook contract, so the UI and the suite run
without Docker, Odoo or n8n.

## Install

Clone the repository, then sync the environment from the lockfile.

### With uv (preferred)

```bash
uv sync
```

`uv.lock` pins the full dependency tree. Do not edit it by hand; regenerate
with `uv lock` after changing `pyproject.toml`.

### With pip

```bash
python3 -m venv .venv
.venv/bin/pip install -e ".[dev]"
```

The `dev` extra (pytest, httpx2, ruff) matches uv's default `dev` group. It
is `httpx2`, not `httpx`: `starlette.testclient` imports `httpx2` and warns
that `httpx` is deprecated there.

## Usage

Start the app from the project root:

```bash
uv run odoo-migrator
```

Or `packaging/serve.sh`, which is the same thing without the flags to
remember (`--for-n8n` widens the bind; `PORT=` moves the port).

Without uv: `.venv/bin/odoo-migrator`, or `python -m odoo_migrator`. Flags:
`--host` (default `127.0.0.1`), `--port` (default `8000`), and `--reload`
for development. Files under `odoo_migrator/web/` are excluded from the
watch — reload the browser page instead.

Then open <http://127.0.0.1:8000/>. Typical loop:

1. **Create a migration** on Overview — name, accent, source and target
   versions, old and new project paths. Same-version and downgrade are
   refused here, where they were typed.
2. **App settings** — `app_base_url` that the n8n *container* can call
   (not host loopback), Sheller base URL (default
   `http://127.0.0.1:8765`), optional n8n base URL and API key.
3. **This migration** — attach or Up the two compose projects, pin a
   flow and a model wrapper, smoke-run one module before a batch.
4. **Tree** — Resolve the graph from the live old Odoo. Skip, group,
   Migrate one module or Batch a group.
5. **Run** — watch the pipeline, open n8n in its own tab, observe Sheller
   without a write key. At checkpoint: diffs, attempts, findings to
   promote, Continue or Override.

The process prints:

```
ui: http://127.0.0.1:8000/
api key: /Users/<you>/.odoo-migrator/api.key
```

SQLite lives at `~/.odoo-migrator/migrator.sqlite`. Copy the key out of the
file when you configure n8n (`MIGRATOR_API_KEY`); do not paste it into a
slide.

### Reaching n8n

A container cannot call loopback. Serving the File API and the callbacks
means widening the bind **deliberately**:

```bash
uv run odoo-migrator --host 0.0.0.0
```

`0.0.0.0` is every interface, not "the container gateway". The pages have
nothing in front of them. On Linux the n8n compose file uses
`extra_hosts: host.docker.internal:host-gateway`; set `app_base_url` to an
address that container can actually call. A payload carrying `127.0.0.1`
works on Docker Desktop and fails only on Linux — the app refuses loopback
URLs in the trigger rather than discovering that at runtime.

## n8n

Stock cartridge: `n8n/stock/`, pinned to `docker.n8n.io/n8nio/n8n:2.39.9`
in `n8n/compose.yml`. Publishing is `POST /api/v1/workflows/{id}/activate`;
every sub-workflow an Execute Workflow node points at must be active first
or the parent is 400 and the production webhook stays 404.

The webhook path is chosen per batch (`migrate-module`, or a thin wrapper
`migrate-module-openai` / `migrate-module-claude`). A smoke-run that has
not passed blocks a batch once a flow is installed.

```bash
packaging/n8n.sh up                          # start it, wait until really ready
N8N_API_KEY=... packaging/n8n.sh publish     # create and activate the workflows
```

Order does not matter: `n8n.sh` mints `~/.odoo-migrator/api.key` itself if
the app has not run yet, through the same function the app uses.

`up` waits for `/rest/settings` to return JSON, because `/healthz` answers
200 while n8n is still starting, and refuses a port that is already
listening rather than recreating the container and failing on the bind.
5678 is often taken — an Odoo container publishes it on the machine this
was written on. Choose once, and both the script and compose read it:

```bash
echo N8N_HOST_PORT=5679 >> n8n/.env
```

The address n8n builds its webhook URLs from follows the port, so the two
cannot drift. `down` keeps the volume: the workflows live in n8n's
database, not in this repo.

The one manual step is inside n8n: create the owner account, then mint an
API key under Settings → n8n API. A fresh n8n has no API before that.

Compose, the webhook contract, Wait/resume, and the wrappers:
[docs/n8n.md](docs/n8n.md).

## Tests

`uv sync` already installs the development group.

```bash
uv run pytest
uv run ruff check odoo_migrator tests
```

The suite must pass with no Docker, no Odoo and no daemon running. Every
external boundary ships a test double in the task that introduced it: a
fake Sheller, a fake compose engine, a fake n8n, a stub executor. Doubles
answer the way the real thing answers.

A `@pytest.mark.live` test talks to a Sheller daemon that may already be
serving on 8765. It depends on that daemon's sessions; skip it when the
port is busy with someone else's work.

```bash
uv run pytest -m "not live"
```

## API

There is no Swagger. `docs_url` is off: the pages are a local tool, and
the machine-facing surface is keyed.

The **pages** (Overview, Tree, Pipeline, n8n, Sheller, Result, Settings) ask nobody for a key. The
**machine-facing** routes require `X-Odoo-Migrator-Key`.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Overview |
| `GET` | `/tree` | Module graph |
| `GET` | `/run` | Pipeline and checkpoint |
| `GET` | `/n8n` | n8n executor link |
| `GET` | `/sheller` | Sheller observer |
| `GET` | `/result` | Migration outcomes dashboard |
| `GET` | `/settings` | App settings |
| `GET` | `/settings/migration` | This migration: containers, flow, smoke-run |
| `GET` | `/sse` | One named-event stream per page |
| `GET` | `/styleguide` | What each migration step is for |
| `GET` | `/components` | Every CSS component, rendered once |
| `GET` | `/api/migrations/{id}/files` | Read a text file under old or new root (`?side=&path=`) |
| `PUT` | `/api/migrations/{id}/files` | Write (`{"side","path","content"}`). Escape is 403 |
| `POST` | `/api/jobs/{job_id}/progress` | Stage + attempt while n8n still holds the job |
| `POST` | `/api/jobs/{job_id}/result` | Verdict: `passed` / `failed` / `waiting_for_human` |
| `*` | `/api/{rest}` | Sheller proxy (sessions, containers, probe). Journals and admin are refused |
| `GET` | `/health` | Proxied to the Sheller daemon |

Example (after the app is up and a migration exists):

```bash
KEY=$(cat ~/.odoo-migrator/api.key)
curl -sS -H "X-Odoo-Migrator-Key: $KEY" \
  "http://127.0.0.1:8000/api/migrations/<id>/files?side=old&path=sale_custom/__manifest__.py"
```

n8n is configured with exactly one host address — `app_base_url` — and
that same key as `$env.MIGRATOR_API_KEY`.

## Development

How it works internally — source of truth, cartridges, sessions, diffs:
[docs/architecture.md](docs/architecture.md). Layout:

| Path | Role |
|---|---|
| `odoo_migrator/api.py` | FastAPI factory, pages, File API, proxy |
| `odoo_migrator/models.py` | Peewee models. Schema changes: `playhouse.migrate` |
| `odoo_migrator/jobs.py` | Start a job, apply callbacks, sequential batch |
| `odoo_migrator/contract.py` | Webhook payload and result validation |
| `odoo_migrator/cartridges.py` | Manifest, smoke-run, pin |
| `odoo_migrator/diffs.py` | Unified diffs from File API writes |
| `odoo_migrator/rules.py` | Per-version files, assembled stack, promote |
| `odoo_migrator/n8n/` | Client, fake, stock publisher, executor, janitor |
| `odoo_migrator/web/` | Jinja in `templates/`, assets in `static/` — only `static/` is mounted |
| `rules/` | `16.0.md` … `19.0.md` at the repo root, not inside the package |
| `prompts/system.md` | System prompt, resolved into the payload |
| `n8n/stock/` | Stock cartridge JSON + `manifest.json` |
| `packaging/` | `serve.sh`, `n8n.sh`, `publish_stock.py`. The desktop build lands here later, as in odoo-sheller |
| `tests/` | Suite; no live Docker required except `@pytest.mark.live` |

Code, comments, docs and commits are in English. Discussion happens in
Russian. `return` gets a blank line before it; run `ruff check`, not
`ruff format` — format strips that line.

Every new UI component gets a row on `/components` in the same change.
Appearance lives in the stylesheet. Inline `style=` is permitted only for
custom properties (`--accent`).

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| n8n 403 on every File API / callback | Empty `$env.MIGRATOR_API_KEY`. Compose must set `N8N_BLOCK_ENV_ACCESS_IN_NODE=false` |
| Production webhook 404 | Parent activated while a child was unpublished. Activate children, then the parent, then wrappers |
| Job stuck `waiting_for_human` after an answer | The Wait node's output is the resume request (`$json.body.answer`), not the job. Stock canvas rebuilds from `Before wait` |
| Checkpoint diff empty on a pass | The cartridge sent no attempts. Stock `Mark passed` logs one; the app also creates a row from File API writes |
| `A smoke-run must pass before a batch.` | An installed cartridge whose smoke-run is not `passed`. With nothing installed the stub is allowed |
| File API 403 | Path left the old or new project root, including a `..` that would have clamped back inside |
| `database is locked` | A Peewee connection crossed a thread. Every operation is its own `connection_context()` |
| `address already in use` on start | Another copy is already serving. uvicorn prints "Application startup complete" before it reports the bind failure, so a failed start reads like a successful one — check `lsof -nP -iTCP:8000 -sTCP:LISTEN` |
| The process survives Ctrl-C / SIGTERM | Fixed: open SSE streams are woken on the signal. Before that the port was freed and the process stayed up for the rest of the stream's window |
| Pages on `0.0.0.0` reachable from the LAN | `--host` was widened for n8n. The pages have no key. Bind loopback when n8n is not in the picture |
