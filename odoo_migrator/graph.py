"""Module graph from live Odoo, via a read-only Sheller session on the source container."""

import ast
import json
import os
from pathlib import Path, PurePosixPath

from odoo_migrator.db import database
from odoo_migrator.models import Job, Migration, Module
from odoo_migrator.module_paths import place
from odoo_migrator.mounts import covering
from odoo_migrator.mounts import load as load_mounts
from odoo_migrator.sheller.errors import ShellerError


def project_addons_roots(project_path: str) -> list[str]:
    """Directories on the host where this migration's custom addons live."""
    if not project_path.strip():
        return []
    root = Path(project_path).expanduser().resolve()
    roots: list[str] = []
    if root.is_dir():
        roots.append(str(root))
    for name in ("addons", "custom_addons", "custom", "odoo"):
        child = root / name
        if child.is_dir():
            roots.append(str(child.resolve()))

    return list(dict.fromkeys(roots))


def build_graph_snippet(project_path: str, container_roots: list[str] | None = None) -> str:
    """Odoo code that returns the module graph as a JSON string.

    Installed modules are always included. Uninstalled rows are kept only
    when their path sits under the project's own addons directories — so a
    Resolve after copying addons in picks them up once Apps is updated,
    without flooding the tree with every uninstalled core module. Those
    directories are the container directories of the source side's bind mounts
    when they are known — compared against paths inside the container, where
    they mean something — and the host project roots otherwise. A root that
    contains, or is inside, Odoo's own tree is dropped: keeping it would list
    every uninstalled core module.
    """
    roots = container_roots if container_roots else project_addons_roots(project_path)
    roots_literal = json.dumps(roots)

    return f"""\
import json, os, odoo
from odoo.modules.module import get_module_path

# Odoo's own tree, definitively. Not the directory `base` lives in: `base`
# is under odoo/addons while most core modules are under addons/, so keying
# off it left all 76 of them looking like the project's. `odoo.__file__` is
# None here because odoo is a namespace package, hence __path__.
_pkg = list(getattr(odoo, '__path__', []) or [])
install_root = os.path.dirname(_pkg[0]) if _pkg else ''
def _overlaps_install(root):
    if not install_root:
        return False
    return (
        root == install_root
        or root.startswith(install_root + os.sep)
        or install_root.startswith(root + os.sep)
    )

project_roots = [root for root in {roots_literal} if not _overlaps_install(root)]

def _under_project(path):
    if not path or not project_roots:
        return False
    for root in project_roots:
        if path == root or path.startswith(root + os.sep):
            return True
    return False

Module = env['ir.module.module']
rows = []
for rec in Module.search(
    [('state', 'in', ('installed', 'to upgrade', 'uninstalled'))],
    order='name',
):
    try:
        path = get_module_path(rec.name, display_warning=False) or ''
    except Exception:
        path = ''
    if rec.state == 'uninstalled' and not _under_project(path):
        continue
    rows.append({{
        'name': rec.name,
        'depends': [dep.name for dep in rec.dependencies_id],
        'state': rec.state,
        'path': path,
        'addons_root': os.path.dirname(path) if path else '',
        'author': rec.author or '',
        'license': rec.license or '',
        'is_core': bool(install_root) and path.startswith(install_root + os.sep),
    }})
json.dumps(rows)
"""


# Finished without further work from the queue. `migrated` is a module the
# target already had installed when the graph was read: done, but not by this
# app, so it is counted apart from `passed` wherever that matters to a person.
DONE = ("passed", "migrated")


class GraphError(Exception):
    """The module graph could not be resolved from the source container."""


def parse_graph_payload(executed: dict) -> list[dict]:
    failure = executed.get("error")
    if failure:
        message = failure["message"] if isinstance(failure, dict) else str(failure)
        raise GraphError(f"Odoo did not return a graph: {message}")
    if executed.get("result_truncated"):
        raise GraphError("the graph was truncated")
    raw = executed.get("result")
    if not raw:
        raise GraphError("the session returned no graph")
    try:
        value = ast.literal_eval(raw) if isinstance(raw, str) else raw
        if isinstance(value, str):
            value = json.loads(value)
    except (ValueError, SyntaxError, json.JSONDecodeError) as exc:
        raise GraphError("the session returned no graph") from exc
    if not isinstance(value, list):
        raise GraphError("the session returned no graph")

    return value


