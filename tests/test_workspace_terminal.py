from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace
from pathlib import Path

import pytest

from agent_coordination import process, providers, terminal


@pytest.mark.parametrize(
    "failure",
    [
        process.ExecutableMissingError("notify-send"),
        PermissionError("notify-send"),
        IsADirectoryError("notify-send"),
    ],
)
def test_login_notification_is_best_effort_and_uses_only_its_bounded_summary(
    monkeypatch, failure: process.ProcessError | OSError
) -> None:
    commands: list[list[str]] = []

    def unavailable(command: list[str], **_kwargs: object) -> process.CapturedResult:
        commands.append(command)
        raise failure

    monkeypatch.setattr(process, "run_captured", unavailable)

    terminal.notify_login_recovery("Workspace recovery completed for 2 project(s).")

    assert commands == [
        ["notify-send", "ACO workspace recovery", "Workspace recovery completed for 2 project(s)."]
    ]


def test_tmux_target_name_is_stable_and_safe() -> None:
    assert terminal.target_name("alpha_project") == "aco-alpha_project"


def test_terminal_title_identifies_the_project() -> None:
    assert terminal.console_title("alpha") == "ACO: alpha"


def test_viewer_attempt_parses_canonical_pending_and_failed_uuidv4_tokens() -> None:
    token = "00000000-0000-4000-8000-000000000000"

    assert terminal._viewer_attempt(token) == terminal.ViewerAttempt(
        terminal.ViewerAttemptState.PENDING, token
    )
    assert terminal._viewer_attempt(f"failed:{token}") == terminal.ViewerAttempt(
        terminal.ViewerAttemptState.FAILED, token
    )
    assert terminal._viewer_attempt(f"accepted:{token}") == terminal.ViewerAttempt(
        terminal.ViewerAttemptState.ACCEPTED, token
    )


def test_target_metadata_conflict_is_not_adopted() -> None:
    assert (
        terminal.target_matches(
            {"@aco_project": "alpha", "@aco_session_id": "session-a"},
            "alpha",
            providers.Provider.CODEX,
            "session-b",
        )
        is False
    )


def test_legacy_target_metadata_matches_only_codex() -> None:
    metadata = {"@aco_project": "alpha", "@aco_session_id": "session-a"}

    assert terminal.target_matches(metadata, "alpha", providers.Provider.CODEX, "session-a") is True
    assert (
        terminal.target_matches(metadata, "alpha", providers.Provider.CLAUDE, "session-a") is False
    )


def test_explicit_target_provider_mismatch_is_not_adopted() -> None:
    metadata = {
        "@aco_project": "alpha",
        "@aco_provider": "codex",
        "@aco_session_id": "session-a",
    }

    assert (
        terminal.target_matches(metadata, "alpha", providers.Provider.CLAUDE, "session-a") is False
    )


def test_live_registration_requires_the_same_native_birth_before_and_after_observation(
    monkeypatch, tmp_path
) -> None:
    snapshots = iter(
        (
            _native_snapshot(41, tmp_path, start_time=10),
            _native_snapshot(41, tmp_path, start_time=11),
        )
    )
    monkeypatch.setattr(process, "inspect_native_process", lambda _pid: next(snapshots))

    with pytest.raises(terminal.TerminalError, match="changed"):
        terminal.register_live_process(providers.Provider.CODEX, "session-a", tmp_path, 41)


def test_live_registration_returns_a_receipt_after_stable_native_observations(
    monkeypatch, tmp_path
) -> None:
    native = _native_snapshot(41, tmp_path)
    monkeypatch.setattr(process, "inspect_native_process", lambda _pid: native)

    receipt = terminal.register_live_process(providers.Provider.CODEX, "session-a", tmp_path, 41)

    assert receipt == terminal.ExternalProcessReceipt("boot", 41, 10)


def test_live_registration_discards_a_claude_resume_prompt_from_its_receipt(
    monkeypatch, tmp_path
) -> None:
    native = process.NativeProcess(
        41,
        process.NativeProcessState.LIVE,
        process.current_user_id(),
        "boot",
        10,
        "claude",
        tmp_path,
        b"claude\0--resume\0session-a\0continue this work\0",
    )
    monkeypatch.setattr(process, "inspect_native_process", lambda _pid: native)

    receipt = terminal.register_live_process(providers.Provider.CLAUDE, "session-a", tmp_path, 41)

    assert receipt == terminal.ExternalProcessReceipt("boot", 41, 10)


def test_live_registration_refuses_a_codex_javascript_wrapper(monkeypatch, tmp_path) -> None:
    wrapper = process.NativeProcess(
        41,
        process.NativeProcessState.LIVE,
        process.current_user_id(),
        "boot",
        10,
        "node",
        tmp_path,
        b"node\0codex.js\0resume\0session-a\0",
    )
    monkeypatch.setattr(process, "inspect_native_process", lambda _pid: wrapper)

    with pytest.raises(terminal.TerminalError, match="exact resumed native conversation"):
        terminal.register_live_process(providers.Provider.CODEX, "session-a", tmp_path, 41)


def test_live_registration_refuses_grok_until_it_has_a_native_owner_shape(tmp_path) -> None:
    with pytest.raises(terminal.TerminalError, match="does not support live registration"):
        terminal.register_live_process(providers.Provider.GROK, "session-a", tmp_path, 41)


