"""Parse GitHub Actions workflow YAML into ciwalk models."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from ciwalk.errors import ParseError
from ciwalk.expressions import (
    extract_input_defaults,
    format_unsupported_expression,
    substitute,
)
from ciwalk.model import Job, Step, StepKind, Workflow

SUPPORTED_RUNS_ON = frozenset({"ubuntu-latest"})
DEFAULT_RUNS_ON = "ubuntu-latest"
CHECKOUT_RE = re.compile(r"^actions/checkout(@.+)?$")

IF_WARNING = (
    "Ignoring step-level 'if:' conditions (not evaluated in MVP); matching steps still run."
)


def parse_workflow_file(
    path: str | Path,
    *,
    inputs: dict[str, str] | None = None,
) -> Workflow:
    path = Path(path)
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ParseError(f"Cannot read workflow file: {path}") from exc
    workflow = parse_workflow(text, inputs=inputs)
    workflow.path = str(path.resolve())
    return workflow


def parse_workflow(
    text: str,
    *,
    inputs: dict[str, str] | None = None,
) -> Workflow:
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ParseError(f"Invalid YAML: {exc}") from exc

    if data is None:
        raise ParseError("Workflow file is empty")
    if not isinstance(data, dict):
        raise ParseError("Workflow root must be a mapping")

    name = data.get("name")
    if name is not None and not isinstance(name, str):
        raise ParseError("'name' must be a string")

    if data.get("defaults") is not None:
        raise ParseError(
            "'defaults:' is not supported in MVP (including defaults.run.working-directory)."
        )

    jobs_raw = data.get("jobs")
    if not isinstance(jobs_raw, dict) or not jobs_raw:
        raise ParseError("Workflow must define a non-empty 'jobs' mapping")

    # CLI inputs override workflow defaults.
    merged_inputs = {**extract_input_defaults(data), **(inputs or {})}

    warnings: list[str] = []
    jobs: list[Job] = []
    unresolved_all: list[str] = []

    for job_id, job_body in jobs_raw.items():
        if not isinstance(job_id, str):
            raise ParseError(f"Job id must be a string, got {job_id!r}")
        job, unresolved = _parse_job(job_id, job_body, warnings, merged_inputs)
        jobs.append(job)
        unresolved_all.extend(unresolved)

    if unresolved_all:
        # Fail loudly — never silently rewrite unsupported expressions to ""
        # (that produces false-positive Job passed, e.g. branch=).
        unique = sorted(set(unresolved_all))
        raise ParseError(format_unsupported_expression(unique[0]))

    return Workflow(name=name, jobs=jobs, warnings=warnings, inputs=merged_inputs)


def select_job(workflow: Workflow, job_id: str | None) -> Job:
    if job_id is not None:
        for job in workflow.jobs:
            if job.id == job_id:
                return job
        available = ", ".join(j.id for j in workflow.jobs)
        raise ParseError(f"Job {job_id!r} not found. Available: {available}")

    if len(workflow.jobs) == 1:
        return workflow.jobs[0]

    available = ", ".join(j.id for j in workflow.jobs)
    raise ParseError(f"Workflow has multiple jobs ({available}). Pass --job <id> to choose one.")


def _parse_job(
    job_id: str,
    body: Any,
    warnings: list[str],
    inputs: dict[str, str],
) -> tuple[Job, list[str]]:
    if not isinstance(body, dict):
        raise ParseError(f"Job {job_id!r} must be a mapping")

    display_name = body.get("name", job_id)
    if not isinstance(display_name, str):
        raise ParseError(f"Job {job_id!r}: 'name' must be a string")

    unresolved: list[str] = []

    # Reusable workflow call: jobs.<id>.uses without local steps
    job_uses = body.get("uses")
    if isinstance(job_uses, str) and job_uses.strip():
        return (
            Job(
                id=job_id,
                name=display_name,
                runs_on="",
                steps=[],
                skip_reason=f"reusable workflow / unsupported feature: uses {job_uses}",
            ),
            unresolved,
        )

    if body.get("needs") is not None:
        return (
            Job(
                id=job_id,
                name=display_name,
                runs_on="",
                steps=[],
                skip_reason="reusable workflow / unsupported feature: needs",
            ),
            unresolved,
        )

    if body.get("if") is not None:
        return (
            Job(
                id=job_id,
                name=display_name,
                runs_on="",
                steps=[],
                skip_reason="reusable workflow / unsupported feature: job-level if",
            ),
            unresolved,
        )

    if "strategy" in body and body["strategy"] is not None:
        raise ParseError(f"Job {job_id!r}: matrix/strategy builds are not supported in MVP")

    if body.get("services") is not None:
        raise ParseError(f"Job {job_id!r}: 'services:' is not supported in MVP")

    if body.get("defaults") is not None:
        raise ParseError(
            f"Job {job_id!r}: 'defaults:' is not supported in MVP "
            "(including defaults.run.working-directory)."
        )

    runs_on_raw = body.get("runs-on", DEFAULT_RUNS_ON)
    if isinstance(runs_on_raw, str):
        runs_on_sub, u = substitute(runs_on_raw, inputs=inputs, env={})
        unresolved.extend(u)
        runs_on_raw = runs_on_sub
    runs_on = _normalize_runs_on(job_id, runs_on_raw)
    if runs_on not in SUPPORTED_RUNS_ON:
        raise ParseError(
            f"Job {job_id!r}: unsupported runs-on {runs_on!r}. "
            f"MVP supports: {', '.join(sorted(SUPPORTED_RUNS_ON))} "
            "(or omit runs-on)."
        )

    job_env, env_unresolved = _parse_env_with_expr(
        body.get("env"),
        where=f"Job {job_id!r}",
        inputs=inputs,
        env={},
    )
    unresolved.extend(env_unresolved)

    # Second pass: allow ${{ env.X }} within job env values using sibling keys.
    job_env, env_unresolved2 = _resubstitute_env_map(job_env, inputs=inputs)
    unresolved.extend(env_unresolved2)

    steps_raw = body.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise ParseError(f"Job {job_id!r} must define a non-empty 'steps' list")

    steps: list[Step] = []
    for i, step_body in enumerate(steps_raw):
        step, step_unresolved = _parse_step(job_id, i, step_body, warnings, inputs, job_env)
        steps.append(step)
        unresolved.extend(step_unresolved)

    name_sub, name_u = substitute(display_name, inputs=inputs, env=job_env)
    unresolved.extend(name_u)

    return (
        Job(id=job_id, name=name_sub, runs_on=runs_on, steps=steps, env=job_env),
        unresolved,
    )


def _normalize_runs_on(job_id: str, value: Any) -> str:
    if value is None:
        return DEFAULT_RUNS_ON
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        if not value or not all(isinstance(v, str) for v in value):
            raise ParseError(f"Job {job_id!r}: invalid runs-on list")
        if len(value) > 1:
            raise ParseError(f"Job {job_id!r}: multi-label runs-on is not supported ({value!r})")
        return value[0]
    raise ParseError(f"Job {job_id!r}: runs-on must be a string")


def _parse_step(
    job_id: str,
    index: int,
    body: Any,
    warnings: list[str],
    inputs: dict[str, str],
    job_env: dict[str, str],
) -> tuple[Step, list[str]]:
    if not isinstance(body, dict):
        raise ParseError(f"Job {job_id!r} step {index}: must be a mapping")

    where = f"Job {job_id!r} step {index}"
    unresolved: list[str] = []

    if body.get("if") is not None and IF_WARNING not in warnings:
        warnings.append(IF_WARNING)

    if body.get("shell") is not None:
        raise ParseError(f"{where}: custom 'shell:' is not supported in MVP")

    if body.get("continue-on-error") is not None:
        raise ParseError(f"{where}: 'continue-on-error:' is not supported in MVP")

    step_env, env_u = _parse_env_with_expr(
        body.get("env"),
        where=where,
        inputs=inputs,
        env=job_env,
    )
    unresolved.extend(env_u)
    step_env, env_u2 = _resubstitute_env_map(step_env, inputs=inputs, base=job_env)
    unresolved.extend(env_u2)

    # Context for ${{ env.* }} in run/name/working-directory: job + step env.
    env_ctx = {**job_env, **step_env}

    working_directory = body.get("working-directory")
    if working_directory is not None:
        if not isinstance(working_directory, str):
            raise ParseError(f"{where}: working-directory must be a string")
        working_directory, wd_u = substitute(working_directory, inputs=inputs, env=env_ctx)
        unresolved.extend(wd_u)

    uses = body.get("uses")
    run = body.get("run")
    name = body.get("name")

    if uses and run:
        raise ParseError(f"{where}: cannot set both 'uses' and 'run'")
    if not uses and not run:
        raise ParseError(f"{where}: must set 'uses' or 'run'")

    if uses is not None:
        if not isinstance(uses, str):
            raise ParseError(f"{where}: 'uses' must be a string")
        uses, uses_u = substitute(uses, inputs=inputs, env=env_ctx)
        unresolved.extend(uses_u)
        if not CHECKOUT_RE.match(uses):
            raise ParseError(
                f"{where}: unsupported action {uses!r}. MVP only allows actions/checkout@*."
            )
        if body.get("with") is not None:
            raise ParseError(
                f"{where}: actions/checkout 'with:' is not supported in MVP "
                "(bind-mount uses the host --workdir / cwd as /github/workspace)."
            )
        if isinstance(name, str) and name:
            step_name, name_u = substitute(name, inputs=inputs, env=env_ctx)
            unresolved.extend(name_u)
        else:
            step_name = f"Checkout ({uses})"
        return (
            Step(
                name=step_name,
                kind=StepKind.CHECKOUT,
                uses=uses,
                env=step_env,
                working_directory=working_directory,
                index=index,
            ),
            unresolved,
        )

    # YAML 1.1 turns unquoted `true`/`false`/`yes` into bools — coerce scalars.
    if isinstance(run, bool):
        run = "true" if run else "false"
    elif isinstance(run, (int, float)):
        run = str(run)

    if not isinstance(run, str) or not run.strip():
        raise ParseError(f"{where}: 'run' must be a non-empty string")

    run, run_u = substitute(run, inputs=inputs, env=env_ctx)
    unresolved.extend(run_u)

    if isinstance(name, str) and name:
        step_name, name_u = substitute(name, inputs=inputs, env=env_ctx)
        unresolved.extend(name_u)
    else:
        first_line = run.strip().splitlines()[0] if run.strip() else "run"
        step_name = first_line if len(first_line) <= 60 else first_line[:57] + "..."

    return (
        Step(
            name=step_name,
            kind=StepKind.RUN,
            run=run,
            env=step_env,
            working_directory=working_directory,
            index=index,
        ),
        unresolved,
    )


def _parse_env_with_expr(
    value: Any,
    *,
    where: str,
    inputs: dict[str, str],
    env: dict[str, str],
) -> tuple[dict[str, str], list[str]]:
    if value is None:
        return {}, []
    if not isinstance(value, dict):
        raise ParseError(f"{where}: 'env' must be a mapping")
    out: dict[str, str] = {}
    unresolved: list[str] = []
    for k, v in value.items():
        if not isinstance(k, str):
            raise ParseError(f"{where}: env keys must be strings")
        if v is None:
            raw = ""
        elif isinstance(v, (str, int, float, bool)):
            raw = str(v)
        else:
            raise ParseError(f"{where}: env[{k!r}] must be a scalar")
        substituted, u = substitute(raw, inputs=inputs, env=env)
        unresolved.extend(u)
        out[k] = substituted
    return out, unresolved


def _resubstitute_env_map(
    env_map: dict[str, str],
    *,
    inputs: dict[str, str],
    base: dict[str, str] | None = None,
) -> tuple[dict[str, str], list[str]]:
    """Re-run env.* substitution now that sibling env keys are known."""
    ctx = {**(base or {}), **env_map}
    out: dict[str, str] = {}
    unresolved: list[str] = []
    for k, v in env_map.items():
        substituted, u = substitute(v, inputs=inputs, env=ctx)
        unresolved.extend(u)
        out[k] = substituted
    return out, unresolved
