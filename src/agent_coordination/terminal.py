"""Tmux and GNOME Terminal operations for one managed workspace console."""

from __future__ import annotations

import shlex
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from . import process

_PROJECT_OPTION = "@aco_project"
_SESSION_OPTION = "@aco_session_id"
_VIEWER_PENDING_OPTION = "@aco_viewer_pending"


class TerminalError(RuntimeError):
    pass


class TargetState(StrEnum):
    ABSENT = "absent"
    ATTACHED = "attached"
    DETACHED = "detached"
    EXITED = "exited"


@dataclass(frozen=True)
class Target:
    state: TargetState
    project: str | None = None
    session_id: str | None = None
    viewer_pending: bool = False


@dataclass(frozen=True)
class Launch:
    command: list[str]
    environment: Mapping[str, str]
    removed_environment_names: frozenset[str]


class TerminalController(Protocol):
    def inspect(self, project: str) -> Target: ...

    def create(
        self,
        project: str,
        session_id: str,
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


def target_name(project: str) -> str:
    return f"aco-{project}"


def console_title(project: str) -> str:
    return f"ACO: {project}"


def target_matches(metadata: dict[str, str], project: str, session_id: str) -> bool:
    return metadata.get(_PROJECT_OPTION) == project and metadata.get(_SESSION_OPTION) == session_id


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
        metadata = {
            option: self._option(name, option)
            for option in (_PROJECT_OPTION, _SESSION_OPTION, _VIEWER_PENDING_OPTION)
        }
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
        )

    def create(
        self,
        project: str,
        session_id: str,
        directory: Path,
        launch: Launch,
    ) -> None:
        name = target_name(project)
        self._require_success(
            self._run("new-session", "-d", "-s", name, "-c", str(directory)),
            "create tmux target",
        )
        try:
            self._set_option(name, "remain-on-exit", "on")
            self._set_option(name, _PROJECT_OPTION, project)
            self._set_option(name, _SESSION_OPTION, session_id)
            self._set_environment(name, launch.environment, launch.removed_environment_names)
            self._start_command(name, launch.command)
        except TerminalError:
            raise

    def retry(
        self,
        project: str,
        launch: Launch,
    ) -> None:
        name = target_name(project)
        self._set_environment(name, launch.environment, launch.removed_environment_names)
        self._require_success(
            self._run("respawn-pane", "-k", "-t", name, shlex.join(["exec", *launch.command])),
            "retry Codex session",
        )

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

    def _run(self, *arguments: str) -> process.CapturedResult:
        try:
            return process.run_captured(["tmux", "-S", str(self._socket_path), *arguments])
        except process.ProcessError as error:
            raise TerminalError(f"tmux is unavailable: {error}") from error

    def _set_option(self, target: str, option: str, value: str) -> None:
        self._require_success(
            self._run("set-option", "-t", target, option, value), "set tmux metadata"
        )

    def _set_environment(
        self,
        target: str,
        environment: Mapping[str, str],
        removed_environment_names: frozenset[str],
    ) -> None:
        for name in removed_environment_names:
            self._require_success(
                self._run("set-environment", "-u", "-t", target, name),
                "clear session identity",
            )
        for name, value in environment.items():
            self._require_success(
                self._run("set-environment", "-t", target, name, value),
                "prepare session environment",
            )

    def _option(self, target: str, option: str) -> str:
        result = self._run("show-options", "-t", target, "-v", option)
        if result.exit_status == 1:
            return ""
        self._require_success(result, "read tmux metadata")
        return self._text(result)

    def _start_command(self, target: str, command: list[str]) -> None:
        self._require_success(
            self._run("send-keys", "-t", target, shlex.join(["exec", *command]), "Enter"),
            "start Codex session",
        )

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
