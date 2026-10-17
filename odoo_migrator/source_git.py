"""The migration's branch in the source project. Filled in by the next task."""

from __future__ import annotations


class SourceGitError(Exception):
    """The source project cannot take a characterization test."""


def ensure_branch(migration) -> str:
    raise SourceGitError("not implemented")
