# Security model

Read this before pointing odoo-migrator at anything you'd mind losing or
exposing. The short version is in the README; this is the reasoning behind
it.

## The core fact

This pipeline executes arbitrary Python as Odoo's superuser
(`SUPERUSER_ID`) against real databases, and writes source files on the
host. It does not add a new privilege — the operator already has Docker
and a Sheller daemon — it sequences that privilege across two containers
and a tree of modules. Every other decision here follows from taking that
seriously.

## What is protected, and how

**The Sheller daemon is never published beyond loopback.** That address
is the whole security boundary around an API that executes arbitrary code
as `SUPERUSER_ID` with no authentication. The migrator is a client: it
attaches to a daemon already on 8765, or spawns one, and never stops a
daemon that may not be ours.

**n8n never talks to 8765.** Inside a container `127.0.0.1` is the
container, and a connection arriving via the host gateway is not
loopback. The app proxies. n8n is configured with exactly one host
address — `app_base_url` — for File API, progress, result, and Sheller
calls.

**The machine-facing surface requires `X-Odoo-Migrator-Key`.** First
start mints it into `~/.odoo-migrator/api.key` (owner-readable only). The
process prints the path, never the value: this tool is demonstrated on a
projector. n8n reads the same value as `$env.MIGRATOR_API_KEY`. Compose
must set `N8N_BLOCK_ENV_ACCESS_IN_NODE=false`; n8n's own v20 breaking
changes flip the default to true, and on the wrong side every header goes
out empty and the app answers 403.

**The File API is scoped to the two project roots.** A path that would
leave them is 403, not silently clipped back inside. `..` in the request
is enough, even when the resolved location would still have been under
the root — clamping `a/../b` into `b` is how an escape becomes a
successful write.

**The proxy allowlist runs on the collapsed path.** `%2e%2e` reaches the
route undecoded by a prefix check and is then collapsed by the HTTP
client on the way out, which turned `/api/sessions/%2e%2e/journals` into
an unmasked journal read. Collapse first, allow second, forward the
collapsed path. Journals are refused. `X-OS-Admin-Key` is stripped and
never read from disk; `~/.odoo-sheller/` is not ours to touch.

**Sessions the migrator opens are attributable.** `owner.kind = agent`,
label migrator-related, `migration_id` on the owner, so concurrent
migrations stay separable. Commit is allowed on the new container and
refused on the old one. Sheller's only hard lock is production, which
this tool never hits because the target container is never production.

**A rewrite invalidates the new-side session.** Code on disk changed, so
a fresh session opens after it. The old container is read-only for the
whole run; one session opened at the start stays valid.

## What is deliberately *not* protected

**The pages have no authentication.** Overview, Tree, Pipeline, n8n, Sheller, Result, Settings are
a local UI. Binding loopback is the boundary. `--host` widens it because
a container cannot reach loopback — Docker Desktop leaves no narrower
address than `0.0.0.0` to bind. The warning at startup is load-bearing:
`0.0.0.0` is every interface, not "the container gateway". Widening is
per run, never a default.

**There is no guard that a compose project "looks like production".**
The operator pointed the migration at two project paths. Guessing would
be wrong in both directions. The backstop is Sheller's `production` lock
on the far side of a remote instance this tool never opens.

**Mounting the Docker socket into a Sheller container grants the host's
whole Engine** — every container, root-equivalent. True of a native
daemon too; worth restating because a container looks like isolation and
provides none in that direction.

**Sheller journals are unmasked** and land in the host's
`~/.odoo-sheller`. Review one before sharing it. The proxy will not fetch
them for n8n.

## The API key is not Sheller's admin key

Two keys, two files, two jobs:

| | Migrator `api.key` | Sheller `admin.key` |
|---|---|---|
| Where | `~/.odoo-migrator/api.key` | `~/.odoo-sheller/admin.key` |
| Who presents it | n8n, as `X-Odoo-Migrator-Key` | a human acting on a session they never owned |
| The migrator | mints it, prints the path | never reads it, never forwards it |

Confusing them is how an n8n node ends up holding a key that closes
somebody else's Sheller session.

## Lost callbacks and a refused trigger

A result the executor never took used to leave the job, the module and
the batch on `running` forever. Nothing was started, so the rows are
undone and the screen says so.

A callback that arrives after a crash is applied if the job is still
unknown/reconciled, not dropped. The workflow-side half is HTTP Request
`retryOnFail` on the callback node.

## What this does not claim

The File API writes whatever the agent sends, inside the two roots. There
is no second review of the patch before it hits disk. The checkpoint
diff is how a human sees what landed, after the fact.
