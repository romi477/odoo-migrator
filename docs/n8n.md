# n8n cartridge

How the stock executor is pinned, published, triggered and resumed. The
app is the source of truth; n8n is a replaceable cartridge that runs one
module and ships the verdict home. Start with the [README](../README.md)
if you just want compose up; this is the contract a cartridge codes
against.

The node-by-node graph the canvas was built from is
[`docs_/n8n-workflow-flow.md`](../docs_/n8n-workflow-flow.md). What a
real 2.39.9 run did to the webhook names is
[`docs_/n8n-spike-findings.md`](../docs_/n8n-spike-findings.md). Task 9
locked the product against those observations, not against n8n's docs.

## Pin

Stock compose is `n8n/compose.yml`, image
`docker.n8n.io/n8nio/n8n:2.39.9`. The spike pulled the floating tag,
which reported 2.39.9; pinning the cartridge to "2.39.x" while composing
an unpinned tag cannot both stay true.

`N8N_WEBHOOK_URL` is the address **this process** uses to resume and to
store the production webhook. It is not hardcoded: 5678 was already
taken on the spike machine. Set `n8n_base_url` in App settings to
whatever host port you mapped.

```
N8N_BLOCK_ENV_ACCESS_IN_NODE=false
```

Every node that calls the app reads `$env.MIGRATOR_API_KEY`. n8n's own
v20 breaking changes flip the default to true. On the wrong side of that
the header goes out empty and the File API answers 403.

`extra_hosts: host.docker.internal:host-gateway` is how the container
reaches the app on Linux. Sheller is never this container's
`127.0.0.1` — only the app's proxy. The payload's `sheller_base_url` is
`app_base_url`. A loopback value is refused at trigger: on Docker
Desktop `host.docker.internal` forwards onto loopback and the mistake
only shows up on Linux.

## Publishing

Production webhook, not the test URL. Publishing is
`POST /api/v1/workflows/{id}/activate`. `/webhook-test/...` works for
one click of Execute workflow and then 404s.

On 2.39.9 a parent whose Execute Workflow points at an unpublished child
is **400** and the production webhook stays **404**. Order:

1. Create every sub-workflow (`sheller-*`, `progress`, `callback-result`,
   `agent-*`).
2. Create `migrate-one-module`.
3. Create the thin wrappers (`migrate-module-openai`,
   `migrate-module-claude`).
4. Activate children, then the parent, then the wrappers.

`odoo_migrator/n8n/stock.py` does that. Placeholders `stock:<name>` in
the JSON are rewritten to the ids n8n just minted.

A cartridge can also arrive as an `.n8np`. Import is
`POST /api/v1/n8n-packages/import` as **multipart**, not JSON (415). A
missing archive is 400.

`/healthz` returns 200 while n8n is still serving "n8n is starting up."
Wait for `/rest/settings` to return JSON before calling the API.

## Manifest

`n8n/stock/manifest.json`:

```json
{
  "id": "stock",
  "version": "1.0.0",
  "contract_version": "1",
  "webhook_path": "migrate-module",
  "wrappers": {
    "openai": "migrate-module-openai",
    "claude": "migrate-module-claude"
  }
}
```

Unknown `contract_version` is refused. The pin on a migration is
`id@version`; every job records both at start. Swapping the pin does not
rewrite past jobs.

Thin wrappers are extra webhook parents that Execute Workflow the shared
`migrate-one-module` loop. The batch records which path it fired. Model
credentials live in n8n; they are not in the payload and not in git.

## Trigger

`POST {n8n}/webhook/{path}` with the payload `build_trigger` built.
Resolved text, never file paths. Every URL n8n will call is
`app_base_url`.

| Field | Content |
|---|---|
| `contract_version` | `"1"` |
| `migration_id` / `job_id` | SQLite identity |
| `cartridge` | `{id, version}` stamped at start |
| `module` | `technical_name`, `old_path`, `new_path` |
| `source_version` / `target_version` | The pair |
| `containers` | `{old, new}` names |
| `sheller_base_url` | The **app** (proxy), not `127.0.0.1:8765` |
| `system_prompt` | Contents of `prompts/system.md` |
| `rules` | `[{version, role, text}, …]` assembled stack |
| `module_notes` | Tree notes, or `null` — the key is always there |
| `webhook_path` | Which wrapper this batch fires; the app routes on it |
| `strictness_level` | `basic` or `enhanced`. Stock does not branch on it |
| `retry_limit` | Canvas loop gate |
| `ask_human_mode` | `sync` or `batch_defer` |
| `callback_url` | `{app}/api/jobs/{job_id}/result` |
| `progress_url` | `{app}/api/jobs/{job_id}/progress` |

