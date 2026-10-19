# Cartridge reference

Every key a cartridge (a flow file, YAML) can carry, what it does, and what the
loader refuses. The stock file, `cartridges/stock.yaml`, is the worked example.
Why a cartridge is shaped this way is in [runner-design.md](runner-design.md);
this page is the lookup.

A test compares each table below with the code, so a key that is added or
removed there and not here fails the suite.

**Unknown keys are ignored, not refused.** A misspelt key (`contexts:`,
`session:`) does nothing and raises no error. After editing a file, open it on
the Flow page and check the step shows what you wrote.

## Top-level keys

| key | meaning |
|---|---|
| `cartridge` | the flow's id. Identity is this plus `version`. Saving from the viewer (Fork) accepts only letters, digits and `. _ + -`, 40 at most, starting with a letter or digit; an import does not check |
| `version` | the flow's version; Fork has the same alphabet rule. Quote it when it looks like a number: a bare `1.0` is a float, not a string |
| `schema` | the file format. This build implements `1`; any other is refused |
| `model` | the model the flow was written for, `provider` and `name`; see "Nested keys" and "Which model runs a step" |
| `pipeline` | the ordered list of steps of one lap |
| `on_failure` | what happens when a step fails: a list of steps, and where to resume |

## Nested keys

| key | meaning |
|---|---|
| `model.provider` | `anthropic`, `openai`, `cursor` or `local`. An unknown provider is refused |
| `model.name` | a model the provider serves. A name it does not serve is refused, with the ones it does |
| `budget.tool_calls` | on a step: most tool calls in one run of the step. Honoured only when lower than the ceiling, 60 |
| `budget.tokens` | on a step: most tokens. Honoured only when lower than the ceiling, 400000 |
| `on_failure.pipeline` | steps that run, in order, after a step of `pipeline` fails. They can carry `when` like any other |
| `on_failure.retry.resume_at` | the `id` (or kind, for a step without one) of a step of `pipeline`. After the lane, the lap restarts from the earlier of this step and the one that failed. A name that is not a step of `pipeline` is refused |

## Step keys

A step is one entry of `pipeline` or `on_failure.pipeline`.

| key | meaning |
|---|---|
| `step` | required. The kind: see "Step kinds". An unknown kind is refused |
| `id` | required on an `agent` step, optional on the others. The step's name on the Flow page, and the key its answer is stored under. Unique across the whole file; a repeat is refused. A step without one is named by its kind |
| `when` | optional. A flag name. The step runs only if that flag was raised earlier in the same lap; otherwise it is skipped. A flag nothing raises is refused |
| `sessions` | `agent` only. `source`, `target` or both: which containers the step may reach. Anything else is refused |
| `tools` | `agent` only. Names from the catalog, below. An unknown tool is refused, and so is one that none of the step's `sessions` allows |
| `context` | `agent` only. Names from "Context items". Rendered above the prompt in a fixed order, whatever order you list them in. An unknown item is refused |
| `raises` | `agent` only. The flags the step may set. They become the fields of its `report` tool. A flag raised here that no `when` reads, and no deterministic step reads, is refused |
| `prompt` | required, non-blank, on an `agent` step; forbidden on the others |
| `model` | optional, on an `agent` step. `provider` and `name`, as at the top level. Wins over everything else |
| `budget` | optional, on an `agent` step. `tool_calls` and `tokens`; see "Nested keys" |
| `fresh` | optional boolean. The module's directory on the target is emptied before the step runs. Only on an `agent` step whose `tools` include `write_file`; otherwise refused |

## Step kinds

