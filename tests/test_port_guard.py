"""A module edited by hand since the agent last wrote it does not start.

The target is often a git checkout of the new branch, still holding the old
code, and the port is written over it in place. Git is the undo. What must
not happen is the agent overwriting — or clearing — work a person did there
and has not committed. The agent's own uncommitted port, from an earlier run
of the same module, is not that: a fingerprint of what the last run left
tells the two apart. Only the module's own directory is looked at.
"""

import shutil
import subprocess

import pytest

from odoo_migrator.contract import ContractError
from odoo_migrator.db import database, init_db
from odoo_migrator.events import EventHub
from odoo_migrator.executor import StubExecutor
from odoo_migrator.jobs import apply_result, start_job
from odoo_migrator.migrations import create_migration
from odoo_migrator.models import Batch, Job, Migration, Module
from odoo_migrator.port_guard import fingerprint, git_state, hand_edits_blocking

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")


def _git(cwd, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd, check=True, capture_output=True,
    )


@pytest.fixture
def repo(tmp_path):
    """A target checkout on branch 20.0 with two modules, committed."""
    root = tmp_path / "new"
    for name in ("sale_x", "stock_x"):
        (root / name).mkdir(parents=True)
        (root / name / "__manifest__.py").write_text(f"{{'name': '{name}'}}\n")
        (root / name / "models.py").write_text("OLD = 19\n")
    _git(root, "init", "-q")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "19.0 code on the 20.0 branch")

    return root


@pytest.fixture
def migration(tmp_path, repo):
    init_db(tmp_path / "guard.sqlite")
    created = create_migration(
        name="Acme", accent="violet", source_version="19.0", target_version="20.0",
        old_project_path=str(tmp_path / "old"), new_project_path=str(repo),
    )
    with database.connection_context():
        for name in ("sale_x", "stock_x"):
            Module.create(
                migration=created["id"], technical_name=name,
                new_path=str(repo / name),
            )

    return created


def _module(name):
    with database.connection_context():

        return Module.get(Module.technical_name == name)


def test_git_state_reads_only_the_module_directory(repo):
    (repo / "stock_x" / "models.py").write_text("NEW = 20\n")

    assert git_state(repo / "sale_x") == "clean"
    assert git_state(repo / "stock_x") == "dirty"


