# odoo-migrator

Automatic migration of Odoo addon projects between framework versions
(17.0 → 18.0, and multi-version jumps). Input: a project with dozens or
hundreds of custom modules. Output: the same project ported, module by
module, verified against two live Odoo containers — the old version to
learn from, the new one to land in.

**Status: in build.** Task 17 of 17 done (checkpoint diffs from File API writes).
Nothing migrates anything yet. Run `odoo-migrator` (add `--reload` for
development); `uv run pytest`.

| Document | What it holds |
|---|---|
| `README.md` | install, quick start, API table, security short version |
| `docs/` | product docs in the Sheller template: architecture, UI, n8n, security, FAQ (en/ru) |
| `docs_/superpowers/specs/2026-09-20-odoo-migrator-design.md` | the design of record |
| `docs_/n8n-workflow-flow.md` | the workflow graph, node by node, and how nodes are built |
| `docs_/n8n-spike-findings.md` | Task 3b: what a real n8n 2.39.9 run did to the webhook contract |
| `.cursor/plans/web_mvp_sequential_684f94cd.plan.md` | the implementation plan — 16 review gates, then diffs |
| `docs_/migrator-web-mockup-brief.md` | the UI brief given to design |
| `docs_/Odoo-Migrator-v1.html` | the returned mockup (bundled; inline styles, no classes — do not copy them) |
| `docs_/sheller-dind-ru.md` | historical: the spec handed to Sheller for its container mode, now implemented |
| `/opt/projects/odoo-sheller/docs/container.md` | Sheller's runtime contract — code against this, not against the spec that asked for it |

This file is the short version for working sessions. Where it and the
design spec disagree, **the spec wins**. Where the spec and the plan
disagree, the plan says so explicitly under "Overrides vs the written
spec" — that list is the record, not a suggestion.

## Shape

```
Browser (Jinja + htmx + Alpine + SSE)
   │
FastAPI app ── SQLite            the source of truth: queue, batches,
   │           rules/*.md        statuses, notes, findings, assembled rules
   │
   ├─ POST webhook ──────────> n8n (containerized, pure executor)
   │                              │  AI Agent stages + deterministic stages
   │ <── progress / result ───────┤
   │ <── File API ────────────────┤
   │ <── Sheller proxy ───────────┘
   │
   └─> Sheller daemon 127.0.0.1:8765 ──> old container / new container
```

Per module: understand the old version, rewrite, install the upgrade on
the new container, test, judge, retry. The retry loop is a real loop on
the n8n canvas, not a model looping inside one node.

## Decisions that are settled

These were argued out. Do not re-open them mid-task; raise them as their
own question if you think one is wrong.

- **The app is the source of truth.** n8n stages pass data through and
  accumulate nothing across jobs. `findings` grows within one job and
  ships home in the callback.
- **n8n is a replaceable cartridge**, selected per project, pinned for a
  run, recorded on every result. The webhook contract is therefore the
  product's API: versioned, declared by each cartridge, validated before
  a run.
- **Prompts and rules travel in the payload as resolved text**, never as
  file paths and never configured inside an n8n node. Quality here *is*
  prompt and rules quality, and both belong in git where they can be
  reviewed.
- **Rules are one file per version** (`rules/18.0.md`), never per pair —
  pair documents grow as the square of the versions. The app assembles
  the ordered stack for a migration. This is also what makes 16.0 → 19.0
  possible at all.
- **Sessions belong to the agent, not the app.** A rewrite invalidates
  any session open on the *new* container, so a fresh one opens after it.
  The old container is read-only for the whole run, so one session opened
  at the start stays valid — do not apply the stricter rule to both sides.
- **Peewee, not SQLAlchemy. Alpine, not React. No Celery, no MCP, no
  LiteLLM in the MVP.** Each has a written reason; the React one has a
  written trigger for revisiting.

## Conventions

- **Code, comments, docs and commits in English. Discussion happens in
  Russian.**
