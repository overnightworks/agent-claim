from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from threading import Event, Thread

import pytest

from agent_coordination import providers, terminal, workspace

SESSION_ID = "123e4567-e89b-12d3-a456-426614174000"


def _completed_login_record() -> dict[str, object]:
    return {
        "version": 1,
        "attempt_id": "123e4567-e89b-42d3-a456-426614174000",
        "started_at": "2026-09-09T00:00:00+00:00",
        "state": "completed",
        "outcomes": [{"project": "alpha", "outcome": "started"}],
        "completed_at": "2026-09-09T00:00:01+00:00",
    }


def _owned_login_entry(
    command: str = "/candidate/python -I -m agent_coordination.cli _run-at-login",
) -> str:
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=ACO workspace recovery\n"
        f"Exec={command}\n"
        "X-Aco-Owner=agent-coordination/login-v1\n"
    )


def test_login_enable_writes_only_a_validated_owned_desktop_entry(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "aco" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "workspace head"),
        config_path,
    )
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "config")}

    changed = workspace.enable_login(config_path, environment, Path("/opt/aco python/bin/python"))

    desktop_entry = tmp_path / "config" / "autostart" / "aco-workspace.desktop"
    assert changed is True
    assert desktop_entry.read_text() == (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=ACO workspace recovery\n"
        'Exec="/opt/aco python/bin/python" -I -m agent_coordination.cli _run-at-login\n'
        "X-Aco-Owner=agent-coordination/login-v1\n"
    )
    assert (
        workspace.enable_login(config_path, environment, Path("/opt/aco python/bin/python"))
        is False
    )


def test_login_attempt_is_running_before_recovery_and_records_ordered_safe_outcomes(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "workspace.toml"
    state_path = tmp_path / "state" / "aco" / "login-attempt.json"
    observed: list[workspace.LoginAttempt] = []

    def recover(*_arguments: object, **_kwargs: object) -> tuple[workspace.RunOutcome, ...]:
        observed.append(workspace.load_login_attempt(state_path))
        return (
            workspace.RunOutcome("alpha", workspace.RunState.REATTACHED, "private detail"),
            workspace.RunOutcome("beta", workspace.RunState.FAILED, "private detail"),
        )

    monkeypatch.setattr(workspace, "run_projects", recover)

    result = workspace.run_login_recovery(
        config_path,
        state_path,
        now=lambda: datetime(2026, 9, 9, tzinfo=UTC),
        new_attempt_id=lambda: "123e4567-e89b-42d3-a456-426614174000",
    )

    assert observed == [
        workspace.LoginAttempt(
            "123e4567-e89b-42d3-a456-426614174000", "2026-09-09T00:00:00+00:00", "running"
        )
    ]
    assert result.exit_status == 1
    assert workspace.load_login_attempt(state_path) == workspace.LoginAttempt(
        "123e4567-e89b-42d3-a456-426614174000",
        "2026-09-09T00:00:00+00:00",
        "completed",
        (("alpha", workspace.RunState.REATTACHED), ("beta", workspace.RunState.FAILED)),
        None,
        "2026-09-09T00:00:00+00:00",
    )


@pytest.mark.parametrize(
    ("executable_text", "expected_exec"),
    [
        ("/opt/aco $bin/python", 'Exec="/opt/aco \\\\$bin/python"'),
        (r"/opt/aco\bin/python", 'Exec="/opt/aco\\\\\\\\bin/python"'),
        ('/opt/aco"bin/python', 'Exec="/opt/aco\\\\"bin/python"'),
        ("/opt/aco`bin/python", 'Exec="/opt/aco\\\\`bin/python"'),
    ],
)
def test_login_enable_serializes_reserved_executable_characters_exactly(
    tmp_path: Path, executable_text: str, expected_exec: str
) -> None:
    config_path = tmp_path / "config" / "aco" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "workspace head"),
        config_path,
    )
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "config")}

    executable = Path(executable_text)
    workspace.enable_login(config_path, environment, executable)

    assert workspace.login_desktop_path(environment).read_text() == (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=ACO workspace recovery\n"
        f"{expected_exec} -I -m agent_coordination.cli _run-at-login\n"
        "X-Aco-Owner=agent-coordination/login-v1\n"
    )
    assert (
        workspace.login_launcher_state(environment, executable)
        is workspace.LoginLauncherState.ENABLED
    )


def test_login_enable_refuses_a_percent_executable_path(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "aco" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "workspace head"),
        config_path,
    )

    with pytest.raises(workspace.WorkspaceError, match="absolute safe path"):
        workspace.enable_login(
            config_path,
            {"XDG_CONFIG_HOME": str(tmp_path / "config")},
            Path("/opt/aco%bin/python"),
        )


def test_login_preserves_the_lexical_venv_python_path(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "aco" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "workspace head"),
        config_path,
    )
    executable = tmp_path / "venv" / "bin" / "python"
    executable.parent.mkdir(parents=True)
    os.symlink("/usr/bin/python3", executable)
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "config")}

    workspace.enable_login(config_path, environment, executable)

    assert (
        f"Exec={executable} -I -m agent_coordination.cli _run-at-login"
        in workspace.login_desktop_path(environment).read_text()
    )


def test_login_recovery_generates_a_canonical_attempt_uuid(monkeypatch, tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "login-attempt.json"
    monkeypatch.setattr(
        workspace,
        "run_projects",
        lambda *_arguments, **_kwargs: (workspace.RunOutcome("alpha", workspace.RunState.STARTED),),
    )

    workspace.run_login_recovery(tmp_path / "workspace.toml", state_path)

    identifier = uuid.UUID(workspace.load_login_attempt(state_path).attempt_id)
    assert identifier.version == 4


def test_login_disable_refuses_a_foreign_launcher_without_changing_it(tmp_path: Path) -> None:
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "config")}
    desktop_path = workspace.login_desktop_path(environment)
    desktop_path.parent.mkdir(parents=True)
    desktop_path.parent.chmod(0o700)
    desktop_path.write_text("[Desktop Entry]\nType=Application\nExec=other\n")
    original = desktop_path.read_bytes()

    with pytest.raises(workspace.WorkspaceError, match="unowned"):
        workspace.disable_login(environment)

    assert desktop_path.read_bytes() == original


def test_login_refuses_a_non_python_launcher_without_changing_it(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "aco" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "workspace head"),
        config_path,
    )
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "config")}
    desktop_path = workspace.login_desktop_path(environment)
    desktop_path.parent.mkdir(parents=True)
    desktop_path.parent.chmod(0o700)
    desktop_path.write_text(
        "[Desktop Entry]\nType=Application\nName=ACO workspace recovery\n"
        "Exec=/bin/sh -I -m agent_coordination.cli _run-at-login\n"
        "X-Aco-Owner=agent-coordination/login-v1\n"
    )
    original = desktop_path.read_bytes()

    with pytest.raises(workspace.WorkspaceError, match="unowned"):
        workspace.enable_login(config_path, environment, Path("/candidate/python"))
    with pytest.raises(workspace.WorkspaceError, match="unowned"):
        workspace.disable_login(environment)

    assert (
        workspace.login_launcher_state(environment, Path("/candidate/python"))
        is workspace.LoginLauncherState.CONFLICT
    )
    assert desktop_path.read_bytes() == original


def test_login_refuses_a_relative_python_launcher_without_changing_it(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "aco" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "workspace head"),
        config_path,
    )
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "config")}
    desktop_path = workspace.login_desktop_path(environment)
    desktop_path.parent.mkdir(parents=True)
    desktop_path.parent.chmod(0o700)
    desktop_path.write_text(
        "[Desktop Entry]\nType=Application\nName=ACO workspace recovery\n"
        "Exec=python -I -m agent_coordination.cli _run-at-login\n"
        "X-Aco-Owner=agent-coordination/login-v1\n"
    )
    original = desktop_path.read_bytes()

    with pytest.raises(workspace.WorkspaceError, match="absolute safe path"):
        workspace.enable_login(config_path, environment, Path("python"))
    with pytest.raises(workspace.WorkspaceError, match="unowned"):
        workspace.enable_login(config_path, environment, Path("/candidate/python"))
    with pytest.raises(workspace.WorkspaceError, match="unowned"):
        workspace.disable_login(environment)

    assert (
        workspace.login_launcher_state(environment, Path("/candidate/python"))
        is workspace.LoginLauncherState.CONFLICT
    )
    assert desktop_path.read_bytes() == original


@pytest.mark.parametrize("kind", ["directory", "symlink"])
def test_login_preserves_non_regular_launcher_conflicts(tmp_path: Path, kind: str) -> None:
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "config")}
    config_path = tmp_path / "config" / "aco" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "workspace head"),
        config_path,
    )
    desktop_path = workspace.login_desktop_path(environment)
    desktop_path.parent.mkdir(parents=True)
    desktop_path.parent.chmod(0o700)
    if kind == "directory":
        desktop_path.mkdir()
    else:
        os.symlink(tmp_path / "other.desktop", desktop_path)

    with pytest.raises(workspace.WorkspaceError, match="unowned"):
        workspace.enable_login(config_path, environment, Path("/candidate/python"))
    with pytest.raises(workspace.WorkspaceError, match="unowned"):
        workspace.disable_login(environment)

    assert (
        workspace.login_launcher_state(environment, Path("/candidate/python"))
        is workspace.LoginLauncherState.CONFLICT
    )
    assert desktop_path.is_dir() if kind == "directory" else desktop_path.is_symlink()


def test_login_reports_an_owned_launcher_for_an_old_distribution_as_stale(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "aco" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "workspace head"),
        config_path,
    )
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "config")}
    workspace.enable_login(config_path, environment, Path("/old/python"))

    assert (
        workspace.login_launcher_state(environment, Path("/new/python"))
        is workspace.LoginLauncherState.STALE
    )


def test_login_recovery_keeps_unknown_outcomes_after_interruption(
    monkeypatch, tmp_path: Path
) -> None:
    state_path = tmp_path / "state" / "login-attempt.json"
    monkeypatch.setattr(
        workspace,
        "run_projects",
        lambda *_arguments, **_kwargs: (_ for _ in ()).throw(KeyboardInterrupt),
    )

    with pytest.raises(KeyboardInterrupt):
        workspace.run_login_recovery(tmp_path / "workspace.toml", state_path)

    attempt = workspace.load_login_attempt(state_path)
    assert attempt.state == "running"
    assert attempt.outcomes == ()


def test_login_recovery_serializes_attempts_and_keeps_the_later_result(
    monkeypatch, tmp_path: Path
) -> None:
    state_path = tmp_path / "state" / "login-attempt.json"
    first_started = Event()
    release_first = Event()
    second_attempted = Event()
    calls: list[str] = []

    def recover(*_arguments: object, **_kwargs: object) -> tuple[workspace.RunOutcome, ...]:
        call = "first" if not calls else "second"
        calls.append(call)
        if call == "first":
            first_started.set()
            assert release_first.wait(timeout=1)
        return (workspace.RunOutcome(call, workspace.RunState.STARTED),)

    monkeypatch.setattr(workspace, "run_projects", recover)
    first = Thread(
        target=workspace.run_login_recovery,
        args=(tmp_path / "workspace.toml", state_path),
        kwargs={"new_attempt_id": lambda: "123e4567-e89b-42d3-a456-426614174000"},
    )
    second = Thread(
        target=lambda: (
            second_attempted.set(),
            workspace.run_login_recovery(
                tmp_path / "workspace.toml",
                state_path,
                new_attempt_id=lambda: "123e4567-e89b-42d3-a456-426614174001",
            ),
        ),
    )
    first.start()
    assert first_started.wait(timeout=1)
    second.start()
    assert second_attempted.wait(timeout=1)
    release_first.set()
    first.join(timeout=1)
    second.join(timeout=1)

    assert not first.is_alive()
    assert not second.is_alive()
    assert calls == ["first", "second"]
    assert (
        workspace.load_login_attempt(state_path).attempt_id
        == "123e4567-e89b-42d3-a456-426614174001"
    )


