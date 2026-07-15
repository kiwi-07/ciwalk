"""Skipped-job session behavior (no silent pass)."""

from __future__ import annotations

from pathlib import Path

from ciwalk.model import Job
from ciwalk.session import JobSession


def test_skipped_job_exits_nonzero(tmp_path: Path) -> None:
    job = Job(
        id="call",
        name="call",
        runs_on="",
        steps=[],
        skip_reason="reusable workflow / unsupported feature: uses ./.github/workflows/x.yml",
    )
    code = JobSession(job, workdir=tmp_path).run()
    assert code == 1