def test_external_manual_resume_blocks_retry_of_an_exited_managed_pane(
    monkeypatch, tmp_path
) -> None:
    replacement = _native_snapshot(52, tmp_path)
    monkeypatch.setattr(
        process,
        "inspect_native_process",
        lambda _pid: process.NativeProcess(41, process.NativeProcessState.ABSENT),
    )
    monkeypatch.setattr(
        process,
        "scan_native_processes",
        lambda _executable: process.NativeProcessScan((replacement,), True),
    )

    ownership = terminal.external_ownership(
        providers.Provider.CODEX,
        "session-a",
        tmp_path,
        terminal.ExternalProcessReceipt("boot", 41, 10),
    )

    assert ownership.state is terminal.ExternalOwnershipState.LIVE


def test_external_ownership_reuses_a_recorded_live_process_without_a_scan(
    monkeypatch, tmp_path
) -> None:
    original = _native_snapshot(41, tmp_path)
    monkeypatch.setattr(process, "inspect_native_process", lambda _pid: original)
    monkeypatch.setattr(
        process, "scan_native_processes", lambda _executable: pytest.fail("scanned")
    )

    ownership = terminal.external_ownership(
        providers.Provider.CODEX,
        "session-a",
        tmp_path,
        terminal.ExternalProcessReceipt("boot", 41, 10),
    )

    assert ownership.state is terminal.ExternalOwnershipState.LIVE


@pytest.mark.parametrize(
    ("boot_id", "start_time", "alternate_pid", "expected"),
    [
        ("new-boot", 10, None, terminal.ExternalOwnershipState.ABSENT),
        ("boot", 11, None, terminal.ExternalOwnershipState.ABSENT),
        ("new-boot", 10, 52, terminal.ExternalOwnershipState.LIVE),
    ],
)
def test_external_ownership_rechecks_an_invalidated_receipt(
    monkeypatch, tmp_path, boot_id, start_time, alternate_pid, expected
) -> None:
    original = _native_snapshot(41, tmp_path, boot_id=boot_id, start_time=start_time)
    alternates = () if alternate_pid is None else (_native_snapshot(alternate_pid, tmp_path),)
    monkeypatch.setattr(process, "inspect_native_process", lambda _pid: original)
    monkeypatch.setattr(
        process,
        "scan_native_processes",
        lambda _executable: process.NativeProcessScan(alternates, True),
    )

    ownership = terminal.external_ownership(
        providers.Provider.CODEX,
        "session-a",
        tmp_path,
        terminal.ExternalProcessReceipt("boot", 41, 10),
    )

    assert ownership.state is expected


@pytest.mark.parametrize("uncertainty", ["incomplete", "wrong-cwd", "ambiguous"])
def test_external_ownership_refuses_uncertainty_alongside_an_exact_owner(
    monkeypatch, tmp_path, uncertainty
) -> None:
    processes = [_native_snapshot(42, tmp_path)]
    complete = True
    if uncertainty == "incomplete":
        complete = False
    elif uncertainty == "wrong-cwd":
        processes.append(_native_snapshot(43, Path("/other")))
    else:
        processes.append(
            process.NativeProcess(
                43,
                process.NativeProcessState.LIVE,
                process.current_user_id(),
                "boot",
                10,
                "codex",
                tmp_path,
                b"codex\0resume\0--unexpected\0session-a\0",
            )
        )
    monkeypatch.setattr(
        process,
        "inspect_native_process",
        lambda _pid: process.NativeProcess(41, process.NativeProcessState.ABSENT),
    )
    monkeypatch.setattr(
        process,
        "scan_native_processes",
        lambda _executable: process.NativeProcessScan(tuple(processes), complete),
    )

    ownership = terminal.external_ownership(providers.Provider.CODEX, "session-a", tmp_path, None)

    assert ownership.state is terminal.ExternalOwnershipState.UNKNOWN


def test_external_ownership_refuses_a_live_recorded_process_with_changed_binding(
    monkeypatch, tmp_path
) -> None:
    changed = process.NativeProcess(
        41,
        process.NativeProcessState.LIVE,
        process.current_user_id(),
        "boot",
        10,
        "codex",
        tmp_path,
        b"codex\0resume\0different\0",
    )
    monkeypatch.setattr(process, "inspect_native_process", lambda _pid: changed)

    ownership = terminal.external_ownership(
        providers.Provider.CODEX,
        "session-a",
        tmp_path,
        terminal.ExternalProcessReceipt("boot", 41, 10),
    )

    assert ownership.state is terminal.ExternalOwnershipState.UNKNOWN


@pytest.mark.parametrize(
    "scan",
    [
        process.NativeProcessScan(
            (
                process.NativeProcess(
                    42,
                    process.NativeProcessState.LIVE,
                    process.current_user_id(),
                    "boot",
                    10,
                    "codex",
                    Path("/other"),
                    b"codex\0resume\0session-a\0",
                ),
            ),
            True,
        ),
        process.NativeProcessScan(
            (
                process.NativeProcess(
                    42,
                    process.NativeProcessState.LIVE,
                    process.current_user_id(),
                    "boot",
                    10,
                    "codex",
                    Path("/other"),
                    b"codex\0resume\0session-a\0",
                ),
            ),
            False,
        ),
        process.NativeProcessScan(
            (
                process.NativeProcess(
                    42,
                    process.NativeProcessState.LIVE,
                    process.current_user_id(),
                    "boot",
                    10,
                    "codex",
                    Path("/other"),
                    b"codex\0resume\0session-a\0",
                ),
                process.NativeProcess(
                    43,
                    process.NativeProcessState.LIVE,
                    process.current_user_id(),
                    "boot",
                    10,
                    "codex",
                    Path("/other"),
                    b"codex\0resume\0session-a\0",
                ),
            ),
            True,
        ),
        process.NativeProcessScan(
            (
                process.NativeProcess(
                    42,
                    process.NativeProcessState.LIVE,
                    process.current_user_id(),
                    "boot",
                    10,
                    "codex",
                    Path("/other"),
                ),
            ),
            True,
        ),
    ],
)
def test_external_ownership_refuses_incomplete_or_ambiguous_scans(
    monkeypatch, tmp_path, scan
) -> None:
    monkeypatch.setattr(
        process,
        "inspect_native_process",
        lambda _pid: process.NativeProcess(41, process.NativeProcessState.ABSENT),
    )
    monkeypatch.setattr(process, "scan_native_processes", lambda _executable: scan)

    ownership = terminal.external_ownership(providers.Provider.CODEX, "session-a", tmp_path, None)

    assert ownership.state is terminal.ExternalOwnershipState.UNKNOWN


