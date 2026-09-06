"""Shared test isolation.

Only the autouse `_isolate_git_toplevel` fixture lives here; everything else
stays local to its test module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agent_claim import checkout

_SHOW_TOPLEVEL_ARGUMENTS = ["rev-parse", "--show-toplevel"]


@pytest.fixture(autouse=True)
def _isolate_git_toplevel(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """No test may resolve this repository's own board configuration.

    `board.CONFIG_PATH` is read relative to `rev-parse --show-toplevel`, so a
    test that never overrides it would otherwise load and be governed by this
    checkout's own `.agent-claim/board.toml`. A test that needs a specific
    toplevel keeps its own `monkeypatch.setattr(checkout, "_git_output", ...)`,
    which takes precedence over this default.
    """
    real_git_output = checkout._git_output

    def fake_git_output(arguments: list[str]) -> str:
        if arguments == _SHOW_TOPLEVEL_ARGUMENTS:
            return str(tmp_path)
        return real_git_output(arguments)

    monkeypatch.setattr(checkout, "_git_output", fake_git_output)
