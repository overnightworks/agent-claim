"""Strict local workspace registration and one-head provider recovery lifecycle."""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
import tomllib
import uuid
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from . import protocol, providers, terminal

_CONFIG_VERSION = 2
_LEGACY_CONFIG_VERSION = 1
_PROJECT_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_PROJECT_FIELDS = frozenset({"path", "session_id", "agent", "model"})
_PROVIDER_PROJECT_FIELDS = _PROJECT_FIELDS | {"provider"}
_LOGIN_OWNER = "agent-coordination/login-v1"
_LOGIN_DESKTOP_NAME = "aco-workspace.desktop"
_LOGIN_ATTEMPT_NAME = "login-attempt.json"
_LOGIN_ENTRY_KEYS = frozenset({"Type", "Name", "Exec", "X-Aco-Owner"})
_DESKTOP_RESERVED_CHARACTERS = frozenset(" \t\n\"'\\><~|&;$*?#()`")
_ASCII_CONTROL_LIMIT = 32
_ASCII_DELETE = 127
_UUID_VERSION = 4
_LOGIN_EXEC_ARGUMENTS = ("-I", "-m", "agent_coordination.cli", "_run-at-login")
_LOGIN_WORKSPACE_FAILURE = "workspace failure"
_LOGIN_MALFORMED_RECORD = "login attempt record is malformed"
_LOGIN_MALFORMED_LAUNCHER = "login launcher is malformed"
_RUNTIME_SOCKET_NAME = "tmux.sock"
_RUNTIME_LOCK_NAME = "workspace.lock"
_LOCK_SUFFIX = ".lock"


class WorkspaceError(protocol.ClaimError):
    pass


class RunState(StrEnum):
    STARTED = "started"
    REUSED = "reused"
    REATTACHED = "reattached"
    PENDING = "viewer pending"
    RETRIED = "retried"
    FAILED = "failed"
    ENROLLMENT_PENDING = "enrollment pending"


class LoginLauncherState(StrEnum):
    DISABLED = "disabled"
    ENABLED = "enabled"
    STALE = "stale"
    CONFLICT = "conflict"


class LoginConfigurationState(StrEnum):
    VALID = "valid"
    MISSING = "missing"
    MALFORMED = "malformed"
    EMPTY = "empty"


@dataclass(frozen=True)
class WorkspaceProject:
    key: str
    directory: Path
    session_id: str
    agent: str
    model: str | None = None
    provider: providers.Provider = providers.Provider.CODEX


@dataclass(frozen=True)
class WorkspaceRegistration:
    key: str
    directory: Path
    session_id: str
    agent: str
    model: str | None = None
    provider: providers.Provider = providers.Provider.CODEX


@dataclass(frozen=True)
class WorkspaceConfig:
    projects: dict[str, WorkspaceProject]


@dataclass(frozen=True)
class RunOutcome:
    project: str
    state: RunState
    detail: str = ""


@dataclass(frozen=True)
class LoginAttempt:
    attempt_id: str
    started_at: str
    state: str
    outcomes: tuple[tuple[str, RunState], ...] = ()
    failure: str | None = None
    completed_at: str | None = None


@dataclass(frozen=True)
class LoginRunResult:
    attempt: LoginAttempt
    exit_status: int


@dataclass(frozen=True)
class StartRequest:
    key: str
    directory: Path
    agent: str
    model: str | None = None


@dataclass(frozen=True)
class StartContext:
    config_path: Path
    callback: str
    environment: Mapping[str, str] | None = None
    runtime_directory: Path | None = None
    terminal_factory: Callable[[Path], terminal.TerminalController] = terminal.TmuxTerminal


def default_config_path(environment: Mapping[str, str], home: Path | None = None) -> Path:
    configured = environment.get("XDG_CONFIG_HOME")
    return (
        (Path(configured) if configured else (home or Path.home()) / ".config")
        / "aco"
        / "workspace.toml"
    )


def login_desktop_path(environment: Mapping[str, str], home: Path | None = None) -> Path:
    configured = environment.get("XDG_CONFIG_HOME")
    root = Path(configured) if configured else (home or _environment_home(environment)) / ".config"
    return root / "autostart" / _LOGIN_DESKTOP_NAME


def login_attempt_path(environment: Mapping[str, str], home: Path | None = None) -> Path:
    configured = environment.get("XDG_STATE_HOME")
    root = (
        Path(configured)
        if configured
        else (home or _environment_home(environment)) / ".local" / "state"
    )
    return root / "aco" / _LOGIN_ATTEMPT_NAME


def enable_login(config_path: Path, environment: Mapping[str, str], executable: Path) -> bool:
    """Install the one owned desktop entry after strict whole-workspace validation."""
    config = load_config(config_path)
    if not config.projects:
        raise WorkspaceError("workspace configuration has no registered projects")
    executable = _login_executable(executable)
    desktop_path = login_desktop_path(environment)
    _ensure_private_directory(desktop_path.parent)
    existing = _owned_launcher_executable(desktop_path)
    if desktop_path.exists() or desktop_path.is_symlink():
        if existing is None:
            raise WorkspaceError("login launcher conflicts with an unowned filesystem object")
        if existing == executable:
            return False
    _atomic_write(desktop_path, _desktop_entry(executable).encode())
    return True


