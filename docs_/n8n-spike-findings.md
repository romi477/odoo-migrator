# n8n contract spike — findings

Throwaway spike, Task 3b. Image `docker.n8n.io/n8nio/n8n:latest`, reported
version **2.39.9**. Workflows were created, exercised and deleted in the
same run. No product code. Task 9 locks the webhook contract against
these observations, not against the docs.

Host port was **5679**. 5678 was already taken by a running Odoo
container (`integra19` longpolling). That is why `n8n_base_url` is a
settings field and must never be hardcoded.

`WEBHOOK_URL` was set to `http://127.0.0.1:5679/` so n8n generated
resume URLs the host process can POST to.

`/healthz` returns 200 while the process is still serving
`n8n is starting up. Please wait`. Wait for `/rest/settings` to return
JSON before calling the API.

## Production webhook vs test URL

Confirmed, including the classic trap.

| Call | When | Result |
|---|---|---|
| `POST /webhook/spike-job` | workflow not published | **404** `The requested webhook "POST spike-job" is not registered.` Hint: *The workflow must be active for a production URL to run successfully.* |
| `POST /webhook-test/spike-job` | editor not listening | **404** Hint: *Click the 'Execute workflow' button on the canvas, then try again. (In test mode, the webhook only works for one call after you click this button)* |
| `POST /webhook/spike-job` | after `POST /api/v1/workflows/{id}/activate` | **200** `{"message":"Workflow was started"}` |

Publishing is `POST /api/v1/workflows/{id}/activate`. It set
`active: true` and `activeVersionId`. Production executions are not
shown on the canvas; they are in the executions list. The app must
store the production URL of a published workflow, never the test URL.

## Progress callback mid-run

Arrived. n8n POSTed to `http://host.docker.internal:18099/progress`
from inside the container (`User-Agent: n8n`, `Content-Type:
application/json`) with:

```json
{
  "job_id": "13fe80ab-fab3-4420-9757-47dbd4f87850",
  "stage": "understand",
  "attempt": 1,
  "resumeUrl": "http://127.0.0.1:5679/webhook-waiting/1?signature=6afb719692c2466a203a511d362157e8f963b46c99332b42d3fa5b03c4aa57e4"
}
```

`$execution.resumeUrl` is available on the HTTP Request node **before**
the Wait node runs. The app can persist `resume_webhook_url` from the
progress callback; it does not have to wait for the result callback.

The query string is part of the URL. Stripping `?signature=` makes
resume fail.

## Wait / resume / result

The Wait node (Resume = On Webhook Call) suspended the execution.
`POST` to that `resumeUrl` with `{"answer": "use the new tax mapping"}`
returned **200** `{"message":"Workflow was started"}`.

The result callback then arrived at
`http://host.docker.internal:18099/result`:

```json
{
  "job_id": "13fe80ab-fab3-4420-9757-47dbd4f87850",
  "status": "passed",
  "answer": "use the new tax mapping"
}
```

The resume body is the Wait node's output (`$json.body.answer`). Task 9
can keep `{"answer": "..."}` as the resume payload.

Not tested here: HTTP Request node retries when the app is down. Task 14
still has to wire that on the workflow side; Task 9 still has startup
reconciliation for lost callbacks.

## Unpublished sub-workflow

Stronger than the docs. On 2.39.9, `POST /api/v1/workflows/{id}/activate`
of a parent whose `Execute Workflow` node points at an unpublished child
is **400**:

> Cannot publish workflow: Node "Execute unpublished" references
> workflow K2JbvDFgqEQLke87 ("spike-child-unpublished") which is not
> published. Please publish all referenced sub-workflows first.

The parent never became active, so its production webhook stayed 404.
The runtime error from the docs — `Workflow is not active and cannot be
executed`, returned **to the agent as the tool result** — remains the
failure mode for a **Call n8n Workflow Tool** if a sub-workflow is
unpublished after the parent is already live. Task 15 must publish every
sub-workflow the stock cartridge's agent tools call, and the smoke-run
in Task 16 must catch a missing publish before a batch.

`Execute Workflow` now fails at publish time. That is the better seam
for deterministic sub-workflows (Task 14).

## n8n packages import API

Available on this self-hosted community edition. Not a 404. Docs say
there is no license feature for packages themselves.

| Request | Result |
|---|---|
| `POST /api/v1/n8n-packages/import` as JSON | **415** `unsupported media type undefined` — matches the spec (multipart only) |
| same, multipart, no API key | **401** `unauthorized` |
| same, multipart, valid key, garbage bytes | **400** `Failed to read package archive` |
| `GET /api/v1/n8n-packages` | **404** — no list endpoint; import/export only |

Creating an API key on 2.39.9 via `POST /rest/api-keys` requires both
`scopes` (array) and `expiresAt` (number; `0` was accepted). The public
API then accepts `X-N8N-API-KEY`. Scopes used for the probe included
`workflow:import` and `workflow:export`.

Task 16 can use the packages API. Manual UI import is the fallback for
a malformed archive, not for a missing endpoint.

## Networking: container vs host loopback

A fake Sheller was bound to **`127.0.0.1:8765`** on the host. The
callback listener was bound to **`0.0.0.0:18099`**.

| From | To | Result |
|---|---|---|
| Host process | `127.0.0.1:8765` | **200** — this is the app's path to the daemon |
| n8n container | `127.0.0.1:8765` | **connection refused** — that address is the container |
| n8n container | `host.docker.internal:8765` (loopback-only bind) | **200** on Docker Desktop for Mac |
| n8n container | `host.docker.internal:18099` (`0.0.0.0` bind) | **200** — this is how callbacks reached the spike |

The expected failure against Sheller's loopback-only port **did not
happen on this OS**. Docker Desktop forwards `host.docker.internal` onto
host loopback. That is a Desktop-specific hole, not a contract:

- On Linux, `host.docker.internal:host-gateway` is the bridge address,
  which Sheller is not listening on.
- Sheller's security boundary is still `127.0.0.1` only. A connection
  that arrives via the host gateway is not loopback, even when Desktop
  happens to deliver it.
- n8n still needs **one** host address for File API, Sheller proxy and
  callbacks. Putting `127.0.0.1:8765` in the payload remains wrong.

Task 9 therefore still locks `sheller_base_url` / `callback_url` /
progress URL in the payload to `app_base_url`. The daemon's
`127.0.0.1:8765` stays in `AppSettings.sheller_base_url` for the app
process only.

## What Task 9 should lock from this run

- Trigger the **production** webhook of a **published** workflow.
  Unpublished → 404, not a silent drop.
- Progress is a mid-run `POST` from n8n (`User-Agent: n8n`) and may
  carry `resumeUrl` already.
- Resume is `POST` to `$execution.resumeUrl` including `?signature=`;
  body `{ "answer": "..." }` comes out of the Wait node.
- Result is a later `POST` to `callback_url`.
- Those two URLs must be reachable **from the n8n container**, so they
  are `app_base_url`, never host loopback.
- `WEBHOOK_URL` on the n8n container must be the address **the app**
  uses to resume and to store the cartridge webhook — here
  `http://127.0.0.1:<published-port>/`.
- Pin the stock cartridge against n8n **2.39.x** until Task 14 says
  otherwise. Publish is `/activate`. Sub-workflows referenced by
  `Execute Workflow` must be published or the parent will not activate.