def _module_row(module: Module, question: str | None = None) -> dict:

    return {
        "id": module.id,
        "technical_name": module.technical_name,
        "depends": json.loads(module.depends or "[]"),
        "addons_root": module.addons_root,
        "author": module.author,
        "license": module.license,
        "absent_from_odoo": bool(module.absent_from_odoo),
        "struck_out": bool(module.struck_out),
        "batch_group": module.batch_group,
        "node_id": module.node_id,
        "status": module.status,
        "has_assumptions": bool(module.has_assumptions),
        "needs_review": bool(module.needs_review),
        "path_problem": module.path_problem or "",
        "notes": module.notes or "",
        "question": question,
        "position": module.position,
    }


def steps_for_migration(migration_id: str) -> dict[str, int]:
    with database.connection_context():
        rows = list(Module.select().where(Module.migration == migration_id))

    return topological_steps(
        {row.technical_name: json.loads(row.depends or "[]") for row in rows}
    )


def _attach_steps(listed: list[dict]) -> None:
    ranks = topological_steps(
        {row["technical_name"]: row["depends"] for row in listed}
    )
    for row in listed:
        row["step"] = ranks.get(row["technical_name"])


def _attach_migration_ranks(listed: list[dict]) -> None:
    """Badge label #rank/step: rank is migration order over the whole graph.

    Sort by step, then name — so the root on step 1 is always #1/1 and
    nothing on step 2 can show #1. node_id stays an internal Resolve serial.
    """
    ordered = sorted(
        listed,
        key=lambda row: (row.get("step") or 1, row["technical_name"]),
    )
    for rank, row in enumerate(ordered, start=1):
        row["migration_rank"] = rank


def list_modules(migration_id: str) -> list[dict]:
    with database.connection_context():
        rows = list(
            Module.select()
            .where(Module.migration == migration_id)
            .order_by(Module.technical_name)
        )
        next_id = max((row.node_id for row in rows), default=0)
        for row in rows:
            if row.node_id == 0:
                next_id += 1
                row.node_id = next_id
                row.save()
        waiting = [row.id for row in rows if row.status == "waiting_for_human"]
        questions: dict[int, str | None] = {}
        if waiting:
            for job in Job.select().where(
                (Job.module.in_(waiting)) & (Job.status == "waiting_for_human")
            ):
                questions[job.module_id] = job.question
        listed = [_module_row(row, questions.get(row.id)) for row in rows]
    _attach_steps(listed)
    _attach_migration_ranks(listed)

    return listed


def list_pipeline_steps(
    migration_id: str, *, pending_only: bool = False, listed: list[dict] | None = None
) -> list[dict]:
    """Dependency steps still in the plan — same waves as Tree.

    Struck modules are out. A wave whose modules have already passed stays
    in the list (``pending`` is 0) so Selection can show it. With
    ``pending_only``, those finished waves are omitted and ``modules`` is
    only the work still left. `listed` is the module list the caller already
    has; without it the steps read their own.
    """
    if listed is None:
        listed = list_modules(migration_id)
    by_step: dict[int, list[dict]] = {}
    for row in listed:
        if row["struck_out"]:
            continue
        wave = row.get("step") or 1
        by_step.setdefault(wave, []).append(row)
    kept = {row["technical_name"]: row for rows in by_step.values() for row in rows}

    def needs(rows: list[dict]) -> list[int]:
        """Earlier steps this wave's modules hang on, through modules not yet migrated."""
        found: set[int] = set()
        seen: set[str] = set()
        stack = [dep for row in rows for dep in row.get("depends") or []]
        while stack:
            name = stack.pop()
            parent = kept.get(name)
            if parent is None or name in seen:
                continue
            seen.add(name)
            if parent["status"] not in DONE:
                found.add(parent.get("step") or 1)
            stack.extend(parent.get("depends") or [])

        return sorted(found)

    steps = []
    for wave in sorted(by_step):
        rows = by_step[wave]
        pending = [row for row in rows if row["status"] not in DONE]
        if pending_only and not pending:
            continue
        shown = pending if pending_only else rows
        steps.append(
            {
                "step": wave,
                "needs": [step for step in needs(pending) if step < wave],
                "count": len(shown),
                "pending": len(pending),
                "modules": sorted(row["technical_name"] for row in shown),
                "passed": sorted(
                    row["technical_name"] for row in rows if row["status"] in DONE
                ),
            }
        )

    return steps


def modules_in_step_order(
    migration_id: str, step: int, *, listed: list[dict] | None = None
) -> list[dict]:
    """Active modules on one dependency step, in migration order."""
    if listed is None:
        listed = list_modules(migration_id)
    active = [
        row
        for row in listed
        if not row["struck_out"] and (row.get("step") or 1) == step
    ]
    active.sort(
        key=lambda row: (row.get("migration_rank") or 0, row["technical_name"])
    )

    return active


