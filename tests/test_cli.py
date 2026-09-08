from __future__ import annotations

import argparse
import io
import json
import os
import re
import runpy
import shlex
import subprocess
import sys
import threading
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import pytest

from agent_coordination import (
    __version__,
    board,
    checkout,
    forge,
    github,
    process,
    protocol,
    store,
)
from agent_coordination import cli as issue_claim
from agent_coordination.cli import (
    ClaimError,
    ClaimRequest,
    ClaimUnavailableError,
    InvalidClaimMarkerError,
    IssueIdentity,
    _status,
    _status_json,
    claims_conflict,
)

GitHubForge = github.GitHubForge

BASE = "a" * 40
REPOSITORY = "example/agent-claim"
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


def request(
    claim_id: str = "claim-a",
    agent: str = "Codex Sol",
    *,
    issue: int | None = 71,
    lane: bool = False,
    role: str = "builder",
    branch: str | None = None,
    scope: tuple[str, ...] = ("docs/COORDINATION.md", "scripts/issue_claim.py"),
    resource: str | None = None,
    resource_value: int | None = None,
    whole_reason: str | None = None,
) -> ClaimRequest:
    """Build a `ClaimRequest`, issue-identified by default or lane-identified via `lane=True`.

    `issue=None` implies `lane=True` (mirrors the CLI's own "omitted issue number
    means lane mode" rule) so parametrized tables can drive both identity kinds
    from one `issue`/`lane` axis without hand-building identities at every call site.
    """
    lane = lane or issue is None
    identity: protocol.ClaimIdentity
    if lane:
        identity = protocol.LaneIdentity()
    else:
        assert issue is not None, "lane is False only when the caller passed an issue"
        identity = protocol.IssueIdentity(issue)
    default_branch = f"docs/lane-{claim_id}" if lane else f"codex/issue-{issue}-claims"
    return ClaimRequest(
        identity=identity,
        agent=agent,
        role=role,
        base=BASE,
        branch=branch or default_branch,
        scope=scope,
        claim_id=claim_id,
        resource=resource,
        resource_value=resource_value,
        whole_reason=whole_reason,
    )


def projected_board(
    issues: tuple[board.Issue, ...],
    open_pull_requests: tuple[board.PullRequest, ...],
    recent_merged_pull_requests: tuple[board.PullRequest, ...],
    claims: tuple[protocol.ScopedClaim, ...],
    config: board.BoardConfig,
    *,
    repository: str = REPOSITORY,
    now: datetime | None = None,
    trunk_landings: tuple[datetime, ...] = (),
    children: Mapping[int, tuple[board.ChildItem, ...]] = MappingProxyType({}),
    dependencies: Mapping[int, tuple[board.IssueDependency, ...]] = MappingProxyType({}),
) -> board.Board:
    """`board.build_board` for scenarios that do not turn on which repository is projected."""
    observed_at = now or datetime(2026, 8, 21, tzinfo=UTC)
    return board.build_board(
        board.BoardBuildInputs(
            issues=issues,
            open_pull_requests=open_pull_requests,
            recent_merged_pull_requests=recent_merged_pull_requests,
            claims=claims,
            config=config,
            repository=repository,
            now=now,
            trunk_landings=trunk_landings,
            children=children,
            dependencies=dependencies,
            claim_ages={claim.claim_id: observed_at for claim in claims},
        )
    )


def _store_claim_from_request(
    claimed: ClaimRequest, *, opened_commit: str = BASE
) -> protocol.ActiveClaim:
    resource = None
    if claimed.resource is not None and claimed.resource_value is not None:
        resource = protocol.ResourceHold(claimed.resource, claimed.resource_value)
    return protocol.ActiveClaim(
        identity=claimed.identity,
        claim_id=protocol.ClaimId(claimed.claim_id),
        agent=claimed.agent,
        role=claimed.role,
        base=protocol.ObjectId(claimed.base),
        branch=claimed.branch,
        scope=claimed.scope,
        opened_commit=protocol.ObjectId(opened_commit),
        resource=resource,
        whole_reason=claimed.whole_reason,
    )


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
    issue_references: dict[int, forge.ItemReference] = field(default_factory=dict)
    issue_reference_lookups: list[int] = field(default_factory=list)
    created_children: list[tuple[int, str, str, board.ItemKind]] = field(default_factory=list)
    next_created_child_number: int = 900
    item_bodies: dict[int, str] = field(default_factory=dict)
    fail_update_item_body: bool = False
    fail_create_child_relation: bool = False
    capability_overrides: dict[forge.ForgeOperation, forge.Capability] = field(default_factory=dict)
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

    def create_child(self, *, parent: int, title: str, body: str, kind: board.ItemKind) -> int:
        number = self.next_created_child_number
        self.next_created_child_number += 1
        self.created_children.append((parent, title, body, kind))
        if self.fail_create_child_relation:
            raise forge.ForgePartialChildCreationError(
                child=number,
                parent=parent,
                step=f"record #{number} as a sub-issue of #{parent}",
                cause=ClaimError("relation POST failed (simulated)"),
            )
        return number

    def update_item_body(self, number: int, body: str) -> None:
        if self.fail_update_item_body:
            raise ClaimError("update item body failed (simulated)")
        self.item_bodies[number] = body

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

    def create_child(self, *, parent: int, title: str, body: str, kind: board.ItemKind) -> int:
        pytest.fail("a read-only command must never create a child")

    def update_item_body(self, number: int, body: str) -> None:
        pytest.fail("a read-only command must never update an item body")


_LIVE_VERSIONED_PATHS = checkout.versioned_paths
_LIVE_TRUNK_LANDING_TIMES = checkout.trunk_landing_times
_LIVE_FETCH_ISSUE_REFERENCE = issue_claim._fetch_issue_reference
_LIVE_REMOTE_URL = checkout.remote_url

BASE = "a" * 40
REPOSITORY = "example/agent-claim"
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


@pytest.mark.parametrize("bad_issue", [0, -1, True])
def test_issue_identity_requires_a_positive_integer(bad_issue: int) -> None:
    with pytest.raises(ClaimError, match="issue identity must be a positive integer"):
        protocol.IssueIdentity(bad_issue)


def test_forge_operation_exhaustiveness_matches_the_declared_reader_and_writer_methods() -> None:
    """Every `ForgeOperation` member names a `ForgeReader`/`ForgeWriter` method and
    nothing else; the count is pinned so a new operation cannot be added without
    its enum member, its capability entry, and this count bump."""
    declared_methods = {
        name
        for name in dir(forge.ForgeWriter)
        if not name.startswith("_") and name not in {"repository", "capability", "requests"}
    }
    assert {operation.value for operation in forge.ForgeOperation} == declared_methods
    assert len(forge.ForgeOperation) == 11
    assert set(github.GITHUB_CAPABILITIES) == set(forge.ForgeOperation)
    assert forge.Capability.UNSUPPORTED not in github.GITHUB_CAPABILITIES.values()


def test_only_the_github_adapter_speaks_gh_argv() -> None:
    """Source check, not a runtime guarantee: import-linter's module contract
    cannot see an argv string, so "only the adapter speaks `gh`" is proven by
    grepping every other module for the literal command name instead. Every
    module in the package is enumerated, not a fixed list, so a new module
    is covered the day it is added."""
    package = Path("src/agent_coordination")
    other_modules = sorted(p for p in package.glob("*.py") if p.name != "github.py")
    assert len(other_modules) >= 7
    for module in other_modules:
        assert '"gh"' not in module.read_text(), f"{module.name} must not construct a gh argv"


def board_issue_page_client(*rows: dict[str, object]) -> GitHubForge:
    return GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: "\n".join(json.dumps(row) for row in rows),
    )


def raw_board_issue(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "number": 1,
        "title": "Title",
        "labels": [],
        "body": "",
        "createdAt": "2026-08-20T00:00:00Z",
        "updatedAt": "2026-08-20T00:00:00Z",
        "isPullRequest": False,
        "kind": None,
        "childrenClosed": None,
        "childrenTotal": None,
        "blockedByCount": 0,
    }
    base.update(overrides)
    return base


def test_github_adapter_reads_the_native_issue_type_and_sub_issue_counts() -> None:
    client = board_issue_page_client(
        raw_board_issue(kind="Container", childrenClosed=1, childrenTotal=2)
    )

    issues = client.list_open_board_issues()

    assert issues[0].kind is board.ItemKind.CONTAINER
    assert issues[0].children_closed == 1
    assert issues[0].children_total == 2


def test_github_adapter_reads_an_unrecognized_issue_type_as_no_kind() -> None:
    client = board_issue_page_client(raw_board_issue(kind="Epic"))

    issues = client.list_open_board_issues()

    assert issues[0].kind is None


def test_github_adapter_reads_a_container_with_zero_children_as_a_real_state() -> None:
    """`0/0` must survive as a real container state, never as the forge
    saying nothing (the malformed-mixed-presence check would otherwise be
    indistinguishable from a genuinely empty container)."""
    client = board_issue_page_client(
        raw_board_issue(kind="Container", childrenClosed=0, childrenTotal=0)
    )

    issues = client.list_open_board_issues()

    assert issues[0].children_closed == 0
    assert issues[0].children_total == 0


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"childrenClosed": 1}, id="total-missing"),
        pytest.param({"childrenTotal": 2}, id="closed-missing"),
        pytest.param({"childrenClosed": -1, "childrenTotal": 2}, id="closed-negative"),
        pytest.param({"childrenClosed": 3, "childrenTotal": 2}, id="closed-exceeds-total"),
        pytest.param({"childrenClosed": True, "childrenTotal": 2}, id="closed-is-a-bool"),
        pytest.param({"kind": 5}, id="kind-not-a-string"),
        pytest.param({"childrenClosed": 2, "childrenTotal": True}, id="total-is-a-bool"),
        pytest.param({"childrenClosed": 2, "childrenTotal": -1}, id="total-negative"),
    ],
)
def test_github_adapter_fails_loud_on_a_malformed_board_issue(
    overrides: dict[str, object],
) -> None:
    client = board_issue_page_client(raw_board_issue(**overrides))

    with pytest.raises(ClaimError, match="malformed board issue"):
        client.list_open_board_issues()


def test_github_adapter_fails_loud_when_a_board_issue_is_not_an_object() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY), run=lambda arguments, input_data=None: '"not an object"'
    )

    with pytest.raises(ClaimError, match="malformed board issue"):
        client.list_open_board_issues()


def _paged_board_issue_client(page_rows: Callable[[int], list[dict[str, object]]]) -> GitHubForge:
    """An adapter whose `gh` stand-in answers each requested page number from
    `page_rows`, so a test can decide where the listing runs short."""

    def run(arguments: list[str], input_data: bytes | None = None) -> str:
        page = int(arguments[1].rsplit("page=", 1)[1])
        return "\n".join(json.dumps(row) for row in page_rows(page))

    return GitHubForge(github._repository_id(REPOSITORY), run=run)


def test_github_adapter_fetches_board_pages_until_one_comes_back_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full first page means more may exist, so the listing asks for the
    next batch; the short page inside that batch is what ends it, and pages
    past the last one come back empty exactly as GitHub answers them."""
    monkeypatch.setattr(github, "ISSUES_PER_PAGE", 2)
    pages = {
        1: [raw_board_issue(number=1), raw_board_issue(number=2)],
        2: [raw_board_issue(number=3)],
    }
    client = _paged_board_issue_client(lambda page: pages.get(page, []))

    assert [issue.number for issue in client.list_open_board_issues()] == [1, 2, 3]


def test_github_adapter_asks_for_a_second_batch_when_the_first_is_still_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One concurrent batch is not always enough: a board whose whole first
    batch comes back full must ask for another rather than stopping there."""
    monkeypatch.setattr(github, "ISSUES_PER_PAGE", 1)
    monkeypatch.setattr(github, "PARALLEL_FETCH_CONCURRENCY", 1)
    client = _paged_board_issue_client(
        lambda page: [raw_board_issue(number=page)] if page <= 3 else []
    )

    assert [issue.number for issue in client.list_open_board_issues()] == [1, 2, 3]


def test_github_adapter_accepts_pretty_and_ansi_colored_json() -> None:
    """`gh` pretty-prints and colorizes when it believes it writes to a TTY;
    the adapter reads that back as the same values as compact NDJSON."""
    pretty = (
        json.dumps(raw_board_issue(number=1), indent=2)
        + "\n"
        + json.dumps(raw_board_issue(number=2), indent=2)
    )
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: f"\x1b[32m{pretty}\x1b[0m",
    )

    assert [issue.number for issue in client.list_open_board_issues()] == [1, 2]


def test_github_adapter_accepts_concatenated_pretty_json_objects() -> None:
    """Pretty-printed objects can arrive with no separator at all between
    them, which is neither NDJSON nor a JSON array."""
    raw = json.dumps(raw_board_issue(number=1), indent=2) + json.dumps(
        raw_board_issue(number=2), indent=2
    )
    client = GitHubForge(
        github._repository_id(REPOSITORY), run=lambda arguments, input_data=None: raw
    )

    assert [issue.number for issue in client.list_open_board_issues()] == [1, 2]


def raw_board_pull_request(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "number": 62,
        "title": "Fixes #10",
        "body": "Ship it.",
        "headRefName": "codex/issue-10",
        "mergedAt": None,
    }
    base.update(overrides)
    return base


def test_github_adapter_reads_an_open_board_pull_request() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: json.dumps(raw_board_pull_request()),
    )

    assert client.list_open_board_pull_requests() == (
        board.PullRequest(62, "Fixes #10", "Ship it.", "codex/issue-10", None),
    )


def test_github_adapter_reads_a_board_pull_request_with_no_body_as_empty() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: json.dumps(raw_board_pull_request(body=None)),
    )

    assert client.list_open_board_pull_requests()[0].body == ""


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"number": True}, id="number-is-a-bool"),
        pytest.param({"number": 0}, id="number-not-positive"),
        pytest.param({"title": 5}, id="title-not-text"),
        pytest.param({"body": 5}, id="body-not-text"),
        pytest.param({"headRefName": None}, id="head-ref-missing"),
        pytest.param({"mergedAt": 5}, id="merged-at-not-text"),
        pytest.param({"mergedAt": "yesterday"}, id="merged-at-unparsable-shape"),
    ],
)
def test_github_adapter_fails_loud_on_a_malformed_board_pull_request(
    overrides: dict[str, object],
) -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: json.dumps(raw_board_pull_request(**overrides)),
    )

    with pytest.raises(ClaimError, match="malformed board pull request"):
        client.list_open_board_pull_requests()


def test_github_adapter_fails_loud_when_a_board_pull_request_is_not_an_object() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY), run=lambda arguments, input_data=None: '"not an object"'
    )

    with pytest.raises(ClaimError, match="malformed board pull request"):
        client.list_open_board_pull_requests()


def test_github_adapter_creates_a_child_and_links_it_as_a_sub_issue() -> None:
    observed: list[tuple[list[str], bytes | None]] = []

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append((arguments, input_data))
        if arguments[2] == "POST" and arguments[3].endswith("/issues"):
            return json.dumps({"id": 555444, "number": 101})
        return ""

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    child = client.create_child(
        parent=79, title="Scheibe 4", body=board.BLOCK_CHILD_SKELETON, kind=board.ItemKind.TASK
    )

    assert child == 101
    assert observed == [
        (
            ["api", "--method", "POST", f"repos/{REPOSITORY}/issues", "--input", "-"],
            json.dumps(
                {"title": "Scheibe 4", "body": board.BLOCK_CHILD_SKELETON, "type": "Task"}
            ).encode("utf-8"),
        ),
        (
            ["api", "--method", "POST", f"repos/{REPOSITORY}/issues/79/sub_issues", "--input", "-"],
            json.dumps({"sub_issue_id": 555444}).encode("utf-8"),
        ),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("not json", id="invalid-json"),
        pytest.param(json.dumps({"id": 1}), id="missing-number"),
        pytest.param(json.dumps({"number": 1}), id="missing-id"),
        pytest.param(json.dumps({"id": True, "number": 1}), id="id-is-a-bool"),
    ],
)
def test_github_adapter_fails_loud_on_a_malformed_created_child(payload: str) -> None:
    client = GitHubForge(github._repository_id(REPOSITORY), run=lambda *_a, **_k: payload)

    with pytest.raises(ClaimError, match=r"created.child"):
        client.create_child(parent=79, title="Scheibe 4", body="", kind=board.ItemKind.TASK)


def test_github_adapter_names_the_created_child_when_the_relation_post_fails() -> None:
    """The issue exists once `create_child`'s first write returns; a second
    write failing after that must name the surviving child (#112 finding 3),
    never just surface the raw relation-POST error."""

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        if arguments[2] == "POST" and arguments[3].endswith("/issues"):
            return json.dumps({"id": 555444, "number": 101})
        raise forge.ForgeError("HTTP 422 could not create sub-issue relation")

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    with pytest.raises(forge.ForgePartialChildCreationError) as excinfo:
        client.create_child(parent=79, title="Scheibe 4", body="", kind=board.ItemKind.TASK)

    assert excinfo.value.child == 101
    assert excinfo.value.parent == 79


def test_github_adapter_updates_an_item_body() -> None:
    observed: list[tuple[list[str], bytes | None]] = []

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append((arguments, input_data))
        return ""

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    client.update_item_body(79, "new body")

    assert observed == [
        (
            ["api", "--method", "PATCH", f"repos/{REPOSITORY}/issues/79", "--input", "-"],
            json.dumps({"body": "new body"}).encode("utf-8"),
        )
    ]


def test_read_only_commands_never_write_through_a_reader_only_forge(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = ReaderOnlyForge()
    client.board_issues = (board_issue(72, "Work", complete_contract("Ship it.")),)
    client.landings[12] = landing_pull_request(body="Work-Item: #72\n\nCloses #72")
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: "")
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

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
    repository = github._repository_id("example/agent-claim")
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


def test_board_renders_fixture_as_text_without_github_writes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    observed = _board_fixture_environment(monkeypatch)

    assert issue_claim.main(["--repo", "example/agent-claim", "board"]) == 0
    rendered = capsys.readouterr().out
    assert "CONTRACT" in rendered
    assert "NEXT" in rendered
    assert "ACTIONABLE" in rendered
    assert "#10" in rendered
    assert "no: claimed" in rendered
    assert "no: body legacy" in rendered
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


def test_recent_merged_pull_requests_refuses_a_window_that_ends_before_it_starts() -> None:
    """A fixed far-future `since` -- never `datetime.now(UTC)`-relative -- so
    this stays deterministic regardless of when the suite runs: a real-clock
    window could cross UTC midnight between this line and the production
    code's own `datetime.now(UTC)` call, sometimes closing the window and
    falling through to a real, unfaked `gh` call instead of raising."""
    client = GitHubForge(github._repository_id("example/agent-claim"))
    raised_argument_1 = datetime(2099, 1, 1, tzinfo=UTC)
    with pytest.raises(ClaimError, match="merged pull request window ends before it starts"):
        client.list_recent_merged_board_pull_requests(raised_argument_1)


