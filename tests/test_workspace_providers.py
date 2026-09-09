from __future__ import annotations

from pathlib import Path

from agent_coordination import providers

SESSION_ID = "123e4567-e89b-12d3-a456-426614174000"
PROJECT_DIRECTORY = Path("/canonical/project")


def test_claude_resume_uses_the_registered_uuid_without_a_prompt() -> None:
    assert providers.resume_command(providers.Provider.CLAUDE, SESSION_ID, PROJECT_DIRECTORY) == [
        "claude",
        "--resume",
        SESSION_ID,
    ]


def test_claude_resume_keeps_an_explicit_model() -> None:
    assert providers.resume_command(
        providers.Provider.CLAUDE, SESSION_ID, PROJECT_DIRECTORY, "sonnet"
    ) == [
        "claude",
        "--resume",
        SESSION_ID,
        "--model",
        "sonnet",
    ]


def test_grok_resume_uses_the_registered_uuid_and_canonical_directory() -> None:
    assert providers.resume_command(providers.Provider.GROK, SESSION_ID, PROJECT_DIRECTORY) == [
        "grok",
        "--resume",
        SESSION_ID,
        "--cwd",
        str(PROJECT_DIRECTORY),
    ]
    assert providers.resume_command(
        providers.Provider.GROK, SESSION_ID, PROJECT_DIRECTORY, "grok-4.6"
    ) == [
        "grok",
        "--resume",
        SESSION_ID,
        "--cwd",
        str(PROJECT_DIRECTORY),
        "--model",
        "grok-4.6",
    ]


def test_provider_choices_are_codex_claude_and_grok() -> None:
    assert tuple(providers.Provider) == (
        providers.Provider.CODEX,
        providers.Provider.CLAUDE,
        providers.Provider.GROK,
    )
