# Shell guidance from the daemon

The migrator's side of
[the Sheller spec](2026-10-17-sheller-help-route-and-js-tests.md), which is
implemented (Sheller 1.11.0: `GET /api/help`, `js_tests`, `js` in the test
listing, a skipped browser suite is a failure).

## Problem

The agent is told what the Odoo shell can do by `prompts/sheller.md`, eight
topics of Sheller's `os_help` copied by hand at 1.10.0 with the tool names
rewritten. Sheller improves a topic, the agent never learns of it. A test next
door notices, but only for a developer who has the sibling checkout.

## Design

**Fetch, don't copy.** `GET /api/help` returns every topic with unresolved
markers (`{{run_test}}`). A job asks for it when it starts, the text is fixed
for the job, and the job records its hash.

When: once per job, in the runner thread that builds the `StepContext`; not
per step, not per module import, not per batch. The answer is cached in the
process by the daemon's `version` (from `/health`), so a batch of three
modules asks once, a Sheller upgrade is noticed by the next job, and a
restart of this app asks again. A job keeps the text it started with even if
Sheller is upgraded halfway.

**Three kinds of topic**, decided in code (`odoo_migrator/shell_guidance.py`):

| kind | topics | what happens |
|---|---|---|
| not ours | `sessions`, `ownership`, `commit`, `remote_server`, `watching`, and every `odoo_<major>` | left out. The runner opens, owns and commits sessions; `rules/<version>.md` already says what a version changes |
| owned | `tests`, `modules` | the migrator's own text replaces the daemon's (`prompts/shell_overlay/<topic>.md`): this app runs a test in the session a step holds and waits, and has `reopen`, which the daemon's text reaches by other tools. Each overlay records the sha256 of the daemon text it was written against; a different upstream hash is a `warn` line in the job's log, so a changed topic is seen instead of silently shadowed |
| taken | everything else, including a topic Sheller adds tomorrow | the daemon's text, markers replaced by this app's tool names |

**Markers.** `exec source journal history run_test test_result list_tests
interrupt rollback` map to the tool of the same name. A call `{{help}}('x')`
becomes "the notes on x". Any other marker (`open_session`, `commit`,
`session`, ...) names something the agent does not have: a taken topic that
carries one is left out and the log says which, so a new topic never ships a
dangling tool name.

**Fallback.** No `/api/help` (a Sheller older than 1.11.0), no answer, an
empty answer: `prompts/sheller.md` as it is now, and a `warn` line says so.
The file stays complete and stays the fallback; a test keeps the two overlay
files identical to their sections in it.

**Record.** `Job.shell_notes_hash` (sha256 of the assembled text) and
`Job.shell_notes_source` (`daemon 1.11.0` or `vendored`). One `info` line at
job start: source, number of topics, hash, which are overlaid and which were
left out.

## JS tests

Sheller can run a Hoot suite through `run_test` with `params`. The agent gets
it:

- `run_test` takes an optional `params`, passed through to the daemon.
- `list_tests` answers as before and now carries the daemon's `js` (files,
  `in_manifest`, the ready `run`) and, in it, `available` and `reason` from
  the session's `js_tests`. An agent knows before it spends a run whether
  Chromium is there.
- The `tests` overlay says: a skipped browser suite is a failure, never a
  pass, `hoot.failed_tests` are ids to rerun with `params`, a file outside
  `in_manifest` is not in the run.

Not in scope: a JS characterization test (a Hoot test written on the source
and ported). It needs a way to write a JS file on the source, its own guard,
and a prompt. It gets its own design once this is used on real modules.

## Tasks

1. The client: `help()` on the protocol, the HTTP client and the fake, which
   answers in the daemon's shape.
2. `shell_guidance.py`: assemble, cache by version, fall back; overlays and
   their drift check.
3. The job: fields on `StepContext` and `Job`, `system_prompt` reads the
   snapshot, the log lines.
4. `tests/test_shell_notes.py` follows Sheller's move of `HELP`; the old
   drift test becomes a check of the overlays' recorded hashes against the
   sibling.
5. JS: `params`, the `js` merge, the overlay paragraph; the cartridge
   reference's tool table.
6. Docs: `AGENTS.md`, `README.md`, `docs/architecture.md`, `docs/security.md`
   if it says the guidance is vendored.
