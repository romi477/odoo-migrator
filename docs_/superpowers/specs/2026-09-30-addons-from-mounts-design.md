# Addons from container mounts, and version selects

Status: design, approved in conversation on 2026-09-30. Not implemented.

Two changes, one spec, because the second is small and was asked for with
the first:

1. A migration's addons can live in several host directories. The app finds
   them from the containers' bind mounts, reads and writes them on the host,
   and ports each source directory into its matching target directory. The
   person types nothing new.
2. Source and target versions on Create migration become selects over the
   versions the app has rules for, with the source strictly older than the
   target.

The `migrated` status (target already has the module installed) shipped
separately and is not part of this spec.

## Why

A real project mounts addons from more than one place. One `odoo.conf`:

```
addons_path = /opt/odoo/odoo/addons,
              /opt/odoo/addons,
              /opt/enterprise,
              /opt/external-addons/integration-queue,
              /opt/external-addons,
              /opt/project,
              /opt/odoo-sh/connectors-extensions
```

Today the app knows one directory per side, `old_project_path` and
`new_project_path`. The graph already lists every installed module with its
path *inside the container*, but `module_paths.host_paths` looks for each
module only under the one project directory on the host. A module under
`/opt/external-addons` is not found: the prompt carries no source, lint has
no directory, and the port is written beside the project instead of where
the target container will load it.

The graph snippet has a second symptom of the same gap. It keeps an
uninstalled module only when its path is under the project roots, and it
compares a container path against a host path. That works only when the two
happen to be equal.

## Decisions

- **The container says where; the disk is read.** Paths come from the
  containers' bind mounts (`docker inspect`). Files are read and written on
  the host, as today. Reading every file through Sheller was considered and
  rejected: one command at a time per session, so a read would wait behind
  an upgrade or a test; sessions are closed while a job is parked on a
  person; answers are truncated and text-only; lint and diffs run on host
  directories; and writes must land on the host anyway, because the target
  container loads the new project from a bind mount.
- **Nothing new is typed.** The project directory on the card stays; it is
  where the compose file is, and the app already uses it to find the
  container. There is no "extra addons, comma separated" field. If a case
  appears that mounts cannot answer, a manual field is its own change.
- **Framework code is read from the container.** Odoo and Enterprise are
  excluded from the plan (`is_odoos_own`), and the agent reads them with the
  existing `source` tool (Sheller `os_source`). This covers the official
  image, where Odoo is inside the image and not mounted. Nothing about this
  changes; the prompt says it plainly.
- **The agent works in container paths.** `/opt/external-addons/sale_x/models/sale.py`,
  the same path Odoo prints in a traceback, in its log and in `source`
  output. The tools translate to the host. The agent never sees a host path.
  Translation lives in one function, used by the tools and by Resolve.

## Part 1 — Mounts and module paths

### Reading mounts

`RunningContainer` gains `mounts: list[Mount]`, where
`Mount = {host: str, container: str, read_only: bool}`, from
`container.attrs["Mounts"]` entries of `Type == "bind"`. Volumes and tmpfs
are ignored: they have no host directory a person can open.

`FakeComposeEngine.add_container` and `compose_up` accept and record
`mounts`. The fake's `compose_up` mounts the project directory at
`/opt/project`, which is what the suite's graph double already reports.

### Snapshot at Build graph

Build graph (`POST /tree/resolve`) reads the mounts of both containers, when
the Engine answers, and stores them on the migration:

- `Migration.old_mounts`, `Migration.new_mounts`: `TextField(default="[]")`,
  JSON lists of `Mount`. Added through `_ensure_columns`.
- An empty list means "not known": Docker was not reachable, or that side's
  container is not attached. It is not an error by itself (see Fallback).

A run does not call Docker for paths; the snapshot is what it uses. A person
who changes the compose file runs Build graph again. The Tree help says so.

### Mapping

`odoo_migrator/mounts.py`, new, holds the only translation:

- `to_host(mounts, container_path) -> Path | None` — the longest mount whose
  `container` is the path or a parent of it; `None` when none is.
- `to_container(mounts, host_path) -> str | None` — the inverse, used to tell
  the agent where a module is on the new side.
- `too_broad(host) -> str | None` — a reason when a mount's host directory is
  `/`, the home directory, a parent of the home directory, or a parent of the
  app's data directory; `None` otherwise.

### Where each module is

The graph row already carries `path` (container). `persist_graph` computes,
per module in the plan:

- `old_container_path` — the graph's `path`.
- `old_path` (host) — `to_host(old_mounts, old_container_path)`.
- `new_container_path` — the same container directory when the target mounts
  it; otherwise the target's mount of the new project directory joined with
  the module's path relative to its old mount; otherwise empty.
- `new_path` (host) — `to_host(new_mounts, new_container_path)`, or an
  existing directory with the module's manifest under the new project, which
  wins as today (`find_module_dir`).