def disable_login(environment: Mapping[str, str]) -> bool:
    """Remove only the exact entry generated by this installed program."""
    desktop_path = login_desktop_path(environment)
    if not desktop_path.exists() and not desktop_path.is_symlink():
        return False
    if _owned_launcher_executable(desktop_path) is None:
        raise WorkspaceError("login launcher conflicts with an unowned filesystem object")
    try:
        desktop_path.unlink()
    except OSError as error:
        raise WorkspaceError("cannot remove login launcher") from error
    return True


def login_launcher_state(environment: Mapping[str, str], executable: Path) -> LoginLauncherState:
    desktop_path = login_desktop_path(environment)
    if not desktop_path.exists() and not desktop_path.is_symlink():
        return LoginLauncherState.DISABLED
    owned_executable = _owned_launcher_executable(desktop_path)
    if owned_executable is None:
        return LoginLauncherState.CONFLICT
    try:
        current_executable = _login_executable(executable)
    except WorkspaceError:
        return LoginLauncherState.STALE
    return (
        LoginLauncherState.ENABLED
        if owned_executable == current_executable
        else LoginLauncherState.STALE
    )


def login_configuration_state(config_path: Path) -> LoginConfigurationState:
    try:
        config = load_config(config_path)
    except WorkspaceError:
        if not config_path.exists():
            return LoginConfigurationState.MISSING
        return LoginConfigurationState.MALFORMED
    return LoginConfigurationState.VALID if config.projects else LoginConfigurationState.EMPTY


def run_login_recovery(
    config_path: Path,
    state_path: Path,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    new_attempt_id: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> LoginRunResult:
    """Serialize one desired-workspace run and preserve only bounded public state."""
    _ensure_private_directory(state_path.parent)
    started = _login_time(now())
    attempt = LoginAttempt(new_attempt_id(), started, "running")
    with _locked(state_path.with_suffix(_LOCK_SUFFIX)):
        _write_login_attempt(state_path, attempt)
        try:
            outcomes = run_projects(config_path)
        except (WorkspaceError, terminal.TerminalError):
            completed = LoginAttempt(
                attempt.attempt_id,
                attempt.started_at,
                "completed",
                failure=_LOGIN_WORKSPACE_FAILURE,
                completed_at=_login_time(now()),
            )
            _write_login_attempt(state_path, completed)
            return LoginRunResult(completed, 1)
        if not outcomes:
            completed = LoginAttempt(
                attempt.attempt_id,
                attempt.started_at,
                "completed",
                failure=_LOGIN_WORKSPACE_FAILURE,
                completed_at=_login_time(now()),
            )
            _write_login_attempt(state_path, completed)
            return LoginRunResult(completed, 1)
        completed = LoginAttempt(
            attempt.attempt_id,
            attempt.started_at,
            "completed",
            tuple((outcome.project, outcome.state) for outcome in outcomes),
            completed_at=_login_time(now()),
        )
        _write_login_attempt(state_path, completed)
    return LoginRunResult(
        completed, 1 if any(state is RunState.FAILED for _, state in completed.outcomes) else 0
    )


def load_login_attempt(state_path: Path) -> LoginAttempt:
    try:
        raw = json.loads(state_path.read_text())
    except FileNotFoundError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD) from error
    return _login_attempt_from_record(raw)


def _login_attempt_from_record(raw: object) -> LoginAttempt:
    if not isinstance(raw, dict):
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    required = {"version", "attempt_id", "started_at", "state", "outcomes"}
    allowed = required | {"failure", "completed_at"}
    if set(raw) - allowed or not required <= set(raw) or raw.get("version") != 1:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    attempt_id = raw["attempt_id"]
    started_at = raw["started_at"]
    state = raw["state"]
    _login_attempt_identifier(attempt_id)
    started = _login_timestamp(started_at)
    if state not in {"running", "completed"}:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    parsed_outcomes = _login_outcomes(raw["outcomes"])
    failure, completed_at = _login_completion(raw, state, parsed_outcomes, started)
    return LoginAttempt(attempt_id, started_at, state, parsed_outcomes, failure, completed_at)


def _login_outcomes(raw_outcomes: object) -> tuple[tuple[str, RunState], ...]:
    if not isinstance(raw_outcomes, list):
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    parsed_outcomes: list[tuple[str, RunState]] = []
    for outcome in raw_outcomes:
        if not isinstance(outcome, dict) or set(outcome) != {"project", "outcome"}:
            raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
        project, outcome_state = outcome["project"], outcome["outcome"]
        if not isinstance(project, str) or not _PROJECT_KEY_PATTERN.fullmatch(project):
            raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
        try:
            parsed_outcomes.append((project, RunState(outcome_state)))
        except (TypeError, ValueError) as error:
            raise WorkspaceError(_LOGIN_MALFORMED_RECORD) from error
    return tuple(parsed_outcomes)


def _login_completion(
    raw: Mapping[str, object],
    state: object,
    outcomes: tuple[tuple[str, RunState], ...],
    started: datetime,
) -> tuple[str | None, str | None]:
    failure = _login_failure(raw.get("failure"))
    completed_at = _login_completed_at(raw.get("completed_at"))
    completed = None if completed_at is None else _login_timestamp(completed_at)
    if state == "running" and (outcomes or failure is not None or completed_at is not None):
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    if state == "completed" and completed_at is None:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    if completed is not None and completed < started:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    if failure is not None and outcomes:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    if state == "completed" and failure is None and not outcomes:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    return failure, completed_at


