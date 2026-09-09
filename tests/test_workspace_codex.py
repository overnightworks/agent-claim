from __future__ import annotations

import tomllib
from pathlib import Path

from agent_coordination import providers

SESSION_ID = "123e4567-e89b-12d3-a456-426614174000"
PROJECT_DIRECTORY = Path("/canonical/project")


def test_codex_resume_keeps_the_registered_uuid_and_optional_model() -> None:
    assert providers.resume_command(providers.Provider.CODEX, SESSION_ID, PROJECT_DIRECTORY) == [
        "codex",
        "resume",
        SESSION_ID,
    ]
    assert providers.resume_command(
        providers.Provider.CODEX, SESSION_ID, PROJECT_DIRECTORY, "gpt-5.3-codex"
    ) == [
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


def test_fresh_codex_uses_one_startup_hook_without_a_prompt() -> None:
    callback = "/venv/bin/python -I -m agent_coordination.cli _capture-codex-start"
    command = providers.fresh_codex_command(
        PROJECT_DIRECTORY,
        "gpt-5.3-codex",
        callback,
    )

    assert command[:5] == ["codex", "-C", str(PROJECT_DIRECTORY), "--model", "gpt-5.3-codex"]
    assert command[5] == "-c"
    hook = tomllib.loads(command[6])["hooks"]["SessionStart"]
    assert hook == [{"matcher": "^startup$", "hooks": [{"type": "command", "command": callback}]}]
    assert providers.fresh_codex_command(Path("/other/project"), None, callback)[-1] == command[-1]
    assert "resume" not in command
    assert "--prompt" not in command


def test_project_environment_removes_stale_fresh_capture_identity() -> None:
    environment = providers.project_environment(
        {"ACO_CAPTURE_ATTEMPT": "old", "ACO_CAPTURE_PROJECT": "other", "TOKEN": "kept"}, "head"
    )

    assert environment == {"TOKEN": "kept", "ACO_AGENT": "head"}
