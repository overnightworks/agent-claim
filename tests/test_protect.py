"""Direct `protect` hook behavior: the pre-tool-use guard that denies a
mutating tool call outside a covering claim. Every test here drives
`issue_claim._protect`/`_protect_write` through `main(["protect"])` with the
hook's JSON payload on stdin (`_protect_main`) -- `protect` is the hook entry
point with its own doctrine, so this stays its owner file even though it
goes through `main`, unlike ordinary CLI-command tests in
`tests/test_cli.py`."""

from __future__ import annotations

import io
import json
import sys
from collections.abc import Callable
from pathlib import Path

import pytest
from board_fixtures import BASE, REPOSITORY, _active_claim
from cli_fixtures import (
    _assert_missing_identity_message,
    _forbid_forge_resolution,
    _forbid_git_fill,
    _forbid_github_construction,
    _forbid_protect_git_github_and_identity,
    _patch_command,
    _real_git,
    _set_agent_identity_env,
    stub_board_config_tracked,
)

from agent_coordination import board, checkout, hook_input, protocol, store
from agent_coordination import cli as issue_claim
from agent_coordination.protocol import ClaimError


@pytest.fixture(autouse=True)
def _stub_board_config_tracked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every `protect` test reads a tracked `board.toml` by default (issue
    #315): `_isolate_protect_home`'s `work` directory is never a real git
    checkout, so a real `git ls-files` check would otherwise always read
    "not tracked" here. A test proving the refusal itself overrides this."""
    stub_board_config_tracked(monkeypatch)


def _isolate_protect_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    return home, work


_PATH_CHECKOUT_ARGUMENTS = (
    "rev-parse",
    "--path-format=absolute",
    "--show-toplevel",
    "--git-dir",
    "--git-common-dir",
)


_ORIGIN_HEAD_SYMBOLIC_REF = ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD")


def _protect_git_values(
    work: Path,
    *,
    branch: str = "codex/issue-72-claims",
    git_directory: Path | None = None,
    common_directory: Path | None = None,
    origin_head: str | None = "refs/remotes/origin/main",
) -> dict[tuple[str, ...], str]:
    """The `_git_output` answers `checkout.resolve_path_checkout` needs for a
    write inside `work` (issue #314): a linked worktree by default, or a
    shared main checkout when `git_directory`/`common_directory` are pinned
    equal. Every payload path these tests use resolves to this one fake
    checkout regardless of the process's real cwd -- proving path-independence
    itself is the real-worktree proofs' job, below.

    `origin_head` is the resolved `origin/HEAD` symbolic ref gate G4 reads to
    determine the repository's own default branch; every fixture branch
    above but the explicit "not main" cases names a non-default branch, so a
    fixed `main` here never trips it by accident. `None` simulates a clone
    that never recorded one (gate G4's own unresolved case) -- omitted from
    this mapping entirely, so `_patch_protect_git` reads it as a denial
    rather than a missing fixture key."""
    resolved_git_directory = git_directory or (work / ".git" / "worktrees" / "issue-72")
    resolved_common_directory = common_directory or (work / ".git")
    values = {
        ("branch", "--show-current"): branch,
        ("rev-parse", "--verify", "HEAD"): BASE,
        _PATH_CHECKOUT_ARGUMENTS: "\n".join(
            (str(work.resolve()), str(resolved_git_directory), str(resolved_common_directory))
        ),
        # The canonical-remote comparison (issue #176, Erwartung 6) reads this
        # to confirm the fake forge target (REPOSITORY) matches it.
        ("config", "--get", "remote.origin.url"): f"git@github.com:{REPOSITORY}.git",
    }
    if origin_head is not None:
        values[_ORIGIN_HEAD_SYMBOLIC_REF] = origin_head
    return values


def _patch_protect_git(
    monkeypatch: pytest.MonkeyPatch,
    work: Path,
    *,
    branch: str = "codex/issue-72-claims",
    git_directory: Path | None = None,
    common_directory: Path | None = None,
    origin_head: str | None = "refs/remotes/origin/main",
) -> None:
    """Fake `checkout._git_output` for exactly one checkout, rooted at
    `work` (issue #314 repeat gate, finding 2). The fake maps every queried
    `directory` to `work`'s own answers only when that directory *is* `work`
    or one of its descendants -- a payload path's parent, or `work` itself --
    an explicit `{directory: checkout values}` mapping in spirit even though
    every descendant of one root shares one git answer (real `git -C
    <any subdirectory>` does too). A directory outside that mapping (the
    hook process's own cwd, or any other stray location) fails the test
    loudly instead of silently being answered with `work`'s checkout, which
    is the one thing a directory-blind fake could never prove: that
    `protect` actually selects its checkout from the payload path, not from
    wherever it happened to ask."""
    values = _protect_git_values(
        work,
        branch=branch,
        git_directory=git_directory,
        common_directory=common_directory,
        origin_head=origin_head,
    )
    resolved_work = work.resolve()

    def git(arguments: list[str], *, directory: Path | None = None) -> str:
        if arguments == ["status", "--porcelain"]:
            pytest.fail("dirty tree is irrelevant to protect")
        if arguments == ["rev-parse", "HEAD"]:
            pytest.fail("protect must not bind HEAD to claim.base")
        if directory is None:
            # Issue #314 repeat gate, B5: `_patch_protect_git` must not answer
            # for a caller that never named a directory at all -- a resolver
            # that silently fell back to the calling process's own cwd (the
            # historical bug) must fail this fixture, not pass it by
            # accident. Which non-`None` directory is queried legitimately
            # varies per test (a payload path's own parent, or a resolved
            # checkout's own toplevel) -- `values` below, not this guard, is
            # what actually pins each one.
            pytest.fail(
                "protect read git with no directory at all (issue #314: every read must "
                "name the payload's own checkout, never the calling process's cwd)"
            )
        resolved_directory = directory.resolve()
        if resolved_directory != resolved_work and resolved_work not in resolved_directory.parents:
            pytest.fail(
                f"protect read git for {directory}, which this fixture does not map "
                f"(issue #314 repeat gate, finding 2): only {work} and its descendants "
                "are a known checkout here"
            )
        key = tuple(arguments)
        if key == _ORIGIN_HEAD_SYMBOLIC_REF and key not in values:
            # A real, unresolved `origin/HEAD` exits nonzero rather than
            # answering empty (measured locally, issue #238); `origin_head is
            # None` reproduces that shape rather than a missing-fixture-key
            # `KeyError`.
            raise ClaimError("unknown git failure")
        return values[key]

    monkeypatch.setattr(checkout, "_git_output", git)


def _protect_active_claim(
    agent: str,
    *,
    scope: tuple[str, ...] = ("src",),
    branch: str = "codex/issue-72-claims",
    lane: bool = False,
    issue: int = 72,
) -> protocol.ActiveClaim:
    return _active_claim(agent, scope=scope, branch=branch, lane=lane, issue=issue)


def _protect_state_with_claim(claim: protocol.ActiveClaim) -> protocol.ClaimState:
    key = protocol.claim_key(claim.identity, claim.branch)
    return protocol.ClaimState(tip=protocol.ObjectId(BASE), claims={key: claim})


def _patch_protect_claim(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent: str = "Grok sess-1",
    scope: tuple[str, ...] = ("src",),
    branch: str = "codex/issue-72-claims",
    lane: bool = False,
) -> None:
    """Fake the store's fetched state with one live claim (issue #176):
    `protect` only ever reads `store.fetch_state`, so faking that boundary
    directly -- rather than a ledger comment `protect` no longer looks at --
    is the whole test double a `protect` test needs.
    """
    state = _protect_state_with_claim(
        _protect_active_claim(agent, scope=scope, branch=branch, lane=lane)
    )
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)


def _protect_main(monkeypatch: pytest.MonkeyPatch, payload: object) -> int:
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    monkeypatch.setattr(sys, "stdin", io.StringIO(raw))
    return issue_claim.main(["--repo", "example/agent-claim", "protect"])


def _assert_protect_decision(
    capsys: pytest.CaptureFixture[str],
    *,
    decision: str,
    reason: str | None = None,
) -> None:
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    if decision == "allow":
        assert payload == {"decision": "allow"}
        return
    assert payload == {"decision": "deny", "reason": reason}


def test_protect_denied_checkout_validation_never_reads_the_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A write from the shared main checkout denies `not main` from
    checkout validation alone (gate G4), before any claim could possibly
    cover it -- a counting fake store, observed the same way gate G5's own
    `test_protect_apply_patch_fetches_store_state_once_per_repository` does
    (`len(fetch_calls) == 1` there), must stay at 0 reads here (issue #314
    repeat gate, finding 5): a resolver that read the store before checkout
    validation finished would pass every other test in this file by
    accident, since none of them assert the store was left untouched."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    git_directory = work / ".git"
    _patch_protect_git(
        monkeypatch, work, git_directory=git_directory, common_directory=git_directory
    )
    _forbid_github_construction(monkeypatch)
    state = _protect_state_with_claim(_protect_active_claim("Grok sess-1"))
    fetch_calls: list[Path] = []

    def counting_fetch_state(*, worktree: Path, remote: str) -> protocol.ClaimState:
        fetch_calls.append(worktree)
        return state

    monkeypatch.setattr(store, "fetch_state", counting_fetch_state)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason=checkout.PROTECT_NOT_MAIN_REASON)
    assert len(fetch_calls) == 0


@pytest.mark.parametrize(
    "payload",
    [
        {"toolName": "Bash", "toolInput": {"command": "git diff"}},
        {"tool_name": "run_terminal_command", "tool_input": {"command": "git status"}},
        {"toolName": "Read", "toolInput": {"path": "src/secret.py"}},
        {"toolName": "read_file", "toolInput": {"path": "src/secret.py"}},
        {"tool_name": "grep", "tool_input": {"pattern": "secret"}},
        {"toolName": "list_dir", "toolInput": {"path": "src"}},
        {"tool_name": "spawn_subagent", "tool_input": {"prompt": "edit src"}},
    ],
)
def test_protect_non_mutating_tools_allow_without_identity_git_or_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    home, work = _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch)
    _forbid_protect_git_github_and_identity(monkeypatch)

    assert _protect_main(monkeypatch, payload) == 0
    _assert_protect_decision(capsys, decision="allow")
    assert list(home.iterdir()) == []
    assert list(work.iterdir()) == []


@pytest.mark.parametrize(
    ("tool_name", "path_key"),
    [("write", "path"), ("search_replace", "filePath")],
)
def test_protect_grok_camelcase_allows_when_session_claim_covers_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    tool_name: str,
    path_key: str,
) -> None:
    home, work = _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": tool_name, "toolInput": {path_key: str(work / "src/widget.py")}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")
    assert list(home.iterdir()) == []


def test_protect_allows_a_lane_claim_covering_the_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Guardrail (Entschieden #6): `_protect_write` already authorizes purely via
    agent/branch/scope, so a lane claim (no GitHub issue at all) passes through it
    unchanged, with no code path change required."""
    home, work = _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work, branch="docs/lane-cleanup")
    _patch_protect_claim(monkeypatch, branch="docs/lane-cleanup", lane=True)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")
    assert list(home.iterdir()) == []


def test_protect_grok_camelcase_denies_write_without_this_session_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    home, work = _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, agent="Codex Sol")

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")
    assert list(home.iterdir()) == []


def test_protect_absolute_file_path_allows_when_claim_scope_covers_it(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)
    target = work / "src" / "agent_coordination" / "cli.py"

    assert (
        _protect_main(
            monkeypatch,
            {
                "tool_name": "Write",
                "tool_input": {"file_path": str(target.resolve())},
            },
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


def test_protect_dirty_worktree_still_allows_covered_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    (work / "dirty.txt").write_text("edited\n", encoding="utf-8")
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


def test_protect_no_matching_claim_denies_claim_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    state = protocol.ClaimState(tip=protocol.ObjectId(BASE))
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")


@pytest.mark.parametrize(
    "payload",
    ["not-json", "[]", "null", "1", '{"toolName": 1}', "{}"],
)
def test_protect_invalid_hook_payload_denies_without_raising(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload: str,
) -> None:
    home, work = _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch)
    _forbid_protect_git_github_and_identity(monkeypatch)

    assert _protect_main(monkeypatch, payload) == 2
    _assert_protect_decision(capsys, decision="deny", reason="invalid hook payload")
    assert list(home.iterdir()) == []
    assert list(work.iterdir()) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"toolName": "Write"},
        {"tool_name": "Edit", "tool_input": "src/widget.py"},
        {"toolName": "MultiEdit", "toolInput": {"contents": "x"}},
        {"toolName": "write", "toolInput": {"path": "", "file_path": ""}},
        {
            "toolName": "apply_patch",
            "toolInput": {"command": "*** Begin Patch\n*** End Patch"},
        },
        {"toolName": "apply_patch", "toolInput": {"command": "not a patch at all"}},
        {"toolName": "apply_patch", "toolInput": {}},
    ],
)
def test_protect_mutating_tool_without_path_denies_path_required(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch)
    _forbid_protect_git_github_and_identity(monkeypatch)

    assert _protect_main(monkeypatch, payload) == 2
    _assert_protect_decision(capsys, decision="deny", reason="path required")


def test_protect_missing_identity_denies_without_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch)
    _forbid_github_construction(monkeypatch)
    _forbid_git_fill(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            # Identity resolution fails before this path is ever read
            # (`_forbid_git_fill` proves it), so it needs no real checkout.
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["decision"] == "deny"
    _assert_missing_identity_message(payload["reason"])


@pytest.mark.parametrize(
    "payload_for_work",
    [
        lambda work: {
            "toolName": "apply_patch",
            "toolInput": {
                "command": _patch_command(
                    f"*** Update File: {work / 'src/widget.py'}", "@@", "-old", "+new"
                )
            },
        },
        lambda work: {
            "tool_name": "NotebookEdit",
            "tool_input": {
                "notebook_path": str(work / "notebook.ipynb"),
                "new_source": "print(1)",
                "cell_type": "code",
                "edit_mode": "replace",
            },
        },
    ],
    ids=["apply_patch", "notebook_edit"],
)
def test_protect_extended_mutating_tools_deny_on_main_without_a_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload_for_work: Callable[[Path], dict[str, object]],
) -> None:
    """`apply_patch` (Codex) and `NotebookEdit` (Claude Code) joined the
    mutating table (issue #238): both are gated exactly like `Write`, denied
    from the shared main checkout before a claim is even looked up. The
    payload's own path must be absolute (issue #314 delta, finding R2), so
    each case builds it from `work` -- not known until the test itself picks
    a fresh `tmp_path` -- rather than at collection time."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(
        monkeypatch, work, git_directory=work / ".git", common_directory=work / ".git"
    )
    _forbid_github_construction(monkeypatch)

    assert _protect_main(monkeypatch, payload_for_work(work)) == 2
    _assert_protect_decision(capsys, decision="deny", reason="not main")


@pytest.mark.parametrize(
    ("notebook_path", "decision", "reason"),
    [
        ("src/widget.ipynb", "allow", None),
        ("docs/widget.ipynb", "deny", "claim first"),
    ],
)
def test_protect_notebook_edit_reads_notebook_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    notebook_path: str,
    decision: str,
    reason: str | None,
) -> None:
    """Claude Code's `NotebookEdit` carries its target under `notebook_path`,
    not `path`/`file_path`/`filePath` (issue #252) -- the real payload shape,
    checked against the claim scope exactly like any other mutating tool."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)

    exit_code = _protect_main(
        monkeypatch,
        {
            "tool_name": "NotebookEdit",
            "tool_input": {
                "notebook_path": str(work / notebook_path),
                "new_source": "print(1)",
                "cell_type": "code",
                "edit_mode": "replace",
            },
        },
    )

    assert exit_code == (0 if decision == "allow" else 2)
    _assert_protect_decision(capsys, decision=decision, reason=reason)


def test_protect_notebook_edit_ignores_a_decoy_path_key_it_never_sends(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`NotebookEdit` reads only `notebook_path` (issue #252): an in-scope
    `path` sitting next to an out-of-scope `notebook_path` -- a key this tool
    never actually sends -- must not smuggle the real target past the claim
    check the way a first-wins generic key list would."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)

    exit_code = _protect_main(
        monkeypatch,
        {
            "tool_name": "NotebookEdit",
            "tool_input": {
                "path": str(work / "src/widget.py"),
                "notebook_path": str(work / "docs/widget.ipynb"),
                "new_source": "print(1)",
                "cell_type": "code",
                "edit_mode": "replace",
            },
        },
    )

    assert exit_code == 2
    _assert_protect_decision(capsys, decision="deny", reason="claim first")


def test_protect_apply_patch_allows_when_every_touched_path_is_in_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Codex's `apply_patch` carries a patch-text `command`, not a path key
    (issue #252), and can touch several files in one call: every one of them
    must be in scope, not just the first."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)
    command = _patch_command(
        f"*** Update File: {work / 'src/widget.py'}",
        "@@",
        "-old",
        "+new",
        f"*** Add File: {work / 'src/new_module.py'}",
        "+content",
    )

    assert (
        _protect_main(monkeypatch, {"toolName": "apply_patch", "toolInput": {"command": command}})
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


@pytest.mark.parametrize(
    ("line_templates", "outside_path"),
    [
        (
            (
                "*** Update File: {work}/src/widget.py",
                "@@",
                "-old",
                "+new",
                "*** Add File: {work}/docs/widget.md",
                "+content",
            ),
            "docs/widget.md",
        ),
        (
            (
                "*** Update File: {work}/src/widget.py",
                "*** Move to: {work}/docs/widget.py",
                "@@",
                "-old",
                "+new",
            ),
            "docs/widget.py",
        ),
    ],
)
def test_protect_apply_patch_denies_naming_the_first_path_outside_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    line_templates: tuple[str, ...],
    outside_path: str,
) -> None:
    """A multi-file `apply_patch` call names the specific file outside the
    claim scope -- unlike a single-path write's generic `claim first` -- since
    the hook payload doesn't otherwise say which of several files was the
    problem (issue #252). Covers both a plain outside path and a `Move to:`
    rename landing outside scope. `line_templates` hold `{work}` -- the
    checkout, not known until the test picks its own `tmp_path` -- rather
    than a path already absolute (issue #314 delta, finding R2)."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)
    lines = (line.format(work=work) for line in line_templates)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "apply_patch", "toolInput": {"command": _patch_command(*lines)}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason=f"{outside_path} outside claim scope")


def test_protect_apply_patch_denies_an_indented_header_smuggled_after_add_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Outside an Update hunk, Codex recognises a header after trimming both
    ends of the line (issue #252's Grok finding): an indented
    `*** Update File:` line right after an in-scope `Add File` block still
    names a real file Codex will write, so the naive `startswith` scan that
    missed it -- letting it slip past the claim check -- is the vulnerability
    this pins shut."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, scope=("README.md",))
    command = _patch_command(
        f"*** Add File: {work / 'README.md'}",
        "+content",
        f"  *** Update File: {work / 'docs/evil.md'}",
        "@@",
        "-old",
        "+new",
    )

    assert (
        _protect_main(monkeypatch, {"toolName": "apply_patch", "toolInput": {"command": command}})
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="docs/evil.md outside claim scope")


def test_protect_apply_patch_denies_with_claim_first_when_no_session_claim_exists(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """With no live claim for this session at all -- as opposed to a live
    claim whose scope simply misses one of the patch's paths -- the repair
    sentence is the same `claim first` a single-path write gets (issue #252):
    naming a path as 'outside claim scope' would be false when there is no
    claim to be outside of."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, agent="Codex Sol")
    command = _patch_command(f"*** Update File: {work / 'src/widget.py'}", "@@", "-old", "+new")

    assert (
        _protect_main(monkeypatch, {"toolName": "apply_patch", "toolInput": {"command": command}})
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")


_BASH_WRITE_COMMAND_TEMPLATES: tuple[tuple[str, str], ...] = (
    ("cat > {path} <<EOF\ncontent\nEOF", hook_input.PATTERN_REDIRECT_OVERWRITE),
    ("echo hi >> {path}", hook_input.PATTERN_REDIRECT_APPEND),
    ("tee {path}", hook_input.PATTERN_TEE),
    ("sed -i 's/a/b/' {path}", hook_input.PATTERN_SED_IN_PLACE),
    ("mv {path} src/renamed.py", hook_input.PATTERN_MOVE),
    ("cp src/source.py {path}", hook_input.PATTERN_COPY),
    ("rm {path}", hook_input.PATTERN_REMOVE),
    ("git checkout -- {path}", hook_input.PATTERN_GIT_CHECKOUT),
    ("git restore {path}", hook_input.PATTERN_GIT_RESTORE),
)


def _write_target_payload(target: Path) -> dict[str, object]:
    return {"toolName": "write", "toolInput": {"path": str(target)}}


def _bash_rm_target_payload(target: Path) -> dict[str, object]:
    return {"toolName": "Bash", "toolInput": {"command": f"rm {target}"}}


_TARGET_PATH_PAYLOAD_BUILDERS = (_write_target_payload, _bash_rm_target_payload)


_BASH_OUTSIDE_SCOPE_PATH = "docs/widget.md"
_BASH_INSIDE_SCOPE_PATH = "src/widget.py"


def _bash_scope_case(
    template: str, pattern: str, *, path: str, denied: bool
) -> tuple[str, int, str | None]:
    command = template.format(path=path)
    if not denied:
        return command, 0, None
    return command, 2, f"{pattern} {path} outside claim scope"


@pytest.mark.parametrize(
    ("command", "status", "reason"),
    [
        *(
            _bash_scope_case(template, pattern, path=_BASH_OUTSIDE_SCOPE_PATH, denied=True)
            for template, pattern in _BASH_WRITE_COMMAND_TEMPLATES
        ),
        *(
            _bash_scope_case(template, pattern, path=_BASH_INSIDE_SCOPE_PATH, denied=False)
            for template, pattern in _BASH_WRITE_COMMAND_TEMPLATES
        ),
    ],
)
def test_protect_bash_judges_a_recognized_pattern_path_by_claim_scope(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    command: str,
    status: int,
    reason: str | None,
) -> None:
    """Issue #380: every write pattern `hook_input.hook_command_paths`
    recognizes runs the same Checkout/Default-Branch/Claim-Scope chain as
    an `Edit` path, resolved against the payload's own `cwd` since Bash's
    own paths are relative (PROT-31). A path inside the live claim's scope
    allows exactly like a covered `Edit` path; one outside it denies naming
    both the pattern and the path (PROT-33) -- for `mv` reporting its own
    (untouched) source rather than its in-scope destination, since `mv`
    judges every operand; `cp` here is tested on its destination alone
    (issue #380 delta), the only operand it actually writes."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, scope=("src",))

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "Bash", "toolInput": {"command": command}, "cwd": str(work)},
        )
        == status
    )
    _assert_protect_decision(capsys, decision="deny" if reason else "allow", reason=reason)


def test_protect_bash_allows_a_recognized_pattern_path_outside_every_repository(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PROT-32: unlike an `Edit` path (PROT-10's own "not in a repository"
    deny), a Bash-recognized path outside every git checkout allows -- a
    shell command routinely touches `/tmp` or a system path `protect` holds
    no claim to judge. The absolute path here also proves PROT-09 does not
    apply to Bash: no `cwd` is given at all, yet the write is still judged
    (and allowed) rather than denied as a relative payload path."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    outside = tmp_path / "not-a-repository"
    outside.mkdir()
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "Bash", "toolInput": {"command": f"rm {outside / 'x'}"}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


def test_protect_bash_allows_a_relative_path_when_the_payload_carries_no_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PROT-31's second half: a relative Bash-recognized path with no `cwd`
    in the payload at all cannot be resolved without guessing, and a wrong
    guess would deny legitimate work `protect` has no way to tell from a
    real out-of-scope write -- so it allows rather than risk a false deny
    (`specs/protect.spec.md`'s own `## Never`). No claim is set up at all:
    a resolver that fell back to guessing a cwd would deny `claim first`
    here instead of allowing. `_forbid_protect_git_github_and_identity`'s
    own `_resolved_agent` stub is left in place, unlike the sibling tests
    below: this path must never resolve identity at all (issue #380 delta,
    review finding: resolving it eagerly, before this allow, used to turn an
    unresolvable identity into a wrongful PROT-08 deny here)."""
    _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _forbid_protect_git_github_and_identity(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "Bash", "toolInput": {"command": "rm docs/widget.md"}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


def test_protect_bash_denies_a_relative_path_resolved_against_the_payloads_own_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The companion to the no-`cwd` allow above: once a `cwd` is given, a
    relative path resolves against it and is judged exactly like an
    already-absolute one, denying when it sits outside the live claim."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, scope=("src",))

    assert (
        _protect_main(
            monkeypatch,
            {
                "toolName": "Bash",
                "toolInput": {"command": "rm docs/widget.md"},
                "cwd": str(work),
            },
        )
        == 2
    )
    _assert_protect_decision(
        capsys, decision="deny", reason="rm docs/widget.md outside claim scope"
    )


def test_protect_bash_cd_changes_the_directory_for_the_rest_of_the_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #380 delta, decision 4: a literal `cd <path> &&` changes the
    directory a later relative path resolves against -- even into a
    different checkout than the payload's own `cwd`, judged there exactly
    as an absolute path in that checkout would be. Were `cd` not tracked,
    `docs/widget.md` would resolve under the payload's own `cwd`
    (`elsewhere`, no checkout at all) and allow outright instead."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, scope=("src",))

    assert (
        _protect_main(
            monkeypatch,
            {
                "toolName": "Bash",
                "toolInput": {"command": f"cd {work} && rm docs/widget.md"},
                "cwd": str(elsewhere),
            },
        )
        == 2
    )
    _assert_protect_decision(
        capsys, decision="deny", reason="rm docs/widget.md outside claim scope"
    )


def test_protect_bash_allows_a_command_with_no_recognized_pattern_without_identity_or_git(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PROT-30: `grep`, `ls`, `pytest`, and `git diff` name no recognized
    write pattern at all, so `protect` never even resolves identity, git,
    or the store for them -- the same "cannot judge what it cannot see"
    limit README documents for the read-only tools (issue #380)."""
    _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch)
    _forbid_protect_git_github_and_identity(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "Bash", "toolInput": {"command": "grep foo bar.py && ls && pytest"}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


@pytest.mark.parametrize(
    "tool_input",
    [{}, {"command": 1}],
    ids=["missing-command", "non-string-command"],
)
def test_protect_bash_allows_a_missing_or_non_string_command(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    tool_input: dict[str, object],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch)
    _forbid_protect_git_github_and_identity(monkeypatch)

    assert _protect_main(monkeypatch, {"toolName": "Bash", "toolInput": tool_input}) == 0
    _assert_protect_decision(capsys, decision="allow")


def test_protect_unknown_tool_name_denies_with_a_repair_sentence(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A tool name in neither the read nor the mutating table fails closed
    (issue #238) instead of the old default-allow, and the refusal names the
    table to extend rather than a bare 'unknown tool'."""
    _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch)
    _forbid_protect_git_github_and_identity(monkeypatch)

    assert _protect_main(monkeypatch, {"toolName": "invented_tool"}) == 2
    captured = capsys.readouterr()
    assert captured.err == ""
    payload = json.loads(captured.out)
    assert payload["decision"] == "deny"
    assert "invented_tool" in payload["reason"]
    assert "HOOK_TOOL_EFFECTS" in payload["reason"]
    assert "238" in payload["reason"]


def test_protect_primary_checkout_denies_not_main_without_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A payload path resolved into the shared main checkout denies `not
    main` (issue #314) regardless of what branch that checkout happens to be
    on -- `git-dir == git-common-dir` is the whole test, not a branch name."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    git_directory = work / ".git"
    _patch_protect_git(
        monkeypatch, work, git_directory=git_directory, common_directory=git_directory
    )
    _forbid_github_construction(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="not main")


@pytest.mark.parametrize(
    ("branch", "origin_head", "expected_reason"),
    [
        ("main", "refs/remotes/origin/main", "not main"),
        ("master", "refs/remotes/origin/master", "not main"),
        ("trunk", "refs/remotes/origin/trunk", "not main"),
        ("codex/issue-72-claims", None, checkout.DEFAULT_BRANCH_UNKNOWN_REASON),
    ],
    ids=["main", "master", "trunk", "unresolved-origin-head"],
)
def test_protect_denies_not_main_for_a_custom_or_unresolved_default_branch(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    branch: str,
    origin_head: str | None,
    expected_reason: str,
) -> None:
    """Gate G4's own retained matrix (issue #238, restored by the #314
    repeat gate and delta review): `protect` reads the repository's default
    branch the same way `claim` does -- a repository whose `origin/HEAD`
    names `trunk` denies a write from `trunk`, not just from the hardcoded
    `main`/`master` -- and a checkout whose `origin/HEAD` cannot be resolved
    at all denies outright, never falling back to that hardcoded guess the
    way `claim`'s own precondition does."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work, branch=branch, origin_head=origin_head)
    _forbid_github_construction(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason=expected_reason)


@pytest.mark.parametrize("payload_for", _TARGET_PATH_PAYLOAD_BUILDERS, ids=["write", "bash-rm"])
def test_protect_path_resolving_to_the_checkout_root_denies_path_required(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload_for: Callable[[Path], dict[str, object]],
) -> None:
    """`PATH_REQUIRED` fires when the payload path's own checkout resolves
    (issue #314 repeat gate, finding 2 fallout: the pre-fix fake answered
    any directory, including one genuinely outside `work`, with `work`'s own
    checkout -- masking that this scenario needs a *real* descendant of the
    checkout, not an outside path, to reach this denial at all) but the path
    itself resolves to exactly the checkout root: `work/subdir/..` queries
    git from the real descendant `work/subdir`, so the checkout resolves
    fine, while the full path resolves to `work` itself -- a repository-
    relative scope entry of `"."`, which `protocol._valid_scope` refuses. A
    Bash-recognized path runs the identical gate (issue #380)."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _forbid_github_construction(monkeypatch)

    assert _protect_main(monkeypatch, payload_for(work / "subdir" / "..")) == 2
    _assert_protect_decision(capsys, decision="deny", reason="path required")


@pytest.mark.parametrize(
    ("branch", "scope", "decision", "reason"),
    [
        pytest.param("codex/issue-72-claims", ("src",), "allow", None, id="matching-claim"),
        pytest.param("other/issue-72", ("src",), "deny", "claim first", id="wrong-branch"),
        pytest.param(
            "codex/issue-72-claims", ("docs",), "deny", "claim first", id="non-overlapping-scope"
        ),
    ],
)
def test_protect_write_decision_reflects_the_live_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    branch: str,
    scope: tuple[str, ...],
    decision: str,
    reason: str | None,
) -> None:
    """One live claim, three shapes (issue #314 repeat gate, finding 5): the
    default claim covers the write (`allow`); a claim on a different branch,
    or one whose scope excludes the payload path, each deny `claim first` --
    `protect` cannot tell an agent with no claim at all from one holding a
    claim that plainly does not cover this write, so both share one reason.
    This folds a former standalone allow-path test (which pinned identity,
    then git, then store as an exact call sequence -- not a contract either
    `protect` or its caller promises) together with its two sibling
    `claim first` denials, which already differed from each other only in
    which claim field misses: the same shape repeated for a fourth verdict
    is exactly the near-identical-copy case a parametrized table replaces,
    not three more functions. The companion proof below,
    `test_protect_denied_checkout_validation_never_reads_the_store`, covers
    the one part of that removed ordering that *is* observable behavior: a
    denial before the checkout resolves must never touch the store at
    all."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, branch=branch, scope=scope)

    status = _protect_main(
        monkeypatch,
        {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
    )

    assert status == (0 if decision == "allow" else 2)
    _assert_protect_decision(capsys, decision=decision, reason=reason)


def test_protect_claim_error_from_write_path_denies_json_without_error_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A `ClaimError` raised before the store is ever reached (here, reading
    the repository's board configuration -- `protect` is forge-free, issue
    #245, so it never resolves a forge target at all) denies with its own
    bare text -- only a failure inside `store.fetch_state` itself gets the
    'cannot reach refs/aco/state' wrapping (see the dedicated store-refusal
    tests below)."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)

    def failed(*_args: object, **_kwargs: object) -> board.BoardConfig:
        raise ClaimError("adapter failed")

    monkeypatch.setattr(board, "load_config", failed)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "ERROR:" not in captured.out
    assert json.loads(captured.out) == {"decision": "deny", "reason": "adapter failed"}


def test_protect_non_claim_error_from_write_path_denies_json_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)

    def crashed(*_args: object, **_kwargs: object) -> board.BoardConfig:
        raise RuntimeError("write path crashed")

    monkeypatch.setattr(board, "load_config", crashed)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.err == ""
    assert "ERROR:" not in captured.out
    assert json.loads(captured.out) == {
        "decision": "deny",
        "reason": "write path crashed",
    }


@pytest.mark.parametrize("payload_for", _TARGET_PATH_PAYLOAD_BUILDERS, ids=["write", "bash-rm"])
@pytest.mark.parametrize(
    ("failure", "match"),
    [
        pytest.param(
            protocol.ClaimError("auth or transport failure"),
            "auth or transport failure",
            id="unreachable",
        ),
        pytest.param(protocol.MalformedStateTreeError("bad tree"), "bad tree", id="malformed"),
        pytest.param(protocol.StateLineageError("rewritten"), "rewritten", id="lineage"),
        pytest.param(protocol.ClaimError("cannot fetch"), "cannot fetch", id="fetch-failure"),
    ],
)
def test_protect_maps_every_store_error_to_cannot_reach_the_state_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    failure: protocol.ClaimError,
    match: str,
    payload_for: Callable[[Path], dict[str, object]],
) -> None:
    """Issue #380: a Bash-recognized path shares this same store-failure
    mapping, since it reads the identical `_protect_cached_claim_state_or_denial`."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)

    def fake_fetch_state(*, worktree: Path, remote: str) -> protocol.ClaimState:
        raise failure

    monkeypatch.setattr(store, "fetch_state", fake_fetch_state)

    assert _protect_main(monkeypatch, payload_for(work / "src/widget.py")) == 2
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["decision"] == "deny"
    assert payload["reason"].startswith(f"cannot reach {store.STATE_REF}: ")
    assert match in payload["reason"]


def test_protect_apply_patch_maps_a_store_error_to_cannot_reach_the_state_ref(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`apply_patch`'s multi-path store check (issue #252) fails closed on a
    store read error exactly like the single-path check above -- it shares
    `_protect_fetch_claim_state` rather than re-deciding this on its own."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)

    def fake_fetch_state(*, worktree: Path, remote: str) -> protocol.ClaimState:
        raise ClaimError("cannot fetch")

    monkeypatch.setattr(store, "fetch_state", fake_fetch_state)
    command = _patch_command(f"*** Update File: {work / 'src/widget.py'}", "@@", "-old", "+new")

    assert (
        _protect_main(monkeypatch, {"toolName": "apply_patch", "toolInput": {"command": command}})
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "decision": "deny",
        "reason": f"cannot reach {store.STATE_REF}: cannot fetch",
    }


def test_protect_missing_state_ref_denies_cannot_reach(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    monkeypatch.setattr(store, "fetch_state", lambda **_k: protocol.EMPTY_STATE)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "decision": "deny",
        "reason": f"cannot reach {store.STATE_REF}: {protocol.MISSING_STATE_REF}",
    }


def test_protect_allow_is_forge_free_against_a_non_github_remote(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, work = _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)
    _forbid_forge_resolution(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


def test_protect_deny_is_forge_free_against_a_non_github_remote(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _, work = _isolate_protect_home(monkeypatch, tmp_path)
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, agent="Codex Sol")
    _forbid_forge_resolution(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(work / "src/widget.py")}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")


# Issue #314's own proofs: real linked worktrees (`git worktree add`), never
# `_git_output` mocking, since the whole point is that `protect`/`rescope`
# judge the payload's own checkout -- proving that requires a resolver that
# actually looks at different real directories, which a fake indifferent to
# `directory` cannot exercise.


_REAL_PATH_IS_TRACKED = checkout.path_is_tracked


def _use_real_path_is_tracked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo this module's autouse tracked-`board.toml` stub (issue #314
    gate B3): a real-worktree test builds an actual fixture repository, so
    `path_is_tracked` must read that repository's real git index -- via the
    payload checkout's own resolved directory -- rather than the stub every
    other (non-git) protect test relies on."""
    monkeypatch.setattr(checkout, "path_is_tracked", _REAL_PATH_IS_TRACKED)


def _protect_real_repo_with_worktree(
    tmp_path: Path, *, slug: str = "issue-72-widget"
) -> tuple[Path, Path]:
    """A real repository (`main`, reused across worktrees) with one linked,
    isolated worktree on a feature branch -- the same `git worktree add`
    recipe `checkout.ISOLATED_WORKTREE_RECIPE` documents. `main` carries a
    real, tracked (`git add -f`) empty `.agent-claim/board.toml` (issue #314
    gate B3): every worktree shares `main`'s history, so `path_is_tracked`
    reads a real "tracked" answer for it from any of them, via
    `_use_real_path_is_tracked`. `main` also carries a real, resolvable
    `origin/HEAD` (gate G4): every worktree's own feature branch reads a
    real default-branch name through it, rather than denying "default
    branch unknown" -- a repository with no recorded `origin/HEAD` at all is
    its own dedicated proof, `_real_worktree_on_default_branch_target`."""
    main = tmp_path / "repo"
    if not main.exists():
        main.mkdir()
        _real_git(main, "init", "-q", "-b", "main")
        _real_git(main, "config", "user.name", "Test")
        _real_git(main, "config", "user.email", "test@example.com")
        (main / "README.md").write_text("hello\n")
        (main / ".agent-claim").mkdir()
        (main / ".agent-claim" / "board.toml").write_text("")
        _real_git(main, "add", "-f", "README.md", ".agent-claim/board.toml")
        _real_git(main, "commit", "-q", "-m", "initial")
        _real_git(main, "remote", "add", "origin", "https://example.invalid/example/repo.git")
        _real_git(main, "update-ref", "refs/remotes/origin/main", "HEAD")
        _real_git(main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    worktree = tmp_path / "repo-worktrees" / slug
    worktree.parent.mkdir(parents=True, exist_ok=True)
    _real_git(main, "worktree", "add", "-q", str(worktree), "-b", f"codex/{slug}")
    (worktree / "src").mkdir()
    (worktree / "docs").mkdir()
    return main, worktree


def test_protect_bash_denies_deleting_a_linked_worktrees_own_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """PROT-14 reused for a Bash-recognized path (issue #380 delta, gate
    finding): a path naming a linked worktree's own root directory exactly
    -- `rm -rf ../<repo>-worktrees/issue-1-x` -- has a *parent*
    (`<repo>-worktrees/`) that is never itself a git checkout, so resolving
    the checkout from only the parent finds nothing. Trying the path itself
    too still finds that checkout and denies it as that checkout's own root
    (PROT-14), rather than silently allowing the whole checkout's deletion
    through PROT-32's "outside every repository" allow."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _use_real_path_is_tracked(monkeypatch)
    main, worktree = _protect_real_repo_with_worktree(tmp_path)
    monkeypatch.chdir(main)

    assert (
        _protect_main(
            monkeypatch,
            {
                "toolName": "Bash",
                "toolInput": {"command": f"rm -rf {worktree}"},
                "cwd": str(main),
            },
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="path required")


@pytest.mark.parametrize(
    "cwd_kind",
    ["main_default_branch", "main_other_branch", "outside_any_repository", "another_worktree"],
)
def test_protect_allows_the_same_payload_path_from_every_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cwd_kind: str,
) -> None:
    """Issue #314's repro, proof 1: the same payload path, claimed in its own
    linked worktree, must allow from all four measured cwds -- the main
    checkout on its default branch, the main checkout on another branch, a
    directory outside every repository, and an unrelated worktree. The old
    cwd-implicit `_git_output` calls allowed only from the fourth."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _use_real_path_is_tracked(monkeypatch)
    main, worktree = _protect_real_repo_with_worktree(tmp_path)
    _main2, other_worktree = _protect_real_repo_with_worktree(tmp_path, slug="issue-90-other")
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    if cwd_kind == "main_other_branch":
        _real_git(main, "checkout", "-q", "-b", "some-other-branch")
    cwd_by_kind = {
        "main_default_branch": main,
        "main_other_branch": main,
        "outside_any_repository": outside,
        "another_worktree": other_worktree,
    }
    monkeypatch.chdir(cwd_by_kind[cwd_kind])
    state = _protect_state_with_claim(
        _protect_active_claim("Grok sess-1", branch="codex/issue-72-widget")
    )
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)
    target = worktree / "src" / "widget.py"

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(target)}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


def test_protect_denies_a_path_outside_every_claim_scope_still(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Path-based resolution allows only the claimed worktree's own scope --
    a write inside a still-unclaimed area of the same worktree still denies,
    proving the fix does not simply allow everything real."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _use_real_path_is_tracked(monkeypatch)
    _main, worktree = _protect_real_repo_with_worktree(tmp_path)
    monkeypatch.chdir(worktree)
    state = _protect_state_with_claim(
        _protect_active_claim("Grok sess-1", branch="codex/issue-72-widget")
    )
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)
    target = worktree / "docs" / "widget.md"

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(target)}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")


def test_protect_denies_a_path_outside_every_repository_as_such(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #314's own second case: a payload path whose directory sits
    outside every git repository denies with its own named reason, never
    "path required" (which would suggest the payload itself was malformed)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    outside = tmp_path / "not-a-repository"
    outside.mkdir()
    monkeypatch.chdir(outside)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": str(outside / "widget.py")}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="not in a repository")


def test_protect_apply_patch_judges_two_worktrees_separately_and_one_deny_wins(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #314's own third proof: an `apply_patch` call touching two real
    linked worktrees of the same repository judges each path in its own
    checkout -- the first worktree holds a covering claim, the second holds
    none at all, and the second's denial wins."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _use_real_path_is_tracked(monkeypatch)
    _main, claimed_worktree = _protect_real_repo_with_worktree(tmp_path)
    _main2, unclaimed_worktree = _protect_real_repo_with_worktree(tmp_path, slug="issue-90-other")
    monkeypatch.chdir(tmp_path)
    state = _protect_state_with_claim(
        _protect_active_claim("Grok sess-1", branch="codex/issue-72-widget")
    )
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)
    command = _patch_command(
        f"*** Update File: {claimed_worktree / 'src' / 'widget.py'}",
        "@@",
        "-old",
        "+new",
        f"*** Update File: {unclaimed_worktree / 'src' / 'other.py'}",
        "@@",
        "-old",
        "+new",
    )

    assert (
        _protect_main(monkeypatch, {"toolName": "apply_patch", "toolInput": {"command": command}})
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")


@pytest.mark.parametrize(
    "cwd_kind", ["claimed_worktree", "foreign_tmp_dir", "foreign_main_checkout"]
)
def test_rescope_succeeds_from_every_cwd_when_the_add_path_is_absolute(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    cwd_kind: str,
) -> None:
    """Issue #314's own fourth proof, as sharpened by the repeat gate
    (finding R1): every `--add`/`--drop` entry must itself be absolute, from
    any cwd -- `rescope` never falls back to interpreting one against the
    calling process's own cwd, not even from the claimed worktree itself.
    The same absolute `--add` path locates the claimed worktree's own
    checkout from the worktree itself, an unrelated tmp directory outside
    every repository, and the shared main checkout alike: the one location
    signal a dispatcher in the head's own shared environment (editing a
    linked worktree through a subagent) can give without knowing its cwd."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})
    _use_real_path_is_tracked(monkeypatch)
    main, worktree = _protect_real_repo_with_worktree(tmp_path)
    add = str(worktree / "docs" / "widget.md")
    if cwd_kind == "claimed_worktree":
        cwd = worktree
    elif cwd_kind == "foreign_main_checkout":
        cwd = main
    else:
        cwd = tmp_path / "elsewhere"
        cwd.mkdir()
    monkeypatch.chdir(cwd)
    claimed = _protect_active_claim(
        "Codex Sol", scope=("src/widget.py",), branch="codex/issue-72-widget"
    )
    state = _protect_state_with_claim(claimed)
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)
    monkeypatch.setattr(
        store,
        "commit_transition",
        lambda *, worktree, remote, subject, intent: protocol.apply(state, intent),
    )

    status = issue_claim.main(["rescope", "72", "--add", add])

    assert status == 0
    assert capsys.readouterr().out == f"RESCOPED issue #72: {claimed.claim_id}\n"


def _rescope_args_all_relative(tmp_path: Path) -> list[str]:
    _protect_real_repo_with_worktree(tmp_path)
    return ["rescope", "72", "--add", "docs/widget.md"]


def _rescope_args_mixed_absolute_and_relative(tmp_path: Path) -> list[str]:
    _main, worktree = _protect_real_repo_with_worktree(tmp_path)
    return [
        "rescope",
        "72",
        "--add",
        str(worktree / "docs" / "widget.md"),
        "--drop",
        "src/widget.py",
    ]


def test_rescope_rejects_a_wide_scope_from_a_foreign_cwd_via_the_resolved_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #314 repeat gate B4: the width guard's own directory classifier
    (`checkout._scope_directories`) must read the resolved checkout's own
    directory, never the calling process's cwd. From a foreign cwd -- which
    has no `docs` directory at all, and is not even a git checkout -- an
    absolute `--add` naming the claimed worktree's own `docs` directory
    still trips the same `--whole` refusal it would from inside that
    worktree, because `docs` is classified against the worktree via `-C`,
    not against the foreign cwd."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})
    _use_real_path_is_tracked(monkeypatch)
    _main, worktree = _protect_real_repo_with_worktree(tmp_path)
    foreign_cwd = tmp_path / "elsewhere"
    foreign_cwd.mkdir()
    monkeypatch.chdir(foreign_cwd)
    claimed = _protect_active_claim(
        "Codex Sol", scope=("src/widget.py",), branch="codex/issue-72-widget"
    )
    state = _protect_state_with_claim(claimed)
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)

    status = issue_claim.main(["rescope", "72", "--add", str(worktree / "docs")])

    assert status == 2
    err = capsys.readouterr().err
    assert "scope is wide" in err
    assert "--whole" in err


def test_protect_denies_a_relative_payload_path_even_from_the_claimed_worktree_cwd(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Finding R2: a relative payload path must never be guessed by joining
    it to the hook process's own cwd -- not even in the one case where doing
    so would happen to land on the right file (cwd already the claimed
    worktree, the historical vulnerable shape this whole item closes).
    Every provider `aco` supports sends an already-absolute `file_path`, so
    a relative one is untrustworthy on its own and denies outright,
    regardless of a live covering claim."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _main, worktree = _protect_real_repo_with_worktree(tmp_path)
    monkeypatch.chdir(worktree)
    state = _protect_state_with_claim(
        _protect_active_claim("Grok sess-1", branch="codex/issue-72-widget")
    )
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)

    assert (
        _protect_main(monkeypatch, {"toolName": "write", "toolInput": {"path": "src/widget.py"}})
        == 2
    )
    _assert_protect_decision(
        capsys, decision="deny", reason=issue_claim.RELATIVE_PAYLOAD_PATH_DENIAL
    )


def _real_main_checkout_target(tmp_path: Path) -> Path:
    """Finding R3's own proof target: a real main checkout's own file, no
    fake `_git_output` involved."""
    main, _worktree = _protect_real_repo_with_worktree(tmp_path)
    return main / "README.md"


def _real_worktree_on_default_branch_target(tmp_path: Path) -> Path:
    """Gate G4's own proof target: a real linked worktree checked out on the
    repository's own default branch -- possible once the main checkout
    moves off it first (git refuses the same branch checked out twice), then
    a worktree attaches the now-free branch directly rather than creating a
    new one, the shape a repository's default-branch change can leave
    behind."""
    main = tmp_path / "repo"
    main.mkdir()
    _real_git(main, "init", "-q", "-b", "main")
    _real_git(main, "config", "user.name", "Test")
    _real_git(main, "config", "user.email", "test@example.com")
    (main / "README.md").write_text("hello\n")
    _real_git(main, "add", "README.md")
    _real_git(main, "commit", "-q", "-m", "initial")
    _real_git(main, "remote", "add", "origin", "https://example.invalid/example/repo.git")
    _real_git(main, "update-ref", "refs/remotes/origin/main", "HEAD")
    _real_git(main, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/main")
    _real_git(main, "checkout", "-q", "-b", "codex/elsewhere")
    worktree = tmp_path / "repo-worktrees" / "issue-1-on-default"
    worktree.parent.mkdir(parents=True)
    _real_git(main, "worktree", "add", "-q", str(worktree), "main")
    return worktree / "README.md"


@pytest.mark.parametrize("payload_for", _TARGET_PATH_PAYLOAD_BUILDERS, ids=["write", "bash-rm"])
@pytest.mark.parametrize(
    "build_target",
    [_real_main_checkout_target, _real_worktree_on_default_branch_target],
    ids=["main-checkout", "linked-worktree-on-default-branch"],
)
def test_protect_denies_not_main_for_a_real_checkout(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    build_target: Callable[[Path], Path],
    payload_for: Callable[[Path], dict[str, object]],
) -> None:
    """Finding R3 and gate G4, against real checkouts rather than
    `_patch_protect_git`'s mock: a payload path in the real shared main
    checkout denies `not main` (R3 -- the previously required proof was
    mocked, never exercising `git worktree add`/`git -C`); so does one in a
    real linked worktree sitting on the repository's own default branch (G4
    -- `resolve_path_checkout`'s structural MAIN/LINKED_WORKTREE split alone
    stopped catching this once issue #314 dropped the old branch-name check,
    so a live claim matching that worktree's agent/branch/scope would
    otherwise be honoured there exactly as if it were a real lane). A
    Bash-recognized path runs the identical gate (issue #380)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    target = build_target(tmp_path)
    monkeypatch.chdir(tmp_path)
    _forbid_github_construction(monkeypatch)

    assert _protect_main(monkeypatch, payload_for(target)) == 2
    _assert_protect_decision(capsys, decision="deny", reason="not main")


@pytest.mark.parametrize("payload_for", _TARGET_PATH_PAYLOAD_BUILDERS, ids=["write", "bash-rm"])
def test_protect_denies_a_checkout_with_no_commit_yet(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload_for: Callable[[Path], dict[str, object]],
) -> None:
    """Gate G3: a freshly `git init`ed checkout with no commit yet -- an
    unborn branch -- still names a real branch (`git branch --show-current`
    reads the symbolic ref's target regardless of whether it resolves to a
    commit), so without a successful-HEAD requirement its name could
    coincidentally match a still-live claim's and be authorized despite
    naming no real history. A Bash-recognized path runs the identical gate
    (issue #380)."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _real_git(unborn, "init", "-q", "-b", "codex/issue-72-widget")
    monkeypatch.chdir(tmp_path)
    _forbid_github_construction(monkeypatch)
    target = unborn / "widget.py"

    assert _protect_main(monkeypatch, payload_for(target)) == 2
    _assert_protect_decision(capsys, decision="deny", reason=checkout.NO_COMMIT_CHECKOUT_REASON)


def test_protect_apply_patch_fetches_store_state_once_per_repository(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Gate G5: two payload paths in one `apply_patch` call, each in its own
    linked worktree of the same repository, are judged against one state
    snapshot -- fetched once for the repository (observed via a counting
    fake store, never by asserting mock call order), not once per path -- so
    a rescope that narrows coverage between two per-path fetches can never
    let each path pass against a different snapshot though no single live
    claim ever covered the whole patch."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.GROK_SESSION_ID_ENV: "sess-1"})
    _use_real_path_is_tracked(monkeypatch)
    _main, claimed_worktree = _protect_real_repo_with_worktree(tmp_path)
    _main2, unclaimed_worktree = _protect_real_repo_with_worktree(tmp_path, slug="issue-90-other")
    monkeypatch.chdir(tmp_path)
    state = _protect_state_with_claim(
        _protect_active_claim("Grok sess-1", branch="codex/issue-72-widget")
    )
    fetch_calls: list[Path] = []

    def counting_fetch_state(*, worktree: Path, remote: str) -> protocol.ClaimState:
        fetch_calls.append(worktree)
        return state

    monkeypatch.setattr(store, "fetch_state", counting_fetch_state)
    command = _patch_command(
        f"*** Update File: {claimed_worktree / 'src' / 'widget.py'}",
        "@@",
        "-old",
        "+new",
        f"*** Update File: {unclaimed_worktree / 'src' / 'other.py'}",
        "@@",
        "-old",
        "+new",
    )

    assert (
        _protect_main(monkeypatch, {"toolName": "apply_patch", "toolInput": {"command": command}})
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")
    assert len(fetch_calls) == 1


def _rescope_args_add_path_outside_the_first_paths_checkout(tmp_path: Path) -> list[str]:
    _main, worktree = _protect_real_repo_with_worktree(tmp_path)
    _main2, other_worktree = _protect_real_repo_with_worktree(tmp_path, slug="issue-90-other")
    return [
        "rescope",
        "72",
        "--add",
        str(worktree / "docs" / "widget.md"),
        "--add",
        str(other_worktree / "src" / "other.py"),
    ]


def _rescope_args_add_path_outside_any_repository(tmp_path: Path) -> list[str]:
    outside = tmp_path / "outside"
    outside.mkdir()
    return ["rescope", "72", "--add", str(outside / "file.py")]


def _rescope_args_add_path_in_an_unborn_checkout(tmp_path: Path) -> list[str]:
    unborn = tmp_path / "unborn"
    unborn.mkdir()
    _real_git(unborn, "init", "-q", "-b", "codex/issue-72-widget")
    return ["rescope", "72", "--add", str(unborn / "widget.py")]


@pytest.mark.parametrize(
    ("build_args", "expected_error_fragment"),
    [
        (
            _rescope_args_add_path_outside_the_first_paths_checkout,
            "is outside the resolved checkout",
        ),
        (_rescope_args_add_path_outside_any_repository, "not in a repository"),
        (_rescope_args_add_path_in_an_unborn_checkout, checkout.NO_COMMIT_CHECKOUT_REASON),
        (_rescope_args_all_relative, issue_claim.RELATIVE_PAYLOAD_PATH_DENIAL),
        (_rescope_args_mixed_absolute_and_relative, issue_claim.RELATIVE_PAYLOAD_PATH_DENIAL),
    ],
    ids=[
        "second-add-path-outside-checkout",
        "outside-any-repository",
        "checkout-has-no-commit",
        "all-relative",
        "mixed-absolute-and-relative",
    ],
)
def test_rescope_denies_before_touching_the_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    build_args: Callable[[Path], list[str]],
    expected_error_fragment: str,
) -> None:
    """`rescope`'s own new checkout-resolution paths (issue #314 delta and
    repeat gate, finding R1): a second `--add`/`--drop` path outside the
    first path's own checkout (`_rescope_scope_entries`) refuses rather than
    silently mis-scoping; a location outside every repository, and gate
    G3's no-commit checkout (`_rescope_checkout`); and a relative
    `--add`/`--drop` entry, alone or mixed with an absolute one, denies with
    the same sentence `protect`'s own relative-payload-path gate uses,
    never falling back to interpreting it against the hook process's own
    cwd -- all four refuse before the store is ever touched."""
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})
    args = build_args(tmp_path)

    status = issue_claim.main(args)

    assert status == 2
    assert expected_error_fragment in capsys.readouterr().err