def test_login_recovery_does_not_launch_if_the_initial_record_cannot_be_written(
    monkeypatch, tmp_path: Path
) -> None:
    launched = False

    def fail_write(*_arguments: object) -> None:
        raise workspace.WorkspaceError("recording failed")

    def recover(*_arguments: object, **_kwargs: object) -> tuple[workspace.RunOutcome, ...]:
        nonlocal launched
        launched = True
        return ()

    monkeypatch.setattr(workspace, "_write_login_attempt", fail_write)
    monkeypatch.setattr(workspace, "run_projects", recover)

    with pytest.raises(workspace.WorkspaceError, match="recording failed"):
        workspace.run_login_recovery(tmp_path / "workspace.toml", tmp_path / "state.json")

    assert launched is False


def test_login_recovery_preserves_its_running_record_when_completion_cannot_be_written(
    monkeypatch, tmp_path: Path
) -> None:
    state_path = tmp_path / "state" / "login-attempt.json"
    writes = 0
    write_attempt = workspace._write_login_attempt

    def fail_completion(path: Path, attempt: workspace.LoginAttempt) -> None:
        nonlocal writes
        writes += 1
        if writes == 2:
            raise workspace.WorkspaceError("recording failed")
        write_attempt(path, attempt)

    monkeypatch.setattr(workspace, "_write_login_attempt", fail_completion)
    monkeypatch.setattr(
        workspace,
        "run_projects",
        lambda *_arguments, **_kwargs: (workspace.RunOutcome("alpha", workspace.RunState.STARTED),),
    )

    with pytest.raises(workspace.WorkspaceError, match="recording failed"):
        workspace.run_login_recovery(tmp_path / "workspace.toml", state_path)

    assert workspace.load_login_attempt(state_path).state == "running"


def test_login_configuration_state_distinguishes_missing_malformed_empty_and_valid(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config" / "aco" / "workspace.toml"

    assert (
        workspace.login_configuration_state(config_path)
        is workspace.LoginConfigurationState.MISSING
    )

    config_path.parent.mkdir(parents=True)
    config_path.write_text("version = 99\n")
    assert (
        workspace.login_configuration_state(config_path)
        is workspace.LoginConfigurationState.MALFORMED
    )

    config_path.write_text("version = 2\nprojects = {}\n")
    assert (
        workspace.login_configuration_state(config_path) is workspace.LoginConfigurationState.EMPTY
    )

    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "workspace head"),
        config_path,
    )
    assert (
        workspace.login_configuration_state(config_path) is workspace.LoginConfigurationState.VALID
    )


def test_login_enable_requires_a_registered_workspace(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "aco" / "workspace.toml"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("version = 2\nprojects = {}\n")

    with pytest.raises(workspace.WorkspaceError, match="no registered projects"):
        workspace.enable_login(
            config_path,
            {"XDG_CONFIG_HOME": str(tmp_path / "config")},
            Path("/candidate/python"),
        )


def test_login_disable_reports_an_absent_launcher_without_creating_directories(
    tmp_path: Path,
) -> None:
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "config")}

    assert workspace.disable_login(environment) is False
    assert not workspace.login_desktop_path(environment).parent.exists()


def test_login_uses_the_configured_home_when_xdg_roots_are_not_set(tmp_path: Path) -> None:
    environment = {"HOME": str(tmp_path / "home")}

    assert (
        workspace.login_desktop_path(environment)
        == tmp_path / "home" / ".config/autostart/aco-workspace.desktop"
    )
    assert (
        workspace.login_attempt_path(environment)
        == tmp_path / "home" / ".local/state/aco/login-attempt.json"
    )


def test_login_uses_the_process_home_when_xdg_roots_and_home_are_not_set(
    monkeypatch, tmp_path: Path
) -> None:
    home = tmp_path / "home"
    monkeypatch.setattr(workspace.Path, "home", lambda: home)

    assert workspace.login_desktop_path({}) == home / ".config/autostart/aco-workspace.desktop"
    assert workspace.login_attempt_path({}) == home / ".local/state/aco/login-attempt.json"


@pytest.mark.parametrize("kind", ["blocked", "insecure"])
def test_login_enable_refuses_an_unusable_private_launcher_directory(
    tmp_path: Path, kind: str
) -> None:
    config_path = tmp_path / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "workspace head"),
        config_path,
    )
    root = tmp_path / "config"
    if kind == "blocked":
        root.write_text("not a directory")
    else:
        (root / "autostart").mkdir(parents=True)
        (root / "autostart").chmod(0o755)

    with pytest.raises(
        workspace.WorkspaceError, match=r"private login directory|private and owned"
    ):
        workspace.enable_login(
            config_path, {"XDG_CONFIG_HOME": str(root)}, Path("/candidate/python")
        )


def test_login_disable_keeps_an_owned_launcher_when_removal_fails(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "workspace head"),
        config_path,
    )
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "config")}
    workspace.enable_login(config_path, environment, Path("/candidate/python"))
    desktop_path = workspace.login_desktop_path(environment)
    original = desktop_path.read_bytes()
    unlink = Path.unlink

    def refuse_unlink(path: Path, missing_ok: bool = False) -> None:
        if path == desktop_path:
            raise OSError("read-only")
        unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", refuse_unlink)

    with pytest.raises(workspace.WorkspaceError, match="cannot remove login launcher"):
        workspace.disable_login(environment)

    assert desktop_path.read_bytes() == original


def test_login_recovery_reports_an_unreplaceable_state_record(tmp_path: Path, monkeypatch) -> None:
    state_path = tmp_path / "state" / "login-attempt.json"
    state_path.parent.mkdir(mode=0o700)
    state_path.mkdir()
    monkeypatch.setattr(
        workspace,
        "run_projects",
        lambda *_arguments, **_kwargs: (workspace.RunOutcome("alpha", workspace.RunState.STARTED),),
    )

    with pytest.raises(workspace.WorkspaceError, match="cannot write login recovery state"):
        workspace.run_login_recovery(tmp_path / "workspace.toml", state_path)

    assert state_path.is_dir()
    assert list(state_path.parent.glob(".login-attempt.json.*")) == []


def test_login_recovery_records_an_empty_workspace_result_as_a_failure(
    monkeypatch, tmp_path: Path
) -> None:
    state_path = tmp_path / "state" / "login-attempt.json"
    monkeypatch.setattr(workspace, "run_projects", lambda *_arguments: ())

    result = workspace.run_login_recovery(tmp_path / "workspace.toml", state_path)

    assert result.exit_status == 1
    assert workspace.load_login_attempt(state_path).failure == "workspace failure"


@pytest.mark.parametrize("contents", ["{", "[not a record]"])
def test_login_rejects_unreadable_or_non_json_attempt_records(
    tmp_path: Path, contents: str
) -> None:
    state_path = tmp_path / "state" / "login-attempt.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(contents)

    with pytest.raises(workspace.WorkspaceError, match="login attempt record is malformed"):
        workspace.load_login_attempt(state_path)


def test_login_rejects_a_directory_as_an_attempt_record(tmp_path: Path) -> None:
    state_path = tmp_path / "state" / "login-attempt.json"
    state_path.mkdir(parents=True)

    with pytest.raises(workspace.WorkspaceError, match="login attempt record is malformed"):
        workspace.load_login_attempt(state_path)


@pytest.mark.parametrize(
    "record",
    [
        [],
        {"version": 1},
        {**_completed_login_record(), "unexpected": True},
        {**_completed_login_record(), "version": 2},
        {**_completed_login_record(), "attempt_id": 7},
        {**_completed_login_record(), "attempt_id": "123e4567-e89b-12d3-a456-426614174000"},
        {**_completed_login_record(), "started_at": "not-a-time"},
        {**_completed_login_record(), "started_at": "invalidTtime"},
        {**_completed_login_record(), "started_at": "2026-09-09T00:00:00"},
        {**_completed_login_record(), "state": "unknown"},
        {**_completed_login_record(), "outcomes": {}},
        {**_completed_login_record(), "outcomes": [{}]},
        {**_completed_login_record(), "outcomes": [{"project": "?", "outcome": "started"}]},
        {**_completed_login_record(), "outcomes": [{"project": "alpha", "outcome": "unknown"}]},
        {**_completed_login_record(), "state": "running", "outcomes": []},
        {key: value for key, value in _completed_login_record().items() if key != "completed_at"},
        {**_completed_login_record(), "completed_at": "2026-09-08T00:00:00+00:00"},
        {**_completed_login_record(), "failure": "workspace failure"},
        {**_completed_login_record(), "outcomes": []},
        {**_completed_login_record(), "outcomes": [], "failure": "private detail"},
        {**_completed_login_record(), "outcomes": [], "completed_at": 7},
    ],
)
def test_login_rejects_malformed_persisted_attempt_records(tmp_path: Path, record: object) -> None:
    state_path = tmp_path / "state" / "login-attempt.json"
    state_path.parent.mkdir(parents=True)
    state_path.write_text(json.dumps(record))

    with pytest.raises(workspace.WorkspaceError, match="login attempt record is malformed"):
        workspace.load_login_attempt(state_path)


@pytest.mark.parametrize(
    "entry",
    [
        _owned_login_entry().rstrip("\n"),
        _owned_login_entry().replace("[Desktop Entry]", "[Foreign Entry]"),
        _owned_login_entry().replace("Type=Application", "Type"),
        _owned_login_entry().replace("Name=ACO workspace recovery", "Other=value"),
        _owned_login_entry().replace("Type=Application\n", "Type=Application\nType=Application\n"),
        _owned_login_entry().replace("Name=ACO workspace recovery", "Name=Foreign recovery"),
        _owned_login_entry().replace("_run-at-login", "_run-at-login extra"),
        _owned_login_entry().replace("Exec=/", "Exec= /"),
        _owned_login_entry('"/candidate\\\\q" -I -m agent_coordination.cli _run-at-login'),
        _owned_login_entry('"/candidate/python -I -m agent_coordination.cli _run-at-login'),
        _owned_login_entry('"/candidate/python"x -I -m agent_coordination.cli _run-at-login'),
        _owned_login_entry("/candidate/$python -I -m agent_coordination.cli _run-at-login"),
        _owned_login_entry("/candidate\\python -I -m agent_coordination.cli _run-at-login"),
        _owned_login_entry("/candidate/python% -I -m agent_coordination.cli _run-at-login"),
    ],
)
def test_login_status_preserves_malformed_desktop_templates_as_conflicts(
    tmp_path: Path, entry: str
) -> None:
    environment = {"XDG_CONFIG_HOME": str(tmp_path / "config")}
    desktop_path = workspace.login_desktop_path(environment)
    desktop_path.parent.mkdir(parents=True)
    desktop_path.parent.chmod(0o700)
    desktop_path.write_text(entry)
    original = desktop_path.read_bytes()

    assert (
        workspace.login_launcher_state(environment, Path("/candidate/python"))
        is workspace.LoginLauncherState.CONFLICT
    )
    assert desktop_path.read_bytes() == original


def test_register_is_idempotent_for_the_same_stopped_mapping(tmp_path: Path) -> None:
    config_path = tmp_path / "aco" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()

    handoff = workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "old head")
    first = workspace.register_project(handoff, config_path)
    second = workspace.register_project(handoff, config_path)

    assert first is True
    assert second is False
    assert workspace.load_config(config_path).projects["alpha"].directory == project_path.resolve()


def test_live_registration_persists_only_a_validated_process_receipt(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "aco" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    receipt = terminal.ExternalProcessReceipt("boot", 41, 31)
    monkeypatch.setattr(terminal, "register_live_process", lambda *_arguments: receipt)

    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "old head", live_pid=41),
        config_path,
    )

    project = workspace.load_config(config_path).projects["alpha"]
    assert project.external_process == workspace.ExternalProcessReceipt("boot", 41, 31)
    assert "codex\\0" not in config_path.read_text()


@pytest.mark.parametrize(
    "receipt",
    [
        'external_process = { boot_id = "boot", pid = 0, start_time = 31 }\n',
        'external_process = { boot_id = "boot", pid = 41 }\n',
        'external_process = { boot_id = "boot", pid = 41, start_time = 31, argv = "secret" }\n',
    ],
)
def test_v3_rejects_partial_or_unknown_external_process_receipts(
    tmp_path: Path, receipt: str
) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    config_path = tmp_path / "workspace.toml"
    config_path.write_text(
        f'version = 3\n[projects.alpha]\npath = "{project_path}"\n'
        f'session_id = "{SESSION_ID}"\nagent = "head"\nprovider = "codex"\n{receipt}'
    )

    with pytest.raises(workspace.WorkspaceError, match="external process receipt"):
        workspace.load_config(config_path)