def _login_failure(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value != _LOGIN_WORKSPACE_FAILURE:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    return value


def _login_completed_at(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    _login_timestamp(value)
    return value


def _login_attempt_identifier(value: object) -> None:
    if not isinstance(value, str):
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    try:
        identifier = uuid.UUID(value)
    except ValueError as error:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD) from error
    if str(identifier) != value or identifier.version != _UUID_VERSION:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)


def _login_timestamp(value: object) -> datetime:
    if not isinstance(value, str) or "T" not in value:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD) from error
    if parsed.tzinfo is None:
        raise WorkspaceError(_LOGIN_MALFORMED_RECORD)
    return parsed


def register_project(handoff: WorkspaceRegistration, config_path: Path) -> bool:
    """Store one explicit stopped-session handoff; return whether it was new."""
    candidate = _project(handoff)
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _locked(config_path.with_suffix(_LOCK_SUFFIX)):
        projects = {} if not config_path.exists() else dict(load_config(config_path).projects)
        existing = projects.get(candidate.key)
        if existing is not None:
            if existing == candidate:
                return False
            raise WorkspaceError(
                f"project {candidate.key!r} is already registered; "
                "native session and claim identity "
                "can only change through an explicit future handoff"
            )
        if any(project.directory == candidate.directory for project in projects.values()):
            raise WorkspaceError(f"directory {candidate.directory} is already registered")
        if any(
            (project.provider, project.session_id) == (candidate.provider, candidate.session_id)
            for project in projects.values()
        ):
            raise WorkspaceError(
                f"{candidate.provider.value} session {candidate.session_id} is already registered"
            )
        projects[candidate.key] = candidate
        _write_config(config_path, projects)
    return True


def load_config(config_path: Path) -> WorkspaceConfig:
    """Read the entire local mapping or reject it before any project starts."""
    try:
        contents = config_path.read_bytes()
    except FileNotFoundError as error:
        raise WorkspaceError(f"workspace configuration does not exist: {config_path}") from error
    except OSError as error:
        raise WorkspaceError(
            f"cannot read workspace configuration {config_path}: {error}"
        ) from error
    try:
        raw = tomllib.loads(contents.decode())
    except (UnicodeDecodeError, tomllib.TOMLDecodeError) as error:
        raise WorkspaceError(f"invalid workspace configuration {config_path}: {error}") from error
    version = raw.get("version")
    supported_versions = {_LEGACY_CONFIG_VERSION, _CONFIG_VERSION}
    if (
        set(raw) != {"version", "projects"}
        or type(version) is not int
        or version not in supported_versions
    ):
        raise WorkspaceError(
            "workspace configuration must contain only version = 1 or version = 2 and projects"
        )
    raw_projects = raw["projects"]
    if not isinstance(raw_projects, dict):
        raise WorkspaceError("workspace configuration projects must be a mapping")
    projects: dict[str, WorkspaceProject] = {}
    for key, record in raw_projects.items():
        project = _project_from_config_record(key, record, version)
        projects[project.key] = project
    _validate_unique_projects(projects)
    return WorkspaceConfig(projects)


def run_projects(
    config_path: Path,
    project_key: str | None = None,
    *,
    environment: Mapping[str, str] | None = None,
    runtime_directory: Path | None = None,
    terminal_factory: Callable[[Path], terminal.TerminalController] = terminal.TmuxTerminal,
) -> tuple[RunOutcome, ...]:
    """Run selected projects in configuration order after whole-config validation."""
    config = load_config(config_path)
    if project_key is not None and project_key not in config.projects:
        raise WorkspaceError(f"project {project_key!r} is not registered")
    selected = (
        (config.projects[project_key],)
        if project_key is not None
        else tuple(config.projects.values())
    )
    runtime = _runtime_directory(runtime_directory, environment or os.environ)
    controller = terminal_factory(runtime / _RUNTIME_SOCKET_NAME)
    child_environment = environment or os.environ
    outcomes: list[RunOutcome] = []
    with _locked(runtime / _RUNTIME_LOCK_NAME):
        targets = _target_index(controller.inspect_all())
        for project in selected:
            try:
                outcomes.append(_run_project(controller, project, child_environment, targets))
            except terminal.TerminalError as error:
                outcomes.append(RunOutcome(project.key, RunState.FAILED, str(error)))
    return tuple(outcomes)


def start_project(
    request: StartRequest,
    context: StartContext,
) -> RunOutcome:
    """Open one promptless Codex console and wait for its startup callback."""
    key, directory, agent, model = _start_definition(request)
    source_environment = context.environment if context.environment is not None else os.environ
    runtime = _runtime_directory(context.runtime_directory, source_environment)
    controller = context.terminal_factory(runtime / _RUNTIME_SOCKET_NAME)
    with _locked(runtime / _RUNTIME_LOCK_NAME):
        projects = (
            {} if not context.config_path.exists() else load_config(context.config_path).projects
        )
        targets = _target_index(controller.inspect_all())
        existing = projects.get(key)
        if existing is not None:
            if (existing.directory, existing.agent, existing.model) != (directory, agent, model):
                raise WorkspaceError(
                    f"project {key!r} is already registered with a different definition"
                )
            return _start_existing_project(controller, existing, targets, source_environment)
        if any(project.directory == directory for project in projects.values()):
            raise WorkspaceError(f"directory {directory} is already registered")
        if any(target.enrollment is None for target in targets.values()):
            raise WorkspaceError("tmux target has unknown ownership or path")
        _refuse_pending_directory(targets, directory, key)
        return _start_unregistered_project(
            controller,
            StartRequest(key, directory, agent, model),
            context,
            runtime,
            targets,
        )


