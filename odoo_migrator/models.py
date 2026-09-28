"""Peewee models. Names locked by the implementation plan, task 2."""

import uuid
from datetime import UTC, datetime

from peewee import (
    BooleanField,
    CharField,
    DateTimeField,
    DeferredForeignKey,
    ForeignKeyField,
    IntegerField,
    Model,
    TextField,
)

from odoo_migrator.db import database

# Status hues (155 / 25 / 62 / 205 / 82) and the background (290) are taken.
ACCENTS = {
    "violet": "oklch(0.70 0.15 305)",
    "magenta": "oklch(0.72 0.14 330)",
    "indigo": "oklch(0.68 0.10 275)",
    "orchid": "oklch(0.72 0.12 320)",
    "slate": "oklch(0.70 0.04 250)",
    "cobalt": "oklch(0.68 0.10 260)",
}


def _uuid() -> str:

    return str(uuid.uuid4())


def _now() -> datetime:

    return datetime.now(UTC)


class BaseModel(Model):
    class Meta:
        database = database


class AppSettings(BaseModel):
    sheller_base_url = TextField(default="http://127.0.0.1:8765")
    sheller_image_tag = TextField(default="")
    min_sheller_version = TextField(default="")
    # Never rendered. A blank save leaves the stored value in place.
    anthropic_api_key = TextField(default="")


class ProviderSetting(BaseModel):
    """One setting for one provider. A key for an SDK, a URL for a daemon."""

    provider = CharField()
    key = CharField()
    value = TextField(default="")

    class Meta:
        indexes = ((("provider", "key"), True),)


class CartridgeInstall(BaseModel):
    cartridge_id = CharField()
    version = CharField()
    sha256 = CharField(default="")
    schema_version = IntegerField(default=1)
    # The file as imported, so a pin can be loaded without the original path.
    body = TextField(default="")
    capabilities = TextField(default="{}")
    smoke_status = CharField(default="untested")


class Migration(BaseModel):
    id = CharField(primary_key=True, default=_uuid)
    name = CharField()
    accent_color = CharField()
    source_version = CharField()
    target_version = CharField()
    old_project_path = TextField()
    new_project_path = TextField()
    old_container = CharField(default="")
    new_container = CharField(default="")
    # Which database on each side. Empty means the one the container's own
    # odoo.conf names. One Postgres commonly holds dozens, and a config
    # without `db_name` is ordinary, so this cannot only be probed.
    old_database = CharField(default="")
    new_database = CharField(default="")
    cartridge_id = CharField(null=True)
    cartridge_version = CharField(null=True)
    model_webhook_path = TextField(default="")
    strictness_default = CharField(default="basic")
    # Empty means the cartridge's model. A pair here is what this migration runs.
    provider_override = CharField(default="")
    model_override = CharField(default="")
    retry_limit = IntegerField(default=5)
    ask_human_mode = CharField(default="sync")
    status = CharField(default="idle")
    archived_at = DateTimeField(null=True)
    created_at = DateTimeField(default=_now)


class Module(BaseModel):
    migration = ForeignKeyField(Migration, backref="modules")
    technical_name = CharField()
    old_path = TextField(default="")
    new_path = TextField(default="")
    status = CharField(default="queued")
    struck_out = BooleanField(default=False)
    has_assumptions = BooleanField(default=False)
    notes = TextField(default="")
    # Every dependency, as a JSON array of names. A single parent kept one
    # edge of the several a module really has: on a real database 81 of 128
    # modules depended on more than one other, and half the graph's 256
    # edges were dropped. Batch order is derived from these edges, so the
    # missing ones are the ones that would let a batch run out of order.
    depends = TextField(default="[]")
    absent_from_odoo = BooleanField(default=False)
    addons_root = TextField(default="")
    author = TextField(default="")
    license = TextField(default="")
    batch_group = IntegerField(null=True)
    # Stable serial on the canvas. Assigned once, max+1, never compacted
    # when a module leaves the graph.
    node_id = IntegerField(default=0)
    position = IntegerField(default=0)
    strictness = CharField(null=True)


class Batch(BaseModel):
    migration = ForeignKeyField(Migration, backref="batches")
    status = CharField(default="idle")
    # Which group on the tree this batch is running. Null for a batch of
    # one started with Migrate. The runner re-reads the order from here
    # after every callback, so it survives a restart mid-batch.
    batch_group = IntegerField(null=True)
    # Which dependency wave this batch is running, for a batch started from
    # the Pipeline queue. A step used to be recorded in `batch_group` above,
    # and the runner read that column back as a group: a step batch walked
    # the group with the same number. Nothing showed because Resolve sets
    # every module's `batch_group` to its own wave — until someone regroups
    # a module by hand on Tree, which is what that control is for.
    step = IntegerField(null=True)
    model_webhook_url = TextField(default="")
    cartridge_id = CharField(null=True)
    cartridge_version = CharField(null=True)
    created_at = DateTimeField(default=_now)


class ShellerSession(BaseModel):
    """A Sheller session this process opened, and the write key it was handed.

    The daemon refuses a keyless close with 403. A session whose process dies
    leaves the registry on its own.
    """

    session_id = CharField(unique=True)
    write_key = TextField()
    migration_id = CharField(default="")
    created_at = DateTimeField(default=_now)


