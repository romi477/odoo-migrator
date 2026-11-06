### Clicking a module's menus

`click_module` runs Odoo's own clickbot over the menus of one installed module,
as admin with debug on, in a test transaction. It opens each app, each menu
under it, each view and each filter, and fails on an error dialog. Nothing is
written. It always runs the whole module and can take up to 15 minutes: it
cannot click one menu alone, so use it to read a failure whole, not to confirm a
fix. A change to a view or an action is not in the database until the runner's
`upgrade` loads it, so a run right after your edit shows the old failure.

The answer lists each failed menu by xmlid with the client's reason, then what
was not reached. A run that did not happen says so: no browser in the container,
Odoo skipped it, or the module has no menu of its own. None of these is a pass.
