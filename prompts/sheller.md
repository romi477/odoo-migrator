# Working inside the Odoo shell

What the tools on the other side of `exec` can do, and the idioms that do it
in one call instead of ten.

This is odoo-sheller's own guidance, vendored. The daemon serves it to MCP
clients through `os_help(topic)`, and this app is an HTTP client of the
daemon, not an MCP client — there is no route that would fetch it. The tool
names are ours; the rest is theirs, kept to the letter. Topics taken:
`limits`, `orm`, `code`, `log`, `records`, `modules`, `jobs`, `tests`. The
ones about opening, closing, owning and committing sessions are left out:
the runner does all of that, and you never see it.

"Running a test" below is rewritten rather than copied. Theirs describes an
MCP tool that opens a session of its own per run and hands back a job to
poll; this app calls the daemon's route directly, in the session the step
already holds, and waits. The spec forms, `failed` and the module's log
count are theirs, as of Sheller 1.8.3. What 1.10.0 added to that topic is
left out: `params`, and the JS (Hoot) runs they drive. `run_test` here takes
a spec and nothing else.

## What you must not attempt

- Grant yourself commit rights, or call the daemon's admin endpoints.
- Take a session you were not handed, or use a key you were not given.
- Read or write `~/.odoo-sheller/` directly. Everything you need is in the
  tools you were given.
- Count on Odoo 13 or 14. They open, with a warning, but are not fully
  tested: sessions, `exec`, commit, rollback and interrupt should work;
  `run_test` does not on 13 and says so.

These are not enforced by anything you hold. They are the terms of using
this tool at all.

## Writing idiomatic ORM code

A recordset is iterable, but a Python loop to pull one field, or to sum a
column, throws away idioms the ORM gives you for free — and reads worse to
whoever is watching the transcript.

- `records.mapped('name')` returns each record's `name` as a plain list.
  Prefer it over `[r.name for r in records]`.
- A dotted path follows a relation: `records.mapped('partner_id.bank_ids')`
  returns the union of every partner's banks, already de-duplicated, as one
  recordset — not a list of lists.
- Pass a callable for anything else: `records.mapped(lambda r: r.a + r.b)`,
  or `sum(records.mapped('qty'))` for a total.

`filtered()` narrows a recordset the same way — a function, a domain, or a
list of field names — and `sorted()` orders one, by a key function, a field
string, or the model's own default order with no argument at all. Neither
loads more from the database than `mapped()` does; none of the three is a
Python-side substitute for a real search domain.

Push filtering into `search()` itself rather than fetching broadly and
filtering in Python: `env['res.partner'].search([('is_company', '=', True)])`
reads and returns only what matches. `search_count(domain)` answers "how
many" without materializing any records at all — reach for it over
`len(records.search(domain))` whenever the records themselves are not needed.

A record that has an XML ID is fetched by that ID rather than searched for.
`env.ref` takes the module and the ID joined by a dot:

    env.ref("base.module_integration")
    env.ref(f"{module}.{xml_id}")

It raises `ValueError` when nothing is there; pass `raise_if_not_found=False`
for an empty recordset instead. Prefer it over `search([('name', '=', ...)])`
whenever the XML ID is known — a name is data and can be changed by anyone,
while an XML ID is the identity the module itself declared.

Recordsets support the usual set operations directly — `|` union, `&`
intersection, `-` difference, `in` membership, `<=` / `<` / `>=` / `>` for
subset and superset — so two recordsets are combined or compared with an
operator, not a loop over ids with a manual dict to deduplicate.

Iterating a recordset yields one-record recordsets, not raw rows: `for r in
records: r.name` already works, no `r['name']` and no re-`browse()`ing an id
out of a dict. A helper that is only meant to run against a single record
should open with `self.ensure_one()` rather than assuming — it raises
immediately and clearly instead of the ambiguous behavior of reading a
multi-record field.

## Reading the code you are debugging

Reading source is one tool call, not a shell:

    source(side="old", path="integration_shopify")
    source(side="old", path="integration_shopify/models/external.py",
           first=363, last=470)
    source(side="new", model="account.move", method="action_post")
    source(side="new", model="account.move", method="action_post",
           module="account")

A directory lists every file under it with its line count, so "what is in
this module" is answered before you guess a path. A file takes a
module-relative path or an absolute one inside an addons directory; anything
outside them is refused. These are paths **inside the container**, not the
host-relative paths `read_file` and `write_file` take.

A method answers what a file cannot. `text` is the implementation that
actually runs, and `overrides` is the chain of modules that define it, in
resolution order — first is the one that runs, last is the base; everything
between them is reached by `super()`. On `account.move.action_post` that is
three modules, while the model's MRO is fifty-two classes, so the chain is
the answer and the MRO is noise. `module=` reads one link instead of the
winner, and naming a module that does not define the method comes back as
`not_in_that_module` with the chain to choose from. Without `method`, the
model form gives the class source and every module extending the model.

When a read is not enough — searching, walking a class, comparing two
overrides — that goes in `exec`. You are in a Python REPL inside the running
instance, and reading through the *loaded registry* answers a question the
filesystem cannot: which override actually wins.

    import inspect
    cls = type(env['res.partner'])
    [m.__module__ for m in cls.__mro__ if 'addons' in str(m.__module__)]

That lists every module that extends the model, in resolution order. Then
read the one that matters:

    inspect.getsource(cls._compute_display_name)
    inspect.getsourcefile(cls._compute_display_name)