New `Module` columns: `old_container_path`, `new_container_path`
(`TextField(default="")`), and `path_problem` (`CharField(default="")`).

`path_problem` is set, with one of these words, when mounts are known for a
side but the module cannot be placed:

- `not mounted` — the module's directory is inside the image, or under no
  bind mount.
- `mount too broad` — the covering mount failed `too_broad`.
- `no target directory` — the target mounts neither the same directory nor
  the new project.

### Fallback

When a side's mounts are not known, that side works as it does today:
`host_paths` searches the project directory, and no `path_problem` is set on
account of that side. The suite's compose double without mounts, and a
machine where Docker is not reachable, keep today's behaviour.

### The graph snippet

`build_graph_snippet` receives the container directories of the old side's
bind mounts (excluding the Odoo install tree) instead of host project roots,
and keeps an uninstalled module when its path is under one of them. With no
mounts known it receives the old host roots, as today.

## Part 2 — Tools and the security boundary

`read_file(side, path)` and `write_file(side, path, content)`:

- **An absolute path is a container path.** It is translated with the side's
  mounts. It must fall under an *allowed root*: a mount of that side that
  holds at least one module of the plan and is not `too_broad`. Anything else
  is refused, not clamped, with the list of allowed roots in the refusal.
  Mounts holding `odoo.conf`, the filestore or logs are therefore not
  readable through the tool.
- **A relative path keeps today's meaning** — relative to the side's project
  directory — so a side with no mounts known still works, and so do the
  flows and tests written against it.
- **Writes stay inside the module.** `_inside_module` checks against the
  module's `new_path`, unchanged in purpose. The old side is never written.
- **When `read_file` refuses,** the refusal says to read the file with
  `source` from the container. The prompt says the same.

`HOST_TOOLS` is unchanged: neither tool touches a Sheller session.

### Prompt and rules

- The module's location in the agent step is its container path on each
  side (`old_container_path`, `new_container_path`), falling back to today's
  relative path when a side has no mounts.
- `prompts/` and `rules/common.md` stop describing paths as "relative to the
  project root" and describe container paths, with the rule: a traceback
  path is passed to `read_file` as it is; framework code is read with
  `source`.

## Part 3 — Interface

- **Overview, migration card.** Under each project directory, after a Build
  graph: `Addons found in: /opt/project, /opt/sources/external-addons` — the
  host directories of the allowed roots, per side. Nothing is editable.
- **Tree.** A module with `path_problem` shows a quiet `path unknown` mark
  with the reason as its title. It stays in the graph and can be excluded as
  any other.
- **Pipeline.** Starting a step that contains a module with `path_problem`
  is refused with the module names and the reason, the same way an earlier
  unfinished step is refused. Nothing is started partially.
- `/components` gets a row for the `path unknown` mark; `docs/ui-guide.md`
  and `docs/` security text describe the allowed roots.

## Part 4 — Version selects

- **Supported versions** are the ones with a rules file: `rules/<N>.0.md`,
  read through the same layers `assemble_rules` uses. Today 16.0 to 20.0.
  `supported_versions() -> list[str]` in `rules.py`, sorted by major.
- **Create migration** shows two selects instead of two text inputs. Target
  offers only versions newer than the chosen source (Alpine hides the rest);
  defaults stay 17.0 → 18.0.
- **Server validation** stays authoritative: `check_versions` also refuses a
  version not in `supported_versions()`, with the list in the message. The
  existing same-version and downgrade refusals are kept. A hand-made POST
  cannot create what the select would not offer.
- Existing migrations are not touched; their stored strings already passed
  `check_versions`.

## Testing

No Docker, Odoo or daemon, as always.

- `mounts.py`: longest-prefix match, a path under no mount, a sibling with a
  common string prefix (`/opt/project2` is not under `/opt/project`),
  `too_broad` for `/`, home, a parent of home and a parent of the data dir.
- Build graph with a compose double that has mounts on both sides:
  - two source directories land each in its matching target directory;
  - a target that lacks one directory sends that module under the new
    project directory;
  - an unmounted module gets `not mounted` and Pipeline refuses its step;
  - a too-broad mount gives `mount too broad`;
  - no mounts known keeps today's paths and sets no `path_problem`;
  - the graph snippet receives container roots, not host roots.
- Tools: a traceback-style absolute path reads the right host file; a path
  outside the allowed roots and one under an `odoo.conf` mount are refused;
  a write outside the module is refused; a relative path still works.
- Version selects: the page offers exactly the rules files' versions;
  a POST with `17.2`, `21.0`, same version and a downgrade are each refused
  with a message; 16.0 → 20.0 is accepted.

## Out of scope

- A manual "extra addons directories" field.
- Reading project files through Sheller as the normal path.
- Re-reading mounts during a run.
- Editing versions of an existing migration.
