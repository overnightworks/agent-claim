"""Tmux and GNOME Terminal operations for one managed workspace console."""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from . import process, providers

_PROJECT_OPTION = "@aco_project"
_SESSION_OPTION = "@aco_session_id"
_PROVIDER_OPTION = "@aco_provider"
_VIEWER_PENDING_OPTION = "@aco_viewer_pending"
_ENROLLMENT_STATE_OPTION = "@aco_enrollment_state"
_ENROLLMENT_ATTEMPT_OPTION = "@aco_enrollment_attempt"
_ENROLLMENT_DIRECTORY_OPTION = "@aco_enrollment_directory"
_ENROLLMENT_AGENT_OPTION = "@aco_enrollment_agent"
_ENROLLMENT_MODEL_OPTION = "@aco_enrollment_model"
_ENROLLMENT_SESSION_OPTION = "@aco_enrollment_session_id"
_ENROLLMENT_OPTIONS = (
    _ENROLLMENT_STATE_OPTION,
    _ENROLLMENT_ATTEMPT_OPTION,
    _ENROLLMENT_DIRECTORY_OPTION,
    _ENROLLMENT_AGENT_OPTION,
    _ENROLLMENT_MODEL_OPTION,
    _ENROLLMENT_SESSION_OPTION,
)
_TARGET_OPTIONS = (
    _PROJECT_OPTION,
    _SESSION_OPTION,
    _PROVIDER_OPTION,
    _VIEWER_PENDING_OPTION,
    *_ENROLLMENT_OPTIONS,
)
_ASCII_CONTROL_LIMIT = 32
_ASCII_DELETE = 127


class TerminalError(RuntimeError):
    pass


def notify_login_recovery(summary: str) -> None:
    """Try to report bounded login recovery state without affecting recovery itself."""
    try:
        process.run_captured(["notify-send", "ACO workspace recovery", summary])
    except (process.ProcessError, OSError):
        return


class TargetState(StrEnum):
    ABSENT = "absent"
    ATTACHED = "attached"
    DETACHED = "detached"
    EXITED = "exited"


class EnrollmentState(StrEnum):
    INITIALIZING = "initializing"
    PENDING = "pending"
    FINAL = "final"


@dataclass(frozen=True)
class Target:
    state: TargetState
    project: str | None = None
    session_id: str | None = None
    viewer_pending: bool = False
    provider: providers.Provider | None = None
    enrollment: Enrollment | None = None


@dataclass(frozen=True)
class Enrollment:
    directory: Path
    agent: str
    model: str | None
    attempt: str
    state: EnrollmentState
    session_id: str | None = None


@dataclass(frozen=True)
class Launch:
    command: list[str]
    environment: Mapping[str, str]
    removed_environment_names: frozenset[str]
    provider: providers.Provider = providers.Provider.CODEX
    enrollment: Enrollment | None = None


class TerminalController(Protocol):
    def inspect(self, project: str) -> Target: ...

    def inspect_all(self) -> tuple[Target, ...]: ...

    def create(
        self,
        project: str,
        session_id: str | None,
        directory: Path,
        launch: Launch,
    ) -> None: ...

    def retry(
        self,
        project: str,
        launch: Launch,
    ) -> None: ...

    def open_viewer(self, project: str) -> None: ...

    def clear_viewer_pending(self, project: str) -> None: ...

    def finalize_enrollment(self, project: str) -> None: ...

    def stage_enrollment(self, project: str, session_id: str) -> None: ...

    def retry_enrollment(self, project: str, launch: Launch) -> None: ...


def target_name(project: str) -> str:
    return f"aco-{project}"


def console_title(project: str) -> str:
    return f"ACO: {project}"


def target_matches(
    metadata: Mapping[str, str], project: str, provider: providers.Provider, session_id: str
) -> bool:
    target_provider = metadata.get(_PROVIDER_OPTION)
    return (
        metadata.get(_PROJECT_OPTION) == project
        and metadata.get(_SESSION_OPTION) == session_id
        and (
            target_provider == provider.value
            if target_provider
            else provider is providers.Provider.CODEX
        )
    )


