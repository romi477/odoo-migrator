"""Job executor. The stub runs in-process until the native runner takes over."""

from __future__ import annotations

from collections.abc import Callable
from typing import Protocol

STUB_STAGES = ("understand", "rewrite", "upgrade", "tests", "verdict")
# Same shape as the spike: the query string is part of the URL.
STUB_RESUME_URL = (
    "http://runner.example:5678/waiting/1"
    "?signature=6afb719692c2466a203a511d362157e8f963b46c99332b42d3fa5b03c4aa57e4"
)


class Executor(Protocol):
    def start(self, payload: dict) -> None:
        """Hand the trigger to the cartridge. The stub callbacks into the app."""


class StubExecutor:
    """Fake stages, fake callback. The suite does not need a live runner."""

    def __init__(
        self,
        *,
        progress: Callable[[dict], None],
        result: Callable[[dict], None],
        stages: tuple[str, ...] = STUB_STAGES,
        ask: str | None = None,
    ):
        self._progress = progress
        self._result = result
        self.stages = stages
        self.ask = ask

    def start(self, payload: dict) -> None:
        job_id = payload["job_id"]
        if self.ask:
            self._progress(
                {
                    "job_id": job_id,
                    "stage": "waiting_for_human",
                    "attempt": 1,
                    "resumeUrl": STUB_RESUME_URL,
                }
            )
            self._result(
                {
                    "job_id": job_id,
                    "status": "waiting_for_human",
                    "question": self.ask,
                    "resumeUrl": STUB_RESUME_URL,
                }
            )

            return
        for stage in self.stages:
            self._progress({"job_id": job_id, "stage": stage, "attempt": 1})
        self._result(
            {
                "job_id": job_id,
                "status": "passed",
                "attempts": [
                    {
                        "iteration": 1,
                        "diff": "",
                        "test_output": "",
                        "verdict": "pass",
                    }
                ],
                "findings": [],
            }
        )
