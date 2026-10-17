# Addons from mounts implementation plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Find a migration's addons in every host directory its containers
bind-mount, port each source directory into its matching target directory,
let the agent work in container paths, and make the version fields selects.

**Architecture:** Build graph snapshots both containers' bind mounts onto the
migration. One module, `odoo_migrator/mounts.py`, translates between
container and host paths. `persist_graph` places every module with it and
records a `path_problem` when it cannot. The file tools accept container
paths and refuse anything outside the mounts that hold modules of the plan.
With no mounts known, every piece falls back to today's project-directory
behaviour.

**Tech Stack:** FastAPI, Jinja, Alpine, Peewee/SQLite (`_ensure_columns`
adds columns), Docker SDK (`container.attrs["Mounts"]`), pytest.

**Spec:** `docs_/superpowers/specs/2026-09-30-addons-from-mounts-design.md`
— read it before Task 1. Where this plan and the spec differ, the spec wins,
except for the override listed below.

**Branch:** `native-runner`.

### Overrides vs the written spec

- The spec says the fake compose engine's `compose_up` mounts the project
  directory at `/opt/project`. It does not: the fake records mounts only when
  a test passes them to `add_container`. Adding a mount to every `compose_up`
  would switch dozens of existing tests from the fallback to mount placement
  and change paths they assert on. The fallback is the behaviour those tests
  pin; mount placement gets its own tests.

## Global Constraints

- Code, comments, docs and commits in English.
- `return` gets a blank line before it. Run `uv run ruff check`, never `ruff format`.
- Every operation inside its own `database.connection_context()`.
- The suite passes with no Docker, no Odoo and no daemon: `uv run pytest -q`.
- Inline `style=` only for custom properties. Every new component gets a row on `/components`.
- A path that escapes an allowed root is refused, not clamped.
- An empty mounts list means "not known", never "no addons".
- Supported versions are exactly the `rules/<N>.0.md` files (today 16.0–20.0).
- One task, its tests, one commit, then stop for the human's review (AGENTS.md).
- Test snippets that say "append" add functions at the end of the file; their `import` lines go to the top of the file with the others (ruff E402).

## Review Focus

1. A container path with `..` (`/opt/project/../../etc/passwd`) — refused, never read. Test in Task 7.
2. `/opt/project2/x` against a mount of `/opt/project` — not covered (string-prefix trap). Test in Task 2.
3. A mount whose host is a file (`/opt/conf/odoo.conf`) or holds no plan module — never an allowed root. Test in Task 7.
4. Docker unreachable at Build graph — graph still builds, paths fall back, nothing marked `path unknown`. Test in Task 4.
5. Re-running Build graph after the compose file changes — the snapshot and every module's paths are replaced, `path_problem` is cleared where it no longer applies. Test in Task 5.

---

### Task 1: Version selects

**Files:**
- Modify: `odoo_migrator/rules.py` (`check_versions`, new `supported_versions`)
- Modify: `odoo_migrator/api.py` (`_overview` context)
- Modify: `odoo_migrator/web/templates/overview.html:148-149`
- Modify: `odoo_migrator/web/static/style.css` (`.version-pair`)
- Modify: `odoo_migrator/web/templates/components.html` (version pair row)
- Test: `tests/test_migrations.py`

**Interfaces:**
- Produces: `supported_versions(rules_dir: Path | None = None) -> list[str]` in `rules.py`, e.g. `["16.0", "17.0", "18.0", "19.0", "20.0"]`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_migrations.py`)

```python
from odoo_migrator.rules import supported_versions


def test_supported_versions_are_the_rules_files():
    assert supported_versions() == ["16.0", "17.0", "18.0", "19.0", "20.0"]


def test_the_create_form_offers_versions_as_selects(client):
    html = client.get("/?create=1").text

    assert '<select class="mono" name="source_version"' in html
    assert '<select class="mono" name="target_version"' in html
    assert 'name="source_version" value=' not in html
    for version in supported_versions():
        assert f'value="{version}"' in html