Header: `X-Odoo-Migrator-Key` from `$env.MIGRATOR_API_KEY`.

## Progress and result

n8n POSTs JSON. The keyed routes accept `resumeUrl` (what 2.39.9 sent)
and persist `resume_webhook_url` (what the spec named). The query string
is part of the URL; stripping `?signature=` makes resume fail.

**Progress** while the job is held:

```json
{
  "job_id": "…",
  "stage": "understand",
  "attempt": 1,
  "resumeUrl": "http://n8n:5678/webhook-waiting/1?signature=…"
}
```

Stages: `understand`, `rewrite`, `upgrade`, `tests`, `smoke`, `verdict`,
`diagnose`, `reprobe`, `waiting_for_human`, `cleanup`. Anything else is
a cartridge bug and is refused.

**Result** ends the job with `passed`, `failed`, or `waiting_for_human`.
The app's own statuses (`queued`, `running`, `unknown`) are not in this
list: a cartridge does not get to say a job is queued. An unrecognised
status used to be written onto the module, and the batch runner treated
anything it did not know as "not failed".

```json
{
  "job_id": "…",
  "status": "passed",
  "attempts": [{"iteration": 1, "diff": "", "test_output": "", "verdict": "passed", "diagnosis": ""}],
  "findings": [{"text": "…", "source": "understand"}],
  "assumptions": []
}
```

`attempts` may be empty. The app then creates rows from File API writes
on the new side, keyed by attempt. A cartridge that already sent a
non-empty `diff` keeps it. Stock `Mark passed` also appends an attempt
on the canvas, so a module that passed first time still has a checkpoint
row.

A result the executor never took used to leave the job, the module and
the batch on `running` forever. Nothing was started, so those rows are
undone. A callback that arrives after a crash is applied if the job is
still `unknown`.

## Wait and resume

`ask_human_mode = sync`: the canvas POSTs `waiting_for_human` with
`$execution.resumeUrl`, then parks on a Wait node (Resume = On Webhook
Call). The app stores the URL from progress or result. Answering on Tree
POSTs `{"answer": "…"}` to that URL.

The Wait node's **output is the resume request**, not the job. The spike
measured `$json.body.answer` coming out of it; `job_id`, `attempt`,
`retry_limit`, `rules` are gone. Stock canvas keeps a **Before wait**
node and a **Resume with answer** Code node that rebuilds the item from
`$('Before wait')` plus that answer.

A human answer always buys a rewrite: Resume connects to **Bump**, not
to a skip. Even on the last attempt — the answer was the point.

`batch_defer` does not wait. The question and the assumption taken are
recorded and surface at checkpoint.

## The loop

Agent nodes make judgment calls. Everything else is deterministic —
cheaper, more debuggable, more legible as a node.

1. Normalize (`attempt = 1`).
2. Open a Sheller session on the **old** container; hold it for the
   whole run.
3. Understand (agent).
4. Rewrite through the File API (agent).
5. Upgrade on a **fresh** session on the new container, then commit.
   A rewrite invalidates any session that was open there.
6. Tests. `enhanced` is passed through and not yet acted on.
7. Verdict is configured, not inferred.
8. On failure: diagnose (agent). Optional reprobe of the old session.
   Retry is `attempt < retry_limit` on the canvas, then Bump, then
   Rewrite again.
9. Close sessions (success, exhausted retries, and the error workflow,
   so they are never orphaned).
10. Callback.

The old container is read-only. Do not apply the "fresh session after
rewrite" rule to both sides.

## Smoke-run

Settings → This migration → **Smoke-run** starts the smallest unstruck
module with `batch.status = smoke`. That batch is not a checkpoint. The
verdict is written when the result lands.

Gate for **Batch**:

- No `CartridgeInstall` rows → the stub is allowed.
- A pin that matches no install, once anything has been imported →
  refused.
- Otherwise the install's `smoke_status` must be `passed`.

## What n8n must not do

- Call `127.0.0.1:8765`. Inside the container that address is the
  container. Use `{app_base_url}/api/...` (the proxy).
- Fetch journals, or send `X-OS-Admin-Key`. The proxy strips and
  refuses.
- Accumulate state across jobs. `findings` grows within one job and
  ships home.
- Point File API paths at anything outside the two project roots. Escape
  is 403, not a clamp.
