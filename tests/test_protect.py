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
from pathlib import Path

import pytest
from board_fixtures import BASE, REPOSITORY, _active_claim
from cli_fixtures import (
    _assert_missing_identity_message,
    _fallback_git_output,
    _forbid_forge_resolution,
    _forbid_git_fill,
    _forbid_github_construction,
    _forbid_protect_git_github_and_identity,
    _patch_command,
    _set_agent_identity_env,
)

from agent_coordination import board, checkout, protocol, store
from agent_coordination import cli as issue_claim
from agent_coordination.protocol import ClaimError


def _isolate_protect_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> tuple[Path, Path]:
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    return home, work


def _protect_git_values(
    work: Path, overrides: dict[tuple[str, ...], str] | None = None
) -> dict[tuple[str, ...], str]:
    values: dict[tuple[str, ...], str] = {
        ("branch", "--show-current"): "codex/issue-72-claims",
        ("rev-parse", "--git-dir"): str(work / ".git" / "worktrees" / "issue-72"),
        ("rev-parse", "--git-common-dir"): str(work / ".git"),
        ("rev-parse", "--show-toplevel"): str(work.resolve()),
        # The canonical-remote comparison (issue #176, Erwartung 6) reads this
        # to confirm the fake forge target (REPOSITORY) matches it.
        ("config", "--get", "remote.origin.url"): f"git@github.com:{REPOSITORY}.git",
        # `protect`'s "not main" check reads the default branch through the
        # same `origin/HEAD` owner `claim` uses (issue #238).
        ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main",
    }
    if overrides:
        values.update(overrides)
    return values


def _patch_protect_git(
    monkeypatch: pytest.MonkeyPatch,
    work: Path,
    overrides: dict[tuple[str, ...], str] | None = None,
) -> None:
    values = _protect_git_values(work, overrides)

    def git(arguments: list[str]) -> str:
        if arguments == ["status", "--porcelain"]:
            pytest.fail("dirty tree is irrelevant to protect")
        if arguments == ["rev-parse", "HEAD"]:
            pytest.fail("protect must not bind HEAD to claim.base")
        return values[tuple(arguments)]

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


