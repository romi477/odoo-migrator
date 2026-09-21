# Odoo Migrator — design

## Context

odoo-sheller already gives a live, persistent `odoo-bin shell` against a
real database, over HTTP/WebSocket. It solves one problem: talking fast to
one running Odoo. Odoo Migrator solves a different one: taking a project of
maybe a hundred custom modules through a whole framework version (17→18,
18→19, ...), module by module, with a human able to check in on progress and
correct course without babysitting every step.

This is a product, not a one-off script for one client's upgrade. Any
project, any supported version pair. The rules that make a migration correct
are part of what ships, not something hand-written per engagement.

## Non-goals (this round)

- n8n as the system of record for the module graph, queue, or batch state.
  Its data model fits workflow execution, not a mouse-editable dependency
  tree — rejected in favor of the app owning that state and n8n being a
  pure executor.
- A UI-editable rules database. The per-version knowledge base is curated,
  reviewed text (files), not rows in a table.
- Pair documents (`17-to-18`). Rules are per version; the app assembles
  the stack for a given migration. See "Rules are per version, not per
  pair".
- Swapping the whole execution agent (Cursor, OpenCode, Claude Code CLI) per
  batch. Only the LLM behind our own n8n AI Agent node changes; the tool-use
  loop and its Sheller tools stay ours.
- A per-batch "allow commit" gate on the target container. The pipeline
  needs real commits to progress (module 50 may depend on 1–49), so this
  isn't a safety knob we build — the only backstop is Sheller's own
  unconditional `production` lock, which we never hit because the target
  container is never production.
- The migrator building the Sheller Docker image itself. It pulls a
  published one (see "Sheller daemon resolution").
- Running more than one Sheller daemon per host. Sheller's container
  contract does allow it (the container name is the caller's to pick and
  the port is configurable via `ODOO_SHELLER_PORT`), but the migrator
  deliberately uses one daemon on the default port — Sheller's model is
  already one daemon, many sessions.

## Architecture overview

```
Desktop shell (Tauri, browser-first MVP)
  ├── module graph + queue + batches + review checkpoints   (app, source of truth)
  ├── rules knowledge base (files, one per version)          (app, source of truth)
  ├── UI: settings, editable module tree, pipeline-progress panel
  ├── iframe: n8n canvas (workflow logic — not the module graph)
  └── iframe: sheller /web (live session observer, no write key)
        │
        │ webhook: "migrate module X" (+ config, no pre-opened session)
        ▼
      n8n (containerized, pure executor, holds no state of its own)
        └── AI Agent node → tools = HTTP calls to the app
              │ (agent opens and owns its own sessions)
              ▼
        app's Sheller proxy  ──►  Sheller daemon (127.0.0.1:8765)
              │
      ┌───────┴───────┐
  old-version         new-version
  container           container
  (read-only)         (writes land here, upgrade, tests, commits)
```

Per module: the agent opens a session on the old container to read current
behavior → edits the module's code → writes it into the new container's
addons path → opens a (fresh, if code just changed) session on the new
container → upgrade, run tests/checks per the configured strictness level →
pass/fail/needs-human → result reported back to the app.

## Components

- **Desktop shell** — Tauri (not Electron — too heavy), same pattern as the
  Sheller desktop app: a window is a shell, the UI lives in iframes. Built
  browser-first as the MVP, wrapped in Tauri once the browser version works.
- **Module Graph Service** (in the app) — resolves the module dependency
  graph from the live old-version Odoo (via a Sheller session; Odoo's own
  install/dependency resolution, not our own manifest parsing), renders it
  as an editable tree, holds queue/batch/status state.
- **Rules Store** — knowledge base as files, **one document per version**
  (`rules/17.0.md`, `rules/18.0.md`), git-versioned, human-curated,
  reviewed like code. See "Rules are per version, not per pair". Separate
  from per-module notes (below), which are app-side runtime state.
- **n8n workflow "migrate-one-module"** — the one reusable workflow
  template. An AI Agent node with tools = HTTP calls to Sheller's REST API
  (`exec`, `run_test`, `commit`, `rollback`, session lifecycle), reached
  through the app's proxy rather than at the daemon's own address (see
  "Networking: n8n is containerized"). No MCP server in this host — it is
  Sheller's plain HTTP API, unchanged, not a different protocol.
- **The app's HTTP surface for n8n** — everything the workflow calls back
  into lives here, and it is the *only* host address n8n is configured
  with (see "Networking: n8n is containerized"):
  - **File API** — read/write scoped to the configured old and new
    project roots, exposed to the agent as a tool. Needed because every
    other tool is a Sheller call and **Sheller does not write host
    files** — it drives a shell inside a container. Porting a module
    means writing it into the new project's directory on the host, which
    nothing else in the design can do. Going through the app also keeps
    n8n containerized without mounting the project into it.
  - **Sheller proxy** — a thin pass-through to the daemon.
  - **Job callbacks** — progress and result.
- **Sheller daemon** — one per host, reached by the migrator through one of
  three resolution paths (below).

## Why n8n, and how to remove it

**Status: decided — n8n.** The question "could we do this without it" was
raised deliberately and answered: yes we could, and we are not going to.
This section records why, and keeps the reversal cheap anyway.

