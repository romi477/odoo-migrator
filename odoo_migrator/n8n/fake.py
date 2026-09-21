"""In-memory n8n. Answers the way 2.39.9 did in the spike."""

from __future__ import annotations

import uuid

from odoo_migrator.n8n.client import N8nError, N8nNotReady, wait_until_ready


def _workflow_id(value) -> str:
    if isinstance(value, dict):

        return str(value.get("value") or "")

    return str(value or "")


def execute_refs(workflow: dict) -> list[str]:
    found = []
    for node in workflow.get("nodes", []):
        if node.get("type") != "n8n-nodes-base.executeWorkflow":
            continue
        ref = _workflow_id(node.get("parameters", {}).get("workflowId"))
        if ref:
            found.append(ref)

    return found


class FakeN8nClient:
    """Create, activate, trigger. No Docker, no n8n process."""

    def __init__(self):
        self.ready = False
        self.refuse_child = ""
        self.workflows: dict[str, dict] = {}
        self.active: set[str] = set()
        self.activated: list[str] = []
        self.webhooks: dict[str, str] = {}
        self.triggers: list[dict] = []
        self.imports: list[dict] = []

    def wait_until_ready(self, timeout: float = 60.0) -> dict:

        return wait_until_ready(self.rest_settings, timeout=timeout, interval=0.01)

    def rest_settings(self) -> dict:
        if not self.ready:
            raise N8nNotReady("n8n is starting up. Please wait")

        return {"ok": True}

    def create_workflow(self, body: dict) -> dict:
        workflow_id = str(uuid.uuid4())
        stored = {
            "id": workflow_id,
            "name": body.get("name", ""),
            "nodes": list(body.get("nodes") or []),
            "connections": dict(body.get("connections") or {}),
            "settings": dict(body.get("settings") or {}),
            "active": False,
        }
        self.workflows[workflow_id] = stored

        return dict(stored)

    def activate(self, workflow_id: str) -> dict:
        workflow = self.workflows.get(workflow_id)
        if workflow is None:
            raise N8nError("unknown workflow", status=404)
        if workflow["name"] == self.refuse_child:
            raise N8nError(
                f'Cannot publish workflow: Node "Execute unpublished" references '
                f'workflow {workflow_id} ("{workflow["name"]}") which is not published. '
                f"Please publish all referenced sub-workflows first.",
                status=400,
            )
        for ref in execute_refs(workflow):
            child = self.workflows.get(ref)
            if child is None or child["id"] not in self.active:
                name = child["name"] if child else ref
                raise N8nError(
                    f'Cannot publish workflow: Node "Execute unpublished" references '
                    f'workflow {ref} ("{name}") which is not published. '
                    f"Please publish all referenced sub-workflows first.",
                    status=400,
                )
        workflow["active"] = True
        self.active.add(workflow_id)
        self.activated.append(workflow_id)
        for node in workflow["nodes"]:
            if node.get("type") == "n8n-nodes-base.webhook":
                path = str(node.get("parameters", {}).get("path") or "").strip("/")
                if path:
                    self.webhooks[path] = workflow_id

        return dict(workflow)

    def trigger(self, path: str, payload: dict) -> dict:
        slug = path.strip("/")
        if slug not in self.webhooks:
            raise N8nError(
                f'The requested webhook "POST {slug}" is not registered.',
                status=404,
            )
        self.triggers.append({"path": slug, "payload": payload})

        return {"message": "Workflow was started"}

    def import_package(self, archive: bytes, *, filename="cartridge.n8np", policy="update"):
        if not archive.startswith(b"PK"):
            raise N8nError("Failed to read package archive", status=400)
        self.imports.append(
            {"filename": filename, "policy": policy, "size": len(archive)}
        )

        return {"ok": True}