def test_board_projects_fixture_json_without_github_writes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _board_fixture_environment(monkeypatch)

    assert issue_claim.main(["--repo", "example/agent-claim", "board", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"items", "ready_now", "stale", "recovery", "uncut", "requests"}
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
    assert fourteen["actionable_reason"] == "body legacy"
    assert [item["number"] for item in payload["ready_now"]] == [10, 13]
    assert [item["number"] for item in payload["stale"]] == [12]
    assert next(item for item in payload["items"] if item["number"] == 12)["stage"] == "text-only"
    assert 11 not in [item["number"] for item in payload["ready_now"]]


def test_board_reports_requests_equal_to_the_adapters_own_invocation_count(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #168: `observed` is the fixture's own independent tally of every
    `gh` call the injected `run` actually received -- never read from the
    counter under test -- so a matching `requests` line/field is real
    evidence, not a tautology. The same client (and its cumulative
    `observed`) serves both invocations below, so the JSON run's count is
    checked against `observed`'s size at that later point, not the text
    run's."""
    observed = _board_fixture_environment(monkeypatch)

    assert issue_claim.main(["--repo", "example/agent-claim", "board"]) == 0
    rendered = capsys.readouterr().out
    assert f"requests: {len(observed)}" in rendered

    assert issue_claim.main(["--repo", "example/agent-claim", "board", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["requests"] == len(observed)


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
            kind=board.ItemKind.CONTAINER,
            children_closed=0,
            children_total=0,
        ),
        replace(
            board_issue(30, "Container with children", complete_contract("Ship #30.")),
            kind=board.ItemKind.CONTAINER,
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
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())
    _patch_store_write(monkeypatch)

    assert issue_claim.main(["--repo", "example/agent-claim", "board"]) == 0
    rendered = capsys.readouterr().out

    assert observed_children_calls == [30]
    # Open issues, open PRs, merged PRs, and exactly one `list_children`
    # call (for #30, never #20) -- claims come from the store now (issue
    # #176), never from a ledger-comments request. No issue here names a
    # blocker, so `list_board_blockers` never runs -- an empty numbers set
    # costs no request, on the fake exactly as on the real adapter.
    assert client.requests == 4
    assert f"requests: {client.requests}" in rendered

    client.requests = 0
    observed_children_calls.clear()
    assert issue_claim.main(["--repo", "example/agent-claim", "board", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)

    assert observed_children_calls == [30]
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
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())
    _patch_store_write(monkeypatch)

    assert issue_claim.main(["--repo", "example/agent-claim", "board"]) == 0
    rendered = capsys.readouterr().out

    assert observed_dependency_calls == [11]
    # Open issues, open PRs, merged PRs, and exactly one dependency lookup
    # (for #11, never #10) -- block mode never calls `list_board_blockers`,
    # and claims come from the store now (issue #176), never a ledger
    # comments request.
    assert client.requests == 4
    assert f"requests: {client.requests}" in rendered


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
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    assert issue_claim.main(["--repo", "example/agent-claim", "board"]) == 0
    rendered = capsys.readouterr().out
    assert "EXPECT" in rendered
    no_expectations = next(line for line in rendered.splitlines() if "No expectations" in line)
    proposed_expectations = next(
        line for line in rendered.splitlines() if "Proposed expectations" in line
    )
    ruled_expectations = next(
        line for line in rendered.splitlines() if "Ruled expectations" in line
    )
    assert "-" in no_expectations
    assert "1/2" in proposed_expectations
    assert "proposed" not in proposed_expectations
    assert "ruled 0" in ruled_expectations

    assert issue_claim.main(["--repo", "example/agent-claim", "board", "--json"]) == 0
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

    assert issue_claim.main(["--repo", "example/agent-claim", "rulings"]) == 0
    assert capsys.readouterr().out.splitlines() == [
        "#50 2/3: In-flight security work",
        "#30 1/2: Later security tie",
        "#10 2/3: Earlier security tie",
        "#40 2/3: More open security work",
        "#60 1/1: Lower-priority product work",
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

    assert issue_claim.main(["--repo", "example/agent-claim", "rulings"]) == 0
    assert capsys.readouterr().out == "#400 1/1: Block-only expectations\n"


def test_rulings_renders_text_json_and_empty_success(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    open_issue = rulings_issue(
        10,
        "Open expectation",
        open_lines=1,
        total_lines=2,
    )
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(open_issue,))

    assert issue_claim.main(["--repo", "example/agent-claim", "rulings"]) == 0
    assert capsys.readouterr().out == "#10 1/2: Open expectation\n"

    assert issue_claim.main(["--repo", "example/agent-claim", "rulings", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {"number": 10, "title": "Open expectation", "open": 1, "total": 2}
    ]

    monkeypatch.setattr(
        client,
        "list_open_board_issues",
        lambda: (
            rulings_issue(
                11,
                "Fully ruled",
                open_lines=0,
                total_lines=1,
            ),
        ),
    )

    assert issue_claim.main(["--repo", "example/agent-claim", "rulings"]) == 0
    assert capsys.readouterr().out == "No open expectation lines.\n"

    assert issue_claim.main(["--repo", "example/agent-claim", "rulings", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def board_issue(
    number: int,
    title: str,
    body: str,
    *,
    labels: tuple[str, ...] = (),
    blocked_by_count: int = 0,
) -> board.Issue:
    return board.Issue(
        number,
        title,
        labels,
        body,
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        blocked_by_count=blocked_by_count,
    )


def block_dependency(
    number: int,
    *,
    repository: str = REPOSITORY,
    state: board.BlockerState = board.BlockerState.OPEN,
    is_pull_request: bool = False,
    closed_at: datetime | None = None,
) -> board.IssueDependency:
    return board.IssueDependency(
        board.IssueReference(repository, number), state, is_pull_request, closed_at
    )


def blocked_issue(
    number: int,
    title: str,
    *dependencies: board.IssueDependency,
    next_step: str | None = None,
    labels: tuple[str, ...] = (),
) -> tuple[board.Issue, dict[int, tuple[board.IssueDependency, ...]]]:
    """One item and the `blocked_by` dependencies GitHub records for it, with
    the listing count the forge would report for exactly those."""
    issue = board_issue(
        number,
        title,
        complete_contract(next_step or f"Claim #{number}."),
        labels=labels,
        blocked_by_count=len(dependencies),
    )
    return issue, {number: dependencies}


def agent_claim_body(toml_text: str, *, fence: str = "```") -> str:
    """A body carrying one recognized `agent-claim` fence around `toml_text`,
    with ordinary prose before and after it (issue #150 §4)."""
    return f"Prose before.\n\n{fence}agent-claim\n{toml_text}\n{fence}\n\nProse after.\n"


MINIMAL_BLOCK_TOML = 'version = 1\nnow = "N"\nnext = "X"\ndone_when = "D"\n'
RULED_ON = date(2026, 8, 28)


def complete_contract(
    next_step: str,
    *,
    now: str = "Work is ready.",
    done_when: str = "The work is merged.",
    **block_entries: object,
) -> str:
    """A body whose one `agent-claim` block carries every projection key
    filled, plus whatever `[[expectation]]`/`[[slice]]`/`frozen_until`
    entries the scenario needs -- serialized by the production writer, so no
    test hand-writes the block's TOML escaping."""
    data: dict[str, object] = {
        "version": 1,
        "now": now,
        "next": next_step,
        "done_when": done_when,
        **block_entries,
    }
    return agent_claim_body(board.render_block(data).rstrip("\n"))


FROZEN_TRIGGER = "eine zweite Maschine bekommt einen Grund"
FROZEN_UNTIL = {"trigger": FROZEN_TRIGGER, "ruled_on": date(2026, 8, 31)}


def proposed_expectation(text: str, *, default: str = "later") -> dict[str, object]:
    return {"text": text, "default": default}


def ruled_expectation(
    text: str, *, ruling: str = "yes", ruled_on: date = RULED_ON
) -> dict[str, object]:
    return {"text": text, "ruling": ruling, "ruled_on": ruled_on}


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


def idea_body(wish: str) -> str:
    """An operator's idea: the wish in their own prose, above a block whose
    projection keys are all still empty -- what `projectionless` reads."""
    return f"## Wunsch\n{wish}\n\n" + complete_contract("", now="", done_when="")


def slice_entries(*titles: str, first_index: int = 1) -> list[dict[str, object]]:
    """`[[slice]]` entries numbered from `first_index`, in order."""
    return [{"index": first_index + offset, "title": title} for offset, title in enumerate(titles)]


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
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())
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
            "Run: aco claim 11 --scope <paths>\n"
            "<paths> cannot be derived; take the files to claim from the item body.\n"
            "\nSKIPPED\n#12: blocked by #11\n",
            id="names_the_highest_scored_actionable_item",
        ),
        pytest.param(
            _TOP_AND_BLOCKED,
            _BLOCKED_BY_ELEVEN,
            (),
            ("next", "--json"),
            0,
            {
                "action": "work_item",
                "number": 11,
                "score": 10,
                "title": "Top work",
                "next": "Claim #11.",
                "command": "aco claim 11 --scope <paths>",
                "recovery": [],
                "skipped": [{"number": 12, "reason": "blocked by #11"}],
                "ruling_landings": None,
                "ruling_old": None,
            },
            id="emits_the_highest_scored_actionable_item_as_json",
        ),
        pytest.param(
            (board_issue(10, "Incomplete", complete_contract("", done_when="")),),
            {},
            (),
            ("next",),
            3,
            "No actionable item.\n\nSKIPPED\n#10: body incomplete: Next, Done when\n",
            id="names_an_incomplete_body_as_the_reason_nothing_is_pullable",
        ),
        pytest.param(
            (board_issue(10, "Legacy", "## Now\nInvestigate."),),
            {},
            (),
            ("next",),
            3,
            "No actionable item.\n\nSKIPPED\n#10: body legacy\n",
            id="names_a_body_with_no_block_as_legacy",
        ),
        pytest.param(
            (board_issue(10, "Claimed", complete_contract("Claim #10.")),),
            {},
            (request(issue=10),),
            ("next",),
            3,
            "No actionable item.\n\nSKIPPED\n#10: claimed\n",
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
            "Run: aco claim 9 --scope <paths>\n"
            "<paths> cannot be derived; take the files to claim from the item body.\n"
            "\nSKIPPED\n#10: blocked by #9\n",
            id="excludes_items_with_open_blockers",
        ),
        pytest.param(
            (),
            {},
            (),
            ("next",),
            3,
            "No actionable item.\n",
            id="prints_no_actionable_item_on_a_fully_empty_board",
        ),
        pytest.param(
            (),
            {},
            (),
            ("next", "--json"),
            3,
            {"action": None, "recovery": [], "skipped": []},
            id="emits_action_null_on_a_fully_empty_board",
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

    assert issue_claim.main(["--repo", "example/agent-claim", *arguments]) == expected_exit
    rendered = capsys.readouterr().out

    if isinstance(expected_output, str):
        assert rendered == expected_output
    else:
        assert json.loads(rendered) == expected_output


PULLED_WITH_REFINING_FIRST = (
    "#10 score -10: Work\nNext: Claim #10.\n"
    "Run: aco claim 10 --scope <paths>\n"
    "<paths> cannot be derived; take the files to claim from the item body.\n"
    "Erwartungen ungeregelt, beim Ziehen zuerst refinen\n"
)


@pytest.mark.parametrize(
    ("expectations", "expected_state", "expected_output"),
    [
        pytest.param(
            (),
            board.ExpectationState.NONE,
            "#10 score -10: Work\nNext: Claim #10.\n"
            "Run: aco claim 10 --scope <paths>\n"
            "<paths> cannot be derived; take the files to claim from the item body.\n",
            id="no_expectation_entry_remains_actionable",
        ),
        pytest.param(
            (proposed_expectation("Name it.", default="yes"),),
            board.ExpectationState.PROPOSED,
            PULLED_WITH_REFINING_FIRST,
            id="proposed_expectations_are_pulled_with_refining_first",
        ),
        pytest.param(
            (ruled_expectation("Name it."), ruled_expectation("Remove it.", ruling="no")),
            board.ExpectationState.RULED,
            "#10 score -10: Work\nNext: Claim #10.\n"
            "Run: aco claim 10 --scope <paths>\n"
            "<paths> cannot be derived; take the files to claim from the item body.\n",
            id="fully_ruled_expectations_remain_actionable",
        ),
        pytest.param(
            (ruled_expectation("Name it.", ruling="no"), proposed_expectation("Remove it.")),
            board.ExpectationState.PROPOSED,
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
    expected_state: board.ExpectationState,
    expected_output: str,
) -> None:
    issue = board_issue(10, "Work", complete_contract("Claim #10.", expectation=list(expectations)))
    client = FakeForge()
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (issue,))
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    assert issue_claim.main(["--repo", "example/agent-claim", "next"]) == 0
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
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())
    _patch_store_write(monkeypatch, _store_claim_from_request(standing))

    assert issue_claim.main(["--repo", "example/agent-claim", "next"]) == 0
    assert capsys.readouterr().out == (
        "#11 score 10: Needs rulings\n"
        "Next: Claim #11.\n"
        "Run: aco claim 11 --scope <paths>\n"
        "<paths> cannot be derived; take the files to claim from the item body.\n"
        "Erwartungen ungeregelt, beim Ziehen zuerst refinen\n"
        "\n"
        "SKIPPED\n"
        "#12: blocked by #11\n"
        "#13: claimed\n"
    )

    assert issue_claim.main(["--repo", "example/agent-claim", "next", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "action": "work_item",
        "number": 11,
        "score": 10,
        "title": "Needs rulings",
        "next": "Claim #11.",
        "command": "aco claim 11 --scope <paths>",
        "ruling_landings": None,
        "ruling_old": None,
        "ruling_hint": "Erwartungen ungeregelt, beim Ziehen zuerst refinen",
        "recovery": [],
        "skipped": [
            {"number": 12, "reason": "blocked by #11"},
            {"number": 13, "reason": "claimed"},
        ],
    }


def test_claim_accepts_an_item_with_no_dependencies(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    issue = board_issue(10, "Work", complete_contract("Claim #10."), labels=("security",))
    _configured_board_client(monkeypatch, tmp_path, open_issues=(issue,))
    monkeypatch.setattr(
        issue_claim, "_request", lambda _arguments: request(issue=10, scope=("src/work.py",))
    )

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
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
        issue_claim, "_request", lambda _arguments: request(issue=10, scope=("src/work.py",))
    )

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
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
        issue_claim, "_request", lambda _arguments: request(issue=10, scope=("src/work.py",))
    )

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
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
        lambda _arguments: replace(
            request(issue=10, scope=("src/work.py",)), out_of_order_reason=reason
        ),
    )

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
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
        issue_claim, "_request", lambda _arguments: request(issue=10, scope=("src/work.py",))
    )

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
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
    assert payload["refused"] is True
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
        issue_claim, "_request", lambda _arguments: request(issue=10, scope=("src/work.py",))
    )

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
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
                "example/agent-claim",
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
            "Claim #11.", expectation=[proposed_expectation("Name it.", default="yes")]
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
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())
    monkeypatch.setattr(issue_claim, "_request", lambda _arguments: claimed_request)

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
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


def test_claim_help_names_the_out_of_order_refusal(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        issue_claim.main(["claim", "--help"])

    assert exited.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "refuse" in help_text
    assert "without a reason" in help_text
    assert "priority actionable item is free" in help_text


def test_claim_help_names_the_whole_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        issue_claim.main(["claim", "--help"])

    assert exited.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--whole" in help_text
    assert "three paths" in help_text
    assert "directory" in help_text
    assert "quarter" in help_text
    assert "twelve" in help_text


def test_rescope_help_names_the_whole_reason(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        issue_claim.main(["rescope", "--help"])

    assert exited.value.code == 0
    help_text = " ".join(capsys.readouterr().out.split())
    assert "--whole" in help_text
    assert "three paths" in help_text


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
        board_issue(11, "Top work", complete_contract("Claim #11.")),
        board_issue(12, "Depends on top", complete_contract("Claim #12."), blocked_by_count=1),
    )
    client.board_dependencies = {12: (block_dependency(11),)}
    claimed_request = request("out-of-order", issue=10, scope=("src/lower.py",))
    monkeypatch.setattr(client, "list_open_board_issues", lambda: issues)
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())
    monkeypatch.setattr(issue_claim, "_request", lambda _arguments: claimed_request)

    arguments = [
        "--repo",
        "example/agent-claim",
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
        board_issue(11, "Top work", complete_contract("Claim #11.")),
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
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())
    monkeypatch.setattr(issue_claim, "_request", lambda _arguments: claimed_request)

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
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
        50, "Prerequisite the operator prioritized", complete_contract("Unblock #52.")
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
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())
    monkeypatch.setattr(issue_claim, "_request", lambda _arguments: claimed_request)

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
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
    top = board_issue(11, "Top work", complete_contract("Claim #11."))
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
        issue_claim, "_request", lambda _arguments: request(issue=10, scope=("src/lower.py",))
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
    assert payload["refused"] is True
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
        issue_claim, "_request", lambda _arguments: request(issue=10, scope=("src/top.py",))
    )

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
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
        issue_claim, "_request", lambda _arguments: request(issue=72, scope=("src/work.py",))
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    monkeypatch.setattr(
        issue_claim, "_request", lambda _arguments: request(issue=72, scope=("src/work.py",))
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
    """`cut`'s fresh child (`board.BLOCK_CHILD_SKELETON`) is defect-free but
    incomplete -- invisible to `next`, and now refused here too, exactly as
    ruled: `claim` requires a complete projection."""
    child = board_issue(101, "Scheibe 1", board.BLOCK_CHILD_SKELETON)
    _configured_board_client(monkeypatch, tmp_path, open_issues=(child,))
    monkeypatch.setattr(
        issue_claim, "_request", lambda _arguments: request(issue=101, scope=("src/work.py",))
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
        issue_claim, "_request", lambda _arguments: request(issue=51, scope=("src/work.py",))
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
        kind=board.ItemKind.CONTAINER,
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
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 2
    assert f"ERROR: #{CUT_CONTAINER} is not a container" in capsys.readouterr().err


def test_cut_refuses_a_number_that_names_no_open_issue(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _configured_board_client(monkeypatch, tmp_path, open_issues=())

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 2
    assert f"ERROR: #{CUT_CONTAINER} is not an open container" in capsys.readouterr().err


def test_cut_refuses_a_container_that_already_has_a_parent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(_one_slice_container(),))
    client.parents[CUT_CONTAINER] = board.ParentIssue(
        board.IssueReference(REPOSITORY, 1), "", board.ItemKind.CONTAINER
    )

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 2
    assert (
        f"ERROR: #{CUT_CONTAINER} is itself a child of {REPOSITORY}#1; "
        "nested containers are not supported" in capsys.readouterr().err
    )


@pytest.mark.parametrize(
    "operation", [forge.ForgeOperation.CREATE_CHILD, forge.ForgeOperation.UPDATE_ITEM_BODY]
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
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
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
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 2
    child = client.next_created_child_number - 1
    assert client.created_children == [
        (CUT_CONTAINER, "Scheibe 1", board.BLOCK_CHILD_SKELETON, board.ItemKind.TASK)
    ]
    assert client.item_bodies == {}
    err = capsys.readouterr().err
    assert f"created #{child} but failed to record #{child} as a sub-issue" in err
    assert "do not re-run" in err


def test_render_block_round_trips_every_field() -> None:
    toml_text = (
        f"{MINIMAL_BLOCK_TOML}"
        'frozen_until = { trigger = "named trigger", ruled_on = 2026-09-06 }\n'
        '[[expectation]]\ntext = "Proposed"\ndefault = "later"\n'
        '[[expectation]]\ntext = "Ruled"\nruling = "yes"\nruled_on = 2026-09-05\n'
        '[[slice]]\nindex = 4\ntitle = "Block contract in issue bodies"\n'
    )
    located = board.locate_agent_claim_block(agent_claim_body(toml_text))

    reparsed = tomllib.loads(board.render_block(located.data))

    assert reparsed == located.data


def test_render_block_escapes_quotes_and_backslashes() -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}[[slice]]\nindex = 1\ntitle = "Quote \\" and back\\\\slash"\n'
    located = board.locate_agent_claim_block(agent_claim_body(toml_text))

    reparsed = tomllib.loads(board.render_block(located.data))

    assert reparsed == located.data


def test_replace_agent_claim_block_preserves_crlf_and_surrounding_bytes() -> None:
    body = (
        "Prose before.\r\n\r\n"
        "```agent-claim\r\n"
        'version = 1\r\nnow = "N"\r\nnext = "X"\r\ndone_when = "D"\r\n'
        "```\r\n\r\nProse after.\r\n"
    )
    located = board.locate_agent_claim_block(body)
    new_data = {**located.data, "now": "Changed"}

    new_body = board.replace_agent_claim_block(body, located, new_data)

    assert new_body.startswith("Prose before.\r\n\r\n```agent-claim\r\n")
    assert new_body.endswith("```\r\n\r\nProse after.\r\n")
    assert '\nnow = "Changed"\r\n' in new_body
    assert board.parse_body(new_body).contract.now == "Changed"


def test_render_block_emits_an_empty_slice_array_after_removing_the_final_entry() -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}[[slice]]\nindex = 1\ntitle = "Only slice"\n'
    located = board.locate_agent_claim_block(agent_claim_body(toml_text))
    new_data = {**located.data, "slice": []}

    rendered = board.render_block(new_data)

    assert "slice = []" in rendered
    assert tomllib.loads(rendered)["slice"] == []


def _write_block_pin(tmp_path: Path) -> None:
    (tmp_path / ".agent-claim").mkdir(exist_ok=True)
    (tmp_path / ".agent-claim" / "board.toml").write_text('body_contract = "block"\n')


def test_cut_creates_a_child_and_removes_the_first_cuttable_slice(
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
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 0
    child = client.next_created_child_number - 1
    assert client.created_children == [
        (CUT_CONTAINER, "Scheibe 1", board.BLOCK_CHILD_SKELETON, board.ItemKind.TASK)
    ]
    new_data = board.locate_agent_claim_block(client.item_bodies[CUT_CONTAINER]).data
    assert new_data["slice"] == [{"index": 2, "title": "Scheibe 2"}]
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
            "example/agent-claim",
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
        "container": CUT_CONTAINER,
        "row": 2,
        "child": child,
    }
    remaining = board.locate_agent_claim_block(client.item_bodies[CUT_CONTAINER]).data
    assert remaining["slice"] == [{"index": 1, "title": "Scheibe 1"}]


def test_cut_creates_an_untied_child_with_no_slice_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    container = _cut_container_issue(MINIMAL_BLOCK_TOML)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    _write_block_pin(tmp_path)

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Untied"]
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
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Untied"]
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
            "example/agent-claim",
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
            "example/agent-claim",
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
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Wrong title"]
    )

    assert exit_code == 2
    assert (
        f"ERROR: #{CUT_CONTAINER}'s slice 1 is titled 'Scheibe 1'; --title must match it exactly"
        in capsys.readouterr().err
    )
    assert client.created_children == []
    assert client.item_bodies == {}


def test_cut_refuses_a_legacy_container_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    container = board.Issue(
        CUT_CONTAINER,
        "Epic",
        (),
        "## Now\nOld prose.\n",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    _write_block_pin(tmp_path)

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "X"]
    )

    assert exit_code == 2
    assert "body legacy" in capsys.readouterr().err
    assert client.created_children == []


def test_cut_refuses_a_malformed_container_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    container = _cut_container_issue('version = 2\nnow = "N"\nnext = "X"\ndone_when = "D"\n')
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    _write_block_pin(tmp_path)

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "X"]
    )

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
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 2
    child = client.next_created_child_number - 1
    assert client.created_children == [
        (CUT_CONTAINER, "Scheibe 1", board.BLOCK_CHILD_SKELETON, board.ItemKind.TASK)
    ]
    err = capsys.readouterr().err
    assert (
        f"created #{child} but failed to remove row 1 from #{CUT_CONTAINER}'s agent-claim block"
        in err
    )
    assert "do not re-run" in err


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

    exit_code = issue_claim.main(["--repo", "example/agent-claim", "next"])

    assert exit_code == 0
    out = capsys.readouterr().out
    assert f'aco cut {CUT_CONTAINER} --title "Scheibe 1"' in out

    cut_exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
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

    json_exit_code = issue_claim.main(["--repo", "example/agent-claim", "next", "--json"])
    assert json_exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "cut_slice"
    assert payload["slice"] == _DIFFERING_NEXT_LINE
    assert payload["cut_title"] == "Scheibe 1"

    json_cut_exit_code = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "cut",
            str(CUT_CONTAINER),
            "--title",
            payload["cut_title"],
        ]
    )
    assert json_cut_exit_code == 0
    capsys.readouterr()  # discard this leg's own "CUT #79 row 1 -> #N" line

    next_exit_code = issue_claim.main(["--repo", "example/agent-claim", "next"])
    assert next_exit_code == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0] == f"cut_slice #{CUT_CONTAINER}: {_DIFFERING_NEXT_LINE}"
    command_line = out.splitlines()[1]
    cut_arguments = shlex.split(command_line.removeprefix("Next: aco "))

    cut_exit_code = issue_claim.main(["--repo", "example/agent-claim", *cut_arguments])

    assert cut_exit_code == 0
    child = client.next_created_child_number - 1
    remaining_slice_entries = board.locate_agent_claim_block(
        client.item_bodies[CUT_CONTAINER]
    ).data["slice"]
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
        issue_claim, "_request", lambda _arguments: request(issue=72, scope=("src/work.py",))
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
        "refused": True,
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
        issue_claim, "_request", lambda _arguments: request(issue=72, scope=("src/work.py",))
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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


