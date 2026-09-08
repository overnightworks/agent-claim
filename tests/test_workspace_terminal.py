from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest

from agent_coordination import process, terminal


def test_tmux_target_name_is_stable_and_safe() -> None:
    assert terminal.target_name("alpha_project") == "aco-alpha_project"


def test_terminal_title_identifies_the_project() -> None:
    assert terminal.console_title("alpha") == "ACO: alpha"


def test_target_metadata_conflict_is_not_adopted() -> None:
    assert (
        terminal.target_matches(
            {"@aco_project": "alpha", "@aco_session_id": "session-a"}, "alpha", "session-b"
        )
        is False
    )


def test_tmux_sets_dead_pane_preservation_and_metadata_before_starting_codex(
    monkeypatch, tmp_path
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> process.CapturedResult:
        commands.append(command)
        if command[3] == "display-message":
            return process.CapturedResult(0, b"@0\n", b"")
        if command[3] == "new-window":
            return process.CapturedResult(0, b"@1\n", b"")
        return process.CapturedResult(0, b"", b"")

    monkeypatch.setattr(process, "run_captured", run)
    monkeypatch.setattr(
        process, "run_bounded", lambda *_arguments, **_kwargs: process.BoundedResult(0, b"")
    )
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")

    adapter.create(
        "alpha",
        "session-a",
        tmp_path,
        terminal.Launch(["codex", "resume", "session-a"], {"ACO_AGENT": "head"}, frozenset()),
    )

    command = commands[0][-1]
    assert command.startswith("sh -c")
    assert command.index("remain-on-exit") < command.index("wait-for")
    assert "@aco_project alpha" in command
    assert "@aco_session_id session-a" in command
    provider = next(command for command in commands if command[3] == "new-window")[-1]
    assert provider.endswith("exec env ACO_AGENT=head codex resume session-a'")


def test_launch_keeps_a_synthetic_secret_out_of_tmux_argv_and_errors(monkeypatch, tmp_path) -> None:
    commands: list[list[str]] = []
    bounded_commands: list[list[str]] = []
    private_inputs: list[bytes | None] = []
    synthetic_secret = "synthetic-secret-marker"

    def run(command: list[str], **_kwargs: object) -> process.CapturedResult:
        commands.append(command)
        return process.CapturedResult(0, b"", b"")

    def fail(
        command: list[str], *, input_data: bytes | None = None, **_kwargs: object
    ) -> process.BoundedResult:
        bounded_commands.append(command)
        private_inputs.append(input_data)
        return process.BoundedResult(0, b"%error 0 0 0\n")

    monkeypatch.setattr(process, "run_captured", run)
    monkeypatch.setattr(process, "run_bounded", fail)
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")

    with pytest.raises(terminal.TerminalError) as raised:
        adapter.create(
            "alpha",
            "session-a",
            tmp_path,
            terminal.Launch(
                ["codex", "resume", "session-a"],
                {"ACO_AGENT": "head", "ACO_PROOF_SECRET": synthetic_secret},
                frozenset({"ACO_AGENT"}),
            ),
        )

    assert private_inputs == [
        b"set-environment -t aco-alpha ACO_AGENT head\n"
        b"set-environment -t aco-alpha ACO_PROOF_SECRET synthetic-secret-marker\n"
    ]
    assert all(synthetic_secret not in argument for command in commands for argument in command)
    assert all(
        synthetic_secret not in argument for command in bounded_commands for argument in command
    )
    assert synthetic_secret not in str(raised.value)


def test_open_viewer_uses_the_dedicated_socket_and_stable_title(monkeypatch, tmp_path) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> process.CapturedResult:
        commands.append(command)
        return process.CapturedResult(0, b"", b"")

    detached: list[list[str]] = []

    def start(command: list[str]) -> None:
        detached.append(command)

    monkeypatch.setattr(process, "run_captured", run)
    monkeypatch.setattr(process, "start_detached", start)
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")

    adapter.open_viewer("alpha")

    assert commands[0][-2:] == ["@aco_viewer_pending", "1"]
    assert detached == [
        [
            "gnome-terminal",
            "--title=ACO: alpha",
            "--",
            "tmux",
            "-S",
            str(tmp_path / "tmux.sock"),
            "attach-session",
            "-t",
            "aco-alpha",
        ]
    ]


@contextmanager
def _probe_lock() -> Iterator[None]:
    descriptor = os.open("/tmp/probe-stack.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _wait_for_probe(socket_path: Path) -> None:
    result = process.run_captured(["tmux", "-S", str(socket_path), "wait-for", "aco-env-proof"])
    assert result.exit_status == 0, result.stderr.decode()


def test_initial_and_retried_codex_children_receive_the_sanitized_environment(
    monkeypatch, tmp_path
) -> None:
    socket_path = tmp_path / "tmux.sock"
    output_path = tmp_path / "environment.txt"
    executable_directory = tmp_path / "bin"
    executable_directory.mkdir()
    fake_codex = executable_directory / "codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        'env > "$ACO_PROOF_OUTPUT"\n'
        'tmux -S "$ACO_PROOF_SOCKET" wait-for -S aco-env-proof\n'
    )
    fake_codex.chmod(0o700)
    synthetic_secret = "synthetic-secret-marker"
    removed_names = frozenset(
        {
            "ACO_AGENT",
            "CODEX_THREAD_ID",
            "CODEX_SESSION_ID",
            "CLAUDE_SESSION_ID",
            "GROK_SESSION_ID",
            "CLAUDECODE",
            "CLAUDE_CODE_CHILD_SESSION",
            "CLAUDE_CODE_SESSION_ID",
            "CLAUDE_PID",
        }
    )
    for name in removed_names:
        monkeypatch.setenv(name, "caller-session")
    launch = terminal.Launch(
        ["codex", "resume", "session-a"],
        {
            "PATH": f"{executable_directory}:{os.environ['PATH']}",
            "ACO_AGENT": "aco-proof-head",
            "ACO_PROOF_OUTPUT": str(output_path),
            "ACO_PROOF_SOCKET": str(socket_path),
            "ACO_PROOF_SECRET": synthetic_secret,
        },
        removed_names,
    )
    adapter = terminal.TmuxTerminal(socket_path)

    with _probe_lock():
        try:
            adapter.create("alpha", "session-a", tmp_path, launch)
            _wait_for_probe(socket_path)
            first_environment = output_path.read_text().splitlines()
            pane_command = process.run_captured(
                [
                    "tmux",
                    "-S",
                    str(socket_path),
                    "list-panes",
                    "-t",
                    "aco-alpha",
                    "-F",
                    "#{pane_start_command}",
                ]
            ).stdout.decode()

            adapter.retry("alpha", launch)
            _wait_for_probe(socket_path)
            retried_environment = output_path.read_text().splitlines()
            retried_pane_command = process.run_captured(
                [
                    "tmux",
                    "-S",
                    str(socket_path),
                    "list-panes",
                    "-t",
                    "aco-alpha",
                    "-F",
                    "#{pane_start_command}",
                ]
            ).stdout.decode()
        finally:
            process.run_captured(["tmux", "-S", str(socket_path), "kill-server"])

    for environment in (first_environment, retried_environment):
        assert "ACO_AGENT=aco-proof-head" in environment
        assert f"ACO_PROOF_SECRET={synthetic_secret}" in environment
        assert all(
            name == "ACO_AGENT" or not line.startswith(f"{name}=")
            for name in removed_names
            for line in environment
        )
    assert synthetic_secret not in pane_command
    assert synthetic_secret not in retried_pane_command
