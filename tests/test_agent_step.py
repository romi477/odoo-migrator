"""Agent steps see only the context the cartridge listed, then the stock flow runs."""

from pathlib import Path

import pytest
from doubles.model import ModelDouble

from odoo_migrator.db import database, init_db
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Migration, Module
from odoo_migrator.rules import RuleDoc
from odoo_migrator.runner.cartridge import ModelRef, Step, load_cartridge, load_cartridge_file
from odoo_migrator.runner.context import StepContext
from odoo_migrator.runner.loop import run_job
from odoo_migrator.runner.prompting import build_context, resolve_model
from odoo_migrator.runner.providers.scripted import ScriptedProvider
from odoo_migrator.sheller.fake import FakeShellerClient

_MINIMAL = """
cartridge: t
version: 1.0.0
schema: 1
model: {provider: anthropic, name: claude-sonnet-5-5}
pipeline:
  - step: lint
on_failure:
  pipeline: []
  retry:
    resume_at: lint
"""

_MANIFEST = "{'name': 'Sale custom', 'depends': ['sale']}\n"


@pytest.fixture
def agent_ctx(tmp_path):
    init_db(tmp_path / "agent-step.sqlite")
    old_mod = tmp_path / "old" / "sale_custom"
    new_mod = tmp_path / "new" / "sale_custom"
    for root in (old_mod, new_mod):
        root.mkdir(parents=True)
        (root / "__manifest__.py").write_text(_MANIFEST, encoding="utf-8")
        (root / "models.py").write_text("class Sale:\n    pass\n", encoding="utf-8")
    created = create_migration(
        name="Acme",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path=str(tmp_path / "old"),
        new_project_path=str(tmp_path / "new"),
    )
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])
        module = Module.create(
            migration=migration,
            technical_name="sale_custom",
            old_path=str(old_mod),
            new_path=str(new_mod),
        )
    cartridge = load_cartridge(_MINIMAL)

    def build(*, previous_diff=""):
        return StepContext(
            job_id="job-1",
            attempt=1,
            migration=migration,
            module=module,
            cartridge=cartridge,
            sheller=FakeShellerClient(),
            sessions={},
            emit=lambda _event: None,
            previous_diff=previous_diff,
        )

    return build


def test_rewrite_is_not_given_the_previous_attempt(agent_ctx):
    """The patch/rewrite distinction is the cartridge's, not the code's."""
    step = Step(
        kind="agent",
        id="rewrite",
        sessions=["old", "new"],
        tools=["write_file"],
        context=["rules", "module_source"],
        prompt="again",
    )

    rendered = build_context(step, agent_ctx(previous_diff="--- a/models.py"))

    assert "--- a/models.py" not in rendered
    assert "module_source" in rendered.lower() or "models.py" in rendered


def test_patch_is_given_it(agent_ctx):
    step = Step(
        kind="agent",
        id="patch",
        sessions=["new"],
        tools=["write_file"],
        context=["previous_diff"],
        prompt="fix",
    )

    rendered = build_context(step, agent_ctx(previous_diff="--- a/models.py"))

    assert "--- a/models.py" in rendered


def test_a_step_model_beats_the_card_and_the_card_beats_the_cartridge(agent_ctx):
    ctx = agent_ctx()
    ctx.migration.model_override = "claude-sonnet-5-5"
    named = Step(
        kind="agent",
        id="diagnose",
        prompt="why",
        model=ModelRef(provider="anthropic", name="claude-sonnet-5"),
    )
    plain = Step(kind="agent", id="port", prompt="port")

    assert resolve_model(named, ctx) == "claude-sonnet-5"
    assert resolve_model(plain, ctx) == "claude-sonnet-5-5"
    ctx.migration.model_override = ""
    assert resolve_model(plain, ctx) == "claude-sonnet-5-5"


