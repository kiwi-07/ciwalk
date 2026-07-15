"""Internal models for parsed workflows and run state."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class StepKind(StrEnum):
    RUN = "run"
    CHECKOUT = "checkout"


class StepStatus(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    PASSED = "passed"
    FAILED = "failed"
    PAUSED = "paused"
    SKIPPED = "skipped"


class PauseAction(StrEnum):
    RETRY = "retry"
    CONTINUE = "continue"
    ABORT = "abort"


@dataclass
class Step:
    name: str
    kind: StepKind
    run: str | None = None
    uses: str | None = None
    env: dict[str, str] = field(default_factory=dict)
    working_directory: str | None = None
    index: int = 0


@dataclass
class Job:
    id: str
    name: str
    runs_on: str
    steps: list[Step]
    env: dict[str, str] = field(default_factory=dict)
    # If set, do not execute — print SKIPPED with this reason (exit non-zero).
    skip_reason: str | None = None


@dataclass
class Workflow:
    name: str | None
    jobs: list[Job]
    path: str | None = None
    warnings: list[str] = field(default_factory=list)
    # Merged workflow_dispatch/call defaults + CLI --input overrides
    inputs: dict[str, str] = field(default_factory=dict)


@dataclass
class StepResult:
    step: Step
    status: StepStatus
    exit_code: int = 0
    duration_s: float = 0.0
    output: str = ""
