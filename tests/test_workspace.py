from __future__ import annotations

from pathlib import Path

import pytest

from agent_coordination import terminal, workspace

SESSION_ID = "123e4567-e89b-12d3-a456-426614174000"


def test_register_is_idempotent_for_the_same_stopped_mapping(tmp_path: Path) -> None:
    config_path = tmp_path / "aco" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()

    handoff = workspace.registration("alpha", project_path, SESSION_ID, "old head")
    first = workspace.register_project(handoff, config_path)
    second = workspace.register_project(handoff, config_path)

    assert first is True
    assert second is False
    assert workspace.load_config(config_path).projects["alpha"].directory == project_path.resolve()


def test_register_refuses_an_identity_replacement(tmp_path: Path) -> None:
    config_path = tmp_path / "aco" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.registration("alpha", project_path, SESSION_ID, "old head"), config_path
    )

    with pytest.raises(workspace.WorkspaceError, match="already registered"):
        workspace.register_project(
            workspace.registration(
                "alpha", project_path, "123e4567-e89b-12d3-a456-426614174001", "old head"
            ),
            config_path,
        )


def test_invalid_configuration_refuses_every_project_before_runtime_work(tmp_path: Path) -> None:
    config_path = tmp_path / "workspace.toml"
    config_path.write_text(
        'version = 1\n[projects.alpha]\npath = "missing"\nsession_id = "'
        + SESSION_ID
        + '"\nagent = "head"\n'
    )

    with pytest.raises(workspace.WorkspaceError, match="directory"):
        workspace.load_config(config_path)


class FakeTerminal:
    def __init__(self, target: terminal.Target, failed_project: str | None = None):
        self.target = target
        self.failed_project = failed_project
        self.created: list[tuple[str, str, Path, terminal.Launch]] = []
        self.retried: list[tuple[str, terminal.Launch]] = []
        self.opened: list[str] = []

    def inspect(self, project: str) -> terminal.Target:
        if project == self.failed_project:
            raise terminal.TerminalError("tmux is unavailable")
        return self.target

    def create(
        self,
        project: str,
        session_id: str,
        directory: Path,
        launch: terminal.Launch,
    ) -> None:
        self.created.append((project, session_id, directory, launch))
        self.target = terminal.Target(terminal.TargetState.DETACHED, project, session_id)

    def retry(self, project: str, launch: terminal.Launch) -> None:
        self.retried.append((project, launch))
        self.target = terminal.Target(terminal.TargetState.DETACHED, project, SESSION_ID)

    def open_viewer(self, project: str) -> None:
        self.opened.append(project)
        self.target = terminal.Target(
            terminal.TargetState.ATTACHED, project, self.target.session_id
        )

    def clear_viewer_pending(self, project: str) -> None:
        pass


def test_run_starts_the_exact_registered_session_with_its_logical_identity(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.registration("alpha", project_path, SESSION_ID, "restored head"), config_path
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
    assert fake.opened == ["alpha"]


def test_run_does_not_create_a_second_viewer_while_an_attachment_is_pending(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.registration("alpha", project_path, SESSION_ID, "restored head"), config_path
    )
    fake = FakeTerminal(
        terminal.Target(terminal.TargetState.DETACHED, "alpha", SESSION_ID, viewer_pending=True)
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
        workspace.registration("alpha", project_path, SESSION_ID, "restored head"), config_path
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
    assert fake.opened == ["alpha"]


def test_run_reports_foreign_tmux_metadata_without_adopting_it(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    workspace.register_project(
        workspace.registration("alpha", project_path, SESSION_ID, "restored head"), config_path
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
        workspace.registration("alpha", project_path, SESSION_ID, "restored head"), config_path
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


def test_runtime_failure_for_one_project_does_not_hide_the_later_project(tmp_path: Path) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    workspace.register_project(
        workspace.registration("alpha", alpha, SESSION_ID, "alpha head"), config_path
    )
    workspace.register_project(
        workspace.registration("beta", beta, "123e4567-e89b-12d3-a456-426614174001", "beta head"),
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
