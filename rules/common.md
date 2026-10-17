# Shared context

Sent once, in front of the version files. A version file says what that
version itself requires. Read this, then the source file (the idioms the
module is written in), then every later file through the target. Satisfy
the target. Do not apply a later version's syntax while the target is
still an earlier one.

A name that is not in these files is not a fact. Call `source` on the
container for that version before using it. Official changelog and the
target source win over memory. These notes cover on-premise majors
(16.0 through 20.0). Online minors such as 17.2 are not a separate file.

## Where the spelling changes

A later file replaces the point it names. The table is the index; the
version file is the instruction.

| Topic | Through | Becomes |
|---|---|---|
| List root, view type, `view_mode` | 17.0: `tree` | 18.0: `list`. `<tree>` is not a view type |
| Kanban card template | 17.0: `t-name="kanban-box"` | 18.0: `t-name="card"` (18 still loads `kanban-box` and warns). 19.0 requires `card` |
| View modifiers | 16.0: `attrs` and `states` | 17.0: Python expressions on the attribute |
| Field aggregation | 17.0: `group_operator` | 18.0: `aggregator` (18 still accepts the old name and warns) |
| SQL constraints | 18.0: `_sql_constraints` tuples | 19.0: `models.Constraint` and `models.Index` |
| Record label | 17.0: `name_get` still exists (deprecated in 17.0) | 18.0: the method is gone; use `display_name` |
| Access checks | 17.0: `check_access_rights` and `check_access_rule` | 18.0: `check_access`, `has_access`, `_filtered_access` |
| User groups | 18.0: `groups_id`, `users`, `category_id` | 19.0: `group_ids`, `user_ids`, `privilege_id` |
| Model access rows | 19.0: `ir.model.access` | 20.0: `ir.access` (that file says how) |
| JSON route | 18.0: `type="json"` | 19.0: `type="jsonrpc"` (`json` still aliases and warns) |
| Python | 16.0: 3.7+ | 17.0–19.0: 3.10+. 20.0: 3.12+ |

## Holds on every version in this stack

- Do not add `@api.multi` or `@api.one`. Both are gone.
- Core `create` is `@api.model_create_multi` and takes `vals_list`.
  Calling `create` with one dict still works. An override must match
  the multi signature.
- x2many commands go through `fields.Command` (`create`, `update`,
  `delete`, `unlink`, `link`, `clear`, `set`). A raw `(0, 0, vals)`
  tuple is the older spelling. Keep a tuple only when you are matching
  code that already uses it.
- Assets belong in the manifest `assets` key, not in an XML asset
  template.
- `request.not_found()` returns a `NotFound` exception. Raise it.
  Returning it logs a warning, and the dispatcher raises it anyway.
- `@api.constrains` runs only for fields present in the `create` or
  `write` call. A field absent from the view does not trip it on a
  form save.
- Through 19.0, access is `ir.model.access.csv` and record rules are
  `ir.rule`. Landing on 20.0 replaces both; that file says how.
- `cr.execute` with a string is the 16.0 form and still runs on later
  versions until a version file says otherwise. `SQL` exists from 17.0
  for composing queries. Do not rewrite a working string unless the
  target file says to, or the target container rejects it.
- Enterprise modules are a reference. Do not copy them into the port
  unless the module already depends on them.
- Upgrade scripts live in `migrations/<version>/`. Prefer that layout
  unless the target version's own modules use another directory; check
  with `source` before inventing `upgrades/`.
  Scripts from the old series do not travel: the upgrade step deletes
  every `migrations/` and `upgrades/` directory not named for the target
  series before Odoo reads the port. A script this migration needs goes
  under the target version (`18.0.1.0.0/`), with the full version.
