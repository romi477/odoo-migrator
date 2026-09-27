# rules/

The version knowledge a migrating agent is given. Prose, not code.

**This file is not part of it.** `odoo_migrator/rules.py` assembles a stack
out of `common.md` and `<major>.md` only, so `AGENTS.md` is never sent to a
model running a migration. It is for whoever edits the others — a person, or
an agent working in this directory.

## What is here

| File | Sent | Holds |
|---|---|---|
| `common.md` | once, in front of the stack | what holds across this directory, and the reading order the agent is given |
| `<major>.md` | the source file, then every later one through the target | what landing on that version requires, as a delta on the file before it |

16.0 → 19.0 sends five documents: `common`, `16.0` (source), `17.0` and
`18.0` (intermediate), `19.0` (target). There is no pair document and there
will not be one — pairs grow as the square of the versions, and the stack is
what makes a multi-version jump possible at all.

A new major is a file and nothing else. No registry, no list to extend:
19.0 → 20.0 started working the moment `20.0.md` existed.

## Where a fact goes

- The file for the version that **introduces** it. `aggregator` is in
  `18.0.md` and not repeated in 19.0 and 20.0.
- A later file replaces a point an earlier one made. Name what it replaces:
  the agent is told the later file wins, and it can only act on that if the
  replacement is visible. "`group_operator` is now `aggregator`", not "use
  `aggregator`".
- What a version still *is* belongs in that version's file too. `16.0.md` is
  mostly "this is what the module you are reading looks like"; the source
  file is not something to satisfy.
- `common.md` takes what spans the directory, and the index of what changes
  where. Its table is an index, not the instruction: a row says a spelling
  changes at 18.0, and `18.0.md` says what to write. Add the row when you add
  the rule, or a stack that skips the middle files still names the topic.
- Never a minor (`17.2.md`). `_parse` refuses those pairs outright, so the
  file would sit here unread.

## How to write one

- A name that is not in these files is not a fact. That is what the agent is
  told, so a rule here has to earn it: check it against the target version's
  own code (`source` on the new container) or the release changelog before
  writing it, and say so where you could not.
- Lines that change a decision. Background costs tokens on every step that
  carries rules, and 17.0 → 20.0 is already around 5k tokens of stack.
- Say what breaks, not only what changed. "A global `product_uom_id` →
  `uom_id` replace breaks `sale.order.line`" is worth more than the rename
  on its own.
- Quote the message the version actually emits, in full. That string is what
  the agent matches against the journal and the upgrade log, and a paraphrase
  matches nothing.
- Say whether the old spelling still runs. "Gone" and "deprecated, warns, and
  still works" lead to different ports, and the second one is not worth a
  failed lap.
- Headings are yours. `runner/prompting.py` delimits each document by file
  name, so a `##` cannot leak into the neighbouring version the way it did
  when the wrapper was itself a heading.

## Promoted findings

A checkpoint can promote a finding into a version file. It lands appended at
the bottom, under an HTML comment naming the module, the date and the job.
Curate it into the right section and keep that comment: it is the only
record of which run produced the paragraph. Promote does not write to
`common.md`.

## Tests

`tests/test_rules.py` covers assembly and the shipped stacks with it. Run it
after editing here.
