"""Docker container lifecycle for a single job."""

from __future__ import annotations

import re
import uuid
from collections.abc import Callable, Mapping
from pathlib import Path

import docker
from docker.errors import DockerException, ImageNotFound, NotFound
from docker.models.containers import Container

from ciwalk.errors import DockerError

DEFAULT_IMAGE = "ciwalk-runner:latest"
WORKSPACE = "/github/workspace"
CIWALK_LABEL = "ciwalk"
CIWALK_JOB_LABEL = "ciwalk.job"

# Match GitHub Actions default ubuntu `run:` shell (non-login, no rc files).
DEBUG_SHELL = ["bash", "--noprofile", "--norc"]


def gha_bash_command(script: str) -> list[str]:
    """GitHub Actions default ubuntu `run:` shell (bash -eo pipefail)."""
    return ["bash", "--noprofile", "--norc", "-eo", "pipefail", "-c", script]


def resolve_workdir(working_directory: str | None) -> str:
    """Map a step working-directory onto an absolute container path."""
    workdir = working_directory or WORKSPACE
    if not workdir.startswith("/"):
        workdir = f"{WORKSPACE.rstrip('/')}/{workdir.lstrip('./')}"
    return workdir


def base_job_env(job_id: str) -> dict[str, str]:
    return {
        "CI": "true",
        "GITHUB_ACTIONS": "true",
        "GITHUB_WORKSPACE": WORKSPACE,
        "GITHUB_JOB": job_id,
    }


def container_name_for(job_id: str) -> str:
    """Docker-safe name with ciwalk- prefix (for cleanup + human dig)."""
    safe = re.sub(r"[^a-zA-Z0-9_.-]", "-", job_id).strip("-") or "job"
    return f"ciwalk-{safe[:40]}-{uuid.uuid4().hex[:8]}"


def list_ciwalk_containers(client: docker.DockerClient | None = None):
    """Return all containers (any state) owned by ciwalk."""
    if client is None:
        try:
            client = docker.from_env()
            client.ping()
        except DockerException as exc:
            raise DockerError(
                "Cannot connect to Docker. Is the Docker daemon running?"
            ) from exc
    try:
        by_label = client.containers.list(all=True, filters={"label": CIWALK_LABEL})
        by_name = client.containers.list(all=True, filters={"name": "ciwalk-"})
    except DockerException as exc:
        raise DockerError(f"Failed to list containers: {exc}") from exc

    seen: set[str] = set()
    out = []
    for c in [*by_label, *by_name]:
        if c.id not in seen:
            seen.add(c.id)
            out.append(c)
    return out


class JobContainer:
    """Owns one long-lived container for a job run."""

    def __init__(
        self,
        *,
        image: str = DEFAULT_IMAGE,
        workdir: Path,
        job_id: str,
        keep: bool = False,
    ) -> None:
        self.image = image
        self.workdir = workdir.resolve()
        self.job_id = job_id
        self.keep = keep
        self._client: docker.DockerClient | None = None
        self.container: Container | None = None

    def __enter__(self) -> JobContainer:
        self.start()
        return self

    def __exit__(self, *_) -> None:
        self.stop()

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            try:
                self._client = docker.from_env()
                self._client.ping()
            except DockerException as exc:
                raise DockerError(
                    "Cannot connect to Docker. Is the Docker daemon running?"
                ) from exc
        return self._client

    def start(self) -> None:
        if not self.workdir.is_dir():
            raise DockerError(f"Workspace path is not a directory: {self.workdir}")

        self._ensure_image()
        try:
            self.container = self.client.containers.run(
                self.image,
                command=["sleep", "infinity"],
                detach=True,
                tty=True,
                name=container_name_for(self.job_id),
                working_dir=WORKSPACE,
                volumes={
                    str(self.workdir): {"bind": WORKSPACE, "mode": "rw"},
                },
                environment=base_job_env(self.job_id),
                labels={CIWALK_LABEL: "1", CIWALK_JOB_LABEL: self.job_id},
            )
        except DockerException as exc:
            raise DockerError(f"Failed to start container: {exc}") from exc

    def stop(self) -> None:
        if self.container is None:
            return
        try:
            if self.keep:
                return
            self.container.remove(force=True)
        except DockerException:
            pass
        finally:
            if not self.keep:
                self.container = None

    def container_id(self) -> str:
        if self.container is None:
            raise DockerError("Container is not running")
        return self.container.short_id

    def exec_run(
        self,
        command: str,
        *,
        env: Mapping[str, str] | None = None,
        working_directory: str | None = None,
        on_chunk: Callable[[str], None] | None = None,
    ) -> tuple[int, str]:
        """Run a shell command; stream output via on_chunk; return (exit_code, output)."""
        container = self._require_container()
        workdir = resolve_workdir(working_directory)
        full_env = {**base_job_env(self.job_id), **(env or {})}
        env_list = [f"{k}={v}" for k, v in full_env.items()]

        api = self.client.api
        try:
            exec_id = api.exec_create(
                container.id,
                gha_bash_command(command),
                workdir=workdir,
                environment=env_list,
                tty=False,
            )["Id"]
            stream = api.exec_start(exec_id, stream=True, demux=False)
        except DockerException as exc:
            raise DockerError(f"docker exec failed: {exc}") from exc

        parts: list[str] = []
        try:
            for chunk in stream:
                if chunk is None:
                    continue
                if isinstance(chunk, tuple):
                    chunk = b"".join(x for x in chunk if x)
                if isinstance(chunk, bytes):
                    text = chunk.decode("utf-8", errors="replace")
                else:
                    text = str(chunk)
                if not text:
                    continue
                parts.append(text)
                if on_chunk is not None:
                    on_chunk(text)
        except DockerException as exc:
            raise DockerError(f"docker exec stream failed: {exc}") from exc

        try:
            inspect = api.exec_inspect(exec_id)
            exit_code = int(inspect.get("ExitCode") or 0)
        except DockerException as exc:
            raise DockerError(f"docker exec inspect failed: {exc}") from exc

        return exit_code, "".join(parts)

    def exec_interactive(
        self,
        *,
        env: Mapping[str, str] | None = None,
        working_directory: str | None = None,
    ) -> int:
        """Attach an interactive TTY shell matching step shell/env/cwd."""
        from ciwalk.shell import attach_interactive_exec

        container = self._require_container()
        workdir = resolve_workdir(working_directory)
        full_env = {**base_job_env(self.job_id), **(env or {})}
        return attach_interactive_exec(
            self.client,
            container.id,
            DEBUG_SHELL,
            workdir=workdir,
            environment=full_env,
        )

    def _require_container(self) -> Container:
        if self.container is None:
            raise DockerError("Container is not running")
        try:
            self.container.reload()
        except NotFound as exc:
            raise DockerError("Container no longer exists") from exc
        if self.container.status not in {"running", "created"}:
            raise DockerError(f"Container is {self.container.status}")
        return self.container

    def _ensure_image(self) -> None:
        try:
            self.client.images.get(self.image)
            return
        except ImageNotFound:
            pass
        except DockerException as exc:
            raise DockerError(f"Failed to inspect image {self.image}: {exc}") from exc

        try:
            self.client.images.pull(self.image)
        except DockerException as exc:
            raise DockerError(f"Failed to pull image {self.image}: {exc}") from exc