# Odoo Enterprise Edition License. Measured on a real database: it marked
# 46 of the 48 modules in the Enterprise directory, and nothing else.
#
# Not the author field, which was the first idea and is the wrong one. On
# the same database it was almost perfect — "Odoo S.A." on 124 of 128 — and
# still had one core module spelling it "odoo", plus a comma-separated
# "VentorTech, Odoo Community Association" on another. Worse than the
# spelling: an author string is self-declared, and copying a core module as
# the base for an override, keeping its manifest header, is ordinary Odoo
# practice. That module would be filtered away as Odoo's and silently never
# migrated — a false negative on exactly the code this tool exists for.
# A licence is a legal claim, not a habit.
ENTERPRISE_LICENSE = "OEEL-1"


def is_odoos_own(row: dict) -> bool:
    """Odoo's code, which the customer does not port: Odoo ships its own.

    Judged per module rather than per directory. Two of the Enterprise
    modules on that database were LGPL-3, and an odoo.sh layout can mix a
    customer's addons into a shared directory — so excluding a whole root
    because something in it is Enterprise would hide real work. Showing two
    modules too many costs a strike; hiding one costs a module that never
    gets migrated and nobody notices.
    """

    return bool(row.get("is_core")) or row.get("license") == ENTERPRISE_LICENSE


def carries_human_work(module: Module) -> bool:
    """Anything a person put on this module by hand.

    ``batch_group`` is not this: Resolve draws the waves from the graph,
    and a derived grouping is not a reason to keep a module Odoo no
    longer reports.
    """

    return bool(
        module.struck_out
        or module.notes
        or module.has_assumptions
        or module.status != "queued"
    )


def persist_graph(migration_id: str, rows: list[dict], *, keep_core: bool = False) -> list[dict]:
    """Store the snapshot. Never lose what a person typed.

    Core Odoo modules are dropped unless asked for: on a real database only
    3 of 128 installed modules belonged to the project, the rest being Odoo
    and Enterprise. The product ports a project's own addons, and a tree of
    125 modules nobody will migrate makes the batching screen useless.
    """
    kept = [
        row
        for row in rows
        if row.get("name") and (keep_core or not is_odoos_own(row))
    ]
    names = {row["name"] for row in kept}
    with database.connection_context():
        migration = Migration.get_by_id(migration_id)
        framework = _framework_mounts(migration, rows)
        existing = {
            row.technical_name: row
            for row in Module.select().where(Module.migration == migration_id)
        }
        next_id = max((row.node_id for row in existing.values()), default=0)
        for position, row in enumerate(kept):
            name = row["name"]
            # Every dependency inside the kept set, not one arbitrary parent.
            depends = json.dumps([dep for dep in (row.get("depends") or []) if dep in names])
            stored = existing.get(name)
            # Where the source is on this machine, and inside each container.
            # The graph's `path` is the container's; the suite's graph double
            # gives only `addons_root`.
            container_path = _container_path(row)
            placed = place(migration, name, container_path, framework)
            if stored is None:
                next_id += 1
                Module.create(
                    migration=migration_id,
                    technical_name=name,
                    depends=depends,
                    addons_root=row.get("addons_root") or "",
                    **placed,
                    author=row.get("author") or "",
                    license=row.get("license") or "",
                    position=position,
                    node_id=next_id,
                )
            else:
                if stored.node_id == 0:
                    next_id += 1
                    stored.node_id = next_id
                stored.depends = depends
                stored.addons_root = row.get("addons_root") or ""
                for field_name, value in placed.items():
                    setattr(stored, field_name, value)
                stored.author = row.get("author") or ""
                stored.license = row.get("license") or ""
                stored.position = position
                stored.absent_from_odoo = False
                stored.save()
        for name, stored in existing.items():
            if name in names:
                continue
            # A module Odoo no longer reports is flagged, not deleted. Deleting
            # it threw away notes, status, batch and assumptions — and a
            # resolve against the wrong database would have wiped the lot.
            if carries_human_work(stored):
                stored.absent_from_odoo = True
                stored.save()
            else:
                stored.delete_instance()
        if names:
            kept_rows = list(
                Module.select().where(
                    (Module.migration == migration_id)
                    & (Module.technical_name.in_(list(names)))
                )
            )
            steps = topological_steps(
                {
                    row.technical_name: json.loads(row.depends or "[]")
                    for row in kept_rows
                }
            )
            for row in kept_rows:
                step = steps[row.technical_name]
                if row.batch_group != step:
                    row.batch_group = step
                    row.save()

    return list_modules(migration_id)


def _container_path(row: dict) -> str:
    addons_root = row.get("addons_root") or ""

    return row.get("path") or (f"{addons_root}/{row['name']}" if addons_root else "")


