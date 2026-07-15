"""Cheap GitHub Actions ${{ }} string templating for MVP."""

from __future__ import annotations

import re
from typing import Any

# Matches ${{ inputs.foo }}, ${{ env.BAR }}, etc. (property access only).
EXPR_RE = re.compile(r"\$\{\{\s*([^}]+?)\s*\}\}")
INPUTS_RE = re.compile(r"^inputs\.([A-Za-z_][A-Za-z0-9_]*)$")
ENV_RE = re.compile(r"^env\.([A-Za-z_][A-Za-z0-9_]*)$")


def format_unsupported_expression(expr: str) -> str:
    """User-facing error for an expression we will not evaluate."""
    return (
        f"unsupported expression: ${{{{ {expr} }}}} — "
        "expression evaluation not implemented, see Limitations."
    )


def substitute(
    text: str,
    *,
    inputs: dict[str, str],
    env: dict[str, str],
) -> tuple[str, list[str]]:
    """
    Replace ${{ inputs.* }} and ${{ env.* }} in text when keys are known.

    Unknown inputs.*/env.* keys and any other expression form are left unchanged
    in the text and listed in the unresolved return value so callers can fail loudly
    (never silently rewrite them to empty strings).
    """
    unresolved: list[str] = []

    def repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        expr = match.group(1).strip()
        m_in = INPUTS_RE.match(expr)
        if m_in:
            key = m_in.group(1)
            if key not in inputs:
                unresolved.append(expr)
                return raw
            return inputs[key]
        m_env = ENV_RE.match(expr)
        if m_env:
            key = m_env.group(1)
            if key not in env:
                unresolved.append(expr)
                return raw
            return env[key]
        unresolved.append(expr)
        return raw

    return EXPR_RE.sub(repl, text), unresolved


def find_expression_literals(text: str) -> list[str]:
    """Return stripped inner expressions for any remaining `${{ ... }}` literals."""
    return [m.group(1).strip() for m in EXPR_RE.finditer(text)]


def extract_input_defaults(workflow_root: dict[str, Any]) -> dict[str, str]:
    """Pull default values from on.workflow_dispatch / on.workflow_call inputs."""
    on = workflow_root.get("on")
    if on is None:
        on = workflow_root.get(True)  # YAML 1.1 quirk for bare `on:`
    if on is None:
        return {}

    # on: push  / on: [push] — no inputs
    if not isinstance(on, dict):
        return {}

    out: dict[str, str] = {}
    for trigger in ("workflow_dispatch", "workflow_call"):
        block = on.get(trigger)
        if not isinstance(block, dict):
            continue
        inputs_block = block.get("inputs")
        if not isinstance(inputs_block, dict):
            continue
        for name, spec in inputs_block.items():
            if not isinstance(name, str):
                continue
            if isinstance(spec, dict) and "default" in spec:
                default = spec["default"]
                if default is None:
                    out[name] = ""
                elif isinstance(default, (str, int, float, bool)):
                    out[name] = str(default)
            elif name not in out:
                out[name] = ""
    return out


def parse_cli_inputs(items: list[str] | None) -> dict[str, str]:
    """Parse --input key=value flags into a dict."""
    if not items:
        return {}
    out: dict[str, str] = {}
    for item in items:
        if "=" not in item:
            raise ValueError(f"Invalid --input {item!r}; expected KEY=VALUE")
        key, _, value = item.partition("=")
        key = key.strip()
        if not key:
            raise ValueError(f"Invalid --input {item!r}; empty key")
        out[key] = value
    return out
