# Sheller: guidance over HTTP, and JS tests an HTTP client can plan around

Written for the agent that implements this **in the odoo-sheller repository**
(`/opt/projects/odoo-sheller`). It is self-contained: you do not need the
odoo-migrator checkout. Everything the migrator will do with it afterwards is
in "What the migrator does next", for context only; none of that is yours.

Read first: `odoo_sheller/mcp.py` (`INSTRUCTIONS`, `HELP`, `HELP_ALIASES`,
`_rules_topic`, `_with_rules`), `odoo_sheller/rules/`, `odoo_sheller/api.py`
(`/health`, `container_tests`, `run_test`), `odoo_sheller/discovery.py`
(`list_tests`, `PROBE_SOURCE`), `docs/container.md`, `docs/agent-guide.md`
("Hoot, headless"), `CHANGELOG.md` 1.10.0.

## Context

Odoo Migrator is an HTTP client of the daemon. It is not an MCP client. It
gives its own agent tools that call the daemon's routes (`exec`, `source`,
`run_test`, `list_tests`, `journal`, ...). Two consequences, one problem each.

**1. The agent never sees Sheller's guidance.** What the daemon tells an MCP
client — `INSTRUCTIONS`, the topics behind `os_help`, the per-major rules — is
only reachable through the MCP surface. The migrator vendored eight topics into
a prompt file by hand, at 1.10.0, and rewrote the tool names. When Sheller
improves a topic, the migrator's agent does not learn it. A test next door
compares hashes and tells a developer; it tells the running agent nothing.

**2. JS tests are runnable and not plannable.** Since 1.10.0 `POST
/api/sessions/{id}/run_test` takes `params` and answers with `hoot`. An HTTP
client can already run a Hoot suite, but it cannot find out, before spending a
run, (a) whether the container can run one at all, (b) which JS test files a
module ships and what filter selects them, or (c) whether a run that "passed"
ran nothing because Chromium is missing. Odoo *skips* the test in that case,
which reads as a pass.

## Goals

1. `GET /api/help`: the guidance the MCP surface carries, served as data, with
   tool names left to the caller.
2. JS tests become discoverable and honest over the same HTTP routes: a
   capability field, a JS section in the existing test listing, and a
   structured "skipped" in the run answer.

## Non-goals

- No change to the MCP surface as a client sees it. `os_help`, the
  instructions and every tool description read **byte for byte** as before
  (see "Golden test").
- No new session type, no auth change. The routes below are read-only, need no
  session key and no admin key, and bind to the same `127.0.0.1` as everything
  else.
- QUnit on 15 to 17 is not driven here. The listing reports it; running it is
  out of scope. Hoot on 18 and 19 is the supported case; 20 is "as verified"
  (see the existing note in `docs/agent-guide.md`) and reports as such.
- No JS authoring help (how to write a Hoot test). The existing `odoo_<major>`
  rules and `tests` topic stay what they are.

## Part A. `GET /api/help`

### Contract

`GET /api/help` answers:

```json
{
  "version": "1.12.0",
  "instructions": "odoo-sheller runs Python in a live Odoo shell. ...",
  "topics": {
    "limits":   {"text": "...", "hash": "<sha256 of text>"},
    "orm":      {"text": "...", "hash": "..."},
    "odoo_17":  {"text": "...", "hash": "..."}
  },
  "markers": ["exec", "source", "journal", "history", "run_test", "test_result",
              "list_tests", "open_session", "close_session", "commit",
              "rollback", "interrupt", "session", "attach_session", "help"]
}
```

- `version` is `running_version`, the same value `/health` reports. A client
  uses it as a cache key.
- `instructions` is `INSTRUCTIONS`. `topics` holds **every** key of `HELP`
  plus one `odoo_<major>` entry per file in `odoo_sheller/rules/` (the text
  `rules.for_major(major)` returns, `common.md` included). Aliases
  (`HELP_ALIASES`) are not listed, as in `os_help()` today.
- `GET /api/help/{topic}` answers `{"topic", "text", "hash"}`, or 404
  `{"error": "no_such_topic", "topic", "topics": [...]}` — the same shape and
  recovery wording `os_help` uses. An alias resolves.
