# Native runner — design

## Context

The migrator was built with n8n as its executor: the app owns all state, n8n
runs one module through understand → rewrite → upgrade → test → verdict →
retry, and reports back over a webhook contract. That decision is recorded in
`2026-09-20-odoo-migrator-design.md` under "Why n8n, and how to remove it",
together with the two reasons that carried it — demonstrability and learning
the tool — and with an explicit exit path.

This document takes the exit. It replaces n8n with a runner in our own
process, keeps the swappable-cartridge property that made n8n's contract the
product's API, and puts the flow picture where it was always meant to be: on
our own page, painted live, with the data n8n never had.

The engine question was reopened and answered again, this time against
measurement rather than assumption. What follows is the result.

## Why leave

The n8n canvas was bought for legibility. It did not deliver it:

- The canvas is not in the app. `/n8n` is a stub with an "Open in n8n" link.
  Embedding the editor runs into cross-origin session cookies and a
  commercial OEM/embed path; framing a specific execution is possible but
  needs an execution id the app does not keep.
- The canvas shows the workflow, never the run. It cannot show what the agent
  executed, what came back, what a step cost, or how one attempt differed
  from the next — the app holds all of that and the canvas holds none of it.
- The graph is 38 nodes, and roughly half of them exist to set a field and
  report progress. That is n8n's verbosity, not the flow's complexity.

Against that, n8n costs a 4 GB container, a published workflow package, an
API key, a webhook contract, and — the expensive part — an entire
machine-facing surface on the app: the File API, the Sheller proxy, the
`X-Odoo-Migrator-Key`, and a `--host` flag that widens the bind beyond
loopback. All of it exists because a process inside a container cannot reach
a daemon that publishes to `127.0.0.1` only. A runner in our own process
needs none of it.

### Why not LangGraph

LangGraph was evaluated, not dismissed. It is a real graph engine with
`interrupt()` / `Command(resume=...)` for human-in-the-loop, a checkpointer
for durable execution, and `stream()` that yields per-node updates. It does
not drag in LangChain proper.

It resolves to 38 packages (`langchain-core`, `langgraph-checkpoint`,
`langgraph-prebuilt`, `langgraph-sdk`, `langsmith`, `requests`, `websockets`,
`zstandard`, …) against our nine direct dependencies. Everything it sells, we
own already:

| LangGraph provides | This app already has |
|---|---|
| checkpointer, `thread_id`, durable execution | `Job` / `Attempt` rows in SQLite, `reconcile_jobs()` |
| `interrupt()` / `Command(resume=...)` | `waiting_for_human`, `answer_job()`, batch checkpoints |
| per-node `stream()` | `EventHub` and the SSE the run screen already consumes |
| `draw_mermaid()` | we need a live-paintable canvas of our own regardless |

Adopting it would mean a second state machine and a second store of run state
beside the app's own — the same mistake as n8n, with fewer pixels to show for
it. Its genuine advantage is branching, parallelism and subgraphs. The flow
is linear with one retry loop and one wait, and is meant to stay that way.

If the flow ever grows real concurrency, this decision is worth reopening.
Until then the runner is ours.

## What does not change

These decisions are carried over intact from the original design and are not
reopened here:

- **The app is the source of truth.** Queue, batches, statuses, notes,
  findings, attempts, checkpoints stay in SQLite behind Peewee.
- **Rules are one file per version**, assembled by the app into an ordered
  stack per migration. Rules are not part of a cartridge.
- **Prompts and rules travel as resolved text**, never as paths handed to an
  executor.
- **Sessions belong to the agent.** A rewrite invalidates a session open on
  the new container; the old container is read-only for the whole run. (One
  exception is introduced below: a job parked on a human closes its
  sessions.)
- **Peewee, not SQLAlchemy. Alpine, not React.** Every operation inside its
  own `db.connection_context()`; a connection never crosses a thread
  boundary.
- **Sheller's contract is Sheller's.** Loopback only, never `admin.key`,
  never the admin endpoints, never stop a daemon that may not be ours.

## Overrides vs the 2026-09-20 spec

Stated explicitly, because the earlier document is still the design of record
for everything this one does not touch:

1. **n8n is removed.** "Workflow cartridges" no longer means an n8n package;
   it means a declarative YAML file this app executes.