def _target_provider(value: str) -> providers.Provider | None:
    if not value:
        return None
    try:
        return providers.Provider(value)
    except ValueError as error:
        raise TerminalError("tmux target has unsupported provider metadata") from error


def _enrollment(metadata: Mapping[str, str]) -> Enrollment | None:
    state_value = metadata[_ENROLLMENT_STATE_OPTION]
    if not state_value:
        if any(metadata[option] for option in _ENROLLMENT_OPTIONS[1:]):
            raise TerminalError("tmux target has incomplete enrollment metadata")
        return None
    attempt = metadata[_ENROLLMENT_ATTEMPT_OPTION]
    directory = metadata[_ENROLLMENT_DIRECTORY_OPTION]
    agent = metadata[_ENROLLMENT_AGENT_OPTION]
    if not attempt or not directory or not agent:
        raise TerminalError("tmux target has incomplete enrollment metadata")
    try:
        state = EnrollmentState(state_value)
    except ValueError as error:
        raise TerminalError("tmux target has invalid enrollment metadata") from error
    enrollment = Enrollment(
        Path(directory),
        agent,
        metadata[_ENROLLMENT_MODEL_OPTION] or None,
        attempt,
        state,
        metadata[_ENROLLMENT_SESSION_OPTION] or None,
    )
    if enrollment.state is EnrollmentState.FINAL and enrollment.session_id is None:
        raise TerminalError("tmux target has incomplete final enrollment metadata")
    if metadata[_SESSION_OPTION] and enrollment.session_id is None:
        raise TerminalError("tmux target has interrupted enrollment UUID metadata")
    if enrollment.session_id is not None and metadata[_SESSION_OPTION] != enrollment.session_id:
        raise TerminalError("tmux target has mismatched enrollment UUID metadata")
    return enrollment