@pytest.fixture
def loop_ctx(tmp_path):
    init_db(tmp_path / "stock-loop.sqlite")
    sheller = FakeShellerClient()
    old_mod = tmp_path / "old" / "sale_custom"
    new_mod = tmp_path / "new" / "sale_custom"
    for root in (old_mod, new_mod):
        root.mkdir(parents=True)
        (root / "__manifest__.py").write_text(_MANIFEST, encoding="utf-8")
        (root / "models.py").write_text("class Sale:\n    pass\n", encoding="utf-8")
    created = create_migration(
        name="Acme",
        accent="violet",
        source_version="17.0",
        target_version="18.0",
        old_project_path=str(tmp_path / "old"),
        new_project_path=str(tmp_path / "new"),
    )
    with database.connection_context():
        migration = Migration.get_by_id(created["id"])
        module = Module.create(
            migration=migration,
            technical_name="sale_custom",
            old_path=str(old_mod),
            new_path=str(new_mod),
        )
    old = sheller.open_session(
        container="odoo-old",
        database="acme",
        odoo_bin="/opt/odoo/odoo-bin",
        owner={"kind": "agent", "migration_id": migration.id},
        allow_commit=False,
    )
    new = sheller.open_session(
        container="odoo-new",
        database="acme",
        odoo_bin="/opt/odoo/odoo-bin",
        owner={"kind": "agent", "migration_id": migration.id},
        allow_commit=True,
    )

    def build():
        return StepContext(
            job_id="job-1",
            attempt=1,
            migration=migration,
            module=module,
            cartridge=load_cartridge(_MINIMAL),
            sheller=sheller,
            sessions={"old": old["id"], "new": new["id"]},
            write_keys={"old": old["write_key"], "new": new["write_key"]},
            # What the runner records: `upgrade` reopens the new side on it.
            session_targets={
                side: {
                    "container": container,
                    "database": "acme",
                    "odoo_bin": "/opt/odoo/odoo-bin",
                    "owner": {"kind": "agent", "migration_id": migration.id},
                }
                for side, container in (("old", "odoo-old"), ("new", "odoo-new"))
            },
            emit=lambda _event: None,
            provider=ScriptedProvider(ModelDouble(always_reports=True)),
        )

    return build


def run_recorded_file(path: str, ctx):
    ran: list[str] = []
    cartridge = load_cartridge_file(Path(path))
    ctx.cartridge = cartridge
    from odoo_migrator.runner.registry import resolve as registry_resolve

    def resolve(kind: str):
        runner = registry_resolve(kind)

        def wrapped(step, current):
            ran.append(step.id or step.kind)

            return runner(step, current)

        return wrapped

    outcome = run_job(cartridge, ctx, retry_limit=3, resolve=resolve)

    return outcome, ran


def test_the_stock_cartridge_runs_to_passed_against_doubles(loop_ctx):
    outcome, ran = run_recorded_file("cartridges/stock.yaml", loop_ctx())

    assert outcome == "passed"
    assert ran[:4] == ["understand", "port", "lint", "upgrade"]


def test_a_version_file_cannot_escape_its_own_section(agent_ctx, monkeypatch):
    """The wrapper used to be a `### 18.0` heading, outranked by the `## ORM`
    the file itself carries. A 17.0 → 20.0 stack then rendered as four
    unattributed `## ORM` sections, with 17.0's "do not rename it to
    `aggregator`" sitting beside 18.0's "`group_operator` is now
    `aggregator`" and nothing saying which version either belonged to.
    """
    docs = [
        RuleDoc(version="common", role="context", text="# Shared\n## Holds\nboth\n"),
        RuleDoc(version="17.0", role="source", text="# 17.0\n## ORM\nnot aggregator\n"),
        RuleDoc(version="18.0", role="target", text="# 18.0\n## ORM\nnow aggregator\n"),
    ]
    monkeypatch.setattr(
        "odoo_migrator.runner.prompting.assemble_rules", lambda *_a, **_k: docs
    )
    step = Step(kind="agent", id="port", context=["rules"], prompt="port")

    rendered = build_context(step, agent_ctx())

    older = rendered.split("--- rules/17.0.md")[1].split("--- end rules/17.0.md")[0]
    assert "not aggregator" in older
    assert "now aggregator" not in older
    assert "--- end rules/18.0.md" in rendered
    assert "--- rules/common.md" in rendered


def test_the_stack_says_which_file_is_the_one_to_satisfy(agent_ctx, monkeypatch):
    """`(source)` and `(target)` were the app's words for the ends of the
    stack, and a model reading only the label has no reason to know the
    source file describes what the module may stop doing."""
    docs = [
        RuleDoc(version="17.0", role="source", text="from\n"),
        RuleDoc(version="18.0", role="intermediate", text="through\n"),
        RuleDoc(version="19.0", role="target", text="to\n"),
    ]
    monkeypatch.setattr(
        "odoo_migrator.runner.prompting.assemble_rules", lambda *_a, **_k: docs
    )
    step = Step(kind="agent", id="port", context=["rules"], prompt="port")

    rendered = build_context(step, agent_ctx())

    assert "rules/19.0.md — the version to land on" in rendered
    assert "rules/17.0.md — the version this module is written for today" in rendered
    assert "rules/18.0.md — passed through" in rendered