2. **The webhook contract is retired** as a transport. Its successor is the
   cartridge schema: same role — the product's real API — different shape.
3. **The canvas is ours.** The "iframe: n8n canvas" line in the workspace
   layout becomes a rendered flow of the loaded cartridge, painted live.
4. **`understand` and friends get code execution on both containers**, not a
   narrow read-only toolset. Sheller is the point of the product; the agent
   gets the shell, not a keyhole.
5. **Commit is allowed on the target container** without a human grant. It is
   a disposable container recreated from compose; blocking on a grant per
   module is friction with no protection behind it. The source container
   stays `allow_commit: false`.

## Architecture

### The seam

`Executor.start(payload)` stays the protocol. It gains a second real
implementation beside the test double:

```
Executor (Protocol)
├── StubExecutor    — fake stages, used by the suite
└── NativeRunner    — loads a cartridge, runs one module
```

`apply_progress()` and `apply_result()` stay functions in `jobs.py` and stop
being HTTP endpoints. Queue, batches, retry accounting, checkpoints, findings
and `reconcile_jobs()` are untouched: the executor changes, the app does not.

### Threading

One job runs in one background thread. Not asyncio: the Sheller client is
synchronous, Peewee is synchronous, and the project's rule is that a
connection never crosses a thread boundary with every operation inside its
own `connection_context()`. A synchronous body in a dedicated thread honours
that naturally; `async` over a synchronous client does not.

A batch stays what it is today — the app's trampoline over the result
callback, one module at a time.

### Parts of the runner

```
odoo_migrator/runner/
├── cartridge.py   load YAML, validate, resolve prompts, hash
├── registry.py    step name → implementation, tools per step
├── loop.py        execute step, read outcome, pick the next one
├── steps/         one module per step implementation
├── agent.py       Anthropic client, tool-use loop, budgets
└── tools.py       the Sheller and filesystem verbs given to agents
```

1. **Cartridge loader** — `yaml.safe_load` (which also parses JSON), Pydantic
   validation, prompts resolved to text, refusal with the list of known names
   on anything unknown.
2. **Loop** — run a step, read its outcome, pick the next edge: the main
   pipeline in order; on failure the `on_failure` pipeline; then `retry`
   returns to `resume_at` until the migration's `retry_limit` is spent.
3. **Step registry** — a closed map from name to implementation. Deterministic
   steps are plain code. Agent steps call the model with a tool set defined
   here, in code, never by the cartridge.
4. **Events** — step entered, step left, tool called, tool returned, token
   spend, all into the existing `EventHub`. The page subscribes; nothing
   polls.

## The cartridge

### One file

A cartridge is a single YAML file. Not a directory: it is uploaded through
the UI, its hash is its identity, and a pull request reviewing it sees one
diff covering both the order of steps and the wording of prompts. Rules
(`rules/18.0.md`) are not part of it — the app assembles those per migration.

### The stock cartridge

```yaml
cartridge: stock
version: 1.0.0
schema: 1

model:
  provider: anthropic
  name: claude-opus-5

pipeline:
  - step: understand
    prompt: |
      You are reading a module on the old Odoo version. A live session is
      open: do not guess from source alone — call methods and look at what
      comes back. Describe current behaviour, what the module overrides,
      and what will break on the target version.

      Then build the smallest set of records this module needs in order to
      do anything, and prove the recipe works here: create them, call the
      methods, check they respond, roll back. The recipe travels with your
      answer — the port will be judged by replaying it on both versions.

  - step: port
    prompt: |
      Port the module to the target version. Write files into the new
      project. The old container's session stays open — use it whenever
      you need to check what the original actually does.

  - step: lint
  - step: upgrade
  - step: tests

  - step: probe
    when: no_tests
    prompt: |
      This module ships no tests. Replay the fixture recipe from the
      understand step on both sides, call the same methods on each, and
      report every difference. A recipe that will not replay on the new
      version is a failure worth reporting on its own.

  - step: write_test
    when: no_tests
    prompt: |
      Turn the recipe you just replayed into a real test in the ported
      module, asserting what the old version actually did. Run it before
      you finish.

  - step: verdict

on_failure:
  pipeline:
    - step: diagnose
      prompt: |
        Work out why it failed. Logs, the journal, source and a live
        session are all available. Decide whether a targeted fix is
        enough or the port should be started over, whether you need to
        look at the old version again, and whether this needs a human.

    - step: ask_human
      when: needs_human

    - step: reprobe
      when: reexamine_old
      prompt: |
        Go back to the old version with the specific question the
        diagnosis raised.

    - step: patch
      when: small_fix
      prompt: |
        Apply the fix the diagnosis named. You have the previous diff —
        change what is wrong, leave the rest alone.

    - step: rewrite
      when: start_over
      prompt: |
        The previous port was judged unsound, for the reason in the
        diagnosis. Write the module again from the old version. Do not
        build on the previous attempt.

  retry:
    resume_at: lint
```