def _start_unregistered_project(
    controller: terminal.TerminalController,
    request: StartRequest,
    context: StartContext,
    runtime: Path,
    targets: Mapping[str, terminal.Target],
) -> RunOutcome:
    environment = context.environment if context.environment is not None else os.environ
    target = controller.inspect(request.key)
    if target.state is terminal.TargetState.ABSENT:
        launch = _fresh_launch(
            request,
            str(uuid.uuid4()),
            context,
            runtime,
            environment,
        )
        controller.create(request.key, None, request.directory, launch)
        return _attach_or_pending(
            controller,
            WorkspaceProject(request.key, request.directory, "", request.agent, request.model),
            RunState.ENROLLMENT_PENDING,
        )
    _require_matching_enrollment(
        target, request.key, request.directory, request.agent, request.model
    )
    enrollment = target.enrollment
    assert enrollment is not None
    if enrollment.state is terminal.EnrollmentState.INITIALIZING:
        raise WorkspaceError("fresh Codex target setup is incomplete")
    if enrollment.state is terminal.EnrollmentState.FINAL:
        raise WorkspaceError("fresh Codex target is final without a workspace mapping")
    if enrollment.session_id is not None:
        candidate = WorkspaceProject(
            request.key, request.directory, enrollment.session_id, request.agent, request.model
        )
        _commit_capture(config_path=context.config_path, candidate=candidate)
        controller.finalize_enrollment(request.key)
        return _run_project(controller, candidate, environment, targets)
    pending = WorkspaceProject(request.key, request.directory, "", request.agent, request.model)
    if target.state is terminal.TargetState.EXITED:
        launch = _fresh_launch(
            request,
            str(uuid.uuid4()),
            context,
            runtime,
            environment,
        )
        controller.retry_enrollment(request.key, launch)
    return _attach_or_pending(controller, pending, RunState.ENROLLMENT_PENDING)


def capture_codex_start(payload: Mapping[str, object], environment: Mapping[str, str]) -> None:
    """Commit the UUID delivered by the one validated native startup hook."""
    required = (
        "ACO_CAPTURE_PROJECT",
        "ACO_CAPTURE_DIRECTORY",
        "ACO_CAPTURE_CONFIG",
        "ACO_CAPTURE_RUNTIME",
        "ACO_CAPTURE_AGENT",
        "ACO_CAPTURE_ATTEMPT",
    )
    if any(not environment.get(name) for name in required):
        raise WorkspaceError("fresh Codex capture identity is incomplete")
    if payload.get("hook_event_name") != "SessionStart" or payload.get("source") != "startup":
        raise WorkspaceError("fresh Codex capture requires a startup SessionStart event")
    session_id = _string(payload.get("session_id"), "native session_id")
    cwd = _string(payload.get("cwd"), "native cwd")
    request = StartRequest(
        environment["ACO_CAPTURE_PROJECT"],
        _canonical_config_directory(environment["ACO_CAPTURE_DIRECTORY"]),
        environment["ACO_CAPTURE_AGENT"],
        environment.get("ACO_CAPTURE_MODEL"),
    )
    key, directory, agent, model = _start_definition(request)
    if cwd != str(directory):
        raise WorkspaceError("native startup cwd does not match the pending project")
    _exact_uuid(session_id, "native session_id")
    _exact_uuid(environment["ACO_CAPTURE_ATTEMPT"], "fresh Codex attempt")
    config_path = Path(environment["ACO_CAPTURE_CONFIG"])
    runtime = _capture_runtime_directory(environment["ACO_CAPTURE_RUNTIME"])
    controller = terminal.TmuxTerminal(runtime / _RUNTIME_SOCKET_NAME)
    with _locked(runtime / _RUNTIME_LOCK_NAME):
        target = controller.inspect(key)
        _require_matching_enrollment(target, key, directory, agent, model)
        enrollment = target.enrollment
        assert enrollment is not None
        candidate = WorkspaceProject(key, directory, session_id, agent, model)
        if enrollment.attempt != environment["ACO_CAPTURE_ATTEMPT"]:
            raise WorkspaceError("fresh Codex capture does not match the pending attempt")
        if enrollment.state is terminal.EnrollmentState.FINAL:
            if enrollment.session_id != session_id or target.session_id != session_id:
                raise WorkspaceError("fresh Codex capture does not match the final attempt")
            _require_exact_capture(config_path, candidate)
            return
        if enrollment.state is not terminal.EnrollmentState.PENDING:
            raise WorkspaceError("fresh Codex capture found incomplete target setup")
        if enrollment.session_id is None:
            controller.stage_enrollment(key, session_id)
        elif enrollment.session_id != session_id:
            raise WorkspaceError("fresh Codex capture has a different staged UUID")
        _commit_capture(config_path=config_path, candidate=candidate)
        controller.finalize_enrollment(key)