class TmuxTerminal:
    """A dedicated-socket tmux adapter; all command execution stays in process."""

    def __init__(self, socket_path: Path):
        self._socket_path = socket_path

    def inspect(self, project: str) -> Target:
        name = target_name(project)
        result = self._run("has-session", "-t", name)
        if result.exit_status == 1:
            return Target(TargetState.ABSENT)
        self._require_success(result, "inspect tmux target")
        metadata = {option: self._option(name, option) for option in _TARGET_OPTIONS}
        dead = self._run("list-panes", "-t", name, "-F", "#{pane_dead}")
        self._require_success(dead, "inspect tmux pane")
        if any(line == "1" for line in self._lines(dead)):
            state = TargetState.EXITED
        else:
            attached = self._run("display-message", "-p", "-t", name, "#{session_attached}")
            self._require_success(attached, "inspect tmux attachment")
            state = TargetState.ATTACHED if self._text(attached) != "0" else TargetState.DETACHED
        return Target(
            state,
            metadata[_PROJECT_OPTION] or None,
            metadata[_SESSION_OPTION] or None,
            metadata[_VIEWER_PENDING_OPTION] == "1",
            _target_provider(metadata[_PROVIDER_OPTION]),
            _enrollment(metadata),
        )

    def inspect_all(self) -> tuple[Target, ...]:
        result = self._run("list-sessions", "-F", "#{session_name}")
        if result.exit_status == 1:
            return ()
        self._require_success(result, "inspect tmux targets")
        targets: list[Target] = []
        for name in self._lines(result):
            if not name.startswith("aco-") or len(name) == len("aco-"):
                raise TerminalError("tmux target has foreign metadata")
            target = self.inspect(name.removeprefix("aco-"))
            if target.project != name.removeprefix("aco-"):
                raise TerminalError("tmux target has incomplete project metadata")
            targets.append(target)
        return tuple(targets)

    def create(
        self,
        project: str,
        session_id: str | None,
        directory: Path,
        launch: Launch,
    ) -> None:
        name = target_name(project)
        self._require_success(
            self._run(
                "new-session",
                "-d",
                "-s",
                name,
                "-c",
                str(directory),
                shlex.join(self._initial_command(name, project, launch, session_id)),
            ),
            "create tmux target",
        )
        self._wait_for_setup(name)
        try:
            self._configure_environment(name, launch)
            if launch.enrollment is not None:
                self._set_option(name, _ENROLLMENT_STATE_OPTION, EnrollmentState.PENDING.value)
        except TerminalError:
            self._run("kill-session", "-t", name)
            raise
        self._start_fresh_window(name, launch, directory)

    def retry(
        self,
        project: str,
        launch: Launch,
    ) -> None:
        name = target_name(project)
        self._configure_environment(name, launch)
        self._start_fresh_window(name, launch)

    def retry_enrollment(self, project: str, launch: Launch) -> None:
        if launch.enrollment is None:
            raise TerminalError("fresh enrollment launch is missing metadata")
        name = target_name(project)
        self._set_enrollment_metadata(name, launch.enrollment, EnrollmentState.INITIALIZING)
        self._set_option(name, _SESSION_OPTION, "")
        self._configure_environment(name, launch)
        self._set_option(name, _ENROLLMENT_STATE_OPTION, EnrollmentState.PENDING.value)
        self._start_fresh_window(name, launch)

    def open_viewer(self, project: str) -> None:
        name = target_name(project)
        self._set_option(name, _VIEWER_PENDING_OPTION, "1")
        try:
            process.start_detached(
                [
                    "gnome-terminal",
                    f"--title={console_title(project)}",
                    "--",
                    "tmux",
                    "-S",
                    str(self._socket_path),
                    "attach-session",
                    "-t",
                    name,
                ]
            )
        except process.ProcessError as error:
            self._set_option(name, _VIEWER_PENDING_OPTION, "")
            raise TerminalError(f"open project console: {error}") from error

    def clear_viewer_pending(self, project: str) -> None:
        self._set_option(target_name(project), _VIEWER_PENDING_OPTION, "")

    def finalize_enrollment(self, project: str) -> None:
        self._set_option(
            target_name(project), _ENROLLMENT_STATE_OPTION, EnrollmentState.FINAL.value
        )

    def stage_enrollment(self, project: str, session_id: str) -> None:
        target = target_name(project)
        self._set_option(target, _SESSION_OPTION, session_id)
        self._set_option(target, _ENROLLMENT_SESSION_OPTION, session_id)

    def _run(
        self, *arguments: str, environment: Mapping[str, str] | None = None
    ) -> process.CapturedResult:
        try:
            return process.run_captured(
                ["tmux", "-S", str(self._socket_path), *arguments],
                env=None if environment is None else dict(environment),
            )
        except process.ProcessError as error:
            raise TerminalError(f"tmux is unavailable: {error}") from error

    def _set_option(self, target: str, option: str, value: str) -> None:
        self._require_success(
            self._run("set-option", "-t", target, option, value), "set tmux metadata"
        )

    def _option(self, target: str, option: str) -> str:
        result = self._run("show-options", "-t", target, "-v", option)
        if result.exit_status == 1:
            return ""
        self._require_success(result, "read tmux metadata")
        return self._text(result)

    @staticmethod
    def _environment_command(launch: Launch) -> list[str]:
        command = ["env"]
        for name in sorted(launch.removed_environment_names):
            command.extend(("-u", name))
        command.append(f"ACO_AGENT={launch.environment['ACO_AGENT']}")
        return [*command, *launch.command]

    def _initial_command(
        self, target: str, project: str, launch: Launch, session_id: str | None
    ) -> list[str]:
        metadata_commands = (
            [
                "tmux",
                "-S",
                str(self._socket_path),
                "set-option",
                "-t",
                target,
                "remain-on-exit",
                "on",
            ],
            [
                "tmux",
                "-S",
                str(self._socket_path),
                "set-option",
                "-t",
                target,
                _PROJECT_OPTION,
                project,
            ],
            [
                "tmux",
                "-S",
                str(self._socket_path),
                "set-option",
                "-t",
                target,
                _SESSION_OPTION,
                session_id or "",
            ],
            [
                "tmux",
                "-S",
                str(self._socket_path),
                "set-option",
                "-t",
                target,
                _PROVIDER_OPTION,
                launch.provider.value,
            ],
        )
        if launch.enrollment is not None:
            enrollment = launch.enrollment
            metadata_commands += (
                [
                    "tmux",
                    "-S",
                    str(self._socket_path),
                    "set-option",
                    "-t",
                    target,
                    _ENROLLMENT_STATE_OPTION,
                    EnrollmentState.INITIALIZING.value,
                ],
                [
                    "tmux",
                    "-S",
                    str(self._socket_path),
                    "set-option",
                    "-t",
                    target,
                    _ENROLLMENT_ATTEMPT_OPTION,
                    enrollment.attempt,
                ],
                [
                    "tmux",
                    "-S",
                    str(self._socket_path),
                    "set-option",
                    "-t",
                    target,
                    _ENROLLMENT_DIRECTORY_OPTION,
                    str(enrollment.directory),
                ],
                [
                    "tmux",
                    "-S",
                    str(self._socket_path),
                    "set-option",
                    "-t",
                    target,
                    _ENROLLMENT_AGENT_OPTION,
                    enrollment.agent,
                ],
                [
                    "tmux",
                    "-S",
                    str(self._socket_path),
                    "set-option",
                    "-t",
                    target,
                    _ENROLLMENT_MODEL_OPTION,
                    enrollment.model or "",
                ],
                [
                    "tmux",
                    "-S",
                    str(self._socket_path),
                    "set-option",
                    "-t",
                    target,
                    _ENROLLMENT_SESSION_OPTION,
                    enrollment.session_id or "",
                ],
            )
        setup = " && ".join(shlex.join(command) for command in metadata_commands)
        ready_signal = shlex.join(
            ["tmux", "-S", str(self._socket_path), "wait-for", "-S", f"aco-ready-{target}"]
        )
        return [
            "sh",
            "-c",
            f"{setup} && {ready_signal}",
        ]

    def _set_enrollment_metadata(
        self, target: str, enrollment: Enrollment, state: EnrollmentState
    ) -> None:
        values = (
            (_ENROLLMENT_STATE_OPTION, state.value),
            (_ENROLLMENT_ATTEMPT_OPTION, enrollment.attempt),
            (_ENROLLMENT_DIRECTORY_OPTION, str(enrollment.directory)),
            (_ENROLLMENT_AGENT_OPTION, enrollment.agent),
            (_ENROLLMENT_MODEL_OPTION, enrollment.model or ""),
            (_ENROLLMENT_SESSION_OPTION, enrollment.session_id or ""),
        )
        for option, value in values:
            self._set_option(target, option, value)

    def _wait_for_setup(self, target: str) -> None:
        self._require_success(
            self._run("wait-for", f"aco-ready-{target}"), "wait for tmux target setup"
        )

    def _configure_environment(self, target: str, launch: Launch) -> None:
        environment_names = frozenset(launch.environment)
        commands = [
            _control_command("set-environment", "-t", target, name, value)
            for name, value in launch.environment.items()
        ]
        commands.extend(
            _control_command("set-environment", "-r", "-t", target, name)
            for name in self._environment_names_to_remove(target, launch, environment_names)
        )
        commands.append(_control_command("detach-client"))
        try:
            result = process.run_bounded(
                [
                    "tmux",
                    "-C",
                    "-S",
                    str(self._socket_path),
                    "attach-session",
                    "-t",
                    target,
                ],
                input_data=("\n".join(commands) + "\n").encode(),
            )
        except process.ProcessError as error:
            raise TerminalError("prepare session environment failed") from error
        if result.exit_status != 0 or b"%error" in result.output:
            raise TerminalError("prepare session environment failed")

    def _environment_names_to_remove(
        self, target: str, launch: Launch, environment_names: frozenset[str]
    ) -> frozenset[str]:
        return (
            self._native_environment_names(target) | launch.removed_environment_names
        ) - environment_names

    def _native_environment_names(self, target: str) -> frozenset[str]:
        return self._shown_environment_names("-g") | self._shown_environment_names("-t", target)

    def _shown_environment_names(self, *arguments: str) -> frozenset[str]:
        result = self._run("show-environment", *arguments)
        if result.exit_status != 0:
            raise TerminalError("read tmux environment failed")
        return frozenset(
            line.removeprefix("-").split("=", maxsplit=1)[0] for line in self._lines(result)
        )

    def _start_fresh_window(
        self, target: str, launch: Launch, directory: Path | None = None
    ) -> None:
        old_window = self._text(
            self._successful_result(
                self._run("display-message", "-p", "-t", target, "#{window_id}"),
                "inspect tmux window",
            )
        )
        if not old_window:
            raise TerminalError("inspect tmux window failed: missing window id")
        command = ["new-window", "-d", "-P", "-F", "#{window_id}", "-t", target]
        if directory is not None:
            command.extend(("-c", str(directory)))
        command.append(shlex.join(self._provider_command(launch)))
        new_window = self._text(
            self._successful_result(
                self._run(*command, environment=launch.environment), "start provider session"
            )
        )
        if not new_window:
            raise TerminalError("start provider session failed: missing window id")
        self._require_success(
            self._run("select-window", "-t", new_window), "select provider session"
        )
        self._require_success(self._run("kill-window", "-t", old_window), "discard setup window")

    def _provider_command(self, launch: Launch) -> list[str]:
        provider = shlex.join(self._environment_command(launch))
        preserve_dead_pane = (
            f'tmux -S {shlex.quote(str(self._socket_path))} set-option -w -t "$TMUX_PANE" '
            "remain-on-exit on"
        )
        return [
            "sh",
            "-c",
            " && ".join(
                (
                    preserve_dead_pane,
                    f"exec {provider}",
                )
            ),
        ]

    @classmethod
    def _successful_result(
        cls, result: process.CapturedResult, action: str
    ) -> process.CapturedResult:
        cls._require_success(result, action)
        return result

    @staticmethod
    def _text(result: process.CapturedResult) -> str:
        return result.stdout.decode(errors="replace").strip()

    @classmethod
    def _lines(cls, result: process.CapturedResult) -> tuple[str, ...]:
        return tuple(line for line in cls._text(result).splitlines() if line)

    @classmethod
    def _require_success(cls, result: process.CapturedResult, action: str) -> None:
        if result.exit_status == 0:
            return
        detail = cls._text(result) or result.stderr.decode(errors="replace").strip()
        raise TerminalError(f"{action} failed: {detail or f'exit {result.exit_status}'}")


def _control_command(command: str, *arguments: str) -> str:
    return " ".join((command, *(_control_argument(argument) for argument in arguments)))


def _control_argument(value: str) -> str:
    if "\0" in value:
        raise TerminalError("tmux environment cannot contain a NUL")
    escaped = "".join(
        _control_character(character, leading=index == 0) for index, character in enumerate(value)
    )
    return f'"{escaped}"'


def _control_character(character: str, *, leading: bool) -> str:
    escaped = {"\\": "\\\\", '"': '\\"', "$": "\\$", "\n": "\\n", "\r": "\\015"}.get(character)
    if escaped is not None:
        return escaped
    if character == "~" and leading:
        return "\\~"
    if ord(character) < _ASCII_CONTROL_LIMIT or ord(character) == _ASCII_DELETE:
        return f"\\{ord(character):03o}"
    return character
