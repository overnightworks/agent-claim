from __future__ import annotations

import pytest

from agent_coordination import cli


def test_register_requires_the_explicit_stopped_handover() -> None:
    with pytest.raises(SystemExit, match="2"):
        cli.main(
            [
                "register",
                "alpha",
                "--path",
                "/tmp/project",
                "--session-id",
                "123e4567-e89b-12d3-a456-426614174000",
                "--agent",
                "old head",
            ]
        )


def test_run_refuses_a_repository_target(capsys) -> None:
    assert cli.main(["--repo", "example/repository", "run"]) == 2
    assert "--repo" in capsys.readouterr().err