def _fresh_launch(
    request: StartRequest,
    attempt: str,
    context: StartContext,
    runtime: Path,
    source_environment: Mapping[str, str],
) -> terminal.Launch:
    enrollment = terminal.Enrollment(
        request.directory,
        request.agent,
        request.model,
        attempt,
        terminal.EnrollmentState.INITIALIZING,
    )
    launch_environment = providers.project_environment(source_environment, request.agent)
    launch_environment.update(
        {
            "ACO_CAPTURE_PROJECT": request.key,
            "ACO_CAPTURE_DIRECTORY": str(request.directory),
            "ACO_CAPTURE_CONFIG": str(context.config_path.resolve()),
            "ACO_CAPTURE_RUNTIME": str(runtime),
            "ACO_CAPTURE_AGENT": request.agent,
            "ACO_CAPTURE_ATTEMPT": attempt,
        }
    )
    if request.model is not None:
        launch_environment["ACO_CAPTURE_MODEL"] = request.model
    return terminal.Launch(
        providers.fresh_codex_command(request.directory, request.model, context.callback),
        launch_environment,
        providers.session_identity_environment_names()
        | providers.start_capture_environment_names(),
        providers.Provider.CODEX,
        enrollment,
    )


def _start_existing_project(
    controller: terminal.TerminalController,
    project: WorkspaceProject,
    targets: Mapping[str, terminal.Target],
    environment: Mapping[str, str],
) -> RunOutcome:
    target = controller.inspect(project.key)
    if target.state is terminal.TargetState.ABSENT or target.enrollment is None:
        return _run_project(controller, project, environment, targets)
    _require_matching_enrollment(
        target, project.key, project.directory, project.agent, project.model
    )
    enrollment = target.enrollment
    if enrollment.state is terminal.EnrollmentState.FINAL:
        return _run_project(controller, project, environment, targets)
    if enrollment.state is not terminal.EnrollmentState.PENDING:
        raise WorkspaceError("fresh Codex target setup is incomplete")
    if enrollment.session_id != project.session_id:
        raise WorkspaceError("registered Codex UUID does not match the pending target")
    controller.finalize_enrollment(project.key)
    return _run_project(controller, project, environment, targets)


def _commit_capture(*, config_path: Path, candidate: WorkspaceProject) -> None:
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _locked(config_path.with_suffix(_LOCK_SUFFIX)):
        projects = {} if not config_path.exists() else dict(load_config(config_path).projects)
        existing = projects.get(candidate.key)
        if existing is not None:
            if existing != candidate:
                raise WorkspaceError(
                    f"project {candidate.key!r} conflicts with the pending Codex capture"
                )
            return
        if any(project.directory == candidate.directory for project in projects.values()):
            raise WorkspaceError(f"directory {candidate.directory} is already registered")
        if any(
            (project.provider, project.session_id) == (candidate.provider, candidate.session_id)
            for project in projects.values()
        ):
            raise WorkspaceError(f"codex session {candidate.session_id} is already registered")
        projects[candidate.key] = candidate
        _write_config(config_path, projects)


def _require_exact_capture(config_path: Path, candidate: WorkspaceProject) -> None:
    if (
        not config_path.exists()
        or load_config(config_path).projects.get(candidate.key) != candidate
    ):
        raise WorkspaceError("fresh Codex capture does not have an exact workspace mapping")


def _target_index(targets: tuple[terminal.Target, ...]) -> dict[str, terminal.Target]:
    indexed: dict[str, terminal.Target] = {}
    for target in targets:
        if target.state is terminal.TargetState.ABSENT or target.project is None:
            raise terminal.TerminalError("tmux target has incomplete project metadata")
        if target.enrollment is not None:
            enrollment = target.enrollment
            _start_definition(
                StartRequest(
                    target.project, enrollment.directory, enrollment.agent, enrollment.model
                )
            )
            _exact_uuid(enrollment.attempt, "fresh Codex attempt")
            if enrollment.session_id is not None:
                _exact_uuid(enrollment.session_id, "staged native session_id")
        indexed[target.project] = target
    return indexed


def _require_matching_enrollment(
    target: terminal.Target, key: str, directory: Path, agent: str, model: str | None
) -> None:
    enrollment = target.enrollment
    if (
        target.project != key
        or target.provider is not providers.Provider.CODEX
        or enrollment is None
        or (enrollment.directory, enrollment.agent, enrollment.model) != (directory, agent, model)
    ):
        raise WorkspaceError(f"tmux target {terminal.target_name(key)!r} has foreign metadata")


def _refuse_pending_directory(
    targets: Mapping[str, terminal.Target], directory: Path, project_key: str
) -> None:
    for key, target in targets.items():
        enrollment = target.enrollment
        if (
            key != project_key
            and enrollment is not None
            and enrollment.state is not terminal.EnrollmentState.FINAL
            and enrollment.directory == directory
        ):
            raise WorkspaceError(f"directory {directory} has a pending Codex start")


def _exact_uuid(value: str, name: str) -> None:
    try:
        parsed_uuid = uuid.UUID(value)
    except ValueError as error:
        raise WorkspaceError(f"{name} must be an exact UUID") from error
    if str(parsed_uuid) != value:
        raise WorkspaceError(f"{name} must be an exact UUID")