def _framework_mounts(migration: Migration, rows: list[dict]) -> frozenset[str]:
    """Source-side mount points that hold any of Odoo's own modules."""
    old_mounts = load_mounts(migration.old_mounts)
    found = set()
    for row in rows:
        if not row.get("name") or not is_odoos_own(row):
            continue
        mount = covering(old_mounts, _container_path(row))
        if mount is not None:
            found.add(mount["container"])

    return frozenset(found)


def _project_mount_points(migration: Migration) -> list[str]:
    """Source-side mount points inside the source project directory.

    Where the graph keeps uninstalled modules. A mounted OCA or Enterprise
    checkout holds dozens nobody installed, and listing them put each in
    the plan.
    """
    project = (migration.old_project_path or "").strip()
    if not project:

        return []
    root = Path(os.path.realpath(Path(project).expanduser()))
    points = []
    for mount in load_mounts(migration.old_mounts):
        host = Path(os.path.realpath(mount["host"]))
        if host == root or root in host.parents:
            points.append(mount["container"])

    return points


# Excluding a module that is already done used to be allowed, by the node
# card's Exclude and by Exclude all alike: a finished port then read as left
# out on Tree and Result, and its parents' exclusions dragged it along. A
# done module stays in the plan; reopening it on Result is how it runs again.
def _finished(row: Module) -> bool:

    return row.status in DONE


def _already_done(names: list[str]) -> GraphError:
    listed = ", ".join(sorted(names))

    return GraphError(
        f"{listed} {'is' if len(names) == 1 else 'are'} already migrated and "
        f"cannot be excluded. Reopen on Result to run again."
    )


def set_struck(module_id: int, struck: bool | None = None) -> dict:
    with database.connection_context():
        module = Module.get_or_none(Module.id == module_id)
        if module is None:
            raise GraphError("unknown module")
        wanted = (not module.struck_out) if struck is None else bool(struck)
        if wanted and not module.struck_out and _finished(module):
            raise _already_done([module.technical_name])
        module.struck_out = wanted
        module.save()

        return _module_row(module)


def _downstream(rows: list[Module], start: str) -> list[str]:
    by_name = {row.technical_name: row for row in rows}
    children: dict[str, list[str]] = {name: [] for name in by_name}
    for row in rows:
        for dep in json.loads(row.depends or "[]"):
            if dep in by_name:
                children[dep].append(row.technical_name)
    seen: set[str] = set()
    order: list[str] = []
    queue = [start]
    while queue:
        name = queue.pop(0)
        if name in seen or name not in by_name:
            continue
        seen.add(name)
        order.append(name)
        queue.extend(children[name])

    return order


def dependents_by_name(rows: list[dict]) -> dict[str, set[str]]:
    """Every module that depends on each one, however far down the chain.

    `rows` are listed modules (`depends` a list of names). A dependency that
    is not in the list is not a module of this migration and is ignored.
    """
    names = {row["technical_name"] for row in rows}
    children: dict[str, list[str]] = {name: [] for name in names}
    for row in rows:
        for dep in row.get("depends") or []:
            if dep in names:
                children[dep].append(row["technical_name"])
    found: dict[str, set[str]] = {}
    for name in names:
        seen: set[str] = set()
        stack = list(children[name])
        while stack:
            child = stack.pop()
            if child in seen or child == name:
                continue
            seen.add(child)
            stack.extend(children[child])
        found[name] = seen

    return found


def skip_subtree(module_id: int, struck: bool | None = None) -> list[dict]:
    """Leave a module out of the migration, and everything that depends on it.

    Restoring brings that same downstream set back, except a module that
    still depends on some other skipped parent.
    """
    with database.connection_context():
        module = Module.get_or_none(Module.id == module_id)
        if module is None:
            raise GraphError("unknown module")
        rows = list(Module.select().where(Module.migration == module.migration_id))
        by_name = {row.technical_name: row for row in rows}
        parents: dict[str, list[str]] = {name: [] for name in by_name}
        for row in rows:
            for dep in json.loads(row.depends or "[]"):
                if dep in by_name:
                    parents[row.technical_name].append(dep)
        order = _downstream(rows, module.technical_name)
        wanted = (not module.struck_out) if struck is None else bool(struck)
        if wanted and not module.struck_out and _finished(module):
            raise _already_done([module.technical_name])
        if not wanted:
            blocking = [
                parent
                for parent in parents[module.technical_name]
                if by_name[parent].struck_out
            ]
            if blocking:
                raise GraphError(
                    f"Bring {blocking[0]} back before {module.technical_name}."
                )
        if not wanted:
            # Parents before children: a module comes back only when each of its
            # parents is in the plan or came back in this pass. Being downstream
            # of the restored module is not enough, the parent may stay out.
            steps = topological_steps(
                {name: json.loads(row.depends or "[]") for name, row in by_name.items()}
            )
            order = sorted(order, key=lambda name: (steps.get(name, 1), name))
        back: set[str] = set()
        updated: list[Module] = []
        for name in order:
            row = by_name[name]
            if wanted and not row.struck_out and _finished(row):
                continue
            if not wanted and any(
                by_name[parent].struck_out and parent not in back
                for parent in parents[name]
            ):
                continue
            back.add(name)
            if row.struck_out != wanted:
                row.struck_out = wanted
                row.save()
            updated.append(row)

        return [_module_row(row) for row in updated]


