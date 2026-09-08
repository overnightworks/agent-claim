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


@pytest.mark.parametrize(
    ("replacement", "message"),
    [
        ("same-directory", "directory"),
        ("same-session", "Codex session"),
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
        workspace.registration("alpha", alpha, SESSION_ID, "alpha head"), config_path
    )
    directory = alpha if replacement == "same-directory" else beta
    session_id = (
        SESSION_ID if replacement == "same-session" else "123e4567-e89b-12d3-a456-426614174001"
    )

    with pytest.raises(workspace.WorkspaceError, match=message):
        workspace.register_project(
            workspace.registration("beta", directory, session_id, "beta head"), config_path
        )


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

    with pytest.raises(workspace.WorkspaceError, match="line breaks or NUL"):
        workspace.register_project(
            workspace.registration("alpha", project_path, SESSION_ID, agent, model), config_path
        )

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
        ("version = 2\nprojects = {}\n", "version = 1"),
        ("version = 1\nprojects = []\n", "projects must be a mapping"),
        (
            'version = 1\n[projects.alpha]\npath = 3\nsession_id = "x"\nagent = "head"\n',
            "path must be a string",
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
        ("beta", SESSION_ID, "duplicate native Codex UUIDs"),
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

    with pytest.raises(workspace.WorkspaceError, match=message):
        workspace.register_project(
            workspace.registration(key, directory, session_id, agent, model),
            tmp_path / "config" / "workspace.toml",
        )


def test_register_leaves_no_configuration_behind_when_atomic_replace_fails(
    monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()

    def fail_replace(*_arguments: object) -> None:
        raise OSError("disk failure")

    monkeypatch.setattr(workspace.os, "replace", fail_replace)

    with pytest.raises(workspace.WorkspaceError, match="cannot write"):
        workspace.register_project(
            workspace.registration("alpha", project_path, SESSION_ID, "restored head"), config_path
        )

    assert [
        path for path in config_path.parent.glob("workspace.*") if path.name != "workspace.lock"
    ] == []


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
        workspace.registration("alpha", project_path, SESSION_ID, "restored head"), config_path
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
        workspace.registration("alpha", project_path, SESSION_ID, "restored head", "gpt-5.3-codex"),
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
        attach_after_launch: bool = False,
    ):
        self.target = target
        self.failed_project = failed_project
        self.attach_viewer = attach_viewer
        self.pending_after_create = pending_after_create
        self.attach_after_launch = attach_after_launch
        self.created: list[tuple[str, str, Path, terminal.Launch]] = []
        self.retried: list[tuple[str, terminal.Launch]] = []
        self.opened: list[str] = []
        self.cleared: list[str] = []

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
        self.target = terminal.Target(
            terminal.TargetState.ATTACHED
            if self.attach_after_launch
            else terminal.TargetState.DETACHED,
            project,
            session_id,
            self.pending_after_create,
        )

    def retry(self, project: str, launch: terminal.Launch) -> None:
        self.retried.append((project, launch))
        self.target = terminal.Target(
            terminal.TargetState.ATTACHED
            if self.attach_after_launch
            else terminal.TargetState.DETACHED,
            project,
            SESSION_ID,
        )

    def open_viewer(self, project: str) -> None:
        self.opened.append(project)
        if self.attach_viewer:
            self.target = terminal.Target(
                terminal.TargetState.ATTACHED, project, self.target.session_id
            )

    def clear_viewer_pending(self, project: str) -> None:
        self.cleared.append(project)


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


@pytest.mark.parametrize(
    ("target", "expected_state"),
    [
        (
            terminal.Target(terminal.TargetState.ATTACHED, "alpha", SESSION_ID),
            workspace.RunState.REUSED,
        ),
        (
            terminal.Target(
                terminal.TargetState.DETACHED, "alpha", SESSION_ID, viewer_pending=True
            ),
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
        workspace.registration("alpha", project_path, SESSION_ID, "restored head"), config_path
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
    assert fake.cleared == (["alpha"] if expected_state is workspace.RunState.REUSED else [])


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
        workspace.registration("alpha", project_path, SESSION_ID, "restored head"), config_path
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
        workspace.registration("alpha", project_path, SESSION_ID, "restored head"), config_path
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
    assert fake.cleared == ["alpha"]


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
