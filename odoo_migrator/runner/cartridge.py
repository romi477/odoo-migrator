"""Load a cartridge file and refuse it before it can drive a live database."""

from __future__ import annotations

import hashlib
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from odoo_migrator.runner.tools import TOOL_SIDES

DETERMINISTIC = ("lint", "upgrade", "tests", "verdict", "ask_human")
KINDS = DETERMINISTIC + ("agent",)
BUILTIN_FLAGS = {"no_tests": "tests", "tests_failed": "tests", "lint_failed": "lint"}
CONTEXT_ITEMS = (
    "rules",
    "module_source",
    "prior_answers",
    "previous_diff",
    "last_failure",
)
SCHEMA_VERSION = 1
PROVIDERS = {"anthropic": frozenset({"claude-opus-5", "claude-sonnet-5"})}
SIDES = frozenset({"old", "new"})


class CartridgeError(Exception):
    """The file is not a cartridge this build will run."""


class ModelRef(BaseModel):
    model_config = ConfigDict(extra="ignore")

    provider: str
    name: str


class Budget(BaseModel):
    model_config = ConfigDict(extra="ignore")

    tool_calls: int | None = None
    tokens: int | None = None


class Step(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    kind: str = Field(alias="step")
    id: str | None = None
    when: str | None = None
    sessions: list[str] = Field(default_factory=list)
    tools: list[str] = Field(default_factory=list)
    context: list[str] = Field(default_factory=list)
    raises: list[str] = Field(default_factory=list)
    prompt: str = ""
    model: ModelRef | None = None
    budget: Budget | None = None


class Retry(BaseModel):
    model_config = ConfigDict(extra="ignore")

    resume_at: str


class FailureLane(BaseModel):
    model_config = ConfigDict(extra="ignore")

    pipeline: list[Step] = Field(default_factory=list)
    retry: Retry


class Cartridge(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    cartridge: str
    version: str
    schema_version: int = Field(alias="schema")
    model: ModelRef
    pipeline: list[Step]
    on_failure: FailureLane
    sha256: str = ""

    def step_by_id(self, name: str) -> Step | None:
        for step in (*self.pipeline, *self.on_failure.pipeline):
            if step.id == name or (step.id is None and step.kind == name):

                return step

        return None


def load_cartridge(text: str) -> Cartridge:
    """Parse YAML or JSON. The hash is the text as given, before parsing."""
    try:
        raw = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise CartridgeError(f"yaml: {exc}") from exc
    if not isinstance(raw, dict):
        raise CartridgeError("cartridge must be a mapping")
    try:
        loaded = Cartridge.model_validate(raw)
    except ValidationError as exc:
        raise CartridgeError(str(exc)) from exc
    _refuse_invalid(loaded)
    loaded.sha256 = hashlib.sha256(text.encode("utf-8")).hexdigest()

    return loaded


def load_cartridge_file(path: Path) -> Cartridge:

    return load_cartridge(Path(path).read_text(encoding="utf-8"))


def _label(step: Step) -> str:

    return step.id or step.kind


def _steps(loaded: Cartridge) -> list[Step]:

    return [*loaded.pipeline, *loaded.on_failure.pipeline]


def _check_model(ref: ModelRef) -> None:
    served = PROVIDERS.get(ref.provider)
    if served is None:
        raise CartridgeError(f"unknown provider '{ref.provider}'")
    if ref.name not in served:
        known = ", ".join(sorted(served))
        raise CartridgeError(f"provider '{ref.provider}' does not serve '{ref.name}' ({known})")


def _refuse_invalid(loaded: Cartridge) -> None:
    if loaded.schema_version != SCHEMA_VERSION:
        raise CartridgeError(f"schema {loaded.schema_version} is not implemented")
    _check_model(loaded.model)
    steps = _steps(loaded)
    seen: set[str] = set()
    for step in steps:
        if step.kind not in KINDS:
            raise CartridgeError(f"unknown step kind '{step.kind}'")
        label = _label(step)
        if step.kind == "agent" and not step.id:
            raise CartridgeError("agent step requires an id")
        if label in seen:
            raise CartridgeError(f"duplicate id '{label}'")
        seen.add(label)
        if step.model is not None:
            _check_model(step.model)
        if step.kind == "agent" and not step.prompt.strip():
            raise CartridgeError(f"agent step '{label}' requires a prompt")
        if step.kind != "agent" and step.prompt.strip():
            raise CartridgeError(f"deterministic step '{label}' must not carry a prompt")
        for side in step.sessions:
            if side not in SIDES:
                raise CartridgeError(f"unknown session '{side}'")
        opened = set(step.sessions)
        for tool in step.tools:
            sides = TOOL_SIDES.get(tool)
            if sides is None:
                raise CartridgeError(f"unknown tool '{tool}'")
            if opened.isdisjoint(sides):
                raise CartridgeError(
                    f"tool '{tool}' is not allowed on session {sorted(opened) or ['none']}"
                )
        for item in step.context:
            if item not in CONTEXT_ITEMS:
                raise CartridgeError(f"unknown context '{item}'")
    raised = {flag for step in steps for flag in step.raises}
    vocabulary = set(BUILTIN_FLAGS) | raised
    read = {step.when for step in steps if step.when}
    for flag in sorted(read):
        if flag not in vocabulary:
            raise CartridgeError(f"unknown flag '{flag}' is not raised")
    for flag in sorted(raised - read):
        raise CartridgeError(f"flag '{flag}' is raised and never read")
    names = {_label(step) for step in loaded.pipeline}
    resume_at = loaded.on_failure.retry.resume_at
    if resume_at not in names:
        raise CartridgeError(f"resume_at '{resume_at}' is not a step in the pipeline")