def test_protect_allowed_write_resolves_identity_then_git_then_store(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    calls: list[str] = []

    resolve_agent = checkout._resolved_agent

    def resolved_agent(explicit: str | None) -> str:
        calls.append("identity")
        return resolve_agent(explicit)

    monkeypatch.setattr(checkout, "_resolved_agent", resolved_agent)

    git_values = _protect_git_values(work)

    def git(arguments: list[str]) -> str:
        calls.append("git")
        return git_values[tuple(arguments)]

    monkeypatch.setattr(checkout, "_git_output", git)

    state = _protect_state_with_claim(_protect_active_claim("Grok sess-1"))

    def fake_fetch_state(*, worktree: Path, remote: str) -> protocol.ClaimState:
        calls.append("store")
        return state

    monkeypatch.setattr(store, "fetch_state", fake_fetch_state)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")
    assert calls == ["identity", "git", "git", "git", "git", "git", "store"]


@pytest.mark.parametrize(
    "payload",
    [
        {"toolName": "Bash", "toolInput": {"path": "src/cli.py", "command": "rm -rf /"}},
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
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": tool_name, "toolInput": {path_key: "src/widget.py"}},
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
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work, {("branch", "--show-current"): "docs/lane-cleanup"})
    _patch_protect_claim(monkeypatch, branch="docs/lane-cleanup", lane=True)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
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
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, agent="Codex Sol")

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
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
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
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
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
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
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    state = protocol.ClaimState(tip=protocol.ObjectId(BASE))
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
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
    ("branch", "origin_head"),
    [
        ("main", "refs/remotes/origin/main"),
        ("master", "refs/remotes/origin/master"),
        ("trunk", "refs/remotes/origin/trunk"),
    ],
)
def test_protect_main_branch_denies_without_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    branch: str,
    origin_head: str,
) -> None:
    """`protect` reads the repository's default branch the same way `claim`
    does (issue #238): a repository whose `origin/HEAD` names `trunk` denies
    a write from `trunk`, not just from the hardcoded `main`/`master`."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(
        monkeypatch,
        work,
        {
            ("branch", "--show-current"): branch,
            ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): origin_head,
        },
    )
    _forbid_github_construction(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="not main")


@pytest.mark.parametrize(
    ("branch", "denied"),
    [("main", True), ("master", True), ("trunk", False)],
)
@pytest.mark.parametrize("origin_head_empty", [False, True], ids=["raises", "empty"])
def test_protect_default_branch_fallback_denies_only_main_and_master(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    origin_head_empty: bool,
    branch: str,
    denied: bool,
) -> None:
    """Same fallback pin as `claim`'s (issue #238, Grok review) at `protect`'s
    own "not main" gate: when `origin/HEAD` cannot be resolved, `main` and
    `master` are still denied by the historical two-name guess and `trunk` is
    not, whether the unresolved symbolic ref raises (git's real shape,
    measured locally) or resolves to an empty name."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    values = _protect_git_values(work, {("branch", "--show-current"): branch})
    monkeypatch.setattr(
        checkout, "_git_output", _fallback_git_output(values, origin_head_empty=origin_head_empty)
    )

    if denied:
        _forbid_github_construction(monkeypatch)
        assert (
            _protect_main(
                monkeypatch,
                {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
            )
            == 2
        )
        _assert_protect_decision(capsys, decision="deny", reason="not main")
        return

    _patch_protect_claim(monkeypatch, branch=branch)
    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


@pytest.mark.parametrize(
    "payload",
    [
        {
            "toolName": "apply_patch",
            "toolInput": {
                "command": _patch_command("*** Update File: src/widget.py", "@@", "-old", "+new")
            },
        },
        {
            "tool_name": "NotebookEdit",
            "tool_input": {
                "notebook_path": "notebook.ipynb",
                "new_source": "print(1)",
                "cell_type": "code",
                "edit_mode": "replace",
            },
        },
    ],
)
def test_protect_extended_mutating_tools_deny_on_main_without_a_claim(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    payload: dict[str, object],
) -> None:
    """`apply_patch` (Codex) and `NotebookEdit` (Claude Code) joined the
    mutating table (issue #238): both are gated exactly like `Write`, denied
    from `main` before a claim is even looked up."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work, {("branch", "--show-current"): "main"})
    _forbid_github_construction(monkeypatch)

    assert _protect_main(monkeypatch, payload) == 2
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
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)

    exit_code = _protect_main(
        monkeypatch,
        {
            "tool_name": "NotebookEdit",
            "tool_input": {
                "notebook_path": notebook_path,
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
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)

    exit_code = _protect_main(
        monkeypatch,
        {
            "tool_name": "NotebookEdit",
            "tool_input": {
                "path": "src/widget.py",
                "notebook_path": "docs/widget.ipynb",
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
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)
    command = _patch_command(
        "*** Update File: src/widget.py",
        "@@",
        "-old",
        "+new",
        "*** Add File: src/new_module.py",
        "+content",
    )

    assert (
        _protect_main(monkeypatch, {"toolName": "apply_patch", "toolInput": {"command": command}})
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")


@pytest.mark.parametrize(
    ("lines", "outside_path"),
    [
        (
            (
                "*** Update File: src/widget.py",
                "@@",
                "-old",
                "+new",
                "*** Add File: docs/widget.md",
                "+content",
            ),
            "docs/widget.md",
        ),
        (
            (
                "*** Update File: src/widget.py",
                "*** Move to: docs/widget.py",
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
    lines: tuple[str, ...],
    outside_path: str,
) -> None:
    """A multi-file `apply_patch` call names the specific file outside the
    claim scope -- unlike a single-path write's generic `claim first` -- since
    the hook payload doesn't otherwise say which of several files was the
    problem (issue #252). Covers both a plain outside path and a `Move to:`
    rename landing outside scope."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)

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
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, scope=("README.md",))
    command = _patch_command(
        "*** Add File: README.md",
        "+content",
        "  *** Update File: docs/evil.md",
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
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, agent="Codex Sol")
    command = _patch_command("*** Update File: src/widget.py", "@@", "-old", "+new")

    assert (
        _protect_main(monkeypatch, {"toolName": "apply_patch", "toolInput": {"command": command}})
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")


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


def test_protect_primary_checkout_denies_worktree_without_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    git_directory = str(work / ".git")
    _patch_protect_git(
        monkeypatch,
        work,
        {
            ("rev-parse", "--git-dir"): git_directory,
            ("rev-parse", "--git-common-dir"): git_directory,
        },
    )
    _forbid_github_construction(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="worktree")


def test_protect_path_outside_repository_denies_path_required(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _forbid_github_construction(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {
                "toolName": "write",
                "toolInput": {"path": str(tmp_path / "outside.py")},
            },
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="path required")


def test_protect_wrong_branch_denies_claim_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, branch="other/issue-72")

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")


def test_protect_non_overlapping_scope_denies_claim_first(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, scope=("docs",))

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")


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
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)

    def failed(*_args: object, **_kwargs: object) -> board.BoardConfig:
        raise ClaimError("adapter failed")

    monkeypatch.setattr(board, "load_config", failed)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
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
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)

    def crashed(*_args: object, **_kwargs: object) -> board.BoardConfig:
        raise RuntimeError("write path crashed")

    monkeypatch.setattr(board, "load_config", crashed)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
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
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)

    def fake_fetch_state(*, worktree: Path, remote: str) -> protocol.ClaimState:
        raise failure

    monkeypatch.setattr(store, "fetch_state", fake_fetch_state)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 2
    )
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
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)

    def fake_fetch_state(*, worktree: Path, remote: str) -> protocol.ClaimState:
        raise ClaimError("cannot fetch")

    monkeypatch.setattr(store, "fetch_state", fake_fetch_state)
    command = _patch_command("*** Update File: src/widget.py", "@@", "-old", "+new")

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
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    monkeypatch.setattr(store, "fetch_state", lambda **_k: protocol.EMPTY_STATE)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
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
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch)
    _forbid_forge_resolution(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
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
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    _patch_protect_claim(monkeypatch, agent="Codex Sol")
    _forbid_forge_resolution(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="claim first")
