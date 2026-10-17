# Characterization tests on the source side

Status: design, approved in conversation on 2026-10-17. Not implemented.

A module that ships no tests is ported on faith: the only proof that the port
behaves like the original is the recipe the `understand` step built, replayed
once by `probe`, and then a test the agent writes **after** the port
(`write_test`) that asserts what the old version "did". That test has never
run on the old version.

This spec adds an optional step that writes the test on the **old** version
first, runs it there until it passes, and lets the port carry it to the new
version. A test that is green on the source is an oracle by construction; the
same assertions green on the target are the proof the port is faithful.

## What changes, in one paragraph

`understand` decides whether a module with no tests needs any, and raises a
flag. A new agent step, `characterize`, runs when that flag is raised: it
writes a test into the old module, reopens the old session and runs the test
to green. The old project is on a branch the app made for the migration, and
the test is committed there, so the source's own branch is never touched.
`port` then sees the tests as part of the old module's source and ports them.
`tests` on the new side finds them, so `probe` and `write_test` (both
`when: no_tests`) skip themselves.

## Non-goals

- Writing tests for a module that already has them. If the old module has any
  test class, nothing is written. This is enforced in code, not only asked of
  the model (see "The write").
- Editing any existing file of the old project. The write creates files in
  `<module>/tests/`, and only when that directory does not exist yet.
- Initialising git in a source project that is not a repository. The step is
  skipped with a reason.
- Taking commits back. `Discard` of a module's run leaves the characterization
  commit on the old branch: it is green on the source and still true of it.
- A per-module UI switch. The flow decides, as every other step does.

## Decisions taken in conversation

1. The source side was read-only to keep the agent from changing source code.
   A test file on a branch of its own does not defeat that, so the rule gets a
   narrow, explicit exception rather than being dropped.
2. The migration runs on a branch the app creates in the old project. The
   default branch stays clean.
3. If tests exist, none are written. If none exist, the model judges whether
   the module needs any; a module of data and views with no logic does not.

## The decision: `understand` raises `needs_tests`

`understand` gains `raises: [needs_tests]` and a paragraph in its prompt:

- Call `list_tests` on the old side first. If it lists any class, leave the
  flag unset and do not mention tests.
- With no tests, raise `needs_tests` when the module has Python logic worth
  pinning (methods, computed fields, constraints, onchanges, workflow
  transitions). Leave it unset for a module that is data, views, security
  files or translations only, and say so in the answer.

A raised flag must be read by a `when`, and `characterize` reads it
(`cartridge.py` refuses a flag that is raised and never read).

## The step: `characterize`

Placed after `understand` and before `port`:

```yaml
- step: agent
  id: characterize
  when: needs_tests
  sessions: [old]
  tools: [exec, source, run_test, list_tests, read_file, write_old_test, reopen]
  context: [rules, module_source, prior_answers]
  prompt: ...
```

The prompt asks for the smallest test that pins the behaviour `understand`
described, built from the recipe it proved, in the base class and with the
helpers the **old** version's own tests use (read one with `source`). Write
it, `reopen` the old session, `run_test` it, and fix it until it passes. A
test that cannot be made to pass on the source is not an oracle: say so and
finish. The runner then deletes what the step wrote (see `source_git.py`).

The step is not fatal when it ends without a passing test. The lap continues
as the stock flow does today: `probe` and `write_test` run because the new
side will still report `no_tests`.

## The write: a tool of its own

`write_file` is `("new",)` today. Adding `old` to it would let `port` and
`rewrite` (both declare `sessions: [old, new]` and `write_file`) write the
source, because the check is "side is in the tool's list and in the step's
sessions". The opt-in has to be a different tool.

- New tool `write_old_test`, `TOOL_SIDES["write_old_test"] = ("old",)`. Same
  arguments as `write_file` (`side`, `path`, `content`). A step is able to
  call it only when it names it in `tools` and opens an `old` session.
- The path resolves through the same host-path logic as `write_file`, then
  must lie **inside `<module>/tests/`** of the old module's directory
  (`module_root(migration, module, "old")`). Anything else is
  `ToolRefused`, not clamped.
- Refused when the old module **already had** tests when the step began: a
  `tests/` directory, or a `test_*.py` file anywhere in the module. The step
  context takes that snapshot once, before the first write, so a step can
  write several files (`tests/__init__.py`, `tests/test_x.py`) but cannot
  overwrite or extend a test that was there. The model cannot change this by
  ignoring the prompt.
- Create-only within the step too: a path the step already wrote may be
  written again (the model fixes its own test); a path that existed before
  the step is refused.
- Refused when the old project is not a git repository, or the migration's
  branch cannot be checked out (see below). The reason goes back to the model;
  nothing is written.
- The model writes `tests/__init__.py` with its own imports. The module's
  own `__init__.py` is never touched (Odoo discovers a `tests` package by
  itself).
- `reopen` becomes `("old", "new")`. Reopening the old side opens a fresh
  session with the same `allow_commit: false`; it is harmless to the steps
  that already list `reopen` with an old session.

## The branch: `source_git.py`

