"""Agent stages, canvas retry, Wait. Task 15.

The suite never talks to Docker or to a live n8n. The cartridge is JSON:
a retry that only lives as Max Iterations on an agent node is invisible
on the canvas, and a resumeUrl posted from a sub-workflow is the child's
Wait, not the parent's.
"""

import json
from pathlib import Path

from odoo_migrator.n8n.stock import PARENT

REPO = Path(__file__).resolve().parents[1]
STOCK = REPO / "n8n" / "stock"
AGENTS = (
    "agent-understand",
    "agent-rewrite",
    "agent-diagnose",
    "agent-reprobe",
)


def _load(name: str) -> dict:

    return json.loads((STOCK / f"{name}.json").read_text(encoding="utf-8"))


def _outgoing(doc: dict, name: str) -> list[str]:
    names = []
    for branch in doc.get("connections", {}).get(name, {}).get("main", []) or []:
        for link in branch or []:
            names.append(link["node"])

    return names


def _branches(doc: dict, name: str) -> tuple[list[str], list[str]]:
    mains = doc.get("connections", {}).get(name, {}).get("main", []) or []
    true_to = [link["node"] for link in (mains[0] if mains else [])]
    false_to = [link["node"] for link in (mains[1] if len(mains) > 1 else [])]

    return true_to, false_to


def _incoming(doc: dict, target: str) -> list[str]:
    found = []
    for src, conns in doc.get("connections", {}).items():
        for branch in conns.get("main", []) or []:
            for link in branch or []:
                if link["node"] == target:
                    found.append(src)

    return found


def _reachable(doc: dict, start: str) -> set[str]:
    seen: set[str] = set()
    stack = [start]
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        stack.extend(_outgoing(doc, name))

    return seen


def _execute_refs(doc: dict) -> list[str]:
    refs = []
    for node in doc["nodes"]:
        if node["type"] != "n8n-nodes-base.executeWorkflow":
            continue
        wid = node["parameters"]["workflowId"]
        refs.append(wid["value"] if isinstance(wid, dict) else wid)

    return refs


def test_the_parent_runs_understand_and_rewrite_instead_of_skipping_them():
    parent = _load(PARENT)
    names = [node["name"] for node in parent["nodes"]]
    refs = _execute_refs(parent)

    assert "Skip understand and rewrite" not in names
    for name in AGENTS:
        assert any(name in str(ref) for ref in refs), name


def test_retry_is_a_loop_on_the_canvas_gated_by_retry_limit():
    """Max Iterations on an agent is inner tool-calling. The module retry
    is attempt < retry_limit on this canvas, looping back into Rewrite."""
    parent = _load(PARENT)
    retry = next(node for node in parent["nodes"] if node["name"] == "Retry?")
    blob = json.dumps(retry["parameters"])

    assert retry["type"] == "n8n-nodes-base.if"
    assert "retry_limit" in blob
    assert "$json.attempt" in blob
    yes, no = _branches(parent, "Retry?")
    assert any("Bump attempt" in name for name in yes)
    assert "Rewrite module" in _reachable(parent, "Bump attempt")
    assert "Callback" in _reachable(parent, no[0])
    sources = _incoming(parent, "Rewrite module")
    assert "Bump attempt" in sources or any(
        "Re-examine" in name or "Probe" in name for name in sources
    )


def test_a_failed_upgrade_skips_tests_and_goes_to_diagnose():
    parent = _load(PARENT)
    yes, no = _branches(parent, "Upgrade ok?")

    assert any("test" in name.lower() or "Progress tests" in name for name in yes)
    assert not any("test" in name.lower() for name in no)
    assert "Diagnose failure" in _reachable(parent, no[0])


def test_ask_human_sync_posts_resumeUrl_then_waits_on_this_execution():
    """$execution.resumeUrl in a sub-workflow is that child's Wait.
    The spike posted it from an HTTP Request sitting in front of Wait
    on the same execution the human will resume."""
    parent = _load(PARENT)
    progress = (STOCK / "progress.json").read_text(encoding="utf-8")
    parent_text = (STOCK / f"{PARENT}.json").read_text(encoding="utf-8")
    wait = next(node for node in parent["nodes"] if node["type"] == "n8n-nodes-base.wait")
    park = next(
        node
        for node in parent["nodes"]
        if node["type"] == "n8n-nodes-base.httpRequest"
        and "resumeUrl" in json.dumps(node.get("parameters", {}))
    )

    assert wait["parameters"].get("resume") == "webhook"
    assert "$execution.resumeUrl" in parent_text
    assert "$execution.resumeUrl" not in progress
    assert "waiting_for_human" in json.dumps(park["parameters"])
    assert park["name"] in _incoming(parent, wait["name"])


def test_batch_defer_records_an_assumption_and_does_not_wait():
    parent = _load(PARENT)
    names = [node["name"] for node in parent["nodes"]]

    assert "Record assumption" in names
    assert "Retry?" in _outgoing(parent, "Record assumption")
    assert "Wait" not in _outgoing(parent, "Record assumption")


def test_old_session_opens_once_and_a_rewrite_does_not_reopen_it():
    parent = _load(PARENT)
    rewrite = _load("agent-rewrite")
    names = [node["name"] for node in parent["nodes"]]

    assert names.index("Open old session") < names.index("Understand source")
    assert "sheller-open-session" not in json.dumps(_execute_refs(rewrite))
    assert "Rewrite module" in _incoming(parent, "Upgrade module") or (
        "Upgrade module" in _reachable(parent, "Rewrite module")
    )


