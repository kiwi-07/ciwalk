"""Unit tests for job exit semantics."""

from __future__ import annotations

from ciwalk.model import Step, StepKind, StepResult, StepStatus


def job_exit_code(results: list[StepResult]) -> int:
    """Mirror session end-of-run rule: any FAILED → non-zero."""
    return 1 if any(r.status == StepStatus.FAILED for r in results) else 0


def _step(name: str = "s") -> Step:
    return Step(name=name, kind=StepKind.RUN, run="true", index=0)


def test_continue_after_failed_step_is_non_zero() -> None:
    results = [
        StepResult(step=_step("a"), status=StepStatus.PASSED),
        StepResult(step=_step("b"), status=StepStatus.FAILED, exit_code=1),
        StepResult(step=_step("c"), status=StepStatus.PASSED),
    ]
    assert job_exit_code(results) == 1


def test_all_passed_is_zero() -> None:
    results = [
        StepResult(step=_step("a"), status=StepStatus.PASSED),
        StepResult(step=_step("b"), status=StepStatus.SKIPPED),
    ]
    assert job_exit_code(results) == 0
