from __future__ import annotations

from agent_coordination import codex


def test_resume_uses_the_registered_uuid_without_a_prompt() -> None:
    assert codex.resume_command("123e4567-e89b-12d3-a456-426614174000") == [
        "codex",
        "resume",
        "123e4567-e89b-12d3-a456-426614174000",
    ]


def test_resume_keeps_an_explicit_model_before_the_registered_uuid() -> None:
    assert codex.resume_command("123e4567-e89b-12d3-a456-426614174000", "gpt-5.3-codex") == [
        "codex",
        "resume",
        "--model",
        "gpt-5.3-codex",
        "123e4567-e89b-12d3-a456-426614174000",
    ]


def test_project_environment_keeps_authentication_but_replaces_session_identity() -> None:
    environment = codex.project_environment(
        {
            "ACO_AGENT": "launching head",
            "CODEX_HOME": "/safe/home",
            "CODEX_THREAD_ID": "old",
            "CLAUDE_CODE_SESSION_ID": "nested",
            "GROK_SESSION_ID": "another nested session",
        },
        "restored head",
    )

    assert environment == {"ACO_AGENT": "restored head", "CODEX_HOME": "/safe/home"}