def test_exhausted_retries_fail_with_the_attempts_log_and_still_close():
    parent = _load(PARENT)
    callback = _load("callback-result")
    _yes, no = _branches(parent, "Retry?")
    fail_start = no[0]

    assert "Close sessions" in _reachable(parent, fail_start)
    assert "Callback" in _reachable(parent, fail_start)
    blob = json.dumps(callback)
    assert "attempts" in blob
    fail_blob = json.dumps(
        next(node for node in parent["nodes"] if node["name"] == fail_start)
    ) + json.dumps(
        [node for node in parent["nodes"] if node["name"] in _reachable(parent, fail_start)]
    )
    assert "failed" in fail_blob


def test_agent_prompts_are_payload_text_and_tools_talk_to_the_app():
    blob = ""
    for name in AGENTS:
        text = (STOCK / f"{name}.json").read_text(encoding="utf-8")
        doc = json.loads(text)
        types = [node["type"] for node in doc["nodes"]]
        blob += text
        assert any("langchain.agent" in node_type for node_type in types), name
        assert "$json.system_prompt" in text or "$json.rules" in text
        assert "prompts/" not in text

    assert "127.0.0.1:8765" not in blob
    assert "/api/migrations/" in blob
    assert "X-Odoo-Migrator-Key" in blob
    assert "$json.sheller_base_url" in blob or "$fromAI" in blob
    assert "$env.MIGRATOR_API_KEY" in blob


def test_publish_activates_agent_children_before_the_parent():
    from odoo_migrator.n8n.fake import FakeN8nClient
    from odoo_migrator.n8n.stock import publish_stock

    n8n = FakeN8nClient()
    n8n.ready = True
    result = publish_stock(n8n)
    names = {n8n.workflows[wid]["name"] for wid in n8n.active}

    assert set(AGENTS).issubset(names)
    parent_at = n8n.activated.index(result["parent_id"])
    for name in AGENTS:
        child_id = next(
            wid for wid, workflow in n8n.workflows.items() if workflow["name"] == name
        )
        assert n8n.activated.index(child_id) < parent_at


def test_the_canvas_rebuilds_the_job_after_the_wait_node():
    """The spike measured it: the Wait node's output is the resume request,
    `$json.body.answer`. Everything else — job_id, attempt, retry_limit,
    rules — is gone, so wiring Wait straight into an IF that reads
    $json.attempt meant the answer landed on an empty item and the callback
    went out with no job_id at all."""
    parent = _load(PARENT)
    names = {node["name"] for node in parent["nodes"]}

    assert {"Before wait", "Resume with answer"} <= names
    assert _outgoing(parent, "Wait") == ["Resume with answer"]
    assert _outgoing(parent, "Resume with answer") == ["Bump attempt"]
    assert _outgoing(parent, "Before wait") == ["Park for human"]
    resume = next(n for n in parent["nodes"] if n["name"] == "Resume with answer")["parameters"]["jsCode"]
    assert "$('Before wait')" in resume
    assert "body.answer" in resume


def test_the_answer_reaches_the_rewrite_prompt():
    """A question answered and then never shown to the agent is a pause for
    nothing."""
    rewrite = _load("agent-rewrite")
    agent = next(node for node in rewrite["nodes"] if node["name"] == "AI Agent")

    assert "human_answer" in agent["parameters"]["text"]


def test_an_attempt_carries_what_the_human_needs_to_read():
    """Attempts reached the checkpoint as "attempt 2 · retry" and nothing
    else: the diagnosis was dropped by the contract's extra="ignore", and
    the test output was never put on the attempt."""
    parent = _load(PARENT)

    for name in ("Bump attempt", "Mark failed"):
        code = next(n for n in parent["nodes"] if n["name"] == name)["parameters"]["jsCode"]
        assert "diagnosis" in code
        assert "test_output" in code
        # run_test hands back an object on failure; the contract takes text.
        assert "JSON.stringify" in code

    from odoo_migrator.contract import AttemptPayload

    parsed = AttemptPayload.model_validate(
        {"iteration": 2, "verdict": "retry", "diagnosis": "moved to account.tax"}
    )
    assert parsed.diagnosis == "moved to account.tax"


def test_a_diagnosis_reaches_the_checkpoint(client):
    from test_batch import _prepared

    from odoo_migrator.db import database
    from odoo_migrator.models import Attempt

    _row, sale = _prepared(client)

    class Diagnosing:
        def __init__(self, app):
            self.app = app

        def start(self, payload):
            self.app.state.on_result(
                {
                    "job_id": payload["job_id"],
                    "status": "failed",
                    "attempts": [
                        {
                            "iteration": 1,
                            "verdict": "retry",
                            "diagnosis": "tax mapping moved",
                            "test_output": "FAILED test_tax",
                        }
                    ],
                }
            )

    client.app.state.executor = Diagnosing(client.app)
    client.post("/tree/migrate", data={"module_id": str(sale["id"])}, follow_redirects=True)

    with database.connection_context():
        stored = Attempt.select().get()
    assert stored.diagnosis == "tax mapping moved"
    assert "tax mapping moved" in client.get("/run").text