Two reasons carried the decision, and neither is developer convenience:

1. **Demonstrability.** This tool gets presented. A canvas showing the
   migration flow as an explicit graph reads as a described, inspectable
   process; working code behind an opaque progress bar reads as a black
   box, however good it is. For a tool whose whole proposition is "trust
   this to rewrite hundreds of your modules", being legible to a viewer is
   a product property, not a vanity one.
2. **Learning the tool on a real task**, with a genuine use case rather
   than a toy.

This has a direct design consequence — see "Canvas legibility" below. It
is not free, and it is worth paying for knowingly.

### The honest case against

Our own design already made the app the source of truth and n8n a pure
executor. What is left for n8n is **one workflow containing one AI Agent
node**. That is a thin use of a thick tool, and the pipeline it drives is
fixed and linear (read → edit → write → upgrade → test → verdict → retry),
not something an operator composes differently per run. n8n's core value —
visual composition by someone who will not write code — is not obviously
being collected.

The alternative is an agent loop in our own backend against the Anthropic
SDK's tool use. The tools are already plain HTTP endpoints on Sheller, so
their definitions are mechanical; retry limits, batching and pause/resume
are app-side state in this design regardless. Realistically a few days of
work, in exchange for: everything in git, one process instead of a 4 GB
container, prompts reviewed in pull requests, and ordinary debugging.

### What n8n actually buys (checked against its docs, not assumed)

- `Max Iterations` on the agent node — our `retry_limit`, natively.
- `Return Intermediate Steps` — the agent's tool-call trace as an output
  field, so the agent loop is not opaque even though the canvas draws it as
  one box.
- Human review on tool calls (approve/deny before a tool runs, via a chosen
  channel) — part of our `ask_human` design, built in.
- `N8N_AGENTS_TRACING_ENABLED` — OpenTelemetry spans for agent runs.
- Swapping the model per batch as node configuration rather than code.

### The longer-term argument — now a requirement

The migrator is meant to be a **generic, reusable product across
projects**, and different client projects will want the flow adjusted:
extra checks, a different test strategy, a project-specific step. So the
workflow is not fixed in the product — it is a **swappable cartridge**,
selected per project from the app. See "Workflow cartridges" below.

This settles the question that was left open when n8n was chosen. It also
raises the stakes: once the flow is swappable, the app↔workflow contract
stops being an internal detail and becomes the product's real API.

### Canvas legibility (consequence of the decision)

If the canvas is partly the point, it must actually show something. The
naive build — one AI Agent node that does everything — draws as a single
box. That is a black box with extra infrastructure: the worst of both
options, and the outcome we get by default if we don't design against it.

So the workflow is **staged explicitly**, splitting on a line that is good
engineering independently of how it looks:

- **Deterministic steps get their own nodes**: write files into the new
  container's addons path, trigger the module upgrade, run tests, evaluate
  the verdict against the strictness level, loop for retry, report back.
  These are not judgment calls and should never have been agent decisions
  — making them nodes removes the chance the agent improvises them, and
  stops us paying tokens for the model to rediscover fixed control flow.
- **Agent nodes handle judgment only**: understand current behavior on the
  old version, rewrite the module, diagnose a failure and decide what to
  change next.

The canvas then tells the actual story stage by stage, each stage is
independently inspectable when something breaks, and `Return Intermediate
Steps` supplies the detail inside the agent stages. This is more legible,
more debuggable and cheaper at once.

The concrete graph — 17 nodes, three of them agents, with the retry cycle
drawn as a real loop — is in `docs_/n8n-workflow-flow.md`, along with how
each node is built.

Build decision: each step is an `Execute Workflow` node backed by a
sub-workflow, cut per logical pipeline operation rather than per HTTP
call. Raw HTTP nodes in the main flow would turn this graph into ~40
boxes, losing the legibility we chose n8n for; a custom
`n8n-nodes-sheller` package is the right end state but premature while the
operation set still moves.

The tension to watch: every stage boundary is a chance to drift state into
n8n, which contradicts "the app is the source of truth". Stages pass data
through, they do not accumulate it. If a stage needs to remember something
across a retry, that belongs in the app.

### The exit path (do this regardless)

Keep n8n **replaceable**, so the choice is not load-bearing:

- The app sends the resolved `system_prompt` and `rules` in the
  trigger payload. They are **not** configured inside the n8n node and
  **not** passed as file paths. Two reasons, one of them structural: the
  quality of this product is the quality of its prompts and rules, and we
  already decided rules live in git as reviewable text — a system prompt
  buried in node configuration is outside git, undiffable and unreviewable,
  which contradicts that decision directly. (The practical reason: n8n in a
  container cannot read the host project's paths without extra mounts.)
- The n8n node configuration therefore holds close to nothing: model
  selection and the tool list.
- Everything else — queue, batches, statuses, notes, retry accounting,
  human answers — is already app-side by design.

With this in place, replacing n8n with an in-process agent loop is roughly
a day's work against a stable webhook contract, not a rewrite. The
decision is therefore reversible, and taken as such.

### Provider independence

Nothing here binds us to one model vendor. n8n attaches the model as a
Chat Model sub-node (Anthropic, OpenAI, Google, Ollama, gateways), so
"model per batch" is configuration, not code — and the Anthropic sub-node
exposes prompt caching (5 min / 1 hour), which matters because our system
prompt plus version rules is long and repeats on every module.

Optional and compatible with n8n: pointing an OpenAI-compatible sub-node
at a **LiteLLM proxy** instead of a provider directly, which centralizes
keys, fallbacks and per-call cost accounting. Worth it mainly for cost
visibility — migrating hundreds of modules costs real money, and
per-module/per-batch spend is a metric the UI should plausibly show. It
also survives a change of orchestrator, so it makes the exit path cheaper
rather than deeper. Not needed for the MVP; do not stack a LiteLLM SDK
*and* n8n's own model nodes.

Caveat regardless of the layer: a portable API is not portable behavior.
Tool-use discipline varies a lot between models, so prompts tuned for one
will likely need retuning for another. Swapping is one line; validating
the swap is a run.

## Concurrent migrations

A **migration** (old project at version A → new project at version B) is
the app's top-level entity, and several run at once. Opening one never
blocks another: switching the view changes what is displayed, never what
is executing.

Scoped per migration: project paths, its two containers, module graph,
queue and batches, statuses, notes, findings, rules selection, pinned
cartridge, strictness/retry/ask-human settings, run history.

Shared app-wide: the n8n connection, the Sheller daemon, installed
cartridges, the rules library, model credentials.

Consequences beyond the UI:

- **Sheller.** One daemon per host, many sessions — which is how Sheller
  already works, so concurrency costs nothing here. But sessions from
  different migrations coexist in one registry, so every session must
  carry enough identity to attribute it to its migration.
- **Containers.** Each migration holds its own old/new pair, so two
  migrations mean four Odoo containers, with the RAM and port allocation
  that implies. Container names and published ports must be derived per
  migration, not fixed. The app does not forbid this — it reports it.
- **n8n.** Concurrent executions of the same workflow are fine, but
  `job_id` must be unique across migrations, and the callback has to
  route to the right migration. The payload therefore carries the
  migration id, and a cartridge pinned in one migration does not affect
  another.
- **Attention routing.** A migration that stops on `waiting_for_human`
  while the operator is looking elsewhere must surface globally, or
  concurrency turns into silent stalling. This is a product requirement,
  not a UI nicety.

## Networking: n8n is containerized

n8n runs from its official image under Docker Compose — `npm install` has
been deprecated since n8n 3.0, and Compose is the supported path (4 GB
RAM, 2 vCPU minimum per their docs).

That creates a problem worth stating before it is discovered at runtime:
**inside the container, `127.0.0.1` is the container**. Both things the
workflow needs — the Sheller API and the app — sit on the host's
loopback.

The usual answer is `host.docker.internal` (with
`extra_hosts: host-gateway` on Linux). **It does not work for Sheller**,
and not by accident: Sheller's contract requires publishing to
`127.0.0.1` only, because that published address is the entire security
boundary around an API that executes arbitrary code as `SUPERUSER_ID`
with no authentication. A connection arriving through the host gateway is
not loopback and is correctly refused. Two of our own decisions collide.

**Resolution: the app proxies Sheller.** n8n is configured with exactly
one host address — the app's — and reaches the daemon through a thin
pass-through on it.

Why this over the alternatives:

- Sheller stays strictly on loopback; its contract is honored rather than
  bent.
- The app runs on the host, so it reaches `127.0.0.1:8765` **whichever of
  the three resolution levels produced the daemon**. The network topology
  stops depending on how the daemon happened to start today.
- One address to configure instead of two.

Rejected: publishing Sheller on the Docker bridge address (weakens the
boundary we just defended); `network_mode: host` (behaves differently on
Docker Desktop than on Linux); joining n8n to the Sheller container's
network (only exists at resolution level 3 — at levels 1 and 2 Sheller is
not a container at all).

The cost is one hop and a small proxy. The `write_key` already travels
through the app in the job payload, so nothing new is exposed.

**Also set n8n's own external URL explicitly** in Compose. n8n generates
webhook URLs from what it believes its address to be, and the app stores
that production webhook URL — left to guess, it hands out an address
nothing can reach.

## Workflow cartridges

The n8n workflow is a replaceable unit, selected per project. One project
runs the stock flow; another runs a variant with an extra lint stage or a
different test strategy. The app installs, selects and pins them.

### Granularity: stage override first, full swap as escape hatch

Forking the whole flow per project is the obvious design and the wrong
default — eight forked copies means fixing every bug eight times, and they
drift. Most real variation is one stage, not the whole pipeline.

The sub-workflow decision already made for canvas legibility gives the
seam for free: the main flow stays ours, and a cartridge is mostly a
**mapping of stage → sub-workflow**. Override the test stage, inherit the
rest. A full replacement of the main flow stays available for the project
that genuinely needs a different shape, and is understood to be on its
own for updates.

### Packaging and install

n8n has the mechanics for this natively, so we do not invent a format:

- A cartridge ships as an **n8n package** (`.n8np`) — the main workflow
  plus its sub-workflows in one archive.
- The app installs it through the public API:
  `POST /api/v1/n8n-packages/import`, `multipart/form-data` (any other
  content type returns 415), with `workflowConflictPolicy` and a target
  `projectId`. Authentication is the `X-N8N-API-KEY` header, so the app
  holds an n8n API key in its settings.
- Each migration project maps to its own **n8n project** (`projectId`), so
  cartridges for different clients do not collide in one namespace.
- Export is the mirror: `POST /api/v1/n8n-packages/export` with
  `workflowIds`. That is how a project-specific variant gets captured and
  shared back.

To verify before building on it: that packages and the public API are both
available on the edition we self-host, and not gated. If they are, the
fallback is manual import in the n8n UI with the app storing only webhook
URLs — workable, less smooth.

### Each cartridge declares itself

Our own manifest travels with the package: cartridge id, version, the
**contract version** it implements, the webhook path, required
parameters/credentials, and the declared capabilities (which
`ask_human_mode` values it honors, which strictness levels it implements,
whether it supports old-version re-probing). The app refuses to run a
cartridge whose contract version it does not speak, rather than
discovering the mismatch on module 40.

### Pinning and provenance

The cartridge is chosen per project and **pinned for the duration of a
run**. Every module result records which cartridge id and version produced
it. Without that, a migration half-done under one flow and half under
another is unreviewable and irreproducible — and swapping mid-migration is
a thing users will do.

### Validation before a long run

The app can smoke-run a cartridge against one trivial module before
committing a batch of two hundred to it: does it answer the webhook, does
its callback satisfy the contract, does it honor the declared
capabilities. Cheap, and it converts a class of failure from "discovered
after an hour" into "refused at selection".

## Session ownership

The app does **not** open Sheller sessions and hand them to n8n. A session
holds a live registry; the moment the agent edits a module's code on disk,
the currently open session on the target container is stale (Sheller's own
docs: after a `.py` change, the running session keeps the old registry, and
a fresh session is usually needed). Since the agent-loop is built around
editing and retrying, it needs a session it can refresh itself, on demand —
a single pre-opened session handed down from the app dies after the first
edit.

So: **the agent owns its own sessions**, opened directly via `POST
/api/sessions`, on both containers:

- Old (source) container: every session opened with `allow_commit: false`,
  explicit in the request body, never configurable.
- New (target) container: every session opened with `allow_commit: true`,
  explicit in the request body. Not a convenience — the pipeline mechanically
  needs it: later modules may depend on earlier ones already being
  installed, which requires real commits to the target database as the
  pipeline progresses.

Verified directly against Sheller's code (`session.py`): an explicit
`allow_commit` in the request body is honored for any `owner.kind`; the
"agent can't open with commit" restriction lives only in the MCP wrapper's
own hardcoded chat-agent policy (`os_open_session`), which the migrator never
uses. **No change needed in Sheller for this.** The one unconditional lock
that remains, regardless of anything we build, is `target.stage ==
PRODUCTION` forcing `allow_commit = False` inside Sheller itself — and the
migrator's target container is never production.

The app receives the current `session_id` in the result callback purely for
driving the live-observer iframe — it has no session-management role.

## Rules knowledge base

Hybrid, deliberately:

- A static, curated knowledge base, **one document per version** (files,
  e.g. `rules/18.0.md`) — what that version changed: renamed generic
  methods, moved fields, new idioms, removals. Analogous to OpenUpgrade's
  migration scripts.
- The agent's own research via Sheller's `os_source` when the static base
  doesn't cover a case — reading and diffing the actual framework source
  between versions.

### Rules are per version, not per pair

One document per version, never per pair. `rules/18.0.md` describes what
18.0 changed, on its own terms.

The reason is combinatorial: pair documents grow as the square of the
supported versions, and every new Odoo release would require writing a
document against every earlier one. Most of those combinations would never
be used, and the ones that were used would each duplicate the same facts.

**The app assembles, the document does not.** For a migration the app
concatenates the relevant per-version documents **in version order** and
sends the result. What goes in:

- the **source** version's document, as context for what the code is
  coming from;
- every version **after** the source up to and including the target — what
  changed along the way.

So 17.0 → 18.0 sends two documents; 16.0 → 19.0 sends four.

**This is what makes multi-version jumps possible at all.** A 16.0 → 19.0
migration has no pair document and never would have; as an ordered stack
of per-version documents it is just a longer list. The module is still
ported in one hop between two containers — only the rules accumulate.

**Consequence for prompt caching.** The assembled rules are the large,
stable prefix of every request in a migration, identical across all its
modules. That is exactly what Anthropic prompt caching is for, and it is
the main reason the option on the model node is worth switching on.

**Consequence for findings promotion.** When an observation is promoted
from a run into the rules base, it has to land in a specific version's
document — normally the version whose change caused the issue. The UI has
to ask which, since the app cannot infer it.

Per-module notes are a different, separate mechanism (see "Human-in-the-loop
feedback") — runtime, app-owned state, not part of this curated base.

## Autonomy and batching

The user selects a block of modules on the (editable) dependency tree — one
module, or many — and triggers "migrate." The block runs autonomously
through the per-module cycle above, with no pause between modules within the
block. When the block finishes, the pipeline stops and waits: the human goes
and looks at the live containers (old and new, via the observer iframe),
reviews, and decides whether to continue to the next batch. Batch size 1
means review after every module; a larger batch means review once per
block.

## Done criteria — configurable strictness

Set per module or per group of modules, from the UI:

- **Baseline (always required)**: the module installs, upgrades, automated
  tests (`run_test`) pass, no errors in the log/stderr.
- **Enhanced (optional)**: additionally, the agent must directly exercise
  every backend method of the module — not only through tests — to catch
  cases where a module calls a generic framework method that's been
  renamed or removed in the new version but isn't covered by its own test
  suite.
- At both levels, the agent must read and react to stdout/stderr warnings
  (deprecated methods/fields in the new version), not just treat "no
  error" as "nothing to look at."

## Retry

Within one module, the edit → upgrade → test loop (the agent-loop inside
the n8n workflow) runs until green or until an iteration limit. The limit is
a configurable interface setting (like strictness), never hardcoded. Hitting
it returns `failed` with the full log of attempts; the module goes red in
the tree, and the batch stops on it rather than silently continuing.

## Human-in-the-loop feedback

A third channel between the app and n8n, beyond "run module X" and "here's
the result":

- **Per-module notes** — persistent, app-owned annotations on one specific
  module (distinct from the version-pair Rules Store). The user can write
  one any time, especially once a module is `failed`: "do X, because Y."
  On the next run of that module, the note is passed as extra context.
- **The agent can ask a human** — an `ask_human(question)` tool in the AI
  Agent's toolset. Calling it pauses the workflow (n8n `Wait` node, resumed
  via webhook); the module's status becomes `waiting_for_human` (distinct
  from `failed` — a deliberate pause, not a limit hit), with the question
  shown in the UI. The human answers there; the app calls the workflow's
  resume webhook with the answer, and the loop continues.