@pytest.mark.parametrize(
    "snapshot",
    [
        process.NativeProcess(41, process.NativeProcessState.LIVE, 999, "boot", 10),
        process.NativeProcess(41, process.NativeProcessState.ZOMBIE, process.current_user_id()),
        process.NativeProcess(41, process.NativeProcessState.UNKNOWN, process.current_user_id()),
    ],
)
def test_live_registration_refuses_an_unowned_or_unobservable_process(
    monkeypatch, tmp_path, snapshot
) -> None:
    monkeypatch.setattr(process, "inspect_native_process", lambda _pid: snapshot)

    with pytest.raises(terminal.TerminalError):
        terminal.register_live_process(providers.Provider.CODEX, "session-a", tmp_path, 41)


def test_external_ownership_does_not_scan_unsupported_live_grok(tmp_path) -> None:
    assert (
        terminal.external_ownership(providers.Provider.GROK, "session-a", tmp_path, None).state
        is terminal.ExternalOwnershipState.ABSENT
    )


def test_external_ownership_recovers_when_a_matching_native_process_is_a_zombie(
    monkeypatch, tmp_path
) -> None:
    proc_root = tmp_path / "proc"
    zombie = proc_root / "41"
    zombie.mkdir(parents=True)
    fields = ["Z", *("0" for _ in range(19)), "31"]
    (zombie / "stat").write_text("41 (codex) " + " ".join(fields))
    (zombie / "status").write_text(f"Uid:\t{process.current_user_id()}\t0\t0\t0\n")
    real_scan = process.scan_native_processes
    monkeypatch.setattr(
        process,
        "inspect_native_process",
        lambda _pid: process.NativeProcess(41, process.NativeProcessState.ABSENT),
    )
    monkeypatch.setattr(
        process,
        "scan_native_processes",
        lambda executable: real_scan(executable, proc_root),
    )

    ownership = terminal.external_ownership(providers.Provider.CODEX, "session-a", tmp_path, None)

    assert ownership.state is terminal.ExternalOwnershipState.ABSENT


def test_external_ownership_refuses_a_relevant_process_that_loses_its_cwd(
    monkeypatch, tmp_path
) -> None:
    proc_root = tmp_path / "proc"
    candidate = proc_root / "41"
    candidate.mkdir(parents=True)
    (candidate / "stat").write_text("41 (codex) " + " ".join(["S", *("0" for _ in range(19))]))
    (candidate / "status").write_text(f"Uid:\t{process.current_user_id()}\t0\t0\t0\n")
    real_scan = process.scan_native_processes
    monkeypatch.setattr(
        process,
        "inspect_native_process",
        lambda _pid: process.NativeProcess(41, process.NativeProcessState.ABSENT),
    )
    monkeypatch.setattr(
        process,
        "scan_native_processes",
        lambda executable: real_scan(executable, proc_root),
    )

    ownership = terminal.external_ownership(providers.Provider.CODEX, "session-a", tmp_path, None)

    assert ownership.state is terminal.ExternalOwnershipState.UNKNOWN


