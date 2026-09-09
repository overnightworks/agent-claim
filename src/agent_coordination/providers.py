"""Native workspace resume commands and inherited environment handling."""

from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path


class NativeCommandState(StrEnum):
    MATCH = "match"
    UNRELATED = "unrelated"
    AMBIGUOUS = "ambiguous"


class Provider(StrEnum):
    CODEX = "codex"
    CLAUDE = "claude"
    GROK = "grok"


_SESSION_IDENTITY_ENVIRONMENTS = frozenset(
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
_RESUME_ARGUMENT_COUNT = 3
_MODELED_RESUME_ARGUMENT_COUNT = 5


def session_identity_environment_names() -> frozenset[str]:
    """The inherited variables that identify a particular launching session."""
    return _SESSION_IDENTITY_ENVIRONMENTS


def resume_command(
    provider: Provider, session_id: str, directory: Path, model: str | None = None
) -> list[str]:
    """Build the provider-native command that resumes one registered conversation."""
    match provider:
        case Provider.CODEX:
            command = ["codex", "resume"]
            if model is not None:
                command.extend(("--model", model))
            return [*command, session_id]
        case Provider.CLAUDE:
            command = ["claude", "--resume", session_id]
            if model is not None:
                command.extend(("--model", model))
            return command
        case Provider.GROK:
            command = ["grok", "--resume", session_id, "--cwd", str(directory)]
            if model is not None:
                command.extend(("--model", model))
            return command


def native_executable(provider: Provider) -> str | None:
    """The directly executable native heads that can be safely observed."""
    return provider.value if provider is not Provider.GROK else None


def classify_native_command(
    provider: Provider, command_line: bytes, session_id: str
) -> NativeCommandState:
    """Classify one bounded native argv without retaining its private contents.

    Only the normal native resume forms identify a conversation.  A command for a
    different UUID is deliberately unrelated; a malformed form of the selected
    executable remains uncertain instead of being used as evidence of absence.
    """
    executable = native_executable(provider)
    if executable is None:
        return NativeCommandState.UNRELATED
    try:
        arguments = tuple(part.decode("utf-8") for part in command_line.split(b"\0") if part)
    except UnicodeDecodeError:
        return NativeCommandState.AMBIGUOUS
    if not arguments or Path(arguments[0]).name != executable:
        return NativeCommandState.AMBIGUOUS
    match provider:
        case Provider.CODEX:
            return _classify_codex_resume(arguments, session_id)
        case Provider.CLAUDE:
            return _classify_claude_resume(arguments, session_id)
        case Provider.GROK:
            return NativeCommandState.UNRELATED


def _classify_codex_resume(arguments: tuple[str, ...], session_id: str) -> NativeCommandState:
    if len(arguments) == _RESUME_ARGUMENT_COUNT and arguments[1:] == ("resume", session_id):
        return NativeCommandState.MATCH
    if (
        len(arguments) == _MODELED_RESUME_ARGUMENT_COUNT
        and arguments[1] == "resume"
        and arguments[2] == "--model"
    ):
        return (
            NativeCommandState.MATCH if arguments[4] == session_id else NativeCommandState.UNRELATED
        )
    if len(arguments) >= _RESUME_ARGUMENT_COUNT and arguments[1] == "resume":
        return (
            NativeCommandState.UNRELATED
            if arguments[-1] != session_id
            else NativeCommandState.AMBIGUOUS
        )
    return NativeCommandState.UNRELATED


def _classify_claude_resume(arguments: tuple[str, ...], session_id: str) -> NativeCommandState:
    if len(arguments) == _RESUME_ARGUMENT_COUNT and arguments[1:] == ("--resume", session_id):
        return NativeCommandState.MATCH
    if (
        len(arguments) == _MODELED_RESUME_ARGUMENT_COUNT
        and arguments[1] == "--resume"
        and arguments[3] == "--model"
    ):
        return (
            NativeCommandState.MATCH if arguments[2] == session_id else NativeCommandState.UNRELATED
        )
    if len(arguments) >= _RESUME_ARGUMENT_COUNT and arguments[1] == "--resume":
        return (
            NativeCommandState.UNRELATED
            if arguments[2] != session_id
            else NativeCommandState.AMBIGUOUS
        )
    return NativeCommandState.UNRELATED


def project_environment(environment: Mapping[str, str], agent: str) -> dict[str, str]:
    """Inherit authentication and configuration without inheriting a caller's session."""
    child = {
        name: value
        for name, value in environment.items()
        if name not in _SESSION_IDENTITY_ENVIRONMENTS
    }
    child["ACO_AGENT"] = agent
    return child
