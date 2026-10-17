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

**Nothing else in this app talks to 8765.** The runner is in this
process, so it is already on the host. There is no second process to
proxy for.

**Other pages in the browser cannot drive the app.** Loopback keeps other
machines out, not other sites: any page the operator has open can submit
a form to `http://127.0.0.1:8000`, and without a login nothing would tell
that form from a click on Settings. Every request must name this machine
in `Host` (`127.0.0.1`, `localhost` or `[::1]`), which refuses a name
rebound to 127.0.0.1 after its page loaded. A request that changes
something must come from a page of this origin, as the browser reports
it in `Sec-Fetch-Site`, or else in `Origin`; a page cannot forge either.
A client that sends neither header — curl, a script — is not a browser
and is let through.

**File tools stay inside the plan's addons directories.** `read_file` and
`write_file` take a path as the container names it and translate it with
the bind mounts Build graph recorded. Only a mount that holds a module of
the plan is a root: a mount of `odoo.conf`, `~/.ssh` or the filestore is
on the container too, and is not reachable through these tools. A mount of
`/`, the home directory or this app's data directory is never a root. The
path is resolved — `..` and symlinks included — before it is checked, and
one that leaves its root is refused, not clipped back inside. Clamping
`a/../b` into `b` is how an escape becomes a successful write. With no
mounts known, a path relative to the old or the target project path is the
only form accepted, under the same rule. Writes land only inside the
ported module's own directory on the target side.

**Provider secrets are stored and never rendered.** A key is a
`ProviderSetting` row. The settings page reports whether it is set. A
blank save keeps the stored value. The process does not print it. OpenAI's
401 message quotes the start and end of the key it was sent, so the runner
replaces it with its own sentence instead of passing it on to the run log.

**A hosted provider is sent the module source.** With `anthropic`, `cursor`
or `openai` the rules, the prompt and every file the agent reads leave this
machine for that provider. `local` does not.

**Sheller journals stay on the daemon.** A tool call is recorded as a
skeleton (tool, side, session id, a short preview). The body is fetched
from the journal when someone opens that line in the run log, because
journals are unmasked and this tool is demonstrated on a projector. `~/.odoo-sheller/` is not ours to touch,
and `admin.key` is never read.

**Sessions the migrator opens are attributable.** `owner.kind = agent`,
label migrator-related, `migration_id` on the owner, so concurrent
migrations stay separable. Commit is allowed on the target container and
refused on the source one. Sheller's only hard lock is production, which
this tool never hits because the target container is never production.

**A rewrite invalidates the new-side session.** Code on disk changed, so
a fresh session opens after it. The source container is read-only for the
whole run; one session opened at the start stays valid.

## What is deliberately *not* protected

**The pages have no authentication.** Overview, Tree, Pipeline, Flow,
Sheller, Result and Settings are a local UI. The app binds
`127.0.0.1` only. There is no flag that widens it. Anything running as
the operator on this machine can use it; only other sites in the browser
are refused (see above).

**There is no guard that a compose project "looks like production".**
The operator pointed the migration at two project paths. Guessing would
be wrong in both directions. The backstop is Sheller's `production` lock
on the far side of a remote instance this tool never opens.

**Mounting the Docker socket into a Sheller container grants the host's
whole Engine** — every container, root-equivalent. True of a native
daemon too; worth restating because a container looks like isolation and
provides none in that direction.

**Sheller journals are unmasked** and land in the host's
`~/.odoo-sheller`. Review one before sharing it.

## Sheller's admin key is not a provider key

| | Provider setting | Sheller `admin.key` |
|---|---|---|
| Where | `ProviderSetting` in the migrator's SQLite | `~/.odoo-sheller/admin.key` |
| Who it is for | the model SDK (Anthropic, Cursor or OpenAI) | a human acting on a session they never owned |
| The migrator | stores it, never renders it | never reads it, never forwards it |

A provider key cannot close someone else's Sheller session. The admin
key is the one that can, and this process does not have it.

## Writing a flow from the viewer

The viewer's Fork is the one write the browser can make to a flow, and it is
shaped so that it cannot damage one that exists. `POST /api/flows/fork` takes
only the text, reads the id and version out of the text itself, and stores a
new row; a name that a row or the shipped `stock.yaml` holds is a 409 and
nothing is written, so a pin that other jobs record cannot change under them.
The id and version may hold only letters, digits and `. _ + -` (40 at most):
the select splits `id@version` at the first `@` and the address carries the
version as a path segment. The body is cut off at 256 KB before it is read as
YAML. The route is a change, so `web_guard` refuses it from another site's
page like any other. A stored fork starts `untested`: a batch waits for its
smoke-run.

## A job that cannot start

Probe, opening sessions, loading the cartridge and a missing model key
all happen inside the run. If any of them raises, the job is `failed`
and the screen hears it. A thread that died before that used to leave
the row `running` with nobody told. The same holds at the other end: a
result that cannot be recorded leaves the job `unknown`, and its batch
too when nothing else of it is in flight, with the reason in the run
log.

An answer for a parked job resumes the thread that is still waiting. If
that thread is gone — a restart — the answer is kept on the checkpoint
thread and the job becomes `unknown`, not `running`.

## What this does not claim

`write_file` writes whatever the model sends, inside the module's directory.
There is no second review of the patch before it hits disk. The
checkpoint diff is how a human sees what landed, after the fact.
