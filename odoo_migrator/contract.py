"""Job payload, progress and result. The runner calls these in-process."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from odoo_migrator.db import database
from odoo_migrator.models import Migration, Module
from odoo_migrator.rules import assemble_rules, load_system_prompt

CONTRACT_VERSION = "1"
DEFAULT_CARTRIDGE = {"id": "stock", "version": "1.0.0"}
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
    "port",
    "lint",
    "probe",
    "write_test",
    "patch",
    "rewrite",
    "ask_human",
)
# What a cartridge is allowed to end a job with. The app's own statuses —
# queued, running, unknown — are not in here: a cartridge does not get to
# say a job is queued.
RESULT_STATUSES = ("passed", "failed", "waiting_for_human")


class ContractError(Exception):
    """The trigger could not be built. Silent skip would send a bad payload."""


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
    """Accept ``resumeUrl`` or ``resume_webhook_url``. Persist the latter.

    The query string is part of the URL.
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


def stack_hash(rules: list[dict]) -> str:
    body = json.dumps(rules, sort_keys=True, ensure_ascii=False)

    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def build_trigger(
    *,
    migration_id: str,
    job_id: str,
    module_id: int,
) -> dict:
    """Resolved text for one module. Paths stay on the host; nothing is called back."""
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
        "system_prompt": load_system_prompt(),
        "rules": rules,
        "module_notes": notes,
        "strictness_level": module.strictness or migration.strictness_default,
        "retry_limit": migration.retry_limit,
        "ask_human_mode": migration.ask_human_mode,
    }

    return payload
