# Documentation

Everything here assumes you've read the top-level [README](../README.md) —
install, quick start, the API table, and the security short version live
there. This folder goes deeper on the parts worth explaining once, properly.

| Document | What it covers |
|---|---|
| [architecture.md](architecture.md) | How odoo-migrator actually works: the app as source of truth, n8n as a cartridge, Sheller sessions, rules, diffs |
| [ui-guide.md](ui-guide.md) | Using the web UI screen by screen — Overview, Tree, Run, Settings — with every control and what it guarantees |
| [n8n.md](n8n.md) | The stock cartridge: compose pin, webhook contract, Wait/resume, wrappers, smoke-run |
| [security.md](security.md) | The actual security model: what's protected, what deliberately isn't, and why 8765 never leaves loopback |
| [faq.md](faq.md) | Plain-language questions and answers, in English |
| [faq-ru.md](faq-ru.md) | То же самое по-русски |

The design of record the sequential build was coded against is not this
folder. It lives under [`docs_/`](../docs_/superpowers/specs/2026-09-20-odoo-migrator-design.md)
together with the n8n workflow graph and the Task 3b spike findings.
These pages describe the software as it behaves. Where the spec and the
implementation plan disagreed, the plan said so under "Overrides vs the
written spec" — that list is the record, not a suggestion.

## Where to start

- **Just want to run it?** The [README](../README.md) is enough — install,
  start the app, open the browser.
- **Want to understand what you're running before you trust it with a
  database and a write to the new project?** [security.md](security.md),
  then [architecture.md](architecture.md).
- **Confused by something in the UI?** [ui-guide.md](ui-guide.md)
  documents every control; [faq.md](faq.md) / [faq-ru.md](faq-ru.md)
  answer the questions people actually ask.
- **Wiring n8n?** [n8n.md](n8n.md).