Dropping `probe` and `write_test` is nine lines. Never asking a human is two.
That is the legibility bar: a consultant who does not write Python must be
able to read this file and change it correctly.

Note where the retry re-enters: `lint`, not `port`. The failure lane now
contains the fixing — `patch` or `rewrite`, whichever the diagnosis chose —
so a retry rejoins the main line at the first check after the code changed.

### Step registry

Twelve names, closed, defined in code:

| step | kind | what it does |
|---|---|---|
| `understand` | agent | studies the module on the source container; returns a description of current behaviour and a verified fixture recipe |
| `port` | agent | writes the ported module's files into the target project |
| `lint` | code | manifest, XML and syntax checks on the written files, before a container is involved |
| `upgrade` | code | installs/upgrades the module on the target container through Sheller, commits |
| `tests` | code | runs the module's declared test classes with Odoo's own runner |
| `probe` | agent | replays `understand`'s fixture recipe on both sides and compares behaviour |
| `write_test` | agent | turns the proven recipe into a real test file in the ported module |
| `verdict` | code | judges the outcome against the migration's strictness |
| `diagnose` | agent | reads the failure, decides what to change, raises flags |
| `patch` | agent | applies a targeted fix, with the previous diff in hand |
| `rewrite` | agent | ports the module again from scratch, discarding the previous attempt |
| `reprobe` | agent | re-reads the old version with the question the diagnosis raised |
| `ask_human` | code | parks the job, opens a checkpoint thread, waits for an answer |

Three of those deserve their reason stated:

- **`port`, not `migrate`.** "Migration" already names the whole run in this
  codebase — the `Migration` row, `migration_id`, the screens. A step called
  `migrate` inside a migration is ambiguous in every event and log line it
  appears in.
- **`patch` against `rewrite`.** The difference is not mechanical — both
  write files — it is what the step is given and what it is told. `patch`
  receives the previous diff and changes what the diagnosis named; `rewrite`
  is told the previous attempt was unsound and is denied it as a base. The
  human answer that says "this code is bad, do it again" has a step to land
  in, and the canvas shows which of the two the diagnosis chose.
- **`lint` before `upgrade`.** A typo in `__manifest__.py` or a missing
  `ref` in a view is a large share of failed installs in Odoo. Discovering it
  by waiting for a container costs a minute to learn what a second would have
  told us.

Opening and closing sessions are not steps. The runner opens the source
session once for the whole job and closes everything at the end; the agent
reopens its own target session through a tool when it needs a fresh process.
The picture still draws them as system nodes — they are simply not authored.

### Conditions are flags, not expressions

`when:` takes the name of a flag from a closed list. No expression language:
a non-programmer does not write `{{ $json.diagnosis.needs_human }}`, and —
worse — does not read it.

| flag | raised by |
|---|---|
| `no_tests` | `tests` — the module declares no test classes |
| `tests_failed` | `tests` |
| `lint_failed` | `lint` |
| `needs_human` | `diagnose` |
| `reexamine_old` | `diagnose` |
| `small_fix` | `diagnose` — a targeted change is enough |
| `start_over` | `diagnose` — the port is unsound as written |

`small_fix` and `start_over` are mutually exclusive by the same rule that
makes them useful: the diagnosis picks one. A diagnosis that raises both is a
refused result, not a step that runs twice.

### Validation

The file arrives through the UI and drives execution against live databases.
Validation is a boundary, not a convenience. Every failure below is a refusal
at load time with a message naming the alternatives, never a crash mid-run:

1. Unknown step name.
2. Unknown flag in `when`.
3. A flag no step in this cartridge can raise — this catches the likeliest
   error in such a file, a typo in a flag name, which would otherwise be a
   step that silently never runs.
4. `retry.resume_at` does not name a step in `pipeline`.
5. A step name repeated within one pipeline (`resume_at` becomes ambiguous
   and the canvas gets two nodes with one name).
6. An agent step without a prompt; a deterministic step with one. The second
   is a typo, and swallowing it lets someone believe they changed behaviour.
7. Unknown `model.provider`, or a model name the provider does not serve.
8. A `schema` this build does not implement.

### What a cartridge does not carry

`retry.limit`, `strictness`, tool-call ceilings and timeouts are migration
settings, already on the card and in the database (`retry_limit`,
`strictness_default`). The cartridge describes the *shape* of the flow; the
migration card carries the *numbers* of one run. A cartridge may narrow a
step's tools (`tools: [source]`); it may never widen them.

### Cartridge identity

`CartridgeInstall` survives, with its predicate changed: id, version, the
SHA-256 of the file, the smoke-run status, and the pin recorded on every job.
"Which cartridge did this run use" is the first question a second client
project will ask. The smoke-run survives too: run one module end to end
before turning a batch loose on a hundred.

## Agent steps

### The model client

The Anthropic SDK, through the beta tool runner (`@beta_tool` +
`client.beta.messages.tool_runner`), which owns the request → execute → loop
cycle and leaves us the tool functions. Adaptive thinking, effort from the
migration's settings, streaming for the long turns. The default model is
`claude-opus-5`; `claude-sonnet-5` is the cheaper alternative, chosen per
migration, never silently.

`model.provider` is validated against what this build implements. A second
provider, when it is wanted, is a second thin client — not a provider
abstraction written before there is a second provider.

### Tools

The tool set is Sheller's own verbs, because Sheller is the product's
leverage: a live Odoo backend where any ORM call, any method, any override
can be executed and read. A migrator that hands its agent a keyhole onto that
is a migrator that cannot check its own work.

| tool | notes |
|---|---|
| `exec` | run Python in a session; returns value and log |
| `source` | read Odoo source, including which override actually wins |
| `run_test` / `list_tests` / `test_result` | Odoo's own test runner, one class or one method at a time |
| `journal` | what Odoo logged |
| `history` | what has been run in this session |
| `interrupt` | stop a command that is taking too long |
| `rollback` | discard the session's transaction |
| `reopen(side)` | close and reopen a session, for when the module's code changed |
| `write_file` / `read_file` | inside the target project root only |

The client already implements `exec`, `run_test`, `commit`, `rollback`,
`open_session` and `close`. `source`, `journal`, `list_tests` and `interrupt`
are additions.

### Both sides at once

Both sessions are open during agent steps. That is what makes differential
probing possible: run the same expression on the source and the target and
compare the two answers. "The tests pass" is a weaker claim than "this method
returned the same thing on both versions", and the second one is only
available because a live old container is part of the design.

### Comparable data: a fixture recipe, not a dump

Differential probing only means something if both sides have something
comparable to run against. The obvious answer — restore the source database
onto the target — does not work and cannot be made to work: the modules being
migrated add their own columns, so a dump of the old database does not fit
the new schema. Loading it *is* the migration this tool has not performed
yet.

So the data is built, not copied, and the recipe for it comes out of
`understand` rather than being improvised in `probe`:

1. `understand` is already inside a live session on the source container.
   While it works out what the module does, it also builds the smallest set
   of records the module needs in order to do anything — a partner, a
   product, an order, whatever this module touches — and **verifies** the
   recipe by running it: create the records, call the methods, see that they
   respond, roll back.
2. Its output is therefore two things: a description of current behaviour,
   and a fixture recipe that has been proven to work on the old version.
3. `probe` replays that same recipe on both sides and compares. The
   comparison is honest by construction — one scenario, already known to run,
   not two improvisations.
4. A recipe that fails to replay on the target is itself a loud result: what
   broke is the creation of data, not the computation over it.

