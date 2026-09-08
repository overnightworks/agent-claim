from __future__ import annotations

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
        return process.CapturedResult(0, b"", b"")

    monkeypatch.setattr(process, "run_captured", run)
    adapter = terminal.TmuxTerminal(tmp_path / "tmux.sock")

    adapter.create(
        "alpha",
        "session-a",
        tmp_path,
        terminal.Launch(["codex", "resume", "session-a"], {"ACO_AGENT": "head"}, frozenset()),
    )

    command_names = [command[3] for command in commands]
    assert command_names.index("set-option") < command_names.index("send-keys")
    assert commands[1][-2:] == ["remain-on-exit", "on"]
    assert commands[-1][-2:] == ["exec codex resume session-a", "Enter"]


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
