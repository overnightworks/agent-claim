from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import re
import runpy
import shlex
import sys
import threading
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from pathlib import Path
from types import MappingProxyType

import pytest
from board_fixtures import (
    BASE,
    FROZEN_TRIGGER,
    FROZEN_UNTIL,
    MINIMAL_BLOCK_TOML,
    REPOSITORY,
    RULED_ON,
    _active_claim,
    _store_claim_from_request,
    agent_claim_body,
    block_dependency,
    blocked_issue,
    board_issue,
    complete_contract,
    idea_body,
    projected_board,
    proposed_expectation,
    request,
    ruled_expectation,
    slice_entries,
)
from cli_fixtures import (
    _assert_missing_identity_message,
    _forbid_forge_resolution,
    _forbid_git_fill,
    _forbid_github_construction,
    _forbid_protect_git_github_and_identity,
    _git_checkout,
    _push_repository_trunk,
    _real_git,
    _real_repository_with_bare_remote,
    _set_agent_identity_env,
    _stub_one_git_call,
    arrange_scope_width,
    stub_board_config_tracked,
)
from github_fixtures import LANDING_BRANCH, MERGE_COMMIT_SHA, WORK_ITEM_ISSUE

from agent_coordination import (
    __version__,
    board,
    body,
    checkout,
    forge,
    github,
    items,
    metrics,
    protocol,
    state_board,
    store,
)
from agent_coordination import cli as issue_claim
from agent_coordination.cli import _status, _status_json
from agent_coordination.protocol import (
    ClaimError,
    ClaimRequest,
    ClaimUnavailableError,
    IssueIdentity,
)

GitHubForge = github.GitHubForge

# Captured before the autouse `_stub_trunk_landings` fixture (below) ever
# monkeypatches `checkout.trunk_landings` to `()`: the atomic-landing tests
# (issue #359) need the real first-parent walk against a real repository,
# the same way `test_checkout.py`'s own `_LIVE_TRUNK_LANDINGS` does.
_LIVE_TRUNK_LANDINGS = checkout.trunk_landings

LANDED = protocol.MergedRelease(12)

# Exactly `protocol.WIDE_SCOPE_SHARE_FLOOR` versioned files: three named scope
# paths (LICENSE, README.md, src) cover four of them (src holds two), the
# minimal fixture that still trips the share condition (issue #163).
TWELVE_VERSIONED_FILES = (
    "LICENSE",
    "README.md",
    "pyproject.toml",
    "src/agent_coordination/__init__.py",
    "src/a.py",
    "docs/b.md",
    "docs/c.md",
    "docs/d.md",
    "docs/e.md",
    "docs/f.md",
    "docs/g.md",
    "docs/h.md",
)


def issue_number(identity: protocol.ClaimIdentity) -> int:
    """The numbered-issue identity's issue number. Every call site here builds an
    issue-scoped claim (`request(issue=...)`, never `lane=True`), so a `LaneIdentity`
    reaching this helper is a real defect in the calling test, not a case to
    tolerate."""
    assert isinstance(identity, IssueIdentity)
    return identity.issue


def _live_store_claim() -> protocol.ActiveClaim:
    """The single live claim on the in-memory store fake."""
    state = store.fetch_state(worktree=Path("."), remote="origin")
    assert len(state.claims) == 1
    return next(iter(state.claims.values()))


@dataclass
class FakeForge:
    board_issues: tuple[board.Issue, ...] = ()
    board_open_pull_requests: tuple[board.PullRequest, ...] = ()
    board_merged_pull_requests: tuple[board.PullRequest, ...] = ()
    board_dependencies: dict[int, tuple[board.IssueDependency, ...]] = field(default_factory=dict)
    repository: forge.RepositoryId = field(
        default_factory=lambda: github._repository_id(REPOSITORY)
    )
    default_branch_name: str = "main"
    landings: dict[int, forge.Landing] = field(default_factory=dict)
    parents: dict[int, board.ParentIssue] = field(default_factory=dict)
    children: dict[int, tuple[board.ChildItem, ...]] = field(default_factory=dict)
    closed_issues: set[int] = field(default_factory=set)
    landing_comments: dict[int, str] = field(default_factory=dict)
    issue_references: dict[int, forge.ItemReference] = field(default_factory=dict)
    issue_reference_lookups: list[int] = field(default_factory=list)
    created_children: list[tuple[int, str, str, body.ItemKind]] = field(default_factory=list)
    created_issues: list[tuple[str, str, body.ItemKind]] = field(default_factory=list)
    linked_children: list[tuple[int, int]] = field(default_factory=list)
    next_created_child_number: int = 900
    item_bodies: dict[int, str] = field(default_factory=dict)
    fail_update_item_body: bool = False
    fail_create_child_relation: bool = False
    capability_overrides: dict[forge.ForgeOperation, forge.Capability] = field(default_factory=dict)
    readiness_by_number: dict[int, forge.LandingReadiness] = field(default_factory=dict)
    merge_calls: list[tuple[int, str, str, str]] = field(default_factory=list)
    merge_sha: str = MERGE_COMMIT_SHA
    merge_repository: Path | None = None
    fail_merge: ClaimError | None = None
    deleted_branches: list[str] = field(default_factory=list)
    requests: int = field(default=0, init=False)
    _requests_lock: threading.Lock = field(
        default_factory=threading.Lock, init=False, repr=False, compare=False
    )

    def _run(self) -> None:
        """This fake's mirror of `GitHubForge._run` (issue #168): every board
        read that would cost a real round trip calls this once, so a test can
        assert `requests` against a hand-counted expectation the same way it
        would against the real adapter. Locked for the same reason: `board`
        fans these reads out across worker threads."""
        with self._requests_lock:
            self.requests += 1

    def capability(self, operation: forge.ForgeOperation) -> forge.Capability:
        return self.capability_overrides.get(operation, github.GITHUB_CAPABILITIES[operation])

    def _create_issue(self, *, title: str, body: str, kind: body.ItemKind) -> int:
        """This fake's mirror of `GitHubForge._create_issue`: a fresh issue
        with no recorded parent, immediately visible to
        `list_open_board_issues` -- the orphan shape a failed `link_child`
        leaves behind (#260). Carries `kind` (#260 Sonnet finding), since a
        repeat `cut`'s orphan scan refuses to adopt anything but a `TASK`."""
        number = self.next_created_child_number
        self.next_created_child_number += 1
        self.created_issues.append((title, body, kind))
        self.board_issues = (*self.board_issues, board_issue(number, title, body, kind=kind))
        return number

    def link_child(self, parent: int, child: int) -> None:
        """This fake's mirror of `GitHubForge.link_child`: records `child` as
        `parent`'s open sub-issue, so a later `list_children`/`parent_issue`
        call sees it exactly as real GitHub would after the sub-issue POST
        succeeds. `fail_create_child_relation` simulates that POST itself
        failing, leaving `child` the orphan a repeat `cut` must adopt (#260).
        """
        self.linked_children.append((parent, child))
        if self.fail_create_child_relation:
            raise ClaimError("relation POST failed (simulated)")
        self.children[parent] = (
            *self.children.get(parent, ()),
            board.ChildItem(child, board.ChildState.OPEN),
        )
        self.parents[child] = board.ParentIssue(
            board.IssueReference(self.repository.path, parent), ""
        )

    def create_child(self, *, parent: int, title: str, body: str, kind: body.ItemKind) -> int:
        """This fake's mirror of `GitHubForge.create_child`: composed from
        `create_issue` and `link_child` exactly as the real adapter is
        (#260), so a relation failure leaves the same real orphan behind
        for a repeat `cut` to find."""
        self.created_children.append((parent, title, body, kind))
        number = self._create_issue(title=title, body=body, kind=kind)
        try:
            self.link_child(parent, number)
        except ClaimError as error:
            raise forge.ForgePartialChildCreationError(
                child=number,
                parent=parent,
                step=f"record #{number} as a sub-issue of #{parent}",
                cause=error,
            ) from error
        return number

    def update_item_body(self, number: int, body: str) -> None:
        if self.fail_update_item_body:
            raise ClaimError("update item body failed (simulated)")
        self.item_bodies[number] = body

    def close_landed_item(self, number: int, *, pull_request: int) -> None:
        """This fake's mirror of `GitHubForge.close_landed_item` (issue
        #359 Card 1): records the comment and the close as state, the same
        two facts a test asserts against the real adapter's own two `_run`
        calls."""
        self.landing_comments[number] = github.landing_comment(pull_request)
        self.closed_issues.add(number)

    def landing_readiness(self, number: int) -> forge.LandingReadiness:
        """This fake's mirror of `GitHubForge.landing_readiness` (issue
        #405): a test seeds `readiness_by_number` directly, one
        `forge.LandingReadiness` per scenario, rather than reconstructing it
        from other fields."""
        self._run()
        readiness = self.readiness_by_number.get(number)
        if readiness is None:
            raise ClaimError(f"GitHub has no readiness for pull request #{number}")
        return readiness

    def merge_landing(self, number: int, *, head_sha: str, title: str, body: str) -> str:
        """This fake's mirror of `GitHubForge.merge_landing` (issue #405):
        records every call for an adapter-shaped assertion, and, when
        `merge_repository` names a real checkout (`land`'s own end-to-end
        tests), performs a real merge there so a later real fast-forward has
        a fresh trunk tip to advance to."""
        self._run()
        self.merge_calls.append((number, head_sha, title, body))
        if self.fail_merge is not None:
            raise self.fail_merge
        sha = self.merge_sha
        landing = self.landings[number]
        if self.merge_repository is not None:
            _real_git(
                self.merge_repository,
                "merge",
                "--no-ff",
                "-m",
                f"{title}\n\n{body}",
                landing.source_branch,
            )
            sha = _real_git(self.merge_repository, "rev-parse", "HEAD").stdout.strip()
            _real_git(self.merge_repository, "push", "-q", "origin", "main")
        self.landings[number] = replace(landing, merged=True, merge_commit=sha)
        return sha

    def delete_branch(self, branch: str) -> None:
        self._run()
        self.deleted_branches.append(branch)

    def list_open_board_issues(self) -> tuple[board.Issue, ...]:
        self._run()
        return self.board_issues

    def landing(self, number: int) -> forge.Landing:
        self._run()
        detail = self.landings.get(number)
        if detail is None:
            raise ClaimError(f"GitHub has no pull request #{number}")
        return detail

    def item_reference(self, number: int) -> forge.ItemReference:
        self._run()
        self.issue_reference_lookups.append(number)
        served = self.issue_references.get(number)
        if served is not None:
            return served
        state = forge.ItemState.CLOSED if number in self.closed_issues else forge.ItemState.OPEN
        # `landings` is this fake's set of pull requests, so the one flag that
        # distributes `check` is derived from it rather than set twice.
        return forge.ItemReference(state, "", "", number in self.landings)

    def default_branch(self) -> str:
        self._run()
        return self.default_branch_name

    def parent_issue(self, number: int) -> board.ParentIssue | None:
        self._run()
        return self.parents.get(number)

    def list_children(self, number: int) -> tuple[board.ChildItem, ...]:
        self._run()
        return self.children.get(number, ())

    def list_board_dependencies(self, number: int) -> tuple[board.IssueDependency, ...]:
        self._run()
        return self.board_dependencies.get(number, ())

    def list_open_board_pull_requests(self) -> tuple[board.PullRequest, ...]:
        self._run()
        return self.board_open_pull_requests

    def list_recent_merged_board_pull_requests(
        self, since: datetime
    ) -> tuple[board.PullRequest, ...]:
        self._run()
        return self.board_merged_pull_requests


class ReaderOnlyForge(FakeForge):
    """A `FakeForge` whose write operations fail the test instead of quietly
    succeeding -- the enforcement that a read-only command never writes,
    independent of the `ForgeReader`/`ForgeWriter` annotations (documentation
    only; nothing type-checks in CI)."""

    def _create_issue(self, *, title: str, body: str, kind: body.ItemKind) -> int:
        pytest.fail("a read-only command must never create an issue")

    def link_child(self, parent: int, child: int) -> None:
        pytest.fail("a read-only command must never link a child")

    def create_child(self, *, parent: int, title: str, body: str, kind: body.ItemKind) -> int:
        pytest.fail("a read-only command must never create a child")

    def update_item_body(self, number: int, body: str) -> None:
        pytest.fail("a read-only command must never update an item body")

    def close_landed_item(self, number: int, *, pull_request: int) -> None:
        pytest.fail("a read-only command must never close a landed item")

    def merge_landing(self, number: int, *, head_sha: str, title: str, body: str) -> str:
        pytest.fail("a read-only command must never merge a pull request")

    def delete_branch(self, branch: str) -> None:
        pytest.fail("a read-only command must never delete a branch")


@dataclass
class _MinimalForgeReader:
    """A `forge.ForgeReader` shape narrower than `FakeForge` for tests that
    exercise only `_board`'s own priority/child-fetch wiring: no request
    counter, no write surface, no repository target resolution -- just the
    open issues, children, and merged-PR floor a `_board` build reads. One
    shared shape rather than three near-identical inline classes, each
    repeating the same `ForgeReader` stub methods."""

    open_issues: tuple[board.Issue, ...] = ()
    children_by_number: dict[int, tuple[board.ChildItem, ...]] = field(default_factory=dict)
    repository: forge.RepositoryId = field(
        default_factory=lambda: github._repository_id(REPOSITORY)
    )
    requests: int = 0
    observed_children_lookups: list[int] = field(default_factory=list)
    observed_merged_pull_request_floors: list[datetime] = field(default_factory=list)

    def capability(self, operation: forge.ForgeOperation) -> forge.Capability:
        return github.GITHUB_CAPABILITIES[operation]

    def item_reference(self, number: int) -> forge.ItemReference:
        return forge.ItemReference(state=forge.ItemState.MISSING)

    def landing(self, number: int) -> forge.Landing:
        raise NotImplementedError

    def parent_issue(self, number: int) -> board.ParentIssue | None:
        return None

    def default_branch(self) -> str:
        return "main"

    def list_board_dependencies(self, number: int) -> tuple[board.IssueDependency, ...]:
        return ()

    def list_open_board_issues(self) -> tuple[board.Issue, ...]:
        return self.open_issues

    def list_open_board_pull_requests(self) -> tuple[board.PullRequest, ...]:
        return ()

    def list_recent_merged_board_pull_requests(
        self, since: datetime
    ) -> tuple[board.PullRequest, ...]:
        self.observed_merged_pull_request_floors.append(since)
        return ()

    def list_children(self, number: int) -> tuple[board.ChildItem, ...]:
        self.observed_children_lookups.append(number)
        return self.children_by_number.get(number, ())


_LIVE_FETCH_ISSUE_REFERENCE = issue_claim._fetch_issue_reference


def test_read_only_commands_never_write_through_a_reader_only_forge(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = ReaderOnlyForge()
    client.board_issues = (board_issue(72, "Work", complete_contract("Ship it.")),)
    client.landings[12] = landing_pull_request(body="Work-Item: #72\n\nCloses #72")
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: "")
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())

    for argv in (
        ["status"],
        ["status", "--path", "src"],
        ["board"],
        ["next"],
        ["rulings"],
        ["check", "12"],
    ):
        issue_claim.main(["--repo", REPOSITORY, *argv])
        capsys.readouterr()


def _board_fixture_environment(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    issues_json = [
        {
            "number": 10,
            "title": "Security boundary",
            "labels": ["security"],
            "body": complete_contract("Land #10.", now="Inspect.", done_when="Merged."),
            "createdAt": "2026-08-10T00:00:00Z",
            "updatedAt": "2026-08-20T00:00:00Z",
            "blockedByCount": 0,
        },
        {
            "number": 11,
            "title": "Product dependency",
            "labels": ["product"],
            "body": complete_contract(
                "Review implementation.", now="Implement.", done_when="Released."
            ),
            "createdAt": "2026-08-12T00:00:00Z",
            "updatedAt": "2026-08-20T00:00:00Z",
            "blockedByCount": 1,
        },
        {
            "number": 12,
            "title": "Old notes",
            "labels": ["ux"],
            "body": "Unstructured notes.",
            "createdAt": "2026-08-01T00:00:00Z",
            "updatedAt": "2026-08-10T00:00:00Z",
            "blockedByCount": 0,
        },
        {
            "number": 13,
            "title": "Cleanup landed",
            "labels": ["cleanup"],
            "body": complete_contract("Close issue.", now="Verify.", done_when="Released."),
            "createdAt": "2026-08-02T00:00:00Z",
            "updatedAt": "2026-08-19T00:00:00Z",
            "blockedByCount": 0,
        },
        {
            "number": 14,
            "title": "Older cleanup",
            "labels": ["cleanup"],
            "body": "Unstructured notes.",
            "createdAt": "2026-08-02T00:00:00Z",
            "updatedAt": "2026-08-20T00:00:00Z",
            "blockedByCount": 0,
        },
    ]
    open_prs_json = [
        {"number": 90, "title": "Fixes #10", "body": "", "headRefName": "other", "mergedAt": None},
        {
            "number": 91,
            "title": "In progress",
            "body": "",
            "headRefName": "codex/issue-11-claims",
            "mergedAt": None,
        },
        {
            "number": 93,
            "title": "Planning note",
            "body": None,
            "headRefName": "notes",
            "mergedAt": None,
        },
    ]
    merged_prs_json = [
        {
            "number": 92,
            "title": "Fixes #13",
            "body": "",
            "headRefName": "codex/issue-13-cleanup",
            "mergedAt": "2026-08-20T12:00:00Z",
        },
        {
            "number": 94,
            "title": "Fixes #14",
            "body": "",
            "headRefName": "codex/issue-14-cleanup",
            "mergedAt": "2026-08-06T23:59:59Z",
        },
    ]
    active = request("board-claim", issue=11, branch="codex/issue-11-claims")
    repository = github._repository_id(REPOSITORY)
    observed: list[list[str]] = []

    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        assert input_data is None
        observed.append(arguments)
        endpoint = next((argument for argument in arguments if argument.startswith("repos/")), "")
        if "/issues?" in endpoint:
            rows = issues_json
        elif endpoint.startswith(f"repos/{repository}/issues/11/dependencies/blocked_by"):
            rows = [
                {
                    "number": 10,
                    "state": "open",
                    "closedAt": None,
                    "repository": str(repository),
                    "isPullRequest": False,
                }
            ]
        elif arguments[:2] == ["pr", "list"] and "open" in arguments:
            rows = open_prs_json
        elif arguments[:2] == ["pr", "list"] and "merged" in arguments:
            day = arguments[arguments.index("--search") + 1].removeprefix("merged:")
            rows = [row for row in merged_prs_json if row["mergedAt"].startswith(day)]
        else:
            pytest.fail(f"unexpected board request: {arguments}")
        return "\n".join(json.dumps(row) for row in rows)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 8, 21, tzinfo=UTC)

    client = GitHubForge(repository, run=run)
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(issue_claim, "datetime", FixedDateTime)
    monkeypatch.setattr(github, "datetime", FixedDateTime)
    _patch_store_write(monkeypatch, _store_claim_from_request(active))
    return observed


def test_board_json_shards_the_merged_pull_request_query_by_day_without_writes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    observed = _board_fixture_environment(monkeypatch)

    assert issue_claim.main(["--repo", REPOSITORY, "board", "--json"]) == 0
    capsys.readouterr()
    assert all("--method" not in arguments for arguments in observed)
    assert all("--jq" in arguments for arguments in observed)
    merged_days = {
        arguments[arguments.index("--search") + 1].removeprefix("merged:")
        for arguments in observed
        if arguments[:2] == ["pr", "list"] and "merged" in arguments
    }
    # The floor is the oldest open issue's creation (#12, 2026-08-01), not a
    # fixed 14 days back — nothing merged before #12 existed could touch any
    # currently open issue. Each day between that floor and "now" is its own
    # query shard (`github._query_days`), fetched in parallel.
    assert merged_days == {
        day.isoformat() for day in github._query_days(date(2026, 8, 1), date(2026, 8, 21))
    }


def test_board_projects_fixture_json_without_github_writes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _board_fixture_environment(monkeypatch)

    assert issue_claim.main(["--repo", REPOSITORY, "board", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "ok",
        "reason",
        "items",
        "ready_now",
        "stale",
        "recovery",
        "landings",
        "uncut",
        "requests",
        "measurements",
    }
    assert (payload["ok"], payload["reason"]) == (True, "projected")
    first = payload["items"][0]
    ten = next(item for item in payload["items"] if item["number"] == 10)
    eleven = next(item for item in payload["items"] if item["number"] == 11)
    thirteen = next(item for item in payload["items"] if item["number"] == 13)
    fourteen = next(item for item in payload["items"] if item["number"] == 14)
    assert first["number"] == 10
    assert ten["stage"] == "in-flight"
    assert ten["unblocks_count"] == 1
    assert ten["contract"]["next"] == "Land #10."
    assert ten["contract_complete"] is True
    assert ten["actionable"] is True
    assert ten["actionable_reason"] is None
    assert eleven["active_claim"] == "Codex Sol (builder)"
    assert eleven["actionable_reason"] == "claimed"
    assert thirteen["stage"] == "code-landed"
    # #94 "Fixes #14" merged 2026-08-06, five days before the old fixed
    # 14-day floor (2026-08-07) would have admitted it — the oldest-open-
    # issue floor (2026-08-01) correctly still counts it.
    assert fourteen["stage"] == "code-landed"
    assert fourteen["actionable_reason"] == "body malformed: agent-claim: no agent-claim block"
    assert [item["number"] for item in payload["ready_now"]] == [10, 13]
    assert [item["number"] for item in payload["stale"]] == [12]
    assert next(item for item in payload["items"] if item["number"] == 12)["stage"] == "text-only"
    assert 11 not in [item["number"] for item in payload["ready_now"]]


def test_board_json_pins_the_raw_envelope_text_for_a_single_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The raw bytes `board` prints on success (OUT-nn's own key order,
    `ok`/`reason` first, and its trailing newline), not just the parsed dict
    `test_board_projects_fixture_json_without_github_writes` already covers."""
    _single_item_board_environment(monkeypatch, tmp_path)

    assert issue_claim.main(["--repo", REPOSITORY, "board", "--json"]) == 0

    assert capsys.readouterr().out == (
        '{"ok": true, "reason": "projected", "items": [{"number": 10, "title": "Plain item", '
        '"labels": [], "kind": null, "priority_category": 6, "priority_bucket": "unlabelled", '
        '"priority_order": 0, "container": null, "container_parent": null, "scope": null, '
        '"contract": {"now": "Work is ready.", "next": "Ship #10.", '
        '"done_when": "The work is merged.", "defects": []}, "next_step": "Ship #10.", '
        '"contract_complete": true, "projectionless_idea": false, "expectation_state": "-", '
        '"expectation_progress": {"open": 0, "total": 0}, "ruling_landings": null, '
        '"ruling_old": null, "frozen_trigger": null, "freed_on": null, "freed_days": null, '
        '"stage": "text-only", "age_days": 1, "idle_days": 1, "active_claim": null, '
        '"claim_age": null, "claim_old": false, "unblocks_count": 0, "score": -10, '
        '"actionable": true, "actionable_reason": null, "size": null, "has_slices": false, '
        '"estimate": null, "open_blockers": [], "foreign_blockers": []}], '
        '"ready_now": [{"number": 10, "title": "Plain item", "labels": [], "kind": null, '
        '"priority_category": 6, "priority_bucket": "unlabelled", "priority_order": 0, '
        '"container": null, "container_parent": null, "scope": null, '
        '"contract": {"now": "Work is ready.", "next": "Ship #10.", '
        '"done_when": "The work is merged.", "defects": []}, "next_step": "Ship #10.", '
        '"contract_complete": true, "projectionless_idea": false, "expectation_state": "-", '
        '"expectation_progress": {"open": 0, "total": 0}, "ruling_landings": null, '
        '"ruling_old": null, "frozen_trigger": null, "freed_on": null, "freed_days": null, '
        '"stage": "text-only", "age_days": 1, "idle_days": 1, "active_claim": null, '
        '"claim_age": null, "claim_old": false, "unblocks_count": 0, "score": -10, '
        '"actionable": true, "actionable_reason": null, "size": null, "has_slices": false, '
        '"estimate": null, "open_blockers": [], "foreign_blockers": []}], "stale": [], '
        '"recovery": [], "landings": [], "uncut": [], "requests": 3, '
        '"measurements": {"classes": [], "unfinished": 0, "unparsed": 0, "since": null, '
        '"as_of": "2026-08-21"}}\n'
    )


def _lane(item: str, day: int, hours: int) -> metrics.LaneEvent:
    return metrics.LaneEvent(
        item=item,
        size=None,
        container=None,
        claimed_at=datetime(2026, 8, day, tzinfo=UTC),
        released_at=datetime(2026, 8, day, hours, tzinfo=UTC),
        landed_at=None,
        rescopes=0,
    )


def test_board_shows_measured_estimates_across_json_and_html(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Proof 3 (issue #357), pinned through the CLI rather than only through
    `board.build_board` directly: one `FakeForge` board build shows every
    estimate state -- a class with `n >= 3` measured lanes (two of them
    completed by items that have since closed, R2's own closed-item join),
    one with `n < 3` ("schwach"), and an item with no `size` at all ("keine
    Größe") -- across `--json` and `--html`."""
    client = FakeForge()
    client.board_issues = (
        board_issue(30, "Measured M", complete_contract("Ship #30.", size="M")),
        board_issue(31, "Weak S", complete_contract("Ship #31.", size="S")),
        board_issue(32, "Unsized", complete_contract("Ship #32.")),
    )
    client.issue_references[33] = forge.ItemReference(
        forge.ItemState.CLOSED, body=complete_contract("Closed.", size="M")
    )
    client.issue_references[34] = forge.ItemReference(
        forge.ItemState.CLOSED, body=complete_contract("Closed.", size="M")
    )
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())
    lane_events = (
        _lane("30", 10, 4),
        _lane("33", 11, 5),
        _lane("34", 12, 6),
        _lane("31", 15, 2),
    )
    _patch_store_write(monkeypatch, lane_events=lane_events)
    # The one scenario value this test's two checks (`--json`, `--html`) both
    # read back rather than each re-typing the M class's own median/count.
    measured_size, measured_median_hours, measured_sample_count = "M", 5, 3
    measured_estimate_cell = (
        f"~{measured_median_hours}h ({measured_size}, n={measured_sample_count})"
    )
    board_args = ["--repo", REPOSITORY, "board"]

    assert issue_claim.main([*board_args, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    thirty = next(item for item in payload["items"] if item["number"] == 30)
    thirty_one = next(item for item in payload["items"] if item["number"] == 31)
    thirty_two = next(item for item in payload["items"] if item["number"] == 32)
    assert thirty["estimate"] == {
        "item": "30",
        "size": measured_size,
        "median_hours": measured_median_hours,
        "n": measured_sample_count,
        "weak": False,
    }
    assert thirty_one["estimate"]["weak"] is True
    assert thirty_two["size"] is None
    assert thirty_two["estimate"] is None
    assert payload["measurements"]["classes"]

    assert issue_claim.main([*board_args, "--html"]) == 0
    html_page = capsys.readouterr().out
    assert "Messungen" in html_page
    assert measured_estimate_cell in html_page


def test_board_shows_the_empty_measurements_sentence_with_nothing_measured(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The fourth estimate/Messungen state (issue #357 proof 3): with no
    lifecycle data read at all, `--json` and `--html` both show the board's
    own empty-measurements sentence, never a fabricated estimate."""
    _single_item_board_environment(monkeypatch, tmp_path)

    assert issue_claim.main(["--repo", REPOSITORY, "board", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["measurements"]["classes"] == []

    assert issue_claim.main(["--repo", REPOSITORY, "board", "--html"]) == 0
    assert "keine Messungen seit" in capsys.readouterr().out


def test_board_html_shows_unparsed_commits_alongside_the_empty_measurements_sentence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Gate B1 (issue #357): `board_html._render_measurements_section` used
    to return only `lines[0]` ("keine Messungen seit ...") whenever no size
    class had a measured lane, silently dropping a nonzero `unparsed` (or
    `unfinished`) trailing line `board.measurements_lines` already carries.
    With a history that carries no measured class but two claim-shaped
    commits this walk could not parse, the HTML page must show both the
    empty-measurements sentence and the unparsed count."""
    _single_item_board_environment(monkeypatch, tmp_path)
    _patch_store_write(monkeypatch, unparsed_lifecycle_commits=2)

    assert issue_claim.main(["--repo", REPOSITORY, "board", "--html"]) == 0
    html_page = capsys.readouterr().out
    assert "keine Messungen seit" in html_page
    assert f"2 {board.UNPARSED_TRAILER_SENTENCE}" in html_page


def test_board_projects_with_no_state_ref_bootstrapped_at_all(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A repository that has never bootstrapped `refs/aco/state` still boards
    cleanly (issue #357): `_claim_ages`/`_claim_lifecycle` both read a missing
    ref as empty rather than crashing, so `measurements` shows the
    empty-measurements sentence and no claim ages are read at all."""
    _single_item_board_environment(monkeypatch, tmp_path)
    _patch_store_write(monkeypatch, tip=None)

    assert issue_claim.main(["--repo", REPOSITORY, "board", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["measurements"]["classes"] == []


def test_board_reports_requests_equal_to_the_adapters_own_invocation_count(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #168: `observed` is the fixture's own independent tally of every
    `gh` call the injected `run` actually received -- never read from the
    counter under test -- so a matching `requests` field is real evidence,
    not a tautology."""
    observed = _board_fixture_environment(monkeypatch)

    assert issue_claim.main(["--repo", REPOSITORY, "board", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["requests"] == len(observed)


def _single_item_board_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeForge:
    client = FakeForge()
    client.board_issues = (board_issue(10, "Plain item", complete_contract("Ship #10.")),)
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())
    _patch_store_write(monkeypatch)
    return client


def test_board_marks_an_item_landed_by_a_trailer_carrying_trunk_commit_without_a_pull_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #304 review finding B2's "lane 2": a trunk commit's own
    `Work-Item:` trailer lands an item even when no merged pull request body
    names it -- `landed_references` unions the trunk-derived set
    (`board.trunk_landed_work_items`) with the PR-derived one, so the union
    still holds an item a merge/squash commit landed without a matching
    pull request body."""
    _single_item_board_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(
        checkout,
        "trunk_landings",
        lambda *_args, **_kwargs: (
            checkout.TrunkLanding(
                "trailersha",
                datetime(2026, 8, 29, tzinfo=UTC),
                board.TrunkWorkItemClassification((10,)),
            ),
        ),
    )

    assert issue_claim.main(["--repo", REPOSITORY, "board", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    ten = next(item for item in payload["items"] if item["number"] == 10)
    assert ten["stage"] == "code-landed"


def test_board_dedupes_a_landing_between_the_trunk_trailer_and_a_squash_pull_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #371, Beweis 2: under `github`, a trunk-trailer landing (#10)
    and an older-style squash pull request with no trailer of its own (#11)
    both show as one row each in `board --json`'s `landings` array and
    `board --html`'s Landungen section -- the trailer path winning for #10
    even though pull request #89 also plainly closes it, proving the dedup
    rather than merely two different items landing."""
    client = _single_item_board_environment(monkeypatch, tmp_path)
    client.board_issues = (
        board_issue(10, "Trailer landed", complete_contract("Ship #10.")),
        board_issue(11, "Squash landed", complete_contract("Ship #11.")),
    )
    client.board_merged_pull_requests = (
        board.PullRequest(
            number=89,
            title="New-style trailer landing",
            body="Work-Item: #10\n\nCloses #10",
            head_ref_name="codex/issue-10-fix",
            merged_at="2026-08-29T00:00:00Z",
        ),
        board.PullRequest(
            number=90,
            title="Old-style squash",
            body="Closes #11",
            head_ref_name="old/squash-11",
            merged_at="2026-08-18T00:00:00Z",
        ),
    )
    trailer_landing = checkout.TrunkLanding(
        "a" * 40, datetime(2026, 8, 29, tzinfo=UTC), board.TrunkWorkItemClassification((10,))
    )
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: (trailer_landing,))
    board_command = ["--repo", REPOSITORY, "board"]

    assert issue_claim.main([*board_command, "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    landings = {row["item"]: row for row in payload["landings"]}
    assert landings.keys() == {10, 11}
    assert landings[10]["sha"] == "a" * 40
    assert landings[10]["pull_request"] is None
    assert landings[11]["sha"] is None
    assert landings[11]["pull_request"] == 90

    assert issue_claim.main([*board_command, "--html"]) == 0
    rendered_html = capsys.readouterr().out
    assert "<li>#10 2026-08-29 <code>aaaaaaa</code></li>" in rendered_html
    assert "<li>#11 2026-08-18 PR #90</li>" in rendered_html


def test_board_html_prints_the_page_naming_its_repository_and_checkout_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #431: the page `board --html` prints carries the repository it
    was built for and the checkout the command ran in, so two boards open
    side by side say which one an operator is ruling on."""
    _single_item_board_environment(monkeypatch, tmp_path)

    assert issue_claim.main(["--repo", REPOSITORY, "board", "--html"]) == 0
    rendered = capsys.readouterr().out
    assert (
        f"<title>example/agent-coordination &middot; {tmp_path} &middot; Board</title>" in rendered
    )
    assert f'<p class="eyebrow">example/agent-coordination &middot; {tmp_path}</p>' in rendered
    assert "#10 Plain item" in rendered


def test_board_html_path_writes_the_page_to_a_file_instead_of_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _single_item_board_environment(monkeypatch, tmp_path)
    output_path = tmp_path / "board.html"

    exit_code = issue_claim.main(["--repo", REPOSITORY, "board", "--html", str(output_path)])

    assert exit_code == 0
    assert capsys.readouterr().out == ""
    written = output_path.read_text(encoding="utf-8")
    assert "#10 Plain item" in written


def test_board_html_costs_no_gh_call_beyond_board_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #276: `board --html` reshapes the exact reads `board --json`
    already performs -- `_board`'s own merged-pull-request fetch, not a
    second one -- so its request count against the same fixture never
    exceeds `board --json`'s."""
    client = _single_item_board_environment(monkeypatch, tmp_path)

    assert issue_claim.main(["--repo", REPOSITORY, "board", "--json"]) == 0
    capsys.readouterr()
    json_requests = client.requests

    client.requests = 0
    assert issue_claim.main(["--repo", REPOSITORY, "board", "--html"]) == 0
    capsys.readouterr()

    assert client.requests == json_requests


def test_board_html_and_json_are_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _single_item_board_environment(monkeypatch, tmp_path)

    status = issue_claim.main(["--repo", REPOSITORY, "board", "--html", "--json"])

    assert status == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "invalid_usage"


def test_board_naming_no_output_mode_refuses_before_any_read(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """BOARD-44 (issue #420): the retired text table left `board` with no
    default mode, so a bare `aco board` must name one instead of reading a
    forge, a pin, or a repository just to guess."""
    status = issue_claim.main(["board"])

    assert status == 2
    assert capsys.readouterr().err == "ERROR: aco board requires --json, --html, or --serve\n"


def test_board_skips_the_children_list_for_a_container_with_zero_children(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #168: a container whose own summary already says 0/0 must never
    pay for `list_children` -- there is no open child either way -- while a
    container that does carry children still gets its detail list."""
    issues = (
        board_issue(10, "Plain item", complete_contract("Ship #10.")),
        replace(
            board_issue(20, "Empty container", complete_contract("Ship #20.")),
            kind=body.ItemKind.CONTAINER,
            children_closed=0,
            children_total=0,
        ),
        replace(
            board_issue(30, "Container with children", complete_contract("Ship #30.")),
            kind=body.ItemKind.CONTAINER,
            children_closed=1,
            children_total=2,
        ),
    )
    client = FakeForge()
    client.board_issues = issues
    client.children[30] = (board.ChildItem(31, board.ChildState.OPEN),)
    observed_children_calls: list[int] = []
    original_list_children = client.list_children

    def spy_list_children(number: int) -> tuple[board.ChildItem, ...]:
        observed_children_calls.append(number)
        return original_list_children(number)

    monkeypatch.setattr(client, "list_children", spy_list_children)
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())
    _patch_store_write(monkeypatch)

    assert issue_claim.main(["--repo", REPOSITORY, "board", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert observed_children_calls == [30]
    # Open issues, open PRs, merged PRs, and exactly one `list_children`
    # call (for #30, never #20) -- claims come from the store now (issue
    # #176), never from a ledger-comments request. No issue here names a
    # blocker, so `list_board_blockers` never runs -- an empty numbers set
    # costs no request, on the fake exactly as on the real adapter.
    assert payload["requests"] == client.requests == 4


def test_board_skips_the_dependency_list_for_a_zero_blocker_item_in_block_mode(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #168: the pattern #150 started with `total_blocked_by` -- an
    item whose own count already says 0 must never pay for its dependency
    list, only the one that actually carries a blocker."""
    (tmp_path / ".agent-claim").mkdir()
    (tmp_path / ".agent-claim" / "board.toml").write_text('body_contract = "block"\n')
    unblocked = board_issue(10, "Unblocked", agent_claim_body(MINIMAL_BLOCK_TOML))
    blocked = replace(
        board_issue(11, "Blocked", agent_claim_body(MINIMAL_BLOCK_TOML)), blocked_by_count=1
    )
    client = FakeForge()
    client.board_issues = (unblocked, blocked)
    client.board_dependencies[11] = (block_dependency(10),)
    observed_dependency_calls: list[int] = []
    original_list_board_dependencies = client.list_board_dependencies

    def spy_list_board_dependencies(number: int) -> tuple[board.IssueDependency, ...]:
        observed_dependency_calls.append(number)
        return original_list_board_dependencies(number)

    monkeypatch.setattr(client, "list_board_dependencies", spy_list_board_dependencies)
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())
    _patch_store_write(monkeypatch)

    assert issue_claim.main(["--repo", REPOSITORY, "board", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert observed_dependency_calls == [11]
    # Open issues, open PRs, merged PRs, and exactly one dependency lookup
    # (for #11, never #10) -- block mode never calls `list_board_blockers`,
    # and claims come from the store now (issue #176), never a ledger
    # comments request.
    assert payload["requests"] == client.requests == 4


def test_board_shows_open_and_total_instead_of_proposed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    issues = (
        board_issue(10, "No expectations", complete_contract("Claim #10.")),
        board_issue(
            11,
            "Proposed expectations",
            complete_contract(
                "Claim #11.",
                expectation=[
                    ruled_expectation("Name it."),
                    proposed_expectation("Settle it.", default="no"),
                ],
            ),
        ),
        board_issue(
            12,
            "Ruled expectations",
            complete_contract("Claim #12.", expectation=[ruled_expectation("Name it.")]),
        ),
    )
    client = FakeForge()
    monkeypatch.setattr(client, "list_open_board_issues", lambda: issues)
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())

    assert issue_claim.main(["--repo", REPOSITORY, "board", "--json"]) == 0
    items = {item["number"]: item for item in json.loads(capsys.readouterr().out)["items"]}
    expectation_states = {number: item["expectation_state"] for number, item in items.items()}
    assert expectation_states == {10: "-", 11: "proposed", 12: "ruled"}
    assert items[11]["expectation_progress"] == {"open": 1, "total": 2}


def test_rulings_lists_open_expectations_by_board_priority_then_open_count(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    issues = (
        rulings_issue(
            50,
            "In-flight security work",
            open_lines=2,
            total_lines=3,
            labels=("security",),
        ),
        rulings_issue(
            30,
            "Later security tie",
            open_lines=1,
            total_lines=2,
            labels=("security",),
        ),
        rulings_issue(
            10,
            "Earlier security tie",
            open_lines=2,
            total_lines=3,
            labels=("security",),
        ),
        rulings_issue(
            40,
            "More open security work",
            open_lines=2,
            total_lines=3,
            labels=("security",),
        ),
        rulings_issue(
            60,
            "Lower-priority product work",
            open_lines=1,
            total_lines=1,
            labels=("product",),
        ),
        rulings_issue(
            70,
            "Fully ruled security work",
            open_lines=0,
            total_lines=1,
            labels=("security",),
        ),
    )
    _configured_board_client(
        monkeypatch,
        tmp_path,
        open_issues=issues,
        open_pull_requests=(board.PullRequest(200, "Fixes #50", "", "branch"),),
    )

    assert issue_claim.main(["--repo", REPOSITORY, "rulings"]) == 0
    headers = [line for line in capsys.readouterr().out.splitlines() if line.startswith("#")]
    assert headers == [
        "#50 2/3: In-flight security work",
        "#30 1/2: Later security tie",
        "#10 2/3: Earlier security tie",
        "#40 2/3: More open security work",
        "#60 1/1: Lower-priority product work",
        "#70 0/1: Fully ruled security work",
    ]


def test_rulings_reads_expectation_progress_from_the_block_not_stale_prose(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`rulings` must consume `BoardItem.expectation_progress`, not re-scan
    the raw body: a fully-ruled `## Erwartungen` heading left beside a still-
    proposed `[[expectation]]` would otherwise hide this item from `rulings`
    entirely (#150)."""
    stale_disagreeing_prose = (
        "\n\n## Erwartungen (refine-Lauf 28.08.2026)\n"
        "- Ruled one *(geregelt: ja)*\n"
        "- Ruled two *(geregelt: ja)*\n"
    )
    toml_text = f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Proposed"\ndefault = "later"\n'
    body = agent_claim_body(toml_text) + stale_disagreeing_prose
    issue = board_issue(400, "Block-only expectations", body)
    _configured_board_client(monkeypatch, tmp_path, open_issues=(issue,))
    _write_block_pin(tmp_path)

    assert issue_claim.main(["--repo", REPOSITORY, "rulings"]) == 0
    assert capsys.readouterr().out == "#400 1/1: Block-only expectations\n  1 open: Proposed\n"


def test_rulings_renders_text_json_and_empty_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #379: a fully-ruled item is listed too (`0/2`, both lines), the
    `--json` line carries `ruling`/`ruled_on` instead of `state`, and the
    empty sentence fires only once no listed item carries any line."""
    open_issue = rulings_issue(
        10,
        "Open expectation",
        open_lines=1,
        total_lines=2,
    )
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(open_issue,))
    rulings_command = ["--repo", REPOSITORY, "rulings"]

    assert issue_claim.main(rulings_command) == 0
    assert capsys.readouterr().out == (
        "#10 1/2: Open expectation\n"
        "  1 open: Open decision 0.\n"
        f"  2 ruled yes {RULED_ON.isoformat()}: Settled decision 0.\n"
    )

    assert issue_claim.main([*rulings_command, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "reason": "listed",
        "rulings": [
            {
                "number": 10,
                "title": "Open expectation",
                "open": 1,
                "total": 2,
                "lines": [
                    {"index": 1, "text": "Open decision 0.", "ruling": None, "ruled_on": None},
                    {
                        "index": 2,
                        "text": "Settled decision 0.",
                        "ruling": "yes",
                        "ruled_on": RULED_ON.isoformat(),
                    },
                ],
            }
        ],
    }

    fully_ruled_issue = rulings_issue(
        11,
        "Fully ruled",
        open_lines=0,
        total_lines=2,
    )
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (fully_ruled_issue,))

    assert issue_claim.main(rulings_command) == 0
    assert capsys.readouterr().out == (
        "#11 0/2: Fully ruled\n"
        f"  1 ruled yes {RULED_ON.isoformat()}: Settled decision 0.\n"
        f"  2 ruled yes {RULED_ON.isoformat()}: Settled decision 1.\n"
    )

    assert issue_claim.main([*rulings_command, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "reason": "listed",
        "rulings": [
            {
                "number": 11,
                "title": "Fully ruled",
                "open": 0,
                "total": 2,
                "lines": [
                    {
                        "index": 1,
                        "text": "Settled decision 0.",
                        "ruling": "yes",
                        "ruled_on": RULED_ON.isoformat(),
                    },
                    {
                        "index": 2,
                        "text": "Settled decision 1.",
                        "ruling": "yes",
                        "ruled_on": RULED_ON.isoformat(),
                    },
                ],
            }
        ],
    }

    monkeypatch.setattr(client, "list_open_board_issues", lambda: ())

    assert issue_claim.main(rulings_command) == 0
    assert capsys.readouterr().out == "No expectation lines.\n"

    assert issue_claim.main([*rulings_command, "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "reason": "listed", "rulings": []}


def test_rulings_json_pins_the_raw_envelope_text_for_the_empty_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The raw bytes `rulings` prints on success (OUT-nn's own key order,
    `ok`/`reason`/`rulings`, and its trailing newline), not just the parsed
    dict `test_rulings_renders_text_json_and_empty_success` already covers."""
    _configured_board_client(monkeypatch, tmp_path, open_issues=())

    assert issue_claim.main(["--repo", REPOSITORY, "rulings", "--json"]) == 0

    assert capsys.readouterr().out == '{"ok": true, "reason": "listed", "rulings": []}\n'


def test_rulings_json_carries_question_example_and_picture(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #295: `rulings --json` carries the three optional card fields
    through unchanged from `expectation_lines`; the human `rulings` text
    form (proven above) keeps printing only `text`, untouched."""
    picture = '<svg xmlns="http://www.w3.org/2000/svg"><circle cx="5" cy="5" r="4"/></svg>'
    open_issue = board_issue(
        10,
        "Open expectation",
        complete_contract(
            "Ship #10.",
            expectation=[
                proposed_expectation(
                    "Open decision.",
                    question="Ship it?",
                    example="Release on Friday.",
                    picture=picture,
                )
            ],
        ),
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(open_issue,))

    assert issue_claim.main(["--repo", REPOSITORY, "rulings", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "reason": "listed",
        "rulings": [
            {
                "number": 10,
                "title": "Open expectation",
                "open": 1,
                "total": 1,
                "lines": [
                    {
                        "index": 1,
                        "text": "Open decision.",
                        "ruling": None,
                        "ruled_on": None,
                        "question": "Ship it?",
                        "example": "Release on Friday.",
                        "picture": picture,
                    }
                ],
            }
        ],
    }


def rulings_issue(
    number: int, title: str, *, open_lines: int, total_lines: int, labels: tuple[str, ...] = ()
) -> board.Issue:
    expectations = [
        *(proposed_expectation(f"Open decision {index}.") for index in range(open_lines)),
        *(
            ruled_expectation(f"Settled decision {index}.")
            for index in range(total_lines - open_lines)
        ),
    ]
    return board_issue(
        number,
        title,
        complete_contract(f"Ship #{number}.", expectation=expectations),
        labels=labels,
    )


def _configured_board_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    open_issues: tuple[board.Issue, ...] = (),
    open_pull_requests: tuple[board.PullRequest, ...] = (),
    dependencies: Mapping[int, tuple[board.IssueDependency, ...]] = MappingProxyType({}),
    standing: tuple[ClaimRequest, ...] = (),
) -> FakeForge:
    """A `FakeForge` client wired the way every board-reading claim test needs."""
    client = FakeForge()
    monkeypatch.setattr(client, "list_open_board_issues", lambda: open_issues)
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: open_pull_requests)
    client.board_open_pull_requests = open_pull_requests
    client.board_dependencies = dict(dependencies)
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())
    _patch_store_write(monkeypatch, *(_store_claim_from_request(claimed) for claimed in standing))
    return client


def _stub_issue_reference(
    monkeypatch: pytest.MonkeyPatch,
    states: dict[int, tuple[forge.ItemState, str, str]],
) -> None:
    """Overrides the autouse OPEN default for exactly the given issue numbers."""

    def fetch(client: object, number: int) -> forge.ItemReference:
        state, title, body = states[number]
        return forge.ItemReference(state, title, body)

    monkeypatch.setattr(issue_claim, "_fetch_issue_reference", fetch)


_TOP_AND_BLOCKED = (
    board_issue(10, "Lower work", complete_contract("Claim #10.")),
    board_issue(11, "Top work", complete_contract("Claim #11.")),
    board_issue(12, "Depends on top", complete_contract("Claim #12."), blocked_by_count=1),
)
_BLOCKED_BY_ELEVEN = {12: (block_dependency(11),)}

# issue #348: every `next` golden below a scopeless `WorkItemAction` needs
# this note right after its `Run:` line -- the item's own body names no
# scope, so `claim` cannot derive one either -- and this tail once the
# action's own lines end: a scopeless first action leaves `parallel_set`
# nothing to found a set on, so `parallel:`/`scope unknown:` collapse into
# the one `unknown` sentence, and `close:` still prints `none`.
_SCOPE_UNKNOWN_NOTE_LINE = "scope unknown\n"
_PARALLEL_UNKNOWN_TAIL = "parallel: unknown (first action names no scope)\nclose: none\n"
_UNKNOWN_SCOPE_NEXT_TAIL = _SCOPE_UNKNOWN_NOTE_LINE + _PARALLEL_UNKNOWN_TAIL
# The same tail once there is no first action to found a parallel set on at
# all (`board.parallel_set` returns an empty, *not* first-scope-unknown, set).
_NO_ACTION_NEXT_TAIL = "parallel: none\nscope unknown: none\nclose: none\n"
_UNKNOWN_SCOPE_PARALLEL_JSON: dict[str, object] = {
    "first_scope_unknown": True,
    "candidates": [],
    "scope_unknown": [],
}
_EMPTY_PARALLEL_JSON: dict[str, object] = {
    "first_scope_unknown": False,
    "candidates": [],
    "scope_unknown": [],
}

# issue #348, Beweis 1: five free items -- Beta and Gamma name the same
# path (the "two overlapping each other" pair), Delta names no scope at
# all, Epsilon names a path under claim-live-1's own directory scope (the
# "excluded by a live claim, not by another candidate" case; R1 review) --
# read against two live claims, claim-live-2's own path sitting inside
# claim-live-1's directory scope, so the two are not disjoint from each
# other either; only the free items are checked for disjointness here.
# Alpha out-ranks the rest purely by its lower issue number (every item
# shares the same score), so it is always the first action; Beta then wins
# the walk over Gamma (board order), and Gamma is dropped silently --
# neither `parallel:` nor `scope unknown:` names an item excluded for
# overlap, only one excluded for lacking a scope at all. Epsilon is
# dropped the same silent way, but for a different reason: were the walk
# not occupying the live claims' own scopes, Epsilon would have nothing to
# collide with and would surface in `parallel:` instead.
_PARALLEL_ALPHA = board_issue(
    50, "Alpha", complete_contract("Ship Alpha.", scope=["src/alpha1.py", "src/alpha2.py"])
)
_PARALLEL_BETA = board_issue(51, "Beta", complete_contract("Ship Beta.", scope=["src/shared.py"]))
_PARALLEL_GAMMA = board_issue(
    52, "Gamma", complete_contract("Ship Gamma.", scope=["src/shared.py"])
)
_PARALLEL_DELTA = board_issue(53, "Delta", complete_contract("Ship Delta."))
_PARALLEL_EPSILON = board_issue(
    54, "Epsilon", complete_contract("Ship Epsilon.", scope=["claimed/deep/file.py"])
)
_PARALLEL_ITEMS = (
    _PARALLEL_ALPHA,
    _PARALLEL_BETA,
    _PARALLEL_GAMMA,
    _PARALLEL_DELTA,
    _PARALLEL_EPSILON,
)
_PARALLEL_LIVE_CLAIMS = (
    request(claim_id="claim-live-1", issue=990, scope=("claimed",)),
    request(claim_id="claim-live-2", issue=991, scope=("claimed/two.py",)),
)


@pytest.mark.parametrize(
    ("issues", "dependencies", "claims", "arguments", "expected_exit", "expected_output"),
    [
        pytest.param(
            _TOP_AND_BLOCKED,
            _BLOCKED_BY_ELEVEN,
            (),
            ("next",),
            0,
            "#11 score 10: Top work\nNext: Claim #11.\n"
            "Run: aco claim 11 --scope <paths>\n" + _UNKNOWN_SCOPE_NEXT_TAIL + "\nSKIPPED\n"
            "#12: blocked by #11\n",
            id="names_the_highest_scored_actionable_item",
        ),
        pytest.param(
            _TOP_AND_BLOCKED,
            _BLOCKED_BY_ELEVEN,
            (),
            ("next", "--json"),
            0,
            {
                "ok": True,
                "reason": "work_item",
                "number": 11,
                "score": 10,
                "title": "Top work",
                "next": "Claim #11.",
                "command": "aco claim 11 --scope <paths>",
                "recovery": [],
                "skipped": [{"number": 12, "reason": "blocked by #11"}],
                "ruling_landings": None,
                "ruling_old": None,
                "parallel": _UNKNOWN_SCOPE_PARALLEL_JSON,
                "close": [],
            },
            id="emits_the_highest_scored_actionable_item_as_json",
        ),
        pytest.param(
            (board_issue(10, "Incomplete", complete_contract("", done_when="")),),
            {},
            (),
            ("next",),
            3,
            "No actionable item.\n" + _NO_ACTION_NEXT_TAIL + "\nSKIPPED\n"
            "#10: body incomplete: Next, Done when\n",
            id="names_an_incomplete_body_as_the_reason_nothing_is_pullable",
        ),
        pytest.param(
            (board_issue(10, "Blockless", "## Now\nInvestigate."),),
            {},
            (),
            ("next",),
            3,
            "No actionable item.\n" + _NO_ACTION_NEXT_TAIL + "\nSKIPPED\n"
            "#10: body malformed: agent-claim: no agent-claim block\n",
            id="names_a_body_with_no_block_as_malformed",
        ),
        pytest.param(
            (board_issue(10, "Claimed", complete_contract("Claim #10.")),),
            {},
            (request(issue=10),),
            ("next",),
            3,
            "No actionable item.\n" + _NO_ACTION_NEXT_TAIL + "\nSKIPPED\n#10: claimed\n",
            id="names_a_live_claim_as_the_reason_nothing_is_pullable",
        ),
        pytest.param(
            (
                board_issue(9, "Open blocker", complete_contract("Claim #9.")),
                board_issue(10, "Blocked", complete_contract("Claim #10."), blocked_by_count=1),
            ),
            {10: (block_dependency(9),)},
            (),
            ("next",),
            0,
            "#9 score 10: Open blocker\nNext: Claim #9.\n"
            "Run: aco claim 9 --scope <paths>\n" + _UNKNOWN_SCOPE_NEXT_TAIL + "\nSKIPPED\n"
            "#10: blocked by #9\n",
            id="excludes_items_with_open_blockers",
        ),
        pytest.param(
            (),
            {},
            (),
            ("next",),
            3,
            "No actionable item.\n" + _NO_ACTION_NEXT_TAIL,
            id="prints_no_actionable_item_on_a_fully_empty_board",
        ),
        pytest.param(
            (),
            {},
            (),
            ("next", "--json"),
            3,
            {
                "ok": False,
                "reason": "nothing_actionable",
                "recovery": [],
                "skipped": [],
                "parallel": _EMPTY_PARALLEL_JSON,
                "close": [],
            },
            id="emits_nothing_actionable_on_a_fully_empty_board",
        ),
        pytest.param(
            _PARALLEL_ITEMS,
            {},
            _PARALLEL_LIVE_CLAIMS,
            ("next",),
            0,
            "#50 score -10: Alpha\nNext: Ship Alpha.\n"
            "Run: aco claim 50\n"
            "parallel: #51 (1 path)\n"
            "scope unknown: #53\n"
            "close: none\n",
            id="parallel_set_names_the_maximal_disjoint_set_and_the_unknown_scope_item",
        ),
        pytest.param(
            _PARALLEL_ITEMS,
            {},
            _PARALLEL_LIVE_CLAIMS,
            ("next", "--json"),
            0,
            {
                "ok": True,
                "reason": "work_item",
                "number": 50,
                "score": -10,
                "title": "Alpha",
                "next": "Ship Alpha.",
                "command": "aco claim 50",
                "recovery": [],
                "skipped": [],
                "ruling_landings": None,
                "ruling_old": None,
                "parallel": {
                    "first_scope_unknown": False,
                    "candidates": [{"number": 51, "scope": ["src/shared.py"]}],
                    "scope_unknown": [53],
                },
                "close": [],
            },
            id="parallel_set_json_carries_full_scopes_for_every_candidate",
        ),
    ],
)
def test_next_reports_the_highest_scored_actionable_item(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    issues: tuple[board.Issue, ...],
    dependencies: dict[int, tuple[board.IssueDependency, ...]],
    claims: tuple[ClaimRequest, ...],
    arguments: tuple[str, ...],
    expected_exit: int,
    expected_output: str | dict[str, object],
) -> None:
    _configured_board_client(
        monkeypatch,
        tmp_path,
        open_issues=issues,
        dependencies=dependencies,
        standing=claims,
    )

    assert issue_claim.main(["--repo", REPOSITORY, *arguments]) == expected_exit
    rendered = capsys.readouterr().out

    if isinstance(expected_output, str):
        assert rendered == expected_output
    else:
        assert json.loads(rendered) == expected_output


def test_next_json_pins_the_raw_envelope_text_for_a_work_item_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The raw bytes `next` prints on success (OUT-nn's own key order,
    `ok`/`reason` first, and its trailing newline), not just the parsed dict
    `test_next_reports_the_highest_scored_actionable_item` already covers."""
    _configured_board_client(
        monkeypatch, tmp_path, open_issues=_TOP_AND_BLOCKED, dependencies=_BLOCKED_BY_ELEVEN
    )

    assert issue_claim.main(["--repo", REPOSITORY, "next", "--json"]) == 0

    assert capsys.readouterr().out == (
        '{"ok": true, "reason": "work_item", "recovery": [], '
        '"skipped": [{"number": 12, "reason": "blocked by #11"}], '
        '"parallel": {"first_scope_unknown": true, "candidates": [], "scope_unknown": []}, '
        '"close": [], "number": 11, "score": 10, "title": "Top work", "next": "Claim #11.", '
        '"command": "aco claim 11 --scope <paths>", "ruling_landings": null, '
        '"ruling_old": null}\n'
    )


PULLED_WITH_REFINING_FIRST = (
    "#10 score -10: Work\nNext: Claim #10.\n"
    "Run: aco claim 10 --scope <paths>\n"
    + _SCOPE_UNKNOWN_NOTE_LINE
    + "expectations unruled: refine before the pull\n"
    + _PARALLEL_UNKNOWN_TAIL
)


@pytest.mark.parametrize(
    ("expectations", "expected_state", "expected_output"),
    [
        pytest.param(
            (),
            body.ExpectationState.NONE,
            "#10 score -10: Work\nNext: Claim #10.\n"
            "Run: aco claim 10 --scope <paths>\n" + _UNKNOWN_SCOPE_NEXT_TAIL,
            id="no_expectation_entry_remains_actionable",
        ),
        pytest.param(
            (proposed_expectation("Name it.", default="yes"),),
            body.ExpectationState.PROPOSED,
            PULLED_WITH_REFINING_FIRST,
            id="proposed_expectations_are_pulled_with_refining_first",
        ),
        pytest.param(
            (ruled_expectation("Name it."), ruled_expectation("Remove it.", ruling="no")),
            body.ExpectationState.RULED,
            "#10 score -10: Work\nNext: Claim #10.\n"
            "Run: aco claim 10 --scope <paths>\n" + _UNKNOWN_SCOPE_NEXT_TAIL,
            id="fully_ruled_expectations_remain_actionable",
        ),
        pytest.param(
            (ruled_expectation("Name it.", ruling="no"), proposed_expectation("Remove it.")),
            body.ExpectationState.PROPOSED,
            PULLED_WITH_REFINING_FIRST,
            id="mixed_expectations_are_pulled_with_refining_first",
        ),
    ],
)
def test_next_reports_expectation_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    expectations: tuple[dict[str, object], ...],
    expected_state: body.ExpectationState,
    expected_output: str,
) -> None:
    issue = board_issue(10, "Work", complete_contract("Claim #10.", expectation=list(expectations)))
    client = FakeForge()
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (issue,))
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())

    assert issue_claim.main(["--repo", REPOSITORY, "next"]) == 0
    assert capsys.readouterr().out == expected_output

    projected = projected_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )
    assert projected.items[0].expectation_state is expected_state


def test_next_pulls_an_unruled_item_and_names_only_unworkable_ones_as_skipped(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    unruled = board_issue(
        11,
        "Needs rulings",
        complete_contract(
            "Claim #11.", expectation=[proposed_expectation("Name it.", default="no")]
        ),
    )
    blocked, blocked_dependencies = blocked_issue(
        12, "Waits for rulings", block_dependency(11), next_step="Claim #12."
    )
    claimed = board_issue(13, "Another lane", complete_contract("Claim #13."))
    standing = request(issue=13)
    client = FakeForge()
    client.board_dependencies = dict(blocked_dependencies)
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (unruled, blocked, claimed))
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())
    _patch_store_write(monkeypatch, _store_claim_from_request(standing))

    assert issue_claim.main(["--repo", REPOSITORY, "next"]) == 0
    assert capsys.readouterr().out == (
        "#11 score 10: Needs rulings\n"
        "Next: Claim #11.\n"
        "Run: aco claim 11 --scope <paths>\n"
        + _SCOPE_UNKNOWN_NOTE_LINE
        + "expectations unruled: refine before the pull\n"
        + _PARALLEL_UNKNOWN_TAIL
        + "\n"
        "SKIPPED\n"
        "#12: blocked by #11\n"
        "#13: claimed\n"
    )

    assert issue_claim.main(["--repo", REPOSITORY, "next", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "reason": "work_item",
        "number": 11,
        "score": 10,
        "title": "Needs rulings",
        "next": "Claim #11.",
        "command": "aco claim 11 --scope <paths>",
        "ruling_landings": None,
        "ruling_old": None,
        "ruling_hint": "expectations unruled: refine before the pull",
        "recovery": [],
        "skipped": [
            {"number": 12, "reason": "blocked by #11"},
            {"number": 13, "reason": "claimed"},
        ],
        "parallel": _UNKNOWN_SCOPE_PARALLEL_JSON,
        "close": [],
    }


def _redirect_toplevel(monkeypatch: pytest.MonkeyPatch, toplevel: Path) -> None:
    """Point `_resolve_toplevel()` (`rev-parse --show-toplevel`) at a real
    repository this test built itself (issue #322), taking precedence over
    the module's autouse `_isolate_git_toplevel` fake -- every other real
    git call `start`'s own worktree creation runs still reaches real git,
    unlike a fully faked `checkout._git_output`."""
    real_git_output = checkout._git_output

    def fake(arguments: list[str], *, directory: Path | None = None) -> str:
        if arguments == ["rev-parse", "--show-toplevel"] and directory is None:
            return str(toplevel)
        return real_git_output(arguments, directory=directory)

    monkeypatch.setattr(checkout, "_git_output", fake)


def _start_scenario(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, agent: str = "Codex Sol"
) -> Path:
    """A real bare-remote-backed repository (issue #322), item #314 open
    with a titled, scoped body, and this test's own toplevel redirected onto
    it -- `start`'s own worktree creation runs real git, unlike every other
    claim test in this module, which never builds one at all."""
    repo, _remote = _real_repository_with_bare_remote(tmp_path)
    (repo / "base.txt").write_text("base\n")
    _real_git(repo, "add", "base.txt")
    _real_git(repo, "commit", "-q", "-m", "initial")
    _push_repository_trunk(repo, "origin")
    issue = board_issue(314, "Fresh Slug Title", complete_contract("Build it.", scope=["src/x.py"]))
    client = FakeForge(board_issues=(issue,))
    client.issue_references[314] = forge.ItemReference(
        forge.ItemState.OPEN, issue.title, issue.body
    )
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    _patch_store_write(monkeypatch)
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: agent})
    _redirect_toplevel(monkeypatch, repo)
    return repo


_START_WORKTREE_NAME = "issue-314-fresh-slug-title"
_START_BRANCH = "codex/issue-314-fresh-slug-title"
_CLAIM_ID_PATTERN = r"[0-9a-f]{32}"


def _claimed_line_id(out: str, subject: str) -> str:
    """The claim id `CLAIMED <subject>: <id>` printed in `out` (issue #322
    review finding 1): `start` mints a fresh id exactly as a bare `aco
    claim` does, so a test proving it never pins one -- only that a create
    prints a real one and a resume reprints the same one."""
    match = re.search(rf"CLAIMED {re.escape(subject)}: ({_CLAIM_ID_PATTERN})\n", out)
    assert match is not None
    return match.group(1)


def test_start_creates_the_linked_worktree_and_claims_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    repo = _start_scenario(monkeypatch, tmp_path)
    monkeypatch.chdir(repo)

    assert issue_claim.main(["--repo", REPOSITORY, "start", "314"]) == 0

    worktree = repo.parent / f"{repo.name}-worktrees" / _START_WORKTREE_NAME
    out = capsys.readouterr().out
    claim_id = _claimed_line_id(out, "issue #314")
    assert out == (
        f"worktree: {worktree}\n"
        f"branch: {_START_BRANCH}\n"
        f"CLAIMED issue #314: {claim_id}\n"
        "0 of 4 versioned files (0%); overlaps no other open claims\n"
    )
    resolved = checkout.resolve_path_checkout(worktree)
    assert resolved is not None
    assert resolved.branch == _START_BRANCH
    assert resolved.kind is checkout.CheckoutKind.LINKED_WORKTREE
    live = store.fetch_state(worktree=Path("."), remote="origin").claims
    assert protocol.claim_key(protocol.IssueIdentity(314), _START_BRANCH) in live
    assert Path.cwd() == repo


def test_start_resumes_an_existing_worktree_by_only_claiming(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    repo = _start_scenario(monkeypatch, tmp_path)
    monkeypatch.chdir(repo)
    assert issue_claim.main(["--repo", REPOSITORY, "start", "314"]) == 0
    first_claim_id = _claimed_line_id(capsys.readouterr().out, "issue #314")
    worktree = repo.parent / f"{repo.name}-worktrees" / _START_WORKTREE_NAME

    assert issue_claim.main(["--repo", REPOSITORY, "start", "314"]) == 0

    resumed_claim_id = _claimed_line_id(capsys.readouterr().out, "issue #314")
    assert resumed_claim_id == first_claim_id
    assert len(store.fetch_state(worktree=Path("."), remote="origin").claims) == 1
    assert checkout.resolve_path_checkout(worktree) is not None
    assert Path.cwd() == repo


def test_start_refuses_a_slug_the_derived_rule_would_never_produce(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    repo = _start_scenario(monkeypatch, tmp_path)
    monkeypatch.chdir(repo)

    status = issue_claim.main(["--repo", REPOSITORY, "start", "314", "--slug", "Bad_Slug"])

    assert status == 2
    assert capsys.readouterr().err == "ERROR: --slug must be " + checkout._SLUG_SHAPE_RULE + "\n"
    assert not (repo.parent / f"{repo.name}-worktrees").exists()


def test_start_refuses_an_unsafe_identity_prefix_before_any_git_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #322 review/gate: an unsafe `ACO_AGENT` first word must never
    reach `git worktree add` -- the branch it would build is refused first,
    by name, and no worktree is ever created."""
    repo = _start_scenario(monkeypatch, tmp_path, agent="-bad")
    monkeypatch.chdir(repo)

    status = issue_claim.main(["--repo", REPOSITORY, "start", "314"])

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: agent identity '-bad' is not usable in a branch name: "
        "'-bad/issue-314-fresh-slug-title' is not a safe Git ref\n"
    )
    assert not (repo.parent / f"{repo.name}-worktrees").exists()


def test_start_refuses_to_resume_a_live_claim_held_by_another_agent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #322 review/gate: `claim_key` is an issue-only key for an
    `IssueIdentity` (it never folds in `branch`), so a live claim on #314
    held by a different agent on a different branch must never be silently
    resumed as this session's own -- it falls through to the store's own
    "is claimed by ..." conflict instead."""
    repo = _start_scenario(monkeypatch, tmp_path)
    foreign = _store_claim_from_request(
        request(
            "other-claim",
            "Grok sess-9",
            issue=314,
            branch="grok/issue-314-other",
            scope=("src/x.py",),
        )
    )
    _patch_store_write(monkeypatch, foreign)
    monkeypatch.chdir(repo)

    status = issue_claim.main(["--repo", REPOSITORY, "start", "314"])

    assert status == 2
    assert "is claimed by Grok sess-9" in capsys.readouterr().err


def test_start_refuses_to_resume_the_same_agents_claim_under_another_role(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #322 review/gate finding 3: `claim_key` folds in neither `role`
    nor `agent`, so a live claim on #314 held by this same session's own
    agent and branch, but as `reviewer` rather than the `builder` role
    `start` always claims with, must never be silently resumed as this
    session's build claim -- it falls through to the store's own
    "is claimed by ..." conflict, exactly as a different agent's claim
    does."""
    repo = _start_scenario(monkeypatch, tmp_path)
    reviewer_claim = _store_claim_from_request(
        request(
            "reviewer-claim",
            "Codex Sol",
            issue=314,
            role="reviewer",
            branch=_START_BRANCH,
            scope=("src/x.py",),
        )
    )
    _patch_store_write(monkeypatch, reviewer_claim)
    monkeypatch.chdir(repo)

    status = issue_claim.main(["--repo", REPOSITORY, "start", "314"])

    assert status == 2
    assert "is claimed by Codex Sol" in capsys.readouterr().err


def test_start_resume_refuses_a_scope_that_differs_from_the_live_claim(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #322 review/gate: an explicit `--scope` on resume that disagrees
    with the live claim's own stored scope is refused rather than silently
    ignored; the identical scope is accepted (covered by
    `test_start_resumes_an_existing_worktree_by_only_claiming`'s bare
    resume, which passes no `--scope` at all)."""
    repo = _start_scenario(monkeypatch, tmp_path)
    monkeypatch.chdir(repo)
    assert issue_claim.main(["--repo", REPOSITORY, "start", "314"]) == 0
    capsys.readouterr()

    status = issue_claim.main(["--repo", REPOSITORY, "start", "314", "--scope", "src/other.py"])

    assert status == 2
    assert capsys.readouterr().err == f"ERROR: {issue_claim.RESUME_SCOPE_MISMATCH}\n"


def test_start_resumes_a_wide_claim_without_repeating_whole(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #322 review/gate: the stored claim's own `whole_reason` already
    justifies a wide scope once; resuming it must not re-demand `--whole`."""
    repo, _remote = _real_repository_with_bare_remote(tmp_path)
    for name in ("a.py", "b.py", "c.py", "d.py"):
        (repo / name).write_text("x\n")
    _real_git(repo, "add", *("a.py", "b.py", "c.py", "d.py"))
    _real_git(repo, "commit", "-q", "-m", "initial")
    _push_repository_trunk(repo, "origin")
    issue = board_issue(
        314,
        "Fresh Slug Title",
        complete_contract("Build it.", scope=["a.py", "b.py", "c.py", "d.py"]),
    )
    client = FakeForge(board_issues=(issue,))
    client.issue_references[314] = forge.ItemReference(
        forge.ItemState.OPEN, issue.title, issue.body
    )
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    _patch_store_write(monkeypatch)
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})
    _redirect_toplevel(monkeypatch, repo)
    monkeypatch.chdir(repo)
    assert (
        issue_claim.main(["--repo", REPOSITORY, "start", "314", "--whole", "the whole thing"]) == 0
    )
    capsys.readouterr()

    status = issue_claim.main(["--repo", REPOSITORY, "start", "314"])

    assert status == 0
    assert "CLAIMED issue #314" in capsys.readouterr().out


def test_start_mints_a_fresh_id_after_an_abandoned_release(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #322 review finding 1: an abandoned release frees the claim but
    never touches the worktree; the next `start` resumes that same worktree
    yet still mints a brand-new id, never reusing a terminal one a
    deterministic `start-314` id would have left behind."""
    repo = _start_scenario(monkeypatch, tmp_path)
    monkeypatch.chdir(repo)
    assert issue_claim.main(["--repo", REPOSITORY, "start", "314"]) == 0
    first_claim_id = _claimed_line_id(capsys.readouterr().out, "issue #314")
    worktree = repo.parent / f"{repo.name}-worktrees" / _START_WORKTREE_NAME

    assert (
        issue_claim.main(
            ["--repo", REPOSITORY, "release", "314", "--abandoned", "stopped for the day"]
        )
        == 0
    )
    capsys.readouterr()

    assert issue_claim.main(["--repo", REPOSITORY, "start", "314"]) == 0

    second_claim_id = _claimed_line_id(capsys.readouterr().out, "issue #314")
    assert second_claim_id != first_claim_id
    assert worktree.exists()


def test_start_mints_a_fresh_id_after_a_merged_release_reopens_the_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #322 review finding 1: a `--merged` release removes #72's own
    worktree and branch entirely; once the item reopens, `start` builds a
    fresh worktree and mints a brand-new id rather than failing before CAS
    on a terminal id a deterministic `start-72` would have left behind."""
    repo, _remote = _real_repository_with_bare_remote(tmp_path)
    (repo / "base.txt").write_text("base\n")
    _real_git(repo, "add", "base.txt")
    _real_git(repo, "commit", "-q", "-m", "initial")
    _push_repository_trunk(repo, "origin")
    issue = board_issue(72, "Cleanup", complete_contract("Build it.", scope=["src"]))
    client = FakeForge(board_issues=(issue,))
    client.issue_references[72] = forge.ItemReference(forge.ItemState.OPEN, issue.title, issue.body)
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    _patch_store_write(monkeypatch)
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})
    _redirect_toplevel(monkeypatch, repo)
    monkeypatch.chdir(repo)
    assert issue_claim.main(["--repo", REPOSITORY, "start", "72"]) == 0
    first_claim_id = _claimed_line_id(capsys.readouterr().out, "issue #72")
    worktree = repo.parent / f"{repo.name}-worktrees" / _CLEANUP_WORKTREE_NAME

    (worktree / "feature.txt").write_text("feature\n")
    _real_git(worktree, "add", "feature.txt")
    _real_git(worktree, "commit", "-q", "-m", "feature work")
    _real_git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "-m",
        "Merge feature",
        "-m",
        f"Work-Item: #{WORK_ITEM_ISSUE}",
        _CLEANUP_BRANCH,
    )
    merge_commit = _real_git(repo, "rev-parse", "HEAD").stdout.strip()
    _push_repository_trunk(repo, "origin")
    client.landings[12] = landing_pull_request(
        body=f"Work-Item: #{WORK_ITEM_ISSUE}\n\nCloses #{WORK_ITEM_ISSUE}",
        merged=True,
        head_ref_name=_CLEANUP_BRANCH,
        merge_commit=merge_commit,
    )
    client.closed_issues.add(WORK_ITEM_ISSUE)
    monkeypatch.setattr(issue_claim, "_fetch_issue_reference", _LIVE_FETCH_ISSUE_REFERENCE)
    monkeypatch.setattr(checkout, "trunk_landings", _LIVE_TRUNK_LANDINGS)

    assert issue_claim.main(["--repo", REPOSITORY, "release", "72", "--merged", "12"]) == 0
    assert not worktree.exists()

    client.closed_issues.discard(WORK_ITEM_ISSUE)
    client.issue_references[72] = forge.ItemReference(forge.ItemState.OPEN, issue.title, issue.body)

    assert issue_claim.main(["--repo", REPOSITORY, "start", "72"]) == 0

    reopened_claim_id = _claimed_line_id(capsys.readouterr().out, "issue #72")
    assert reopened_claim_id != first_claim_id
    assert worktree.exists()


@pytest.mark.parametrize(
    ("closed", "message"),
    [
        pytest.param(True, "issue #314 is closed", id="closed"),
        pytest.param(False, "issue #314 does not exist here", id="missing"),
    ],
)
def test_start_refuses_a_closed_or_missing_item(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    closed: bool,
    message: str,
) -> None:
    repo = _start_scenario(monkeypatch, tmp_path)
    monkeypatch.chdir(repo)
    client = FakeForge()
    if closed:
        client.closed_issues.add(314)
    else:
        client.issue_references[314] = forge.ItemReference(forge.ItemState.MISSING, None, None)
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)

    assert issue_claim.main(["--repo", REPOSITORY, "start", "314"]) == 2

    assert capsys.readouterr().err == f"ERROR: {message}\n"
    assert not (repo.parent / f"{repo.name}-worktrees").exists()


def test_start_refuses_a_malformed_body_before_no_scope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #310 finding 43, mirrored for `start` (issue #406): a target
    whose `agent-claim` block is malformed refuses by naming that defect --
    the same block-defect reader `claim` shares -- before `start`'s own
    claim delegation ever gets to name the less specific "item names no
    scope"."""
    repo = _start_scenario(monkeypatch, tmp_path)
    monkeypatch.chdir(repo)
    body = agent_claim_body(f'{MINIMAL_BLOCK_TOML}owner = "someone"\n')
    issue = board_issue(314, "Fresh Slug Title", body)
    client = FakeForge(board_issues=(issue,))
    client.issue_references[314] = forge.ItemReference(
        forge.ItemState.OPEN, issue.title, issue.body
    )
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)

    status = issue_claim.main(["--repo", REPOSITORY, "start", "314"])

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: #314 body malformed: owner: unknown top-level key owner\n"
    )


def _state_ref_item_body(title: str, *, scope: list[str] | None = None) -> str:
    data: dict[str, object] = {
        "version": 1,
        "now": "Ship it.",
        "next": "keiner",
        "done_when": "Merged.",
        "record": {
            "title": title,
            "state": "open",
            "kind": "task",
            "labels": [],
            "blocked_by": [],
            "created_at": "2026-09-10T00:00:00Z",
            "updated_at": "2026-09-10T00:00:00Z",
        },
    }
    if scope is not None:
        data["scope"] = scope
    return f"Prose.\n\n```agent-claim\n{body.render_block(data)}```\n"


def _real_state_ref_start_scenario(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Path, protocol.ObjectId]:
    """A real bare-remote-backed, `storage = "state-ref"` repository (issue
    #322 review finding 2) with item #314 open, untitled scope, ready for
    `start` -- the state-ref counterpart of `_start_scenario`, real
    `refs/aco/state` and all (`_use_real_store`), since `_FakeStore` cannot
    see which directory a read ran against."""
    _use_real_store(monkeypatch)
    repo, remote = _real_repository_with_bare_remote(tmp_path)
    config_dir = repo / ".agent-claim"
    config_dir.mkdir()
    (config_dir / "board.toml").write_text('storage = "state-ref"\n')
    _real_git(repo, "add", ".agent-claim/board.toml")
    _real_git(repo, "commit", "-q", "-m", "pin state-ref storage")
    _push_repository_trunk(repo, "origin")
    store.bootstrap(worktree=repo, remote=str(remote))
    content = _state_ref_item_body("Fresh Slug Title").encode()
    seeded_oid = store.hash_blob(repo, content)
    store.commit_transition(
        worktree=repo,
        remote=str(remote),
        subject=store.TransitionSubject(f"seed item {items.format_item_id(314)}"),
        intent=protocol.ItemWriteIntent(
            item_id=items.format_item_id(314),
            expected=None,
            new_oid=seeded_oid,
            operation_id="item-op-314",
        ),
    )
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})
    _redirect_toplevel(monkeypatch, repo)
    monkeypatch.chdir(repo)
    return repo, remote, seeded_oid


def test_state_ref_forge_resolves_default_branch_from_its_own_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #322 review finding 1: `_state_ref_forge`'s default-branch read
    must be scoped to its own `directory` parameter -- `start`'s freshly
    created worktree, once one is handed in -- never left to read
    `origin/HEAD` from the calling process's own cwd regardless of it. A
    fake `checkout.default_branch_name` capturing the `directory` it
    receives is the right proof here: the argument itself is the contract
    this finding is about, not a git outcome a real checkout could also
    produce by coincidence (every worktree of one repository tracks the
    same `origin/HEAD`). Driven directly against `_state_ref_forge` (issue
    #322 review, delta finding 1) rather than through `aco start` end to
    end, since the full CLI flow also calls `checkout.default_branch_name`
    from `_validate_worktree_branch`'s own, already worktree-scoped call
    site and would conflate the two."""
    _use_real_store(monkeypatch)
    repo, remote = _real_repository_with_bare_remote(tmp_path)
    (repo / "base.txt").write_text("base\n")
    _real_git(repo, "add", "base.txt")
    _real_git(repo, "commit", "-q", "-m", "initial")
    _push_repository_trunk(repo, "origin")
    store.bootstrap(worktree=repo, remote=str(remote))
    recorded_directories: list[Path | None] = []

    def fake_default_branch_name(*, directory: Path | None = None) -> str | None:
        recorded_directories.append(directory)
        return "main"

    monkeypatch.setattr(checkout, "default_branch_name", fake_default_branch_name)

    issue_claim._state_ref_forge(None, "origin", directory=repo)

    assert recorded_directories == [repo]


def test_start_under_state_ref_claims_from_the_worktree_it_creates(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #322 review finding 2: under `storage = "state-ref"`, `start`
    must claim through a forge directed at the worktree it just created,
    never the one it cached from the caller checkout before that worktree
    existed. A real bare remote and a real `refs/aco/state` (`_use_real_store`)
    prove it end to end: the claim `start` prints is read back through
    `store.fetch_state` scoped to the worktree path itself, not the
    original checkout. Both checkouts share the same bare remote, so that
    final read alone would also pass under the pre-fix bug -- reusing
    `session.forge()`'s cached instance for the claim never even calls
    `_state_ref_forge` a second time, but the claim write itself is
    resolved by `_cmd_claim`'s own `directory` argument regardless of which
    forge instance served the claim, so it lands in the right place either
    way. The recorded `directory` each `_state_ref_forge` call receives
    (issue #322 review, delta finding 2) is what distinguishes the two: a
    reused forge never resolves a second one at all, while the fix's fresh
    instance resolves its last one against the created worktree."""
    repo, _remote, _seeded_oid = _real_state_ref_start_scenario(monkeypatch, tmp_path)
    item_id = items.format_item_id(314)
    real_state_ref_forge = issue_claim._state_ref_forge
    recorded_directories: list[Path | None] = []

    def recording_state_ref_forge(
        repo_argument: str | None, canonical_remote: str, *, directory: Path | None = None
    ) -> state_board.StateRefBoard:
        recorded_directories.append(directory)
        return real_state_ref_forge(repo_argument, canonical_remote, directory=directory)

    monkeypatch.setattr(issue_claim, "_state_ref_forge", recording_state_ref_forge)

    status = issue_claim.main(["start", "314", "--scope", "src/x.py"])

    assert status == 0
    worktree = repo.parent / f"{repo.name}-worktrees" / _START_WORKTREE_NAME
    assert recorded_directories[-1] == worktree
    claim_id = _claimed_line_id(capsys.readouterr().out, f"issue {item_id}")
    live = store.fetch_state(worktree=worktree, remote="origin").claims
    claim = live[protocol.claim_key(protocol.IssueIdentity(314), _START_BRANCH)]
    assert claim.claim_id == claim_id
    assert claim.scope == ("src/x.py",)


def test_start_under_state_ref_claims_against_the_item_current_when_the_worktree_exists(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #322 review finding 2 (the fix's own gate): item #314 gains a
    `scope` after `start`'s pre-worktree forge read but before its claim
    step, exactly the window a caller-checkout-cached forge cannot see. A
    fresh forge built from the worktree after `resolve_or_create_worktree`
    re-reads the item and refuses the now-mismatched `--scope`; a forge
    reused from the pre-worktree read would still see no `scope` at all and
    wrongly let the claim through."""
    repo, remote, seeded_oid = _real_state_ref_start_scenario(monkeypatch, tmp_path)
    item_id = items.format_item_id(314)
    real_resolve_or_create_worktree = checkout.resolve_or_create_worktree

    def advance_item_then_create_worktree(path: Path, branch: str, *, remote: str) -> None:
        real_resolve_or_create_worktree(path, branch, remote=remote)
        advanced = _state_ref_item_body("Fresh Slug Title", scope=["mismatched/path.py"]).encode()
        advanced_oid = store.hash_blob(repo, advanced)
        store.commit_transition(
            worktree=repo,
            remote=str(remote_path),
            subject=store.TransitionSubject(f"advance item {item_id}"),
            intent=protocol.ItemWriteIntent(
                item_id=item_id,
                expected=seeded_oid,
                new_oid=advanced_oid,
                operation_id="item-op-314-race",
            ),
        )

    remote_path = remote
    monkeypatch.setattr(checkout, "resolve_or_create_worktree", advance_item_then_create_worktree)

    status = issue_claim.main(["start", "314", "--scope", "src/x.py"])

    assert status == 2
    assert capsys.readouterr().err == f"ERROR: {issue_claim.CLAIM_SCOPE_MISMATCH}\n"
    worktree = repo.parent / f"{repo.name}-worktrees" / _START_WORKTREE_NAME
    live = store.fetch_state(worktree=worktree, remote="origin").claims
    assert protocol.claim_key(protocol.IssueIdentity(314), _START_BRANCH) not in live


def test_claim_accepts_an_item_with_no_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    issue = board_issue(10, "Work", complete_contract("Claim #10."), labels=("security",))
    _configured_board_client(monkeypatch, tmp_path, open_issues=(issue,))
    monkeypatch.setattr(
        issue_claim,
        "_request",
        lambda _arguments, **_kwargs: request(issue=10, scope=("src/work.py",)),
    )

    assert (
        issue_claim.main(
            [
                "--repo",
                REPOSITORY,
                "claim",
                "10",
                "--agent",
                "Codex Sol",
                "--scope",
                "src/work.py",
            ]
        )
        == 0
    )

    assert store.fetch_state(worktree=Path("."), remote="origin").claims
    assert "ERROR:" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("dependencies", "expected_blockers"),
    [
        pytest.param((block_dependency(9),), "#9", id="single-open-dependency"),
        pytest.param(
            (block_dependency(9), block_dependency(11)), "#9, #11", id="two-open-dependencies"
        ),
        pytest.param(
            (block_dependency(9), block_dependency(11, is_pull_request=True)),
            "#9, #11",
            id="a-pull-request-dependency-blocks-like-any-other",
        ),
        pytest.param(
            (block_dependency(7, repository="overnightworks/other-repo"),),
            "overnightworks/other-repo#7",
            id="a-foreign-dependency-blocks-and-is-named-qualified",
        ),
    ],
)
def test_claim_refuses_an_open_dependency_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    dependencies: tuple[board.IssueDependency, ...],
    expected_blockers: str,
) -> None:
    issue, blocked_by = blocked_issue(10, "Work", *dependencies, labels=("security",))
    _configured_board_client(monkeypatch, tmp_path, open_issues=(issue,), dependencies=blocked_by)
    monkeypatch.setattr(
        issue_claim,
        "_request",
        lambda _arguments, **_kwargs: request(issue=10, scope=("src/work.py",)),
    )

    assert (
        issue_claim.main(
            [
                "--repo",
                REPOSITORY,
                "claim",
                "10",
                "--agent",
                "Codex Sol",
                "--scope",
                "src/work.py",
            ]
        )
        == 2
    )

    captured = capsys.readouterr()
    assert captured.err == (
        f"ERROR: #10 is blocked by {expected_blockers} (open); "
        "pass --out-of-order REASON to claim it anyway\n"
    )


def test_claim_ignores_a_closed_dependency(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    closed = block_dependency(
        9, state=board.BlockerState.CLOSED, closed_at=datetime(2026, 8, 20, tzinfo=UTC)
    )
    issue, blocked_by = blocked_issue(10, "Work", closed, labels=("security",))
    _configured_board_client(monkeypatch, tmp_path, open_issues=(issue,), dependencies=blocked_by)
    monkeypatch.setattr(
        issue_claim,
        "_request",
        lambda _arguments, **_kwargs: request(issue=10, scope=("src/work.py",)),
    )

    assert (
        issue_claim.main(
            [
                "--repo",
                REPOSITORY,
                "claim",
                "10",
                "--agent",
                "Codex Sol",
                "--scope",
                "src/work.py",
            ]
        )
        == 0
    )

    assert store.fetch_state(worktree=Path("."), remote="origin").claims
    assert "ERROR:" not in capsys.readouterr().err


def test_claim_allows_an_open_dependency_with_out_of_order_and_records_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    reason = "Blocker #9 is stuck on review; unblocking manually."
    blocker = board_issue(9, "Blocker 9", complete_contract("Claim #9."))
    issue, blocked_by = blocked_issue(10, "Work", block_dependency(9), labels=("security",))
    _configured_board_client(
        monkeypatch, tmp_path, open_issues=(issue, blocker), dependencies=blocked_by
    )
    monkeypatch.setattr(
        issue_claim,
        "_request",
        lambda _arguments, **_kwargs: replace(
            request(issue=10, scope=("src/work.py",)), out_of_order_reason=reason
        ),
    )

    assert (
        issue_claim.main(
            [
                "--repo",
                REPOSITORY,
                "claim",
                "10",
                "--agent",
                "Codex Sol",
                "--scope",
                "src/work.py",
                "--out-of-order",
                reason,
            ]
        )
        == 0
    )

    output = capsys.readouterr().out
    assert "WARNING: #10 is blocked by #9 (open)" in output


def test_claim_refuses_a_malformed_block_before_mutation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A body whose block carries a key the schema does not define is refused
    by name -- the typed successor to prose's duplicate-section defect."""
    issue = board_issue(10, "Work", agent_claim_body(f'{MINIMAL_BLOCK_TOML}owner = "someone"\n'))
    _configured_board_client(monkeypatch, tmp_path, open_issues=(issue,))
    monkeypatch.setattr(
        issue_claim,
        "_request",
        lambda _arguments, **_kwargs: request(issue=10, scope=("src/work.py",)),
    )

    assert (
        issue_claim.main(
            [
                "--repo",
                REPOSITORY,
                "claim",
                "10",
                "--agent",
                "Codex Sol",
                "--scope",
                "src/work.py",
                "--json",
            ]
        )
        == 2
    )

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["reason"] == "precondition_failed"
    assert payload["checks"] == [
        {
            "level": "error",
            "check": "body-contract",
            "text": "body malformed: owner: unknown top-level key owner",
            "slice": None,
            "issue": None,
        }
    ]


def test_claim_ignores_body_size_and_closed_next_references(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    issue = board_issue(
        10,
        "Work",
        complete_contract("#9 follow up.") + "\n\n" + "x" * 50_000,
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(issue,))
    monkeypatch.setattr(
        issue_claim,
        "_fetch_issue_reference",
        lambda _client, _number: pytest.fail("claim must not inspect Next references"),
    )
    monkeypatch.setattr(
        issue_claim,
        "_request",
        lambda _arguments, **_kwargs: request(issue=10, scope=("src/work.py",)),
    )

    assert (
        issue_claim.main(
            [
                "--repo",
                REPOSITORY,
                "claim",
                "10",
                "--agent",
                "Codex Sol",
                "--scope",
                "src/work.py",
            ]
        )
        == 0
    )

    assert store.fetch_state(worktree=Path("."), remote="origin").claims
    assert "ERROR:" not in capsys.readouterr().err


def test_release_ignores_body_contract_defects(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeForge()
    client.board_issues = (
        board_issue(
            10,
            "Work",
            complete_contract("Claim #10.") + "\n\n**Done when:** Duplicate.",
        ),
    )
    monkeypatch.setattr(
        client, "list_open_board_issues", lambda: pytest.fail("release checks no body")
    )
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    standing = request("held", issue=10, scope=("src/work.py",))
    _patch_store_write(monkeypatch, _store_claim_from_request(standing))

    assert (
        issue_claim.main(
            [
                "--repo",
                REPOSITORY,
                "release",
                "10",
                "--agent",
                "Codex Sol",
                "--claim-id",
                "held",
                "--abandoned",
                "stopped",
            ]
        )
        == 0
    )

    assert "RELEASED issue #10: held" in capsys.readouterr().out


def test_claim_refuses_when_the_higher_priority_item_needs_refining(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = FakeForge()
    unruled = board_issue(
        11,
        "Needs rulings",
        complete_contract(
            "Claim #11.",
            scope=["src/needs-rulings.py"],
            expectation=[proposed_expectation("Name it.", default="yes")],
        ),
    )
    waiting, waiting_dependencies = blocked_issue(
        12, "Waits for rulings", block_dependency(11), next_step="Claim #12."
    )
    ready = board_issue(10, "Ready work", complete_contract("Claim #10."))
    claimed_request = request(issue=10, scope=("src/work.py",))
    client.board_dependencies = dict(waiting_dependencies)
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (ready, unruled, waiting))
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(issue_claim, "_request", lambda _arguments, **_kwargs: claimed_request)

    assert (
        issue_claim.main(
            [
                "--repo",
                REPOSITORY,
                "claim",
                "10",
                "--agent",
                "Codex Sol",
                "--scope",
                "src/work.py",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert "ERROR: higher-priority actionable item #11" in captured.err
    assert "Needs rulings" in captured.err
    assert "--out-of-order REASON" in captured.err


def test_claim_parser_description_names_what_refuses_first() -> None:
    """`claim --help` must not send an agent to the README for what refuses
    first in practice (issue #201): the parser's own description, pinned at
    the layer that produces it, names an isolated worktree on a non-main
    branch, a clean tree before the first edit, and --scope paths being
    repository-relative."""
    parser = issue_claim._parser()
    subparsers_action = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    claim_parser = subparsers_action.choices["claim"]

    assert claim_parser.description == issue_claim.CLAIM_DESCRIPTION
    assert "isolated" in issue_claim.CLAIM_DESCRIPTION
    assert "non-main branch" in issue_claim.CLAIM_DESCRIPTION
    assert "clean" in issue_claim.CLAIM_DESCRIPTION
    assert "repository-relative" in issue_claim.CLAIM_DESCRIPTION


def test_help_lists_commands_in_their_stable_registration_order() -> None:
    """`aco --help`'s command order is part of the CLI's own contract (issue
    #372 R3): `_COMMAND_TABLE`'s dispatch-backed commands keep the table's
    own order, and `status`/`body` -- which dispatch outside that table,
    ahead of `_dispatch`'s own `_LazyForge` -- keep their original,
    interleaved positions rather than trailing behind every table entry."""
    parser = issue_claim._parser()
    subparsers_action = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )

    assert list(subparsers_action.choices) == [
        "bootstrap",
        "reset",
        "status",
        "board",
        "rulings",
        "next",
        "start",
        "claim",
        "release",
        "land",
        "rescope",
        "cut",
        "ask",
        "rule",
        "check",
        "body",
        "brief",
        "item",
        "protect",
        "register",
        "run",
        "login",
        "_run-at-login",
    ]


def _all_parser_help_texts(parser: argparse.ArgumentParser) -> tuple[str, ...]:
    texts = [parser.format_help()]
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            for sub_parser in action.choices.values():
                texts.extend(_all_parser_help_texts(sub_parser))
    return tuple(texts)


def test_readme_and_help_texts_carry_no_stale_state_ref_read_only_sentence() -> None:
    """Issue #292 proof 5: state-ref's write commands (`cut`/`rule`/`ask`/
    `item new`/`item edit`/`item close`, issues #283/#285/#287/#289/#291)
    are real; no README line and no `--help` text still calls the storage
    read-only, or names `item new`/`item edit`/`item close` as "not yet"
    built or "slice 4" future work. `state-ref`'s own still-true residual --
    a merged release's landing verification (#230 slice 6) -- keeps its own
    "not yet" sentence; this proof is about the write path itself, not that
    named residual."""
    readme = (Path(__file__).parent.parent / "README.md").read_text(encoding="utf-8")
    texts = (readme, *_all_parser_help_texts(issue_claim._parser()))
    for text in texts:
        for line in text.splitlines():
            lowered = line.lower()
            if "state-ref" not in lowered and "storage" not in lowered:
                continue
            assert "read-only" not in lowered
            assert "slice 4" not in lowered
            if "not yet" in lowered:
                assert "item new" not in lowered
                assert "item edit" not in lowered
                assert "item close" not in lowered


@pytest.mark.parametrize(
    ("command", "substrings"),
    [
        pytest.param(
            "claim",
            ("refuse", "without a reason", "priority actionable item is free"),
            id="claim-names-the-out-of-order-refusal",
        ),
        pytest.param(
            "claim",
            (
                "takes it from the item's own body when omitted",
                "lane mode always requires it",
            ),
            id="claim-names-where-an-omitted-scope-comes-from",
        ),
        pytest.param(
            "claim",
            ("--whole", "three paths", "directory", "quarter", "twelve"),
            id="claim-names-the-whole-reason",
        ),
        pytest.param(
            "rescope",
            ("--whole", "three paths"),
            id="rescope-names-the-whole-reason",
        ),
    ],
)
def test_help_text_names_the_refusal_or_source_it_documents(
    capsys: pytest.CaptureFixture[str], command: str, substrings: tuple[str, ...]
) -> None:
    """`claim --help` and `rescope --help` each name, in prose, the refusal
    or derivation source their own behaviour documents -- the out-of-order
    and wide-scope refusals, and (issue #337 proof 4, REVISE finding 2)
    where an omitted `--scope` comes from."""
    with pytest.raises(SystemExit) as exited:
        issue_claim.main([command, "--help"])

    assert exited.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    for substring in substrings:
        assert substring in help_text


@pytest.mark.parametrize(
    "arguments",
    [
        ["claim", "5", "--scope", "src", "--allow-directory", "x"],
        ["rescope", "5", "--allow-directory", "x"],
    ],
    ids=["claim", "rescope"],
)
def test_cli_claim_and_rescope_reject_the_removed_allow_directory_flag(
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        issue_claim.main(arguments)

    assert exited.value.code == 2

    command = arguments[0]
    with pytest.raises(SystemExit) as help_exited:
        issue_claim.main([command, "--help"])

    assert help_exited.value.code == 0
    assert "--allow-directory" not in capsys.readouterr().out


def test_claim_refuses_out_of_order_without_a_reason_before_mutating(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    client = FakeForge()
    issues = (
        board_issue(10, "Lower work", complete_contract("Claim #10.")),
        board_issue(11, "Top work", complete_contract("Claim #11.", scope=["src/top.py"])),
        board_issue(12, "Depends on top", complete_contract("Claim #12."), blocked_by_count=1),
    )
    client.board_dependencies = {12: (block_dependency(11),)}
    claimed_request = request("out-of-order", issue=10, scope=("src/lower.py",))
    monkeypatch.setattr(client, "list_open_board_issues", lambda: issues)
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(issue_claim, "_request", lambda _arguments, **_kwargs: claimed_request)

    arguments = [
        "--repo",
        REPOSITORY,
        "claim",
        "10",
        "--agent",
        "Codex Sol",
        "--scope",
        "src/lower.py",
    ]
    assert issue_claim.main(arguments) == 2
    captured = capsys.readouterr()

    assert "ERROR: higher-priority actionable item #11" in captured.err
    assert "Top work" in captured.err
    assert "--out-of-order REASON" in captured.err


def test_claim_allows_out_of_order_with_a_reason_and_records_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    client = FakeForge()
    issues = (
        board_issue(10, "Lower work", complete_contract("Claim #10.")),
        board_issue(11, "Top work", complete_contract("Claim #11.", scope=["src/top.py"])),
        board_issue(12, "Depends on top", complete_contract("Claim #12."), blocked_by_count=1),
    )
    client.board_dependencies = {12: (block_dependency(11),)}
    reason = "Urgent customer incident."
    claimed_request = replace(
        request("out-of-order", issue=10, scope=("src/lower.py",)),
        out_of_order_reason=reason,
    )
    monkeypatch.setattr(client, "list_open_board_issues", lambda: issues)
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(issue_claim, "_request", lambda _arguments, **_kwargs: claimed_request)

    assert (
        issue_claim.main(
            [
                "--repo",
                REPOSITORY,
                "claim",
                "10",
                "--agent",
                "Codex Sol",
                "--scope",
                "src/lower.py",
                "--out-of-order",
                reason,
            ]
        )
        == 0
    )
    output = capsys.readouterr().out

    assert "WARNING" in output
    assert "#11" in output


def test_claim_refuses_for_a_higher_priority_item_even_at_a_lower_score(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`board`/`next` rank a labelled blocker ahead of an unlabelled item even
    when the blocker scores lower; the out-of-order refusal must agree, or
    claiming the unlabelled item would silently skip past the very item
    `next` would have named.
    """
    client = FakeForge()
    blocker = board_issue(
        50,
        "Prerequisite the operator prioritized",
        complete_contract("Unblock #52.", scope=["src/prerequisite.py"]),
    )
    dependent, dependent_blockers = blocked_issue(
        52, "Depends on the prerequisite", block_dependency(50), next_step="Ship it."
    )
    in_flight_unlabelled = board_issue(51, "In-flight, unlabelled", complete_contract("Ship it."))
    client.board_dependencies = dict(dependent_blockers)
    open_pull_request = board.PullRequest(200, "Fixes #51", "", "branch")
    claimed_request = request("lower-priority", issue=51, scope=("src/lower.py",))
    monkeypatch.setattr(
        client, "list_open_board_issues", lambda: (blocker, dependent, in_flight_unlabelled)
    )
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: (open_pull_request,))
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(issue_claim, "_request", lambda _arguments, **_kwargs: claimed_request)

    assert (
        issue_claim.main(
            [
                "--repo",
                REPOSITORY,
                "claim",
                "51",
                "--agent",
                "Codex Sol",
                "--scope",
                "src/lower.py",
            ]
        )
        == 2
    )
    captured = capsys.readouterr()

    # #51 (score 40: in-flight + single next) outscores #50 (score 10: it
    # unblocks #52, text-only, single next), but #50 leads on the board
    # because it carries the higher-priority "blocker" bucket.
    assert "ERROR" in captured.err
    assert "#50" in captured.err


def test_claim_json_refusal_reports_out_of_order_without_mutating(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    lower = board_issue(10, "Lower work", complete_contract("Claim #10."))
    top = board_issue(11, "Top work", complete_contract("Claim #11.", scope=["src/top.py"]))
    dependent, dependent_blockers = blocked_issue(
        12, "Depends on top", block_dependency(11), next_step="Claim #12."
    )
    _configured_board_client(
        monkeypatch,
        tmp_path,
        open_issues=(lower, top, dependent),
        dependencies=dependent_blockers,
    )
    monkeypatch.setattr(
        issue_claim,
        "_request",
        lambda _arguments, **_kwargs: request(issue=10, scope=("src/lower.py",)),
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "10",
            "--agent",
            "Codex Sol",
            "--scope",
            "src/lower.py",
            "--json",
        ]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["reason"] == "precondition_failed"
    assert payload["issue"] == 10
    checks = payload["checks"]
    assert len(checks) == 1
    check = checks[0]
    assert check["level"] == "error"
    assert check["check"] == "out-of-order"
    assert check["issue"] == 11
    assert check["slice"] is None
    assert "#11" in check["text"]
    assert "Top work" in check["text"]
    assert "--out-of-order REASON" in check["text"]


def test_claim_does_not_require_out_of_order_for_the_top_ranked_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    top = board_issue(10, "Top work", complete_contract("Claim #10."))
    lower = board_issue(11, "Lower work", complete_contract("Claim #11."))
    _configured_board_client(monkeypatch, tmp_path, open_issues=(top, lower))
    monkeypatch.setattr(
        issue_claim,
        "_request",
        lambda _arguments, **_kwargs: request(issue=10, scope=("src/top.py",)),
    )

    assert (
        issue_claim.main(
            [
                "--repo",
                REPOSITORY,
                "claim",
                "10",
                "--agent",
                "Codex Sol",
                "--scope",
                "src/top.py",
            ]
        )
        == 0
    )
    assert "WARNING: higher-priority actionable item" not in capsys.readouterr().out
    assert store.fetch_state(worktree=Path("."), remote="origin").claims


@pytest.mark.parametrize(
    ("state", "check", "expected_text"),
    [
        (forge.ItemState.CLOSED, "closed-issue", "issue #72 is closed"),
        (forge.ItemState.MISSING, "missing-issue", "issue #72 does not exist here"),
    ],
    ids=["closed", "missing"],
)
def test_claim_refuses_a_closed_or_missing_target(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    state: forge.ItemState,
    check: str,
    expected_text: str,
) -> None:
    _configured_board_client(monkeypatch, tmp_path)
    _stub_issue_reference(monkeypatch, {72: (state, "Some title", "")})
    monkeypatch.setattr(
        issue_claim,
        "_request",
        lambda _arguments, **_kwargs: request(issue=72, scope=("src/work.py",)),
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Codex Sol",
            "--scope",
            "src/work.py",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert f"ERROR: {expected_text}" in captured.err


def test_claim_refuses_a_container(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    container = board.Issue(
        72,
        "Container work",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=body.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    monkeypatch.setattr(
        issue_claim,
        "_request",
        lambda _arguments, **_kwargs: request(issue=72, scope=("src/work.py",)),
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Codex Sol",
            "--scope",
            "src/work.py",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR: #72 is a container; claim a child" in captured.err


def test_claim_refuses_a_freshly_cut_childs_incomplete_skeleton(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """`cut`'s fresh child (`body.BLOCK_CHILD_SKELETON`) is defect-free but
    incomplete -- invisible to `next`, and now refused here too, exactly as
    ruled: `claim` requires a complete projection."""
    child = board_issue(101, "Scheibe 1", body.BLOCK_CHILD_SKELETON)
    _configured_board_client(monkeypatch, tmp_path, open_issues=(child,))
    monkeypatch.setattr(
        issue_claim,
        "_request",
        lambda _arguments, **_kwargs: request(issue=101, scope=("src/work.py",)),
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "101",
            "--agent",
            "Codex Sol",
            "--scope",
            "src/work.py",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR: #101 body incomplete: Now, Next, Done when" in captured.err


def test_claim_names_an_incomplete_body_even_when_the_item_is_also_blocked(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """`item.actionable_reason` names only the first applicable reason
    (frozen, claimed, blocked, then incomplete) -- that must not mask the
    incomplete-body refusal when another reason also applies (#112 finding
    2, delta review)."""
    blocker = board_issue(50, "Blocker", complete_contract("Ship it."))
    dependent = board_issue(
        51, "Dependent", complete_contract("", now="Work.", done_when=""), blocked_by_count=1
    )
    _configured_board_client(
        monkeypatch,
        tmp_path,
        open_issues=(blocker, dependent),
        dependencies={51: (block_dependency(50),)},
    )
    monkeypatch.setattr(
        issue_claim,
        "_request",
        lambda _arguments, **_kwargs: request(issue=51, scope=("src/work.py",)),
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "51",
            "--agent",
            "Codex Sol",
            "--scope",
            "src/work.py",
            "--json",
        ]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert "body-incomplete" in {check["check"] for check in payload["checks"]}


CUT_CONTAINER = 79


def _cut_container_issue(toml_text: str) -> board.Issue:
    return board.Issue(
        CUT_CONTAINER,
        "Epic",
        (),
        agent_claim_body(toml_text),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=body.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )


def _one_slice_container() -> board.Issue:
    return _cut_container_issue(f'{MINIMAL_BLOCK_TOML}[[slice]]\nindex = 1\ntitle = "Scheibe 1"\n')


def test_cut_refuses_a_non_container(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    plain = board_issue(CUT_CONTAINER, "Not a container", complete_contract("Ship it."))
    _configured_board_client(monkeypatch, tmp_path, open_issues=(plain,))

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 2
    assert f"ERROR: #{CUT_CONTAINER} is not a container" in capsys.readouterr().err


def test_cut_json_refusal_reports_precondition_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #425: every `cut` refusal before any write -- here CUT-02's own
    non-container target -- reports through the shared envelope as
    `precondition_failed`, never a bare object."""
    plain = board_issue(CUT_CONTAINER, "Not a container", complete_contract("Ship it."))
    _configured_board_client(monkeypatch, tmp_path, open_issues=(plain,))

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "cut",
            str(CUT_CONTAINER),
            "--title",
            "Scheibe 1",
            "--json",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert f"ERROR: #{CUT_CONTAINER} is not a container" in captured.err
    _assert_json_refusal_object(captured.err, captured.out, reason="precondition_failed")


def test_cut_refuses_a_number_that_names_no_open_issue(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _configured_board_client(monkeypatch, tmp_path, open_issues=())

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 2
    assert f"ERROR: #{CUT_CONTAINER} is not an open container" in capsys.readouterr().err


def test_cut_refuses_a_container_that_already_has_a_parent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(_one_slice_container(),))
    client.parents[CUT_CONTAINER] = board.ParentIssue(
        board.IssueReference(REPOSITORY, 1), "", body.ItemKind.CONTAINER
    )

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 2
    assert (
        f"ERROR: #{CUT_CONTAINER} is itself a child of {REPOSITORY}#1; "
        "nested containers are not supported" in capsys.readouterr().err
    )


@pytest.mark.parametrize(
    "operation",
    [
        forge.ForgeOperation.CREATE_CHILD,
        forge.ForgeOperation.LINK_CHILD,
        forge.ForgeOperation.UPDATE_ITEM_BODY,
    ],
)
def test_cut_refuses_when_the_forge_cannot_perform_a_required_write(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operation: forge.ForgeOperation,
) -> None:
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(_one_slice_container(),))
    client.capability_overrides[operation] = forge.Capability.READ_ONLY

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 2
    assert client.created_children == []
    assert client.item_bodies == {}
    assert (
        f"ERROR: this forge cannot {operation.value}; cut the slice by hand"
        in capsys.readouterr().err
    )


def test_cut_names_the_created_child_when_the_relation_post_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The sub-issue relation POST is `create_child`'s own second write --
    also not atomic with the first, so a failure there must name the child
    exactly as a failed block rewrite does (#112 finding 3)."""
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(_one_slice_container(),))
    client.fail_create_child_relation = True

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 2
    child = client.next_created_child_number - 1
    assert client.created_children == [
        (
            CUT_CONTAINER,
            "Scheibe 1",
            issue_claim._cut_child_body(CUT_CONTAINER),
            body.ItemKind.TASK,
        )
    ]
    assert client.item_bodies == {}
    err = capsys.readouterr().err
    assert f"created #{child} but failed to record #{child} as a sub-issue" in err
    assert "re-run the same cut -- it adopts the child" in err


def test_cut_json_reports_partial_write_with_written_and_failed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #425: a partial write's own `--json` shape carries `written`/
    `failed` as structured siblings next to `reason: "partial_write"`,
    never only the prose sentence stderr already printed."""
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(_one_slice_container(),))
    client.fail_create_child_relation = True

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "cut",
            str(CUT_CONTAINER),
            "--title",
            "Scheibe 1",
            "--json",
        ]
    )

    assert exit_code == 2
    child = client.next_created_child_number - 1
    captured = capsys.readouterr()
    message = (
        f"created #{child} but failed to record #{child} as a sub-issue of #{CUT_CONTAINER}: "
        "relation POST failed (simulated); re-run the same cut -- it adopts the child"
    )
    assert captured.err == f"ERROR: {message}\n"
    expected = {
        "ok": False,
        "reason": "partial_write",
        "written": child,
        "failed": f"record #{child} as a sub-issue of #{CUT_CONTAINER}",
        "message": message,
    }
    assert captured.out == json.dumps(expected) + "\n"


def _write_block_pin(tmp_path: Path) -> None:
    (tmp_path / ".agent-claim").mkdir(exist_ok=True)
    (tmp_path / ".agent-claim" / "board.toml").write_text('body_contract = "block"\n')


@pytest.mark.parametrize(
    ("toml_text", "scope_flags", "created_scope", "remaining_slice"),
    [
        pytest.param(
            f"{MINIMAL_BLOCK_TOML}"
            '[[slice]]\nindex = 1\ntitle = "Scheibe 1"\n'
            '[[slice]]\nindex = 2\ntitle = "Scheibe 2"\n',
            (),
            None,
            [{"index": 2, "title": "Scheibe 2"}],
            id="no-scope-leaves-the-remaining-row",
        ),
        pytest.param(
            f'{MINIMAL_BLOCK_TOML}[[slice]]\nindex = 1\ntitle = "Scheibe 1"\n',
            ("--scope", "src/c.py"),
            ("src/c.py",),
            [],
            id="a-scope-fills-the-row-and-becomes-the-childs-own-scope",
        ),
    ],
)
def test_cut_creates_a_child_and_removes_the_first_cuttable_slice(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    toml_text: str,
    scope_flags: tuple[str, ...],
    created_scope: tuple[str, ...] | None,
    remaining_slice: list[dict[str, object]],
) -> None:
    """`cut` creates the fresh child and removes the cut row from the
    container's own slice table; with `--scope` (issue #337 proof 2, REVISE
    finding 2), that scope fills the row and becomes the fresh child's own
    top-level scope under GitHub storage too -- the same rule
    `test_cut_scope_fills_inherits_or_refuses_against_a_slice_rows_scope`
    proves end to end under `state-ref`."""
    container = _cut_container_issue(toml_text)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    _write_block_pin(tmp_path)

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "cut",
            str(CUT_CONTAINER),
            "--title",
            "Scheibe 1",
            *scope_flags,
        ]
    )

    assert exit_code == 0
    child = client.next_created_child_number - 1
    assert client.created_children == [
        (
            CUT_CONTAINER,
            "Scheibe 1",
            issue_claim._cut_child_body(CUT_CONTAINER, created_scope),
            body.ItemKind.TASK,
        )
    ]
    new_data = body.locate_agent_claim_block(client.item_bodies[CUT_CONTAINER]).data
    assert new_data["slice"] == remaining_slice
    assert capsys.readouterr().out == f"CUT #{CUT_CONTAINER} row 1 -> #{child}\n"


def test_cut_selects_a_row_by_number_and_removes_only_that_entry(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    toml_text = (
        f"{MINIMAL_BLOCK_TOML}"
        '[[slice]]\nindex = 1\ntitle = "Scheibe 1"\n'
        '[[slice]]\nindex = 2\ntitle = "Scheibe 2"\n'
    )
    container = _cut_container_issue(toml_text)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    _write_block_pin(tmp_path)

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "cut",
            str(CUT_CONTAINER),
            "--title",
            "Scheibe 2",
            "--row",
            "2",
            "--json",
        ]
    )

    assert exit_code == 0
    child = client.next_created_child_number - 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "reason": "cut",
        "container": CUT_CONTAINER,
        "row": 2,
        "child": child,
    }
    remaining = body.locate_agent_claim_block(client.item_bodies[CUT_CONTAINER]).data
    assert remaining["slice"] == [{"index": 1, "title": "Scheibe 1"}]


def test_cut_creates_an_untied_child_with_no_slice_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    container = _cut_container_issue(MINIMAL_BLOCK_TOML)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    _write_block_pin(tmp_path)

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "cut", str(CUT_CONTAINER), "--title", "Untied"]
    )

    assert exit_code == 0
    assert client.item_bodies == {}
    child = client.next_created_child_number - 1
    assert capsys.readouterr().out == f"CUT #{CUT_CONTAINER} -> #{child}\n"


def test_cut_creates_an_untied_child_when_slice_is_explicitly_empty(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    toml_text = f"{MINIMAL_BLOCK_TOML}slice = []\n"
    container = _cut_container_issue(toml_text)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    _write_block_pin(tmp_path)

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "cut", str(CUT_CONTAINER), "--title", "Untied"]
    )

    assert exit_code == 0
    assert client.item_bodies == {}


def test_cut_refuses_a_row_with_no_slice_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    container = _cut_container_issue(MINIMAL_BLOCK_TOML)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    _write_block_pin(tmp_path)

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "cut",
            str(CUT_CONTAINER),
            "--title",
            "X",
            "--row",
            "1",
        ]
    )

    assert exit_code == 2
    assert (
        f"ERROR: #{CUT_CONTAINER} has no slice table; --row needs one to select a row from"
        in capsys.readouterr().err
    )
    assert client.created_children == []


def test_cut_refuses_a_row_with_no_cuttable_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`--row 9` names no entry while row 1 is still cuttable: the refusal
    names the requested row and the row that is actually still cuttable,
    not the unqualified (and false) claim that none is."""
    toml_text = f'{MINIMAL_BLOCK_TOML}[[slice]]\nindex = 1\ntitle = "Scheibe 1"\n'
    container = _cut_container_issue(toml_text)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    _write_block_pin(tmp_path)

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "cut",
            str(CUT_CONTAINER),
            "--title",
            "X",
            "--row",
            "9",
        ]
    )

    assert exit_code == 2
    assert capsys.readouterr().err == f"ERROR: #{CUT_CONTAINER} has no row 9; cuttable rows: 1\n"
    assert client.created_children == []


def test_cut_refuses_a_title_mismatch_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}[[slice]]\nindex = 1\ntitle = "Scheibe 1"\n'
    container = _cut_container_issue(toml_text)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    _write_block_pin(tmp_path)

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "cut", str(CUT_CONTAINER), "--title", "Wrong title"]
    )

    assert exit_code == 2
    assert (
        f"ERROR: #{CUT_CONTAINER}'s slice 1 is titled 'Scheibe 1'; --title must match it exactly"
        in capsys.readouterr().err
    )
    assert client.created_children == []
    assert client.item_bodies == {}


def test_cut_refuses_a_blockless_container_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    container = board.Issue(
        CUT_CONTAINER,
        "Epic",
        (),
        "## Now\nOld prose.\n",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=body.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    _write_block_pin(tmp_path)

    exit_code = issue_claim.main(["--repo", REPOSITORY, "cut", str(CUT_CONTAINER), "--title", "X"])

    assert exit_code == 2
    assert "body malformed: agent-claim: no agent-claim block" in capsys.readouterr().err
    assert client.created_children == []


def test_cut_refuses_a_malformed_container_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    container = _cut_container_issue('version = 2\nnow = "N"\nnext = "X"\ndone_when = "D"\n')
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    _write_block_pin(tmp_path)

    exit_code = issue_claim.main(["--repo", REPOSITORY, "cut", str(CUT_CONTAINER), "--title", "X"])

    assert exit_code == 2
    assert (
        f"ERROR: #{CUT_CONTAINER} body malformed: version: version must be exactly 1; "
        "cut needs a valid agent-claim block" in capsys.readouterr().err
    )
    assert client.created_children == []


def test_cut_names_the_created_child_when_linking_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}[[slice]]\nindex = 1\ntitle = "Scheibe 1"\n'
    container = _cut_container_issue(toml_text)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    _write_block_pin(tmp_path)
    client.fail_update_item_body = True

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 2
    child = client.next_created_child_number - 1
    assert client.created_children == [
        (
            CUT_CONTAINER,
            "Scheibe 1",
            issue_claim._cut_child_body(CUT_CONTAINER),
            body.ItemKind.TASK,
        )
    ]
    err = capsys.readouterr().err
    assert (
        f"created #{child} but failed to remove row 1 from #{CUT_CONTAINER}'s agent-claim block"
        in err
    )
    assert "re-run the same cut -- it adopts the child" in err


def _forge_with_existing_child(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    child_number: int,
    child_state: board.ChildState,
    child_title: str = "Scheibe 1",
) -> FakeForge:
    """`_one_slice_container`'s forge, already carrying one child titled
    `child_title` under `CUT_CONTAINER` -- the fixture every adopt-instead-of-
    duplicate test (#260) starts from. The child also carries a recorded
    parent and, when open, sits on the board with a body that would itself
    pass `_orphan_names_container`, exactly like a real linked issue: a
    broken `parent_issue` filter in `_adoptable_child` would then double-count
    it as its own orphan, and the surrounding test would fail."""
    child_body = issue_claim._cut_child_body(CUT_CONTAINER)
    open_issues = (_one_slice_container(),)
    if child_state is board.ChildState.OPEN:
        open_issues = (
            *open_issues,
            board_issue(child_number, child_title, child_body, kind=body.ItemKind.TASK),
        )
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=open_issues)
    _write_block_pin(tmp_path)
    client.children[CUT_CONTAINER] = (board.ChildItem(child_number, child_state),)
    client.issue_references[child_number] = forge.ItemReference(
        forge.ItemState(child_state.value), child_title, child_body, False
    )
    client.parents[child_number] = board.ParentIssue(
        board.IssueReference(client.repository.path, CUT_CONTAINER), ""
    )
    return client


def test_cut_adopts_an_existing_open_child_instead_of_creating_one(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = _forge_with_existing_child(
        monkeypatch, tmp_path, child_number=950, child_state=board.ChildState.OPEN
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "cut",
            str(CUT_CONTAINER),
            "--title",
            "Scheibe 1",
            "--json",
        ]
    )

    assert exit_code == 0
    assert client.created_children == []
    assert client.created_issues == []
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "reason": "adopted",
        "container": CUT_CONTAINER,
        "row": 1,
        "child": 950,
    }
    remaining = body.locate_agent_claim_block(client.item_bodies[CUT_CONTAINER]).data
    assert remaining["slice"] == []


def test_cut_refuses_to_adopt_a_closed_child_with_a_matching_title(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = _forge_with_existing_child(
        monkeypatch, tmp_path, child_number=950, child_state=board.ChildState.CLOSED
    )

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 2
    assert client.created_children == []
    assert client.item_bodies == {}
    assert (
        f"ERROR: #{CUT_CONTAINER} already has a closed child #950 titled 'Scheibe 1'; "
        "reopen it or remove the row by hand" in capsys.readouterr().err
    )


def test_cut_refuses_to_adopt_when_two_open_issues_match_the_row_title(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """An already-linked open child and an orphan sharing the row's exact
    title are two live candidates `cut` cannot tell apart -- it refuses by
    name, naming both, rather than guess which one the failed `cut` that
    left the orphan behind actually created (#260)."""
    client = _forge_with_existing_child(
        monkeypatch, tmp_path, child_number=950, child_state=board.ChildState.OPEN
    )
    orphan = board_issue(
        951,
        "Scheibe 1",
        issue_claim._cut_child_body(CUT_CONTAINER),
        kind=body.ItemKind.TASK,
    )
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (_one_slice_container(), orphan))

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 2
    assert client.created_issues == []
    assert client.linked_children == []
    assert client.item_bodies == {}
    assert (
        f"ERROR: #{CUT_CONTAINER}'s row 'Scheibe 1' matches more than one open issue "
        "(#950, #951); adopt the right one by hand and remove the row" in capsys.readouterr().err
    )


@pytest.mark.parametrize(
    ("orphan", "idea_label"),
    [
        pytest.param(
            board_issue(
                951,
                "Scheibe 1",
                "Just an idea, someone should look into this.",
                kind=body.ItemKind.TASK,
            ),
            None,
            id="human_filed_issue_with_a_free_text_body",
        ),
        pytest.param(
            board_issue(
                951,
                "Scheibe 1",
                issue_claim._cut_child_body(CUT_CONTAINER),
                labels=("idea",),
                kind=body.ItemKind.TASK,
            ),
            "idea",
            id="idea_labelled_issue",
        ),
        pytest.param(
            board_issue(
                CUT_CONTAINER,
                "Scheibe 1",
                issue_claim._cut_child_body(CUT_CONTAINER),
                kind=body.ItemKind.TASK,
            ),
            None,
            id="the_container_itself",
        ),
        pytest.param(
            board_issue(951, "Scheibe 1", issue_claim._cut_child_body(80), kind=body.ItemKind.TASK),
            None,
            id="orphan_names_a_different_container_as_parent",
        ),
    ],
)
def test_cut_never_adopts_an_orphan_that_is_not_this_containers_recovery_shape(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    orphan: board.Issue,
    idea_label: str | None,
) -> None:
    """A title match alone is too weak to adopt an orphan (#260): a
    human-filed issue, an idea, the container's own issue, or another
    container's own failed-cut orphan can all share the row's exact title
    without being this container's recovery shape, so `cut` creates a fresh
    child instead of silently re-parenting any of them."""
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(_one_slice_container(),))
    _write_block_pin(tmp_path)
    if idea_label is not None:
        (tmp_path / ".agent-claim" / "board.toml").write_text(
            f'body_contract = "block"\nidea_label = "{idea_label}"\n'
        )
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (_one_slice_container(), orphan))

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 0
    child = client.next_created_child_number - 1
    assert client.linked_children == [(CUT_CONTAINER, child)]
    assert client.created_children == [
        (
            CUT_CONTAINER,
            "Scheibe 1",
            issue_claim._cut_child_body(CUT_CONTAINER),
            body.ItemKind.TASK,
        )
    ]
    assert capsys.readouterr().out == f"CUT #{CUT_CONTAINER} row 1 -> #{child}\n"


@pytest.mark.parametrize("line_ending", ["\n", "\r\n"], ids=["orphan_body_lf", "orphan_body_crlf"])
def test_cut_adopts_the_orphan_after_a_relation_partial_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    line_ending: str,
) -> None:
    """The real partial failure (#260): `create_issue` succeeds, `link_child`
    raises, so `create_child` names a child that exists but carries no
    recorded parent -- an orphan `list_open_board_issues`/`parent_issue`
    can find. An identical retry adopts it: the relation is written exactly
    once by the retry, the row is removed, and no second issue is ever
    created. Parametrized over the orphan's line ending because a real
    GitHub GET normalizes every body to CRLF (`board._line_ending`)
    regardless of what was written, and the retry's orphan scan must match
    both forms."""
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(_one_slice_container(),))
    _write_block_pin(tmp_path)
    client.board_issues = (_one_slice_container(),)
    monkeypatch.setattr(client, "list_open_board_issues", lambda: client.board_issues)
    client.fail_create_child_relation = True

    first_exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert first_exit_code == 2
    child = client.next_created_child_number - 1
    expected_body = issue_claim._cut_child_body(CUT_CONTAINER)
    assert client.created_issues == [("Scheibe 1", expected_body, body.ItemKind.TASK)]
    assert client.linked_children == [(CUT_CONTAINER, child)]
    capsys.readouterr()
    client.fail_create_child_relation = False
    client.board_issues = tuple(
        replace(issue, body=issue.body.replace("\n", line_ending))
        if issue.number == child
        else issue
        for issue in client.board_issues
    )

    second_exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert second_exit_code == 0
    assert client.created_issues == [("Scheibe 1", expected_body, body.ItemKind.TASK)]
    assert client.linked_children == [(CUT_CONTAINER, child), (CUT_CONTAINER, child)]
    remaining = body.locate_agent_claim_block(client.item_bodies[CUT_CONTAINER]).data
    assert remaining["slice"] == []
    assert capsys.readouterr().out == f"ADOPTED #{CUT_CONTAINER} row 1 -> #{child}\n"


RULE_ITEM = 90
RULE_TODAY = date(2026, 8, 21)  # `_freeze_cli_now` (autouse) pins `datetime.now(UTC)` here.


def _client_with_item(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    number: int,
    body: str,
    *,
    title: str = "Decide something",
    is_landing: bool = False,
    closed: bool = False,
) -> FakeForge:
    client = _configured_board_client(monkeypatch, tmp_path)
    state = forge.ItemState.CLOSED if closed else forge.ItemState.OPEN
    client.issue_references[number] = forge.ItemReference(state, title, body, is_landing)
    return client


@pytest.mark.parametrize("flag,ruling", [("--yes", "yes"), ("--no", "no"), ("--later", "later")])
def test_rule_writes_a_ruling_and_reports_remaining_open_lines(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    flag: str,
    ruling: str,
) -> None:
    toml_text = (
        f"{MINIMAL_BLOCK_TOML}"
        '[[expectation]]\ntext = "Ship it?"\ndefault = "later"\n'
        '[[expectation]]\ntext = "Ship it too?"\ndefault = "later"\n'
    )
    client = _client_with_item(monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(toml_text))

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "rule", str(RULE_ITEM), "--line", "1", flag]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == f"RULED #{RULE_ITEM} line 1 {ruling}; 1 line(s) still open\n"
    lines = body.expectation_lines(client.item_bodies[RULE_ITEM])
    assert lines[0] == body.ExpectationLine(1, "Ship it?", ruling, RULE_TODAY)
    assert lines[1] == body.ExpectationLine(2, "Ship it too?", None, None, default="later")


def test_rule_json_reports_item_index_ruling_date_and_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Ship it?"\ndefault = "later"\n'
    _client_with_item(monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(toml_text))

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "rule", str(RULE_ITEM), "--line", "1", "--yes", "--json"]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    expected = {
        "ok": True,
        "reason": "ruled",
        "item": RULE_ITEM,
        "index": 1,
        "ruling": "yes",
        "ruled_on": RULE_TODAY.isoformat(),
        "open": 0,
    }
    assert output == json.dumps(expected) + "\n"
    assert json.loads(output) == expected


def test_rule_appends_a_note_to_the_ruled_line_via_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Ship it?"\ndefault = "later"\n'
    client = _client_with_item(monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(toml_text))

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "rule",
            str(RULE_ITEM),
            "--line",
            "1",
            "--yes",
            "--note",
            "Ja, sofort.",
        ]
    )

    assert exit_code == 0
    lines = body.expectation_lines(client.item_bodies[RULE_ITEM])
    assert lines[0].text == "Ship it? Anmerkung: Ja, sofort."


def test_rule_refuses_an_already_ruled_line_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    toml_text = (
        f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Ship it?"\n'
        'ruling = "yes"\nruled_on = 2026-08-01\n'
    )
    client = _client_with_item(monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(toml_text))

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "rule", str(RULE_ITEM), "--line", "1", "--no", "--json"]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "line 1 is already ruled" in captured.err
    assert client.item_bodies == {}
    _assert_json_refusal_object(captured.err, captured.out, reason="already_ruled")


def test_rule_refuses_an_out_of_range_line_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Ship it?"\ndefault = "later"\n'
    client = _client_with_item(monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(toml_text))

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "rule", str(RULE_ITEM), "--line", "2", "--yes", "--json"]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "out of range" in captured.err
    assert client.item_bodies == {}
    _assert_json_refusal_object(captured.err, captured.out, reason="line_out_of_range")


def test_rule_refuses_when_the_forge_cannot_update_item_body(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Ship it?"\ndefault = "later"\n'
    client = _client_with_item(monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(toml_text))
    client.capability_overrides[forge.ForgeOperation.UPDATE_ITEM_BODY] = forge.Capability.READ_ONLY

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "rule", str(RULE_ITEM), "--line", "1", "--yes", "--json"]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert client.item_bodies == {}
    assert "ERROR: this forge cannot update_item_body; rule by hand" in captured.err
    _assert_json_refusal_object(captured.err, captured.out, reason="unavailable")


def test_rule_refuses_a_non_github_canonical_remote_by_host(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`rule` resolves its forge (`session.forge.writer()`) before its own
    typed-refusal handlers (issue #396 review finding): a resolution
    failure -- here a canonical remote on a host no adapter serves -- must
    still reach `rule`'s own `_refuse`, not `main`'s legacy `error` object."""
    monkeypatch.setattr(
        checkout, "remote_url", lambda remote: "file:///srv/git/agent-coordination.git"
    )

    def unused(*_args: object, **_kwargs: object) -> forge.RepositoryId:
        pytest.fail("rule must refuse the host before ever calling discover_repository")

    monkeypatch.setattr(github, "discover_repository", unused)

    status = issue_claim.main(["rule", "258", "--line", "1", "--yes", "--json"])

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err == "ERROR: no forge adapter for host file\n"
    _assert_json_refusal_object(captured.err, captured.out, reason="unavailable")


def test_rule_reports_invalid_usage_when_repo_is_given_under_state_ref(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A new `RULE-10` cites `specs/storage-pin.spec.md`'s PIN-04: under
    `storage = state-ref`, `--repo` is the wrong flag for this run,
    reported through `--json` as `invalid_usage` -- `rule`'s forge
    resolution must tell this apart from its generic `unavailable`
    bucket, the same distinction `brief` already draws."""
    _write_state_ref_pin(tmp_path)

    status = issue_claim.main(
        ["--repo", "acme/items", "rule", "258", "--line", "1", "--yes", "--json"]
    )

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err == "ERROR: --repo is meaningless under storage = state-ref\n"
    _assert_json_refusal_object(captured.err, captured.out, reason="invalid_usage")


def test_cli_claim_reports_invalid_usage_when_repo_is_given_under_state_ref(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A wide `--scope` with no `--whole` reads the item's own body for one
    (issue #399), resolving the forge before `_cmd_claim`'s own typed-
    refusal handlers ever see it (issue #406): under `storage = state-ref`,
    `--repo` is the wrong flag for this run, reported as `invalid_usage`
    rather than the generic `unavailable` bucket -- the same distinction
    `ask`/`rule`/`brief` already draw."""
    _write_state_ref_pin(tmp_path)
    client = FakeForge()
    arrange_scope_width(monkeypatch, client, versioned=("a.py", "b.py", "c.py", "d.py"))

    status = issue_claim.main(
        [
            "--repo",
            "acme/items",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "a.py",
            "--scope",
            "b.py",
            "--scope",
            "c.py",
            "--scope",
            "d.py",
            "--json",
        ]
    )

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err == "ERROR: --repo is meaningless under storage = state-ref\n"
    _assert_json_refusal_object(captured.err, captured.out, reason="invalid_usage")


def test_rule_refuses_a_missing_item_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = _configured_board_client(monkeypatch, tmp_path)
    client.issue_references[RULE_ITEM] = forge.ItemReference(forge.ItemState.MISSING)

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "rule", str(RULE_ITEM), "--line", "1", "--yes", "--json"]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert client.item_bodies == {}
    assert f"#{RULE_ITEM} does not exist" in captured.err
    _assert_json_refusal_object(captured.err, captured.out, reason="invalid_item")


def test_rule_refuses_a_pull_request_target_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Ship it?"\ndefault = "later"\n'
    client = _client_with_item(
        monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(toml_text), is_landing=True
    )

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "rule", str(RULE_ITEM), "--line", "1", "--yes", "--json"]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert client.item_bodies == {}
    assert f"#{RULE_ITEM} is a pull request, not an issue; rule needs an issue" in captured.err
    _assert_json_refusal_object(captured.err, captured.out, reason="invalid_item")


def test_ask_appends_a_proposed_line_and_rulings_shows_it_as_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    toml_text = (
        f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Ship it?"\n'
        'ruling = "yes"\nruled_on = 2026-08-01\n'
    )
    client = _client_with_item(monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(toml_text))

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "ask", str(RULE_ITEM), "--text", "New question?"]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == f"ASKED #{RULE_ITEM} line 2: New question?\n"

    new_body = client.item_bodies[RULE_ITEM]
    monkeypatch.setattr(
        client,
        "list_open_board_issues",
        lambda: (board_issue(RULE_ITEM, "Decide something", new_body),),
    )

    assert issue_claim.main(["--repo", REPOSITORY, "rulings"]) == 0
    assert capsys.readouterr().out == (
        f"#{RULE_ITEM} 1/2: Decide something\n"
        "  1 ruled yes 2026-08-01: Ship it?\n"
        "  2 open: New question?\n"
    )


FAILING_BODY_WRITES = [
    pytest.param(["ask", str(RULE_ITEM), "--text", "New question?"], id="ask"),
    pytest.param(["rule", str(RULE_ITEM), "--line", "1", "--yes"], id="rule"),
]


@pytest.mark.parametrize("arguments", FAILING_BODY_WRITES)
def test_a_failing_body_write_names_the_command_own_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    arguments: list[str],
) -> None:
    """ASK-12 and RULE-11 (issue #432): the write both commands end with can
    fail like any other forge call, and used to escape the handler with the
    bare sentence alone, leaving a `--json` caller nothing on stdout."""
    toml_text = f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Ship it?"\ndefault = "later"\n'
    client = _client_with_item(monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(toml_text))
    client.fail_update_item_body = True
    refusal = "update item body failed (simulated)"

    status = issue_claim.main(["--repo", REPOSITORY, *arguments, "--json"])

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err == f"ERROR: {refusal}\n"
    assert json.loads(captured.out) == {
        "ok": False,
        "reason": "unavailable",
        "message": refusal,
    }


def test_ask_json_reports_item_index_text_and_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _client_with_item(monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(MINIMAL_BLOCK_TOML))

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "ask",
            str(RULE_ITEM),
            "--text",
            "New question?",
            "--default",
            "later",
            "--json",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    expected = {
        "ok": True,
        "reason": "asked",
        "item": RULE_ITEM,
        "index": 1,
        "text": "New question?",
        "default": "later",
    }
    assert output == json.dumps(expected) + "\n"
    assert json.loads(output) == expected


def test_ask_refuses_a_blockless_item_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = _client_with_item(monkeypatch, tmp_path, RULE_ITEM, "## Now\nOld prose.\n")

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "ask",
            str(RULE_ITEM),
            "--text",
            "New question?",
            "--json",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert (
        "body malformed: agent-claim: no agent-claim block; ask needs a valid agent-claim block"
        in captured.err
    )
    assert client.item_bodies == {}
    _assert_json_refusal_object(captured.err, captured.out, reason="invalid_item")


def test_ask_refuses_when_the_forge_cannot_update_item_body(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = _client_with_item(
        monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(MINIMAL_BLOCK_TOML)
    )
    client.capability_overrides[forge.ForgeOperation.UPDATE_ITEM_BODY] = forge.Capability.READ_ONLY

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "ask",
            str(RULE_ITEM),
            "--text",
            "New question?",
            "--json",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert client.item_bodies == {}
    assert "ERROR: this forge cannot update_item_body; ask by hand" in captured.err
    _assert_json_refusal_object(captured.err, captured.out, reason="unavailable")


def test_ask_refuses_a_non_github_canonical_remote_by_host(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`ask` resolves its forge (`session.forge.writer()`) before its own
    typed-refusal handlers (issue #396 review finding): a resolution
    failure -- here a canonical remote on a host no adapter serves -- must
    still reach `ask`'s own `_refuse`, not `main`'s legacy `error` object."""
    monkeypatch.setattr(
        checkout, "remote_url", lambda remote: "file:///srv/git/agent-coordination.git"
    )

    def unused(*_args: object, **_kwargs: object) -> forge.RepositoryId:
        pytest.fail("ask must refuse the host before ever calling discover_repository")

    monkeypatch.setattr(github, "discover_repository", unused)

    status = issue_claim.main(["ask", "258", "--text", "New question?", "--json"])

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err == "ERROR: no forge adapter for host file\n"
    _assert_json_refusal_object(captured.err, captured.out, reason="unavailable")


def test_ask_reports_invalid_usage_when_repo_is_given_under_state_ref(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A new `ASK-11` cites `specs/storage-pin.spec.md`'s PIN-04: under
    `storage = state-ref`, `--repo` is the wrong flag for this run,
    reported through `--json` as `invalid_usage` -- `ask`'s forge
    resolution must tell this apart from its generic `unavailable`
    bucket, the same distinction `brief` already draws."""
    _write_state_ref_pin(tmp_path)

    status = issue_claim.main(
        ["--repo", "acme/items", "ask", "258", "--text", "New question?", "--json"]
    )

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err == "ERROR: --repo is meaningless under storage = state-ref\n"
    _assert_json_refusal_object(captured.err, captured.out, reason="invalid_usage")


def test_ask_refuses_blank_text_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = _client_with_item(
        monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(MINIMAL_BLOCK_TOML)
    )

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "ask", str(RULE_ITEM), "--text", "   ", "--json"]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "expectation text must be a non-empty string" in captured.err
    assert client.item_bodies == {}
    _assert_json_refusal_object(captured.err, captured.out, reason="invalid_expectation")


ASK_PICTURE_SVG = '<svg xmlns="http://www.w3.org/2000/svg"><circle cx="5" cy="5" r="4"/></svg>'


def test_ask_writes_question_example_and_picture(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #295: `--question`/`--example`/`--picture FILE.svg` land on the
    appended line -- the same `body.expectation_lines` projection `rulings`
    reads. The human `ASKED` line stays exactly what it was before."""
    client = _client_with_item(
        monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(MINIMAL_BLOCK_TOML)
    )
    picture_file = tmp_path / "sketch.svg"
    picture_file.write_text(ASK_PICTURE_SVG, encoding="utf-8")

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "ask",
            str(RULE_ITEM),
            "--text",
            "New question?",
            "--question",
            "Ship it?",
            "--example",
            "Release on Friday.",
            "--picture",
            str(picture_file),
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == f"ASKED #{RULE_ITEM} line 1: New question?\n"
    assert body.expectation_lines(client.item_bodies[RULE_ITEM]) == (
        body.ExpectationLine(
            1,
            "New question?",
            None,
            None,
            default="yes",
            question="Ship it?",
            example="Release on Friday.",
            picture=ASK_PICTURE_SVG,
        ),
    )


def test_ask_json_reports_question_example_and_picture_when_given(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _client_with_item(monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(MINIMAL_BLOCK_TOML))
    picture_file = tmp_path / "sketch.svg"
    picture_file.write_text(ASK_PICTURE_SVG, encoding="utf-8")

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "ask",
            str(RULE_ITEM),
            "--text",
            "New question?",
            "--question",
            "Ship it?",
            "--example",
            "Release on Friday.",
            "--picture",
            str(picture_file),
            "--json",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "reason": "asked",
        "item": RULE_ITEM,
        "index": 1,
        "text": "New question?",
        "default": "yes",
        "question": "Ship it?",
        "example": "Release on Friday.",
        "picture": ASK_PICTURE_SVG,
    }


def test_ask_refuses_an_invalid_picture_file_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = _client_with_item(
        monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(MINIMAL_BLOCK_TOML)
    )
    picture_file = tmp_path / "sketch.svg"
    picture_file.write_text("<div>not an svg</div>", encoding="utf-8")

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "ask",
            str(RULE_ITEM),
            "--text",
            "New question?",
            "--picture",
            str(picture_file),
            "--json",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "picture must be inline SVG rooted at <svg>" in captured.err
    assert client.item_bodies == {}
    _assert_json_refusal_object(captured.err, captured.out, reason="invalid_picture")


def test_ask_refuses_a_blank_question_with_invalid_expectation_not_invalid_picture(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`ExpectationFieldError` covers `question`, `example`, and `picture`
    alike (issue #396 review finding): only a refused `picture` names
    `invalid_picture`, per `specs/ask.spec.md`'s ASK-10 -- a refused
    `--question`/`--example` names `invalid_expectation` instead."""
    client = _client_with_item(
        monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(MINIMAL_BLOCK_TOML)
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "ask",
            str(RULE_ITEM),
            "--text",
            "New question?",
            "--question",
            "   ",
            "--json",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "question must be a non-empty string" in captured.err
    assert client.item_bodies == {}
    _assert_json_refusal_object(captured.err, captured.out, reason="invalid_expectation")


def test_ask_refuses_a_missing_picture_file_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = _client_with_item(
        monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(MINIMAL_BLOCK_TOML)
    )
    missing_file = tmp_path / "missing.svg"

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "ask",
            str(RULE_ITEM),
            "--text",
            "New question?",
            "--picture",
            str(missing_file),
        ]
    )

    assert exit_code == 2
    assert f"--picture {missing_file} could not be read" in capsys.readouterr().err
    assert client.item_bodies == {}


def test_next_prints_a_cut_command_block_mode_accepts_for_a_valid_container(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    toml_text = (
        'version = 1\nnow = "N"\nnext = "nichts"\ndone_when = "D"\n'
        '[[slice]]\nindex = 1\ntitle = "Scheibe 1"\n'
    )
    container = _cut_container_issue(toml_text)
    _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    _write_block_pin(tmp_path)

    exit_code = issue_claim.main(["--repo", REPOSITORY, "next"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert f'aco cut {CUT_CONTAINER} --title "Scheibe 1"' in out

    cut_exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )
    assert cut_exit_code == 0


def test_next_prints_a_cut_command_block_mode_accepts_a_differing_next_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """#177: a block container whose own `next` line still names work in
    its own words, while its first uncut `[[slice]]` entry carries a
    different title, must still print a `cut` command that `cut` itself
    accepts and that links exactly that entry. Without `--row`, `cut` links
    the first uncut entry and refuses unless `--title` matches its title
    exactly (atelier-2, seven live containers), so the printed command must
    carry the entry's title, never the `next` line's prose -- while the
    action line above it keeps naming the container's own words. `next
    --json` carries the same split as two fields: `slice` is that human
    step, `cut_title` is the title `cut` accepts -- a JSON consumer must
    build `--title` from `cut_title`, never `slice` (the README used to say
    otherwise)."""
    toml_text = (
        f'version = 1\nnow = "N"\nnext = "{_DIFFERING_NEXT_LINE}"\ndone_when = "D"\n'
        '[[slice]]\nindex = 1\ntitle = "Scheibe 1"\n'
    )
    container = _cut_container_issue(toml_text)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    _write_block_pin(tmp_path)

    json_exit_code = issue_claim.main(["--repo", REPOSITORY, "next", "--json"])
    assert json_exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "cut_slice"
    assert payload["slice"] == _DIFFERING_NEXT_LINE
    assert payload["cut_title"] == "Scheibe 1"

    json_cut_exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "cut",
            str(CUT_CONTAINER),
            "--title",
            payload["cut_title"],
        ]
    )
    assert json_cut_exit_code == 0
    capsys.readouterr()  # discard this leg's own "CUT #79 row 1 -> #N" line

    next_exit_code = issue_claim.main(["--repo", REPOSITORY, "next"])
    assert next_exit_code == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"cut_slice #{CUT_CONTAINER}: {_DIFFERING_NEXT_LINE}"
    command_line = out.splitlines()[1]
    cut_arguments = shlex.split(command_line.removeprefix("Next: aco "))

    cut_exit_code = issue_claim.main(["--repo", REPOSITORY, *cut_arguments])

    assert cut_exit_code == 0
    child = client.next_created_child_number - 1
    remaining_slice_entries = body.locate_agent_claim_block(client.item_bodies[CUT_CONTAINER]).data[
        "slice"
    ]
    assert remaining_slice_entries == []
    assert capsys.readouterr().out == f"CUT #{CUT_CONTAINER} row 1 -> #{child}\n"


def test_claim_json_refusal_carries_refused_issue_and_checks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    _configured_board_client(monkeypatch, tmp_path)
    _stub_issue_reference(monkeypatch, {72: (forge.ItemState.CLOSED, "Title", "")})
    monkeypatch.setattr(
        issue_claim,
        "_request",
        lambda _arguments, **_kwargs: request(issue=72, scope=("src/work.py",)),
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Codex Sol",
            "--scope",
            "src/work.py",
            "--json",
        ]
    )

    assert exit_code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "ok": False,
        "reason": "precondition_failed",
        "issue": 72,
        "checks": [
            {
                "level": "error",
                "check": "closed-issue",
                "text": "issue #72 is closed",
                "slice": None,
                "issue": 72,
            }
        ],
    }


def test_claim_does_not_corridor_on_a_slice_list(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    body = complete_contract("Ship it.", slice=slice_entries("First slice"))
    target = board_issue(72, "Epic", body)
    _configured_board_client(monkeypatch, tmp_path, open_issues=(target,))
    monkeypatch.setattr(
        issue_claim,
        "_request",
        lambda _arguments, **_kwargs: request(issue=72, scope=("src/work.py",)),
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Codex Sol",
            "--scope",
            "src/work.py",
            "--json",
        ]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["checks"] == []
    assert store.fetch_state(worktree=Path("."), remote="origin").claims


@pytest.mark.parametrize(
    ("parents", "expect_warning"),
    [
        pytest.param({}, True, id="without_sub_issue_relation"),
        pytest.param(
            {1017: board.ParentIssue(board.IssueReference(REPOSITORY, 79), "## Now\nCut.")},
            False,
            id="with_sub_issue_relation",
        ),
    ],
)
def test_claim_checks_a_slice_shaped_title_for_its_recorded_parent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    parents: dict[int, board.ParentIssue],
    expect_warning: bool,
) -> None:
    target = board_issue(
        1017, "Schema traegt den Titel (#79 Scheibe 21)", complete_contract("Claim #1017.")
    )
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(target,))
    client.parents.update(parents)
    monkeypatch.setattr(
        issue_claim,
        "_request",
        lambda _arguments, **_kwargs: request(issue=1017, scope=("src/work.py",)),
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "1017",
            "--agent",
            "Codex Sol",
            "--scope",
            "src/work.py",
        ]
    )

    assert exit_code == 0
    output = capsys.readouterr().out
    expected = (
        "WARNING: looks like slice 21 of #79 but is no sub-issue of #79; "
        "the parent inherits nothing"
    )
    assert (expected in output) is expect_warning
    assert store.fetch_state(worktree=Path("."), remote="origin").claims


def test_next_skips_a_frozen_item_and_names_it_as_such(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    frozen = board_issue(
        301, "Highest scored", complete_contract("Claim #301.", frozen_until=FROZEN_UNTIL)
    )
    lower = board_issue(10, "Lower work", complete_contract("Claim #10."))
    client = FakeForge()
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (frozen, lower))
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())

    assert issue_claim.main(["--repo", REPOSITORY, "next"]) == 0
    assert capsys.readouterr().out == (
        "#10 score -10: Lower work\n"
        "Next: Claim #10.\n"
        "Run: aco claim 10 --scope <paths>\n" + _UNKNOWN_SCOPE_NEXT_TAIL + "\n"
        "SKIPPED\n"
        f"#301: frozen: {FROZEN_TRIGGER}\n"
    )

    assert issue_claim.main(["--repo", REPOSITORY, "next", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["number"] == 10
    assert payload["skipped"] == [{"number": 301, "reason": f"frozen: {FROZEN_TRIGGER}"}]


def test_claim_does_not_warn_about_a_frozen_higher_scored_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = FakeForge()
    frozen = board_issue(
        301, "Highest scored", complete_contract("Claim #301.", frozen_until=FROZEN_UNTIL)
    )
    lower = board_issue(10, "Lower work", complete_contract("Claim #10."))
    claimed_request = request(issue=10, scope=("src/lower.py",))
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (frozen, lower))
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())
    monkeypatch.setattr(issue_claim, "_request", lambda _arguments, **_kwargs: claimed_request)

    assert (
        issue_claim.main(
            [
                "--repo",
                REPOSITORY,
                "claim",
                "10",
                "--agent",
                "Codex Sol",
                "--scope",
                "src/lower.py",
            ]
        )
        == 0
    )
    assert "WARNING" not in capsys.readouterr().out


def test_board_reads_priority_configuration_from_the_checkout_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    toplevel = tmp_path / "checkout"
    configuration_directory = toplevel / ".agent-claim"
    configuration_directory.mkdir(parents=True)
    (configuration_directory / "board.toml").write_text('priority_labels = ["ux", "security"]\n')
    nested_directory = toplevel / "src" / "agent_coordination"
    nested_directory.mkdir(parents=True)
    monkeypatch.chdir(nested_directory)
    observed: list[list[str]] = []

    def git_output(arguments: list[str], **_kwargs: object) -> str:
        observed.append(arguments)
        return str(toplevel)

    client = _MinimalForgeReader(
        open_issues=(
            board.Issue(
                20,
                "Security issue",
                ("security",),
                "",
                "2026-08-20T00:00:00Z",
                "2026-08-20T00:00:00Z",
            ),
            board.Issue(
                21, "UX issue", ("ux",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z"
            ),
        )
    )
    monkeypatch.setattr(checkout, "_git_output", git_output)
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())

    projected = issue_claim._board(client, ())

    assert [item.number for item in projected.items] == [21, 20]
    assert observed == [["rev-parse", "--show-toplevel"]]


def test_next_names_a_cuttable_container_slice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Exact `cut_slice #N: …` text, per #112's own body example."""
    container = board.Issue(
        180,
        "Epic",
        (),
        complete_contract(
            "Scheibe B — Kartenraster", slice=slice_entries("Scheibe B — Kartenraster")
        ),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=body.ItemKind.CONTAINER,
        children_closed=1,
        children_total=1,
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    exit_code = issue_claim.main(["--repo", REPOSITORY, "next"])

    assert exit_code == 0
    assert capsys.readouterr().out == (
        "cut_slice #180: Scheibe B — Kartenraster\n"
        'Next: aco cut 180 --title "Scheibe B — Kartenraster"\n' + _PARALLEL_UNKNOWN_TAIL
    )


# A `Next` line whose own prose never matches any slice-table row title used
# below -- the shape #177 fixes: a container whose Next line and first uncut
# row disagree.
_DIFFERING_NEXT_LINE = "Weitere Aufgabe."


@dataclass(frozen=True)
class _CutRoundTripCase:
    """One #151 round-trip scenario for a container whose block still
    carries an undispatched `[[slice]]` row: the `cut` command `next` prints
    for `container_number` must be one `cut` itself accepts. Since issue
    #208, that row is the only thing that makes `next` print a `cut`
    command at all -- a container with no uncut row is never one of these
    cases, whatever its `Next` line says (see
    `test_next_names_a_container_with_no_slice_row_by_its_own_next_line` for
    that combination instead). `expected_item_bodies`/`expected_output` take
    the freshly created child's number, since only `cut` fixes that."""

    case_id: str
    container_number: int
    body: str
    expected_created_title: str
    expected_item_bodies: Callable[[int], dict[int, str]]
    expected_output: Callable[[int], str]


def _uncut_row_case(
    case_id: str, container_number: int, next_line: str, row_title: str
) -> _CutRoundTripCase:
    """A container whose block still carries one undispatched `[[slice]]` --
    `cut` always links it and titles the created child with that entry's own
    title, regardless of what the container's `Next` line itself says."""
    return _CutRoundTripCase(
        case_id,
        container_number,
        complete_contract(next_line, slice=slice_entries(row_title)),
        row_title,
        lambda _child: {container_number: complete_contract(next_line, slice=[])},
        lambda child: f"CUT #{container_number} row 1 -> #{child}\n",
    )


_CUT_ROUND_TRIP_CASES = (
    # next=no, uncut=yes -- an uncut row on its own already qualifies (#151).
    _uncut_row_case("uncut_row_only", 184, "", "Scheibe E"),
    # next=yes, uncut=yes, and they disagree -- #177 itself: seven live
    # atelier-2 containers where `next` printed the `Next` line's prose and
    # `cut` refused it, because the row it actually links carries a
    # different title.
    _uncut_row_case("next_and_differing_uncut_row", 186, _DIFFERING_NEXT_LINE, "Scheibe F"),
    # The remaining combinations -- no uncut row, whether or not the `Next`
    # line still names work -- close the container instead of cutting a
    # slice (issue #208: an empty slice table, with or without a `slice` key
    # at all, is the typed statement that there is nothing here to cut, and
    # #122 is what happened when a fallback ignored it), so they have no
    # `cut` command to round-trip; `test_next_names_a_closeable_container`
    # and `test_next_names_a_container_with_no_slice_row_by_its_own_next_line`
    # prove those instead.
)


@pytest.mark.parametrize("case", _CUT_ROUND_TRIP_CASES, ids=lambda case: case.case_id)
def test_next_prints_a_cut_command_that_cut_accepts(
    case: _CutRoundTripCase,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """#151's own invariant, completed by #177: whatever `cut` command
    `next` prints for a childless container is one `cut` itself accepts."""
    container = board.Issue(
        case.container_number,
        "Epic",
        (),
        case.body,
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=body.ItemKind.CONTAINER,
        children_closed=1,
        children_total=1,
    )
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    next_exit_code = issue_claim.main(["--repo", REPOSITORY, "next"])
    assert next_exit_code == 0
    command_line = capsys.readouterr().out.splitlines()[1]
    cut_arguments = shlex.split(command_line.removeprefix("Next: aco "))

    cut_exit_code = issue_claim.main(["--repo", REPOSITORY, *cut_arguments])

    assert cut_exit_code == 0
    child = client.next_created_child_number - 1
    assert client.created_children == [
        (
            case.container_number,
            case.expected_created_title,
            issue_claim._cut_child_body(case.container_number),
            body.ItemKind.TASK,
        )
    ]
    assert client.item_bodies == case.expected_item_bodies(child)
    assert capsys.readouterr().out == case.expected_output(child)


def test_next_prints_a_cut_command_that_cut_accepts_for_every_qualifying_container(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """#177 (independent counter-check, 06.09.2026): `next` only ever proves
    the round-trip invariant for the single top-ranked container a board
    carries -- exactly how this repository's own #122 sat with a disagreeing
    `Next` line and first uncut row unnoticed, since only one container's
    printed command is ever checked per poll. This walks every container a
    real, multi-container board carries, deriving each one's own printed
    `cut` command through the public `next_action` path -- each container
    alone on its own board, so it is necessarily the one `next_action`
    names -- and proves every one of them is a command `cut` itself accepts
    on a fresh fake, not only the board's own top-ranked pick."""
    top_ranked = board.Issue(
        130,
        "Epic ranked first",
        (),
        complete_contract(_DIFFERING_NEXT_LINE, slice=slice_entries("Scheibe I-top")),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=body.ItemKind.CONTAINER,
        children_closed=1,
        children_total=1,
    )
    lower_ranked = board.Issue(
        145,
        "Epic ranked second",
        (),
        complete_contract(_DIFFERING_NEXT_LINE, slice=slice_entries("Scheibe I")),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=body.ItemKind.CONTAINER,
        children_closed=1,
        children_total=1,
    )
    containers = (top_ranked, lower_ranked)
    containers_by_number = {issue.number: issue for issue in containers}

    projected = projected_board(
        containers, (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )
    top_action = board.next_action(projected)
    assert isinstance(top_action, board.CutSliceAction)
    assert top_action.container.number == 130

    for item in projected.items:
        if item.kind is not body.ItemKind.CONTAINER:
            continue
        isolated = projected_board(
            (containers_by_number[item.number],),
            (),
            (),
            (),
            board.BoardConfig(),
            now=datetime(2026, 8, 21, tzinfo=UTC),
        )
        action = board.next_action(isolated)
        assert isinstance(action, board.CutSliceAction)

        command_line = issue_claim._next_action_lines(action, body.Storage.GITHUB)[1]
        cut_arguments = shlex.split(command_line.removeprefix("Next: aco "))
        client = _configured_board_client(
            monkeypatch, tmp_path, open_issues=(containers_by_number[item.number],)
        )

        cut_exit_code = issue_claim.main(["--repo", REPOSITORY, *cut_arguments])

        assert cut_exit_code == 0
        assert client.created_children[0][0] == item.number


def test_next_json_names_a_cuttable_container_slice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    container = board.Issue(
        181,
        "Epic",
        (),
        complete_contract("Scheibe C", slice=slice_entries("Scheibe C")),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=body.ItemKind.CONTAINER,
        children_closed=2,
        children_total=2,
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    exit_code = issue_claim.main(["--repo", REPOSITORY, "next", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "cut_slice"
    assert payload["number"] == 181
    assert payload["title"] == "Epic"
    assert payload["slice"] == "Scheibe C"
    assert payload["cut_title"] == "Scheibe C"
    assert payload["command"] == 'aco cut 181 --title "Scheibe C"'


def test_next_names_a_closeable_container(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    container = board.Issue(
        182,
        "Epic",
        (),
        complete_contract("keiner"),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=body.ItemKind.CONTAINER,
        children_closed=3,
        children_total=3,
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    exit_code = issue_claim.main(["--repo", REPOSITORY, "next"])

    assert exit_code == 0
    assert capsys.readouterr().out == (
        "close_container #182: 3/3 children closed, no Next work\n"
        "parallel: none\nscope unknown: none\nclose: #182\n"
    )


def test_next_json_names_a_closeable_container(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    container = board.Issue(
        183,
        "Epic",
        (),
        complete_contract("keiner"),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=body.ItemKind.CONTAINER,
        children_closed=4,
        children_total=4,
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    exit_code = issue_claim.main(["--repo", REPOSITORY, "next", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "close_container"
    assert payload["number"] == 183
    assert payload["closed"] == 4
    assert payload["total"] == 4
    assert payload["next_step"] is None
    assert "command" not in payload


def test_next_names_a_container_with_no_slice_row_by_its_own_next_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #208, reproduced live at #122: an empty slice table is the
    typed statement that there is nothing here to cut, even though the
    container's own `Next` line still names real work. `next` must not
    fabricate `cut --title "<the whole Next paragraph>"` from that prose --
    it names the container and its own sentence, the same way
    `close_container` already declines a command when there is none."""
    container = board.Issue(
        187,
        "Epic",
        (),
        complete_contract("Schließen, sobald die letzte Bedingung erfüllt ist."),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=body.ItemKind.CONTAINER,
        children_closed=2,
        children_total=2,
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    exit_code = issue_claim.main(["--repo", REPOSITORY, "next"])

    assert exit_code == 0
    assert capsys.readouterr().out == (
        "close_container #187: Schließen, sobald die letzte Bedingung erfüllt ist.\n"
        "parallel: none\nscope unknown: none\nclose: #187\n"
    )


def test_next_json_names_a_container_with_no_slice_row_by_its_own_next_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The JSON form of the same #208 case: `reason` stays `close_container`
    (there is still nothing to cut) but `next_step` carries the container's
    own sentence instead of `null`, and no `command` or `cut_title` is
    invented from it -- text and JSON agree on there being no command to
    run."""
    container = board.Issue(
        188,
        "Epic",
        (),
        complete_contract("Schließen, sobald die letzte Bedingung erfüllt ist."),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=body.ItemKind.CONTAINER,
        children_closed=2,
        children_total=2,
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    exit_code = issue_claim.main(["--repo", REPOSITORY, "next", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["reason"] == "close_container"
    assert payload["number"] == 188
    assert payload["closed"] == 2
    assert payload["total"] == 2
    assert payload["next_step"] == "Schließen, sobald die letzte Bedingung erfüllt ist."
    assert "command" not in payload
    assert "cut_title" not in payload


def test_next_caps_the_parallel_text_list_at_three_and_counts_the_rest(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """issue #348, Beweis 1 (text cap): five mutually disjoint candidates --
    the text form names only the first three, in board order, plus a
    trailing count; `--json` still carries every one with its full scope."""
    alpha = board_issue(60, "Alpha", complete_contract("Ship Alpha.", scope=["alpha/a.py"]))
    candidates = tuple(
        board_issue(60 + offset, name, complete_contract(f"Ship {name}.", scope=[f"{name}/x.py"]))
        for offset, name in enumerate(("Bravo", "Charlie", "Delta", "Echo", "Foxtrot"), start=1)
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(alpha, *candidates))

    exit_code = issue_claim.main(["--repo", REPOSITORY, "next"])

    assert exit_code == 0
    assert capsys.readouterr().out == (
        "#60 score -10: Alpha\nNext: Ship Alpha.\nRun: aco claim 60\n"
        "parallel: #61 (1 path), #62 (1 path), #63 (1 path), and 2 more\n"
        "scope unknown: none\nclose: none\n"
    )

    json_exit_code = issue_claim.main(["--repo", REPOSITORY, "next", "--json"])
    assert json_exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["parallel"]["candidates"] == [
        {"number": 61, "scope": ["Bravo/x.py"]},
        {"number": 62, "scope": ["Charlie/x.py"]},
        {"number": 63, "scope": ["Delta/x.py"]},
        {"number": 64, "scope": ["Echo/x.py"]},
        {"number": 65, "scope": ["Foxtrot/x.py"]},
    ]


def test_next_parallel_set_uses_a_cut_proposals_own_row_scope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """issue #348, Beweis 2: a cut proposal's own uncut-row scope is what
    `parallel_set` occupies and checks disjointness against -- exactly like
    a work item's top-level scope, never a second rule. `#82`'s row repeats
    `#80`'s own path and is dropped silently, the same way an overlapping
    work item would be; the scopeless-row collapse into `parallel: unknown`
    is `test_next_names_a_cuttable_container_slice`'s own proof."""
    epic1 = board.Issue(
        80,
        "Epic1",
        (),
        complete_contract(
            "Cut it.", slice=[{"index": 1, "title": "Slice A", "scope": ["epic/a.py"]}]
        ),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=body.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    epic2 = board.Issue(
        81,
        "Epic2",
        (),
        complete_contract(
            "Cut it.", slice=[{"index": 1, "title": "Slice B", "scope": ["epic/b.py"]}]
        ),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=body.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    epic3 = board.Issue(
        82,
        "Epic3",
        (),
        complete_contract(
            "Cut it.", slice=[{"index": 1, "title": "Slice C", "scope": ["epic/a.py"]}]
        ),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=body.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(epic1, epic2, epic3))

    exit_code = issue_claim.main(["--repo", REPOSITORY, "next"])

    assert exit_code == 0
    assert capsys.readouterr().out == (
        'cut_slice #80: Cut it.\nNext: aco cut 80 --title "Slice A"\n'
        "parallel: #81 (1 path)\nscope unknown: none\nclose: none\n"
        "\nSKIPPED\n#81: container; claim a child\n#82: container; claim a child\n"
    )

    json_exit_code = issue_claim.main(["--repo", REPOSITORY, "next", "--json"])
    assert json_exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["parallel"] == {
        "first_scope_unknown": False,
        "candidates": [{"number": 81, "scope": ["epic/b.py"]}],
        "scope_unknown": [],
    }


def test_next_close_names_every_zero_cost_action_regardless_of_rank(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """issue #348, Beweis 3 (#310 finding 29): a closable container ranked
    well below the board's top row, and a landed-but-open recovery item,
    both still appear under `close:` -- unconditionally, never gated by
    which row `next` happens to recommend."""
    top_ranked = board_issue(
        70,
        "Top ranked work",
        complete_contract("Ship it.", scope=["a"]),
        labels=("security",),
    )
    closable_container = board.Issue(
        71,
        "Closable epic",
        (),
        complete_contract("keiner"),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=body.ItemKind.CONTAINER,
        children_closed=2,
        children_total=2,
    )
    landed_but_open = board_issue(72, "Landed but open", complete_contract("Close it."))
    client = _configured_board_client(
        monkeypatch, tmp_path, open_issues=(top_ranked, closable_container, landed_but_open)
    )
    monkeypatch.setattr(
        client,
        "list_recent_merged_board_pull_requests",
        lambda _since: (
            board.PullRequest(
                150, "Lands it", "Work-Item: #72\n\nCloses #72", "branch", "2026-08-20T00:00:00Z"
            ),
        ),
    )

    exit_code = issue_claim.main(["--repo", REPOSITORY, "next"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.startswith(f"RECOVERY\n#72: {board.RECOVERY_STEP}\n\n")
    assert out.splitlines()[0:2] == ["RECOVERY", f"#72: {board.RECOVERY_STEP}"]
    assert "#70 score" in out
    assert "close: #71, #72" in out

    json_exit_code = issue_claim.main(["--repo", REPOSITORY, "next", "--json"])
    assert json_exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["close"] == [71, 72]


_RECOVERY_SHARED_SCOPE = "b"


def test_next_parallel_set_never_lets_a_recovery_item_occupy_or_candidate(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """issue #348 review (G1): a landed-but-open item is `zero_cost_closes`'
    own domain, never `parallel_set`'s. `landed_but_open` (#71) and
    `free_item` (#72) name the same scope, and board order visits #71
    first -- pre-fix, the walk occupied that scope for #71 and silently
    dropped #72 as "overlapping", even though #71 was never real work in
    flight. #72 must still surface in `parallel:`, and #71 only under
    `close:`, never as a candidate of its own."""
    top_ranked = board_issue(
        70, "Top ranked work", complete_contract("Ship it.", scope=["a"]), labels=("security",)
    )
    landed_but_open = board_issue(
        71, "Landed but open", complete_contract("Close it.", scope=[_RECOVERY_SHARED_SCOPE])
    )
    free_item = board_issue(
        72, "Free item", complete_contract("Ship it too.", scope=[_RECOVERY_SHARED_SCOPE])
    )
    client = _configured_board_client(
        monkeypatch, tmp_path, open_issues=(top_ranked, landed_but_open, free_item)
    )
    monkeypatch.setattr(
        client,
        "list_recent_merged_board_pull_requests",
        lambda _since: (
            board.PullRequest(
                150, "Lands it", "Work-Item: #71\n\nCloses #71", "branch", "2026-08-20T00:00:00Z"
            ),
        ),
    )

    exit_code = issue_claim.main(["--repo", REPOSITORY, "next"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert "#70 score" in out
    assert "parallel: #72 (1 path)\n" in out
    assert "close: #71\n" in out
    assert "#71 (1 path)" not in out

    json_exit_code = issue_claim.main(["--repo", REPOSITORY, "next", "--json"])
    assert json_exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["parallel"] == {
        "first_scope_unknown": False,
        "candidates": [{"number": 72, "scope": [_RECOVERY_SHARED_SCOPE]}],
        "scope_unknown": [],
    }
    assert payload["close"] == [71]


def test_board_queries_merged_pull_requests_back_to_the_oldest_open_issue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    old_epic = replace(
        board_issue(70, "Epic open for months", complete_contract("Cut the next slice.")),
        created_at="2026-06-01T00:00:00Z",
    )
    recent_issue = board_issue(71, "Recently filed work", complete_contract("Ship it."))
    client = _MinimalForgeReader(open_issues=(old_epic, recent_issue))
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())

    issue_claim._board(client, ())

    # A fixed 14-day window (now - 14 days = 2026-08-07) would have missed
    # anything the six-month-old epic's own slices landed months ago.
    assert client.observed_merged_pull_request_floors == [datetime(2026, 6, 1, tzinfo=UTC)]


def test_board_fetches_children_only_for_container_kinded_issues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    container = board.Issue(
        90,
        "Container",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=body.ItemKind.CONTAINER,
        children_closed=0,
        children_total=1,
    )
    plain = board_issue(91, "Plain", complete_contract("Ship it."))
    client = _MinimalForgeReader(
        open_issues=(container, plain),
        children_by_number={90: (board.ChildItem(92, board.ChildState.OPEN),)},
    )
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())

    projected = issue_claim._board(client, ())

    assert client.observed_children_lookups == [90]
    container_item = next(item for item in projected.items if item.number == 90)
    assert container_item.container is not None
    assert container_item.container.open_children == (board.ChildItem(92, board.ChildState.OPEN),)


def test_the_body_fence_and_config_path_keep_their_agent_claim_names() -> None:
    """The package renamed to `agent-coordination` and the command to `aco`
    (issue #191); these two strings deliberately did not follow.

    The fence info string is spelled inside the issue bodies of every migrated
    repository and the configuration file already sits at this path in each
    checkout. Renaming either would make this release silently stop reading
    state that is already written -- so they are protocol, not product name,
    and this test is what says so out loud.
    """
    assert body.AGENT_CLAIM_FENCE_INFO == "agent-claim"
    assert body.BLOCK_CHILD_SKELETON.startswith(f"```{body.AGENT_CLAIM_FENCE_INFO}\n")
    assert board.CONFIG_PATH.as_posix() == ".agent-claim/board.toml"


def test_body_contract_checks_names_a_blockless_container_by_its_no_block_defect() -> None:
    raw_body = "## Now\nOld prose.\n\n## Next\nDo the thing.\n"
    blockless = replace(
        board_issue(201, "Blockless container", raw_body),
        kind=body.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    projected = projected_board(
        (blockless,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )
    item = next(item for item in projected.items if item.number == 201)

    checks = issue_claim._body_contract_checks(item)

    assert checks == (
        issue_claim.SliceCheck(
            "error", "body-contract", "body malformed: agent-claim: no agent-claim block"
        ),
    )


def test_body_contract_checks_names_a_malformed_body_by_its_first_defect() -> None:
    malformed_body = agent_claim_body('version = 2\nnow = "N"\nnext = "X"\ndone_when = "D"\n')
    malformed = board_issue(202, "Malformed", malformed_body)
    projected = projected_board(
        (malformed,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )
    item = next(item for item in projected.items if item.number == 202)

    checks = issue_claim._body_contract_checks(item)

    assert checks == (
        issue_claim.SliceCheck(
            "error", "body-contract", "body malformed: version: version must be exactly 1"
        ),
    )


def test_board_shows_freed_from_a_sole_closed_local_dependency_and_claim_reaches_mutation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A complete block item whose sole dependency is closed passes body
    checks and reaches claim mutation (#150 §6/§10): FREED on the projection,
    and `claim` (without `--out-of-order`) actually posts a claim comment
    through the fake, not just a non-blocked projection."""
    closed_dependency = (
        block_dependency(
            151, state=board.BlockerState.CLOSED, closed_at=datetime(2026, 8, 20, tzinfo=UTC)
        ),
    )
    projected = projected_board(
        (board_issue(301, "Freed", agent_claim_body(MINIMAL_BLOCK_TOML)),),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        dependencies={301: closed_dependency},
    )
    item = next(item for item in projected.items if item.number == 301)
    assert item.open_blockers == ()
    assert item.freed_on == datetime(2026, 8, 20, tzinfo=UTC)
    assert item.actionable is True

    live_issue = replace(
        board_issue(301, "Freed by a closed dependency", agent_claim_body(MINIMAL_BLOCK_TOML)),
        blocked_by_count=1,
    )
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(live_issue,))
    _write_block_pin(tmp_path)
    client.board_dependencies = {301: closed_dependency}
    monkeypatch.setattr(
        issue_claim,
        "_request",
        lambda _arguments, **_kwargs: request(issue=301, scope=("src/work.py",)),
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "301",
            "--agent",
            "Ada",
            "--scope",
            "src/work.py",
        ]
    )

    assert exit_code == 0
    assert store.fetch_state(worktree=Path("."), remote="origin").claims
    assert "ERROR:" not in capsys.readouterr().err


def test_blocked_check_reports_a_foreign_dependency_and_the_out_of_order_warning() -> None:
    issue = board_issue(304, "Foreign blocked", agent_claim_body(MINIMAL_BLOCK_TOML))
    dependencies = {304: (block_dependency(9, repository="overnightworks/other-repo"),)}
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        dependencies=dependencies,
    )
    item = next(item for item in projected.items if item.number == 304)

    error_check = issue_claim._blocked_check(item, None, REPOSITORY, body.Storage.GITHUB)
    assert error_check == issue_claim.SliceCheck(
        "error",
        "blocked",
        "#304 is blocked by overnightworks/other-repo#9 (open); "
        "pass --out-of-order REASON to claim it anyway",
        issue=304,
    )
    warning_check = issue_claim._blocked_check(item, "reason", REPOSITORY, body.Storage.GITHUB)
    assert warning_check is not None
    assert warning_check.level == "warning"


def test_blocked_check_labels_a_local_dependency_under_the_state_ref_pin() -> None:
    """Issue #300 (Codex Terra review): a same-repository blocker prints
    `board.item_label`'s own `aco-...` id under `storage = STATE_REF`, the
    same as everywhere else that pin already changes narrative output --
    never the bare `#n` GitHub uses."""
    issue = board_issue(304, "Local blocked", agent_claim_body(MINIMAL_BLOCK_TOML))
    dependencies = {304: (block_dependency(9, repository=REPOSITORY),)}
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        dependencies=dependencies,
    )
    item = next(item for item in projected.items if item.number == 304)

    check = issue_claim._blocked_check(item, None, REPOSITORY, body.Storage.STATE_REF)

    assert check is not None
    assert board.item_label(9, body.Storage.STATE_REF) in check.text
    assert "#9" not in check.text


@dataclass
class _MinimalBoardSource:
    """The smallest real `forge.BoardSource` -- every read empty, `capability`
    and the dependency fetch injectable -- for tests that exercise exactly
    one of `_load_board_config`/`_fetch_dependencies` in isolation."""

    repository: forge.RepositoryId = field(
        default_factory=lambda: github._repository_id(REPOSITORY)
    )
    requests: int = 0
    capability_result: forge.Capability = forge.Capability.READ_ONLY
    dependencies_fetcher: Callable[[int], tuple[board.IssueDependency, ...]] = lambda _number: ()

    def capability(self, operation: forge.ForgeOperation) -> forge.Capability:
        del operation
        return self.capability_result

    def list_open_board_issues(self) -> tuple[board.Issue, ...]:
        return ()

    def list_board_dependencies(self, number: int) -> tuple[board.IssueDependency, ...]:
        return self.dependencies_fetcher(number)

    def list_open_board_pull_requests(self) -> tuple[board.PullRequest, ...]:
        return ()

    def list_recent_merged_board_pull_requests(
        self, since: datetime
    ) -> tuple[board.PullRequest, ...]:
        del since
        return ()

    def list_children(self, number: int) -> tuple[board.ChildItem, ...]:
        del number
        return ()


def test_load_board_config_refuses_a_block_pin_the_forge_cannot_support(tmp_path: Path) -> None:
    (tmp_path / ".agent-claim").mkdir()
    (tmp_path / ".agent-claim" / "board.toml").write_text('body_contract = "block"\n')

    client = _MinimalBoardSource(capability_result=forge.Capability.UNSUPPORTED)

    with pytest.raises(ClaimError, match="list_board_dependencies"):
        issue_claim._load_board_config(client, tmp_path)


def test_fetch_dependencies_bounds_concurrency_at_the_shared_constant() -> None:
    concurrency = issue_claim.BOARD_CHILD_FETCH_CONCURRENCY
    release = threading.Barrier(concurrency)
    active = 0
    peak = 0
    lock = threading.Lock()
    exceeded = threading.Event()

    def fetch(number: int) -> tuple[board.IssueDependency, ...]:
        nonlocal active, peak
        del number
        with lock:
            active += 1
            peak = max(peak, active)
            if active > concurrency:
                exceeded.set()
        release.wait(timeout=5)
        with lock:
            active -= 1
        return ()

    client = _MinimalBoardSource(dependencies_fetcher=fetch)

    issue_claim._fetch_dependencies(client, tuple(range(concurrency * 2)))

    assert not exceeded.is_set()
    assert peak == concurrency


def test_validated_dependencies_refuses_a_length_mismatch() -> None:
    issue = board_issue(305, "Length mismatch", agent_claim_body(MINIMAL_BLOCK_TOML))
    issue = replace(issue, blocked_by_count=2)
    fetched = {305: (block_dependency(1),)}

    with pytest.raises(
        forge.ForgeMalformedResponseError,
        match=r"malformed board blocked-by list for #305: listing total_blocked_by=2, "
        r"detail length=1",
    ):
        issue_claim._validated_dependencies((issue,), fetched)


def test_validated_dependencies_refuses_a_duplicate_dependency() -> None:
    issue = board_issue(306, "Duplicate", agent_claim_body(MINIMAL_BLOCK_TOML))
    issue = replace(issue, blocked_by_count=2)
    fetched = {306: (block_dependency(1), block_dependency(1))}

    with pytest.raises(forge.ForgeMalformedResponseError, match="malformed board blocked-by list"):
        issue_claim._validated_dependencies((issue,), fetched)


def test_next_pulls_a_configured_projectionless_idea_with_refinement_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (tmp_path / ".agent-claim").mkdir()
    (tmp_path / ".agent-claim" / "board.toml").write_text('idea_label = "idea"\n')
    idea = board_issue(10, "Operator idea", idea_body("Make the board clearer."), labels=("idea",))
    _configured_board_client(monkeypatch, tmp_path, open_issues=(idea,))

    assert issue_claim.main(["--repo", REPOSITORY, "next"]) == 0
    assert capsys.readouterr().out == (
        "#10 score -20: Operator idea\nNext: Problem neu prüfen und Item verfeinern\n"
        "Run: aco claim 10 --scope <paths>\n" + _UNKNOWN_SCOPE_NEXT_TAIL
    )

    assert issue_claim.main(["--repo", REPOSITORY, "next", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "reason": "work_item",
        "number": 10,
        "score": -20,
        "title": "Operator idea",
        "next": "Problem neu prüfen und Item verfeinern",
        "command": "aco claim 10 --scope <paths>",
        "ruling_landings": None,
        "ruling_old": None,
        "recovery": [],
        "skipped": [],
        "parallel": _UNKNOWN_SCOPE_PARALLEL_JSON,
        "close": [],
    }


def test_next_keeps_an_unlabelled_projectionless_item_skipped_with_an_active_idea_label(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (tmp_path / ".agent-claim").mkdir()
    (tmp_path / ".agent-claim" / "board.toml").write_text('idea_label = "idea"\n')
    incomplete = board_issue(10, "Incomplete work", idea_body("Investigate."))
    _configured_board_client(monkeypatch, tmp_path, open_issues=(incomplete,))

    assert issue_claim.main(["--repo", REPOSITORY, "next"]) == 3
    assert capsys.readouterr().out == (
        "No actionable item.\n"
        + _NO_ACTION_NEXT_TAIL
        + "\nSKIPPED\n#10: body incomplete: Now, Next, Done when\n"
    )

    assert issue_claim.main(["--repo", REPOSITORY, "next", "--json"]) == 3
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "reason": "nothing_actionable",
        "recovery": [],
        "skipped": [{"number": 10, "reason": "body incomplete: Now, Next, Done when"}],
        "parallel": _EMPTY_PARALLEL_JSON,
        "close": [],
    }


def test_next_keeps_a_vision_labelled_projectionless_item_incomplete_without_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    idea = board_issue(10, "Operator vision", idea_body("Investigate."), labels=("vision",))
    _configured_board_client(monkeypatch, tmp_path, open_issues=(idea,))

    assert issue_claim.main(["--repo", REPOSITORY, "next"]) == 3
    assert capsys.readouterr().out == (
        "No actionable item.\n"
        + _NO_ACTION_NEXT_TAIL
        + "\nSKIPPED\n#10: body incomplete: Now, Next, Done when\n"
    )


def test_next_keeps_a_configured_idea_with_a_complete_projection_own_next(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (tmp_path / ".agent-claim").mkdir()
    (tmp_path / ".agent-claim" / "board.toml").write_text('idea_label = "idea"\n')
    idea = board_issue(
        10,
        "Refined idea",
        complete_contract("Build the chosen direction."),
        labels=("idea",),
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(idea,))

    assert issue_claim.main(["--repo", REPOSITORY, "next"]) == 0
    assert capsys.readouterr().out == (
        "#10 score -10: Refined idea\nNext: Build the chosen direction.\n"
        "Run: aco claim 10 --scope <paths>\n" + _UNKNOWN_SCOPE_NEXT_TAIL
    )


def test_claim_treats_a_higher_ranked_configured_idea_as_out_of_order(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (tmp_path / ".agent-claim").mkdir()
    (tmp_path / ".agent-claim" / "board.toml").write_text('idea_label = "vision"\n')
    lower = board_issue(10, "Lower work", complete_contract("Claim #10."))
    idea = board_issue(
        11,
        "Higher-ranked vision",
        "## Wunsch\nImprove claims.\n\n"
        + complete_contract("", now="", done_when="", scope=["src/idea.py"]),
        labels=("vision", "security"),
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(lower, idea))
    monkeypatch.setattr(
        issue_claim,
        "_request",
        lambda _arguments, **_kwargs: request(issue=10, scope=("src/lower.py",)),
    )

    assert (
        issue_claim.main(
            [
                "--repo",
                REPOSITORY,
                "claim",
                "10",
                "--agent",
                "Codex Sol",
                "--scope",
                "src/lower.py",
            ]
        )
        == 2
    )
    assert "ERROR: higher-priority actionable item #11" in capsys.readouterr().err


def test_status_scope_index_never_rescans_scope_pairs(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    claims = tuple(
        _active_claim(
            claim_id=f"claim-{claim_index}",
            issue=claim_index + 100,
            scope=tuple(f"area-{claim_index}/path-{scope_index}" for scope_index in range(32)),
        )
        for claim_index in range(50)
    )
    opened_at = datetime(2026, 8, 21, tzinfo=UTC)
    ages: dict[str, datetime] = {claim.claim_id: opened_at for claim in claims}

    def scope_pair_scan(*args, **kwargs):
        pytest.fail("status must use its single scope index")

    monkeypatch.setattr(protocol, "claims_conflict", scope_pair_scan)

    assert _status(claims, None, ages, body.Storage.GITHUB) == 0
    assert capsys.readouterr().out.count("CLAIMED") == 50
    assert _status(claims, 100, ages, body.Storage.GITHUB) == 0
    assert capsys.readouterr().out.count("CLAIMED") == 1


def test_status_reports_repository_scope_overlaps_as_notes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = _active_claim(issue=72, scope=("shared",))
    second = _active_claim(claim_id="claim-b", issue=73, scope=("shared/file.py",))
    opened_at = datetime(2026, 8, 21, tzinfo=UTC)
    ages: dict[str, datetime] = {first.claim_id: opened_at, second.claim_id: opened_at}

    exit_code = _status((first, second), None, ages, body.Storage.GITHUB)

    assert exit_code == 0
    rendered = capsys.readouterr().out
    assert rendered.count("CLAIMED") == 2
    assert "CONFLICT" not in rendered
    assert "overlaps issue #73 (claim-b)" in rendered
    assert "overlaps issue #72 (cli-claim)" in rendered
    assert _status((first, second), 72, ages, body.Storage.GITHUB) == 0
    issue_rendered = capsys.readouterr().out
    assert issue_rendered.count("CLAIMED") == 2
    assert "overlaps issue #73 (claim-b)" in issue_rendered


def test_status_notes_a_scope_that_is_claimed_after_its_descendant(
    capsys: pytest.CaptureFixture[str],
) -> None:
    descendant = _active_claim(issue=72, scope=("shared/file.py",))
    parent = _active_claim(claim_id="claim-b", issue=73, scope=("shared",))
    opened_at = datetime(2026, 8, 21, tzinfo=UTC)
    ages: dict[str, datetime] = {descendant.claim_id: opened_at, parent.claim_id: opened_at}

    assert _status((descendant, parent), None, ages, body.Storage.GITHUB) == 0
    rendered = capsys.readouterr().out
    assert rendered.count("CLAIMED") == 2
    assert "CONFLICT" not in rendered


def test_canonical_remote_location_parses_the_configured_remote_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkout, "remote_url", lambda remote: "git@github.com:owner/repo.git")

    location = issue_claim._canonical_remote_location("origin")

    assert location == checkout.RemoteLocation(github.GITHUB_HOST, "owner/repo")


def test_refuse_unsupported_forge_host_allows_github() -> None:
    issue_claim._refuse_unsupported_forge_host(
        checkout.RemoteLocation(github.GITHUB_HOST, "owner/repo")
    )


def test_refuse_unsupported_forge_host_refuses_another_host() -> None:
    """No forge adapter but GitHub's exists yet (#230 slice 2) -- a forge
    command against any other host refuses by its own name (issue #245),
    never with GitHub's "does not name a GitHub repository" text."""
    location = checkout.RemoteLocation("gitlab.com", "o/r")

    with pytest.raises(ClaimUnavailableError, match=r"no forge adapter for host gitlab\.com"):
        issue_claim._refuse_unsupported_forge_host(location)


def test_refuse_canonical_remote_mismatch_allows_a_matching_target() -> None:
    issue_claim._refuse_canonical_remote_mismatch(
        forge.RepositoryId(github.GITHUB_HOST, ("owner",), "repo"),
        checkout.RemoteLocation(github.GITHUB_HOST, "owner/repo"),
    )


def test_refuse_canonical_remote_mismatch_names_both_repositories() -> None:
    mismatched = forge.RepositoryId(github.GITHUB_HOST, ("other",), "repo")
    canonical_remote = checkout.RemoteLocation(github.GITHUB_HOST, "owner/repo")

    with pytest.raises(
        ClaimUnavailableError,
        match="forge target other/repo does not match canonical remote owner/repo",
    ):
        issue_claim._refuse_canonical_remote_mismatch(mismatched, canonical_remote)


def test_resolved_forge_target_refuses_before_asking_gh_on_a_non_github_host(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_resolved_forge_target` gates on the canonical remote's own host
    before it ever calls `discover_repository` (issue #245): a `gh` call
    here would fail the test outright."""
    monkeypatch.setattr(checkout, "remote_url", lambda remote: "file:///srv/git/repo.git")

    def unused(*_args: object, **_kwargs: object) -> forge.RepositoryId:
        pytest.fail("a non-GitHub canonical remote must refuse before discover_repository runs")

    monkeypatch.setattr(github, "discover_repository", unused)

    with pytest.raises(ClaimUnavailableError, match="no forge adapter for host file"):
        issue_claim._resolved_forge_target(None, "origin")


def test_resolved_forge_target_checks_erwartung_6_against_a_github_remote(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkout, "remote_url", lambda remote: "git@github.com:owner/repo.git")
    monkeypatch.setattr(
        github,
        "discover_repository",
        lambda repo, remote_url: forge.RepositoryId(github.GITHUB_HOST, ("owner",), "repo"),
    )

    target = issue_claim._resolved_forge_target(None, "origin")

    assert target == forge.RepositoryId(github.GITHUB_HOST, ("owner",), "repo")


def _write_state_ref_pin(tmp_path: Path) -> None:
    """`.agent-claim/board.toml` pinned to `storage = "state-ref"`, in the
    isolated toplevel `_isolate_git_toplevel` (conftest.py) already
    redirects this process's `rev-parse --show-toplevel` to (issue #248)."""
    config_dir = tmp_path / ".agent-claim"
    config_dir.mkdir()
    (config_dir / "board.toml").write_text('storage = "state-ref"\n')


def test_lazy_forge_builds_a_state_ref_board_under_the_state_ref_pin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`_LazyForge` chooses its adapter by the repository's own `storage`
    pin (issue #248), never by the canonical remote's host: a state-ref
    pin must never build a `github.GitHubForge`, even when nothing else
    about the checkout looks unusual."""
    _write_state_ref_pin(tmp_path)
    stub = FakeForge(repository=forge.RepositoryId("file", (), str(tmp_path)))
    monkeypatch.setattr(
        issue_claim, "_state_ref_forge", lambda _repo, _remote, *, directory=None: stub
    )

    def unused(*_args: object, **_kwargs: object) -> None:
        pytest.fail("storage = state-ref must never build a GitHubForge")

    monkeypatch.setattr(github, "GitHubForge", unused)

    assert issue_claim.main(["board", "--json"]) == 0


def test_the_state_ref_read_only_stub_no_longer_exists() -> None:
    """Issue #283 proof 7: the placeholder `cut`/`rule`/`ask`/`claim` all
    refused with until #230 slice 4 wrote is gone, name and refusal
    function alike -- a state-ref pin now reaches its own write path
    instead."""
    assert not hasattr(issue_claim, "NOT_YET_STATE_REF_WRITE")
    assert not hasattr(issue_claim, "_refuse_state_ref_write")


def test_repo_is_refused_under_the_state_ref_pin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`--repo` names a GitHub target; under `storage = "state-ref"` there
    is no host-based target to override (issue #248), refused before this
    ever resolves a repository identity or touches the state ref."""
    _write_state_ref_pin(tmp_path)

    status = issue_claim.main(["--repo", "acme/items", "board", "--json"])

    assert status == 2
    assert capsys.readouterr().err == "ERROR: --repo is meaningless under storage = state-ref\n"


@pytest.mark.parametrize(
    "command", [["board", "--json"], ["rulings", "--json"], ["next", "--json"]]
)
def test_cli_board_family_reports_invalid_usage_when_repo_is_given_under_state_ref(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    command: list[str],
) -> None:
    """PIN-04, cited by BOARD-01/02 (`specs/board.spec.md`) and reused by
    `rulings`/`next` (issue #412): all three resolve the same `_LazyForge`
    `board` does, so `--repo` under `storage = state-ref` reports the same
    `invalid_usage` `ask`/`rule`/`brief` already do, never their broad
    `unavailable` catch-all."""
    _write_state_ref_pin(tmp_path)

    status = issue_claim.main(["--repo", "acme/items", *command])

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err == "ERROR: --repo is meaningless under storage = state-ref\n"
    _assert_json_refusal_object(captured.err, captured.out, reason="invalid_usage")


@pytest.mark.parametrize(
    "command", [["board", "--json"], ["rulings", "--json"], ["next", "--json"]]
)
def test_cli_board_family_reports_unavailable_for_a_forge_adapter_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    command: list[str],
) -> None:
    """The same generic catch-all `ask`/`rule`/`brief` already fall to
    (issue #412): a forge adapter construction failure that is not
    `RepoMeaninglessUnderStateRefError` reports `unavailable`, never
    `invalid_usage`, for `board`, `rulings`, and `next` alike."""
    monkeypatch.setattr(
        github,
        "GitHubForge",
        lambda repository: (_ for _ in ()).throw(ClaimError("adapter failed")),
    )

    status = issue_claim.main(["--repo", REPOSITORY, *command])

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err == "ERROR: adapter failed\n"
    _assert_json_refusal_object(captured.err, captured.out, reason="unavailable")


class _RefusingItemWriter:
    """`state_board.ItemWriter` for a landing test: `release --merged
    <sha>` under `storage = "state-ref"` folds its item write into one
    atomic `protocol.LandingIntent` (issue #359) and never calls
    `StateRefBoard`'s own injected writer at all -- a call reaching this is
    the test's own defect, not behaviour under test."""

    def write_item(
        self, item_id: str, *, expected: protocol.ObjectId | None, content: bytes
    ) -> protocol.ObjectId:
        raise AssertionError(f"unexpected write to item {item_id}")


def _landing_item_body(title: str) -> str:
    data: dict[str, object] = {
        "version": 1,
        "now": "Ship it.",
        "next": "keiner",
        "done_when": "Merged.",
        "record": {
            "title": title,
            "state": "open",
            "kind": "task",
            "labels": [],
            "blocked_by": [],
            "created_at": "2026-09-10T00:00:00Z",
            "updated_at": "2026-09-10T00:00:00Z",
        },
    }
    return f"Prose.\n\n```agent-claim\n{body.render_block(data)}```\n"


def _landing_item_oid(number: int) -> protocol.ObjectId:
    """A deterministic, well-formed fake blob oid for one landing test item
    -- its actual content-addressing never matters here, only that the
    same value seeds both the fake `StateRefBoard` and the fake store's own
    `ClaimState.items`, so `LandingIntent`'s CAS agrees with what
    `prepare_landing` reads."""
    return protocol.ObjectId(hashlib.sha1(f"landing-item-{number}".encode()).hexdigest())


_LANDING_ITEM_NUMBERS = (10, 11, 13)  # merge (#10), squash (#11, #12), rebase (#13)


def _landing_repository(tmp_path: Path) -> Path:
    """A real `origin`-backed repository (issue #359) whose `main` carries,
    in first-parent order: a merge commit trailer-naming item `aco-00000a`
    (#10), a squash commit whose trailer repeats `Work-Item:` for #11 and
    #12, and a commit landed through a real `git rebase` naming #13 --
    real merge/squash/rebase trunk history for `release --merged
    <sha|empty>`/`check <sha>` (LAND-47/LAND-48/LAND-52) to walk. `feature`
    never joins the first-parent line itself (only its merge commit does),
    giving the off-trunk refusal proof a real sha to reject."""
    repo, _remote = _real_repository_with_bare_remote(tmp_path)
    (repo / "base.txt").write_text("base\n")
    _real_git(repo, "add", "base.txt")
    _real_git(repo, "commit", "-q", "-m", "initial")

    _real_git(repo, "checkout", "-q", "-b", "feature")
    (repo / "feature.txt").write_text("feature\n")
    _real_git(repo, "add", "feature.txt")
    _real_git(repo, "commit", "-q", "-m", "feature work")
    _real_git(repo, "checkout", "-q", "main")
    _real_git(
        repo,
        "merge",
        "-q",
        "--no-ff",
        "-m",
        "Merge feature",
        "-m",
        "Work-Item: aco-00000a",
        "feature",
    )

    _real_git(repo, "checkout", "-q", "-b", "squashed")
    (repo / "squash.txt").write_text("one\n")
    _real_git(repo, "add", "squash.txt")
    _real_git(repo, "commit", "-q", "-m", "squash step")
    _real_git(repo, "checkout", "-q", "main")
    _real_git(repo, "merge", "-q", "--squash", "squashed")
    _real_git(repo, "commit", "-q", "-m", "Squash landing", "-m", "Work-Item: #11\nWork-Item: #12")

    _real_git(repo, "checkout", "-q", "-b", "docslane", "feature")
    (repo / "docs.txt").write_text("docs\n")
    _real_git(repo, "add", "docs.txt")
    _real_git(repo, "commit", "-q", "-m", "rebased landing", "-m", "Work-Item: #13")
    _real_git(repo, "rebase", "-q", "main")
    _real_git(repo, "checkout", "-q", "main")
    _real_git(repo, "merge", "-q", "--ff-only", "docslane")

    _push_repository_trunk(repo, "origin")
    return repo


def _landing_scenario(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, state_board.StateRefBoard]:
    """The shared fixture every atomic-landing test builds on (issue #359):
    `storage = "state-ref"`, a real trunk history (`_landing_repository`),
    a fake `StateRefBoard` over the three items it lands, a live claim on
    each, and a matching fake store whose `ClaimState.items` agrees with
    the board's own oids -- so `release --merged` (this module's own CLI
    path) and `prepare_landing`/`mark_landed` (`state_board.py`'s) are
    exercised together, exactly as a real run composes them."""
    _write_state_ref_pin(tmp_path)
    monkeypatch.setattr(checkout, "trunk_landings", _LIVE_TRUNK_LANDINGS)
    repo = _landing_repository(tmp_path)
    item_ids = {number: items.format_item_id(number) for number in _LANDING_ITEM_NUMBERS}
    item_files = {
        f"{item_id}.md": _landing_item_body(f"Item {number}").encode()
        for number, item_id in item_ids.items()
    }
    item_oids = {item_id: _landing_item_oid(number) for number, item_id in item_ids.items()}
    client = state_board.StateRefBoard(
        repository=forge.RepositoryId("file", ("acme",), "items"),
        default_branch="main",
        item_files=item_files,
        item_oids=item_oids,
        writer=_RefusingItemWriter(),
    )
    monkeypatch.setattr(
        issue_claim, "_state_ref_forge", lambda _repo, _remote, *, directory=None: client
    )
    claims = tuple(
        _active_claim(
            "Codex Sol",
            claim_id=f"claim-{number}",
            issue=number,
            branch=f"codex/issue-{number}-claims",
            scope=("src",),
        )
        for number in _LANDING_ITEM_NUMBERS
    )
    _patch_store_write(monkeypatch, *claims, items=item_oids)
    monkeypatch.chdir(repo)
    return repo, client


@pytest.mark.parametrize(
    ("number", "landing_ref", "use_sha"),
    [
        pytest.param(10, "main~2", True, id="merge-by-sha"),
        pytest.param(10, "main~2", False, id="merge-by-empty"),
        pytest.param(11, "main~1", True, id="squash-by-sha"),
        pytest.param(13, "main", False, id="rebase-by-empty"),
    ],
)
def test_release_merged_closes_and_releases_atomically_under_the_state_ref_pin(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    number: int,
    landing_ref: str,
    use_sha: bool,
) -> None:
    """Issue #359, LAND-47/LAND-52 (Beweis 1): a merge, squash, or rebase
    landing carrying `Work-Item:`/`aco-xxxxxx` in the trunk closes its item
    and releases its claim in one commit, one CAS, whether `--merged` names
    the exact sha or is given bare (the newest trunk commit naming this
    item)."""
    _repo, client = _landing_scenario(monkeypatch, tmp_path)
    args = ["release", str(number), "--agent", "Codex Sol", "--claim-id", f"claim-{number}"]
    if use_sha:
        sha = _real_git(_repo, "rev-parse", landing_ref).stdout.strip()
        args += ["--merged", sha]
    else:
        args += ["--merged"]

    status = issue_claim.main(args)

    assert status == 0
    out = capsys.readouterr().out
    assert out.startswith(f"RELEASED issue {items.format_item_id(number)}: claim-{number}\n")
    assert "freed:" in out
    assert "next:" in out
    assert client.item_reference(number).state is forge.ItemState.CLOSED
    remaining = store.fetch_state(worktree=Path("."), remote="origin").claims
    assert protocol.claim_key(protocol.IssueIdentity(number), "") not in remaining
    assert len(remaining) == len(_LANDING_ITEM_NUMBERS) - 1


def test_release_merged_refuses_a_trunk_item_the_state_ref_has_no_entry_for(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #359/CI: the squash commit's trailer names both `#11` and
    `#12` (LAND-02), but `_landing_scenario` seeds the state ref with only
    `#11`'s own item -- `release --merged` for `#12` refuses by name,
    before any claim or write, rather than crashing on a decode miss."""
    repo, client = _landing_scenario(monkeypatch, tmp_path)
    sha = _real_git(repo, "rev-parse", "main~1").stdout.strip()

    status = issue_claim.main(["release", "12", "--agent", "Codex Sol", "--merged", sha])

    assert status == 2
    assert capsys.readouterr().err == f"ERROR: #12 does not exist in {client.repository.path}\n"


@pytest.mark.parametrize(
    ("landing_ref", "reason"),
    [
        pytest.param("main~3", "carries no `Work-Item:` trailer", id="no-trailer"),
        pytest.param("main~1", "does not name work item #10", id="foreign-item"),
        pytest.param("feature", "is not on the first-parent trunk", id="off-trunk"),
    ],
)
def test_release_merged_refuses_a_landing_it_cannot_verify_under_the_state_ref_pin(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    landing_ref: str,
    reason: str,
) -> None:
    """Issue #359, LAND-52 (Beweis 2): a commit with no trailer, one naming
    a different item, or one outside the first-parent trunk all refuse by
    name, close nothing, and leave the claim live."""
    repo, client = _landing_scenario(monkeypatch, tmp_path)
    sha = _real_git(repo, "rev-parse", landing_ref).stdout.strip()

    status = issue_claim.main(
        ["release", "10", "--agent", "Codex Sol", "--claim-id", "claim-10", "--merged", sha]
    )

    assert status == 2
    assert capsys.readouterr().err == f"ERROR: {sha} {reason}\n"
    assert client.item_reference(10).state is forge.ItemState.OPEN
    remaining = store.fetch_state(worktree=Path("."), remote="origin").claims
    assert protocol.claim_key(protocol.IssueIdentity(10), "") in remaining


# --- Real `file://` state-ref proofs (issue #359 R3) ------------------------
#
# Every test above this point patches `store.fetch_state`/`commit_transition`
# with `_FakeStore` (`_landing_scenario`): it proves this module's own CLI
# wiring -- argument selection, the trunk walk, the printed report -- agrees
# with `protocol.apply`, but `protocol.apply` is exactly what `_FakeStore`
# calls too, so it can never prove one real git commit, a real CAS refusal,
# a real moved-ref race, or a real replay refusal. The tests below run
# `store.fetch_state`/`commit_transition` for real against a real bare
# `file://` remote (`_use_real_store`, below in the `reset` section, applies
# module-wide from here on) and assert on the ref/tree state a real clone
# would see, never on a fake's own bookkeeping.


def _real_landing_scenario(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, numbers: tuple[int, ...] = (10,)
) -> tuple[Path, Path]:
    """The real counterpart of `_landing_scenario`: the same real trunk
    history (`_landing_repository`), but `refs/aco/state` is real too, on
    the very same bare remote -- `store.fetch_state`/`commit_transition` run
    unfaked, and `_state_ref_forge` is never stubbed, so `release --merged`
    below drives the exact git commits and CAS its own atomicity claim is
    about."""
    _write_state_ref_pin(tmp_path)
    _use_real_store(monkeypatch)
    monkeypatch.setattr(checkout, "trunk_landings", _LIVE_TRUNK_LANDINGS)
    repo = _landing_repository(tmp_path)
    remote = tmp_path / "remote.git"
    store.bootstrap(worktree=repo, remote=str(remote))
    for number in numbers:
        _seed_real_claim_and_item(
            repo,
            remote,
            issue=number,
            claim_id=f"claim-{number}",
            content=_landing_item_body(f"Item {number}").encode(),
        )
    monkeypatch.chdir(repo)
    return repo, remote


def _seed_real_claim_and_item(
    worktree: Path, remote: Path, *, issue: int, claim_id: str, content: bytes
) -> tuple[protocol.ActiveClaim, protocol.ObjectId]:
    """Lands a real claim and a real item blob for `issue` in two ordinary
    transitions, so an atomicity proof below can land a third -- one
    `protocol.LandingIntent` -- and check that it, unlike these two, closes
    the item and releases the claim in the very same commit."""
    claim_state = store.commit_transition(
        worktree=worktree,
        remote=str(remote),
        subject=store.ClaimTransitionSubject(f"claim issue {issue}", item=str(issue)),
        intent=protocol.ClaimIntent(
            identity=protocol.IssueIdentity(issue),
            agent="Codex Sol",
            role="builder",
            base=protocol.ObjectId("c" * 40),
            branch=f"codex/issue-{issue}-claims",
            scope=("src",),
            claim_id=protocol.ClaimId(claim_id),
            operation_id=f"claim-op-{issue}",
        ),
    )
    claim = next(
        value
        for value in claim_state.claims.values()
        if value.identity == protocol.IssueIdentity(issue)
    )
    item_id = items.format_item_id(issue)
    new_oid = store.hash_blob(worktree, content)
    item_state = store.commit_transition(
        worktree=worktree,
        remote=str(remote),
        subject=store.TransitionSubject(f"seed item {item_id}"),
        intent=protocol.ItemWriteIntent(
            item_id=item_id, expected=None, new_oid=new_oid, operation_id=f"item-op-{issue}"
        ),
    )
    return claim, item_state.items[item_id]


def _state_ref_paths(repo: Path, tip: str) -> set[str]:
    return set(_real_git(repo, "ls-tree", "-r", "--name-only", tip).stdout.splitlines())


def _state_ref_blob(repo: Path, tip: str, path: str) -> str:
    return _real_git(repo, "show", f"{tip}:{path}").stdout


def _state_ref_tip(repo: Path, remote: Path) -> str:
    return _real_git(repo, "ls-remote", str(remote), store.STATE_REF).stdout.split()[0]


def test_release_merged_under_state_ref_commits_once_then_refuses_a_replay_as_closed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Issue #359 R3: a real `file://` proof of Beweis 1 -- one landing is
    exactly one new commit on `refs/aco/state` whose tree closes the item
    and drops the claim -- and of the replay case `_FakeStore` cannot see: a
    second `release` against the now-closed item refuses by name, through
    the real `state_board.StateRefBoard` re-read from the real ref, and
    moves the ref not at all."""
    repo, remote = _real_landing_scenario(monkeypatch, tmp_path, numbers=(10,))
    sha = _real_git(repo, "rev-parse", "main~2").stdout.strip()
    item_id = items.format_item_id(10)
    tip_before = _state_ref_tip(repo, remote)
    assert "claims/issue-10.toml" in _state_ref_paths(repo, tip_before)
    count_before = int(_real_git(repo, "rev-list", "--count", tip_before).stdout.strip())

    status = issue_claim.main(
        ["release", "10", "--agent", "Codex Sol", "--claim-id", "claim-10", "--merged", sha]
    )

    assert status == 0
    tip_after = _state_ref_tip(repo, remote)
    count_after = int(_real_git(repo, "rev-list", "--count", tip_after).stdout.strip())
    assert count_after == count_before + 1
    assert _real_git(repo, "rev-parse", f"{tip_after}^").stdout.strip() == tip_before
    paths_after = _state_ref_paths(repo, tip_after)
    assert "claims/issue-10.toml" not in paths_after
    assert f"items/{item_id}.md" in paths_after
    closed_body = body.parse_body(
        _state_ref_blob(repo, tip_after, f"items/{item_id}.md"), storage=body.Storage.STATE_REF
    )
    assert closed_body.record is not None
    closed_record = items.parse_item_record(item_id, closed_body.record)
    assert closed_record.state is items.RecordState.CLOSED
    capsys.readouterr()

    replay_status = issue_claim.main(
        ["release", "10", "--agent", "Codex Sol", "--claim-id", "claim-10", "--merged", sha]
    )

    assert replay_status == 2
    assert capsys.readouterr().err.startswith("ERROR: #10 is already closed (closed on ")
    assert _state_ref_tip(repo, remote) == tip_after


class _RaceOnceTransport:
    """A `store.PushTransport` that lets one unrelated writer land on
    `refs/aco/state` between this transition's own read and its first real
    push attempt (issue #359 R3): a genuine second `git push`, not a
    monkeypatched rejection, so the ordinary `commit_transition` retry loop
    meets a real non-fast-forward rejection and must actually refetch and
    reapply -- proof that no half-state (neither the racer's write nor this
    transition's own) is ever left applied only in part."""

    def __init__(self, worktree: Path, remote: Path) -> None:
        self._worktree = worktree
        self._remote = remote
        self._raced = False
        self._real = store.GitPushTransport()
        self.racer_commit: str | None = None

    def push(self, *, worktree: Path, remote: str, ref: str, new_oid: protocol.ObjectId) -> None:
        if not self._raced:
            self._raced = True
            current = _state_ref_tip(self._worktree, self._remote)
            tree = _real_git(self._worktree, "rev-parse", f"{current}^{{tree}}").stdout.strip()
            racer = _real_git(
                self._worktree, "commit-tree", tree, "-p", current, "-m", "unrelated racer"
            ).stdout.strip()
            _real_git(self._worktree, "push", str(self._remote), f"{racer}:{store.STATE_REF}")
            self.racer_commit = racer
        self._real.push(worktree=worktree, remote=remote, ref=ref, new_oid=new_oid)


def test_landing_intent_survives_an_unrelated_ref_move_with_no_half_state(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #359 R3: an unrelated writer lands on `refs/aco/state` between
    this transition's own fetch and its first push attempt -- the real,
    non-fast-forward rejection `commit_transition`'s retry loop exists for.
    The retry refetches, reapplies on top of the racer's own commit, and
    lands exactly one more commit: neither the racer's write nor this
    transition's is ever left half-applied."""
    worktree, bare_remote = _reset_repository(tmp_path)
    _use_real_store(monkeypatch)
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    claim, open_oid = _seed_real_claim_and_item(
        worktree, bare_remote, issue=10, claim_id="claim-10", content=b"open\n"
    )
    item_id = items.format_item_id(10)
    closed_oid = store.hash_blob(worktree, b"closed\n")
    racer = _RaceOnceTransport(worktree, bare_remote)

    new_state = store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject=store.ClaimTransitionSubject("release issue 10", item="10"),
        intent=protocol.LandingIntent(
            item_id=item_id,
            item_expected=open_oid,
            item_new_oid=closed_oid,
            claim_id=claim.claim_id,
            agent="Codex Sol",
            role="builder",
            outcome=protocol.LandedRelease(commit=protocol.ObjectId("d" * 40)),
            operation_id="land-op-10",
        ),
        transport=racer,
    )

    assert racer.racer_commit is not None
    # The racer's own commit is the ref state that stood between the
    # rejected first push and the retry that succeeded -- reading its tree
    # (an immutable git object, not a live poll) proves neither the item nor
    # the claim was ever half-landed there: the item is still open at its
    # pre-landing oid and the claim is still present, exactly as they were
    # before this transition ever touched the ref.
    racer_item_oid = _real_git(
        worktree, "rev-parse", f"{racer.racer_commit}:items/{item_id}.md"
    ).stdout.strip()
    assert racer_item_oid == open_oid
    assert "claims/issue-10.toml" in _state_ref_paths(worktree, racer.racer_commit)
    assert new_state.items[item_id] == closed_oid
    assert protocol.claim_key(protocol.IssueIdentity(10), "") not in new_state.claims
    assert new_state.tip is not None
    # The retry rebuilt directly on the racer's own commit -- no commit from
    # the rejected first attempt sits between them.
    parent = _real_git(worktree, "rev-parse", f"{new_state.tip}^").stdout.strip()
    assert parent == racer.racer_commit
    refetched = store.fetch_state(worktree=worktree, remote=str(bare_remote))
    assert refetched.items[item_id] == closed_oid
    assert protocol.claim_key(protocol.IssueIdentity(10), "") not in refetched.claims


def test_landing_intent_refuses_a_stale_item_oid_without_writing_anything(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #359 R3: a `LandingIntent` built from a stale `item_expected`
    (someone else edited the item after this caller's own read) refuses by
    name, the same CAS discipline `ItemWriteIntent` already proves, and
    writes nothing -- the claim stays live and the item stays open on the
    real ref, not just in a fake's own state."""
    worktree, bare_remote = _reset_repository(tmp_path)
    _use_real_store(monkeypatch)
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    claim, open_oid = _seed_real_claim_and_item(
        worktree, bare_remote, issue=10, claim_id="claim-10", content=b"open\n"
    )
    item_id = items.format_item_id(10)
    stale_oid = store.hash_blob(worktree, b"some other content\n")
    closed_oid_attempt = store.hash_blob(worktree, b"closed\n")
    tip_before = _state_ref_tip(worktree, bare_remote)
    intent = protocol.LandingIntent(
        item_id=item_id,
        item_expected=stale_oid,
        item_new_oid=closed_oid_attempt,
        claim_id=claim.claim_id,
        agent="Codex Sol",
        role="builder",
        outcome=protocol.LandedRelease(commit=protocol.ObjectId("d" * 40)),
        operation_id="land-op-10-stale",
    )

    subject = store.ClaimTransitionSubject("release issue 10", item="10")
    with pytest.raises(protocol.ClaimUnavailableError, match="was written since it was read"):
        store.commit_transition(
            worktree=worktree,
            remote=str(bare_remote),
            subject=subject,
            intent=intent,
        )

    assert _state_ref_tip(worktree, bare_remote) == tip_before
    unchanged = store.fetch_state(worktree=worktree, remote=str(bare_remote))
    assert unchanged.items[item_id] == open_oid
    assert protocol.claim_key(protocol.IssueIdentity(10), "") in unchanged.claims


def test_release_merged_under_the_state_ref_pin_requires_an_issue(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """Issue #359: an issue-less lane has no state-ref item to close, so
    `--merged` under `storage = "state-ref"` refuses it by name rather than
    resolving a trunk walk that could never apply."""
    _write_state_ref_pin(tmp_path)
    repo = _landing_repository(tmp_path)
    monkeypatch.chdir(repo)

    status = issue_claim.main(["release", "--merged", "--agent", "Codex Sol", "--branch", "docs/x"])

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: --merged under storage = state-ref requires an issue number; "
        "an issue-less lane has no item to close\n"
    )


def test_cli_version_exits_before_requiring_a_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        issue_claim.main(["--version"])

    assert exited.value.code == 0
    assert capsys.readouterr().out == f"aco {__version__}\n"


def test_cli_rescope_from_the_primary_checkout_points_back_at_the_claims_worktree(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The reported bug (#211): a held claim's worktree already exists, so
    running `rescope` from the primary checkout on `main` must not send an
    agent to build a second one. No branch is known from `main`, so the
    refusal names none."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    git_values = _git_checkout(branch="main")
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )

    status = issue_claim.main(
        ["--repo", REPOSITORY, "rescope", "72", "--agent", "Ada", "--add", "/repo/x.py"]
    )

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: build claims require an isolated non-main worktree branch; "
        "run this command from this claim's own worktree, not the primary checkout\n"
    )


def test_cli_rescope_from_a_shared_checkout_names_the_known_branch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Checked out directly on the claim's own branch inside the primary
    checkout, without a linked worktree -- the branch is already known here,
    so the refusal names it instead of leaving the sentence blank."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    git_values = _git_checkout(
        branch="codex/issue-72", git_directory="/repo/.git", common_directory="/repo/.git"
    )
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )

    status = issue_claim.main(
        ["--repo", REPOSITORY, "rescope", "72", "--agent", "Ada", "--add", "/repo/x.py"]
    )

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: build claims require a linked isolated worktree checkout; "
        "run this command from this claim's own worktree on 'codex/issue-72', "
        "not the primary checkout\n"
    )


def test_cli_claim_from_the_primary_checkout_still_names_the_create_recipe(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`claim`'s refusal is unchanged by #211 -- pinned here through the real
    command, alongside `rescope`'s corrected sentence above, since a fresh
    claim genuinely has no worktree yet to return to."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    git_values = _git_checkout(branch="main")
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )

    status = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Ada",
            "--branch",
            "main",
            "--scope",
            "src",
            "--claim-id",
            "cli-claim",
        ]
    )

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: build claims require an isolated non-main worktree branch; "
        f"run {checkout.ISOLATED_WORKTREE_RECIPE}\n"
    )


def _patch_release_session(
    monkeypatch: pytest.MonkeyPatch,
    client: FakeForge,
    *standing: ClaimRequest,
    agent: str = "Ada",
    branch: str | None = "lane-72",
    forbid_git: bool = False,
) -> None:
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: agent})
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    _patch_store_write(monkeypatch, *(_store_claim_from_request(claimed) for claimed in standing))
    if forbid_git:

        def git(arguments: list[str], **_kwargs: object) -> str:
            if tuple(arguments) == ("rev-parse", "--show-toplevel"):
                return "/repo"
            pytest.fail("explicit --claim-id must not inspect checkout branch")

        monkeypatch.setattr(checkout, "_git_output", git)
        return
    git_values = {
        ("branch", "--show-current"): branch or "",
        ("rev-parse", "--show-toplevel"): "/repo",
    }
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )


def _claim_without_agent_args(*flags: str) -> list[str]:
    return [
        "claim",
        "72",
        "--role",
        "builder",
        "--scope",
        "src",
        "--claim-id",
        "cli-claim",
        *flags,
    ]


def _parse_claim_command(*flags: str):
    return issue_claim._parser().parse_args(
        [
            "claim",
            "72",
            "--agent",
            "Codex Sol",
            "--role",
            "builder",
            "--scope",
            "src",
            "--claim-id",
            "cli-claim",
            *flags,
        ]
    )


@pytest.mark.parametrize(
    ("flags", "git_values", "error"),
    [
        ((), _git_checkout(), None),
        (("--branch", "codex/issue-72"), _git_checkout(), None),
        (("--base", BASE), _git_checkout(), None),
        (("--branch", "other"), _git_checkout(), "does not match checkout branch"),
        (("--base", "b" * 40), _git_checkout(), "does not match checkout HEAD"),
        (
            ("--base", "b" * 40, "--branch", "other"),
            _git_checkout(),
            "does not match checkout HEAD",
        ),
        ((), _git_checkout(branch="main"), "isolated non-main worktree branch"),
        # `master` is not this repository's default branch (`_git_checkout`'s
        # `origin/HEAD` resolves to `main`), so it binds like any other
        # non-default branch (issue #238) -- the fallback-denied case for an
        # unresolvable `origin/HEAD` is pinned separately, in
        # test_claim_default_branch_fallback_denies_only_main_and_master.
        ((), _git_checkout(branch="master"), None),
        (
            (),
            _git_checkout(git_directory="/repo/.git", common_directory="/repo/.git"),
            "linked isolated worktree",
        ),
        ((), _git_checkout(dirty=" M file"), "before the first worktree edit"),
    ],
)
def test_claim_request_binds_omitted_base_and_branch_to_checkout(
    monkeypatch: pytest.MonkeyPatch,
    flags: tuple[str, ...],
    git_values: dict[tuple[str, ...], str],
    error: str | None,
) -> None:
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    parsed = _parse_claim_command(*flags)
    if "--base" not in flags:
        assert parsed.base is None
    if "--branch" not in flags:
        assert parsed.branch is None

    if error is not None:
        with pytest.raises(ClaimError, match=error):
            issue_claim._request(parsed)
        return

    claimed = issue_claim._request(parsed)
    assert claimed.base == git_values[("rev-parse", "HEAD")]
    assert claimed.branch == git_values[("branch", "--show-current")]


def test_claim_request_refuses_a_base_that_is_not_a_full_commit_sha() -> None:
    parsed = _parse_claim_command("--base", "not-a-sha")

    with pytest.raises(ClaimError, match="base must be a full lowercase commit SHA"):
        issue_claim._request(parsed)


def test_matching_store_claim_never_replays_an_issueless_lane() -> None:
    """Issueless lanes keep the one-claim-per-branch contract: only a
    numbered item's replay is ever detected, matching `_cmd_claim`'s own
    `isinstance(requested.identity, IssueIdentity)` guard around this
    function's sole call site."""
    lane_request = request(lane=True, branch="docs/lane-claim-a")
    standing = _store_claim_from_request(lane_request)
    observed = protocol.ClaimState(
        tip=protocol.ObjectId(BASE),
        claims={protocol.claim_key(lane_request.identity, lane_request.branch): standing},
    )

    assert issue_claim._matching_store_claim(observed, lane_request) is None


def test_selected_store_claim_refuses_a_lane_identity_without_a_branch() -> None:
    """`rescope`/`release` both resolve a non-empty branch before this
    function ever sees a `LaneIdentity` in production; this pins the
    function's own boundary check as a direct unit test rather than relying
    on that upstream guarantee never slipping."""
    identity = protocol.LaneIdentity()
    with pytest.raises(ClaimUnavailableError, match="lane release requires a non-empty"):
        issue_claim._selected_store_claim(protocol.EMPTY_STATE, identity, "", None)


def test_claim_scope_is_optional_at_the_argparse_layer_for_issue_mode() -> None:
    """Issue #337: `required=True` is gone from `--scope` -- issue mode
    derives it from the item body when omitted, so argparse itself must
    accept the omission; `_cmd_claim`'s own runtime checks (lane mode still
    requiring it, issue mode refusing a body with no scope) are proven
    separately, against `main`."""
    parsed = issue_claim._parser().parse_args(
        ["claim", "42", "--agent", "Ada", "--role", "builder"]
    )

    assert parsed.scope is None


def test_cli_claim_role_argparse_default_unchanged_and_release_omits_role() -> None:
    claimed = issue_claim._parser().parse_args(["claim", "42", "--scope", "src/widget.py"])
    released = issue_claim._parser().parse_args(["release", "42", "--merged", "12"])

    assert claimed.role == issue_claim.DEFAULT_CLAIM_ROLE
    assert released.role is None
    assert released.merged == "12"
    assert released.abandoned is None
    assert released.claim_id is None
    assert released.coordinator_override is False


def test_release_merged_bare_flag_parses_as_the_empty_sentinel() -> None:
    """`release --merged` with no value (issue #359): the state-ref "pick
    the newest trunk landing" form -- distinct from omitting `--merged`
    altogether, which `--abandoned`'s own required-group partner still
    catches."""
    released = issue_claim._parser().parse_args(["release", "42", "--merged"])

    assert released.merged == ""


@pytest.mark.parametrize(
    ("role_flags", "role"),
    [
        ((), issue_claim.DEFAULT_CLAIM_ROLE),
        (("--role", "builder"), "builder"),
        (("--role", "coordinator"), "coordinator"),
    ],
)
def test_cli_claim_omitted_role_posts_default_and_explicit_wins(
    monkeypatch: pytest.MonkeyPatch,
    role_flags: tuple[str, ...],
    role: str,
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())

    claimed = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Codex Sol",
            *role_flags,
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src",
            "--claim-id",
            "cli-claim",
        ]
    )

    assert claimed == 0
    posted = _live_store_claim()
    assert posted.role == role


def test_cli_claim_empty_role_fails_closed_without_posting_builder(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    argv = [
        "--repo",
        REPOSITORY,
        "claim",
        "72",
        "--agent",
        "Codex Sol",
        "--role",
        "",
        "--base",
        BASE,
        "--branch",
        "codex/issue-72",
        "--scope",
        "src",
        "--claim-id",
        "cli-claim",
    ]

    parsed = issue_claim._parser().parse_args(argv)
    with pytest.raises(ClaimError, match=r"role.+must be one bounded non-empty line"):
        issue_claim._request(parsed)

    claimed = issue_claim.main(argv)
    captured = capsys.readouterr()

    assert claimed == 2
    assert captured.out == ""
    assert "ERROR:" in captured.err
    assert "role" in captured.err
    assert "must be one bounded non-empty line" in captured.err
    assert not store.fetch_state(worktree=Path("."), remote="origin").claims


@pytest.mark.parametrize(
    "arguments",
    [
        ["claim", "42", "--role", "builder", "--scope", "src/widget.py"],
        ["release", "42", "--role", "builder", "--abandoned", "stopped"],
    ],
)
def test_claim_and_release_parse_omitted_agent(
    monkeypatch: pytest.MonkeyPatch, arguments: list[str]
) -> None:
    _set_agent_identity_env(monkeypatch)
    parsed = issue_claim._parser().parse_args(arguments)
    assert parsed.agent is None


@pytest.mark.parametrize(
    ("explicit", "environ", "agent"),
    [
        (
            "Ada",
            {
                "ACO_AGENT": "Other",
                "GROK_SESSION_ID": "grok-session",
                "CLAUDE_SESSION_ID": "claude-session",
            },
            "Ada",
        ),
        (None, {"ACO_AGENT": "Ada"}, "Ada"),
        (None, {"ACO_AGENT": "", "GROK_SESSION_ID": "sess-1"}, "Grok sess-1"),
        (
            None,
            {"GROK_SESSION_ID": "sess-1", "CLAUDE_SESSION_ID": "sess-2"},
            "Grok sess-1",
        ),
        (None, {"CLAUDE_SESSION_ID": "sess-2"}, "Claude sess-2"),
        (
            None,
            {
                "ACO_AGENT": "",
                "GROK_SESSION_ID": "",
                "CLAUDE_SESSION_ID": "sess-2",
            },
            "Claude sess-2",
        ),
    ],
)
def test_request_and_cli_claim_fill_agent_from_documented_else_chain(
    monkeypatch: pytest.MonkeyPatch,
    explicit: str | None,
    environ: dict[str, str],
    agent: str,
) -> None:
    _set_agent_identity_env(monkeypatch, environ)
    git_values = _git_checkout()
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    command = _claim_without_agent_args()
    if explicit is not None:
        command.extend(["--agent", explicit])
    parsed = issue_claim._parser().parse_args(command)
    assert issue_claim._request(parsed).agent == agent

    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    assert issue_claim.main(["--repo", REPOSITORY, *command]) == 0
    posted = _live_store_claim()
    assert posted.agent == agent


@pytest.mark.parametrize(
    ("explicit", "environ"),
    [
        ("", {"ACO_AGENT": "Ada"}),
        (None, {"ACO_AGENT": " ", "GROK_SESSION_ID": "sess-1"}),
        (None, {"GROK_SESSION_ID": "bad\nid", "CLAUDE_SESSION_ID": "sess-2"}),
        (None, {"GROK_SESSION_ID": "x" * 200, "CLAUDE_SESSION_ID": "sess-2"}),
    ],
)
def test_invalid_agent_identity_fails_before_git_and_github(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    explicit: str | None,
    environ: dict[str, str],
) -> None:
    _set_agent_identity_env(monkeypatch, environ)
    _forbid_git_fill(monkeypatch)
    command = _claim_without_agent_args()
    if explicit is not None:
        command.extend(["--agent", explicit])
    parsed = issue_claim._parser().parse_args(command)
    with pytest.raises(ClaimError, match="agent must be one bounded non-empty line"):
        issue_claim._request(parsed)

    _forbid_github_construction(monkeypatch)
    releases = [
        ["release", "72", "--abandoned", "stopped"],
        ["release", "72", "--role", "builder", "--abandoned", "stopped"],
    ]
    if explicit is not None:
        for argv in releases:
            argv.extend(["--agent", explicit])
    for argv in (command, *releases):
        assert issue_claim.main(["--repo", REPOSITORY, *argv]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "ERROR:" in captured.err
        assert "agent must be one bounded non-empty line" in captured.err


@pytest.mark.parametrize(
    "environ",
    [
        {},
        {
            "ACO_AGENT": "",
            "GROK_SESSION_ID": "",
            "CLAUDE_SESSION_ID": "",
        },
        {"GROK_AGENT": "should-not-fill"},
    ],
)
def test_missing_agent_identity_fails_closed_without_github(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    environ: dict[str, str],
) -> None:
    _set_agent_identity_env(monkeypatch, environ)
    _forbid_git_fill(monkeypatch)
    command = _claim_without_agent_args()
    parsed = issue_claim._parser().parse_args(command)
    with pytest.raises(ClaimError) as raised:
        issue_claim._request(parsed)
    _assert_missing_identity_message(str(raised.value))

    _forbid_github_construction(monkeypatch)
    for argv in (
        command,
        ["release", "72", "--abandoned", "stopped"],
        ["release", "72", "--role", "builder", "--abandoned", "stopped"],
    ):
        assert issue_claim.main(["--repo", REPOSITORY, *argv]) == 2
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err.startswith("ERROR:")
        _assert_missing_identity_message(captured.err)


def test_cli_same_filled_agent_can_claim_and_release_without_flag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_agent_identity_env(monkeypatch, {"GROK_SESSION_ID": "session-1"})
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())

    claimed = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src",
            "--claim-id",
            "cli-claim",
        ]
    )
    released = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "release",
            "72",
            "--role",
            "builder",
            "--abandoned",
            "stopped",
            "--claim-id",
            "cli-claim",
        ]
    )

    assert (claimed, released) == (0, 0)
    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


def test_cli_two_session_claimants_cannot_release_without_extra_comment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_agent_identity_env(monkeypatch, {"GROK_SESSION_ID": "session-1"})
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())

    assert (
        issue_claim.main(
            [
                "--repo",
                REPOSITORY,
                "claim",
                "72",
                "--role",
                "builder",
                "--base",
                BASE,
                "--branch",
                "codex/issue-72",
                "--scope",
                "src",
                "--claim-id",
                "cli-claim",
            ]
        )
        == 0
    )
    capsys.readouterr()

    _set_agent_identity_env(monkeypatch, {"CLAUDE_SESSION_ID": "session-2"})
    released = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "release",
            "72",
            "--role",
            "builder",
            "--abandoned",
            "stopped",
            "--claim-id",
            "cli-claim",
        ]
    )
    captured = capsys.readouterr()

    assert released == 2
    assert "original claimant" in captured.err
    live = _live_store_claim()
    assert live.agent == "Grok session-1"


@pytest.mark.parametrize("role", ["builder", "reviewer"])
def test_cli_release_omitted_flags_posts_the_outcome_using_selected_claim_role(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    standing = request("mine", "Ada", issue=72, role=role, branch="lane-72", scope=("src",))
    client = FakeForge()
    _patch_release_session(monkeypatch, client, standing)

    released = issue_claim.main(["--repo", REPOSITORY, "release", "72", "--abandoned", "stopped"])

    assert released == 0
    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


def test_cli_release_omitted_claim_id_releases_when_foreign_peer_exists_on_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mine = request("mine", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",))
    client = FakeForge()
    _patch_release_session(monkeypatch, client, mine)

    released = issue_claim.main(["--repo", REPOSITORY, "release", "72", "--abandoned", "stopped"])

    assert released == 0
    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


@pytest.mark.parametrize(
    ("agent", "branch", "standing"),
    [
        (
            "Other",
            "lane-72",
            (
                request(
                    "mine",
                    "Ada",
                    issue=72,
                    role="reviewer",
                    branch="lane-72",
                    scope=("src",),
                ),
            ),
        ),
    ],
)
def test_cli_release_wrong_agent_or_branch_or_two_matches_fails_without_post(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    agent: str,
    branch: str,
    standing: tuple[ClaimRequest, ...],
) -> None:
    client = FakeForge()
    _patch_release_session(monkeypatch, client, *standing, agent=agent, branch=branch)

    released = issue_claim.main(["--repo", REPOSITORY, "release", "72", "--abandoned", "stopped"])
    captured = capsys.readouterr()

    assert released == 2
    assert captured.out == ""
    assert captured.err == (
        "ERROR: only the original claimant may release; use an explicit coordinator override "
        "(holder='Ada (reviewer)', this session='Other (reviewer)')\n"
    )
    assert "conflicting claims" not in captured.err


def test_cli_release_explicit_claim_id_ignores_checkout_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    standing = request("mine", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",))
    client = FakeForge()
    _patch_release_session(monkeypatch, client, standing, forbid_git=True)

    released = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "release",
            "72",
            "--claim-id",
            "mine",
            "--abandoned",
            "stopped",
        ]
    )
    assert released == 0
    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


@pytest.mark.parametrize(
    "flags",
    [
        ("--coordinator-override", "--abandoned", "takeover"),
        ("--coordinator-override", "--role", "builder", "--abandoned", "takeover"),
        ("--coordinator-override", "--merged", "12"),
    ],
)
def test_cli_release_override_fails_before_git_and_github(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    flags: tuple[str, ...],
) -> None:
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Ada"})
    _forbid_github_construction(monkeypatch)

    def unused(arguments: list[str], **_kwargs: object) -> str:
        pytest.fail("coordinator override must fail before git")

    monkeypatch.setattr(checkout, "_git_output", unused)

    released = issue_claim.main(["--repo", REPOSITORY, "release", "72", *flags])
    captured = capsys.readouterr()

    assert released == 2
    assert captured.out == ""
    assert "ERROR:" in captured.err
    assert "coordinator override" in captured.err


def test_cli_release_omitted_claim_id_fails_closed_on_detached_head(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Ada"})
    _forbid_github_construction(monkeypatch)
    monkeypatch.setattr(checkout, "_git_output", lambda arguments, **_kwargs: "")

    released = issue_claim.main(["--repo", REPOSITORY, "release", "72", "--abandoned", "stopped"])
    captured = capsys.readouterr()

    assert released == 2
    assert captured.out == ""
    assert "ERROR:" in captured.err
    assert "pass --claim-id" in captured.err


def test_cli_claim_omitted_base_and_branch_posts_filled_checkout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    git_values = _git_checkout()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())

    claimed = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Codex Sol",
            "--role",
            "builder",
            "--scope",
            "src",
            "--claim-id",
            "cli-claim",
        ]
    )

    assert claimed == 0
    assert "CLAIMED issue #72" in capsys.readouterr().out
    posted = _live_store_claim()
    assert posted.base == BASE
    assert posted.branch == "codex/issue-72"
    assert posted.scope == ("src",)


def test_cli_claim_and_release_round_trip_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())

    claimed = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Codex Sol",
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src",
            "--claim-id",
            "cli-claim",
        ]
    )
    released = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "release",
            "72",
            "--agent",
            "Codex Sol",
            "--role",
            "builder",
            "--abandoned",
            "stopped",
            "--claim-id",
            "cli-claim",
        ]
    )

    assert (claimed, released) == (0, 0)


def test_cli_dispatch_adapter_error_denies_with_exit_code_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`main`'s shared `_dispatch` error handling (still the path every
    command but `status`/`protect` takes -- issue #176): a forge
    adapter construction failure denies loud with exit 2, never a
    traceback."""
    monkeypatch.setattr(
        github,
        "GitHubForge",
        lambda repository: (_ for _ in ()).throw(ClaimError("adapter failed")),
    )
    assert issue_claim.main(["--repo", REPOSITORY, "board", "--json"]) == 2
    assert "ERROR: adapter failed" in capsys.readouterr().err


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 8, 21, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _stub_versioned_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        checkout,
        "versioned_paths",
        lambda **_kwargs: (
            "LICENSE",
            "README.md",
            "pyproject.toml",
            "src/agent_coordination/__init__.py",
        ),
    )


@pytest.fixture(autouse=True)
def _stub_board_config_tracked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every CLI test reads a tracked `board.toml` by default (issue #315):
    the untracked/ignored refusal is its own axis from `versioned_paths()`'s
    scope-width listing above, so a scope-width fixture fixing one never has
    to carry the other. A test proving the refusal itself overrides this."""
    stub_board_config_tracked(monkeypatch)


@pytest.fixture(autouse=True)
def _stub_canonical_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every CLI store command refuses a forge-target / canonical-remote
    mismatch (issue #176 done-when 6). Tests talk to `--repo example/agent-coordination`
    against a fake; this stub is the matching remote URL so they are not
    refused before the behaviour under test. Tests of `remote_url` itself
    (`tests/test_checkout.py`) rebind `_LIVE_REMOTE_URL`.
    """
    monkeypatch.setattr(checkout, "remote_url", lambda remote: f"git@github.com:{REPOSITORY}.git")


@pytest.fixture(autouse=True)
def _default_open_issue_reference(monkeypatch: pytest.MonkeyPatch) -> None:
    """A claim target outside the fetched open board defaults to OPEN.

    A closed or missing issue never appears in `list_open_board_issues` and
    would otherwise need a real `gh api` call; every test that isn't
    exercising that lookup relies on this default instead, and a test that
    does exercise it overrides `issue_claim._fetch_issue_reference` directly.
    """
    monkeypatch.setattr(
        issue_claim,
        "_fetch_issue_reference",
        lambda client, number: forge.ItemReference(forge.ItemState.OPEN, "", ""),
    )


# A PR checkout has no origin/main, so the live function would fail loud in CI;
# tests of trunk_landings itself (tests/test_checkout.py) call
# _LIVE_TRUNK_LANDINGS.
@pytest.fixture(autouse=True)
def _stub_trunk_landings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())


@pytest.fixture(autouse=True)
def _freeze_cli_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(issue_claim, "datetime", FixedDateTime)


def _patch_status_cli(monkeypatch: pytest.MonkeyPatch, client: FakeForge) -> None:
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(
        checkout,
        "versioned_paths",
        lambda **_kwargs: (
            "LICENSE",
            "README.md",
            "pyproject.toml",
            "src/agent_coordination/__init__.py",
        ),
    )
    monkeypatch.setattr(issue_claim, "datetime", FixedDateTime)


_STATUS_NOW = datetime(2026, 8, 21, tzinfo=UTC)


class _FakeStore:
    """An in-memory `refs/aco/state` double for CLI write-path tests (issue
    #176): `fetch_state`/`commit_transition` delegate here, but every
    transition still runs through the real `protocol.apply` -- identity/
    resource conflicts, coordinator override, and the codec all behave
    exactly as the real store would, without a git subprocess. This is the
    "store fake" the plan's own acceptance criteria name for claim/release/
    rescope tests. `claim_ages` answers every live claim's age as
    `_STATUS_NOW` (matching the file's autouse `FixedDateTime` "now") unless
    `ages` names a claim id's age explicitly -- board/rulings/next read ages
    through this same fake rather than a separate one.
    """

    def __init__(
        self,
        claims: Mapping[str, protocol.ActiveClaim] | None = None,
        *,
        tip: str | None = BASE,
        ages: Mapping[str, datetime] | None = None,
        consumed_ids: frozenset[protocol.ClaimId] | None = None,
        resources: Mapping[str, protocol.ResourceRecord] | None = None,
        items: Mapping[str, protocol.ObjectId] | None = None,
        lane_events: tuple[metrics.LaneEvent, ...] = (),
        unparsed_lifecycle_commits: int = 0,
    ) -> None:
        live = dict(claims or {})
        derived_ids = frozenset(claim.claim_id for claim in live.values())
        derived_resources: dict[str, protocol.ResourceRecord] = {}
        occupied: dict[str, set[int]] = {}
        for claim in live.values():
            if claim.resource is None:
                continue
            occupied.setdefault(claim.resource.name, set()).add(claim.resource.value)
        derived_resources = {
            name: protocol.ResourceRecord(name, tuple(sorted(values)))
            for name, values in occupied.items()
        }
        self.state = protocol.ClaimState(
            tip=None if tip is None else protocol.ObjectId(tip),
            claims=live,
            consumed_ids=consumed_ids if consumed_ids is not None else derived_ids,
            resources=dict(resources) if resources is not None else derived_resources,
            items=dict(items or {}),
        )
        self.transitions: list[protocol.ClaimTransitionIntent] = []
        self._ages = dict(ages or {})
        self._lane_events = lane_events
        self._unparsed_lifecycle_commits = unparsed_lifecycle_commits

    def fetch_state(self, *, worktree: Path, remote: str) -> protocol.ClaimState:
        return self.state

    def peek_state(self, *, worktree: Path, remote: str) -> protocol.ClaimState:
        # This fake never models the anchor/lineage-stamp side effect
        # `fetch_state` alone carries against real git, so the same
        # in-memory state answers both (issue #405): `land`'s claims
        # observation is the one caller this fake needs it for.
        return self.state

    def commit_transition(
        self,
        *,
        worktree: Path,
        subject: str,
        intent: protocol.ClaimTransitionIntent,
        item: str | None = None,
        remote: str = "origin",
        transport: object = None,
    ) -> protocol.ClaimState:
        if self.state.tip is None:
            raise protocol.ClaimError(protocol.MISSING_STATE_REF)
        self.transitions.append(intent)
        self.state = protocol.apply(self.state, intent)
        return self.state

    def claim_ages(
        self,
        *,
        worktree: Path,
        tip: protocol.ObjectId,
        claims: Iterable[protocol.ActiveClaim],
    ) -> dict[str, datetime]:
        return {claim.claim_id: self._ages.get(claim.claim_id, _STATUS_NOW) for claim in claims}

    def claim_lifecycle(self, *, worktree: Path, tip: protocol.ObjectId) -> store.ClaimLifecycle:
        return store.ClaimLifecycle(
            events=self._lane_events, unparsed=self._unparsed_lifecycle_commits
        )


def _patch_store_write(
    monkeypatch: pytest.MonkeyPatch,
    *claims: protocol.ActiveClaim,
    tip: str | None = BASE,
    ages: Mapping[str, datetime] | None = None,
    consumed_ids: frozenset[protocol.ClaimId] | None = None,
    resources: Mapping[str, protocol.ResourceRecord] | None = None,
    items: Mapping[str, protocol.ObjectId] | None = None,
    lane_events: tuple[metrics.LaneEvent, ...] = (),
    unparsed_lifecycle_commits: int = 0,
) -> _FakeStore:
    fake = _FakeStore(
        {protocol.claim_key(claim.identity, claim.branch): claim for claim in claims},
        tip=tip,
        ages=ages,
        consumed_ids=consumed_ids,
        resources=resources,
        items=items,
        lane_events=lane_events,
        unparsed_lifecycle_commits=unparsed_lifecycle_commits,
    )
    monkeypatch.setattr(store, "fetch_state", fake.fetch_state)
    monkeypatch.setattr(store, "peek_state", fake.peek_state)
    monkeypatch.setattr(store, "commit_transition", fake.commit_transition)
    monkeypatch.setattr(store, "claim_ages", fake.claim_ages)
    monkeypatch.setattr(store, "claim_lifecycle", fake.claim_lifecycle)
    monkeypatch.setattr(checkout, "remote_url", lambda remote: f"git@github.com:{REPOSITORY}.git")
    return fake


@pytest.fixture(autouse=True)
def _stub_store_write(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every CLI write/read of `refs/aco/state` uses the in-memory fake unless
    a test installs a more specific one (`_patch_store_write` with standing
    claims, `_patch_status_store`, or `tip=None` for the missing-ref case).
    """
    _patch_store_write(monkeypatch)


def _patch_status_store(
    monkeypatch: pytest.MonkeyPatch,
    *claims: protocol.ActiveClaim,
    ages: Mapping[str, datetime] | None = None,
) -> None:
    """Fake `status`'s two store reads (issue #176): the fetched claim state,
    and each claim's age (every claim reads as opened at `_STATUS_NOW` -- 0h
    0m old -- unless `ages` names it by claim id). Every `status`/
    `status --path` test builds its live claims via `_active_claim` and
    wires them in here instead of posting through a ledger-comment
    `FakeForge`.
    """
    monkeypatch.setattr(checkout, "remote_url", lambda remote: f"git@github.com:{REPOSITORY}.git")
    keyed = {protocol.claim_key(claim.identity, claim.branch): claim for claim in claims}
    state = protocol.ClaimState(tip=protocol.ObjectId(BASE), claims=keyed)
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)
    resolved_ages: dict[str, datetime] = {claim.claim_id: _STATUS_NOW for claim in claims}
    if ages is not None:
        resolved_ages.update(ages)

    def fake_claim_ages(
        *,
        worktree: Path,
        tip: protocol.ObjectId,
        claims: Iterable[protocol.ActiveClaim],
    ) -> dict[str, datetime]:
        return {claim.claim_id: resolved_ages[claim.claim_id] for claim in claims}

    monkeypatch.setattr(store, "claim_ages", fake_claim_ages)
    monkeypatch.setattr(issue_claim, "datetime", FixedDateTime)


def test_cli_status_empty_store_prints_unclaimed_repository(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_store(monkeypatch)

    assert issue_claim.main(["--repo", REPOSITORY, "status"]) == 0
    assert capsys.readouterr().out == "UNCLAIMED repository\n"


def test_cli_status_before_bootstrap_prints_unclaimed_repository(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A repository with no `refs/aco/state` at all (`EMPTY_STATE`, `tip is
    None`) still answers `status` plainly -- there is nothing to derive a
    claim's age from yet because there are no claims yet either."""
    monkeypatch.setattr(checkout, "remote_url", lambda remote: f"git@github.com:{REPOSITORY}.git")
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: protocol.EMPTY_STATE)
    monkeypatch.setattr(issue_claim, "datetime", FixedDateTime)

    assert issue_claim.main(["--repo", REPOSITORY, "status"]) == 0
    assert capsys.readouterr().out == "UNCLAIMED repository\n"


def test_cli_status_reports_unavailable_for_a_rewritten_state_ref(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`status` (issue #406) reports every `protocol.ClaimError` `fetch_state`
    itself can raise -- here a rewritten `refs/aco/state` (CLAIM-50) -- through
    the shared emitter as `reason: unavailable`, never `main`'s legacy
    `{"ok": false, "error": ...}` shape."""
    monkeypatch.setattr(checkout, "remote_url", lambda remote: f"git@github.com:{REPOSITORY}.git")

    def raising_fetch_state(*, worktree: Path, remote: str) -> protocol.ClaimState:
        raise protocol.StateLineageError("<oid> is not an ancestor of <tip>")

    monkeypatch.setattr(store, "fetch_state", raising_fetch_state)

    status = issue_claim.main(["--repo", REPOSITORY, "status", "--json"])

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err == "ERROR: <oid> is not an ancestor of <tip>\n"
    _assert_json_refusal_object(captured.err, captured.out, reason="unavailable")


def test_cli_status_json_before_bootstrap_reports_a_null_tip(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`status --json`'s `tip` is `null` for `EMPTY_STATE` (issue #256): a
    repository with no `refs/aco/state` ref yet has no oid a monitor could
    poll for movement."""
    monkeypatch.setattr(checkout, "remote_url", lambda remote: f"git@github.com:{REPOSITORY}.git")
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: protocol.EMPTY_STATE)
    monkeypatch.setattr(issue_claim, "datetime", FixedDateTime)

    assert issue_claim.main(["--repo", REPOSITORY, "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tip"] is None


def test_cli_status_issue_with_no_claim_prints_unclaimed_issue(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_store(monkeypatch)

    assert issue_claim.main(["--repo", REPOSITORY, "status", "72"]) == 0
    assert capsys.readouterr().out == "UNCLAIMED issue #72\n"


def test_cli_status_shows_a_live_store_claim(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    claimed = _active_claim(
        "Codex Sol", claim_id="cli-claim", issue=72, branch="codex/issue-72", scope=("src",)
    )
    _patch_status_store(monkeypatch, claimed)

    status = issue_claim.main(["--repo", REPOSITORY, "status", "72"])
    assert status == 0
    assert capsys.readouterr().out == (
        f"CLAIMED issue #72: Codex Sol (builder) base={BASE} "
        "branch=codex/issue-72 claim=cli-claim 0h 0m\n"
        "  src\n"
    )


def test_cli_status_prints_the_resource_line_for_an_allocated_hold(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    claimed = _active_claim(
        "Ada",
        claim_id="hop-1",
        issue=72,
        scope=("src",),
        resource=protocol.ResourceHold("schema-hop", 1),
    )
    _patch_status_store(monkeypatch, claimed)

    status = issue_claim.main(["--repo", REPOSITORY, "status", "72"])

    assert status == 0
    assert "  resource schema-hop=1\n" in capsys.readouterr().out


def test_cli_lane_claim_and_release_round_trip_without_issue_number(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The `claim`/`release` half of `Done when` #1: a docs/ checkout claims
    and releases again, all without ever passing an issue number. (The
    `status` half -- that the store shows a live lane claim -- is proven
    separately below now that `status` no longer reads what this ledger
    `claim`/`release` pair posts; issue #176 migrates one command at a
    time, and `claim`/`release` have not moved onto the store yet.)"""
    _set_agent_identity_env(monkeypatch, {"ACO_AGENT": "Codex Sol"})
    client = FakeForge()
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    git_values = {
        ("branch", "--show-current"): "docs/lane-cleanup",
        ("rev-parse", "--show-toplevel"): "/repo",
        ("rev-parse", "HEAD"): BASE,
    }
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )

    assert (
        issue_claim.main(
            [
                "--repo",
                REPOSITORY,
                "claim",
                "--role",
                "builder",
                "--base",
                BASE,
                "--branch",
                "docs/lane-cleanup",
                "--scope",
                "docs",
                "--claim-id",
                "cli-lane-claim",
            ]
        )
        == 0
    )
    capsys.readouterr()

    released = issue_claim.main(["--repo", REPOSITORY, "release", "--abandoned", "stopped"])
    assert released == 0
    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


def test_cli_status_shows_a_live_lane_claim(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    claimed = _active_claim(
        "Codex Sol",
        claim_id="cli-lane-claim",
        lane=True,
        branch="docs/lane-cleanup",
        scope=("docs",),
    )
    _patch_status_store(monkeypatch, claimed)

    assert issue_claim.main(["--repo", REPOSITORY, "status"]) == 0
    assert capsys.readouterr().out == (
        f"CLAIMED lane docs/lane-cleanup: Codex Sol (builder) base={BASE} "
        "branch=docs/lane-cleanup claim=cli-lane-claim 0h 0m\n"
        "  docs\n"
    )


@pytest.mark.parametrize("command", ["claim", "release"])
def test_cli_lane_mode_refuses_a_non_conventional_branch(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_agent_identity_env(monkeypatch, {"ACO_AGENT": "Codex Sol"})
    client = FakeForge()
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: "codex/issue-38-issueless-claims"
    )

    arguments = ["--repo", REPOSITORY, command]
    if command == "claim":
        arguments += [
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            "codex/issue-38-issueless-claims",
            "--scope",
            "src",
            "--claim-id",
            "cli-lane-claim",
        ]
    else:
        arguments += ["--abandoned", "stopped"]

    assert issue_claim.main(arguments) == 2
    captured = capsys.readouterr()
    assert "codex/issue-38-issueless-claims" in captured.err
    assert "issue number" in captured.err
    assert "'docs/'" in captured.err
    assert "'fix/'" in captured.err


def test_cli_release_requires_a_non_empty_current_branch_without_an_issue(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_agent_identity_env(monkeypatch, {"ACO_AGENT": "Codex Sol"})
    client = FakeForge()
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_git_output", lambda arguments, **_kwargs: "")

    status = issue_claim.main(["--repo", REPOSITORY, "release", "--abandoned", "stopped"])

    assert status == 2
    assert "lane release requires a non-empty current branch" in capsys.readouterr().err


def test_cli_status_overlapping_store_claims_print_notes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = _active_claim("Codex Sol", claim_id="claim-a", issue=72, scope=("shared",))
    second = _active_claim(
        "Grok 4.6",
        claim_id="claim-b",
        issue=73,
        branch="codex/issue-73-claims",
        scope=("shared/file.py",),
    )
    _patch_status_store(monkeypatch, first, second)

    status = issue_claim.main(["--repo", REPOSITORY, "status"])
    assert status == 0
    assert capsys.readouterr().out == (
        f"CLAIMED issue #72: Codex Sol (builder) base={BASE} "
        "branch=codex/issue-72-claims claim=claim-a 0h 0m\n"
        "  shared\n"
        "  overlaps issue #73 (claim-b)\n"
        f"CLAIMED issue #73: Grok 4.6 (builder) base={BASE} "
        "branch=codex/issue-73-claims claim=claim-b 0h 0m\n"
        "  shared/file.py\n"
        "  overlaps issue #72 (claim-a)\n"
    )


def test_cli_rescope_requires_a_non_empty_current_branch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rescope always operates on the checked-out worktree's own claim, so a
    detached or branchless checkout must refuse even when an issue number is
    also given -- unlike release, it never falls back to the issue alone."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    git_values = _git_checkout(branch="")
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )

    status = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "rescope",
            "72",
            "--agent",
            "Ada",
            "--add",
            "/repo/src/new.py",
        ]
    )

    assert status == 2
    assert "non-empty current branch" in capsys.readouterr().err


def test_status_direct_empty_claims_prints_unclaimed_repository_without_ledger(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _status((), None, {}, body.Storage.GITHUB) == 0
    assert capsys.readouterr().out == "UNCLAIMED repository\n"


def test_cli_status_json_empty_store_prints_unclaimed_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_store(monkeypatch)

    assert issue_claim.main(["--repo", REPOSITORY, "status", "--json"]) == 0
    assert (
        capsys.readouterr().out
        == json.dumps({"ok": True, "reason": "unclaimed", "issue": None, "tip": BASE, "claims": []})
        + "\n"
    )


def test_cli_status_json_issue_with_no_claim_prints_unclaimed_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_store(monkeypatch)

    assert issue_claim.main(["--repo", REPOSITORY, "status", "72", "--json"]) == 0
    assert (
        capsys.readouterr().out
        == json.dumps({"ok": True, "reason": "unclaimed", "issue": 72, "tip": BASE, "claims": []})
        + "\n"
    )


def test_cli_status_json_shows_a_live_store_claim(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    claimed = _active_claim(
        "Codex Sol", claim_id="cli-claim", issue=72, branch="codex/issue-72", scope=("src",)
    )
    _patch_status_store(monkeypatch, claimed)

    status = issue_claim.main(["--repo", REPOSITORY, "status", "72", "--json"])
    assert status == 0
    assert (
        capsys.readouterr().out
        == json.dumps(
            {
                "ok": True,
                "reason": "claimed",
                "issue": 72,
                "tip": BASE,
                "claims": [
                    {
                        "issue": 72,
                        "lane": None,
                        "claim_id": "cli-claim",
                        "agent": "Codex Sol",
                        "role": "builder",
                        "base": BASE,
                        "branch": "codex/issue-72",
                        "scope": ["src"],
                        "resource": None,
                        "resource_value": None,
                        "overlaps": [],
                        "state": "CLAIMED",
                        "age": "0h 0m",
                        "old": False,
                    }
                ],
            }
        )
        + "\n"
    )


def test_cli_status_json_overlapping_store_claims_print_claimed_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = _active_claim("Codex Sol", claim_id="claim-a", issue=72, scope=("shared",))
    second = _active_claim(
        "Grok 4.6",
        claim_id="claim-b",
        issue=73,
        branch="codex/issue-73-claims",
        scope=("shared/file.py",),
    )
    _patch_status_store(monkeypatch, first, second)

    status = issue_claim.main(["--repo", REPOSITORY, "status", "--json"])
    assert status == 0
    assert (
        capsys.readouterr().out
        == json.dumps(
            {
                "ok": True,
                "reason": "claimed",
                "issue": None,
                "tip": BASE,
                "claims": [
                    {
                        "issue": 72,
                        "lane": None,
                        "claim_id": "claim-a",
                        "agent": "Codex Sol",
                        "role": "builder",
                        "base": BASE,
                        "branch": "codex/issue-72-claims",
                        "scope": ["shared"],
                        "resource": None,
                        "resource_value": None,
                        "overlaps": [
                            {
                                "issue": 73,
                                "lane": None,
                                "claim_id": "claim-b",
                                "agent": "Grok 4.6",
                            }
                        ],
                        "state": "CLAIMED",
                        "age": "0h 0m",
                        "old": False,
                    },
                    {
                        "issue": 73,
                        "lane": None,
                        "claim_id": "claim-b",
                        "agent": "Grok 4.6",
                        "role": "builder",
                        "base": BASE,
                        "branch": "codex/issue-73-claims",
                        "scope": ["shared/file.py"],
                        "resource": None,
                        "resource_value": None,
                        "overlaps": [
                            {
                                "issue": 72,
                                "lane": None,
                                "claim_id": "claim-a",
                                "agent": "Codex Sol",
                            }
                        ],
                        "state": "CLAIMED",
                        "age": "0h 0m",
                        "old": False,
                    },
                ],
            }
        )
        + "\n"
    )


def test_cli_status_json_issue_on_overlap_prints_related_claimed_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = _active_claim("Codex Sol", claim_id="claim-a", issue=72, scope=("shared",))
    second = _active_claim(
        "Grok 4.6",
        claim_id="claim-b",
        issue=73,
        branch="codex/issue-73-claims",
        scope=("shared/file.py",),
    )
    _patch_status_store(monkeypatch, first, second)

    status = issue_claim.main(["--repo", REPOSITORY, "status", "72", "--json"])
    assert status == 0
    assert (
        capsys.readouterr().out
        == json.dumps(
            {
                "ok": True,
                "reason": "claimed",
                "issue": 72,
                "tip": BASE,
                "claims": [
                    {
                        "issue": 72,
                        "lane": None,
                        "claim_id": "claim-a",
                        "agent": "Codex Sol",
                        "role": "builder",
                        "base": BASE,
                        "branch": "codex/issue-72-claims",
                        "scope": ["shared"],
                        "resource": None,
                        "resource_value": None,
                        "overlaps": [
                            {
                                "issue": 73,
                                "lane": None,
                                "claim_id": "claim-b",
                                "agent": "Grok 4.6",
                            }
                        ],
                        "state": "CLAIMED",
                        "age": "0h 0m",
                        "old": False,
                    },
                    {
                        "issue": 73,
                        "lane": None,
                        "claim_id": "claim-b",
                        "agent": "Grok 4.6",
                        "role": "builder",
                        "base": BASE,
                        "branch": "codex/issue-73-claims",
                        "scope": ["shared/file.py"],
                        "resource": None,
                        "resource_value": None,
                        "overlaps": [
                            {
                                "issue": 72,
                                "lane": None,
                                "claim_id": "claim-a",
                                "agent": "Codex Sol",
                            }
                        ],
                        "state": "CLAIMED",
                        "age": "0h 0m",
                        "old": False,
                    },
                ],
            }
        )
        + "\n"
    )


def test_cli_claim_and_release_accept_json_while_parent_and_bootstrap_reject_it() -> None:
    claimed = issue_claim._parser().parse_args(
        ["claim", "42", "--scope", "src/widget.py", "--json"]
    )
    released = issue_claim._parser().parse_args(["release", "42", "--merged", "12", "--json"])
    omitted_claim = issue_claim._parser().parse_args(["claim", "42", "--scope", "src"])
    omitted_release = issue_claim._parser().parse_args(["release", "42", "--merged", "12"])

    assert claimed.json is True
    assert released.json is True
    assert omitted_claim.json is False
    assert omitted_release.json is False
    with pytest.raises(SystemExit) as refused_parent:
        issue_claim.main(["--json", "status"])
    assert refused_parent.value.code == 2
    with pytest.raises(SystemExit) as refused_bootstrap:
        issue_claim.main(["bootstrap", "--json"])
    assert refused_bootstrap.value.code == 2


@pytest.mark.parametrize(
    ("scope_flags", "board_issues"),
    [
        pytest.param(("--scope", "src"), (), id="an-explicit-scope"),
        pytest.param(
            (),
            (board_issue(72, "Work", complete_contract("Ship it.", scope=["src"])),),
            id="a-derived-scope",
        ),
    ],
)
def test_cli_claim_without_json_prints_the_claimed_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scope_flags: tuple[str, ...],
    board_issues: tuple[board.Issue, ...],
) -> None:
    """Issue #337 proof 3 (REVISE finding 2): the human cost line prints
    identically whether the scope came from `--scope` or was derived from
    the item's own body -- the board fetch a derived scope costs is never a
    silent, unprinted detour."""
    client = _arranged_claim_client(monkeypatch)
    client.board_issues = board_issues

    claimed = issue_claim.main(_claim_argv(*scope_flags))

    assert claimed == 0
    assert capsys.readouterr().out == (
        "CLAIMED issue #72: cli-claim\n"
        "1 of 4 versioned files (25%); overlaps no other open claims\n"
    )


def _arranged_claim_client(monkeypatch: pytest.MonkeyPatch) -> FakeForge:
    """The GitHub-storage `claim` arrangement every scope-derivation proof
    below shares (issue #337): a fake forge, a no-op checkout validator, a
    fixed versioned-file listing via `_git_checkout`, and a fresh in-memory
    store -- the same six lines `test_cli_claim_without_json_prints_the_claimed_line`
    repeats inline, factored so each derivation case states only what makes
    it different."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    git_values = _git_checkout()
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    _patch_store_write(monkeypatch)
    return client


def _claim_argv(*flags: str) -> list[str]:
    return [
        "--repo",
        REPOSITORY,
        "claim",
        "72",
        "--agent",
        "Codex Sol",
        "--role",
        "builder",
        "--base",
        BASE,
        "--branch",
        "codex/issue-72",
        "--claim-id",
        "cli-claim",
        *flags,
    ]


@pytest.mark.parametrize(
    (
        "item_scope",
        "requested_scope_flags",
        "expected_status",
        "expected_scope_or_error",
        "versioned",
        "expected_cost",
    ),
    [
        pytest.param(
            ["src/work.py"],
            (),
            0,
            ["src/work.py"],
            None,
            (0, 4, 0.0),
            id="omitted-takes-the-items-own-scope",
        ),
        pytest.param(
            None,
            (),
            2,
            issue_claim.CLAIM_SCOPE_MISSING,
            None,
            None,
            id="omitted-with-no-body-scope-refuses-by-name",
        ),
        pytest.param(
            ["src/work.py"],
            ("--scope", "src/other.py"),
            2,
            issue_claim.CLAIM_SCOPE_MISMATCH,
            None,
            None,
            id="a-differing-explicit-scope-refuses-by-name",
        ),
        pytest.param(
            ["a.py", "b.py"],
            ("--scope", "b.py", "--scope", "a.py"),
            0,
            ["a.py", "b.py"],
            None,
            (0, 4, 0.0),
            id="the-same-set-in-a-different-order-is-accepted",
        ),
        pytest.param(
            ["a.py", "b.py", "c.py", "d.py"],
            (),
            2,
            "scope is wide: 4 paths exceeds three; pass --whole REASON or set whole in the body",
            None,
            None,
            id="a-derived-wide-scope-refuses-without-whole",
        ),
        pytest.param(
            ["a.py", "b.py", "c.py", "d.py"],
            ("--whole", "spans the whole review pass"),
            0,
            ["a.py", "b.py", "c.py", "d.py"],
            None,
            (0, 4, 0.0),
            id="a-derived-wide-scope-is-accepted-with-whole",
        ),
        pytest.param(
            ["docs/report,v2.md"],
            (),
            0,
            ["docs/report,v2.md"],
            ("docs/report,v2.md",),
            (1, 1, 1.0),
            id="a-comma-inside-a-derived-path-grounds-and-is-kept-whole",
        ),
        pytest.param(
            ["a.py,b.py"],
            (),
            2,
            "'a.py,b.py' matches no versioned file; one --scope path per flag, so its comma "
            "is read literally -- repeat --scope for a second path",
            None,
            None,
            id="a-comma-inside-a-derived-path-that-grounds-nothing-refuses-by-name",
        ),
    ],
)
def test_cli_claim_scope_derivation_against_the_items_own_body(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    item_scope: list[str] | None,
    requested_scope_flags: tuple[str, ...],
    expected_status: int,
    expected_scope_or_error: list[str] | str,
    versioned: tuple[str, ...] | None,
    expected_cost: tuple[int, int, float] | None,
) -> None:
    """Issue #337 proof 3 (REVISE finding 2): issue-mode `--scope`
    derivation and validation against the item's own body -- omitted takes
    it, refusing by name when the body carries none; an explicit value must
    name the same canonical set (#331's own `protocol.valid_scope`),
    refusing by name when it differs and accepting the same set typed in a
    different order; a derived scope is exactly as wide, by the same rule,
    as one passed on `--scope` -- refused without `--whole`, accepted with
    it. Every row reads the open board -- the listing `claim` needs anyway
    for its slice-rule checks -- exactly once, whether the scope came from
    it (omitted `--scope`) or was only checked against it (explicit
    `--scope`), and never falls back to the single-item lookup, since #72
    is always open and always in that listing.

    Delta review (issue #337): a derived scope routes through the same
    `_scope_versioning` call as an explicit one (`cli.py`'s `_cmd_claim`),
    so the comma-grounding rule proved on `--scope` by
    `test_cli_claim_scope_keeps_a_comma_inside_one_path` and
    `test_cli_claim_refuses_a_comma_scope_that_matches_nothing_in_the_checkout`
    must hold identically for a body-derived one -- the two trailing rows
    here ground a comma-bearing entry against a matching versioned file and
    refuse one that matches nothing, by the same message. Every accepting
    row also asserts the `--json` cost fields (`versioned_files`,
    `versioned_files_total`, `share`) a derived claim prints, not only its
    resolved `scope` -- the same fields an explicit `--scope` claim prints,
    since both origins feed the one shared `_scope_versioning` call."""
    client = _arranged_claim_client(monkeypatch)
    body = (
        complete_contract("Ship it.")
        if item_scope is None
        else complete_contract("Ship it.", scope=item_scope)
    )
    client.board_issues = (board_issue(72, "Work", body),)
    if versioned is not None:
        monkeypatch.setattr(checkout, "versioned_paths", lambda **_kwargs: versioned)
    board_reads: list[None] = []
    real_list_open_board_issues = client.list_open_board_issues

    def _counted_list_open_board_issues() -> tuple[board.Issue, ...]:
        board_reads.append(None)
        return real_list_open_board_issues()

    monkeypatch.setattr(client, "list_open_board_issues", _counted_list_open_board_issues)

    status = issue_claim.main(_claim_argv(*requested_scope_flags, "--json"))

    assert len(board_reads) == 1
    assert client.issue_reference_lookups == []
    assert status == expected_status
    if expected_status == 0:
        payload = json.loads(capsys.readouterr().out)
        assert payload["scope"] == expected_scope_or_error
        if expected_cost is not None:
            n, total, share = expected_cost
            assert payload["versioned_files"] == n
            assert payload["versioned_files_total"] == total
            assert payload["share"] == share
        return
    assert capsys.readouterr().err == f"ERROR: {expected_scope_or_error}\n"
    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


def test_cli_claim_without_scope_names_a_malformed_body_before_no_scope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #310 finding 43: a target whose `agent-claim` block is
    malformed refuses by naming that defect -- reusing the same block-
    defect reader `body --check` uses -- before scope derivation ever gets
    to name the less specific "item names no scope"."""
    client = _arranged_claim_client(monkeypatch)
    client.board_issues = (
        board_issue(72, "Work", agent_claim_body(f'{MINIMAL_BLOCK_TOML}owner = "someone"\n')),
    )

    status = issue_claim.main(_claim_argv("--json"))

    assert status == 2
    captured = capsys.readouterr()
    assert captured.err == "ERROR: #72 body malformed: owner: unknown top-level key owner\n"
    _assert_json_refusal_object(captured.err, captured.out, reason="body_invalid")


def test_cli_claim_without_scope_refuses_a_missing_target_by_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A target unreachable through the open-board listing (issue #406):
    scope derivation itself refuses `target_invalid`, before any slice-rule
    check ever runs against it."""
    client = _arranged_claim_client(monkeypatch)
    client.issue_references[72] = forge.ItemReference(forge.ItemState.MISSING, "", "")

    status = issue_claim.main(_claim_argv("--json"))

    assert status == 2
    captured = capsys.readouterr()
    assert captured.err == "ERROR: #72 does not exist\n"
    _assert_json_refusal_object(captured.err, captured.out, reason="target_invalid")


def test_cli_claim_without_scope_reports_unavailable_for_a_forge_outage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A forge outage while deriving scope (issue #406, CLM-27) is a
    different failure from a missing or pull-request target: it never
    appears in the open-board listing `client.item_reference` itself
    raises `forge.ForgeTransientError` reading it, so `claim --json`
    reports `unavailable`, not `target_invalid`."""
    client = _arranged_claim_client(monkeypatch)

    def unreachable(number: int) -> forge.ItemReference:
        raise forge.ForgeTransientError("gh: connection reset")

    monkeypatch.setattr(client, "item_reference", unreachable)

    status = issue_claim.main(_claim_argv("--json"))

    assert status == 2
    captured = capsys.readouterr()
    _assert_json_refusal_object(captured.err, captured.out, reason="unavailable")


def test_cli_claim_derives_whole_from_the_items_own_body_when_wide(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #399 proof 1: `--whole` omitted, a target naming its own body
    `whole` justifies a wide derived scope exactly as `--whole REASON`
    would -- the item's own sentence lands on the claim itself."""
    reason = "the four adapters share one lock"
    client = _arranged_claim_client(monkeypatch)
    client.board_issues = (
        board_issue(
            72,
            "Work",
            complete_contract("Ship it.", scope=["a.py", "b.py", "c.py", "d.py"], whole=reason),
        ),
    )

    status = issue_claim.main(_claim_argv())

    assert status == 0
    assert "CLAIMED issue #72" in capsys.readouterr().out
    assert _live_store_claim().whole_reason == reason


def test_cli_claim_names_both_remedies_when_the_body_has_no_whole(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #399 proof 1: neither `--whole` nor the item's own body `whole`
    present, the width gate's refusal names both remedies."""
    client = _arranged_claim_client(monkeypatch)
    client.board_issues = (
        board_issue(
            72, "Work", complete_contract("Ship it.", scope=["a.py", "b.py", "c.py", "d.py"])
        ),
    )

    status = issue_claim.main(_claim_argv())

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: scope is wide: 4 paths exceeds three; "
        "pass --whole REASON or set whole in the body\n"
    )


def test_cli_claim_explicit_whole_overrides_the_items_own_body(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #399 proof 1: an explicit `--whole` always wins over the
    item's own body `whole`."""
    body_reason = "the four adapters share one lock"
    cli_reason = "urgent, splitting later"
    client = _arranged_claim_client(monkeypatch)
    client.board_issues = (
        board_issue(
            72,
            "Work",
            complete_contract(
                "Ship it.", scope=["a.py", "b.py", "c.py", "d.py"], whole=body_reason
            ),
        ),
    )

    status = issue_claim.main(_claim_argv("--whole", cli_reason))

    assert status == 0
    assert _live_store_claim().whole_reason == cli_reason


def test_cli_claim_replay_without_scope_takes_the_live_claims_own_stored_scope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #337 proof 3 (REVISE finding 2): a live claim already on this
    identity is a replayed, interrupted request even when the retry omits
    `--scope` -- its own stored scope is taken outright, with no forge call
    and no body read at all, so a retry never refuses merely because
    `--scope` was dropped, or the body changed, since the original claim was
    opened. The item's own current body now names a different scope
    entirely (`src/other.py`, not the claim's stored `src`); the stored
    scope still wins, proving the body is never consulted for a replay."""
    existing = request("live-claim", "Ada", issue=72, branch="codex/issue-72", scope=("src",))
    client = FakeForge(
        board_issues=(
            board_issue(72, "Work", complete_contract("Ship it.", scope=["src/other.py"])),
        )
    )
    _patch_status_cli(monkeypatch, client)
    _patch_store_write(monkeypatch, _store_claim_from_request(existing))
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())

    claimed = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Ada",
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--claim-id",
            "live-claim",
            "--json",
        ]
    )

    assert claimed == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["scope"] == ["src"]
    assert client.issue_reference_lookups == []
    assert client.requests == 0


def test_cli_lane_claim_without_scope_refuses_by_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #337 proof 3: lane mode has no item to derive a scope from, so
    `required=True`'s removal from `--scope` never reaches it -- omitting it
    still refuses, by name, and forge-free like every other lane claim."""
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Ada"})
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    git_values = {("branch", "--show-current"): "docs/lane-cleanup"}
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    _forbid_forge_resolution(monkeypatch)

    status = issue_claim.main(["claim", "--base", BASE, "--branch", "docs/lane-cleanup"])

    assert status == 2
    assert capsys.readouterr().err == f"ERROR: {issue_claim.LANE_CLAIM_SCOPE_REQUIRED}\n"


def test_cli_claim_replay_reports_the_matching_live_claim_after_an_interrupted_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    existing = request("live-claim", "Ada", issue=72, branch="codex/issue-72", scope=("src",))
    client = FakeForge()
    _patch_status_cli(monkeypatch, client)
    _patch_store_write(monkeypatch, _store_claim_from_request(existing))
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    arguments = [
        "--repo",
        REPOSITORY,
        "claim",
        "72",
        "--agent",
        "Ada",
        "--role",
        "builder",
        "--base",
        BASE,
        "--branch",
        "codex/issue-72",
        "--scope",
        "src",
        "--claim-id",
        "live-claim",
    ]

    assert issue_claim.main(arguments) == 0
    assert capsys.readouterr().out == (
        "CLAIMED issue #72: live-claim\n"
        "1 of 4 versioned files (25%); overlaps no other open claims\n"
    )

    assert issue_claim.main([*arguments, "--json"]) == 0
    replay = json.loads(capsys.readouterr().out)
    assert replay["claim_id"] == "live-claim"
    assert replay["issue"] == 72
    assert replay["agent"] == "Ada"
    assert replay["role"] == "builder"
    assert replay["branch"] == "codex/issue-72"
    assert replay["scope"] == ["src"]
    assert store.fetch_state(worktree=Path("."), remote="origin").claims


@pytest.mark.parametrize(
    ("agent", "role", "branch", "scope"),
    [
        ("Grok 4.6", "builder", "codex/issue-72", ("src",)),
        ("Ada", "reviewer", "codex/issue-72", ("src",)),
        ("Ada", "builder", "codex/issue-72-retry", ("src",)),
        ("Ada", "builder", "codex/issue-72", ("src", "tests")),
    ],
    ids=["agent", "role", "branch", "scope"],
)
def test_cli_claim_replay_refuses_a_live_claim_with_different_retry_fields(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    agent: str,
    role: str,
    branch: str,
    scope: tuple[str, ...],
) -> None:
    existing = request("live-claim", "Ada", issue=72, branch="codex/issue-72", scope=("src",))
    client = FakeForge()
    _patch_status_cli(monkeypatch, client)
    _patch_store_write(monkeypatch, _store_claim_from_request(existing))
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())

    assert (
        issue_claim.main(
            [
                "--repo",
                REPOSITORY,
                "claim",
                "72",
                "--agent",
                agent,
                "--role",
                role,
                "--base",
                BASE,
                "--branch",
                branch,
                *(part for path in scope for part in ("--scope", path)),
            ]
        )
        == 2
    )

    assert "ERROR: issue #72 is claimed by Ada (builder)" in capsys.readouterr().err
    assert store.fetch_state(worktree=Path("."), remote="origin").claims


def test_cli_claim_replay_skips_out_of_order_for_the_matching_lower_priority_item(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    existing = request("live-claim", "Ada", issue=10, branch="codex/issue-10", scope=("src",))
    client = FakeForge()
    client.board_issues = (
        board_issue(10, "Lower work", complete_contract("Claim #10.")),
        board_issue(11, "Top work", complete_contract("Claim #11.")),
        board_issue(12, "Depends on top", complete_contract("Claim #12."), blocked_by_count=1),
    )
    client.board_dependencies = {12: (block_dependency(11),)}
    _patch_status_cli(monkeypatch, client)
    _patch_store_write(monkeypatch, _store_claim_from_request(existing))
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())

    assert (
        issue_claim.main(
            [
                "--repo",
                REPOSITORY,
                "claim",
                "10",
                "--agent",
                "Ada",
                "--role",
                "builder",
                "--base",
                BASE,
                "--branch",
                "codex/issue-10",
                "--scope",
                "src",
                "--claim-id",
                "live-claim",
            ]
        )
        == 0
    )

    assert "out-of-order" not in capsys.readouterr().out
    assert store.fetch_state(worktree=Path("."), remote="origin").claims


def test_cli_claim_replay_does_not_bypass_out_of_order_for_another_agent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    existing = request("live-claim", "Ada", issue=10, branch="codex/issue-10", scope=("src",))
    client = FakeForge()
    client.board_issues = (
        board_issue(10, "Lower work", complete_contract("Claim #10.")),
        board_issue(11, "Top work", complete_contract("Claim #11.", scope=["src/top.py"])),
        board_issue(12, "Depends on top", complete_contract("Claim #12."), blocked_by_count=1),
    )
    client.board_dependencies = {12: (block_dependency(11),)}
    _patch_status_cli(monkeypatch, client)
    _patch_store_write(monkeypatch, _store_claim_from_request(existing))
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())

    assert (
        issue_claim.main(
            [
                "--repo",
                REPOSITORY,
                "claim",
                "10",
                "--agent",
                "Grok 4.6",
                "--role",
                "builder",
                "--base",
                BASE,
                "--branch",
                "codex/issue-10",
                "--scope",
                "src",
            ]
        )
        == 2
    )

    assert "ERROR: higher-priority actionable item #11" in capsys.readouterr().err
    assert store.fetch_state(worktree=Path("."), remote="origin").claims


def test_cli_claim_replay_does_not_resurrect_a_released_claim(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    _patch_store_write(monkeypatch, consumed_ids=frozenset({protocol.ClaimId("released-claim")}))

    assert (
        issue_claim.main(
            [
                "--repo",
                REPOSITORY,
                "claim",
                "72",
                "--agent",
                "Ada",
                "--role",
                "builder",
                "--base",
                BASE,
                "--branch",
                "codex/issue-72",
                "--scope",
                "src",
                "--claim-id",
                "released-claim",
            ]
        )
        == 2
    )

    assert "already on this ledger, active or released" in capsys.readouterr().err


def test_cli_claim_scope_keeps_a_comma_inside_one_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A repository-relative path may itself contain a comma
    (`docs/report,v2.md`); the removed comma-splitting used to turn that
    silently into two wrong paths. One --scope occurrence is now exactly one
    path, comma and all -- proved here end to end through the real store, and
    by the absence of an overlap with the substring before the comma, which
    the old splitting would have claimed as its own path."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    monkeypatch.setattr(checkout, "versioned_paths", lambda **_kwargs: ("docs/report,v2.md",))

    claimed = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Ada",
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs/report,v2.md",
            "--claim-id",
            "comma-path",
        ]
    )

    assert claimed == 0
    assert _live_store_claim().scope == ("docs/report,v2.md",)

    second = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "73",
            "--agent",
            "Grace",
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            "codex/issue-73",
            "--scope",
            "docs/report",
            "--claim-id",
            "half-path",
        ]
    )
    captured = capsys.readouterr()

    assert second == 0
    assert "CLAIMED issue #73: half-path" in captured.out
    assert "overlaps no other open claims" in captured.out
    assert len(store.fetch_state(worktree=Path("."), remote="origin").claims) == 2


def test_cli_claim_scope_comma_differs_from_repeated_scope_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A comma inside one --scope value is no longer equivalent to repeating
    the flag: the joined form is a single path whose name contains a comma,
    the repeated form two distinct paths."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    monkeypatch.setattr(
        checkout, "versioned_paths", lambda **_kwargs: ("docs/PRODUCT.md,src/widget.py",)
    )

    joined = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs/PRODUCT.md,src/widget.py",
            "--claim-id",
            "joined",
        ]
    )
    repeated_client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: repeated_client)
    repeated = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "73",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-73",
            "--scope",
            "docs/PRODUCT.md",
            "--scope",
            "src/widget.py",
            "--claim-id",
            "repeated",
        ]
    )

    assert (joined, repeated) == (0, 0)
    claims = store.fetch_state(worktree=Path("."), remote="origin").claims
    assert {claim.scope for claim in claims.values()} == {
        ("docs/PRODUCT.md,src/widget.py",),
        ("docs/PRODUCT.md", "src/widget.py"),
    }


def test_cli_claim_refuses_a_comma_scope_that_matches_nothing_in_the_checkout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--scope a.py,b.py` passed from the comma-splitting habit that #201
    removed used to store one path that guards nothing: no such file exists,
    so the lane's real files stayed unclaimed and no overlap check could ever
    fire for them (issue #207). The claim is refused before it is written,
    naming the one-path-per-flag rule."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())

    status = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "a.py,b.py",
            "--claim-id",
            "habit-comma",
        ]
    )

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: 'a.py,b.py' matches no versioned file; one --scope path per flag, so its "
        "comma is read literally -- repeat --scope for a second path\n"
    )


def test_cli_claim_accepts_a_scope_path_without_a_comma_that_does_not_exist_yet(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A lane routinely claims files it is about to create; only a comma
    with no match in the checkout trips the new refusal (issue #207), so a
    comma-free path git has never heard of still claims cleanly."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())

    status = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src/not-created-yet.py",
            "--claim-id",
            "future-file",
        ]
    )

    assert status == 0
    assert capsys.readouterr().err == ""
    assert _live_store_claim().scope == ("src/not-created-yet.py",)


def test_cli_rescope_adds_a_path_without_matching_head_or_a_clean_tree(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    claimed_request = request(issue=72, branch="codex/issue-72", scope=("src/widget.py",))
    acquired = _store_claim_from_request(claimed_request)
    _patch_store_write(monkeypatch, acquired)
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    git_values = _git_checkout(head="b" * 40, dirty=" M file")
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "rescope",
            "72",
            "--add",
            "/repo/src/new.py",
        ]
    )

    assert status == 0
    assert capsys.readouterr().out == f"RESCOPED issue #72: {acquired.claim_id}\n"
    standing = _live_store_claim()
    assert standing.claim_id == acquired.claim_id
    assert standing.base == BASE
    assert standing.scope == ("src/new.py", "src/widget.py")


def test_cli_rescope_add_keeps_a_comma_inside_one_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--add` shares `--scope`'s rule: one occurrence is one path, comma and
    all, never split into two."""
    client = FakeForge()
    claimed_request = request(issue=72, branch="codex/issue-72", scope=("src/widget.py",))
    acquired = _store_claim_from_request(claimed_request)
    _patch_store_write(monkeypatch, acquired)
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    git_values = _git_checkout(head="b" * 40, dirty=" M file")
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    monkeypatch.setattr(checkout, "versioned_paths", lambda **_kwargs: ("reports/a,b.md",))
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "rescope",
            "72",
            "--add",
            "/repo/reports/a,b.md",
        ]
    )

    assert status == 0
    assert capsys.readouterr().out == f"RESCOPED issue #72: {acquired.claim_id}\n"
    assert _live_store_claim().scope == ("reports/a,b.md", "src/widget.py")


def test_cli_rescope_drop_matches_a_comma_path_as_one_whole_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--drop` shares the same rule: dropping `reports/a,b.md` removes that
    one path and leaves an unrelated `reports/a` scope entry untouched --
    the old comma-splitting would instead have tried, and failed, to drop
    `reports/a` and `b.md` as two separate paths."""
    client = FakeForge()
    claimed_request = request(
        issue=72, branch="codex/issue-72", scope=("reports/a", "reports/a,b.md")
    )
    acquired = _store_claim_from_request(claimed_request)
    _patch_store_write(monkeypatch, acquired)
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    git_values = _git_checkout(head="b" * 40, dirty=" M file")
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    monkeypatch.setattr(checkout, "versioned_paths", lambda **_kwargs: ("reports/a,b.md",))
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "rescope",
            "72",
            "--drop",
            "/repo/reports/a,b.md",
        ]
    )

    assert status == 0
    assert capsys.readouterr().out == f"RESCOPED issue #72: {acquired.claim_id}\n"
    assert _live_store_claim().scope == ("reports/a",)


def test_cli_rescope_add_refuses_a_comma_scope_that_matches_nothing_in_the_checkout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--add` must not be a way around the same refusal `claim` applies
    (issue #207): a comma-habit value that names no real path is refused
    before the rescope is written, leaving the live claim untouched."""
    client = FakeForge()
    claimed_request = request(issue=72, branch="codex/issue-72", scope=("src/widget.py",))
    acquired = _store_claim_from_request(claimed_request)
    _patch_store_write(monkeypatch, acquired)
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    git_values = _git_checkout(head="b" * 40, dirty=" M file")
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "rescope",
            "72",
            "--add",
            "/repo/a.py,b.py",
        ]
    )

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: 'a.py,b.py' matches no versioned file; one --add path per flag, so its comma "
        "is read literally -- repeat --add for a second path\n"
    )
    assert _live_store_claim().scope == ("src/widget.py",)


def test_cli_rescope_drop_of_a_value_not_in_scope_refuses_with_the_claims_own_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--drop` of a comma-bearing value the live claim never held is
    refused by `_combined_scope`'s own 'not in this claim's scope' sentence
    (issue #207): the ungrounded-comma refusal never runs over `--drop` at
    all, because that existing refusal already covers every value not
    currently held, with a truer reason naming the claim rather than the
    checkout."""
    client = FakeForge()
    claimed_request = request(issue=72, branch="codex/issue-72", scope=("src/widget.py",))
    acquired = _store_claim_from_request(claimed_request)
    _patch_store_write(monkeypatch, acquired)
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    git_values = _git_checkout(head="b" * 40, dirty=" M file")
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "rescope",
            "72",
            "--drop",
            "/repo/a.py,b.py",
        ]
    )

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: cannot drop 'a.py,b.py'; it is not in this claim's scope\n"
    )
    assert _live_store_claim().scope == ("src/widget.py",)


def test_cli_rescope_drop_removes_a_comma_entry_the_claim_holds_though_no_file_matches_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The repair this item exists to allow: a claim already holding the
    comma-habit value `a.py,b.py` (as if claimed before issue #207's fix
    landed) drops it and adds the two real paths in one rescope. A value the
    live claim already holds is a fact about the claim, not a typo about the
    checkout, so the ungrounded-comma refusal must never block dropping it --
    even though no versioned file matches `a.py,b.py` itself."""
    client = FakeForge()
    claimed_request = request(issue=72, branch="codex/issue-72", scope=("a.py,b.py",))
    acquired = _store_claim_from_request(claimed_request)
    _patch_store_write(monkeypatch, acquired)
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    git_values = _git_checkout(head="b" * 40, dirty=" M file")
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    monkeypatch.setattr(checkout, "versioned_paths", lambda **_kwargs: ("a.py", "b.py"))
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "rescope",
            "72",
            "--drop",
            "/repo/a.py,b.py",
            "--add",
            "/repo/a.py",
            "--add",
            "/repo/b.py",
        ]
    )

    assert status == 0
    assert capsys.readouterr().out == f"RESCOPED issue #72: {acquired.claim_id}\n"
    assert _live_store_claim().scope == ("a.py", "b.py")


def test_cli_rescope_json_prints_updated_scope_and_same_claim_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    standing = request(
        "cli-claim", "Ada", issue=72, branch="codex/issue-72", scope=("src/widget.py",)
    )
    _patch_store_write(monkeypatch, _store_claim_from_request(standing))
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    git_values = _git_checkout()
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Ada"})

    status = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "rescope",
            "72",
            "--add",
            "/repo/docs/PRODUCT.md",
            "--add",
            "/repo/src/new.py",
            "--drop",
            "/repo/src/widget.py",
            "--json",
        ]
    )

    assert status == 0
    assert (
        capsys.readouterr().out
        == json.dumps(
            {
                "ok": True,
                "reason": "rescoped",
                "issue": 72,
                "lane": None,
                "claim_id": "cli-claim",
                "agent": "Ada",
                "role": "builder",
                "base": BASE,
                "branch": "codex/issue-72",
                "scope": ["docs/PRODUCT.md", "src/new.py"],
            }
        )
        + "\n"
    )


def test_cli_rescope_refuses_a_different_agent_than_the_claimant(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    claimed_request = request(agent="Ada", issue=72, branch="codex/issue-72", scope=("src",))
    _patch_store_write(monkeypatch, _store_claim_from_request(claimed_request))
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    git_values = _git_checkout()
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Grok 4.6"})

    status = issue_claim.main(["--repo", REPOSITORY, "rescope", "72", "--add", "/repo/src/new.py"])

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: only the original claimant may rescope "
        "(holder='Ada (builder)', this session='Grok 4.6 (builder)')\n"
    )


def test_cli_rescope_json_refuses_no_active_claim_with_precondition_failed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """RESC-14 (issue #406): no live claim on the target identity/branch at
    all reports `precondition_failed`, not the generic `unavailable`
    bucket every checkout- or store-level refusal falls to."""
    client = FakeForge()
    _patch_store_write(monkeypatch)
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    git_values = _git_checkout()
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Ada"})

    status = issue_claim.main(
        ["--repo", REPOSITORY, "rescope", "72", "--add", "/repo/src/new.py", "--json"]
    )

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err == "ERROR: issue #72 has no active build claim\n"
    _assert_json_refusal_object(captured.err, captured.out, reason="precondition_failed")


def test_cli_rescope_without_add_or_drop_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    _patch_store_write(
        monkeypatch,
        _store_claim_from_request(
            request(issue=72, branch="codex/issue-72", scope=("src/widget.py",))
        ),
    )
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    git_values = _git_checkout()
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(["--repo", REPOSITORY, "rescope", "72"])
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "ERROR:" in captured.err
    assert "does not change the claim scope" in captured.err or "--add" in captured.err


def test_cli_rescope_refuses_primary_checkout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    _patch_store_write(
        monkeypatch,
        _store_claim_from_request(
            request(issue=72, branch="codex/issue-72", scope=("src/widget.py",))
        ),
    )
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    git_values = _git_checkout(git_directory="/repo/.git", common_directory="/repo/.git")
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(["--repo", REPOSITORY, "rescope", "72", "--add", "/repo/src/new.py"])
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "linked isolated worktree" in captured.err


@dataclass(frozen=True)
class _ScopeWidthRefusal:
    """One `claim`/`rescope` case that must refuse for a wide scope, sharing
    the monkeypatch quadruple and argv skeleton with every other row and
    differing only in what trips the width check and what the refusal says."""

    id: str
    command: str
    argv_tail: tuple[str, ...]
    directories: frozenset[str] = frozenset()
    versioned: tuple[str, ...] | None = None
    board_issues: tuple[board.Issue, ...] = ()
    standing_scope: tuple[str, ...] | None = None
    exact_err: str | None = None


_SCOPE_WIDTH_REFUSALS = (
    _ScopeWidthRefusal(
        id="directory-scope",
        command="claim",
        argv_tail=("--scope", "docs", "--claim-id", "tree"),
        directories=frozenset({"docs"}),
    ),
    _ScopeWidthRefusal(
        id="named-directory",
        command="claim",
        argv_tail=("--scope", "docs", "--claim-id", "named-directory"),
        directories=frozenset({"docs"}),
        exact_err=(
            "ERROR: scope is wide: 1 directory in scope (docs); "
            "pass --whole REASON or set whole in the body\n"
        ),
    ),
    _ScopeWidthRefusal(
        id="directory-plus-child-scope",
        command="claim",
        argv_tail=("--scope", "docs", "--scope", "docs/a.md", "--claim-id", "tree"),
        directories=frozenset({"docs"}),
    ),
    _ScopeWidthRefusal(
        id="rescope-add-directory",
        command="rescope",
        argv_tail=("--add", "/repo/docs"),
        directories=frozenset({"docs"}),
        standing_scope=("src/widget.py",),
    ),
    _ScopeWidthRefusal(
        id="share-above-quarter",
        command="claim",
        argv_tail=(
            "--scope",
            "LICENSE",
            "--scope",
            "README.md",
            "--scope",
            "src",
            "--claim-id",
            "wide",
        ),
        versioned=TWELVE_VERSIONED_FILES,
    ),
    _ScopeWidthRefusal(
        id="named-share",
        command="claim",
        argv_tail=(
            "--scope",
            "LICENSE",
            "--scope",
            "README.md",
            "--scope",
            "src",
            "--claim-id",
            "named-share",
        ),
        versioned=TWELVE_VERSIONED_FILES,
        exact_err=(
            "ERROR: scope is wide: 4 paths of 12 versioned files (33 %) exceeds a quarter; "
            "pass --whole REASON or set whole in the body\n"
        ),
    ),
    _ScopeWidthRefusal(
        id="cut-directory-scope",
        command="claim",
        argv_tail=("--scope", "docs", "--claim-id", "cut"),
        directories=frozenset({"docs"}),
        board_issues=(
            board_issue(
                72,
                "Cut work",
                complete_contract("Claim #72.") + "\n\n## Schnitt\n\n**Scheibe 1: Title**\n",
            ),
        ),
    ),
    _ScopeWidthRefusal(
        id="schnitt-heading-without-scheibe",
        command="claim",
        argv_tail=("--scope", "docs", "--claim-id", "heading"),
        directories=frozenset({"docs"}),
        board_issues=(
            board_issue(
                72,
                "Uncut",
                complete_contract("Claim #72.") + "\n\n## Schnitt\n\nNo slices yet.\n",
            ),
        ),
    ),
    _ScopeWidthRefusal(
        id="lane-directory",
        command="claim-lane",
        argv_tail=("--scope", "docs", "--claim-id", "lane-docs"),
        directories=frozenset({"docs"}),
    ),
    _ScopeWidthRefusal(
        id="cut-directory-high-share",
        command="claim",
        argv_tail=("--scope", "docs", "--claim-id", "wide-cut"),
        directories=frozenset({"docs"}),
        versioned=("LICENSE", "README.md", "docs/a.md", "docs/b.md"),
        board_issues=(
            board_issue(
                72,
                "Cut work",
                complete_contract("Claim #72.") + "\n\n## Schnitt\n\n**Scheibe 1: Title**\n",
            ),
        ),
    ),
    _ScopeWidthRefusal(
        id="rescope-add-combined-share",
        command="rescope",
        argv_tail=("--add", "/repo/LICENSE", "--add", "/repo/README.md"),
        versioned=TWELVE_VERSIONED_FILES,
        standing_scope=("src",),
    ),
    _ScopeWidthRefusal(
        id="claim-refuses-four-named-paths",
        command="claim",
        argv_tail=(
            "--scope",
            "new_a.py",
            "--scope",
            "new_b.py",
            "--scope",
            "new_c.py",
            "--scope",
            "new_d.py",
            "--claim-id",
            "four",
        ),
    ),
    _ScopeWidthRefusal(
        id="named-path-count",
        command="claim",
        argv_tail=(
            "--scope",
            "new_a.py",
            "--scope",
            "new_b.py",
            "--scope",
            "new_c.py",
            "--scope",
            "new_d.py",
            "--claim-id",
            "named-path-count",
        ),
        exact_err=(
            "ERROR: scope is wide: 4 paths exceeds three; "
            "pass --whole REASON or set whole in the body\n"
        ),
    ),
    _ScopeWidthRefusal(
        id="rescope-widening-to-four-paths",
        command="rescope",
        argv_tail=("--add", "/repo/new_b.py", "--add", "/repo/new_c.py", "--add", "/repo/new_d.py"),
        standing_scope=("new_a.py",),
    ),
)


@dataclass(frozen=True)
class _ScopeWidthAcceptance:
    """One `claim`/`rescope` case that must accept a scope within the width
    limits, sharing the same arrangement as `_ScopeWidthRefusal` and
    differing only in the trigger and in what the acceptance reports."""

    id: str
    command: str
    argv_tail: tuple[str, ...]
    check: Callable[[str, str], None]
    directories: frozenset[str] = frozenset()
    versioned: tuple[str, ...] | None = None
    standing_scope: tuple[str, ...] | None = None


def _assert_below_share_floor_human_line(out: str, err: str) -> None:
    assert out.endswith("4 of 11 versioned files (36%); overlaps no other open claims\n")


def _assert_share_at_quarter_human_line(out: str, err: str) -> None:
    assert out.endswith("3 of 12 versioned files (25%); overlaps no other open claims\n")


def _assert_share_above_a_quarter_with_whole_payload(out: str, err: str) -> None:
    payload = json.loads(out)
    assert payload["versioned_files"] == 4
    assert payload["versioned_files_total"] == 12
    assert payload["share"] == pytest.approx(1 / 3)
    assert payload["touches"] == []


def _assert_rescope_persisted_whole_reason(out: str, err: str) -> None:
    standing = _live_store_claim()
    assert standing.scope == ("docs", "src/widget.py")
    assert standing.whole_reason == "widen to the docs tree"


def _assert_claim_accepted_three_named_paths(out: str, err: str) -> None:
    posted = _live_store_claim()
    assert posted.scope == ("new_a.py", "new_b.py", "new_c.py")
    assert posted.whole_reason is None


def _assert_claim_persisted_whole_reason(out: str, err: str) -> None:
    posted = _live_store_claim()
    assert posted.whole_reason == "the four adapters share one lock"


def _assert_claim_allowed_directory_with_whole(out: str, err: str) -> None:
    posted = _live_store_claim()
    assert posted.scope == ("docs",)
    assert posted.whole_reason == "rewrite the docs tree"


_SCOPE_WIDTH_ACCEPTANCES = (
    _ScopeWidthAcceptance(
        id="below-share-floor",
        command="claim",
        argv_tail=(
            "--scope",
            "LICENSE",
            "--scope",
            "README.md",
            "--scope",
            "src",
            "--claim-id",
            "below-floor",
        ),
        versioned=TWELVE_VERSIONED_FILES[:-1],
        check=_assert_below_share_floor_human_line,
    ),
    _ScopeWidthAcceptance(
        id="share-above-quarter-with-whole",
        command="claim",
        argv_tail=(
            "--scope",
            "LICENSE",
            "--scope",
            "README.md",
            "--scope",
            "src",
            "--whole",
            "cover four files",
            "--claim-id",
            "wide",
            "--json",
        ),
        versioned=TWELVE_VERSIONED_FILES,
        check=_assert_share_above_a_quarter_with_whole_payload,
    ),
    _ScopeWidthAcceptance(
        id="share-at-quarter",
        command="claim",
        argv_tail=(
            "--scope",
            "LICENSE",
            "--scope",
            "README.md",
            "--scope",
            "pyproject.toml",
            "--claim-id",
            "quarter",
        ),
        versioned=TWELVE_VERSIONED_FILES,
        check=_assert_share_at_quarter_human_line,
    ),
    _ScopeWidthAcceptance(
        id="rescope-persists-whole-reason",
        command="rescope",
        argv_tail=("--add", "/repo/docs", "--whole", "widen to the docs tree"),
        directories=frozenset({"docs"}),
        standing_scope=("src/widget.py",),
        check=_assert_rescope_persisted_whole_reason,
    ),
    _ScopeWidthAcceptance(
        id="claim-accepts-three-named-paths",
        command="claim",
        argv_tail=(
            "--scope",
            "new_a.py",
            "--scope",
            "new_b.py",
            "--scope",
            "new_c.py",
            "--claim-id",
            "three",
        ),
        check=_assert_claim_accepted_three_named_paths,
    ),
    _ScopeWidthAcceptance(
        id="claim-persists-whole-reason",
        command="claim",
        argv_tail=(
            "--scope",
            "new_a.py",
            "--scope",
            "new_b.py",
            "--scope",
            "new_c.py",
            "--scope",
            "new_d.py",
            "--whole",
            "the four adapters share one lock",
            "--claim-id",
            "wide",
        ),
        check=_assert_claim_persisted_whole_reason,
    ),
    _ScopeWidthAcceptance(
        id="claim-allows-directory-with-whole",
        command="claim",
        argv_tail=("--scope", "docs", "--whole", "rewrite the docs tree", "--claim-id", "tree"),
        directories=frozenset({"docs"}),
        check=_assert_claim_allowed_directory_with_whole,
    ),
)


def _run_scope_width_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    *,
    command: str,
    argv_tail: tuple[str, ...],
    directories: frozenset[str] = frozenset(),
    versioned: tuple[str, ...] | None = None,
    standing_scope: tuple[str, ...] | None = None,
    board_issues: tuple[board.Issue, ...] = (),
) -> tuple[int, str, str]:
    """Run one `claim`/`rescope` scope-width case end to end and return its
    exit status, stdout, and stderr, for the refusal and acceptance tables
    that share every arrangement and differ only in trigger and outcome."""
    client = FakeForge(board_issues=board_issues)
    if command == "rescope":
        assert standing_scope is not None
        _patch_store_write(
            monkeypatch,
            _store_claim_from_request(
                request(issue=72, branch="codex/issue-72", scope=standing_scope)
            ),
        )
        git_values = _git_checkout()
        monkeypatch.setattr(
            checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
        )
        _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})
        arrange_scope_width(
            monkeypatch,
            client,
            directories=directories,
            versioned=versioned,
            validate_checkout=False,
        )
        argv = ["--repo", REPOSITORY, "rescope", "72", *argv_tail]
    elif command == "claim-lane":
        _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Ada"})
        git_values = {("branch", "--show-current"): "docs/lane-cleanup"}
        monkeypatch.setattr(
            checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
        )
        arrange_scope_width(monkeypatch, client, directories=directories, versioned=versioned)
        argv = [
            "--repo",
            REPOSITORY,
            "claim",
            "--base",
            BASE,
            "--branch",
            "docs/lane-cleanup",
            *argv_tail,
        ]
    else:
        arrange_scope_width(monkeypatch, client, directories=directories, versioned=versioned)
        argv = [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            *argv_tail,
        ]

    status = issue_claim.main(argv)
    captured = capsys.readouterr()
    return status, captured.out, captured.err


@pytest.mark.parametrize(
    "case", _SCOPE_WIDTH_REFUSALS, ids=[case.id for case in _SCOPE_WIDTH_REFUSALS]
)
def test_cli_claim_and_rescope_refuse_a_wide_scope_without_whole(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: _ScopeWidthRefusal,
) -> None:
    status, out, err = _run_scope_width_command(
        monkeypatch,
        capsys,
        command=case.command,
        argv_tail=case.argv_tail,
        directories=case.directories,
        versioned=case.versioned,
        standing_scope=case.standing_scope,
        board_issues=case.board_issues,
    )

    assert status == 2
    assert out == ""
    if case.exact_err is not None:
        assert err == case.exact_err
    else:
        assert "scope is wide" in err
        assert "--whole" in err
    if case.command == "rescope":
        assert _live_store_claim().scope == case.standing_scope


@pytest.mark.parametrize(
    "case", _SCOPE_WIDTH_ACCEPTANCES, ids=[case.id for case in _SCOPE_WIDTH_ACCEPTANCES]
)
def test_cli_claim_and_rescope_accept_a_scope_within_width_limits(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    case: _ScopeWidthAcceptance,
) -> None:
    status, out, err = _run_scope_width_command(
        monkeypatch,
        capsys,
        command=case.command,
        argv_tail=case.argv_tail,
        directories=case.directories,
        versioned=case.versioned,
        standing_scope=case.standing_scope,
    )

    assert status == 0
    case.check(out, err)


def test_cli_status_path_prints_the_claim_holding_a_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    claimed_claim = _active_claim(
        "Ada", claim_id="mine", issue=72, scope=("docs/PRODUCT.md", "src/widget.py")
    )
    _patch_status_store(monkeypatch, claimed_claim)

    claimed = issue_claim.main(["--repo", REPOSITORY, "status", "--path", "docs/PRODUCT.md"])
    free = issue_claim.main(["--repo", REPOSITORY, "status", "--path", "README.md"])
    claimed_out = capsys.readouterr().out

    assert claimed == 0
    assert free == 0
    assert "CLAIMED docs/PRODUCT.md issue #72: Ada (builder) claim=mine" in claimed_out
    assert "UNCLAIMED README.md" in claimed_out


def test_cli_status_path_json_prints_holder_or_unclaimed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    claimed_claim = _active_claim("Ada", claim_id="mine", issue=72, scope=("docs",))
    _patch_status_store(monkeypatch, claimed_claim)

    descendant = issue_claim.main(
        ["--repo", REPOSITORY, "status", "--path", "docs/decisions/one.md", "--json"]
    )
    claimed = json.loads(capsys.readouterr().out)
    free = issue_claim.main(["--repo", REPOSITORY, "status", "--path", "src/widget.py", "--json"])
    unclaimed = json.loads(capsys.readouterr().out)

    assert descendant == 0
    assert claimed["ok"] is True
    assert claimed["reason"] == "claimed"
    assert claimed["path"] == "docs/decisions/one.md"
    assert claimed["claims"][0]["claim_id"] == "mine"
    assert free == 0
    assert unclaimed == {
        "ok": True,
        "reason": "unclaimed",
        "path": "src/widget.py",
        "claims": [],
    }


def test_cli_status_path_answers_even_when_a_claim_age_read_would_raise(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--path` prints no age, so it never reads claim ancestry (README
    "status --path"): a lineage break in some claim's `opened_commit` must
    not stop this answer.
    """
    claimed_claim = _active_claim("Ada", claim_id="mine", issue=72, scope=("docs/PRODUCT.md",))
    _patch_status_store(monkeypatch, claimed_claim)

    def raising_claim_ages(
        *, worktree: Path, tip: protocol.ObjectId, claims: object
    ) -> dict[str, datetime]:
        raise protocol.StateLineageError("must not be called by status --path")

    monkeypatch.setattr(store, "claim_ages", raising_claim_ages)

    status = issue_claim.main(["--repo", REPOSITORY, "status", "--path", "docs/PRODUCT.md"])

    assert status == 0
    assert "CLAIMED docs/PRODUCT.md issue #72: Ada (builder) claim=mine" in capsys.readouterr().out


def test_cli_claim_touches_stay_empty_beside_a_disjoint_standing_claim(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    standing = request("claim-a", "Ada", issue=73, scope=("LICENSE",))
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    _patch_store_write(monkeypatch, _store_claim_from_request(standing))

    status = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src",
            "--claim-id",
            "disjoint",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert payload["touches"] == []


def test_cli_claim_json_lists_an_overlapping_standing_claim_as_a_touch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    standing = request("claim-a", "Ada", issue=73, scope=("src",))
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    _patch_store_write(monkeypatch, _store_claim_from_request(standing))

    status = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src/work.py",
            "--claim-id",
            "overlapping",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert payload["touches"] == [
        {"issue": 73, "lane": None, "claim_id": "claim-a", "agent": "Ada", "scope": ["src"]}
    ]


def test_cli_claim_json_touch_key_set_is_unchanged_by_the_human_overlap_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A consumer pins `touches`' exact field set (issue #206): naming the
    colliding path on the human line must add no key here, and the full
    scope a touch already carries is how a consumer can compute that path
    itself today."""
    standing = request(
        "claim-a", "Ada", issue=1400, scope=("tests/adapters/test_agent_claim_cli.py",)
    )
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(
        checkout,
        "_scope_directories",
        lambda paths, **_kwargs: tuple(p for p in paths if p == "tests"),
    )
    _patch_store_write(monkeypatch, _store_claim_from_request(standing))

    status = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "1401",
            "--agent",
            "Grok 4.6",
            "--base",
            BASE,
            "--branch",
            "codex/issue-1401",
            "--scope",
            "tests",
            "--claim-id",
            "challenger",
            "--whole",
            "the whole test tree",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert len(payload["touches"]) == 1
    assert set(payload["touches"][0]) == {"issue", "lane", "claim_id", "agent", "scope"}
    assert payload["touches"][0] == {
        "issue": 1400,
        "lane": None,
        "claim_id": "claim-a",
        "agent": "Ada",
        "scope": ["tests/adapters/test_agent_claim_cli.py"],
    }


def test_claim_cost_lists_an_overlapping_standing_claim_as_a_touch() -> None:
    standing = _store_claim_from_request(request("claim-a", issue=55, scope=("src",)))
    lane = _store_claim_from_request(
        request("claim-b", "Grok 4.6", lane=True, branch="docs/foo", scope=("docs",))
    )
    narrow_scope = ("src/widget.py",)
    overlapping = protocol.conflicting_claims(
        (standing, lane), request("challenger", issue=56, scope=narrow_scope)
    )
    wide_scope = ("src", "docs")
    both = protocol.conflicting_claims(
        (standing, lane), request("wide", issue=56, scope=wide_scope)
    )

    assert [claim.claim_id for claim in overlapping] == ["claim-a"]
    assert issue_claim._touch_summary(narrow_scope, overlapping) == (
        "overlaps issue #55 on src/widget.py"
    )
    assert issue_claim._touch_summary(wide_scope, both) == (
        "overlaps issue #55 on src, lane docs/foo on docs"
    )
    assert issue_claim._touch_summary(wide_scope, ()) == "overlaps no other open claims"


def test_claim_cost_names_a_directory_scope_meeting_a_single_file_of_a_standing_claim() -> None:
    """The case that hurt a consumer twice in one night (issue #206): a
    directory in the newly granted scope contains a single file a standing
    claim already holds, so the overlap line must name that file, not just
    the standing claim's issue."""
    standing = _store_claim_from_request(
        request("claim-a", issue=1400, scope=("tests/adapters/test_agent_claim_cli.py",))
    )
    own_scope = ("tests",)

    touches = protocol.conflicting_claims(
        (standing,), request("challenger", issue=1401, scope=own_scope)
    )

    assert issue_claim._touch_summary(own_scope, touches) == (
        "overlaps issue #1400 on tests/adapters/test_agent_claim_cli.py"
    )


def test_claim_cost_counts_overflow_when_many_paths_collide_in_one_claim() -> None:
    standing = _store_claim_from_request(
        request(
            "claim-a",
            issue=55,
            scope=("src/a.py", "docs/b.md", "tests/c.py", "scripts/d.py"),
        )
    )
    own_scope = ("src", "docs", "tests", "scripts")

    touches = protocol.conflicting_claims(
        (standing,), request("challenger", issue=56, scope=own_scope)
    )

    assert issue_claim._touch_summary(own_scope, touches) == (
        "overlaps issue #55 on docs/b.md, scripts/d.py, src/a.py, and 1 more"
    )


def test_claim_cost_lists_every_overlapping_claim_separately() -> None:
    first = _store_claim_from_request(request("claim-a", issue=55, scope=("src/a.py",)))
    second = _store_claim_from_request(request("claim-b", issue=57, scope=("docs/b.md",)))
    third = _store_claim_from_request(
        request("claim-c", "Grok 4.6", lane=True, branch="docs/foo", scope=("tests/c.py",))
    )
    own_scope = ("src", "docs", "tests")

    touches = protocol.conflicting_claims(
        (first, second, third), request("challenger", issue=56, scope=own_scope)
    )

    assert issue_claim._touch_summary(own_scope, touches) == (
        "overlaps issue #55 on src/a.py, issue #57 on docs/b.md, lane docs/foo on tests/c.py"
    )


def test_board_shows_claim_age_from_the_claim_comment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    claimed = request("mine", "Ada", issue=72, branch="codex/issue-72", scope=("src",))
    client = FakeForge()
    client.board_issues = (board_issue(72, "Work", complete_contract("Claim #72.")),)
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())
    _patch_store_write(
        monkeypatch,
        _store_claim_from_request(claimed),
        ages={"mine": datetime(2026, 8, 20, 23, 30, tzinfo=UTC)},
    )

    assert issue_claim.main(["--repo", REPOSITORY, "board", "--json"]) == 0
    item = next(row for row in json.loads(capsys.readouterr().out)["items"] if row["number"] == 72)
    assert item["claim_age"] == "0h 30m"
    assert item["claim_old"] is False


def test_board_marks_a_claim_old_after_sixty_one_minutes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    claimed = request("mine", "Ada", issue=72, branch="codex/issue-72", scope=("src",))
    client = FakeForge()
    client.board_issues = (board_issue(72, "Work", complete_contract("Claim #72.")),)
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())
    _patch_store_write(
        monkeypatch,
        _store_claim_from_request(claimed),
        ages={"mine": datetime(2026, 8, 20, 22, 59, tzinfo=UTC)},
    )

    assert issue_claim.main(["--repo", REPOSITORY, "board", "--json"]) == 0
    item = next(row for row in json.loads(capsys.readouterr().out)["items"] if row["number"] == 72)
    assert item["claim_age"] == "1h 1m"
    assert item["claim_old"] is True


def test_cli_status_shows_claim_age_from_the_opened_commit(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The store equivalent of the ledger's claim-age display (issue #176):
    age comes from `opened_commit`'s committer date (faked here via
    `_patch_status_store`'s `ages`), never a later rescope -- that
    invariant is `apply`'s own (see test_store.py), not re-proven here."""
    claimed = _active_claim(
        "Ada", claim_id="mine", issue=72, branch="codex/issue-72", scope=("src",)
    )
    opened_at = datetime.fromisoformat("2026-08-20T23:30:00+00:00")
    _patch_status_store(monkeypatch, claimed, ages={claimed.claim_id: opened_at})

    assert issue_claim.main(["--repo", REPOSITORY, "status", "72"]) == 0
    status_out = capsys.readouterr().out
    assert " 0h 30m\n" in status_out
    assert " old" not in status_out.split("CLAIMED", 1)[1]

    assert issue_claim.main(["--repo", REPOSITORY, "status", "72", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["claims"][0]["age"] == "0h 30m"
    assert payload["claims"][0]["old"] is False


def test_cli_status_marks_a_claim_old_after_sixty_one_minutes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    claimed = _active_claim(
        "Ada", claim_id="mine", issue=72, branch="codex/issue-72", scope=("src",)
    )
    opened_at = datetime.fromisoformat("2026-08-20T22:59:00+00:00")
    _patch_status_store(monkeypatch, claimed, ages={claimed.claim_id: opened_at})

    assert issue_claim.main(["--repo", REPOSITORY, "status", "72"]) == 0
    assert " 1h 1m old\n" in capsys.readouterr().out

    assert issue_claim.main(["--repo", REPOSITORY, "status", "72", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["claims"][0]["age"] == "1h 1m"
    assert payload["claims"][0]["old"] is True


def test_cli_status_and_status_path_show_the_whole_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    reason = "the four adapters share one lock"
    claimed = _active_claim(
        "Ada",
        claim_id="wide",
        issue=72,
        branch="codex/issue-72",
        scope=("new_a.py", "new_b.py", "new_c.py", "new_d.py"),
        whole_reason=reason,
    )
    _patch_status_store(monkeypatch, claimed)

    assert issue_claim.main(["--repo", REPOSITORY, "status", "72"]) == 0
    status_out = capsys.readouterr().out
    assert f"  whole: {reason}" in status_out

    assert issue_claim.main(["--repo", REPOSITORY, "status", "72", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["claims"][0]["whole"] == reason

    assert issue_claim.main(["--repo", REPOSITORY, "status", "--path", "new_a.py"]) == 0
    who_out = capsys.readouterr().out
    assert f"  whole: {reason}" in who_out

    assert issue_claim.main(["--repo", REPOSITORY, "status", "--path", "new_a.py", "--json"]) == 0
    who_payload = json.loads(capsys.readouterr().out)
    assert who_payload["claims"][0]["whole"] == reason


def test_cli_release_without_json_prints_the_released_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    standing = request("mine", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",))
    client = FakeForge()
    _patch_release_session(monkeypatch, client, standing)

    released = issue_claim.main(["--repo", REPOSITORY, "release", "72", "--abandoned", "stopped"])

    assert released == 0
    assert capsys.readouterr().out == "RELEASED issue #72: mine\n"


@pytest.mark.parametrize(
    ("issue_argument", "branch", "identity_fields"),
    [
        (["72"], "codex/issue-72", {"issue": 72, "lane": None}),
        ([], "docs/lane-cleanup", {"issue": None, "lane": True}),
    ],
    ids=["issue", "lane"],
)
def test_cli_claim_json_prints_acquired_claim_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    issue_argument: list[str],
    branch: str,
    identity_fields: dict[str, object],
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())

    claimed = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            *issue_argument,
            "--agent",
            "Codex Sol",
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            branch,
            "--scope",
            "src",
            "--scope",
            "docs",
            "--claim-id",
            "cli-claim",
            "--json",
        ]
    )

    assert claimed == 0
    assert (
        capsys.readouterr().out
        == json.dumps(
            {
                "ok": True,
                "reason": "claimed",
                **identity_fields,
                "claim_id": "cli-claim",
                "agent": "Codex Sol",
                "role": "builder",
                "base": BASE,
                "branch": branch,
                "scope": ["docs", "src"],
                "resource": None,
                "resource_value": None,
                "versioned_files": 1,
                "versioned_files_total": 4,
                "share": 0.25,
                "touches": [],
                "checks": [],
            }
        )
        + "\n"
    )
    posted = _live_store_claim()
    assert posted.scope == ("docs", "src")


@pytest.mark.parametrize(
    (
        "issue_argument",
        "branch",
        "identity_fields",
        "standing_role",
        "flags",
        "agent",
        "role",
        "outcome",
    ),
    [
        (
            ["72"],
            "lane-72",
            {"issue": 72, "lane": None},
            "reviewer",
            ("--abandoned", "stopped", "--json"),
            "Ada",
            "reviewer",
            "abandoned: stopped",
        ),
        (
            ["72"],
            "lane-72",
            {"issue": 72, "lane": None},
            "reviewer",
            (
                "--claim-id",
                "mine",
                "--coordinator-override",
                "--role",
                "coordinator",
                "--abandoned",
                "verified abandoned",
                "--json",
            ),
            "Fleet Coordinator",
            "coordinator",
            "abandoned: verified abandoned",
        ),
        (
            [],
            "docs/lane-cleanup",
            {"issue": None, "lane": True},
            "reviewer",
            ("--abandoned", "stopped", "--json"),
            "Ada",
            "reviewer",
            "abandoned: stopped",
        ),
        (
            [],
            "docs/lane-cleanup",
            {"issue": None, "lane": True},
            "reviewer",
            (
                "--claim-id",
                "mine",
                "--coordinator-override",
                "--role",
                "coordinator",
                "--abandoned",
                "verified abandoned",
                "--json",
            ),
            "Fleet Coordinator",
            "coordinator",
            "abandoned: verified abandoned",
        ),
    ],
    ids=["issue-abandoned", "issue-override", "lane-abandoned", "lane-override"],
)
def test_cli_release_json_prints_effective_posted_identity(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    issue_argument: list[str],
    branch: str,
    identity_fields: dict[str, object],
    standing_role: str,
    flags: tuple[str, ...],
    agent: str,
    role: str,
    outcome: str,
) -> None:
    lane = not issue_argument
    standing = request(
        "mine", "Ada", issue=72, lane=lane, role=standing_role, branch=branch, scope=("src",)
    )
    client = FakeForge()
    # Lane mode always derives its branch from the checkout, even with an explicit
    # --claim-id (Entschieden #2: LaneIdentity carries no branch of its own), so git
    # is only forbidden for the issue-mode explicit-claim-id case.
    forbid_git = bool(issue_argument) and "--claim-id" in flags
    _patch_release_session(
        monkeypatch, client, standing, agent=agent, branch=branch, forbid_git=forbid_git
    )

    released = issue_claim.main(["--repo", REPOSITORY, "release", *issue_argument, *flags])

    assert released == 0
    assert (
        capsys.readouterr().out
        == json.dumps(
            {
                "ok": True,
                "reason": "abandoned",
                "outcome": outcome,
                **identity_fields,
                "branch": branch,
                "claim_id": "mine",
                "agent": agent,
                "role": role,
            }
        )
        + "\n"
    )
    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


def _assert_json_refusal_object(err: str, out: str, *, reason: str) -> None:
    """`ask`/`rule`/`brief`'s own `_emit_json` refusal (issue #396,
    `specs/output.spec.md`): the identical sentence stderr already printed,
    now under `message`, next to `reason` instead of a dropped `error`
    key. Compares the raw JSON text, not a parsed dict, so a reordering of
    OUT-01's `ok`, `reason`, `message` sequence would fail this assertion."""
    assert err.startswith("ERROR: ")
    message = err.removeprefix("ERROR: ").rstrip("\n")
    expected = {"ok": False, "reason": reason, "message": message}
    assert out == json.dumps(expected) + "\n"
    assert json.loads(out) == expected


@pytest.mark.parametrize(
    ("arguments", "assert_refusal"),
    [
        pytest.param(
            ["claim", "72", "--agent", "Ada", "--scope", "src", "--claim-id", "cli-claim"],
            lambda err, out: _assert_json_refusal_object(err, out, reason="unavailable"),
            id="claim-envelope-refusal",
        ),
        pytest.param(
            ["release", "72", "--agent", "Ada", "--claim-id", "mine", "--abandoned", "stopped"],
            lambda err, out: _assert_json_refusal_object(err, out, reason="precondition_failed"),
            id="release-precondition-failed",
        ),
    ],
)
def test_cli_claim_and_release_json_errors_choose_their_own_shape(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    assert_refusal: Callable[[str, str], None],
) -> None:
    """`claim`'s own dirty-tree checkout precondition (issue #406) reports
    through the shared emitter, `reason: unavailable`; `release`'s own
    generic refusal bucket (issue #425) reports `reason: precondition_failed`."""
    _patch_status_cli(monkeypatch, FakeForge())

    assert issue_claim.main(["--repo", REPOSITORY, *arguments, "--json"]) == 2
    captured = capsys.readouterr()
    assert_refusal(captured.err, captured.out)


def _prepare_pre_dispatch_refusal(
    monkeypatch: pytest.MonkeyPatch, agent: str | None, branch: str | None
) -> None:
    """The world every pre-dispatch refusal below fires in: an identity when
    the case is not about a missing one, and a checkout branch only where the
    refusal is allowed to read one -- `None` forbids git outright."""
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: agent} if agent else None)
    _forbid_github_construction(monkeypatch)
    if branch is None:

        def unused(arguments: list[str], **_kwargs: object) -> str:
            pytest.fail("this refusal must fire before git is read")

        monkeypatch.setattr(checkout, "_git_output", unused)
        return
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: branch)


@pytest.mark.parametrize(
    ("arguments", "agent", "branch"),
    [
        pytest.param(_claim_without_agent_args(), None, None, id="claim-without-an-identity"),
        pytest.param(
            ["rescope", "72", "--add", "/repo/x.py"], None, None, id="rescope-without-an-identity"
        ),
        pytest.param(
            ["release", "--abandoned", "stopped"], "Ada", "", id="release-without-issue-or-branch"
        ),
        pytest.param(
            ["release", "72", "--abandoned", "stopped"], "Ada", "", id="release-without-claim-id"
        ),
        pytest.param(
            ["release", "72", "--coordinator-override", "--abandoned", "takeover"],
            "Ada",
            None,
            id="release-override-without-the-coordinator-role",
        ),
    ],
)
def test_cli_refusals_before_the_handler_print_the_shared_envelope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
    agent: str | None,
    branch: str | None,
) -> None:
    """Issue #425 review: identity resolution and `release`'s own branch and
    override checks (REL-06..08) refuse before the named command starts, so
    they print the shared envelope (OUT-05); the text form keeps the bare
    `ERROR:` sentence it always printed."""
    _prepare_pre_dispatch_refusal(monkeypatch, agent, branch)
    text_status = issue_claim.main(["--repo", REPOSITORY, *arguments])
    text = capsys.readouterr()

    _prepare_pre_dispatch_refusal(monkeypatch, agent, branch)
    json_status = issue_claim.main(["--repo", REPOSITORY, *arguments, "--json"])
    envelope = capsys.readouterr()

    assert (text_status, json_status) == (2, 2)
    assert text.out == ""
    assert envelope.err == text.err
    _assert_json_refusal_object(envelope.err, envelope.out, reason="precondition_failed")


def test_cli_claim_json_conflict_prints_the_stdout_error_object_not_a_success_shape(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    standing = request(issue=72, scope=("src",))
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    _patch_store_write(monkeypatch, _store_claim_from_request(standing))

    claimed = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Grok 4.6",
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs",
            "--claim-id",
            "challenger",
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert claimed == 2
    _assert_json_refusal_object(captured.err, captured.out, reason="claim_conflict")


def test_cli_claim_json_transport_failure_reports_unavailable_not_conflict(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A transport/CAS failure surfacing from `store.commit_transition` is a
    plain `protocol.ClaimError`, never `protocol.ClaimConflictError` (issue
    #406, CLM-27): `claim --json` reports `unavailable`, not
    `claim_conflict`, exactly as `release`'s own retry-exhaustion refusal
    does."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    _patch_store_write(monkeypatch)

    def failing_commit_transition(*args: object, **kwargs: object) -> protocol.ClaimState:
        raise protocol.ClaimUnavailableError(
            f"{store.STATE_REF} moved 5 times while retrying: another writer on "
            "origin keeps landing first; retry the command"
        )

    monkeypatch.setattr(store, "commit_transition", failing_commit_transition)

    claimed = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Grok 4.6",
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs",
            "--claim-id",
            "challenger",
            "--json",
        ]
    )
    captured = capsys.readouterr()

    assert claimed == 2
    _assert_json_refusal_object(captured.err, captured.out, reason="unavailable")


def test_cli_module_entry_point_exits_with_mains_return_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`python -m agent_coordination.cli` and the installed console script run
    the `if __name__ == "__main__":` guard, not `main()` as a library call --
    exercise that guard directly rather than only ever calling `main()`.

    `protect` on an unparseable payload is the one command that answers
    without git, GitHub, or the store, so the exit code this observes is
    `main`'s own return value and nothing else's."""
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    _forbid_protect_git_github_and_identity(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["aco", "--repo", REPOSITORY, "protect"])
    monkeypatch.setattr(sys, "stdin", io.StringIO("not a hook payload"))

    with (
        pytest.warns(RuntimeWarning, match="agent_coordination.cli"),
        pytest.raises(SystemExit) as exited,
    ):
        runpy.run_module("agent_coordination.cli", run_name="__main__")

    assert exited.value.code == 2
    assert json.loads(capsys.readouterr().out) == {
        "decision": "deny",
        "reason": "invalid hook payload",
    }


def test_cli_claim_resource_prints_the_allocated_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())

    status = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src",
            "--resource",
            "schema-hop",
            "--claim-id",
            "hop-1",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert payload["resource"] == "schema-hop"
    assert payload["resource_value"] == 1
    posted = _live_store_claim()
    assert posted.resource == protocol.ResourceHold("schema-hop", 1)


def test_cli_two_claims_of_the_same_directory_are_advisory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeForge()
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(
        checkout,
        "_scope_directories",
        lambda paths, **_kwargs: tuple(path for path in paths if path == "src"),
    )

    first = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src",
            "--claim-id",
            "dir-a",
            "--whole",
            "shared directory",
        ]
    )
    capsys.readouterr()
    second = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "73",
            "--agent",
            "Grok 4.6",
            "--base",
            BASE,
            "--branch",
            "codex/issue-73",
            "--scope",
            "src",
            "--claim-id",
            "dir-b",
            "--whole",
            "shared directory",
        ]
    )
    claimed = capsys.readouterr().out

    assert first == 0
    assert second == 0
    assert "CONFLICT" not in claimed
    assert "overlaps issue #72 on src" in claimed


def test_cli_claim_on_a_directory_names_the_file_a_standing_claim_holds_under_it(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The real `claim` output for the case that hurt a consumer twice in one
    night (issue #206): a directory in the newly granted scope contains a
    single file a standing claim already holds."""
    client = FakeForge()
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(
        checkout,
        "_scope_directories",
        lambda paths, **_kwargs: tuple(path for path in paths if path == "tests"),
    )

    first = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "1400",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-1400",
            "--scope",
            "tests/adapters/test_agent_claim_cli.py",
            "--claim-id",
            "claim-a",
        ]
    )
    capsys.readouterr()
    second = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "1401",
            "--agent",
            "Grok 4.6",
            "--base",
            BASE,
            "--branch",
            "codex/issue-1401",
            "--scope",
            "tests",
            "--claim-id",
            "claim-b",
            "--whole",
            "the whole test tree",
        ]
    )
    claimed = capsys.readouterr().out

    assert first == 0
    assert second == 0
    assert "CONFLICT" not in claimed
    assert "overlaps issue #1400 on tests/adapters/test_agent_claim_cli.py" in claimed


def test_cli_status_and_status_path_show_two_directory_claims_as_advisory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    dir_a = _active_claim(
        "Ada", claim_id="dir-a", issue=72, branch="codex/issue-72", scope=("src",)
    )
    dir_b = _active_claim(
        "Grok 4.6", claim_id="dir-b", issue=73, branch="codex/issue-73", scope=("src",)
    )
    _patch_status_store(monkeypatch, dir_a, dir_b)

    status = issue_claim.main(["--repo", REPOSITORY, "status"])
    rendered = capsys.readouterr().out
    assert status == 0
    assert "CONFLICT" not in rendered
    assert "CLAIMED issue #72" in rendered
    assert "CLAIMED issue #73" in rendered
    assert "overlaps issue #73 (dir-b)" in rendered
    assert "overlaps issue #72 (dir-a)" in rendered

    who = issue_claim.main(["--repo", REPOSITORY, "status", "--path", "src"])
    holders = capsys.readouterr().out
    assert who == 0
    assert "CONFLICT" not in holders
    assert "CLAIMED src issue #72" in holders
    assert "CLAIMED src issue #73" in holders
    assert "overlap: issue #72 (dir-a), issue #73 (dir-b)" in holders


def test_cli_two_claims_of_the_same_file_are_advisory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeForge()
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())

    first = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src/widget.py",
            "--claim-id",
            "file-a",
        ]
    )
    capsys.readouterr()
    second = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "73",
            "--agent",
            "Grok 4.6",
            "--base",
            BASE,
            "--branch",
            "codex/issue-73",
            "--scope",
            "src/widget.py",
            "--claim-id",
            "file-b",
        ]
    )
    claimed = capsys.readouterr().out

    assert first == 0
    assert second == 0
    assert "CONFLICT" not in claimed
    assert "overlaps issue #72 on src/widget.py" in claimed


def test_cli_status_and_status_path_show_two_file_claims_as_advisory(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    file_a = _active_claim(
        "Ada", claim_id="file-a", issue=72, branch="codex/issue-72", scope=("src/widget.py",)
    )
    file_b = _active_claim(
        "Grok 4.6",
        claim_id="file-b",
        issue=73,
        branch="codex/issue-73",
        scope=("src/widget.py",),
    )
    _patch_status_store(monkeypatch, file_a, file_b)

    status = issue_claim.main(["--repo", REPOSITORY, "status"])
    rendered = capsys.readouterr().out
    assert status == 0
    assert "CONFLICT" not in rendered
    assert "CLAIMED issue #72" in rendered
    assert "CLAIMED issue #73" in rendered

    who = issue_claim.main(["--repo", REPOSITORY, "status", "--path", "src/widget.py"])
    holders = capsys.readouterr().out
    assert who == 0
    assert "CONFLICT" not in holders
    assert "CLAIMED src/widget.py issue #72" in holders
    assert "CLAIMED src/widget.py issue #73" in holders
    assert "overlap: issue #72 (file-a), issue #73 (file-b)" in holders


def test_cli_two_resource_claims_allocate_one_then_two(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeForge()
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())

    first = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src/a.py",
            "--resource",
            "schema-hop",
            "--claim-id",
            "hop-1",
            "--json",
        ]
    )
    first_payload = json.loads(capsys.readouterr().out)
    second = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "73",
            "--agent",
            "Grok 4.6",
            "--base",
            BASE,
            "--branch",
            "codex/issue-73",
            "--scope",
            "src/b.py",
            "--resource",
            "schema-hop",
            "--claim-id",
            "hop-2",
            "--json",
        ]
    )
    second_payload = json.loads(capsys.readouterr().out)

    assert first == 0
    assert second == 0
    assert first_payload["resource_value"] == 1
    assert second_payload["resource_value"] == 2


def test_cli_resource_race_still_yields_unique_live_holds(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeForge()
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    earlier = request(
        "earlier",
        "Grok 4.6",
        issue=72,
        scope=("src/a.py",),
        resource="schema-hop",
        resource_value=1,
    )
    _patch_store_write(monkeypatch, _store_claim_from_request(earlier))

    status = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "73",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-73",
            "--scope",
            "src/b.py",
            "--resource",
            "schema-hop",
            "--claim-id",
            "later",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    holds = sorted(
        claim.resource.value
        for claim in store.fetch_state(worktree=Path("."), remote="origin").claims.values()
        if claim.resource is not None and claim.resource.name == "schema-hop"
    )

    assert status == 0
    assert payload["resource_value"] == 2
    assert holds == [1, 2]


def test_status_path_lists_every_holder_without_calling_overlap_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    mine = _active_claim("Ada", claim_id="mine", issue=72, scope=("src/widget.py",))
    theirs = _active_claim(
        "Grok 4.6",
        claim_id="theirs",
        issue=73,
        branch="codex/issue-73-claims",
        scope=("src/widget.py",),
    )
    _patch_status_store(monkeypatch, mine, theirs)

    status = issue_claim.main(["--repo", REPOSITORY, "status", "--path", "src/widget.py"])
    rendered = capsys.readouterr().out

    assert status == 0
    assert "CONFLICT" not in rendered
    assert "CLAIMED src/widget.py issue #72" in rendered
    assert "CLAIMED src/widget.py issue #73" in rendered
    assert "overlap: issue #72 (mine), issue #73 (theirs)" in rendered


def test_next_names_an_old_ruling_when_the_item_is_pulled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    issue = board_issue(
        10,
        "Work",
        complete_contract("Claim #10.", expectation=[ruled_expectation("Name it.")]),
    )
    client = FakeForge()
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (issue,))
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(
        checkout,
        "trunk_landings",
        lambda *_args, **_kwargs: tuple(
            checkout.TrunkLanding(f"sha{hour}", datetime(2026, 8, 29, hour, tzinfo=UTC), None)
            for hour in range(10)
        ),
    )
    _patch_store_write(monkeypatch)

    assert issue_claim.main(["--repo", REPOSITORY, "next"]) == 0
    assert capsys.readouterr().out == (
        "#10 score -10: Work\n"
        "Next: Claim #10.\n"
        "Run: aco claim 10 --scope <paths>\n"
        + _SCOPE_UNKNOWN_NOTE_LINE
        + "ruled 10 landings ago: refine again at the pull\n"
        + _PARALLEL_UNKNOWN_TAIL
    )

    assert issue_claim.main(["--repo", REPOSITORY, "next", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ruling_landings"] == 10
    assert payload["ruling_old"] is True
    assert payload["ruling_hint"] == "ruled 10 landings ago: refine again at the pull"


def test_identity_conflict_still_marks_status_conflict(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = _active_claim(issue=72, scope=("src/a.py",))
    second = _active_claim(claim_id="claim-b", agent="Grok 4.6", issue=72, scope=("src/b.py",))
    opened_at = datetime(2026, 8, 21, tzinfo=UTC)
    ages: dict[str, datetime] = {first.claim_id: opened_at, second.claim_id: opened_at}

    assert _status((first, second), None, ages, body.Storage.GITHUB) == 2
    rendered = capsys.readouterr().out
    assert rendered.count("CONFLICT") == 2


def test_identity_conflict_still_marks_status_json_conflict(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = _active_claim(issue=72, scope=("src/a.py",))
    second = _active_claim(claim_id="claim-b", agent="Grok 4.6", issue=72, scope=("src/b.py",))
    opened_at = datetime(2026, 8, 21, tzinfo=UTC)
    ages: dict[str, datetime] = {first.claim_id: opened_at, second.claim_id: opened_at}

    assert _status_json((first, second), None, ages, None) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["reason"] == "conflict"


def test_no_path_class_list_is_read_or_written() -> None:
    assert not Path("src/agent_coordination").joinpath("single_writer.py").exists()
    text = Path("src/agent_coordination/protocol.py").read_text()
    assert "single-writer" not in text
    assert "single_writer" not in text


DOCUMENTATION_LANE_BRANCH = "docs/tidy-readme"


def landing_pull_request(
    *,
    body: str,
    number: int = 12,
    base_ref_name: str = "main",
    head_ref_name: str = LANDING_BRANCH,
    head_repository: str = REPOSITORY,
    author: str = "ada",
    merged: bool = False,
    merge_commit: str | None = None,
) -> forge.Landing:
    """`merge_commit` defaults to a shared, well-formed sha once `merged` is
    true (a real merged pull request always carries one) and to `None`
    otherwise; a test proving the merge commit's own authority
    (issue #397) passes its own sha instead."""
    return forge.Landing(
        number,
        author,
        body,
        github._repository_id(head_repository),
        head_ref_name,
        base_ref_name,
        merged,
        merge_commit if merge_commit is not None else (MERGE_COMMIT_SHA if merged else None),
    )


def documentation_lane_claim(
    claim_id: str = "tidy", branch: str = DOCUMENTATION_LANE_BRANCH
) -> ClaimRequest:
    return request(claim_id, lane=True, branch=branch, scope=("README.md",))


def check_client(
    monkeypatch: pytest.MonkeyPatch,
    detail: forge.Landing,
    *,
    standing: tuple[ClaimRequest, ...] = (),
) -> FakeForge:
    """A client serving one pull request and the claims that back it."""
    claims = standing or (
        request("landing", issue=WORK_ITEM_ISSUE, branch=LANDING_BRANCH, scope=("src",)),
    )
    client = FakeForge()
    client.landings[detail.number] = detail
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    _patch_store_write(monkeypatch, *(_store_claim_from_request(claimed) for claimed in claims))
    return client


def run_check(number: int = 12) -> int:
    return issue_claim.main(["--repo", REPOSITORY, "check", str(number)])


def test_check_accepts_a_claimed_work_item_that_the_pull_request_closes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    check_client(
        monkeypatch,
        landing_pull_request(
            body=f"Work-Item: {REPOSITORY}#{WORK_ITEM_ISSUE}\n\nCloses #{WORK_ITEM_ISSUE}"
        ),
    )

    assert run_check() == 0
    assert capsys.readouterr().out == (
        f"PR #12 by ada declares Work-Item: {REPOSITORY}#{WORK_ITEM_ISSUE}\n"
    )


def test_check_reads_the_same_work_item_from_shorthand_and_qualified_lines(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    check_client(
        monkeypatch,
        landing_pull_request(
            body=f"Work-Item: #{WORK_ITEM_ISSUE}\n\nCloses {REPOSITORY}#{WORK_ITEM_ISSUE}"
        ),
    )

    assert run_check() == 0
    assert capsys.readouterr().out == (
        f"PR #12 by ada declares Work-Item: {REPOSITORY}#{WORK_ITEM_ISSUE}\n"
    )


def test_check_refuses_a_named_sentence_outside_a_checkout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #178: `tests/conftest.py`'s autouse `_isolate_git_toplevel`
    fixture fakes `rev-parse --show-toplevel` to succeed for every test, so
    no test could otherwise observe a missing working tree -- this is its
    counterpart, overriding the fake back to the failure atelier-2's
    checkout-less CI job hit, to prove the command refuses with the named
    sentence instead of raising git's own message."""
    check_client(
        monkeypatch,
        landing_pull_request(
            body=f"Work-Item: {REPOSITORY}#{WORK_ITEM_ISSUE}\n\nCloses #{WORK_ITEM_ISSUE}"
        ),
    )

    def outside_a_checkout(arguments: list[str], **_kwargs: object) -> str:
        assert arguments == ["rev-parse", "--show-toplevel"]
        raise ClaimError("fatal: not a git repository (or any of the parent directories): .git")

    monkeypatch.setattr(checkout, "_git_output", outside_a_checkout)

    assert run_check() == 2
    assert capsys.readouterr().err == (
        "ERROR: this command reads the repository's body contract from "
        ".agent-claim/board.toml and needs a checkout (a shallow one is "
        "enough): fatal: not a git repository (or any of the parent "
        "directories): .git\n"
    )


def test_check_json_reports_unavailable_outside_a_checkout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The same refusal as `test_check_refuses_a_named_sentence_outside_a_checkout`,
    now read through `--json`'s own envelope (issue #404): `reason:
    "unavailable"`, the generic bucket every other pre-dispatch
    forge-resolution failure reports through."""

    def outside_a_checkout(arguments: list[str], **_kwargs: object) -> str:
        raise ClaimError("fatal: not a git repository (or any of the parent directories): .git")

    monkeypatch.setattr(checkout, "_git_output", outside_a_checkout)

    status = issue_claim.main(["--repo", REPOSITORY, "check", "12", "--json"])

    captured = capsys.readouterr()
    assert status == 2
    _assert_json_refusal_object(captured.err, captured.out, reason="unavailable")


def test_check_reports_invalid_usage_when_repo_is_given_under_state_ref(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`check`'s forge resolution draws the same `invalid_usage`/`unavailable`
    split `rule`/`brief` already do (issue #404): `--repo` is the wrong
    flag under `storage = state-ref`."""
    _write_state_ref_pin(tmp_path)

    status = issue_claim.main(["--repo", "acme/items", "check", "258", "--json"])

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err == "ERROR: --repo is meaningless under storage = state-ref\n"
    _assert_json_refusal_object(captured.err, captured.out, reason="invalid_usage")


def test_check_accepts_an_issueless_documentation_pull_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    check_client(
        monkeypatch,
        landing_pull_request(
            body="No-Item: docs\n\nTidy the README.",
            head_ref_name=DOCUMENTATION_LANE_BRANCH,
        ),
        standing=(documentation_lane_claim(),),
    )

    assert run_check() == 0
    assert capsys.readouterr().out == "PR #12 by ada declares No-Item: docs\n"


@pytest.mark.parametrize(
    ("standing", "reason"),
    [
        pytest.param(
            (documentation_lane_claim(branch="docs/another-lane"),),
            f"has no active issue-less lane claim on branch {DOCUMENTATION_LANE_BRANCH!r}",
            id="lane-claim-on-another-branch",
        ),
        pytest.param(
            (
                request(
                    "item-lane",
                    issue=WORK_ITEM_ISSUE,
                    branch=DOCUMENTATION_LANE_BRANCH,
                    scope=("README.md",),
                ),
            ),
            f"has no active issue-less lane claim on branch {DOCUMENTATION_LANE_BRANCH!r}",
            id="issue-claim-on-the-head-branch",
        ),
    ],
)
def test_check_refuses_an_issueless_pull_request_without_its_lane_claim(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    standing: tuple[ClaimRequest, ...],
    reason: str,
) -> None:
    check_client(
        monkeypatch,
        landing_pull_request(
            body="No-Item: docs\n\nTidy the README.",
            head_ref_name=DOCUMENTATION_LANE_BRANCH,
        ),
        standing=standing,
    )

    assert run_check() == 2
    assert capsys.readouterr().err == f"REFUSED: pull request #12 {reason}\n"


def test_check_refuses_an_issueless_pull_request_that_closes_an_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    check_client(
        monkeypatch,
        landing_pull_request(
            body=f"No-Item: fix\n\nCloses #{WORK_ITEM_ISSUE}",
            head_ref_name=DOCUMENTATION_LANE_BRANCH,
        ),
        standing=(documentation_lane_claim(),),
    )

    assert run_check() == 2
    assert capsys.readouterr().err == (
        f"REFUSED: pull request #12 declares no work item but closes "
        f"{REPOSITORY}#{WORK_ITEM_ISSUE}; name it as the work item\n"
    )


def test_check_refuses_a_pull_request_proposing_another_repositorys_branch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    check_client(
        monkeypatch,
        landing_pull_request(
            body=f"Work-Item: #{WORK_ITEM_ISSUE}\n\nCloses #{WORK_ITEM_ISSUE}",
            head_repository="fork/agent-coordination",
        ),
    )

    assert run_check() == 2
    assert capsys.readouterr().err == (
        "REFUSED: pull request #12 proposes a branch of fork/agent-coordination; "
        "cross-repository pull requests are not classified\n"
    )


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        pytest.param(
            "Advances #72\n\nJust some prose.",
            "carries no `Work-Item:` or `No-Item:` line",
            id="advances-is-not-a-classification",
        ),
        pytest.param(
            "Work-Item: #72\nNo-Item: docs\n\nCloses #72",
            "carries 2 classification lines; exactly one is required",
            id="duplicate-classification",
        ),
        pytest.param(
            "Work-Item: #72\nWork-Item: #73\n\nCloses #72\nCloses #73",
            "names two work items, #72 and #73; split it",
            id="two-work-items",
        ),
        pytest.param(
            "No-Item: chore\n\nHousekeeping.",
            "carries `No-Item: chore`; an issue-less pull request is docs or fix",
            id="unknown-no-item-kind",
        ),
        pytest.param(
            "Work-Item: soon\n\nCloses #72",
            "carries `Work-Item: soon`; a work item reads OWNER/REPO#n or #n",
            id="malformed-work-item",
        ),
        pytest.param(
            "Work-Item: #72\n\nNo closing keyword here.",
            f"carries no closing reference for its work item {REPOSITORY}#72",
            id="missing-closing-reference",
        ),
        pytest.param(
            "Work-Item: #72\n\nCloses #72\nCloses #99",
            f"closes {REPOSITORY}#99 besides its work item {REPOSITORY}#72; "
            "a pull request lands one item",
            id="closes-another-item",
        ),
        pytest.param(
            "Work-Item: other/repo#5\n\nCloses other/repo#5",
            "names work item other/repo#5 of another repository, which holds no claim here",
            id="foreign-work-item",
        ),
    ],
)
def test_check_refuses_a_pull_request_body_with_one_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    body: str,
    reason: str,
) -> None:
    check_client(monkeypatch, landing_pull_request(body=body))

    assert run_check() == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"REFUSED: pull request #12 {reason}\n"


def test_check_refuses_a_work_item_without_a_claim_on_the_head_branch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    check_client(
        monkeypatch,
        landing_pull_request(body="Work-Item: #72\n\nCloses #72"),
        standing=(request("elsewhere", issue=72, branch="codex/other-lane", scope=("src",)),),
    )

    assert run_check() == 2
    assert capsys.readouterr().err == (
        f"REFUSED: pull request #12 has no active claim for #72 on branch {LANDING_BRANCH!r}\n"
    )


def test_check_refuses_a_pull_request_that_does_not_target_the_default_branch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = check_client(
        monkeypatch,
        landing_pull_request(body="Work-Item: #72\n\nCloses #72", base_ref_name="release"),
    )
    client.default_branch_name = "trunk"

    assert run_check() == 2
    assert capsys.readouterr().err == (
        "REFUSED: pull request #12 targets 'release', not the default branch 'trunk'\n"
    )


def test_check_reads_a_fenced_classification_line_as_documentation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    check_client(
        monkeypatch,
        landing_pull_request(body="Documents the convention:\n\n```\nWork-Item: #72\n```\n"),
    )

    assert run_check() == 2
    assert capsys.readouterr().err == (
        "REFUSED: pull request #12 carries no `Work-Item:` or `No-Item:` line\n"
    )


LANE_BRANCH = "docs/tidy-readme"


_MERGE_COMMIT_COMMITTED_AT = datetime(2026, 9, 1, tzinfo=UTC)


def _trunk_landing(
    sha: str, classification: board.TrunkClassification | board.ClassificationDefect | None
) -> checkout.TrunkLanding:
    """One walked trunk commit (issue #397): the shape
    `merged_release_client`'s own `checkout.trunk_landings` stub returns,
    and every test proving a merge-commit defect builds its own variant
    from -- the same reader and grammar `storage = state-ref` already
    trusts (`_trunk_landing_defect`)."""
    return checkout.TrunkLanding(sha, _MERGE_COMMIT_COMMITTED_AT, classification)


@dataclass(frozen=True)
class ReleaseMergeScenario:
    """The pull request body, merge facts, and walked trunk
    `merged_release_client` builds a landing from -- one parametrized case's
    worth, typed instead of a loose `dict[str, object]` so each keyword
    forwards to it honestly."""

    body: str
    merged: bool = True
    base_ref_name: str = "main"
    landings: tuple[checkout.TrunkLanding, ...] | None = None


def merged_release_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: str,
    merged: bool = True,
    base_ref_name: str = "main",
    lane: bool = False,
    merge_commit: str = MERGE_COMMIT_SHA,
    landings: tuple[checkout.TrunkLanding, ...] | None = None,
) -> FakeForge:
    """A session whose one claim can be released against pull request #12.

    `landings` stubs the walked first-parent trunk every release now
    verifies its merge commit against (issue #397, Befund 41; issue #405
    gate follow-up): `None` -- the default -- seeds the one trunk commit
    that authorizes closing `WORK_ITEM_ISSUE` at `merge_commit` for an
    issue release, or declaring the lane issue-less for a lane release,
    matching this fixture's own happy path either way; a test of a
    merge-commit defect passes its own tuple instead.
    """
    branch = LANE_BRANCH if lane else LANDING_BRANCH
    standing = request(
        "landing",
        "Ada",
        issue=None if lane else WORK_ITEM_ISSUE,
        branch=branch,
        scope=("src",),
    )
    client = FakeForge()
    client.landings[12] = landing_pull_request(
        body=body,
        merged=merged,
        base_ref_name=base_ref_name,
        head_ref_name=branch,
        merge_commit=merge_commit if merged else None,
    )
    _patch_release_session(monkeypatch, client, standing, branch=branch)
    default_classification = (
        board.NoItemClassification(board.NoItemKind.DOCS)
        if lane
        else board.TrunkWorkItemClassification((WORK_ITEM_ISSUE,))
    )
    resolved = (
        landings
        if landings is not None
        else (_trunk_landing(merge_commit, default_classification),)
    )
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: resolved)
    return client


def test_release_merged_records_the_pull_request_that_landed_the_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")
    client.closed_issues.add(WORK_ITEM_ISSUE)
    monkeypatch.setattr(issue_claim, "_fetch_issue_reference", _LIVE_FETCH_ISSUE_REFERENCE)

    assert issue_claim.main(["--repo", REPOSITORY, "release", "72", "--merged", "12"]) == 0

    assert client.issue_reference_lookups == [WORK_ITEM_ISSUE]
    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


def _landed_dependency(
    closed_at: datetime = datetime(2026, 9, 10, tzinfo=UTC),
) -> board.IssueDependency:
    """The now-closed `blocked_by` relation a dependent of `WORK_ITEM_ISSUE`
    carries once GitHub has recorded the landing (issue #256)."""
    return block_dependency(WORK_ITEM_ISSUE, state=board.BlockerState.CLOSED, closed_at=closed_at)


def _two_dependants_freed_by_the_landing() -> tuple[
    tuple[board.Issue, ...], dict[int, tuple[board.IssueDependency, ...]]
]:
    """Two open items whose only blocker was `WORK_ITEM_ISSUE`, one of which
    also unblocks a third, still-waiting item, plus a fourth item a foreign
    repository still blocks even though its own local blocker just closed
    (issue #256) -- the fixture `release --merged`'s own `freed`/`next`
    tests share, so a lower- and a higher-scored freed pick differ only by
    which one unblocks something else, and a not-fully-freed item stays out
    of `freed` even once its local blocker is gone."""
    lower, lower_dependencies = blocked_issue(80, "Lower priority freed item", _landed_dependency())
    higher, higher_dependencies = blocked_issue(
        81, "Higher priority freed item", _landed_dependency()
    )
    waiting, waiting_dependencies = blocked_issue(82, "Still waiting", block_dependency(81))
    still_foreign_blocked, still_foreign_blocked_dependencies = blocked_issue(
        83,
        "Still foreign-blocked",
        _landed_dependency(),
        block_dependency(9, repository="other/repo"),
    )
    issues = (lower, higher, waiting, still_foreign_blocked)
    dependencies = {
        **lower_dependencies,
        **higher_dependencies,
        **waiting_dependencies,
        **still_foreign_blocked_dependencies,
    }
    return issues, dependencies


@dataclass(frozen=True)
class ReleaseFreedScenario:
    """One landing's currently open board and the `freed`/`next` release
    should report for it (issue #256)."""

    issues: tuple[board.Issue, ...]
    dependencies: dict[int, tuple[board.IssueDependency, ...]]
    freed: list[int]
    next_number: int | None


def _release_freed_scenarios() -> list[ReleaseFreedScenario]:
    freeing_issues, freeing_dependencies = _two_dependants_freed_by_the_landing()
    return [
        ReleaseFreedScenario(freeing_issues, freeing_dependencies, [80, 81], 81),
        ReleaseFreedScenario((), {}, [], None),
    ]


@pytest.mark.parametrize(
    "scenario", _release_freed_scenarios(), ids=["two-dependants-freed", "no-dependants"]
)
def test_release_merged_reports_the_json_freed_list_and_next_pick(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: ReleaseFreedScenario,
) -> None:
    client = merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")
    client.closed_issues.add(WORK_ITEM_ISSUE)
    monkeypatch.setattr(issue_claim, "_fetch_issue_reference", _LIVE_FETCH_ISSUE_REFERENCE)
    client.board_issues = scenario.issues
    client.board_dependencies = scenario.dependencies

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "release", str(WORK_ITEM_ISSUE), "--merged", "12", "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["freed"] == scenario.freed
    assert payload["next"] == scenario.next_number


def test_release_merged_json_reports_ok_reason_merged_and_outcome(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #425: a `--merged` release's own `reason` is the stable token
    `merged`, distinct from `outcome`'s own prose sentence."""
    client = merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")
    client.closed_issues.add(WORK_ITEM_ISSUE)
    monkeypatch.setattr(issue_claim, "_fetch_issue_reference", _LIVE_FETCH_ISSUE_REFERENCE)

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "release", str(WORK_ITEM_ISSUE), "--merged", "12", "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["reason"] == "merged"
    assert payload["outcome"] == "merged #12"


def test_release_merged_prints_the_freed_and_next_lines(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")
    client.closed_issues.add(WORK_ITEM_ISSUE)
    monkeypatch.setattr(issue_claim, "_fetch_issue_reference", _LIVE_FETCH_ISSUE_REFERENCE)
    client.board_issues, client.board_dependencies = _two_dependants_freed_by_the_landing()

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "release", str(WORK_ITEM_ISSUE), "--merged", "12"]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert f"RELEASED issue #{WORK_ITEM_ISSUE}: landing\n" in out
    assert "freed: #80, #81\n" in out
    assert "next: #81 score" in out
    assert "Higher priority freed item" in out


PARENT_OF_WORK_ITEM = 79


def _released_last_child_client(
    monkeypatch: pytest.MonkeyPatch, *, sibling_open: bool, parent_closed: bool = False
) -> FakeForge:
    """`merged_release_client` plus a recorded parent relation (issue #348,
    Beweis 4): `WORK_ITEM_ISSUE` is `PARENT_OF_WORK_ITEM`'s only child when
    `sibling_open` is `False` -- its own close leaves the parent with no
    open children and no uncut `[[slice]]` row, exactly `next`'s own
    `CloseContainerAction` branch -- or one still-open sibling when `True`,
    the parent hint's own negative case. `parent_closed` (G2 review) covers
    the third negative case: the parent itself already closed (by some
    other landing) before this release even runs -- a childless, uncut
    parent that is not open must never be named closable, since a second
    close would only refuse."""
    client = merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")
    client.closed_issues.add(WORK_ITEM_ISSUE)
    monkeypatch.setattr(issue_claim, "_fetch_issue_reference", _LIVE_FETCH_ISSUE_REFERENCE)
    client.parents[WORK_ITEM_ISSUE] = board.ParentIssue(
        board.IssueReference(REPOSITORY, PARENT_OF_WORK_ITEM),
        complete_contract("keiner"),
        body.ItemKind.CONTAINER,
    )
    children = [board.ChildItem(WORK_ITEM_ISSUE, board.ChildState.CLOSED)]
    if sibling_open:
        children.append(board.ChildItem(999, board.ChildState.OPEN))
    client.children[PARENT_OF_WORK_ITEM] = tuple(children)
    if parent_closed:
        client.closed_issues.add(PARENT_OF_WORK_ITEM)
    return client


@pytest.mark.parametrize(
    ("sibling_open", "parent_closed", "hint_expected"),
    [
        pytest.param(False, False, True, id="last_open_child_names_the_parent"),
        pytest.param(True, False, False, id="a_sibling_still_open_omits_the_hint"),
        pytest.param(False, True, False, id="an_already_closed_parent_omits_the_hint"),
    ],
)
def test_release_merged_names_the_parent_hint_only_for_the_last_open_child(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    sibling_open: bool,
    parent_closed: bool,
    hint_expected: bool,
) -> None:
    """issue #348, Beweis 4: releasing a container's last open child names
    the parent as freshly closable, the same decision `next`'s own `close:`
    line makes for a childless, uncut container -- a still-open sibling
    keeps the container un-closable and the hint absent, and so does a
    parent that is already closed itself (G2 review)."""
    _released_last_child_client(monkeypatch, sibling_open=sibling_open, parent_closed=parent_closed)

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "release", str(WORK_ITEM_ISSUE), "--merged", "12"]
    )

    assert exit_code == 0
    hint = f"parent #{PARENT_OF_WORK_ITEM}: no open children — close it\n"
    assert (hint in capsys.readouterr().out) is hint_expected


def test_release_merged_json_carries_the_parent_closable_number(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """issue #348, Beweis 4 (JSON): `parent_closable` carries the same
    number the text form's parent hint names."""
    _released_last_child_client(monkeypatch, sibling_open=False)

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "release", str(WORK_ITEM_ISSUE), "--merged", "12", "--json"]
    )

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["parent_closable"] == PARENT_OF_WORK_ITEM


def test_release_merged_fetches_each_candidates_dependencies_only_once(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The `freed` report and the projected `next` pick share one dependency
    fetch (issue #256 review): `_board`'s own board build must not re-list
    the same blocked-by candidates `_freed_item_numbers` already read."""
    client = merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")
    client.closed_issues.add(WORK_ITEM_ISSUE)
    monkeypatch.setattr(issue_claim, "_fetch_issue_reference", _LIVE_FETCH_ISSUE_REFERENCE)
    client.board_issues, client.board_dependencies = _two_dependants_freed_by_the_landing()
    observed_dependency_calls: list[int] = []
    original_list_board_dependencies = client.list_board_dependencies

    def spy_list_board_dependencies(number: int) -> tuple[board.IssueDependency, ...]:
        observed_dependency_calls.append(number)
        return original_list_board_dependencies(number)

    monkeypatch.setattr(client, "list_board_dependencies", spy_list_board_dependencies)

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "release", str(WORK_ITEM_ISSUE), "--merged", "12"]
    )

    assert exit_code == 0
    assert sorted(observed_dependency_calls) == [80, 81, 82, 83]


def test_release_merged_prints_a_hint_instead_of_failing_when_the_board_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A forge outage that only starts after the release itself already
    committed must not undo or fail it (issue #256): the release's own
    exit code and store effect stay exactly what a reachable forge would
    have produced, with one hint line standing in for `freed`/`next`."""
    client = merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")
    client.closed_issues.add(WORK_ITEM_ISSUE)
    monkeypatch.setattr(issue_claim, "_fetch_issue_reference", _LIVE_FETCH_ISSUE_REFERENCE)

    def unreachable() -> tuple[board.Issue, ...]:
        raise forge.ForgeTransientError("gh: connection reset")

    monkeypatch.setattr(client, "list_open_board_issues", unreachable)

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "release", str(WORK_ITEM_ISSUE), "--merged", "12"]
    )

    assert exit_code == 0
    out = capsys.readouterr().out
    assert out.startswith(f"RELEASED issue #{WORK_ITEM_ISSUE}: landing\n")
    assert "hint:" in out
    assert "freed:" not in out
    assert "next:" not in out
    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


def test_release_merged_json_omits_freed_and_next_when_the_board_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")
    client.closed_issues.add(WORK_ITEM_ISSUE)
    monkeypatch.setattr(issue_claim, "_fetch_issue_reference", _LIVE_FETCH_ISSUE_REFERENCE)

    def unreachable() -> tuple[board.Issue, ...]:
        raise forge.ForgeTransientError("gh: connection reset")

    monkeypatch.setattr(client, "list_open_board_issues", unreachable)

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "release", str(WORK_ITEM_ISSUE), "--merged", "12", "--json"]
    )

    assert exit_code == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert "freed" not in payload
    assert "next" not in payload
    assert "hint:" in captured.err


def test_release_merged_accepts_an_issueless_lane_that_landed_without_an_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    merged_release_client(monkeypatch, body="No-Item: docs", lane=True)

    assert issue_claim.main(["--repo", REPOSITORY, "release", "--merged", "12"]) == 0

    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


def test_release_merged_refuses_an_issueless_lane_landing_off_trunk(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #405 CI coverage follow-up: `_trunk_no_item_landing_defect`'s
    own off-trunk branch, the `LaneIdentity` counterpart to
    `test_release_merged_refuses_a_landing_it_cannot_verify`'s `off-trunk`
    case -- an issue-less lane's merge commit missing from the walked
    trunk refuses exactly the same way."""
    merged_release_client(monkeypatch, body="No-Item: docs", lane=True, landings=())

    assert issue_claim.main(["--repo", REPOSITORY, "release", "--merged", "12"]) == 2

    assert capsys.readouterr().err == (
        f"ERROR: merge commit {MERGE_COMMIT_SHA} of pull request #12 "
        "is not on the first-parent trunk\n"
    )


def test_release_merged_refuses_an_issueless_lane_landing_with_a_classification_defect(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #405 CI coverage follow-up: `_trunk_no_item_landing_defect`
    surfaces the walked trunk's own `ClassificationDefect` message
    verbatim when the merge commit's own trailer is malformed, never a
    generic "no `No-Item:` trailer" refusal."""
    merged_release_client(
        monkeypatch,
        body="No-Item: docs",
        lane=True,
        landings=(
            _trunk_landing(
                MERGE_COMMIT_SHA, board.ClassificationDefect("names two conflicting issues")
            ),
        ),
    )

    assert issue_claim.main(["--repo", REPOSITORY, "release", "--merged", "12"]) == 2

    assert capsys.readouterr().err == (
        f"ERROR: merge commit {MERGE_COMMIT_SHA} of pull request #12 names two conflicting issues\n"
    )


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        pytest.param(
            ReleaseMergeScenario(body="Work-Item: #72\n\nCloses #72", merged=False),
            "pull request #12 is not merged",
            id="not-merged",
        ),
        pytest.param(
            ReleaseMergeScenario(body="Work-Item: #72\n\nCloses #72", base_ref_name="release"),
            "pull request #12 merged into 'release', not the default branch 'main'",
            id="wrong-base",
        ),
        pytest.param(
            ReleaseMergeScenario(body="Work-Item: #72\n\nCloses #72", landings=()),
            f"merge commit {MERGE_COMMIT_SHA} of pull request #12 is not on the first-parent trunk",
            id="off-trunk",
        ),
        pytest.param(
            ReleaseMergeScenario(
                body="Work-Item: #72\n\nCloses #72",
                landings=(_trunk_landing(MERGE_COMMIT_SHA, None),),
            ),
            f"merge commit {MERGE_COMMIT_SHA} of pull request #12 carries no `Work-Item:` trailer",
            id="no-trailer",
        ),
        pytest.param(
            ReleaseMergeScenario(
                body="Work-Item: #72\n\nCloses #72",
                landings=(
                    _trunk_landing(MERGE_COMMIT_SHA, board.TrunkWorkItemClassification((99,))),
                ),
            ),
            f"merge commit {MERGE_COMMIT_SHA} of pull request #12 does not name work item #72",
            id="another-item",
        ),
        pytest.param(
            ReleaseMergeScenario(
                body="Work-Item: #72\n\nCloses #72",
                landings=(
                    _trunk_landing(
                        MERGE_COMMIT_SHA, board.NoItemClassification(board.NoItemKind.DOCS)
                    ),
                ),
            ),
            f"merge commit {MERGE_COMMIT_SHA} of pull request #12 does not name work item #72",
            id="trunk-declares-no-item",
        ),
    ],
)
def test_release_merged_refuses_a_landing_it_cannot_verify(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: ReleaseMergeScenario,
    reason: str,
) -> None:
    """Issue #397, Befund 41 (Beweis 1): a merge commit that is off-trunk,
    carries no `Work-Item:` trailer, or names a different item refuses
    before close/release, regardless of what the pull request's own --
    mutable -- body still says."""
    merged_release_client(
        monkeypatch,
        body=scenario.body,
        merged=scenario.merged,
        base_ref_name=scenario.base_ref_name,
        landings=scenario.landings,
    )
    _stub_issue_reference(monkeypatch, {WORK_ITEM_ISSUE: (forge.ItemState.CLOSED, "", "")})

    assert issue_claim.main(["--repo", REPOSITORY, "release", "72", "--merged", "12"]) == 2
    assert capsys.readouterr().err == f"ERROR: {reason}\n"
    assert store.fetch_state(worktree=Path("."), remote="origin").claims


@pytest.mark.parametrize(
    "body",
    [
        pytest.param("", id="empty-body"),
        pytest.param("Advances #72", id="no-work-item-line"),
        pytest.param("Work-Item: #99\n\nCloses #99", id="wrong-numbered-work-item"),
    ],
)
def test_release_merged_ignores_the_pull_requests_own_body_for_an_issue(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], body: str
) -> None:
    """Issue #397, Befund 41: the merge commit's own `Work-Item: #72`
    trailer is the authority for an issue release, not the pull request's
    mutable `body` -- an edited, missing, or wrong-numbered body still
    lands the release the trailer already authorizes."""
    client = merged_release_client(monkeypatch, body=body)

    assert issue_claim.main(["--repo", REPOSITORY, "release", "72", "--merged", "12"]) == 0

    assert client.closed_issues == {WORK_ITEM_ISSUE}
    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


def test_release_merged_closes_the_still_open_work_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #359 Card 1 (Beweis 4): a merged release against a still-open
    work item closes it through the forge writer -- one comment naming the
    landing pull request, then the close -- instead of refusing, and still
    releases the claim."""
    client = merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")

    assert issue_claim.main(["--repo", REPOSITORY, "release", "72", "--merged", "12"]) == 0

    assert client.closed_issues == {WORK_ITEM_ISSUE}
    assert client.landing_comments == {WORK_ITEM_ISSUE: github.landing_comment(12)}
    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


def test_release_merged_refuses_a_lane_whose_merge_commit_names_a_work_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #405 gate follow-up: a lane release's authority is the merge
    commit's own trailer block, not the pull request's mutable `body` --
    ignored here even though it declares `No-Item:` -- so a trunk commit
    that actually names a work item still refuses."""
    merged_release_client(
        monkeypatch,
        body="No-Item: docs",
        lane=True,
        landings=(_trunk_landing(MERGE_COMMIT_SHA, board.TrunkWorkItemClassification((72,))),),
    )

    assert issue_claim.main(["--repo", REPOSITORY, "release", "--merged", "12"]) == 2
    assert capsys.readouterr().err == (
        f"ERROR: merge commit {MERGE_COMMIT_SHA} of pull request #12 carries a "
        "`Work-Item:` trailer; an issue-less lane needs a `No-Item:` trailer\n"
    )


def test_release_merged_refuses_an_unclassified_lane_merge_commit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An issue-less lane's authority is its own merge commit's trailer
    block (issue #405 gate follow-up): a trunk commit carrying neither
    `Work-Item:` nor `No-Item:` refuses regardless of what the pull
    request's own mutable body says."""
    merged_release_client(
        monkeypatch,
        body="No-Item: docs",
        lane=True,
        landings=(_trunk_landing(MERGE_COMMIT_SHA, None),),
    )

    assert issue_claim.main(["--repo", REPOSITORY, "release", "--merged", "12"]) == 2
    assert capsys.readouterr().err == (
        f"ERROR: merge commit {MERGE_COMMIT_SHA} of pull request #12 carries no "
        "`Work-Item:` or `No-Item:` trailer\n"
    )


def test_release_merged_refuses_a_work_item_neither_open_nor_closed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`_verify_merged_release`'s third `reference.state` branch: the named
    work item vanished between the pull request's own classification and
    this read (a real race, not reachable through the already-covered open/
    closed cases), so the release refuses rather than guessing either
    outcome."""
    merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")
    _stub_issue_reference(monkeypatch, {WORK_ITEM_ISSUE: (forge.ItemState.MISSING, "", "")})

    assert issue_claim.main(["--repo", REPOSITORY, "release", "72", "--merged", "12"]) == 2
    assert capsys.readouterr().err == (
        f"ERROR: work item #{WORK_ITEM_ISSUE} is missing, not closed\n"
    )
    assert store.fetch_state(worktree=Path("."), remote="origin").claims


def test_release_merged_refuses_a_non_numeric_pull_request_under_github(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`_github_pull_request_number`'s own guard (issue #359): `--merged`
    under `storage = "github"` (the default here -- no board.toml pins
    state-ref) takes only a bare pull request number, never the sha grammar
    that pin's own `<sha|empty>` form reads; refuses before touching git,
    the store, or the forge at all -- so the store this session never
    resolved stays at its own untouched default."""
    status = issue_claim.main(
        ["release", "72", "--agent", "Codex Sol", "--claim-id", "claim-72", "--merged", "sha123"]
    )

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: --merged requires a pull request number under storage = github\n"
    )
    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


@pytest.mark.parametrize(
    "args",
    [
        pytest.param(("72", "--agent", "Other"), id="mismatched-claimant"),
        pytest.param(("72", "--claim-id", "no-such-claim"), id="no-matching-claim"),
        pytest.param(("999", "--agent", "Ada"), id="no-claim-on-this-issue"),
    ],
)
def test_release_merged_unauthorized_makes_no_forge_call_close_or_comment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    args: tuple[str, ...],
) -> None:
    """Issue #359 R1: authorization gates every forge read and write on a
    `--merged` release -- a mismatched claimant, an explicit claim id that
    matches nothing, or an issue with no live claim at all never reaches the
    forge, so it can neither verify the pull request, comment on its issue,
    nor close it; the forge is never asked in the first place
    (`client.requests == 0`)."""
    client = merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")

    status = issue_claim.main(["--repo", REPOSITORY, "release", *args, "--merged", "12"])

    assert status == 2
    assert "ERROR:" in capsys.readouterr().err
    assert client.requests == 0
    assert client.closed_issues == set()
    assert client.landing_comments == {}
    assert store.fetch_state(worktree=Path("."), remote="origin").claims


def test_release_merged_close_failure_preserves_the_claim_and_prints_a_sentence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #359 R1: a close failure (a transient forge error, most often)
    runs before the release transition -- it leaves the claim exactly as
    live as it was, reported as the one sentence `main`'s own `ClaimError`
    handler already prints, never a partially released claim with a
    still-open, uncommented issue."""
    client = merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")

    def failing_close(number: int, *, pull_request: int) -> None:
        raise forge.ForgeTransientError("gh: connection reset")

    monkeypatch.setattr(client, "close_landed_item", failing_close)

    status = issue_claim.main(["--repo", REPOSITORY, "release", "72", "--merged", "12"])

    assert status == 2
    assert capsys.readouterr().err == "ERROR: gh: connection reset\n"
    assert store.fetch_state(worktree=Path("."), remote="origin").claims
    assert client.closed_issues == set()


def test_release_merged_retries_after_a_post_close_cas_failure_without_a_second_comment(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #359: unlike a close failure (above), a CAS failure on the
    release transition itself strikes *after* `close_landed_item` already
    ran -- the comment is posted and the issue closed on the forge before
    `store.commit_transition` ever raises. The retry's own
    `_verify_merged_release` now finds the work item already closed and
    returns no pending close (the same branch the replay-refusal proof
    exercises under `storage = state-ref`), so `close_landed_item` never
    runs a second time -- one comment total -- and the retried transition
    releases the claim."""
    client = merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")
    monkeypatch.setattr(issue_claim, "_fetch_issue_reference", _LIVE_FETCH_ISSUE_REFERENCE)
    close_calls: list[int] = []
    real_close_landed_item = client.close_landed_item

    def counting_close(number: int, *, pull_request: int) -> None:
        close_calls.append(number)
        real_close_landed_item(number, pull_request=pull_request)

    monkeypatch.setattr(client, "close_landed_item", counting_close)
    real_commit_transition = store.commit_transition
    attempts = 0

    def cas_failure_once(*args: object, **kwargs: object) -> protocol.ClaimState:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise protocol.ClaimUnavailableError(
                f"{store.STATE_REF} moved 5 times while retrying: another writer on "
                "origin keeps landing first; retry the command"
            )
        return real_commit_transition(*args, **kwargs)

    monkeypatch.setattr(store, "commit_transition", cas_failure_once)

    status = issue_claim.main(["--repo", REPOSITORY, "release", "72", "--merged", "12"])

    assert status == 2
    assert capsys.readouterr().err == (
        f"ERROR: {store.STATE_REF} moved 5 times while retrying: another writer on "
        "origin keeps landing first; retry the command\n"
    )
    assert close_calls == [WORK_ITEM_ISSUE]
    assert client.closed_issues == {WORK_ITEM_ISSUE}
    assert store.fetch_state(worktree=Path("."), remote="origin").claims

    retry_status = issue_claim.main(["--repo", REPOSITORY, "release", "72", "--merged", "12"])

    assert retry_status == 0
    assert close_calls == [WORK_ITEM_ISSUE]
    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


_CONTRADICTORY_TRAILERS = (
    pytest.param("Work-Item: #20\nNo-Item: docs", id="both"),
    pytest.param("No-Item: docs\nNo-Item: fix", id="repeated-no-item"),
)


def _contradictory_trailer_repository(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, trailer: str
) -> Path:
    """A real trunk repository (issue #359, LAND-60/61) whose one commit
    carries a contradictory trailer block: shared arrangement for both
    `check <sha>`'s and `release --merged <sha>`'s own refusal proofs, which
    read the identical classification."""
    monkeypatch.setattr(checkout, "trunk_landings", _LIVE_TRUNK_LANDINGS)
    repo, _remote = _real_repository_with_bare_remote(tmp_path)
    (repo / "work.txt").write_text("work\n")
    _real_git(repo, "add", "work.txt")
    _real_git(repo, "commit", "-q", "-m", "contradictory landing", "-m", trailer)
    _push_repository_trunk(repo, "origin")
    monkeypatch.chdir(repo)
    return repo


@pytest.mark.parametrize("trailer", _CONTRADICTORY_TRAILERS)
def test_check_sha_refuses_a_contradictory_trailer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    trailer: str,
) -> None:
    """Issue #359, LAND-60: a trunk commit whose trailer block names both
    `Work-Item:` and `No-Item:`, or repeats `No-Item:`, refuses through
    `check <sha>` by the classification's own defect message -- never
    letting `Work-Item:` win by ordering, and never silently landing
    nothing the way `aco board`'s own trunk-trailer reading does (LAND-42).
    The `--json` form carries the same refusal through the one envelope,
    its `message` the line's own finding (issue #435)."""
    repo = _contradictory_trailer_repository(monkeypatch, tmp_path, trailer)
    sha = _real_git(repo, "rev-parse", "main").stdout.strip()

    status = issue_claim.main(["check", sha])
    printed = capsys.readouterr()
    json_status = issue_claim.main(["check", sha, "--json"])
    envelope = json.loads(capsys.readouterr().out)

    assert (status, json_status) == (2, 2)
    assert printed.err.startswith(f"REFUSED: {sha} carries")
    assert "one is required" not in printed.err
    finding = printed.err.removeprefix(f"REFUSED: {sha} ").strip()
    assert envelope == _expected_trunk_envelope(sha, "invalid_classification", finding)


@pytest.mark.parametrize("trailer", _CONTRADICTORY_TRAILERS)
def test_release_merged_by_sha_refuses_a_contradictory_trailer(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    trailer: str,
) -> None:
    """Issue #359, LAND-61/CI: `release --merged <sha>` under state-ref
    reads the exact same classification `check <sha>` does (LAND-60) and
    refuses the same contradictory trailer by the same defect sentence,
    exit `2`, before any write -- `_landed_commit_by_sha`'s own
    `ClassificationDefect` branch."""
    _write_state_ref_pin(tmp_path)
    repo = _contradictory_trailer_repository(monkeypatch, tmp_path, trailer)
    sha = _real_git(repo, "rev-parse", "main").stdout.strip()

    status = issue_claim.main(["release", "20", "--agent", "Codex Sol", "--merged", sha])

    assert status == 2
    err = capsys.readouterr().err
    assert err.startswith(f"ERROR: {sha} carries")


def test_release_merged_empty_refuses_when_no_trunk_commit_names_the_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #359, LAND-47/CI: the empty `--merged` form under state-ref
    refuses by name when no trunk commit's own trailer names this item at
    all -- `_newest_landed_commit`'s own raise, reached before any claim or
    forge read, so every standing claim `_landing_scenario` seeded is still
    exactly as live afterward."""
    _landing_scenario(monkeypatch, tmp_path)

    status = issue_claim.main(["release", "99", "--agent", "Codex Sol", "--merged"])

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: no trunk commit carries a Work-Item: trailer naming #99\n"
    )
    remaining = store.fetch_state(worktree=Path("."), remote="origin").claims
    assert len(remaining) == len(_LANDING_ITEM_NUMBERS)


_CLEANUP_BRANCH = "codex/issue-72-cleanup"
_CLEANUP_WORKTREE_NAME = "issue-72-cleanup"


def _release_cleanup_repository(tmp_path: Path, *, merge_into_main: bool = True) -> Path:
    """A real bare-remote-backed repository (issue #322) with `_CLEANUP_BRANCH`
    diverged from `main` by one commit, merged into it through a real merge
    commit unless `merge_into_main` is `False` -- `release --merged`'s own
    cleanup walks real worktrees and a real merge-base ancestry check,
    unlike every other release test in this module, which never builds real
    git state at all."""
    repo, _remote = _real_repository_with_bare_remote(tmp_path)
    (repo / "base.txt").write_text("base\n")
    _real_git(repo, "add", "base.txt")
    _real_git(repo, "commit", "-q", "-m", "initial")
    _real_git(repo, "checkout", "-q", "-b", _CLEANUP_BRANCH)
    (repo / "feature.txt").write_text("feature\n")
    _real_git(repo, "add", "feature.txt")
    _real_git(repo, "commit", "-q", "-m", "feature work")
    _real_git(repo, "checkout", "-q", "main")
    if merge_into_main:
        _real_git(repo, "merge", "-q", "--no-ff", "-m", "Merge feature", _CLEANUP_BRANCH)
    _push_repository_trunk(repo, "origin")
    return repo


def _release_cleanup_scenario(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    merge_into_main: bool = True,
    link_worktree: bool = True,
) -> Path:
    """`repo`'s own would-be lane worktree path -- linked to `_CLEANUP_BRANCH`
    unless `link_worktree` is `False` -- a claimed issue #72 on that branch,
    and a merged, closing pull request #12 ready for `release --merged` to
    verify."""
    repo = _release_cleanup_repository(tmp_path, merge_into_main=merge_into_main)
    worktree = repo.parent / f"{repo.name}-worktrees" / _CLEANUP_WORKTREE_NAME
    if link_worktree:
        worktree.parent.mkdir(parents=True)
        _real_git(repo, "worktree", "add", "-q", str(worktree), _CLEANUP_BRANCH)
    standing = request(
        "landing", "Ada", issue=WORK_ITEM_ISSUE, branch=_CLEANUP_BRANCH, scope=("src",)
    )
    client = FakeForge()
    client.landings[12] = landing_pull_request(
        body=f"Work-Item: #{WORK_ITEM_ISSUE}\n\nCloses #{WORK_ITEM_ISSUE}",
        merged=True,
        head_ref_name=_CLEANUP_BRANCH,
        merge_commit=MERGE_COMMIT_SHA,
    )
    client.closed_issues.add(WORK_ITEM_ISSUE)
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(issue_claim, "_fetch_issue_reference", _LIVE_FETCH_ISSUE_REFERENCE)
    # This module's own worktree/branch cleanup mechanics (issue #322), not
    # the merge-commit trailer authority (issue #397): every scenario below
    # -- including one that never actually merges `_CLEANUP_BRANCH` into
    # `main` -- stubs the walked trunk to authorize closing #72 regardless.
    monkeypatch.setattr(
        checkout,
        "trunk_landings",
        lambda *_args, **_kwargs: (
            _trunk_landing(MERGE_COMMIT_SHA, board.TrunkWorkItemClassification((WORK_ITEM_ISSUE,))),
        ),
    )
    _patch_store_write(monkeypatch, _store_claim_from_request(standing))
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Ada"})
    _redirect_toplevel(monkeypatch, repo)
    monkeypatch.chdir(repo)
    return worktree


def test_release_merged_removes_a_clean_merged_lane_worktree_and_branch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    worktree = _release_cleanup_scenario(monkeypatch, tmp_path)

    assert issue_claim.main(["--repo", REPOSITORY, "release", "72", "--merged", "12"]) == 0

    assert not worktree.exists()
    assert checkout.branch_exists(_CLEANUP_BRANCH) is False
    assert "worktree: removed\n" in capsys.readouterr().out


def test_release_merged_json_carries_the_worktree_cleanup_outcome(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _release_cleanup_scenario(monkeypatch, tmp_path)

    status = issue_claim.main(["--repo", REPOSITORY, "release", "72", "--merged", "12", "--json"])

    assert status == 0
    assert json.loads(capsys.readouterr().out)["worktree"] == "removed"


def test_release_merged_removes_the_worktree_and_reports_the_branch_kept(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #322 review/gate finding 4: a branch-deletion failure after the
    worktree is already gone must never read as a bare `kept`."""
    worktree = _release_cleanup_scenario(monkeypatch, tmp_path)
    _stub_one_git_call(
        monkeypatch,
        ["branch", "-d", _CLEANUP_BRANCH],
        exit_status=1,
        stderr="error: branch not fully merged",
    )

    status = issue_claim.main(["--repo", REPOSITORY, "release", "72", "--merged", "12"])

    assert status == 0
    assert not worktree.exists()
    assert checkout.branch_exists(_CLEANUP_BRANCH) is True
    out = capsys.readouterr().out
    assert "worktree: removed; branch kept -- git failure: error: branch not fully merged\n" in out


def test_release_merged_json_carries_the_removed_worktree_branch_kept_outcome(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _release_cleanup_scenario(monkeypatch, tmp_path)
    _stub_one_git_call(
        monkeypatch,
        ["branch", "-d", _CLEANUP_BRANCH],
        exit_status=1,
        stderr="error: branch not fully merged",
    )

    status = issue_claim.main(["--repo", REPOSITORY, "release", "72", "--merged", "12", "--json"])

    assert status == 0
    assert json.loads(capsys.readouterr().out)["worktree"] == (
        "removed; branch kept -- git failure: error: branch not fully merged"
    )


def _dirty_worktree_scenario(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    worktree = _release_cleanup_scenario(monkeypatch, tmp_path)
    (worktree / "scratch.txt").write_text("uncommitted\n")
    return worktree


def _ran_from_inside_scenario(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    worktree = _release_cleanup_scenario(monkeypatch, tmp_path)
    _redirect_toplevel(monkeypatch, worktree)
    monkeypatch.chdir(worktree)
    return worktree


def _no_linked_worktree_scenario(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path | None:
    _release_cleanup_scenario(monkeypatch, tmp_path, link_worktree=False)
    return None


def _not_merged_locally_scenario(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    return _release_cleanup_scenario(monkeypatch, tmp_path, merge_into_main=False)


def _checked_out_elsewhere_scenario(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Issue #322 review finding 4: the lane branch is checked out on the
    repository's own shared main checkout -- reachable when `release` runs
    from a different linked worktree entirely -- rather than in a disposable
    linked worktree; cleanup declines rather than trying (and failing) to
    remove the main checkout as if it were one."""
    repo = _release_cleanup_repository(tmp_path)
    _real_git(repo, "checkout", "-q", _CLEANUP_BRANCH)
    bystander = repo.parent / f"{repo.name}-worktrees" / "issue-1-bystander"
    bystander.parent.mkdir(parents=True)
    _real_git(repo, "worktree", "add", "-q", str(bystander), "-b", "codex/issue-1-bystander")
    standing = request(
        "landing", "Ada", issue=WORK_ITEM_ISSUE, branch=_CLEANUP_BRANCH, scope=("src",)
    )
    client = FakeForge()
    client.landings[12] = landing_pull_request(
        body=f"Work-Item: #{WORK_ITEM_ISSUE}\n\nCloses #{WORK_ITEM_ISSUE}",
        merged=True,
        head_ref_name=_CLEANUP_BRANCH,
        merge_commit=MERGE_COMMIT_SHA,
    )
    client.closed_issues.add(WORK_ITEM_ISSUE)
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(issue_claim, "_fetch_issue_reference", _LIVE_FETCH_ISSUE_REFERENCE)
    monkeypatch.setattr(
        checkout,
        "trunk_landings",
        lambda *_args, **_kwargs: (
            _trunk_landing(MERGE_COMMIT_SHA, board.TrunkWorkItemClassification((WORK_ITEM_ISSUE,))),
        ),
    )
    _patch_store_write(monkeypatch, _store_claim_from_request(standing))
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Ada"})
    _redirect_toplevel(monkeypatch, bystander)
    monkeypatch.chdir(bystander)


def _git_failure_scenario(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Issue #322 review/gate finding: a git failure resolving the lane's
    own worktrees must surface as `kept -- git failure: ...`, never as `no
    linked worktree found`."""
    worktree = _release_cleanup_scenario(monkeypatch, tmp_path)
    _stub_one_git_call(
        monkeypatch,
        [
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-dir",
            "--git-common-dir",
        ],
        exit_status=128,
        stderr="fatal: cannot change to 'gone': No such file or directory",
    )
    return worktree


_WORKTREE_CLEANUP_KEPT_SCENARIOS: tuple[
    tuple[str, Callable[[pytest.MonkeyPatch, Path], Path | None], tuple[str, ...], str], ...
] = (
    (
        "keep-worktree-flag",
        _release_cleanup_scenario,
        ("--keep-worktree",),
        "--keep-worktree was given",
    ),
    ("dirty", _dirty_worktree_scenario, (), "dirty"),
    ("ran-from-inside", _ran_from_inside_scenario, (), "release ran from inside it"),
    ("no-linked-worktree", _no_linked_worktree_scenario, (), "no linked worktree found"),
    (
        "not-merged-locally",
        _not_merged_locally_scenario,
        (),
        "not merged into the default branch",
    ),
    ("checked-out-elsewhere", _checked_out_elsewhere_scenario, (), "branch checked out elsewhere"),
    (
        "git-failure",
        _git_failure_scenario,
        (),
        "git failure: fatal: cannot change to 'gone': No such file or directory",
    ),
)


@pytest.mark.parametrize("as_json", [False, True], ids=["text", "json"])
@pytest.mark.parametrize(
    ("configure", "extra_args", "reason"),
    [entry[1:] for entry in _WORKTREE_CLEANUP_KEPT_SCENARIOS],
    ids=[entry[0] for entry in _WORKTREE_CLEANUP_KEPT_SCENARIOS],
)
def test_release_merged_keeps_the_worktree_for_every_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    configure: Callable[[pytest.MonkeyPatch, Path], Path | None],
    extra_args: tuple[str, ...],
    reason: str,
    as_json: bool,
) -> None:
    """Issue #322 review/gate finding 4: text and `--json` proof for every
    reason cleanup keeps the worktree instead of removing it, parametrized
    over one fixture rather than one near-identical test per reason."""
    worktree = configure(monkeypatch, tmp_path)
    arguments = ["--repo", REPOSITORY, "release", "72", "--merged", "12", *extra_args]
    if as_json:
        arguments.append("--json")

    status = issue_claim.main(arguments)

    assert status == 0
    assert checkout.branch_exists(_CLEANUP_BRANCH) is True
    output = capsys.readouterr().out
    if as_json:
        assert json.loads(output)["worktree"] == f"kept -- {reason}"
    else:
        assert f"worktree: kept -- {reason}\n" in output
    if worktree is not None:
        assert worktree.exists()


def _land_readiness(
    number: int = 12,
    *,
    checks: tuple[forge.CheckRun, ...] = (forge.CheckRun("ci", forge.CHECK_CONCLUSION_SUCCESS),),
) -> forge.LandingReadiness:
    return forge.LandingReadiness(
        number, True, MERGE_COMMIT_SHA, forge.MERGEABLE_STATE_CLEAN, checks
    )


def _land_preflight_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    readiness: forge.LandingReadiness,
    body: str = f"Work-Item: #{WORK_ITEM_ISSUE}\n\nCloses #{WORK_ITEM_ISSUE}",
    item_closed: bool = False,
    claimed: bool = True,
    claim_agent: str = "Ada",
) -> FakeForge:
    """A session `aco land 12` can preflight-refuse against, with no real
    git at all (issue #405): every scenario here fails before the checkout
    is ever consulted, unlike `_land_scenario`'s real-git happy path.
    `claimed=False` leaves the item with no live claim at all -- the other
    half of LANDCMD-08's own ordering proof, alongside `item_closed`.
    `claim_agent`, when it differs from `_patch_release_session`'s own
    default session identity `Ada`, is the claim/parent/closing check's
    existence proof standing beside a foreign claimant this session is not
    authorized to land (issue #405 point 7)."""
    standing = (
        (
            request(
                "landing", claim_agent, issue=WORK_ITEM_ISSUE, branch=LANDING_BRANCH, scope=("src",)
            ),
        )
        if claimed
        else ()
    )
    client = FakeForge()
    client.landings[12] = landing_pull_request(
        body=body, merged=False, head_ref_name=LANDING_BRANCH
    )
    client.readiness_by_number[12] = readiness
    if item_closed:
        client.closed_issues.add(WORK_ITEM_ISSUE)
    _patch_release_session(monkeypatch, client, *standing, branch=LANDING_BRANCH)
    return client


def _land_repository(
    tmp_path: Path, *, set_head: bool = True, branch: str = LANDING_BRANCH
) -> Path:
    """A real bare-remote-backed repository (issue #405) with `branch`
    diverged from `main` by one pushed commit and `main` itself checked out
    clean -- `aco land`'s own merge, branch deletion, and fast-forward run
    against real git here, the one proof a fake checkout cannot give.
    `set_head=False` skips recording `origin/HEAD`, the one precondition a
    test of the "default branch unknown" refusal needs missing. `branch`,
    when it names a lane branch instead of the default `LANDING_BRANCH`,
    stands the real checkout an issue-less (`No-Item:`) land proves against
    (issue #405 CI coverage follow-up)."""
    repo, _remote = _real_repository_with_bare_remote(tmp_path)
    (repo / "base.txt").write_text("base\n")
    _real_git(repo, "add", "base.txt")
    _real_git(repo, "commit", "-q", "-m", "initial")
    if set_head:
        _push_repository_trunk(repo, "origin")
    else:
        _real_git(repo, "push", "-q", "origin", "main")
    _real_git(repo, "checkout", "-q", "-b", branch)
    (repo / "feature.txt").write_text("feature\n")
    _real_git(repo, "add", "feature.txt")
    _real_git(repo, "commit", "-q", "-m", "feature work")
    _real_git(repo, "push", "-q", "origin", branch)
    _real_git(repo, "checkout", "-q", "main")
    return repo


def _land_scenario(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    set_head: bool = True,
    claim_agent: str = "Ada",
    branch: str = LANDING_BRANCH,
    issue: int | None = WORK_ITEM_ISSUE,
    body: str | None = None,
) -> tuple[Path, FakeForge]:
    """`claim_agent`, when it differs from the `Ada` session identity set
    below, stands the same foreign claim `_land_preflight_client`'s own
    `claim_agent` proves against a fake reader (issue #405 round-4 finding
    5), but here against `land`'s real merge/checkout path, so a
    `--coordinator-override --role coordinator` land of it can be proven
    reaching the merge, not just proven refused without those flags.
    `branch`/`issue`/`body`, when they name an issue-less lane instead of
    the default `Work-Item:` scenario, stand `_land_preflight`'s own
    `LaneIdentity` branch (issue #405 CI coverage follow-up)."""
    repo = _land_repository(tmp_path, set_head=set_head, branch=branch)
    standing = request("landing", claim_agent, issue=issue, branch=branch, scope=("src",))
    client = FakeForge()
    client.landings[12] = landing_pull_request(
        body=body or f"Work-Item: #{WORK_ITEM_ISSUE}\n\nCloses #{WORK_ITEM_ISSUE}",
        merged=False,
        head_ref_name=branch,
    )
    client.readiness_by_number[12] = _land_readiness()
    client.merge_repository = repo
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "trunk_landings", _LIVE_TRUNK_LANDINGS)
    _patch_store_write(monkeypatch, _store_claim_from_request(standing))
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Ada"})
    _redirect_toplevel(monkeypatch, repo)
    monkeypatch.chdir(repo)
    return repo, client


@pytest.mark.parametrize(
    ("readiness", "reason"),
    [
        pytest.param(
            forge.LandingReadiness(12, False, MERGE_COMMIT_SHA, forge.MERGEABLE_STATE_CLEAN, ()),
            "pull request #12 is not open; it cannot be landed",
            id="not-open",
        ),
        pytest.param(
            forge.LandingReadiness(12, True, MERGE_COMMIT_SHA, "dirty", ()),
            "pull request #12 is not mergeable (dirty)",
            id="not-mergeable",
        ),
        pytest.param(
            _land_readiness(checks=()),
            "pull request #12 exposes no CI checks; cannot verify green CI",
            id="no-checks",
        ),
        pytest.param(
            _land_readiness(checks=(forge.CheckRun("build", None),)),
            "pull request #12 has checks still running: build; wait for every check to succeed",
            id="check-running",
        ),
        pytest.param(
            _land_readiness(checks=(forge.CheckRun("build", "failure"),)),
            "pull request #12 has non-successful checks: build (failure); "
            "land only after every check succeeds",
            id="check-failed",
        ),
        pytest.param(
            _land_readiness(
                checks=tuple(forge.CheckRun(f"check-{index}", None) for index in range(5))
            ),
            "pull request #12 has checks still running: check-0, check-1, check-2, and 2 more; "
            "wait for every check to succeed",
            id="check-running-name-list-capped",
        ),
        pytest.param(
            _land_readiness(checks=(forge.CheckRun("x" * 300, None),)),
            "pull request #12 has checks still running: "
            + ("x" * 39 + "…")
            + "; wait for every check to succeed",
            id="check-running-name-truncated",
        ),
        pytest.param(
            _land_readiness(checks=tuple(forge.CheckRun("x" * 300, None) for _ in range(5))),
            "pull request #12 has checks still running: "
            + ", ".join([("x" * 39 + "…")] * 3)
            + ", and 2 more; wait for ev…",
            id="check-running-many-long-names-bounded-with-error-prefix",
        ),
    ],
)
def test_land_refuses_every_readiness_defect_before_any_write(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    readiness: forge.LandingReadiness,
    reason: str,
) -> None:
    """Issue #405 Beweis 1: every readiness-based preflight refusal merges
    nothing, deletes no branch, and reaches no store write -- `aco land`'s
    own read-only order. The printed line (issue #405 round-4 finding) never
    exceeds 200 characters including `main`'s own `ERROR: ` prefix, not just
    the sentence the refusal builds before that prefix is added."""
    client = _land_preflight_client(monkeypatch, readiness=readiness)

    assert issue_claim.main(["--repo", REPOSITORY, "land", "12"]) == 2

    error_line = capsys.readouterr().err
    assert error_line == f"ERROR: {reason}\n"
    assert len(error_line.rstrip("\n")) <= issue_claim.LAND_REFUSAL_LINE_LENGTH_LIMIT
    assert client.merge_calls == []
    assert client.deleted_branches == []


def test_land_refuses_a_classification_defect_reusing_checks_own_rules(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #405: once every check succeeds, `check <pr>`'s own
    classification rules apply unchanged -- an unclassified body refuses
    the same sentence `check` would, before any write."""
    client = _land_preflight_client(monkeypatch, readiness=_land_readiness(), body="Advances #72")

    assert issue_claim.main(["--repo", REPOSITORY, "land", "12"]) == 2

    assert capsys.readouterr().err == (
        "ERROR: pull request #12 carries no `Work-Item:` or `No-Item:` line\n"
    )
    assert client.merge_calls == []


def test_land_refuses_a_closed_work_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #405: a classified work item that is not open refuses by name,
    before any write -- distinct from `check`'s own rules, which never
    verify the item's live state."""
    monkeypatch.setattr(issue_claim, "_fetch_issue_reference", _LIVE_FETCH_ISSUE_REFERENCE)
    client = _land_preflight_client(monkeypatch, readiness=_land_readiness(), item_closed=True)

    assert issue_claim.main(["--repo", REPOSITORY, "land", "12"]) == 2

    assert capsys.readouterr().err == (
        f"ERROR: work item #{WORK_ITEM_ISSUE} is not open; it cannot be landed\n"
    )
    assert client.merge_calls == []


def test_land_refuses_a_closed_work_item_before_its_own_missing_claim(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #405 review/gate finding: LANDCMD-08 (item not open) is checked
    before claim validation -- a closed item with no live claim at all
    refuses by its own closed state, never the claim it also lacks, and
    never reads the store's claims to find out."""
    monkeypatch.setattr(issue_claim, "_fetch_issue_reference", _LIVE_FETCH_ISSUE_REFERENCE)
    client = _land_preflight_client(
        monkeypatch, readiness=_land_readiness(), item_closed=True, claimed=False
    )

    assert issue_claim.main(["--repo", REPOSITORY, "land", "12"]) == 2

    assert capsys.readouterr().err == (
        f"ERROR: work item #{WORK_ITEM_ISSUE} is not open; it cannot be landed\n"
    )
    assert client.merge_calls == []


def test_land_refuses_a_classification_defect_from_a_missing_claim(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #405 CI coverage follow-up: `_classification_defect`'s own
    claim-check branch inside `_land_preflight` -- distinct from
    `test_land_refuses_a_classification_defect_reusing_checks_own_rules`'s
    shape defect and `test_land_refuses_a_closed_work_item_before_its_own_
    missing_claim`'s LANDCMD-08-first ordering -- an *open* work item with
    no live claim at all refuses by its own missing claim."""
    client = _land_preflight_client(monkeypatch, readiness=_land_readiness(), claimed=False)

    assert issue_claim.main(["--repo", REPOSITORY, "land", "12"]) == 2

    assert capsys.readouterr().err == (
        f"ERROR: pull request #12 has no active claim for #{WORK_ITEM_ISSUE} "
        f"on branch {LANDING_BRANCH!r}\n"
    )
    assert client.merge_calls == []


def test_land_refuses_a_foreign_claim_before_the_merge(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #405 point 7 review/gate finding: `_land_preflight` itself
    authorizes this session against the live claim, reusing `release`'s own
    claimant/coordinator-override check (`_resolve_release_claimant`) --
    a claim held by another agent refuses before the merge, not only once
    the delegated `release --merged` step runs after it."""
    client = _land_preflight_client(monkeypatch, readiness=_land_readiness(), claim_agent="Grok")

    assert issue_claim.main(["--repo", REPOSITORY, "land", "12"]) == 2

    assert capsys.readouterr().err == (
        "ERROR: only the original claimant may release; use an explicit coordinator "
        "override (holder='Grok (builder)', this session='Ada (builder)')\n"
    )
    assert client.merge_calls == []


def test_land_refuses_a_coordinator_override_with_no_role_before_the_merge(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #405 point 8 review finding (LANDCMD-19): `--coordinator-override`
    with no `--role` refuses via `_land_preflight`'s own call to `release`'s
    `protocol._require_coordinator_override`, before any merge."""
    client = _land_preflight_client(monkeypatch, readiness=_land_readiness())

    assert issue_claim.main(["--repo", REPOSITORY, "land", "12", "--coordinator-override"]) == 2

    assert capsys.readouterr().err == "ERROR: a coordinator override requires --role coordinator\n"
    assert client.merge_calls == []


def test_land_refuses_a_coordinator_override_with_the_wrong_role_before_the_merge(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #405 point 8 review finding (LANDCMD-19): `--coordinator-override
    --role builder` refuses the same way as an omitted `--role` -- only
    `--role coordinator` behind the override authorizes it."""
    client = _land_preflight_client(monkeypatch, readiness=_land_readiness())

    assert (
        issue_claim.main(
            ["--repo", REPOSITORY, "land", "12", "--coordinator-override", "--role", "builder"]
        )
        == 2
    )

    assert capsys.readouterr().err == "ERROR: a coordinator override requires --role coordinator\n"
    assert client.merge_calls == []


def test_land_merges_a_green_pull_request_and_runs_the_release_path(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #405 Beweis 1: a green pull request merges with a pinned head
    sha and a self-composed commit message whose classification trailer is
    its own last paragraph, the remote branch delete request is made, this
    checkout's own `main` fast-forwards to the fresh merge commit, and the
    existing `release --merged` path closes the item and frees the claim."""
    repo, client = _land_scenario(monkeypatch, tmp_path)
    trunk_before = _real_git(repo, "rev-parse", "main").stdout.strip()

    assert issue_claim.main(["--repo", REPOSITORY, "land", "12"]) == 0

    [(number, head_sha, _title, body)] = client.merge_calls
    assert (number, head_sha) == (12, MERGE_COMMIT_SHA)
    paragraphs = body.strip().split("\n\n")
    assert paragraphs[-1] == f"Work-Item: #{WORK_ITEM_ISSUE}"
    assert client.deleted_branches == [LANDING_BRANCH]
    assert client.closed_issues == {WORK_ITEM_ISSUE}
    trunk_after = _real_git(repo, "rev-parse", "main").stdout.strip()
    assert trunk_after != trunk_before
    assert client.landings[12].merge_commit == trunk_after
    out = capsys.readouterr().out
    assert "freed:" in out
    assert "next:" in out


def test_land_merges_an_issueless_lane_pull_request(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #405 CI coverage follow-up: `_land_preflight`'s own
    `LaneIdentity` branch -- an issue-less, `No-Item:` pull request -- merges
    through `land`'s real path exactly as a `Work-Item:` one does
    (`test_land_merges_a_green_pull_request_and_runs_the_release_path`
    proves the work-item branch): no item to close, but the claim releases."""
    _repo, client = _land_scenario(
        monkeypatch, tmp_path, branch=LANE_BRANCH, issue=None, body="No-Item: docs"
    )

    assert issue_claim.main(["--repo", REPOSITORY, "land", "12"]) == 0

    [(number, head_sha, _title, body)] = client.merge_calls
    assert (number, head_sha) == (12, MERGE_COMMIT_SHA)
    assert body.strip().split("\n\n")[-1] == "No-Item: docs"
    assert client.deleted_branches == [LANE_BRANCH]
    assert client.closed_issues == set()
    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


def test_land_merges_a_foreign_claim_under_a_coordinator_override(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #405 round-4 finding 5: `--coordinator-override --role
    coordinator` against a claim held by another agent (`Grok`, not this
    session's own `Ada`) reaches the merge and completes exactly like an
    ordinary land -- `test_land_refuses_a_foreign_claim_before_the_merge`
    proves the same claim refused without those flags."""
    repo, client = _land_scenario(monkeypatch, tmp_path, claim_agent="Grok")

    status = issue_claim.main(
        ["--repo", REPOSITORY, "land", "12", "--coordinator-override", "--role", "coordinator"]
    )

    assert status == 0
    [(number, head_sha, _title, _body)] = client.merge_calls
    assert (number, head_sha) == (12, MERGE_COMMIT_SHA)
    assert client.landings[12].merge_commit == _real_git(repo, "rev-parse", "main").stdout.strip()


def _break_delete_branch(monkeypatch: pytest.MonkeyPatch, client: FakeForge) -> None:
    def failing(branch: str) -> None:
        raise ClaimError("delete branch failed (simulated)")

    monkeypatch.setattr(client, "delete_branch", failing)


def _break_fetch(monkeypatch: pytest.MonkeyPatch, _client: FakeForge) -> None:
    _stub_one_git_call(monkeypatch, ["fetch", "origin"], exit_status=1, stderr="fatal: unreachable")


def _break_fast_forward_merge(monkeypatch: pytest.MonkeyPatch, _client: FakeForge) -> None:
    _stub_one_git_call(
        monkeypatch,
        ["merge", "--ff-only", "origin/main"],
        exit_status=1,
        stderr="fatal: Not possible to fast-forward, aborting.",
    )


@pytest.mark.parametrize(
    ("arrange", "step"),
    [
        pytest.param(_break_delete_branch, "delete-branch", id="delete-branch"),
        pytest.param(_break_fetch, "fast-forward", id="fetch-fails"),
        pytest.param(_break_fast_forward_merge, "fast-forward", id="ff-only-fails"),
    ],
)
def test_land_reports_incomplete_follow_up_for_every_post_merge_step(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    arrange: Callable[[pytest.MonkeyPatch, FakeForge], None],
    step: str,
) -> None:
    """Issue #405: every step after a successful merge -- deleting the
    branch, fetching, or fast-forwarding -- names its own step in the one
    ruled recovery line, the merge itself never repeated."""
    _repo, client = _land_scenario(monkeypatch, tmp_path)
    arrange(monkeypatch, client)

    status = issue_claim.main(["--repo", REPOSITORY, "land", "12"])

    assert status == 2
    merge_commit = client.landings[12].merge_commit
    assert merge_commit is not None
    assert capsys.readouterr().err == (
        f"ERROR: MERGED pull request #12 as {merge_commit}; "
        f"follow-up incomplete: {step}; re-run aco land 12\n"
    )
    assert len(client.merge_calls) == 1


def _land_merged_pending_release(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> tuple[Path, FakeForge]:
    """A pull request `aco land` already merged once, its own delegated
    `release --merged` blocked by a failing close (issue #405 Beweis 2): the
    shared rerun setup both the plain recovery proof and the release-routing
    recovery proof resume from, `close_landed_item` restored to real once
    this returns so the caller's own rerun can succeed."""
    repo, client = _land_scenario(monkeypatch, tmp_path)
    real_close = client.close_landed_item

    def failing_close(number: int, *, pull_request: int) -> None:
        raise ClaimError("forge unreachable (simulated)")

    monkeypatch.setattr(client, "close_landed_item", failing_close)

    status = issue_claim.main(["--repo", REPOSITORY, "land", "12"])

    assert status == 2
    merge_commit = client.landings[12].merge_commit
    assert merge_commit is not None
    assert capsys.readouterr().err == (
        f"ERROR: MERGED pull request #12 as {merge_commit}; "
        "follow-up incomplete: release; re-run aco land 12\n"
    )
    assert len(client.merge_calls) == 1
    assert client.closed_issues == set()

    monkeypatch.setattr(client, "close_landed_item", real_close)
    return repo, client


def test_land_reports_incomplete_follow_up_and_a_rerun_resumes_without_a_second_merge(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #405 Beweis 2: a failure after the merge names the exact
    recovery line, never a second merge on rerun, and the rerun still closes
    the item and frees the claim."""
    _repo, client = _land_merged_pending_release(monkeypatch, capsys, tmp_path)

    assert issue_claim.main(["--repo", REPOSITORY, "land", "12"]) == 0

    assert len(client.merge_calls) == 1
    assert client.closed_issues == {WORK_ITEM_ISSUE}


def _land_mark_already_merged(client: FakeForge) -> None:
    """Simulate `land` having already merged pull request 12 in an earlier
    run (issue #405 round-4 finding 1): the fake forge's own landing record
    is the one signal `_cmd_land` reads to tell a rerun from a fresh run, so
    a rerun test never needs to actually run the merge first."""
    client.landings[12] = replace(client.landings[12], merged=True, merge_commit=MERGE_COMMIT_SHA)


@pytest.mark.parametrize(
    "override_arguments",
    [
        pytest.param(["--coordinator-override"], id="omitted-role"),
        pytest.param(["--coordinator-override", "--role", "builder"], id="wrong-role"),
    ],
)
def test_land_rerun_refuses_a_coordinator_override_with_no_valid_role_before_any_side_effect(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    override_arguments: list[str],
) -> None:
    """Issue #405 round-4 finding 1: an already-merged rerun skips
    `_land_preflight` entirely, so a coordinator-override role check placed
    only there would leave a rerun free to delete the branch, fast-forward,
    and let the delegated `release --merged` step close the item on a bare
    `--coordinator-override` with no coordinator role behind it.
    `_cmd_land`'s own entry validates this before the fresh/rerun split, so
    none of that runs."""
    repo, client = _land_scenario(monkeypatch, tmp_path)
    _land_mark_already_merged(client)
    trunk_before = _real_git(repo, "rev-parse", "main").stdout.strip()

    status = issue_claim.main(["--repo", REPOSITORY, "land", "12", *override_arguments])

    assert status == 2
    assert capsys.readouterr().err == "ERROR: a coordinator override requires --role coordinator\n"
    assert client.merge_calls == []
    assert client.deleted_branches == []
    assert client.closed_issues == set()
    assert _real_git(repo, "rev-parse", "main").stdout.strip() == trunk_before


def test_land_refuses_under_the_state_ref_pin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #405: `aco land` is a github-only command; a repository pinned
    to `storage = state-ref` has no pull requests to land, refused before
    any forge or store read."""
    _write_state_ref_pin(tmp_path)
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Ada"})

    assert issue_claim.main(["land", "12"]) == 2

    assert capsys.readouterr().err == (
        "ERROR: aco land is a github command; storage = state-ref has no pull requests to land\n"
    )


def test_land_reports_a_merge_conflict_when_the_pull_request_changed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #405: a 405/409 from the merge endpoint means the pull request
    changed since preflight read it -- refused by name, before any
    follow-up step, with the one recovery this refusal ever names: re-run."""
    _repo, client = _land_scenario(monkeypatch, tmp_path)
    client.fail_merge = forge.ForgeMergeConflictError("HTTP 409 head changed")

    assert issue_claim.main(["--repo", REPOSITORY, "land", "12"]) == 2

    assert capsys.readouterr().err == (
        "ERROR: pull request #12 changed while it was checked; re-run land\n"
    )
    assert len(client.merge_calls) == 1
    assert client.deleted_branches == []


def test_land_prints_the_reinstall_line_in_this_packages_own_repository(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #405: a successful landing in this very package's own
    repository ends with its own reinstall reminder; every other repository
    prints nothing further."""
    repo, _client = _land_scenario(monkeypatch, tmp_path)
    (repo / "pyproject.toml").write_text('[project]\nname = "agent-coordination"\n')
    _real_git(repo, "add", "pyproject.toml")
    _real_git(repo, "commit", "-q", "-m", "add pyproject.toml")
    _real_git(repo, "push", "-q", "origin", "main")

    assert issue_claim.main(["--repo", REPOSITORY, "land", "12"]) == 0

    assert capsys.readouterr().out.splitlines()[-1] == (
        "reinstall: uv tool install --force --from . agent-coordination"
    )


def test_land_refuses_a_dirty_checkout_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #405: `land` fast-forwards this exact checkout once it merges,
    so an uncommitted change here refuses before any write, never merged
    away or silently ignored."""
    repo, client = _land_scenario(monkeypatch, tmp_path)
    (repo / "untracked.txt").write_text("dirty\n")

    assert issue_claim.main(["--repo", REPOSITORY, "land", "12"]) == 2

    assert capsys.readouterr().err == (
        "ERROR: land must run from a clean checkout of the default branch 'main'\n"
    )
    assert client.merge_calls == []


def test_land_refuses_when_the_default_branch_is_unknown(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #405: a checkout whose `origin/HEAD` was never recorded denies
    outright rather than guessing, the same risk `rescope`'s own resolved
    checkout precondition refuses (issue #314 gate G4)."""
    _repo, client = _land_scenario(monkeypatch, tmp_path, set_head=False)

    assert issue_claim.main(["--repo", REPOSITORY, "land", "12"]) == 2

    assert capsys.readouterr().err == f"ERROR: {checkout.DEFAULT_BRANCH_UNKNOWN_REASON}\n"
    assert client.merge_calls == []


def test_land_trunk_trailer_renders_the_trunk_grammar_for_both_classifications() -> None:
    """Issue #405: a trunk trailer is always local (`Work-Item: #<n>`,
    never the pull request body's qualified `owner/repo#n` form); a
    `No-Item:` classification renders unchanged either way."""
    work_item = board.WorkItemClassification(board.IssueReference(REPOSITORY, 72))
    no_item = board.NoItemClassification(board.NoItemKind.DOCS)

    assert issue_claim._land_trunk_trailer(work_item) == "Work-Item: #72"
    assert issue_claim._land_trunk_trailer(no_item) == "No-Item: docs"


def test_land_merge_body_composes_the_trailer_as_its_own_last_paragraph() -> None:
    """Issue #405, Befund 42 on #310: the classification line is removed
    from wherever the body put it and reappears as the message's own final
    paragraph; a body with nothing left over is the trailer alone."""
    no_item = board.NoItemClassification(board.NoItemKind.FIX)

    with_prose = issue_claim._land_merge_body("Tidies the README.\n\nNo-Item: fix\n", no_item)
    assert with_prose == "Tidies the README.\n\nNo-Item: fix\n"

    bare = issue_claim._land_merge_body("No-Item: fix\n", no_item)
    assert bare == "No-Item: fix\n"


def test_land_release_routing_reuses_the_verified_classification_for_a_fresh_merge() -> None:
    """Issue #405 point 4: a fresh merge routes `release --merged` straight
    from the classification this same run's own preflight already verified
    -- never a read of anything, pull request body included."""
    work_item = board.WorkItemClassification(board.IssueReference(REPOSITORY, 72))
    no_item = board.NoItemClassification(board.NoItemKind.DOCS)

    assert issue_claim._land_release_routing(work_item, MERGE_COMMIT_SHA, REPOSITORY) == 72
    assert issue_claim._land_release_routing(no_item, MERGE_COMMIT_SHA, REPOSITORY) is None


def test_land_release_routing_reads_the_merge_commit_trailer_for_a_rerun(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #405 point 4: `classification=None` (a rerun) reads the walked
    trunk's own trailer instead, routing a `No-Item:` merge commit to no
    issue -- the same recovery `test_land_rerun_recovers_release_routing_
    after_the_body_changed` proves end to end for a `Work-Item:` one."""
    landing = checkout.TrunkLanding(
        MERGE_COMMIT_SHA, datetime.now(UTC), board.NoItemClassification(board.NoItemKind.FIX)
    )
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: (landing,))

    assert issue_claim._land_release_routing(None, MERGE_COMMIT_SHA, REPOSITORY) is None


def test_land_release_routing_refuses_a_rerun_with_no_usable_merge_commit_trailer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #405 point 4: a rerun's own merge-commit trailer read refuses
    by name when the walked trunk carries the sha with neither a
    `Work-Item:` nor a `No-Item:` trailer -- never a bare re-read of the
    pull request's own body."""
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())

    with pytest.raises(ClaimError, match="carries no `Work-Item:` or `No-Item:` trailer"):
        issue_claim._land_release_routing(None, MERGE_COMMIT_SHA, REPOSITORY)


def test_land_rerun_recovers_release_routing_after_the_body_changed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #405 point 4 (Befund 42 on #310's own risk): once merged,
    `_land_release_routing` never re-reads the pull request's own mutable
    body -- a fixer editing it away after the merge still leaves a rerun
    able to recover the exact item the merge commit's own trailer names,
    read exactly as `_verify_merged_release` reads it (issue #397, Befund
    41)."""
    _repo, client = _land_merged_pending_release(monkeypatch, capsys, tmp_path)

    # A fixer edits the merged pull request's own body afterward (Befund 41):
    # its classification line is gone, but the merge commit's own trailer
    # `aco land` composed at merge time is untouched.
    client.landings[12] = replace(client.landings[12], body="Advances #72")

    assert issue_claim.main(["--repo", REPOSITORY, "land", "12"]) == 0

    assert len(client.merge_calls) == 1
    assert client.closed_issues == {WORK_ITEM_ISSUE}


@pytest.mark.parametrize(
    ("project_toml", "expected"),
    [
        pytest.param('[project]\nname = "agent-coordination"\n', True, id="this-package"),
        pytest.param('[project]\nname = "other-package"\n', False, id="another-package"),
        pytest.param(None, False, id="no-pyproject-toml"),
    ],
)
def test_land_is_own_repository(tmp_path: Path, project_toml: str | None, expected: bool) -> None:
    if project_toml is not None:
        (tmp_path / "pyproject.toml").write_text(project_toml)

    assert issue_claim._land_is_own_repository(tmp_path) is expected


def test_release_abandoned_records_why_the_lane_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")

    assert (
        issue_claim.main(["--repo", REPOSITORY, "release", "72", "--abandoned", "overtaken by #80"])
        == 0
    )

    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


def test_release_abandoned_prints_no_freed_or_next_lines(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """An abandoned release never resolves the forge (issue #245), so it has
    nothing to report a landing freed (issue #256): `RELEASED` stands alone."""
    merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")

    exit_code = issue_claim.main(["--repo", REPOSITORY, "release", "72", "--abandoned", "stopped"])

    assert exit_code == 0
    assert capsys.readouterr().out == f"RELEASED issue #{WORK_ITEM_ISSUE}: landing\n"


@pytest.mark.parametrize(
    "outcome_flags",
    [
        pytest.param(("--abandoned", "stopped"), id="abandoned"),
        pytest.param(("--merged", "12"), id="merged"),
    ],
)
def test_release_branch_selects_a_lane_claim_without_checking_out_that_branch(
    monkeypatch: pytest.MonkeyPatch, outcome_flags: tuple[str, str]
) -> None:
    """`--branch` selects the same lane claim `claim --branch` would (issue
    #250), but -- unlike `claim`'s own `--branch` -- never inspects the
    checkout branch at all: `forbid_git` fails the test the moment anything
    but `rev-parse --show-toplevel` reaches git, so a deleted or foreign
    worktree can never block this release."""
    standing = request("mine", "Ada", issue=None, branch=LANE_BRANCH, scope=("docs",))
    client = FakeForge()
    if outcome_flags[0] == "--merged":
        client.landings[12] = landing_pull_request(
            body="No-Item: docs", merged=True, base_ref_name="main", head_ref_name=LANE_BRANCH
        )
        monkeypatch.setattr(
            checkout,
            "trunk_landings",
            lambda *_args, **_kwargs: (
                _trunk_landing(MERGE_COMMIT_SHA, board.NoItemClassification(board.NoItemKind.DOCS)),
            ),
        )
    _patch_release_session(monkeypatch, client, standing, forbid_git=True)

    released = issue_claim.main(
        ["--repo", REPOSITORY, "release", "--branch", LANE_BRANCH, *outcome_flags]
    )

    assert released == 0
    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


def test_release_branch_selects_a_coordinator_override_from_another_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The headline scenario (issue #250): naming the lane's own `--branch`
    alongside `--claim-id` for a coordinator-override abandon works from any
    checkout, not only one on the lane branch -- `forbid_git` fails the test
    the moment anything but `rev-parse --show-toplevel` reaches git, so a
    checkout left on another branch entirely can never block this release."""
    standing = request(
        "mine", "Ada", issue=None, branch=LANE_BRANCH, role="reviewer", scope=("docs",)
    )
    client = FakeForge()
    _patch_release_session(monkeypatch, client, standing, forbid_git=True)

    released = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "release",
            "--branch",
            LANE_BRANCH,
            "--claim-id",
            "mine",
            "--coordinator-override",
            "--role",
            "coordinator",
            "--abandoned",
            "stopped",
        ]
    )

    assert released == 0
    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


def test_release_refuses_a_branch_and_claim_id_naming_different_claims(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    standing = request("mine", "Ada", issue=72, branch="codex/issue-72-x", scope=("src",))
    client = FakeForge()
    _patch_release_session(monkeypatch, client, standing, forbid_git=True)

    released = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "release",
            "72",
            "--branch",
            "codex/issue-99-other",
            "--claim-id",
            "mine",
            "--abandoned",
            "stopped",
        ]
    )

    assert released == 2
    assert capsys.readouterr().err == (
        "ERROR: --branch 'codex/issue-99-other' and --claim-id 'mine' disagree: the claim's "
        "own branch is 'codex/issue-72-x'; drop --branch or pass its own value\n"
    )
    assert store.fetch_state(worktree=Path("."), remote="origin").claims


@pytest.mark.parametrize(
    "arguments",
    [
        ["release", "42"],
        ["release", "42", "--merged", "12", "--abandoned", "stuck"],
    ],
)
def test_release_requires_exactly_one_landing_outcome(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as exited:
        issue_claim.main(arguments)

    assert exited.value.code == 2


ARGPARSE_USAGE_REFUSALS = [
    pytest.param(
        ["release", "42"],
        "one of the arguments --merged --abandoned is required",
        id="release-naming-no-outcome",
    ),
    pytest.param(
        ["claim", "42", "--scope", "src", "--nope"],
        "unrecognized arguments: --nope",
        id="claim-carrying-an-unknown-flag",
    ),
    pytest.param(
        ["item", "new"],
        "the following arguments are required: --title",
        id="item-new-missing-its-own-required-flag",
    ),
]
UNREADABLE_ITEM_REFERENCE_REFUSAL = pytest.param(
    ["status", "notanumber"],
    "'notanumber' is not an item reference; use aco-xxxxxx, #n, or the bare number n",
    id="status-naming-an-unreadable-item-reference",
)


@pytest.mark.parametrize(
    ("arguments", "message"), [*ARGPARSE_USAGE_REFUSALS, UNREADABLE_ITEM_REFERENCE_REFUSAL]
)
def test_a_refused_parse_under_json_prints_the_invalid_usage_envelope(
    arguments: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """OUT-06 (issue #432): a `--json` caller reads one object for every
    refusal its command's own parse raises before that command ever runs --
    an outcome flag it requires, a flag it does not know, and a positional
    value its own reader refuses."""
    status = issue_claim.main([*arguments, "--json"])

    captured = capsys.readouterr()
    assert status == 2
    assert json.loads(captured.out) == {
        "ok": False,
        "reason": "invalid_usage",
        "message": message,
    }
    assert captured.err == f"ERROR: {message}\n"


@pytest.mark.parametrize(("arguments", "message"), ARGPARSE_USAGE_REFUSALS)
def test_a_refused_parse_without_json_keeps_the_usage_text(
    arguments: list[str], message: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """OUT-06's own Never clause: without `--json` the parser's refusal is
    still argparse's own usage block and sentence on stderr, exit `2`, with
    stdout untouched."""
    with pytest.raises(SystemExit) as exited:
        issue_claim.main(arguments)

    captured = capsys.readouterr()
    assert exited.value.code == 2
    assert captured.out == ""
    assert captured.err.startswith("usage: aco")
    assert captured.err.endswith(f"error: {message}\n")


def test_a_bad_choice_on_a_json_command_prints_the_invalid_usage_envelope(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A value outside an option's own `choices` is one more refusal the
    parser raises (issue #432), and `brief` declares `--json`, so it reports
    through OUT-06 like every other one. The sentence is argparse's own,
    read back off stderr rather than spelled out here: its choice list is
    argparse's wording, not this contract's."""
    status = issue_claim.main(["brief", "42", "--step", "nope", "--json"])

    captured = capsys.readouterr()
    assert status == 2
    assert "argument --step: invalid choice:" in captured.err
    _assert_json_refusal_object(captured.err, captured.out, reason="invalid_usage")


def test_an_abbreviated_json_flag_asks_for_the_envelope_too(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`--jso` is `--json` wherever no other option of that command shares
    the prefix, so argparse accepts it and the refusal answers in the shape
    that caller asked for (issue #432)."""
    status = issue_claim.main(["release", "42", "--jso"])

    captured = capsys.readouterr()
    assert status == 2
    _assert_json_refusal_object(captured.err, captured.out, reason="invalid_usage")


@pytest.mark.parametrize(
    ("arguments", "envelope"),
    [
        pytest.param(
            ["status", "--json", "--", "--nope"], True, id="json-before-the-commands-own-dash-dash"
        ),
        pytest.param(
            ["status", "--", "--json"], False, id="json-behind-the-commands-own-dash-dash"
        ),
    ],
)
def test_a_dash_dash_ends_the_options_of_its_own_level_only(
    arguments: list[str], envelope: bool, capsys: pytest.CaptureFixture[str]
) -> None:
    """A `--` ends the options of the parser level that reads it and of no
    other (issue #432): `status --json -- --nope` still asked for the
    envelope, while behind `status`'s own `--` the very same spelling is
    just the item reference `status` refuses. Both refusals are `status`'s
    own item reader, so the tokens alone never decide the shape."""
    status = issue_claim.main(arguments)

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err.startswith("ERROR: ")
    if envelope:
        _assert_json_refusal_object(captured.err, captured.out, reason="invalid_usage")
    else:
        assert captured.out == ""


def _stock_argparse_reads_a_dash_dash_as_the_command_name() -> bool:
    """Ask this interpreter's own argparse what a leading `--` becomes before
    a subcommand action: the command name, or nothing at all. CPython changed
    that within a release series, so the answer is measured rather than read
    off a version number."""
    parser = argparse.ArgumentParser(prog="oracle", add_help=False)
    parser.add_subparsers(dest="command", required=True).add_parser("status")

    with contextlib.redirect_stderr(io.StringIO()):
        try:
            parser.parse_args(["--", "status"])
        except SystemExit:
            return True
    return False


def test_a_dash_dash_before_the_command_follows_the_parse_argparse_made(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The mode follows the parse argparse actually made, never the raw
    tokens (issue #432). Where this interpreter's argparse drops a leading
    `--` before the subcommand action, `status` is chosen and reads the
    `--json` behind it -- the envelope; where that `--` reaches the action it
    is the command name nobody declares, so no command was ever chosen that
    could declare the flag. Which of the two a CPython release does is a
    change in argparse itself, so the oracle above measures it here."""
    arguments = ["--", "status", "--json", "--nope"]

    if _stock_argparse_reads_a_dash_dash_as_the_command_name():
        with pytest.raises(SystemExit) as refused:
            issue_claim.main(arguments)
        assert refused.value.code == 2
        assert capsys.readouterr().out == ""
        return

    status = issue_claim.main(arguments)

    captured = capsys.readouterr()
    assert status == 2
    _assert_json_refusal_object(captured.err, captured.out, reason="invalid_usage")


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["bootstrap", "--json"], id="bootstrap-declaring-no-json-mode"),
        pytest.param(
            ["register", "--provider", "nope", "--json"], id="register-naming-an-unknown-provider"
        ),
        pytest.param(["item", "--json"], id="item-naming-no-subcommand"),
        pytest.param(["nope", "--json"], id="a-command-name-the-parser-does-not-know"),
        pytest.param(["--json"], id="no-command-at-all"),
    ],
)
def test_a_json_flag_no_command_declares_never_reaches_the_envelope(
    arguments: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """OUT-06's envelope belongs to the commands that declare `--json`
    (issue #432): `bootstrap` and `register` never offered the mode, and a
    missing subcommand, an unknown command name, or no command at all never
    chose one, so each keeps argparse's own usage block with stdout empty --
    no object a script could mistake for an answer."""
    with pytest.raises(SystemExit) as exited:
        issue_claim.main(arguments)

    captured = capsys.readouterr()
    assert exited.value.code == 2
    assert captured.out == ""
    assert captured.err.startswith("usage: aco")


PARENT_ISSUE = 79


def parented_check_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: str,
    parent_body: str,
    open_children: tuple[board.IssueReference, ...],
    parent_repository: str = REPOSITORY,
    parent_kind: body.ItemKind | None = body.ItemKind.CONTAINER,
) -> FakeForge:
    client = check_client(monkeypatch, landing_pull_request(body=body))
    client.parents[WORK_ITEM_ISSUE] = board.ParentIssue(
        board.IssueReference(parent_repository, PARENT_ISSUE), parent_body, parent_kind
    )
    client.children[PARENT_ISSUE] = tuple(
        board.ChildItem(reference.number, board.ChildState.OPEN) for reference in open_children
    )
    return client


def test_check_requires_the_parent_to_close_with_its_last_open_child(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Closing is required only when the parent's own `Next` line names no
    further work -- `complete_contract("keiner")` is exactly that."""
    parented_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72",
        parent_body=complete_contract("keiner"),
        open_children=(board.IssueReference(REPOSITORY, WORK_ITEM_ISSUE),),
    )

    assert run_check() == 2
    assert capsys.readouterr().err == (
        f"REFUSED: pull request #12 closes the last open child of parent "
        f"{REPOSITORY}#{PARENT_ISSUE}; close the parent too\n"
    )


def test_check_accepts_a_last_child_landing_when_the_parent_still_has_next_work(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ruled example: the container's own `Next` line still names work, so
    the landing may pass without closing it -- a container with a single
    dispatched child is the normal case, not the end."""
    parented_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72",
        parent_body=complete_contract("Cut the next slice."),
        open_children=(board.IssueReference(REPOSITORY, WORK_ITEM_ISSUE),),
    )

    assert run_check() == 0
    assert capsys.readouterr().out == (
        f"PR #12 by ada declares Work-Item: {REPOSITORY}#{WORK_ITEM_ISSUE}\n"
    )


def test_check_reads_the_parents_next_from_the_block_not_stale_prose(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The last-child rule reads a block-pinned parent's `next` through
    `parse_body` under the loaded pin, not the stale prose beside it (#150)."""
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    _write_block_pin(tmp_path)
    parent_body = (
        agent_claim_body('version = 1\nnow = "N"\nnext = "Cut the next slice."\ndone_when = "D"\n')
        + "\n\n## Next\nnichts\n"
    )
    parented_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72",
        parent_body=parent_body,
        open_children=(board.IssueReference(REPOSITORY, WORK_ITEM_ISSUE),),
    )

    assert run_check() == 0
    assert capsys.readouterr().out == (
        f"PR #12 by ada declares Work-Item: {REPOSITORY}#{WORK_ITEM_ISSUE}\n"
    )


def test_check_refuses_a_blockless_parent_before_the_next_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    _write_block_pin(tmp_path)
    parented_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72",
        parent_body="## Now\nOld prose.\n",
        open_children=(board.IssueReference(REPOSITORY, WORK_ITEM_ISSUE),),
    )

    assert run_check() == 2
    assert capsys.readouterr().err == (
        f"REFUSED: pull request #12 has parent {REPOSITORY}#{PARENT_ISSUE} with a body "
        "malformed: agent-claim: no agent-claim block\n"
    )


def test_check_refuses_a_malformed_parent_before_the_next_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    _write_block_pin(tmp_path)
    malformed_parent_body = agent_claim_body(
        'version = 2\nnow = "N"\nnext = "X"\ndone_when = "D"\n'
    )
    parented_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72",
        parent_body=malformed_parent_body,
        open_children=(board.IssueReference(REPOSITORY, WORK_ITEM_ISSUE),),
    )

    assert run_check() == 2
    assert capsys.readouterr().err == (
        f"REFUSED: pull request #12 has parent {REPOSITORY}#{PARENT_ISSUE} "
        "with a body malformed: version: version must be exactly 1\n"
    )


def test_check_permits_but_does_not_require_closing_a_parent_with_further_next_work(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parented_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72\nCloses #79",
        parent_body=complete_contract("Cut the next slice."),
        open_children=(board.IssueReference(REPOSITORY, WORK_ITEM_ISSUE),),
    )

    assert run_check() == 0


def test_check_refuses_a_parent_that_is_not_a_container(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parented_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72",
        parent_body=complete_contract("keiner"),
        open_children=(),
        parent_kind=body.ItemKind.TASK,
    )

    assert run_check() == 2
    assert capsys.readouterr().err == (
        f"REFUSED: pull request #12 has parent {REPOSITORY}#{PARENT_ISSUE} of kind task, "
        "which is not a container; only a container holds children\n"
    )


def test_check_accepts_a_landing_that_closes_its_completed_parent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parented_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72\nCloses #79",
        parent_body=complete_contract("keiner", now="Epic."),
        open_children=(board.IssueReference(REPOSITORY, WORK_ITEM_ISSUE),),
    )

    assert run_check() == 0
    assert capsys.readouterr().out == (
        f"PR #12 by ada declares Work-Item: {REPOSITORY}#{WORK_ITEM_ISSUE}\n"
    )


def test_check_requires_a_next_line_on_a_parent_that_keeps_other_children(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parented_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72",
        parent_body=complete_contract("", now="Epic without a next step."),
        open_children=(
            board.IssueReference(REPOSITORY, WORK_ITEM_ISSUE),
            board.IssueReference(REPOSITORY, 73),
        ),
    )

    assert run_check() == 2
    assert capsys.readouterr().err == (
        f"REFUSED: pull request #12 leaves parent {REPOSITORY}#{PARENT_ISSUE} open with "
        "1 other open child, whose body carries no Next line\n"
    )


def test_check_accepts_a_landing_whose_parent_says_what_comes_next(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parented_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72",
        parent_body=complete_contract("Dispatch slice 4."),
        open_children=(
            board.IssueReference(REPOSITORY, WORK_ITEM_ISSUE),
            board.IssueReference(REPOSITORY, 73),
        ),
    )

    assert run_check() == 0
    assert capsys.readouterr().out == (
        f"PR #12 by ada declares Work-Item: {REPOSITORY}#{WORK_ITEM_ISSUE}\n"
    )


def test_check_refuses_to_close_a_parent_that_keeps_other_children(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parented_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72\nCloses #79",
        parent_body=complete_contract("Dispatch slice 4."),
        open_children=(
            board.IssueReference(REPOSITORY, WORK_ITEM_ISSUE),
            board.IssueReference(REPOSITORY, 73),
        ),
    )

    assert run_check() == 2
    assert capsys.readouterr().err == (
        f"REFUSED: pull request #12 closes {REPOSITORY}#{PARENT_ISSUE} besides its work "
        f"item {REPOSITORY}#{WORK_ITEM_ISSUE}; a pull request lands one item\n"
    )


def test_check_refuses_a_parent_recorded_in_another_repository(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parented_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72",
        parent_body=complete_contract("Cut the next slice."),
        open_children=(),
        parent_repository="other/repo",
    )

    assert run_check() == 2
    assert capsys.readouterr().err == (
        f"REFUSED: pull request #12 has parent other/repo#{PARENT_ISSUE} in another "
        "repository, whose children this check cannot read\n"
    )


def test_next_names_a_recovery_item_before_the_item_it_recommends(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    landed = board_issue(90, "Landed but open", complete_contract("Close it."))
    ready = board_issue(91, "Waiting work", complete_contract("Claim #91."))
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(landed, ready))
    monkeypatch.setattr(
        client,
        "list_recent_merged_board_pull_requests",
        lambda _since: (
            board.PullRequest(
                140,
                "Lands it",
                "Work-Item: #90\n\nCloses #90",
                "branch",
                "2026-08-20T00:00:00Z",
            ),
        ),
    )

    assert issue_claim.main(["--repo", REPOSITORY, "next"]) == 0
    assert capsys.readouterr().out.startswith(f"RECOVERY\n#90: {board.RECOVERY_STEP}\n\n")


def test_check_accepts_a_body_naming_work_github_does_not_close_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`Implements #80` retires nothing on GitHub, so it is no closing reference."""
    check_client(
        monkeypatch,
        landing_pull_request(body="Work-Item: #72\n\nCloses #72\n\nImplements #80"),
    )

    assert run_check() == 0
    assert capsys.readouterr().out == (
        f"PR #12 by ada declares Work-Item: {REPOSITORY}#{WORK_ITEM_ISSUE}\n"
    )


CHECKED_ISSUE = 81


def issue_check_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    body: str,
    state: forge.ItemState = forge.ItemState.OPEN,
    dependencies: tuple[board.IssueDependency, ...] = (),
) -> FakeForge:
    """A client serving one issue, in a checkout carrying the `"block"` pin
    the migrated repositories still write.

    No store patching: the issue mode of `check` never reads the state ref,
    so a test that needed one would be proving the wrong command.
    """
    (tmp_path / ".agent-claim").mkdir(parents=True, exist_ok=True)
    (tmp_path / ".agent-claim" / "board.toml").write_text('body_contract = "block"\n')
    client = FakeForge()
    client.issue_references[CHECKED_ISSUE] = forge.ItemReference(state, "Work", body)
    client.board_dependencies[CHECKED_ISSUE] = dependencies
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    return client


def open_dependency(number: int, repository: str = REPOSITORY) -> board.IssueDependency:
    return board.IssueDependency(
        board.IssueReference(repository, number), board.BlockerState.OPEN, False
    )


def test_check_accepts_a_complete_unblocked_issue_in_two_requests(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The reference read cannot carry the empty dependency list, so reading
    an issue always costs the second request."""
    client = issue_check_client(monkeypatch, tmp_path, body=agent_claim_body(MINIMAL_BLOCK_TOML))

    assert run_check(CHECKED_ISSUE) == 0
    assert capsys.readouterr().out == f"ISSUE #{CHECKED_ISSUE} body ok\n"
    assert client.requests == 2


def _check_trunk_repository(tmp_path: Path) -> Path:
    """A minimal real `origin`-backed repository (issue #359, LAND-48) for
    `check <sha>`: an initial commit with no trailer, a commit trailer-
    naming `#20`, and one trailer-classified `No-Item: docs` -- the three
    classifications `check <sha>` reads from a commit's own trailer block,
    the same grammar `check <pr>` reads from a pull request body with. A
    fourth commit sits on the `side` branch, classified as soundly as the
    trunk ones: it exists in this repository, but never on its trunk."""
    repo, _remote = _real_repository_with_bare_remote(tmp_path)
    (repo / "base.txt").write_text("base\n")
    _real_git(repo, "add", "base.txt")
    _real_git(repo, "commit", "-q", "-m", "initial")
    (repo / "work.txt").write_text("work\n")
    _real_git(repo, "add", "work.txt")
    _real_git(repo, "commit", "-q", "-m", "work item change", "-m", "Work-Item: #20")
    (repo / "docs.txt").write_text("docs\n")
    _real_git(repo, "add", "docs.txt")
    _real_git(repo, "commit", "-q", "-m", "docs change", "-m", "No-Item: docs")
    _push_repository_trunk(repo, "origin")
    _real_git(repo, "checkout", "-q", "-b", "side")
    (repo / "side.txt").write_text("side\n")
    _real_git(repo, "add", "side.txt")
    _real_git(repo, "commit", "-q", "-m", "side change", "-m", "Work-Item: #21")
    _real_git(repo, "checkout", "-q", "main")
    return repo


def _check_sha(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> tuple[Path, Callable[[str], str]]:
    """`check <sha>`'s own shared setup: the real trunk history, and a
    `ref -> sha` resolver over it (issue #359)."""
    monkeypatch.setattr(checkout, "trunk_landings", _LIVE_TRUNK_LANDINGS)
    repo = _check_trunk_repository(tmp_path)
    monkeypatch.chdir(repo)
    return repo, lambda ref: _real_git(repo, "rev-parse", ref).stdout.strip()


_UNWALKED_SHA = "0" * 40

_TRUNK_CHECK_CASES = (
    pytest.param("main~1", "{sha} declares Work-Item: #20", "valid", None, 0, id="work-item"),
    pytest.param("main", "{sha} declares No-Item: docs", "valid", None, 0, id="no-item"),
    pytest.param(
        "main~2",
        "REFUSED: {sha} carries no `Work-Item:` or `No-Item:` trailer",
        "invalid_classification",
        "carries no `Work-Item:` or `No-Item:` trailer",
        2,
        id="no-trailer",
    ),
    pytest.param(
        "side",
        "REFUSED: {sha} is not on the first-parent trunk",
        "not_on_trunk",
        "is not on the first-parent trunk",
        2,
        id="off-trunk",
    ),
    pytest.param(
        None,
        "REFUSED: {sha} is not on the first-parent trunk",
        "not_on_trunk",
        "is not on the first-parent trunk",
        2,
        id="unknown-sha",
    ),
)


def _expected_trunk_envelope(sha: str, reason: str, message: str | None) -> dict[str, object]:
    """`specs/output.spec.md`'s envelope as `check <sha>` fills it: `ok` and
    `reason` first, the commit under the `sha` key its own kind names, and a
    refusal's sentence last -- no `refused` key anywhere (issue #435)."""
    envelope: dict[str, object] = {
        "ok": message is None,
        "reason": reason,
        "kind": "trunk_commit",
        "sha": sha,
    }
    if message is not None:
        envelope["message"] = message
    return envelope


@pytest.mark.parametrize(
    ("landing_ref", "line", "reason", "message", "exit_code"), _TRUNK_CHECK_CASES
)
def test_check_sha_answers_in_text_and_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    landing_ref: str | None,
    line: str,
    reason: str,
    message: str | None,
    exit_code: int,
) -> None:
    """Issue #435 (LAND-48, LAND-57, LAND-58, CHECK-12..CHECK-14): one trunk
    answer in both forms -- the human line on stdout when the commit
    classifies and on stderr when it does not, the `--json` object the one
    shared envelope carrying `check`'s own `reason` -- and one exit for both:
    `0` for a classified commit, `2` for every defect, never `1`. The
    `side` commit exists here and carries a sound trailer, so only the walk
    can refuse it; the unknown sha is in no repository at all, and answers
    `not_on_trunk` too -- `check <sha>` never claims a commit is missing,
    because a walk that does not hold it proves nothing about its
    existence."""
    _repo, sha_of = _check_sha(monkeypatch, tmp_path)
    sha = _UNWALKED_SHA if landing_ref is None else sha_of(landing_ref)

    text_status = issue_claim.main(["check", sha])
    printed = capsys.readouterr()
    json_status = issue_claim.main(["check", sha, "--json"])
    envelope = json.loads(capsys.readouterr().out)

    expected_line = line.format(sha=sha) + "\n"
    expected_streams = ("", expected_line) if message is not None else (expected_line, "")
    assert (text_status, printed.out, printed.err) == (exit_code, *expected_streams)
    assert (json_status, envelope) == (exit_code, _expected_trunk_envelope(sha, reason, message))


def test_check_names_a_number_that_exists_in_neither_number_space(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """GitHub gives issues and pull requests one number space, so an absent
    number was never proven to be either -- the refusal names no kind word."""
    client = issue_check_client(monkeypatch, tmp_path, body="", state=forge.ItemState.MISSING)

    assert run_check(CHECKED_ISSUE) == 2
    assert capsys.readouterr().err == (
        f"REFUSED: #{CHECKED_ISSUE} does not exist in {REPOSITORY}\n"
    )
    assert client.requests == 1


def test_check_json_names_a_missing_number_as_its_own_kind(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    issue_check_client(monkeypatch, tmp_path, body="", state=forge.ItemState.MISSING)

    exit_code = issue_claim.main(["--repo", REPOSITORY, "check", str(CHECKED_ISSUE), "--json"])

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "reason": "missing",
        "kind": "missing",
        "number": CHECKED_ISSUE,
        "message": f"does not exist in {REPOSITORY}",
    }


def test_check_names_a_body_with_no_recognized_block_as_malformed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`no agent-claim block` keeps its meaning: no recognized block was
    found, never "recognized prose" (#204)."""
    issue_check_client(
        monkeypatch,
        tmp_path,
        body="## Now\nReady.\n\n## Next\nLand it.\n\n## Done when\nMerged.",
    )

    assert run_check(CHECKED_ISSUE) == 2
    assert capsys.readouterr().err == (
        f"ISSUE #{CHECKED_ISSUE} body malformed: agent-claim: no agent-claim block\n"
    )


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        pytest.param(
            "```agent-claim\nversion = 1\n",
            "agent-claim: unclosed agent-claim block",
            id="broken-fence",
        ),
        pytest.param(
            agent_claim_body('version = 1\nnow = 1\nnext = "X"\ndone_when = "D"\n'),
            "now: now must be a string",
            id="broken-value",
        ),
        pytest.param(
            agent_claim_body(f'{MINIMAL_BLOCK_TOML}blocked_by = "#7"\n'),
            "blocked_by: unknown top-level key blocked_by",
            id="unknown-key",
        ),
    ],
)
def test_check_names_a_malformed_block_by_its_first_defect(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    body: str,
    reason: str,
) -> None:
    issue_check_client(monkeypatch, tmp_path, body=body)

    assert run_check(CHECKED_ISSUE) == 2
    assert capsys.readouterr().err == f"ISSUE #{CHECKED_ISSUE} body malformed: {reason}\n"


@pytest.mark.parametrize(
    ("toml_text", "missing"),
    [
        pytest.param(
            'version = 1\nnow = "Ready."\nnext = "Land it."\ndone_when = ""\n',
            "Done when",
            id="one-key-left-empty",
        ),
        pytest.param(
            'version = 1\nnow = ""\nnext = ""\ndone_when = ""\n',
            "Now, Next, Done when",
            id="a-fresh-skeleton-never-names-a-dependency-key",
        ),
    ],
)
def test_check_names_the_sections_an_incomplete_body_leaves_empty(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    toml_text: str,
    missing: str,
) -> None:
    issue_check_client(monkeypatch, tmp_path, body=agent_claim_body(toml_text))

    assert run_check(CHECKED_ISSUE) == 2
    assert capsys.readouterr().err == f"ISSUE #{CHECKED_ISSUE} body incomplete: {missing}\n"


def test_check_reads_blockers_from_the_forge_and_qualifies_foreign_ones(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = issue_check_client(
        monkeypatch,
        tmp_path,
        body=agent_claim_body(MINIMAL_BLOCK_TOML),
        dependencies=(open_dependency(7), open_dependency(9, "other/repo")),
    )

    assert run_check(CHECKED_ISSUE) == 3
    assert capsys.readouterr().err == f"ISSUE #{CHECKED_ISSUE} blocked by #7, other/repo#9\n"
    assert client.requests == 2


def test_issue_check_labels_a_local_blocker_under_the_state_ref_pin() -> None:
    """Issue #300 (Codex Terra review): `_issue_check` reads the repository's
    own `storage` pin for a local blocker exactly as `claim`'s
    `_blocked_check` does -- a foreign one (`other/repo#9`) is unaffected,
    since it is never local to this repository's own storage pin
    (`board.open_blocker_label`)."""
    client = FakeForge(
        board_dependencies={CHECKED_ISSUE: (open_dependency(7), open_dependency(9, "other/repo"))}
    )

    outcome = issue_claim._issue_check(
        client,
        REPOSITORY,
        agent_claim_body(MINIMAL_BLOCK_TOML),
        CHECKED_ISSUE,
        storage=body.Storage.STATE_REF,
    )

    local_label = board.item_label(7, body.Storage.STATE_REF)
    assert outcome.line == f"ISSUE #{CHECKED_ISSUE} blocked by {local_label}, other/repo#9"


def test_check_reads_a_pull_request_in_one_dispatch_landing_and_classification_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Four round trips for a parentless work item: the dispatch reference,
    the landing itself, the default branch, and the sub-issue relation."""
    client = check_client(
        monkeypatch,
        landing_pull_request(body=f"Work-Item: #{WORK_ITEM_ISSUE}\n\nCloses #{WORK_ITEM_ISSUE}"),
    )

    assert run_check() == 0
    capsys.readouterr()
    assert client.requests == 4


def test_check_json_reports_unavailable_on_a_real_forge_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real `GitHubForge` (issue #199), not `FakeForge`: its own `_run`
    chokepoint raises the forge failure once dispatch itself reads the
    reference, proving that cause reaches the shared envelope as `reason:
    "unavailable"` (issue #404) rather than escaping to `main`'s legacy
    generic `{"ok": false, "error": ...}` sink."""

    def failing_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        raise forge.ForgeTransientError("gh: simulated network failure")

    real_client = GitHubForge(github._repository_id(REPOSITORY), run=failing_run)
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: real_client)

    status = issue_claim.main(["--repo", REPOSITORY, "check", "12", "--json"])

    captured = capsys.readouterr()
    assert status == 2
    _assert_json_refusal_object(captured.err, captured.out, reason="unavailable")


@pytest.mark.parametrize(
    ("body", "exit_code", "expected"),
    [
        pytest.param(
            f"Work-Item: #{WORK_ITEM_ISSUE}\n\nCloses #{WORK_ITEM_ISSUE}",
            0,
            {"ok": True, "reason": "valid", "kind": "pull_request", "number": 12},
            id="declared-pull-request",
        ),
        pytest.param(
            "Tidy the README.",
            2,
            {
                "ok": False,
                "reason": "invalid_classification",
                "kind": "pull_request",
                "number": 12,
                "message": "carries no `Work-Item:` or `No-Item:` line",
            },
            id="unclassified-pull-request",
        ),
    ],
)
def test_check_json_discriminates_a_pull_request(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    body: str,
    exit_code: int,
    expected: dict[str, object],
) -> None:
    check_client(monkeypatch, landing_pull_request(body=body))

    status = issue_claim.main(["--repo", REPOSITORY, "check", "12", "--json"])

    assert status == exit_code
    assert json.loads(capsys.readouterr().out) == expected


def test_check_json_pins_the_raw_envelope_text_for_a_valid_pull_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The raw bytes `check` prints on success (OUT-01's own key order,
    `ok`/`reason`/`kind`/`number`, and its trailing newline), not just the
    parsed dict `test_check_json_discriminates_a_pull_request` already
    covers."""
    check_client(
        monkeypatch,
        landing_pull_request(body=f"Work-Item: #{WORK_ITEM_ISSUE}\n\nCloses #{WORK_ITEM_ISSUE}"),
    )

    status = issue_claim.main(["--repo", REPOSITORY, "check", "12", "--json"])

    assert status == 0
    assert (
        capsys.readouterr().out
        == '{"ok": true, "reason": "valid", "kind": "pull_request", "number": 12}\n'
    )


@pytest.mark.parametrize(
    ("dependencies", "exit_code", "expected"),
    [
        pytest.param(
            (),
            0,
            {"ok": True, "reason": "valid", "kind": "issue", "number": CHECKED_ISSUE},
            id="sound-issue",
        ),
        pytest.param(
            (open_dependency(62),),
            3,
            {
                "ok": False,
                "reason": "blocked",
                "kind": "issue",
                "number": CHECKED_ISSUE,
                "blocked_by": ["#62"],
                "message": "blocked by #62",
            },
            id="blocked-issue",
        ),
    ],
)
def test_check_json_discriminates_an_issue(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    dependencies: tuple[board.IssueDependency, ...],
    exit_code: int,
    expected: dict[str, object],
) -> None:
    issue_check_client(
        monkeypatch,
        tmp_path,
        body=agent_claim_body(MINIMAL_BLOCK_TOML),
        dependencies=dependencies,
    )

    status = issue_claim.main(["--repo", REPOSITORY, "check", str(CHECKED_ISSUE), "--json"])

    assert status == exit_code
    assert json.loads(capsys.readouterr().out) == expected


def body_check_main(*, extra: tuple[str, ...] = ()) -> int:
    return issue_claim.main(["body", "--check", *extra])


def test_body_check_accepts_a_complete_block_with_no_defects(
    capsys: pytest.CaptureFixture[str],
) -> None:
    body_file = io.StringIO(agent_claim_body(MINIMAL_BLOCK_TOML))
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(sys, "stdin", body_file)
        assert body_check_main() == 0
    assert capsys.readouterr().out == "body ok\n"


def test_body_check_accepts_a_valid_size(capsys: pytest.CaptureFixture[str]) -> None:
    """BODY-58 (issue #357): a valid `size` is `body ok`, exit `0`."""
    body_file = io.StringIO(agent_claim_body(f'{MINIMAL_BLOCK_TOML}size = "M"\n'))
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(sys, "stdin", body_file)
        assert body_check_main() == 0
    assert capsys.readouterr().out == "body ok\n"


def test_body_check_refuses_an_invalid_size(capsys: pytest.CaptureFixture[str]) -> None:
    """BODY-59 (issue #357): an out-of-grammar `size` is `body malformed`,
    exit `2`, the same sentence for an invalid string or a non-scalar value."""
    body_file = io.StringIO(agent_claim_body(f'{MINIMAL_BLOCK_TOML}size = "XL"\n'))
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(sys, "stdin", body_file)
        assert body_check_main() == 2
    assert capsys.readouterr().err == "body malformed: size: size must be S, M, or L\n"


_RECORD_TOML = (
    '\n[record]\ntitle = "T"\nstate = "open"\nlabels = []\nblocked_by = []\n'
    'created_at = "2026-09-10T00:00:00Z"\nupdated_at = "2026-09-15T00:00:00Z"\n'
)


def test_body_check_reads_the_storage_pin_for_the_record_key(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #287 proof 6: `body --check` reads the repository's own
    storage pin -- `[record]` is a known key under `storage = "state-ref"`
    and an unknown one under the default `storage = "github"`."""
    body = agent_claim_body(MINIMAL_BLOCK_TOML + _RECORD_TOML)

    monkeypatch.setattr(sys, "stdin", io.StringIO(body))
    assert body_check_main() == 2
    assert "unknown top-level key record" in capsys.readouterr().err

    _write_state_ref_pin(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(body))
    assert body_check_main() == 0
    assert capsys.readouterr().out == "body ok\n"


def test_body_check_names_a_body_with_no_recognized_block_as_malformed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("no block\n"))
    assert body_check_main() == 2
    assert capsys.readouterr().err == "body malformed: agent-claim: no agent-claim block\n"


@pytest.mark.parametrize(
    ("toml_text", "reason"),
    [
        pytest.param(
            'version = 1\nnow = "N"\nnext = "X"\n',
            "done_when: done_when is required",
            id="missing-done-when",
        ),
        pytest.param(
            f'{MINIMAL_BLOCK_TOML}owner = "x"\n',
            "owner: unknown top-level key owner",
            id="unknown-key",
        ),
        pytest.param(
            f'{MINIMAL_BLOCK_TOML}\n[[expectation]]\ntext = "x"\ndefault = "yes"\nruling = "yes"\n',
            "expectation[0].default: expectation[0] must be proposed (default) or ruled "
            "(ruling, ruled_on), not both",
            id="default-and-ruling",
        ),
    ],
)
def test_body_check_names_defects_with_checks_own_sentences(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    toml_text: str,
    reason: str,
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(agent_claim_body(toml_text)))
    assert body_check_main() == 2
    assert capsys.readouterr().err == f"body malformed: {reason}\n"


def test_body_check_prints_every_simultaneous_defect_not_just_the_first(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The one behavior that sets `body --check` apart from `check <item>`,
    which truncates to the first malformed defect
    (`_body_contract_checks`): with two simultaneous defects, both surface,
    in order, in plain text and in `--json`'s `defects` list (Sonnet review,
    issue #262)."""
    toml_text = 'version = 1\nnext = "X"\n'  # missing both now and done_when

    monkeypatch.setattr(sys, "stdin", io.StringIO(agent_claim_body(toml_text)))
    assert body_check_main() == 2
    assert capsys.readouterr().err == (
        "body malformed: now: now is required\nbody malformed: done_when: done_when is required\n"
    )

    monkeypatch.setattr(sys, "stdin", io.StringIO(agent_claim_body(toml_text)))
    assert body_check_main(extra=("--json",)) == 2
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "reason": "malformed",
        "defects": [
            "body malformed: now: now is required",
            "body malformed: done_when: done_when is required",
        ],
    }


def test_body_check_json_carries_the_defect_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("no block\n"))
    assert body_check_main(extra=("--json",)) == 2
    assert capsys.readouterr().out == (
        '{"ok": false, "reason": "malformed", '
        '"defects": ["body malformed: agent-claim: no agent-claim block"]}\n'
    )


def test_body_check_json_reports_ok_with_an_empty_defect_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(agent_claim_body(MINIMAL_BLOCK_TOML)))
    assert body_check_main(extra=("--json",)) == 0
    assert capsys.readouterr().out == '{"ok": true, "reason": "valid", "defects": []}\n'


class _NotUtf8Stdin:
    """A stdin stand-in for the one input `body --check` cannot decode --
    `sys.stdin.read()` raises `UnicodeDecodeError` on invalid bytes exactly
    like this (issue #262 Sonar S8707 follow-up)."""

    def read(self) -> str:
        raise UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")


def test_body_check_refuses_stdin_that_is_not_valid_utf8(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", _NotUtf8Stdin())

    assert body_check_main() == 2
    assert "stdin is not valid UTF-8" in capsys.readouterr().err


def test_body_requires_check() -> None:
    with pytest.raises(SystemExit) as refused:
        issue_claim.main(["body"])

    assert refused.value.code == 2


def test_body_check_never_touches_a_forge_the_store_or_gh(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`body --check` is forge-free like `status` (issue #245): it never
    resolves a `_LazyForge`, reads the state ref, or shells out to `gh`."""

    def unused(*args: object, **kwargs: object) -> None:
        pytest.fail("body --check must not touch a forge, the store, or gh")

    monkeypatch.setattr(github, "GitHubForge", unused)
    monkeypatch.setattr(github, "discover_repository", unused)
    monkeypatch.setattr(store, "fetch_state", unused)
    monkeypatch.setattr(sys, "stdin", io.StringIO(agent_claim_body(MINIMAL_BLOCK_TOML)))

    assert body_check_main() == 0
    assert capsys.readouterr().out == "body ok\n"


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["pr-check", "--pr", "12"], id="replaced-command"),
        pytest.param(["check", "--pr", "12"], id="replaced-option"),
    ],
)
def test_the_replaced_pull_request_check_surface_is_gone(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        issue_claim.main(["--repo", REPOSITORY, *argv])

    assert refused.value.code == 2


@pytest.mark.parametrize(
    "json_flag", [pytest.param(False, id="without-json"), pytest.param(True, id="with-json")]
)
def test_cli_claim_refuses_a_missing_state_ref(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], json_flag: bool
) -> None:
    """A missing `refs/aco/state` (issue #199): with `--json`, the stdout
    envelope names the unchanged stderr sentence under `reason: unavailable`
    (issue #406); without it, stdout stays exactly as empty as it always
    has."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    git_values = _git_checkout()
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    _patch_store_write(monkeypatch, tip=None)

    arguments = [
        "--repo",
        REPOSITORY,
        "claim",
        "72",
        "--agent",
        "Ada",
        "--base",
        BASE,
        "--branch",
        "codex/issue-72",
        "--scope",
        "src",
        "--claim-id",
        "cli-claim",
    ]
    if json_flag:
        arguments.append("--json")

    status = issue_claim.main(arguments)

    captured = capsys.readouterr()
    assert status == 2
    assert protocol.MISSING_STATE_REF in captured.err
    if json_flag:
        _assert_json_refusal_object(captured.err, captured.out, reason="unavailable")
    else:
        assert captured.out == ""


def test_cli_claim_refuses_canonical_remote_mismatch_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    git_values = _git_checkout()
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    fake = _patch_store_write(monkeypatch)
    monkeypatch.setattr(checkout, "remote_url", lambda remote: "git@github.com:other/repo.git")

    status = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src",
            "--claim-id",
            "cli-claim",
        ]
    )

    assert status == 2
    assert "forge target example/agent-coordination does not match canonical remote other/repo" in (
        capsys.readouterr().err
    )
    assert fake.transitions == []


def test_cli_bootstrap_takes_no_ledger_argument() -> None:
    """The one-time ledger import is gone: `bootstrap` creates the state ref
    and nothing else, so `--ledger` is an unknown argument, not a quietly
    ignored one."""
    parser = issue_claim._parser()
    subparsers_action = next(
        action for action in parser._actions if isinstance(action, argparse._SubParsersAction)
    )
    bootstrap = subparsers_action.choices["bootstrap"]

    assert parser.prog == "aco"
    assert all("--ledger" not in action.option_strings for action in bootstrap._actions)
    with pytest.raises(SystemExit):
        issue_claim.main(["bootstrap", "--ledger", "5"])


def test_cli_bootstrap_ignores_repo_and_a_non_github_remote(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`bootstrap` is forge-free (issue #245): it never resolves a forge
    target, so `--repo` and a canonical remote naming a different, even a
    non-GitHub, repository are no error for it -- only `status`, `protect`,
    and a lane `claim`/`rescope`/`release` share that guarantee too; an
    issue `claim` or `board` still checks Erwartung 6."""
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: "/repo")
    monkeypatch.setattr(checkout, "remote_url", lambda remote: "git@gitlab.com:other/repo.git")
    monkeypatch.setattr(store, "bootstrap", lambda *, worktree, remote: BASE)

    def unused(*_args: object, **_kwargs: object) -> forge.RepositoryId:
        pytest.fail("bootstrap must never resolve a forge target")

    monkeypatch.setattr(github, "discover_repository", unused)

    status = issue_claim.main(["--repo", REPOSITORY, "bootstrap"])

    captured = capsys.readouterr()
    assert status == 0
    assert captured.err == ""
    assert captured.out == f"{BASE}\n"


def test_cli_bootstrap_surfaces_a_store_failure_without_inventing_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`bootstrap`'s parsed namespace has no `json` attribute at all (issue
    #199): the general `ClaimError` sink must read it defensively rather
    than inventing a default that would make `bootstrap` emit JSON it never
    offered -- stdout stays empty, exactly as before this command grew a
    `--json`-aware sink."""
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: "/repo")

    def failing_bootstrap(*, worktree: Path, remote: str) -> str:
        raise ClaimError("cannot reach refs/aco/state: auth or transport failure")

    monkeypatch.setattr(store, "bootstrap", failing_bootstrap)

    status = issue_claim.main(["bootstrap"])

    captured = capsys.readouterr()
    assert status == 2
    assert "cannot reach refs/aco/state: auth or transport failure" in captured.err
    assert "--ledger" not in captured.err
    assert captured.out == ""


def test_cli_rescope_refuses_a_missing_state_ref(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    git_values = _git_checkout()
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    _set_agent_identity_env(monkeypatch, {checkout.ACO_AGENT_ENV: "Codex Sol"})
    _patch_store_write(monkeypatch, tip=None)

    status = issue_claim.main(["--repo", REPOSITORY, "rescope", "72", "--add", "/repo/src/new.py"])

    assert status == 2
    assert protocol.MISSING_STATE_REF in capsys.readouterr().err


def test_cli_release_refuses_a_missing_state_ref(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    standing = request("mine", "Ada", issue=72, branch="lane-72", scope=("src",))
    client = FakeForge()
    _patch_release_session(monkeypatch, client, standing)
    _patch_store_write(monkeypatch, tip=None)

    status = issue_claim.main(["--repo", REPOSITORY, "release", "72", "--abandoned", "stopped"])

    assert status == 2
    assert protocol.MISSING_STATE_REF in capsys.readouterr().err


# Lazy forge (issue #245): a forge-free command never resolves a repository,
# reads a remote's own URL, or calls `gh` -- proven here against a bare
# `file://` canonical remote (the shape a repository with no forge adapter at
# all still uses for its state ref) with `discover_repository`/`GitHubForge`
# and `checkout.remote_url` all forbidden outright, never merely absent.


def test_cli_status_is_forge_free_against_a_non_github_remote(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _patch_status_store(monkeypatch)
    _forbid_forge_resolution(monkeypatch)

    assert issue_claim.main(["status"]) == 0
    assert capsys.readouterr().out == "UNCLAIMED repository\n"


def test_cli_lane_claim_rescope_and_release_are_forge_free_against_a_non_github_remote(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A lane `claim`/`rescope`/`release` never resolves a forge target
    (issue #245): the round trip below runs entirely against a `file://`
    canonical remote with the forge and the remote's own URL both forbidden,
    and still claims, rescopes, and releases."""
    _set_agent_identity_env(monkeypatch, {"ACO_AGENT": "Codex Sol"})
    monkeypatch.setattr(
        checkout,
        "versioned_paths",
        lambda **_kwargs: (
            "LICENSE",
            "README.md",
            "pyproject.toml",
            "src/agent_coordination/__init__.py",
        ),
    )
    monkeypatch.setattr(issue_claim, "datetime", FixedDateTime)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
    # `rescope` fetches and commits real store state against its resolved
    # checkout's own toplevel (issue #314 delta, finding R1) -- unlike
    # `claim`'s cwd-based store observation above, still unaffected by this
    # fix, this fake toplevel must therefore be a real directory the test's
    # own real `store.fetch_state`/`commit_transition` calls can `-C` into,
    # not the placeholder `"/repo"` every other checkout fact below stays.
    real_toplevel = str(Path.cwd())
    git_values = {
        ("branch", "--show-current"): "docs/lane-cleanup",
        ("rev-parse", "--show-toplevel"): real_toplevel,
        ("rev-parse", "HEAD"): BASE,
        ("rev-parse", "--verify", "HEAD"): BASE,
        ("rev-parse", "--git-dir"): "/repo/.git/worktrees/lane-cleanup",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        # `rescope`'s path-based checkout resolution (issue #314): the same
        # toplevel/git-dir/common-dir facts above, combined in the one call
        # `resolve_path_checkout` actually sends.
        (
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-dir",
            "--git-common-dir",
        ): f"{real_toplevel}\n/repo/.git/worktrees/lane-cleanup\n/repo/.git",
        ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main",
    }
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: git_values[tuple(arguments)]
    )
    _forbid_forge_resolution(monkeypatch)

    claimed = issue_claim.main(
        [
            "claim",
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            "docs/lane-cleanup",
            "--scope",
            "docs",
            "--claim-id",
            "cli-lane-claim",
        ]
    )
    assert claimed == 0
    capsys.readouterr()

    rescoped = issue_claim.main(["rescope", "--add", f"{real_toplevel}/README.md"])
    assert rescoped == 0
    lane_key = protocol.claim_key(protocol.LaneIdentity(), "docs/lane-cleanup")
    assert store.fetch_state(worktree=Path("."), remote="origin").claims[lane_key].scope == (
        "README.md",
        "docs",
    )
    capsys.readouterr()

    released = issue_claim.main(["release", "--abandoned", "stopped"])
    assert released == 0
    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


def test_cli_board_refuses_a_non_github_canonical_remote_by_host(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`board` is a forge command (issue #245): a canonical remote on any
    host but GitHub refuses by that host's own name, before ever calling
    `discover_repository`/`gh` -- never the store-blind path a forge-free
    command like `status` takes for the same remote, and never GitHub's own
    "does not name a GitHub repository" text."""
    monkeypatch.setattr(
        checkout, "remote_url", lambda remote: "file:///srv/git/agent-coordination.git"
    )

    def unused(*_args: object, **_kwargs: object) -> forge.RepositoryId:
        pytest.fail("board must refuse the host before ever calling discover_repository")

    monkeypatch.setattr(github, "discover_repository", unused)

    status = issue_claim.main(["board", "--json"])

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err == "ERROR: no forge adapter for host file\n"


_UNTRACKED_BOARD_CONFIG_ERROR = (
    "ERROR: .agent-claim/board.toml is not tracked in this checkout, so its "
    "storage pin cannot be trusted: git add -f .agent-claim/board.toml\n"
)


@pytest.mark.parametrize(
    "arguments",
    [
        pytest.param(["bootstrap"], id="bootstrap"),
        pytest.param(["board", "--json"], id="board"),
    ],
)
def test_untracked_board_config_refuses_every_store_command_by_name(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], arguments: list[str]
) -> None:
    """Issue #315: an absent, untracked, or ignored `.agent-claim/board.toml`
    no longer reads as `storage = "github"`'s silent default -- `bootstrap`
    (which never resolves a forge) and `board` (which does, through
    `_LazyForge`) both refuse by the same sentence, naming the repair,
    before either does any other work."""
    monkeypatch.setattr(checkout, "path_is_tracked", lambda _path, **_kwargs: False)

    status = issue_claim.main(arguments)

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err == _UNTRACKED_BOARD_CONFIG_ERROR


def _scratch_lane_repository(tmp_path: Path) -> tuple[Path, str, str]:
    """A repository with a base commit on `main` and a lane branch one commit
    ahead of it -- `brief`'s own real reads (`rev-parse --verify`, `diff
    --name-only`) run against real git history here, never a hand-typed
    `_git_output` fake."""
    repository = tmp_path / "repo"
    repository.mkdir()
    _real_git(repository, "init", "-q", "-b", "main")
    _real_git(repository, "config", "user.name", "Test")
    _real_git(repository, "config", "user.email", "test@example.com")
    (repository / "README.md").write_text("hello\n")
    _real_git(repository, "add", "README.md")
    _real_git(repository, "commit", "-q", "-m", "initial")
    base = _real_git(repository, "rev-parse", "HEAD").stdout.strip()
    _real_git(repository, "checkout", "-q", "-b", "codex/issue-258-brief")
    (repository / "README.md").write_text("hello\nbrief\n")
    _real_git(repository, "add", "README.md")
    _real_git(repository, "commit", "-q", "-m", "lane work")
    tip = _real_git(repository, "rev-parse", "HEAD").stdout.strip()
    return repository, base, tip


def _brief_claim(
    base: str, *, branch: str = "codex/issue-258-brief", whole_reason: str | None = None
) -> protocol.ActiveClaim:
    return _store_claim_from_request(
        replace(
            request(
                issue=258,
                claim_id="brief-claim",
                branch=branch,
                scope=("README.md",),
                whole_reason=whole_reason,
            ),
            base=base,
        )
    )


def test_cli_brief_prints_body_claim_lane_tip_and_touched_files(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    repository, base, tip = _scratch_lane_repository(tmp_path)
    client = FakeForge()
    client.issue_references[258] = forge.ItemReference(
        forge.ItemState.OPEN, "Brief", "The item's own body."
    )
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    claim = _brief_claim(base, whole_reason="lane touches too much to split")
    _patch_store_write(monkeypatch, claim, ages={claim.claim_id: datetime(2026, 8, 20, tzinfo=UTC)})
    monkeypatch.chdir(repository)

    status = issue_claim.main(["--repo", REPOSITORY, "brief", "258"])

    assert status == 0
    assert capsys.readouterr().out.splitlines() == [
        "The item's own body.",
        "",
        "CLAIM",
        f"Codex Sol (builder) branch=codex/issue-258-brief base={base} 24h 0m old",
        "  README.md",
        "  whole: lane touches too much to split",
        "",
        "TIP",
        tip,
        "",
        "TOUCHED",
        "README.md",
    ]


def test_cli_brief_reports_no_active_claim_with_empty_tip_and_touched_files(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    repository, _base, _tip = _scratch_lane_repository(tmp_path)
    client = FakeForge()
    client.issue_references[258] = forge.ItemReference(
        forge.ItemState.OPEN, "Brief", "No claim yet."
    )
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    _patch_store_write(monkeypatch)
    monkeypatch.chdir(repository)

    status = issue_claim.main(["--repo", REPOSITORY, "brief", "258"])

    assert status == 0
    assert capsys.readouterr().out.splitlines() == [
        "No claim yet.",
        "",
        "CLAIM",
        "no active claim",
        "",
        "TIP",
        "",
        "TOUCHED",
    ]


def test_cli_brief_reports_branch_not_found_when_the_claim_branch_is_gone(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    repository, base, _tip = _scratch_lane_repository(tmp_path)
    client = FakeForge()
    client.issue_references[258] = forge.ItemReference(forge.ItemState.OPEN, "Brief", "Gone lane.")
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    claim = _brief_claim(base, branch="codex/issue-258-gone")
    _patch_store_write(monkeypatch, claim, ages={claim.claim_id: datetime(2026, 8, 20, tzinfo=UTC)})
    monkeypatch.chdir(repository)

    status = issue_claim.main(["--repo", REPOSITORY, "brief", "258"])

    assert status == 0
    assert capsys.readouterr().out.splitlines() == [
        "Gone lane.",
        "",
        "CLAIM",
        f"Codex Sol (builder) branch=codex/issue-258-gone base={base} 24h 0m old",
        "  README.md",
        "",
        "TIP",
        "branch not found",
        "",
        "TOUCHED",
    ]


def _unreadable_item_reference(_number: int) -> forge.ItemReference:
    raise ClaimError("simulated forge read failure")


def test_cli_brief_refuses_when_the_lane_tip_read_fails_outright(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #390 finding 9b: `rev-parse --verify --quiet`'s exit `1` is the
    one documented "does not resolve" outcome; any other nonzero exit --
    here, a simulated broken git -- is a tool failure, not a missing
    branch, and must refuse instead of printing `branch not found`."""
    repository, base, _tip = _scratch_lane_repository(tmp_path)
    client = FakeForge()
    client.issue_references[258] = forge.ItemReference(forge.ItemState.OPEN, "Brief", "Broken git.")
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    claim = _brief_claim(base)
    _patch_store_write(monkeypatch, claim, ages={claim.claim_id: datetime(2026, 8, 20, tzinfo=UTC)})
    monkeypatch.chdir(repository)
    _stub_one_git_call(
        monkeypatch,
        ["rev-parse", "--verify", "--quiet", claim.branch],
        exit_status=128,
        stderr="simulated git failure",
    )

    status = issue_claim.main(["--repo", REPOSITORY, "brief", "258"])

    assert status == 2
    assert capsys.readouterr().err == "ERROR: simulated git failure\n"


def test_cli_brief_json_names_a_failing_item_read_unavailable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """BRIEF-19 (issue #432): the item read is a forge call like any other,
    so a failure there is this command's own `unavailable`. It used to escape
    the handler and leave a `--json` caller with an empty stdout."""
    repository, base, _tip = _scratch_lane_repository(tmp_path)
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(client, "item_reference", _unreadable_item_reference)
    claim = _brief_claim(base)
    _patch_store_write(monkeypatch, claim, ages={claim.claim_id: datetime(2026, 8, 20, tzinfo=UTC)})
    monkeypatch.chdir(repository)

    status = issue_claim.main(["--repo", REPOSITORY, "brief", "258", "--json"])

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err == "ERROR: simulated forge read failure\n"
    assert json.loads(captured.out) == {
        "ok": False,
        "reason": "unavailable",
        "message": "simulated forge read failure",
    }


def test_cli_brief_json_prints_one_object_with_body_claim_tip_and_touched(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    repository, base, tip = _scratch_lane_repository(tmp_path)
    client = FakeForge()
    client.issue_references[258] = forge.ItemReference(
        forge.ItemState.OPEN, "Brief", "The item's own body."
    )
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    claim = _brief_claim(base)
    _patch_store_write(monkeypatch, claim, ages={claim.claim_id: datetime(2026, 8, 20, tzinfo=UTC)})
    monkeypatch.chdir(repository)

    status = issue_claim.main(["--repo", REPOSITORY, "brief", "258", "--json"])

    assert status == 0
    output = capsys.readouterr().out
    expected = {
        "ok": True,
        "reason": "composed",
        "body": "The item's own body.",
        "claim": {
            "agent": "Codex Sol",
            "role": "builder",
            "branch": "codex/issue-258-brief",
            "base": base,
            "scope": ["README.md"],
            "whole": None,
            "age": "24h 0m",
        },
        "tip": tip,
        "touched": ["README.md"],
    }
    assert output == json.dumps(expected) + "\n"
    assert json.loads(output) == expected


_DEFAULT_BRIEF_TOML = '[build]\nrules = ["Stay in scope."]\nchecks = ["ruff check ."]\n'

# Captured at import time, before any test's monkeypatching runs.
_REAL_PATH_IS_TRACKED = checkout.path_is_tracked


def _write_repository_agent_claim_configs(
    toplevel: Path, *, brief_content: str = _DEFAULT_BRIEF_TOML, brief_tracked: bool = True
) -> None:
    """`.agent-claim/board.toml` and `.agent-claim/brief.toml`, both inside a
    real, initialized git repository at `toplevel`, the resolved checkout
    toplevel -- under this suite's autouse `_isolate_git_toplevel`
    (`tests/conftest.py`), that root is `tmp_path`, never the scratch lane
    repository nested inside it. `board.toml` is always tracked for real, the
    same proof `test_checkout.py`'s `_tracked_board_config` gives its own
    tracked-file gate: `_LazyForge` reads it (`_board_config`) on every
    happy-path `--step` scenario below, so it must genuinely exist in the
    index rather than lean on the module's blanket `stub_board_config_tracked`
    (issue #324 review). `brief_tracked=False` leaves `brief.toml` on disk
    outside git's index, for the genuine untracked-file refusal."""
    _real_git(toplevel, "init", "-q", "-b", "main")
    _real_git(toplevel, "config", "user.name", "Test")
    _real_git(toplevel, "config", "user.email", "test@example.com")
    agent_claim = toplevel / ".agent-claim"
    agent_claim.mkdir(exist_ok=True)
    (agent_claim / "board.toml").write_text("")
    _real_git(toplevel, "add", board.CONFIG_PATH.as_posix())
    (agent_claim / "brief.toml").write_text(brief_content)
    if brief_tracked:
        _real_git(toplevel, "add", board.BRIEF_CONFIG_PATH.as_posix())
    _real_git(toplevel, "commit", "-q", "-m", "add .agent-claim configs")


def _brief_step_scenario(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, *, content: str = _DEFAULT_BRIEF_TOML
) -> tuple[str, str]:
    """The scratch lane repository, `.agent-claim/board.toml` and
    `.agent-claim/brief.toml` both genuinely `git add`-ed at the resolved
    toplevel, and issue #258's own live claim -- the one arrangement
    `--step`'s text, `--json`, and no-`--step` cases all share (issue #324).
    Reads both configs' real tracked status instead of the file's blanket
    autouse stub, since `_LazyForge` reads `board.toml`'s own tracked-file
    gate once the brief.toml check passes (issue #324 review). Returns
    `(base, tip)`; the repository itself is only `monkeypatch.chdir`-ed into,
    never asserted on."""
    repository, base, tip = _scratch_lane_repository(tmp_path)
    _write_repository_agent_claim_configs(tmp_path, brief_content=content)
    monkeypatch.setattr(checkout, "path_is_tracked", _REAL_PATH_IS_TRACKED)
    client = FakeForge()
    client.issue_references[258] = forge.ItemReference(
        forge.ItemState.OPEN, "Brief", "The item's own body."
    )
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    claim = _brief_claim(base)
    _patch_store_write(monkeypatch, claim, ages={claim.claim_id: datetime(2026, 8, 20, tzinfo=UTC)})
    monkeypatch.chdir(repository)
    return base, tip


def _brief_touched_lines(base: str, tip: str, *step_lines: str) -> list[str]:
    """The four sections `aco brief` always prints for issue #258's live
    claim scenario, plus whichever `RULES`/`CHECKS` lines a `--step` case
    appends -- the one shape `_brief_step_scenario`'s callers all assert."""
    return [
        "The item's own body.",
        "",
        "CLAIM",
        f"Codex Sol (builder) branch=codex/issue-258-brief base={base} 24h 0m old",
        "  README.md",
        "",
        "TIP",
        tip,
        "",
        "TOUCHED",
        "README.md",
        *step_lines,
    ]


_BRIEF_TOML_ALL_STEPS = (
    '[build]\nrules = ["Stay in scope."]\nchecks = ["ruff check ."]\n'
    "\n"
    '[review]\nrules = ["Mark every finding blocking or follow-up."]\nchecks = []\n'
    "\n"
    '[fix]\nrules = ["Resolve only what the review marked blocking."]\n'
    'checks = ["ruff check ."]\n'
    "\n"
    "[land]\nrules = []\nchecks = []\n"
)


@pytest.mark.parametrize(
    ("step", "step_lines"),
    [
        ("build", ("", "RULES", "Stay in scope.", "", "CHECKS", "ruff check .")),
        (
            "review",
            ("", "RULES", "Mark every finding blocking or follow-up.", "", "CHECKS"),
        ),
        (
            "fix",
            (
                "",
                "RULES",
                "Resolve only what the review marked blocking.",
                "",
                "CHECKS",
                "ruff check .",
            ),
        ),
        ("land", ("", "RULES", "", "CHECKS")),
    ],
    ids=["build", "review", "fix", "land-empty"],
)
def test_cli_brief_step_prints_this_repository_own_rules_and_checks_by_step(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    step: str,
    step_lines: tuple[str, ...],
) -> None:
    """Issue #324: `--step <step>` appends `.agent-claim/brief.toml`'s own
    `RULES` and `CHECKS`, one line per that section's own entries, after the
    four sections a plain brief always prints -- for every step the file can
    name, including one (`[land]`) that names neither rules nor checks at
    all (BRIEF-12, BRIEF-13)."""
    base, tip = _brief_step_scenario(monkeypatch, tmp_path, content=_BRIEF_TOML_ALL_STEPS)
    arguments = ["--repo", REPOSITORY, "brief", "258", "--step", step]

    status = issue_claim.main(arguments)

    assert status == 0
    assert capsys.readouterr().out.splitlines() == _brief_touched_lines(base, tip, *step_lines)


def test_cli_brief_without_step_ignores_the_tracked_brief_config(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Without `--step`, a tracked `.agent-claim/brief.toml`'s presence or
    content changes nothing: `brief` still prints exactly its own four
    sections (BRIEF-16)."""
    base, tip = _brief_step_scenario(monkeypatch, tmp_path)
    arguments = ["--repo", REPOSITORY, "brief", "258"]

    status = issue_claim.main(arguments)

    assert status == 0
    assert capsys.readouterr().out.splitlines() == _brief_touched_lines(base, tip)


def test_cli_brief_step_json_adds_rules_and_checks_to_the_existing_object(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    base, tip = _brief_step_scenario(monkeypatch, tmp_path)
    arguments = ["--repo", REPOSITORY, "brief", "258", "--step", "build"]

    status = issue_claim.main([*arguments, "--json"])

    assert status == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "reason": "composed",
        "body": "The item's own body.",
        "claim": {
            "agent": "Codex Sol",
            "role": "builder",
            "branch": "codex/issue-258-brief",
            "base": base,
            "scope": ["README.md"],
            "whole": None,
            "age": "24h 0m",
        },
        "tip": tip,
        "touched": ["README.md"],
        "rules": ["Stay in scope."],
        "checks": ["ruff check ."],
    }


@pytest.mark.parametrize(
    "brief_toml_present",
    [False, True],
    ids=["missing", "untracked"],
)
def test_cli_brief_step_refuses_with_no_usable_brief_config(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    brief_toml_present: bool,
) -> None:
    """A `.agent-claim/brief.toml` this repository cannot actually read from
    -- absent entirely, or genuinely present on disk but never `git add`-ed
    -- refuses the same way before ever reading the item's body (the same
    tracked-file requirement `_board_config` enforces for `board.toml`'s
    storage pin), proven against the real `path_is_tracked` rather than a
    hand-rolled stub."""
    repository, _base, _tip = _scratch_lane_repository(tmp_path)
    monkeypatch.setattr(checkout, "path_is_tracked", _REAL_PATH_IS_TRACKED)
    if brief_toml_present:
        _write_repository_agent_claim_configs(tmp_path, brief_tracked=False)
    else:
        _real_git(tmp_path, "init", "-q", "-b", "main")

    def unused(_self: FakeForge, _number: int) -> forge.ItemReference:
        pytest.fail("brief --step must refuse before reading the item's body")

    client = FakeForge()
    monkeypatch.setattr(FakeForge, "item_reference", unused)
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.chdir(repository)
    arguments = ["--repo", REPOSITORY, "brief", "258", "--step", "build", "--json"]

    status = issue_claim.main(arguments)

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err == "ERROR: no .agent-claim/brief.toml in the repository\n"
    _assert_json_refusal_object(captured.err, captured.out, reason="unavailable")


def test_cli_brief_refuses_a_non_github_canonical_remote_by_host(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`brief` is a forge command through the same `_LazyForge` gate `board`
    uses (issue #245): a canonical remote on any host but GitHub refuses by
    that host's own name, before ever calling `discover_repository`/`gh` --
    the same refusal `board` gives for the same remote."""
    monkeypatch.setattr(
        checkout, "remote_url", lambda remote: "file:///srv/git/agent-coordination.git"
    )

    def unused(*_args: object, **_kwargs: object) -> forge.RepositoryId:
        pytest.fail("brief must refuse the host before ever calling discover_repository")

    monkeypatch.setattr(github, "discover_repository", unused)

    status = issue_claim.main(["brief", "258", "--json"])

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err == "ERROR: no forge adapter for host file\n"
    _assert_json_refusal_object(captured.err, captured.out, reason="unavailable")


def test_cli_brief_reports_invalid_usage_when_repo_is_given_under_state_ref(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """BRIEF-09 cites `specs/storage-pin.spec.md`'s PIN-04: under `storage =
    state-ref`, `--repo` is the wrong flag for this run, reported through
    `--json` as `invalid_usage` -- brief's one refusal that is not this
    environment being generically `unavailable`."""
    _write_state_ref_pin(tmp_path)

    status = issue_claim.main(["--repo", "acme/items", "brief", "258", "--json"])

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err == "ERROR: --repo is meaningless under storage = state-ref\n"
    _assert_json_refusal_object(captured.err, captured.out, reason="invalid_usage")


@pytest.mark.parametrize(
    ("value", "number"),
    [("aco-3f9a2c", 0x3F9A2C), ("#42", 42), ("42", 42)],
)
def test_rescope_parses_every_item_reference_syntax(value: str, number: int) -> None:
    """Issue #285 proof 5: `rescope`'s `issue` argument is wired to
    `board.parse_item_reference` (moved from `cli._parse_item_ref` by issue
    #304; that function's own grammar proof lives in `tests/test_board.py`)."""
    rescoped = issue_claim._parser().parse_args(["rescope", value, "--add", "src"])

    assert rescoped.issue == number


def test_main_refuses_a_malformed_item_reference_before_ever_dispatching(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`board.parse_item_reference` runs as an argparse `type=` inside
    `parse_args`, so its refusal must reach `main`'s own error rendering
    rather than an argparse usage error or an unhandled exception
    (issue #285)."""
    status = issue_claim.main(["claim", "not-an-item", "--scope", "README"])

    assert status == 2
    assert "is not an item reference" in capsys.readouterr().err


def test_item_new_refuses_under_github_storage(capsys: pytest.CaptureFixture[str]) -> None:
    """Issue #285 proof 6: under `storage = "github"` (the default, and
    what an unconfigured toplevel reads), `item new` refuses by name rather
    than opening a GitHub issue on the repository's behalf."""
    status = issue_claim.main(["item", "new", "--title", "X"])

    assert status == 2
    assert capsys.readouterr().err == "ERROR: items live on the forge; open the issue there\n"


def test_item_new_json_reports_ok_reason_created(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #425: a fresh item's own `--json` success carries `reason:
    "created"` first, ahead of the minted `item`/`number` pair."""
    _write_state_ref_pin(tmp_path)
    client = FakeForge(repository=forge.RepositoryId("file", (), str(tmp_path)))
    item_id = items.format_item_id(42)
    monkeypatch.setattr(client, "create_item", lambda **_kwargs: item_id, raising=False)
    monkeypatch.setattr(
        issue_claim, "_state_ref_forge", lambda _repo, _remote, *, directory=None: client
    )

    status = issue_claim.main(["item", "new", "--title", "Fresh Item", "--json"])

    assert status == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "reason": "created",
        "item": item_id,
        "number": 42,
    }


def test_item_edit_refuses_under_github_storage(capsys: pytest.CaptureFixture[str]) -> None:
    """Issue #287 proof 7: under `storage = "github"` (the default), `item
    edit` refuses by name -- forge issues are edited on the forge, never
    governed by aco -- before it ever reads stdin (no `sys.stdin` stand-in
    is installed here, so a stray read would surface as a test failure)."""
    status = issue_claim.main(["item", "edit", "42"])

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: forge issues are edited on the forge; aco never governs them\n"
    )


def test_item_edit_size_writes_the_top_level_field_under_github_storage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #357 proof 2: `item edit --size M` writes through the generic
    `ForgeWriter.update_item_body` both storages already implement, so it
    reaches a `github`-stored item too -- unlike the whole-body `item edit`
    above, which refuses under `storage = "github"` by name."""
    client = _client_with_item(
        monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(MINIMAL_BLOCK_TOML)
    )

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "item", "edit", str(RULE_ITEM), "--size", "M"]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == f"EDITED #{RULE_ITEM} size=M\n"
    assert body.locate_agent_claim_block(client.item_bodies[RULE_ITEM]).data["size"] == "M"


def test_item_edit_size_json_reports_the_item_and_size(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _client_with_item(monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(MINIMAL_BLOCK_TOML))

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "item",
            "edit",
            str(RULE_ITEM),
            "--size",
            "S",
            "--json",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "reason": "edited",
        "item": RULE_ITEM,
        "size": "S",
    }


def test_item_edit_size_refuses_an_invalid_value_before_any_write(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        issue_claim.main(["item", "edit", "42", "--size", "XL"])

    assert exited.value.code == 2
    assert "invalid choice" in capsys.readouterr().err


def test_item_edit_size_refuses_through_the_shared_precondition_failed_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #425: `item edit --size`'s own runtime refusal -- here the
    forge refusing `update_item_body` -- reports through the shared
    envelope as `precondition_failed`."""
    client = _client_with_item(
        monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(MINIMAL_BLOCK_TOML)
    )
    client.capability_overrides[forge.ForgeOperation.UPDATE_ITEM_BODY] = forge.Capability.READ_ONLY

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "item", "edit", str(RULE_ITEM), "--size", "M", "--json"]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "this forge cannot update_item_body; item edit --size by hand" in captured.err
    _assert_json_refusal_object(captured.err, captured.out, reason="precondition_failed")


def test_item_edit_whole_writes_the_top_level_field_under_github_storage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #399: `item edit --whole REASON` writes through the same
    generic `ForgeWriter.update_item_body` `--size` already uses, mirroring
    `test_item_edit_size_writes_the_top_level_field_under_github_storage`."""
    reason = "the four adapters share one lock"
    client = _client_with_item(
        monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(MINIMAL_BLOCK_TOML)
    )

    exit_code = issue_claim.main(
        ["--repo", REPOSITORY, "item", "edit", str(RULE_ITEM), "--whole", reason]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == f"EDITED #{RULE_ITEM} whole={reason}\n"
    assert body.locate_agent_claim_block(client.item_bodies[RULE_ITEM]).data["whole"] == reason


def test_item_edit_whole_json_reports_the_item_and_reason(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    reason = "the four adapters share one lock"
    _client_with_item(monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(MINIMAL_BLOCK_TOML))

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "item",
            "edit",
            str(RULE_ITEM),
            "--whole",
            reason,
            "--json",
        ]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "reason": "edited",
        "item": RULE_ITEM,
        "whole": reason,
    }


def test_item_edit_whole_refuses_through_the_shared_precondition_failed_envelope(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #425: `item edit --whole`'s own runtime refusal -- here the
    forge refusing `update_item_body` -- reports through the shared
    envelope as `precondition_failed`."""
    client = _client_with_item(
        monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(MINIMAL_BLOCK_TOML)
    )
    client.capability_overrides[forge.ForgeOperation.UPDATE_ITEM_BODY] = forge.Capability.READ_ONLY

    exit_code = issue_claim.main(
        [
            "--repo",
            REPOSITORY,
            "item",
            "edit",
            str(RULE_ITEM),
            "--whole",
            "a reason",
            "--json",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "this forge cannot update_item_body; item edit --whole by hand" in captured.err
    _assert_json_refusal_object(captured.err, captured.out, reason="precondition_failed")


def test_item_close_refuses_under_github_storage(capsys: pytest.CaptureFixture[str]) -> None:
    """Issue #289 proof 6: under `storage = "github"` (the default), `item
    close` refuses by name -- the forge closes its own issues, aco never
    governs them -- before it ever resolves a forge or reads a claim."""
    status = issue_claim.main(["item", "close", "42"])

    assert status == 2
    assert capsys.readouterr().err == "ERROR: the forge closes its issues; aco never governs them\n"


def test_item_close_prints_json_under_the_state_ref_pin(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #289: `item close ITEM --json` under `storage = "state-ref"`
    prints the shared envelope, `reason: "closed"`, then `{"item", "number",
    "closed_at", "parent_closable"}`, and returns before the plain-text
    `CLOSED`/`freed:` lines, the branch
    `test_item_close_refuses_under_github_storage`'s refusal never reaches.
    `parent_closable` (issue #348) is `null` here: `42` names no parent on
    this fake."""
    _write_state_ref_pin(tmp_path)
    client = FakeForge(repository=forge.RepositoryId("file", (), str(tmp_path)))
    client.issue_references[42] = forge.ItemReference(
        forge.ItemState.OPEN, "Title", "Body text.\n", False
    )
    monkeypatch.setattr(client, "close_item", lambda _number: "2026-09-16T12:00:00Z", raising=False)
    monkeypatch.setattr(
        issue_claim, "_state_ref_forge", lambda _repo, _remote, *, directory=None: client
    )

    status = issue_claim.main(["item", "close", "42", "--json"])

    assert status == 0
    assert json.loads(capsys.readouterr().out) == {
        "ok": True,
        "reason": "closed",
        "item": items.format_item_id(42),
        "number": 42,
        "closed_at": "2026-09-16T12:00:00Z",
        "parent_closable": None,
    }


def test_item_show_reads_the_fake_forge_body_under_github_storage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #285 proof 6: under `storage = "github"`, `item show` reads
    the issue body through the ordinary forge reader -- the same output
    shape `state-ref` prints, an id encoded from the plain issue number."""
    client = FakeForge()
    client.issue_references[42] = forge.ItemReference(
        forge.ItemState.OPEN, "Title", "Body text.\n", False
    )
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)

    status = issue_claim.main(["--repo", REPOSITORY, "item", "show", "42"])

    assert status == 0
    expected_id = items.format_item_id(42)
    assert (
        capsys.readouterr().out
        == f"{expected_id} · #42 · open · parent none · origin none\nBody text.\n"
    )


def test_item_show_as_json_reads_the_fake_forge_body_under_github_storage(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeForge()
    client.issue_references[42] = forge.ItemReference(
        forge.ItemState.OPEN, "Title", "Body text.\n", False
    )
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)

    status = issue_claim.main(["--repo", REPOSITORY, "item", "show", "42", "--json"])

    assert status == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {
        "ok": True,
        "reason": "shown",
        "item": items.format_item_id(42),
        "number": 42,
        "state": "open",
        "parent": None,
        "origin": None,
        "body": "Body text.\n",
    }


def test_item_show_refuses_an_unknown_id(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeForge()
    client.issue_references[42] = forge.ItemReference(forge.ItemState.MISSING)
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)

    status = issue_claim.main(["--repo", REPOSITORY, "item", "show", "42"])

    assert status == 2
    assert capsys.readouterr().err == f"ERROR: #42 does not exist in {REPOSITORY}\n"


def test_item_show_refuses_when_the_parent_read_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #425 review: the parent read the header needs is inside `item
    show`'s own refusal boundary, so a forge that fails it reports the same
    envelope, never an error escaping past every `--json` printer."""
    sentence = "GitHub returned a malformed parent issue"

    def prepare() -> FakeForge:
        client = FakeForge()
        client.issue_references[42] = forge.ItemReference(
            forge.ItemState.OPEN, "Title", "Body text.\n", False
        )

        def failing_parent_read(_number: int) -> board.ParentIssue | None:
            raise forge.ForgeMalformedResponseError(sentence)

        monkeypatch.setattr(client, "parent_issue", failing_parent_read)
        monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
        return client

    prepare()
    text_status = issue_claim.main(["--repo", REPOSITORY, "item", "show", "42"])
    text = capsys.readouterr()

    prepare()
    json_status = issue_claim.main(["--repo", REPOSITORY, "item", "show", "42", "--json"])
    envelope = capsys.readouterr()

    assert (text_status, json_status) == (2, 2)
    assert (text.out, text.err) == ("", f"ERROR: {sentence}\n")
    assert envelope.err == text.err
    _assert_json_refusal_object(envelope.err, envelope.out, reason="precondition_failed")


def _item_new_github_storage_refusal(
    _monkeypatch: pytest.MonkeyPatch, _tmp_path: Path
) -> list[str]:
    return ["item", "new", "--title", "X", "--json"]


def _item_edit_github_storage_refusal(
    _monkeypatch: pytest.MonkeyPatch, _tmp_path: Path
) -> list[str]:
    return ["item", "edit", "42", "--json"]


def _item_close_github_storage_refusal(
    _monkeypatch: pytest.MonkeyPatch, _tmp_path: Path
) -> list[str]:
    return ["item", "close", "42", "--json"]


def _item_show_unknown_id_refusal(monkeypatch: pytest.MonkeyPatch, _tmp_path: Path) -> list[str]:
    client = FakeForge()
    client.issue_references[42] = forge.ItemReference(forge.ItemState.MISSING)
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    return ["--repo", REPOSITORY, "item", "show", "42", "--json"]


@pytest.mark.parametrize(
    "build_arguments",
    [
        _item_new_github_storage_refusal,
        _item_edit_github_storage_refusal,
        _item_close_github_storage_refusal,
        _item_show_unknown_id_refusal,
    ],
    ids=["new", "edit", "close", "show"],
)
def test_item_refuses_through_the_shared_precondition_failed_envelope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    build_arguments: Callable[[pytest.MonkeyPatch, Path], list[str]],
) -> None:
    """Issue #425: every runtime refusal from `item new`/`edit`/`close`/
    `show`, reached with `--json`, reports through the shared envelope as
    `precondition_failed` -- never a bare object."""
    arguments = build_arguments(monkeypatch, tmp_path)

    status = issue_claim.main(arguments)

    assert status == 2
    captured = capsys.readouterr()
    _assert_json_refusal_object(captured.err, captured.out, reason="precondition_failed")


def test_item_edit_json_reports_body_invalid_with_defects(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #425: `item edit`'s own malformed piped body reports
    `reason: "body_invalid"`, with `body --check`'s own `defects` list as a
    structured sibling, before any forge is ever resolved."""
    _write_state_ref_pin(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO("no block"))

    status = issue_claim.main(["item", "edit", "42", "--json"])

    assert status == 2
    captured = capsys.readouterr()
    defect = "body malformed: agent-claim: no agent-claim block"
    assert captured.err == f"ERROR: {defect}\n"
    assert json.loads(captured.out) == {
        "ok": False,
        "reason": "body_invalid",
        "defects": [defect],
        "message": defect,
    }


# `reset` (issue #298): real bare `file://` remotes and real checkouts
# throughout -- `_reset_state` orchestrates `store`'s own git chokepoint end
# to end, so a mock of `store` would only prove this file's own fakes agree
# with each other, never that a real lease, a real bundle, or a real
# lineage stamp behaves as the printed line claims. Captured at import time,
# before the file's own autouse `_stub_store_write` ever runs, so each
# `reset` test can hand the real functions straight back to `store`.
_REAL_STORE_FETCH_STATE = store.fetch_state
_REAL_STORE_PEEK_STATE = store.peek_state
_REAL_STORE_COMMIT_TRANSITION = store.commit_transition
_REAL_STORE_CLAIM_AGES = store.claim_ages
_REAL_STORE_CLAIM_LIFECYCLE = store.claim_lifecycle


def _use_real_store(monkeypatch: pytest.MonkeyPatch) -> None:
    """Undo this file's autouse in-memory `store` fake for one `reset` test:
    `reset` proves real git behaviour end to end, never the fake's own
    agreement with itself."""
    monkeypatch.setattr(store, "fetch_state", _REAL_STORE_FETCH_STATE)
    monkeypatch.setattr(store, "peek_state", _REAL_STORE_PEEK_STATE)
    monkeypatch.setattr(store, "commit_transition", _REAL_STORE_COMMIT_TRANSITION)
    monkeypatch.setattr(store, "claim_ages", _REAL_STORE_CLAIM_AGES)
    monkeypatch.setattr(store, "claim_lifecycle", _REAL_STORE_CLAIM_LIFECYCLE)


def _reset_repository(tmp_path: Path) -> tuple[Path, Path]:
    """A real checkout with a real `origin` remote pointing at a real bare
    repository. `board.toml` is never written: `_isolate_git_toplevel`
    (conftest) resolves the repository's toplevel to `tmp_path`, where no
    `.agent-claim/board.toml` exists, so `board.load_config` falls back to
    its own default `canonical_remote = "origin"` -- exactly this remote's
    name."""
    bare_remote = tmp_path / "remote.git"
    _real_git(tmp_path, "init", "--bare", "-q", "-b", "main", str(bare_remote))
    repository = tmp_path / "repo"
    repository.mkdir()
    _real_git(repository, "init", "-q", "-b", "main")
    _real_git(repository, "config", "user.email", "test@example.com")
    _real_git(repository, "config", "user.name", "Test")
    (repository / "README.md").write_text("hello\n")
    _real_git(repository, "add", "README.md")
    _real_git(repository, "commit", "-q", "-m", "initial")
    _real_git(repository, "remote", "add", "origin", str(bare_remote))
    return repository, bare_remote


def _real_claim_intent(issue: int) -> protocol.ClaimIntent:
    return protocol.ClaimIntent(
        identity=protocol.IssueIdentity(issue),
        agent="Codex Sol",
        role="builder",
        base=protocol.ObjectId("c" * 40),
        branch=f"codex/issue-{issue}-reset",
        scope=(f"src/issue-{issue}.py",),
        claim_id=protocol.ClaimId(f"claim-{issue}"),
        operation_id=f"op-{issue}",
    )


def _force_advance_state_ref(repository: Path, bare_remote: Path, tip: str) -> str:
    """Simulate someone else's push landing on `STATE_REF` between reset's
    own read of `tip` and its lease-guarded delete -- the exact race
    `--force-with-lease` exists to catch."""
    tree = _real_git(repository, "rev-parse", f"{tip}^{{tree}}").stdout.strip()
    new_commit = _real_git(repository, "commit-tree", tree, "-p", tip, "-m", "moved").stdout.strip()
    _real_git(repository, "push", "--force", str(bare_remote), f"{new_commit}:{store.STATE_REF}")
    return new_commit


def _lineage_observation(repository: Path) -> tuple[protocol.ObjectId | None, str | None]:
    """This worktree's lineage stamp and fetch anchor -- the two
    per-worktree values a reset dry run, a live-claim refusal, or a failed
    export must leave byte-identical to whatever `fetch_state` last wrote
    (issue #298, 19.09.2026 gate finding 2)."""
    stamp = store._read_lineage_stamp(repository)
    anchor = store._run_git(repository, ["rev-parse", store._FETCH_ANCHOR_REF])
    return stamp, anchor.stdout.decode().strip() if anchor.exit_status == 0 else None


def test_cli_reset_dry_run_prints_five_would_lines_and_changes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _use_real_store(monkeypatch)
    repository, bare_remote = _reset_repository(tmp_path)
    tip = store.bootstrap(worktree=repository, remote=str(bare_remote))
    store.fetch_state(worktree=repository, remote=str(bare_remote))
    lineage_before = _lineage_observation(repository)
    assert lineage_before != (None, None)
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    monkeypatch.chdir(repository)
    monkeypatch.setattr(issue_claim, "datetime", FixedDateTime)

    status = issue_claim.main(["reset", "--export-dir", str(export_dir)])

    assert status == 0
    bundle_path = export_dir / f"aco-state-repo-2026-08-21-{tip[:12]}.bundle"
    assert capsys.readouterr().out.splitlines() == [
        f"would: export {store.STATE_REF} at {tip} to {bundle_path} "
        f"(restore with: git fetch {bundle_path} {store.EXPORT_BUNDLE_REF}:{store.STATE_REF})",
        f"would: delete {store.STATE_REF} on origin (lease {tip})",
        f"would: no local {store.STATE_REF} to delete",
        "would: clear lineage stamps and fetch anchors in 1 worktree",
        "would: bootstrap a fresh empty state",
    ]
    assert not bundle_path.exists()
    assert (
        _real_git(
            repository, "ls-remote", "--exit-code", str(bare_remote), store.STATE_REF
        ).stdout.split("\t")[0]
        == tip
    )
    assert not store.local_state_ref_exists(repository)
    assert _lineage_observation(repository) == lineage_before


def test_cli_reset_confirm_exports_a_verifiable_bundle_and_bootstraps_a_fresh_ref(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _use_real_store(monkeypatch)
    repository, bare_remote = _reset_repository(tmp_path)
    tip = store.bootstrap(worktree=repository, remote=str(bare_remote))
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    monkeypatch.chdir(repository)
    monkeypatch.setattr(issue_claim, "datetime", FixedDateTime)

    status = issue_claim.main(["reset", "--confirm", "--export-dir", str(export_dir)])

    assert status == 0
    lines = capsys.readouterr().out.splitlines()
    bundle_path = export_dir / f"aco-state-repo-2026-08-21-{tip[:12]}.bundle"
    assert lines[0] == (
        f"exported {store.STATE_REF} at {tip} to {bundle_path} "
        f"(restore with: git fetch {bundle_path} {store.EXPORT_BUNDLE_REF}:{store.STATE_REF})"
    )
    assert lines[1] == f"deleted {store.STATE_REF} on origin (lease {tip})"
    # `export_state_bundle` no longer touches the shared `STATE_REF` at all
    # (19.09.2026 REVISE findings 1+2), so there is never a local one left
    # for this step to delete.
    assert lines[2] == f"no local {store.STATE_REF} to delete"
    assert lines[3] == "cleared lineage stamps and fetch anchors in 1 worktree"
    assert lines[4].startswith("bootstrapped a fresh empty state at ")
    fresh_tip = lines[4].removeprefix("bootstrapped a fresh empty state at ")
    assert fresh_tip != tip
    _real_git(repository, "bundle", "verify", str(bundle_path))
    heads = _real_git(repository, "bundle", "list-heads", str(bundle_path)).stdout
    assert heads.strip() == f"{tip} {store.EXPORT_BUNDLE_REF}"
    probe = _real_git(repository, "ls-remote", "--exit-code", str(bare_remote), store.STATE_REF)
    assert probe.stdout.split("\t")[0] == fresh_tip
    assert not store.local_state_ref_exists(repository)


def test_cli_reset_refuses_when_a_claim_is_live_and_touches_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _use_real_store(monkeypatch)
    repository, bare_remote = _reset_repository(tmp_path)
    store.bootstrap(worktree=repository, remote=str(bare_remote))
    store.commit_transition(
        worktree=repository,
        remote=str(bare_remote),
        subject=store.ClaimTransitionSubject("claim issue 42", item="42"),
        intent=_real_claim_intent(42),
    )
    tip_before = _real_git(repository, "ls-remote", str(bare_remote), store.STATE_REF).stdout.split(
        "\t"
    )[0]
    lineage_before = _lineage_observation(repository)
    assert lineage_before != (None, None)
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    monkeypatch.chdir(repository)

    status = issue_claim.main(["reset", "--confirm", "--export-dir", str(export_dir)])

    assert status == 2
    out = capsys.readouterr().out
    assert "CLAIMED issue #42" in out
    assert "codex/issue-42-reset" in out
    tip_after = _real_git(repository, "ls-remote", str(bare_remote), store.STATE_REF).stdout.split(
        "\t"
    )[0]
    assert tip_after == tip_before
    assert list(export_dir.iterdir()) == []
    assert not store.local_state_ref_exists(repository)
    assert _lineage_observation(repository) == lineage_before


def test_cli_reset_no_export_skips_the_bundle_but_still_resets(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _use_real_store(monkeypatch)
    repository, bare_remote = _reset_repository(tmp_path)
    tip = store.bootstrap(worktree=repository, remote=str(bare_remote))
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    monkeypatch.chdir(repository)

    status = issue_claim.main(
        ["reset", "--confirm", "--no-export", "--export-dir", str(export_dir)]
    )

    assert status == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"skipped export (--no-export): {store.STATE_REF} at {tip} not saved"
    assert list(export_dir.iterdir()) == []


def test_cli_reset_dry_run_reports_nothing_to_export_or_delete_when_the_ref_never_existed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _use_real_store(monkeypatch)
    repository, _bare_remote = _reset_repository(tmp_path)
    monkeypatch.chdir(repository)

    status = issue_claim.main(["reset"])

    assert status == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"would: nothing to export: {store.STATE_REF} does not exist on origin"
    assert lines[1] == f"would: nothing to delete on origin: {store.STATE_REF} does not exist"


def test_cli_reset_confirm_deletes_a_local_ref_a_foreign_tool_left_behind(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _use_real_store(monkeypatch)
    repository, bare_remote = _reset_repository(tmp_path)
    tip = store.bootstrap(worktree=repository, remote=str(bare_remote))
    _real_git(repository, "update-ref", store.STATE_REF, tip)
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    monkeypatch.chdir(repository)

    status = issue_claim.main(["reset", "--confirm", "--export-dir", str(export_dir)])

    assert status == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[2] == f"deleted local {store.STATE_REF}"
    assert not store.local_state_ref_exists(repository)
    probe = _real_git(repository, "ls-remote", "--exit-code", str(bare_remote), store.STATE_REF)
    assert probe.stdout.split("\t")[0] != tip


def test_cli_reset_export_failure_leaves_the_ref_untouched(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _use_real_store(monkeypatch)
    repository, bare_remote = _reset_repository(tmp_path)
    tip = store.bootstrap(worktree=repository, remote=str(bare_remote))
    store.fetch_state(worktree=repository, remote=str(bare_remote))
    lineage_before = _lineage_observation(repository)
    assert lineage_before != (None, None)
    readonly_export_dir = tmp_path / "readonly"
    readonly_export_dir.mkdir()
    readonly_export_dir.chmod(0o500)
    monkeypatch.chdir(repository)

    try:
        status = issue_claim.main(["reset", "--confirm", "--export-dir", str(readonly_export_dir)])
    finally:
        readonly_export_dir.chmod(0o700)

    assert status == 2
    assert "cannot export" in capsys.readouterr().err
    probe = _real_git(repository, "ls-remote", "--exit-code", str(bare_remote), store.STATE_REF)
    assert probe.stdout.split("\t")[0] == tip
    assert not store.local_state_ref_exists(repository)
    assert _lineage_observation(repository) == lineage_before


def test_cli_reset_a_moved_remote_tip_leaves_the_bundle_intact_and_names_the_repair(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _use_real_store(monkeypatch)
    repository, bare_remote = _reset_repository(tmp_path)
    tip = store.bootstrap(worktree=repository, remote=str(bare_remote))
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    monkeypatch.chdir(repository)
    real_export = store.export_state_bundle
    moved_tip = ""

    def export_then_move_remote(
        *, worktree: Path, tip: protocol.ObjectId, destination: Path
    ) -> Path:
        nonlocal moved_tip
        result = real_export(worktree=worktree, tip=tip, destination=destination)
        moved_tip = _force_advance_state_ref(repository, bare_remote, tip)
        return result

    monkeypatch.setattr(store, "export_state_bundle", export_then_move_remote)

    status = issue_claim.main(["reset", "--confirm", "--export-dir", str(export_dir)])

    assert status == 2
    err = capsys.readouterr().err
    assert "cannot delete" in err
    assert "lease" in err
    # The repair line names the remote's *current* tip -- the one
    # re-probed after the failed push, never the stale tip that push
    # itself carried -- but never a manual lease command against it
    # (19.09.2026 REVISE finding 3): that tip was never validated against a
    # live claim, nor exported, so the only repair named is re-running
    # `aco reset --confirm` itself, which repeats both checks.
    assert f"the remote moved to {moved_tip}" in err
    assert "re-run `aco reset --confirm`" in err
    assert "--force-with-lease" not in err
    bundle_path = next(export_dir.iterdir())
    _real_git(repository, "bundle", "verify", str(bundle_path))
    # `export_state_bundle` never touches the shared `STATE_REF` at all
    # (19.09.2026 REVISE findings 1+2), so the remote-deletion failure
    # leaves nothing local to have been left behind either.
    assert not store.local_state_ref_exists(repository)
    probe = _real_git(repository, "ls-remote", "--exit-code", str(bare_remote), store.STATE_REF)
    assert probe.stdout.split("\t")[0] != tip


def test_cli_reset_restore_from_the_bundle_into_a_fresh_repository_recovers_status(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`reset` always refuses over a live claim, so the "old state" it can
    ever legitimately reset past is one whose claims have all already been
    released -- this claims, then releases, one issue before resetting, and
    proves the restored bundle carries that exact history forward (its
    `tip`, not just a same-shaped fresh one) rather than only an
    equally-empty `aco status` a plain bootstrap would print too.
    """
    _use_real_store(monkeypatch)
    repository, bare_remote = _reset_repository(tmp_path)
    store.bootstrap(worktree=repository, remote=str(bare_remote))
    store.commit_transition(
        worktree=repository,
        remote=str(bare_remote),
        subject=store.ClaimTransitionSubject("claim issue 42", item="42"),
        intent=_real_claim_intent(42),
    )
    store.commit_transition(
        worktree=repository,
        remote=str(bare_remote),
        subject=store.ClaimTransitionSubject("release issue 42", item="42"),
        intent=protocol.ReleaseIntent(
            claim_id=protocol.ClaimId("claim-42"),
            agent="Codex Sol",
            role="builder",
            outcome=protocol.AbandonedRelease("test cleanup"),
            operation_id="op-42-release",
        ),
    )
    pre_reset_tip = store.fetch_state(worktree=repository, remote=str(bare_remote)).tip
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    monkeypatch.chdir(repository)

    status = issue_claim.main(["reset", "--confirm", "--export-dir", str(export_dir)])
    assert status == 0
    capsys.readouterr()
    bundle_path = next(export_dir.iterdir())

    restored_remote = tmp_path / "restored-remote.git"
    _real_git(tmp_path, "init", "--bare", "-q", "-b", "main", str(restored_remote))
    # The bundle carries `EXPORT_BUNDLE_REF`'s name, not `STATE_REF`'s
    # (19.09.2026 REVISE findings 1+2) -- this is the exact command
    # `_reset_restore_command` prints, proven end to end here.
    _real_git(
        restored_remote,
        "fetch",
        "-q",
        str(bundle_path),
        f"{store.EXPORT_BUNDLE_REF}:{store.STATE_REF}",
    )
    fresh_repository = tmp_path / "fresh"
    fresh_repository.mkdir()
    _real_git(fresh_repository, "init", "-q", "-b", "main")
    _real_git(fresh_repository, "remote", "add", "origin", str(restored_remote))
    monkeypatch.chdir(fresh_repository)

    status = issue_claim.main(["status"])

    assert status == 0
    assert "UNCLAIMED repository" in capsys.readouterr().out
    restored_state = store.fetch_state(worktree=fresh_repository, remote=str(restored_remote))
    assert restored_state.tip == pre_reset_tip
    assert protocol.ClaimId("claim-42") in restored_state.consumed_ids


def test_cli_reset_recovers_from_a_deleted_ref_this_worktree_had_already_observed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The one scenario reset exists to unblock (issue #298, 19.09.2026 gate
    finding 1): this worktree fetched `STATE_REF` once and stamped it, then
    the ref was deleted directly on the remote -- exactly what an ordinary
    `fetch_state`'s own `_check_lineage`/absent-ref guard refuses on the
    next read. `reset` must still recover, and must clear the stale stamp
    so an ordinary read works again afterward."""
    _use_real_store(monkeypatch)
    repository, bare_remote = _reset_repository(tmp_path)
    store.bootstrap(worktree=repository, remote=str(bare_remote))
    store.fetch_state(worktree=repository, remote=str(bare_remote))
    assert store._read_lineage_stamp(repository) is not None
    _real_git(bare_remote, "update-ref", "-d", store.STATE_REF)
    with pytest.raises(protocol.StateLineageError):
        store.fetch_state(worktree=repository, remote=str(bare_remote))
    export_dir = tmp_path / "export"
    export_dir.mkdir()
    monkeypatch.chdir(repository)

    status = issue_claim.main(["reset", "--confirm", "--export-dir", str(export_dir)])

    assert status == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"nothing to export: {store.STATE_REF} does not exist on origin"
    assert lines[1] == f"nothing to delete on origin: {store.STATE_REF} does not exist"
    assert lines[2] == f"no local {store.STATE_REF} to delete"
    assert lines[3] == "cleared lineage stamps and fetch anchors in 1 worktree"
    assert lines[4].startswith("bootstrapped a fresh empty state at ")
    fresh_state = store.fetch_state(worktree=repository, remote=str(bare_remote))
    assert fresh_state.tip is not None