| kind | what it is | flags |
|---|---|---|
| `agent` | one model turn-loop: the prompt, the tools and the context the step declares. Ends when the model calls `report` | raises what its `raises` lists |
| `lint` | no container. The module's Python parses, its XML parses, its manifest is a dict with `name` and `depends` | raises `lint_failed` |
| `upgrade` | installs or upgrades the module on the target container, then commits | none |
| `tests` | first checks, where the source's tests were written by the migrator, that each of those test methods is defined on the target; then runs the module's tests with Odoo's own runner | raises `no_tests` when the module declares none, `tests_failed` when a test failed or a characterization test is missing |
| `smoke` | no model, nothing written. On the target container: each model the module declares fields on gets a new record (defaults, and the module's fields computed) and its latest records read; each window action's domain is evaluated and searched; each report renders as HTML; each asset bundle the manifest adds to is compiled past the cache. Every button the module puts in a view is listed, and checked without pressing: its method exists, is public and takes no argument; its action exists. A break in the module's own fails the step; one in another module's, and checks that could not run, are findings | raises `has_buttons` when the module puts a button in a view |
| `verdict` | judges the lap against the migration's strictness | reads `behaviour_differs` |
| `ask_human` | parks the job and opens a checkpoint. The diagnosis in the last answer is the question | none |

A deterministic step (everything but `agent`) takes only `step`, `id` and
`when`. It has no prompt, no tools, no sessions.

## Context items

What is rendered above a step's prompt, each under a `## name` heading. The
vocabulary is closed: you pick from this list and never write an expression.

| item | what it puts in the prompt |
|---|---|
| `rules` | the rules stack for this migration: `rules/common.md`, then one file per version from the source's next to the target's. They describe what the target changes |
| `project_notes` | `AGENTS.md`, `CLAUDE.md` and READMEs of the source project, of each addons directory of the plan and of the module itself; clipped |
| `module_source` | the module's text files as they are on the source, code first; clipped |
| `prior_answers` | the answers earlier agent steps of this job gave, by step id |
| `previous_diff` | the module now, against the module as the job found it |
| `hand_changes` | what a person committed in the module's directory on the target since the app's last commit there; `(none)` when nothing |
| `last_failure` | the output of the step that failed: test report, traceback, lint findings |
| `test_guide` | `prompts/test_guide.md`: what to pin, what to assert on, how to write and name a test |

Every agent step also gets the standing prompt and the shell notes (fetched from the
daemon when the job starts, the same for every step of it), and a
"This run" block (the module, the step, which side is the source and which the
target, and the module's directory on each). None of that is set in the file.

## Tools

A step declares tool names; the catalog is code, and a file can pick from it and
never extend it. A tool is valid on a side only if both the tool and the step's
`sessions` allow it, and every call carries `side`. `*` marks a required
argument. `report`, which ends the step, is given to every agent step and is not
listed here.

| tool | sides | arguments | what it does |
|---|---|---|---|
| `exec` | source, target | `side*`, `code*` | run Python in the session's Odoo shell, `env` bound. Sheller |
| `source` | source, target | `side*`, `path`, `first`, `last`, `model`, `method`, `module` | read code in the container: a file or directory by `path`, or the code a model method runs by `model` and `method`. Sheller |
| `journal` | source, target | `side*` | what Odoo logged in the session. Sheller |
| `history` | source, target | `side*` | the commands the session has run. Sheller |
| `run_test` | source, target | `side*`, `test*`, `params` | run `module`, `module.Class` or `module.Class.test_method`; answers with counts and `failed`. `params` is Hoot's filter for a browser suite (`web.WebSuite.test_unit_desktop` with `@module`); a suite Odoo skipped answers `js_tests_skipped`. Sheller |
| `list_tests` | source, target | `side*`, `module` | the test classes a module declares on disk, each with a ready spec, and `js`: its browser test files, whether the manifest puts them in the run, the call that runs them, and whether this container can. Sheller |
| `test_result` | target | `side*` | the report of the last `run_test`. Held by the app |
| `interrupt` | source, target | `side*` | stop the command the session is running. Sheller |
| `rollback` | source, target | `side*` | discard what the session holds uncommitted. Sheller |
| `reopen` | source, target | `side*` | close the session and open a fresh one; needed after a file changes, because the running process does not re-import it |
| `read_file` | source, target | `side*`, `path*`, `first`, `last` | read a file of the project on the host disk, by container path or relative to the project root. Not Sheller |
| `write_file` | target | `side*`, `path*`, `content*` | write a file in the module's directory on the target. Not Sheller |
| `write_source_test` | source | `side*`, `path*`, `content*` | create a file under `tests/` of the module on the source, for a module that had no tests; see below. Not Sheller |

`read_file`, `write_file` and `write_source_test` touch the host disk, inside
the configured project roots; a path that escapes them is refused, not clamped.
`write_source_test` is held tighter still: only the module's `tests/` on the
source, only when the module had no test when the step began, on a branch of its
own in the source project. See [security.md](security.md).

## Flags

A flag is a name one step raises and another step's `when` reads. Nothing is
parsed out of prose; a step raises a flag by setting it in `report`. Flags are
collected per lap and start empty on each.

The built-in flags are raised, or read, by the deterministic steps:

| flag | who |
|---|---|
| `no_tests` | raised by `tests`: the module declares no test classes |
| `tests_failed` | raised by `tests` |
| `lint_failed` | raised by `lint` |
| `has_buttons` | raised by `smoke`: the module puts at least one button in a view, and the catalogue of them is ready for the next step |
| `behaviour_differs` | read by `verdict`: the lap fails at every strictness. A cartridge raises it from an agent step |

Two flags cannot be raised in one report: `small_fix` and `start_over`. They ask
for different repairs, and the report is refused with an explanation.

## Flags the stock cartridge declares

| flag | raised by | read by |
|---|---|---|
| `needs_tests` | `understand` | `characterize` |
| `behaviour_differs` | `probe` | `verdict` |
| `needs_human` | `diagnose` | `ask_human` |
| `reexamine_source` | `diagnose` | `reprobe` |
| `small_fix` | `diagnose` | `patch` |
| `start_over` | `diagnose` | `rewrite` |

## Which model runs a step

In this order, the first that applies: the step's own `model`; the migration
card's provider and model pair; the card's model alone, under the cartridge's
provider; the cartridge's `model`. Prompts are tuned against a model, so the
file names the one it was written for, and a card can still replace it.

## The lap

1. The steps of `pipeline` run in order. A step with a `when` whose flag is not
   in the lap's flags is skipped.
2. A step that fails runs the lane: each step of `on_failure.pipeline` in order,
   with its `when`.
3. The lap restarts from the earlier of `retry.resume_at` and the step that
   failed, with the flags cleared. `retry_limit` on the migration card bounds
   the laps, and a retry that would repeat the failure it just had stops.

## A smallest cartridge

```yaml
cartridge: mine
version: "1.0.0"
schema: 1
model: {provider: anthropic, name: claude-sonnet-5-5}
pipeline:
  - step: agent
    id: port
    sessions: [source, target]
    tools: [exec, source, read_file, write_file]
    context: [rules, module_source]
    prompt: |
      Port the module to the target version.
  - step: lint
  - step: upgrade
  - step: tests
  - step: verdict
on_failure:
  pipeline: []
  retry:
    resume_at: lint
```
