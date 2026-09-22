"""Module graph from live Odoo, via a read-only Sheller session on the old container."""

import ast
import json

from odoo_migrator.db import database
from odoo_migrator.models import Job, Migration, Module
from odoo_migrator.sheller.errors import ShellerError

# Last expression is json.dumps(...): the session returns the repr of that
# string, the same shape os_source parses in Sheller.
GRAPH_SNIPPET = """\
import json, os, odoo
from odoo.modules.module import get_module_path

# Odoo's own tree, definitively. Not the directory `base` lives in: `base`
# is under odoo/addons while most core modules are under addons/, so keying
# off it left all 76 of them looking like the project's. `odoo.__file__` is
# None here because odoo is a namespace package, hence __path__.
_pkg = list(getattr(odoo, '__path__', []) or [])
install_root = os.path.dirname(_pkg[0]) if _pkg else ''

Module = env['ir.module.module']
rows = []
for rec in Module.search([('state', 'in', ('installed', 'to upgrade'))], order='name'):
    try:
        path = get_module_path(rec.name, display_warning=False) or ''
    except Exception:
        path = ''
    rows.append({
        'name': rec.name,
        'depends': [dep.name for dep in rec.dependencies_id],
        'state': rec.state,
        'path': path,
        'addons_root': os.path.dirname(path) if path else '',
        'author': rec.author or '',
        'license': rec.license or '',
        'is_core': bool(install_root) and path.startswith(install_root + os.sep),
    })
json.dumps(rows)
"""


class GraphError(Exception):
    """The module graph could not be resolved from the old container."""


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
            if stored is None:
                next_id += 1
                Module.create(
                    migration=migration_id,
                    technical_name=name,
                    depends=depends,
                    addons_root=row.get("addons_root") or "",
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


def set_struck(module_id: int, struck: bool | None = None) -> dict:
    with database.connection_context():
        module = Module.get_or_none(Module.id == module_id)
        if module is None:
            raise GraphError("unknown module")
        module.struck_out = (not module.struck_out) if struck is None else bool(struck)
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
        downstream = set(order)
        wanted = (not module.struck_out) if struck is None else bool(struck)
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
        updated: list[Module] = []
        for name in order:
            row = by_name[name]
            if not wanted and any(
                by_name[parent].struck_out and parent not in downstream
                for parent in parents[name]
            ):
                continue
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
        updated: list[Module] = []
        for name in ordered_names:
            row = by_name[name]
            if not row.struck_out:
                continue
            blocking = [
                parent
                for parent in parents[name]
                if by_name[parent].struck_out
            ]
            if blocking:
                raise GraphError(
                    f"Bring {blocking[0]} back before {name}."
                )
            row.struck_out = False
            row.save()
            updated.append(row)

        return [_module_row(row) for row in updated]


def _prune_shared_strike_ancestors(
    names: set[str],
    children: dict[str, list[str]],
    by_name: dict[str, Module],
) -> set[str]:
    pruned = set(names)
    changed = True
    while changed:
        changed = False
        for name in list(pruned):
            for child in children[name]:
                if child not in pruned and not by_name[child].struck_out:
                    pruned.discard(name)
                    changed = True
                    break

    return pruned


def leave_out_selection(module_ids: list[int]) -> list[dict]:
    """Leave selected modules out of the migration.

    One leaf module walks depends upward (shared ancestors stay if a sibling
    branch is still in). Several modules, or one with dependents, strike the
    selection and everything downstream of it.
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
        children: dict[str, list[str]] = {name: [] for name in by_name}
        for row in rows:
            for dep in json.loads(row.depends or "[]"):
                if dep in by_name:
                    parents[row.technical_name].append(dep)
                    children[dep].append(row.technical_name)
        steps = topological_steps(
            {name: json.loads(row.depends or "[]") for name, row in by_name.items()}
        )
        seeds: set[str] = set()
        for module_id in unique_ids:
            row = by_id.get(module_id)
            if row is None:
                raise GraphError("unknown module")
            seeds.add(row.technical_name)
        if len(seeds) == 1:
            only = next(iter(seeds))
            downstream = _downstream(rows, only)
            if len(downstream) <= 1:
                names = _prune_shared_strike_ancestors(
                    _ancestor_names(seeds, parents),
                    children,
                    by_name,
                )
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
            if row.struck_out:
                continue
            row.struck_out = True
            row.save()
            updated.append(row)

        return [_module_row(row) for row in updated]


def topological_steps(depends_by_name: dict[str, list[str]]) -> dict[str, int]:
    """Visual row: in-graph edges from a root, plus one.

    Roots (no parent in the kept set) are step 1. A child is one more
    than the nearest parent — the same rank breadthfirst paints.
    """
    names = set(depends_by_name)
    children: dict[str, list[str]] = {name: [] for name in names}
    for name, deps in depends_by_name.items():
        for dep in deps:
            if dep in names:
                children[dep].append(name)
    roots = sorted(
        name
        for name, deps in depends_by_name.items()
        if not any(dep in names for dep in deps)
    )
    rank = {name: 1 for name in roots}
    queue = list(roots)
    while queue:
        name = queue.pop(0)
        for child in children[name]:
            if child not in rank:
                rank[child] = rank[name] + 1
                queue.append(child)
    leftover = [name for name in names if name not in rank]
    if leftover:
        bump = max(rank.values(), default=0) + 1
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


def earlier_steps_blocking(migration_id: str, step: int | None) -> str | None:
    """A later wave does not start while an earlier one is still unfinished.

    Struck modules are out of the plan. Failed, queued, running and
    waiting_for_human on a previous step all block.
    """
    if step is None or step <= 1:

        return None
    with database.connection_context():
        rows = list(
            Module.select().where(
                (Module.migration == migration_id) & (Module.struck_out == False)
            )
        )
    ranks = topological_steps(
        {row.technical_name: json.loads(row.depends or "[]") for row in rows}
    )
    blockers = [
        row
        for row in rows
        if row.status != "passed" and ranks.get(row.technical_name, 1) < step
    ]
    if not blockers:

        return None
    earliest = min(ranks[row.technical_name] for row in blockers)

    return f"Step {step} cannot start until step {earliest} is migrated."


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


def resolve_graph(client, migration_id: str) -> list[dict]:
    with database.connection_context():
        migration = Migration.get_or_none(
            (Migration.id == migration_id) & (Migration.archived_at.is_null())
        )
        if migration is None:
            raise GraphError("no open migration")
        old_container = migration.old_container
    if not old_container:
        raise GraphError("Attach the old container first.")
    try:
        probed = client.probe(old_container)
    except ShellerError as exc:
        raise GraphError(str(exc)) from exc
    if not probed.get("ok") or not probed.get("db_name") or not probed.get("odoo_bin"):
        raise GraphError("the old container could not be probed")
    opened = None
    write_key = None
    try:
        opened = client.open_session(
            container=old_container,
            database=probed["db_name"],
            odoo_bin=probed["odoo_bin"],
            owner={
                "kind": "agent",
                "label": "odoo-migrator",
                "migration_id": migration_id,
            },
            allow_commit=False,
        )
        write_key = opened["write_key"]
        executed = client.exec(opened["id"], GRAPH_SNIPPET, write_key=write_key)
        rows = parse_graph_payload(executed)
    except GraphError:
        raise
    except ShellerError as exc:
        raise GraphError(str(exc)) from exc
    except Exception as exc:
        raise GraphError(str(exc)) from exc
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

    return persist_graph(migration_id, rows)
