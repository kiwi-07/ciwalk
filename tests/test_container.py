"""Tests for container helpers."""

from ciwalk.container import (
    DEBUG_SHELL,
    WORKSPACE,
    container_name_for,
    gha_bash_command,
    resolve_workdir,
)


def test_gha_bash_command_matches_actions_default_shell() -> None:
    assert gha_bash_command("echo hi") == [
        "bash",
        "--noprofile",
        "--norc",
        "-eo",
        "pipefail",
        "-c",
        "echo hi",
    ]


def test_debug_shell_matches_step_shell_flags() -> None:
    assert DEBUG_SHELL == ["bash", "--noprofile", "--norc"]


def test_resolve_workdir() -> None:
    assert resolve_workdir(None) == WORKSPACE
    assert resolve_workdir("subdir") == f"{WORKSPACE}/subdir"
    assert resolve_workdir("./subdir") == f"{WORKSPACE}/subdir"
    assert resolve_workdir("/abs") == "/abs"


def test_container_name_for_has_ciwalk_prefix() -> None:
    name = container_name_for("build/job")
    assert name.startswith("ciwalk-")
    assert " " not in name