- Texts are served **with markers unresolved** (next section). `markers`
  lists the names a client may substitute. A client that substitutes nothing
  still gets readable text.
- `hash` is SHA-256 of the served text, hex. It lets a client record exactly
  what its agent was told without storing the text.
- No state: no session, no `_rules_given` bookkeeping. `/api/help` always
  carries the full text of every topic, whatever an MCP server has already
  handed over. (`_with_rules` and its once-per-major pointer stay exclusive to
  the MCP session open.)
- Size: the whole body is a few tens of kilobytes. No pagination.

### Markers: names of tools are the caller's

Today the topics name MCP tools: `os_source`, `os_run_test`, `os_exec`,
`os_help('code')`. An HTTP client has different verbs, so the text must not
hard-code them.

- In the source of `HELP`, `INSTRUCTIONS` and the rules files, write a tool as
  `{{name}}`, where `name` is one of the `markers` above. `os_run_test(` is
  written `{{run_test}}(`; `os_help('code')` is `{{help}}('code')`.
- One function, `render_for_mcp(text)`, replaces `{{name}}` with `os_name`
  (`{{session}}` → `os_session`, and so on). Every place that serves a topic
  to MCP — `os_help`, `INSTRUCTIONS` as passed to `MCPServer`, `_with_rules` —
  goes through it.
- `GET /api/help` returns the text with `{{name}}` intact.
- A name outside `markers` is a bug: a unit test scans every text for `{{...}}`
  and fails on an unknown name. A second test scans the **rendered MCP** text
  for the bare `{{` and fails if any survive.
- Prose that names a tool in passing ("use os_source, not docker exec") is
  rewritten to the marker too. Do not leave a few `os_` literals behind: the
  test that scans the served HTTP text for `os_` (must find none, except as
  part of a longer word such as `os_help` inside an example the topic is
  *about*; there are none today) is what keeps this honest.

### Golden test

Before touching any text, record `INSTRUCTIONS` and every `HELP` topic and
every `odoo_<major>` text as they are now (a fixture under `tests/`, or a
sha256 table in the test). After the marker conversion, the MCP rendering must
equal that record exactly. This is the proof that no MCP client sees a change.
Update the record only when a topic's *wording* changes on purpose, in a later
commit of its own.

### Which topics describe a flow an HTTP client does not have

`sessions`, `ownership`, `commit`, `remote_server`, `watching` are about the
MCP client opening, handing over and committing sessions. They are served like
any other, because the daemon should not decide what a client wants. Do not
flag or hide them. The client chooses.

## Part B. JS tests over HTTP

### B1. A capability on the session and on the container

Whether a container can run a browser suite is a property of its Python, not
of a run. Report it.

- `GET /api/sessions/{id}` (`Session.describe()`) gains
  `js_tests`:

  ```json
  "js_tests": {
    "runner": "hoot",
    "available": true,
    "reason": null,
    "tested": "18-19"
  }
  ```

  - `runner`: `"hoot"` on 18 to 20, `"qunit"` on 15 to 17 (information only),
    `null` when the major is unknown.
  - `available`: `true` only when the process can actually run the suite
    here: runner is `hoot`, a Chromium/Chrome executable is found the way
    Odoo's own `ChromeBrowser` looks for one, and `websocket` (the
    `websocket-client` package) imports. `false` otherwise, always with a
    `reason` from a closed list: `"runner_not_supported"` (qunit, 15 to 17),
    `"chrome_not_found"`, `"websocket_client_missing"`.
  - `tested`: how far this daemon's own verification goes, so a client can
    word its confidence: `"18-19"` for 18 and 19, `"unverified"` for 20 (the
    `docs/agent-guide.md` note says `test_unit_desktop` there is read from
    source, not run), `null` otherwise.
- It is computed once, in the session's bootstrap (the process is already
  there and already imports Odoo; the check is a `shutil.which`-style lookup
  and one `import`). It does not start a browser. If the lookup fails for an
  unrelated reason, report `available: false, reason: "probe_failed"` and log
  the cause; never raise out of `describe()`.
- `GET /api/containers/{container}/tests` is the only container-level read a
  client makes before a session exists, but it opens none. Do not add a probe
  there. The capability is session-level; a client opens its session first, as
  it does anyway.
