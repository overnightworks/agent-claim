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
from enum import StrEnum
from pathlib import Path

from . import protocol, providers, terminal

_CONFIG_VERSION = 2
_LEGACY_CONFIG_VERSION = 1
_PROJECT_KEY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}")
_PROJECT_FIELDS = frozenset({"path", "session_id", "agent", "model"})
_PROVIDER_PROJECT_FIELDS = _PROJECT_FIELDS | {"provider"}


class WorkspaceError(protocol.ClaimError):
    pass


class RunState(StrEnum):
    STARTED = "started"
    REUSED = "reused"
    REATTACHED = "reattached"
    PENDING = "viewer pending"
    RETRIED = "retried"
    FAILED = "failed"


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


def default_config_path(environment: Mapping[str, str], home: Path | None = None) -> Path:
    configured = environment.get("XDG_CONFIG_HOME")
    return (
        (Path(configured) if configured else (home or Path.home()) / ".config")
        / "aco"
        / "workspace.toml"
    )


def register_project(handoff: WorkspaceRegistration, config_path: Path) -> bool:
    """Store one explicit stopped-session handoff; return whether it was new."""
    candidate = _project(handoff)
    config_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with _locked(config_path.with_suffix(".lock")):
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
    controller = terminal_factory(runtime / "tmux.sock")
    child_environment = environment or os.environ
    outcomes: list[RunOutcome] = []
    with _locked(runtime / "workspace.lock"):
        for project in selected:
            try:
                outcomes.append(_run_project(controller, project, child_environment))
            except terminal.TerminalError as error:
                outcomes.append(RunOutcome(project.key, RunState.FAILED, str(error)))
    return tuple(outcomes)


def _run_project(
    controller: terminal.TerminalController,
    project: WorkspaceProject,
    environment: Mapping[str, str],
) -> RunOutcome:
    target = controller.inspect(project.key)
    launch = terminal.Launch(
        providers.resume_command(project.provider, project.session_id, project.model),
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
        return RunOutcome(
            project.key, RunState.PENDING, "waiting for the previously launched console"
        )
    controller.open_viewer(project.key)
    observed = controller.inspect(project.key)
    if observed.state is terminal.TargetState.ATTACHED:
        controller.clear_viewer_pending(project.key)
        return RunOutcome(project.key, state)
    return RunOutcome(project.key, RunState.PENDING, "console launch is pending")


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


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


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
