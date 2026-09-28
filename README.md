# odoo-migrator

Automatic migration of Odoo addon projects between framework versions
(17.0 → 18.0, and multi-version jumps). Input: a project with dozens or
hundreds of custom modules. Output: the same project ported, module by
module, verified against two live Odoo containers — the old version to
learn from, the new one to land in.

The app owns the queue, the batches, the rules and the review. A cartridge
executes one module inside this process. odoo-sheller is how both sides
talk to a live Odoo.

```
Browser (Jinja + htmx + Alpine + SSE)
 │
FastAPI app ── SQLite: queue, batches, notes, findings, assembled rules
 │
 ├─ runner (stub, or native when ODOO_MIGRATOR_RUNNER=native)
 │
 └─> Sheller daemon 127.0.0.1:8765 ──> old container / new container
```

Per module: understand the old version, rewrite, install the upgrade on the
new container, test, judge, retry. The retry loop is the cartridge's
failure lane. The default executor is a stub, so the UI and the suite run
with nothing else up. `ODOO_MIGRATOR_RUNNER=native` selects the live runner.

## Table of Contents

- [Background](#background)
- [Security](#security)
- [Requirements](#requirements)
- [Install](#install)
- [Usage](#usage)
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
16.0 → 20.0 possible at all. `rules/common.md` is shared context and is
sent in front of that stack. Prompts and rules travel in the job payload
as resolved text.

Full documentation: [docs/](docs/README.md) — architecture, the UI guide,
the security model, and an FAQ in
[English](docs/faq.md) and [Russian](docs/faq-ru.md). The design of record
for the runner is
[`docs_/superpowers/specs/2026-09-23-native-runner-design.md`](docs_/superpowers/specs/2026-09-23-native-runner-design.md).

## Security

This pipeline executes arbitrary code as `SUPERUSER_ID` against real
databases, and edits source files on the host.

- The pages have **no authentication**. The app binds **loopback**. There
  is no flag that widens it.
- The daemon publishes to `127.0.0.1` only. This process is the only
  client. Never read `admin.key`, never touch `~/.odoo-sheller/`, never
  call the daemon's admin endpoints.
- Never publish Sheller's `8765` beyond loopback. A daemon that already
  answers on 8765 may not be ours: use it, never stop it.

The reasoning behind each of these is in [docs/security.md](docs/security.md).

## Requirements

| | |
|---|---|
| OS | macOS or Linux |
| Python | 3.12 or newer |
| Package manager | [uv](https://docs.astral.sh/uv/) (preferred) or pip + venv |
| Docker | Docker Engine, for the two Odoo compose projects |
| Odoo | two live containers — old version and new — each with its own compose project |
| [odoo-sheller](https://github.com/romi477/odoo-sheller) | a daemon on `127.0.0.1:8765`, or one the app can spawn |

Without `ODOO_MIGRATOR_RUNNER=native` the app uses an in-process stub
executor, so the UI and the suite run without Docker, Odoo, or a model key.

## Install

Clone the repository, then create the environment:

```bash
packaging/setup.sh
```

That runs `uv sync` when `uv` is on `PATH`, and otherwise `python3 -m venv`
plus `pip install -e ".[dev]"`. It does not start Docker or the app.

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

Or `packaging/serve.sh`, which is the same thing, after `packaging/setup.sh`.
`PORT=` moves the port. The live runner is read once at startup from
`~/.odoo-migrator/.env`:

```bash
ODOO_MIGRATOR_RUNNER=native
```

A variable already set in the shell wins over that file. There is no
settings field for it. The process prints `runner: native` or
`runner: stub` next to the URL. Restart after editing the file.
Extra arguments are passed through (`--reload` for development).

Without uv: `.venv/bin/odoo-migrator`, or `python -m odoo_migrator`. Flags:
`--port` (default `8000`) and `--reload` for development. The bind is
`127.0.0.1`. Files under `odoo_migrator/web/` are excluded from the
watch — reload the browser page instead.

Then open <http://127.0.0.1:8000/>. Typical loop:

1. **Create a migration** on Overview — name, accent, source and target
   versions, old and new project paths. Same-version and downgrade are
   refused here, where they were typed.
2. **App settings** — Sheller base URL (default `http://127.0.0.1:8765`),
   the image tag when the app must spawn the daemon, and a provider
   secret when the native runner should call a model. A saved key shows
   as dots. The bin at the end of the field removes it.
3. **This migration** — attach or Up the two compose projects, pin a
   flow and optionally override its model, smoke-run one module before
   a batch.
4. **Tree** — Resolve the graph from the live old Odoo. Skip, group,
   Migrate one module or Batch a group.
5. **Run** — watch the pipeline, observe Sheller without a write key.
   At checkpoint: diffs, attempts, findings to promote, Continue or
   Override.

The process prints:

```
ui: http://127.0.0.1:8000/
```

SQLite lives at `~/.odoo-migrator/migrator.sqlite`.

## Tests

`uv sync` already installs the development group.

```bash
uv run pytest
uv run ruff check odoo_migrator tests
```

The suite must pass with no Docker, no Odoo and no daemon running. Every
external boundary ships a test double in the task that introduced it: a
fake Sheller, a fake compose engine, a stub executor. Doubles answer the
way the real thing answers.

A `@pytest.mark.live` test talks to a Sheller daemon that may already be
serving on 8765. It depends on that daemon's sessions, so the default run
leaves it out; run it on purpose:

```bash
uv run pytest -m live
```

## API

There is no Swagger. `docs_url` is off: the pages are a local tool, and
nothing machine-facing is left.

The pages (Overview, Tree, Pipeline, Flow, Sheller, Result, Settings)
ask nobody for a key.

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Overview |
| `GET` | `/tree` | Module graph |
| `GET` | `/run` | Pipeline and checkpoint |
| `GET` | `/flow` | The pinned cartridge, drawn |
| `GET` | `/sheller` | Sheller observer |
| `GET` | `/result` | Migration outcomes dashboard |
| `GET` | `/settings` | App settings |
| `GET` | `/settings/migration` | This migration: containers, flow, model, smoke-run |
| `GET` | `/sse` | One named-event stream per page |
| `GET` | `/styleguide` | What each migration step is for |
| `GET` | `/components` | Every CSS component, rendered once |

## Development

How it works internally — source of truth, cartridges, sessions, diffs:
[docs/architecture.md](docs/architecture.md). Layout:

| Path | Role |
|---|---|
| `odoo_migrator/api.py` | FastAPI factory and pages |
| `odoo_migrator/models.py` | Peewee models. Schema changes: `playhouse.migrate` |
| `odoo_migrator/jobs.py` | Start a job, apply a result, resume, sequential batch |
| `odoo_migrator/runner/` | Cartridge language, loop, tools, providers, native thread |
| `odoo_migrator/contract.py` | Job payload and result validation |
| `odoo_migrator/cartridges.py` | Import, pin, smoke-run |
| `odoo_migrator/diffs.py` | Unified diffs recorded on the in-flight job |
| `odoo_migrator/rules.py` | Per-version files, assembled stack, promote |
| `odoo_migrator/web/` | Jinja in `templates/`, assets in `static/` — only `static/` is mounted |
| `cartridges/stock.yaml` | The stock flow |
| `rules/` | `common.md` and `16.0.md` … `20.0.md`, edited at the repo root. A wheel carries them, with `prompts/` and `cartridges/`, as `odoo_migrator/bundled_*`; the checkout wins when there is one |
| `prompts/system.md` | System prompt, resolved into the agent step |
| `prompts/sheller.md` | What the Odoo shell can do, vendored from odoo-sheller's `os_help`: the daemon serves that text over MCP and this app is an HTTP client |
| `packaging/` | `setup.sh` creates the virtualenv; `serve.sh` starts the app. The desktop build lands here later, as in odoo-sheller |
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
| Job becomes `unknown` after an answer | The thread that was waiting is gone, usually after a restart. The answer stays on the checkpoint thread |
| Answered, and the row is `running` but nothing continues | The stub reports that it can resume and then has no thread left to run. A real continuation needs `ODOO_MIGRATOR_RUNNER=native` and the same process that parked the job |
| `A smoke-run must pass before a batch.` | An installed cartridge whose smoke-run is not `passed`. With nothing installed the stub is allowed |
| `database is locked` | A Peewee connection crossed a thread. Every operation is its own `connection_context()` |
| `address already in use` on start | Another copy is already serving. uvicorn prints "Application startup complete" before it reports the bind failure, so a failed start reads like a successful one — check `lsof -nP -iTCP:8000 -sTCP:LISTEN` |
| The process survives Ctrl-C / SIGTERM | Fixed: open SSE streams are woken on the signal. Before that the port was freed and the process stayed up for the rest of the stream's window |