def _start_definition(request: StartRequest) -> tuple[str, Path, str, str | None]:
    """Validate the fresh definition before it owns a pending console."""
    placeholder = "00000000-0000-0000-0000-000000000000"
    project = _project(
        WorkspaceRegistration(
            request.key, request.directory, placeholder, request.agent, request.model
        )
    )
    return project.key, project.directory, project.agent, project.model


def _run_project(
    controller: terminal.TerminalController,
    project: WorkspaceProject,
    environment: Mapping[str, str],
    targets: Mapping[str, terminal.Target],
) -> RunOutcome:
    try:
        _refuse_pending_directory(targets, project.directory, project.key)
    except WorkspaceError as error:
        raise terminal.TerminalError(str(error)) from error
    target = controller.inspect(project.key)
    if (
        target.enrollment is not None
        and target.enrollment.state is not terminal.EnrollmentState.FINAL
    ):
        raise terminal.TerminalError(
            f"tmux target {terminal.target_name(project.key)!r} has pending enrollment metadata"
        )
    launch = terminal.Launch(
        providers.resume_command(
            project.provider, project.session_id, project.directory, project.model
        ),
        providers.project_environment(environment, project.agent),
        providers.session_identity_environment_names(),
        project.provider,
    )
    if target.state is terminal.TargetState.ABSENT:
        controller.create(
            project.key,
            project.session_id,
            project.directory,
            launch,
        )
        return _attach_or_pending(controller, project, RunState.STARTED)
    if not terminal.target_matches(
        {
            "@aco_project": target.project or "",
            "@aco_provider": target.provider.value if target.provider is not None else "",
            "@aco_session_id": target.session_id or "",
        },
        project.key,
        project.provider,
        project.session_id,
    ):
        raise terminal.TerminalError(
            f"tmux target {terminal.target_name(project.key)!r} has foreign metadata"
        )
    if target.state is terminal.TargetState.EXITED:
        controller.retry(
            project.key,
            launch,
        )
        return _attach_or_pending(controller, project, RunState.RETRIED)
    if target.state is terminal.TargetState.ATTACHED:
        controller.clear_viewer_pending(project.key)
        return RunOutcome(project.key, RunState.REUSED)
    if target.viewer_pending:
        return RunOutcome(
            project.key, RunState.PENDING, "waiting for the previously launched console"
        )
    return _attach_or_pending(controller, project, RunState.REATTACHED)


def _attach_or_pending(
    controller: terminal.TerminalController, project: WorkspaceProject, state: RunState
) -> RunOutcome:
    target = controller.inspect(project.key)
    if target.state is terminal.TargetState.ATTACHED:
        controller.clear_viewer_pending(project.key)
        return RunOutcome(project.key, state)
    if target.viewer_pending:
        if state is RunState.ENROLLMENT_PENDING:
            return RunOutcome(project.key, state, "waiting for first user submission")
        return RunOutcome(
            project.key, RunState.PENDING, "waiting for the previously launched console"
        )
    controller.open_viewer(project.key)
    observed = controller.inspect(project.key)
    if observed.state is terminal.TargetState.ATTACHED:
        controller.clear_viewer_pending(project.key)
        return RunOutcome(project.key, state)
    return RunOutcome(
        project.key,
        state if state is RunState.ENROLLMENT_PENDING else RunState.PENDING,
        "console launch is pending",
    )


def _project(handoff: WorkspaceRegistration) -> WorkspaceProject:
    key = handoff.key
    directory = handoff.directory
    session_id = handoff.session_id
    agent = handoff.agent
    model = handoff.model
    provider = _provider(handoff.provider)
    if _PROJECT_KEY_PATTERN.fullmatch(key) is None:
        raise WorkspaceError("project key must use letters, numbers, underscores, or hyphens")
    path = Path(directory).expanduser()
    try:
        canonical = path.resolve(strict=True)
    except OSError as error:
        raise WorkspaceError(f"project {key!r} directory does not exist: {path}") from error
    if not canonical.is_dir():
        raise WorkspaceError(f"project {key!r} directory is not a directory: {canonical}")
    try:
        parsed_uuid = uuid.UUID(session_id)
    except (AttributeError, ValueError) as error:
        raise WorkspaceError(f"project {key!r} session_id must be an exact UUID") from error
    if str(parsed_uuid) != session_id:
        raise WorkspaceError(f"project {key!r} session_id must be an exact UUID")
    if not agent or not agent.strip():
        raise WorkspaceError(f"project {key!r} agent must not be empty")
    _validate_launch_identifier(agent, f"project {key!r} agent")
    if model is not None and (not model or not model.strip()):
        raise WorkspaceError(f"project {key!r} model must not be empty")
    if model is not None:
        _validate_launch_identifier(model, f"project {key!r} model")
    return WorkspaceProject(key, canonical, session_id, agent, model, provider)


