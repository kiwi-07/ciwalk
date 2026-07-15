"""CLI entrypoint for ciwalk."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from ciwalk import __version__
from ciwalk.container import DEFAULT_IMAGE, list_ciwalk_containers
from ciwalk.errors import CiwalkError, ConfigError, DockerError
from ciwalk.expressions import parse_cli_inputs
from ciwalk.parser import parse_workflow_file, select_job
from ciwalk.session import JobSession

app = typer.Typer(
    name="ciwalk",
    help="Run GitHub Actions workflows locally with interactive pause/resume debugging.",
    add_completion=False,
    no_args_is_help=True,
)
console = Console()
err_console = Console(stderr=True)


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"ciwalk {__version__}")
        raise typer.Exit()


@app.callback()
def main(
    version: Annotated[
        bool,
        typer.Option(
            "--version",
            "-V",
            help="Show version and exit.",
            callback=_version_callback,
            is_eager=True,
        ),
    ] = False,
) -> None:
    """ciwalk — local CI with a debugger."""


@app.command()
def run(
    workflow: Annotated[
        Path,
        typer.Argument(
            exists=True,
            dir_okay=False,
            readable=True,
            help="Path to a GitHub Actions workflow YAML file.",
        ),
    ],
    job: Annotated[
        str | None,
        typer.Option(
            "--job",
            "-j",
            help="Job id to run (required if the workflow defines multiple jobs).",
        ),
    ] = None,
    pause_on_fail: Annotated[
        bool,
        typer.Option(
            "--pause-on-fail",
            help="On step failure, open an interactive shell then retry/continue/abort.",
        ),
    ] = False,
    breakpoint: Annotated[
        str | None,
        typer.Option(
            "--breakpoint",
            "-b",
            help="Pause before the named step (exact step name match).",
        ),
    ] = None,
    image: Annotated[
        str,
        typer.Option(
            "--image",
            help="Docker image override for the job runner.",
        ),
    ] = DEFAULT_IMAGE,
    workdir: Annotated[
        Path | None,
        typer.Option(
            "--workdir",
            "-C",
            exists=True,
            file_okay=False,
            help="Host directory to mount as /github/workspace (default: cwd).",
        ),
    ] = None,
    keep: Annotated[
        bool,
        typer.Option(
            "--keep",
            help="Do not remove the container when the run ends.",
        ),
    ] = False,
    workflow_input: Annotated[
        list[str] | None,
        typer.Option(
            "--input",
            "-i",
            help="Workflow input KEY=VALUE for ${{ inputs.* }} (repeatable).",
        ),
    ] = None,
) -> None:
    """Execute a workflow job step-by-step inside Docker."""
    workspace = (workdir or Path.cwd()).resolve()
    try:
        try:
            cli_inputs = parse_cli_inputs(workflow_input)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        parsed = parse_workflow_file(workflow, inputs=cli_inputs)
        for warning in parsed.warnings:
            console.print(f"[yellow]warning:[/] {warning}")
        selected = select_job(parsed, job)
        session = JobSession(
            selected,
            workdir=workspace,
            image=image,
            pause_on_fail=pause_on_fail,
            breakpoint=breakpoint,
            keep=keep,
        )
        code = session.run()
    except CiwalkError as exc:
        err_console.print(f"[red bold]error:[/] {exc}")
        raise typer.Exit(code=1) from exc
    raise typer.Exit(code=code)


@app.command("cleanup")
def cleanup_cmd(
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run",
            help="List matching containers without removing them.",
        ),
    ] = False,
) -> None:
    """Remove leftover ciwalk containers (e.g. after kill -9 / a crashed run)."""
    try:
        containers = list_ciwalk_containers()
    except DockerError as exc:
        err_console.print(f"[red bold]error:[/] {exc}")
        raise typer.Exit(code=1) from exc

    if not containers:
        console.print("No ciwalk containers found.")
        raise typer.Exit(code=0)

    for c in containers:
        name = (c.name or "").lstrip("/")
        short = c.short_id
        status = getattr(c, "status", "?")
        label = f"{short} {name} ({status})"
        if dry_run:
            console.print(f"would remove {label}")
            continue
        try:
            c.remove(force=True)
            console.print(f"removed {label}")
        except Exception as exc:  # noqa: BLE001
            err_console.print(f"[red]failed to remove {label}: {exc}[/]")
            raise typer.Exit(code=1) from exc

    raise typer.Exit(code=0)


if __name__ == "__main__":
    app()