- A batch does not silently treat `waiting_for_human` as "keep going" — like
  `failed`, it stops the batch on that module until a human responds.

**"Don't ask human" mode** (a toggle, per batch or global) — for when a human
won't be around to answer:

- `ask_human` no longer pauses the workflow. The agent must make its own
  best-effort call, explicitly flagged as an assumption, and continue.
- The question and the assumption are logged to an accumulator rather than
  blocking the module. The module proceeds on its normal pass/fail path but
  is flagged as carrying unreviewed assumptions, even if it ends up green.
- At the end-of-batch checkpoint, accumulated questions and assumptions are
  surfaced alongside the normal review. Corrections become per-module notes
  and can trigger a targeted re-run of just those modules.

## Model selection

Only the LLM behind the n8n AI Agent node changes per batch — never the
whole execution agent (see Non-goals). Implementation is at the canvas
level, not a runtime parameter: the shared agent-loop logic (read old
version → edit → test → retry) lives in one sub-workflow (`Execute
Workflow` node); on the canvas sit several thin wrapper workflows, each
wiring one model/credential set and calling the shared sub-workflow (e.g.
`migrate-module-claude`, `migrate-module-deepseek`, `migrate-module-gpt`).
The app picks which webhook (which wrapper) to call per batch — the model
choice is visible on the canvas, not buried in a JSON payload, and there's
no duplicated agent logic.