The recipe is stored on the `Attempt`. For a module that shipped no tests, it
is the first executable description of that module anyone has written, and it
is the obvious seed for a real test later.

### Containment

The agent is confined to two containers by the shape of its tools, not by a
permission on the daemon. Tools take `side: old | new`; the container name
comes from the migration row. There is no verb for listing containers and no
verb for naming one. A daemon-side scope was considered and rejected: a
daemon answering on 8765 may not be ours, so a process-wide restriction is
both unsettable and rude. If defence in depth is wanted later, the shape is a
key-scoped lease (`POST /api/scopes` returning a key under which only two
containers are visible), not a flag.

What holds the rest:

- **Source container**: `allow_commit: false` for the whole run. The agent
  does not need commit to compare behaviour — rollback is Sheller's default,
  so records it creates exist inside the session and vanish at close. The old
  project is the one thing in the pipeline that cannot be recreated.
- **Target container**: `allow_commit: true` at open, no human grant. It is
  recreated from compose in a minute; Sheller still refuses commit on
  production, which we never touch.
- **Every command carries a timeout**, and `interrupt` is a tool. One
  `search([])` over a million rows is the realistic way a run hangs.
- **Budgets**: a ceiling on tool calls and on tokens per agent step, from
  migration settings, enforced by the runner.

## The human in the loop

`ask_human` parks the job and writes a checkpoint. Two decisions about that
checkpoint are made here rather than discovered later.

### The answer is a thread, not a string

A checkpoint stores a thread of messages from the first version, even when
the thread only ever holds one: the agent's question and the human's answer.
The whole thread travels into the prompt of the step that resumes, so
whoever fixes the module sees what was asked and what was decided, not a
single line torn out of context.

This is storage shape, not a feature. It is chosen now because the feature it
admits — below — is otherwise a schema migration and a rewrite of the
checkpoint screen.

### Assisted answers (named now, built later)

The realistic case is a human who cannot answer the question as asked: "is
this override still needed?" is not answerable without reading something.
What they want is to ask back — *go and read how the neighbouring module does
this, then tell me* — decide, and only then let the flow go on.

That is **not a step**. The pipeline does not advance; no attempt is spent;
nothing new appears on the canvas. It is a side channel on the parked
checkpoint: the human writes a turn, a short read-only agent turn answers
with the same tools, and both land in the thread. A step would instead put a
second cycle on the canvas returning into `ask_human`, which reads as the
retry loop and is not one.

Consequences to honour when `ask_human` is built:

- The thread is append-only and attributed — who said what, agent or human.
- A research turn is read-only and metered separately from the module's
  attempt budget. It never writes files and never commits.
- **A parked job closes its sessions.** A checkpoint can sit for a day, and a
  live `odoo-bin` process per parked module is how a machine ends up with
  forty of them. Sessions reopen on demand for a research turn and for the
  step that resumes. This is a deliberate exception to "one source session
  for the whole run" — that rule holds for a job that is running, not one
  that is waiting on a person.

## The flow screen

The `/n8n` stub becomes `/flow`, and it is the reason the runner is worth
building rather than merely cheaper.

**The picture is derived, not authored.** The cartridge has no coordinates.
The renderer lays the main pipeline out left to right, the `on_failure`
pipeline in a lane below it, and the retry edge back to `resume_at`. Cytoscape
is already vendored for the module tree; this is a preset layout over a graph
we compute. A cartridge stays readable because nobody maintains positions in
it.

**The painting is live.** From the same SSE stream the run screen uses:

- the active node pulses; finished nodes dim; the failed one is red;
- the edge actually taken is lit, so a second lap of the retry loop is
  visible as a loop rather than inferred from a log;
- each node carries its own elapsed time and token cost, because the model
  calls are ours;
- clicking a node opens what that attempt did: for an agent step the tool
  calls and their results, for `tests` the report, for `verdict` the
  judgement. The `Attempt` rows already hold diff, test output, verdict and
  diagnosis.

**The trace is the spectacle.** Under the flow, the live tool calls: the code
the agent sent, and what each side answered. Differential probing shows two
columns. This is the demonstration the n8n canvas was bought to provide and
never could, because n8n never had this data.

**Replay.** A finished job can be replayed from its stored events. On a
projector, a replay is stronger than a live run that may stall on a slow
container.

