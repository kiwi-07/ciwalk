"""Interactive TTY bridge into a container exec session."""

from __future__ import annotations

import errno
import os
import select
import sys
import termios
import tty
from collections.abc import Mapping
from typing import Any

from docker import DockerClient
from docker.errors import DockerException
from rich.console import Console

from ciwalk.errors import DockerError

console = Console()


def attach_interactive_exec(
    client: DockerClient,
    container_id: str,
    command: list[str],
    *,
    workdir: str | None = None,
    environment: Mapping[str, str] | None = None,
) -> int:
    """
    Create an interactive exec, attach the local terminal, block until exit.

    Returns the remote process exit code (best-effort; 0 if unavailable).
    """
    if not sys.stdin.isatty() or not sys.stdout.isatty():
        raise DockerError(
            "Interactive debug shell requires a TTY. "
            "Run ciwalk from a terminal (or omit --pause-on-fail / --breakpoint)."
        )

    api = client.api
    env_list = [f"{k}={v}" for k, v in (environment or {}).items()]
    try:
        exec_id = api.exec_create(
            container_id,
            command,
            tty=True,
            stdin=True,
            stdout=True,
            stderr=True,
            workdir=workdir,
            environment=env_list or None,
        )["Id"]
        sock = api.exec_start(exec_id, tty=True, socket=True, stream=True)
    except DockerException as exc:
        raise DockerError(f"Failed to start interactive shell: {exc}") from exc

    raw_sock = _unwrap_socket(sock)
    old_settings = termios.tcgetattr(sys.stdin.fileno())
    try:
        tty.setraw(sys.stdin.fileno())
        _pump(raw_sock)
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old_settings)
        try:
            raw_sock.close()
        except OSError:
            pass

    try:
        inspect = api.exec_inspect(exec_id)
        code = inspect.get("ExitCode")
        return int(code) if code is not None else 0
    except DockerException:
        return 0


def prompt_pause_action() -> str:
    """Ask the user how to resume after leaving the debug shell."""
    console.print()
    console.print("Paused. Choose an action:")
    console.print("  [r] retry     — re-run this step")
    console.print("  [c] continue  — skip to the next step")
    console.print("  [a] abort     — stop the pipeline")
    while True:
        try:
            choice = console.input("Action [r/c/a]: ").strip().lower()
        except EOFError:
            return "abort"
        if choice in {"r", "retry"}:
            return "retry"
        if choice in {"c", "continue"}:
            return "continue"
        if choice in {"a", "abort", "q", "quit"}:
            return "abort"
        console.print("Please enter r, c, or a.")


def _unwrap_socket(sock: Any) -> Any:
    # docker-py may return a socket-like with ._sock
    if hasattr(sock, "_sock"):
        return sock._sock
    return sock


def _pump(raw_sock: Any) -> None:
    stdin_fd = sys.stdin.fileno()
    while True:
        try:
            readable, _, _ = select.select([stdin_fd, raw_sock], [], [])
        except (ValueError, OSError):
            break

        if stdin_fd in readable:
            try:
                data = os.read(stdin_fd, 1024)
            except OSError as exc:
                if exc.errno == errno.EIO:
                    break
                raise
            if not data:
                # Local EOF — close write side so remote bash exits.
                try:
                    raw_sock.shutdown(1)  # SHUT_WR
                except OSError:
                    pass
            else:
                try:
                    raw_sock.sendall(data)
                except OSError:
                    break

        if raw_sock in readable:
            try:
                data = raw_sock.recv(4096)
            except OSError:
                break
            if not data:
                break
            try:
                os.write(sys.stdout.fileno(), data)
            except OSError:
                break