def _ancestor_names(
    seeds: set[str],
    parents: dict[str, list[str]],
) -> set[str]:
    """Every module on a depends edge walk from each seed up to the roots."""
    names = set(seeds)
    stack = list(seeds)
    while stack:
        name = stack.pop()
        for parent in parents[name]:
            if parent in names:
                continue
            names.add(parent)
            stack.append(parent)

    return names


def restore_selection(module_ids: list[int]) -> list[dict]:
    """Bring selected modules back along their depends chains only.

    Multi-select is the union of those upward paths. Sibling branches stay
    out; nothing downstream of an ancestor is woken unless it was selected.
    """
    if not module_ids:
        return []
    unique_ids = list(dict.fromkeys(module_ids))
    with database.connection_context():
        first = Module.get_or_none(Module.id == unique_ids[0])
        if first is None:
            raise GraphError("unknown module")
        migration_id = first.migration_id
        rows = list(Module.select().where(Module.migration == migration_id))
        by_id = {row.id: row for row in rows}
        by_name = {row.technical_name: row for row in rows}
        parents: dict[str, list[str]] = {name: [] for name in by_name}
        for row in rows:
            for dep in json.loads(row.depends or "[]"):
                if dep in by_name:
                    parents[row.technical_name].append(dep)
        steps = topological_steps(
            {name: json.loads(row.depends or "[]") for name, row in by_name.items()}
        )
        seeds: set[str] = set()
        for module_id in unique_ids:
            row = by_id.get(module_id)
            if row is None:
                raise GraphError("unknown module")
            seeds.add(row.technical_name)
        names = _ancestor_names(seeds, parents)
        ordered_names = sorted(names, key=lambda n: (steps.get(n, 1), n))
        # Decided whole, then written: a refusal half-way used to leave the
        # ancestors it had already brought back in.
        back: set[str] = set()
        for name in ordered_names:
            if not by_name[name].struck_out:
                continue
            blocking = [
                parent
                for parent in parents[name]
                if by_name[parent].struck_out and parent not in back
            ]
            if blocking:
                raise GraphError(
                    f"Bring {blocking[0]} back before {name}."
                )
            back.add(name)
        updated = [by_name[name] for name in ordered_names if name in back]
        with database.atomic():
            for row in updated:
                row.struck_out = False
                row.save()

        return [_module_row(row) for row in updated]


def leave_out_selection(module_ids: list[int]) -> list[dict]:
    """Leave selected modules out of the migration.

    A leaf is struck alone — parents stay in the plan. A module with
    dependents, or a multi-select, strikes the selection and everything
    downstream of it.
    """
    if not module_ids:
        return []
    unique_ids = list(dict.fromkeys(module_ids))
    with database.connection_context():
        first = Module.get_or_none(Module.id == unique_ids[0])
        if first is None:
            raise GraphError("unknown module")
        migration_id = first.migration_id
        rows = list(Module.select().where(Module.migration == migration_id))
        by_id = {row.id: row for row in rows}
        by_name = {row.technical_name: row for row in rows}
        steps = topological_steps(
            {name: json.loads(row.depends or "[]") for name, row in by_name.items()}
        )
        seeds: set[str] = set()
        for module_id in unique_ids:
            row = by_id.get(module_id)
            if row is None:
                raise GraphError("unknown module")
            seeds.add(row.technical_name)
        open_seeds = {name for name in seeds if not by_name[name].struck_out}
        if open_seeds and all(_finished(by_name[name]) for name in open_seeds):
            raise _already_done(list(open_seeds))
        if len(seeds) == 1:
            only = next(iter(seeds))
            downstream = _downstream(rows, only)
            if len(downstream) <= 1:
                names = set(seeds)
            else:
                names = set(downstream)
        else:
            names = set(seeds)
            for name in seeds:
                names.update(_downstream(rows, name))
        ordered_names = sorted(
            names,
            key=lambda n: (steps.get(n, 1), n),
            reverse=True,
        )
        updated: list[Module] = []
        for name in ordered_names:
            row = by_name[name]
            if row.struck_out or _finished(row):
                continue
            row.struck_out = True
            row.save()
            updated.append(row)

        return [_module_row(row) for row in updated]


