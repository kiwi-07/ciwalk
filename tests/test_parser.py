"""Unit tests for the GitHub Actions subset parser."""

from __future__ import annotations

import pytest

from ciwalk.errors import ParseError
from ciwalk.model import StepKind
from ciwalk.parser import parse_workflow, select_job

MINIMAL = """
name: CI
on: push
jobs:
  build:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - name: Hello
        run: echo hello
"""


def test_parse_minimal_workflow() -> None:
    wf = parse_workflow(MINIMAL)
    assert wf.name == "CI"
    assert len(wf.jobs) == 1
    job = wf.jobs[0]
    assert job.id == "build"
    assert job.runs_on == "ubuntu-latest"
    assert len(job.steps) == 2
    assert job.steps[0].kind == StepKind.CHECKOUT
    assert job.steps[0].uses == "actions/checkout@v4"
    assert job.steps[1].kind == StepKind.RUN
    assert job.steps[1].name == "Hello"
    assert job.steps[1].run == "echo hello"


def test_default_step_name_from_run() -> None:
    wf = parse_workflow(
        """
jobs:
  j:
    steps:
      - run: |
          echo one
          echo two
"""
    )
    assert wf.jobs[0].steps[0].name == "echo one"


def test_step_env_and_working_directory() -> None:
    wf = parse_workflow(
        """
jobs:
  j:
    env:
      JOB_VAR: a
    steps:
      - name: with env
        working-directory: subdir
        env:
          STEP_VAR: b
          NUM: 1
        run: echo $STEP_VAR
"""
    )
    job = wf.jobs[0]
    assert job.env == {"JOB_VAR": "a"}
    step = job.steps[0]
    assert step.working_directory == "subdir"
    assert step.env == {"STEP_VAR": "b", "NUM": "1"}


def test_omitted_runs_on_defaults() -> None:
    wf = parse_workflow(
        """
jobs:
  j:
    steps:
      - run: true
"""
    )
    assert wf.jobs[0].runs_on == "ubuntu-latest"


def test_unsupported_runs_on() -> None:
    with pytest.raises(ParseError, match="unsupported runs-on"):
        parse_workflow(
            """
jobs:
  j:
    runs-on: windows-latest
    steps:
      - run: true
"""
        )


def test_unsupported_action() -> None:
    with pytest.raises(ParseError, match="unsupported action"):
        parse_workflow(
            """
jobs:
  j:
    steps:
      - uses: actions/setup-node@v4
"""
        )


def test_matrix_rejected() -> None:
    with pytest.raises(ParseError, match="matrix"):
        parse_workflow(
            """
jobs:
  j:
    strategy:
      matrix:
        py: [3.11, 3.12]
    steps:
      - run: true
"""
        )


def test_select_single_job() -> None:
    wf = parse_workflow(MINIMAL)
    assert select_job(wf, None).id == "build"


def test_select_job_requires_flag_when_multiple() -> None:
    wf = parse_workflow(
        """
jobs:
  a:
    steps:
      - run: true
  b:
    steps:
      - run: true
"""
    )
    with pytest.raises(ParseError, match="--job"):
        select_job(wf, None)
    assert select_job(wf, "b").id == "b"


def test_select_unknown_job() -> None:
    wf = parse_workflow(MINIMAL)
    with pytest.raises(ParseError, match="not found"):
        select_job(wf, "missing")


def test_both_uses_and_run_rejected() -> None:
    with pytest.raises(ParseError, match="both"):
        parse_workflow(
            """
jobs:
  j:
    steps:
      - uses: actions/checkout@v4
        run: echo no
"""
        )


def test_empty_steps_rejected() -> None:
    with pytest.raises(ParseError, match="steps"):
        parse_workflow(
            """
jobs:
  j:
    steps: []
"""
        )


def test_on_true_yaml_quirk_still_parses() -> None:
    # Unquoted `on:` can become key True in YAML 1.1
    wf = parse_workflow(
        """
name: quirk
on: [push]
jobs:
  j:
    steps:
      - run: echo ok
"""
    )
    assert wf.jobs[0].steps[0].run == "echo ok"


def test_if_warns_once_and_still_parses() -> None:
    wf = parse_workflow(
        """
jobs:
  j:
    steps:
      - name: a
        if: false
        run: echo a
      - name: b
        if: "github.ref == 'refs/heads/main'"
        run: echo b
"""
    )
    assert len(wf.jobs[0].steps) == 2
    assert len(wf.warnings) == 1
    assert "if:" in wf.warnings[0]


def test_reusable_workflow_job_is_skipped_not_passed() -> None:
    wf = parse_workflow(
        """
jobs:
  call:
    uses: org/repo/.github/workflows/reuse.yml@main
"""
    )
    job = wf.jobs[0]
    assert job.skip_reason is not None
    assert "reusable workflow" in job.skip_reason
    assert job.steps == []


def test_needs_job_is_skipped() -> None:
    wf = parse_workflow(
        """
jobs:
  a:
    steps:
      - run: true
  b:
    needs: a
    steps:
      - run: true
"""
    )
    by_id = {j.id: j for j in wf.jobs}
    assert by_id["a"].skip_reason is None
    assert by_id["b"].skip_reason is not None
    assert "needs" in by_id["b"].skip_reason


def test_job_level_if_is_skipped() -> None:
    wf = parse_workflow(
        """
jobs:
  j:
    if: github.ref == 'refs/heads/main'
    steps:
      - run: true
"""
    )
    assert wf.jobs[0].skip_reason is not None
    assert "job-level if" in wf.jobs[0].skip_reason


def test_custom_shell_rejected() -> None:
    with pytest.raises(ParseError, match="shell:"):
        parse_workflow(
            """
jobs:
  j:
    steps:
      - shell: python
        run: print(1)
"""
        )


def test_continue_on_error_rejected() -> None:
    with pytest.raises(ParseError, match="continue-on-error"):
        parse_workflow(
            """
jobs:
  j:
    steps:
      - continue-on-error: true
        run: false
"""
        )


def test_services_rejected() -> None:
    with pytest.raises(ParseError, match="services:"):
        parse_workflow(
            """
jobs:
  j:
    services:
      postgres:
        image: postgres
    steps:
      - run: true
"""
        )


def test_defaults_rejected() -> None:
    with pytest.raises(ParseError, match="defaults:"):
        parse_workflow(
            """
defaults:
  run:
    working-directory: src
jobs:
  j:
    steps:
      - run: true
"""
        )


def test_checkout_with_rejected() -> None:
    with pytest.raises(ParseError, match="with:"):
        parse_workflow(
            """
jobs:
  j:
    steps:
      - uses: actions/checkout@v4
        with:
          path: app
"""
        )
