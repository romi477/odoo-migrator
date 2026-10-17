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
the source version has to stay reachable to answer "what did this actually
do?", and a failed upgrade on module 40 should not require re-deriving
the rules that module 3 already taught you.

odoo-migrator keeps the source container read-only, writes the new tree
through the catalog's file tools, installs the upgrade on the target
container, runs the tests, and stops at a checkpoint with the diffs.

## Who is this for?

You, through the browser, on your own machine. The runner is part of
this process. A provider runs one turn of one model and keeps nothing
about the project.

## How it's built

**What are the moving parts?**

Three of them:

1. **The app** — FastAPI + SQLite on your machine. Queue, batches,
   rules, notes, findings, the cartridge. Source of truth. The runner
   is a thread inside it. Without `ODOO_MIGRATOR_RUNNER=native` that
   thread is a stub.
2. **The web page** — Jinja, htmx, Alpine, one SSE stream. Talks to the
   app with no key.
3. **odoo-sheller** — a daemon on `127.0.0.1:8765` that holds
   `odoo-bin shell` open inside each Odoo container.

The app is the only client of Sheller.

**Does it reimplement Odoo's upgrade?**

No. The target container's Odoo installs and upgrades the module the
rewrite wrote. Tests are Odoo's tests. Sheller is a persistent shell,
not a second ORM.

**Where does the rewrite actually happen?**

On disk, in the target project path, through the `write_file` tool. A
path that leaves the configured root is refused.

## Migrations, batches, jobs

**Does opening a second migration stop the first?**

No. The switcher only changes the screen. The running count in the
header is how many are in flight.

**What is a batch?**

One wave of the dependency graph, started from the queue on
**Pipeline**. A module's wave is one after the deepest module it depends
on, so nothing in a wave needs another module of the same wave. Its
modules run **sequentially**. A checkpoint after the wave is the review
gate; starting another batch while one is sitting there is refused.

**What is a job?**

One module, one run of the cartridge. Attempts are laps of the failure
lane, gated by `retry_limit`, not by how many tools one model call may
make.

**Why can't I Batch before a smoke-run?**

Once a cartridge is installed, an untested pin does not get to run two
hundred modules. Smoke-run takes the first unstruck module by name; the
verdict is the job's result, not the instant the run was started. The
button is on the migration card, under Import a flow. With nothing
installed the app uses its own stub and a batch is allowed — that is how
the suite runs with no model and no containers. The stock flow ships
with the app and needs no smoke-run.

**A module passed and the checkpoint diff is empty.**

No step that was allowed to call `write_file` changed a file under the
module. Writes to the source project are not this job's diff.

**Exclude vs archive?**

**Exclude** on Tree (stored as `struck_out`) removes modules from
Migrate/Batch according to the depends graph — multi-select and whole
waves also strike downstream; **Include** walks depends upward.
Archive takes a whole migration off Overview. Opening another migration
never archives the one you left.

## The runner

**Do I have to set a model key to use the UI?**

No. The default executor is a stub. The pages, the queue, the checkpoint
and the tests work. What you will not get is an actual port — set
`ODOO_MIGRATOR_RUNNER=native` and a provider key for that.

**I answered and the job is `unknown`, not running.**

The thread that was waiting is gone (usually a restart). The answer is
still stored and still in the checkpoint thread. The job is not
pretending to run.

**Which models can a cartridge name?**

Whatever the registered provider accepts. Anthropic: the Sonnets
(`claude-sonnet-5-5`, `claude-sonnet-4-6`) and
`claude-haiku-5-5`; Opus is not offered. Cursor: the names it lists, plus `auto-smart`
with parameters. Local: whatever the server at the URL in settings has
loaded, e.g. `qwen/qwen3.8-27b` in LM Studio — free per token, slower, and
weaker at long tool use, so it is for debugging the pipeline rather than
judging a port. OpenAI: `gpt-6-sol`, through the key on the settings page. OpenCode is specified and not built.

**Can I edit a flow?**

Not in place. The eye next to the Flow field opens the file read-only; Fork
copies it into an editable text with the next free version, and Save fork
stores it as a new flow. The flow you copied from, `stock` included, is never
overwritten: a name that is taken is refused. The copy appears in the Flow
select and is not picked for you, and it needs its own smoke-run before a
batch.

## Rules and findings

**Why one file per version, not per pair?**

Pair documents grow as the square of the versions, and a 16→19 jump
would have nothing to read. `rules/18.0.md` is "how to land on 18.0".
The app concatenates source plus every version after it through target.
`rules/common.md` is sent once in front of that stack.

**Where do findings go?**

They accumulate inside one job and stay on that job. At
checkpoint you **Promote** a finding into a version file you pick. The
next module's prompt includes that text. Editing a rules file mid-batch
changes the next module; the hash on the job records what this one
actually saw. In a checkout the file written is the one in git. An
installed build cannot keep anything inside itself, so Promote writes to
`~/.odoo-migrator/rules/` and that copy is read first.

**`sync` vs `batch_defer`?**

`sync`: the job parks until you answer. `batch_defer`: the step records
an assumption and continues; you read the list at checkpoint.

## Sheller and containers

**Does this create Odoo containers from nothing?**

No. You point a migration at two compose projects that already exist.
**Up** starts ours; **Attach** uses something already running; someone
else's port is an error. The Sheller daemon is attached-to or spawned;
a daemon already on 8765 may not be ours and is never stopped.

**Will it commit on the old database?**

No. Commit is allowed on the target container and refused on the source one.
The source session stays for the run, except while the job is parked on a
person: both sessions close then, and open again if the same thread
gets the answer.

## Security, briefly

The pages have no login. The app binds `127.0.0.1` only, and refuses a
request sent from another site in the browser. Provider keys
are stored and never shown back. Sheller's admin key is never read.

Full reasoning: [security.md](security.md).

## What's still missing

The loop exists: port, upgrade, test, checkpoint, promote, retry. What
is not claimed: that a real customer project comes out the other side
without you reading the diffs. The native runner is opt-in until one
real module has been checked by hand. Quality here *is* prompt and
rules quality.

Out of this round on purpose: Tauri, LiteLLM, the app performing
`docker login`, a UI editor for rules files, Windows. OpenCode is
designed and not built.

**What do I need to install?**

Python 3.12, [uv](https://docs.astral.sh/uv/) (or pip + venv), Docker,
two Odoo compose projects, and odoo-sheller (or let the app spawn it).
A provider key only when `ODOO_MIGRATOR_RUNNER=native`. The test suite
needs none of those except `@pytest.mark.live`.