def test_a_directory_outside_git_is_not_a_repository(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    assert git_state(plain) == "not_git"
    assert git_state(tmp_path / "missing") == "missing"


def test_a_clean_module_starts(migration):
    assert hand_edits_blocking(migration["id"], [_module("sale_x").id]) is None


def test_an_uncommitted_port_of_another_module_does_not_stop_this_one(migration, repo):
    (repo / "stock_x" / "models.py").write_text("PORTED = 20\n")

    assert hand_edits_blocking(migration["id"], [_module("sale_x").id]) is None


def test_our_own_uncommitted_port_does_not_stop_a_rerun(migration, repo):
    (repo / "sale_x" / "models.py").write_text("PORTED = 20\n")
    with database.connection_context():
        Module.update(port_fingerprint=fingerprint(repo / "sale_x")).where(
            Module.technical_name == "sale_x"
        ).execute()

    assert hand_edits_blocking(migration["id"], [_module("sale_x").id]) is None


def test_a_hand_edit_after_the_agent_stops_the_module(migration, repo):
    (repo / "sale_x" / "models.py").write_text("PORTED = 20\n")
    with database.connection_context():
        Module.update(port_fingerprint=fingerprint(repo / "sale_x")).where(
            Module.technical_name == "sale_x"
        ).execute()
    (repo / "sale_x" / "models.py").write_text("PORTED = 20\nFIXED_BY_HAND = 1\n")

    message = hand_edits_blocking(migration["id"], [_module("sale_x").id])

    assert "sale_x" in message
    assert "edited by hand" in message
    with pytest.raises(ContractError, match="edited by hand"):
        start_job(
            migration_id=migration["id"], module_id=_module("sale_x").id,
            executor=StubExecutor(progress=lambda body: None, result=lambda body: None),
            hub=EventHub(),
        )


def test_a_hand_edit_before_any_run_stops_the_module(migration, repo):
    (repo / "sale_x" / "models.py").write_text("MINE = 1\n")

    assert "edited by hand" in hand_edits_blocking(migration["id"], [_module("sale_x").id])


def test_a_finished_run_records_what_it_left(migration, repo):
    module = _module("sale_x")
    with database.connection_context():
        batch = Batch.create(migration=Migration.get_by_id(migration["id"]), status="running")
        Job.create(batch=batch, module=module, status="running", job_id="job-1")
    (repo / "sale_x" / "models.py").write_text("PORTED = 20\n")

    apply_result({"job_id": "job-1", "status": "passed"}, EventHub())

    assert _module("sale_x").port_fingerprint == fingerprint(repo / "sale_x")
    assert hand_edits_blocking(migration["id"], [module.id]) is None


def test_fingerprint_ignores_bytecode(repo):
    before = fingerprint(repo / "sale_x")
    (repo / "sale_x" / "__pycache__").mkdir()
    (repo / "sale_x" / "__pycache__" / "models.cpython-312.pyc").write_bytes(b"\x00")

    assert fingerprint(repo / "sale_x") == before


def test_a_fingerprint_already_recorded_still_matches(repo):
    """The walk changed; what it hashes for an ordinary module did not.
    Every module whose uncommitted port was fingerprinted before would
    otherwise read as edited by hand on its next start."""
    import hashlib
    from pathlib import Path

    root = repo / "sale_x"
    (root / "views").mkdir()
    (root / "views" / "sale.xml").write_text("<odoo/>\n")
    (root / "__pycache__").mkdir()
    (root / "__pycache__" / "models.cpython-312.pyc").write_bytes(b"\x00")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        noise = "__pycache__" in Path(relative).parts or relative.endswith((".pyc", ".pyo"))
        if not path.is_file() or noise:
            continue
        digest.update(relative.encode("utf-8") + b"\0")
        digest.update(path.read_bytes() + b"\0")

    assert fingerprint(root) == digest.hexdigest()


def test_our_own_port_in_a_module_that_is_its_own_repository(tmp_path):
    """The repository inside the module was hashed with it. Any git command
    there — a fetch, the index `git status` refreshes — changed the
    fingerprint, and the agent's own uncommitted port was refused as a hand
    edit on every start after."""
    from odoo_migrator.port_guard import record_fingerprint

    init_db(tmp_path / "own.sqlite")
    root = tmp_path / "new"
    module_dir = root / "sale_own"
    module_dir.mkdir(parents=True)
    (module_dir / "__manifest__.py").write_text("{'name': 'sale_own'}\n")
    _git(module_dir, "init", "-q")
    _git(module_dir, "add", ".")
    _git(module_dir, "commit", "-qm", "19.0 code")
    created = create_migration(
        name="Acme", accent="violet", source_version="19.0", target_version="20.0",
        old_project_path=str(tmp_path / "old"), new_project_path=str(root),
    )
    with database.connection_context():
        module = Module.create(
            migration=created["id"], technical_name="sale_own", new_path=str(module_dir)
        )
    (module_dir / "models.py").write_text("NEW = 20\n")
    with database.connection_context():
        module = Module.get_by_id(module.id)
        record_fingerprint(module)
        module.save()

    (module_dir / ".git" / "FETCH_HEAD").write_text("0" * 40 + "\tbranch '20.0'\n")

    assert hand_edits_blocking(created["id"], [module.id]) is None


def test_a_file_the_app_cannot_read_still_fingerprints(repo):
    """The container can leave a file this user cannot read. Fingerprinting
    raised, and that took the whole result down with it."""
    import os

    secret = repo / "sale_x" / "data.bin"
    secret.write_bytes(b"\x01")
    secret.chmod(0)
    try:
        if os.access(secret, os.R_OK):
            pytest.skip("this user reads everything")
        assert fingerprint(repo / "sale_x")
    finally:
        secret.chmod(0o600)


def test_bytecode_odoo_left_in_the_checkout_is_not_a_change(repo):
    """The target imports the mounted module and leaves __pycache__ behind.
    Unless the repository ignores it, git lists it as untracked."""
    cache = repo / "sale_x" / "__pycache__"
    cache.mkdir()
    (cache / "models.cpython-312.pyc").write_bytes(b"\x00")

    assert git_state(repo / "sale_x") == "clean"


def test_a_module_outside_git_starts_with_a_warning_in_its_log(tmp_path):
    """The project directory is put under git on the first start. A module
    kept outside it is not, and nothing can undo a port written there."""
    from odoo_migrator.runlog import lines

    init_db(tmp_path / "plain.sqlite")
    plain = tmp_path / "plain"
    plain.mkdir()
    elsewhere = tmp_path / "elsewhere" / "sale_x"
    elsewhere.mkdir(parents=True)
    created = create_migration(
        name="Plain", accent="violet", source_version="19.0", target_version="20.0",
        old_project_path=str(tmp_path / "old"), new_project_path=str(plain),
    )
    with database.connection_context():
        module = Module.create(
            migration=created["id"], technical_name="sale_x", new_path=str(elsewhere),
        )

    job = start_job(
        migration_id=created["id"], module_id=module.id,
        executor=StubExecutor(progress=lambda body: None, result=lambda body: None),
        hub=EventHub(),
    )

    texts = [line["text"] for line in lines(job.job_id)]
    assert any("not in a git repository" in text for text in texts)


def test_a_blocked_start_names_the_module_and_its_files(migration, repo):
    from odoo_migrator.port_guard import HandEdits

    (repo / "sale_x" / "models.py").write_text("MINE = 1\n")
    (repo / "sale_x" / "tests.py").write_text("NEW = 1\n")

    with pytest.raises(HandEdits) as refused:
        start_job(
            migration_id=migration["id"], module_id=_module("sale_x").id,
            executor=StubExecutor(progress=lambda body: None, result=lambda body: None),
            hub=EventHub(),
        )

    [edited] = refused.value.modules
    assert edited["technical_name"] == "sale_x"
    assert edited["files"] == ["M models.py", "?? tests.py"]


def test_changed_files_are_named_as_they_are_on_disk(repo):
    """Porcelain quotes and escapes a name that is not plain ASCII, and a
    rename is "old -> new". The card listed `"donn\\303\\251es.xml"`."""
    from odoo_migrator.port_guard import changed_files

    (repo / "sale_x" / "données.xml").write_text("<odoo/>\n")
    (repo / "sale_x" / "with space.py").write_text("X = 1\n")
    _git(repo, "mv", "sale_x/models.py", "sale_x/renamed.py")

    assert sorted(changed_files(repo / "sale_x")) == [
        "?? données.xml",
        "?? with space.py",
        "R renamed.py",
    ]


def test_discard_restores_the_module_and_leaves_its_neighbour(migration, repo):
    from odoo_migrator.port_guard import discard_hand_edits

    (repo / "sale_x" / "models.py").write_text("MINE = 1\n")
    (repo / "sale_x" / "tests.py").write_text("NEW = 1\n")
    _git(repo, "add", "sale_x/tests.py")
    (repo / "stock_x" / "models.py").write_text("PORTED = 20\n")

    discard_hand_edits(migration["id"], _module("sale_x").id)

    assert (repo / "sale_x" / "models.py").read_text() == "OLD = 19\n"
    assert not (repo / "sale_x" / "tests.py").exists()
    assert git_state(repo / "sale_x") == "clean"
    assert (repo / "stock_x" / "models.py").read_text() == "PORTED = 20\n"


def test_discard_is_refused_outside_git_and_while_running(tmp_path, migration, repo):
    from odoo_migrator.port_guard import discard_hand_edits

    module = _module("sale_x")
    with database.connection_context():
        batch = Batch.create(migration=Migration.get_by_id(migration["id"]), status="running")
        Job.create(batch=batch, module=module, status="running", job_id="job-busy")
    with pytest.raises(ContractError, match="running"):
        discard_hand_edits(migration["id"], module.id)
    with database.connection_context():
        Job.update(status="failed").where(Job.job_id == "job-busy").execute()
        plain = tmp_path / "plain_x"
        plain.mkdir()
        Module.update(new_path=str(plain)).where(Module.id == module.id).execute()
    with pytest.raises(ContractError, match="git"):
        discard_hand_edits(migration["id"], module.id)
    with pytest.raises(ContractError, match="unknown module"):
        discard_hand_edits("another-migration", module.id)


def test_keep_takes_the_files_as_they_are(migration, repo):
    from odoo_migrator.port_guard import keep_hand_edits

    (repo / "sale_x" / "models.py").write_text("PORTED = 20\n")

    keep_hand_edits(migration["id"], _module("sale_x").id)

    assert (repo / "sale_x" / "models.py").read_text() == "PORTED = 20\n"
    assert hand_edits_blocking(migration["id"], [_module("sale_x").id]) is None


def _dirty_sale_custom(client, tmp_path):
    from test_batch import _prepared

    row, sale = _prepared(client)
    checkout = tmp_path / "checkout"
    (checkout / "sale_custom").mkdir(parents=True)
    (checkout / "sale_custom" / "models.py").write_text("OLD = 17\n")
    _git(checkout, "init", "-q")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-qm", "source")
    (checkout / "sale_custom" / "models.py").write_text("PORTED = 18\n")
    with database.connection_context():
        Module.update(new_path=str(checkout / "sale_custom"), path_problem="").where(
            Module.id == sale["id"]
        ).execute()
    client.post("/run/queue/add", data={"step": "1"}, follow_redirects=True)

    return row, sale, checkout


def test_run_shows_hand_edits_in_the_workspace_with_both_ways_out(client, tmp_path):
    _row, sale, _checkout = _dirty_sale_custom(client, tmp_path)

    page = client.post("/run/queue/run", follow_redirects=True)

    top = page.text[: page.text.index("pipeline-panels")]
    assert "edited by hand" not in top
    panel = page.text[page.text.index("data-hand-edits") :]
    assert "sale_custom" in panel
    assert "M models.py" in panel
    assert f'action="/run/modules/{sale["id"]}/discard"' in panel
    assert "data-confirm-title" in panel
    assert f'action="/run/modules/{sale["id"]}/keep"' in panel


def test_keep_and_run_starts_the_queue(client, tmp_path):
    from odoo_migrator.pipeline_queue import run_state

    row, sale, checkout = _dirty_sale_custom(client, tmp_path)
    client.app.state.executor = type("Hold", (), {"start": lambda self, payload: None})()

    page = client.post(f"/run/modules/{sale['id']}/keep", follow_redirects=True)

    assert page.status_code == 200
    assert "data-hand-edits" not in page.text
    assert run_state(row.id) == "running"
    assert (checkout / "sale_custom" / "models.py").read_text() == "PORTED = 18\n"


def test_discard_and_run_starts_the_queue_on_the_committed_code(client, tmp_path):
    from odoo_migrator.pipeline_queue import run_state

    row, sale, checkout = _dirty_sale_custom(client, tmp_path)
    client.app.state.executor = type("Hold", (), {"start": lambda self, payload: None})()

    page = client.post(f"/run/modules/{sale['id']}/discard", follow_redirects=True)

    assert page.status_code == 200
    assert run_state(row.id) == "running"
    assert (checkout / "sale_custom" / "models.py").read_text() == "OLD = 17\n"


def test_run_shows_a_persons_commits_and_only_the_way_that_keeps_them(client, tmp_path):
    """Committed changes have nothing to discard: the card lists the commits
    and offers to run with them, the agent carrying them into the port."""
    from test_batch import _prepared

    _row, sale = _prepared(client)
    checkout = tmp_path / "checkout"
    (checkout / "sale_custom").mkdir(parents=True)
    (checkout / "sale_custom" / "models.py").write_text("PORTED = 18\n")
    subprocess.run(["git", "init", "-q"], cwd=checkout, check=True)
    for author, text, subject in (
        ("odoo-migrator <odoo-migrator@localhost>", None, "odoo-migrator: port sale_custom"),
        ("Ann <ann@example.com>", "PORTED = 18\nFIXED = 1\n", "keep the shop margin"),
    ):
        if text:
            (checkout / "sale_custom" / "models.py").write_text(text)
        name, email = author[:-1].split(" <")
        _git(checkout, "add", ".")
        subprocess.run(
            ["git", "-c", f"user.name={name}", "-c", f"user.email={email}",
             "commit", "-qm", subject],
            cwd=checkout, check=True, capture_output=True,
        )
    with database.connection_context():
        Module.update(new_path=str(checkout / "sale_custom"), path_problem="").where(
            Module.id == sale["id"]
        ).execute()
    client.post("/run/queue/add", data={"step": "1"}, follow_redirects=True)

    page = client.post("/run/queue/run", follow_redirects=True)

    panel = page.text[page.text.index("data-hand-edits") :]
    assert "keep the shop margin" in panel and "Ann" in panel
    assert f'action="/run/modules/{sale["id"]}/keep"' in panel
    assert f'action="/run/modules/{sale["id"]}/discard"' not in panel
