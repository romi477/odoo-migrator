# FAQ

Plain-language questions and answers about how odoo-migrator works. A
Russian version lives at [faq-ru.md](faq-ru.md); for the technical
version of most of this, see [architecture.md](architecture.md).

## What is this, in one sentence

It takes a custom Odoo addon project through a framework version — 17→18,
or a longer jump — module by module, against two live containers, with a
human able to check in at a batch checkpoint without babysitting every
file.

## Why not just rewrite the modules by hand?

You can, and for one module there is no reason not to. The problem shows
up on a project with dozens or hundreds of them: the same class of
breakage (attrs, views, `_sql_constraints`, cron signatures) repeats,
the old version has to stay reachable to answer "what did this actually
do?", and a failed upgrade on module 40 should not require re-deriving
the rules that module 3 already taught you.

odoo-migrator keeps the old container read-only, writes the new tree
through a scoped File API, installs the upgrade on the new container,
runs the tests, and stops at a checkpoint with the diffs.

## Who is this for?

You, through the browser, on your own machine. n8n is the executor for
one module; it is not a second product and it has no memory of the
project. An AI agent inside n8n uses the same File API and the same
Sheller proxy a human could curl — there is no smaller, separate
interface for the model.

## How it's built

**What are the moving parts?**

Four of them:

1. **The app** — FastAPI + SQLite on your machine. Queue, batches,
   rules, notes, findings. Source of truth.
2. **The web page** — Jinja, htmx, Alpine, one SSE stream. Talks to the
   app with no key.
3. **n8n** — a container pinned to 2.39.9, or an in-process stub that
   speaks the same webhook contract. Pure executor.
4. **odoo-sheller** — a daemon on `127.0.0.1:8765` that holds
   `odoo-bin shell` open inside each Odoo container.

The app is the only piece that talks to both n8n and Sheller. n8n never
sees 8765.

**Does it reimplement Odoo's upgrade?**

No. The new container's Odoo installs and upgrades the module the
rewrite wrote. Tests are Odoo's tests. Sheller is a persistent shell,
not a second ORM.

**Where does the rewrite actually happen?**

On disk, in the new project path, through `PUT /api/migrations/.../files`.
n8n does not mount your tree. A path that leaves the two configured
roots is 403.

## Migrations, batches, jobs

**Does opening a second migration stop the first?**

No. The switcher only changes the screen. The running count in the
header is how many are in flight.

**What is a batch?**

A group of modules you drew on the Tree (Group, or drag onto another
node) and started with **Batch**. They run **sequentially**, in
dependency order. A checkpoint after the group is the review gate;
starting another batch while one is sitting there is refused.

**What is a job?**

One module, one run of the cartridge. Attempts are retries inside that
run. The canvas loop is gated by `retry_limit`, not by an agent's Max
Iterations.

**Why can't I Batch before a smoke-run?**

Once a cartridge is installed, an untested pin does not get to run two
hundred modules. Smoke-run takes the first unstruck module by name; the verdict is the
callback, not the instant after the trigger. With nothing installed the
app uses its own stub and a batch is allowed — that is how the suite
runs without n8n.

**A module passed and the checkpoint diff is empty.**

The cartridge sent `attempts: []` and nothing was written on the new
side that contained the module's technical name as a folder. Stock
`Mark passed` logs an attempt; File API writes fill an empty `diff`.
Writes to the old project are not this job's diff.

**Strike vs archive?**

Strike hides modules from Migrate/Batch (and is its own undo). Archive
takes a whole migration off Overview. Opening another migration never
archives the one you left.

## n8n

**Do I have to run n8n?**

No. Empty `n8n_base_url` uses the in-process stub. The UI, the sequential
batch, the checkpoint, the File API and the tests all work. What you will
not get is an actual rewrite — the stub speaks the contract, it does not
port code.

**Why 2.39.9 specifically?**

That is the version the webhook contract was locked against. A floating
`latest` reported 2.39.9 on the spike machine; pinning the cartridge
without pinning the image cannot both stay true. Details:
[n8n.md](n8n.md).

**I answered on Tree and the job is still `waiting_for_human`.**

The Wait node's output is the resume HTTP body (`$json.body.answer`),
not the job. If the canvas does not rebuild from **Before wait**,
everything the rest of the loop needs is gone. Stock does; a hand-edited
cartridge may not.

**Why is `app_base_url` not `http://127.0.0.1:8000`?**

n8n runs in a container. That address is the container. The app refuses
loopback in the trigger so the failure is here, not two hours into a
batch on Linux. Use an address the container can call
(`host.docker.internal`, or the host's LAN IP if you widened `--host`).

## Rules and findings

**Why one file per version, not per pair?**

Pair documents grow as the square of the versions, and a 16→19 jump
would have nothing to read. `rules/18.0.md` is "how to land on 18.0".
The app concatenates source plus every version after it through target.

**Where do findings go?**

They accumulate inside one job and come home on the callback. At
checkpoint you **Promote** a finding into a version file you pick. The
next module's prompt includes that text. Editing a rules file mid-batch
changes the next module; the hash on the job records what this one
actually saw.

**`sync` vs `batch_defer`?**

`sync`: the canvas waits. You answer on Tree; that answer always buys
another rewrite. `batch_defer`: the agent records an assumption and
continues; you read the list at checkpoint.

## Sheller and containers

**Does this create Odoo containers from nothing?**

No. You point a migration at two compose projects that already exist.
**Up** starts ours; **Attach** uses something already running; someone
else's port is an error. The Sheller daemon is attached-to or spawned;
a daemon already on 8765 may not be ours and is never stopped.

**Why can't n8n talk to Sheller itself?**

The daemon binds loopback only. That address is the security boundary
around `SUPERUSER_ID` with no authentication. Inside n8n's container,
`127.0.0.1` is n8n. The app proxies an allowlist (sessions, containers,
probe). Journals and admin are refused.

**Will it commit on the old database?**

No. Commit is allowed on the new container and refused on the old one.
The old session is held for the whole run so "what did this do?" still
has an answer on attempt three.

## Security, briefly

The pages have no login. Loopback is the boundary; `--host 0.0.0.0`
widens every interface because Docker Desktop leaves no narrower
address for a container to call. The File API and the proxy require a
key in `~/.odoo-migrator/api.key` — the process prints the path, never
the value.

Full reasoning: [security.md](security.md).

## What's still missing

The MVP loop exists: trigger, rewrite, upgrade, test, checkpoint,
promote, retry. What is not claimed: that a real customer project comes
out the other side without you reading the diffs. Quality here *is*
prompt and rules quality.

Out of this round on purpose: Tauri, an in-app n8n canvas, LiteLLM, a
custom `n8n-nodes-sheller`, the app performing `docker login`, a UI
editor for rules files, Windows. Each has a written reason in the
design spec or the plan.

**What do I need to install?**

Python 3.12, [uv](https://docs.astral.sh/uv/) (or pip + venv), Docker,
two Odoo compose projects, and odoo-sheller (or let the app spawn it).
n8n is optional until you want a real cartridge. The test suite needs
none of those except `@pytest.mark.live`.
