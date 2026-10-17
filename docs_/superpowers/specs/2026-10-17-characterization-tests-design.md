# Characterization tests on the source side

Status: design, approved in conversation on 2026-10-17; vocabulary section added the same day at the author's request. Not implemented.

A module that ships no tests is ported on faith: the only proof that the port
behaves like the original is the recipe the `understand` step built, replayed
once by `probe`, and then a test the agent writes **after** the port
(`write_test`) that asserts what the source version "did". That test has never
run on the source version.

This spec adds an optional step that writes the test on the **source** version
first, runs it there until it passes, and lets the port carry it to the target
version. A test that is green on the source is an oracle by construction; the
same assertions green on the target are the proof the port is faithful.

## What changes, in one paragraph

`understand` decides whether a module with no tests needs any, and raises a
flag. A new agent step, `characterize`, runs when that flag is raised: it
writes a test into the source module, reopens the source session and runs the test
to green. The source project is on a branch the app made for the migration, and
the test is committed there, so the source's own branch is never touched.
`port` then sees the tests as part of the source module's source and ports them.
`tests` on the target side finds them, so `probe` and `write_test` (both
`when: no_tests`) skip themselves.

## Vocabulary: `source` and `target`, not `old` and `new`

The two sides are named `source` and `target` everywhere a flow, a tool or a
person sees them. This lands **first**, as its own task and commit, before
anything below depends on it. The rest of this spec is written in the new
words, so `write_file` is `("target",)` and `reopen` is `("source", "target")`
once the rename is in.

What changes:

- Cartridge `sessions:` values: `source`, `target` (`SIDES`).
- The `side` argument of every tool, and `TOOL_SIDES`.
- Every prompt, tool description and run-log line that says "old" or "new"
  for a side, including `cartridges/stock.yaml`, `prompts/sheller.md` and
  `docs/runner-design.md`. `source(side='source', ...)` reads oddly, because
  `source` is also the name of the tool that reads code. It stays: renaming
  that tool is a larger decision and nobody has asked for it.
- Every `side` value in the code, with one rule: a side is `source` or
  `target`, never `old` or `new`. That covers `ctx.sessions`,
  `session_targets`, `module_root(..., side)`, `allowed_roots`,
  `relative_to_root`, the mounts snapshot, the contract's `containers` map,
  and the web layer's side values: the route
  `/migrations/{id}/containers/{side}/{action}`, the `provision` map the
  templates read, and the JSON that `describe_side` returns.
- New `ToolCall.side` rows record `source` / `target`.

What does **not** change, and why:

