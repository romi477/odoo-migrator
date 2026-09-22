# Using the web UI

A walkthrough of the screens — Overview, Tree, Pipeline, n8n, Sheller,
Result, Settings — and
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
  lands on `/components` before the screen that uses it.

## Chrome

The bar is two rows, on every page.

- **Overview** — the list of every migration. It sits on the top row,
  just left of the selection, not beside the wordmark. It is not a
  step of the migration you have open. Guide mirrors it on the
  right of the selection.
- **Migration switcher** — an outlined field, centered in the bar. The
  open migration, by name, a vertical accent mark and the version pair.
  The field is as wide as that migration's steps, and extends past them
  on the right by the same amount the hook occupies on the left. Pick
  another and the cookie follows; a batch already running on the one you
  left keeps running. A rounded hook under the mark points at that
  migration's screens.
- **Tree / Pipeline / n8n / Sheller / Result** — the screens of the open
  migration, on the second row. Each one takes the full width.
- **Guide** — the steps under the open migration, numbered in menu order:
  Tree, Pipeline, n8n, Sheller, Result. Each says what the screen is
  and which controls it has.
- **Attention lamp** — `/attention`. Global: whichever migration has a
  module on `failed` or `waiting_for_human` that has not been dismissed.
  Clicking it opens that migration and focuses the node on Tree
  (`?focus=`). There is no "which one?" picker; the lamp is not a list.
- **Running count** — how many migrations currently have work in
  flight, labelled “N run”. It is a number, not a button.
- **Settings** — an outline gear at the far right of the top row,
  set off by a vertical rule. App-wide only. What belongs to one
  migration lives in that migration's row on Overview.

SSE is hooked on every page (`/static/sse.js`). Stage patches land on
the Tree node card, Result cards, and the Pipeline panel without a
reload. Reloading would wipe a half-typed field mid-edit.

## Overview

The list of migrations this SQLite knows, and the form that creates one.

Each row is collapsed: a chevron, the name, a status badge, the version
pair, **Open** and **Archive**. Click the row — anywhere that is not
Open or Archive — and it expands into that migration's containers and
run settings. Click the summary again to collapse it. Archive names
the row in the accessible label and the tooltip, not as visible text.

**Create migration** is a button until you ask for it. The form then
sits centered, with a cyan outline. It asks for:

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

**Toolbar** — **Resolve** reads installed custom modules and `depends`
from the old Odoo (through Sheller) and assigns **steps**: in-graph edges
from a root, plus one — the same wave the canvas draws. A later step
cannot start until every module on an earlier step is passed or struck.
**Run batch** opens **Group**, **Ungroup**, **Migrate**, and **Batch**
(optional; the main day-to-day control is the node card below).

Under the canvas, centred **step** buttons (one colour per wave) select
every module on that wave. The **node card** sits below them: a compact
dashed panel (same outline language as **Create migration** on Overview),
not full width. One row shows `#rank/step`, the technical name, a status
badge (`excluded` when the module is left out, otherwise `queued`,
`running`, …), and **Leave out** or **Include again**. A footnote under
the card (`* …`) explains what the button will do for the current
selection.

The canvas is Cytoscape. Box-select is on. Selection is additive.
Each node shows its technical name. `#3/2` in the corner is **migration
order**, then the step: `#1/1` is the first module you migrate (the
root wave); `#2/2` is next, on wave 2. It is not the internal Resolve
serial. The badge stays in the corner, inside the shape; it is not
repeated in the centre when you zoom. Colour follows the step, not a
Group overlay, so a hand-drawn batch cannot collapse four waves into two.
Struck nodes show `(out)`, dashed borders, and faded colour.

**Leave out** and **Include again** follow the depends graph, not
“everything vaguely nearby”:

| Selection | Leave out | Include again |
|---|---|---|
| Several modules, or a whole step | Those modules and everything **downstream** | Each module's **depends chain upward** only (sibling branches stay out) |
| One module that still has dependents | That module and everything **downstream** | Depends chain **upward** only |
| One **leaf** (nothing depends on it) | Depends chain **upward**; a shared parent stays in if another branch is still active | Depends chain **upward** only |

When the status is `waiting_for_human` and the module is not excluded,
the card also has an **Answer** form. There is no row per module under
the tree.