class Job(BaseModel):
    job_id = CharField(unique=True, default=_uuid)
    batch = ForeignKeyField(Batch, backref="jobs")
    module = ForeignKeyField(Module, backref="jobs")
    status = CharField(default="queued")
    current_stage = CharField(default="")
    attempt = IntegerField(default=0)
    current_session_id = CharField(null=True)
    resume_webhook_url = TextField(null=True)
    # The question the Wait node is parked on. Shown in the row until answered.
    question = TextField(null=True)
    # What the human replied. Stored here so the runner can read it later.
    answer = TextField(null=True)
    # Set by Dismiss on the checkpoint. Continue stays disabled until every
    # failed or waiting job in the batch has been read, unless Override.
    reviewed = BooleanField(default=False)
    # Hash of the assembled rules stack as sent. Promoting a finding into a
    # version file mid-batch changes the next module's prompt; this records
    # what this job actually saw.
    rules_hash = CharField(null=True)
    cartridge_id = CharField(null=True)
    cartridge_version = CharField(null=True)
    cartridge_sha256 = CharField(default="")
    # The pair the card was showing when this job started: the flow's default,
    # unless the migration settings replaced it. A later edit of the card does
    # not rewrite a job that already ran.
    provider = CharField(default="")
    model_name = CharField(default="")
    # Unified diffs of File API writes on the new side, keyed by attempt.
    # The cartridge callback's attempt.diff is often empty; this is what
    # the checkpoint actually has to show.
    file_diffs = TextField(default="{}")


class Attempt(BaseModel):
    job = ForeignKeyField(Job, backref="attempts")
    iteration = IntegerField()
    stages = TextField(default="[]")
    diff = TextField(default="")
    test_output = TextField(default="")
    verdict = CharField(default="")
    diagnosis = TextField(default="")


class Finding(BaseModel):
    job = ForeignKeyField(Job, backref="findings")
    text = TextField()
    source = CharField()
    promoted_to_version = CharField(null=True)


class Assumption(BaseModel):
    job = ForeignKeyField(Job, backref="assumptions")
    question = TextField()
    assumption = TextField()


class StepRun(BaseModel):
    """One step of one attempt. The trace hangs off this, not off the job."""

    job = ForeignKeyField(Job, backref="step_runs")
    attempt = IntegerField(default=1)
    step_id = CharField(default="")
    kind = CharField(default="")
    started_at = DateTimeField(default=_now)
    finished_at = DateTimeField(null=True)
    ok = BooleanField(null=True)
    flags = TextField(default="")
    tokens_in = IntegerField(default=0)
    tokens_out = IntegerField(default=0)
    # Cache reads are most of a long step's input and are billed differently.
    # The providers report it; nothing used to store it.
    tokens_cached = IntegerField(default=0)
    cost_cents = IntegerField(default=0)
    # Why the step ended the way it did. The runner computed this and threw
    # it away, so a failed step was a red badge with no sentence behind it.
    output = TextField(default="")


class RunLog(BaseModel):
    """One line of one job's run log.

    Kept in the database rather than a file: a job runs on a thread, the
    page that watches it reloads, and a run that failed at 13:26 has to be
    readable at 14:00. `detail` is the body — a prompt, an answer, a
    traceback — shown only when the line is opened.
    """

    job = ForeignKeyField(Job, backref="log_lines")
    at = DateTimeField(default=_now)
    level = CharField(default="info")
    source = CharField(default="")
    text = TextField(default="")
    detail = TextField(default="")
    # A line about a tool call points at its skeleton row, which is what can
    # find the command in Sheller's journal. The line itself carries no body:
    # a `detail` capped at 20k would be a second, shorter copy of a command's
    # output the daemon already keeps whole. Deferred because `ToolCall` is
    # declared below this.
    tool_call = DeferredForeignKey("ToolCall", null=True, backref="log_lines")


class ToolCall(BaseModel):
    """A skeleton row. The body stays in Sheller's journal until someone opens it."""

    step_run = ForeignKeyField(StepRun, backref="tool_calls")
    tool = CharField()
    side = CharField(default="")
    session_id = CharField(default="")
    command_id = CharField(default="")
    duration_ms = IntegerField(default=0)
    ok = BooleanField(default=True)
    preview = TextField(default="")


class CheckpointMessage(BaseModel):
    """One line of the checkpoint thread. author is ``agent`` or ``human``."""

    job = ForeignKeyField(Job, backref="messages")
    author = CharField()
    text = TextField()
    created_at = DateTimeField(default=_now)


class PipelineQueueState(BaseModel):
    migration = ForeignKeyField(Migration, backref="pipeline_queue_state", unique=True)
    run_state = CharField(default="stopped")


class PipelineQueueItem(BaseModel):
    migration = ForeignKeyField(Migration, backref="pipeline_queue_items")
    step = IntegerField()
    position = IntegerField(default=0)

    class Meta:
        indexes = ((("migration", "step"), True),)


ALL_MODELS = [
    ShellerSession,
    AppSettings,
    ProviderSetting,
    CartridgeInstall,
    Migration,
    Module,
    Batch,
    Job,
    Attempt,
    Finding,
    Assumption,
    PipelineQueueState,
    PipelineQueueItem,
    StepRun,
    RunLog,
    ToolCall,
    CheckpointMessage,
]
