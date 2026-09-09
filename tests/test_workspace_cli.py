from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from agent_coordination import cli, providers, terminal, workspace


def test_register_requires_the_explicit_stopped_handover() -> None:
    with pytest.raises(SystemExit, match="2"):
        cli.main(
            [
                "register",
                "alpha",
                "--path",
                "/tmp/project",
                "--session-id",
                "123e4567-e89b-12d3-a456-426614174000",
                "--agent",
                "old head",
            ]
        )


def test_run_refuses_a_repository_target(capsys) -> None:
    assert cli.main(["--repo", "example/repository", "run"]) == 2
    assert "--repo" in capsys.readouterr().err


def test_start_requires_a_path_and_logical_agent() -> None:
    with pytest.raises(SystemExit, match="2"):
        cli.main(["start", "alpha"])


def test_start_uses_its_own_isolated_python_callback(capsys, monkeypatch, tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    observed: dict[str, object] = {}
    monkeypatch.setattr(cli.sys, "executable", "/candidate/bin/python")
    monkeypatch.setattr(cli, "_workspace_config_path", lambda: tmp_path / "workspace.toml")

    def start(
        request: workspace.StartRequest, context: workspace.StartContext
    ) -> workspace.RunOutcome:
        observed["request"] = request
        observed["context"] = context
        return workspace.RunOutcome("alpha", workspace.RunState.ENROLLMENT_PENDING)

    monkeypatch.setattr(workspace, "start_project", start)

    assert cli.main(["start", "alpha", "--path", str(project_path), "--agent", "new head"]) == 0

    assert observed["request"] == workspace.StartRequest("alpha", project_path, "new head")
    assert isinstance(observed["context"], workspace.StartContext)
    assert observed["context"].callback == (
        "/candidate/bin/python -I -m agent_coordination.cli _capture-codex-start"
    )
    assert capsys.readouterr().out == "alpha: enrollment pending\n"


def test_start_reports_a_terminal_refusal_without_a_traceback(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()

    def refuse(*_arguments: object) -> workspace.RunOutcome:
        raise terminal.TerminalError("foreign metadata")

    monkeypatch.setattr(
        workspace,
        "start_project",
        refuse,
    )

    assert cli.main(["start", "alpha", "--path", str(project_path), "--agent", "new head"]) == 2

    assert capsys.readouterr().err == "ERROR: foreign metadata\n"


def test_capture_callback_reports_an_invalid_payload(capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("[]"))

    assert cli.main(["_capture-codex-start"]) == 2

    assert capsys.readouterr().err == "ERROR: native startup hook payload must be an object\n"


def test_capture_callback_reports_a_terminal_refusal(capsys, monkeypatch) -> None:
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO("{}"))

    def refuse(*_arguments: object) -> None:
        raise terminal.TerminalError("target has foreign metadata")

    monkeypatch.setattr(workspace, "capture_codex_start", refuse)

    assert cli.main(["_capture-codex-start"]) == 2

    assert capsys.readouterr().err == "ERROR: target has foreign metadata\n"


def test_capture_callback_emits_no_model_context_on_success(capsys, monkeypatch) -> None:
    received: dict[str, object] = {}
    monkeypatch.setattr(cli.sys, "stdin", io.StringIO('{"hook_event_name": "SessionStart"}'))
    monkeypatch.setattr(
        workspace,
        "capture_codex_start",
        lambda payload, environment: received.update(payload=payload, environment=environment),
    )

    assert cli.main(["_capture-codex-start"]) == 0

    assert received["payload"] == {"hook_event_name": "SessionStart"}
    assert capsys.readouterr().out == ""


def test_register_writes_an_explicit_stopped_handover_through_the_cli(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    monkeypatch.setattr(cli, "_workspace_config_path", lambda: config_path)

    result = cli.main(
        [
            "register",
            "alpha",
            "--path",
            str(project_path),
            "--session-id",
            "123e4567-e89b-12d3-a456-426614174000",
            "--agent",
            "restored head",
            "--stopped",
        ]
    )

    assert result == 0
    assert capsys.readouterr().out == "alpha: registered\n"
    project = workspace.load_config(config_path).projects["alpha"]
    assert project.agent == "restored head"
    assert project.provider is providers.Provider.CODEX


@pytest.mark.parametrize(
    ("provider", "agent", "expected_provider"),
    [
        ("claude", "Claude workspace head", providers.Provider.CLAUDE),
        ("grok", "Grok workspace head", providers.Provider.GROK),
    ],
)
def test_register_accepts_an_explicit_provider(
    capsys,
    monkeypatch,
    tmp_path: Path,
    provider: str,
    agent: str,
    expected_provider: providers.Provider,
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    monkeypatch.setattr(cli, "_workspace_config_path", lambda: config_path)

    assert (
        cli.main(
            [
                "register",
                "alpha",
                "--provider",
                provider,
                "--path",
                str(project_path),
                "--session-id",
                "123e4567-e89b-12d3-a456-426614174000",
                "--agent",
                agent,
                "--stopped",
            ]
        )
        == 0
    )

    assert capsys.readouterr().out == "alpha: registered\n"
    assert workspace.load_config(config_path).projects["alpha"].provider is expected_provider


def test_register_uses_the_xdg_workspace_configuration_path(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    configuration = tmp_path / "config"
    project_path = tmp_path / "project"
    project_path.mkdir()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(configuration))

    assert (
        cli.main(
            [
                "register",
                "alpha",
                "--path",
                str(project_path),
                "--session-id",
                "123e4567-e89b-12d3-a456-426614174000",
                "--agent",
                "restored head",
                "--stopped",
            ]
        )
        == 0
    )

    config_path = configuration / "aco" / "workspace.toml"
    assert capsys.readouterr().out == "alpha: registered\n"
    assert workspace.load_config(config_path).projects["alpha"].directory == project_path


@pytest.mark.parametrize(
    ("outcomes", "expected_status", "expected_output"),
    [
        ([workspace.RunOutcome("alpha", workspace.RunState.REATTACHED)], 0, "alpha: reattached\n"),
        (
            [workspace.RunOutcome("alpha", workspace.RunState.FAILED, "tmux unavailable")],
            1,
            "alpha: failed: tmux unavailable\n",
        ),
    ],
)
def test_run_prints_each_workspace_outcome_and_uses_failure_status(
    capsys,
    monkeypatch,
    outcomes: list[workspace.RunOutcome],
    expected_status: int,
    expected_output: str,
) -> None:
    monkeypatch.setattr(cli, "_workspace_config_path", lambda: Path("/workspace.toml"))
    monkeypatch.setattr(workspace, "run_projects", lambda *_arguments: tuple(outcomes))

    assert cli.main(["run", "alpha"]) == expected_status
    assert capsys.readouterr().out == expected_output


def test_login_status_reports_independent_disabled_and_missing_states(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_workspace_config_path", lambda: tmp_path / "workspace.toml")

    assert cli.main(["login", "status"]) == 0

    assert capsys.readouterr().out == (
        "launcher: disabled\nconfiguration: missing\nattempt: no login attempt recorded\n"
    )


def test_login_status_reports_an_owned_launcher_as_stale_when_its_current_interpreter_is_unsafe(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    configuration = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(configuration))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_workspace_config_path", lambda: tmp_path / "workspace.toml")
    monkeypatch.setattr(cli.sys, "executable", "/new%installation/python")
    desktop_path = workspace.login_desktop_path(os.environ)
    desktop_path.parent.mkdir(parents=True, mode=0o700)
    desktop_path.write_text(
        "[Desktop Entry]\nType=Application\nName=ACO workspace recovery\n"
        "Exec=/old/python -I -m agent_coordination.cli _run-at-login\n"
        "X-Aco-Owner=agent-coordination/login-v1\n"
    )

    assert cli.main(["login", "status"]) == 0

    assert capsys.readouterr().out == (
        "launcher: stale\nconfiguration: missing\nattempt: no login attempt recorded\n"
    )


def test_login_cli_enables_and_disables_the_owned_launcher(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    configuration = tmp_path / "config"
    config_path = configuration / "aco" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration(
            "alpha", project_path, "123e4567-e89b-12d3-a456-426614174000", "workspace head"
        ),
        config_path,
    )
    monkeypatch.setenv("XDG_CONFIG_HOME", str(configuration))
    monkeypatch.setattr(cli, "_workspace_config_path", lambda: config_path)

    assert cli.main(["login", "enable"]) == 0
    assert capsys.readouterr().out == "login launcher enabled\n"
    assert cli.main(["login", "enable"]) == 0
    assert capsys.readouterr().out == "login launcher already enabled\n"
    assert cli.main(["login", "disable"]) == 0
    assert capsys.readouterr().out == "login launcher disabled\n"
    assert cli.main(["login", "disable"]) == 0
    assert capsys.readouterr().out == "login launcher already disabled\n"


def test_login_cli_refuses_a_repository_target(capsys) -> None:
    assert cli.main(["--repo", "example/repository", "login", "status"]) == 2

    assert "--repo" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("outcomes", "expected_status", "expected_notification"),
    [
        (
            (workspace.RunOutcome("alpha", workspace.RunState.STARTED),),
            0,
            "Workspace recovery completed for 1 project(s).",
        ),
        (
            (workspace.RunOutcome("alpha", workspace.RunState.FAILED),),
            1,
            "Workspace recovery completed with 1 failed project(s).",
        ),
    ],
)
def test_hidden_login_runner_reports_completed_recovery(
    monkeypatch,
    tmp_path: Path,
    outcomes: tuple[workspace.RunOutcome, ...],
    expected_status: int,
    expected_notification: str,
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_workspace_config_path", lambda: tmp_path / "workspace.toml")
    monkeypatch.setattr(workspace, "run_projects", lambda *_arguments: outcomes)
    notifications: list[str] = []
    monkeypatch.setattr(terminal, "notify_login_recovery", notifications.append)

    assert cli.main(["_run-at-login"]) == expected_status

    assert notifications == [expected_notification]


def test_hidden_login_runner_reports_a_recording_failure(monkeypatch) -> None:
    def fail(*_arguments: object) -> workspace.LoginRunResult:
        raise workspace.WorkspaceError("recording failed")

    notifications: list[str] = []
    monkeypatch.setattr(workspace, "run_login_recovery", fail)
    monkeypatch.setattr(terminal, "notify_login_recovery", notifications.append)

    assert cli.main(["_run-at-login"]) == 2

    assert notifications == ["Workspace recovery could not record its attempt."]


def test_login_status_reports_a_completed_attempt(capsys, monkeypatch, tmp_path: Path) -> None:
    configuration = tmp_path / "config"
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(configuration))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setattr(cli, "_workspace_config_path", lambda: tmp_path / "workspace.toml")
    attempt_path = workspace.login_attempt_path(os.environ)
    attempt_path.parent.mkdir(parents=True)
    attempt_path.write_text(
        json.dumps(
            {
                "attempt_id": "123e4567-e89b-42d3-a456-426614174000",
                "completed_at": "2026-09-09T00:00:01+00:00",
                "outcomes": [{"outcome": "started", "project": "alpha"}],
                "started_at": "2026-09-09T00:00:00+00:00",
                "state": "completed",
                "version": 1,
            }
        )
    )

    assert cli.main(["login", "status"]) == 0

    assert capsys.readouterr().out == (
        "launcher: disabled\n"
        "configuration: missing\n"
        "attempt: 123e4567-e89b-42d3-a456-426614174000 2026-09-09T00:00:00+00:00 completed\n"
        "alpha: started\n"
        "completed: 2026-09-09T00:00:01+00:00\n"
    )


def test_hidden_login_runner_reports_workspace_failure_without_private_detail(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_workspace_config_path", lambda: tmp_path / "missing.toml")
    notifications: list[str] = []
    monkeypatch.setattr(terminal, "notify_login_recovery", notifications.append)

    assert cli.main(["_run-at-login"]) == 1

    attempt = workspace.load_login_attempt(workspace.login_attempt_path(os.environ))
    assert attempt.failure == "workspace failure"
    assert notifications == ["Workspace recovery failed."]
    assert cli.main(["login", "status"]) == 0

    output = capsys.readouterr().out
    assert "workspace: workspace failure\n" in output
    assert "missing.toml" not in output


def test_login_status_reports_a_malformed_attempt_without_echoing_its_contents(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    marker = "private-malformed-record"
    configuration = tmp_path / "config"
    state = tmp_path / "state"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(configuration))
    monkeypatch.setenv("XDG_STATE_HOME", str(state))
    monkeypatch.setattr(cli, "_workspace_config_path", lambda: tmp_path / "workspace.toml")
    attempt_path = workspace.login_attempt_path(os.environ)
    attempt_path.parent.mkdir(parents=True)
    attempt_path.write_text(
        '{"version": 1, "attempt_id": "' + marker + '", "started_at": "not-a-time", '
        '"state": "completed", "outcomes": []}\n'
    )

    assert cli.main(["login", "status"]) == 1

    output = capsys.readouterr().out
    assert "attempt: malformed" in output
    assert marker not in output


@pytest.mark.parametrize(
    ("entry", "expected_launcher"),
    [
        (
            "[Desktop Entry]\nType=Application\nName=ACO workspace recovery\n"
            "Exec=/old/python -I -m agent_coordination.cli _run-at-login\n"
            "X-Aco-Owner=agent-coordination/login-v1\n",
            "stale",
        ),
        ("[Desktop Entry]\nType=Application\nExec=/bin/sh\n", "conflict"),
    ],
)
def test_login_status_reports_launcher_ownership_state_without_writing(
    capsys,
    monkeypatch,
    tmp_path: Path,
    entry: str,
    expected_launcher: str,
) -> None:
    configuration = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(configuration))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(cli, "_workspace_config_path", lambda: tmp_path / "workspace.toml")
    monkeypatch.setattr(cli.sys, "executable", "/new/python")
    desktop_path = workspace.login_desktop_path(os.environ)
    desktop_path.parent.mkdir(parents=True, mode=0o700)
    desktop_path.write_text(entry)

    assert cli.main(["login", "status"]) == 0

    assert capsys.readouterr().out.startswith(f"launcher: {expected_launcher}\n")