def _project_from_config_record(key: object, record: object, version: int) -> WorkspaceProject:
    if not isinstance(key, str) or not isinstance(record, dict):
        raise WorkspaceError("workspace projects must use project keys and table records")
    fields = _PROJECT_FIELDS if version == _LEGACY_CONFIG_VERSION else _PROVIDER_PROJECT_FIELDS
    required = {"path", "session_id", "agent"}
    if version == _CONFIG_VERSION:
        required.add("provider")
    if set(record) - fields or not required <= set(record):
        raise WorkspaceError(f"project {key!r} has unsupported or missing fields")
    directory = _canonical_config_directory(_string(record["path"], f"project {key!r} path"))
    session_id = _string(record["session_id"], f"project {key!r} session_id")
    agent = _string(record["agent"], f"project {key!r} agent")
    model = _optional_string(record.get("model"), f"project {key!r} model")
    provider = (
        providers.Provider.CODEX
        if version == _LEGACY_CONFIG_VERSION
        else _provider(record["provider"], f"project {key!r} provider")
    )
    return _project(
        WorkspaceRegistration(
            key,
            directory,
            session_id,
            agent,
            model,
            provider=provider,
        )
    )


def _validate_unique_projects(projects: Mapping[str, WorkspaceProject]) -> None:
    directories = [project.directory for project in projects.values()]
    sessions = [(project.provider, project.session_id) for project in projects.values()]
    if len(directories) != len(set(directories)):
        raise WorkspaceError("workspace configuration contains duplicate canonical paths")
    if len(sessions) != len(set(sessions)):
        raise WorkspaceError("workspace configuration contains duplicate native provider UUIDs")


def _provider(value: providers.Provider | str, name: str = "provider") -> providers.Provider:
    try:
        return providers.Provider(value)
    except (TypeError, ValueError) as error:
        choices = ", ".join(provider.value for provider in providers.Provider)
        raise WorkspaceError(f"{name} must be one of {choices}") from error


def _canonical_config_directory(value: str) -> Path:
    configured = Path(value)
    if not configured.is_absolute():
        raise WorkspaceError("workspace project path must be an absolute canonical directory")
    try:
        canonical = configured.resolve(strict=True)
    except OSError as error:
        raise WorkspaceError(f"workspace project directory does not exist: {configured}") from error
    if configured != canonical:
        raise WorkspaceError("workspace project path must be a canonical directory")
    return canonical


def _string(value: object, name: str) -> str:
    if not isinstance(value, str):
        raise WorkspaceError(f"{name} must be a string")
    return value


def _optional_string(value: object, name: str) -> str | None:
    if value is None:
        return None
    return _string(value, name)


def _validate_launch_identifier(value: str, name: str) -> None:
    if "\0" in value or "\r" in value or "\n" in value:
        raise WorkspaceError(f"{name} must not contain line breaks or NUL")


def _runtime_directory(runtime_directory: Path | None, environment: Mapping[str, str]) -> Path:
    candidate = runtime_directory or Path(environment.get("XDG_RUNTIME_DIR", ""))
    if not candidate.is_absolute():
        raise WorkspaceError("XDG_RUNTIME_DIR must be an absolute user-owned directory")
    try:
        metadata = candidate.stat()
    except OSError as error:
        raise WorkspaceError(f"XDG_RUNTIME_DIR is unavailable: {candidate}") from error
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise WorkspaceError("XDG_RUNTIME_DIR must be a private directory owned by this user")
    root = candidate / "aco"
    root.mkdir(mode=0o700, exist_ok=True)
    if root.is_symlink() or stat.S_IMODE(root.stat().st_mode) & 0o077:
        raise WorkspaceError(
            "workspace runtime directory must be private and must not be a symlink"
        )
    return root


def _capture_runtime_directory(value: str) -> Path:
    runtime = Path(value)
    if not runtime.is_absolute() or runtime.name != "aco":
        raise WorkspaceError("fresh Codex runtime identity is invalid")
    _runtime_directory(runtime.parent, {})
    return runtime


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _environment_home(environment: Mapping[str, str]) -> Path:
    configured = environment.get("HOME")
    return Path(configured) if configured else Path.home()


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=0o700, parents=True, exist_ok=True)
        metadata = path.stat()
    except OSError as error:
        raise WorkspaceError("cannot create private login directory") from error
    if (
        path.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.getuid()
        or stat.S_IMODE(metadata.st_mode) & 0o077
    ):
        raise WorkspaceError("login directory must be private and owned by this user")


def _login_executable(executable: Path) -> Path:
    if not executable.is_absolute():
        raise WorkspaceError("login executable must be an absolute safe path")
    lexical = Path(os.path.abspath(executable))
    if (
        "=" in str(lexical)
        or "%" in str(lexical)
        or _unsafe_desktop_text(str(lexical))
        or re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", lexical.name, flags=re.ASCII) is None
    ):
        raise WorkspaceError("login executable must be an absolute safe path")
    return lexical


def _desktop_entry(executable: Path) -> str:
    arguments = (str(executable), *_LOGIN_EXEC_ARGUMENTS)
    return "\n".join(
        (
            "[Desktop Entry]",
            "Type=Application",
            "Name=ACO workspace recovery",
            "Exec=" + " ".join(_desktop_argument(argument) for argument in arguments),
            f"X-Aco-Owner={_LOGIN_OWNER}",
            "",
        )
    )


def _desktop_argument(argument: str) -> str:
    if not argument or any(character in _DESKTOP_RESERVED_CHARACTERS for character in argument):
        quoted = "".join(
            f"\\{character}" if character in {"\\", '"', "`", "$"} else character
            for character in argument
        )
        return '"' + quoted.replace("\\", "\\\\") + '"'
    return argument