## UI

Desktop shell (browser-first, then Tauri), same shape as the Sheller
desktop app:

- iframe: n8n canvas — the workflow's own logic (node graph), not the module
  graph.
- iframe: Sheller `/web` — live session observer, no write key handed to it.
- A separate pipeline-progress panel — live status of the module set
  (queued / running / done / failed / waiting_for_human) — not the same
  thing as the n8n canvas.
- Settings.

## Module dependency graph

Not parsed from `__manifest__.py` by us. On start, the user gives a project
plus source/target version. The old-version container must already be
running, or gets started from the UI. The app opens a Sheller session
against the live old-version Odoo and asks Odoo itself to resolve the
module/dependency graph (it already knows how), returning it to the app as
a tree.

The tree is editable in the UI: already-migrated modules can be struck out,
nodes dragged/regrouped into a sequential pipeline (the batches used for
autonomous runs above).

## Container provisioning

The user points the app at the old project's path and the new (target)
project's path at startup. Both already have a compose file prepared by the
user/devops — the app doesn't generate one. The app can:

- **up** — start the containers from compose if not running;
- **attach** — join already-running containers (same rule as Sheller's own:
  ours to attach to, someone else's is an error, never steal the port);
- **restart / rebuild** as needed.

This is deterministic infrastructure work (docker-compose CLI/SDK), done
directly by the app's backend — not an agentic task, no LLM/MCP involved.
The agent only ever touches Odoo through Sheller; it never touches Docker.
The app's own backend doesn't need to be containerized itself (plain Python
+ venv is enough) — it's a tool that manages other people's containers, not
one that needs to live in a container of its own. Distribution later goes
through the Tauri bundle.

## Sheller daemon resolution

Three levels, in priority order, instead of a plain "attach or fail":

1. **Attach to a system instance** — `GET http://127.0.0.1:8765/health`. If
   a live Sheller API answers, attach as a client; its lifecycle isn't ours
   (never killed on migrator exit). Covers "the desktop app is already open"
   or "someone already started a daemon." Sheller's contract is explicit
   that a daemon that answers may not be ours and must not be stopped.
   Before level 3, one intermediate check: nothing on the port, but
   `docker ps -q --filter label=io.github.romi477.odoo-sheller` finds a
   container — then a Sheller container exists and is stopped or unhealthy;
   `docker start` it (or recreate) rather than running a second one.
2. **Spawn a locally installed core** — port free, so look for the
   `odoo-sheller` binary/CLI on the system (headless, no Tauri UI). If
   found, spawn it as a subprocess and own its lifecycle. Confirmed already
   architecturally ready on Sheller's side: `packaging/odoo-sheller.spec`
   freezes the daemon independently of the desktop app bundle. Only
   distribution (is the frozen daemon binary published on its own) is open.
3. **Provision via the Docker image** — neither of the above; pull and run
   Sheller's published container image, mounting the host's `docker.sock`
   (Docker-outside-of-Docker, so the daemon inside can still `docker exec`
   into the target Odoo containers). The migrator owns its lifecycle.

If none of the three works, fail explicitly — never silently fall through.

Details confirmed directly against Sheller's code and design docs (no
guessing left):

