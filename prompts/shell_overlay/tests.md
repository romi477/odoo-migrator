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