## Security posture after n8n

The machine-facing surface disappears with the executor that needed it:

- No File API, no Sheller proxy, no `X-Odoo-Migrator-Key`, no
  `app_base_url`.
- No `--host`. The app binds loopback, without an exception to misuse.
- The path-escape rules that the File API and the proxy allowlist enforced do
  not vanish as *rules*: the `write_file` / `read_file` tools are scoped to
  the target project root, and a path escaping it is a refusal, not a clamp.

What remains is unchanged and inherited: the pipeline still executes
arbitrary code as `SUPERUSER_ID` against real databases and still edits
source files on the host. Sheller journals are still unmasked and still land
in `~/.odoo-sheller`. The model provider's API key lives in app settings as a
password field — printed as a path, never as a value.

One new exposure, and it is real: module source, ORM data and test output now
leave the machine, to the model provider. It was already true through n8n's
agent nodes; it is stated here because the runner makes the app the sender.
The consequence to record: what a prompt carries is a product decision, and
findings that quote customer data are the part to watch.

## Testing

The project's rule stands: every external boundary ships a test double in the
task that introduces it, the suite passes with no Docker, no Odoo and no
daemon, and **a double must answer the way the real thing answers**. Three
stages once looked finished because a stub answered inline.

- **Model double** — replays scripted turns, including a tool-call turn, a
  turn that returns nothing useful, and a turn that hits the ceiling. It must
  be able to *fail* to converge; an agent double that always succeeds proves
  the loop compiles, not that it terminates.
- **Sheller fake** — already exists and already answers the way the daemon
  answers (403 `not_owner` on a keyless close was measured against the live
  daemon). It gains `source`, `journal`, `list_tests`, `interrupt`.
- **Cartridge fixtures** — one valid cartridge per shape, and one file per
  refusal in the validation list. These are cheap and they are the product's
  API.
- **Runner tests** — the retry loop actually loops; `resume_at` resumes where
  it says; a `when` flag that is never raised skips its step; a job that
  parks for a human resumes on an answer; a budget ceiling ends a step
  without ending the run.

Roughly a hundred of the current 297 tests describe the n8n contract, the
webhooks, the proxy and the File API. They go with it. That rewrite, not the
runner, is the bulk of the work.

## Demolition

**Deleted:** `proxy.py`, `files.py`, `authkey.py`, `resume.py`,
`odoo_migrator/n8n/`, `n8n/stock/*`, `packaging/n8n.sh`, `n8n/.env`, the
`--host` flag, the `app_base_url` setting, the `X-Odoo-Migrator-Key` header
and everything that validated it.

**Kept, predicate changed:** `CartridgeInstall` (now a YAML cartridge's id,
version, hash, smoke status, pin), `contract.py` (now the cartridge schema),
`cartridges.py` (now load, validate and smoke-run a YAML cartridge).

**Untouched:** the module graph and tree, the queue, batches, checkpoints,
findings, notes, diffs, provisioning, the Sheller client and resolver, the
event hub and SSE, every screen except `/n8n`, and the settings page minus
the fields that leave with n8n.

**The rule for this branch:** anything kept must have a purpose that can be
stated without the word "n8n". `webhook_path` cannot, and goes.

**Branch:** `native-runner`, off `main`. `main` remains the working n8n build
until a real module completes a real migration on the branch. Reverting is
then one command rather than archaeology.

## Open questions

1. **Does a human answer buy an extra attempt?** Today it always does. Left
   as it is; worth revisiting after real runs.
2. **What a prompt is allowed to carry off the machine.** Named above under
   security; the policy is not written yet.
3. **Per-step model choice.** The cartridge declares one model for the run. A
   cheaper model for `diagnose` than for `port` is plausible and is not in
   this design.
4. **Assisted answers** are specified above but not scheduled. The first pass
   builds the checkpoint thread that makes them additive later.
5. **The step registry's granularity.** Twelve steps is the current cut.
   `review` (the agent reading its own diff against the rules) and `report`
   (a step that collects findings) were proposed and rejected: the first is
   the model checking itself where `tests` and `probe` answer with facts, the
   second is an empty node for something that accumulates anyway. Revisit
   after the first real project.
