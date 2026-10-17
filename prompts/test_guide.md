# Writing a test for a module

A test here is the oracle for a port. It says, in assertions, what the module
does on the version it runs on, so that the same test can be run on the other
version and the two answers compared.

## What to pin

The behaviours that decide whether a port is faithful: a method that returns or
computes something; a computed field; a constraint that refuses a write; a
state change behind a button; an onchange or a default. One test method per
behaviour, eight at most: the smallest set that touches each once. Skip what has
no logic: field declarations, views, access files, translations, menus.

If you suspect the target version changes a behaviour, pin it anyway. The rules
for the target are not in front of you, and the step that ports the test has
them: it records a change that is on purpose.

## What to assert on

What a business user would see: field values, states, amounts, counts, returned
values. Do not assert on the text of an exception (check its class), on
translated strings, on record ids or sequence numbers, on the order of records
you did not sort, on timestamps, or on log output. Every test method needs an
assertion: one that only creates records passes whatever the module does.

## How to write it

Write for the version the test runs on, as it is. Read a test of a module that
has one with `source` before writing yours: the base class and the helpers are
that version's, not the ones you remember. Use TransactionCase, and
@tagged('post_install', '-at_install') when you rely on another module's data.
Make all your data in setUpClass or in the test; do not rely on demo data, or on
records this module's `depends` does not guarantee. No commit, no network, no
sleep, no HTTP and no tours.

## Names

Name each test method for the behaviour it pins (`test_total_includes_tax`, not
`test_1`). The port keeps the names, and a step checks by name that each one
arrived on the target. One file, `tests/test_<module>.py`, and a
`tests/__init__.py` that imports it.
