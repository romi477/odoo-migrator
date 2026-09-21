"""Webhook contract: trigger, progress, result. Locked against the Task 3b spike."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from odoo_migrator.db import database
from odoo_migrator.models import Migration, Module
from odoo_migrator.rules import assemble_rules, load_system_prompt

CONTRACT_VERSION = "1"
DEFAULT_CARTRIDGE = {"id": "stock", "version": "1.0.0"}
LOOPBACK_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}
STAGES = (
    "understand",
    "rewrite",
    "upgrade",
    "tests",
    "smoke",
    "verdict",
    "diagnose",
    "reprobe",
    "waiting_for_human",
    "cleanup",
)
# What a cartridge is allowed to end a job with. The app's own statuses —
# queued, running, unknown — are not in here: a cartridge does not get to
# say a job is queued.
RESULT_STATUSES = ("passed", "failed", "waiting_for_human")


class ContractError(Exception):
    """The trigger could not be built. Silent skip would send a bad payload."""


class LoopbackInPayload(ContractError):
    """A URL n8n would call is host loopback. That only works on Docker Desktop."""


class AttemptPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    iteration: int
    diff: str = ""
    test_output: str = ""
    verdict: str = ""
    # What the diagnose stage concluded. The cartridge was already sending
    # it and `extra="ignore"` threw it away, so every retry reached the
    # checkpoint as "attempt 2 · retry" with nothing saying why.
    diagnosis: str = ""


class FindingPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    text: str
    source: str


class AssumptionPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    question: str
    assumption: str


class ProgressPayload(BaseModel):
    """n8n 2.39.9 posted ``resumeUrl``; the spec named it ``resume_webhook_url``.

    Accept both. Persist the spec name. The query string is part of the URL.
    """

    model_config = ConfigDict(extra="ignore")
    job_id: str
    stage: str
    attempt: int = Field(ge=1)
    resume_webhook_url: str | None = None
    resumeUrl: str | None = None

    @field_validator("stage")
    @classmethod
    def known_stage(cls, value: str) -> str:
        # STAGES was declared and then never used, so a cartridge could
        # report any string and the app stored it. A stage nobody recognises
        # is a cartridge bug, and it should be loud where it happens.
        if value not in STAGES:
            raise ValueError(f"unknown stage: {value!r}; expected one of {', '.join(STAGES)}")

        return value

    @model_validator(mode="after")
    def unify_resume(self):
        if not self.resume_webhook_url and self.resumeUrl:
            self.resume_webhook_url = self.resumeUrl

        return self


class ResultPayload(BaseModel):
    model_config = ConfigDict(extra="ignore")
    job_id: str
    status: str
    current_session_id: str | None = None
    attempts: list[AttemptPayload] = []
    assumptions: list[AssumptionPayload] = []
    findings: list[FindingPayload] = []
    question: str | None = None
    resume_webhook_url: str | None = None
    resumeUrl: str | None = None

    @field_validator("status")
    @classmethod
    def known_status(cls, value: str) -> str:
        # An unrecognised status used to be written straight onto the module.
        # It is not merely a wrong badge: the batch runner reads this field to
        # decide whether to stop, and anything it does not know reads as "not
        # failed" — so a cartridge typo would march the batch past a failure.
        if value not in RESULT_STATUSES:
            raise ValueError(
                f"unknown status: {value!r}; expected one of {', '.join(RESULT_STATUSES)}"
            )

        return value

    @model_validator(mode="after")
    def unify_resume(self):
        if not self.resume_webhook_url and self.resumeUrl:
            self.resume_webhook_url = self.resumeUrl

        return self


def reject_loopback(payload: dict) -> None:
    for key in ("sheller_base_url", "callback_url", "progress_url"):
        host = (urlparse(payload[key]).hostname or "").lower()
        if host in LOOPBACK_HOSTS:
            raise LoopbackInPayload(
                f"{key} must be reachable from n8n, not loopback ({host}). "
                "Set the app base URL to an address the container can call."
            )


def stack_hash(rules: list[dict]) -> str:
    body = json.dumps(rules, sort_keys=True, ensure_ascii=False)

    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _base_url(app_base_url: str) -> str:
    base = (app_base_url or "").strip().rstrip("/")
    if not base:
        raise ContractError(
            "Set the app base URL — n8n calls File API, the Sheller proxy "
            "and callbacks on it."
        )

    return base


def build_trigger(
    *,
    migration_id: str,
    job_id: str,
    module_id: int,
    app_base_url: str,
) -> dict:
    """Resolved text, never paths. Every URL is ``app_base_url``.

    ``sheller_base_url`` in this payload is the app itself (the proxy lives
    at Sheller's native paths). The daemon's ``127.0.0.1:8765`` must not
    appear here: on this Mac ``host.docker.internal`` forwards onto loopback
    and the mistake only shows up on Linux.
    """
    base = _base_url(app_base_url)
    with database.connection_context():
        migration = Migration.get_or_none(Migration.id == migration_id)
        module = Module.get_or_none(
            (Module.id == module_id) & (Module.migration == migration_id)
        )
    if migration is None:
        raise ContractError("unknown migration")
    if module is None:
        raise ContractError("unknown module")
    docs = assemble_rules(migration.source_version, migration.target_version)
    rules = [{"version": doc.version, "role": doc.role, "text": doc.text} for doc in docs]
    old_path = module.old_path or str(Path(migration.old_project_path) / module.technical_name)
    new_path = module.new_path or str(Path(migration.new_project_path) / module.technical_name)
    cartridge_id = migration.cartridge_id or DEFAULT_CARTRIDGE["id"]
    cartridge_version = migration.cartridge_version or DEFAULT_CARTRIDGE["version"]
    notes = module.notes.strip() or None
    payload = {
        "contract_version": CONTRACT_VERSION,
        "migration_id": migration.id,
        "job_id": job_id,
        "cartridge": {"id": cartridge_id, "version": cartridge_version},
        "module": {
            "technical_name": module.technical_name,
            "old_path": old_path,
            "new_path": new_path,
        },
        "source_version": migration.source_version,
        "target_version": migration.target_version,
        "containers": {"old": migration.old_container, "new": migration.new_container},
        "sheller_base_url": base,
        "system_prompt": load_system_prompt(),
        "rules": rules,
        "module_notes": notes,
        "strictness_level": module.strictness or migration.strictness_default,
        "retry_limit": migration.retry_limit,
        "ask_human_mode": migration.ask_human_mode,
        "callback_url": f"{base}/api/jobs/{job_id}/result",
        "progress_url": f"{base}/api/jobs/{job_id}/progress",
    }
    reject_loopback(payload)

    return payload