def _native_snapshot(
    pid: int, directory, *, boot_id: str = "boot", start_time: int = 10
) -> process.NativeProcess:
    return process.NativeProcess(
        pid,
        process.NativeProcessState.LIVE,
        process.current_user_id(),
        boot_id,
        start_time,
        "codex",
        directory,
        b"codex\0resume\0session-a\0",
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
    assert "@aco_provider codex" in command
    provider = next(command for command in commands if command[3] == "new-window")[-1]
    assert provider.endswith("exec env ACO_AGENT=head codex resume session-a'")


def test_tmux_makes_fresh_enrollment_pending_before_starting_codex(monkeypatch, tmp_path) -> None:
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
    launch = terminal.Launch(
        ["codex", "-C", str(tmp_path)],
        {"ACO_AGENT": "new head", "ACO_CAPTURE_ATTEMPT": "attempt"},
        frozenset(),
        enrollment=terminal.Enrollment(
            tmp_path, "new head", None, "attempt", terminal.EnrollmentState.INITIALIZING
        ),
    )

    adapter.create("alpha", None, tmp_path, launch)

    setup = commands[0][-1]
    pending = next(
        index
        for index, command in enumerate(commands)
        if command[3:5] == ["set-option", "-t"]
        and command[-2:] == ["@aco_enrollment_state", "pending"]
    )
    provider = next(index for index, command in enumerate(commands) if command[3] == "new-window")
    assert "@aco_enrollment_state initializing" in setup
    assert "@aco_enrollment_attempt attempt" in setup
    assert pending < provider


def test_launch_keeps_a_synthetic_secret_out_of_tmux_argv_and_errors(monkeypatch, tmp_path) -> None:
    commands: list[list[str]] = []
    bounded_commands: list[list[str]] = []
    private_inputs: list[bytes | None] = []
    synthetic_secret = "synthetic-secret-marker"

    def run(command: list[str], **_kwargs: object) -> process.CapturedResult:
        commands.append(command)
        if command[3] == "display-message":
            return process.CapturedResult(0, b"@0\n", b"")
        if command[3] == "new-window":
            return process.CapturedResult(0, b"@1\n", b"")
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
    launch = terminal.Launch(
        ["codex", "resume", "session-a"],
        {"ACO_AGENT": "head", "ACO_PROOF_SECRET": synthetic_secret},
        frozenset({"ACO_AGENT"}),
    )

    with pytest.raises(terminal.TerminalError) as raised:
        adapter.create("alpha", "session-a", tmp_path, launch)

    assert private_inputs == [
        b'set-environment "-t" "aco-alpha" "ACO_AGENT" "head"\n'
        b'set-environment "-t" "aco-alpha" "ACO_PROOF_SECRET" "synthetic-secret-marker"\n'
        b"detach-client\n"
    ]
    assert all(synthetic_secret not in argument for command in commands for argument in command)
    assert all(
        synthetic_secret not in argument for command in bounded_commands for argument in command
    )
    assert synthetic_secret not in str(raised.value)


def test_open_viewer_enqueues_one_token_owned_waiting_console_without_exposing_environment(
    monkeypatch, tmp_path
) -> None:
    commands: list[list[str]] = []
    private_inputs: list[bytes | None] = []
    token = "00000000-0000-4000-8000-000000000000"
    synthetic_secret = "synthetic-secret-marker"

    def bounded(
        command: list[str], *, input_data: bytes | None = None, **_kwargs: object
    ) -> process.BoundedResult:
        commands.append(command)
        private_inputs.append(input_data)
        return process.BoundedResult(0, b"")

    monkeypatch.setattr(process, "run_bounded", bounded)
    monkeypatch.setattr(terminal.uuid, "uuid4", lambda: terminal.uuid.UUID(token))
    socket_path = tmp_path / "tmux #{session_name}.sock"

    terminal.TmuxTerminal(socket_path).open_viewer(
        "alpha", {"DISPLAY": "current-display", "ACO_PROOF_SECRET": synthetic_secret}
    )

    assert commands == [
        ["tmux", "-C", "-S", str(socket_path), "attach-session", "-t", "aco-alpha"],
        ["tmux", "-C", "-S", str(socket_path)],
    ]
    assert private_inputs[0] == (
        b'set-environment "-t" "aco-alpha" "DISPLAY" "current-display"\n'
        b'set-environment "-r" "-t" "aco-alpha" "WAYLAND_DISPLAY"\n'
        b'set-environment "-r" "-t" "aco-alpha" "DBUS_SESSION_BUS_ADDRESS"\n'
        b'set-environment "-r" "-t" "aco-alpha" "XDG_RUNTIME_DIR"\n'
        b'set-environment "-r" "-t" "aco-alpha" "XAUTHORITY"\n'
        b"detach-client\n"
    )
    assert private_inputs[1] is not None
    assert (
        b'set-option "-t" "aco-alpha" "@aco_viewer_pending" "' + token.encode() in private_inputs[1]
    )
    assert b'run-shell "-b" "-t" "aco-alpha"' in private_inputs[1]
    assert b"gnome-terminal" in private_inputs[1]
    assert b"--wait" in private_inputs[1]
    assert b"##{session_name}" in private_inputs[1]
    assert b"if-shell -t aco-alpha -F" in private_inputs[1]
    assert b"accepted:" + token.encode() in private_inputs[1]
    assert b"failed:" + token.encode() in private_inputs[1]
    assert b"##{==:##{@aco_viewer_pending}" in private_inputs[1]
    assert b"viewer_status=\\$?" in private_inputs[1]
    assert b"cleanup_status=\\$?" in private_inputs[1]
    assert b'exit \\"\\$viewer_status\\"' in private_inputs[1]
    assert synthetic_secret.encode() not in b"".join(input for input in private_inputs if input)
    assert all(synthetic_secret not in argument for command in commands for argument in command)


def test_open_viewer_conditionally_clears_only_its_token_when_enqueue_fails(
    monkeypatch, tmp_path
) -> None:
    commands: list[list[str]] = []
    token = "00000000-0000-4000-8000-000000000000"
    results = iter((process.BoundedResult(0, b""), process.BoundedResult(0, b"%error 1\n")))

    def bounded(*_arguments: object, **_kwargs: object) -> process.BoundedResult:
        return next(results)

    def run(command: list[str], **_kwargs: object) -> process.CapturedResult:
        commands.append(command)
        return process.CapturedResult(0, b"", b"")

    monkeypatch.setattr(process, "run_bounded", bounded)
    monkeypatch.setattr(process, "run_captured", run)
    monkeypatch.setattr(terminal.uuid, "uuid4", lambda: terminal.uuid.UUID(token))
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")

    with pytest.raises(terminal.TerminalError, match="open project console failed"):
        adapter.open_viewer("alpha", {})

    assert commands == [
        [
            "tmux",
            "-S",
            str(tmp_path / "tmux.sock"),
            "if-shell",
            "-t",
            "aco-alpha",
            "-F",
            f"#{{==:#{{@aco_viewer_pending}},{token}}}",
            "set-option -t aco-alpha @aco_viewer_pending ''",
        ]
    ]


def test_open_viewer_hides_enqueue_and_cleanup_failures(monkeypatch, tmp_path) -> None:
    token = "00000000-0000-4000-8000-000000000000"
    synthetic_secret = "synthetic-secret-marker"
    results = iter(
        (process.BoundedResult(0, b""), process.ProcessStartFailedError(synthetic_secret))
    )

    def bounded(*_arguments: object, **_kwargs: object) -> process.BoundedResult:
        result = next(results)
        if isinstance(result, process.ProcessError):
            raise result
        return result

    monkeypatch.setattr(process, "run_bounded", bounded)
    monkeypatch.setattr(
        process,
        "run_captured",
        lambda *_arguments, **_kwargs: process.CapturedResult(1, b"", b""),
    )
    monkeypatch.setattr(terminal.uuid, "uuid4", lambda: terminal.uuid.UUID(token))
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")

    with pytest.raises(terminal.TerminalError, match="open project console failed") as raised:
        adapter.open_viewer("alpha", {})

    assert synthetic_secret not in str(raised.value)


def test_terminal_consumes_only_the_matching_failed_viewer_attempt(monkeypatch, tmp_path) -> None:
    commands: list[list[str]] = []
    token = "00000000-0000-4000-8000-000000000000"

    def run(command: list[str], **_kwargs: object) -> process.CapturedResult:
        commands.append(command)
        return process.CapturedResult(0, b"", b"")

    monkeypatch.setattr(process, "run_captured", run)
    monkeypatch.setattr(
        process, "run_bounded", lambda *_arguments, **_kwargs: process.BoundedResult(0, b"")
    )
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")

    adapter.consume_viewer_failure("alpha", token)

    assert commands == [
        [
            "tmux",
            "-S",
            str(tmp_path / "tmux.sock"),
            "if-shell",
            "-t",
            "aco-alpha",
            "-F",
            f"#{{==:#{{@aco_viewer_pending}},failed:{token}}}",
            "set-option -t aco-alpha @aco_viewer_pending ''",
        ]
    ]


def test_old_viewer_cleanup_keeps_a_newer_attempt_token(tmp_path) -> None:
    socket_path = tmp_path / "tmux.sock"
    target = "aco-alpha"
    old_token = "00000000-0000-4000-8000-000000000000"
    new_token = "00000000-0000-4000-8000-000000000001"

    with _probe_lock():
        try:
            assert (
                process.run_captured(
                    ["tmux", "-S", str(socket_path), "new-session", "-d", "-s", target, "sleep 60"]
                ).exit_status
                == 0
            )
            assert (
                process.run_captured(
                    [
                        "tmux",
                        "-S",
                        str(socket_path),
                        "set-option",
                        "-t",
                        target,
                        "@aco_viewer_pending",
                        new_token,
                    ]
                ).exit_status
                == 0
            )

            terminal.TmuxTerminal(socket_path)._conditionally_clear_viewer_attempt(
                target,
                terminal.ViewerAttemptState.PENDING,
                old_token,
                "clear viewer launch token",
            )

            pending = process.run_captured(
                [
                    "tmux",
                    "-S",
                    str(socket_path),
                    "show-options",
                    "-t",
                    target,
                    "-v",
                    "@aco_viewer_pending",
                ]
            )
        finally:
            process.run_captured(["tmux", "-S", str(socket_path), "kill-server"])

    assert pending == process.CapturedResult(0, f"{new_token}\n".encode(), b"")


def test_open_viewer_acceptance_keeps_a_newer_attempt_token_after_its_condition_runs(
    monkeypatch, tmp_path
) -> None:
    socket_path = tmp_path / "tmux.sock"
    target = "aco-alpha"
    token = "00000000-0000-4000-8000-000000000000"
    new_token = "00000000-0000-4000-8000-000000000001"
    channel_prefix = f"aco-viewer-{tmp_path.name}"
    ready = f"{channel_prefix}-ready"
    gate = f"{channel_prefix}-gate"
    done = f"{channel_prefix}-done"
    fake_gnome = tmp_path / "gnome-terminal"
    fake_gnome.write_text(
        f"""#!/usr/bin/env python3
import os
import subprocess
import sys

arguments = sys.argv[sys.argv.index("--") + 1 :]
separator = arguments.index(";")
tmux_command = [arguments[0], *arguments[1:3]]
subprocess.run([*tmux_command, "wait-for", "-S", "{ready}"], check=True)
subprocess.run([*tmux_command, "wait-for", "{gate}"], check=True)
status = subprocess.run(
    [*tmux_command, *arguments[separator + 1 :]], check=False
).returncode
subprocess.run([*tmux_command, "wait-for", "-S", "{done}"], check=True)
sys.exit(status)
""",
        encoding="utf-8",
    )
    fake_gnome.chmod(0o700)
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}:{os.environ['PATH']}",
    }
    adapter = terminal.TmuxTerminal(socket_path)
    monkeypatch.setattr(terminal.uuid, "uuid4", lambda: terminal.uuid.UUID(token))

    with _probe_lock():
        try:
            assert (
                process.run_captured(
                    ["tmux", "-S", str(socket_path), "new-session", "-d", "-s", target, "sleep 60"],
                    env=environment,
                ).exit_status
                == 0
            )
            assert (
                process.run_captured(
                    [
                        "tmux",
                        "-S",
                        str(socket_path),
                        "set-environment",
                        "-g",
                        "PATH",
                        environment["PATH"],
                    ]
                ).exit_status
                == 0
            )
            adapter.open_viewer("alpha", {})
            assert (
                process.run_captured(
                    ["tmux", "-S", str(socket_path), "wait-for", ready], timeout=5
                ).exit_status
                == 0
            )
            assert (
                process.run_captured(
                    [
                        "tmux",
                        "-S",
                        str(socket_path),
                        "set-option",
                        "-t",
                        target,
                        "@aco_viewer_pending",
                        new_token,
                    ]
                ).exit_status
                == 0
            )
            assert (
                process.run_captured(
                    ["tmux", "-S", str(socket_path), "wait-for", "-S", gate]
                ).exit_status
                == 0
            )
            assert (
                process.run_captured(
                    ["tmux", "-S", str(socket_path), "wait-for", done], timeout=5
                ).exit_status
                == 0
            )
            pending = process.run_captured(
                [
                    "tmux",
                    "-S",
                    str(socket_path),
                    "show-options",
                    "-t",
                    target,
                    "-v",
                    "@aco_viewer_pending",
                ]
            )
        finally:
            process.run_captured(["tmux", "-S", str(socket_path), "kill-server"])

    assert pending == process.CapturedResult(0, f"{new_token}\n".encode(), b"")


