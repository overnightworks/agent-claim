"""Behavioral tests for `scripts/check_root_layout.py`.

Each test drives `violations` against a real, freshly initialized git
repository under `tmp_path`. Git itself is the boundary this script queries,
so faking its output would test the wrong layer.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

_MODULE_PATH = Path(__file__).parent.parent / "scripts" / "check_root_layout.py"


def _load_check_root_layout() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_root_layout", _MODULE_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check_root_layout = _load_check_root_layout()
sys.modules.setdefault("check_root_layout", check_root_layout)
violations = check_root_layout.violations
VIOLATION_MESSAGE = check_root_layout.VIOLATION_MESSAGE


def _init_repository(repo_root: Path) -> None:
    subprocess.run(["git", "init", "--quiet"], cwd=repo_root, check=True)


def _track(repo_root: Path, *relative_paths: str) -> None:
    for relative_path in relative_paths:
        path = repo_root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("content\n")
    subprocess.run(["git", "add", *relative_paths], cwd=repo_root, check=True)


def test_allowed_root_layout_has_no_violations(tmp_path: Path) -> None:
    _init_repository(tmp_path)
    _track(
        tmp_path,
        "pyproject.toml",
        "README.md",
        "AGENTS.md",
        "src/agent_claim/__init__.py",
        "tests/test_example.py",
        "scripts/check_root_layout.py",
    )

    assert violations(tmp_path) == ()


def test_stray_tracked_root_file_is_reported(tmp_path: Path) -> None:
    _init_repository(tmp_path)
    _track(tmp_path, "pyproject.toml", "stray.txt")

    assert violations(tmp_path) == (f"stray.txt: {VIOLATION_MESSAGE}",)


def test_ignored_stray_file_does_not_count(tmp_path: Path) -> None:
    _init_repository(tmp_path)
    _track(tmp_path, "pyproject.toml")
    (tmp_path / ".gitignore").write_text("ignored.txt\n")
    (tmp_path / "ignored.txt").write_text("content\n")

    assert violations(tmp_path) == ()


def test_untracked_non_ignored_stray_file_counts(tmp_path: Path) -> None:
    _init_repository(tmp_path)
    _track(tmp_path, "pyproject.toml")
    (tmp_path / "stray.txt").write_text("content\n")

    assert violations(tmp_path) == (f"stray.txt: {VIOLATION_MESSAGE}",)


def test_agent_claim_config_directory_is_allowed(tmp_path: Path) -> None:
    _init_repository(tmp_path)
    _track(tmp_path, "pyproject.toml", ".agent-claim/board.toml")

    assert violations(tmp_path) == ()
