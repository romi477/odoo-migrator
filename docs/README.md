# Documentation

Everything here assumes you've read the top-level [README](../README.md) —
install, quick start, the API table, and the security short version live
there. This folder goes deeper on the parts worth explaining once.

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | The app as source of truth, the YAML cartridge, providers, Sheller sessions, rules, diffs |
| [ui-guide.md](ui-guide.md) | The screens — Overview, Tree, Pipeline, Flow, Sheller, Result, Settings — and what each control guarantees |
| [security.md](security.md) | What is protected, what deliberately isn't, and why 8765 never leaves loopback |
| [faq.md](faq.md) | Plain-language questions and answers, in English |
| [faq-ru.md](faq-ru.md) | То же самое по-русски |

The design of record for the runner is
[runner-design.md](runner-design.md).
These pages describe the software as it behaves.

## Where to start

- **Just want to run it?** The [README](../README.md).
- **Want to understand what you're running before you trust it with a
  database and a write to the new project?** [security.md](security.md),
  then [architecture.md](architecture.md).
- **Confused by something in the UI?** [ui-guide.md](ui-guide.md).
  [faq.md](faq.md) / [faq-ru.md](faq-ru.md) answer the questions people
  actually ask.