def test_tmux_refuses_a_launch_environment_containing_nul(monkeypatch, tmp_path) -> None:
    def run(_command: list[str], **_kwargs: object) -> process.CapturedResult:
        return process.CapturedResult(0, b"", b"")

    monkeypatch.setattr(process, "run_captured", run)
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")
    launch = terminal.Launch(["codex", "resume", "session-a"], {"ACO_AGENT": "head\0"}, frozenset())

    with pytest.raises(terminal.TerminalError, match="cannot contain a NUL"):
        adapter.create("alpha", "session-a", tmp_path, launch)


def test_terminal_refuses_a_nul_socket_path_before_any_command_runs() -> None:
    with pytest.raises(terminal.TerminalError, match="socket path cannot contain a NUL"):
        terminal.TmuxTerminal(Path("tmux\0.sock"))


def test_tmux_reports_when_the_dedicated_socket_cannot_start(monkeypatch, tmp_path) -> None:
    def unavailable(*_arguments: object, **_kwargs: object) -> process.CapturedResult:
        raise process.ExecutableMissingError("tmux")

    monkeypatch.setattr(process, "run_captured", unavailable)
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")

    with pytest.raises(terminal.TerminalError, match="tmux is unavailable: tmux"):
        adapter.inspect("alpha")


def test_tmux_hides_private_environment_transport_start_failures(monkeypatch, tmp_path) -> None:
    synthetic_secret = "synthetic-secret-marker"

    def run(_command: list[str], **_kwargs: object) -> process.CapturedResult:
        return process.CapturedResult(0, b"", b"")

    def fail(*_arguments: object, **_kwargs: object) -> process.BoundedResult:
        raise process.ProcessStartFailedError(synthetic_secret)

    monkeypatch.setattr(process, "run_captured", run)
    monkeypatch.setattr(process, "run_bounded", fail)
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")
    launch = terminal.Launch(
        ["codex", "resume", "session-a"],
        {"ACO_AGENT": "head", "ACO_PROOF_SECRET": synthetic_secret},
        frozenset(),
    )

    with pytest.raises(terminal.TerminalError) as raised:
        adapter.create("alpha", "session-a", tmp_path, launch)

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
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")
    launch = terminal.Launch(["codex", "resume", "session-a"], {"ACO_AGENT": "head"}, frozenset())

    with pytest.raises(terminal.TerminalError) as raised:
        adapter.create("alpha", "session-a", tmp_path, launch)

    assert str(raised.value) == "read tmux environment failed"
    assert "synthetic-secret-marker" not in str(raised.value)


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("old", "inspect tmux window failed: missing window id"),
        ("new", "start provider session failed: missing window id"),
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
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")
    launch = terminal.Launch(["codex", "resume", "session-a"], {"ACO_AGENT": "head"}, frozenset())

    with pytest.raises(terminal.TerminalError, match=message):
        adapter.create("alpha", "session-a", tmp_path, launch)


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
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")
    launch = terminal.Launch(["codex", "resume", "session-a"], {"ACO_AGENT": "head"}, frozenset())

    with pytest.raises(terminal.TerminalError, match=message):
        adapter.create("alpha", "session-a", tmp_path, launch)