def leave_out_all(migration_id: str) -> list[dict]:
    """Leave every module still in the plan out, except the ones already done."""
    with database.connection_context():
        rows = list(
            Module.select().where(
                (Module.migration == migration_id)
                & (~Module.struck_out)
                & (Module.status.not_in(DONE))
            )
        )
        updated: list[Module] = []
        for row in rows:
            row.struck_out = True
            row.save()
            updated.append(row)

    return [_module_row(row) for row in updated]


def include_all(migration_id: str) -> list[dict]:
    """Bring every excluded module back, parents before dependents."""
    with database.connection_context():
        rows = list(Module.select().where(Module.migration == migration_id))
        by_name = {row.technical_name: row for row in rows}
        parents: dict[str, list[str]] = {name: [] for name in by_name}
        for row in rows:
            for dep in json.loads(row.depends or "[]"):
                if dep in by_name:
                    parents[row.technical_name].append(dep)
        steps = topological_steps(
            {name: json.loads(row.depends or "[]") for name, row in by_name.items()}
        )
        ordered = sorted(
            rows,
            key=lambda row: (steps.get(row.technical_name, 1), row.technical_name),
        )
        updated: list[Module] = []
        for row in ordered:
            if not row.struck_out:
                continue
            blocking = [
                parent
                for parent in parents[row.technical_name]
                if by_name[parent].struck_out
            ]
            if blocking:
                continue
            row.struck_out = False
            row.save()
            updated.append(row)

    return [_module_row(row) for row in updated]


def topological_steps(depends_by_name: dict[str, list[str]]) -> dict[str, int]:
    """The wave each module runs in: one after its deepest parent.

    Roots (no parent in the set) are step 1. A wave runs only after every
    earlier one, so a module has to come after *all* it depends on. With
    the nearest parent deciding — the row breadthfirst paints — a module
    that needed both the root and a step-2 module sat in step 2 beside the
    one it needed, and ran first when its name sorted first.

    Modules in a cycle have no order to find; they share the step after
    everything else.
    """
    names = set(depends_by_name)
    children: dict[str, list[str]] = {name: [] for name in names}
    waiting: dict[str, int] = {name: 0 for name in names}
    for name, deps in depends_by_name.items():
        for dep in set(deps):
            if dep in names and dep != name:
                children[dep].append(name)
                waiting[name] += 1
    rank: dict[str, int] = {}
    ready = sorted(name for name, count in waiting.items() if count == 0)
    for name in ready:
        rank[name] = 1
    while ready:
        name = ready.pop(0)
        for child in children[name]:
            rank[child] = max(rank.get(child, 1), rank[name] + 1)
            waiting[child] -= 1
            if waiting[child] == 0:
                ready.append(child)
    leftover = [name for name in names if waiting[name] > 0]
    if leftover:
        bump = max((rank[name] for name in names if waiting[name] == 0), default=0) + 1
        for name in leftover:
            rank[name] = bump

    return rank


def modules_in_batch_order(migration_id: str, batch_group: int) -> list[dict]:
    """Dependents after their parents. Batch order is read off ``depends``.

    A single parent field used to drop half the edges on a real database,
    which is exactly how a batch would have run out of order.
    """
    with database.connection_context():
        rows = [
            row
            for row in Module.select().where(
                (Module.migration == migration_id) & (Module.batch_group == batch_group)
            )
            if not row.struck_out
        ]
    if not rows:

        return []
    by_name = {row.technical_name: row for row in rows}
    incoming = {row.technical_name: 0 for row in rows}
    outgoing: dict[str, list[str]] = {row.technical_name: [] for row in rows}
    for row in rows:
        for dep in json.loads(row.depends or "[]"):
            if dep in by_name:
                outgoing[dep].append(row.technical_name)
                incoming[row.technical_name] += 1
    ready = sorted(name for name, count in incoming.items() if count == 0)
    ordered = []
    while ready:
        name = ready.pop(0)
        ordered.append(by_name[name])
        for child in sorted(outgoing[name]):
            incoming[child] -= 1
            if incoming[child] == 0:
                ready.append(child)
                ready.sort()
    seen = {row.id for row in ordered}
    leftover = [row for row in rows if row.id not in seen]
    leftover.sort(key=lambda row: row.technical_name)
    ordered.extend(leftover)

    return [_module_row(row) for row in ordered]