def test_uncut_is_empty_when_the_block_carries_no_slice_entry() -> None:
    container = board.Issue(
        79,
        "Container",
        (),
        agent_claim_body(MINIMAL_BLOCK_TOML),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )

    projected = projected_board(
        (container,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.uncut == ()


@pytest.mark.parametrize(
    ("next_line", "expected"),
    [
        pytest.param(None, False, id="no-next-line"),
        pytest.param("keiner", False, id="german-none"),
        pytest.param("Keine", False, id="german-none-casefolded"),
        pytest.param("nichts", False, id="no-blockers-spelling"),
        pytest.param("none", False, id="english-none"),
        pytest.param("-", False, id="dash"),
        pytest.param("Cut the next slice.", True, id="concrete-work"),
    ],
)
def test_has_further_work(next_line: str | None, expected: bool) -> None:
    assert board.has_further_work(next_line) is expected


@pytest.mark.parametrize(
    "raw_timestamp",
    [
        pytest.param("not-a-timestamp", id="unparsable"),
        pytest.param("2026-08-20T00:00:00", id="missing-offset"),
    ],
)
def test_timestamp_fails_loud_on_a_malformed_github_timestamp(raw_timestamp: str) -> None:
    """`board._timestamp` backs an issue's `age_days`/`idle_days` (its
    `created_at`/`updated_at`); GitHub's own timestamp shape is the only
    thing it ever trusts."""
    with pytest.raises(ClaimError, match="GitHub returned a malformed board timestamp"):
        board._timestamp(raw_timestamp)


def test_child_skeleton_is_an_incomplete_contract_with_no_defects() -> None:
    parsed = board.parse_body(board.BLOCK_CHILD_SKELETON)

    assert parsed.read_state is board.BodyReadState.VALID
    assert parsed.contract_complete is False
    assert parsed.contract == board.Contract("", "", "", ())


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
        issue_claim, "_request", lambda _arguments: request(issue=1017, scope=("src/work.py",))
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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


@pytest.mark.parametrize(
    ("issue", "claims", "dependencies", "expected"),
    [
        pytest.param(
            board_issue(10, "Ready", complete_contract("Claim #10.")),
            (),
            (),
            (True, None),
            id="ready",
        ),
        pytest.param(
            board_issue(10, "Claimed", complete_contract("Claim #10.")),
            (request(issue=10),),
            (),
            (False, "claimed"),
            id="claimed",
        ),
        pytest.param(
            board_issue(10, "Blocked", complete_contract("Claim #10."), blocked_by_count=1),
            (),
            (block_dependency(9),),
            (False, "blocked by #9"),
            id="blocked",
        ),
        pytest.param(
            board_issue(10, "Unblocked", complete_contract("Claim #10."), blocked_by_count=1),
            (),
            (
                block_dependency(
                    9,
                    state=board.BlockerState.CLOSED,
                    closed_at=datetime(2026, 8, 20, tzinfo=UTC),
                ),
            ),
            (True, None),
            id="closed_dependency",
        ),
        pytest.param(
            board_issue(10, "Incomplete", agent_claim_body('version = 1\nnow = "Investigate."\n')),
            (),
            (),
            (False, "body malformed: next: next is required"),
            id="malformed",
        ),
        pytest.param(
            board_issue(
                10,
                "Half-filled skeleton",
                complete_contract("", done_when=""),
            ),
            (),
            (),
            (False, "body incomplete: Next, Done when"),
            id="incomplete",
        ),
        pytest.param(
            board_issue(10, "Frozen", complete_contract("Claim #10.", frozen_until=FROZEN_UNTIL)),
            (),
            (),
            (False, f"frozen: {FROZEN_TRIGGER}"),
            id="frozen",
        ),
        pytest.param(
            board_issue(
                10,
                "Frozen and claimed",
                complete_contract("Claim #10.", frozen_until=FROZEN_UNTIL),
            ),
            (request(issue=10),),
            (),
            (False, f"frozen: {FROZEN_TRIGGER}"),
            id="frozen_takes_priority_over_claimed",
        ),
    ],
)
def test_board_reports_each_item_actionability_reason(
    issue: board.Issue,
    claims: tuple[ClaimRequest, ...],
    dependencies: tuple[board.IssueDependency, ...],
    expected: tuple[bool, str | None],
) -> None:
    blocker = board_issue(9, "Blocker", complete_contract("Claim #9."))
    projected = projected_board(
        (blocker, issue),
        (),
        (),
        tuple(_store_claim_from_request(request_value) for request_value in claims),
        board.BoardConfig(),
        dependencies={issue.number: dependencies},
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )
    item = next(item for item in projected.items if item.number == issue.number)

    actual = (item.actionable, item.actionable_reason)
    assert actual == expected


def test_board_names_every_open_dependency_in_order() -> None:
    blocked, blocked_by = blocked_issue(10, "Blocked", block_dependency(790), block_dependency(642))
    projected = projected_board(
        (
            blocked,
            board_issue(642, "P3", complete_contract("Claim #642.")),
            board_issue(790, "Review", complete_contract("Claim #790.")),
        ),
        (),
        (),
        (),
        board.BoardConfig(),
        dependencies=blocked_by,
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )
    item = next(item for item in projected.items if item.number == 10)

    assert item.open_blockers == (
        board.IssueReference(REPOSITORY, 642),
        board.IssueReference(REPOSITORY, 790),
    )
    assert item.actionable is False
    assert item.actionable_reason == "blocked by #642, #790"


def test_board_treats_an_item_with_no_dependency_as_unblocked() -> None:
    issue = board_issue(10, "Ready", complete_contract("Claim #10."))
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert projected.items[0].open_blockers == ()
    assert projected.items[0].actionable is True
    assert projected.items[0].actionable_reason is None


def test_frozen_item_leaves_actionable_and_thaws_when_the_marker_is_removed() -> None:
    frozen = board_issue(
        301, "Highest scored", complete_contract("Claim #301.", frozen_until=FROZEN_UNTIL)
    )
    projected_while_frozen = projected_board(
        (frozen,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 31, tzinfo=UTC),
    )
    item = projected_while_frozen.items[0]

    assert item.actionable is False
    assert item.actionable_reason == f"frozen: {FROZEN_TRIGGER}"
    assert item.frozen_trigger == FROZEN_TRIGGER
    assert item not in projected_while_frozen.ready_now
    assert board.highest_scored_actionable(projected_while_frozen) is None

    thawed = board_issue(301, "Highest scored", complete_contract("Claim #301."))
    projected_after_thaw = projected_board(
        (thawed,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 31, tzinfo=UTC),
    )
    thawed_item = projected_after_thaw.items[0]

    assert thawed_item.actionable is True
    assert thawed_item.actionable_reason is None
    assert thawed_item.frozen_trigger is None
    assert thawed_item in projected_after_thaw.ready_now
    assert board.highest_scored_actionable(projected_after_thaw) is thawed_item
    # The frozen marker alone changes actionability, never the score itself.
    assert item.score == thawed_item.score


def test_frozen_item_score_stays_visible_on_the_rendered_board() -> None:
    frozen = board_issue(
        301, "Highest scored", complete_contract("Claim #301.", frozen_until=FROZEN_UNTIL)
    )
    projected = projected_board(
        (frozen,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 31, tzinfo=UTC),
    )
    item = projected.items[0]
    rendered = board.render(projected)

    frozen_row = next(line for line in rendered.splitlines() if "#301" in line)
    assert str(item.score) in frozen_row
    assert f"frozen: {FROZEN_TRIGGER}" in frozen_row
    ready_now_section = rendered.split("READY NOW\n", 1)[1].split("\n\nSTALE", 1)[0]
    assert "#301" not in ready_now_section


def test_a_second_agent_claim_fence_inside_a_documentation_fence_is_not_read() -> None:
    """A body may document the block grammar in a fenced example; only one
    fence is ever open at a time, so the inner delimiter never opens a second
    recognized block and the real one stays the only read (#150 §4)."""
    body = (
        agent_claim_body(MINIMAL_BLOCK_TOML)
        + '\n~~~\n```agent-claim\nversion = 1\nnow = "Example only."\n```\n~~~\n'
    )

    parsed = board.parse_body(body)

    assert parsed.read_state is board.BodyReadState.VALID
    assert parsed.contract.now == "N"


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
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    assert issue_claim.main(["--repo", "example/agent-claim", "next"]) == 0
    assert capsys.readouterr().out == (
        "#10 score -10: Lower work\n"
        "Next: Claim #10.\n"
        "Run: aco claim 10 --scope <paths>\n"
        "<paths> cannot be derived; take the files to claim from the item body.\n"
        "\n"
        "SKIPPED\n"
        f"#301: frozen: {FROZEN_TRIGGER}\n"
    )

    assert issue_claim.main(["--repo", "example/agent-claim", "next", "--json"]) == 0
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
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())
    monkeypatch.setattr(issue_claim, "_request", lambda _arguments: claimed_request)

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
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

    def git_output(arguments: list[str]) -> str:
        observed.append(arguments)
        return str(toplevel)

    class BoardClient:
        repository = github._repository_id(REPOSITORY)
        requests = 0

        def capability(self, operation: forge.ForgeOperation) -> forge.Capability:
            return github.GITHUB_CAPABILITIES[operation]

        def list_board_dependencies(self, number: int) -> tuple[board.IssueDependency, ...]:
            return ()

        def list_open_board_issues(self) -> tuple[board.Issue, ...]:
            return (
                board.Issue(
                    20,
                    "Security issue",
                    ("security",),
                    "",
                    "2026-08-20T00:00:00Z",
                    "2026-08-20T00:00:00Z",
                ),
                board.Issue(
                    21,
                    "UX issue",
                    ("ux",),
                    "",
                    "2026-08-20T00:00:00Z",
                    "2026-08-20T00:00:00Z",
                ),
            )

        def list_open_board_pull_requests(self) -> tuple[board.PullRequest, ...]:
            return ()

        def list_recent_merged_board_pull_requests(
            self, since: datetime
        ) -> tuple[board.PullRequest, ...]:
            return ()

        def list_children(self, number: int) -> tuple[board.ChildItem, ...]:
            return ()

    monkeypatch.setattr(checkout, "_git_output", git_output)
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    projected = issue_claim._board(BoardClient(), ())

    assert [item.number for item in projected.items] == [21, 20]
    assert observed == [["rev-parse", "--show-toplevel"]]


@pytest.mark.parametrize(
    ("updated_at", "expected_stale"),
    [
        ("2026-08-14T00:00:00Z", False),
        ("2026-08-13T00:00:00Z", True),
    ],
)
def test_board_marks_text_only_items_stale_only_after_seven_idle_days(
    updated_at: str, expected_stale: bool
) -> None:
    issue = board.Issue(22, "Idle issue", (), "", "2026-08-01T00:00:00Z", updated_at)

    projected = projected_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert [item.number for item in projected.stale] == ([22] if expected_stale else [])


def test_board_ranks_a_real_blocker_ahead_of_a_blocked_product_item() -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    blocker = board.Issue(
        20,
        "Unlabelled prerequisite",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
    )
    product = board.Issue(
        21,
        "Product work",
        ("product",),
        complete_contract("Ship it."),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        blocked_by_count=1,
    )

    projected = projected_board(
        (blocker, product),
        (),
        (),
        (),
        board.BoardConfig(),
        dependencies={21: (block_dependency(20),)},
        now=now,
    )

    assert [item.number for item in projected.items] == [20, 21]
    assert projected.items[0].unblocks_count == 1
    assert projected.items[1].open_blockers == (board.IssueReference(REPOSITORY, 20),)


@pytest.mark.parametrize(
    ("dependencies", "expected_freed_on"),
    [
        pytest.param(
            (
                block_dependency(
                    10,
                    state=board.BlockerState.CLOSED,
                    closed_at=datetime(2026, 9, 1, tzinfo=UTC),
                ),
                block_dependency(11),
            ),
            None,
            id="one-dependency-remains-open",
        ),
        pytest.param(
            (
                block_dependency(
                    10,
                    state=board.BlockerState.CLOSED,
                    closed_at=datetime(2026, 9, 1, tzinfo=UTC),
                ),
                block_dependency(
                    11,
                    state=board.BlockerState.CLOSED,
                    closed_at=datetime(2026, 9, 3, tzinfo=UTC),
                ),
            ),
            datetime(2026, 9, 3, tzinfo=UTC),
            id="all-dependencies-closed",
        ),
    ],
)
def test_board_records_the_latest_closed_dependency(
    dependencies: tuple[board.IssueDependency, ...], expected_freed_on: datetime | None
) -> None:
    freed, blocked_by = blocked_issue(20, "Freed", *dependencies)
    unblocked = board_issue(21, "Never blocked", complete_contract("Ship it."))

    projected = projected_board(
        (freed, unblocked),
        (),
        (),
        (),
        board.BoardConfig(),
        dependencies=blocked_by,
        now=datetime(2026, 9, 5, tzinfo=UTC),
    )
    by_number = {item.number: item for item in projected.items}

    assert by_number[20].freed_on == expected_freed_on
    assert by_number[21].freed_on is None


def test_board_reports_when_the_last_stale_dependency_closed() -> None:
    dependent, blocked_by = blocked_issue(
        20,
        "Freed",
        block_dependency(
            10, state=board.BlockerState.CLOSED, closed_at=datetime(2026, 9, 1, tzinfo=UTC)
        ),
        block_dependency(
            11, state=board.BlockerState.CLOSED, closed_at=datetime(2026, 9, 3, tzinfo=UTC)
        ),
    )

    projected = projected_board(
        (dependent,),
        (),
        (),
        (),
        board.BoardConfig(),
        dependencies=blocked_by,
        now=datetime(2026, 9, 5, tzinfo=UTC),
    )

    item = json.loads(board.board_json(projected))["items"][0]
    assert item["freed_on"] == "2026-09-03"
    assert item["freed_days"] == 2


def test_board_text_and_json_show_freed_on_and_freed_days() -> None:
    freed, freed_dependencies = blocked_issue(
        20,
        "Freed",
        block_dependency(
            10, state=board.BlockerState.CLOSED, closed_at=datetime(2026, 9, 3, tzinfo=UTC)
        ),
    )
    blocked, blocked_dependencies = blocked_issue(21, "Blocked", block_dependency(11))
    unblocked = board_issue(22, "Never blocked", complete_contract("Ship it."))

    projected = projected_board(
        (freed, blocked, unblocked),
        (),
        (),
        (),
        board.BoardConfig(),
        dependencies={**freed_dependencies, **blocked_dependencies},
        now=datetime(2026, 9, 5, tzinfo=UTC),
    )

    rendered = board.render(projected)
    header, *rows = rendered.splitlines()
    freed_start = header.index("FREED")
    claim_start = header.index("CLAIM")

    def freed_cell(title: str) -> str:
        row = next(line for line in rows if line.endswith(title))
        return row[freed_start:claim_start].strip()

    assert freed_cell("Freed") == "2026-09-03 (2 d)"
    assert freed_cell("Blocked") == "-"
    assert freed_cell("Never blocked") == "-"

    items = {item["number"]: item for item in json.loads(board.board_json(projected))["items"]}
    assert items[20]["freed_on"] == "2026-09-03"
    assert items[20]["freed_days"] == 2
    assert items[21]["freed_on"] is None
    assert items[21]["freed_days"] is None
    assert items[22]["freed_on"] is None
    assert items[22]["freed_days"] is None


def test_board_category_order_keeps_ci_ahead_of_a_high_scoring_blocker() -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    ci = board.Issue(30, "CI", ("ci",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z")
    blocker = board.Issue(31, "Blocker", (), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z")
    dependent = board.Issue(
        32,
        "Dependent",
        (),
        "## Blocked by\n#31",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
    )
    open_pull_request = board.PullRequest(90, "Fixes #31", "", "branch")

    projected = projected_board(
        (ci, blocker, dependent),
        (open_pull_request,),
        (),
        (),
        board.BoardConfig(),
        now=now,
    )

    assert [item.number for item in projected.items[:2]] == [30, 31]
    assert projected.items[1].score > projected.items[0].score


def test_board_ranks_a_labelled_critical_item_ahead_of_a_bug_at_equal_score() -> None:
    """Both stay in the critical category (0), but the configured label's
    index still tie-breaks ahead of an unlabelled Bug's -- the same order
    the critical category has always used inside itself. The Bug carries the
    lower issue number, so a naive number tie-break (the Bug ladder removed)
    would flip this to `[1, 30]`."""
    now = datetime(2026, 8, 21, tzinfo=UTC)
    bug = board.Issue(
        1,
        "A fresh bug",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.BUG,
    )
    ci = board.Issue(30, "CI work", ("ci",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z")

    projected = projected_board((ci, bug), (), (), (), board.BoardConfig(), now=now)

    assert [item.number for item in projected.items] == [30, 1]
    assert projected.items[0].score == projected.items[1].score
    assert projected.items[0].priority_category == projected.items[1].priority_category


def test_board_ranks_a_bug_last_inside_the_critical_category() -> None:
    """The Bug and a non-critical product competitor both carry the lowest
    issue numbers here, so a naive number tie-break (the Bug ladder removed)
    would rank them `[1, 2, 40, 41, 42]` instead."""
    now = datetime(2026, 8, 21, tzinfo=UTC)
    bug = board.Issue(
        1,
        "A fresh bug",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.BUG,
    )
    product = board.Issue(
        2, "Product work", ("product",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z"
    )
    security = board.Issue(
        40, "Security", ("security",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z"
    )
    data = board.Issue(41, "Data", ("data",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z")
    ci = board.Issue(42, "CI", ("ci",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z")

    projected = projected_board(
        (security, data, ci, bug, product), (), (), (), board.BoardConfig(), now=now
    )

    assert [item.number for item in projected.items] == [40, 41, 42, 1, 2]
    assert [item.priority_category for item in projected.items[:4]] == [0, 0, 0, 0]
    assert projected.items[4].priority_category > 0


def test_board_ranks_a_bug_ahead_of_a_higher_scoring_product_item() -> None:
    """Category always wins over score: a fresh Bug (category 0) outranks an
    in-flight product item (category 3) even though the product item scores
    higher and carries the lower issue number -- neither a score- nor a
    number-based sort would save this."""
    now = datetime(2026, 8, 21, tzinfo=UTC)
    product = board.Issue(
        2, "Product work", ("product",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z"
    )
    bug = board.Issue(
        40,
        "A fresh bug",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.BUG,
    )
    in_flight_pull_request = board.PullRequest(90, "Fixes #2", "", "branch")

    projected = projected_board(
        (product, bug), (in_flight_pull_request,), (), (), board.BoardConfig(), now=now
    )

    assert [item.number for item in projected.items] == [40, 2]
    assert projected.items[1].score > projected.items[0].score


def test_board_ranks_a_blocker_ahead_of_a_last_open_child() -> None:
    """The completion boost (category 2) never outranks a real blocker (1)."""
    now = datetime(2026, 8, 21, tzinfo=UTC)
    container = board.Issue(
        100,
        "Container",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=1,
        children_total=2,
    )
    last_child = board.Issue(
        101, "Last open child", (), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z"
    )
    blocker = board.Issue(
        102, "Unblocks other work", (), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z"
    )
    dependent = board.Issue(
        103,
        "Depends on the blocker",
        (),
        complete_contract("Ship it."),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        blocked_by_count=1,
    )

    projected = projected_board(
        (container, last_child, blocker, dependent),
        (),
        (),
        (),
        board.BoardConfig(),
        dependencies={103: (block_dependency(102),)},
        now=now,
        children={100: (board.ChildItem(101, board.ChildState.OPEN),)},
    )
    by_number = {item.number: item for item in projected.items}

    assert by_number[101].priority_bucket == "last-child"
    assert by_number[102].priority_bucket == "blocker"
    assert projected.items.index(by_number[102]) < projected.items.index(by_number[101])


def test_completion_boost_requires_at_least_one_closed_sibling() -> None:
    container = board.Issue(
        110,
        "Container",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=1,
    )
    only_child = board.Issue(
        111, "Only child", (), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z"
    )

    projected = projected_board(
        (container, only_child),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        children={110: (board.ChildItem(111, board.ChildState.OPEN),)},
    )

    child_item = next(item for item in projected.items if item.number == 111)
    assert child_item.priority_bucket == "unlabelled"


def test_board_shows_container_progress_and_refuses_it_as_actionable() -> None:
    container = board.Issue(
        120,
        "Container",
        (),
        complete_contract("Cut the next slice."),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=1,
        children_total=2,
    )
    open_child = board_issue(121, "Open child", complete_contract("Ship it."))

    projected = projected_board(
        (container, open_child),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        children={120: (board.ChildItem(121, board.ChildState.OPEN),)},
    )

    container_item = next(item for item in projected.items if item.number == 120)
    assert container_item.actionable is False
    assert container_item.actionable_reason == "container; claim a child"
    assert container_item not in projected.ready_now
    assert container_item.container == board.ContainerProgress(
        1, 2, (board.ChildItem(121, board.ChildState.OPEN, blocked_by=()),)
    )

    rendered = board.render(projected)
    header = rendered.splitlines()[0]
    assert "KIND" in header
    assert "container 1/2" in rendered
    assert "CONTAINERS" in rendered
    assert "#120 1/2 closed; open: #121" in rendered

    payload = json.loads(board.board_json(projected))
    container_json = next(item for item in payload["items"] if item["number"] == 120)
    child_json = next(item for item in payload["items"] if item["number"] == 121)
    assert container_json["kind"] == "container"
    assert container_json["container"] == {
        "closed": 1,
        "total": 2,
        "open_children": [
            {"number": 121, "state": "open", "blocked_by": [], "foreign_blockers": []}
        ],
    }
    assert container_json["container_parent"] is None
    assert child_json["kind"] is None
    assert child_json["container_parent"] == 120
    assert child_json["priority_order"] == 0


def test_board_shows_a_container_child_blocked_by_another_open_issue() -> None:
    """An open container child can itself be blocked; the container's own
    open-children note must show that, not just the bare child number."""
    container = board.Issue(
        120,
        "Container",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=1,
    )
    blocker = board_issue(130, "Blocker", complete_contract("Ship it."))
    open_child, child_dependencies = blocked_issue(
        121, "Open child", block_dependency(130), next_step="Ship it."
    )

    projected = projected_board(
        (container, blocker, open_child),
        (),
        (),
        (),
        board.BoardConfig(),
        dependencies=child_dependencies,
        now=datetime(2026, 8, 21, tzinfo=UTC),
        children={120: (board.ChildItem(121, board.ChildState.OPEN),)},
    )

    assert "#120 0/1 closed; open: #121 (blocked by #130)" in board.render(projected)

    payload = json.loads(board.board_json(projected))
    container_json = next(item for item in payload["items"] if item["number"] == 120)
    assert container_json["container"]["open_children"] == [
        {"number": 121, "state": "open", "blocked_by": [130], "foreign_blockers": []}
    ]


def test_board_json_splits_a_container_childs_foreign_blocker() -> None:
    """`board --json`'s `container.open_children[].blocked_by` projects the
    same way `BoardItem.open_blockers` does (#150 A2): local ints, with the
    qualified foreign references in a sibling `foreign_blockers` key."""
    container = board.Issue(
        120,
        "Container",
        (),
        agent_claim_body(MINIMAL_BLOCK_TOML),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=1,
    )
    open_child = board.Issue(
        121,
        "Open child",
        (),
        agent_claim_body(MINIMAL_BLOCK_TOML),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        blocked_by_count=1,
    )
    dependencies = {
        121: (block_dependency(3), block_dependency(9, repository="overnightworks/other-repo"))
    }

    projected = projected_board(
        (container, open_child),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        children={120: (board.ChildItem(121, board.ChildState.OPEN),)},
        dependencies=dependencies,
    )

    payload = json.loads(board.board_json(projected))
    container_json = next(item for item in payload["items"] if item["number"] == 120)
    open_children = container_json["container"]["open_children"]

    assert open_children == [
        {
            "number": 121,
            "state": "open",
            "blocked_by": [3],
            "foreign_blockers": ["overnightworks/other-repo#9"],
        }
    ]


def test_board_kind_cell_shows_a_plain_kind_for_a_non_container_item() -> None:
    """`_kind_cell` names a real kind (task/bug/feature) plainly, without the
    container's "closed/total" progress suffix that only a container gets."""
    task = board.Issue(
        90,
        "Fix the thing",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.TASK,
    )

    projected = projected_board((task,), (), (), (), board.BoardConfig())

    header, row = board.render(projected).splitlines()[:2]
    kind_start = header.index("KIND")
    assert row[kind_start:].startswith("task")


def test_board_json_carries_a_nonzero_priority_order_for_a_critical_label() -> None:
    security_item = board.Issue(
        60, "Security work", ("security",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z"
    )
    ux_item = board.Issue(
        61, "UX work", ("ux",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z"
    )

    projected = projected_board(
        (security_item, ux_item),
        (),
        (),
        (),
        board.BoardConfig(priority_labels=("ux", "security")),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )
    payload = json.loads(board.board_json(projected))
    by_number = {item["number"]: item for item in payload["items"]}

    assert by_number[60]["priority_order"] == 1
    assert by_number[61]["priority_order"] == 0


def test_next_action_names_the_top_actionable_work_item() -> None:
    item = board_issue(10, "Top work", complete_contract("Claim #10."))
    projected = projected_board(
        (item,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    action = board.next_action(projected)

    assert isinstance(action, board.WorkItemAction)
    assert action.item.number == 10


def test_next_action_cuts_a_container_with_no_open_child_and_further_next_work() -> None:
    container = board.Issue(
        130,
        "Container",
        (),
        complete_contract("Cut the next slice.", done_when="All slices land."),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=2,
        children_total=2,
    )
    projected = projected_board(
        (container,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    action = board.next_action(projected)

    assert isinstance(action, board.CutSliceAction)
    assert action.container.number == 130
    assert action.next_step == "Cut the next slice."


def test_next_action_closes_a_container_with_no_open_child_and_no_further_work() -> None:
    container = board.Issue(
        140,
        "Container",
        (),
        complete_contract("keiner", done_when="All slices land."),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=3,
        children_total=3,
    )
    projected = projected_board(
        (container,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    action = board.next_action(projected)

    assert isinstance(action, board.CloseContainerAction)
    assert action.container.number == 140
    assert action.container_progress == board.ContainerProgress(3, 3, ())


def test_next_action_cuts_a_container_with_an_uncut_row_and_no_further_next_work() -> None:
    """An empty `Next` line alone must not close a container that still has
    an undispatched `[[slice]]` entry (#112 finding 1)."""
    container = board.Issue(
        141,
        "Container",
        (),
        complete_contract("", slice=slice_entries("Scheibe C")),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=1,
        children_total=1,
    )
    projected = projected_board(
        (container,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    action = board.next_action(projected)

    assert isinstance(action, board.CutSliceAction)
    assert action.container.number == 141
    assert action.next_step == "Scheibe C"


def test_container_progress_raises_when_an_open_child_contradicts_a_closed_summary() -> None:
    container = board.Issue(
        190,
        "Container",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=2,
        children_total=2,
    )
    raised_argument_1 = board.BoardConfig()
    raised_argument_2 = datetime(2026, 8, 21, tzinfo=UTC)
    raised_argument_3 = {190: (board.ChildItem(191, board.ChildState.OPEN),)}

    with pytest.raises(protocol.ClaimError, match=r"malformed board container #190"):
        projected_board(
            (container,),
            (),
            (),
            (),
            raised_argument_1,
            now=raised_argument_2,
            children=raised_argument_3,
        )


def test_container_progress_raises_when_no_open_child_contradicts_an_unclosed_summary() -> None:
    container = board.Issue(
        191,
        "Container",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=1,
        children_total=2,
    )
    raised_argument_1 = board.BoardConfig()
    raised_argument_2 = datetime(2026, 8, 21, tzinfo=UTC)

    with pytest.raises(protocol.ClaimError, match=r"malformed board container #191"):
        projected_board((container,), (), (), (), raised_argument_1, now=raised_argument_2)


def test_board_json_and_render_report_an_uncut_slice_entry() -> None:
    container = board.Issue(
        160,
        "Container",
        (),
        complete_contract("Cut it.", slice=slice_entries("Undispatched slice")),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    projected = projected_board(
        (container,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.uncut == (board.UncutSlices(160, (board.SliceRow(1, "Undispatched slice"),)),)
    payload = json.loads(board.board_json(projected))
    assert payload["uncut"] == [
        {"item": 160, "rows": [{"index": 1, "title": "Undispatched slice"}]}
    ]
    assert "UNCUT\n#160: rows 1 uncut" in board.render(projected)


def test_board_json_and_render_name_several_uncut_rows_by_index() -> None:
    """`#122`'s own shape (06.09.2026): several still-open rows are named
    by index, not by re-deriving it from a name string."""
    container = board.Issue(
        122,
        "Container",
        (),
        complete_contract(
            "Cut them.",
            slice=slice_entries("Fifth slice", "Sixth slice", "Seventh slice", first_index=5),
        ),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    projected = projected_board(
        (container,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.uncut == (
        board.UncutSlices(
            122,
            (
                board.SliceRow(5, "Fifth slice"),
                board.SliceRow(6, "Sixth slice"),
                board.SliceRow(7, "Seventh slice"),
            ),
        ),
    )
    payload = json.loads(board.board_json(projected))
    assert payload["uncut"] == [
        {
            "item": 122,
            "rows": [
                {"index": 5, "title": "Fifth slice"},
                {"index": 6, "title": "Sixth slice"},
                {"index": 7, "title": "Seventh slice"},
            ],
        }
    ]
    assert "UNCUT\n#122: rows 5, 6, 7 uncut" in board.render(projected)


def test_next_action_skips_a_container_that_still_holds_an_open_child() -> None:
    container = board.Issue(
        150,
        "Container",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=1,
    )
    projected = projected_board(
        (container,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        children={150: (board.ChildItem(151, board.ChildState.OPEN),)},
    )

    assert board.next_action(projected) is None


def test_next_names_a_cuttable_container_slice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Exact `cut_slice #N: …` text, per #112's own body example."""
    container = board.Issue(
        180,
        "Epic",
        (),
        complete_contract("Scheibe B — Kartenraster"),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=1,
        children_total=1,
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    exit_code = issue_claim.main(["--repo", "example/agent-claim", "next"])

    assert exit_code == 0
    assert capsys.readouterr().out == (
        "cut_slice #180: Scheibe B — Kartenraster\n"
        'Next: aco cut 180 --title "Scheibe B — Kartenraster"\n'
    )


# A `Next` line whose own prose never matches any slice-table row title used
# below -- the shape #177 fixes: a container whose Next line and first uncut
# row disagree.
_DIFFERING_NEXT_LINE = "Weitere Aufgabe."


@dataclass(frozen=True)
class _CutRoundTripCase:
    """One #151 round-trip scenario, over {container's own `Next` line still
    names work} x {block carries an undispatched `[[slice]]`}: the `cut`
    command `next` prints for `container_number` must be one `cut` itself
    accepts. `row_title` is that one entry's title when the container
    carries one, else `None`. `expected_item_bodies`/`expected_output` take
    the freshly created child's number, since only `cut` fixes that."""

    case_id: str
    container_number: int
    body: str
    expected_created_title: str
    row_title: str | None
    expected_item_bodies: Callable[[int], dict[int, str]]
    expected_output: Callable[[int], str]


def _no_uncut_row_case(
    case_id: str, container_number: int, body: str, next_step: str
) -> _CutRoundTripCase:
    """When no undispatched entry exists (no `slice` key, or an empty
    array), `cut` always creates a child tied to no entry, titled with the
    container's own `Next` words."""
    return _CutRoundTripCase(
        case_id,
        container_number,
        body,
        next_step,
        None,
        lambda _child: {},
        lambda child: f"CUT #{container_number} -> #{child}\n",
    )


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
        row_title,
        lambda _child: {container_number: complete_contract(next_line, slice=[])},
        lambda child: f"CUT #{container_number} row 1 -> #{child}\n",
    )


_CUT_ROUND_TRIP_CASES = (
    # next=yes, uncut=no (no `slice` key at all) -- exactly what #122 hit on
    # 06.09.2026, whose container carried a `Next` line but no slice list.
    _no_uncut_row_case("next_only_no_slice_key", 183, complete_contract("Scheibe D"), "Scheibe D"),
    # next=yes, uncut=no (every entry already cut away) -- the remaining #151
    # gap: an emptied list must not block the `Next` line's own pathway.
    _no_uncut_row_case(
        "next_only_emptied_slice_list",
        185,
        complete_contract(_DIFFERING_NEXT_LINE, slice=[]),
        _DIFFERING_NEXT_LINE,
    ),
    # next=no, uncut=yes -- the slice-backed twin of the case above (#151).
    _uncut_row_case("uncut_row_only", 184, "", "Scheibe E"),
    # next=yes, uncut=yes, and they disagree -- #177 itself: seven live
    # atelier-2 containers where `next` printed the `Next` line's prose and
    # `cut` refused it, because the row it actually links carries a
    # different title.
    _uncut_row_case("next_and_differing_uncut_row", 186, _DIFFERING_NEXT_LINE, "Scheibe F"),
    # The remaining combination -- neither a `Next` line nor an uncut row --
    # closes the container instead of cutting a slice, so it has no `cut`
    # command to round-trip; `test_next_names_a_closeable_container` proves it.
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
        kind=board.ItemKind.CONTAINER,
        children_closed=1,
        children_total=1,
    )
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    next_exit_code = issue_claim.main(["--repo", "example/agent-claim", "next"])
    assert next_exit_code == 0
    command_line = capsys.readouterr().out.splitlines()[1]
    cut_arguments = shlex.split(command_line.removeprefix("Next: aco "))

    cut_exit_code = issue_claim.main(["--repo", "example/agent-claim", *cut_arguments])

    assert cut_exit_code == 0
    child = client.next_created_child_number - 1
    assert client.created_children == [
        (
            case.container_number,
            case.expected_created_title,
            board.BLOCK_CHILD_SKELETON,
            board.ItemKind.TASK,
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
        complete_contract(_DIFFERING_NEXT_LINE),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
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
        kind=board.ItemKind.CONTAINER,
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
        if item.kind is not board.ItemKind.CONTAINER:
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

        command_line = issue_claim._next_action_lines(action)[1]
        cut_arguments = shlex.split(command_line.removeprefix("Next: aco "))
        client = _configured_board_client(
            monkeypatch, tmp_path, open_issues=(containers_by_number[item.number],)
        )

        cut_exit_code = issue_claim.main(["--repo", "example/agent-claim", *cut_arguments])

        assert cut_exit_code == 0
        assert client.created_children[0][0] == item.number


def test_next_json_names_a_cuttable_container_slice(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    container = board.Issue(
        181,
        "Epic",
        (),
        complete_contract("Scheibe C"),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=2,
        children_total=2,
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    exit_code = issue_claim.main(["--repo", "example/agent-claim", "next", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "cut_slice"
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
        kind=board.ItemKind.CONTAINER,
        children_closed=3,
        children_total=3,
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    exit_code = issue_claim.main(["--repo", "example/agent-claim", "next"])

    assert exit_code == 0
    assert capsys.readouterr().out == "close_container #182: 3/3 children closed, no Next work\n"


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
        kind=board.ItemKind.CONTAINER,
        children_closed=4,
        children_total=4,
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    exit_code = issue_claim.main(["--repo", "example/agent-claim", "next", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "close_container"
    assert payload["number"] == 183
    assert payload["closed"] == 4
    assert payload["total"] == 4
    assert "command" not in payload


def test_next_names_the_boards_top_row_even_when_it_is_not_the_highest_score() -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    in_flight_unlabelled = board_issue(50, "In-flight, unlabelled", complete_contract("Ship it."))
    blocker = board_issue(
        51, "Prerequisite the operator prioritized", complete_contract("Unblock #52.")
    )
    dependent, dependent_blockers = blocked_issue(
        52, "Depends on the prerequisite", block_dependency(51), next_step="Ship it."
    )
    open_pull_request = board.PullRequest(200, "Fixes #50", "", "branch")

    projected = projected_board(
        (in_flight_unlabelled, blocker, dependent),
        (open_pull_request,),
        (),
        (),
        board.BoardConfig(),
        dependencies=dependent_blockers,
        now=now,
    )
    by_number = {item.number: item for item in projected.items}

    # #50 outscores #51 on raw score alone; #51 still leads because it carries
    # the higher-priority "blocker" bucket (it unblocks #52) that `board`
    # already sorts on ahead of score.
    assert by_number[50].score > by_number[51].score
    assert projected.items[0].number == 51

    recommended = board.highest_scored_actionable(projected)
    assert recommended is not None
    assert recommended.number == 51


def _slice_pull_request_body(epic: int) -> str:
    """A genuine slice-to-epic pull request body, in the shape observed
    verbatim in atelier-2's #848 and #960: the epic is named twice, once in
    substantive prose and again in a dedicated, non-closing trailer line.
    Both mentions are required — see
    `test_a_dedicated_reference_line_without_corroboration_confers_no_stage`
    for why a single, uncorroborated trailer line is not enough on its own.
    """
    return f"Ships one slice of epic #{epic}'s plan.\n\nPart of #{epic}."


def test_an_epic_inherits_the_landed_stage_of_a_slice_that_did_not_close_it() -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    epic = board_issue(
        60, "Epic cut into dispatched slices", complete_contract("Cut the next slice.")
    )
    slice_pull_request = board.PullRequest(120, "Slice 1", _slice_pull_request_body(60), "branch")

    projected = projected_board(
        (epic,), (), (slice_pull_request,), (), board.BoardConfig(), now=now
    )

    assert projected.items[0].stage is board.Stage.CODE_LANDED


def test_an_epic_is_in_flight_while_an_open_slice_touches_it_without_closing_it() -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    epic = board_issue(
        62, "Epic cut into dispatched slices", complete_contract("Cut the next slice.")
    )
    open_slice_pull_request = board.PullRequest(
        122, "Slice 1", _slice_pull_request_body(62), "branch"
    )

    projected = projected_board(
        (epic,), (open_slice_pull_request,), (), (), board.BoardConfig(), now=now
    )

    assert projected.items[0].stage is board.Stage.IN_FLIGHT


def test_a_pull_request_merely_mentioning_the_epic_number_confers_no_stage() -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    epic = board_issue(
        61, "Epic untouched by this pull request", complete_contract("Cut the next slice.")
    )
    unrelated_pull_request = board.PullRequest(
        121,
        "Unrelated fix",
        "This closes a bug that was discovered while reading #61's plan.",
        "branch",
    )

    projected = projected_board(
        (epic,), (), (unrelated_pull_request,), (), board.BoardConfig(), now=now
    )

    assert projected.items[0].stage is board.Stage.TEXT_ONLY


def test_a_dedicated_reference_line_without_corroboration_confers_no_stage() -> None:
    """A foreign pull request can still write a dedicated `Refs #N` line for an
    unrelated reason; this tool has no typed parentage relation to rule that
    out (see `_touched_without_closing`'s docstring). The one thing it can
    require is that the epic is discussed, not just named once in a trailer —
    dropping this drops the false positive without dropping the two real
    landings above, which both name their epic a second time.
    """
    now = datetime(2026, 8, 21, tzinfo=UTC)
    epic = board_issue(
        63, "Epic named only once, in passing", complete_contract("Cut the next slice.")
    )
    drive_by_pull_request = board.PullRequest(123, "Unrelated cleanup", "Refs #63.", "branch")

    projected = projected_board(
        (epic,), (), (drive_by_pull_request,), (), board.BoardConfig(), now=now
    )

    assert projected.items[0].stage is board.Stage.TEXT_ONLY


def test_a_reference_line_inside_a_fenced_code_block_confers_no_stage() -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    epic = board_issue(
        64, "Epic quoted inside an example, not touched", complete_contract("Cut the next slice.")
    )
    fenced_pull_request = board.PullRequest(
        124,
        "Documents the marker syntax",
        "Example of the convention:\n\n```\nPart of #64.\n```\n\nSee also #64 above.",
        "branch",
    )

    projected = projected_board(
        (epic,), (), (fenced_pull_request,), (), board.BoardConfig(), now=now
    )

    assert projected.items[0].stage is board.Stage.TEXT_ONLY


def test_a_fenced_closing_keyword_confers_no_stage() -> None:
    """The closing-keyword path (`_associated_issues`) must read the body the
    same way `_touched_without_closing` already does: a fenced example of the
    `Fixes #N` convention documents the syntax, it does not close #65.
    """
    now = datetime(2026, 8, 21, tzinfo=UTC)
    issue = board_issue(
        65, "Issue documented, never actually closed", complete_contract("Cut the next slice.")
    )
    fenced_pull_request = board.PullRequest(
        125,
        "Documents the closing-keyword syntax",
        "Example of the convention:\n\n```\nFixes #65.\n```\n\nNot itself a closing PR.",
        "branch",
    )

    projected = projected_board(
        (issue,), (), (fenced_pull_request,), (), board.BoardConfig(), now=now
    )

    assert projected.items[0].stage is board.Stage.TEXT_ONLY


def test_board_queries_merged_pull_requests_back_to_the_oldest_open_issue(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    old_epic = replace(
        board_issue(70, "Epic open for months", complete_contract("Cut the next slice.")),
        created_at="2026-06-01T00:00:00Z",
    )
    recent_issue = board_issue(71, "Recently filed work", complete_contract("Ship it."))
    observed_since: list[datetime] = []

    class BoardClient:
        repository = github._repository_id(REPOSITORY)
        requests = 0

        def capability(self, operation: forge.ForgeOperation) -> forge.Capability:
            return github.GITHUB_CAPABILITIES[operation]

        def list_board_dependencies(self, number: int) -> tuple[board.IssueDependency, ...]:
            return ()

        def list_open_board_issues(self) -> tuple[board.Issue, ...]:
            return (old_epic, recent_issue)

        def list_open_board_pull_requests(self) -> tuple[board.PullRequest, ...]:
            return ()

        def list_recent_merged_board_pull_requests(
            self, since: datetime
        ) -> tuple[board.PullRequest, ...]:
            observed_since.append(since)
            return ()

        def list_children(self, number: int) -> tuple[board.ChildItem, ...]:
            return ()

    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    issue_claim._board(BoardClient(), ())

    # A fixed 14-day window (now - 14 days = 2026-08-07) would have missed
    # anything the six-month-old epic's own slices landed months ago.
    assert observed_since == [datetime(2026, 6, 1, tzinfo=UTC)]


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
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=1,
    )
    plain = board_issue(91, "Plain", complete_contract("Ship it."))
    observed: list[int] = []

    class BoardClient:
        repository = github._repository_id(REPOSITORY)
        requests = 0

        def capability(self, operation: forge.ForgeOperation) -> forge.Capability:
            return github.GITHUB_CAPABILITIES[operation]

        def list_board_dependencies(self, number: int) -> tuple[board.IssueDependency, ...]:
            return ()

        def list_open_board_issues(self) -> tuple[board.Issue, ...]:
            return (container, plain)

        def list_open_board_pull_requests(self) -> tuple[board.PullRequest, ...]:
            return ()

        def list_recent_merged_board_pull_requests(
            self, since: datetime
        ) -> tuple[board.PullRequest, ...]:
            return ()

        def list_children(self, number: int) -> tuple[board.ChildItem, ...]:
            observed.append(number)
            return (board.ChildItem(92, board.ChildState.OPEN),)

    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    projected = issue_claim._board(BoardClient(), ())

    assert observed == [90]
    container_item = next(item for item in projected.items if item.number == 90)
    assert container_item.container is not None
    assert container_item.container.open_children == (board.ChildItem(92, board.ChildState.OPEN),)


def test_board_configuration_requires_unique_ordered_labels(tmp_path: Path) -> None:
    config_path = tmp_path / "board.toml"
    config_path.write_text('priority_labels = ["ux", "security"]\n')
    assert board.load_config(config_path).priority_labels == ("ux", "security")

    config_path.write_text("priority_labels = []\n")
    with pytest.raises(ClaimError, match="priority_labels"):
        board.load_config(config_path)


def test_board_configuration_fails_loud_on_unparsable_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "board.toml"
    config_path.write_text("this is not valid toml =\n")

    with pytest.raises(ClaimError, match=f"cannot read board configuration {config_path}"):
        board.load_config(config_path)


def test_board_configuration_reads_and_validates_the_idea_label(tmp_path: Path) -> None:
    config_path = tmp_path / "board.toml"
    config_path.write_text('priority_labels = ["ux", "security"]\nidea_label = "idea"\n')

    assert board.load_config(config_path) == board.BoardConfig(("ux", "security"), "idea")

    config_path.write_text('idea_label = ""\n')
    with pytest.raises(ClaimError, match="idea_label"):
        board.load_config(config_path)


def test_board_configuration_keeps_body_contract_as_a_known_block_only_key(
    tmp_path: Path,
) -> None:
    """Issue #204: the pin survives with one legal value. A repository that
    carries `"block"` loads unchanged, an absent key means the block, and
    `"prose"` is refused by name -- never as an unknown key, which would
    refuse every store command in the repositories that still pin it."""
    config_path = tmp_path / "board.toml"
    assert board.load_config(config_path) == board.BoardConfig()

    config_path.write_text('body_contract = "block"\n')
    assert board.load_config(config_path) == board.BoardConfig()

    config_path.write_text('body_contract = "prose"\n')
    with pytest.raises(ClaimError) as refused_prose:
        board.load_config(config_path)
    assert str(refused_prose.value) == (
        f"board configuration {config_path} pins body_contract 'prose': "
        "prose bodies are no longer supported"
    )

    config_path.write_text('body_contract = "sideways"\n')
    with pytest.raises(
        ClaimError, match=f"{re.escape(str(config_path))} body_contract must be 'block'"
    ):
        board.load_config(config_path)

    config_path.write_text("body_contract = true\n")
    with pytest.raises(ClaimError, match="body_contract must be 'block'"):
        board.load_config(config_path)


def test_board_configuration_reads_and_validates_canonical_remote(tmp_path: Path) -> None:
    config_path = tmp_path / "board.toml"
    assert board.load_config(config_path).canonical_remote == "origin"

    config_path.write_text('canonical_remote = "upstream"\n')
    assert board.load_config(config_path).canonical_remote == "upstream"

    config_path.write_text('canonical_remote = ""\n')
    with pytest.raises(ClaimError, match="canonical_remote must be a non-empty remote name"):
        board.load_config(config_path)

    config_path.write_text("canonical_remote = true\n")
    with pytest.raises(ClaimError, match="canonical_remote must be a non-empty remote name"):
        board.load_config(config_path)


def test_board_configuration_refuses_an_unknown_key_by_name(tmp_path: Path) -> None:
    """A typo would otherwise leave the setting at its default, with
    nothing in any output saying the file's own value was never read."""
    config_path = tmp_path / "board.toml"
    config_path.write_text('body_contarct = "block"\n')

    with pytest.raises(ClaimError) as refused:
        board.load_config(config_path)

    assert str(refused.value) == (
        f"board configuration {config_path} has unknown top-level key body_contarct"
    )

    config_path.write_text('bodies = "block"\nannotation = "x"\n')
    with pytest.raises(ClaimError, match="unknown top-level key annotation, bodies"):
        board.load_config(config_path)


def test_board_configuration_accepts_every_key_it_defines(tmp_path: Path) -> None:
    config_path = tmp_path / "board.toml"
    config_path.write_text(
        'priority_labels = ["ux"]\nidea_label = "idea"\n'
        'body_contract = "block"\ncanonical_remote = "upstream"\n'
    )

    assert board.load_config(config_path) == board.BoardConfig(("ux",), "idea", "upstream")


def test_the_body_fence_and_config_path_keep_their_agent_claim_names() -> None:
    """The package renamed to `agent-coordination` and the command to `aco`
    (issue #191); these two strings deliberately did not follow.

    The fence info string is spelled inside the issue bodies of every migrated
    repository and the configuration file already sits at this path in each
    checkout. Renaming either would make this release silently stop reading
    state that is already written -- so they are protocol, not product name,
    and this test is what says so out loud.
    """
    assert board.AGENT_CLAIM_FENCE_INFO == "agent-claim"
    assert board.BLOCK_CHILD_SKELETON.startswith(f"```{board.AGENT_CLAIM_FENCE_INFO}\n")
    assert board.CONFIG_PATH.as_posix() == ".agent-claim/board.toml"


def test_parse_body_reads_a_valid_minimal_block() -> None:
    parsed = board.parse_body(agent_claim_body(MINIMAL_BLOCK_TOML))

    assert parsed.read_state is board.BodyReadState.VALID
    assert parsed.contract == board.Contract("N", "X", "D", ())
    assert parsed.contract_complete is True


def test_parse_body_reads_a_skeleton_block_as_incomplete_but_valid() -> None:
    skeleton = 'version = 1\nnow = ""\nnext = ""\ndone_when = ""\n'

    parsed = board.parse_body(agent_claim_body(skeleton))

    assert parsed.read_state is board.BodyReadState.VALID
    assert parsed.contract_complete is False
    assert parsed.projectionless is True


@pytest.mark.parametrize(
    ("toml_text", "missing"),
    [
        pytest.param(MINIMAL_BLOCK_TOML, (), id="a-filled-block-is-complete"),
        pytest.param(
            'version = 1\nnow = "N"\nnext = ""\ndone_when = ""\n',
            ("Next", "Done when"),
            id="a-half-filled-skeleton-names-only-its-empty-keys",
        ),
    ],
)
def test_missing_or_empty_sections_never_names_a_dependency_key(
    toml_text: str, missing: tuple[str, ...]
) -> None:
    """A body carries no dependency key at all -- dependencies live on the
    forge -- so naming one would refuse every correctly migrated body."""
    contract = board.parse_body(agent_claim_body(toml_text)).contract

    assert board.missing_or_empty_sections(contract) == missing


def test_parse_body_treats_a_fenceless_body_as_legacy() -> None:
    parsed = board.parse_body("## Now\nOld prose.\n")

    assert parsed.read_state is board.BodyReadState.LEGACY
    assert parsed.contract == board.Contract(None, None, None, ())


def test_parse_body_refuses_multiple_agent_claim_blocks() -> None:
    body = agent_claim_body(MINIMAL_BLOCK_TOML) + agent_claim_body(MINIMAL_BLOCK_TOML)

    parsed = board.parse_body(body)

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert parsed.contract.defects[0].field == "agent-claim"


def test_parse_body_refuses_an_unclosed_agent_claim_block() -> None:
    parsed = board.parse_body("```agent-claim\nversion = 1\n")

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert parsed.contract.defects == (
        board.ContractDefect("agent-claim", "unclosed agent-claim block"),
    )


def test_parse_body_refuses_invalid_toml() -> None:
    parsed = board.parse_body(agent_claim_body("this is not toml ="))

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert parsed.contract.defects[0].field == "agent-claim"


def test_parse_body_orders_schema_defects_deterministically() -> None:
    toml_text = (
        "now = 1\n"
        "unexpected = 1\n"
        'frozen_until = { trigger = "", ruled_on = "2026-09-06", odd = 1 }\n'
        "[[expectation]]\n"
        'text = ""\n'
        "[[slice]]\n"
        'title = ""\n'
        "weird = 1\n"
    )

    parsed = board.parse_body(agent_claim_body(toml_text))

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert [defect.field for defect in parsed.contract.defects] == [
        "version",
        "now",
        "next",
        "done_when",
        "frozen_until.trigger",
        "frozen_until.ruled_on",
        "frozen_until.odd",
        "expectation[0].text",
        "expectation[0].default",
        "slice[0].index",
        "slice[0].title",
        "slice[0].weird",
        "unexpected",
    ]


@pytest.mark.parametrize(
    ("entry_toml", "expected_field"),
    [
        pytest.param('text = "E"\ndefault = "maybe"\n', "expectation[0].default", id="bad-default"),
        pytest.param(
            'text = "E"\nruling = "maybe"\nruled_on = 2026-09-06\n',
            "expectation[0].ruling",
            id="bad-ruling",
        ),
        pytest.param(
            'text = "E"\ndefault = "yes"\nruling = "yes"\nruled_on = 2026-09-06\n',
            "expectation[0].default",
            id="both-default-and-ruling",
        ),
        pytest.param('text = "E"\n', "expectation[0].default", id="neither"),
        pytest.param(
            'text = "E"\nruling = "yes"\nruled_on = "not-a-date"\n',
            "expectation[0].ruled_on",
            id="bad-ruled-on",
        ),
    ],
)
def test_parse_body_validates_the_expectation_variant_union(
    entry_toml: str, expected_field: str
) -> None:
    toml_text = f"{MINIMAL_BLOCK_TOML}[[expectation]]\n{entry_toml}"

    parsed = board.parse_body(agent_claim_body(toml_text))

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert parsed.contract.defects[0].field == expected_field


def test_parse_body_ruling_date_is_the_oldest_across_non_monotonic_expectations() -> None:
    toml_text = (
        f"{MINIMAL_BLOCK_TOML}"
        '[[expectation]]\ntext = "A"\nruling = "yes"\nruled_on = 2026-09-10\n'
        '[[expectation]]\ntext = "B"\nruling = "yes"\nruled_on = 2026-08-01\n'
    )

    parsed = board.parse_body(agent_claim_body(toml_text))

    assert parsed.read_state is board.BodyReadState.VALID
    assert parsed.expectation_state is board.ExpectationState.RULED
    assert parsed.ruling_date == date(2026, 8, 1)


def test_parse_body_refuses_a_frozen_until_that_is_not_a_table() -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}frozen_until = "not a table"\n'

    parsed = board.parse_body(agent_claim_body(toml_text))

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert parsed.contract.defects[0].field == "frozen_until.trigger"


def test_parse_body_refuses_a_non_table_expectation_entry() -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}expectation = ["oops"]\n'

    parsed = board.parse_body(agent_claim_body(toml_text))

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert parsed.contract.defects[0].field == "expectation[0]"


def test_parse_body_refuses_a_non_table_slice_entry() -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}slice = ["oops"]\n'

    parsed = board.parse_body(agent_claim_body(toml_text))

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert parsed.contract.defects[0].field == "slice[0]"


def test_parse_body_refuses_a_duplicate_slice_index() -> None:
    toml_text = (
        f'{MINIMAL_BLOCK_TOML}[[slice]]\nindex = 1\ntitle = "First"\n'
        '[[slice]]\nindex = 1\ntitle = "Second"\n'
    )

    parsed = board.parse_body(agent_claim_body(toml_text))

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert parsed.contract.defects[0].field == "slice[1].index"


@pytest.mark.parametrize(
    ("key", "malformed_toml"),
    [
        pytest.param("expectation", 'expectation = "oops"\n', id="expectation-not-a-list"),
        pytest.param("slice", 'slice = "oops"\n', id="slice-not-a-list"),
    ],
)
def test_parse_body_refuses_a_top_level_array_key_that_is_not_a_list(
    key: str, malformed_toml: str
) -> None:
    parsed = board.parse_body(agent_claim_body(f"{MINIMAL_BLOCK_TOML}{malformed_toml}"))

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert parsed.contract.defects[0].field == key


def test_parse_body_handles_a_body_with_no_trailing_newline() -> None:
    """`_line_ending` (used while walking every line for a fenced block)
    must also return `""` for the last line of a body that ends without a
    newline at all -- an ordinary GitHub body shape, not just a CRLF/LF one."""
    body = agent_claim_body(MINIMAL_BLOCK_TOML).rstrip("\n") + "\nProse with no trailing newline"

    parsed = board.parse_body(body)

    assert parsed.read_state is board.BodyReadState.VALID


def test_locate_agent_claim_block_fails_loud_with_no_recognized_fence() -> None:
    with pytest.raises(ClaimError, match="found no recognized agent-claim fence"):
        board.locate_agent_claim_block("## Now\nOld prose.\n")


def test_locate_agent_claim_block_fails_loud_with_an_unclosed_fence() -> None:
    with pytest.raises(ClaimError, match="found no closed agent-claim fence"):
        board.locate_agent_claim_block("```agent-claim\nversion = 1\n")


def test_parse_body_reads_an_emptied_slice_array_as_nothing_left_to_cut() -> None:
    toml_text = f"{MINIMAL_BLOCK_TOML}slice = []\n"

    parsed = board.parse_body(agent_claim_body(toml_text))

    assert parsed.read_state is board.BodyReadState.VALID
    assert parsed.slices == ()


def test_parse_body_reads_slice_entries_as_still_uncut() -> None:
    toml_text = (
        f'{MINIMAL_BLOCK_TOML}[[slice]]\nindex = 4\ntitle = "Block contract in issue bodies"\n'
    )

    parsed = board.parse_body(agent_claim_body(toml_text))

    assert parsed.slices == (board.SliceRow(4, "Block contract in issue bodies"),)


def test_parse_body_recognizes_a_crlf_fenced_block() -> None:
    body = (
        "Prose before.\r\n\r\n"
        "```agent-claim\r\n"
        'version = 1\r\nnow = "N"\r\nnext = "X"\r\ndone_when = "D"\r\n'
        "```\r\n\r\nProse after.\r\n"
    )

    parsed = board.parse_body(body)

    assert parsed.read_state is board.BodyReadState.VALID
    assert parsed.contract == board.Contract("N", "X", "D", ())


def test_body_contract_checks_names_a_legacy_container_by_the_body_legacy_check() -> None:
    body = "## Now\nOld prose.\n\n## Next\nDo the thing.\n"
    legacy = replace(
        board_issue(201, "Legacy container", body),
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    projected = projected_board(
        (legacy,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )
    item = next(item for item in projected.items if item.number == 201)

    checks = issue_claim._body_contract_checks(item)

    assert checks == (issue_claim.SliceCheck("error", "body-legacy", "body legacy", issue=201),)


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


def test_next_action_skips_a_legacy_childless_container() -> None:
    body = "## Now\nStill going.\n\n## Next\nDo the thing.\n"
    container = replace(
        board_issue(210, "Legacy container", body),
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    projected = projected_board(
        (container,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert board.next_action(projected) is None
    item = next(item for item in projected.items if item.number == 210)
    assert item.actionable_reason == "body legacy"


def test_next_action_skips_a_malformed_childless_container() -> None:
    body = agent_claim_body('version = 2\nnow = "N"\nnext = "X"\ndone_when = "D"\n')
    container = replace(
        board_issue(211, "Malformed container", body),
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    projected = projected_board(
        (container,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert board.next_action(projected) is None
    item = next(item for item in projected.items if item.number == 211)
    assert item.actionable_reason == "body malformed: version: version must be exactly 1"


def test_render_shows_projection_presence_and_dash_next_for_a_valid_block_skeleton() -> None:
    skeleton = 'version = 1\nnow = ""\nnext = ""\ndone_when = ""\n'
    issue = board_issue(220, "Skeleton", agent_claim_body(skeleton))
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    line = next(line for line in board.render(projected).splitlines() if f"#{issue.number}" in line)
    cells = re.split(r"\s{2,}", line.strip())

    assert cells[5] == "Now, Next, Done when"
    assert cells[7] == "-"


def test_a_complete_block_item_is_body_complete_with_no_dependency_projection() -> None:
    issue = board_issue(230, "Complete block item", agent_claim_body(MINIMAL_BLOCK_TOML))
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    item = next(item for item in projected.items if item.number == 230)

    assert item.contract_complete is True
    assert item.open_blockers == ()


def test_board_reports_open_local_and_foreign_dependencies_as_blockers() -> None:
    issue = board_issue(300, "Depends on two", agent_claim_body(MINIMAL_BLOCK_TOML))
    dependencies = {
        300: (
            block_dependency(3),
            block_dependency(7, repository="overnightworks/other-repo"),
        )
    }
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        dependencies=dependencies,
    )

    item = next(item for item in projected.items if item.number == 300)

    assert item.open_blockers == (
        board.IssueReference(REPOSITORY, 3),
        board.IssueReference("overnightworks/other-repo", 7),
    )
    assert item.actionable_reason == "blocked by #3, overnightworks/other-repo#7"


def test_board_json_splits_local_and_foreign_blockers_only_in_block_mode() -> None:
    """A2 (#150): `open_blockers` keeps its pre-#150 local-int-only shape;
    `foreign_blockers` is a separate key, present only under the block pin."""
    issue = board_issue(300, "Depends on two", agent_claim_body(MINIMAL_BLOCK_TOML))
    dependencies = {
        300: (
            block_dependency(3),
            block_dependency(7, repository="overnightworks/other-repo"),
        )
    }
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        dependencies=dependencies,
    )

    payload = json.loads(board.board_json(projected))
    item = next(item for item in payload["items"] if item["number"] == 300)

    assert item["open_blockers"] == [3]
    assert item["foreign_blockers"] == ["overnightworks/other-repo#7"]


def test_board_json_carries_an_empty_foreign_blockers_list_without_a_foreign_dependency() -> None:
    issue, blocked_by = blocked_issue(10, "Local only", block_dependency(642))
    other = board_issue(642, "Blocker", complete_contract("Ship it."))
    projected = projected_board(
        (issue, other),
        (),
        (),
        (),
        board.BoardConfig(),
        dependencies=blocked_by,
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    payload = json.loads(board.board_json(projected))
    item = next(item for item in payload["items"] if item["number"] == 10)

    assert item["open_blockers"] == [642]
    assert item["foreign_blockers"] == []


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
        issue_claim, "_request", lambda _arguments: request(issue=301, scope=("src/work.py",))
    )

    exit_code = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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


def test_board_never_frees_on_a_closed_foreign_dependency_alone() -> None:
    issue = board_issue(302, "Foreign-only", agent_claim_body(MINIMAL_BLOCK_TOML))
    dependencies = {
        302: (
            block_dependency(
                9,
                repository="overnightworks/other-repo",
                state=board.BlockerState.CLOSED,
                closed_at=datetime(2026, 8, 20, tzinfo=UTC),
            ),
        )
    }
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        dependencies=dependencies,
    )

    item = next(item for item in projected.items if item.number == 302)

    assert item.freed_on is None
    assert item.open_blockers == ()


def test_board_treats_a_same_repository_pull_request_dependency_like_any_other() -> None:
    """Block mode has no `blocker-is-a-PR` check (prose-only): a same-
    repository PR dependency follows its own open/closed state."""
    issue = board_issue(303, "PR blocker", agent_claim_body(MINIMAL_BLOCK_TOML))
    dependencies = {303: (block_dependency(88, is_pull_request=True),)}
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        dependencies=dependencies,
    )

    item = next(item for item in projected.items if item.number == 303)

    assert item.open_blockers == (board.IssueReference(REPOSITORY, 88),)


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

    error_check = issue_claim._blocked_check(item, None, REPOSITORY)
    assert error_check == issue_claim.SliceCheck(
        "error",
        "blocked",
        "#304 is blocked by overnightworks/other-repo#9 (open); "
        "pass --out-of-order REASON to claim it anyway",
        issue=304,
    )
    warning_check = issue_claim._blocked_check(item, "reason", REPOSITORY)
    assert warning_check is not None
    assert warning_check.level == "warning"


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

    assert issue_claim.main(["--repo", "example/agent-claim", "next"]) == 0
    assert capsys.readouterr().out == (
        "#10 score -20: Operator idea\nNext: Problem neu prüfen und Item verfeinern\n"
        "Run: aco claim 10 --scope <paths>\n"
        "<paths> cannot be derived; take the files to claim from the item body.\n"
    )

    assert issue_claim.main(["--repo", "example/agent-claim", "next", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "action": "work_item",
        "number": 10,
        "score": -20,
        "title": "Operator idea",
        "next": "Problem neu prüfen und Item verfeinern",
        "command": "aco claim 10 --scope <paths>",
        "ruling_landings": None,
        "ruling_old": None,
        "recovery": [],
        "skipped": [],
    }


def test_next_keeps_an_unlabelled_projectionless_item_skipped_with_an_active_idea_label(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (tmp_path / ".agent-claim").mkdir()
    (tmp_path / ".agent-claim" / "board.toml").write_text('idea_label = "idea"\n')
    incomplete = board_issue(10, "Incomplete work", idea_body("Investigate."))
    _configured_board_client(monkeypatch, tmp_path, open_issues=(incomplete,))

    assert issue_claim.main(["--repo", "example/agent-claim", "next"]) == 3
    assert capsys.readouterr().out == (
        "No actionable item.\n\nSKIPPED\n#10: body incomplete: Now, Next, Done when\n"
    )

    assert issue_claim.main(["--repo", "example/agent-claim", "next", "--json"]) == 3
    assert json.loads(capsys.readouterr().out) == {
        "action": None,
        "recovery": [],
        "skipped": [{"number": 10, "reason": "body incomplete: Now, Next, Done when"}],
    }


def test_next_keeps_a_vision_labelled_projectionless_item_incomplete_without_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    idea = board_issue(10, "Operator vision", idea_body("Investigate."), labels=("vision",))
    _configured_board_client(monkeypatch, tmp_path, open_issues=(idea,))

    assert issue_claim.main(["--repo", "example/agent-claim", "next"]) == 3
    assert capsys.readouterr().out == (
        "No actionable item.\n\nSKIPPED\n#10: body incomplete: Now, Next, Done when\n"
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

    assert issue_claim.main(["--repo", "example/agent-claim", "next"]) == 0
    assert capsys.readouterr().out == (
        "#10 score -10: Refined idea\nNext: Build the chosen direction.\n"
        "Run: aco claim 10 --scope <paths>\n"
        "<paths> cannot be derived; take the files to claim from the item body.\n"
    )


@pytest.mark.parametrize(
    ("frozen_until", "claims", "dependencies", "expected_reason"),
    [
        pytest.param(FROZEN_UNTIL, (), (), f"frozen: {FROZEN_TRIGGER}", id="frozen"),
        pytest.param(None, (request(issue=10, agent="Grok 4.6"),), (), "claimed", id="claimed"),
        pytest.param(None, (), (block_dependency(9),), "blocked by #9", id="blocked"),
    ],
)
def test_a_configured_idea_keeps_freeze_claim_and_blocker_reasons(
    frozen_until: dict[str, object] | None,
    claims: tuple[ClaimRequest, ...],
    dependencies: tuple[board.IssueDependency, ...],
    expected_reason: str,
) -> None:
    wish = "## Wunsch\nMake the board clearer.\n\n"
    block_entries: dict[str, object] = (
        {} if frozen_until is None else {"frozen_until": frozen_until}
    )
    idea = board_issue(
        10,
        "Operator idea",
        wish + complete_contract("", now="", done_when="", **block_entries),
        labels=("idea",),
        blocked_by_count=len(dependencies),
    )
    blocker = board_issue(9, "Open blocker", complete_contract("Resolve the blocker."))
    projected = projected_board(
        (blocker, idea),
        (),
        (),
        tuple(_store_claim_from_request(claim_request) for claim_request in claims),
        board.BoardConfig(idea_label="idea"),
        dependencies={idea.number: dependencies},
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )
    item = next(item for item in projected.items if item.number == idea.number)

    assert item not in projected.ready_now
    assert item.actionable_reason == expected_reason


def test_an_idea_without_a_priority_label_follows_the_regular_score_order() -> None:
    regular_work = board_issue(10, "Regular work", complete_contract("Ship the change."))
    idea = board_issue(11, "Operator idea", idea_body("Make the board clearer."), labels=("idea",))

    projected = projected_board(
        (idea, regular_work),
        (),
        (),
        (),
        board.BoardConfig(idea_label="idea"),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert [item.number for item in projected.items] == [regular_work.number, idea.number]
    assert [item.priority_bucket for item in projected.items] == ["unlabelled", "unlabelled"]
    assert [item.score for item in projected.items] == [-10, -20]
    assert board.highest_scored_actionable(projected) == projected.items[0]


def test_claim_treats_a_higher_ranked_configured_idea_as_out_of_order(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (tmp_path / ".agent-claim").mkdir()
    (tmp_path / ".agent-claim" / "board.toml").write_text('idea_label = "vision"\n')
    lower = board_issue(10, "Lower work", complete_contract("Claim #10."))
    idea = board_issue(
        11,
        "Higher-ranked vision",
        idea_body("Improve claims."),
        labels=("vision", "security"),
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(lower, idea))
    monkeypatch.setattr(
        issue_claim, "_request", lambda _arguments: request(issue=10, scope=("src/lower.py",))
    )

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
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


def test_outbound_resource_name_refuses_a_value_that_is_not_a_resource_name() -> None:
    with pytest.raises(ClaimError, match="resource is not a resource name"):
        protocol._outbound_resource_name("not a valid name!")


def test_merged_release_reason_names_the_pull_request() -> None:
    assert protocol.MergedRelease(12).reason == "merged #12"


@pytest.mark.parametrize(
    ("add", "drop", "match"),
    [
        pytest.param(
            ("new.py",), ("missing.py",), "cannot drop 'missing.py'", id="drop-not-present"
        ),
        pytest.param((), ("src",), "rescope must leave a non-empty scope", id="empty-after-drop"),
    ],
)
def test_combined_scope_refuses_an_invalid_rescope(
    add: tuple[str, ...], drop: tuple[str, ...], match: str
) -> None:
    with pytest.raises(ClaimUnavailableError, match=match):
        protocol._combined_scope(("src",), add, drop)


def test_outbound_text_refuses_a_non_string_field() -> None:
    with pytest.raises(ClaimError, match="agent must be text"):
        protocol._outbound_text(123, "agent", maximum=128)


@pytest.mark.parametrize(
    "invalid",
    ["Codex\nSol", "Codex\x1fSol", " ", "x" * 129],
)
def test_outbound_text_rejects_controlled_or_overlong_fields(invalid: str) -> None:
    """`_outbound_text` is the one owner of this validation since issue #176
    moved it out of the deleted ledger comment writers (`claim_comment` /
    `release_comment` / `supersede_comment`) and onto every real call site
    that builds an intent: `cli._request`'s agent/role, and `release`'s
    abandoned reason."""
    with pytest.raises(ClaimError, match="agent must be one bounded non-empty line"):
        protocol._outbound_text(invalid, "agent", maximum=128)


@pytest.mark.parametrize(
    ("branch", "match"),
    [
        pytest.param(5, "must be text", id="not-text"),
        pytest.param(" codex/issue-72", "must be one bounded non-empty line", id="padded"),
        pytest.param("", "must be one bounded non-empty line", id="empty"),
        pytest.param("codex\x1f/issue-72", "must be one bounded non-empty line", id="control"),
        pytest.param("-codex/issue-72", "not a safe Git ref", id="leading-dash"),
        pytest.param("codex/../issue-72", "not a safe Git ref", id="dot-dot"),
        pytest.param("codex//issue-72", "not a safe Git ref", id="double-slash"),
        pytest.param("codex/issue-72@{1}", "not a safe Git ref", id="reflog-syntax"),
        pytest.param("codex/issue-72.lock", "not a safe Git ref", id="lock-suffix"),
        pytest.param("codex/.hidden", "not a safe Git ref", id="dot-segment"),
    ],
)
def test_claim_branch_must_be_a_safe_git_ref(branch: object, match: str) -> None:
    """`_valid_branch` guards every branch that reaches a claim -- `cli._request`
    is its live caller, taking the value from `--branch` or the checked-out
    branch name, neither of which this repository controls."""
    with pytest.raises(InvalidClaimMarkerError, match=match):
        protocol._valid_branch({"branch": branch})


@pytest.mark.parametrize(
    ("scope", "match"),
    [
        pytest.param("src", "must be a non-empty list", id="not-a-list"),
        pytest.param([], "must be a non-empty list", id="empty-list"),
        pytest.param([5], "scope entries must be text", id="entry-not-text"),
        pytest.param([" src"], "canonical bounded paths", id="padded-entry"),
        pytest.param(
            [f"src/file{index}.py" for index in range(protocol.MAX_SCOPE_ENTRIES + 1)],
            "exceeds 256 entries",
            id="too-many-entries",
        ),
        pytest.param(["src\\widget.py"], "canonical bounded paths", id="backslash"),
        pytest.param(["src/\x1fwidget.py"], "canonical bounded paths", id="control-character"),
        pytest.param(["x" * (protocol.MAX_SCOPE_PATH_LENGTH + 1)], "canonical", id="overlong"),
        pytest.param(["/etc/passwd"], "must be repository-relative", id="absolute"),
        pytest.param(["../outside.py"], "must be repository-relative", id="escapes-upwards"),
        pytest.param(["~/secrets"], "must be repository-relative", id="home-relative"),
        pytest.param([".git/config"], "must be repository-relative", id="git-directory"),
        pytest.param(["./src"], "must be repository-relative", id="not-normalized"),
        pytest.param(["src", "src"], "duplicate paths", id="duplicate"),
    ],
)
def test_claim_scope_must_be_canonical_repository_relative_paths(scope: object, match: str) -> None:
    """`_valid_scope` guards every path that reaches a claim -- `cli._request`,
    `cli._rescope`'s add/drop, the `--path` lookup, and `claims_holding_path`
    all hand it operator-supplied text."""
    with pytest.raises(InvalidClaimMarkerError, match=match):
        protocol._valid_scope(scope)


def test_scope_overlap_is_repository_wide_and_path_aware() -> None:
    left = request(issue=71, scope=("frontend/src",))
    nested = request("claim-b", issue=72, scope=("frontend/src/lib/player.ts",))
    sibling = request("claim-c", issue=73, scope=("frontend/tests",))

    assert not claims_conflict(left, nested)
    assert protocol.claims_overlap(left, nested)
    assert not claims_conflict(left, sibling)
    assert not protocol.claims_overlap(left, sibling)


@pytest.mark.parametrize(
    ("right", "expected"),
    [
        pytest.param(
            request("claim-b", lane=True, branch="docs/lane-a", scope=("other",)),
            True,
            id="same-lane-disjoint-scope-still-conflicts",
        ),
        pytest.param(
            request("claim-b", lane=True, branch="docs/lane-b", scope=("shared/file.py",)),
            False,
            id="different-lanes-overlapping-scope-is-not-a-conflict",
        ),
        pytest.param(
            request("claim-b", lane=True, branch="docs/lane-b", scope=("other",)),
            False,
            id="different-lanes-disjoint-scope-no-conflict",
        ),
        pytest.param(
            request("claim-b", issue=72, scope=("shared/file.py",)),
            False,
            id="lane-and-issue-overlapping-scope-is-not-a-conflict",
        ),
        pytest.param(
            request("claim-b", issue=72, scope=("other",)),
            False,
            id="lane-and-issue-disjoint-scope-no-conflict",
        ),
    ],
)
def test_lane_and_issue_conflict_matrix(right: ClaimRequest, expected: bool) -> None:
    left = request(lane=True, branch="docs/lane-a", scope=("shared",))
    assert claims_conflict(left, right) == expected


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

    assert _status(claims, None, ages) == 0
    assert capsys.readouterr().out.count("CLAIMED") == 50
    assert _status(claims, 100, ages) == 0
    assert capsys.readouterr().out.count("CLAIMED") == 1


def test_status_reports_repository_scope_overlaps_as_notes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = _active_claim(issue=72, scope=("shared",))
    second = _active_claim(claim_id="claim-b", issue=73, scope=("shared/file.py",))
    opened_at = datetime(2026, 8, 21, tzinfo=UTC)
    ages: dict[str, datetime] = {first.claim_id: opened_at, second.claim_id: opened_at}

    exit_code = _status((first, second), None, ages)

    assert exit_code == 0
    rendered = capsys.readouterr().out
    assert rendered.count("CLAIMED") == 2
    assert "CONFLICT" not in rendered
    assert "overlaps issue #73 (claim-b)" in rendered
    assert "overlaps issue #72 (cli-claim)" in rendered
    assert _status((first, second), 72, ages) == 0
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

    assert _status((descendant, parent), None, ages) == 0
    rendered = capsys.readouterr().out
    assert rendered.count("CLAIMED") == 2
    assert "CONFLICT" not in rendered


def test_github_adapter_runs_gh_when_no_fake_run_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every other adapter test injects `run=` to avoid a real subprocess;
    this proves the adapter's own default (`_gh`, via `_bounded_command`)
    actually builds a `gh` command and runs it through the real bounded-I/O
    machinery -- substituting the child process itself so the test needs no
    real `gh` executable."""
    observed: list[list[str]] = []
    original_popen = subprocess.Popen

    def start(
        command: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        observed.append(command)
        substituted = [sys.executable, "-c", "print('main')"]
        return original_popen(substituted, stdin=stdin, stdout=stdout, stderr=stderr, env=env)

    monkeypatch.setattr(subprocess, "Popen", start)
    client = GitHubForge(github._repository_id("example/agent-claim"))

    assert client.default_branch() == "main"
    assert observed == [["gh", "api", "repos/example/agent-claim", "--jq", ".default_branch"]]


def test_github_adapter_capability_reads_the_declared_table() -> None:
    client = GitHubForge(github._repository_id("example/agent-claim"))

    assert client.capability(forge.ForgeOperation.ITEM_REFERENCE) is forge.Capability.READ_ONLY
    assert client.capability(forge.ForgeOperation.CREATE_CHILD) is forge.Capability.READ_WRITE


def test_github_adapter_item_reference_reads_state_title_and_body() -> None:
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda _arguments: json.dumps(
            {"state": "open", "title": "Work", "body": "Do it.", "is_landing": False}
        ),
    )

    assert client.item_reference(10) == forge.ItemReference(forge.ItemState.OPEN, "Work", "Do it.")


def test_github_adapter_item_reference_reports_a_pull_request_as_a_landing() -> None:
    """The one read `check` distributes on: GitHub answers for a pull request
    at the issues endpoint too, and only this flag tells the two apart."""
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda _arguments: json.dumps(
            {"state": "open", "title": "Land it", "body": "Work-Item: #7", "is_landing": True}
        ),
    )

    assert client.item_reference(12) == forge.ItemReference(
        forge.ItemState.OPEN, "Land it", "Work-Item: #7", True
    )


def test_github_adapter_item_reference_reads_a_closed_issue_with_no_body() -> None:
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda _arguments: json.dumps(
            {"state": "closed", "title": "Work", "body": None, "is_landing": False}
        ),
    )

    assert client.item_reference(10) == forge.ItemReference(forge.ItemState.CLOSED, "Work", "")


def test_github_adapter_item_reference_is_missing_after_a_404() -> None:
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda _arguments: (_ for _ in ()).throw(
            forge.ForgeNotFoundError("GitHub API failed: HTTP 404")
        ),
    )

    assert client.item_reference(10) == forge.ItemReference(forge.ItemState.MISSING)


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        pytest.param("not-json", "invalid issue reference JSON", id="not-json"),
        pytest.param(json.dumps([]), "malformed issue reference", id="no-values"),
        pytest.param(
            json.dumps([{"a": 1}, {"b": 2}]), "malformed issue reference", id="two-values"
        ),
        pytest.param(json.dumps(["not-a-dict"]), "malformed issue reference", id="not-a-dict"),
        pytest.param(
            json.dumps({"state": "unknown", "title": "x", "body": None}),
            "malformed issue reference",
            id="unknown-state",
        ),
        pytest.param(
            json.dumps({"state": "open", "title": 5, "body": None}),
            "malformed issue reference",
            id="title-not-text",
        ),
        pytest.param(
            json.dumps({"state": "open", "title": "x", "body": 5}),
            "malformed issue reference",
            id="body-not-text",
        ),
    ],
)
def test_github_adapter_item_reference_fails_loud_on_a_malformed_response(
    raw: str, match: str
) -> None:
    client = GitHubForge(github._repository_id("example/agent-claim"), run=lambda _arguments: raw)

    with pytest.raises(ClaimError, match=match):
        client.item_reference(10)


def test_github_reads_board_dependencies_local_and_foreign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[list[str]] = []

    def run(arguments: list[str]) -> str:
        observed.append(arguments)
        rows = [
            {
                "number": 151,
                "state": "closed",
                "closedAt": "2026-09-05T00:00:00Z",
                "repository": "example/agent-claim",
                "isPullRequest": False,
            },
            {
                "number": 7,
                "state": "open",
                "closedAt": None,
                "repository": "overnightworks/other-repo",
                "isPullRequest": False,
            },
        ]
        return "\n".join(json.dumps(row) for row in rows)

    client = GitHubForge(github._repository_id("example/agent-claim"), run=run)

    assert client.list_board_dependencies(150) == (
        board.IssueDependency(
            board.IssueReference("example/agent-claim", 151),
            board.BlockerState.CLOSED,
            False,
            datetime(2026, 9, 5, tzinfo=UTC),
        ),
        board.IssueDependency(
            board.IssueReference("overnightworks/other-repo", 7),
            board.BlockerState.OPEN,
            False,
            None,
        ),
    )
    assert observed == [
        [
            "api",
            "--paginate",
            "repos/example/agent-claim/issues/150/dependencies/blocked_by?per_page=100",
            "--jq",
            ".[] | {number,state,closedAt:.closed_at,repository:.repository.full_name,"
            'isPullRequest:has("pull_request")}',
        ]
    ]


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(json.dumps(["not-a-dict"]), id="not-a-dict"),
        pytest.param(
            json.dumps(
                {
                    "number": 151,
                    "state": "open",
                    "closedAt": None,
                    "repository": "not a repo",
                    "isPullRequest": False,
                }
            ),
            id="malformed-repository",
        ),
        pytest.param(
            json.dumps(
                {
                    "number": 151,
                    "state": "closed",
                    "closedAt": None,
                    "repository": "example/agent-claim",
                    "isPullRequest": False,
                }
            ),
            id="closed-without-timestamp",
        ),
    ],
)
def test_github_board_dependency_fails_loud_on_a_malformed_shape(raw: str) -> None:
    client = GitHubForge(github._repository_id("example/agent-claim"), run=lambda _arguments: raw)

    with pytest.raises(ClaimError, match="malformed board blocked-by dependency"):
        client.list_board_dependencies(150)


def test_github_board_dependency_fails_loud_on_an_uncalendared_closed_timestamp() -> None:
    """`closedAt` can pass the timestamp-shape check (digits in the right
    places) while still naming no real calendar date; `datetime.fromisoformat`
    itself is the second, calendar-aware check that catches that."""
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda _arguments: json.dumps(
            {
                "number": 151,
                "state": "closed",
                "closedAt": "9999-99-99T00:00:00Z",
                "repository": "example/agent-claim",
                "isPullRequest": False,
            }
        ),
    )

    with pytest.raises(ClaimError, match="malformed board blocked-by dependency"):
        client.list_board_dependencies(150)


def test_bounded_command_sets_github_quiet_environment() -> None:
    observed = github._bounded_command(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['NO_COLOR']); print(os.environ['GH_NO_UPDATE_NOTIFIER'])",
        ],
        purpose="env probe",
    )

    assert observed.splitlines() == ["1", "1"]


def _unreachable_remote_url() -> str:
    pytest.fail("gh answered; the git remote fallback must not run")


def test_repository_resolution_uses_github_quiet_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed["command"] = command
        observed["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(command, 0, b"\x1b[32mowner/repository\x1b[0m\n", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    resolved = github.discover_repository(None, remote_url=_unreachable_remote_url)

    assert resolved == forge.RepositoryId(github.GITHUB_HOST, ("owner",), "repository")
    command = observed["command"]
    assert isinstance(command, list)
    assert command[0] == "gh"
    env = observed["env"]
    assert isinstance(env, dict)
    assert env["NO_COLOR"] == "1"
    assert env["GH_NO_UPDATE_NOTIFIER"] == "1"


def test_origin_remote_url_reads_the_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkout, "remote_url", _LIVE_REMOTE_URL)
    calls: list[list[str]] = []

    def git(arguments: list[str]) -> str:
        calls.append(arguments)
        return "git@github.com:owner/repository.git"

    monkeypatch.setattr(checkout, "_git_output", git)

    assert checkout.origin_remote_url() == "git@github.com:owner/repository.git"
    assert calls == [["config", "--get", "remote.origin.url"]]


def test_remote_url_reads_any_named_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkout, "remote_url", _LIVE_REMOTE_URL)
    calls: list[list[str]] = []

    def git(arguments: list[str]) -> str:
        calls.append(arguments)
        return "git@github.com:owner/repository.git"

    monkeypatch.setattr(checkout, "_git_output", git)

    assert checkout.remote_url("upstream") == "git@github.com:owner/repository.git"
    assert calls == [["config", "--get", "remote.upstream.url"]]


def test_canonical_remote_repository_parses_the_configured_remote_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkout, "remote_url", lambda remote: "git@github.com:owner/repo.git")

    repository = issue_claim._canonical_remote_repository("origin")

    assert repository.path == "owner/repo"


def test_canonical_remote_repository_refuses_a_non_github_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkout, "remote_url", lambda remote: "https://example.com/owner/repo")

    with pytest.raises(ClaimError, match="does not name a GitHub repository"):
        issue_claim._canonical_remote_repository("origin")


def test_refuse_canonical_remote_mismatch_allows_a_matching_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkout, "remote_url", lambda remote: "git@github.com:owner/repo.git")

    issue_claim._refuse_canonical_remote_mismatch(
        forge.RepositoryId(github.GITHUB_HOST, ("owner",), "repo"), "origin"
    )


def test_refuse_canonical_remote_mismatch_names_both_repositories(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(checkout, "remote_url", lambda remote: "git@github.com:owner/repo.git")
    mismatched = forge.RepositoryId(github.GITHUB_HOST, ("other",), "repo")

    with pytest.raises(
        ClaimUnavailableError,
        match="forge target other/repo does not match canonical remote owner/repo",
    ):
        issue_claim._refuse_canonical_remote_mismatch(mismatched, "origin")


def test_merged_pull_request_history_warns_when_it_reaches_the_result_cap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A full result page for a day's shard means more merged pull requests
    could exist that day beyond the cap, so the board must say that day's
    history may be incomplete rather than silently compute stages from a
    truncated query. `since` and "now" are pinned to the same day so the
    fetch is exactly one shard, matching the fixture below.
    """
    since = datetime(2026, 8, 1, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return since

    monkeypatch.setattr(github, "datetime", FixedDateTime)
    saturated_rows = [
        {
            "number": index,
            "title": f"Fixes #{index}",
            "body": "",
            "headRefName": f"codex/issue-{index}",
            "mergedAt": "2026-08-01T00:00:00Z",
        }
        for index in range(1, github.MAX_RECENT_MERGED_PULL_REQUESTS + 1)
    ]
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda arguments: "\n".join(json.dumps(row) for row in saturated_rows),
    )

    pull_requests = client.list_recent_merged_board_pull_requests(since)

    assert len(pull_requests) == github.MAX_RECENT_MERGED_PULL_REQUESTS
    error = capsys.readouterr().err
    assert "WARNING" in error
    assert str(github.MAX_RECENT_MERGED_PULL_REQUESTS) in error
    assert since.date().isoformat() in error


def test_merged_pull_request_history_below_the_cap_warns_of_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    since = datetime(2026, 8, 1, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return since

    monkeypatch.setattr(github, "datetime", FixedDateTime)
    row = {
        "number": 1,
        "title": "Fixes #1",
        "body": "",
        "headRefName": "codex/issue-1",
        "mergedAt": "2026-08-01T00:00:00Z",
    }
    client = GitHubForge(
        github._repository_id("example/agent-claim"), run=lambda arguments: json.dumps(row)
    )

    client.list_recent_merged_board_pull_requests(since)

    assert capsys.readouterr().err == ""


def test_recent_merged_pull_requests_skips_an_entry_with_no_merge_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mergedAt` can be `None` on an otherwise well-shaped merged-search row
    (a defensive read, not a documented GitHub behavior); such a row cannot
    be dated against the window, so it is skipped rather than crashing the
    whole fetch."""
    since = datetime(2026, 8, 1, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return since

    monkeypatch.setattr(github, "datetime", FixedDateTime)
    row = {
        "number": 1,
        "title": "Fixes #1",
        "body": "",
        "headRefName": "codex/issue-1",
        "mergedAt": None,
    }
    client = GitHubForge(
        github._repository_id("example/agent-claim"), run=lambda arguments: json.dumps(row)
    )

    assert client.list_recent_merged_board_pull_requests(since) == ()


def test_recent_merged_pull_requests_fails_loud_on_an_uncalendared_merge_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    since = datetime(2026, 8, 1, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return since

    monkeypatch.setattr(github, "datetime", FixedDateTime)
    row = {
        "number": 1,
        "title": "Fixes #1",
        "body": "",
        "headRefName": "codex/issue-1",
        "mergedAt": "9999-99-99T00:00:00Z",
    }
    client = GitHubForge(
        github._repository_id("example/agent-claim"), run=lambda arguments: json.dumps(row)
    )

    with pytest.raises(ClaimError, match="malformed merged board pull request"):
        client.list_recent_merged_board_pull_requests(since)


def test_missing_gh_repository_resolution_is_a_controlled_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)

    with pytest.raises(ClaimError, match="gh is required"):
        github.discover_repository(None, remote_url=_unreachable_remote_url)


def test_repository_resolution_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(["gh"], process.DEFAULT_TIMEOUT_SECONDS)

    monkeypatch.setattr(subprocess, "run", timed_out)

    with pytest.raises(ClaimError, match="gh timed out while resolving the repository"):
        github.discover_repository(None, remote_url=_unreachable_remote_url)


def test_repository_resolution_refuses_when_no_remote_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_gh(*arguments, **kwargs):
        return subprocess.CompletedProcess(arguments[0], 1, b"", b"not a gh repo")

    monkeypatch.setattr(subprocess, "run", failed_gh)

    with pytest.raises(ClaimError, match="cannot resolve GitHub repository"):
        github.discover_repository(None, remote_url=lambda: "https://example.com/owner/repo")


def test_discover_repository_requires_owner_slash_repo_shape_for_an_explicit_repo() -> None:
    with pytest.raises(ClaimError, match="repository must be OWNER/REPO"):
        github.discover_repository("not-a-repository-shape", remote_url=lambda: "")


def test_cli_version_exits_before_requiring_a_command(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        issue_claim.main(["--version"])

    assert exited.value.code == 0
    assert capsys.readouterr().out == f"aco {__version__}\n"


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/owner/repository.git",
        "git@github.com:owner/repository.git",
    ],
)
def test_repository_falls_back_to_standard_github_remote(
    monkeypatch: pytest.MonkeyPatch, remote: str
) -> None:
    calls: list[list[str]] = []

    def failed_gh(*arguments, **kwargs):
        command = arguments[0]
        calls.append(command)
        return subprocess.CompletedProcess(command, 1, b"", b"not a gh repo")

    monkeypatch.setattr(subprocess, "run", failed_gh)

    resolved = github.discover_repository(None, remote_url=lambda: remote)

    assert resolved == forge.RepositoryId(github.GITHUB_HOST, ("owner",), "repository")
    assert calls == [["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"]]


def test_bounded_command_stops_before_unbounded_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process, "MAX_COMMAND_OUTPUT_BYTES", 32)

    with pytest.raises(ClaimError, match="output limit"):
        github._bounded_command(
            [sys.executable, "-c", "print('x' * 1000)"],
            purpose="test command",
        )


def test_bounded_command_disables_github_update_notifications() -> None:
    observed = github._bounded_command(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['GH_NO_UPDATE_NOTIFIER'])",
        ],
        purpose="update notifier probe",
    )

    assert observed == "1"


def test_bounded_command_streams_stdin_without_putting_it_in_argv() -> None:
    observed = github._bounded_command(
        [sys.executable, "-c", "import sys; print(sys.stdin.buffer.read().decode())"],
        purpose="stdin probe",
        input_data=b"bounded body",
    )

    assert observed == "bounded body"


def test_bounded_command_wraps_process_argument_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cannot_start(*args, **kwargs):
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr(subprocess, "Popen", cannot_start)

    with pytest.raises(ClaimError, match="cannot start test command"):
        github._bounded_command(["gh", "issue"], purpose="test command")


def test_bounded_command_wraps_stdin_write_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cannot_write(*args, **kwargs):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(github.os, "write", cannot_write)

    with pytest.raises(ClaimError, match="failed while sending bounded input"):
        github._bounded_command(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
            purpose="stdin write probe",
            input_data=b"body",
        )


def test_bounded_command_reaps_child_when_selector_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, subprocess.Popen[bytes]] = {}
    original_popen = subprocess.Popen

    def start(
        command: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        process = original_popen(command, stdin=stdin, stdout=stdout, stderr=stderr, env=env)
        observed["process"] = process
        return process

    def cannot_select():
        raise OSError(5, "selector failed")

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(process.selectors, "DefaultSelector", cannot_select)

    with pytest.raises(ClaimError, match="failed while coordinating I/O"):
        github._bounded_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            purpose="selector setup probe",
        )

    assert observed["process"].poll() is not None
    assert observed["process"].stdout is not None
    assert observed["process"].stdout.closed


def test_bounded_command_reaps_child_when_select_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, subprocess.Popen[bytes]] = {}
    original_popen = subprocess.Popen

    def start(
        command: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        process = original_popen(command, stdin=stdin, stdout=stdout, stderr=stderr, env=env)
        observed["process"] = process
        return process

    class FailingSelector:
        instance: FailingSelector | None = None

        def __init__(self) -> None:
            self.closed = False
            FailingSelector.instance = self

        def register(self, fileobj, events, data) -> None:
            pass

        def get_map(self):
            return {"stdout": object()}

        def select(self, timeout):
            raise OSError(5, "select failed")

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(process.selectors, "DefaultSelector", FailingSelector)

    with pytest.raises(ClaimError, match="failed while waiting for I/O"):
        github._bounded_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            purpose="select probe",
        )

    spawned = observed["process"]
    assert spawned.poll() is not None
    assert spawned.stdout is not None
    assert spawned.stdout.closed
    assert FailingSelector.instance is not None
    assert FailingSelector.instance.closed


def test_bounded_command_reaps_child_when_output_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, subprocess.Popen[bytes]] = {}
    original_popen = subprocess.Popen
    original_read = github.os.read

    def start(
        command: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        process = original_popen(command, stdin=stdin, stdout=stdout, stderr=stderr, env=env)
        observed["process"] = process
        return process

    def cannot_read(file_descriptor: int, count: int) -> bytes:
        process = observed.get("process")
        if (
            process is not None
            and process.stdout is not None
            and file_descriptor == process.stdout.fileno()
        ):
            raise OSError(5, "read failed")
        return original_read(file_descriptor, count)

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(github.os, "read", cannot_read)

    with pytest.raises(ClaimError, match="failed while reading output"):
        github._bounded_command(
            [
                sys.executable,
                "-u",
                "-c",
                "import time; print('ready'); time.sleep(30)",
            ],
            purpose="read probe",
        )

    assert observed["process"].poll() is not None
    assert observed["process"].stdout is not None
    assert observed["process"].stdout.closed


def test_bounded_command_reaps_child_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, subprocess.Popen[bytes]] = {}
    original_popen = subprocess.Popen

    def start(
        command: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        process = original_popen(command, stdin=stdin, stdout=stdout, stderr=stderr, env=env)
        observed["process"] = process
        return process

    class CancellationSentinel(BaseException):
        pass

    class CancellingSelector:
        def register(self, fileobj, events, data) -> None:
            pass

        def get_map(self):
            return {"stdout": object()}

        def select(self, timeout):
            raise CancellationSentinel

        def close(self) -> None:
            pass

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(process.selectors, "DefaultSelector", CancellingSelector)

    with pytest.raises(CancellationSentinel):
        github._bounded_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            purpose="cancellation probe",
        )

    assert observed["process"].poll() is not None
    assert observed["process"].stdout is not None
    assert observed["process"].stdout.closed


def test_bounded_command_requires_the_named_executable() -> None:
    with pytest.raises(ClaimError) as excinfo:
        github._bounded_command(
            ["missing-claim-command"],
            purpose="missing executable probe",
        )
    assert str(excinfo.value) == "missing-claim-command is required for issue claims"


def test_bounded_command_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, subprocess.Popen[bytes]] = {}
    original_popen = subprocess.Popen

    def start(
        command: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        process = original_popen(command, stdin=stdin, stdout=stdout, stderr=stderr, env=env)
        recorded["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(github, "GH_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(ClaimError) as excinfo:
        github._bounded_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            purpose="timeout probe",
        )
    assert str(excinfo.value) == "timeout probe timed out"
    assert recorded["process"].poll() is not None


class _FakeBoundedProcess:
    """A deterministic `subprocess.Popen`-shaped double: a real closed pipe for
    `stdout` (so the selector has a real, immediately-EOF file descriptor to
    register) plus scripted `poll`/`terminate`/`kill`/`wait`, so
    `process.run_bounded`'s stop-and-reap behavior is proven without spawning a
    child or depending on real OS scheduling.
    """

    def __init__(self, *, already_exited: bool = False, ignores_terminate: bool = False) -> None:
        read_fd, write_fd = os.pipe()
        os.close(write_fd)
        self.stdout = os.fdopen(read_fd, "rb")
        self.stdin = None
        self.stderr = None
        self._ignores_terminate = ignores_terminate
        self.events: list[str] = []
        self._exited = already_exited

    def poll(self) -> int | None:
        return 0 if self._exited else None

    def terminate(self) -> None:
        self.events.append("terminate")
        if not self._ignores_terminate:
            self._exited = True

    def kill(self) -> None:
        self.events.append("kill")
        self._exited = True

    def wait(self, timeout: float = 0.0) -> int:
        if not self._exited:
            raise subprocess.TimeoutExpired(cmd="fake", timeout=timeout)
        return 0


def test_run_bounded_times_out_even_when_the_child_already_exited(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deadline already passed by the time I/O is awaited must fail loud even
    when the child happened to finish first -- stopping an already-exited
    process is then a safe no-op, never a second signal or a raised error."""
    fake_process = _FakeBoundedProcess(already_exited=True)
    calls = {"n": 0}

    def already_past_deadline() -> float:
        calls["n"] += 1
        return 0.0 if calls["n"] == 1 else 1_000_000.0

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: fake_process)
    monkeypatch.setattr(process.time, "monotonic", already_past_deadline)

    with pytest.raises(process.ProcessTimedOutError):
        process.run_bounded(["fake"], timeout=5.0)

    assert fake_process.events == []
    assert fake_process.stdout.closed is True
    assert fake_process.poll() is not None


def test_run_bounded_kills_a_child_that_ignores_termination(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A child that survives `terminate()` (ignoring the request to stop) must
    be `kill()`ed -- proven with a deterministic process fake controlling
    `poll`/`terminate`/`kill`/`wait`, not a real child ignoring a real SIGTERM."""
    fake_process = _FakeBoundedProcess(ignores_terminate=True)

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: fake_process)
    monkeypatch.setattr(process.time, "monotonic", lambda: 0.0)

    with pytest.raises(process.ProcessTimedOutError):
        process.run_bounded(["fake"], timeout=-1.0)

    assert fake_process.events == ["terminate", "kill"]
    assert fake_process.stdout.closed is True
    assert fake_process.poll() is not None


def test_run_bounded_treats_a_broken_input_pipe_as_fully_sent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stdin write that raises `BrokenPipeError` -- the child closed its read
    end without consuming the input -- must not fail the whole exchange: the
    write is treated as fully sent and output collection continues."""

    def broken_write(_file_descriptor: int, data: bytes) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    monkeypatch.setattr(process.os, "write", broken_write)

    observed = process.run_bounded(
        [sys.executable, "-c", "print('done')"],
        input_data=b"unread input",
    )
    assert observed.output == b"done\n"


def test_run_bounded_reaps_the_child_when_the_selector_fails_to_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A selector that cannot close itself on the way out must not mask the
    command's own result -- reaping swallows that failure."""
    real_selector_class = process.selectors.DefaultSelector

    class CloseFailingSelector:
        def __init__(self) -> None:
            self._inner = real_selector_class()

        def register(self, fileobj, events, data=None):
            return self._inner.register(fileobj, events, data)

        def unregister(self, fileobj):
            return self._inner.unregister(fileobj)

        def get_map(self):
            return self._inner.get_map()

        def select(self, timeout=None):
            return self._inner.select(timeout)

        def close(self) -> None:
            raise OSError(5, "close failed")

    monkeypatch.setattr(process.selectors, "DefaultSelector", CloseFailingSelector)

    observed = process.run_bounded([sys.executable, "-c", "print('ok')"])
    assert observed.output == b"ok\n"


def test_bounded_command_stops_a_child_that_hangs_after_closing_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, subprocess.Popen[bytes]] = {}
    original_popen = subprocess.Popen

    def start(
        command: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        process = original_popen(command, stdin=stdin, stdout=stdout, stderr=stderr, env=env)
        recorded["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", start)
    with pytest.raises(ClaimError) as excinfo:
        github._bounded_command(
            [
                sys.executable,
                "-c",
                (
                    "import os, sys, time\n"
                    "for stream in (sys.stdout, sys.stderr):\n"
                    "    try:\n"
                    "        stream.close()\n"
                    "    except OSError:\n"
                    "        pass\n"
                    "for fd in (1, 2):\n"
                    "    try:\n"
                    "        os.close(fd)\n"
                    "    except OSError:\n"
                    "        pass\n"
                    "time.sleep(30)"
                ),
            ],
            purpose="hang probe",
        )
    assert str(excinfo.value) == "hang probe did not exit after closing its output"
    assert recorded["process"].poll() is not None


def test_bounded_command_uses_combined_output_as_nonzero_exit_message() -> None:
    with pytest.raises(ClaimError) as excinfo:
        github._bounded_command(
            [sys.executable, "-c", "raise SystemExit('boom')"],
            purpose="exit probe",
        )
    assert str(excinfo.value) == "boom"


def test_bounded_command_names_the_exit_code_when_nonzero_output_is_empty() -> None:
    with pytest.raises(ClaimError) as excinfo:
        github._bounded_command(
            [sys.executable, "-c", "raise SystemExit(7)"],
            purpose="empty exit probe",
        )
    assert str(excinfo.value) == "empty exit probe failed with exit 7"


def test_bounded_command_rejects_non_utf8_output() -> None:
    with pytest.raises(ClaimError) as excinfo:
        github._bounded_command(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(bytes((255,))); sys.stdout.buffer.flush()",
            ],
            purpose="decode probe",
        )
    assert str(excinfo.value) == "decode probe returned non-UTF-8 output"


@pytest.mark.parametrize(
    ("outcome", "expected_type", "expected_message"),
    [
        pytest.param(
            process.BoundedResult(0, bytes((255,))),
            forge.ForgeMalformedResponseError,
            "GitHub issue coordination returned non-UTF-8 output",
            id="malformed-non-utf8-output",
        ),
        pytest.param(
            process.BoundedResult(1, b"HTTP 404 Not Found"),
            forge.ForgeNotFoundError,
            "HTTP 404 Not Found",
            id="http-404",
        ),
        pytest.param(
            process.BoundedResult(1, b"HTTP 401 Unauthorized"),
            forge.ForgePermissionDeniedError,
            "HTTP 401 Unauthorized",
            id="http-401",
        ),
        pytest.param(
            process.BoundedResult(1, b"HTTP 403 Forbidden"),
            forge.ForgePermissionDeniedError,
            "HTTP 403 Forbidden",
            id="http-403",
        ),
        pytest.param(
            process.BoundedResult(1, b"HTTP 502 Bad Gateway"),
            forge.ForgeTransientError,
            "HTTP 502 Bad Gateway",
            id="http-5xx",
        ),
        pytest.param(
            process.BoundedResult(1, b"connection reset by peer"),
            forge.ForgeTransientError,
            "connection reset by peer",
            id="connection-reset-signal",
        ),
        pytest.param(
            process.BoundedResult(1, b"request timeout"),
            forge.ForgeTransientError,
            "request timeout",
            id="decoded-timeout-signal",
        ),
        pytest.param(
            process.BoundedResult(1, b""),
            forge.ForgeError,
            "GitHub issue coordination failed with exit 1",
            id="unclassified-empty-exit",
        ),
        pytest.param(
            process.BoundedResult(1, b"some other prose"),
            forge.ForgeError,
            "some other prose",
            id="unclassified-nonzero-exit",
        ),
        pytest.param(
            process.ProcessIoFailedError(process.IoStage.WAITING, "boom"),
            forge.ForgeTransientError,
            "GitHub issue coordination failed while waiting for I/O: boom",
            id="io-stage-waiting",
        ),
        pytest.param(
            process.ProcessIoFailedError(process.IoStage.SENDING, "boom"),
            forge.ForgeTransientError,
            "GitHub issue coordination failed while sending bounded input: boom",
            id="io-stage-sending",
        ),
        pytest.param(
            process.ProcessIoFailedError(process.IoStage.READING, "boom"),
            forge.ForgeTransientError,
            "GitHub issue coordination failed while reading output: boom",
            id="io-stage-reading",
        ),
        pytest.param(
            process.ProcessIoFailedError(process.IoStage.COORDINATING, "boom"),
            forge.ForgeTransientError,
            "GitHub issue coordination failed while coordinating I/O: boom",
            id="io-stage-coordinating",
        ),
        pytest.param(
            process.ProcessDidNotExitError(),
            forge.ForgeTransientError,
            "GitHub issue coordination did not exit after closing its output",
            id="did-not-exit",
        ),
        pytest.param(
            process.ProcessTimedOutError(),
            forge.ForgeTransientError,
            "GitHub issue coordination timed out",
            id="process-timed-out",
        ),
        pytest.param(
            process.ProcessOutputTooLargeError(),
            forge.ForgeMalformedResponseError,
            "GitHub issue coordination exceeded its output limit",
            id="output-too-large",
        ),
    ],
)
def test_bounded_command_classifies_every_forge_failure_signal(
    monkeypatch: pytest.MonkeyPatch,
    outcome: process.BoundedResult | process.ProcessError,
    expected_type: type[forge.ForgeError],
    expected_message: str,
) -> None:
    """Adapter-boundary proof, driven through `GitHubForge.default_branch`
    (the real caller of `_gh` -> `_bounded_command`) with `process.run_bounded`
    faked: every nonzero-exit signal (#4.2) and every process failure that
    reaches no forge response (#4.1) becomes the exact typed `ForgeError`
    subclass with the exact message -- not just some `ClaimError`."""

    def fake_run_bounded(*args, **kwargs):
        if isinstance(outcome, process.ProcessError):
            raise outcome
        return outcome

    monkeypatch.setattr(process, "run_bounded", fake_run_bounded)
    client = GitHubForge(github._repository_id("example/agent-claim"))

    with pytest.raises(expected_type) as excinfo:
        client.default_branch()

    assert type(excinfo.value) is expected_type
    assert str(excinfo.value) == expected_message


def test_bounded_command_refuses_a_process_error_type_it_does_not_classify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The isinstance chain the GitHub adapter's failure classifier walks names
    every concrete `process.ProcessError` subclass; a hypothetical new one added
    there without updating this dispatch must fail loud as a defect, not
    silently fall through as some generic `ForgeError` -- proven through
    `GitHubForge.default_branch`, the real caller, with `process.run_bounded`
    faked to raise the unclassified type."""

    class _UnknownProcessError(process.ProcessError):
        pass

    def fake_run_bounded(*args, **kwargs):
        raise _UnknownProcessError

    monkeypatch.setattr(process, "run_bounded", fake_run_bounded)
    client = GitHubForge(github._repository_id("example/agent-claim"))

    with pytest.raises(AssertionError, match="unhandled process failure type"):
        client.default_branch()


def test_scope_directories_detects_a_git_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    def git(arguments: list[str]) -> str:
        if arguments == ["cat-file", "-t", "HEAD:docs"]:
            return "tree"
        if arguments == ["cat-file", "-t", "HEAD:README.md"]:
            return "blob"
        raise ClaimError("not a git object")

    monkeypatch.setattr(checkout, "_git_output", git)

    assert checkout._scope_directories(("docs", "README.md")) == ("docs",)


def test_scope_directories_detects_an_untracked_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "scratch").mkdir()
    (tmp_path / "file.py").write_text("x\n")

    def git(arguments: list[str]) -> str:
        if arguments[:2] == ["cat-file", "-t"]:
            raise ClaimError("not in HEAD")
        if arguments == ["rev-parse", "--show-toplevel"]:
            return str(tmp_path)
        raise ClaimError("unexpected git")

    monkeypatch.setattr(checkout, "_git_output", git)

    assert checkout._scope_directories(("scratch", "file.py")) == ("scratch",)


def test_paths_under_scope_matches_prefix_or_exact_entry() -> None:
    paths = ("LICENSE", "src/a.py", "src/b.py", "docs/a.md")

    assert checkout.paths_under_scope(paths, ("src",)) == ("src/a.py", "src/b.py")
    assert checkout.paths_under_scope(paths, ("LICENSE",)) == ("LICENSE",)
    assert checkout.paths_under_scope(paths, ("src/a.py", "docs")) == ("src/a.py", "docs/a.md")
    assert checkout.paths_under_scope(paths, ("missing",)) == ()


def test_checkout_validation_binds_clean_head_and_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        ("rev-parse", "HEAD"): BASE,
        ("branch", "--show-current"): "codex/issue-71-claims",
        ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        ("status", "--porcelain"): "",
    }
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: values[tuple(arguments)])

    issue_claim._validate_checkout(request())


@pytest.mark.parametrize(
    ("candidate", "values", "message"),
    [
        (
            request(),
            {
                ("rev-parse", "HEAD"): "b" * 40,
                ("branch", "--show-current"): "codex/issue-71-claims",
                ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
                ("rev-parse", "--git-common-dir"): "/repo/.git",
                ("status", "--porcelain"): "",
            },
            "does not match checkout HEAD",
        ),
        (
            request(),
            {
                ("rev-parse", "HEAD"): BASE,
                ("branch", "--show-current"): "other",
                ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
                ("rev-parse", "--git-common-dir"): "/repo/.git",
                ("status", "--porcelain"): "",
            },
            "does not match checkout branch",
        ),
        (
            request(),
            {
                ("rev-parse", "HEAD"): BASE,
                ("branch", "--show-current"): "codex/issue-71-claims",
                ("rev-parse", "--git-dir"): "/repo/.git",
                ("rev-parse", "--git-common-dir"): "/repo/.git",
                ("status", "--porcelain"): "",
            },
            "linked isolated worktree",
        ),
        (
            request(),
            {
                ("rev-parse", "HEAD"): BASE,
                ("branch", "--show-current"): "codex/issue-71-claims",
                ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
                ("rev-parse", "--git-common-dir"): "/repo/.git",
                ("status", "--porcelain"): " M file",
            },
            "before the first worktree edit",
        ),
    ],
)
def test_checkout_validation_rejects_false_or_late_claims(
    monkeypatch: pytest.MonkeyPatch,
    candidate: ClaimRequest,
    values: dict[tuple[str, ...], str],
    message: str,
) -> None:
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: values[tuple(arguments)])

    with pytest.raises(ClaimError, match=message):
        issue_claim._validate_checkout(candidate)


def test_checkout_validation_names_the_base_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    """The base-mismatch refusal names both SHAs (unchanged) and the repair
    an agent reading it needs: omitting `--base` binds it to checkout HEAD."""
    values = {
        ("rev-parse", "HEAD"): "b" * 40,
        ("branch", "--show-current"): "codex/issue-71-claims",
        ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        ("status", "--porcelain"): "",
    }
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: values[tuple(arguments)])
    candidate = request()

    with pytest.raises(ClaimError) as error:
        issue_claim._validate_checkout(candidate)

    assert str(error.value) == (
        f"claim base {BASE} does not match checkout HEAD {'b' * 40}; "
        "omit --base to use checkout HEAD"
    )


def test_checkout_validation_names_the_isolated_worktree_recipe_for_a_trunk_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Claiming from a checkout of `main`/`master` names the exact `git
    worktree add` recipe (#52), not just the rule it violates."""
    values = {("rev-parse", "HEAD"): BASE}
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: values[tuple(arguments)])
    candidate = request(branch="main")

    with pytest.raises(ClaimError) as error:
        issue_claim._validate_checkout(candidate)

    assert str(error.value) == (
        "build claims require an isolated non-main worktree branch; "
        f"run {checkout.ISOLATED_WORKTREE_RECIPE}"
    )


def test_checkout_validation_names_the_isolated_worktree_recipe_for_a_shared_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkout whose git-dir is the shared common dir (not a linked
    worktree) names the same recipe as the trunk-branch refusal above."""
    values = {
        ("rev-parse", "HEAD"): BASE,
        ("branch", "--show-current"): "codex/issue-71-claims",
        ("rev-parse", "--git-dir"): "/repo/.git",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        ("status", "--porcelain"): "",
    }
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: values[tuple(arguments)])
    candidate = request()

    with pytest.raises(ClaimError) as error:
        issue_claim._validate_checkout(candidate)

    assert str(error.value) == (
        "build claims require a linked isolated worktree checkout; "
        f"run {checkout.ISOLATED_WORKTREE_RECIPE}"
    )


def test_checkout_validation_names_the_first_three_dirty_paths_and_the_rest_as_a_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dirty-tree refusal used to discard `git status --porcelain`'s
    list entirely; it now names the first three changed paths and how many
    more there are (#52)."""
    porcelain = "\n".join(
        [" M src/a.py", " M src/b.py", "?? src/c.py", " M src/d.py", " M src/e.py"]
    )
    values = {
        ("rev-parse", "HEAD"): BASE,
        ("branch", "--show-current"): "codex/issue-71-claims",
        ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        ("status", "--porcelain"): porcelain,
    }
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: values[tuple(arguments)])
    candidate = request()

    with pytest.raises(ClaimError) as error:
        issue_claim._validate_checkout(candidate)

    assert str(error.value) == (
        "claim must be acquired before the first worktree edit: "
        "src/a.py, src/b.py, src/c.py, and 2 more"
    )


def test_checkout_validation_names_every_dirty_path_when_three_or_fewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No trailing count when every changed path already fits in the first
    three named."""
    values = {
        ("rev-parse", "HEAD"): BASE,
        ("branch", "--show-current"): "codex/issue-71-claims",
        ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        ("status", "--porcelain"): " M src/a.py",
    }
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: values[tuple(arguments)])
    candidate = request()

    with pytest.raises(ClaimError) as error:
        issue_claim._validate_checkout(candidate)

    assert str(error.value) == "claim must be acquired before the first worktree edit: src/a.py"


def _real_git(repository: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments], cwd=repository, check=True, capture_output=True, text=True
    )


def _scratch_git_repository(tmp_path: Path) -> Path:
    """An initialized repository with one committed, tracked file -- for
    tests that drive `_dirty_paths` against real `git status --porcelain`
    output instead of a fake standing in for `_git_output` itself."""
    repository = tmp_path / "repo"
    repository.mkdir()
    _real_git(repository, "init", "-q", "-b", "main")
    _real_git(repository, "config", "user.name", "Test")
    _real_git(repository, "config", "user.email", "test@example.com")
    (repository / "README.md").write_text("hello\n")
    _real_git(repository, "add", "README.md")
    _real_git(repository, "commit", "-q", "-m", "initial")
    return repository


def test_dirty_paths_reads_a_modified_tracked_file_from_real_git_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the truncated-name bug (#52 follow-up): `_git_output`
    used to `.strip()` its whole decoded output, which ate the leading space
    of a modified file's ` M path` porcelain line before `_dirty_paths`
    sliced off the fixed three-character status prefix -- `README.md` came
    back as `EADME.md`. A fake that hands `_dirty_paths` a hand-typed string
    with the leading space intact cannot catch this; only the real reader
    against real git output can."""
    repository = _scratch_git_repository(tmp_path)
    (repository / "README.md").write_text("hello\nmodified\n")
    monkeypatch.chdir(repository)

    assert checkout._dirty_paths(checkout._git_output(["status", "--porcelain"])) == ("README.md",)


def test_dirty_paths_reads_an_untracked_file_from_real_git_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The untracked `?? path` line has no leading space to lose, which is
    why the truncation bug above went unnoticed."""
    repository = _scratch_git_repository(tmp_path)
    (repository / "extra.txt").write_text("new\n")
    monkeypatch.chdir(repository)

    assert checkout._dirty_paths(checkout._git_output(["status", "--porcelain"])) == ("extra.txt",)


def test_dirty_paths_reads_a_rename_from_real_git_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_dirty_paths`'s docstring claims a rename's `old -> new` line is
    handled; prove it against a real rename rather than a hand-typed line
    that could not tell us whether the code actually handles it."""
    repository = _scratch_git_repository(tmp_path)
    _real_git(repository, "mv", "README.md", "RENAMED.md")
    monkeypatch.chdir(repository)

    assert checkout._dirty_paths(checkout._git_output(["status", "--porcelain"])) == (
        "README.md -> RENAMED.md",
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
        ("log", "--first-parent", "--reverse", "--format=%cI", "refs/remotes/origin/main"): "",
    }


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


def _patch_release_session(
    monkeypatch: pytest.MonkeyPatch,
    client: FakeForge,
    *standing: ClaimRequest,
    agent: str = "Ada",
    branch: str | None = "lane-72",
    forbid_git: bool = False,
) -> None:
    _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: agent})
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    _patch_store_write(monkeypatch, *(_store_claim_from_request(claimed) for claimed in standing))
    if forbid_git:

        def git(arguments: list[str]) -> str:
            if tuple(arguments) == ("rev-parse", "--show-toplevel"):
                return "/repo"
            pytest.fail("explicit --claim-id must not inspect checkout branch")

        monkeypatch.setattr(checkout, "_git_output", git)
        return
    git_values = {
        ("branch", "--show-current"): branch or "",
        ("rev-parse", "--show-toplevel"): "/repo",
    }
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])


def _assert_missing_identity_message(message: str) -> None:
    assert "--agent" in message
    assert issue_claim.ACO_AGENT_ENV in message
    assert issue_claim.GROK_SESSION_ID_ENV in message
    assert issue_claim.CLAUDE_SESSION_ID_ENV in message
    assert "GROK_AGENT" not in message


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
        ((), _git_checkout(branch="master"), "isolated non-main worktree branch"),
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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
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


def test_claim_still_requires_scope() -> None:
    parser = issue_claim._parser()
    with pytest.raises(SystemExit) as exited:
        parser.parse_args(["claim", "42", "--agent", "Ada", "--role", "builder"])

    assert exited.value.code == 2


def test_cli_claim_role_argparse_default_unchanged_and_release_omits_role() -> None:
    claimed = issue_claim._parser().parse_args(["claim", "42", "--scope", "src/widget.py"])
    released = issue_claim._parser().parse_args(["release", "42", "--merged", "12"])

    assert claimed.role == issue_claim.DEFAULT_CLAIM_ROLE
    assert released.role is None
    assert released.merged == 12
    assert released.abandoned is None
    assert released.claim_id is None
    assert released.coordinator_override is False


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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    claimed = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    argv = [
        "--repo",
        "example/agent-claim",
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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    command = _claim_without_agent_args()
    if explicit is not None:
        command.extend(["--agent", explicit])
    parsed = issue_claim._parser().parse_args(command)
    assert issue_claim._request(parsed).agent == agent

    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    assert issue_claim.main(["--repo", "example/agent-claim", *command]) == 0
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
        assert issue_claim.main(["--repo", "example/agent-claim", *argv]) == 2
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
        assert issue_claim.main(["--repo", "example/agent-claim", *argv]) == 2
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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    claimed = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
            "example/agent-claim",
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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
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
            "example/agent-claim",
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

    released = issue_claim.main(
        ["--repo", "example/agent-claim", "release", "72", "--abandoned", "stopped"]
    )

    assert released == 0
    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


def test_cli_release_omitted_claim_id_releases_when_foreign_peer_exists_on_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mine = request("mine", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",))
    client = FakeForge()
    _patch_release_session(monkeypatch, client, mine)

    released = issue_claim.main(
        ["--repo", "example/agent-claim", "release", "72", "--abandoned", "stopped"]
    )

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

    released = issue_claim.main(
        ["--repo", "example/agent-claim", "release", "72", "--abandoned", "stopped"]
    )
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
            "example/agent-claim",
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
    _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: "Ada"})
    _forbid_github_construction(monkeypatch)

    def unused(arguments: list[str]) -> str:
        pytest.fail("coordinator override must fail before git")

    monkeypatch.setattr(checkout, "_git_output", unused)

    released = issue_claim.main(["--repo", "example/agent-claim", "release", "72", *flags])
    captured = capsys.readouterr()

    assert released == 2
    assert captured.out == ""
    assert "ERROR:" in captured.err
    assert "coordinator override" in captured.err


def test_cli_release_omitted_claim_id_fails_closed_on_detached_head(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: "Ada"})
    _forbid_github_construction(monkeypatch)
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: "")

    released = issue_claim.main(
        ["--repo", "example/agent-claim", "release", "72", "--abandoned", "stopped"]
    )
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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    claimed = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    claimed = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
            "example/agent-claim",
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
    assert issue_claim.main(["--repo", "example/agent-claim", "board"]) == 2
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
        lambda: (
            "LICENSE",
            "README.md",
            "pyproject.toml",
            "src/agent_coordination/__init__.py",
        ),
    )


@pytest.fixture(autouse=True)
def _stub_canonical_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every CLI store command refuses a forge-target / canonical-remote
    mismatch (issue #176 done-when 6). Tests talk to `--repo example/agent-claim`
    against a fake; this stub is the matching remote URL so they are not
    refused before the behaviour under test. Tests of `remote_url` itself
    rebind `_LIVE_REMOTE_URL`.
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
# tests of trunk_landing_times itself call _LIVE_TRUNK_LANDING_TIMES.
@pytest.fixture(autouse=True)
def _stub_trunk_landing_times(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())


def test_versioned_paths_reads_nul_terminated_ls_files_without_stripping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[list[str]] = []

    def run(arguments, **kwargs):
        observed.append(arguments)
        return subprocess.CompletedProcess(
            arguments, 0, stdout=b" foo.py\0bar.py\0 foo.py\0", stderr=b""
        )

    monkeypatch.setattr(subprocess, "run", run)

    assert _LIVE_VERSIONED_PATHS() == (" foo.py", "bar.py")
    assert observed == [["git", "ls-files", "-z", "--full-name"]]


@pytest.mark.parametrize(
    "git_call",
    [
        pytest.param(_LIVE_VERSIONED_PATHS, id="versioned-paths"),
        pytest.param(checkout.origin_remote_url, id="origin-remote-url"),
    ],
)
@pytest.mark.parametrize(
    ("raised", "match"),
    [
        pytest.param(
            FileNotFoundError("git"), "git is required for issue claims", id="missing-executable"
        ),
        pytest.param(
            subprocess.TimeoutExpired(["git"], process.DEFAULT_TIMEOUT_SECONDS),
            "git timed out while validating the build checkout",
            id="timed-out",
        ),
    ],
)
def test_checkout_git_calls_fail_loud_when_git_is_missing_or_times_out(
    monkeypatch: pytest.MonkeyPatch,
    git_call: Callable[[], object],
    raised: Exception,
    match: str,
) -> None:
    """`versioned_paths` and `origin_remote_url` -- both direct `subprocess.run`
    callers (`_git_output` backs the latter) -- must translate a missing
    executable or a timeout to the same `ClaimError` text."""
    # `_stub_canonical_remote` (autouse) replaces `checkout.remote_url` with a
    # fixed string so every other store-command test skips a real git call;
    # `origin_remote_url` looks that name up dynamically, so this test must
    # restore the live implementation to actually reach `subprocess.run`.
    monkeypatch.setattr(checkout, "remote_url", _LIVE_REMOTE_URL)

    def fails(*_arguments, **_kwargs):
        raise raised

    monkeypatch.setattr(subprocess, "run", fails)
    with pytest.raises(ClaimError, match=match):
        git_call()


def test_versioned_paths_fails_loud_on_a_nonzero_git_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(arguments, **_kwargs):
        return subprocess.CompletedProcess(
            arguments, 128, stdout=b"", stderr=b"fatal: not a git repository\n"
        )

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(ClaimError, match="fatal: not a git repository"):
        _LIVE_VERSIONED_PATHS()


@pytest.fixture(autouse=True)
def _freeze_cli_now(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(issue_claim, "datetime", FixedDateTime)


def _patch_status_cli(monkeypatch: pytest.MonkeyPatch, client: FakeForge) -> None:
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(
        checkout,
        "versioned_paths",
        lambda: (
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
    rescope tests. `committer_date` answers every live claim's age as
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
        )
        self.transitions: list[protocol.ClaimTransitionIntent] = []
        self._ages = dict(ages or {})

    def fetch_state(self, *, worktree: Path, remote: str) -> protocol.ClaimState:
        return self.state

    def commit_transition(
        self,
        *,
        worktree: Path,
        subject: str,
        intent: protocol.ClaimTransitionIntent,
        remote: str = "origin",
        transport: object = None,
    ) -> protocol.ClaimState:
        if self.state.tip is None:
            raise protocol.ClaimError(protocol.MISSING_STATE_REF)
        self.transitions.append(intent)
        self.state = protocol.apply(self.state, intent)
        return self.state

    def committer_date(
        self, *, worktree: Path, tip: protocol.ObjectId, commit: protocol.ObjectId
    ) -> datetime:
        matching = next(
            claim for claim in self.state.claims.values() if claim.opened_commit == commit
        )
        return self._ages.get(matching.claim_id, _STATUS_NOW)


def _patch_store_write(
    monkeypatch: pytest.MonkeyPatch,
    *claims: protocol.ActiveClaim,
    tip: str | None = BASE,
    ages: Mapping[str, datetime] | None = None,
    consumed_ids: frozenset[protocol.ClaimId] | None = None,
    resources: Mapping[str, protocol.ResourceRecord] | None = None,
) -> _FakeStore:
    fake = _FakeStore(
        {protocol.claim_key(claim.identity, claim.branch): claim for claim in claims},
        tip=tip,
        ages=ages,
        consumed_ids=consumed_ids,
        resources=resources,
    )
    monkeypatch.setattr(store, "fetch_state", fake.fetch_state)
    monkeypatch.setattr(store, "commit_transition", fake.commit_transition)
    monkeypatch.setattr(store, "committer_date", fake.committer_date)
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
    and each claim's `opened_commit` committer date (every claim reads as
    opened at `_STATUS_NOW` -- 0h 0m old -- unless `ages` names it by claim
    id). Every `status`/`status --path` test builds its live claims via
    `_active_claim` and wires them in here instead of posting through a
    ledger-comment `FakeForge`.
    """
    monkeypatch.setattr(checkout, "remote_url", lambda remote: f"git@github.com:{REPOSITORY}.git")
    keyed = {protocol.claim_key(claim.identity, claim.branch): claim for claim in claims}
    state = protocol.ClaimState(tip=protocol.ObjectId(BASE), claims=keyed)
    monkeypatch.setattr(store, "fetch_state", lambda *, worktree, remote: state)
    resolved_ages: dict[str, datetime] = {claim.claim_id: _STATUS_NOW for claim in claims}
    if ages is not None:
        resolved_ages.update(ages)

    def fake_committer_date(
        *, worktree: Path, tip: protocol.ObjectId, commit: protocol.ObjectId
    ) -> datetime:
        matching = next(claim for claim in claims if claim.opened_commit == commit)
        return resolved_ages[matching.claim_id]

    monkeypatch.setattr(store, "committer_date", fake_committer_date)
    monkeypatch.setattr(issue_claim, "datetime", FixedDateTime)


def test_cli_status_empty_store_prints_unclaimed_repository(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_store(monkeypatch)

    assert issue_claim.main(["--repo", "example/agent-claim", "status"]) == 0
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

    assert issue_claim.main(["--repo", "example/agent-claim", "status"]) == 0
    assert capsys.readouterr().out == "UNCLAIMED repository\n"


def test_cli_status_issue_with_no_claim_prints_unclaimed_issue(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_store(monkeypatch)

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72"]) == 0
    assert capsys.readouterr().out == "UNCLAIMED issue #72\n"


def test_cli_status_shows_a_live_store_claim(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    claimed = _active_claim(
        "Codex Sol", claim_id="cli-claim", issue=72, branch="codex/issue-72", scope=("src",)
    )
    _patch_status_store(monkeypatch, claimed)

    status = issue_claim.main(["--repo", "example/agent-claim", "status", "72"])
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

    status = issue_claim.main(["--repo", "example/agent-claim", "status", "72"])

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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    git_values = {
        ("branch", "--show-current"): "docs/lane-cleanup",
        ("rev-parse", "--show-toplevel"): "/repo",
        ("rev-parse", "HEAD"): BASE,
    }
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
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

    released = issue_claim.main(
        ["--repo", "example/agent-claim", "release", "--abandoned", "stopped"]
    )
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

    assert issue_claim.main(["--repo", "example/agent-claim", "status"]) == 0
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
        checkout, "_git_output", lambda arguments: "codex/issue-38-issueless-claims"
    )

    arguments = ["--repo", "example/agent-claim", command]
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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: "")

    status = issue_claim.main(
        ["--repo", "example/agent-claim", "release", "--abandoned", "stopped"]
    )

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

    status = issue_claim.main(["--repo", "example/agent-claim", "status"])
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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "rescope",
            "72",
            "--agent",
            "Ada",
            "--add",
            "src/new.py",
        ]
    )

    assert status == 2
    assert "non-empty current branch" in capsys.readouterr().err


def test_status_direct_empty_claims_prints_unclaimed_repository_without_ledger(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _status((), None, {}) == 0
    assert capsys.readouterr().out == "UNCLAIMED repository\n"


def test_cli_status_json_empty_store_prints_unclaimed_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_store(monkeypatch)

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "--json"]) == 0
    assert (
        capsys.readouterr().out
        == json.dumps({"issue": None, "state": "UNCLAIMED", "claims": []}) + "\n"
    )


def test_cli_status_json_issue_with_no_claim_prints_unclaimed_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_store(monkeypatch)

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72", "--json"]) == 0
    assert (
        capsys.readouterr().out
        == json.dumps({"issue": 72, "state": "UNCLAIMED", "claims": []}) + "\n"
    )


def test_cli_status_json_shows_a_live_store_claim(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    claimed = _active_claim(
        "Codex Sol", claim_id="cli-claim", issue=72, branch="codex/issue-72", scope=("src",)
    )
    _patch_status_store(monkeypatch, claimed)

    status = issue_claim.main(["--repo", "example/agent-claim", "status", "72", "--json"])
    assert status == 0
    assert (
        capsys.readouterr().out
        == json.dumps(
            {
                "issue": 72,
                "state": "CLAIMED",
                "claims": [
                    {
                        "issue": 72,
                        "lane": None,
                        "agent": "Codex Sol",
                        "role": "builder",
                        "base": BASE,
                        "branch": "codex/issue-72",
                        "claim_id": "cli-claim",
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

    status = issue_claim.main(["--repo", "example/agent-claim", "status", "--json"])
    assert status == 0
    assert (
        capsys.readouterr().out
        == json.dumps(
            {
                "issue": None,
                "state": "CLAIMED",
                "claims": [
                    {
                        "issue": 72,
                        "lane": None,
                        "agent": "Codex Sol",
                        "role": "builder",
                        "base": BASE,
                        "branch": "codex/issue-72-claims",
                        "claim_id": "claim-a",
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
                        "agent": "Grok 4.6",
                        "role": "builder",
                        "base": BASE,
                        "branch": "codex/issue-73-claims",
                        "claim_id": "claim-b",
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

    status = issue_claim.main(["--repo", "example/agent-claim", "status", "72", "--json"])
    assert status == 0
    assert (
        capsys.readouterr().out
        == json.dumps(
            {
                "issue": 72,
                "state": "CLAIMED",
                "claims": [
                    {
                        "issue": 72,
                        "lane": None,
                        "agent": "Codex Sol",
                        "role": "builder",
                        "base": BASE,
                        "branch": "codex/issue-72-claims",
                        "claim_id": "claim-a",
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
                        "agent": "Grok 4.6",
                        "role": "builder",
                        "base": BASE,
                        "branch": "codex/issue-73-claims",
                        "claim_id": "claim-b",
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
    for arguments in (["--json", "status"], ["bootstrap", "--json"]):
        parser = issue_claim._parser()
        with pytest.raises(SystemExit) as exited:
            parser.parse_args(arguments)
        assert exited.value.code == 2


def test_cli_claim_without_json_prints_the_claimed_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    git_values = _git_checkout()
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    _patch_store_write(monkeypatch)

    claimed = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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

    assert claimed == 0
    assert capsys.readouterr().out == (
        "CLAIMED issue #72: cli-claim\n"
        "1 of 4 versioned files (25%); overlaps no other open claims\n"
    )


def test_cli_claim_replay_reports_the_matching_live_claim_after_an_interrupted_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    existing = request("live-claim", "Ada", issue=72, branch="codex/issue-72", scope=("src",))
    client = FakeForge()
    _patch_status_cli(monkeypatch, client)
    _patch_store_write(monkeypatch, _store_claim_from_request(existing))
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    arguments = [
        "--repo",
        "example/agent-claim",
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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
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
        board_issue(11, "Top work", complete_contract("Claim #11.")),
        board_issue(12, "Depends on top", complete_contract("Claim #12."), blocked_by_count=1),
    )
    client.board_dependencies = {12: (block_dependency(11),)}
    _patch_status_cli(monkeypatch, client)
    _patch_store_write(monkeypatch, _store_claim_from_request(existing))
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _patch_store_write(monkeypatch, consumed_ids=frozenset({protocol.ClaimId("released-claim")}))

    assert (
        issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    monkeypatch.setattr(checkout, "versioned_paths", lambda: ("docs/report,v2.md",))

    claimed = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
            "example/agent-claim",
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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    monkeypatch.setattr(checkout, "versioned_paths", lambda: ("docs/PRODUCT.md,src/widget.py",))

    joined = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
            "example/agent-claim",
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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "rescope",
            "72",
            "--add",
            "src/new.py",
        ]
    )

    assert status == 0
    assert capsys.readouterr().out == f"RESCOPED issue #72: {acquired.claim_id}\n"
    standing = _live_store_claim()
    assert standing.claim_id == acquired.claim_id
    assert standing.base == BASE
    assert standing.scope == ("src/widget.py", "src/new.py")


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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    monkeypatch.setattr(checkout, "versioned_paths", lambda: ("reports/a,b.md",))
    _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "rescope",
            "72",
            "--add",
            "reports/a,b.md",
        ]
    )

    assert status == 0
    assert capsys.readouterr().out == f"RESCOPED issue #72: {acquired.claim_id}\n"
    assert _live_store_claim().scope == ("src/widget.py", "reports/a,b.md")


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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    monkeypatch.setattr(checkout, "versioned_paths", lambda: ("reports/a,b.md",))
    _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "rescope",
            "72",
            "--drop",
            "reports/a,b.md",
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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "rescope",
            "72",
            "--add",
            "a.py,b.py",
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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "rescope",
            "72",
            "--drop",
            "a.py,b.py",
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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    monkeypatch.setattr(checkout, "versioned_paths", lambda: ("a.py", "b.py"))
    _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "rescope",
            "72",
            "--drop",
            "a.py,b.py",
            "--add",
            "a.py",
            "--add",
            "b.py",
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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: "Ada"})

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "rescope",
            "72",
            "--add",
            "docs/PRODUCT.md",
            "--add",
            "src/new.py",
            "--drop",
            "src/widget.py",
            "--json",
        ]
    )

    assert status == 0
    assert (
        capsys.readouterr().out
        == json.dumps(
            {
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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: "Grok 4.6"})

    status = issue_claim.main(
        ["--repo", "example/agent-claim", "rescope", "72", "--add", "src/new.py"]
    )

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: only the original claimant may rescope "
        "(holder='Ada (builder)', this session='Grok 4.6 (builder)')\n"
    )


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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(["--repo", "example/agent-claim", "rescope", "72"])
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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        ["--repo", "example/agent-claim", "rescope", "72", "--add", "src/new.py"]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "linked isolated worktree" in captured.err


def test_cli_claim_refuses_a_directory_scope_without_whole(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(
        checkout, "_scope_directories", lambda paths: tuple(p for p in paths if p == "docs")
    )

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs",
            "--claim-id",
            "tree",
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "scope is wide" in captured.err
    assert "--whole" in captured.err


def test_cli_claim_wide_scope_refusal_names_the_directory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A directory-tripped refusal names the directory, not the whole rule."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(
        checkout, "_scope_directories", lambda paths: tuple(p for p in paths if p == "docs")
    )

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs",
            "--claim-id",
            "named-directory",
        ]
    )

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: scope is wide: 1 directory in scope (docs); pass --whole REASON\n"
    )


def test_cli_claim_refuses_a_directory_plus_child_scope_without_whole(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(
        checkout, "_scope_directories", lambda paths: tuple(p for p in paths if p == "docs")
    )

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs",
            "--scope",
            "docs/a.md",
            "--claim-id",
            "tree",
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "scope is wide" in captured.err
    assert "--whole" in captured.err


def test_cli_status_path_prints_the_claim_holding_a_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    claimed_claim = _active_claim(
        "Ada", claim_id="mine", issue=72, scope=("docs/PRODUCT.md", "src/widget.py")
    )
    _patch_status_store(monkeypatch, claimed_claim)

    claimed = issue_claim.main(
        ["--repo", "example/agent-claim", "status", "--path", "docs/PRODUCT.md"]
    )
    free = issue_claim.main(["--repo", "example/agent-claim", "status", "--path", "README.md"])
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
        ["--repo", "example/agent-claim", "status", "--path", "docs/decisions/one.md", "--json"]
    )
    claimed = json.loads(capsys.readouterr().out)
    free = issue_claim.main(
        ["--repo", "example/agent-claim", "status", "--path", "src/widget.py", "--json"]
    )
    unclaimed = json.loads(capsys.readouterr().out)

    assert descendant == 0
    assert claimed["state"] == "CLAIMED"
    assert claimed["path"] == "docs/decisions/one.md"
    assert claimed["claims"][0]["claim_id"] == "mine"
    assert free == 0
    assert unclaimed == {
        "path": "src/widget.py",
        "state": "UNCLAIMED",
        "claims": [],
    }


def test_cli_rescope_refuses_adding_a_directory_without_whole(
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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: paths)
    _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(["--repo", "example/agent-claim", "rescope", "72", "--add", "docs"])
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "scope is wide" in captured.err
    assert "--whole" in captured.err
    standing = _live_store_claim()
    assert standing.scope == ("src/widget.py",)


def test_cli_claim_share_above_a_quarter_requires_whole(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    monkeypatch.setattr(checkout, "versioned_paths", lambda: TWELVE_VERSIONED_FILES)

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "LICENSE",
            "--scope",
            "README.md",
            "--scope",
            "src",
            "--claim-id",
            "wide",
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "scope is wide" in captured.err
    assert "--whole" in captured.err


def test_cli_claim_below_the_share_floor_is_never_wide_on_share(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Eleven versioned files stay under `WIDE_SCOPE_SHARE_FLOOR`: three named
    paths covering 4 of 11 is still not wide (Audit ruling 7c, #163)."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    monkeypatch.setattr(checkout, "versioned_paths", lambda: TWELVE_VERSIONED_FILES[:-1])

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "LICENSE",
            "--scope",
            "README.md",
            "--scope",
            "src",
            "--claim-id",
            "below-floor",
        ]
    )
    captured = capsys.readouterr()

    assert status == 0
    assert captured.out.endswith("4 of 11 versioned files (36%); overlaps no other open claims\n")


def test_cli_claim_wide_scope_refusal_names_the_share(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A share-tripped refusal names the covered/versioned counts and the
    percentage, not the whole rule."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    monkeypatch.setattr(checkout, "versioned_paths", lambda: TWELVE_VERSIONED_FILES)

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "LICENSE",
            "--scope",
            "README.md",
            "--scope",
            "src",
            "--claim-id",
            "named-share",
        ]
    )

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: scope is wide: 4 paths of 12 versioned files (33 %) exceeds a quarter; "
        "pass --whole REASON\n"
    )


def test_cli_claim_share_above_a_quarter_succeeds_with_whole(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    monkeypatch.setattr(checkout, "versioned_paths", lambda: TWELVE_VERSIONED_FILES)

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
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
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert payload["versioned_files"] == 4
    assert payload["versioned_files_total"] == 12
    assert payload["share"] == pytest.approx(1 / 3)
    assert payload["touches"] == []


def test_cli_claim_share_at_a_quarter_does_not_need_whole(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Exactly a quarter of twelve versioned files does not exceed the limit."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    monkeypatch.setattr(checkout, "versioned_paths", lambda: TWELVE_VERSIONED_FILES)

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "LICENSE",
            "--scope",
            "README.md",
            "--scope",
            "pyproject.toml",
            "--claim-id",
            "quarter",
        ]
    )

    assert status == 0
    assert capsys.readouterr().out.endswith(
        "3 of 12 versioned files (25%); overlaps no other open claims\n"
    )


def test_cli_claim_touches_stay_empty_beside_a_disjoint_standing_claim(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    standing = request("claim-a", "Ada", issue=73, scope=("LICENSE",))
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _patch_store_write(monkeypatch, _store_claim_from_request(standing))

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _patch_store_write(monkeypatch, _store_claim_from_request(standing))

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(
        checkout, "_scope_directories", lambda paths: tuple(p for p in paths if p == "tests")
    )
    _patch_store_write(monkeypatch, _store_claim_from_request(standing))

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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


def test_claim_age_old_compares_real_age_against_the_threshold() -> None:
    just_over_an_hour = timedelta(seconds=3601)
    exactly_one_hour = timedelta(hours=1)
    sixty_one_minutes = timedelta(seconds=3660)

    assert board.format_claim_age(just_over_an_hour) == "1h 0m"
    assert board.claim_is_old(just_over_an_hour) is True
    assert board.format_claim_age(sixty_one_minutes) == "1h 1m"
    assert board.claim_is_old(sixty_one_minutes) is True
    assert board.claim_is_old(exactly_one_hour) is False


def test_board_shows_claim_age_from_the_claim_comment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    claimed = request("mine", "Ada", issue=72, branch="codex/issue-72", scope=("src",))
    client = FakeForge()
    client.board_issues = (board_issue(72, "Work", complete_contract("Claim #72.")),)
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())
    _patch_store_write(
        monkeypatch,
        _store_claim_from_request(claimed),
        ages={"mine": datetime(2026, 8, 20, 23, 30, tzinfo=UTC)},
    )

    assert issue_claim.main(["--repo", "example/agent-claim", "board"]) == 0
    assert "Ada (builder) 0h 30m" in capsys.readouterr().out
    assert issue_claim.main(["--repo", "example/agent-claim", "board", "--json"]) == 0
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
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())
    _patch_store_write(
        monkeypatch,
        _store_claim_from_request(claimed),
        ages={"mine": datetime(2026, 8, 20, 22, 59, tzinfo=UTC)},
    )

    assert issue_claim.main(["--repo", "example/agent-claim", "board"]) == 0
    assert "Ada (builder) 1h 1m old" in capsys.readouterr().out


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

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72"]) == 0
    status_out = capsys.readouterr().out
    assert " 0h 30m\n" in status_out
    assert " old" not in status_out.split("CLAIMED", 1)[1]

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72", "--json"]) == 0
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

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72"]) == 0
    assert " 1h 1m old\n" in capsys.readouterr().out

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["claims"][0]["age"] == "1h 1m"
    assert payload["claims"][0]["old"] is True


def test_cli_claim_cut_does_not_exempt_a_directory_scope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    client.board_issues = (
        board_issue(
            72,
            "Cut work",
            complete_contract("Claim #72.") + "\n\n## Schnitt\n\n**Scheibe 1: Title**\n",
        ),
    )
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(
        checkout, "_scope_directories", lambda paths: tuple(p for p in paths if p == "docs")
    )

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs",
            "--claim-id",
            "cut",
        ]
    )

    captured = capsys.readouterr()

    assert status == 2
    assert "scope is wide" in captured.err
    assert "--whole" in captured.err


def test_cli_claim_refuses_a_schnitt_heading_without_a_scheibe_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    client.board_issues = (
        board_issue(
            72,
            "Uncut",
            complete_contract("Claim #72.") + "\n\n## Schnitt\n\nNo slices yet.\n",
        ),
    )
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(
        checkout, "_scope_directories", lambda paths: tuple(p for p in paths if p == "docs")
    )

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs",
            "--claim-id",
            "heading",
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert "scope is wide" in captured.err
    assert "--whole" in captured.err


def test_cli_lane_directory_without_whole_is_wide(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_agent_identity_env(monkeypatch, {"ACO_AGENT": "Ada"})
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(
        checkout, "_scope_directories", lambda paths: tuple(p for p in paths if p == "docs")
    )
    git_values = {("branch", "--show-current"): "docs/lane-cleanup"}
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "--base",
            BASE,
            "--branch",
            "docs/lane-cleanup",
            "--scope",
            "docs",
            "--claim-id",
            "lane-docs",
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert "scope is wide" in captured.err
    assert "--whole" in captured.err


def test_cli_claim_cut_directory_still_needs_whole_when_share_is_high(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    client.board_issues = (
        board_issue(
            72,
            "Cut work",
            complete_contract("Claim #72.") + "\n\n## Schnitt\n\n**Scheibe 1: Title**\n",
        ),
    )
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(
        checkout, "_scope_directories", lambda paths: tuple(p for p in paths if p == "docs")
    )
    monkeypatch.setattr(
        checkout,
        "versioned_paths",
        lambda: ("LICENSE", "README.md", "docs/a.md", "docs/b.md"),
    )

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs",
            "--claim-id",
            "wide-cut",
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "scope is wide" in captured.err
    assert "--whole" in captured.err


def test_cli_rescope_add_that_raises_combined_share_requires_whole(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    standing = request(issue=72, branch="codex/issue-72", scope=("src",))
    _patch_store_write(monkeypatch, _store_claim_from_request(standing))
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    git_values = _git_checkout()
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    monkeypatch.setattr(checkout, "versioned_paths", lambda: TWELVE_VERSIONED_FILES)
    _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "rescope",
            "72",
            "--add",
            "LICENSE",
            "--add",
            "README.md",
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "scope is wide" in captured.err
    assert "--whole" in captured.err
    standing = _live_store_claim()
    assert standing.scope == ("src",)


def test_cli_rescope_persists_whole_reason(
    monkeypatch: pytest.MonkeyPatch,
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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: paths)
    _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "rescope",
            "72",
            "--add",
            "docs",
            "--whole",
            "widen to the docs tree",
        ]
    )

    assert status == 0
    standing = _live_store_claim()
    assert standing.scope == ("src/widget.py", "docs")
    assert standing.whole_reason == "widen to the docs tree"


def test_wide_scope_trip_for_paths_directory_or_share_above_the_limits() -> None:
    three = ("a.py", "b.py", "c.py")
    four = (*three, "d.py")
    assert (
        protocol.wide_scope_trip(
            three, directories=(), covered_file_count=3, versioned_file_count=20
        )
        is None
    )
    assert (
        protocol.wide_scope_trip(
            four, directories=(), covered_file_count=4, versioned_file_count=20
        )
        is not None
    )
    assert (
        protocol.wide_scope_trip(
            ("docs",), directories=("docs",), covered_file_count=1, versioned_file_count=20
        )
        is not None
    )
    assert (
        protocol.wide_scope_trip(
            ("a.py",), directories=(), covered_file_count=1, versioned_file_count=4
        )
        is None
    )
    assert (
        protocol.wide_scope_trip(
            ("a.py", "b.py", "c.py"),
            directories=(),
            covered_file_count=3,
            versioned_file_count=protocol.WIDE_SCOPE_SHARE_FLOOR - 1,
        )
        is None
    ), "below the share floor, a share over a quarter still does not trip"
    assert (
        protocol.wide_scope_trip(
            ("a.py", "b.py", "c.py"),
            directories=(),
            covered_file_count=4,
            versioned_file_count=protocol.WIDE_SCOPE_SHARE_FLOOR,
        )
        is not None
    ), "at the share floor, a share over a quarter trips"
    assert (
        protocol.wide_scope_trip(
            ("a.py",), directories=(), covered_file_count=0, versioned_file_count=0
        )
        is None
    )


def test_wide_scope_trip_names_the_condition_in_the_rule_s_priority_order() -> None:
    """`wide_scope_trip(...) is not None` is the one rule owner -- a
    path-count trip outranks a directory trip that would also fire."""
    four = ("a.py", "b.py", "c.py", "d.py")
    assert protocol.wide_scope_trip(
        four, directories=("a.py",), covered_file_count=4, versioned_file_count=20
    ) == protocol.WideScopeTrip(protocol.WideScopeReason.PATH_COUNT, 4, ("a.py",), 4, 20)
    assert protocol.wide_scope_trip(
        ("docs",), directories=("docs",), covered_file_count=1, versioned_file_count=20
    ) == protocol.WideScopeTrip(protocol.WideScopeReason.DIRECTORY, 1, ("docs",), 1, 20)
    assert protocol.wide_scope_trip(
        ("a.py", "b.py", "c.py"),
        directories=(),
        covered_file_count=4,
        versioned_file_count=protocol.WIDE_SCOPE_SHARE_FLOOR,
    ) == protocol.WideScopeTrip(
        protocol.WideScopeReason.SHARE, 3, (), 4, protocol.WIDE_SCOPE_SHARE_FLOOR
    )
    assert (
        protocol.wide_scope_trip(
            ("a.py",), directories=(), covered_file_count=1, versioned_file_count=4
        )
        is None
    )


def test_cli_claim_accepts_three_named_paths_without_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "new_a.py",
            "--scope",
            "new_b.py",
            "--scope",
            "new_c.py",
            "--claim-id",
            "three",
        ]
    )

    assert status == 0
    posted = _live_store_claim()
    assert posted.scope == ("new_a.py", "new_b.py", "new_c.py")
    assert posted.whole_reason is None


def test_cli_claim_refuses_four_named_paths_without_whole(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
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
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "scope is wide" in captured.err
    assert "--whole" in captured.err


def test_cli_claim_wide_scope_refusal_names_the_path_count(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A path-count-tripped refusal names the count and the limit, not the
    whole rule."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
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
        ]
    )

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: scope is wide: 4 paths exceeds three; pass --whole REASON\n"
    )


def test_cli_rescope_widening_to_four_paths_refuses_without_whole(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    standing = request(issue=72, branch="codex/issue-72", scope=("new_a.py",))
    _patch_store_write(monkeypatch, _store_claim_from_request(standing))
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    git_values = _git_checkout()
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "rescope",
            "72",
            "--add",
            "new_b.py",
            "--add",
            "new_c.py",
            "--add",
            "new_d.py",
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "scope is wide" in captured.err
    assert "--whole" in captured.err
    standing = _live_store_claim()
    assert standing.scope == ("new_a.py",)


def test_cli_claim_persists_whole_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    reason = "the four adapters share one lock"

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "new_a.py",
            "--scope",
            "new_b.py",
            "--scope",
            "new_c.py",
            "--scope",
            "new_d.py",
            "--whole",
            reason,
            "--claim-id",
            "wide",
        ]
    )

    assert status == 0
    posted = _live_store_claim()
    assert posted.whole_reason == reason


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

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72"]) == 0
    status_out = capsys.readouterr().out
    assert f"  whole: {reason}" in status_out

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["claims"][0]["whole"] == reason

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "--path", "new_a.py"]) == 0
    who_out = capsys.readouterr().out
    assert f"  whole: {reason}" in who_out

    assert (
        issue_claim.main(
            ["--repo", "example/agent-claim", "status", "--path", "new_a.py", "--json"]
        )
        == 0
    )
    who_payload = json.loads(capsys.readouterr().out)
    assert who_payload["claims"][0]["whole"] == reason


def test_cli_claim_allows_a_directory_scope_with_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(
        checkout, "_scope_directories", lambda paths: tuple(p for p in paths if p == "docs")
    )

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Ada",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs",
            "--whole",
            "rewrite the docs tree",
            "--claim-id",
            "tree",
        ]
    )

    assert status == 0
    posted = _live_store_claim()
    assert posted.scope == ("docs",)
    assert posted.whole_reason == "rewrite the docs tree"


def test_cli_release_without_json_prints_the_released_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    standing = request("mine", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",))
    client = FakeForge()
    _patch_release_session(monkeypatch, client, standing)

    released = issue_claim.main(
        ["--repo", "example/agent-claim", "release", "72", "--abandoned", "stopped"]
    )

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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    claimed = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
                **identity_fields,
                "claim_id": "cli-claim",
                "agent": "Codex Sol",
                "role": "builder",
                "base": BASE,
                "branch": branch,
                "scope": ["src", "docs"],
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
    assert posted.scope == ("src", "docs")


@pytest.mark.parametrize(
    (
        "issue_argument",
        "branch",
        "identity_fields",
        "standing_role",
        "flags",
        "agent",
        "role",
        "reason",
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
    reason: str,
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

    released = issue_claim.main(
        ["--repo", "example/agent-claim", "release", *issue_argument, *flags]
    )

    assert released == 0
    assert (
        capsys.readouterr().out
        == json.dumps(
            {
                **identity_fields,
                "branch": branch,
                "claim_id": "mine",
                "agent": agent,
                "role": role,
                "reason": reason,
            }
        )
        + "\n"
    )
    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


def _assert_json_error_object_mirrors_stderr(err: str, out: str) -> None:
    """`main`'s general `ClaimError` sink (issue #199): a `--json` caller's
    stdout object states exactly the sentence stderr already printed --
    never a second, drifting copy of the error text."""
    assert err.startswith("ERROR: ")
    message = err.removeprefix("ERROR: ").rstrip("\n")
    assert json.loads(out) == {"ok": False, "error": message}


@pytest.mark.parametrize(
    "arguments",
    [
        [
            "claim",
            "72",
            "--agent",
            "Ada",
            "--scope",
            "src",
            "--claim-id",
            "cli-claim",
            "--json",
        ],
        [
            "release",
            "72",
            "--agent",
            "Ada",
            "--claim-id",
            "mine",
            "--abandoned",
            "stopped",
            "--json",
        ],
    ],
)
def test_cli_claim_and_release_json_errors_print_the_stdout_error_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    _patch_status_cli(monkeypatch, FakeForge())

    assert issue_claim.main(["--repo", "example/agent-claim", *arguments]) == 2
    captured = capsys.readouterr()
    _assert_json_error_object_mirrors_stderr(captured.err, captured.out)


def test_cli_claim_json_conflict_prints_the_stdout_error_object_not_a_success_shape(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    standing = request(issue=72, scope=("src",))
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _patch_store_write(monkeypatch, _store_claim_from_request(standing))

    claimed = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
    _assert_json_error_object_mirrors_stderr(captured.err, captured.out)


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
    monkeypatch.setattr(sys, "argv", ["aco", "--repo", "example/agent-claim", "protect"])
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


def _active_claim(
    agent: str = "Grok sess-1",
    *,
    claim_id: str = "cli-claim",
    role: str = "builder",
    scope: tuple[str, ...] = ("src",),
    branch: str = "codex/issue-72-claims",
    lane: bool = False,
    issue: int = 72,
    base: str = BASE,
    opened_commit: str = BASE,
    resource: protocol.ResourceHold | None = None,
    whole_reason: str | None = None,
) -> protocol.ActiveClaim:
    """Build one store-truth `ActiveClaim` directly (issue #176): the store
    fake's counterpart to `request()`'s ledger-comment `ClaimRequest` --
    every `protect`/`status` test that needs a live claim on a faked
    `store.fetch_state` builds it from here instead of round-tripping
    through a comment marker no store command reads any more."""
    identity: protocol.ClaimIdentity = (
        protocol.LaneIdentity() if lane else protocol.IssueIdentity(issue)
    )
    return protocol.ActiveClaim(
        identity=identity,
        claim_id=protocol.ClaimId(claim_id),
        agent=agent,
        role=role,
        base=protocol.ObjectId(base),
        branch=branch,
        scope=scope,
        opened_commit=protocol.ObjectId(opened_commit),
        resource=resource,
        whole_reason=whole_reason,
    )


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


def _forbid_protect_git_github_and_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    def unused(*args, **kwargs):
        pytest.fail("this protect path must not use identity, git, GitHub, or the store")

    monkeypatch.setattr(checkout, "_resolved_agent", unused)
    monkeypatch.setattr(checkout, "_git_output", unused)
    monkeypatch.setattr(github, "GitHubForge", unused)
    monkeypatch.setattr(github, "discover_repository", unused)
    monkeypatch.setattr(store, "fetch_state", unused)


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
    assert calls == ["identity", "git", "git", "git", "git", "store"]


@pytest.mark.parametrize(
    "payload",
    [
        {"toolName": "Bash", "toolInput": {"path": "src/cli.py", "command": "rm -rf /"}},
        {"tool_name": "run_terminal_command", "tool_input": {"command": "git status"}},
        {"toolName": "read_file", "toolInput": {"path": "src/secret.py"}},
        {"tool_name": "grep", "tool_input": {"pattern": "secret"}},
        {"toolName": "list_dir", "toolInput": {"path": "src"}},
        {"tool_name": "spawn_subagent", "tool_input": {"prompt": "edit src"}},
        {"toolName": "unknown"},
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


@pytest.mark.parametrize("branch", ["main", "master"])
def test_protect_main_branch_denies_without_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    branch: str,
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work, {("branch", "--show-current"): branch})
    _forbid_github_construction(monkeypatch)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 2
    )
    _assert_protect_decision(capsys, decision="deny", reason="not main")


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
    """A `ClaimError` raised before the store is ever reached (here, resolving
    the forge target) denies with its own bare text -- only a failure inside
    `store.fetch_state` itself gets the 'cannot reach refs/aco/state' wrapping
    (see the dedicated store-refusal tests below)."""
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)

    def failed(*_args: object, **_kwargs: object) -> forge.RepositoryId:
        raise ClaimError("adapter failed")

    monkeypatch.setattr(github, "discover_repository", failed)

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

    def crashed(*_args: object, **_kwargs: object) -> forge.RepositoryId:
        raise RuntimeError("write path crashed")

    monkeypatch.setattr(github, "discover_repository", crashed)

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


def test_cli_claim_resource_prints_the_allocated_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(
        checkout,
        "_scope_directories",
        lambda paths: tuple(path for path in paths if path == "src"),
    )

    first = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
            "example/agent-claim",
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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(
        checkout,
        "_scope_directories",
        lambda paths: tuple(path for path in paths if path == "tests"),
    )

    first = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
            "example/agent-claim",
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

    status = issue_claim.main(["--repo", "example/agent-claim", "status"])
    rendered = capsys.readouterr().out
    assert status == 0
    assert "CONFLICT" not in rendered
    assert "CLAIMED issue #72" in rendered
    assert "CLAIMED issue #73" in rendered
    assert "overlaps issue #73 (dir-b)" in rendered
    assert "overlaps issue #72 (dir-a)" in rendered

    who = issue_claim.main(["--repo", "example/agent-claim", "status", "--path", "src"])
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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    first = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
            "example/agent-claim",
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

    status = issue_claim.main(["--repo", "example/agent-claim", "status"])
    rendered = capsys.readouterr().out
    assert status == 0
    assert "CONFLICT" not in rendered
    assert "CLAIMED issue #72" in rendered
    assert "CLAIMED issue #73" in rendered

    who = issue_claim.main(["--repo", "example/agent-claim", "status", "--path", "src/widget.py"])
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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    first = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
            "example/agent-claim",
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
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
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
            "example/agent-claim",
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

    status = issue_claim.main(
        ["--repo", "example/agent-claim", "status", "--path", "src/widget.py"]
    )
    rendered = capsys.readouterr().out

    assert status == 0
    assert "CONFLICT" not in rendered
    assert "CLAIMED src/widget.py issue #72" in rendered
    assert "CLAIMED src/widget.py issue #73" in rendered
    assert "overlap: issue #72 (mine), issue #73 (theirs)" in rendered


def test_proposed_expectations_have_neither_fresh_nor_old() -> None:
    issue = board_issue(
        10,
        "Proposed",
        complete_contract("Claim #10.", expectation=[proposed_expectation("Name it.")]),
    )
    projected = projected_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.items[0].expectation_state is board.ExpectationState.PROPOSED
    assert projected.items[0].ruling_landings is None
    assert projected.items[0].ruling_old is None


def test_one_unruled_entry_among_ruled_ones_keeps_the_item_proposed() -> None:
    issue = board_issue(
        10,
        "Three expectations",
        complete_contract(
            "Claim #10.",
            expectation=[
                ruled_expectation("Create it."),
                ruled_expectation("Change it.", ruling="no"),
                proposed_expectation("Remove it."),
            ],
        ),
    )
    projected = projected_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.items[0].expectation_state is board.ExpectationState.PROPOSED


def test_expectation_progress_counts_open_and_total_entries() -> None:
    body = complete_contract(
        "Claim #10.",
        expectation=[
            ruled_expectation("Create it."),
            proposed_expectation("Change it."),
            ruled_expectation("Remove it.", ruling="no"),
            proposed_expectation("Scale it.", default="no"),
            ruled_expectation("Keep it."),
        ],
    )

    parsed = board.parse_body(body)

    assert parsed.expectation_state is board.ExpectationState.PROPOSED
    assert parsed.expectation_progress == board.ExpectationProgress(open=2, total=5)


@pytest.mark.parametrize(
    ("now", "trunk_landings", "expected_landings", "expected_old"),
    [
        pytest.param(
            datetime(2026, 8, 30, tzinfo=UTC),
            tuple(datetime(2026, 8, 29, hour, tzinfo=UTC) for hour in range(10)),
            10,
            True,
            id="ten-landings-age-a-ruling",
        ),
        pytest.param(
            datetime(2026, 8, 30, tzinfo=UTC),
            (datetime(2026, 8, 29, tzinfo=UTC),),
            1,
            False,
            id="one-landing-does-not",
        ),
        pytest.param(
            datetime(2026, 8, 28, tzinfo=UTC),
            (datetime(2026, 8, 28, 23, tzinfo=UTC),),
            0,
            False,
            id="a-same-day-landing-does-not-age-the-ruling",
        ),
    ],
)
def test_ruling_freshness_counts_trunk_landings_after_the_ruled_on_date(
    now: datetime,
    trunk_landings: tuple[datetime, ...],
    expected_landings: int,
    expected_old: bool,
) -> None:
    issue = board_issue(
        10,
        "Ruled",
        complete_contract("Claim #10.", expectation=[ruled_expectation("Name it.")]),
    )
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=now,
        trunk_landings=trunk_landings,
    )
    item = projected.items[0]

    assert item.ruling_landings == expected_landings
    assert item.ruling_old is expected_old
    assert f"ruled {expected_landings}" in board.render(projected)


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
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(
        checkout,
        "trunk_landing_times",
        lambda: tuple(datetime(2026, 8, 29, hour, tzinfo=UTC) for hour in range(10)),
    )
    _patch_store_write(monkeypatch)

    assert issue_claim.main(["--repo", "example/agent-claim", "next"]) == 0
    assert capsys.readouterr().out == (
        "#10 score -10: Work\n"
        "Next: Claim #10.\n"
        "Run: aco claim 10 --scope <paths>\n"
        "<paths> cannot be derived; take the files to claim from the item body.\n"
        "vor 10 Landungen geregelt, beim Ziehen neu refinen\n"
    )

    assert issue_claim.main(["--repo", "example/agent-claim", "next", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ruling_landings"] == 10
    assert payload["ruling_old"] is True
    assert payload["ruling_hint"] == "vor 10 Landungen geregelt, beim Ziehen neu refinen"


def test_each_item_carries_its_own_ruling_age() -> None:
    fresh = board_issue(
        10,
        "Fresh",
        complete_contract("Claim #10.", expectation=[ruled_expectation("Name it.")]),
    )
    old = board_issue(
        11,
        "Old",
        complete_contract(
            "Claim #11.",
            expectation=[ruled_expectation("Name it.", ruled_on=date(2026, 8, 1))],
        ),
    )
    landings = tuple(datetime(2026, 8, 10 + index, tzinfo=UTC) for index in range(12))
    projected = projected_board(
        (fresh, old),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 30, tzinfo=UTC),
        trunk_landings=landings,
    )
    by_number = {item.number: item for item in projected.items}

    assert by_number[10].ruling_old is False
    assert by_number[11].ruling_old is True
    assert by_number[11].ruling_landings == 12


def test_identity_conflict_still_marks_status_conflict(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = _active_claim(issue=72, scope=("src/a.py",))
    second = _active_claim(claim_id="claim-b", agent="Grok 4.6", issue=72, scope=("src/b.py",))
    opened_at = datetime(2026, 8, 21, tzinfo=UTC)
    ages: dict[str, datetime] = {first.claim_id: opened_at, second.claim_id: opened_at}

    assert _status((first, second), None, ages) == 2
    rendered = capsys.readouterr().out
    assert rendered.count("CONFLICT") == 2


def test_identity_conflict_still_marks_status_json_conflict(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = _active_claim(issue=72, scope=("src/a.py",))
    second = _active_claim(claim_id="claim-b", agent="Grok 4.6", issue=72, scope=("src/b.py",))
    opened_at = datetime(2026, 8, 21, tzinfo=UTC)
    ages: dict[str, datetime] = {first.claim_id: opened_at, second.claim_id: opened_at}

    assert _status_json((first, second), None, ages) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "CONFLICT"


def test_trunk_landing_times_read_the_default_branch_not_the_work_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[list[str]] = []

    def git_output(arguments: list[str]) -> str:
        observed.append(arguments)
        if arguments[:3] == ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"]:
            return "refs/remotes/origin/main"
        if arguments[:4] == ["log", "--first-parent", "--reverse", "--format=%cI"]:
            assert arguments[4] == "refs/remotes/origin/main"
            return "2026-08-29T00:00:00+00:00\n2026-08-30T00:00:00Z"
        raise AssertionError(arguments)

    monkeypatch.setattr(checkout, "_git_output", git_output)
    times = _LIVE_TRUNK_LANDING_TIMES()

    assert times == (
        datetime(2026, 8, 29, tzinfo=UTC),
        datetime(2026, 8, 30, tzinfo=UTC),
    )
    assert [
        "log",
        "--first-parent",
        "--reverse",
        "--format=%cI",
        "refs/remotes/origin/main",
    ] in observed


def test_trunk_ref_fails_loud_when_no_candidate_branch_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither the symbolic ref nor any of the default-branch-name candidates
    resolving must fail loud rather than silently ruling every candidate's age
    as unknown."""

    def git_output(_arguments: list[str]) -> str:
        raise ClaimError("fatal: not a git repository")

    monkeypatch.setattr(checkout, "_git_output", git_output)
    with pytest.raises(ClaimError, match="cannot determine the main branch for ruling age"):
        _LIVE_TRUNK_LANDING_TIMES()


def test_trunk_landing_times_is_empty_when_trunk_has_no_first_parent_landings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def git_output(arguments: list[str]) -> str:
        if arguments[:3] == ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"]:
            return "refs/remotes/origin/main"
        if arguments[:4] == ["log", "--first-parent", "--reverse", "--format=%cI"]:
            return ""
        raise AssertionError(arguments)

    monkeypatch.setattr(checkout, "_git_output", git_output)
    assert _LIVE_TRUNK_LANDING_TIMES() == ()


@pytest.mark.parametrize(
    "raw_commit_time",
    [
        pytest.param("not-a-timestamp", id="unparsable"),
        pytest.param("2026-08-29T00:00:00", id="missing-offset"),
    ],
)
def test_trunk_landing_times_fails_loud_on_a_malformed_commit_timestamp(
    monkeypatch: pytest.MonkeyPatch, raw_commit_time: str
) -> None:
    """Neither an unparsable `%cI` line nor one git left offset-naive (both
    would only occur if git itself misbehaved) may silently produce a wrong
    ruling age; both fail loud with the same diagnostic."""

    def git_output(arguments: list[str]) -> str:
        if arguments[:3] == ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"]:
            return "refs/remotes/origin/main"
        if arguments[:4] == ["log", "--first-parent", "--reverse", "--format=%cI"]:
            return raw_commit_time
        raise AssertionError(arguments)

    monkeypatch.setattr(checkout, "_git_output", git_output)
    with pytest.raises(ClaimError, match="git returned a malformed trunk landing timestamp"):
        _LIVE_TRUNK_LANDING_TIMES()


def test_trunk_landing_times_count_a_five_commit_merge_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["git", *arguments],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        )

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    git("config", "commit.gpgsign", "false")
    (repo / "file.txt").write_text("0\n")
    git("add", "file.txt")
    git("commit", "-m", "initial")
    git("checkout", "-b", "feature")
    for index in range(1, 6):
        (repo / "file.txt").write_text(f"{index}\n")
        git("add", "file.txt")
        git("commit", "-m", f"commit-{index}")
    git("checkout", "main")
    git("merge", "--no-ff", "-m", "merge feature", "feature")
    git("checkout", "-b", "work")
    monkeypatch.chdir(repo)

    unrestricted = git("log", "--reverse", "--format=%cI").stdout.splitlines()
    assert len(unrestricted) == 7
    assert len(_LIVE_TRUNK_LANDING_TIMES()) == 2


def test_no_path_class_list_is_read_or_written() -> None:
    assert not Path("src/agent_coordination").joinpath("single_writer.py").exists()
    text = Path("src/agent_coordination/protocol.py").read_text()
    assert "single-writer" not in text
    assert "single_writer" not in text


WORK_ITEM_ISSUE = 72
LANDING_BRANCH = f"codex/issue-{WORK_ITEM_ISSUE}-claims"
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
) -> forge.Landing:
    return forge.Landing(
        number,
        author,
        body,
        github._repository_id(head_repository),
        head_ref_name,
        base_ref_name,
        merged,
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

    def outside_a_checkout(arguments: list[str]) -> str:
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

    assert run_check() == 1
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

    assert run_check() == 1
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
            head_repository="fork/agent-claim",
        ),
    )

    assert run_check() == 1
    assert capsys.readouterr().err == (
        "REFUSED: pull request #12 proposes a branch of fork/agent-claim; "
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

    assert run_check() == 1
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

    assert run_check() == 1
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

    assert run_check() == 1
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

    assert run_check() == 1
    assert capsys.readouterr().err == (
        "REFUSED: pull request #12 carries no `Work-Item:` or `No-Item:` line\n"
    )


def api_pull_request(**overrides: object) -> dict[str, object]:
    owner, _, name = REPOSITORY.partition("/")
    payload: dict[str, object] = {
        "number": 12,
        "body": "Work-Item: #72",
        "baseRefName": "main",
        "headRefName": LANDING_BRANCH,
        "headRepository": {"name": name},
        "headRepositoryOwner": {"login": owner},
        "author": {"login": "ada"},
        "mergedAt": "2026-09-05T10:00:00Z",
    }
    return payload | overrides


@pytest.mark.parametrize(
    ("body", "closed"),
    [
        pytest.param("Closes #72", (72,), id="keyword-space-reference"),
        pytest.param("Fixes: #72", (72,), id="colon-then-space"),
        pytest.param("resolved  #72.", (72,), id="sentence-punctuation-ends-it"),
        pytest.param(f"Closes {REPOSITORY}#72, then rest", (72,), id="qualified-reference"),
        pytest.param("Closes#72", (), id="no-space-after-the-keyword"),
        pytest.param("Closes:#72", (), id="colon-without-space"),
        pytest.param("Closes\n#72", (), id="reference-on-the-next-line"),
        pytest.param("Closes #72suffix", (), id="reference-runs-into-a-word"),
        pytest.param("Lands #72", (), id="keyword-github-never-closes-on"),
    ],
)
def test_a_closing_reference_follows_githubs_own_syntax(body: str, closed: tuple[int, ...]) -> None:
    assert board.closing_references(body, REPOSITORY) == frozenset(
        board.IssueReference(REPOSITORY, number) for number in closed
    )


def test_github_adapter_reads_a_pull_request_and_the_default_branch() -> None:
    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        if arguments[:2] == ["pr", "view"]:
            return json.dumps(api_pull_request())
        return "main"

    client = GitHubForge(github._repository_id(REPOSITORY), run=run)

    assert client.landing(12) == forge.Landing(
        12, "ada", "Work-Item: #72", github._repository_id(REPOSITORY), LANDING_BRANCH, "main", True
    )
    assert client.default_branch() == "main"


def test_github_adapter_reads_a_landing_with_no_body_as_empty() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: json.dumps(api_pull_request(body=None)),
    )

    assert client.landing(12).body == ""


def test_github_adapter_reads_a_fork_branch_as_its_own_repository() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: json.dumps(
            api_pull_request(
                headRepository={"name": "agent-claim"},
                headRepositoryOwner={"login": "fork"},
            )
        ),
    )

    assert client.landing(12).source_repository == github._repository_id("fork/agent-claim")


def test_github_adapter_fails_loud_when_github_answers_for_another_pull_request() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: json.dumps(api_pull_request(number=13)),
    )

    with pytest.raises(ClaimError, match="answered for pull request #13, not #12"):
        client.landing(12)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"number": 12, "body": "b"}, id="missing-refs"),
        pytest.param(api_pull_request(author={}), id="author-without-login"),
        pytest.param(api_pull_request(mergedAt="yesterday"), id="malformed-merge-time"),
        pytest.param(api_pull_request(headRepository={}), id="head-repository-without-name"),
        pytest.param(
            api_pull_request(headRepositoryOwner=None), id="head-repository-without-owner"
        ),
        pytest.param(
            api_pull_request(
                headRepository={"name": "repo/extra"}, headRepositoryOwner={"login": "owner"}
            ),
            id="head-repository-invalid-shape",
        ),
    ],
)
def test_github_adapter_fails_loud_on_a_malformed_pull_request(payload: dict[str, object]) -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: json.dumps(payload),
    )

    with pytest.raises(ClaimError) as excinfo:
        client.landing(12)

    assert str(excinfo.value) == github.MALFORMED_PULL_REQUEST


def test_github_adapter_fails_loud_when_the_pull_request_payload_is_not_a_dict() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: json.dumps("not a pull request"),
    )

    with pytest.raises(ClaimError) as excinfo:
        client.landing(12)

    assert str(excinfo.value) == github.MALFORMED_PULL_REQUEST


def test_github_adapter_fails_loud_when_github_answers_with_more_than_one_pull_request() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: (
            f"{json.dumps(api_pull_request())}\n{json.dumps(api_pull_request())}"
        ),
    )

    with pytest.raises(ClaimError) as excinfo:
        client.landing(12)

    assert str(excinfo.value) == github.MALFORMED_PULL_REQUEST


def test_github_adapter_fails_loud_when_github_answers_with_no_pull_request() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY), run=lambda arguments, input_data=None: ""
    )

    with pytest.raises(ClaimError) as excinfo:
        client.landing(12)

    assert str(excinfo.value) == github.MALFORMED_PULL_REQUEST


def test_github_adapter_fails_loud_on_a_malformed_default_branch() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY), run=lambda arguments, input_data=None: "not a branch"
    )

    with pytest.raises(ClaimError, match="malformed default branch"):
        client.default_branch()


def test_a_closing_reference_to_another_repository_confers_no_stage() -> None:
    issue = board_issue(65, "Same number, other repository", complete_contract("Cut it."))
    foreign = board.PullRequest(
        130, "Lands elsewhere", "Fixes other/repo#65", "branch", "2026-08-20T00:00:00Z"
    )

    projected = projected_board(
        (issue,),
        (),
        (foreign,),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert projected.items[0].stage is board.Stage.TEXT_ONLY


LANE_BRANCH = "docs/tidy-readme"


@dataclass(frozen=True)
class ReleaseMergeScenario:
    """The pull request body and merge facts `merged_release_client` builds a
    landing from -- one parametrized case's worth, typed instead of a loose
    `dict[str, object]` so each keyword forwards to it honestly."""

    body: str
    merged: bool = True
    base_ref_name: str = "main"


def merged_release_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: str,
    merged: bool = True,
    base_ref_name: str = "main",
    lane: bool = False,
) -> FakeForge:
    """A session whose one claim can be released against pull request #12."""
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
        body=body, merged=merged, base_ref_name=base_ref_name, head_ref_name=branch
    )
    _patch_release_session(monkeypatch, client, standing, branch=branch)
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


def test_release_merged_accepts_an_issueless_lane_that_landed_without_an_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    merged_release_client(monkeypatch, body="No-Item: docs", lane=True)

    assert issue_claim.main(["--repo", REPOSITORY, "release", "--merged", "12"]) == 0

    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


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
            ReleaseMergeScenario(body="Work-Item: #99\n\nCloses #99"),
            f"pull request #12 names Work-Item: {REPOSITORY}#99, not work item #72",
            id="another-item",
        ),
        pytest.param(
            ReleaseMergeScenario(body="No-Item: docs"),
            "pull request #12 names No-Item: docs, not work item #72",
            id="no-item-for-an-issue-claim",
        ),
        pytest.param(
            ReleaseMergeScenario(body="Advances #72"),
            "pull request #12 carries no `Work-Item:` or `No-Item:` line",
            id="unclassified",
        ),
    ],
)
def test_release_merged_refuses_a_landing_it_cannot_verify(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    scenario: ReleaseMergeScenario,
    reason: str,
) -> None:
    merged_release_client(
        monkeypatch,
        body=scenario.body,
        merged=scenario.merged,
        base_ref_name=scenario.base_ref_name,
    )
    _stub_issue_reference(monkeypatch, {WORK_ITEM_ISSUE: (forge.ItemState.CLOSED, "", "")})

    assert issue_claim.main(["--repo", REPOSITORY, "release", "72", "--merged", "12"]) == 2
    assert capsys.readouterr().err == f"ERROR: {reason}\n"
    assert store.fetch_state(worktree=Path("."), remote="origin").claims


def test_release_merged_refuses_while_the_work_item_is_still_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")

    assert issue_claim.main(["--repo", REPOSITORY, "release", "72", "--merged", "12"]) == 2
    assert capsys.readouterr().err == "ERROR: work item #72 is open, not closed\n"
    assert store.fetch_state(worktree=Path("."), remote="origin").claims


def test_release_merged_refuses_a_lane_whose_pull_request_names_an_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72", lane=True)

    assert issue_claim.main(["--repo", REPOSITORY, "release", "--merged", "12"]) == 2
    assert capsys.readouterr().err == (
        f"ERROR: pull request #12 names {REPOSITORY}#72; an issue-less lane needs a No-Item line\n"
    )


def test_release_abandoned_records_why_the_lane_stopped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")

    assert (
        issue_claim.main(["--repo", REPOSITORY, "release", "72", "--abandoned", "overtaken by #80"])
        == 0
    )

    assert store.fetch_state(worktree=Path("."), remote="origin").claims == {}


@pytest.mark.parametrize(
    "arguments",
    [
        ["release", "42"],
        ["release", "42", "--merged", "12", "--abandoned", "stuck"],
    ],
)
def test_release_requires_exactly_one_landing_outcome(arguments: list[str]) -> None:
    parser = issue_claim._parser()
    with pytest.raises(SystemExit) as exited:
        parser.parse_args(arguments)

    assert exited.value.code == 2


PARENT_ISSUE = 79


def parented_check_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: str,
    parent_body: str,
    open_children: tuple[board.IssueReference, ...],
    parent_repository: str = REPOSITORY,
    parent_kind: board.ItemKind | None = board.ItemKind.CONTAINER,
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

    assert run_check() == 1
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
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
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


def test_check_refuses_a_legacy_parent_before_the_next_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    _write_block_pin(tmp_path)
    parented_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72",
        parent_body="## Now\nOld prose.\n",
        open_children=(board.IssueReference(REPOSITORY, WORK_ITEM_ISSUE),),
    )

    assert run_check() == 1
    assert capsys.readouterr().err == (
        f"REFUSED: pull request #12 has parent {REPOSITORY}#{PARENT_ISSUE} with a legacy body\n"
    )


def test_check_refuses_a_malformed_parent_before_the_next_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
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

    assert run_check() == 1
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
        parent_kind=board.ItemKind.TASK,
    )

    assert run_check() == 1
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

    assert run_check() == 1
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

    assert run_check() == 1
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

    assert run_check() == 1
    assert capsys.readouterr().err == (
        f"REFUSED: pull request #12 has parent other/repo#{PARENT_ISSUE} in another "
        "repository, whose children this check cannot read\n"
    )


API_REPOSITORY_URL = f"https://api.github.com/repos/{REPOSITORY}"


def api_sub_issue(number: int, state: str) -> dict[str, object]:
    return {"number": number, "repository": API_REPOSITORY_URL, "state": state}


def sub_issue_client(*children: dict[str, object]) -> GitHubForge:
    return GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: "\n".join(json.dumps(child) for child in children),
    )


def test_github_adapter_reads_a_recorded_parent_and_its_children() -> None:
    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        if arguments[1].endswith("/parent"):
            return json.dumps(
                {"number": 79, "repository": API_REPOSITORY_URL, "body": "## Next\nCut."}
            )
        return "\n".join(json.dumps(api_sub_issue(number, "open")) for number in (72, 73))

    client = GitHubForge(github._repository_id(REPOSITORY), run=run)

    assert client.parent_issue(72) == board.ParentIssue(
        board.IssueReference(REPOSITORY, 79), "## Next\nCut."
    )
    assert client.list_children(79) == (
        board.ChildItem(72, board.ChildState.OPEN),
        board.ChildItem(73, board.ChildState.OPEN),
    )


def test_github_adapter_reports_a_closed_child_alongside_open_ones() -> None:
    client = sub_issue_client(api_sub_issue(72, "closed"), api_sub_issue(73, "open"))

    assert client.list_children(79) == (
        board.ChildItem(72, board.ChildState.CLOSED),
        board.ChildItem(73, board.ChildState.OPEN),
    )


@pytest.mark.parametrize(
    "child",
    [
        pytest.param({"number": 72, "repository": API_REPOSITORY_URL}, id="state-missing"),
        pytest.param(api_sub_issue(72, "archived"), id="state-unknown"),
    ],
)
def test_github_adapter_fails_loud_on_a_sub_issue_state_it_cannot_read(
    child: dict[str, object],
) -> None:
    client = sub_issue_client(child)

    with pytest.raises(ClaimError, match="malformed sub-issue"):
        client.list_children(79)


def test_github_adapter_refuses_a_sub_issue_from_another_repository() -> None:
    client = sub_issue_client(
        {"number": 72, "repository": "https://api.github.com/repos/other/repo", "state": "open"}
    )

    with pytest.raises(ClaimError, match="sub-issue from another repository"):
        client.list_children(79)


def test_github_adapter_fails_loud_when_a_sub_issue_is_not_an_object() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY), run=lambda arguments, input_data=None: "5"
    )

    with pytest.raises(ClaimError, match="malformed sub-issue"):
        client.list_children(79)


def test_github_adapter_reads_an_issue_without_a_parent_as_parentless() -> None:
    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        raise forge.ForgeNotFoundError("gh: No parent issue found (HTTP 404)")

    client = GitHubForge(github._repository_id(REPOSITORY), run=run)

    assert client.parent_issue(72) is None


def test_github_adapter_reads_the_parents_native_kind() -> None:
    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        return json.dumps(
            {
                "number": 79,
                "repository": API_REPOSITORY_URL,
                "body": "## Next\nCut.",
                "kind": "Container",
            }
        )

    client = GitHubForge(github._repository_id(REPOSITORY), run=run)

    assert client.parent_issue(72) == board.ParentIssue(
        board.IssueReference(REPOSITORY, 79), "## Next\nCut.", board.ItemKind.CONTAINER
    )


def test_github_adapter_fails_loud_on_a_malformed_parent_kind() -> None:
    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        return json.dumps(
            {"number": 79, "repository": API_REPOSITORY_URL, "body": "## Next\nCut.", "kind": 5}
        )

    client = GitHubForge(github._repository_id(REPOSITORY), run=run)

    with pytest.raises(ClaimError, match="malformed parent issue"):
        client.parent_issue(72)


def test_github_adapter_fails_loud_when_the_parent_issue_response_is_not_one_object() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY), run=lambda arguments, input_data=None: json.dumps([])
    )

    with pytest.raises(ClaimError, match="malformed parent issue"):
        client.parent_issue(72)


def test_github_adapter_fails_loud_when_the_parent_issue_body_is_not_text() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: json.dumps(
            {"number": 79, "repository": API_REPOSITORY_URL, "body": 5}
        ),
    )

    with pytest.raises(ClaimError, match="malformed parent issue"):
        client.parent_issue(72)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"number": True}, id="number-is-a-bool"),
        pytest.param({"repository": "not-a-repository-url"}, id="repository-unparsable"),
    ],
)
def test_github_adapter_fails_loud_on_a_malformed_parent_reference(
    overrides: dict[str, object],
) -> None:
    value = {"number": 79, "repository": API_REPOSITORY_URL, "body": "## Next\nCut.", **overrides}
    client = GitHubForge(
        github._repository_id(REPOSITORY), run=lambda arguments, input_data=None: json.dumps(value)
    )

    with pytest.raises(ClaimError, match="malformed parent issue"):
        client.parent_issue(72)


def test_board_recovers_an_open_item_a_merged_pull_request_already_landed() -> None:
    landed = board_issue(90, "Landed but open", complete_contract("Close it."))
    also_landed = board_issue(91, "Also landed but open", "")
    merged = board.PullRequest(
        140,
        "Lands the slice",
        "Work-Item: #90\n\nCloses #90",
        "branch",
        "2026-08-20T00:00:00Z",
    )
    also_merged = board.PullRequest(
        141,
        "Lands the other slice",
        "Work-Item: #91\n\nCloses #91",
        "branch",
        "2026-08-20T00:00:00Z",
    )

    projected = projected_board(
        (landed, also_landed),
        (),
        (merged, also_merged),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert [item.number for item in projected.recovery] == [90, 91]
    assert f"RECOVERY ({board.RECOVERY_STEP})\n#90" in board.render(projected)


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


def test_check_names_a_number_that_exists_in_neither_number_space(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """GitHub gives issues and pull requests one number space, so an absent
    number was never proven to be either -- the refusal names no kind word."""
    client = issue_check_client(monkeypatch, tmp_path, body="", state=forge.ItemState.MISSING)

    assert run_check(CHECKED_ISSUE) == 1
    assert capsys.readouterr().err == (
        f"REFUSED: #{CHECKED_ISSUE} does not exist in {REPOSITORY}\n"
    )
    assert client.requests == 1


def test_check_json_names_a_missing_number_as_its_own_kind(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    issue_check_client(monkeypatch, tmp_path, body="", state=forge.ItemState.MISSING)

    exit_code = issue_claim.main(["--repo", REPOSITORY, "check", str(CHECKED_ISSUE), "--json"])

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "kind": "missing",
        "number": CHECKED_ISSUE,
        "refused": f"does not exist in {REPOSITORY}",
    }


def test_check_names_a_body_with_no_recognized_block_as_legacy(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`body legacy` keeps its meaning: no recognized block was found, never
    "recognized prose" (#204)."""
    issue_check_client(
        monkeypatch,
        tmp_path,
        body="## Now\nReady.\n\n## Next\nLand it.\n\n## Done when\nMerged.",
    )

    assert run_check(CHECKED_ISSUE) == 1
    assert capsys.readouterr().err == f"ISSUE #{CHECKED_ISSUE} body legacy\n"


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

    assert run_check(CHECKED_ISSUE) == 1
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

    assert run_check(CHECKED_ISSUE) == 1
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

    assert run_check(CHECKED_ISSUE) == 1
    assert capsys.readouterr().err == f"ISSUE #{CHECKED_ISSUE} blocked by #7, other/repo#9\n"
    assert client.requests == 2


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


def test_check_json_prints_the_stdout_error_object_on_a_real_forge_failure(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real `GitHubForge` (issue #199), not `FakeForge`: its own `_run`
    chokepoint raises the forge failure, proving that cause -- not just a
    conflict or a missing state ref -- reaches `main`'s general sink too."""

    def failing_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        raise forge.ForgeTransientError("gh: simulated network failure")

    real_client = GitHubForge(github._repository_id(REPOSITORY), run=failing_run)
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: real_client)

    status = issue_claim.main(["--repo", REPOSITORY, "check", "12", "--json"])

    captured = capsys.readouterr()
    assert status == 2
    _assert_json_error_object_mirrors_stderr(captured.err, captured.out)


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        pytest.param(
            f"Work-Item: #{WORK_ITEM_ISSUE}\n\nCloses #{WORK_ITEM_ISSUE}",
            {"ok": True, "kind": "pull_request", "number": 12},
            id="declared-pull-request",
        ),
        pytest.param(
            "Tidy the README.",
            {
                "ok": False,
                "kind": "pull_request",
                "number": 12,
                "refused": "carries no `Work-Item:` or `No-Item:` line",
            },
            id="unclassified-pull-request",
        ),
    ],
)
def test_check_json_discriminates_a_pull_request(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    body: str,
    expected: dict[str, object],
) -> None:
    check_client(monkeypatch, landing_pull_request(body=body))

    exit_code = issue_claim.main(["--repo", REPOSITORY, "check", "12", "--json"])

    assert exit_code == (0 if expected["ok"] else 1)
    assert json.loads(capsys.readouterr().out) == expected


@pytest.mark.parametrize(
    ("dependencies", "expected"),
    [
        pytest.param(
            (),
            {"ok": True, "kind": "issue", "number": CHECKED_ISSUE},
            id="sound-issue",
        ),
        pytest.param(
            (open_dependency(62),),
            {
                "ok": False,
                "kind": "issue",
                "number": CHECKED_ISSUE,
                "refused": "blocked by #62",
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
    expected: dict[str, object],
) -> None:
    issue_check_client(
        monkeypatch,
        tmp_path,
        body=agent_claim_body(MINIMAL_BLOCK_TOML),
        dependencies=dependencies,
    )

    exit_code = issue_claim.main(["--repo", REPOSITORY, "check", str(CHECKED_ISSUE), "--json"])

    assert exit_code == (0 if expected["ok"] else 1)
    assert json.loads(capsys.readouterr().out) == expected


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


def test_a_non_ascii_digit_in_a_hash_reference_is_not_an_issue_number() -> None:
    qualified = board.closing_references(f"Closes {REPOSITORY}#٣", REPOSITORY)
    work_item = board.parse_pull_request_classification("Work-Item: #٣", REPOSITORY)
    assert qualified == frozenset()
    assert isinstance(work_item, board.ClassificationDefect)


@pytest.mark.parametrize(
    "json_flag", [pytest.param(False, id="without-json"), pytest.param(True, id="with-json")]
)
def test_cli_claim_refuses_a_missing_state_ref(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], json_flag: bool
) -> None:
    """A missing `refs/aco/state` (issue #199): with `--json`, the stdout
    error object mirrors the unchanged stderr sentence; without it, stdout
    stays exactly as empty as it always has."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    git_values = _git_checkout()
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    _patch_store_write(monkeypatch, tip=None)

    arguments = [
        "--repo",
        "example/agent-claim",
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
        _assert_json_error_object_mirrors_stderr(captured.err, captured.out)
    else:
        assert captured.out == ""


def test_cli_claim_refuses_canonical_remote_mismatch_and_writes_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    git_values = _git_checkout()
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    fake = _patch_store_write(monkeypatch)
    monkeypatch.setattr(checkout, "remote_url", lambda remote: "git@github.com:other/repo.git")

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
    assert "forge target example/agent-claim does not match canonical remote other/repo" in (
        capsys.readouterr().err
    )
    assert fake.transitions == []


def test_body_defect_text_is_the_shared_renderer() -> None:
    defect = board.ContractDefect("now", "missing")
    assert board.body_defect_text(defect) == "body malformed: now: missing"


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
        parser.parse_args(["bootstrap", "--ledger", "5"])


def test_cli_bootstrap_refuses_canonical_remote_mismatch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`bootstrap`'s parsed namespace has no `json` attribute at all (issue
    #199): the general `ClaimError` sink must read it defensively rather
    than inventing a default that would make `bootstrap` emit JSON it never
    offered -- stdout stays empty, exactly as before this command grew a
    `--json`-aware sink."""
    monkeypatch.setattr(github, "GitHubForge", lambda repository: FakeForge())
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: "/repo")
    _patch_store_write(monkeypatch)
    monkeypatch.setattr(checkout, "remote_url", lambda remote: "git@github.com:other/repo.git")

    status = issue_claim.main(["--repo", "example/agent-claim", "bootstrap"])

    captured = capsys.readouterr()
    assert status == 2
    assert (
        "forge target example/agent-claim does not match canonical remote other/repo; "
        "run aco from that repository's checkout"
    ) in captured.err
    assert "--ledger" not in captured.err
    assert captured.out == ""


def test_cli_rescope_refuses_a_missing_state_ref(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    git_values = _git_checkout()
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: "Codex Sol"})
    _patch_store_write(monkeypatch, tip=None)

    status = issue_claim.main(
        ["--repo", "example/agent-claim", "rescope", "72", "--add", "src/new.py"]
    )

    assert status == 2
    assert protocol.MISSING_STATE_REF in capsys.readouterr().err


def test_cli_release_refuses_a_missing_state_ref(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    standing = request("mine", "Ada", issue=72, branch="lane-72", scope=("src",))
    client = FakeForge()
    _patch_release_session(monkeypatch, client, standing)
    _patch_store_write(monkeypatch, tip=None)

    status = issue_claim.main(
        ["--repo", "example/agent-claim", "release", "72", "--abandoned", "stopped"]
    )

    assert status == 2
    assert protocol.MISSING_STATE_REF in capsys.readouterr().err


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