- The daemon is a plain host process that itself drives `docker exec -i
  <container> ...` as a pipe into `odoo-bin shell`. Nothing is installed or
  copied into the target container (the bootstrap flows in over the exec
  pipe's own stdin heredoc) — no target-container prep needed at any level.
- Session ownership (agent-owner, never reading `admin.key`) doesn't depend
  on which resolution level fired.
- Level 3 is now implemented and its runtime contract is published as
  `odoo-sheller/docs/container.md` — that file, not the design doc, is what
  the migrator codes against. Supported hosts: Linux and macOS with Docker
  Desktop, including rootless Docker. Windows and a non-unix-socket
  `DOCKER_HOST` are out.

  The exact start the migrator issues:

  ```bash
  docker run -d \
    -v /var/run/docker.sock:/var/run/docker.sock \
    -v "$HOME/.odoo-sheller:/data/.odoo-sheller" \
    -e ODOO_SHELLER_UID="$(id -u)" -e ODOO_SHELLER_GID="$(id -g)" \
    -p 127.0.0.1:8765:8765 \
    ghcr.io/romi477/odoo-sheller:<version>
  ```

  All five flags are mandatory, and the migrator must treat them as such:

  - The socket mount is the only way into the Engine (missing: **exit 1**).
    Under rootless Docker only the host side changes —
    `$XDG_RUNTIME_DIR/docker.sock` — the in-container path is always
    `/var/run/docker.sock`.
  - The state mount is a **bind mount** of the host's real
    `~/.odoo-sheller` onto `/data/.odoo-sheller`, so journal history is one
    history across all three resolution levels. A named volume would fork
    it by how the daemon happened to start that day.
  - `ODOO_SHELLER_UID`/`_GID` decide who owns those journals. On a first
    run there is no directory to infer them from (missing: **exit 2**, on
    every host by design), so the migrator always passes them explicitly.
  - `-p 127.0.0.1:8765:8765` is the entire security boundary: inside the
    container the daemon binds `0.0.0.0` because container loopback is not
    what `-p` reaches, so the published address is the only thing keeping
    an unauthenticated arbitrary-code-execution API on this machine. The
    migrator must never publish it more widely.
- Readiness: the image carries a `HEALTHCHECK`. Wait on
  `docker inspect --format '{{.State.Health.Status}}' <id>` reaching
  `healthy` rather than polling the port ourselves. The daemon opens no
  session on its own, so `healthy` means ready.
- Container name is the migrator's to pick (nothing in the contract depends
  on it). Discovery is by label instead:
  `docker ps -q --filter label=io.github.romi477.odoo-sheller`. Shutdown of
  a container we started is `docker rm -f` — and only of one we started;
  the label alone doesn't prove ownership.
- Port-conflict hygiene: starting a second daemon on a taken port exits 125
  with `bind: address already in use` **and leaves a container behind in
  `created` state**. With `--name` the retry then fails on the name and
  points at the wrong problem. If the migrator hits this, it removes the
  leftover before retrying — which is also why the probe in level 1 comes
  first.
- `/health` is unauthenticated and session-free, and returns `container`
  (true/false) plus an `mcp` object with the exact command to spawn the MCP
  server for *that* daemon — for a containerized one,
  `docker exec -i <id> python -m odoo_sheller.mcp`. The migrator does not
  use MCP itself (the n8n agent calls the HTTP API directly), but this is
  the human-escalation path when nothing Sheller-related is installed on
  the host, and the UI should surface the command verbatim rather than
  construct one. `"mcp": null` means the daemon cannot say (frozen build,
  MCP tree missing) — show nothing rather than invent a command.
- The image is published to GHCR, tagged by release. **The package is
  private** (accepted deliberately for now — access is controlled manually
  while the user base is small). Consequence for the migrator: level 3 is
  not fully hands-off — `docker login ghcr.io` with a `read:packages` token
  is needed on each new host, and access is granted per user/team in the
  package's own settings, separate from the repository's. Open, not
  blocking: whether the migrator prompts for and performs this login
  itself, or documents it as a one-time manual step.
- Security note to carry into the migrator's own docs: mounting the socket
  grants the host's whole Engine (every container, root-equivalent), not
  just the Odoo ones. Equally true of a natively installed daemon; worth
  stating because a container looks like isolation and gives none here.

## Webhook contract (app ↔ n8n)