- One review gate at a time. Implement only the current task, with tests,
  commit, stop. The human commits their review before the next task
  starts. Do not begin task N+1 in the turn that commits N.
- Copy from the spec rather than inventing a second wording for the same
  thing.
- `return` gets a blank line before it. **`ruff format` disagrees and
  will strip it** — run `ruff check`, not `ruff format`.
- Every external boundary ships a test double in the task that introduces
  it. The suite must pass with no Docker, no Odoo and no daemon running.
  **A double must answer the way the real thing answers.** The executor
  double answered inline, so a batch that fired every module at once read
  as sequential; it must go through the app's own callback, and there is a
  second double that answers later, because n8n always does.
- Appearance lives in the stylesheet. Inline `style=` is permitted only
  for custom properties (`--accent`), because some values are per-row
  data. A test enforces this — do not widen it.
- Every new component gets a row on `/styleguide` in the task that adds
  it. CSS nothing renders is CSS nobody has checked.
- Templates live in `odoo_migrator/web/templates/`, assets in
  `web/static/`. Only `static/` is mounted; a mount over the whole of
  `web/` once served Jinja source at `/static/base.html`.

## Peewee

Sync ORM behind async routes. Every operation inside its own
`db.connection_context()`; a connection never crosses a thread boundary.
`check_same_thread=False` plus a threadpool plus long-lived SSE handlers
is a reliable way to produce intermittent `database is locked` that is
painful to trace afterwards. Schema changes use `playhouse.migrate`,
never Alembic.

## Sheller

The migrator is a client of odoo-sheller, a sibling project. Its daemon
is resolved in three steps: attach to one already serving on 8765, spawn
a locally installed core, or `docker run` the published image. Its
contract — the five mandatory flags, the labels, the health check, the
exit codes — is `docs/container.md` in that repo, and it is written to be
coded against.

Rules that are not ours to bend:

- **The daemon publishes to `127.0.0.1` only.** That address is the whole
  security boundary around an API that executes arbitrary code as
  `SUPERUSER_ID` with no authentication.
- Because of that, **n8n cannot reach it directly** — inside a container
  `127.0.0.1` is the container, and a connection arriving via the host
  gateway is not loopback. The app proxies. n8n is configured with
  exactly one host address.
- Never read `admin.key`, never touch `~/.odoo-sheller/`, never call the
  daemon's admin endpoints.
- A daemon that answers on 8765 may not be ours. Use it; never stop it.

Sessions the migrator opens are `owner.kind = agent` and carry the
`migration_id`, so concurrent migrations stay attributable. Commit is
allowed on the new container and refused on the old one. Sheller's only
hard lock is production, which we never touch because the target
container is never production.

## Security posture

Inherited, not incidental: this pipeline executes arbitrary code as
`SUPERUSER_ID` against real databases, and edits source files on the
host.

- Never publish `8765` beyond loopback. The app binds loopback too, and
  `--host` widens it only because a container cannot reach loopback —
  Docker Desktop leaves no narrower address than `0.0.0.0` to bind. The
  pages are not protected by anything, so widening is per run, never a
  default.
- The machine-facing surface — File API and Sheller proxy — requires
  `X-Odoo-Migrator-Key`, minted into `~/.odoo-migrator/api.key` on first
  start. Print the path, never the value: this tool gets demonstrated on
  a projector.
- The File API is scoped to the configured old and new project roots. A
  path that escapes them is a 403, not a clamp.
- The proxy allowlist runs on the *collapsed* path. `%2e%2e` reaches the
  route undecoded by a prefix check and is then collapsed by the HTTP
  client on the way out, which turned `/api/sessions/%2e%2e/journals`
  into an unmasked journal read.
- Mounting the Docker socket into the Sheller container grants the host's
  whole Engine — every container, root-equivalent. True of a native
  daemon too; worth restating because a container looks like isolation
  and provides none in that direction.
- Sheller journals are unmasked and land in the host's `~/.odoo-sheller`.
  Review one before sharing it.