def _unsafe_desktop_text(value: str) -> bool:
    return any(
        ord(character) < _ASCII_CONTROL_LIMIT or ord(character) == _ASCII_DELETE
        for character in value
    )


def _owned_launcher_executable(path: Path) -> Path | None:
    try:
        metadata = path.lstat()
    except OSError:
        return None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        return None
    try:
        return _launcher_executable(path.read_text())
    except (OSError, UnicodeDecodeError, WorkspaceError):
        return None


def _launcher_executable(contents: str) -> Path:
    if not contents.endswith("\n"):
        raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
    lines = contents.splitlines()
    if not lines or lines[0] != "[Desktop Entry]":
        raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
    values: dict[str, str] = {}
    for line in lines[1:]:
        if "=" not in line:
            raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
        key, value = line.split("=", maxsplit=1)
        if key not in _LOGIN_ENTRY_KEYS or key in values:
            raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
        values[key] = value
    if set(values) != _LOGIN_ENTRY_KEYS or values.get("Type") != "Application":
        raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
    if values.get("Name") != "ACO workspace recovery" or values.get("X-Aco-Owner") != _LOGIN_OWNER:
        raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
    arguments = _desktop_exec_arguments(values["Exec"])
    if len(arguments) != len(_LOGIN_EXEC_ARGUMENTS) + 1 or arguments[1:] != _LOGIN_EXEC_ARGUMENTS:
        raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
    return _login_executable(Path(arguments[0]))


def _desktop_exec_arguments(value: str) -> tuple[str, ...]:
    unescaped = _desktop_string_unescape(value)
    arguments: list[str] = []
    index = 0
    while index < len(unescaped):
        if unescaped[index] == " ":
            raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
        argument, index = (
            _quoted_desktop_argument(unescaped, index + 1)
            if unescaped[index] == '"'
            else _unquoted_desktop_argument(unescaped, index)
        )
        arguments.append(argument)
        if index < len(unescaped):
            index += 1
    return tuple(arguments)


def _quoted_desktop_argument(value: str, index: int) -> tuple[str, int]:
    characters: list[str] = []
    while index < len(value) and value[index] != '"':
        character = value[index]
        if character == "\\":
            index += 1
            if index == len(value) or value[index] not in {"\\", '"', "`", "$"}:
                raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
            character = value[index]
        characters.append(character)
        index += 1
    if index == len(value):
        raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
    index += 1
    if index < len(value) and value[index] != " ":
        raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
    return "".join(characters), index


def _unquoted_desktop_argument(value: str, index: int) -> tuple[str, int]:
    start = index
    while index < len(value) and value[index] != " ":
        if value[index] in _DESKTOP_RESERVED_CHARACTERS:
            raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
        index += 1
    return value[start:index], index


def _desktop_string_unescape(value: str) -> str:
    characters: list[str] = []
    index = 0
    while index < len(value):
        character = value[index]
        if character != "\\":
            characters.append(character)
            index += 1
            continue
        index += 1
        if index == len(value) or value[index] != "\\":
            raise WorkspaceError(_LOGIN_MALFORMED_LAUNCHER)
        characters.append("\\")
        index += 1
    return "".join(characters)


def _write_login_attempt(state_path: Path, attempt: LoginAttempt) -> None:
    payload: dict[str, object] = {
        "version": 1,
        "attempt_id": attempt.attempt_id,
        "started_at": attempt.started_at,
        "state": attempt.state,
        "outcomes": [
            {"project": project, "outcome": outcome.value} for project, outcome in attempt.outcomes
        ],
    }
    if attempt.failure is not None:
        payload["failure"] = attempt.failure
    if attempt.completed_at is not None:
        payload["completed_at"] = attempt.completed_at
    _atomic_write(state_path, (json.dumps(payload, sort_keys=True) + "\n").encode())


def _atomic_write(path: Path, contents: bytes) -> None:
    temporary_name: str | None = None
    try:
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
        with os.fdopen(descriptor, "wb") as temporary:
            temporary.write(contents)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, path)
    except OSError as error:
        raise WorkspaceError("cannot write login recovery state") from error
    finally:
        if temporary_name is not None and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def _login_time(value: datetime) -> str:
    return value.astimezone(UTC).isoformat()


def _write_config(config_path: Path, projects: Mapping[str, WorkspaceProject]) -> None:
    records = ["version = 2", ""]
    for key, project in projects.items():
        records.extend(
            (
                f"[projects.{json.dumps(key)}]",
                f"path = {json.dumps(str(project.directory))}",
                f"session_id = {json.dumps(project.session_id)}",
                f"agent = {json.dumps(project.agent)}",
                f"provider = {json.dumps(project.provider.value)}",
            )
        )
        if project.model is not None:
            records.append(f"model = {json.dumps(project.model)}")
        records.append("")
    content = "\n".join(records)
    descriptor, temporary_name = tempfile.mkstemp(prefix="workspace.", dir=config_path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as temporary:
            temporary.write(content)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, config_path)
    except OSError as error:
        raise WorkspaceError(
            f"cannot write workspace configuration {config_path}: {error}"
        ) from error
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