def test_register_refuses_an_identity_replacement(tmp_path: Path) -> None:
    config_path = tmp_path / "aco" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "old head"), config_path
    )

    replacement = workspace.WorkspaceRegistration(
        "alpha", project_path, "123e4567-e89b-12d3-a456-426614174001", "old head"
    )

    with pytest.raises(workspace.WorkspaceError, match="already registered"):
        workspace.register_project(replacement, config_path)


def test_register_refuses_a_provider_replacement(tmp_path: Path) -> None:
    config_path = tmp_path / "aco" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "old head"), config_path
    )

    replacement = workspace.WorkspaceRegistration(
        "alpha", project_path, SESSION_ID, "old head", provider=providers.Provider.CLAUDE
    )

    with pytest.raises(workspace.WorkspaceError, match="already registered"):
        workspace.register_project(replacement, config_path)


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("same-directory", "directory"),
        ("same-session", "codex session"),
    ],
)
def test_register_refuses_a_directory_or_session_used_by_another_project(
    tmp_path: Path, replacement: str, message: str
) -> None:
    config_path = tmp_path / "aco" / "workspace.toml"
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", alpha, SESSION_ID, "alpha head"), config_path
    )
    directory = alpha if replacement == "same-directory" else beta
    session_id = (
        SESSION_ID if replacement == "same-session" else "123e4567-e89b-12d3-a456-426614174001"
    )

    replacement_handoff = workspace.WorkspaceRegistration(
        "beta", directory, session_id, "beta head"
    )

    with pytest.raises(workspace.WorkspaceError, match=message):
        workspace.register_project(replacement_handoff, config_path)


@pytest.mark.parametrize(
    ("agent", "model"),
    [
        ("head\nsecond", None),
        ("head\rsecond", None),
        ("head\0second", None),
        ("head", "model\nsecond"),
    ],
)
def test_register_refuses_launch_identifiers_that_cannot_be_safely_relaunched(
    tmp_path: Path, agent: str, model: str | None
) -> None:
    config_path = tmp_path / "aco" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()

    invalid_handoff = workspace.WorkspaceRegistration(
        "alpha", project_path, SESSION_ID, agent, model
    )

    with pytest.raises(workspace.WorkspaceError, match="line breaks or NUL"):
        workspace.register_project(invalid_handoff, config_path)

    assert config_path.exists() is False


def test_default_config_path_prefers_xdg_configuration(tmp_path: Path) -> None:
    assert (
        workspace.default_config_path({"XDG_CONFIG_HOME": str(tmp_path)})
        == tmp_path / "aco" / "workspace.toml"
    )
    assert (
        workspace.default_config_path({}, tmp_path)
        == tmp_path / ".config" / "aco" / "workspace.toml"
    )


def test_loading_a_legacy_mapping_defaults_its_provider_without_rewriting(tmp_path: Path) -> None:
    config_path = tmp_path / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    contents = (
        f'version = 1\n[projects.alpha]\npath = "{project_path}"\n'
        f'session_id = "{SESSION_ID}"\nagent = "restored head"\n'
    )
    config_path.write_text(contents)

    project = workspace.load_config(config_path).projects["alpha"]
    idempotent = workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "restored head"),
        config_path,
    )
    fake = FakeTerminal(terminal.Target(terminal.TargetState.ABSENT))
    outcomes = workspace.run_projects(
        config_path,
        environment={"XDG_RUNTIME_DIR": str(tmp_path)},
        runtime_directory=tmp_path,
        terminal_factory=lambda _socket: fake,
    )

    assert project.provider is providers.Provider.CODEX
    assert idempotent is False
    assert outcomes == (workspace.RunOutcome("alpha", workspace.RunState.STARTED),)
    assert fake.created[0][3].command == ["codex", "resume", SESSION_ID]
    assert config_path.read_text() == contents


def test_registering_a_provider_adds_explicit_identity_to_a_legacy_mapping(tmp_path: Path) -> None:
    config_path = tmp_path / "workspace.toml"
    codex_path = tmp_path / "codex"
    claude_path = tmp_path / "claude"
    codex_path.mkdir()
    claude_path.mkdir()
    config_path.write_text(
        f'version = 1\n[projects.codex]\npath = "{codex_path}"\n'
        f'session_id = "{SESSION_ID}"\nagent = "Codex head"\nmodel = "gpt-5.3-codex"\n'
    )

    created = workspace.register_project(
        workspace.WorkspaceRegistration(
            "claude",
            claude_path,
            "123e4567-e89b-12d3-a456-426614174001",
            "Claude head",
            "sonnet",
            provider=providers.Provider.CLAUDE,
        ),
        config_path,
    )
    projects = workspace.load_config(config_path).projects

    assert created is True
    assert config_path.read_text().startswith("version = 3\n")
    assert projects["codex"].provider is providers.Provider.CODEX
    assert projects["codex"].model == "gpt-5.3-codex"
    assert projects["claude"].provider is providers.Provider.CLAUDE
    assert projects["claude"].model == "sonnet"


@pytest.mark.parametrize(
    ("version", "provider", "message"),
    [
        (1, 'provider = "codex"\n', "unsupported or missing"),
        (2, "", "unsupported or missing"),
        (2, 'provider = "gemini"\n', "provider must be one of codex, claude, grok"),
    ],
)
def test_load_config_refuses_mixed_or_unknown_provider_records(
    tmp_path: Path, version: int, provider: str, message: str
) -> None:
    config_path = tmp_path / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    config_path.write_text(
        f'version = {version}\n[projects.alpha]\npath = "{project_path}"\n'
        f'session_id = "{SESSION_ID}"\nagent = "head"\n{provider}'
    )

    with pytest.raises(workspace.WorkspaceError, match=message):
        workspace.load_config(config_path)


def test_provider_scopes_native_uuid_uniqueness_but_not_directory_ownership(tmp_path: Path) -> None:
    config_path = tmp_path / "workspace.toml"
    codex_path = tmp_path / "codex"
    claude_path = tmp_path / "claude"
    duplicate_path = tmp_path / "duplicate"
    codex_path.mkdir()
    claude_path.mkdir()
    duplicate_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("codex", codex_path, SESSION_ID, "Codex head"), config_path
    )

    assert workspace.register_project(
        workspace.WorkspaceRegistration(
            "claude", claude_path, SESSION_ID, "Claude head", provider=providers.Provider.CLAUDE
        ),
        config_path,
    )

    duplicate = workspace.WorkspaceRegistration(
        "claude-copy", duplicate_path, SESSION_ID, "Claude head", provider=providers.Provider.CLAUDE
    )
    with pytest.raises(workspace.WorkspaceError, match="claude session"):
        workspace.register_project(duplicate, config_path)


def test_load_config_refuses_a_relative_project_directory(tmp_path: Path) -> None:
    config_path = tmp_path / "workspace.toml"
    config_path.write_text(
        'version = 1\n[projects.alpha]\npath = "missing"\nsession_id = "'
        + SESSION_ID
        + '"\nagent = "head"\n'
    )

    with pytest.raises(
        workspace.WorkspaceError,
        match="workspace project path must be an absolute canonical directory",
    ):
        workspace.load_config(config_path)


@pytest.mark.parametrize(
    ("contents", "message"),
    [
        ("version = [", "invalid workspace configuration"),
        ("version = 4\nprojects = {}\n", "version = 1, version = 2, or version = 3"),
        ("version = true\nprojects = {}\n", "version = 1, version = 2, or version = 3"),
        ("version = false\nprojects = {}\n", "version = 1, version = 2, or version = 3"),
        ("version = 1.0\nprojects = {}\n", "version = 1, version = 2, or version = 3"),
        ("version = 2.0\nprojects = {}\n", "version = 1, version = 2, or version = 3"),
        ("version = 1\nprojects = []\n", "projects must be a mapping"),
        (
            'version = 1\n[projects.alpha]\npath = 3\nsession_id = "x"\nagent = "head"\n',
            "path must be a string",
        ),
        (
            'version = 2\n[projects.alpha]\npath = "/tmp"\nsession_id = 3\n'
            'agent = "head"\nprovider = "gemini"\n',
            "session_id must be a string",
        ),
        (
            'version = 1\n[projects.alpha]\npath = "/tmp"\nsession_id = "x"\n',
            "unsupported or missing",
        ),
    ],
)
def test_load_config_refuses_malformed_or_non_strict_records(
    tmp_path: Path, contents: str, message: str
) -> None:
    config_path = tmp_path / "workspace.toml"
    config_path.write_text(contents)

    with pytest.raises(workspace.WorkspaceError, match=message):
        workspace.load_config(config_path)


def test_load_config_reports_missing_or_unreadable_configuration(tmp_path: Path) -> None:
    with pytest.raises(workspace.WorkspaceError, match="workspace configuration does not exist"):
        workspace.load_config(tmp_path / "missing.toml")

    with pytest.raises(workspace.WorkspaceError, match="cannot read workspace configuration"):
        workspace.load_config(tmp_path)


def test_load_config_refuses_a_non_table_project_record(tmp_path: Path) -> None:
    config_path = tmp_path / "workspace.toml"
    config_path.write_text('version = 1\nprojects = { alpha = "not-a-table" }\n')

    with pytest.raises(
        workspace.WorkspaceError, match="workspace projects must use project keys and table records"
    ):
        workspace.load_config(config_path)


def test_load_config_refuses_missing_or_noncanonical_project_directories(tmp_path: Path) -> None:
    config_path = tmp_path / "workspace.toml"
    missing = tmp_path / "missing"
    config_path.write_text(
        f'version = 1\n[projects.alpha]\npath = "{missing}"\n'
        f'session_id = "{SESSION_ID}"\nagent = "head"\n'
    )

    with pytest.raises(
        workspace.WorkspaceError, match="workspace project directory does not exist"
    ):
        workspace.load_config(config_path)

    directory = tmp_path / "project"
    directory.mkdir()
    link = tmp_path / "project-link"
    link.symlink_to(directory)
    config_path.write_text(
        f'version = 1\n[projects.alpha]\npath = "{link}"\n'
        f'session_id = "{SESSION_ID}"\nagent = "head"\n'
    )

    with pytest.raises(
        workspace.WorkspaceError, match="workspace project path must be a canonical directory"
    ):
        workspace.load_config(config_path)


@pytest.mark.parametrize(
    ("beta_path", "beta_session", "message"),
    [
        ("alpha", "123e4567-e89b-12d3-a456-426614174001", "duplicate canonical paths"),
        ("beta", SESSION_ID, "duplicate native provider UUIDs"),
    ],
)
def test_load_config_refuses_duplicate_project_identity(
    tmp_path: Path, beta_path: str, beta_session: str, message: str
) -> None:
    config_path = tmp_path / "workspace.toml"
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    directory = alpha if beta_path == "alpha" else beta
    config_path.write_text(
        f'version = 1\n[projects.alpha]\npath = "{alpha}"\n'
        f'session_id = "{SESSION_ID}"\nagent = "head"\n'
        f'\n[projects.beta]\npath = "{directory}"\nsession_id = "{beta_session}"\nagent = "head"\n'
    )

    with pytest.raises(workspace.WorkspaceError, match=message):
        workspace.load_config(config_path)