def earlier_steps_blocking(
    migration_id: str, step: int | None, *, listed: list[dict] | None = None
) -> str | None:
    """A later wave does not start while an earlier one is still unfinished.

    Struck modules are out of the plan. Failed, queued, running and
    waiting_for_human on a previous step all block; passed and migrated do
    not. `listed` is the module list a caller that asks for many steps
    already holds: the same ranks, read once.
    """
    if step is None or step <= 1:

        return None
    if listed is None:
        listed = list_modules(migration_id)
    # Ranked over every module, struck or not, as Tree and the queue rank
    # them. Ranking the kept ones alone moved a module whose parent was
    # struck one step up here, and the guard then blocked a step on the
    # very module it was starting.
    ranks = {row["technical_name"]: row.get("step") or 1 for row in listed}
    blockers = [
        row
        for row in listed
        if not row["struck_out"]
        and row["status"] not in DONE
        and ranks.get(row["technical_name"], 1) < step
    ]
    if not blockers:

        return None
    earliest = min(ranks[row["technical_name"]] for row in blockers)

    return f"Step {step} cannot start until step {earliest} is migrated."


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


def set_batch_group(module_id: int, batch_group: int | None) -> dict:
    with database.connection_context():
        module = Module.get_or_none(Module.id == module_id)
        if module is None:
            raise GraphError("unknown module")
        module.batch_group = batch_group
        module.save()

        return _module_row(module)


def module_for_migration(migration_id: str, module_id: int) -> dict:
    with database.connection_context():
        module = Module.get_or_none(
            (Module.id == module_id) & (Module.migration == migration_id)
        )
        if module is None:
            raise GraphError("unknown module")

        return _module_row(module)


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
        containers = {"source": migration.old_container, "target": migration.new_container}
    snapshot = {}
    for side, name in containers.items():
        found = container_mounts(engine, name) if name else None
        snapshot[side] = found or []
    with database.connection_context():
        Migration.update(
            old_mounts=json.dumps(snapshot["source"]),
            new_mounts=json.dumps(snapshot["target"]),
        ).where(Migration.id == migration_id).execute()

    return snapshot


def resolve_graph(client, migration_id: str) -> list[dict]:
    with database.connection_context():
        migration = Migration.get_or_none(
            (Migration.id == migration_id) & (Migration.archived_at.is_null())
        )
        if migration is None:
            raise GraphError("no open migration")
        old_container = migration.old_container
        old_project_path = migration.old_project_path or ""
        old_mounts = load_mounts(migration.old_mounts)
        project_points = _project_mount_points(migration)
    if not old_container:
        raise GraphError("Start the source container on the migration's card first.")
    try:
        probed = client.probe(old_container)
    except ShellerError as exc:
        raise GraphError(str(exc)) from exc
    if not probed.get("ok") or not probed.get("db_name") or not probed.get("odoo_bin"):
        raise GraphError("the source container could not be probed")
    try:
        executed = _read_only_exec(
            client,
            container=old_container,
            database=probed["db_name"],
            odoo_bin=probed["odoo_bin"],
            migration_id=migration_id,
            code=build_graph_snippet(
                old_project_path,
                container_roots=project_points if old_mounts else None,
            ),
        )
        rows = parse_graph_payload(executed)
    except GraphError:
        raise
    except Exception as exc:
        raise GraphError(str(exc)) from exc

    return persist_graph(migration_id, rows)


def _read_only_exec(
    client,
    *,
    container: str,
    database: str,
    odoo_bin: str,
    migration_id: str,
    code: str,
) -> dict:
    """Run one read on a container in a session of its own, then drop it.

    Opened without commit, rolled back and closed whatever happens: a read
    leaves nothing behind on either side.
    """
    opened = None
    write_key = None
    try:
        opened = client.open_session(
            container=container,
            database=database,
            odoo_bin=odoo_bin,
            owner={
                "kind": "agent",
                "label": "odoo-migrator",
                "migration_id": migration_id,
            },
            allow_commit=False,
        )
        write_key = opened["write_key"]

        return client.exec(opened["id"], code, write_key=write_key)
    finally:
        if opened is not None and write_key:
            try:
                client.rollback(opened["id"], write_key=write_key)
            except ShellerError:
                pass
            try:
                client.close(opened["id"], write_key=write_key)
            except ShellerError:
                pass


# `odoo.addons.__path__` is where this Odoo looks for modules: the addons_path
# from its config, resolved, and the same on every version.
INSTALLED_SNIPPET = """\
import json, odoo
installed_names = env['ir.module.module'].search(
    [('state', 'in', ('installed', 'to upgrade'))]
).mapped('name')
json.dumps({
    'installed': sorted(installed_names),
    'addons_path': [str(path) for path in odoo.addons.__path__],
})
"""


