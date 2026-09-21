# Architecture

How odoo-migrator works internally: the app as source of truth, n8n as a
replaceable cartridge, Sheller sessions, the rules stack, and how a
checkpoint learns what the rewrite did to a module. Start with the README
if you just want to run the thing — this is for understanding or extending
it.

## The pieces

```
Browser (Jinja + htmx + Alpine + SSE)
 │
FastAPI app ── SQLite the source of truth: queue, batches,
 │ rules/*.md statuses, notes, findings, assembled rules
 │
 ├─ POST webhook ──────────> n8n (containerized, pure executor)
 │ │ AI Agent stages + deterministic stages
 │ <── progress / result ───────┤
 │ <── File API ────────────────┤
 │ <── Sheller proxy ───────────┘
 │
 └─> Sheller daemon 127.0.0.1:8765 ──> old container / new container
```

- **The app** is the only piece that accumulates anything across jobs.
  SQLite holds migrations, modules, batches, jobs, attempts, findings,
  assumptions. n8n stages pass data through one run and ship `findings`
  home in the callback.
- **The web UI** is a client of that app. One SSE connection per page,
  named events — not one stream per row. HTTP/1.1 allows roughly six
  connections per origin, and a batch view has dozens of module rows.
- **n8n** is a replaceable cartridge, selected per project, pinned for a
  run, recorded on every result. The webhook contract is the product's
  API: versioned, declared by each cartridge, validated before a run.
- **Sheller** is a sibling daemon. The migrator is a client. Sessions it
  opens are `owner.kind = agent` and carry the `migration_id`. Commit is
  allowed on the new container and refused on the old one. The daemon
  publishes to `127.0.0.1` only, so n8n cannot reach it; the app proxies.

Peewee, not SQLAlchemy. Alpine, not React. No Celery, no MCP, no LiteLLM
in this MVP. Sync ORM behind async routes: every operation inside its own
`db.connection_context()`, and a connection never crosses a thread
boundary. Schema changes use `playhouse.migrate`, never Alembic.

## Per module

One job is one module. The app builds the trigger, posts the production
webhook, and waits. The stock loop:

1. **Understand** the old version (read-only Sheller session, held for
   the whole run).
2. **Rewrite** into the new project through the File API.
3. **Upgrade** on a fresh session on the new container, then commit.
4. **Tests**. `strictness_level` travels in the payload; the stock
   cartridge does not branch on it yet, so `enhanced` currently runs
   what `basic` runs.
5. **Diagnose** on failure. Retry is a canvas loop gated by
   `retry_limit`, not `Max Iterations` on an agent node.
6. **Ask a human** (`sync` parks on a Wait node;
   `batch_defer` records an assumption and continues).
7. **Callback** with status, attempts, findings, assumptions.

A rewrite invalidates any session open on the *new* container, so a fresh
one opens after it. The old container is read-only for the whole run, so
one session opened at the start stays valid — do not apply the stricter
rule to both sides.

## Rules are per version, not per pair

`rules/18.0.md` is the document for landing on 18.0. A 17→18 migration
sends two entries (source + target); 16→19 sends four. Pair documents
would grow as the square of the versions, and a 16→19 jump would have
nothing to read.

The app concatenates source and every version after it through target,
stamps a hash of that stack on the job, and sends the texts in the
payload. Editing a rules file mid-batch changes the next module's prompt;
the hash records what this job actually saw. Promoting a finding at
checkpoint **asks which version file** — the app does not pick one.

## Cartridges

A cartridge ships a manifest (`id`, `version`, `contract_version`,
webhook path, wrappers, capabilities). Unknown `contract_version` is
refused. Every job stores the cartridge id and version at start; swapping
the pin does not rewrite past jobs.

Thin wrappers (`migrate-module-openai`, `migrate-module-claude`) are extra
webhook parents that Execute Workflow the shared `migrate-one-module`
loop. The batch records which webhook path it fired.

A smoke-run starts the smallest module through the pinned cartridge. The
verdict is whatever the job ends as — reading `running` the moment after
the trigger is how a live cartridge used to record `failed` every time
and block every batch. With nothing installed the app is on its own stub
and a batch is allowed; once a cartridge exists, an unknown pin is
refused.

## Diffs

The rewrite agent writes through the File API. The cartridge's
`attempt.diff` is often empty, so the app compares before and after on
each new-side write for the in-flight job, keyed by attempt, and fills
an empty attempt when the result lands.

A cartridge that already sent a diff keeps it. Writes to the old side,
and writes whose path does not contain the module's technical name as a
folder, are not this job's diff. A module that passes first time still
gets an attempt row: stock `Mark passed` logs one, and `apply_result`
creates a row from `file_diffs` even if the cartridge sent
`attempts: []`.

## Layout of the package

| Path | Role |
|---|---|
| `odoo_migrator/api.py` | Factory, pages, File API, Sheller proxy, SSE |
| `odoo_migrator/models.py` | Peewee. `create_tables` plus `_ensure_columns` |
| `odoo_migrator/jobs.py` | Trigger, progress, result, sequential batch, checkpoint |
| `odoo_migrator/contract.py` | Payload build, loopback refusal, result validation |
| `odoo_migrator/cartridges.py` | Manifest, install, smoke-run gate |
| `odoo_migrator/diffs.py` | Unified diffs from File API writes |
| `odoo_migrator/n8n/` | HTTP client, fake, stock publisher, executor, janitor |
| `odoo_migrator/sheller/` | HTTP client, fake, three-level daemon resolver |
| `odoo_migrator/provision/` | Docker SDK compose up / attach / rebuild |
| `odoo_migrator/web/templates/` | Jinja only — never inside the static mount |
| `odoo_migrator/web/static/` | Served at `/static` |

`rules/` and `prompts/` live at the repo root, not as package data. A
`pip install` of the wheel would not carry them; `repo_dir()` already
returns `package_dir()` when frozen, but nothing puts the files there.
That only bites at packaging.

## What this round is not

Tauri, an in-app n8n canvas, LiteLLM, a custom `n8n-nodes-sheller`, the
app performing `docker login`, a UI editor for rules files, Windows.
Each has a written reason in the design spec or the plan's "Explicitly
out of this plan".