A sibling of `target_git.py`, same conventions (fixed author, `--no-verify`,
no signing, one lock, never a repository at or above the home directory):

- `ensure_branch(migration)` — on the first `write_old_test` of a migration:
  `git rev-parse --show-toplevel` in the old project; refuse if there is
  none or it is too broad. Name `migrator/<slug>-<id8>` (`slug` from the
  migration name, ASCII, checked with `git check-ref-format --branch`).
  Create it from the current `HEAD` and check it out if it is not already
  the current branch. Uncommitted work in the old checkout travels with the
  switch and is never committed by us. A switch git refuses (a conflicting
  local change) refuses the write, with git's words.
- `settle_tests(module, job_id, green)` — after an agent step that declared
  `write_old_test` ends, ok or not. The runner knows the files the step
  created and reads the step's recorded tool calls: `green` is true only when
  the last `run_test` on the old side after the last write passed.
  - green: `git add` those files and commit
    `odoo-migrator: characterization tests for <module>` with the module's
    `tests/` as the pathspec, so nothing else staged is swept in.
  - not green (the step stopped, failed, or never got the test to pass): the
    files the step created are deleted and nothing is committed. A red test
    must not travel into the port and fail a lap on a claim nobody checked.
  - `None` when nothing was written.

The migration's old checkout stays on the migrator branch for as long as the
migration runs. This is the cost the person accepts: that directory is not
the place to work on the source's default branch meanwhile.

## `port` and `tests`

- `module_source` already reads the old module's directory, so `port` sees the
  new `tests/` files. Its prompt gains one paragraph: tests under `tests/` in
  the old module are characterization tests this run wrote and proved on the
  old version; port them like the rest and keep what they assert, changing
  only what the target version forces.
- The `tests` step is unchanged. It finds test classes on the new side, so the
  `no_tests` flag stays unset and `probe` / `write_test` / `retest` skip.
- `commit_module` commits the ported tests with the port, as it commits any
  file of the module.

## The flow file

`cartridges/stock.yaml` gains `characterize` and the `needs_tests` raise. The
version stays `1.0.0`: the shipped file is the only thing `flow_text` resolves
for `stock`, a bump would orphan every migration pinned to `1.0.0`, and the
native runner has not run live yet (no job has recorded a sha that would
silently change meaning). **Ruling to confirm at review.**

## Security

- The old side stops being read-only only by this one tool, in one directory,
  creating files, on a branch the app made. `docs/security.md` and
  `docs/runner-design.md` ("the old container is read-only for the whole
  run") say so, and the override list in the design gets item 6.
- The Sheller session on the old side keeps `allow_commit: false`. The test
  runs in a rolled-back transaction as before. The database is not written.
- No path outside the old module's `tests/` can be written, and no existing
  file can be overwritten: a request to the tool cannot be widened by the
  model, the cartridge or the user's flow text.
- A forked flow that names `write_old_test` gets this power and no more.
  Validation already refuses an unknown tool; the tool's own checks are what
  bound it.

## Failure modes

| Case | What happens |
|---|---|
| Old module has tests | `understand` leaves the flag unset; if the model raises it anyway, `write_old_test` refuses |
| Old project not under git | the write is refused with the reason; the lap continues as today |
| Branch cannot be checked out | the write is refused with git's message |
| Test never goes green on the old side | the step ends; nothing claims coverage; `probe` and `write_test` run |
| Step stops or fails midway | `settle_tests` deletes the files the step created (not green); the old module is as it was |
| Runner dies between the write and `settle_tests` | a `tests/` directory is left uncommitted on the branch; the next run sees tests, writes none, and the port carries them. Start-up reconciliation is out of scope and the plan names it |
| `reopen` of the old session fails | the step ends as a tool error, as on the new side |

## Tests (no Docker, no Odoo, no daemon)

- `write_old_test`: writes under `tests/`; refuses `..`, a path outside
  `tests/`, a container path outside the mounts, an existing `tests/`, a
  non-repository; is not offered to a step that lacks it; `port`'s
  `write_file` still refuses `side=old`.
- `source_git`: branch created from HEAD and reused on a second call;
  default branch untouched; dirty work stays uncommitted; a green step is
  committed limited to its files, a red one is deleted and leaves the tree
  as it was; refuses a repository at the home directory.
- Cartridge validation: `needs_tests` raised and read; `write_old_test` needs
  an `old` session; `reopen` is valid with an `old` session.
- Stock flow: the native runner against the stub double, a module with no
  tests: `characterize` runs, `port` receives the files, `tests` finds
  classes, `probe` and `write_test` are skipped. A module with tests: no
  `characterize`. The double answers `list_tests` and `run_test` on the old
  session the way the real daemon does.
- Components page, UI guide and FAQ rows for anything a person sees (the
  step in the flow viewer, a run-log line for the branch and the commit).

## Docs

`docs/runner-design.md` (read-only line and override 6), `docs/security.md`,
`docs/architecture.md` (the step list), `docs/ui-guide.md`, `docs/faq.md` and
`docs/faq-ru.md` (why a branch appears in the source project), `README.md`
(`source_git.py`).
