# Architecture

How odoo-migrator works internally: the app as source of truth, a YAML
cartridge executed in this process, Sheller sessions, the rules stack,
and how a checkpoint learns what a step wrote. Start with the README if
you just want to run the thing.

## The pieces

```
Browser (Jinja + htmx + Alpine + SSE)
 │
FastAPI app ── SQLite the source of truth: queue, batches,
 │              rules, notes, findings, the cartridge pin
 │
 ├─ native runner (one job, one thread) ── model provider
 │     tools call Sheller and write files under the mounted addons dirs
 │
 └─> Sheller daemon 127.0.0.1:8765 ──> old container / new container
```

- **The app** is the only piece that accumulates anything across jobs.
  SQLite holds migrations, modules, batches, jobs, attempts, findings,
  assumptions, the step and tool-call skeleton, and the checkpoint
  thread.
- **The web UI** is a client of that app. One SSE connection per page,
  named events — not one stream per row.
- **The cartridge** is a YAML file, pinned per migration and recorded
  on every job. The stock flow is `cartridges/stock.yaml`. A provider
  runs one turn of one model; it does not own the loop, the tools, or
  the records.
- **Sheller** is a sibling daemon. The migrator is its client. Sessions
  it opens are `owner.kind = agent` and carry the `migration_id`. Commit
  is allowed on the new container and refused on the old one. The daemon
  publishes to `127.0.0.1` only, and nothing else in this process needs
  to reach it.

Until `ODOO_MIGRATOR_RUNNER=native`, the app uses an in-process stub
executor. The UI and the suite run with no Docker, no Odoo and no model
key. The switch is temporary: it goes away once one real module has been
migrated and checked by hand.

Peewee, not SQLAlchemy. Alpine, not React. No Celery, no LiteLLM.
Schema changes use `playhouse.migrate`, never Alembic. Every database
operation sits in its own `connection_context()`; a connection never
crosses a thread boundary.

## Per module

One job is one module, on one background thread. The stock flow:

1. **Understand** the old version (read-only session).
2. **Port** into the new project by writing files under the target root.
3. **Lint**, then **upgrade** on the new container (commit allowed),
   then **tests**.
4. **Probe** and **write_test** only when the module has no tests.
5. **Verdict**.
6. On failure, a side lane: diagnose, optionally ask a human, reprobe
   the old version, patch, or rewrite. The lane then resumes at lint.
   `retry_limit` bounds the laps.

A step that rewrites code reopens the new-side session afterwards. The
old container is read-only for the run. While a job is parked on a
human, both sessions are closed; they open again when the answer
releases the same thread. An answer that arrives after that thread is
gone is stored, and the job becomes `unknown` rather than `running`.

`exec` has no timeout on the daemon, so the client puts one on the HTTP
call (300 seconds unless the step context says otherwise) and interrupts
the command that outlived it. Leaving it running would make every later
call `session_busy`.

## Rules are per version, not per pair

`rules/18.0.md` is the document for landing on 18.0. A 17→18 migration
sends two version entries (source + target); 16→19 sends four. The app
concatenates those, stamps a hash of that stack on the job, and renders
the text into the prompt. `rules/common.md` is sent once in front of the
versions: shared context, not a version, and not a file Promote writes to.
Like a version file, it is required: a stack without it is refused.

Promoting a finding at checkpoint asks which version file. In a checkout
that file is the one in git, where it is reviewed. An installed build reads
its rules from inside the package, which the next upgrade replaces and which
is often not writable, so Promote writes to `~/.odoo-migrator/rules/`
instead and the app reads that copy in front of the build's own.

## Cartridges and providers

A cartridge is YAML (or JSON): an id, a version, schema `1`, a model
(`provider` + `name`), a pipeline, and a failure lane. The loader asks
the provider whether it accepts that model name. A flat list of names
is not the check: Cursor's `auto-smart` carries parameters.

Providers registered today:

| Name | What runs the turn |
|---|---|
| `anthropic` | The Anthropic SDK tool runner. Models `claude-opus-5` and `claude-sonnet-5`. |
| `cursor` | `cursor-sdk`, in-process `custom_tools`, built-in tools off (`tools=[]`). |
| `scripted` | The test double. A cartridge cannot select it. |

Keys live in `ProviderSetting` rows (`provider`, `key`, `value`), not
one column per SDK. The settings page shows whether a secret is set and
never renders the value. A blank save keeps the stored one.

The tool catalog and its JSON schemas live in `runner/tools.py`. A
provider translates them. `write_file` and `read_file` take container
paths and stay under the bind mounts that hold the plan's modules
(`mounts.py`, snapshot taken at Build graph); a path that escapes them is
refused, not clamped.

## Diffs

A step that may call `write_file` is snapshotted before it runs and
again after. The unified diff is `previous_diff` for the next prompt
and what the checkpoint shows. The prompt clips that text the same way
it clips module source.

## Layout of the package

| Path | Role |
|---|---|
| `odoo_migrator/api.py` | Factory and pages |
| `odoo_migrator/models.py` | Peewee. `create_tables` plus `_ensure_columns` |
| `odoo_migrator/jobs.py` | Start a job, apply results, resume, checkpoint |
| `odoo_migrator/runner/` | Cartridge language, loop, tools, providers, native thread |
| `odoo_migrator/cartridges.py` | Import, pin, smoke-run |
| `odoo_migrator/sheller/` | HTTP client and the fake that answers like the daemon |
| `odoo_migrator/provision/` | Docker SDK compose up / attach / rebuild |
| `cartridges/stock.yaml` | The stock flow |
| `odoo_migrator/web/templates/` | Jinja only — never inside the static mount |
| `odoo_migrator/web/static/` | Served at `/static` |

`rules/` and `prompts/` live at the repo root. `repo_dir()` finds them.

## What this is not

Tauri, LiteLLM, the app performing `docker login`, a UI editor for
rules files, Windows. OpenCode is a provider in the design and is not
built. Aider and a headless Claude Code CLI are not providers: they
cannot call `report`.
