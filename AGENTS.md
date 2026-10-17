# odoo-migrator

Automatic migration of Odoo addon projects between framework versions
(17.0 → 18.0, and multi-version jumps). Input: a project with dozens or
hundreds of custom modules. Output: the same project ported, module by
module, verified against two live Odoo containers — the source version to
learn from, the target one to land in.

**Status: native runner in the tree, stub by default.** Tasks 1–12 of the
native-runner plan are done. The live gate — one real module against two
containers — has not been run, so the process stays on the stub until
`ODOO_MIGRATOR_RUNNER=native`. Run `odoo-migrator` (add `--reload` for
development); `uv run pytest`.

| Document | What it holds |
|---|---|
| `README.md` | install, quick start, API table, security short version |
| `packaging/` | `setup.sh` creates the virtualenv; `serve.sh` starts the app. The desktop build goes here later, same shape as odoo-sheller |
| `docs/` | product docs: architecture, UI, security, FAQ (en/ru) |
| `docs/runner-design.md` | the design of record for the runner |
| `docs_/superpowers/plans/2026-09-23-native-runner.md` | the implementation plan — 13 tasks; task 13 is the live gate |
| `docs_/superpowers/specs/2026-09-20-odoo-migrator-design.md` | the earlier product spec; the runner spec wins where they disagree |
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
   │           rules/*.md        statuses, notes, findings, the cartridge
   │
   ├─ runner (stub, or native when ODOO_MIGRATOR_RUNNER=native)
   │     one job, one thread; a provider runs one model turn
   │     tools call Sheller and write files under the project roots
   │
   └─> Sheller daemon 127.0.0.1:8765 ──> source container / target container
```

Per module: understand the source version, rewrite, install the upgrade on
the target container, test, judge, retry. The retry loop is the cartridge's
failure lane, not another turn inside one model call.

## Decisions that are settled

These were argued out. Do not re-open them mid-task; raise them as their
own question if you think one is wrong.

- **The app is the source of truth.** A provider runs one turn and keeps
  nothing. `findings`, the checkpoint thread and the trace skeleton live
  in SQLite.
- **The cartridge is a YAML file**, pinned per migration and recorded on
  every job (`id`, version, sha256 of the raw text). The stock flow is
  `cartridges/stock.yaml`. A provider is selected by name on the model;
  it does not own the loop, the tools, or the records.
- **Prompts and rules travel as resolved text**, rendered into the agent
  step. Quality here *is* prompt and rules quality, and both belong in
  git where they can be reviewed.
- **Rules are one file per version** (`rules/18.0.md`), never per pair —
  pair documents grow as the square of the versions. The app assembles
  the ordered stack for a migration. `rules/common.md` is shared context
  and is sent in front of that stack. This is also what makes 16.0 → 20.0
  possible at all.
- **Sessions belong to the agent, not the app.** A rewrite invalidates
  any session open on the *new* container, so a fresh one opens after it.
  The source container is read-only. Both sessions close while the job is
  parked on a person, and open again when the same thread gets the
  answer.
- **Peewee, not SQLAlchemy. Alpine, not React. No Celery, no LiteLLM.**
  The app does not serve MCP. Cursor's provider calls tools in-process
  (`custom_tools`, built-in tools off). A shared MCP bridge is in the
  design and is not built. OpenCode is specified and not built.

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
  **A double must answer the way the real thing answers.** The stub posts
  progress and a result through the app, the same path a finished job
  uses. A second double answers later, because a live run does not finish
  inside the request that started the job.
- Appearance lives in the stylesheet. Inline `style=` is permitted only
  for custom properties (`--accent`), because some values are per-row
  data. A test enforces this — do not widen it.
- Every new component gets a row on `/components` in the task that adds
  it. CSS nothing renders is CSS nobody has checked. `/styleguide` is
  the step guide, not that catalog.
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
  `SUPERUSER_ID` with no authentication. This process is the client.
  There is no proxy.
- Never read `admin.key`, never touch `~/.odoo-sheller/`, never call the
  daemon's admin endpoints.
- A daemon that answers on 8765 may not be ours. Use it; never stop it.

Two things the daemon only serves over MCP are vendored here, because this
process speaks HTTP to it and there is no route for either: the source
snippet in `sheller/source.py`, and the shell guidance in
`prompts/sheller.md` (its `os_help` topics, minus the ones about owning and
committing sessions, which the runner does). Both have a test that compares
them with the sibling checkout and skips when it is absent. A copy can
drift; an agent told its tools exist and nothing about what they do writes
Python loops where `mapped` is one call.

Sessions the migrator opens are `owner.kind = agent` and carry the
`migration_id`, so concurrent migrations stay attributable. Commit is
allowed on the target container and refused on the source one. Since Sheller
1.8.3 an agent cannot be handed commit at open; from 1.11.0 a person turns
on *agents may commit* on the target container's card in Sheller, and the
runner's target side opens with the right. The source side opens with
`allow_commit: false`, an opt-out the switch honours. Sheller's only
hard lock is production, which we never touch because the target
container is never production.

## Security posture

Inherited, not incidental: this pipeline executes arbitrary code as
`SUPERUSER_ID` against real databases, and edits source files on the
host.

- Never publish `8765` beyond loopback. The app binds `127.0.0.1` only.
  There is no flag that widens it. The pages have no login; what they
  have is `web_guard`: a Host that is not a loopback name, or a change
  sent from another site's page, is refused. Tests talk to
  `http://127.0.0.1`, not Starlette's `testserver`.
- `read_file` and `write_file` stay under the configured old and new
  project roots. A path that escapes them is refused, not clamped.
- Provider secrets are `ProviderSetting` rows. The settings page reports
  whether one is set and never renders the value. A blank save keeps the
  stored value. Do not print a key: this tool gets demonstrated on a
  projector.
- Mounting the Docker socket into the Sheller container grants the host's
  whole Engine — every container, root-equivalent. True of a native
  daemon too; worth restating because a container looks like isolation
  and provides none in that direction.
- Sheller journals are unmasked and land in the host's `~/.odoo-sheller`.
  Review one before sharing it.