def _read_target(client, migration: Migration) -> dict:
    """The target's installed module names and its addons_path."""
    probed = client.probe(migration.new_container)
    if not probed.get("ok") or not probed.get("odoo_bin"):
        raise GraphError("the target container could not be probed")
    known = probed.get("databases") or []
    # The card first, then the container's own odoo.conf, as a run does.
    chosen = (migration.new_database or "").strip() or (probed.get("db_name") or "")
    if not chosen:
        raise GraphError("the target container names no database")
    if known and chosen not in known:
        raise GraphError(f"database {chosen!r} is not on the target container")
    executed = _read_only_exec(
        client,
        container=migration.new_container,
        database=chosen,
        odoo_bin=probed["odoo_bin"],
        migration_id=migration.id,
        code=INSTALLED_SNIPPET,
    )
    value = _parse_target_payload(executed)
    installed = {name for name in value.get("installed") or [] if isinstance(name, str)}
    addons_path = [path for path in value.get("addons_path") or [] if isinstance(path, str)]

    return {"installed": installed, "addons_path": addons_path}


def _parse_target_payload(executed: dict) -> dict:
    failure = executed.get("error")
    if failure:
        message = failure["message"] if isinstance(failure, dict) else str(failure)
        raise GraphError(f"the target did not answer: {message}")
    raw = executed.get("result")
    try:
        value = ast.literal_eval(raw) if isinstance(raw, str) else raw
        if isinstance(value, str):
            value = json.loads(value)
    except (ValueError, SyntaxError, json.JSONDecodeError) as exc:
        raise GraphError("the target returned nothing readable") from exc
    if not isinstance(value, dict):
        raise GraphError("the target returned nothing readable")

    return value


OFF_PATH = "not on the target's addons_path"


def _mark_off_path(migration_id: str, addons_path: list[str]) -> int:
    """A module whose target directory Odoo does not load cannot be ported.

    The port would be written where the target mounts it and never read:
    upgrade answers "uninstallable", and an agent left to it once copied the
    module into a directory that is loaded, through exec, where two copies
    then drifted apart. Only modules placed from mounts are judged. The
    caller holds nothing.
    """
    loaded = {str(PurePosixPath(path)) for path in addons_path}
    if not loaded:

        return 0
    marked = 0
    with database.connection_context():
        for module in Module.select().where(
            (Module.migration == migration_id)
            & (Module.struck_out == False)
            & (Module.new_container_path != "")
            & (Module.path_problem == "")
        ):
            directory = str(PurePosixPath(module.new_container_path).parent)
            if directory in loaded:
                continue
            module.path_problem = OFF_PATH
            module.save()
            marked += 1

    return marked


def mark_migrated_from_target(client, migration_id: str) -> dict:
    """Mark the modules the target already has installed as `migrated`.

    A project often arrives half-ported. Only modules the app has not
    touched are marked: queued, in the plan, and with no job of ours. A
    module that ran here keeps what its run said. Nothing is unmarked: a
    module that has left the target stays `migrated` until a person
    reopens it.

    The same read checks where the target looks for modules: a module placed
    in a directory the target does not load gets `path_problem` (see
    `_mark_off_path`).

    Returns ``{"marked": n, "error": reason or None, "off_path": n}``. A
    target that is missing or does not answer is reported, not raised: the
    graph from the source is already stored and stays useful.
    """
    with database.connection_context():
        migration = Migration.get_or_none(Migration.id == migration_id)
    if migration is None:

        return {"marked": 0, "error": "no open migration", "off_path": 0}
    if not migration.new_container:

        return {"marked": 0, "error": "no target container is attached", "off_path": 0}
    try:
        target = _read_target(client, migration)
    except (GraphError, ShellerError) as exc:

        return {"marked": 0, "error": str(exc) or type(exc).__name__, "off_path": 0}
    off_path = _mark_off_path(migration_id, target["addons_path"])
    installed = target["installed"]
    if not installed:

        return {"marked": 0, "error": None, "off_path": off_path}
    with database.connection_context():
        ran = Job.select(Job.module).distinct()
        marked = (
            Module.update(status="migrated")
            .where(
                (Module.migration == migration_id)
                & (Module.technical_name.in_(sorted(installed)))
                & (Module.status == "queued")
                & (Module.struck_out == False)
                & (Module.id.not_in(ran))
            )
            .execute()
        )

    return {"marked": marked, "error": None, "off_path": off_path}