- The MCP side may expose the same field in `os_session`'s answer. Optional.

### B2. JS tests in the existing listing

`GET /api/containers/{container}/tests?module=<name>` answers today with
`{"module", "path", "classes"}`, Python test classes read from disk with a
ready spec for each. Add a sibling key, `js`, in the same call:

```json
"js": {
  "runner": "hoot",
  "files": [
    {
      "path": "static/tests/product_tour.test.js",
      "in_manifest": true,
      "tests": ["renders the badge", "keeps the order on sort"]
    }
  ],
  "run": {
    "test": "web.WebSuite.test_unit_desktop",
    "params": "@my_module"
  }
}
```

- `files`: JS test files of the module on disk. For Hoot (18 and later) the
  convention is `static/tests/**/*.test.js`; for the legacy QUnit layout
  (15 to 17) it is `static/tests/**/*.js` excluding `tours/` and `helpers/`.
  Use the major of the container (the listing already reaches it) to choose.
  When nothing matches, `files` is `[]` and `run` is `null`.
- `in_manifest`: the file is covered by an `assets` entry of the manifest —
  under `web.assets_unit_tests` for Hoot, `web.qunit_suite_tests` for QUnit —
  by literal path or by glob. This is the existing gotcha: a Hoot file that
  is not listed is not in the run, and the run reports success over fewer
  tests than the module has. Read the manifest as text with `ast.literal_eval`
  of its dict, as the PROBE already reads files; never import the module.
  An unreadable or non-literal manifest reports `in_manifest: null`.
- `tests`: best-effort names, the first string argument of `test(`, `it(` and
  `describe(` calls found by a plain regex over the file. They are for a human
  and for choosing a filter, not an oracle; a name built at run time is
  missed. Say so in the topic text. The ids Hoot itself reports in
  `hoot.failed_tests` stay the authority for a rerun.
- `run`: the call an HTTP client makes, ready: the Python test that drives
  Hoot and the filter for this module (`@<module>`), exactly the form
  `docs/agent-guide.md` documents. `null` when the runner is not `hoot` or
  there are no files.
- Existing keys and the 400/404/502 behaviour of the route are unchanged. A
  module without JS tests answers `"js": {"runner": ..., "files": [], "run":
  null}`; a Sheller older than this change answers without `js`, and the
  client must treat absence as "unknown".

### B3. A run that was skipped says so

When `run_test` runs `web.WebSuite.test_unit_desktop` (or any spec with
`params`) and Odoo *skips* it — `Chrome executable not found`,
`websocket-client module is not installed` — the answer today is a success
whose counts show a skip only to someone who reads the log. Add to the answer
(the dict `Session.run_test` returns, and so `test_result`, the journal's
test entry, and the MCP shaping that already copies `hoot`):

```json
"hoot": {"skipped": true, "reason": "chrome_not_found"}
```

- Set `skipped: true` when the run's log carries Odoo's skip of that test
  with either message, and put the matching `reason` from the same closed list
  as B1 (`chrome_not_found`, `websocket_client_missing`).
- A skipped run is **not** `success`. Set `success: false` and add
  `"error": "js_tests_skipped"` alongside the reason, so a client that checks
  only `success` and `error` cannot mistake a skip for a pass. This is the one
  behaviour change in this spec and the reason for the CHANGELOG entry.
- A Hoot run that executed has `hoot.skipped` absent (not `false`), as `hoot`
  itself is absent on a non-browser run today.

## Part C. Documentation

- `docs/container.md`: a new section "Guidance over HTTP" with the `/api/help`
  contract and the marker rule, and the new fields of `GET
  /api/sessions/{id}`, `GET /api/containers/{container}/tests` and the run
  answer. This document is what a client program is coded against.
- `docs/agent-guide.md`: in "Hoot, headless", the skip behaviour of B3 and the
  `js_tests` capability. In `HELP['tests']`, a paragraph that an MCP agent
  can see the same facts in `os_session` if you chose to expose B1 there.