@pytest.mark.parametrize(
    ("attached", "viewer_option_absent", "provider"),
    [(False, False, ""), (True, False, "claude"), (False, True, "")],
)
def test_tmux_inspect_reports_each_live_attachment_state(
    monkeypatch, tmp_path, attached: bool, viewer_option_absent: bool, provider: str
) -> None:
    def run(command: list[str], **_kwargs: object) -> process.CapturedResult:
        action = command[3]
        if action == "has-session":
            return process.CapturedResult(0, b"", b"")
        if action == "show-options":
            if command[-1] == "@aco_viewer_pending" and viewer_option_absent:
                return process.CapturedResult(1, b"", b"")
            values = {
                "@aco_project": b"alpha\n",
                "@aco_provider": provider.encode(),
                "@aco_session_id": b"session-a\n",
            }
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
        provider=providers.Provider(provider) if provider else None,
    )


def test_tmux_inspect_refuses_unknown_provider_metadata(monkeypatch, tmp_path) -> None:
    def run(command: list[str], **_kwargs: object) -> process.CapturedResult:
        action = command[3]
        if action == "has-session":
            return process.CapturedResult(0, b"", b"")
        if action == "show-options":
            values = {
                "@aco_project": b"alpha\n",
                "@aco_provider": b"gemini\n",
                "@aco_session_id": b"session-a\n",
            }
            return process.CapturedResult(0, values.get(command[-1], b""), b"")
        if action == "list-panes":
            return process.CapturedResult(0, b"0\n", b"")
        if action == "display-message":
            return process.CapturedResult(0, b"0\n", b"")
        raise AssertionError(command)

    monkeypatch.setattr(process, "run_captured", run)
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")

    with pytest.raises(terminal.TerminalError, match="unsupported provider metadata"):
        adapter.inspect("alpha")


