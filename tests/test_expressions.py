"""Tests for ${{ }} substitution."""

import pytest

from ciwalk.errors import ParseError
from ciwalk.expressions import (
    extract_input_defaults,
    find_expression_literals,
    format_unsupported_expression,
    parse_cli_inputs,
    substitute,
)
from ciwalk.parser import parse_workflow


def test_substitute_inputs_and_env() -> None:
    out, unresolved = substitute(
        "hello ${{ inputs.name }} / ${{ env.TAG }}",
        inputs={"name": "world"},
        env={"TAG": "v1"},
    )
    assert out == "hello world / v1"
    assert unresolved == []


def test_unresolved_expressions_left_in_place() -> None:
    out, unresolved = substitute(
        "sha=${{ github.sha }}",
        inputs={},
        env={},
    )
    assert out == "sha=${{ github.sha }}"
    assert unresolved == ["github.sha"]


def test_missing_input_key_is_unresolved() -> None:
    out, unresolved = substitute(
        "BRANCH=${{ inputs.branch }}",
        inputs={},
        env={},
    )
    assert out == "BRANCH=${{ inputs.branch }}"
    assert unresolved == ["inputs.branch"]


def test_parse_cli_inputs() -> None:
    assert parse_cli_inputs(["foo=bar", "n=1"]) == {"foo": "bar", "n": "1"}


def test_extract_input_defaults_from_workflow_dispatch() -> None:
    defaults = extract_input_defaults(
        {
            "on": {
                "workflow_dispatch": {
                    "inputs": {
                        "name": {"description": "x", "default": "ciwalk"},
                        "empty": {"type": "string"},
                    }
                }
            }
        }
    )
    assert defaults["name"] == "ciwalk"
    assert defaults["empty"] == ""


def test_workflow_substitutes_inputs_in_run() -> None:
    wf = parse_workflow(
        """
on:
  workflow_dispatch:
    inputs:
      name:
        default: friend
jobs:
  j:
    steps:
      - run: echo "hi ${{ inputs.name }}"
""",
        inputs={"name": "ankit"},
    )
    assert wf.jobs[0].steps[0].run == 'echo "hi ankit"'


def test_workflow_substitutes_env_in_run() -> None:
    wf = parse_workflow(
        """
jobs:
  j:
    env:
      APP: myapp
    steps:
      - run: echo ${{ env.APP }}
"""
    )
    assert wf.jobs[0].steps[0].run == "echo myapp"


def test_unresolved_expression_fails_loudly() -> None:
    with pytest.raises(ParseError, match="unsupported expression"):
        parse_workflow(
            """
jobs:
  j:
    steps:
      - run: echo ${{ github.sha }}
"""
        )


def test_missing_input_expression_fails_loudly() -> None:
    with pytest.raises(ParseError, match=r"inputs\.branch"):
        parse_workflow(
            """
jobs:
  j:
    steps:
      - run: echo "${{ inputs.branch }}"
"""
        )


def test_format_unsupported_expression_message() -> None:
    msg = format_unsupported_expression("inputs.branch")
    assert "unsupported expression: ${{ inputs.branch }}" in msg
    assert "Limitations" in msg


def test_find_expression_literals() -> None:
    assert find_expression_literals("x ${{ github.sha }} y") == ["github.sha"]
