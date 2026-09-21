# Using the web UI

A walkthrough of the four screens — Overview, Tree, Run, Settings — and
every control in them. The UI is a local tool for one developer working
a custom Odoo project through a version jump: speed of the loop (start a
module, see the stage, review the batch) matters more than
discoverability, so a few controls reward knowing they're there.

The UI is served at `/` and is a plain client of the HTTP/SSE API
described in the [README](../README.md#api). n8n talks to the keyed
machine surface; the pages ask nobody for a key. Binding loopback is the
boundary — see [security.md](security.md).

## Guiding rules

Worth knowing before the controls, because they explain *why* things
behave the way they do:

- **The migration is the subject.** Opening one never stops another. The
  switcher only changes what is on screen.
- **The app is the source of truth.** n8n stages pass data through one
  job and ship findings home in the callback. Nothing accumulates on the
  canvas across jobs.
- **One SSE connection per page**, named events — not one stream per
  row. HTTP/1.1 allows roughly six connections per origin, and a batch
  view has dozens of module rows.
- **Appearance lives in the stylesheet.** Inline `style=` is only for
  `--accent`, because some values are per-row data. Every component
  lands on `/styleguide` before the screen that uses it.

## Chrome

The top bar is on every page.

- **Overview / Tree / Run / Settings** — the four screens. Settings
  splits into App settings and This migration under the title.
- **Migration switcher** — the open migration, by name and accent. Pick
  another and the cookie follows; a batch already running on the one you
  left keeps running.
- **Styleguide** — every CSS component, rendered once. A slot that looks
  unstyled is a component the stylesheet has not grown yet.
- **Attention lamp** — `/attention`. Global: whichever migration has a
  module on `failed` or `waiting_for_human` that has not been dismissed.
  Clicking it opens that migration and focuses the node on Tree
  (`?focus=`). There is no "which one?" picker; the lamp is not a list.
- **Running count** — how many migrations currently have work in
  flight. It is a number, not a button.

SSE is hooked on every page (`/static/sse.js`). Stage patches land on
the selected node card and the Run pipeline without a reload. Reloading
would wipe a half-typed notes field.

## Overview

The list of migrations this SQLite knows, and the form that creates one.

Each row is a name, a status badge, the version pair, **Open** and
**Archive**. Archive names the row in the accessible label and the
tooltip, not as visible text — the name is already a few centimetres to
the left, and spelling it out again wraps the button over three lines.

**Create migration** asks for:

| Field | Why it is here |
|---|---|
| Name | The identity the switcher and the logs use |
| Accent | A colour per migration so two concurrent ones stay distinguishable |
| Source / Target | Same-version and downgrade are refused here, where they were typed |
| Old / New project | The two File API roots. A path that later escapes them is 403 |

There is no "start the pipeline" on this screen. Create, Open, then
Settings and Tree.

## Tree

The dependency graph for the open migration. The canvas is the list.

**Resolve** asks the old Odoo (through Sheller) for installed custom
modules and their `depends`, then **groups them into steps**: a module's
step is the number of in-graph edges from a root, plus one — the same
row the canvas draws. Step 3 cannot start until every module on step 2
is passed or struck. The edges are still the order *inside* a step.
Each step has its own colour; the layout is the same dependency tree
as before. Resolve again after the old container changes; that redraws
the waves from the live graph.

The canvas is Cytoscape. Box-select is on. Selection is additive.
Each node shows its technical name. `#12/2` stays in the corner of the
card, inside the shape; it is not repeated in the centre when you zoom.
Colour follows the step, not a Group overlay, so a hand-drawn batch
cannot collapse four waves into two. The number is assigned once and
never reused if that module leaves the graph.

Click a node. The card under the graph is that module and one action:
**Skip** leaves it out of the migration, and every module that depends
on it. **Undo skip** brings that subtree back, except a module that
still depends on another skipped parent. When the status is
`waiting_for_human` the same card has an **Answer** form. There is no
row per module under the tree.

| Control | What it does |
|---|---|
| **Skip** | One decision for the whole selection. Anything still in the pick means skip; otherwise bring them back. Skipping a module also skips every module that depends on it. Undo skip restores that subtree, except where another skipped parent still holds a module out. |
| **Group** | Assigns the selection the next batch number, overlaying the step Resolve drew. Colour stays with the dependency step. |
| **Ungroup** | Clears the batch number. Resolve will put the waves back. |
| **Migrate** | Exactly one module that is not skipped. Two is an error named on the next page, not a silent first-of-two. SSE patches the selected card; the page does not reload. A later step is refused while an earlier one is still unfinished. |
| **Batch** | The selected module's step. A later step is refused while an earlier one is still unfinished. A smoke-run that has not passed blocks this once a cartridge is installed. Review the last checkpoint before starting another. |

Dragging a node onto another puts both in the same group (creating one
if the target had none).

A module that recorded assumptions at `batch_defer` keeps an
`assumptions` badge until you read them at checkpoint.

The lamp's `?focus=` selects and centres that node and opens its card.
Opening the right migration and then leaving you to find one stalled
node in a hundred is not opening the module.

## Run

Three panels, then — when a batch has finished — the checkpoint.

**Pipeline** is the live job: module, status, stage, attempt. Empty
reads "No module is running." SSE fills it. When the batch reaches
checkpoint a banner appears: *This batch has finished. Reload to review
it.* The screen does not swap itself for the review while you are
looking at a live stage.

**n8n** is a stub, on purpose. The canvas is not embedded. A settings
string becomes `target=_blank`; an empty iframe `src` is not an empty
frame — the browser would load this page inside itself.

**Sheller** is an observer iframe of Sheller's `/web`. The frame does
not hold a write key. The live `session_id` is shown so you can pick the
tab inside Sheller. Do not append `?session=` — Sheller ignores it and
it would look like we picked one. `javascript:` in an iframe `src` would
run on this origin; only `http`/`https` become an href.

### Checkpoint

A finished batch (not a smoke-run) stops here. Cartridge id and version
are in the heading — swapping the pin later does not rewrite this
review.

| Control | What it guarantees |
|---|---|
| **Continue** | Starts the next work. Disabled while any job is still `failed` or `waiting_for_human` and not dismissed. |
| **Override** | Continues anyway. Named, not hidden in Continue. |
| **Dismiss** | Marks one stalled job reviewed so Continue can enable. Only failed or waiting jobs. |
| **Promote** | Appends a finding to a `rules/<version>.md`. You pick the version file; the app does not guess. A 17→19 finding that belongs in 18.0.md must not land in 19.0.md by default. |

Each job lists attempts (iteration, verdict, diagnosis, unified diff,
test output), findings, and assumptions taken under `batch_defer`. A
module that passed first time still has an attempt: the stock canvas
logs one, and the app fills an empty `diff` from File API writes on the
new side.

## Settings

Two tabs, because they differ in kind.

### App settings

Shared by every migration.

| Field | What it is |
|---|---|
| n8n base URL | Where this process publishes and triggers. Not hardcoded: 5678 is often already an Odoo longpolling port. |
| n8n API key | Never rendered back. Blank keeps the stored value; typing replaces it. **Clear** is a separate danger button. |
| App base URL | The only host n8n is given. File API, Sheller proxy, progress and result. **Not** host loopback — a payload with `127.0.0.1` is refused at trigger. On Docker Desktop `host.docker.internal` reaches the app; on Linux compose adds `extra_hosts`. |
| Sheller base URL | This process's client and the observer iframe. **Never sent to n8n.** |
| Sheller image tag / minimum version | What to spawn if 8765 is empty, and what is too old to attach to. |

### This migration

The two compose projects, then the knobs for this run.

**Resource honesty** at the top: how many of this app's containers are
running, versus how many containers are on the machine. Docker
unreachable says so; it does not invent zeros.

Each side — old and new — is a card: path, state badge, container name,
**Up / Attach / Restart / Rebuild**. Up starts ours; someone else's port
is an error, not a silent steal. Attach uses a compose project already
running. The migration name and the side live in the heading, so the
eight buttons keep them as the accessible name rather than wrapping
every label over two lines.

| Field | What it is |
|---|---|
| Strictness | Travels to the cartridge as `strictness_level`. **The stock cartridge ignores it**: `enhanced` runs what `basic` runs until a cartridge honours it. `Module.strictness` exists for a per-module override and no screen sets it yet. |
| Retry limit | Canvas loop gate, not `Max Iterations` on an agent node. |
| Ask-human mode | `sync` parks on a Wait node until you answer on Tree. `batch_defer` records an assumption and continues; you read it at checkpoint. |
| Cartridge | `id@version`. The pin is recorded on every job. |
| Model wrapper | Which production webhook path this batch fires: stock `migrate-module`, or a thin OpenAI / Claude parent that Execute-Workflows the same loop. |

**Smoke-run** starts the first unstruck module by name through the pinned
cartridge. The button says the run started; the verdict is whatever the
job ends as on the callback. Reading `running` a millisecond after the
trigger used to record `failed` every time against live n8n and block
every batch. A failed smoke-run blocks Batch. With nothing installed the
app is on its own stub and a batch is allowed; once a cartridge exists,
an unknown pin is refused.

## Styleguide

`/styleguide` is not a fifth product screen. It is how CSS gets
reviewed: badges, row cards, pipeline, checkpoint, forms, the lamp, the
switcher. Add a row in the same change that adds a component.