@pytest.mark.parametrize(
    ("metadata", "message"),
    [
        (
            {"@aco_enrollment_attempt": "attempt"},
            "incomplete enrollment",
        ),
        (
            {"@aco_enrollment_state": "pending"},
            "incomplete enrollment",
        ),
        (
            {
                "@aco_enrollment_state": "unknown",
                "@aco_enrollment_attempt": "attempt",
                "@aco_enrollment_directory": "/workspace",
                "@aco_enrollment_agent": "head",
            },
            "invalid enrollment",
        ),
        (
            {
                "@aco_enrollment_state": "final",
                "@aco_enrollment_attempt": "attempt",
                "@aco_enrollment_directory": "/workspace",
                "@aco_enrollment_agent": "head",
            },
            "incomplete final",
        ),
        (
            {
                "@aco_enrollment_state": "pending",
                "@aco_enrollment_attempt": "attempt",
                "@aco_enrollment_directory": "/workspace",
                "@aco_enrollment_agent": "head",
                "@aco_session_id": "123e4567-e89b-12d3-a456-426614174000",
            },
            "interrupted enrollment UUID",
        ),
        (
            {
                "@aco_enrollment_state": "pending",
                "@aco_enrollment_attempt": "attempt",
                "@aco_enrollment_directory": "/workspace",
                "@aco_enrollment_agent": "head",
                "@aco_session_id": "session-a",
                "@aco_enrollment_session_id": "session-b",
            },
            "mismatched enrollment UUID",
        ),
    ],
)
def test_tmux_inspect_refuses_incomplete_enrollment_metadata(
    monkeypatch, tmp_path, metadata: dict[str, str], message: str
) -> None:
    def run(command: list[str], **_kwargs: object) -> process.CapturedResult:
        action = command[3]
        if action == "has-session":
            return process.CapturedResult(0, b"", b"")
        if action == "show-options":
            value = metadata.get(command[-1], "")
            return process.CapturedResult(0, value.encode(), b"")
        if action == "list-panes":
            return process.CapturedResult(0, b"0\n", b"")
        if action == "display-message":
            return process.CapturedResult(0, b"0\n", b"")
        raise AssertionError(command)

    monkeypatch.setattr(process, "run_captured", run)
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")

    with pytest.raises(terminal.TerminalError, match=message):
        adapter.inspect("alpha")


def test_tmux_inspect_all_refuses_a_foreign_session(monkeypatch, tmp_path) -> None:
    def run(command: list[str], **_kwargs: object) -> process.CapturedResult:
        assert command[3:] == ["list-sessions", "-F", "#{session_name}"]
        return process.CapturedResult(0, b"1\naco-alpha\n", b"")

    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")
    monkeypatch.setattr(process, "run_captured", run)

    with pytest.raises(terminal.TerminalError, match="foreign metadata"):
        adapter.inspect_all()


@pytest.mark.parametrize(
    ("target", "message"),
    [
        (terminal.Target(terminal.TargetState.DETACHED, "alpha", "session-a"), None),
        (terminal.Target(terminal.TargetState.DETACHED, "beta", "session-a"), "incomplete project"),
    ],
)
def test_tmux_inspect_all_reads_each_owned_target(
    monkeypatch, tmp_path, target: terminal.Target, message: str | None
) -> None:
    monkeypatch.setattr(
        process,
        "run_captured",
        lambda *_arguments, **_kwargs: process.CapturedResult(0, b"aco-alpha\n", b""),
    )
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")
    monkeypatch.setattr(adapter, "inspect", lambda _project: target)

    if message is None:
        assert adapter.inspect_all() == (target,)
    else:
        with pytest.raises(terminal.TerminalError, match=message):
            adapter.inspect_all()


def test_tmux_inspect_all_reports_no_targets_when_the_socket_has_no_server(
    monkeypatch, tmp_path
) -> None:
    monkeypatch.setattr(
        process,
        "run_captured",
        lambda *_arguments, **_kwargs: process.CapturedResult(1, b"", b""),
    )

    assert terminal.TmuxTerminal(tmp_path / "tmux.sock").inspect_all() == ()


def test_tmux_inspect_reports_a_complete_pending_enrollment(monkeypatch, tmp_path) -> None:
    metadata = {
        "@aco_project": "alpha",
        "@aco_provider": "codex",
        "@aco_session_id": "session-a",
        "@aco_enrollment_state": "pending",
        "@aco_enrollment_attempt": "attempt",
        "@aco_enrollment_directory": str(tmp_path),
        "@aco_enrollment_agent": "new head",
        "@aco_enrollment_model": "gpt-5",
        "@aco_enrollment_session_id": "session-a",
    }

    def run(command: list[str], **_kwargs: object) -> process.CapturedResult:
        action = command[3]
        if action == "has-session":
            return process.CapturedResult(0, b"", b"")
        if action == "show-options":
            return process.CapturedResult(0, metadata.get(command[-1], "").encode(), b"")
        if action == "list-panes":
            return process.CapturedResult(0, b"0\n", b"")
        if action == "display-message":
            return process.CapturedResult(0, b"0\n", b"")
        raise AssertionError(command)

    monkeypatch.setattr(process, "run_captured", run)

    assert terminal.TmuxTerminal(tmp_path / "tmux.sock").inspect("alpha") == terminal.Target(
        terminal.TargetState.DETACHED,
        "alpha",
        "session-a",
        provider=providers.Provider.CODEX,
        enrollment=terminal.Enrollment(
            tmp_path,
            "new head",
            "gpt-5",
            "attempt",
            terminal.EnrollmentState.PENDING,
            "session-a",
        ),
    )


