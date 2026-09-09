from __future__ import annotations

from pathlib import Path

import pytest

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


@pytest.mark.parametrize(
    ("provider", "command", "expected"),
    [
        (providers.Provider.CODEX, b"codex\0resume\0" + SESSION_ID.encode() + b"\0", "match"),
        (providers.Provider.CLAUDE, b"claude\0--resume\0" + SESSION_ID.encode() + b"\0", "match"),
        (
            providers.Provider.CLAUDE,
            b"claude\0--resume\0" + SESSION_ID.encode() + b"\0continue this work\0",
            "match",
        ),
        (
            providers.Provider.CODEX,
            b"node\0codex.js\0resume\0" + SESSION_ID.encode() + b"\0",
            "ambiguous",
        ),
        (providers.Provider.GROK, b"grok\0--resume\0" + SESSION_ID.encode() + b"\0", "unrelated"),
        (providers.Provider.CODEX, b"codex\0resume\0--model\0model\0different\0", "unrelated"),
        (
            providers.Provider.CODEX,
            b"codex\0resume\0--model\0model\0" + SESSION_ID.encode() + b"\0",
            "match",
        ),
        (
            providers.Provider.CODEX,
            b"codex\0resume\0--unexpected\0" + SESSION_ID.encode() + b"\0",
            "ambiguous",
        ),
        (
            providers.Provider.CODEX,
            b"codex\0resume\0" + SESSION_ID.encode() + b"\0--unexpected\0",
            "ambiguous",
        ),
        (
            providers.Provider.CODEX,
            b"codex\0--model\0model\0resume\0" + SESSION_ID.encode() + b"\0",
            "ambiguous",
        ),
        (
            providers.Provider.CLAUDE,
            b"claude\0--resume\0" + SESSION_ID.encode() + b"\0--model\0model\0",
            "match",
        ),
        (
            providers.Provider.CLAUDE,
            b"claude\0--resume\0" + SESSION_ID.encode() + b"\0--unexpected\0",
            "ambiguous",
        ),
        (
            providers.Provider.CLAUDE,
            b"claude\0--model\0model\0--resume\0" + SESSION_ID.encode() + b"\0",
            "ambiguous",
        ),
        (providers.Provider.CLAUDE, b"claude\0--resume\0\xff\0", "ambiguous"),
        (providers.Provider.CLAUDE, b"claude\0--resume\0different\0", "unrelated"),
        (providers.Provider.CODEX, b"codex\0--version\0", "unrelated"),
        (providers.Provider.CLAUDE, b"claude\0--help\0", "unrelated"),
    ],
)
def test_native_command_classification_accepts_only_exact_native_resumes(
    provider: providers.Provider, command: bytes, expected: str
) -> None:
    assert providers.classify_native_command(provider, command, SESSION_ID).value == expected
