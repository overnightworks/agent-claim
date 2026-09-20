"""CLI-boundary test scaffolding shared by `tests/test_cli.py` and the
owner test files split from it (`tests/test_checkout.py`,
`tests/test_protect.py`, `tests/test_store.py`): real and faked `git`
process helpers, agent-identity env setup, and the "this boundary must not
be reached" forbid-helpers. All four import this module directly; pytest's
rootless collection puts `tests/` on `sys.path`, so a plain
`import cli_fixtures` resolves here."""

from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

import pytest
from board_fixtures import BASE

from agent_coordination import checkout, github, process, store
from agent_coordination.protocol import ClaimError


def _stub_one_git_call(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str], *, exit_status: int, stderr: str
) -> None:
    """Force exactly one `checkout._git_run` argv to a chosen failure exit,
    every other call reaching the real launcher -- shared by
    `test_checkout.py` and `test_cli.py` (issue #322): a git-level failure
    (an unreachable remote, a colliding worktree path, an unmerged branch
    `git branch -d` itself refuses, ...) is cheaper to force this way than
    to reproduce with real git state."""
    real_git_run = checkout._git_run

    def fake(call_arguments: list[str], *, directory: Path | None = None) -> process.CapturedResult:
        if call_arguments == arguments:
            return process.CapturedResult(exit_status, b"", stderr.encode())
        return real_git_run(call_arguments, directory=directory)

    monkeypatch.setattr(checkout, "_git_run", fake)


def _real_git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True, text=True
    )


def _real_repository_with_bare_remote(
    tmp_path: Path, *, remote_name: str = "origin"
) -> tuple[Path, Path]:
    """A real `git init`-ed worktree, commit identity configured, with a
    real bare `remote_name` remote pointing at a sibling bare repository --
    the one raw-git skeleton every trunk-history or trailer-classification
    fixture in this suite builds its own commits onto (`test_checkout.py`'s
    and `test_cli.py`'s own real-repository proofs, issue #359 CI), instead
    of each hand-rolling the same `init`/`config`/`remote add` sequence."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _real_git(remote, "init", "-q", "--bare", "-b", "main")
    repo = tmp_path / "repo"
    repo.mkdir()
    _real_git(repo, "init", "-q", "-b", "main")
    _real_git(repo, "config", "user.name", "Test")
    _real_git(repo, "config", "user.email", "test@example.com")
    _real_git(repo, "config", "commit.gpgsign", "false")
    _real_git(repo, "remote", "add", remote_name, str(remote))
    return repo, remote


def _push_repository_trunk(repo: Path, remote_name: str) -> None:
    """`repo`'s current `main` pushed to `remote_name`, with `<remote_name>/HEAD`
    resolved -- the trailing half of the skeleton every trunk-history
    fixture repeats once its own commits are built (issue #359 CI)."""
    _real_git(repo, "push", "-q", remote_name, "main")
    _real_git(repo, "remote", "set-head", remote_name, "main")


def stub_board_config_tracked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every store-command test reads a tracked `board.toml` by default
    (issue #315), whichever of `test_cli.py`'s faked worktree, `test_protect.py`'s
    non-git `_isolate_protect_home` directory, or `test_store.py`'s real
    scratch checkout it runs against -- none of them actually `git add`s the
    file, so a real `git ls-files` check would otherwise always read "not
    tracked" here. The untracked/ignored refusal is its own axis, proven by
    `checkout.path_is_tracked`'s own tests in `test_checkout.py` and by
    `test_cli.py`'s `test_untracked_board_config_refuses_every_store_command_by_name`,
    which overrides this stub back to `False`. Each caller wraps this in its
    own `@pytest.fixture(autouse=True)` (never placed here itself, matching
    `conftest.py`'s "everything but git-toplevel isolation stays local to its
    test module") so every test file states in its own body that it reads a
    tracked board.toml by default."""
    monkeypatch.setattr(checkout, "path_is_tracked", lambda _path, **_kwargs: True)


def _git_checkout(
    *,
    head: str = BASE,
    branch: str = "codex/issue-72",
    git_directory: str = "/repo/.git/worktrees/issue-72",
    common_directory: str = "/repo/.git",
    dirty: str = "",
) -> dict[tuple[str, ...], str]:
    toplevel = "/repo"
    return {
        ("rev-parse", "HEAD"): head,
        ("rev-parse", "--verify", "HEAD"): head,
        ("rev-parse", "--show-toplevel"): toplevel,
        ("branch", "--show-current"): branch,
        ("rev-parse", "--git-dir"): git_directory,
        ("rev-parse", "--git-common-dir"): common_directory,
        # `resolve_path_checkout`'s one combined call (issue #314): the same
        # three facts above, in the order it requests them via
        # `-C`/`--path-format=absolute`, so `rescope`'s path-based checkout
        # resolution reads the identical fixture the separate keys above
        # already describe.
        (
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-dir",
            "--git-common-dir",
        ): "\n".join((toplevel, git_directory, common_directory)),
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

    def git(arguments: list[str], **_kwargs: object) -> str:
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
        checkout.ACO_AGENT_ENV,
        checkout.GROK_SESSION_ID_ENV,
        checkout.CLAUDE_SESSION_ID_ENV,
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
    assert checkout.ACO_AGENT_ENV in message
    assert checkout.GROK_SESSION_ID_ENV in message
    assert checkout.CLAUDE_SESSION_ID_ENV in message
    assert "GROK_AGENT" not in message


def _forbid_protect_git_github_and_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    def unused(*args, **kwargs):
        pytest.fail("this protect path must not use identity, git, GitHub, or the store")

    monkeypatch.setattr(checkout, "resolved_agent", unused)
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
        lambda paths, **_kwargs: tuple(path for path in paths if path in directories),
    )
    monkeypatch.setattr(checkout, "versioned_paths", lambda **_kwargs: versioned or ())
    if validate_checkout:
        monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