@pytest.mark.parametrize(
    ("key", "directory_kind", "session_id", "agent", "model", "message"),
    [
        ("bad key", "directory", SESSION_ID, "head", None, "project key must use letters"),
        ("alpha", "missing", SESSION_ID, "head", None, "directory does not exist"),
        ("alpha", "file", SESSION_ID, "head", None, "directory is not a directory"),
        ("alpha", "directory", "not-a-uuid", "head", None, "session_id must be an exact UUID"),
        (
            "alpha",
            "directory",
            SESSION_ID.upper(),
            "head",
            None,
            "session_id must be an exact UUID",
        ),
        ("alpha", "directory", SESSION_ID, " ", None, "agent must not be empty"),
        ("alpha", "directory", SESSION_ID, "head", " ", "model must not be empty"),
    ],
)
def test_register_refuses_each_invalid_mapping_field(
    tmp_path: Path,
    key: str,
    directory_kind: str,
    session_id: str,
    agent: str,
    model: str | None,
    message: str,
) -> None:
    directory = tmp_path / directory_kind
    if directory_kind == "directory":
        directory.mkdir()
    elif directory_kind == "file":
        directory.write_text("not a directory")

    invalid_handoff = workspace.WorkspaceRegistration(key, directory, session_id, agent, model)
    config_path = tmp_path / "config" / "workspace.toml"

    with pytest.raises(workspace.WorkspaceError, match=message):
        workspace.register_project(invalid_handoff, config_path)


def test_register_leaves_no_configuration_behind_when_atomic_replace_fails(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()

    def fail_replace(*_arguments: object) -> None:
        raise OSError("disk failure")

    monkeypatch.setattr(workspace.os, "replace", fail_replace)

    handoff = workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "restored head")

    with pytest.raises(workspace.WorkspaceError, match="cannot write"):
        workspace.register_project(handoff, config_path)

    assert [
        path for path in config_path.parent.glob("workspace.*") if path.name != "workspace.lock"
    ] == []


def test_failed_provider_migration_preserves_legacy_mapping(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "workspace.toml"
    codex_path = tmp_path / "codex"
    claude_path = tmp_path / "claude"
    codex_path.mkdir()
    claude_path.mkdir()
    original = (
        f'version = 1\n[projects.codex]\npath = "{codex_path}"\n'
        f'session_id = "{SESSION_ID}"\nagent = "Codex head"\n'
    )
    config_path.write_text(original)

    def fail_replace(*_arguments: object) -> None:
        raise OSError("disk failure")

    monkeypatch.setattr(workspace.os, "replace", fail_replace)
    handoff = workspace.WorkspaceRegistration(
        "claude",
        claude_path,
        "123e4567-e89b-12d3-a456-426614174001",
        "Claude head",
        provider=providers.Provider.CLAUDE,
    )

    with pytest.raises(workspace.WorkspaceError, match="cannot write"):
        workspace.register_project(handoff, config_path)

    assert config_path.read_text() == original


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        (False, "absolute"),
        (True, "unavailable"),
    ],
)
def test_run_refuses_an_invalid_runtime_directory(
    tmp_path: Path, missing: bool, message: str
) -> None:
    config_path = tmp_path / "workspace.toml"
    config_path.write_text("version = 1\nprojects = {}\n")
    selected_runtime = tmp_path / "missing" if missing else Path("relative")

    with pytest.raises(workspace.WorkspaceError, match=message):
        workspace.run_projects(config_path, runtime_directory=selected_runtime)


def test_run_refuses_a_shared_or_symlinked_workspace_runtime(tmp_path: Path) -> None:
    config_path = tmp_path / "workspace.toml"
    config_path.write_text("version = 1\nprojects = {}\n")
    shared_runtime = tmp_path / "shared-runtime"
    shared_runtime.mkdir(mode=0o755)

    with pytest.raises(workspace.WorkspaceError, match="private directory"):
        workspace.run_projects(config_path, runtime_directory=shared_runtime)

    private_runtime = tmp_path / "private-runtime"
    private_runtime.mkdir(mode=0o700)
    (private_runtime / "aco").symlink_to(tmp_path)

    with pytest.raises(workspace.WorkspaceError, match="must not be a symlink"):
        workspace.run_projects(config_path, runtime_directory=private_runtime)


def test_run_refuses_an_unknown_selected_project(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "restored head"),
        config_path,
    )

    with pytest.raises(workspace.WorkspaceError, match="not registered"):
        workspace.run_projects(config_path, "beta", runtime_directory=tmp_path)


def test_run_validates_the_complete_configuration_before_runtime(tmp_path: Path) -> None:
    config_path = tmp_path / "workspace.toml"
    config_path.write_text(
        'version = 1\n[projects.alpha]\npath = "relative"\n'
        f'session_id = "{SESSION_ID}"\nagent = "head"\n'
    )

    with pytest.raises(
        workspace.WorkspaceError,
        match="workspace project path must be an absolute canonical directory",
    ):
        workspace.run_projects(config_path, runtime_directory=Path("relative"))


