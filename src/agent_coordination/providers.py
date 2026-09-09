"""Native workspace resume commands and inherited environment handling."""

from __future__ import annotations

import json
from collections.abc import Mapping
from enum import StrEnum
from pathlib import Path


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

_START_CAPTURE_ENVIRONMENTS = frozenset(
    {
        "ACO_CAPTURE_PROJECT",
        "ACO_CAPTURE_DIRECTORY",
        "ACO_CAPTURE_CONFIG",
        "ACO_CAPTURE_RUNTIME",
        "ACO_CAPTURE_AGENT",
        "ACO_CAPTURE_MODEL",
        "ACO_CAPTURE_ATTEMPT",
    }
)


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


def fresh_codex_command(directory: Path, model: str | None, callback: str) -> list[str]:
    """Build Codex's promptless startup command with one launch-only hook."""
    command = ["codex", "-C", str(directory)]
    if model is not None:
        command.extend(("--model", model))
    hook = (
        '[{matcher = "^startup$", hooks = '
        f'[{{type = "command", command = {json.dumps(callback)}}}]}}]'
    )
    return [*command, "-c", f"hooks.SessionStart={hook}"]


def project_environment(environment: Mapping[str, str], agent: str) -> dict[str, str]:
    """Inherit authentication and configuration without inheriting a caller's session."""
    child = {
        name: value
        for name, value in environment.items()
        if name not in _SESSION_IDENTITY_ENVIRONMENTS | _START_CAPTURE_ENVIRONMENTS
    }
    child["ACO_AGENT"] = agent
    return child


def start_capture_environment_names() -> frozenset[str]:
    """Names reserved for one fresh Codex enrollment callback."""
    return _START_CAPTURE_ENVIRONMENTS
