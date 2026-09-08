from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
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
        b'set-environment "-t" "aco-alpha" "ACO_AGENT" "head"\n'
        b'set-environment "-t" "aco-alpha" "ACO_PROOF_SECRET" "synthetic-secret-marker"\n'
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


def test_clear_viewer_pending_removes_the_pending_marker(monkeypatch, tmp_path) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> process.CapturedResult:
        commands.append(command)
        return process.CapturedResult(0, b"", b"")

    monkeypatch.setattr(process, "run_captured", run)

    terminal.TmuxTerminal(tmp_path / "tmux.sock").clear_viewer_pending("alpha")

    assert commands[0][-2:] == ["@aco_viewer_pending", ""]


def test_tmux_refuses_a_launch_environment_containing_nul(monkeypatch, tmp_path) -> None:
    def run(_command: list[str], **_kwargs: object) -> process.CapturedResult:
        return process.CapturedResult(0, b"", b"")

    monkeypatch.setattr(process, "run_captured", run)
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")

    with pytest.raises(terminal.TerminalError, match="cannot contain a NUL"):
        adapter.create(
            "alpha",
            "session-a",
            tmp_path,
            terminal.Launch(["codex", "resume", "session-a"], {"ACO_AGENT": "head\0"}, frozenset()),
        )


def test_tmux_reports_when_the_dedicated_socket_cannot_start(monkeypatch, tmp_path) -> None:
    def unavailable(*_arguments: object, **_kwargs: object) -> process.CapturedResult:
        raise process.ExecutableMissingError("tmux")

    monkeypatch.setattr(process, "run_captured", unavailable)

    with pytest.raises(terminal.TerminalError, match="tmux is unavailable: tmux"):
        terminal.TmuxTerminal(tmp_path / "tmux.sock").inspect("alpha")


def test_tmux_hides_private_environment_transport_start_failures(monkeypatch, tmp_path) -> None:
    synthetic_secret = "synthetic-secret-marker"

    def run(_command: list[str], **_kwargs: object) -> process.CapturedResult:
        return process.CapturedResult(0, b"", b"")

    def fail(*_arguments: object, **_kwargs: object) -> process.BoundedResult:
        raise process.ProcessStartFailedError(synthetic_secret)

    monkeypatch.setattr(process, "run_captured", run)
    monkeypatch.setattr(process, "run_bounded", fail)

    with pytest.raises(terminal.TerminalError) as raised:
        terminal.TmuxTerminal(tmp_path / "tmux.sock").create(
            "alpha",
            "session-a",
            tmp_path,
            terminal.Launch(
                ["codex", "resume", "session-a"],
                {"ACO_AGENT": "head", "ACO_PROOF_SECRET": synthetic_secret},
                frozenset(),
            ),
        )

    assert str(raised.value) == "prepare session environment failed"
    assert synthetic_secret not in str(raised.value)


def test_tmux_hides_native_environment_query_failures(monkeypatch, tmp_path) -> None:
    def run(command: list[str], **_kwargs: object) -> process.CapturedResult:
        if command[3] == "show-environment":
            return process.CapturedResult(1, b"synthetic-secret-marker", b"")
        return process.CapturedResult(0, b"", b"")

    monkeypatch.setattr(process, "run_captured", run)
    monkeypatch.setattr(
        process, "run_bounded", lambda *_arguments, **_kwargs: process.BoundedResult(0, b"")
    )

    with pytest.raises(terminal.TerminalError) as raised:
        terminal.TmuxTerminal(tmp_path / "tmux.sock").create(
            "alpha",
            "session-a",
            tmp_path,
            terminal.Launch(["codex", "resume", "session-a"], {"ACO_AGENT": "head"}, frozenset()),
        )

    assert str(raised.value) == "read tmux environment failed"
    assert "synthetic-secret-marker" not in str(raised.value)


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("old", "inspect tmux window failed: missing window id"),
        ("new", "start Codex session failed: missing window id"),
    ],
)
def test_tmux_refuses_to_start_when_it_cannot_identify_a_window(
    monkeypatch, tmp_path, missing: str, message: str
) -> None:
    def run(command: list[str], **_kwargs: object) -> process.CapturedResult:
        action = command[3]
        if action == "show-environment":
            return process.CapturedResult(0, b"", b"")
        if action == "display-message":
            return process.CapturedResult(0, b"" if missing == "old" else b"@0\n", b"")
        if action == "new-window":
            return process.CapturedResult(0, b"" if missing == "new" else b"@1\n", b"")
        return process.CapturedResult(0, b"", b"")

    monkeypatch.setattr(process, "run_captured", run)
    monkeypatch.setattr(
        process, "run_bounded", lambda *_arguments, **_kwargs: process.BoundedResult(0, b"")
    )

    with pytest.raises(terminal.TerminalError, match=message):
        terminal.TmuxTerminal(tmp_path / "tmux.sock").create(
            "alpha",
            "session-a",
            tmp_path,
            terminal.Launch(["codex", "resume", "session-a"], {"ACO_AGENT": "head"}, frozenset()),
        )


