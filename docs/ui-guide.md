# Using the web UI

A walkthrough of the screens — Overview, Tree, Pipeline, Flow, Sheller,
Result, Settings — and every control in them. The UI is a local tool for
one developer working a custom Odoo project through a version jump.

The UI is served at `/` and is a plain client of the HTTP/SSE API
described in the [README](../README.md#api). The pages ask nobody for a
key. Binding loopback is the boundary — see [security.md](security.md).

## Guiding rules

Worth knowing before the controls, because they explain *why* things
behave the way they do:

- **The migration is the subject.** Opening one never stops another. The
  switcher only changes what is on screen.
- **The app is the source of truth.** A provider runs one turn and
  keeps nothing. Findings, the checkpoint thread and the trace skeleton
  live in SQLite.
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
- **Tree / Pipeline / Flow / Sheller / Result** — the screens of the
  open migration, on the second row. Each one takes the full width.
  Pipeline sits after Tree and before Flow. Sheller follows Flow, then Result.
- **Guide** — `/styleguide`: Tree, Pipeline, Sheller, Result, in that
  order, with the controls on each screen (queue-based Pipeline, Tree
  leave-out rules, Result pagination and corner badges). Flow is its
  own screen and is not one of those four headings. Not a substitute
  for this file.
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
| Source / Target | Chosen from the versions that have a rules file (16.0–20.0 today); target lists only newer ones. The server refuses anything else |
| Old / New project | Where the compose file is. The file tools reach the addons directories the containers mount from here, not the whole project (see Tree) |

There is no "start the pipeline" on this screen. Create, Open, then
Settings and Tree.

## Tree

The dependency graph for the open migration. The canvas is the list.

**Build graph** reads installed custom modules and `depends`
from the old Odoo (through Sheller) and assigns **steps**: in-graph edges
from a root, plus one — the same wave the canvas draws. A later step
cannot start until every module on an earlier step is passed, migrated or
struck.

It then reads the new container, when one is attached. A module already
installed in the target database, that this app has not run and that is
still queued, is marked **migrated**: done, but not checked here. The queue
skips it, it counts toward the green bar with its own "already in target"
counter, and **Reopen** on Result puts it back to queued. A target that is
not attached or does not answer is named above the canvas; the graph from
the old side stands and nothing is marked.

**Where the addons are.** Build graph also reads each container's bind
mounts from Docker and keeps them until the next Build graph — a changed
compose file needs one. Every module is placed from them: its directory on
the host, and inside each container. A port goes into the target directory
mounted at the same container path; failing that, under the new project's
mount. The card on Overview names the directories found, under each
project path ("Addons found in …"). A module that cannot be placed keeps
its status, carries a dashed **path unknown** mark on the node card and
does not start. The reason is one of: *not mounted* (its code is inside
the image), *mount too broad* (the mount is `/`, the home directory or
this app's data directory), *no target directory*. With Docker not
reachable, paths come from the project directories as before.

Grouping blocks for a run happens here (Resolve waves, or drag one module
onto another); starting a block is on **Pipeline**.

Above the canvas, centred **step** buttons (one colour per wave) select
every module on that wave. The **node card** sits under them: a compact dashed panel (same outline
language as **Create migration** on Overview), fixed width (~28rem), not
full bleed. Optional help under the card expands the leave-out rules. One row shows `#rank/step`, the technical name, a status
badge (`excluded` when the module is left out, otherwise `queued`,
`running`, …), and **Exclude** or **Include** (one button for a
single module). A whole **step** or any multi-select shows **both**
buttons so you can exclude the group or bring excluded modules back. A
footnote under
the card (`* …`) explains what the button will do for the current
selection. Under the footnote, **Exclude all** and **Include all** apply
to the whole migration (every module in or out of the plan).

The canvas is Cytoscape. Box-select is on. Selection is additive.
Each node shows its technical name. `#3/2` in the corner is **migration
order**, then the step: `#1/1` is the first module you migrate (the
root wave); `#2/2` is next, on wave 2. It is not the internal Resolve
serial. The badge stays in the corner, inside the shape; it is not
repeated in the centre when you zoom. Colour follows the step, not a
Group overlay, so a hand-drawn batch cannot collapse four waves into two.
Struck nodes show `(out)`, dashed borders, and faded colour.

**Exclude** and **Include** follow the depends graph, not
“everything vaguely nearby”:

| Selection | Exclude | Include |
|---|---|---|
| Several modules, or a whole step | Those modules and everything **downstream** | Each module's **depends chain upward** only (sibling branches stay out) |
| One module that still has dependents | That module and everything **downstream** | Depends chain **upward** only |
| One **leaf** (nothing depends on it) | **That module only** — parents stay in the plan | Depends chain **upward** only |

When the status is `waiting_for_human` and the module is not excluded,
the card also has an **Answer** form. There is no row per module under
the tree.

| Control | What it does |
|---|---|
| **Build graph** | Re-reads the module graph from the old container: new project
modules appear, vanished ones drop (or stay flagged if you already worked
on them). **Exclude**, status, and notes on surviving rows stay as they
were. Uninstalled addons under the source project path count once Odoo
knows them (Update Apps List). |
| **Exclude / Include** | On the node card; applies to the current selection (see table above). |

Dragging a node onto another puts both in the same group (creating one
if the target had none).

A module that recorded assumptions at `batch_defer` keeps an
`assumptions` badge until you read them at checkpoint.

The lamp's `?focus=` selects and centres that node and opens its card.
Opening the right migration and then leaving you to find one stalled
node in a hundred is not opening the module.

## Pipeline, Flow, Sheller, Result

Pipeline also holds the checkpoint once a batch has finished. Result is
the scoreboard. Flow draws the pinned cartridge.

**Pipeline** (`/run`) has **Selection** and **Queue**. In Selection, pick
a **Step** (same waves as Tree), **Add to queue**, or **Add all steps to
queue**. Every wave still in the plan is listed, including one whose
modules have already passed: that option reads **passed**, names the
modules, and cannot be queued. Excluded modules stay out. **Add all**
skips finished waves. Duplicates are ignored. **Queue** lists pending modules in order; **Run** / **Stop**
drive the sequential pipe (you can add steps while it runs; Stop finishes
the current module first). **Clear** empties the list when the pipe is
stopped and no module is still running. The active module pulses in the queue; passed
modules drop off the list and show on **Result**. The live panel below is the module, its status, and a ribbon of the
cartridge's steps, with the current one marked. A parked question sits
under the ribbon as a thread. Empty reads "No module is running." SSE
fills the stage. When the batch reaches
checkpoint a banner appears: *This batch has finished. Reload to review
it.* The screen does not swap itself for the review while you are
looking at a live stage.

**Flow** (`/flow`) draws the pinned cartridge. The picture is computed
from the file each time the page is opened; nothing about it is stored.
Stock is two frames. **Pipeline** is the lap: `understand` (old session,
behaviour and a fixture recipe), `port` (writes the new project, both
sessions), `lint` (Python, XML, manifest — no container), `upgrade`
(install or upgrade on the new container, then commit), `tests` (the
module's own classes; no classes raises `no_tests` and is not a failure),
`probe` and `write_test` only when `no_tests`, then `verdict`.
**On failure** is entered when any pipeline step fails. `diagnose` raises
`needs_human`, `reexamine_old`, `small_fix`, or `start_over`, and only the
block for that flag runs: `ask_human` parks the job, `reprobe` returns to
the old version, `patch` edits the previous diff, `rewrite` starts the
port again without the previous attempt. The lane then resumes once, at
`lint`. Orange blocks are a model turn; quiet blocks are the app; a dashed
border is a flag; the cut rectangle is `ask_human`. The canvas is the
whole screen: a run's tool calls are the run log on Pipeline, and reading
one job on two screens is what a second list under the picture meant.

**Sheller** (`/sheller`) is only an observer iframe of Sheller's `/web`,
edge to edge under the nav. The frame does not hold a write key. Do not
append `?session=` — Sheller ignores it and it would look like we picked
one. Pick the live session inside Sheller's own UI. `javascript:` in an
iframe `src` would run on this origin; only `http`/`https` become an href.

**Result** (`/result`) is the migration dashboard: progress counts only
modules still in the plan (not left out), then columns **L0, L1, …** (one
per dependency wave) with a card per module. More than four waves paginate:
four columns per page, dash controls in the panel header (one dash per page).
Each card outline follows
the **step** colour; a filled circle in the top-right corner marks
**passed** (green, ✓), **waiting for you** (warning, pause bars), or **excluded**
(circle with slash — left out on Tree). In-flight or failed
modules keep a small inline badge instead. Subtitle is `depends` or
*excluded — skipped on Tree*. Skipped modules stay in their wave, name
struck through. SSE
updates badges while this screen is open. **Reopen** on a finished card,
or **Reopen finished modules…** in the migration's settings on Overview (behind a confirmation), puts `passed`, `failed`, and
`unknown` modules back to `queued` so Selection can queue them again.
The old jobs stay. Files in the new project are not touched. With no
graph yet, the page points you at Tree → Resolve.

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
module that passed first time still has an attempt: the runner records
the unified diff of each step that was allowed to write files.

## Settings

Two tabs, because they differ in kind.

### App settings

Shared by every migration.

| Field | What it is |
|---|---|
| Sheller base URL | This process's client and the observer iframe. |
| Sheller image tag | Docker tag when the app must spawn Sheller because nothing answers on 8765. |
| Provider secrets | One password field per secret a registered provider declares (Anthropic, Cursor). A saved key shows as dots, never as the key. The bin at the end of the field removes it. |

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
| Strictness | Stored on the migration. The stock flow does not branch on it yet: `enhanced` runs what `basic` runs. |
| Retry limit | How many laps the failure lane may take. Not a cap inside one model call. |
| Ask-human mode | `sync` parks the job until you answer. `batch_defer` records an assumption and continues; you read it at checkpoint. |
| Flow | The pinned cartridge, `id@version`. Recorded on every job. Import replaces the pin. |
| Provider / Model | The flow file names a default. The card shows it and can replace it; the model list is the models of the selected provider. The job records the pair that was on the card. A step that names its own model still wins. |

**Smoke-run** starts the first unstruck module by name through the pinned
flow. The button says the run started; the verdict is whatever the
job ends as. A failed smoke-run blocks Batch. With nothing installed
the app is on its stub and a batch is allowed; once a flow exists, an
unknown pin is refused.

## Guide (in-app)

**Guide** in the chrome opens `/styleguide`: Tree, Pipeline, Sheller,
Result, in that order, with the controls that exist today. Flow follows
that list and names each stock block. This file is the long-form
reference.

`/components` is how CSS gets reviewed: badges, row cards, pipeline,
checkpoint, forms, the lamp, the switcher. Add a row in the same change
that adds a component. It is not in the menu.