- `CHANGELOG.md`, under `[Unreleased]`: Added `GET /api/help`; Added
  `js_tests` on a session; Added `js` in the module test listing; **Changed**
  a skipped browser suite is a failure with `js_tests_skipped`. Nothing is
  removed from the API or the MCP surface.
- `AGENTS.md` of Sheller, if it lists the routes, gets the new one.
- Bump to the next minor in `pyproject.toml` only when releasing; do not bump
  as part of this work unless that is how releases are cut here.

## Tests

Follow the repo's conventions (pytest, the existing doubles, `tests/test_mcp.py`
for the MCP surface, `tests/test_api.py` for routes, `tests/test_rules.py`).
Name each new test after the behaviour. Write each test first and watch it
fail.

1. **Golden**: MCP rendering of `INSTRUCTIONS`, every `HELP` topic, every
   `odoo_<major>`, equals the record taken before the change.
2. **Markers**: every `{{name}}` in any served text is in `markers`; none
   survives MCP rendering; the HTTP text contains no `os_` tool name.
3. **`/api/help`**: lists every `HELP` key and every rules file, no alias;
   `hash` equals the SHA-256 of `text`; `version` equals `/health`'s; an
   unknown topic is 404 with the topic list; an alias resolves; the route needs
   no session key.
4. **`/api/help` is stateless**: ask twice, plus an MCP session open between
   them; the second answer still carries the full `odoo_17` text.
5. **Capability**: with Chromium and `websocket` present (the bootstrap
   harness), `available: true`; with each missing in turn, `false` with the
   right `reason`; on a QUnit major, `runner_not_supported`; a failing probe
   never makes `describe()` raise.
6. **JS listing**: a module with `static/tests/a.test.js` listed under
   `web.assets_unit_tests` reports `in_manifest: true`; the same file absent
   from the manifest reports `false`; a glob entry covers it; a non-literal
   manifest reports `null`; a module without JS tests answers `files: []`,
   `run: null`; a Python-only module's `classes` is unchanged.
7. **Skip**: a run whose log has Odoo's Chrome-not-found skip answers
   `success: false`, `error: js_tests_skipped`, `hoot.skipped: true`,
   `reason: chrome_not_found`; the same for the websocket message; a Hoot run
   that executed has no `hoot.skipped`. `test_result` and the journal entry
   carry it too.
8. **Docs**: `tests/test_docs.py` already ties documents to code; extend it
   if it covers route lists.

## Review focus

Inputs the tests above do not make obvious, most likely first:

- A topic that quotes a tool with an `os_` prefix in a code sample the marker
  scan misses because it sits inside a Python string in an example.
- `odoo_<major>` text served over HTTP when `common.md` itself mentions a
  tool: the marker conversion must reach the rules files too, not only `HELP`.
- A manifest `assets` value that is not a plain dict of lists (a tuple, an
  `('include', ...)` directive, a dict key that is a variable). `in_manifest`
  must say `null`, never guess and never raise.
- A module name that fails `MODULE_NAME_RE` on the listing route: unchanged
  400, and no half-filled `js`.
- The daemon serving an `/api/help` text that `hash`-mismatches because the
  text was re-rendered per request with something non-deterministic (a
  timestamp, a set order). The hash must be stable across calls.
- Hoot on 20 reporting `available: true, tested: "unverified"`: the client must
  be able to tell "can run" from "known to work".

## What the migrator does next (context, not scope)

After this lands the migrator will, in its own repository:

1. At job start, call `GET /api/help`, cache by `version`, substitute the
   markers with its own tool names, append a small overlay it owns (the
   `tests` topic's transport differences, its `reopen` rule), record the hash
   on the job, and fall back to its vendored `prompts/sheller.md` when the
   route is absent or the daemon is down.
2. Read `js_tests` on its sessions and, where `available` and the module ships
   JS files, offer the agent the `run_test` call from `js.run` for the module's
   Hoot suite on both sides, and treat `js_tests_skipped` as a failure, never
   a pass. Characterization for JS (a Hoot test written on the source and
   ported) is a later design of its own; it needs B1 to B3 first.
3. Replace its drift test with one that checks the overlay against the live
   `markers` list.

If anything here cannot be done the way it is written, say what you found and
what you did instead in the commit message and CHANGELOG. Do not widen the
scope.