| Control | What it does |
|---|---|
| **Resolve** | Reads the module graph from the old container and redraws steps. |
| **Leave out / Include again** | On the node card; applies to the current selection (see table above). |
| **Run batch → Group … Batch** | Manual batch groups and one-off migrate triggers. |

Dragging a node onto another puts both in the same group (creating one
if the target had none).

A module that recorded assumptions at `batch_defer` keeps an
`assumptions` badge until you read them at checkpoint.

The lamp's `?focus=` selects and centres that node and opens its card.
Opening the right migration and then leaving you to find one stalled
node in a hundred is not opening the module.

## Pipeline, n8n, Sheller, Result

Four run screens after Tree, not four columns. Pipeline also holds the
checkpoint once a batch has finished. Result is the scoreboard.

**Pipeline** (`/run`) is the live job: module, status, stage, attempt.
Empty reads "No module is running." SSE fills it. When the batch reaches
checkpoint a banner appears: *This batch has finished. Reload to review
it.* The screen does not swap itself for the review while you are
looking at a live stage.

**n8n** (`/n8n`) is a stub, on purpose. The canvas is not embedded. A
settings string becomes `target=_blank`; an empty iframe `src` is not an
empty frame — the browser would load this page inside itself.

**Sheller** (`/sheller`) is only an observer iframe of Sheller's `/web`,
edge to edge under the nav. The frame does not hold a write key. Do not
append `?session=` — Sheller ignores it and it would look like we picked
one. Pick the live session inside Sheller's own UI. `javascript:` in an
iframe `src` would run on this origin; only `http`/`https` become an href.

**Result** (`/result`) is the migration dashboard: progress counts only
modules still in the plan (not left out), then columns **L0, L1, …** (one
per dependency wave) with a card per module — status dot, badge, and a
subtitle (`depends` list, or *excluded — skipped on Tree*). Skipped
modules stay in their wave, struck through, badge **excluded** (not
`queued`). Wave columns replace batch-group numbers on the card. SSE
updates badges while this screen is open. With no graph yet, the page
points you at Tree → Resolve.

### Checkpoint

A finished batch (not a smoke-run) stops here. Flow id and version
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
| n8n API key | Masked when stored. Focus to replace; save without editing keeps it. **Clear** is the bin beside the field. |
| App base URL | The only host n8n is given. File API, Sheller proxy, progress and result. **Not** host loopback — a payload with `127.0.0.1` is refused at trigger. On Docker Desktop `host.docker.internal` reaches the app; on Linux compose adds `extra_hosts`. |
| Sheller base URL | This process's client and the observer iframe. **Never sent to n8n.** |
| Sheller image tag | Docker tag when the app must spawn Sheller because nothing answers on 8765. |

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
| Strictness | Travels to the flow as `strictness_level`. **The stock flow ignores it**: `enhanced` runs what `basic` runs until a flow honours it. `Module.strictness` exists for a per-module override and no screen sets it yet. |
| Retry limit | Canvas loop gate, not `Max Iterations` on an agent node. |
| Ask-human mode | `sync` parks on a Wait node until you answer on Tree. `batch_defer` records an assumption and continues; you read it at checkpoint. |
| Flow | `id@version` (`cartridge_id` in the payload). The pin is recorded on every job. |
| Model wrapper | Which production webhook path this batch fires: stock `migrate-module`, or a thin OpenAI / Claude parent that Execute-Workflows the same loop. |

**Smoke-run** starts the first unstruck module by name through the pinned
flow. The button says the run started; the verdict is whatever the
job ends as on the callback. Reading `running` a millisecond after the
trigger used to record `failed` every time against live n8n and block
every batch. A failed smoke-run blocks Batch. With nothing installed the
app is on its own stub and a batch is allowed; once a flow exists,
an unknown pin is refused.

## Guide

`/styleguide` is the four steps under the open migration, numbered in
the same order as the menu: Tree, Pipeline, n8n, Sheller, Result. Each step
says what the screen is for and which control does what.

`/components` is how CSS gets reviewed: badges, row cards, pipeline,
checkpoint, forms, the lamp, the switcher. Add a row in the same change
that adds a component. It is not in the menu.
