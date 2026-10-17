## Installing and updating a module

The `upgrade` step is the runner's, and it installs or upgrades the module
you are porting. This is for a dependency you need in place before you can
test anything.

A session loads the registry once, so a module's data, views and schema are
whatever they were when it opened:

    Module = env['ir.module.module']
    Module.search([('name', '=', 'sale')]).button_immediate_upgrade()
    Module.search([('name', '=', 'sale_stock')]).button_immediate_install()

Install pulls in the module's dependencies, and an empty recordset means the
loader has never seen that module: call `Module.update_list()` first — it
scans the addons paths for manifests — then search again. Either call
commits on its own, because it has to rebuild the registry.

A module's migration scripts run on upgrade, which is usually the point. A
script runs only if its version is above what `ir.module.module.latest_version`
records and no higher than the manifest's, so a database already at the
manifest version redoes schema and data but runs no script.

Edited Python is not picked up: the process imported those files at startup
and an upgrade does not re-import them. After you change a `.py` on the target
side, call `reopen`.

