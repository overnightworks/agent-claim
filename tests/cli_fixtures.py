"""CLI-boundary test scaffolding shared by `tests/test_cli.py` and the
owner test files split from it (`tests/test_checkout.py`,
`tests/test_protect.py`): real and faked `git` process helpers, agent-identity
env setup, and the "this boundary must not be reached" forbid-helpers. All
three import this module directly; pytest's rootless collection puts
`tests/` on `sys.path`, so a plain `import cli_fixtures` resolves here."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from board_fixtures import BASE

from agent_coordination import checkout, github, store
from agent_coordination import cli as issue_claim
from agent_coordination.protocol import ClaimError


def _real_git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True, text=True
    )


def _git_checkout(
    *,
    head: str = BASE,
    branch: str = "codex/issue-72",
    git_directory: str = "/repo/.git/worktrees/issue-72",
    common_directory: str = "/repo/.git",
    dirty: str = "",
) -> dict[tuple[str, ...], str]:
    return {
        ("rev-parse", "HEAD"): head,
        ("rev-parse", "--show-toplevel"): "/repo",
        ("branch", "--show-current"): branch,
        ("rev-parse", "--git-dir"): git_directory,
        ("rev-parse", "--git-common-dir"): common_directory,
        ("status", "--porcelain"): dirty,
        ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main",
    }


_ORIGIN_HEAD_SYMBOLIC_REF = ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")


def _fallback_git_output(
    values: dict[tuple[str, ...], str], *, origin_head_empty: bool
) -> Callable[[list[str]], str]:
    """A `_git_output` fake for a clone whose `origin/HEAD` never got recorded
    (issue #238, Grok review): measured locally, real
    `git symbolic-ref --quiet refs/remotes/origin/HEAD` then exits non-zero
    with empty stdout and stderr, which `_git_output` turns into
    `ClaimError("unknown git failure")` -- the `origin_head_empty=True` branch
    additionally covers the otherwise-untested case of git exiting 0 with an
    empty ref name."""

    def git(arguments: list[str]) -> str:
        key = tuple(arguments)
        if key == _ORIGIN_HEAD_SYMBOLIC_REF:
            if origin_head_empty:
                return ""
            raise ClaimError("unknown git failure")
        return values[key]

    return git


def _set_agent_identity_env(
    monkeypatch: pytest.MonkeyPatch, environ: dict[str, str] | None = None
) -> None:
    for name in (
        issue_claim.ACO_AGENT_ENV,
        issue_claim.GROK_SESSION_ID_ENV,
        issue_claim.CLAUDE_SESSION_ID_ENV,
    ):
        monkeypatch.delenv(name, raising=False)
    for name, value in (environ or {}).items():
        monkeypatch.setenv(name, value)


def _forbid_github_construction(monkeypatch: pytest.MonkeyPatch) -> None:
    def unused(*args, **kwargs):
        pytest.fail("agent identity must be resolved before GitHub")

    monkeypatch.setattr(github, "GitHubForge", unused)
    monkeypatch.setattr(github, "discover_repository", unused)


def _forbid_git_fill(monkeypatch: pytest.MonkeyPatch) -> None:
    def unused(arguments: list[str]) -> str:
        pytest.fail("agent identity must be resolved before git fill")

    monkeypatch.setattr(checkout, "_git_output", unused)


def _assert_missing_identity_message(message: str) -> None:
    assert "--agent" in message
    assert issue_claim.ACO_AGENT_ENV in message
    assert issue_claim.GROK_SESSION_ID_ENV in message
    assert issue_claim.CLAUDE_SESSION_ID_ENV in message
    assert "GROK_AGENT" not in message


def _forbid_protect_git_github_and_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    def unused(*args, **kwargs):
        pytest.fail("this protect path must not use identity, git, GitHub, or the store")

    monkeypatch.setattr(checkout, "_resolved_agent", unused)
    monkeypatch.setattr(checkout, "_git_output", unused)
    monkeypatch.setattr(github, "GitHubForge", unused)
    monkeypatch.setattr(github, "discover_repository", unused)
    monkeypatch.setattr(store, "fetch_state", unused)


def _patch_command(*lines: str) -> str:
    """Wrap Codex's `apply_patch` file-line grammar in its `Begin`/`End Patch`
    envelope, the way `command` actually arrives in the hook payload."""
    return "\n".join(("*** Begin Patch", *lines, "*** End Patch"))


def _forbid_remote_url(monkeypatch: pytest.MonkeyPatch) -> None:
    def unused(remote: str) -> str:
        pytest.fail("a forge-free command must never read a remote's own URL")

    monkeypatch.setattr(checkout, "remote_url", unused)


def _forbid_forge_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    _forbid_github_construction(monkeypatch)
    _forbid_remote_url(monkeypatch)


def arrange_scope_width(
    monkeypatch: pytest.MonkeyPatch,
    client: object,
    *,
    directories: frozenset[str] = frozenset(),
    versioned: tuple[str, ...] | None = None,
    validate_checkout: bool = True,
) -> None:
    """The monkeypatch quadruple every scope-width `claim`/`rescope` case in
    `test_cli.py`'s wide-scope tables shares: a GitHub forge, a directory
    classifier, an optional versioned-file listing, and -- for a fresh
    `claim` -- a no-op checkout validator (`rescope` patches the store and
    git output for its own standing claim instead, so it passes
    `validate_checkout=False`)."""
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(
        checkout,
        "_scope_directories",
        lambda paths: tuple(path for path in paths if path in directories),
    )
    if versioned is not None:
        monkeypatch.setattr(checkout, "versioned_paths", lambda: versioned)
    if validate_checkout:
        monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