- There are no aliases and no compatibility. The tool is not deployed
  anywhere, so there is nothing to migrate and the decision is final. A flow
  that says `sessions: [old]` or `side: old` is refused like any other
  unknown session, and the error names the replacement ("unknown session
  'old'; the sides are 'source' and 'target'"). A person's local database
  holds no stored flow that says otherwise (the flow table is empty).
- `ToolCall.side` rows already in a local database keep their words and are
  shown as written; no code reads them back as a side.
- Database columns and form fields (`old_project_path`, `new_project_path`,
  `old_database`, `Module.old_path`, ...). The test for what is in scope is
  what the agent reads and writes: it never sees a column. Renaming them is a
  schema migration and a route change for no gain to the agent, and is not
  part of this work. If it is ever wanted it is its own change.

The point of the rename is that an agent never mistakes which side is which,
so it is held by tests rather than left to a grep:

- Every agent step's prompt opens with a fixed header that says what the two
  sides are for this run: *source* is the version the module is ported from
  (read-only, except for the characterization tests); *target* is the
  version it is ported to (the one that is written). The runner renders it,
  so a cartridge author cannot forget it and a fork cannot contradict it.
- A test renders every step of the stock flow and fails if a prompt, a tool
  description or a tool error names a side as `old` or `new`.
- A tool that is given a side the step did not declare answers with the
  sides it did declare, in these words.

## Non-goals

- Writing tests for a module that already has them. If the source module has any
  test class, nothing is written. This is enforced in code, not only asked of
  the model (see "The write").
- Editing any existing file of the source project. The write creates files in
  `<module>/tests/`, and only when that directory does not exist yet.
- Initialising git in a source project that is not a repository. The step is
  skipped with a reason.
- Taking commits back. `Discard` of a module's run leaves the characterization
  commit on the source branch: it is green on the source and still true of it.
- A per-module UI switch. The flow decides, as every other step does.

## Decisions taken in conversation

1. The source side was read-only to keep the agent from changing source code.
   A test file on a branch of its own does not defeat that, so the rule gets a
   narrow, explicit exception rather than being dropped.
2. The migration runs on a branch the app creates in the source project. The
   default branch stays clean.
3. If tests exist, none are written. If none exist, the model judges whether
   the module needs any; a module of data and views with no logic does not.

## The decision: `understand` raises `needs_tests`

`understand` gains `raises: [needs_tests]` and a paragraph in its prompt:

- Call `list_tests` on the source side first. If it lists any class, leave the
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
  sessions: [source]
  tools: [exec, source, run_test, list_tests, read_file, write_source_test, reopen]
  context: [rules, module_source, prior_answers]
  prompt: ...
```

The prompt asks for the smallest test that pins the behaviour `understand`
described, built from the recipe it proved, in the base class and with the
helpers the **source** version's own tests use (read one with `source`). Write
it, `reopen` the source session, `run_test` it, and fix it until it passes. A
test that cannot be made to pass on the source is not an oracle: say so and
finish. The runner then deletes what the step wrote (see `source_git.py`).

The step is not fatal when it ends without a passing test. The lap continues
as the stock flow does today: `probe` and `write_test` run because the target
side will still report `no_tests`.

## The write: a tool of its own

`write_file` is `("target",)` today. Adding `source` to it would let `port` and
`rewrite` (both declare `sessions: [source, target]` and `write_file`) write the
source, because the check is "side is in the tool's list and in the step's
sessions". The opt-in has to be a different tool.

- New tool `write_source_test`, `TOOL_SIDES["write_source_test"] = ("source",)`. Same
  arguments as `write_file` (`side`, `path`, `content`). A step is able to
  call it only when it names it in `tools` and opens an `source` session.
- The path resolves through the same host-path logic as `write_file`, then
  must lie **inside `<module>/tests/`** of the source module's directory
  (`module_root(migration, module, "source")`). Anything else is
  `ToolRefused`, not clamped.
- Refused when the source module **already had** tests when the step began: a
  `tests/` directory, or a `test_*.py` file anywhere in the module. The step
  context takes that snapshot once, before the first write, so a step can
  write several files (`tests/__init__.py`, `tests/test_x.py`) but cannot
  overwrite or extend a test that was there. The model cannot change this by
  ignoring the prompt.
- Create-only within the step too: a path the step already wrote may be
  written again (the model fixes its own test); a path that existed before
  the step is refused.
- Refused when the source project is not a git repository, or the migration's
  branch cannot be checked out (see below). The reason goes back to the model;
  nothing is written.
- The model writes `tests/__init__.py` with its own imports. The module's
  own `__init__.py` is never touched (Odoo discovers a `tests` package by
  itself).
- `reopen` becomes `("source", "target")`. Reopening the source side opens a fresh
  session with the same `allow_commit: false`; it is harmless to the steps
  that already list `reopen` with an source session.

## The branch: `source_git.py`

A sibling of `target_git.py`, same conventions (fixed author, `--no-verify`,
no signing, one lock, never a repository at or above the home directory):

- `ensure_branch(migration)` — on the first `write_source_test` of a migration:
  `git rev-parse --show-toplevel` in the source project; refuse if there is
  none or it is too broad. Name `migrator/<slug>-<id8>` (`slug` from the
  migration name, ASCII, checked with `git check-ref-format --branch`).
  The first write creates it from the current `HEAD` and moves the checkout
  onto it. After that the checkout is never moved back: a branch that exists
  but is not checked out means a person moved off it, perhaps after committing
  fixes, and switching back would put older code under the source container and
  every port after it. The write is refused with that explanation. A second
  migration from the same source project gets a branch of its own, and a write
  for the first while the second is checked out is refused the same way.
  Uncommitted work travels with the first switch and is never committed by us.
- `settle_tests(migration, module, job_id, green)` — after an agent step that
  declared `write_source_test` ends, ok or not. The step context records
  whether the tests the step wrote are green: `green` is true only when the
  last `run_test` on the source side after the last write was for this module
  (its spec is the module name or starts with `<module>.`), passed, and ran at
  least as many tests as the step wrote test methods. Another module's run, or
  a rerun of the one method just fixed, proves nothing.
  - green, and the checkout is on the migration's branch: `git add` the
    module's `tests/` (without `__pycache__`) and commit
    with that directory as the pathspec, so nothing else staged is swept in.
    The title is fixed, so a failure later can be traced to the run that
    wrote the test:

    ```
    odoo-migrator: characterization tests for <module> [sheller:<session12>]

    Job <job id>.
    Sheller-session: <whole session id>
    Sheller-command: <id of the green run_test>
    ```

    `<session12>` is the first twelve characters of the Sheller session the
    green `run_test` ran in; the body carries the whole id and the command,
    so the run can be found in Sheller's journal. The session's write key is
    never written: it is a secret, the id is not. The author is the
    migrator's fixed one (`odoo-migrator@localhost`).
  - `migrator_test_commits(directory)` lists such commits, newest first.
    A commit counts only when **both** the author and the title match: either
    alone is easy to write by hand, so a person who copies the title does not
    make the commit the migrator's.
  - not green (the step stopped, failed, or never got the test to pass), or the
    checkout is not on the migration's branch: the module's `tests/` directory
    is deleted and nothing is committed. If the directory cannot be deleted
    (a `__pycache__` made by the container) the log says so and names it. It did
    not exist before the step (the write is refused otherwise), so this
    removes only what the step made. A red test must not travel into the
    port and fail a lap on a claim nobody checked.
  - `None` when nothing was written.

The migration's source checkout stays on the migrator branch for as long as the
migration runs. This is the cost the person accepts: that directory is not
the place to work on the source's default branch meanwhile.

## `port` and `tests`

- `module_source` already reads the source module's directory, so `port` sees the
  new `tests/` files. Its prompt gains one paragraph: tests under `tests/` in
  the source module are characterization tests this run wrote and proved on the
  source version; port them like the rest and keep what they assert, changing
  only what the target version forces.
- The `tests` step is unchanged. It finds test classes on the target side, so the
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

- The source side stops being read-only only by this one tool, in one directory,
  creating files, on a branch the app made. `docs/security.md` and
  `docs/runner-design.md` ("the source container is read-only for the whole
  run") say so, and the override list in the design gets item 6.
- The Sheller session on the source side keeps `allow_commit: false`. The test
  runs in a rolled-back transaction as before. The database is not written.
- No path outside the source module's `tests/` can be written, and no existing
  file can be overwritten: a request to the tool cannot be widened by the
  model, the cartridge or the user's flow text.
- A forked flow that names `write_source_test` gets this power and no more.
  Validation already refuses an unknown tool; the tool's own checks are what
  bound it.

## Failure modes

| Case | What happens |
|---|---|
| Old module has tests | `understand` leaves the flag unset; if the model raises it anyway, `write_source_test` refuses |
| Old project not under git | the write is refused with the reason; the lap continues as today |
| Branch cannot be checked out | the write is refused with git's message |
| Test never goes green on the source side | the step ends; nothing claims coverage; `probe` and `write_test` run |
| Step stops or fails midway | `settle_tests` deletes the files the step created (not green); the source module is as it was |
| Runner dies between the write and `settle_tests` | a `tests/` directory is left uncommitted on the branch; the next run sees tests, writes none, and the port carries them. Start-up reconciliation is out of scope and the plan names it |
| `reopen` of the source session fails | the step ends as a tool error, as on the target side |

## Tests (no Docker, no Odoo, no daemon)

- `write_source_test`: writes under `tests/`; refuses `..`, a path outside
  `tests/`, a container path outside the mounts, an existing `tests/`, a
  non-repository; is not offered to a step that lacks it; `port`'s
  `write_file` still refuses `side=source`.
- `source_git`: branch created from HEAD and reused on a second call;
  default branch untouched; dirty work stays uncommitted; a green step is
  committed limited to its files, a red one is deleted and leaves the tree
  as it was; refuses a repository at the home directory.
- Cartridge validation: `needs_tests` raised and read; `write_source_test` needs
  an `source` session; `reopen` is valid with an `source` session.
- Stock flow: the native runner against the stub double, a module with no
  tests: `characterize` runs, `port` receives the files, `tests` finds
  classes, `probe` and `write_test` are skipped. A module with tests: no
  `characterize`. The double answers `list_tests` and `run_test` on the source
  session the way the real daemon does.
- Components page, UI guide and FAQ rows for anything a person sees (the
  step in the flow viewer, a run-log line for the branch and the commit).

## Docs

`docs/runner-design.md` (read-only line and override 6), `docs/security.md`,
`docs/architecture.md` (the step list), `docs/ui-guide.md`, `docs/faq.md` and
`docs/faq-ru.md` (why a branch appears in the source project), `README.md`
(`source_git.py`).
