"""Sequential job execution with pause / retry / continue."""

from __future__ import annotations

import sys
import time
from pathlib import Path

from rich.console import Console

from ciwalk.container import DEFAULT_IMAGE, WORKSPACE, JobContainer, resolve_workdir
from ciwalk.errors import CiwalkError, ConfigError
from ciwalk.expressions import find_expression_literals, format_unsupported_expression
from ciwalk.model import Job, PauseAction, Step, StepKind, StepResult, StepStatus
from ciwalk.shell import prompt_pause_action

# stdout so `ciwalk run … > log` captures status and step output
console = Console()


class JobSession:
    def __init__(
        self,
        job: Job,
        *,
        workdir: Path,
        image: str = DEFAULT_IMAGE,
        pause_on_fail: bool = False,
        breakpoint: str | None = None,
        keep: bool = False,
    ) -> None:
        self.job = job
        self.workdir = workdir
        self.image = image
        self.pause_on_fail = pause_on_fail
        self.breakpoint = breakpoint
        self.keep = keep
        self.results: list[StepResult] = []

        if breakpoint is not None and job.skip_reason is None:
            names = {s.name for s in job.steps}
            if breakpoint not in names:
                available = ", ".join(repr(s.name) for s in job.steps)
                raise ConfigError(f"Breakpoint step {breakpoint!r} not found. Steps: {available}")

    def run(self) -> int:
        """Execute the job. Returns process exit code (0 = success)."""
        if self.job.skip_reason:
            console.print(
                f"[yellow bold]SKIPPED[/] job [cyan]{self.job.id}[/] "
                f"[dim]({self.job.skip_reason})[/]"
            )
            console.print(
                "[red bold]Job not run[/] — unsupported / reusable feature (not a silent pass)"
            )
            return 1

        console.print(
            f"[bold]ciwalk[/] running job [cyan]{self.job.id}[/] "
            f"({len(self.job.steps)} steps) on [dim]{self.image}[/]"
        )
        console.print(f"[dim]workspace:[/] {self.workdir}")

        with JobContainer(
            image=self.image,
            workdir=self.workdir,
            job_id=self.job.id,
            keep=self.keep,
        ) as container:
            console.print(f"[dim]container:[/] {container.container_id()}")
            if self.keep:
                console.print("[yellow]--keep[/]: container will not be removed on exit")

            try:
                return self._run_steps(container)
            finally:
                if self.keep and container.container is not None:
                    self._print_keep_hint(container)

    def _run_steps(self, container: JobContainer) -> int:
        i = 0
        while i < len(self.job.steps):
            step = self.job.steps[i]
            is_breakpoint = self.breakpoint is not None and step.name == self.breakpoint

            if is_breakpoint:
                self._print_status(step, StepStatus.PAUSED, note="breakpoint")
                action = self._pause(container, step, reason="breakpoint")
                if action == PauseAction.ABORT:
                    console.print("[red bold]Job aborted[/]")
                    return 1
                if action == PauseAction.CONTINUE:
                    self.results.append(StepResult(step=step, status=StepStatus.SKIPPED))
                    i += 1
                    continue
                # RETRY: fall through and run the step

            result = self._run_step(container, step)
            self.results.append(result)

            if result.status == StepStatus.PASSED:
                i += 1
                continue

            # Failed
            if not self.pause_on_fail:
                console.print("[red bold]Job failed[/] (use --pause-on-fail to debug)")
                return 1

            action = self._pause(container, step, reason="step failed")
            if action == PauseAction.ABORT:
                console.print("[red bold]Job aborted[/]")
                return 1
            if action == PauseAction.CONTINUE:
                # Keep FAILED result; advance. Job will still exit non-zero.
                i += 1
                continue
            # RETRY: re-run same step; drop this failed attempt from results
            self.results.pop()

        if any(r.status == StepStatus.FAILED for r in self.results):
            console.print("[red bold]Job failed[/] (continued past one or more failed steps)")
            return 1

        console.print("[green bold]Job passed[/]")
        return 0

    def _run_step(self, container: JobContainer, step: Step) -> StepResult:
        self._print_status(step, StepStatus.RUNNING)
        started = time.perf_counter()

        if step.kind == StepKind.CHECKOUT:
            duration = time.perf_counter() - started
            result = StepResult(
                step=step,
                status=StepStatus.PASSED,
                exit_code=0,
                duration_s=duration,
                output=f"(bind-mount) {WORKSPACE} ← {self.workdir}\n",
            )
            self._print_status(step, StepStatus.PASSED, duration=duration)
            console.print(f"[dim]{result.output.rstrip()}[/]")
            return result

        assert step.run is not None
        # Belt-and-suspenders: never exec a command that still has ${{ ... }}.
        leftover = find_expression_literals(step.run)
        if leftover:
            raise ConfigError(format_unsupported_expression(leftover[0]))

        env = {**self.job.env, **step.env}

        def _on_chunk(chunk: str) -> None:
            # Live stream to the terminal (flush so long steps don't look hung).
            sys.stdout.write(chunk)
            sys.stdout.flush()

        try:
            exit_code, output = container.exec_run(
                step.run,
                env=env,
                working_directory=step.working_directory,
                on_chunk=_on_chunk,
            )
        except CiwalkError:
            raise
        except Exception as exc:  # noqa: BLE001
            duration = time.perf_counter() - started
            self._print_status(step, StepStatus.FAILED, duration=duration)
            console.print(f"[red]{exc}[/]")
            return StepResult(
                step=step,
                status=StepStatus.FAILED,
                exit_code=1,
                duration_s=duration,
                output=str(exc),
            )

        duration = time.perf_counter() - started
        status = StepStatus.PASSED if exit_code == 0 else StepStatus.FAILED
        if output and not output.endswith("\n"):
            sys.stdout.write("\n")
            sys.stdout.flush()
        self._print_status(step, status, duration=duration, exit_code=exit_code)
        return StepResult(
            step=step,
            status=status,
            exit_code=exit_code,
            duration_s=duration,
            output=output,
        )

    def _pause(
        self,
        container: JobContainer,
        step: Step,
        *,
        reason: str,
    ) -> PauseAction:
        cwd = resolve_workdir(step.working_directory)
        env = {**self.job.env, **step.env}
        console.print()
        console.rule(f"[yellow]paused[/] — {reason}: [bold]{step.name}[/]")
        console.print(
            "You are dropping into a shell [bold]inside the running container[/].\n"
            f"cwd: [cyan]{cwd}[/]\n"
            f"Workspace: [cyan]{WORKSPACE}[/]\n"
            "Shell matches the step runner "
            "([dim]bash --noprofile --norc[/]) with this step's env.\n"
            "Inspect or fix state, then [bold]exit[/] the shell "
            "(or Ctrl+D) to choose retry / continue / abort."
        )
        console.print()
        try:
            container.exec_interactive(
                env=env,
                working_directory=step.working_directory,
            )
        except CiwalkError as exc:
            console.print(f"[red]{exc}[/]")
            return PauseAction.ABORT

        action = prompt_pause_action()
        console.print(f"[dim]→ {action}[/]")
        return PauseAction(action)

    def _print_keep_hint(self, container: JobContainer) -> None:
        cid = container.container_id()
        console.print(
            f"[yellow]Container kept:[/] {cid} (docker exec -it {cid} bash --noprofile --norc)"
        )

    def _print_status(
        self,
        step: Step,
        status: StepStatus,
        *,
        duration: float | None = None,
        exit_code: int | None = None,
        note: str | None = None,
    ) -> None:
        colors = {
            StepStatus.RUNNING: "blue",
            StepStatus.PASSED: "green",
            StepStatus.FAILED: "red",
            StepStatus.PAUSED: "yellow",
            StepStatus.SKIPPED: "dim",
            StepStatus.PENDING: "dim",
        }
        label = status.value.upper()
        parts = [
            f"[{colors[status]}]{label}[/{colors[status]}]",
            f"[{step.index}] {step.name}",
        ]
        if note:
            parts.append(f"[dim]({note})[/]")
        if duration is not None:
            parts.append(f"[dim]{duration:.2f}s[/]")
        if exit_code is not None and status == StepStatus.FAILED:
            parts.append(f"[red]exit {exit_code}[/]")
        console.print(" ".join(parts))