@pytest.mark.parametrize(
    ("stdout", "stderr", "message"),
    [
        (b"tmux startup failed\n", b"", "create tmux target failed: tmux startup failed"),
        (b"", b"permission denied\n", "create tmux target failed: permission denied"),
    ],
)
def test_tmux_surfaces_command_output_when_target_creation_fails(
    monkeypatch, tmp_path, stdout: bytes, stderr: bytes, message: str
) -> None:
    monkeypatch.setattr(
        process,
        "run_captured",
        lambda *_arguments, **_kwargs: process.CapturedResult(1, stdout, stderr),
    )

    with pytest.raises(terminal.TerminalError, match=message):
        terminal.TmuxTerminal(tmp_path / "tmux.sock").create(
            "alpha",
            "session-a",
            tmp_path,
            terminal.Launch(["codex", "resume", "session-a"], {"ACO_AGENT": "head"}, frozenset()),
        )


@pytest.mark.parametrize("attached", [False, True])
def test_tmux_inspect_reports_each_live_attachment_state(
    monkeypatch, tmp_path, attached: bool
) -> None:
    def run(command: list[str], **_kwargs: object) -> process.CapturedResult:
        action = command[3]
        if action == "has-session":
            return process.CapturedResult(0, b"", b"")
        if action == "show-options":
            values = {"@aco_project": b"alpha\n", "@aco_session_id": b"session-a\n"}
            return process.CapturedResult(0, values.get(command[-1], b""), b"")
        if action == "list-panes":
            return process.CapturedResult(0, b"0\n", b"")
        if action == "display-message":
            return process.CapturedResult(0, (b"1\n" if attached else b"0\n"), b"")
        raise AssertionError(command)

    monkeypatch.setattr(process, "run_captured", run)

    target = terminal.TmuxTerminal(tmp_path / "tmux.sock").inspect("alpha")

    assert target == terminal.Target(
        terminal.TargetState.ATTACHED if attached else terminal.TargetState.DETACHED,
        "alpha",
        "session-a",
    )


def test_tmux_inspect_reports_an_absent_or_exited_target(monkeypatch, tmp_path) -> None:
    def absent(command: list[str], **_kwargs: object) -> process.CapturedResult:
        return process.CapturedResult(1, b"", b"")

    monkeypatch.setattr(process, "run_captured", absent)
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")
    assert adapter.inspect("alpha") == terminal.Target(terminal.TargetState.ABSENT)

    def exited(command: list[str], **_kwargs: object) -> process.CapturedResult:
        action = command[3]
        if action == "show-options":
            values = {"@aco_project": b"alpha\n", "@aco_session_id": b"session-a\n"}
            return process.CapturedResult(0, values.get(command[-1], b""), b"")
        return process.CapturedResult(0, b"1\n" if action == "list-panes" else b"", b"")

    monkeypatch.setattr(process, "run_captured", exited)
    assert adapter.inspect("alpha").state is terminal.TargetState.EXITED


@pytest.mark.parametrize("failure", [FileNotFoundError(), OSError("display unavailable")])
def test_open_viewer_surfaces_a_terminal_start_failure(
    monkeypatch, tmp_path, failure: OSError
) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> process.CapturedResult:
        commands.append(command)
        return process.CapturedResult(0, b"", b"")

    def fail(*_arguments: object, **_kwargs: object) -> None:
        raise failure

    monkeypatch.setattr(process, "run_captured", run)
    monkeypatch.setattr(process.subprocess, "Popen", fail)

    with pytest.raises(terminal.TerminalError, match="open project console"):
        terminal.TmuxTerminal(tmp_path / "tmux.sock").open_viewer("alpha")

    assert [command[-1] for command in commands] == ["1", ""]


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


