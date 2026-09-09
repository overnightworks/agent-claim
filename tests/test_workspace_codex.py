from __future__ import annotations

from agent_coordination import providers

SESSION_ID = "123e4567-e89b-12d3-a456-426614174000"


def test_codex_resume_keeps_the_registered_uuid_and_optional_model() -> None:
    assert providers.resume_command(providers.Provider.CODEX, SESSION_ID) == [
        "codex",
        "resume",
        SESSION_ID,
    ]
    assert providers.resume_command(providers.Provider.CODEX, SESSION_ID, "gpt-5.3-codex") == [
        "codex",
        "resume",
        "--model",
        "gpt-5.3-codex",
        SESSION_ID,
    ]


def test_project_environment_keeps_configuration_but_replaces_session_identity() -> None:
    environment = providers.project_environment(
        {
            "ACO_AGENT": "launching head",
            "CODEX_HOME": "/safe/home",
            "CLAUDE_CONFIG_DIR": "/safe/claude",
            "CODEX_THREAD_ID": "old",
            "CLAUDE_CODE_SESSION_ID": "nested",
            "GROK_SESSION_ID": "another nested session",
        },
        "restored head",
    )

    assert environment == {
        "ACO_AGENT": "restored head",
        "CODEX_HOME": "/safe/home",
        "CLAUDE_CONFIG_DIR": "/safe/claude",
    }
