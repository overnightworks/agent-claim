from __future__ import annotations

from agent_coordination import providers

SESSION_ID = "123e4567-e89b-12d3-a456-426614174000"


def test_claude_resume_uses_the_registered_uuid_without_a_prompt() -> None:
    assert providers.resume_command(providers.Provider.CLAUDE, SESSION_ID) == [
        "claude",
        "--resume",
        SESSION_ID,
    ]


def test_claude_resume_keeps_an_explicit_model() -> None:
    assert providers.resume_command(providers.Provider.CLAUDE, SESSION_ID, "sonnet") == [
        "claude",
        "--resume",
        SESSION_ID,
        "--model",
        "sonnet",
    ]


def test_provider_choices_are_codex_and_claude() -> None:
    assert tuple(providers.Provider) == (providers.Provider.CODEX, providers.Provider.CLAUDE)
