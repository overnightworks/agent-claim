"""Codex-specific resume arguments and inherited environment handling."""

from __future__ import annotations

from collections.abc import Mapping

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


def session_identity_environment_names() -> frozenset[str]:
    """The inherited variables that identify a particular launching session."""
    return _SESSION_IDENTITY_ENVIRONMENTS


def resume_command(session_id: str, model: str | None = None) -> list[str]:
    """Build the native command that resumes one explicitly registered session."""
    command = ["codex", "resume"]
    if model is not None:
        command.extend(("--model", model))
    command.append(session_id)
    return command


def project_environment(environment: Mapping[str, str], agent: str) -> dict[str, str]:
    """Inherit authentication and configuration without inheriting a caller's session."""
    child = {
        name: value
        for name, value in environment.items()
        if name not in _SESSION_IDENTITY_ENVIRONMENTS
    }
    child["ACO_AGENT"] = agent
    return child