def test_a_version_without_rules_is_refused_at_creation(client):
    response = client.post(
        "/migrations",
        data={
            "name": "Acme",
            "accent": "violet",
            "source_version": "17.0",
            "target_version": "21.0",
            "old_project_path": "/tmp/old",
            "new_project_path": "/tmp/new",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    assert "21.0 is not supported" in response.text


def test_the_widest_supported_jump_is_accepted(client):
    response = client.post(
        "/migrations",
        data={
            "name": "Wide",
            "accent": "violet",
            "source_version": "16.0",
            "target_version": "20.0",
            "old_project_path": "/tmp/wide-old",
            "new_project_path": "/tmp/wide-new",
        },
        follow_redirects=True,
    )

    assert response.status_code == 200
    with database.connection_context():
        assert Migration.get(Migration.name == "Wide").target_version == "20.0"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -q tests/test_migrations.py -k "supported or selects or without_rules or widest"`
Expected: FAIL — `ImportError: cannot import name 'supported_versions'`.

- [ ] **Step 3: Implement `supported_versions` and extend `check_versions`** in `rules.py`

```python
def supported_versions(rules_dir: Path | None = None) -> list[str]:
    """The versions a migration can start or end on: the ones with a rules file.

    Read through the same layers `assemble_rules` reads, so a version the
    create form offers is one whose stack will assemble.
    """
    majors: set[int] = set()
    for directory in _layers(rules_dir):
        if not directory.is_dir():
            continue
        for path in directory.glob("*.0.md"):
            major = path.name.split(".", 1)[0]
            if major.isdigit():
                majors.add(int(major))

    return [f"{major}.0" for major in sorted(majors)]


def check_versions(source: str, target: str) -> None:
    """Raise if this pair can never assemble. Called where a human types it.

    Refusing at assembly time only is too late: nothing would object until a
    job was built for the first module, long after the migration was created.
    The shape is checked first, so `17.2` still gets the message that says
    why a minor is refused.
    """
    _versions(source, target)
    known = supported_versions()
    for version in (source, target):
        if known and version not in known:
            raise RulesError(
                f"{version} is not supported. Choose one of: {', '.join(known)}."
            )
```

- [ ] **Step 4: Pass the versions to the page** — in `api.py` `_overview`, add to the context dict, beside `"typed"`:

```python
                "versions": supported_versions(),
```

and import it: `from odoo_migrator.rules import RulesError, stack_versions, supported_versions`.

- [ ] **Step 5: Replace the two inputs** in `overview.html` (lines 148–149)

```html
    {# Selects, not text: "17" and "17.0" are the same version to a person
       and not to the rules stack. Target offers only what is newer than the
       source; the server refuses the rest anyway. #}
    {% set chosen_source = typed.source_version or "17.0" %}
    {% set chosen_target = typed.target_version or "18.0" %}
    <div class="version-pair"
         x-data="{ source: '', target: '' }"
         x-init="source = $refs.source.value; target = $refs.target.value">
      <label>Source version
        <select class="mono" name="source_version" x-ref="source" x-model="source"
                @change="if (parseInt(target, 10) <= parseInt(source, 10)) { const next = Array.from($refs.target.options).find((o) => parseInt(o.value, 10) > parseInt(source, 10)); if (next) { target = next.value; } }"
                required>
          {% for version in versions[:-1] %}
          <option value="{{ version }}"{% if version == chosen_source %} selected{% endif %}>{{ version }}</option>
          {% endfor %}
        </select>
      </label>
      <label>Target version
        <select class="mono" name="target_version" x-ref="target" x-model="target" required>
          {% for version in versions[1:] %}
          <option value="{{ version }}"{% if version == chosen_target %} selected{% endif %}
                  :disabled="parseInt('{{ version }}', 10) <= parseInt(source, 10)"
                  :hidden="parseInt('{{ version }}', 10) <= parseInt(source, 10)">{{ version }}</option>
          {% endfor %}
        </select>
      </label>
    </div>
```

- [ ] **Step 6: Style** — append to `style.css` after the `.create-migration input` rule

```css
.version-pair {
  display: grid;
  grid-template-columns: repeat(2, minmax(0, 1fr));
  gap: 10px;
}

.create-migration select {
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 4px 8px;
  background: var(--panel-raised);
}
```

- [ ] **Step 7: `/components` row** — add before `<h2>Confirm dialog</h2>` in `components.html`

```html
  <h2>Version pair</h2>
  <form class="create-migration">
    <div class="version-pair">
      <label>Source version
        <select class="mono"><option>17.0</option></select>
      </label>
      <label>Target version
        <select class="mono"><option>18.0</option></select>
      </label>
    </div>
  </form>
  <p>Versions are chosen, never typed. Target lists only what is newer than
  the source.</p>

```

- [ ] **Step 8: Run the suite**

Run: `uv run pytest -q && uv run ruff check`
Expected: all pass, including `test_an_impossible_version_pair_is_refused_at_creation` (its three messages are unchanged).

- [ ] **Step 9: Commit**

```bash
git add odoo_migrator/rules.py odoo_migrator/api.py odoo_migrator/web/templates/overview.html odoo_migrator/web/static/style.css odoo_migrator/web/templates/components.html tests/test_migrations.py
git commit -m "feat: versions are chosen from the rules files, not typed"
```

---

### Task 2: `mounts.py`, the one translation

**Files:**
- Create: `odoo_migrator/mounts.py`
- Test: `tests/test_mounts.py`

**Interfaces:**
- Produces, all pure (no filesystem, no Docker):
  - `load(raw: str | None) -> list[dict]` — JSON text to mounts; bad input is `[]`.
  - `covering(mounts: list[dict], container_path: str) -> dict | None`
  - `to_host(mounts: list[dict], container_path: str) -> Path | None`
  - `to_container(mounts: list[dict], host_path: str) -> str | None`
  - `too_broad(host: str, *, home: Path | None = None, data_dir: Path | None = None) -> str | None`
- A mount is `{"host": str, "container": str, "read_only": bool}`.

- [ ] **Step 1: Write the failing tests** — `tests/test_mounts.py`

```python
"""Container path to host path and back, from bind mounts. Pure: no Docker."""

from pathlib import Path

from odoo_migrator.mounts import covering, load, to_container, to_host, too_broad

MOUNTS = [
    {"host": "/opt/sources/project", "container": "/opt/project", "read_only": False},
    {"host": "/opt/sources/external", "container": "/opt/external-addons", "read_only": True},
    {"host": "/opt/sources/queue", "container": "/opt/external-addons/integration-queue", "read_only": True},
]


def test_a_container_path_maps_to_its_host_file():
    assert to_host(MOUNTS, "/opt/project/sale_x/models/sale.py") == Path(
        "/opt/sources/project/sale_x/models/sale.py"
    )


def test_the_longest_mount_wins():
    assert to_host(MOUNTS, "/opt/external-addons/integration-queue/q/__init__.py") == Path(
        "/opt/sources/queue/q/__init__.py"
    )


def test_a_common_string_prefix_is_not_a_parent():
    assert covering(MOUNTS, "/opt/project2/sale_x") is None
    assert to_host(MOUNTS, "/opt/project2/sale_x") is None


def test_the_mount_point_itself_is_covered():
    assert to_host(MOUNTS, "/opt/project") == Path("/opt/sources/project")


def test_a_path_under_no_mount_is_none():
    assert to_host(MOUNTS, "/usr/lib/python3/dist-packages/odoo/models.py") is None


def test_a_host_path_maps_back_to_the_container():
    assert to_container(MOUNTS, "/opt/sources/external/stock_x") == "/opt/external-addons/stock_x"
    assert to_container(MOUNTS, "/elsewhere/stock_x") is None


def test_load_ignores_garbage():
    assert load(None) == []
    assert load("not json") == []
    assert load('{"host": "/a"}') == []
    assert load('[{"host": "/a"}]') == []
    assert load('[{"host": "/a", "container": "/b", "read_only": false}]') == [
        {"host": "/a", "container": "/b", "read_only": False}
    ]


def test_too_broad_refuses_the_disk_home_and_data_dir_parents():
    home = Path("/Users/someone")
    data = home / ".odoo-migrator"

    assert too_broad("/", home=home, data_dir=data)
    assert too_broad("/Users", home=home, data_dir=data)
    assert too_broad("/Users/someone", home=home, data_dir=data)
    assert too_broad("/Users/someone/.odoo-migrator", home=home, data_dir=data)
    assert too_broad("/Users/someone/work/addons", home=home, data_dir=data) is None
    assert too_broad("/opt/sources/project", home=home, data_dir=data) is None
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -q tests/test_mounts.py`
Expected: FAIL — `ModuleNotFoundError: No module named 'odoo_migrator.mounts'`.

- [ ] **Step 3: Implement** — `odoo_migrator/mounts.py`

```python
"""Where a container's paths are on this machine, from its bind mounts.

The only translation between a path inside a container and one on the host.
Build graph, the file tools and the prompt all go through here, so a path
from a traceback and a path on disk never disagree about which file they
mean. Pure: nothing here touches the filesystem or Docker.
"""

from __future__ import annotations

import json
import os
from pathlib import Path, PurePosixPath


def load(raw: str | None) -> list[dict]:
    """Mounts as stored on a migration. Anything malformed is no mount."""
    try:
        value = json.loads(raw or "[]")
    except ValueError:

        return []
    if not isinstance(value, list):

        return []

    return [
        {
            "host": str(entry["host"]),
            "container": str(entry["container"]),
            "read_only": bool(entry.get("read_only", False)),
        }
        for entry in value
        if isinstance(entry, dict) and entry.get("host") and entry.get("container")
    ]


def _under(path: PurePosixPath, root: PurePosixPath) -> bool:
    # Parts, not strings: /opt/project2 is not under /opt/project.

    return path == root or root in path.parents


def covering(mounts: list[dict], container_path: str) -> dict | None:
    """The mount a container path is under; the deepest one when nested."""
    path = PurePosixPath(container_path)
    best = None
    for mount in mounts:
        root = PurePosixPath(mount["container"])
        if not _under(path, root):
            continue
        if best is None or len(root.parts) > len(PurePosixPath(best["container"]).parts):
            best = mount

    return best


def to_host(mounts: list[dict], container_path: str) -> Path | None:
    mount = covering(mounts, container_path)
    if mount is None:

        return None
    relative = PurePosixPath(container_path).relative_to(mount["container"])

    return Path(mount["host"]) / relative


def to_container(mounts: list[dict], host_path: str) -> str | None:
    path = Path(host_path)
    best = None
    for mount in mounts:
        root = Path(mount["host"])
        if not (path == root or root in path.parents):
            continue
        if best is None or len(root.parts) > len(Path(best["host"]).parts):
            best = mount
    if best is None:

        return None
    relative = path.relative_to(best["host"]).as_posix()

    return str(PurePosixPath(best["container"]) / relative)


def too_broad(
    host: str, *, home: Path | None = None, data_dir: Path | None = None
) -> str | None:
    """Why a mount's host directory must not become a root the agent can read.

    One line in someone else's compose file — `- /:/mnt` or `- ~:/src` —
    would otherwise hand the model the whole disk or this app's own data.
    """
    from odoo_migrator.paths import user_dir

    home = home or Path.home()
    data_dir = data_dir or user_dir()
    path = Path(os.path.normpath(host))
    if path == Path("/"):

        return "it mounts the whole disk"
    if path == home or path in home.parents:

        return "it mounts the home directory"
    if path == data_dir or path in data_dir.parents:

        return "it holds this app's own data"

    return None
```

- [ ] **Step 4: Run to verify they pass**

Run: `uv run pytest -q tests/test_mounts.py && uv run ruff check`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add odoo_migrator/mounts.py tests/test_mounts.py
git commit -m "feat: one translation between container and host paths"
```

---

### Task 3: The compose engine reports bind mounts

**Files:**
- Modify: `odoo_migrator/provision/engine.py` (`RunningContainer`, `FakeComposeEngine.add_container`, `DockerComposeEngine.list_containers`, new `_bind_mounts`)
- Modify: `odoo_migrator/mounts.py` (new `container_mounts`)
- Test: `tests/test_mounts.py`

**Interfaces:**
- Consumes: nothing from earlier tasks beyond the mount dict shape.
- Produces:
  - `RunningContainer.mounts: list[dict]` (default `[]`).
  - `FakeComposeEngine.add_container(..., mounts: list[dict] | None = None)`.
  - `container_mounts(engine, name: str) -> list[dict] | None` in `mounts.py` — `None` when the Engine is unreachable or no container has that name.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_mounts.py`)

```python
from types import SimpleNamespace

from odoo_migrator.mounts import container_mounts
from odoo_migrator.provision import FakeComposeEngine
from odoo_migrator.provision.engine import _bind_mounts
from odoo_migrator.provision.errors import DockerUnavailable


def test_only_bind_mounts_are_reported():
    container = SimpleNamespace(
        attrs={
            "Mounts": [
                {"Type": "bind", "Source": "/opt/sources/project", "Destination": "/opt/project", "RW": True},
                {"Type": "volume", "Source": "/var/lib/docker/volumes/x", "Destination": "/var/lib/odoo", "RW": True},
                {"Type": "bind", "Source": "/opt/conf", "Destination": "/etc/odoo", "RW": False},
            ]
        }
    )

    assert _bind_mounts(container) == [
        {"host": "/opt/sources/project", "container": "/opt/project", "read_only": False},
        {"host": "/opt/conf", "container": "/etc/odoo", "read_only": True},
    ]


def test_container_mounts_reads_the_named_container():
    engine = FakeComposeEngine()
    engine.add_container(
        name="odoo-old", image="odoo:17", status="running", labels={}, host_ports=[],
        mounts=[{"host": "/opt/sources/project", "container": "/opt/project", "read_only": False}],
    )

    assert container_mounts(engine, "odoo-old") == [
        {"host": "/opt/sources/project", "container": "/opt/project", "read_only": False}
    ]
    assert container_mounts(engine, "missing") is None


def test_an_unreachable_engine_is_not_known_rather_than_empty():
    class Down:
        def list_containers(self):
            raise DockerUnavailable("Docker Engine is not reachable: no socket")

    assert container_mounts(Down(), "odoo-old") is None
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -q tests/test_mounts.py`
Expected: FAIL — `ImportError: cannot import name 'container_mounts'`.

- [ ] **Step 3: Implement in `engine.py`**

Add the field to the dataclass:

```python
@dataclass
class RunningContainer:
    name: str
    image: str
    status: str
    labels: dict
    host_ports: list[int] = field(default_factory=list)
    # Bind mounts only, as {"host", "container", "read_only"}. Volumes have
    # no directory on this machine a person can open.
    mounts: list[dict] = field(default_factory=list)
```

`FakeComposeEngine.add_container` gains `mounts: list[dict] | None = None` and passes `mounts=list(mounts or [])`. `compose_up` is unchanged (see Overrides).

In `DockerComposeEngine.list_containers`, pass `mounts=_bind_mounts(container)`, and add at module level:

```python
def _bind_mounts(container) -> list[dict]:
    found = []
    for entry in container.attrs.get("Mounts") or []:
        if entry.get("Type") != "bind":
            continue
        host = entry.get("Source") or ""
        inside = entry.get("Destination") or ""
        if host and inside:
            found.append(
                {"host": host, "container": inside, "read_only": not entry.get("RW", True)}
            )

    return found
```

- [ ] **Step 4: Implement `container_mounts` in `mounts.py`**

```python
def container_mounts(engine, name: str) -> list[dict] | None:
    """The bind mounts of container `name`, or None when that is not known."""
    from odoo_migrator.provision.errors import DockerUnavailable

    try:
        containers = engine.list_containers()
    except DockerUnavailable:

        return None
    for container in containers:
        if container.name == name:

            return [dict(mount) for mount in container.mounts]

    return None
```

- [ ] **Step 5: Run and commit**

Run: `uv run pytest -q && uv run ruff check` — expected PASS.

```bash
git add odoo_migrator/provision/engine.py odoo_migrator/mounts.py tests/test_mounts.py
git commit -m "feat: the compose engine reports each container's bind mounts"
```

---

### Task 4: Snapshot mounts at Build graph; the graph reads container roots

**Files:**
- Modify: `odoo_migrator/models.py` (`Migration.old_mounts`, `Migration.new_mounts`; `Module.old_container_path`, `Module.new_container_path`, `Module.path_problem`)
- Modify: `odoo_migrator/graph.py` (`snapshot_mounts`, `build_graph_snippet`, `resolve_graph`, `_module_row`)
- Modify: `odoo_migrator/api.py` (`tree_resolve`)
- Test: `tests/test_graph_mounts.py` (new)

**Interfaces:**
- Consumes: `container_mounts` (Task 3), `load` (Task 2).
- Produces:
  - `snapshot_mounts(engine, migration_id: str) -> dict[str, list[dict]]` in `graph.py` — stores and returns `{"old": [...], "new": [...]}`.
  - `build_graph_snippet(project_path: str, container_roots: list[str] | None = None) -> str`.
  - `_module_row` gains `"path_problem"`.

- [ ] **Step 1: Model columns** in `models.py`

On `Migration`, after `new_database`:

```python
    # Bind mounts of each side's container at the last Build graph, as JSON
    # [{"host", "container", "read_only"}]. "[]" is "not known": Docker was
    # not reachable or the side had no container. See mounts.py.
    old_mounts = TextField(default="[]")
    new_mounts = TextField(default="[]")
```

On `Module`, after `new_path`:

```python
    # The module's directory inside each container, as Odoo and a traceback
    # name it. Empty when that side's mounts are not known.
    old_container_path = TextField(default="")
    new_container_path = TextField(default="")
    # Why the app does not know where this module is on disk: "not mounted",
    # "mount too broad" or "no target directory". A module with one does not
    # start.
    path_problem = CharField(default="")
```

- [ ] **Step 2: Write the failing tests** — `tests/test_graph_mounts.py`

```python
"""Build graph snapshots both containers' bind mounts, and reads the graph
from the container directories they name."""

import json

from odoo_migrator.db import database, init_db
from odoo_migrator.graph import build_graph_snippet, resolve_graph, snapshot_mounts
from odoo_migrator.migrations import create_migration, save_side_container
from odoo_migrator.models import Migration
from odoo_migrator.provision import FakeComposeEngine
from odoo_migrator.provision.errors import DockerUnavailable
from odoo_migrator.sheller import FakeShellerClient

OLD_MOUNTS = [
    {"host": "/opt/sources/project", "container": "/opt/project", "read_only": False},
    {"host": "/opt/sources/external", "container": "/opt/external-addons", "read_only": True},
]
NEW_MOUNTS = [
    {"host": "/opt/sources/project-18", "container": "/opt/project", "read_only": False},
]


def _migration(tmp_path):
    init_db(tmp_path / "mounts.sqlite")
    created = create_migration(
        name="Acme",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path="/tmp/old",
        new_project_path="/tmp/new",
    )
    save_side_container(created["id"], "old", "odoo-old")
    save_side_container(created["id"], "new", "odoo-new")

    return created


def _engine():
    engine = FakeComposeEngine()
    engine.add_container(name="odoo-old", image="odoo:17", status="running",
                         labels={}, host_ports=[], mounts=OLD_MOUNTS)
    engine.add_container(name="odoo-new", image="odoo:18", status="running",
                         labels={}, host_ports=[], mounts=NEW_MOUNTS)

    return engine


def test_build_graph_stores_both_sides_mounts(tmp_path):
    created = _migration(tmp_path)

    snapshot = snapshot_mounts(_engine(), created["id"])

    assert snapshot == {"old": OLD_MOUNTS, "new": NEW_MOUNTS}
    with database.connection_context():
        row = Migration.get_by_id(created["id"])
        assert json.loads(row.old_mounts) == OLD_MOUNTS
        assert json.loads(row.new_mounts) == NEW_MOUNTS


def test_an_unreachable_engine_stores_not_known(tmp_path):
    created = _migration(tmp_path)

    class Down:
        def list_containers(self):
            raise DockerUnavailable("Docker Engine is not reachable")

    assert snapshot_mounts(Down(), created["id"]) == {"old": [], "new": []}
    # And the graph still builds, from the project directory as before.
    rows = resolve_graph(FakeShellerClient(), created["id"])
    assert {row["technical_name"] for row in rows} >= {"sale_custom", "stock_custom"}
    assert all(row["path_problem"] == "" for row in rows)


def test_the_graph_snippet_reads_container_roots_when_mounts_are_known(tmp_path):
    created = _migration(tmp_path)
    snapshot_mounts(_engine(), created["id"])
    fake = FakeShellerClient()

    resolve_graph(fake, created["id"])

    snippet = fake.execs[-1]
    assert '"/opt/project"' in snippet
    assert '"/opt/external-addons"' in snippet
    assert "/tmp/old" not in snippet


def test_the_snippet_drops_a_root_that_overlaps_the_odoo_install():
    snippet = build_graph_snippet("/tmp/old", container_roots=["/opt/odoo", "/opt/project"])

    assert "install_root" in snippet
    assert "_overlaps_install" in snippet
```

- [ ] **Step 3: Run to verify they fail**

Run: `uv run pytest -q tests/test_graph_mounts.py`
Expected: FAIL — `ImportError: cannot import name 'snapshot_mounts'`.

- [ ] **Step 4: Implement `snapshot_mounts`** in `graph.py`

```python
def snapshot_mounts(engine, migration_id: str) -> dict[str, list[dict]]:
    """Record where each side's container mounts its directories from.

    Taken at Build graph and nowhere else: a run reads this snapshot and
    does not call Docker for paths. A side with no container, or an Engine
    that does not answer, is stored as "[]" — not known — and that side
    keeps today's project-directory paths.
    """
    from odoo_migrator.mounts import container_mounts

    with database.connection_context():
        migration = Migration.get_by_id(migration_id)
        containers = {"old": migration.old_container, "new": migration.new_container}
    snapshot = {}
    for side, name in containers.items():
        found = container_mounts(engine, name) if name else None
        snapshot[side] = found or []
    with database.connection_context():
        Migration.update(
            old_mounts=json.dumps(snapshot["old"]),
            new_mounts=json.dumps(snapshot["new"]),
        ).where(Migration.id == migration_id).execute()

    return snapshot
```

- [ ] **Step 5: Snippet takes container roots** — change `build_graph_snippet`

```python
def build_graph_snippet(project_path: str, container_roots: list[str] | None = None) -> str:
    """Odoo code that returns the module graph as a JSON string.

    Installed modules are always included. Uninstalled rows are kept only
    when their path sits under the project's own addons directories. Those
    are the container directories of the old side's bind mounts when they
    are known — compared against paths inside the container, where they
    mean something — and the host project roots otherwise. A root that
    contains, or is inside, Odoo's own tree is dropped: keeping it would
    list every uninstalled core module.
    """
    roots = container_roots if container_roots else project_addons_roots(project_path)
    roots_literal = json.dumps(roots)
```

and inside the returned code, replace `project_roots = {roots_literal}` with:

```python
def _overlaps_install(root):
    if not install_root:
        return False
    return (
        root == install_root
        or root.startswith(install_root + os.sep)
        or install_root.startswith(root + os.sep)
    )

project_roots = [root for root in {roots_literal} if not _overlaps_install(root)]
```

- [ ] **Step 6: `resolve_graph` passes the roots** — read the stored mounts with the migration and pass them:

```python
        old_mounts = load_mounts(migration.old_mounts)
```

(import `from odoo_migrator.mounts import load as load_mounts` at the top of `graph.py`), and

```python
            code=build_graph_snippet(
                old_project_path,
                container_roots=[mount["container"] for mount in old_mounts],
            ),
```

- [ ] **Step 7: `_module_row` gains the problem** — add `"path_problem": module.path_problem or "",` beside `"needs_review"`.

- [ ] **Step 8: Build graph snapshots first** — in `api.py` `tree_resolve`, after the old container is linked and before `resolve_graph`:

```python
        # Where each container mounts its addons from. Read now, once; a run
        # uses this snapshot. A changed compose file needs Build graph again.
        snapshot_mounts(request.app.state.compose, current["id"])
```

and add `snapshot_mounts` to the `from odoo_migrator.graph import (...)` list.

- [ ] **Step 9: Run and commit**

Run: `uv run pytest -q && uv run ruff check` — expected PASS (existing graph tests use no mounts and keep the host roots).

```bash
git add odoo_migrator/models.py odoo_migrator/graph.py odoo_migrator/api.py tests/test_graph_mounts.py
git commit -m "feat: Build graph records each side's bind mounts and reads the graph from them"
```

---

### Task 5: Place every module from the mounts

**Files:**
- Modify: `odoo_migrator/module_paths.py` (new `place`, `_new_container_path`; `module_root` honours `path_problem`)
- Modify: `odoo_migrator/graph.py` (`persist_graph` uses `place`)
- Test: `tests/test_graph_mounts.py`

**Interfaces:**
- Consumes: `load`, `covering`, `to_host`, `to_container`, `too_broad` (Task 2); `Migration.*_mounts`, `Module.*_container_path`, `Module.path_problem` (Task 4).
- Produces: `place(migration, name: str, container_path: str) -> dict` with keys `old_path`, `new_path`, `old_container_path`, `new_container_path`, `path_problem`.

- [ ] **Step 1: Write the failing tests** (append to `tests/test_graph_mounts.py`)

```python
from odoo_migrator.models import Module
from odoo_migrator.sheller.fake import PROJECT


def _graph_with(fake, *extra):
    """The default graph plus modules at the given container directories."""
    for name, root in extra:
        fake.graph_modules.append(
            {"name": name, "depends": ["sale"], "state": "installed",
             "addons_root": root, "path": f"{root}/{name}",
             "author": "Acme", "license": "OPL-1", "is_core": False}
        )

    return fake


def _module(name):
    with database.connection_context():

        return Module.get(Module.technical_name == name)


def _placed(tmp_path, engine, *extra):
    created = _migration(tmp_path)
    snapshot_mounts(engine, created["id"])
    resolve_graph(_graph_with(FakeShellerClient(), *extra), created["id"])

    return created


def test_two_source_directories_land_in_their_matching_targets(tmp_path):
    engine = FakeComposeEngine()
    engine.add_container(name="odoo-old", image="odoo:17", status="running", labels={}, host_ports=[],
                         mounts=OLD_MOUNTS)
    engine.add_container(name="odoo-new", image="odoo:18", status="running", labels={}, host_ports=[],
                         mounts=NEW_MOUNTS + [{"host": "/opt/sources/external-18",
                                               "container": "/opt/external-addons", "read_only": False}])
    _placed(tmp_path, engine, ("queue_x", "/opt/external-addons"))

    project = _module("sale_custom")
    assert project.old_container_path == f"{PROJECT}/sale_custom"
    assert project.old_path == "/opt/sources/project/sale_custom"
    assert project.new_container_path == "/opt/project/sale_custom"
    assert project.new_path == "/opt/sources/project-18/sale_custom"
    external = _module("queue_x")
    assert external.old_path == "/opt/sources/external/queue_x"
    assert external.new_container_path == "/opt/external-addons/queue_x"
    assert external.new_path == "/opt/sources/external-18/queue_x"
    assert external.path_problem == ""


def test_a_directory_the_target_lacks_goes_under_the_new_project(tmp_path):
    new_mounts = [{"host": "/tmp/new", "container": "/mnt/new-project", "read_only": False}]
    engine = FakeComposeEngine()
    engine.add_container(name="odoo-old", image="odoo:17", status="running", labels={}, host_ports=[],
                         mounts=OLD_MOUNTS)
    engine.add_container(name="odoo-new", image="odoo:18", status="running", labels={}, host_ports=[],
                         mounts=new_mounts)
    _placed(tmp_path, engine, ("queue_x", "/opt/external-addons"))

    external = _module("queue_x")
    assert external.new_container_path == "/mnt/new-project/queue_x"
    assert external.new_path == "/tmp/new/queue_x"


def test_a_module_under_no_mount_is_not_mounted(tmp_path):
    _placed(tmp_path, _engine(), ("baked_x", "/opt/odoo-sh/connectors-extensions"))

    baked = _module("baked_x")
    assert baked.path_problem == "not mounted"
    assert baked.old_path == ""


def test_a_mount_of_the_home_directory_is_too_broad(tmp_path):
    from pathlib import Path

    old_mounts = OLD_MOUNTS + [{"host": str(Path.home()), "container": "/mnt/home", "read_only": True}]
    engine = FakeComposeEngine()
    engine.add_container(name="odoo-old", image="odoo:17", status="running", labels={}, host_ports=[],
                         mounts=old_mounts)
    engine.add_container(name="odoo-new", image="odoo:18", status="running", labels={}, host_ports=[],
                         mounts=NEW_MOUNTS)
    _placed(tmp_path, engine, ("home_x", "/mnt/home"))

    assert _module("home_x").path_problem == "mount too broad"


def test_no_target_mount_for_the_module_or_project_is_no_target_directory(tmp_path):
    engine = FakeComposeEngine()
    engine.add_container(name="odoo-old", image="odoo:17", status="running", labels={}, host_ports=[],
                         mounts=OLD_MOUNTS)
    engine.add_container(name="odoo-new", image="odoo:18", status="running", labels={}, host_ports=[],
                         mounts=[{"host": "/opt/other", "container": "/opt/other", "read_only": False}])
    _placed(tmp_path, engine)

    assert _module("sale_custom").path_problem == "no target directory"


def test_a_second_build_graph_replaces_the_placement(tmp_path):
    created = _placed(tmp_path, _engine(), ("baked_x", "/opt/odoo-sh/connectors-extensions"))
    assert _module("baked_x").path_problem == "not mounted"
    fixed = FakeComposeEngine()
    fixed.add_container(name="odoo-old", image="odoo:17", status="running", labels={}, host_ports=[],
                        mounts=OLD_MOUNTS + [{"host": "/opt/sources/connectors",
                                              "container": "/opt/odoo-sh/connectors-extensions",
                                              "read_only": True}])
    fixed.add_container(name="odoo-new", image="odoo:18", status="running", labels={}, host_ports=[],
                        mounts=NEW_MOUNTS + [{"host": "/opt/sources/connectors-18",
                                              "container": "/opt/odoo-sh/connectors-extensions",
                                              "read_only": False}])

    snapshot_mounts(fixed, created["id"])
    resolve_graph(_graph_with(FakeShellerClient(), ("baked_x", "/opt/odoo-sh/connectors-extensions")),
                  created["id"])

    baked = _module("baked_x")
    assert baked.path_problem == ""
    assert baked.new_path == "/opt/sources/connectors-18/baked_x"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -q tests/test_graph_mounts.py`
Expected: FAIL — `old_container_path` is `""` (nothing places modules yet).

- [ ] **Step 3: Implement `place`** in `module_paths.py`

```python
from pathlib import PurePosixPath

from odoo_migrator.mounts import covering, load, to_container, to_host, too_broad


def place(migration, name: str, container_path: str) -> dict:
    """Where module `name` is on each side, on the host and in the container.

    A side whose mounts are not known answers as `host_paths` does, and
    raises no problem. A side whose mounts are known and do not place the
    module sets `path_problem`, and that side's host path is left empty:
    a guessed directory is where a port would be written and never loaded.
    """
    old_mounts = load(migration.old_mounts)
    new_mounts = load(migration.new_mounts)
    fallback_old, fallback_new = host_paths(migration, name)
    placed = {
        "old_path": fallback_old,
        "new_path": fallback_new,
        "old_container_path": "",
        "new_container_path": "",
        "path_problem": "",
    }
    old_mount = covering(old_mounts, container_path) if old_mounts and container_path else None
    if old_mounts:
        placed["old_container_path"] = container_path
        if old_mount is None:
            placed["old_path"] = ""
            placed["path_problem"] = "not mounted"

            return placed
        if too_broad(old_mount["host"]):
            placed["old_path"] = ""
            placed["path_problem"] = "mount too broad"

            return placed
        placed["old_path"] = str(to_host(old_mounts, container_path))
    if new_mounts:
        target = _new_container_path(migration, new_mounts, old_mount, container_path, name)
        new_mount = covering(new_mounts, target) if target else None
        if new_mount is None:
            placed["new_path"] = ""
            placed["path_problem"] = "no target directory"

            return placed
        if too_broad(new_mount["host"]):
            placed["new_path"] = ""
            placed["path_problem"] = "mount too broad"

            return placed
        placed["new_container_path"] = target
        placed["new_path"] = str(to_host(new_mounts, target))

    return placed


def _new_container_path(migration, new_mounts, old_mount, container_path, name) -> str:
    """Where the port goes inside the target container.

    An existing port under the new project wins. Then the same container
    directory, when the target mounts it. Then the new project's own mount,
    at the module's place relative to its old mount.
    """
    project = (migration.new_project_path or "").strip()
    if project:
        existing = find_module_dir(project, name)
        if existing is not None:
            inside = to_container(new_mounts, str(existing))
            if inside:

                return inside
    if container_path and covering(new_mounts, container_path) is not None:

        return container_path
    if not project:

        return ""
    root = to_container(new_mounts, str(Path(project).expanduser()))
    if root is None:

        return ""
    if old_mount is not None:
        relative = PurePosixPath(container_path).relative_to(old_mount["container"])
    else:
        relative = PurePosixPath(name)

    return str(PurePosixPath(root) / relative)
```

In `module_root`, first line of the body:

```python
    if getattr(module, "path_problem", ""):

        return None
```

- [ ] **Step 4: `persist_graph` uses `place`** — replace the `host_paths` call and the path assignments. The row's container path is the graph's `path`, or its `addons_root` joined with the name (the suite's graph double has no `path`):

```python
            addons_root = row.get("addons_root") or ""
            container_path = row.get("path") or (f"{addons_root}/{name}" if addons_root else "")
            placed = place(migration, name, container_path)
```

In `Module.create(...)` replace `old_path=old_path, new_path=new_path,` with `**placed,`. In the update branch replace the two path lines with:

```python
                for field_name, value in placed.items():
                    setattr(stored, field_name, value)
```

Change the import at the top of `graph.py` from `from odoo_migrator.module_paths import host_paths` to `from odoo_migrator.module_paths import place`.

- [ ] **Step 5: Run and commit**

Run: `uv run pytest -q && uv run ruff check` — expected PASS.

```bash
git add odoo_migrator/module_paths.py odoo_migrator/graph.py tests/test_graph_mounts.py
git commit -m "feat: each module is placed from the mounts, on both sides"
```

---

### Task 6: A module without a known path does not start

**Files:**
- Modify: `odoo_migrator/graph.py` (new `path_problems_blocking`)
- Modify: `odoo_migrator/jobs.py` (`start_job`, `start_batch`)
- Test: `tests/test_graph_mounts.py`

**Interfaces:**
- Consumes: `Module.path_problem` (Task 4).
- Produces: `path_problems_blocking(migration_id: str, module_ids: list[int]) -> str | None`.

- [ ] **Step 1: Write the failing tests** (append)

```python
import pytest

from odoo_migrator.contract import ContractError
from odoo_migrator.events import EventHub
from odoo_migrator.executor import StubExecutor
from odoo_migrator.graph import path_problems_blocking
from odoo_migrator.jobs import start_batch, start_job


def test_a_step_with_an_unplaced_module_is_refused_whole(tmp_path):
    created = _placed(tmp_path, _engine(), ("baked_x", "/opt/odoo-sh/connectors-extensions"))
    baked = _module("baked_x")

    message = path_problems_blocking(created["id"], [baked.id, _module("sale_custom").id])

    assert "baked_x (not mounted)" in message
    assert "sale_custom" not in message
    with pytest.raises(ContractError, match="baked_x"):
        start_batch(migration_id=created["id"], executor=StubExecutor(), hub=EventHub(),
                    step=baked.batch_group)
    with pytest.raises(ContractError, match="baked_x"):
        start_job(migration_id=created["id"], module_id=baked.id,
                  executor=StubExecutor(), hub=EventHub())
    with database.connection_context():
        from odoo_migrator.models import Job
        assert Job.select().count() == 0


def test_an_excluded_unplaced_module_does_not_block(tmp_path):
    created = _placed(tmp_path, _engine(), ("baked_x", "/opt/odoo-sh/connectors-extensions"))
    with database.connection_context():
        Module.update(struck_out=True).where(Module.technical_name == "baked_x").execute()

    assert path_problems_blocking(created["id"], [_module("baked_x").id]) is None
```

If `StubExecutor()` needs arguments, build it the way `tests/test_batch.py` does; read that file's fixture first.

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -q tests/test_graph_mounts.py -k "unplaced"`
Expected: FAIL — `ImportError: cannot import name 'path_problems_blocking'`.

- [ ] **Step 3: Implement** in `graph.py`

```python
def path_problems_blocking(migration_id: str, module_ids: list[int]) -> str | None:
    """Modules the app cannot find on disk do not start. Nothing starts partly."""
    if not module_ids:

        return None
    with database.connection_context():
        rows = list(
            Module.select().where(
                (Module.migration == migration_id)
                & (Module.id.in_(module_ids))
                & (Module.struck_out == False)
                & (Module.path_problem != "")
            )
        )
    if not rows:

        return None
    listed = ", ".join(
        f"{row.technical_name} ({row.path_problem})"
        for row in sorted(rows, key=lambda row: row.technical_name)
    )

    return (
        f"The app does not know where these modules are on disk: {listed}. "
        "Mount their directories in the compose file and press Build graph "
        "again, or exclude them on Tree."
    )
```

- [ ] **Step 4: Call it** in `jobs.py` — import `path_problems_blocking` with the other graph names. In `start_job`, before `blocked = None`:

```python
    unplaced = path_problems_blocking(migration_id, [module_id])
    if unplaced:
        raise ContractError(unplaced)
```

In `start_batch`, right after `ordered = _still_to_run(listed)` and its empty check:

```python
    unplaced = path_problems_blocking(migration_id, [row["id"] for row in ordered])
    if unplaced:
        raise ContractError(unplaced)
```

- [ ] **Step 5: Run and commit**

Run: `uv run pytest -q && uv run ruff check` — expected PASS.

```bash
git add odoo_migrator/graph.py odoo_migrator/jobs.py tests/test_graph_mounts.py
git commit -m "feat: a module the app cannot find on disk does not start"
```

---

### Task 7: Tools take container paths; the prompt names them

**Files:**
- Modify: `odoo_migrator/module_paths.py` (new `allowed_roots`, `agent_path`)
- Modify: `odoo_migrator/runner/tools.py` (`_inside`, `_inside_module`)
- Modify: `odoo_migrator/runner/prompting.py:105-109`
- Modify: `odoo_migrator/runner/native.py` (clear-module guard, ~`:437-445`)
- Modify: `prompts/system.md:17-29` (not `prompts/sheller.md`: it is vendored and a test compares it with Sheller's)
- Modify: `rules/common.md` if it describes project-relative paths (grep first; change only such lines)
- Test: `tests/test_tool_paths.py` (new)

**Interfaces:**
- Consumes: Tasks 2, 4, 5.
- Produces:
  - `allowed_roots(migration, side: str) -> list[dict]` — mounts of that side that hold at least one in-plan module's container path and are not `too_broad`.
  - `agent_path(migration, module, side: str) -> str` — container path when the side's mounts are known, else `relative_to_root`.

- [ ] **Step 1: Write the failing tests** — `tests/test_tool_paths.py`. Build the migration with mounts whose `host` sides are real `tmp_path` directories, so reads and writes hit disk. Read `tests/test_tools.py` (or the file that tests `_read_file` today) and reuse its `ctx` construction.

```python
"""read_file and write_file take container paths when the mounts are known."""

import json

import pytest

from odoo_migrator.db import database, init_db
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Migration, Module
from odoo_migrator.module_paths import agent_path, allowed_roots
from odoo_migrator.runner.tools import ToolRefused, _read_file, _write_file


class Ctx:
    def __init__(self, migration, module):
        self.migration = migration
        self.module = module


@pytest.fixture
def ctx(tmp_path):
    init_db(tmp_path / "tools.sqlite")
    old_host = tmp_path / "old-src"
    ext_host = tmp_path / "old-ext"
    conf_host = tmp_path / "conf"
    new_host = tmp_path / "new-src"
    for directory in (old_host / "sale_x" / "models", ext_host / "queue_x", conf_host, new_host / "sale_x"):
        directory.mkdir(parents=True)
    (old_host / "sale_x" / "models" / "sale.py").write_text("OLD = 1\n")
    (conf_host / "odoo.conf").write_text("admin_passwd = secret\n")
    (tmp_path / "outside.txt").write_text("nope\n")
    created = create_migration(
        name="Acme", accent="violet", source_version="17.0", target_version="18.0",
        old_project_path=str(old_host), new_project_path=str(new_host),
    )
    old_mounts = [
        {"host": str(old_host), "container": "/opt/project", "read_only": False},
        {"host": str(ext_host), "container": "/opt/external-addons", "read_only": True},
        {"host": str(conf_host), "container": "/etc/odoo", "read_only": True},
    ]
    new_mounts = [{"host": str(new_host), "container": "/opt/project", "read_only": False}]
    with database.connection_context():
        Migration.update(old_mounts=json.dumps(old_mounts), new_mounts=json.dumps(new_mounts)).where(
            Migration.id == created["id"]
        ).execute()
        migration = Migration.get_by_id(created["id"])
        module = Module.create(
            migration=migration, technical_name="sale_x",
            old_path=str(old_host / "sale_x"), new_path=str(new_host / "sale_x"),
            old_container_path="/opt/project/sale_x", new_container_path="/opt/project/sale_x",
        )

    return Ctx(migration, module)


def test_a_traceback_path_reads_the_host_file(ctx):
    assert _read_file(ctx, "old", "/opt/project/sale_x/models/sale.py") == "OLD = 1\n"


def test_a_mount_with_no_plan_module_is_not_a_root(ctx):
    roots = [mount["container"] for mount in allowed_roots(ctx.migration, "old")]

    assert roots == ["/opt/project"]
    with pytest.raises(ToolRefused, match="source"):
        _read_file(ctx, "old", "/etc/odoo/odoo.conf")
    with pytest.raises(ToolRefused):
        _read_file(ctx, "old", "/opt/external-addons/queue_x/__init__.py")


def test_dot_dot_cannot_climb_out_of_a_root(ctx):
    with pytest.raises(ToolRefused):
        _read_file(ctx, "old", "/opt/project/../../outside.txt")
    with pytest.raises(ToolRefused):
        _read_file(ctx, "old", "/opt/project/sale_x/../../../outside.txt")


def test_a_write_lands_in_the_module_on_the_new_side(ctx, tmp_path):
    written = _write_file(ctx, "new", "/opt/project/sale_x/models/sale.py", "NEW = 1\n")

    assert (tmp_path / "new-src" / "sale_x" / "models" / "sale.py").read_text() == "NEW = 1\n"
    assert written.endswith("sale_x/models/sale.py")
    with pytest.raises(ToolRefused):
        _write_file(ctx, "new", "/opt/project/other_x/x.py", "")


def test_a_relative_path_keeps_working(ctx):
    assert _read_file(ctx, "old", "sale_x/models/sale.py") == "OLD = 1\n"


def test_the_prompt_names_the_container_path(ctx):
    assert agent_path(ctx.migration, ctx.module, "old") == "/opt/project/sale_x"
    assert agent_path(ctx.migration, ctx.module, "new") == "/opt/project/sale_x"


def test_without_mounts_the_prompt_keeps_the_relative_path(ctx):
    with database.connection_context():
        Migration.update(old_mounts="[]", new_mounts="[]").where(Migration.id == ctx.migration.id).execute()
        ctx.migration = Migration.get_by_id(ctx.migration.id)

    assert agent_path(ctx.migration, ctx.module, "old") == "sale_x"
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest -q tests/test_tool_paths.py`
Expected: FAIL — `ImportError: cannot import name 'agent_path'`.

- [ ] **Step 3: Implement `allowed_roots` and `agent_path`** in `module_paths.py`

```python
def _side_mounts(migration, side: str) -> list[dict]:

    return load(migration.old_mounts if side == "old" else migration.new_mounts)


def allowed_roots(migration, side: str) -> list[dict]:
    """The mounts the file tools may reach on `side`.

    Only the ones that hold a module of the plan. A mount of odoo.conf, the
    filestore or the logs is on the container too, and none of it is the
    agent's to read through a host tool.
    """
    from odoo_migrator.db import database
    from odoo_migrator.models import Module

    mounts = _side_mounts(migration, side)
    if not mounts:

        return []
    column = Module.old_container_path if side == "old" else Module.new_container_path
    with database.connection_context():
        paths = [
            value
            for (value,) in Module.select(column)
            .where(
                (Module.migration == migration.id)
                & (Module.struck_out == False)
                & (column != "")
            )
            .tuples()
        ]
    roots: list[dict] = []
    for path in paths:
        mount = covering(mounts, path)
        if mount is None or too_broad(mount["host"]) or mount in roots:
            continue
        roots.append(mount)

    return roots


def agent_path(migration, module, side: str) -> str:
    """The module's directory as the file tools take it on `side`.

    A container path when that side's mounts are known — the path Odoo and
    its tracebacks use — and the project-relative path otherwise.
    """
    stored = module.old_container_path if side == "old" else module.new_container_path
    if stored and _side_mounts(migration, side):

        return stored

    return relative_to_root(migration, module, side)
```

- [ ] **Step 4: The tools** — in `tools.py`, rename today's `_inside` body into `_inside_project` and route:

```python
def _inside(ctx, side: str, given: str) -> Path:
    """An absolute path is a container path; a relative one is under the
    project directory, as before mounts were known."""
    if given.startswith("/"):

        return _inside_mounts(ctx, side, given)

    return _inside_project(ctx, side, given)


def _inside_mounts(ctx, side: str, given: str) -> Path:
    """Translate a container path to the host, inside an allowed root, or refuse.

    Resolved before the check, so `..` and a symlink cannot climb out of the
    root they appear to be under.
    """
    from odoo_migrator.module_paths import allowed_roots
    from odoo_migrator.mounts import covering, to_host

    roots = allowed_roots(ctx.migration, side)
    if not roots:
        raise ToolRefused(
            f"no addons directories are known on the {side} side, so {given} "
            "cannot be read from disk. Read it with `source` from the container."
        )
    mount = covering(roots, given)
    if mount is None:
        listed = ", ".join(root["container"] for root in roots)
        raise ToolRefused(
            f"{given} is outside the addons directories of the {side} side "
            f"({listed}). Read it with `source` from the container."
        )
    root = Path(mount["host"]).expanduser().resolve()
    candidate = to_host([mount], given).resolve()
    if not candidate.is_relative_to(root):
        raise ToolRefused(f"path escapes its addons directory: {given}")

    return candidate
```

In `_inside_module`, the hint names the path the agent should use: replace `wanted = relative_to_root(ctx.migration, ctx.module, "new")` with `wanted = agent_path(ctx.migration, ctx.module, "new")` and import `agent_path` in its local import line.

- [ ] **Step 5: The prompt** — in `prompting.py`, import `agent_path` instead of `relative_to_root`, and change lines 108–109 to:

```python
        f"Its directory on the old side: {agent_path(ctx.migration, ctx.module, 'old')}\n"
        f"Its directory on the new side: {agent_path(ctx.migration, ctx.module, 'new')}\n"
```

- [ ] **Step 6: `prompts/system.md`** — replace lines 17–29 with:

```markdown
The module's source lives on the host and is mounted into each container.
`read_file` and `write_file` take the path **as the container names it** —
the directory given below under "This run", or a path from a traceback,
exactly as Odoo printed it. A path outside the module's addons directories
is refused. A path relative to the project root still works where no
container path is given.

**The module's own directory is given below, under "This run". Use it.** A
port written anywhere else lands outside what the new container loads: the
files exist, nothing refuses them, and Odoo never sees a line of them.

Odoo's own code and Enterprise are not on the host for you. Read them with
`source`, which reads inside the container. When `read_file` refuses a
path, `source` is how you read it.
```

Then `grep -n "relative" rules/common.md` and change only lines that tell the agent file paths are project-relative, to the same wording.

- [ ] **Step 7: Clearing a module on the new side** — in `native.py`, the guard around `:437` allows clearing only strictly inside the new project. Allow it strictly inside an allowed root of the new side as well:

```python
        from odoo_migrator.module_paths import allowed_roots

        roots = [Path(mount["host"]).expanduser().resolve() for mount in allowed_roots(ctx.migration, "new")]
        if not roots and str(ctx.migration.new_project_path or "").strip():
            roots = [Path(ctx.migration.new_project_path).expanduser().resolve()]
        if root is None or not root.is_dir() or not roots:

            return
        resolved = root.resolve()
        if not any(resolved != one and resolved.is_relative_to(one) for one in roots):
            self._log(ctx.job_id, label, f"not clearing {root}: outside the new addons directories", level="warn")

            return
```

Keep the old-project overlap check and the manifest check that follow it unchanged. Run the existing clear-module tests (`grep -rln "not clearing" tests`) to confirm they still pass.

- [ ] **Step 8: Run and commit**

Run: `uv run pytest -q && uv run ruff check` — expected PASS.

```bash
git add odoo_migrator/module_paths.py odoo_migrator/runner/tools.py odoo_migrator/runner/prompting.py odoo_migrator/runner/native.py prompts/system.md rules/common.md tests/test_tool_paths.py
git commit -m "feat: the file tools take container paths inside the plan's addons directories"
```

---

### Task 8: Interface and docs

**Files:**
- Modify: `odoo_migrator/module_paths.py` (new `addon_directories`)
- Modify: `odoo_migrator/api.py` (`_overview` context; `tree_resolve` flash count)
- Modify: `odoo_migrator/web/templates/migration_settings.html` (under `side-path`)
- Modify: `odoo_migrator/web/templates/tree.html` (node card mark, flash line, help text)
- Modify: `odoo_migrator/web/static/tree.js` (node data, card mark)
- Modify: `odoo_migrator/web/static/style.css` (`.badge.path-unknown`, `.side-addons`)
- Modify: `odoo_migrator/web/templates/components.html`
- Modify: `docs/ui-guide.md`, `docs/security.md`, `docs/architecture.md`
- Test: `tests/test_graph_mounts.py`

**Interfaces:**
- Consumes: `allowed_roots` (Task 7), `path_problem` on module rows (Task 4).
- Produces: `addon_directories(migration_id: str) -> dict[str, list[str]]` — host directories of the allowed roots, per side.

- [ ] **Step 1: Write the failing tests** (append)

```python
def test_the_card_names_the_addons_directories_found(client, tmp_path):
    client.post("/migrations", data={
        "name": "Acme", "accent": "violet", "source_version": "17.0", "target_version": "18.0",
        "old_project_path": "/tmp/Acme/old", "new_project_path": "/tmp/Acme/new",
    }, follow_redirects=True)
    with database.connection_context():
        row = Migration.get(Migration.name == "Acme")
    client.post(f"/migrations/{row.id}/open", follow_redirects=True)
    save_side_container(row.id, "old", "odoo-old")
    save_side_container(row.id, "new", "odoo-new")
    engine = client.app.state.compose
    engine.add_container(name="odoo-old", image="odoo:17", status="running", labels={}, host_ports=[],
                         mounts=OLD_MOUNTS)
    engine.add_container(name="odoo-new", image="odoo:18", status="running", labels={}, host_ports=[],
                         mounts=NEW_MOUNTS)
    client.app.state.sheller.graph_modules.append(
        {"name": "baked_x", "depends": ["sale"], "state": "installed",
         "addons_root": "/opt/odoo-sh/x", "path": "/opt/odoo-sh/x/baked_x",
         "author": "Acme", "license": "OPL-1", "is_core": False}
    )

    tree = client.post("/tree/resolve", follow_redirects=True).text
    card = client.get(f"/?settings={row.id}").text

    assert "1 module has no known path on disk" in tree
    assert "Addons found in" in card
    assert "/opt/sources/project" in card
    assert "/opt/sources/project-18" in card
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest -q tests/test_graph_mounts.py -k card_names`
Expected: FAIL on the flash text.

- [ ] **Step 3: `addon_directories`** in `module_paths.py`

```python
def addon_directories(migration_id: str) -> dict[str, list[str]]:
    """What the card shows: the host directories the tools may reach."""
    from odoo_migrator.db import database
    from odoo_migrator.models import Migration

    with database.connection_context():
        migration = Migration.get_or_none(Migration.id == migration_id)
    if migration is None:

        return {"old": [], "new": []}

    return {
        side: [mount["host"] for mount in allowed_roots(migration, side)]
        for side in ("old", "new")
    }
```

- [ ] **Step 4: API** — in `_overview` add `"addons_roots": addon_directories(migration["id"]) if migration else {"old": [], "new": []},`. In `tree_resolve`, count modules with a problem after `resolve_graph` and add it to the query:

```python
        rows = resolve_graph(_sheller(request), current["id"])
        unplaced = sum(1 for row in rows if row.get("path_problem") and not row.get("struck_out"))
```

then `query["unplaced"] = unplaced` beside `migrated`, and in `_tree_page` add `"unplaced": _count_param(request, "unplaced"),`.

- [ ] **Step 5: Templates**

`migration_settings.html`, right after the `<p class="mono side-path">…</p>` line:

```html
        {% set found = addons_roots[side] %}
        {% if found %}
        <p class="field-hint side-addons">Addons found in <span class="mono">{{ found | join(", ") }}</span></p>
        {% endif %}
```

`tree.html`, inside the `graph_updated` block, after the migrated sentence:

```html
    {% if unplaced %}
    <p class="tree-flash field-hint">{{ unplaced }} module{% if unplaced != 1 %}s have{% else %} has{% endif %} no known path on disk and will not start. The node card says why.</p>
    {% endif %}
```

In the node card, right after `id="node-card-status"` span:

```html
                  <span class="badge path-unknown" id="node-card-path" hidden>path unknown</span>
```

In the Help text add one sentence: `A changed compose file needs <strong>Build graph</strong> again: the app reads where addons are mounted only then.`

- [ ] **Step 6: `tree.js`** — add `path_problem: row.path_problem || "",` to the node `data`, and in the card-fill function after the status badge:

```javascript
    const pathEl = document.getElementById("node-card-path");
    if (pathEl) {
      const problem = node.data("path_problem");
      pathEl.hidden = !problem;
      pathEl.title = problem
        ? "The app does not know where this module is on disk: " + problem
        : "";
    }
```

and hide it (`pathEl.hidden = true`) where the card empties, next to `statusEl.hidden = true`.

- [ ] **Step 7: CSS** — append near `.badge.migrated`

```css
/* Not a status: the module keeps its own. A quiet mark that it cannot
   start until its directory is mounted. */
.badge.path-unknown {
  border-style: dashed;
  color: var(--muted);
}

.side-addons {
  margin-top: 2px;
  overflow-wrap: anywhere;
}
```

- [ ] **Step 8: `/components` row** — beside the badges row add `<span class="badge path-unknown">path unknown</span>`, and a line: `A module whose directory is not mounted keeps its status and carries this mark; it does not start.`

- [ ] **Step 9: Docs**
  - `docs/ui-guide.md`: in the Tree section, after the migrated paragraph, a paragraph on Build graph reading mounts, "Addons found in" on the card, and `path unknown` with its three reasons. In the Old / New project row: the tools reach the addons directories the containers mount, not the whole project.
  - `docs/security.md` line 28 paragraph and line 96: the tools reach only bind-mounted directories that hold a module of the plan; `/`, the home directory and the app's data directory are never roots; `..` and symlinks are resolved before the check.
  - `docs/architecture.md` lines 17 and 108–109: same sentence as security, shorter.

- [ ] **Step 10: Run and commit**

Run: `uv run pytest -q && uv run ruff check` — expected PASS. Open `/components` and a migration card in the browser; the new mark and line render.

```bash
git add odoo_migrator/module_paths.py odoo_migrator/api.py odoo_migrator/web docs tests/test_graph_mounts.py
git commit -m "feat: the card names where addons were found; Tree marks modules with no path"
```