This contract is **the product's API**, not an internal detail — workflows
are swappable cartridges (see "Workflow cartridges"), so it is versioned,
declared by each cartridge, and validated before a run.

Verified against a real n8n (2.39.9) rather than against documentation:
`docs_/n8n-spike-findings.md`. A workflow must be **published** for its
production webhook to exist at all — an unpublished one answers 404, which
is at least loud.

**Trigger** (app → n8n webhook, `POST`) — no pre-opened sessions; the agent
opens its own (see "Session ownership"). Note `system_prompt` and
`rules` are sent as **resolved text, not paths**: see "Why n8n, and how to
remove it" for why this matters more than it looks. `rules` is an ordered
list, one entry per version, assembled by the app — see "Rules are per
version, not per pair".

```json
{
  "contract_version": "1",
  "migration_id": "uuid",
  "job_id": "uuid",
  "cartridge": {"id": "stock", "version": "1.0.0"},
  "module": {"technical_name": "sale_custom", "old_path": "...", "new_path": "..."},
  "source_version": "17.0",
  "target_version": "18.0",
  "containers": {"old": "container_name_old", "new": "container_name_new"},
  "sheller_base_url": "<app_base_url>/api/sheller",
  "system_prompt": "the full agent system prompt, resolved by the app",
  "rules": [
    {"version": "17.0", "role": "source", "text": "full text of rules/17.0.md"},
    {"version": "18.0", "role": "target", "text": "full text of rules/18.0.md"}
  ],
  "//rules": "ordered by version; role is source | intermediate | target",
  "module_notes": "free text, or null",
  "strictness_level": "basic|enhanced",
  "retry_limit": 5,
  "ask_human_mode": "sync|batch_defer",
  "callback_url": "<app_base_url>/api/jobs/{job_id}/result",
  "progress_url": "<app_base_url>/api/jobs/{job_id}/progress"
}
```

**Every URL in this payload is `app_base_url`, never host loopback.** n8n
runs in a container, where `127.0.0.1` is the container. `sheller_base_url`
points at the app's proxy; the daemon's own `127.0.0.1:8765` stays inside
the app process and is never sent.

**This rule cannot be verified by running it on a Mac.** Docker Desktop
forwards `host.docker.internal` onto host loopback, so a payload that
wrongly carries `127.0.0.1:8765` works there and fails only on Linux — the
spike confirmed both halves. A payload containing a loopback host is
therefore checked mechanically, not by trying it.

**Progress** (n8n → app, `POST` to `progress_url`, several times per job):

```json
{
  "job_id": "uuid",
  "stage": "understand|rewrite|upgrade|tests|smoke|verdict|diagnose|reprobe|waiting_for_human|cleanup",
  "attempt": 1,
  "resume_webhook_url": "optional, see below"
}
```

`$execution.resumeUrl` resolves **before** the Wait node runs, so progress
can carry it and the app can persist it then rather than waiting for the
result callback. Keep the whole URL including `?signature=` — stripping the
query makes resume fail.

**Result callback** (n8n → app, `POST` to `callback_url`):

```json
{
  "job_id": "uuid",
  "status": "passed|failed|waiting_for_human",
  "current_session_id": "for the live observer iframe",
  "attempts": [{"iteration": 1, "diff": "...", "test_output": "...", "verdict": "fail"}],
  "assumptions": [{"question": "...", "assumption": "..."}],
  "question": "only if status=waiting_for_human",
  "resume_webhook_url": "only if status=waiting_for_human"
}
```

**Resume** (app → n8n, `POST` to `resume_webhook_url`, only for
`waiting_for_human`):

```json
{"answer": "the human's answer"}
```

## Stack

| Layer | Choice |
|---|---|
| Backend | FastAPI (Python) |
| Templates | Jinja, server-rendered |
| Live updates | SSE, one stream per updating element |
| Interactivity | htmx for server round-trips, Alpine.js for local UI state |
| Persistence | SQLite via SQLAlchemy |
| Docker control | Docker SDK for Python |
| Module graph | a framework-agnostic graph library (Cytoscape.js or similar) |
| Desktop wrapper | Tauri, later |

### Backend

FastAPI. Same language as Sheller and the Odoo ecosystem the team works
in. The app computes little — it triggers webhooks, receives callbacks,
holds state and pushes updates, which is what async suits. Pydantic does
real work here rather than ceremony: validating `contract_version` and
cartridge manifests is exactly its job, and that validation is a product
requirement now that workflows are swappable.

**No Celery or RQ.** The heavy, long-running work happens in n8n; the app
waits for callbacks. In-process background tasks cover container
operations and daemon resolution. Adding a broker here would be
infrastructure for work that does not exist.

**SQLite via SQLAlchemy.** Migrations, modules, statuses, attempts, notes,
findings and run history are real state that must survive a restart. The
rules knowledge base stays as files (decided earlier, for git review); run
state does not belong there.

### Frontend: Alpine, not React

Server-rendered Jinja, htmx for anything that touches the server, SSE for
live updates, Alpine for local UI state (dropdowns, expanders, the
migration switcher).

Measured from the actual mockup: one screen contains 35 loops, 27
conditionals, 34 click handlers and 279 data bindings — and there are
eight-plus screens. That rules out hand-written vanilla DOM code: it means
writing a small reactive framework, which would become the least reliable
part of the codebase.

