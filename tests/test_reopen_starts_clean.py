"""Reopen means run the whole flow again: the module starts from an empty directory.

A module reopened on Result used to be ported over what the last run left, and
whatever the new port did not rewrite stayed and was imported with the rest.
"""

# The fixtures come from the module whose job they build; pytest finds them by name.
# ruff: noqa: F811
from pathlib import Path

from test_shell_notes_in_a_job import Capture, _run, _texts, fresh, job  # noqa: F401

from odoo_migrator.db import database
from odoo_migrator.jobs import reopen_module
from odoo_migrator.models import Job, Module


def _leave_a_port(job):
    root = Path(job.module.new_path)
    (root / "models").mkdir(parents=True)
    (root / "__manifest__.py").write_text("{'name': 'x', 'depends': []}", encoding="utf-8")
    (root / "models" / "old_attempt.py").write_text("x = 1\n", encoding="utf-8")

    return root


def _reopen(job):
    """The module passed on an earlier job; Reopen put it back; this job is the next."""
    with database.connection_context():
        Module.update(status="passed").where(Module.id == job.module_id).execute()
        Job.update(status="passed").where(Job.id == job.id).execute()
    reopen_module(job.module_id, str(job.module.migration_id))
    with database.connection_context():
        Job.update(status="queued").where(Job.id == job.id).execute()


def test_a_reopened_module_is_ported_into_an_empty_directory(app, fake_sheller, job):
    root = _leave_a_port(job)
    _reopen(job)

    _run(app, fake_sheller, job, Capture())

    assert not (root / "models" / "old_attempt.py").exists()
    assert (root / "__manifest__.py").is_file()
    assert any("cleared" in text and "before starting over" in text for text in _texts(job))


def test_the_clearing_is_spent_by_the_run_that_does_it(app, fake_sheller, job):
    _leave_a_port(job)
    _reopen(job)
    _run(app, fake_sheller, job, Capture())
    with database.connection_context():
        assert Module.get_by_id(job.module_id).start_clean is False


def test_a_module_that_was_not_reopened_keeps_what_is_there(app, fake_sheller, job):
    root = _leave_a_port(job)

    _run(app, fake_sheller, job, Capture())

    assert (root / "models" / "old_attempt.py").is_file()