def _environment(output_path: Path) -> dict[str, str]:
    return {
        name: value
        for entry in output_path.read_bytes().split(b"\0")
        if entry
        for name, value in (entry.decode().split("=", maxsplit=1),)
    }


def test_initial_and_retried_codex_children_receive_the_sanitized_environment(
    monkeypatch, tmp_path
) -> None:
    path = os.environ["PATH"]
    for name in tuple(os.environ):
        monkeypatch.delenv(name)
    monkeypatch.setenv("PATH", path)
    socket_path = tmp_path / "tmux.sock"
    output_path = tmp_path / "environment.txt"
    executable_directory = tmp_path / "bin"
    executable_directory.mkdir()
    fake_codex = executable_directory / "codex"
    fake_codex.write_text(
        "#!/bin/sh\n"
        'env -0 > "$ACO_PROOF_OUTPUT"\n'
        'tmux -S "$ACO_PROOF_SOCKET" wait-for -S aco-env-proof\n'
    )
    fake_codex.chmod(0o700)
    synthetic_secret = (
        "~/.codex/pre$PATH/${ACO_PROOF_VARIABLE}/first\n"
        'new-session -d -s aco-injected\nsecond\r"\\$\t'
    )
    synthetic_authentication = "initial-authentication-marker"
    stale_global_marker = "stale-global-marker"
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
    monkeypatch.setenv("ACO_PROOF_STALE_GLOBAL", stale_global_marker)
    launch = terminal.Launch(
        ["codex", "resume", "session-a"],
        {
            "PATH": f"{executable_directory}:{path}",
            "ACO_AGENT": "aco-proof-head",
            "ACO_PROOF_OUTPUT": str(output_path),
            "ACO_PROOF_SOCKET": str(socket_path),
            "ACO_PROOF_SECRET": synthetic_secret,
            "ACO_PROOF_AUTH": synthetic_authentication,
        },
        removed_names,
    )
    adapter = terminal.TmuxTerminal(socket_path)

    with _probe_lock():
        try:
            adapter.create("alpha", "session-a", tmp_path, launch)
            _wait_for_probe(socket_path)
            first_environment = _environment(output_path)
            injected_target = process.run_captured(
                ["tmux", "-S", str(socket_path), "has-session", "-t", "aco-injected"]
            )
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

            monkeypatch.delenv("ACO_PROOF_STALE_GLOBAL")
            retry_launch = replace(
                launch,
                environment={
                    name: value
                    for name, value in launch.environment.items()
                    if name != "ACO_PROOF_AUTH"
                },
            )
            adapter.retry("alpha", retry_launch)
            _wait_for_probe(socket_path)
            retried_environment = _environment(output_path)
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
            adapter.create(
                "beta",
                "session-b",
                tmp_path,
                replace(retry_launch, command=["codex", "resume", "session-b"]),
            )
            _wait_for_probe(socket_path)
            second_project_environment = _environment(output_path)
        finally:
            process.run_captured(["tmux", "-S", str(socket_path), "kill-server"])

    for environment in (first_environment, retried_environment):
        assert environment["ACO_AGENT"] == "aco-proof-head"
        assert environment["ACO_PROOF_SECRET"] == synthetic_secret
        assert all(name == "ACO_AGENT" or name not in environment for name in removed_names)
    assert first_environment["ACO_PROOF_AUTH"] == synthetic_authentication
    assert "ACO_PROOF_AUTH" not in retried_environment
    assert "ACO_PROOF_AUTH" not in second_project_environment
    assert "ACO_PROOF_STALE_GLOBAL" not in first_environment
    assert "ACO_PROOF_STALE_GLOBAL" not in retried_environment
    assert "ACO_PROOF_STALE_GLOBAL" not in second_project_environment
    assert injected_target.exit_status == 1
    assert synthetic_secret not in pane_command
    assert synthetic_secret not in retried_pane_command