@pytest.mark.parametrize(
    "enrollment",
    [
        None,
        terminal.Enrollment(
            Path("/workspace"), "new head", None, "attempt", terminal.EnrollmentState.INITIALIZING
        ),
    ],
)
def test_tmux_retries_existing_targets_with_current_launch_environment(
    monkeypatch, tmp_path, enrollment: terminal.Enrollment | None
) -> None:
    commands: list[list[str]] = []
    bounded: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> process.CapturedResult:
        commands.append(command)
        action = command[3]
        if action == "show-environment":
            return process.CapturedResult(0, b"", b"")
        if action == "display-message":
            return process.CapturedResult(0, b"@0\n", b"")
        if action == "new-window":
            return process.CapturedResult(0, b"@1\n", b"")
        return process.CapturedResult(0, b"", b"")

    def configure(command: list[str], **_kwargs: object) -> process.BoundedResult:
        bounded.append(command)
        return process.BoundedResult(0, b"")

    monkeypatch.setattr(process, "run_captured", run)
    monkeypatch.setattr(process, "run_bounded", configure)
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")
    launch = terminal.Launch(
        ["codex", "-C", "/workspace"],
        {"ACO_AGENT": "new head"},
        frozenset(),
        enrollment=enrollment,
    )

    if enrollment is None:
        adapter.retry("alpha", tmp_path, launch)
    else:
        adapter.retry_enrollment("alpha", tmp_path, launch)

    assert bounded == [
        ["tmux", "-C", "-S", str(tmp_path / "tmux.sock"), "attach-session", "-t", "aco-alpha"]
    ]
    new_window = next(command for command in commands if command[3] == "new-window")
    assert new_window[new_window.index("-c") + 1] == str(tmp_path)
    if enrollment is not None:
        assert [command[-2:] for command in commands if command[3] == "set-option"][:2] == [
            ["@aco_enrollment_state", "initializing"],
            ["@aco_enrollment_attempt", "attempt"],
        ]


def test_tmux_refuses_to_retry_enrollment_without_fresh_metadata(tmp_path) -> None:
    launch = terminal.Launch(["codex", "-C", "/workspace"], {"ACO_AGENT": "new head"}, frozenset())
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")

    with pytest.raises(terminal.TerminalError, match="missing metadata"):
        adapter.retry_enrollment("alpha", tmp_path, launch)


def test_tmux_updates_enrollment_metadata_for_a_captured_session(monkeypatch, tmp_path) -> None:
    commands: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> process.CapturedResult:
        commands.append(command)
        return process.CapturedResult(0, b"", b"")

    monkeypatch.setattr(process, "run_captured", run)
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")

    adapter.stage_enrollment("alpha", "session-a")
    adapter.finalize_enrollment("alpha")

    assert [command[-2:] for command in commands] == [
        ["@aco_session_id", "session-a"],
        ["@aco_enrollment_session_id", "session-a"],
        ["@aco_enrollment_state", "final"],
    ]


@pytest.mark.parametrize(
    "viewer_attempt",
    [
        "00000000-0000-4000-8000-000000000000",
        "accepted:00000000-0000-4000-8000-000000000000",
        "failed:00000000-0000-4000-8000-000000000000",
    ],
)
def test_enrollment_transitions_do_not_write_the_viewer_attempt(
    monkeypatch, tmp_path, viewer_attempt: str
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
    launch = terminal.Launch(
        ["codex", "-C", str(tmp_path)],
        {"ACO_AGENT": "new head"},
        frozenset(),
        enrollment=terminal.Enrollment(
            tmp_path,
            "new head",
            None,
            "attempt",
            terminal.EnrollmentState.INITIALIZING,
        ),
    )

    adapter.stage_enrollment("alpha", "session-a")
    adapter.finalize_enrollment("alpha")
    adapter.retry_enrollment("alpha", tmp_path, launch)

    assert terminal._viewer_attempt(viewer_attempt) is not None
    assert all(command[-2] != "@aco_viewer_pending" for command in commands)


@pytest.mark.parametrize(
    "pending",
    [
        "1",
        "not-a-launch-token",
        "00000000-0000-1000-8000-000000000000",
        "failed:not-a-launch-token",
        "accepted:00000000-0000-1000-8000-000000000000",
    ],
)
def test_tmux_inspect_refuses_unowned_viewer_pending_metadata(
    monkeypatch, tmp_path, pending: str
) -> None:
    def run(command: list[str], **_kwargs: object) -> process.CapturedResult:
        if command[3] == "has-session":
            return process.CapturedResult(0, b"", b"")
        if command[3] == "show-options":
            values = {
                "@aco_project": b"alpha\n",
                "@aco_provider": b"codex\n",
                "@aco_session_id": b"session-a\n",
                "@aco_viewer_pending": f"{pending}\n".encode(),
            }
            return process.CapturedResult(0, values.get(command[-1], b""), b"")
        if command[3] == "list-panes":
            return process.CapturedResult(0, b"0\n", b"")
        if command[3] == "display-message":
            return process.CapturedResult(0, b"0\n", b"")
        raise AssertionError(command)

    monkeypatch.setattr(process, "run_captured", run)

    with pytest.raises(terminal.TerminalError, match="unowned viewer pending"):
        terminal.TmuxTerminal(tmp_path / "tmux.sock").inspect("alpha")


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
        'export ACO_PROOF_CWD="$(pwd)"\n'
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
            assert process.run_captured(
                ["tmux", "-S", str(socket_path), "list-sessions", "-F", "#{session_name}"]
            ).stdout.decode().splitlines() == ["aco-alpha"]
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
            adapter.retry("alpha", tmp_path, retry_launch)
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
    assert all(
        "ACO_PROOF_STALE_GLOBAL" not in environment
        for environment in (
            first_environment,
            retried_environment,
            second_project_environment,
        )
    )
    assert retried_environment["ACO_PROOF_CWD"] == str(tmp_path)
    assert injected_target.exit_status == 1
    assert synthetic_secret not in pane_command
    assert synthetic_secret not in retried_pane_command