On disk every override sits side by side and nothing says which is in
effect; the MRO says. A real method's source is longer than the result
ceiling, so slice it rather than fetching it whole and losing the end:

    src = inspect.getsource(cls._compute_display_name).splitlines()
    src[:40]

To read a file by path and line range from inside `exec`, use Odoo's own
reader, which is confined to the addons paths:

    from odoo.tools import file_path, file_open
    file_path('sale')
    with file_open('sale/views/sale_views.xml') as f:
        head = f.read(4000)

`file_open` raises `FileNotFoundError` for a path outside the addons
directories (including one that climbs out with `..`) and will not create
files. Use it instead of bare `open()`.

## Seeing what Odoo logged

Every command reports `stderr_lines`: how many lines Odoo logged while it
ran. That is deliberately a number and not the lines — a log in every
response is context spent on output you did not ask for — but it is the one
thing you could not otherwise guess, so it is always there.

If `stderr_lines` was non-zero and you decide afterwards that you need the
lines, do **not** run the command again — it may have written to the
database, and a second run is a second write. Read them with `journal`,
which has every line in order, interleaved with the commands by time,
including whatever a response clipped and including a traceback Odoo logged
rather than raised. Never install a logging handler of your own to capture
the log; it is collected for you either way.

`run_test` on a class or a method returns its `stderr` without being asked,
because there the log *is* the answer — which test failed and why. A whole
module answers with `stderr_lines` instead and leaves its log in the
journal.

## Reading a record

To see what a record actually holds, read it whole rather than naming the
fields you expect. `read()` with no arguments returns every field you may
read, computed ones included, so a field you did not think to ask for is
there:

    env['res.partner'].browse(11).read()[0]

The return is always a list, one dict per record, so a single record ends in
`[0]`. On a recordset the list is what you want — keep it and read the whole
set in one call rather than one record at a time.

Two things it does not do. Relations are not followed: a many2one comes back
as `(id, display_name)` and a one2many or many2many as a list of `ids`, so
going a level deeper is a second read on the related model. And a wide model
read whole is expensive and can be large — binary fields, long text, every
computed field evaluated — so read one record whole to learn its shape, then
`search_read` with the fields that turned out to matter for many.

`read()` on several records returns one dict each, in no guaranteed order,
and silently drops records that no longer exist. `read(load=None)` gives
bare ids for relations instead of the `(id, display_name)` pairs.

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
and an upgrade does not re-import them. After you change a `.py` on the new
side, call `reopen`.

## Delayed jobs

`with_delay()` enqueues a job; this session will not run the queue for you.
To run a delayed method inline instead of enqueueing it, put
`queue_job__no_delay` on the environment context. Nested `with_delay()`
calls inherit it, so the whole chain runs on the spot:

    env = env(context=dict(env.context, queue_job__no_delay=True))
    record.with_delay().do_work()

A recordset you already hold still has the old context: call
`.with_context(queue_job__no_delay=True)` on it before `with_delay()`.

## Running a test

`run_test(side, test)` takes three forms:

- `module` — every standard test in that module, at_install and
  post_install, the set `--test-tags /module` runs. Tests tagged `-standard`
  or `external` are left out: name their class to run one of those.
- `module.TestClass` — one class.
- `module.TestClass.test_method` — one method.

Nothing else is a valid spec, and the daemon refuses anything that is not
one of those shapes with `invalid_test_spec`.

Every answer carries `failed`: one entry per failing test, `{"test": spec,
"kind": "failure" | "error"}`, where `test` is ready to pass straight back to
`run_test`. A failed subtest is listed as its method, a class whose
setUpClass failed as the class, and a test file whose setUpModule failed as
the whole module — no tag can name a file. To find out why one failed, run
that spec on its own: a class or a method answers with its log tail in
`stderr`.

A whole module answers with counts and `failed` only. Its log is thousands
of lines, so it comes back as `stderr_lines`, a count, and stays in the
journal — `journal` has every line if you really need it. Do not rerun a
module to see its log; rerun the failed specs.

Pick the narrowest run that answers your question. To check that a module
holds up, run the module, once — not class by class. To check one fix, run
the spec that failed. Take a single method only when that method is the
point.

Do not compose a spec out of names you guessed. `list_tests(side)` answers
with the classes this module declares on disk and hands back each one's spec
already shaped — use the string it gives you, for the class and for each
method. Pass `module=` to ask about a different addon. The answer is files
on disk, not "installed in this database", and an empty `classes` means the
module ships no tests.

**A test you just wrote is not there yet.** The session's process imported
the module's `tests` package when it opened, and writing a new file does not
re-import it. Write the file, call `reopen`, then `run_test`. Without the
reopen the run finds the old set of tests and says nothing about the one you
added — or finds no class by that name at all. The same holds after you
change the module's Python: reopen before you run its tests again.

**The run happens in this side's session, not a fresh one.** Odoo's own test
runner rolls back whatever transaction that session is holding before it
starts, so anything you set up with `exec` and had not committed is gone
when the run begins. `discarded_pending` in the answer says whether that
happened. Build a fixture with `exec` and then run a test against it in the
same session and you will be testing an empty database: the test must make
its own data, which is what a real Odoo test does anyway.

The call waits for the run to finish — there is nothing to poll and nothing
to close afterwards. A module may take up to 30 minutes, a class or a method
5; a run past that is interrupted and answers with an error. `test_result(side)`
re-reads the report of the last run in this step; it does not start one and
does not wait for anything.

What comes back is the daemon's shape, not a pass/fail flag: `test` holds
`success`, `tests_run`, `failures` and `errors`. A run of zero tests is not
a pass — it means the spec matched nothing.