Why not React:

- A second toolchain (Vite, node_modules, a build step, another
  dependency manager, another CI stage) inside a Python repository.
- **It duplicates state by design.** The app is the source of truth,
  server-side. React wants to own client state, which means building a
  JSON API plus a client store and keeping them in agreement — paying for
  an architecture that contradicts one already chosen.
- SSE into server-rendered fragments is less code than EventSource plus a
  reducer plus re-render discipline.
- Neither is "what the team already knows" — Odoo's own frontend is
  neither. Alpine is learnable in an evening.

The one genuine argument for React was the module graph screen (marquee
selection, drag, regrouping). It is answered by a library rather than a
framework: graph renderers are framework-agnostic and mount into a plain
`div`.

**Escape hatch, and its trigger.** React can be mounted into a single
`div` inside an htmx page, so one screen can convert without the app
converting. The trigger to do it: catching ourselves holding client state
more complex than open/closed in three or more places. That is a signal,
not a premonition. Same principle as everywhere else in this design —
take the lighter thing, keep the seam.

### Styling

The mockup carries **499 inline `style` attributes and no classes**, with
the tokens declared once on a wrapper. Do not carry that into the
templates; the first implementation task is extracting it into a real
stylesheet. The base already exists: Sheller's `web/style.css`, whose
token set the mockup reuses unchanged. Migrator-specific components are
added on top of it rather than reinvented.

## Environment and build order

Browser-based web app first (MVP, so desktop packaging doesn't block
progress), wrapped into the Tauri desktop shell once it works. Tauri
constrains none of the stack choices above — it wraps a webview pointed at
localhost.

n8n itself (per its own docs): npm install is deprecated as of n8n 3.0 —
Docker Compose or the one-line setup script (`curl -fsSL https://get.n8n.io
| sh`) is the supported path. Docker Compose's official minimum is 4 GB RAM
+ 2 vCPU. The standard compose includes a `sandbox-runner-1` service (a
Docker-in-Docker sandbox for AI-generated code) that we most likely don't
need — our agent never executes code through n8n's own Code node, only
through Sheller — but this should be confirmed during implementation.
Locally on a dev Mac this adds up to: Docker for n8n, Docker for the two
Odoo containers (old/new version), and the app's own backend without Docker
(venv) — not weightless, but reasonable for a dev machine.

## Open, deliberately

- ~~Whether n8n is the right orchestrator at all.~~ Decided: n8n, for
  demonstrability and to learn the tool on a real task. See "Why n8n, and
  how to remove it" — the exit path and the staged-canvas constraint in
  that section are binding, not optional.
- **Where exactly the stage boundaries fall** in the staged workflow (how
  much the agent nodes own vs. the deterministic nodes) — the principle is
  settled and a first cut is drawn in `docs_/n8n-workflow-flow.md`, but the
  split will move once we see real runs.
- **Embedding the n8n UI in the app's iframe.** Three separate obstacles,
  only the first of which is trivial: (a) n8n refuses framing by default —
  set `N8N_CONTENT_SECURITY_POLICY={"frame-ancestors":[...]}`, the only
  security-header variable it has; (b) cross-origin session cookies are
  blocked as third-party in Chrome, so the frame would show a login the
  user cannot complete — the clean fix is a reverse proxy putting the app
  and n8n on **one origin** (n8n behind a subpath) rather than fighting
  cookies; (c) n8n has a commercial OEM/embed path
  (`N8N_EMBED_LOGIN_ENABLED`, short-lived RS256 JWT exchange, iframe SSO),
  which implies embedding is licensed. Irrelevant for an internal tool,
  potentially relevant if the migrator is ever sold with the canvas inside
  — to confirm with n8n, not to guess at. Also: frame a specific
  **execution view**, not the full editor, which is too heavy for a panel.
- Whether the migrator performs `docker login ghcr.io` itself (prompting
  for a token) or documents it as a manual one-time step per host, for
  Sheller daemon resolution level 3.
- ~~Rootless Docker~~ — resolved on Sheller's side: supported, only the
  host side of the socket mount changes (`$XDG_RUNTIME_DIR/docker.sock`).
  What remains for the migrator is detecting which socket path this host
  actually uses before it builds the `-v` flag.
- Exact test-class-by-test-class scheduling and timeout tuning for "run all
  tests for a module" (N sequential `run_test` calls) — mechanical, deferred
  to implementation.
- **Minimum daemon version.** `/health` reports `version`. At level 1 the
  migrator attaches to a daemon it did not start and cannot upgrade — it
  should check that version against a minimum it needs and refuse loudly
  instead of failing later on a missing endpoint. Which minimum, and
  whether a too-old daemon is a hard stop or a degraded mode, is open.
- **Which image tag level 3 pulls.** A pinned release tag (tested against)
  rather than `:latest`, but the pinning policy — bumped by hand per
  migrator release, or configurable — isn't decided.
- **Level 2 distribution** is still the weakest link: Sheller's frozen
  daemon is built independently of the desktop bundle, but whether that
  binary is published on its own (so "a locally installed core" is a thing
  a host can actually have without the desktop app) is unanswered.
  `container.md` covers level 3 only.