def test_register_persists_an_optional_model(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()

    workspace.register_project(
        workspace.WorkspaceRegistration(
            "alpha", project_path, SESSION_ID, "restored head", "gpt-5.3-codex"
        ),
        config_path,
    )

    assert workspace.load_config(config_path).projects["alpha"].model == "gpt-5.3-codex"


class FakeTerminal:
    def __init__(
        self,
        target: terminal.Target,
        failed_project: str | None = None,
        *,
        attach_viewer: bool = True,
        pending_after_create: bool = False,
        pending_after_open: bool = False,
        failure_after_open: bool = False,
        ownerless_after_open: bool = False,
        attach_after_launch: bool = False,
        failure_consumption_error: bool = False,
    ):
        self.target = target
        self.failed_project = failed_project
        self.attach_viewer = attach_viewer
        self.pending_after_create = pending_after_create
        self.pending_after_open = pending_after_open
        self.failure_after_open = failure_after_open
        self.ownerless_after_open = ownerless_after_open
        self.attach_after_launch = attach_after_launch
        self.failure_consumption_error = failure_consumption_error
        self.created: list[tuple[str, str | None, Path, terminal.Launch]] = []
        self.retried: list[tuple[str, terminal.Launch]] = []
        self.opened: list[tuple[str, Mapping[str, str]]] = []
        self.consumed_failures: list[tuple[str, str]] = []

    def inspect(self, project: str) -> terminal.Target:
        if project == self.failed_project:
            raise terminal.TerminalError("tmux is unavailable")
        return self.target

    def inspect_all(self) -> tuple[terminal.Target, ...]:
        if self.target.state is terminal.TargetState.ABSENT:
            return ()
        return (self.target,)

    def create(
        self,
        project: str,
        session_id: str | None,
        directory: Path,
        launch: terminal.Launch,
    ) -> None:
        self.created.append((project, session_id, directory, launch))
        enrollment = launch.enrollment
        if enrollment is not None:
            enrollment = terminal.Enrollment(
                enrollment.directory,
                enrollment.agent,
                enrollment.model,
                enrollment.attempt,
                terminal.EnrollmentState.PENDING,
            )
        self.target = terminal.Target(
            terminal.TargetState.ATTACHED
            if self.attach_after_launch
            else terminal.TargetState.DETACHED,
            project,
            session_id,
            _viewer_attempt() if self.pending_after_create else None,
            launch.provider,
            enrollment,
        )

    def retry(self, project: str, directory: Path, launch: terminal.Launch) -> None:
        self.retried.append((project, launch))
        self.target = terminal.Target(
            terminal.TargetState.ATTACHED
            if self.attach_after_launch
            else terminal.TargetState.DETACHED,
            project,
            SESSION_ID,
            self.target.viewer_attempt,
            self.target.provider,
            self.target.enrollment,
        )

    def retry_enrollment(self, project: str, directory: Path, launch: terminal.Launch) -> None:
        self.retried.append((project, launch))
        assert launch.enrollment is not None
        self.target = terminal.Target(
            terminal.TargetState.DETACHED,
            project,
            viewer_attempt=self.target.viewer_attempt,
            provider=providers.Provider.CODEX,
            enrollment=terminal.Enrollment(
                launch.enrollment.directory,
                launch.enrollment.agent,
                launch.enrollment.model,
                launch.enrollment.attempt,
                terminal.EnrollmentState.PENDING,
            ),
        )

    def open_viewer(self, project: str, environment: Mapping[str, str]) -> None:
        self.opened.append((project, environment))
        if self.attach_viewer:
            self.target = terminal.Target(
                terminal.TargetState.ATTACHED,
                project,
                self.target.session_id,
                provider=self.target.provider,
                enrollment=self.target.enrollment,
            )
        elif self.pending_after_open:
            self.target = replace(self.target, viewer_attempt=_viewer_attempt())
        elif self.failure_after_open:
            self.target = replace(
                self.target,
                viewer_attempt=_viewer_attempt(terminal.ViewerAttemptState.FAILED),
            )
        elif self.ownerless_after_open:
            self.target = replace(self.target, viewer_attempt=None)
        else:
            self.target = replace(self.target, viewer_attempt=_viewer_attempt())

    def consume_viewer_failure(self, project: str, token: str) -> None:
        self.consumed_failures.append((project, token))
        if self.failure_consumption_error:
            raise terminal.TerminalError("consume failed viewer attempt failed")
        if self.target.viewer_attempt == _viewer_attempt(terminal.ViewerAttemptState.FAILED, token):
            self.target = replace(self.target, viewer_attempt=None)

    def finalize_enrollment(self, project: str) -> None:
        assert self.target.enrollment is not None
        assert self.target.session_id is not None
        enrollment = self.target.enrollment
        self.target = terminal.Target(
            self.target.state,
            project,
            self.target.session_id,
            self.target.viewer_attempt,
            self.target.provider,
            terminal.Enrollment(
                enrollment.directory,
                enrollment.agent,
                enrollment.model,
                enrollment.attempt,
                terminal.EnrollmentState.FINAL,
                self.target.session_id,
            ),
        )

    def stage_enrollment(self, project: str, session_id: str) -> None:
        assert self.target.enrollment is not None
        enrollment = self.target.enrollment
        self.target = terminal.Target(
            self.target.state,
            project,
            session_id,
            self.target.viewer_attempt,
            self.target.provider,
            terminal.Enrollment(
                enrollment.directory,
                enrollment.agent,
                enrollment.model,
                enrollment.attempt,
                enrollment.state,
                session_id,
            ),
        )


def _viewer_attempt(
    state: terminal.ViewerAttemptState = terminal.ViewerAttemptState.PENDING,
    token: str = "00000000-0000-4000-8000-000000000000",
) -> terminal.ViewerAttempt:
    return terminal.ViewerAttempt(state, token)


def _pending_enrollment(
    directory: Path, attempt: str = "123e4567-e89b-12d3-a456-426614174002"
) -> terminal.Enrollment:
    return terminal.Enrollment(
        directory,
        "new head",
        None,
        attempt,
        terminal.EnrollmentState.PENDING,
    )


def _start_context(config_path: Path, runtime: Path, fake: FakeTerminal) -> workspace.StartContext:
    return workspace.StartContext(
        config_path,
        "/venv/bin/python -I -m agent_coordination.cli _capture-codex-start",
        {"XDG_RUNTIME_DIR": str(runtime)},
        runtime,
        lambda _socket: fake,
    )


def _pending_capture(tmp_path: Path) -> tuple[Path, Path, FakeTerminal, terminal.Launch]:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(terminal.Target(terminal.TargetState.ABSENT))
    workspace.start_project(
        workspace.StartRequest("alpha", project_path, "new head"),
        _start_context(config_path, tmp_path, fake),
    )
    return config_path, project_path, fake, fake.created[0][3]


def _startup_event(project_path: Path, session_id: str = SESSION_ID) -> dict[str, str]:
    return {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "session_id": session_id,
        "cwd": str(project_path),
    }


@pytest.mark.parametrize("model", [None, "gpt-5.3-codex"])
def test_start_opens_a_promptless_pending_codex_console(tmp_path: Path, model: str | None) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(terminal.Target(terminal.TargetState.ABSENT), attach_viewer=False)

    outcome = workspace.start_project(
        workspace.StartRequest("alpha", project_path, "new head", model),
        workspace.StartContext(
            config_path,
            "/venv/bin/python -I -m agent_coordination.cli _capture-codex-start",
            {"XDG_RUNTIME_DIR": str(tmp_path), "ACO_CAPTURE_ATTEMPT": "stale"},
            tmp_path,
            lambda _socket: fake,
        ),
    )

    assert outcome.state is workspace.RunState.ENROLLMENT_PENDING
    assert config_path.exists() is False
    launch = fake.created[0][3]
    assert launch.command[:3] == ["codex", "-C", str(project_path.resolve())]
    assert launch.enrollment is not None
    assert {
        "ACO_CAPTURE_PROJECT",
        "ACO_CAPTURE_DIRECTORY",
        "ACO_CAPTURE_CONFIG",
        "ACO_CAPTURE_RUNTIME",
        "ACO_CAPTURE_AGENT",
        "ACO_CAPTURE_ATTEMPT",
    } <= set(launch.environment)
    assert launch.environment["ACO_CAPTURE_PROJECT"] == "alpha"
    assert launch.environment["ACO_CAPTURE_DIRECTORY"] == str(project_path.resolve())
    assert launch.environment["ACO_CAPTURE_ATTEMPT"] == launch.enrollment.attempt
    assert launch.environment.get("ACO_CAPTURE_MODEL") == model
    assert "stale" not in launch.environment.values()


def test_start_reports_when_a_pending_console_already_has_a_viewer(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(terminal.Target(terminal.TargetState.ABSENT), pending_after_create=True)

    outcome = workspace.start_project(
        workspace.StartRequest("alpha", project_path, "new head"),
        _start_context(config_path, tmp_path, fake),
    )

    assert outcome == workspace.RunOutcome(
        "alpha", workspace.RunState.ENROLLMENT_PENDING, "waiting for first user submission"
    )
    assert fake.opened == []


def test_start_reuses_an_attached_pending_console_after_consuming_its_failed_receipt(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(
        terminal.Target(
            terminal.TargetState.ATTACHED,
            "alpha",
            viewer_attempt=_viewer_attempt(terminal.ViewerAttemptState.FAILED),
            provider=providers.Provider.CODEX,
            enrollment=_pending_enrollment(project_path),
        )
    )

    outcome = workspace.start_project(
        workspace.StartRequest("alpha", project_path, "new head"),
        _start_context(config_path, tmp_path, fake),
    )

    assert outcome == workspace.RunOutcome("alpha", workspace.RunState.ENROLLMENT_PENDING)
    assert fake.consumed_failures == [("alpha", "00000000-0000-4000-8000-000000000000")]
    assert fake.opened == []
    assert fake.retried == []


def test_start_surfaces_and_consumes_a_viewer_failure_without_retrying_enrollment(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    environment = {"XDG_RUNTIME_DIR": str(tmp_path)}
    fake = FakeTerminal(
        terminal.Target(terminal.TargetState.ABSENT),
        attach_viewer=False,
        failure_after_open=True,
    )

    outcome = workspace.start_project(
        workspace.StartRequest("alpha", project_path, "new head"),
        workspace.StartContext(
            config_path,
            "/venv/bin/python -I -m agent_coordination.cli _capture-codex-start",
            environment,
            tmp_path,
            lambda _socket: fake,
        ),
    )

    assert outcome == workspace.RunOutcome(
        "alpha", workspace.RunState.FAILED, "project console failed to attach"
    )
    assert fake.opened == [("alpha", environment)]
    assert fake.retried == []
    assert fake.consumed_failures == [("alpha", "00000000-0000-4000-8000-000000000000")]


def test_capture_commits_the_matching_pending_native_uuid(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700)
    fake = FakeTerminal(terminal.Target(terminal.TargetState.ABSENT))
    request = workspace.StartRequest("alpha", project_path, "new head")
    workspace.start_project(
        request,
        workspace.StartContext(
            config_path,
            "callback",
            {"XDG_RUNTIME_DIR": str(runtime)},
            runtime,
            lambda _socket: fake,
        ),
    )
    monkeypatch.setattr(terminal, "TmuxTerminal", lambda _socket: fake)
    launch = fake.created[0][3]
    workspace.capture_codex_start(
        {
            "hook_event_name": "SessionStart",
            "source": "startup",
            "session_id": SESSION_ID,
            "cwd": str(project_path.resolve()),
        },
        launch.environment,
    )

    assert workspace.load_config(config_path).projects["alpha"].session_id == SESSION_ID
    assert fake.target.enrollment is not None
    assert fake.target.enrollment.state is terminal.EnrollmentState.FINAL


def test_start_refuses_a_second_pending_project_for_the_same_canonical_path(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(terminal.Target(terminal.TargetState.ABSENT))
    pending = terminal.Target(
        terminal.TargetState.DETACHED,
        "beta",
        provider=providers.Provider.CODEX,
        enrollment=_pending_enrollment(project_path),
    )
    monkeypatch.setattr(fake, "inspect_all", lambda: (pending,))
    request = workspace.StartRequest("alpha", project_path, "new head")
    context = _start_context(config_path, tmp_path, fake)

    with pytest.raises(workspace.WorkspaceError, match="pending Codex start"):
        workspace.start_project(request, context)

    assert fake.created == []


@pytest.mark.parametrize(
    ("registered_key", "request_agent", "message"),
    [
        ("alpha", "other head", "different definition"),
        ("beta", "new head", "directory .* already registered"),
    ],
)
def test_start_refuses_a_definition_already_owned_by_configuration(
    tmp_path: Path, registered_key: str, request_agent: str, message: str
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration(registered_key, project_path, SESSION_ID, "new head"),
        config_path,
    )
    fake = FakeTerminal(terminal.Target(terminal.TargetState.ABSENT))
    request = workspace.StartRequest("alpha", project_path, request_agent)
    context = _start_context(config_path, tmp_path, fake)

    with pytest.raises(workspace.WorkspaceError, match=message):
        workspace.start_project(request, context)

    assert fake.created == []


def test_start_refuses_an_initializing_target_until_its_setup_is_complete(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(
        terminal.Target(
            terminal.TargetState.DETACHED,
            "alpha",
            provider=providers.Provider.CODEX,
            enrollment=terminal.Enrollment(
                project_path,
                "new head",
                None,
                "123e4567-e89b-12d3-a456-426614174002",
                terminal.EnrollmentState.INITIALIZING,
            ),
        )
    )
    request = workspace.StartRequest("alpha", project_path, "new head")
    context = _start_context(config_path, tmp_path, fake)

    with pytest.raises(workspace.WorkspaceError, match="setup is incomplete"):
        workspace.start_project(request, context)

    assert fake.created == []


def test_start_recovers_a_staged_native_uuid_before_starting_another_console(
    tmp_path: Path,
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    enrollment = terminal.Enrollment(
        project_path,
        "new head",
        None,
        "123e4567-e89b-12d3-a456-426614174002",
        terminal.EnrollmentState.PENDING,
        SESSION_ID,
    )
    fake = FakeTerminal(
        terminal.Target(
            terminal.TargetState.DETACHED,
            "alpha",
            SESSION_ID,
            provider=providers.Provider.CODEX,
            enrollment=enrollment,
        )
    )

    outcome = workspace.start_project(
        workspace.StartRequest("alpha", project_path, "new head"),
        _start_context(config_path, tmp_path, fake),
    )

    assert workspace.load_config(config_path).projects["alpha"].session_id == SESSION_ID
    assert fake.target.enrollment is not None
    assert fake.target.enrollment.state is terminal.EnrollmentState.FINAL
    assert fake.created == []
    assert outcome.state is workspace.RunState.REATTACHED


def test_start_reattaches_a_live_pending_enrollment_without_replacing_it(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(
        terminal.Target(
            terminal.TargetState.DETACHED,
            "alpha",
            provider=providers.Provider.CODEX,
            enrollment=_pending_enrollment(project_path),
        )
    )

    outcome = workspace.start_project(
        workspace.StartRequest("alpha", project_path, "new head"),
        _start_context(config_path, tmp_path, fake),
    )

    assert outcome == workspace.RunOutcome("alpha", workspace.RunState.ENROLLMENT_PENDING)
    assert fake.created == []


@pytest.mark.parametrize(
    "staged_session_id",
    [
        "not-a-uuid",
        "123E4567-E89B-12D3-A456-426614174000",
    ],
)
def test_start_refuses_an_invalid_staged_native_uuid_before_recovery(
    tmp_path: Path, staged_session_id: str
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(
        terminal.Target(
            terminal.TargetState.DETACHED,
            "alpha",
            staged_session_id,
            provider=providers.Provider.CODEX,
            enrollment=terminal.Enrollment(
                project_path,
                "new head",
                None,
                "123e4567-e89b-12d3-a456-426614174002",
                terminal.EnrollmentState.PENDING,
                staged_session_id,
            ),
        )
    )
    request = workspace.StartRequest("alpha", project_path, "new head")
    context = _start_context(config_path, tmp_path, fake)

    with pytest.raises(workspace.WorkspaceError, match="staged native session_id"):
        workspace.start_project(request, context)

    assert config_path.exists() is False
    assert fake.target.enrollment is not None
    assert fake.target.enrollment.state is terminal.EnrollmentState.PENDING
    assert fake.created == []


def test_start_refuses_a_target_with_a_different_requested_definition(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(
        terminal.Target(
            terminal.TargetState.DETACHED,
            "alpha",
            provider=providers.Provider.CODEX,
            enrollment=terminal.Enrollment(
                project_path,
                "other head",
                None,
                "123e4567-e89b-12d3-a456-426614174002",
                terminal.EnrollmentState.PENDING,
            ),
        )
    )
    request = workspace.StartRequest("alpha", project_path, "new head")
    context = _start_context(config_path, tmp_path, fake)

    with pytest.raises(workspace.WorkspaceError, match="foreign metadata"):
        workspace.start_project(request, context)

    assert config_path.exists() is False
    assert fake.created == []


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        (terminal.TargetState.ABSENT, workspace.RunState.STARTED),
        (terminal.TargetState.DETACHED, workspace.RunState.REATTACHED),
    ],
)
def test_start_uses_normal_recovery_for_a_registered_project(
    tmp_path: Path, state: terminal.TargetState, expected: workspace.RunState
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "new head"), config_path
    )
    enrollment = (
        None
        if state is terminal.TargetState.ABSENT
        else terminal.Enrollment(
            project_path,
            "new head",
            None,
            "123e4567-e89b-12d3-a456-426614174002",
            terminal.EnrollmentState.FINAL,
            SESSION_ID,
        )
    )
    fake = FakeTerminal(
        terminal.Target(
            state,
            "alpha" if enrollment else None,
            SESSION_ID if enrollment else None,
            provider=providers.Provider.CODEX if enrollment else None,
            enrollment=enrollment,
        )
    )

    outcome = workspace.start_project(
        workspace.StartRequest("alpha", project_path, "new head"),
        _start_context(config_path, tmp_path, fake),
    )

    assert outcome.state is expected


@pytest.mark.parametrize(
    ("enrollment", "message"),
    [
        (
            terminal.Enrollment(
                Path("/tmp"),
                "new head",
                None,
                "123e4567-e89b-12d3-a456-426614174002",
                terminal.EnrollmentState.INITIALIZING,
            ),
            "setup is incomplete",
        ),
        (
            terminal.Enrollment(
                Path("/tmp"),
                "new head",
                None,
                "123e4567-e89b-12d3-a456-426614174002",
                terminal.EnrollmentState.PENDING,
                "123e4567-e89b-12d3-a456-426614174009",
            ),
            "registered Codex UUID",
        ),
    ],
)
def test_start_refuses_an_inconsistent_registered_pending_target(
    tmp_path: Path, enrollment: terminal.Enrollment, message: str
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "new head"), config_path
    )
    target_enrollment = terminal.Enrollment(
        project_path,
        enrollment.agent,
        enrollment.model,
        enrollment.attempt,
        enrollment.state,
        enrollment.session_id,
    )
    fake = FakeTerminal(
        terminal.Target(
            terminal.TargetState.DETACHED,
            "alpha",
            target_enrollment.session_id,
            provider=providers.Provider.CODEX,
            enrollment=target_enrollment,
        )
    )
    request = workspace.StartRequest("alpha", project_path, "new head")
    context = _start_context(config_path, tmp_path, fake)

    with pytest.raises(workspace.WorkspaceError, match=message):
        workspace.start_project(request, context)


def test_start_refuses_an_ordinary_target_whose_path_is_not_terminal_owned(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(
        terminal.Target(
            terminal.TargetState.DETACHED,
            "old",
            SESSION_ID,
            provider=providers.Provider.CODEX,
        )
    )
    request = workspace.StartRequest("alpha", project_path, "new head")
    context = _start_context(config_path, tmp_path, fake)

    with pytest.raises(workspace.WorkspaceError, match="unknown ownership or path"):
        workspace.start_project(request, context)

    assert fake.created == []


@pytest.mark.parametrize(
    ("target", "error", "message"),
    [
        (
            terminal.Target(terminal.TargetState.ABSENT),
            terminal.TerminalError,
            "incomplete project",
        ),
        (
            terminal.Target(
                terminal.TargetState.DETACHED,
                "alpha",
                provider=providers.Provider.CODEX,
                enrollment=terminal.Enrollment(
                    Path("/workspace"),
                    "new head",
                    None,
                    "not-a-uuid",
                    terminal.EnrollmentState.PENDING,
                ),
            ),
            workspace.WorkspaceError,
            "fresh Codex attempt",
        ),
    ],
)
def test_start_refuses_an_invalid_terminal_snapshot(
    monkeypatch,
    tmp_path: Path,
    target: terminal.Target,
    error: type[Exception],
    message: str,
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(terminal.Target(terminal.TargetState.ABSENT))
    if target.enrollment is not None:
        enrollment = target.enrollment
        target = terminal.Target(
            target.state,
            target.project,
            provider=target.provider,
            enrollment=terminal.Enrollment(
                project_path,
                enrollment.agent,
                enrollment.model,
                enrollment.attempt,
                enrollment.state,
            ),
        )
    monkeypatch.setattr(fake, "inspect_all", lambda: (target,))
    request = workspace.StartRequest("alpha", project_path, "new head")
    context = _start_context(config_path, tmp_path, fake)

    with pytest.raises(error, match=message):
        workspace.start_project(request, context)


def test_run_refuses_a_registered_path_held_by_another_pending_start(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "restored head"),
        config_path,
    )
    fake = FakeTerminal(terminal.Target(terminal.TargetState.ABSENT))
    pending = terminal.Target(
        terminal.TargetState.DETACHED,
        "beta",
        provider=providers.Provider.CODEX,
        enrollment=_pending_enrollment(project_path),
    )
    monkeypatch.setattr(fake, "inspect_all", lambda: (pending,))

    outcomes = workspace.run_projects(
        config_path,
        "alpha",
        environment={"XDG_RUNTIME_DIR": str(tmp_path)},
        runtime_directory=tmp_path,
        terminal_factory=lambda _socket: fake,
    )

    assert outcomes[0].state is workspace.RunState.FAILED
    assert "pending Codex start" in outcomes[0].detail
    assert fake.created == []


def test_start_replaces_only_an_exited_uncaptured_attempt(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(
        terminal.Target(
            terminal.TargetState.EXITED,
            "alpha",
            provider=providers.Provider.CODEX,
            enrollment=_pending_enrollment(project_path),
        ),
        attach_viewer=False,
    )

    outcome = workspace.start_project(
        workspace.StartRequest("alpha", project_path, "new head"),
        _start_context(config_path, tmp_path, fake),
    )

    assert outcome.state is workspace.RunState.ENROLLMENT_PENDING
    assert config_path.exists() is False
    assert fake.retried[0][1].enrollment is not None
    assert fake.retried[0][1].enrollment.attempt != "123e4567-e89b-12d3-a456-426614174002"


@pytest.mark.parametrize(
    ("payload", "message"),
    [
        ({"hook_event_name": "SessionStart", "source": "resume"}, "startup SessionStart"),
        (
            {"hook_event_name": "SessionStart", "source": "startup", "session_id": "not-a-uuid"},
            "UUID",
        ),
        (
            {"hook_event_name": "SessionStart", "source": "startup", "cwd": "/tmp"},
            "cwd does not match",
        ),
        (
            {"hook_event_name": "SessionStart", "source": "startup", "cwd": None},
            "native cwd must be a string",
        ),
    ],
)
def test_capture_rejects_invalid_native_events_without_writing_configuration(
    monkeypatch, tmp_path: Path, payload: dict[str, object], message: str
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(terminal.Target(terminal.TargetState.ABSENT))
    workspace.start_project(
        workspace.StartRequest("alpha", project_path, "new head"),
        _start_context(config_path, tmp_path, fake),
    )
    monkeypatch.setattr(terminal, "TmuxTerminal", lambda _socket: fake)
    event: dict[str, object] = {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "session_id": SESSION_ID,
        "cwd": str(project_path),
    }
    event.update(payload)
    launch = fake.created[0][3]

    with pytest.raises(workspace.WorkspaceError, match=message):
        workspace.capture_codex_start(event, launch.environment)

    assert config_path.exists() is False
    assert fake.target.enrollment is not None
    assert fake.target.enrollment.session_id is None


@pytest.mark.parametrize("suffix", ["/", "/."])
def test_capture_refuses_noncanonical_native_cwd_text_without_writing_configuration(
    monkeypatch, tmp_path: Path, suffix: str
) -> None:
    config_path, project_path, fake, launch = _pending_capture(tmp_path)
    monkeypatch.setattr(terminal, "TmuxTerminal", lambda _socket: fake)
    event = _startup_event(project_path)
    event["cwd"] = f"{project_path}{suffix}"

    with pytest.raises(workspace.WorkspaceError, match="cwd does not match"):
        workspace.capture_codex_start(event, launch.environment)

    assert config_path.exists() is False
    assert fake.target.enrollment is not None
    assert fake.target.enrollment.session_id is None


def test_capture_refuses_a_missing_fresh_identity_without_writing_configuration(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(terminal.Target(terminal.TargetState.ABSENT))
    workspace.start_project(
        workspace.StartRequest("alpha", project_path, "new head"),
        _start_context(config_path, tmp_path, fake),
    )
    monkeypatch.setattr(terminal, "TmuxTerminal", lambda _socket: fake)
    environment = dict(fake.created[0][3].environment)
    del environment["ACO_CAPTURE_ATTEMPT"]

    with pytest.raises(workspace.WorkspaceError, match="capture identity is incomplete"):
        workspace.capture_codex_start(
            {
                "hook_event_name": "SessionStart",
                "source": "startup",
                "session_id": SESSION_ID,
                "cwd": str(project_path),
            },
            environment,
        )

    assert config_path.exists() is False


def test_capture_refuses_an_invalid_runtime_identity_before_inspecting_tmux(tmp_path: Path) -> None:
    _config_path, project_path, _fake, launch = _pending_capture(tmp_path)
    environment = {**launch.environment, "ACO_CAPTURE_RUNTIME": str(tmp_path)}
    event = _startup_event(project_path)

    with pytest.raises(workspace.WorkspaceError, match="runtime identity is invalid"):
        workspace.capture_codex_start(event, environment)


def test_capture_stages_the_native_uuid_before_an_atomic_config_failure(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(terminal.Target(terminal.TargetState.ABSENT))
    workspace.start_project(
        workspace.StartRequest("alpha", project_path, "new head"),
        _start_context(config_path, tmp_path, fake),
    )
    monkeypatch.setattr(terminal, "TmuxTerminal", lambda _socket: fake)
    monkeypatch.setattr(
        workspace, "_write_config", lambda *_arguments: (_ for _ in ()).throw(OSError())
    )
    launch = fake.created[0][3]

    with pytest.raises(OSError):
        workspace.capture_codex_start(
            {
                "hook_event_name": "SessionStart",
                "source": "startup",
                "session_id": SESSION_ID,
                "cwd": str(project_path),
            },
            launch.environment,
        )

    assert config_path.exists() is False
    assert fake.target.enrollment is not None
    assert fake.target.enrollment.session_id == SESSION_ID
    assert fake.target.enrollment.state is terminal.EnrollmentState.PENDING


def test_start_repairs_final_metadata_after_configuration_commits(
    tmp_path: Path, monkeypatch
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(terminal.Target(terminal.TargetState.ABSENT), attach_viewer=False)
    context = _start_context(config_path, tmp_path, fake)
    workspace.start_project(workspace.StartRequest("alpha", project_path, "new head"), context)
    monkeypatch.setattr(terminal, "TmuxTerminal", lambda _socket: fake)
    original_finalize = fake.finalize_enrollment
    monkeypatch.setattr(
        fake,
        "finalize_enrollment",
        lambda *_arguments: (_ for _ in ()).throw(terminal.TerminalError("tmux failed")),
    )
    launch = fake.created[0][3]

    with pytest.raises(terminal.TerminalError, match="tmux failed"):
        workspace.capture_codex_start(
            {
                "hook_event_name": "SessionStart",
                "source": "startup",
                "session_id": SESSION_ID,
                "cwd": str(project_path),
            },
            launch.environment,
        )

    monkeypatch.setattr(fake, "finalize_enrollment", original_finalize)
    outcome = workspace.start_project(
        workspace.StartRequest("alpha", project_path, "new head"), context
    )

    assert workspace.load_config(config_path).projects["alpha"].session_id == SESSION_ID
    assert fake.target.enrollment is not None
    assert fake.target.enrollment.state is terminal.EnrollmentState.FINAL
    assert fake.retried == []
    assert outcome.state in {
        workspace.RunState.ENROLLMENT_PENDING,
        workspace.RunState.REATTACHED,
        workspace.RunState.PENDING,
    }


def test_exact_repeated_capture_is_a_noop_after_finalization(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(terminal.Target(terminal.TargetState.ABSENT))
    workspace.start_project(
        workspace.StartRequest("alpha", project_path, "new head"),
        _start_context(config_path, tmp_path, fake),
    )
    monkeypatch.setattr(terminal, "TmuxTerminal", lambda _socket: fake)
    launch = fake.created[0][3]
    event = {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "session_id": SESSION_ID,
        "cwd": str(project_path),
    }

    workspace.capture_codex_start(event, launch.environment)
    workspace.capture_codex_start(event, launch.environment)

    assert workspace.load_config(config_path).projects == {
        "alpha": workspace.WorkspaceProject("alpha", project_path, SESSION_ID, "new head")
    }


def test_capture_refuses_a_different_uuid_after_finalization(tmp_path: Path, monkeypatch) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(terminal.Target(terminal.TargetState.ABSENT))
    workspace.start_project(
        workspace.StartRequest("alpha", project_path, "new head"),
        _start_context(config_path, tmp_path, fake),
    )
    monkeypatch.setattr(terminal, "TmuxTerminal", lambda _socket: fake)
    launch = fake.created[0][3]
    event = {
        "hook_event_name": "SessionStart",
        "source": "startup",
        "session_id": SESSION_ID,
        "cwd": str(project_path),
    }
    workspace.capture_codex_start(event, launch.environment)
    event["session_id"] = "123e4567-e89b-12d3-a456-426614174005"

    with pytest.raises(workspace.WorkspaceError, match="final attempt"):
        workspace.capture_codex_start(event, launch.environment)

    assert workspace.load_config(config_path).projects["alpha"].session_id == SESSION_ID


@pytest.mark.parametrize(
    ("state", "staged_session_id", "message"),
    [
        (terminal.EnrollmentState.INITIALIZING, None, "incomplete target setup"),
        (
            terminal.EnrollmentState.PENDING,
            "123e4567-e89b-12d3-a456-426614174008",
            "different staged UUID",
        ),
    ],
)
def test_capture_refuses_an_unready_or_differently_staged_target(
    monkeypatch,
    tmp_path: Path,
    state: terminal.EnrollmentState,
    staged_session_id: str | None,
    message: str,
) -> None:
    config_path, project_path, fake, launch = _pending_capture(tmp_path)
    enrollment = fake.target.enrollment
    assert enrollment is not None
    fake.target = terminal.Target(
        terminal.TargetState.DETACHED,
        "alpha",
        staged_session_id,
        provider=providers.Provider.CODEX,
        enrollment=terminal.Enrollment(
            project_path,
            enrollment.agent,
            enrollment.model,
            enrollment.attempt,
            state,
            staged_session_id,
        ),
    )
    monkeypatch.setattr(terminal, "TmuxTerminal", lambda _socket: fake)
    event = _startup_event(project_path)

    with pytest.raises(workspace.WorkspaceError, match=message):
        workspace.capture_codex_start(event, launch.environment)

    assert config_path.exists() is False


def test_capture_retains_an_unrelated_live_process_receipt(monkeypatch, tmp_path: Path) -> None:
    config_path, project_path, fake, launch = _pending_capture(tmp_path)
    other_path = tmp_path / "other"
    other_path.mkdir()
    receipt = terminal.ExternalProcessReceipt("boot", 41, 10)
    monkeypatch.setattr(terminal, "register_live_process", lambda *_arguments: receipt)
    workspace.register_project(
        workspace.WorkspaceRegistration(
            "beta",
            other_path,
            "123e4567-e89b-12d3-a456-426614174006",
            "other head",
            live_pid=41,
        ),
        config_path,
    )
    monkeypatch.setattr(terminal, "TmuxTerminal", lambda _socket: fake)

    workspace.capture_codex_start(_startup_event(project_path), launch.environment)

    projects = workspace.load_config(config_path).projects
    assert projects["alpha"].session_id == SESSION_ID
    assert projects["beta"].external_process == workspace.ExternalProcessReceipt("boot", 41, 10)


@pytest.mark.parametrize(
    ("conflict", "message"),
    [
        ("exact", None),
        ("directory", "directory .* already registered"),
        ("session", "codex session .* already registered"),
        ("final", "exact workspace mapping"),
    ],
)
def test_capture_preserves_each_existing_configuration_conflict(
    monkeypatch, tmp_path: Path, conflict: str, message: str | None
) -> None:
    config_path, project_path, fake, launch = _pending_capture(tmp_path)
    event = _startup_event(project_path)
    if conflict == "exact":
        workspace.register_project(
            workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "new head"),
            config_path,
        )
    elif conflict == "directory":
        workspace.register_project(
            workspace.WorkspaceRegistration(
                "beta", project_path, "123e4567-e89b-12d3-a456-426614174006", "new head"
            ),
            config_path,
        )
    elif conflict == "session":
        other_path = tmp_path / "other"
        other_path.mkdir()
        workspace.register_project(
            workspace.WorkspaceRegistration("beta", other_path, SESSION_ID, "new head"), config_path
        )
    else:
        enrollment = fake.target.enrollment
        assert enrollment is not None
        fake.target = terminal.Target(
            terminal.TargetState.DETACHED,
            "alpha",
            SESSION_ID,
            provider=providers.Provider.CODEX,
            enrollment=terminal.Enrollment(
                project_path,
                enrollment.agent,
                enrollment.model,
                enrollment.attempt,
                terminal.EnrollmentState.FINAL,
                SESSION_ID,
            ),
        )
    monkeypatch.setattr(terminal, "TmuxTerminal", lambda _socket: fake)

    if conflict == "exact":
        workspace.capture_codex_start(event, launch.environment)
        assert fake.target.enrollment is not None
        assert fake.target.enrollment.state is terminal.EnrollmentState.FINAL
    else:
        assert message is not None
        with pytest.raises(workspace.WorkspaceError, match=message):
            workspace.capture_codex_start(event, launch.environment)

    projects = {} if not config_path.exists() else workspace.load_config(config_path).projects
    if conflict == "exact":
        assert projects["alpha"].session_id == SESSION_ID
    elif conflict == "final":
        assert projects == {}
    else:
        assert set(projects) == {"beta"}


def test_final_target_without_its_workspace_mapping_is_not_adopted(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(
        terminal.Target(
            terminal.TargetState.DETACHED,
            "alpha",
            SESSION_ID,
            provider=providers.Provider.CODEX,
            enrollment=terminal.Enrollment(
                project_path,
                "new head",
                None,
                "123e4567-e89b-12d3-a456-426614174002",
                terminal.EnrollmentState.FINAL,
                SESSION_ID,
            ),
        )
    )
    request = workspace.StartRequest("alpha", project_path, "new head")
    context = _start_context(config_path, tmp_path, fake)

    with pytest.raises(workspace.WorkspaceError, match="final without a workspace mapping"):
        workspace.start_project(request, context)

    assert config_path.exists() is False


def test_capture_refuses_a_stale_attempt_after_a_new_attempt_is_pending(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(terminal.Target(terminal.TargetState.ABSENT))
    workspace.start_project(
        workspace.StartRequest("alpha", project_path, "new head"),
        _start_context(config_path, tmp_path, fake),
    )
    monkeypatch.setattr(terminal, "TmuxTerminal", lambda _socket: fake)
    launch = fake.created[0][3]
    stale_environment = {
        **launch.environment,
        "ACO_CAPTURE_ATTEMPT": "123e4567-e89b-12d3-a456-426614174003",
    }

    with pytest.raises(workspace.WorkspaceError, match="pending attempt"):
        workspace.capture_codex_start(
            {
                "hook_event_name": "SessionStart",
                "source": "startup",
                "session_id": SESSION_ID,
                "cwd": str(project_path),
            },
            stale_environment,
        )

    assert config_path.exists() is False


def test_capture_does_not_adopt_a_registration_that_raced_the_pending_attempt(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    fake = FakeTerminal(terminal.Target(terminal.TargetState.ABSENT))
    workspace.start_project(
        workspace.StartRequest("alpha", project_path, "new head"),
        _start_context(config_path, tmp_path, fake),
    )
    registered_uuid = "123e4567-e89b-12d3-a456-426614174004"
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, registered_uuid, "new head"),
        config_path,
    )
    monkeypatch.setattr(terminal, "TmuxTerminal", lambda _socket: fake)
    launch = fake.created[0][3]

    with pytest.raises(workspace.WorkspaceError, match="conflicts with the pending"):
        workspace.capture_codex_start(
            {
                "hook_event_name": "SessionStart",
                "source": "startup",
                "session_id": SESSION_ID,
                "cwd": str(project_path),
            },
            launch.environment,
        )

    assert workspace.load_config(config_path).projects["alpha"].session_id == registered_uuid


def test_run_starts_the_exact_registered_session_with_its_logical_identity(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "restored head"),
        config_path,
    )
    fake = FakeTerminal(terminal.Target(terminal.TargetState.ABSENT))

    outcomes = workspace.run_projects(
        config_path,
        environment={"XDG_RUNTIME_DIR": str(tmp_path), "ACO_AGENT": "launcher"},
        runtime_directory=tmp_path,
        terminal_factory=lambda _socket: fake,
    )

    assert outcomes == (workspace.RunOutcome("alpha", workspace.RunState.STARTED),)
    launch = fake.created[0][3]
    assert isinstance(launch, terminal.Launch)
    assert launch.command == ["codex", "resume", SESSION_ID]
    assert launch.environment == {"XDG_RUNTIME_DIR": str(tmp_path), "ACO_AGENT": "restored head"}
    assert fake.opened == [("alpha", {"XDG_RUNTIME_DIR": str(tmp_path), "ACO_AGENT": "launcher"})]


def test_run_refuses_a_registered_target_that_is_still_pending_enrollment(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "restored head"),
        config_path,
    )
    fake = FakeTerminal(
        terminal.Target(
            terminal.TargetState.DETACHED,
            "alpha",
            SESSION_ID,
            provider=providers.Provider.CODEX,
            enrollment=terminal.Enrollment(
                project_path,
                "restored head",
                None,
                "123e4567-e89b-12d3-a456-426614174002",
                terminal.EnrollmentState.PENDING,
                SESSION_ID,
            ),
        )
    )

    outcomes = workspace.run_projects(
        config_path,
        environment={"XDG_RUNTIME_DIR": str(tmp_path)},
        runtime_directory=tmp_path,
        terminal_factory=lambda _socket: fake,
    )

    assert outcomes == (
        workspace.RunOutcome(
            "alpha",
            workspace.RunState.FAILED,
            "tmux target 'aco-alpha' has pending enrollment metadata",
        ),
    )
    assert fake.created == []


def test_run_does_not_create_a_second_viewer_while_an_attachment_is_pending(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "restored head"),
        config_path,
    )
    fake = FakeTerminal(
        terminal.Target(terminal.TargetState.DETACHED, "alpha", SESSION_ID, _viewer_attempt())
    )

    outcomes = workspace.run_projects(
        config_path,
        environment={"XDG_RUNTIME_DIR": str(tmp_path)},
        runtime_directory=tmp_path,
        terminal_factory=lambda _socket: fake,
    )

    assert outcomes[0].state is workspace.RunState.PENDING
    assert fake.opened == []


def test_run_reattaches_a_matching_detached_head(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "restored head"),
        config_path,
    )
    fake = FakeTerminal(terminal.Target(terminal.TargetState.DETACHED, "alpha", SESSION_ID))

    outcomes = workspace.run_projects(
        config_path,
        environment={"XDG_RUNTIME_DIR": str(tmp_path)},
        runtime_directory=tmp_path,
        terminal_factory=lambda _socket: fake,
    )

    assert outcomes == (workspace.RunOutcome("alpha", workspace.RunState.REATTACHED),)
    assert fake.created == []
    assert fake.opened == [("alpha", {"XDG_RUNTIME_DIR": str(tmp_path)})]


@pytest.mark.parametrize(
    ("target", "expected_state"),
    [
        (
            terminal.Target(terminal.TargetState.ATTACHED, "alpha", SESSION_ID),
            workspace.RunState.REUSED,
        ),
        (
            terminal.Target(terminal.TargetState.DETACHED, "alpha", SESSION_ID, _viewer_attempt()),
            workspace.RunState.PENDING,
        ),
    ],
)
def test_run_reuses_an_attached_head_or_reports_an_existing_viewer_pending(
    tmp_path: Path, target: terminal.Target, expected_state: workspace.RunState
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "restored head"),
        config_path,
    )
    fake = FakeTerminal(target)

    outcomes = workspace.run_projects(
        config_path,
        environment={"XDG_RUNTIME_DIR": str(tmp_path)},
        runtime_directory=tmp_path,
        terminal_factory=lambda _socket: fake,
    )

    assert outcomes[0].state is expected_state
    assert fake.opened == []
    assert fake.consumed_failures == []


@pytest.mark.parametrize(
    ("pending_after_create", "attach_viewer", "expected"),
    [
        (True, True, workspace.RunState.PENDING),
        (False, False, workspace.RunState.PENDING),
    ],
)
def test_run_reports_pending_when_a_new_viewer_has_not_attached(
    tmp_path: Path, pending_after_create: bool, attach_viewer: bool, expected: workspace.RunState
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "restored head"),
        config_path,
    )
    fake = FakeTerminal(
        terminal.Target(terminal.TargetState.ABSENT),
        attach_viewer=attach_viewer,
        pending_after_create=pending_after_create,
    )

    outcomes = workspace.run_projects(
        config_path,
        environment={"XDG_RUNTIME_DIR": str(tmp_path)},
        runtime_directory=tmp_path,
        terminal_factory=lambda _socket: fake,
    )

    assert outcomes[0].state is expected
    if pending_after_create:
        assert fake.opened == []
    else:
        assert fake.opened == [("alpha", {"XDG_RUNTIME_DIR": str(tmp_path)})]


def _run_registered_project(tmp_path: Path, fake: FakeTerminal) -> tuple[workspace.RunOutcome, ...]:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir(exist_ok=True)
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "restored head"),
        config_path,
    )
    return workspace.run_projects(
        config_path,
        environment={"XDG_RUNTIME_DIR": str(tmp_path)},
        runtime_directory=tmp_path,
        terminal_factory=lambda _socket: fake,
    )


def test_run_reports_an_ownerless_post_open_failure_then_allows_an_explicit_retry(
    tmp_path: Path,
) -> None:
    fake = FakeTerminal(
        terminal.Target(terminal.TargetState.ABSENT),
        attach_viewer=False,
        ownerless_after_open=True,
    )

    first = _run_registered_project(tmp_path, fake)
    fake.attach_viewer = True
    second = _run_registered_project(tmp_path, fake)

    assert first == (
        workspace.RunOutcome("alpha", workspace.RunState.FAILED, "project console is not attached"),
    )
    assert second == (workspace.RunOutcome("alpha", workspace.RunState.REATTACHED),)
    assert len(fake.opened) == 2


def test_run_consumes_a_post_open_failed_receipt_without_retrying(tmp_path: Path) -> None:
    fake = FakeTerminal(
        terminal.Target(terminal.TargetState.ABSENT),
        attach_viewer=False,
        failure_after_open=True,
    )

    outcomes = _run_registered_project(tmp_path, fake)

    assert outcomes == (
        workspace.RunOutcome(
            "alpha", workspace.RunState.FAILED, "project console failed to attach"
        ),
    )
    assert fake.retried == []
    assert fake.consumed_failures == [("alpha", "00000000-0000-4000-8000-000000000000")]


def test_run_consumes_a_detached_failed_receipt_before_a_later_recovery(tmp_path: Path) -> None:
    fake = FakeTerminal(
        terminal.Target(
            terminal.TargetState.DETACHED,
            "alpha",
            SESSION_ID,
            _viewer_attempt(terminal.ViewerAttemptState.FAILED),
        )
    )

    first = _run_registered_project(tmp_path, fake)
    second = _run_registered_project(tmp_path, fake)

    assert first[0].state is workspace.RunState.FAILED
    assert second == (workspace.RunOutcome("alpha", workspace.RunState.REATTACHED),)
    assert len(fake.opened) == 1


def test_run_reuses_an_attached_target_after_consuming_its_failed_receipt(tmp_path: Path) -> None:
    fake = FakeTerminal(
        terminal.Target(
            terminal.TargetState.ATTACHED,
            "alpha",
            SESSION_ID,
            _viewer_attempt(terminal.ViewerAttemptState.FAILED),
        )
    )

    outcomes = _run_registered_project(tmp_path, fake)

    assert outcomes == (workspace.RunOutcome("alpha", workspace.RunState.REUSED),)
    assert fake.consumed_failures == [("alpha", "00000000-0000-4000-8000-000000000000")]
    assert fake.opened == []


def test_run_consumes_an_exited_failed_receipt_before_retrying_the_provider(tmp_path: Path) -> None:
    fake = FakeTerminal(
        terminal.Target(
            terminal.TargetState.EXITED,
            "alpha",
            SESSION_ID,
            _viewer_attempt(terminal.ViewerAttemptState.FAILED),
        )
    )

    outcomes = _run_registered_project(tmp_path, fake)

    assert outcomes[0].state is workspace.RunState.FAILED
    assert fake.retried == []
    assert fake.opened == []


def test_run_refuses_to_launch_when_failed_receipt_consumption_fails(tmp_path: Path) -> None:
    fake = FakeTerminal(
        terminal.Target(
            terminal.TargetState.DETACHED,
            "alpha",
            SESSION_ID,
            _viewer_attempt(terminal.ViewerAttemptState.FAILED),
        ),
        failure_consumption_error=True,
    )

    outcomes = _run_registered_project(tmp_path, fake)

    assert outcomes[0].state is workspace.RunState.FAILED
    assert fake.opened == []


@pytest.mark.parametrize(
    "attempt_state",
    [terminal.ViewerAttemptState.PENDING, terminal.ViewerAttemptState.ACCEPTED],
)
def test_run_retry_preserves_a_live_viewer_attempt_without_opening_another(
    tmp_path: Path, attempt_state: terminal.ViewerAttemptState
) -> None:
    fake = FakeTerminal(
        terminal.Target(
            terminal.TargetState.EXITED,
            "alpha",
            SESSION_ID,
            _viewer_attempt(attempt_state),
        )
    )

    outcomes = _run_registered_project(tmp_path, fake)

    assert outcomes[0].state is workspace.RunState.PENDING
    assert len(fake.retried) == 1
    assert fake.target.viewer_attempt == _viewer_attempt(attempt_state)
    assert fake.opened == []


def test_run_reports_foreign_tmux_metadata_without_adopting_it(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "restored head"),
        config_path,
    )
    fake = FakeTerminal(terminal.Target(terminal.TargetState.DETACHED, "foreign", SESSION_ID))

    outcomes = workspace.run_projects(
        config_path,
        environment={"XDG_RUNTIME_DIR": str(tmp_path)},
        runtime_directory=tmp_path,
        terminal_factory=lambda _socket: fake,
    )

    assert outcomes[0].state is workspace.RunState.FAILED
    assert fake.created == []
    assert fake.opened == []


def test_run_retries_an_exited_matching_pane_only_when_explicitly_invoked(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "restored head"),
        config_path,
    )
    fake = FakeTerminal(terminal.Target(terminal.TargetState.EXITED, "alpha", SESSION_ID))

    outcomes = workspace.run_projects(
        config_path,
        environment={"XDG_RUNTIME_DIR": str(tmp_path)},
        runtime_directory=tmp_path,
        terminal_factory=lambda _socket: fake,
    )

    assert outcomes == (workspace.RunOutcome("alpha", workspace.RunState.RETRIED),)
    assert fake.retried[0][1].command == ["codex", "resume", SESSION_ID]


def test_stopped_mapping_does_not_scan_for_external_ownership(monkeypatch, tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "restored head"),
        config_path,
    )
    fake = FakeTerminal(terminal.Target(terminal.TargetState.EXITED, "alpha", SESSION_ID))
    monkeypatch.setattr(
        terminal,
        "external_ownership",
        lambda *_arguments: pytest.fail("stopped mapping scanned for external ownership"),
    )

    outcomes = workspace.run_projects(
        config_path,
        environment={"XDG_RUNTIME_DIR": str(tmp_path)},
        runtime_directory=tmp_path,
        terminal_factory=lambda _socket: fake,
    )

    assert outcomes == (workspace.RunOutcome("alpha", workspace.RunState.RETRIED),)


def test_run_leaves_a_manual_native_replacement_alive_when_its_managed_pane_exited(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    monkeypatch.setattr(
        terminal,
        "register_live_process",
        lambda *_arguments: terminal.ExternalProcessReceipt("boot", 41, 10),
    )
    workspace.register_project(
        workspace.WorkspaceRegistration(
            "alpha", project_path, SESSION_ID, "restored head", live_pid=41
        ),
        config_path,
    )
    fake = FakeTerminal(terminal.Target(terminal.TargetState.EXITED, "alpha", SESSION_ID))
    monkeypatch.setattr(
        terminal,
        "external_ownership",
        lambda *_arguments: terminal.ExternalOwnership(terminal.ExternalOwnershipState.LIVE),
    )

    outcomes = workspace.run_projects(
        config_path,
        environment={"XDG_RUNTIME_DIR": str(tmp_path)},
        runtime_directory=tmp_path,
        terminal_factory=lambda _socket: fake,
    )

    assert outcomes == (workspace.RunOutcome("alpha", workspace.RunState.EXTERNAL),)
    assert fake.retried == []


def test_run_reports_unknown_external_ownership_without_retrying(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    monkeypatch.setattr(
        terminal,
        "register_live_process",
        lambda *_arguments: terminal.ExternalProcessReceipt("boot", 41, 10),
    )
    workspace.register_project(
        workspace.WorkspaceRegistration(
            "alpha", project_path, SESSION_ID, "restored head", live_pid=41
        ),
        config_path,
    )
    fake = FakeTerminal(terminal.Target(terminal.TargetState.EXITED, "alpha", SESSION_ID))
    monkeypatch.setattr(
        terminal,
        "external_ownership",
        lambda *_arguments: terminal.ExternalOwnership(terminal.ExternalOwnershipState.UNKNOWN),
    )

    outcomes = workspace.run_projects(
        config_path,
        environment={"XDG_RUNTIME_DIR": str(tmp_path)},
        runtime_directory=tmp_path,
        terminal_factory=lambda _socket: fake,
    )

    assert outcomes == (workspace.RunOutcome("alpha", workspace.RunState.UNKNOWN),)
    assert fake.retried == []


def test_live_receipt_allows_retry_after_observation_proves_no_external_owner(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    monkeypatch.setattr(
        terminal,
        "register_live_process",
        lambda *_arguments: terminal.ExternalProcessReceipt("boot", 41, 10),
    )
    workspace.register_project(
        workspace.WorkspaceRegistration(
            "alpha", project_path, SESSION_ID, "restored head", live_pid=41
        ),
        config_path,
    )
    fake = FakeTerminal(terminal.Target(terminal.TargetState.EXITED, "alpha", SESSION_ID))
    monkeypatch.setattr(
        terminal,
        "external_ownership",
        lambda *_arguments: terminal.ExternalOwnership(terminal.ExternalOwnershipState.ABSENT),
    )

    outcomes = workspace.run_projects(
        config_path,
        environment={"XDG_RUNTIME_DIR": str(tmp_path)},
        runtime_directory=tmp_path,
        terminal_factory=lambda _socket: fake,
    )

    assert outcomes == (workspace.RunOutcome("alpha", workspace.RunState.RETRIED),)
    assert len(fake.retried) == 1


def test_live_registration_refuses_a_nonpositive_pid_before_observation(tmp_path: Path) -> None:
    project_path = tmp_path / "project"
    project_path.mkdir()
    registration = workspace.WorkspaceRegistration(
        "alpha", project_path, SESSION_ID, "head", live_pid=0
    )

    with pytest.raises(workspace.WorkspaceError, match="live_pid"):
        workspace.register_project(
            registration,
            tmp_path / "workspace.toml",
        )


@pytest.mark.parametrize(
    ("target", "expected_state"),
    [
        (terminal.Target(terminal.TargetState.ABSENT), workspace.RunState.STARTED),
        (
            terminal.Target(terminal.TargetState.EXITED, "alpha", SESSION_ID),
            workspace.RunState.RETRIED,
        ),
    ],
)
def test_run_reports_a_console_that_attached_during_launch_without_opening_another(
    tmp_path: Path, target: terminal.Target, expected_state: workspace.RunState
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", project_path, SESSION_ID, "restored head"),
        config_path,
    )
    fake = FakeTerminal(target, attach_after_launch=True)

    outcomes = workspace.run_projects(
        config_path,
        environment={"XDG_RUNTIME_DIR": str(tmp_path)},
        runtime_directory=tmp_path,
        terminal_factory=lambda _socket: fake,
    )

    assert outcomes == (workspace.RunOutcome("alpha", expected_state),)
    assert fake.opened == []
    assert fake.consumed_failures == []


def test_runtime_failure_for_one_project_does_not_hide_the_later_project(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration("alpha", alpha, SESSION_ID, "alpha head"), config_path
    )
    workspace.register_project(
        workspace.WorkspaceRegistration(
            "beta",
            beta,
            "123e4567-e89b-12d3-a456-426614174001",
            "beta head",
            provider=providers.Provider.GROK,
        ),
        config_path,
    )
    fake = FakeTerminal(terminal.Target(terminal.TargetState.ABSENT), failed_project="alpha")

    outcomes = workspace.run_projects(
        config_path,
        environment={"XDG_RUNTIME_DIR": str(tmp_path)},
        runtime_directory=tmp_path,
        terminal_factory=lambda _socket: fake,
    )

    assert [outcome.state for outcome in outcomes] == [
        workspace.RunState.FAILED,
        workspace.RunState.STARTED,
    ]
    assert fake.created[0][3].command == [
        "grok",
        "--resume",
        "123e4567-e89b-12d3-a456-426614174001",
        "--cwd",
        str(beta.resolve()),
    ]


@pytest.mark.parametrize("target_provider", [None, providers.Provider.CODEX])
@pytest.mark.parametrize(
    "target_state",
    [terminal.TargetState.ATTACHED, terminal.TargetState.DETACHED, terminal.TargetState.EXITED],
)
def test_run_refuses_a_legacy_or_mismatched_target_for_claude_before_opening_a_viewer(
    tmp_path: Path,
    target_provider: providers.Provider | None,
    target_state: terminal.TargetState,
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.WorkspaceRegistration(
            "alpha", project_path, SESSION_ID, "Claude head", provider=providers.Provider.CLAUDE
        ),
        config_path,
    )
    fake = FakeTerminal(
        terminal.Target(target_state, "alpha", SESSION_ID, provider=target_provider)
    )

    outcomes = workspace.run_projects(
        config_path,
        environment={"XDG_RUNTIME_DIR": str(tmp_path)},
        runtime_directory=tmp_path,
        terminal_factory=lambda _socket: fake,
    )

    assert outcomes[0].state is workspace.RunState.FAILED
    assert fake.created == []
    assert fake.retried == []
    assert fake.opened == []
