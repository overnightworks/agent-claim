from __future__ import annotations

from pathlib import Path

import pytest

from agent_coordination import cli, workspace


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


def test_register_writes_an_explicit_stopped_handover_through_the_cli(
    capsys, monkeypatch, tmp_path: Path
) -> None:
    config_path = tmp_path / "config" / "workspace.toml"
    project_path = tmp_path / "project"
    project_path.mkdir()
    monkeypatch.setattr(cli, "_workspace_config_path", lambda: config_path)

    result = cli.main(
        [
            "register",
            "alpha",
            "--path",
            str(project_path),
            "--session-id",
            "123e4567-e89b-12d3-a456-426614174000",
            "--agent",
            "restored head",
            "--stopped",
        ]
    )

    assert result == 0
    assert capsys.readouterr().out == "alpha: registered\n"
    assert workspace.load_config(config_path).projects["alpha"].agent == "restored head"


@pytest.mark.parametrize(
    ("outcomes", "expected_status", "expected_output"),
    [
        ([workspace.RunOutcome("alpha", workspace.RunState.REATTACHED)], 0, "alpha: reattached\n"),
        (
            [workspace.RunOutcome("alpha", workspace.RunState.FAILED, "tmux unavailable")],
            1,
            "alpha: failed: tmux unavailable\n",
        ),
    ],
)
def test_run_prints_each_workspace_outcome_and_uses_failure_status(
    capsys,
    monkeypatch,
    outcomes: list[workspace.RunOutcome],
    expected_status: int,
    expected_output: str,
) -> None:
    monkeypatch.setattr(cli, "_workspace_config_path", lambda: Path("/workspace.toml"))
    monkeypatch.setattr(workspace, "run_projects", lambda *_arguments: tuple(outcomes))

    assert cli.main(["run", "alpha"]) == expected_status
    assert capsys.readouterr().out == expected_output
