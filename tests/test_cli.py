from __future__ import annotations

import argparse
import io
import json
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
    _patch_command,
    _real_git,
    _set_agent_identity_env,
    arrange_scope_width,
)
from github_fixtures import LANDING_BRANCH, WORK_ITEM_ISSUE

from agent_coordination import (
    __version__,
    board,
    checkout,
    forge,
    github,
    hook_input,
    items,
    protocol,
    store,
)
from agent_coordination import cli as issue_claim
from agent_coordination.cli import (
    ClaimError,
    ClaimRequest,
    ClaimUnavailableError,
    IssueIdentity,
    _status,
    _status_json,
)

GitHubForge = github.GitHubForge

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
    issue_references: dict[int, forge.ItemReference] = field(default_factory=dict)
    issue_reference_lookups: list[int] = field(default_factory=list)
    created_children: list[tuple[int, str, str, board.ItemKind]] = field(default_factory=list)
    created_issues: list[tuple[str, str, board.ItemKind]] = field(default_factory=list)
    linked_children: list[tuple[int, int]] = field(default_factory=list)
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

    def _create_issue(self, *, title: str, body: str, kind: board.ItemKind) -> int:
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

    def create_child(self, *, parent: int, title: str, body: str, kind: board.ItemKind) -> int:
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

    def _create_issue(self, *, title: str, body: str, kind: board.ItemKind) -> int:
        pytest.fail("a read-only command must never create an issue")

    def link_child(self, parent: int, child: int) -> None:
        pytest.fail("a read-only command must never link a child")

    def create_child(self, *, parent: int, title: str, body: str, kind: board.ItemKind) -> int:
        pytest.fail("a read-only command must never create a child")

    def update_item_body(self, number: int, body: str) -> None:
        pytest.fail("a read-only command must never update an item body")


_LIVE_FETCH_ISSUE_REFERENCE = issue_claim._fetch_issue_reference


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
    assert "no: body malformed: agent-claim: no agent-claim block" in rendered
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

    assert issue_claim.main(["--repo", "example/agent-claim", "board", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {
        "items",
        "ready_now",
        "stale",
        "recovery",
        "uncut",
        "requests",
        "landings_derivable",
    }
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


def _single_item_board_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> FakeForge:
    client = FakeForge()
    client.board_issues = (board_issue(10, "Plain item", complete_contract("Ship #10.")),)
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())
    _patch_store_write(monkeypatch)
    return client


def test_board_html_prints_the_rendered_page_to_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _single_item_board_environment(monkeypatch, tmp_path)

    assert issue_claim.main(["--repo", "example/agent-claim", "board", "--html"]) == 0
    rendered = capsys.readouterr().out
    assert "<title>agent-claim Board</title>" in rendered
    assert "#10 Plain item" in rendered


def test_board_html_path_writes_the_page_to_a_file_instead_of_stdout(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _single_item_board_environment(monkeypatch, tmp_path)
    output_path = tmp_path / "board.html"

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "board", "--html", str(output_path)]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == ""
    written = output_path.read_text(encoding="utf-8")
    assert "#10 Plain item" in written


def test_board_html_costs_no_gh_call_beyond_plain_board(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """Issue #276: `board --html` reshapes the exact reads `board` already
    performs -- `_board`'s own merged-pull-request fetch, not a second one --
    so its request count against the same fixture never exceeds plain
    `board`'s."""
    client = _single_item_board_environment(monkeypatch, tmp_path)

    assert issue_claim.main(["--repo", "example/agent-claim", "board"]) == 0
    capsys.readouterr()
    plain_requests = client.requests

    client.requests = 0
    assert issue_claim.main(["--repo", "example/agent-claim", "board", "--html"]) == 0
    capsys.readouterr()

    assert client.requests == plain_requests


def test_board_html_and_json_are_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _single_item_board_environment(monkeypatch, tmp_path)

    with pytest.raises(SystemExit):
        issue_claim.main(["--repo", "example/agent-claim", "board", "--html", "--json"])


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
    headers = [line for line in capsys.readouterr().out.splitlines() if line.startswith("#")]
    assert headers == [
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
    assert capsys.readouterr().out == "#400 1/1: Block-only expectations\n  1 open: Proposed\n"


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
    assert capsys.readouterr().out == (
        "#10 1/2: Open expectation\n"
        "  1 open: Open decision 0.\n"
        f"  2 ruled yes {RULED_ON.isoformat()}: Settled decision 0.\n"
    )

    assert issue_claim.main(["--repo", "example/agent-claim", "rulings", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == [
        {
            "number": 10,
            "title": "Open expectation",
            "open": 1,
            "total": 2,
            "lines": [
                {"index": 1, "text": "Open decision 0.", "state": "open"},
                {
                    "index": 2,
                    "text": "Settled decision 0.",
                    "state": f"ruled yes {RULED_ON.isoformat()}",
                },
            ],
        }
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
            (board_issue(10, "Blockless", "## Now\nInvestigate."),),
            {},
            (),
            ("next",),
            3,
            "No actionable item.\n\nSKIPPED\n"
            "#10: body malformed: agent-claim: no agent-claim block\n",
            id="names_a_body_with_no_block_as_malformed",
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
    "expectations unruled: refine before the pull\n"
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
        "expectations unruled: refine before the pull\n"
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
        "ruling_hint": "expectations unruled: refine before the pull",
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
        (
            CUT_CONTAINER,
            "Scheibe 1",
            issue_claim._cut_child_body(CUT_CONTAINER),
            board.ItemKind.TASK,
        )
    ]
    assert client.item_bodies == {}
    err = capsys.readouterr().err
    assert f"created #{child} but failed to record #{child} as a sub-issue" in err
    assert "re-run the same cut -- it adopts the child" in err


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
        (
            CUT_CONTAINER,
            "Scheibe 1",
            issue_claim._cut_child_body(CUT_CONTAINER),
            board.ItemKind.TASK,
        )
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
    assert "body malformed: agent-claim: no agent-claim block" in capsys.readouterr().err
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
        (
            CUT_CONTAINER,
            "Scheibe 1",
            issue_claim._cut_child_body(CUT_CONTAINER),
            board.ItemKind.TASK,
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
            board_issue(child_number, child_title, child_body, kind=board.ItemKind.TASK),
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
            "example/agent-claim",
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
        "container": CUT_CONTAINER,
        "row": 1,
        "child": 950,
        "adopted": True,
    }
    remaining = board.locate_agent_claim_block(client.item_bodies[CUT_CONTAINER]).data
    assert remaining["slice"] == []


def test_cut_refuses_to_adopt_a_closed_child_with_a_matching_title(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = _forge_with_existing_child(
        monkeypatch, tmp_path, child_number=950, child_state=board.ChildState.CLOSED
    )

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
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
        kind=board.ItemKind.TASK,
    )
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (_one_slice_container(), orphan))

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
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
                kind=board.ItemKind.TASK,
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
                kind=board.ItemKind.TASK,
            ),
            "idea",
            id="idea_labelled_issue",
        ),
        pytest.param(
            board_issue(
                CUT_CONTAINER,
                "Scheibe 1",
                issue_claim._cut_child_body(CUT_CONTAINER),
                kind=board.ItemKind.TASK,
            ),
            None,
            id="the_container_itself",
        ),
        pytest.param(
            board_issue(
                951, "Scheibe 1", issue_claim._cut_child_body(80), kind=board.ItemKind.TASK
            ),
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
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 0
    child = client.next_created_child_number - 1
    assert client.linked_children == [(CUT_CONTAINER, child)]
    assert client.created_children == [
        (
            CUT_CONTAINER,
            "Scheibe 1",
            issue_claim._cut_child_body(CUT_CONTAINER),
            board.ItemKind.TASK,
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
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert first_exit_code == 2
    child = client.next_created_child_number - 1
    expected_body = issue_claim._cut_child_body(CUT_CONTAINER)
    assert client.created_issues == [("Scheibe 1", expected_body, board.ItemKind.TASK)]
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
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert second_exit_code == 0
    assert client.created_issues == [("Scheibe 1", expected_body, board.ItemKind.TASK)]
    assert client.linked_children == [(CUT_CONTAINER, child), (CUT_CONTAINER, child)]
    remaining = board.locate_agent_claim_block(client.item_bodies[CUT_CONTAINER]).data
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
        ["--repo", "example/agent-claim", "rule", str(RULE_ITEM), "--line", "1", flag]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == f"RULED #{RULE_ITEM} line 1 {ruling}; 1 line(s) still open\n"
    lines = board.expectation_lines(client.item_bodies[RULE_ITEM])
    assert lines[0] == board.ExpectationLine(1, "Ship it?", ruling, RULE_TODAY)
    assert lines[1] == board.ExpectationLine(2, "Ship it too?", None, None)


def test_rule_json_reports_item_index_ruling_date_and_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Ship it?"\ndefault = "later"\n'
    _client_with_item(monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(toml_text))

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "rule", str(RULE_ITEM), "--line", "1", "--yes", "--json"]
    )

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == {
        "item": RULE_ITEM,
        "index": 1,
        "ruling": "yes",
        "ruled_on": RULE_TODAY.isoformat(),
        "open": 0,
    }


def test_rule_appends_a_note_to_the_ruled_line_via_cli(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Ship it?"\ndefault = "later"\n'
    client = _client_with_item(monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(toml_text))

    exit_code = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
    lines = board.expectation_lines(client.item_bodies[RULE_ITEM])
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
        ["--repo", "example/agent-claim", "rule", str(RULE_ITEM), "--line", "1", "--no"]
    )

    assert exit_code == 2
    assert "line 1 is already ruled" in capsys.readouterr().err
    assert client.item_bodies == {}


def test_rule_refuses_an_out_of_range_line_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Ship it?"\ndefault = "later"\n'
    client = _client_with_item(monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(toml_text))

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "rule", str(RULE_ITEM), "--line", "2", "--yes"]
    )

    assert exit_code == 2
    assert "out of range" in capsys.readouterr().err
    assert client.item_bodies == {}


def test_rule_refuses_when_the_forge_cannot_update_item_body(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Ship it?"\ndefault = "later"\n'
    client = _client_with_item(monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(toml_text))
    client.capability_overrides[forge.ForgeOperation.UPDATE_ITEM_BODY] = forge.Capability.READ_ONLY

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "rule", str(RULE_ITEM), "--line", "1", "--yes"]
    )

    assert exit_code == 2
    assert client.item_bodies == {}
    assert "ERROR: this forge cannot update_item_body; rule by hand" in capsys.readouterr().err


def test_rule_refuses_a_missing_item_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = _configured_board_client(monkeypatch, tmp_path)
    client.issue_references[RULE_ITEM] = forge.ItemReference(forge.ItemState.MISSING)

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "rule", str(RULE_ITEM), "--line", "1", "--yes"]
    )

    assert exit_code == 2
    assert client.item_bodies == {}
    assert f"#{RULE_ITEM} does not exist" in capsys.readouterr().err


def test_rule_refuses_a_pull_request_target_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Ship it?"\ndefault = "later"\n'
    client = _client_with_item(
        monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(toml_text), is_landing=True
    )

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "rule", str(RULE_ITEM), "--line", "1", "--yes"]
    )

    assert exit_code == 2
    assert client.item_bodies == {}
    assert (
        f"#{RULE_ITEM} is a pull request, not an issue; rule needs an issue"
        in capsys.readouterr().err
    )


def test_ask_appends_a_proposed_line_and_rulings_shows_it_as_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    toml_text = (
        f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Ship it?"\n'
        'ruling = "yes"\nruled_on = 2026-08-01\n'
    )
    client = _client_with_item(monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(toml_text))

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "ask", str(RULE_ITEM), "--text", "New question?"]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == f"ASKED #{RULE_ITEM} line 2: New question?\n"

    new_body = client.item_bodies[RULE_ITEM]
    monkeypatch.setattr(
        client,
        "list_open_board_issues",
        lambda: (board_issue(RULE_ITEM, "Decide something", new_body),),
    )

    assert issue_claim.main(["--repo", "example/agent-claim", "rulings"]) == 0
    assert capsys.readouterr().out == (
        f"#{RULE_ITEM} 1/2: Decide something\n"
        "  1 ruled yes 2026-08-01: Ship it?\n"
        "  2 open: New question?\n"
    )


def test_ask_json_reports_item_index_text_and_default(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    _client_with_item(monkeypatch, tmp_path, RULE_ITEM, agent_claim_body(MINIMAL_BLOCK_TOML))

    exit_code = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
    assert json.loads(capsys.readouterr().out) == {
        "item": RULE_ITEM,
        "index": 1,
        "text": "New question?",
        "default": "later",
    }


def test_ask_refuses_a_blockless_item_before_any_write(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = _client_with_item(monkeypatch, tmp_path, RULE_ITEM, "## Now\nOld prose.\n")

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "ask", str(RULE_ITEM), "--text", "New question?"]
    )

    assert exit_code == 2
    assert (
        "body malformed: agent-claim: no agent-claim block; ask needs a valid agent-claim block"
        in capsys.readouterr().err
    )
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

    projected = issue_claim._board(BoardClient(), ()).board

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
            issue_claim._cut_child_body(case.container_number),
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
        complete_contract(_DIFFERING_NEXT_LINE, slice=slice_entries("Scheibe I-top")),
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
        complete_contract("Scheibe C", slice=slice_entries("Scheibe C")),
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
        kind=board.ItemKind.CONTAINER,
        children_closed=2,
        children_total=2,
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    exit_code = issue_claim.main(["--repo", "example/agent-claim", "next"])

    assert exit_code == 0
    assert capsys.readouterr().out == (
        "close_container #187: Schließen, sobald die letzte Bedingung erfüllt ist.\n"
    )


def test_next_json_names_a_container_with_no_slice_row_by_its_own_next_line(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The JSON form of the same #208 case: `action` stays `close_container`
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
        kind=board.ItemKind.CONTAINER,
        children_closed=2,
        children_total=2,
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    exit_code = issue_claim.main(["--repo", "example/agent-claim", "next", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["action"] == "close_container"
    assert payload["number"] == 188
    assert payload["closed"] == 2
    assert payload["total"] == 2
    assert payload["next_step"] == "Schließen, sobald die letzte Bedingung erfüllt ist."
    assert "command" not in payload
    assert "cut_title" not in payload


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

    projected = issue_claim._board(BoardClient(), ()).board

    assert observed == [90]
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
    assert board.AGENT_CLAIM_FENCE_INFO == "agent-claim"
    assert board.BLOCK_CHILD_SKELETON.startswith(f"```{board.AGENT_CLAIM_FENCE_INFO}\n")
    assert board.CONFIG_PATH.as_posix() == ".agent-claim/board.toml"


def test_body_contract_checks_names_a_blockless_container_by_its_no_block_defect() -> None:
    body = "## Now\nOld prose.\n\n## Next\nDo the thing.\n"
    blockless = replace(
        board_issue(201, "Blockless container", body),
        kind=board.ItemKind.CONTAINER,
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
    monkeypatch.setattr(issue_claim, "_state_ref_forge", lambda _repo, _remote: stub)

    def unused(*_args: object, **_kwargs: object) -> None:
        pytest.fail("storage = state-ref must never build a GitHubForge")

    monkeypatch.setattr(github, "GitHubForge", unused)

    assert issue_claim.main(["board"]) == 0


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

    status = issue_claim.main(["--repo", "acme/items", "board"])

    assert status == 2
    assert capsys.readouterr().err == "ERROR: --repo is meaningless under storage = state-ref\n"


def test_release_merged_refuses_under_the_state_ref_pin(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """`release --merged` refuses under `storage = "state-ref"` (issue
    #283) before ever resolving a forge: `state_board.StateRefBoard` has no
    data for `landing` at all, unlike `cut`/`rule`/`ask`/`claim`, which now
    write and read state-ref items (issue #283's own proofs, `test_cut`,
    `test_rule_and_ask_write_a_state_ref_item`, `test_claim`). `--claim-id`
    keeps this test from reading this process's real current branch, which
    is empty under CI's detached-HEAD checkout."""
    _write_state_ref_pin(tmp_path)

    status = issue_claim.main(
        ["release", "10", "--merged", "12", "--agent", "Codex Sol", "--claim-id", "claim-1"]
    )

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: state-ref cannot verify a merged pull request yet (#230 slice 6); land "
        'offline with `item close` and `release --abandoned "landed as <sha>"` until then\n'
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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])

    status = issue_claim.main(
        ["--repo", "example/agent-claim", "rescope", "72", "--agent", "Ada", "--add", "src/new.py"]
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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])

    status = issue_claim.main(
        ["--repo", "example/agent-claim", "rescope", "72", "--agent", "Ada", "--add", "src/new.py"]
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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
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
# tests of trunk_landing_times itself (tests/test_checkout.py) call
# _LIVE_TRUNK_LANDING_TIMES.
@pytest.fixture(autouse=True)
def _stub_trunk_landing_times(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())


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

    def claim_ages(
        self,
        *,
        worktree: Path,
        tip: protocol.ObjectId,
        claims: Iterable[protocol.ActiveClaim],
    ) -> dict[str, datetime]:
        return {claim.claim_id: self._ages.get(claim.claim_id, _STATUS_NOW) for claim in claims}


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
    monkeypatch.setattr(store, "claim_ages", fake.claim_ages)
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

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["tip"] is None


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
        == json.dumps({"issue": None, "state": "UNCLAIMED", "tip": BASE, "claims": []}) + "\n"
    )


def test_cli_status_json_issue_with_no_claim_prints_unclaimed_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_store(monkeypatch)

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72", "--json"]) == 0
    assert (
        capsys.readouterr().out
        == json.dumps({"issue": 72, "state": "UNCLAIMED", "tip": BASE, "claims": []}) + "\n"
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
                "tip": BASE,
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
                "tip": BASE,
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
                "tip": BASE,
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
        exact_err="ERROR: scope is wide: 1 directory in scope (docs); pass --whole REASON\n",
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
        argv_tail=("--add", "docs"),
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
            "pass --whole REASON\n"
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
        argv_tail=("--add", "LICENSE", "--add", "README.md"),
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
        exact_err="ERROR: scope is wide: 4 paths exceeds three; pass --whole REASON\n",
    ),
    _ScopeWidthRefusal(
        id="rescope-widening-to-four-paths",
        command="rescope",
        argv_tail=("--add", "new_b.py", "--add", "new_c.py", "--add", "new_d.py"),
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
    assert standing.scope == ("src/widget.py", "docs")
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
        argv_tail=("--add", "docs", "--whole", "widen to the docs tree"),
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
        monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
        _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: "Codex Sol"})
        arrange_scope_width(
            monkeypatch,
            client,
            directories=directories,
            versioned=versioned,
            validate_checkout=False,
        )
        argv = ["--repo", "example/agent-claim", "rescope", "72", *argv_tail]
    elif command == "claim-lane":
        _set_agent_identity_env(monkeypatch, {issue_claim.ACO_AGENT_ENV: "Ada"})
        git_values = {("branch", "--show-current"): "docs/lane-cleanup"}
        monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
        arrange_scope_width(monkeypatch, client, directories=directories, versioned=versioned)
        argv = [
            "--repo",
            "example/agent-claim",
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
            "example/agent-claim",
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

    status = issue_claim.main(
        ["--repo", "example/agent-claim", "status", "--path", "docs/PRODUCT.md"]
    )

    assert status == 0
    assert "CLAIMED docs/PRODUCT.md issue #72: Ada (builder) claim=mine" in capsys.readouterr().out


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


@pytest.mark.parametrize(
    ("lines", "paths"),
    [
        (("*** Add File: src/new_module.py", "+content"), ("src/new_module.py",)),
        (("*** Delete File: src/old_module.py",), ("src/old_module.py",)),
        (
            ("*** Update File: src/widget.py", "@@", "-old", "+new"),
            ("src/widget.py",),
        ),
        (
            (
                "*** Update File: src/widget.py",
                "*** Move to: src/renamed.py",
                "@@",
                "-old",
                "+new",
            ),
            ("src/widget.py", "src/renamed.py"),
        ),
        (
            (
                "*** Update File: src/widget.py",
                "@@",
                "-old",
                "+new",
                "*** Add File: src/new_module.py",
                "+content",
            ),
            ("src/widget.py", "src/new_module.py"),
        ),
        pytest.param(
            (
                "*** Add File: src/ok.py",
                "+x",
                "  *** Update File: docs/evil.md",
                "@@",
                "-a",
                "+b",
            ),
            ("src/ok.py", "docs/evil.md"),
            id="indented-header-after-add-is-a-real-header",
        ),
        pytest.param(
            (
                "*** Add File: src/ok.py",
                "+x",
                "\t*** Update File: docs/evil.md",
                "@@",
                "-a",
                "+b",
            ),
            ("src/ok.py", "docs/evil.md"),
            id="tab-indented-header-is-a-real-header",
        ),
        pytest.param(
            ("  *** Add File: src/ok.py", "+x"),
            ("src/ok.py",),
            id="indented-first-header-is-a-real-header",
        ),
        pytest.param(
            (
                "*** Update File: src/widget.py",
                "@@",
                "-old",
                "+new",
                " *** Update File: docs/evil.md",
            ),
            ("src/widget.py",),
            id="leading-space-header-inside-an-update-hunk-is-context-not-a-file",
        ),
        pytest.param(
            ('*** Add File: "src/ok.py"', "+x"),
            ('"src/ok.py"',),
            id="a-quoted-path-is-extracted-literally",
        ),
        pytest.param(
            ("*** Add File: ../outside.md", "+x"),
            ("../outside.md",),
            id="a-traversal-path-is-extracted-literally",
        ),
        pytest.param(
            ("*** Add File: /etc/passwd", "+x"),
            ("/etc/passwd",),
            id="an-absolute-path-is-extracted-literally",
        ),
        pytest.param(
            ("*** Add File: src/ok.py  ", "+x"),
            ("src/ok.py",),
            id="trailing-spaces-outside-an-update-hunk-are-trimmed-like-codex",
        ),
    ],
)
def test_hook_patch_paths_extracts_every_file_line(
    lines: tuple[str, ...], paths: tuple[str, ...]
) -> None:
    assert hook_input.hook_patch_paths(_patch_command(*lines)) == paths


def test_hook_patch_paths_ignores_a_trailing_carriage_return_like_codex() -> None:
    text = (
        "*** Begin Patch\r\n"
        "*** Update File: src/widget.py\r\n"
        "@@\r\n"
        "-old\r\n"
        "+new\r\n"
        "*** End Patch\r\n"
    )
    assert hook_input.hook_patch_paths(text) == ("src/widget.py",)


@pytest.mark.parametrize(
    "text",
    [
        "*** Begin Patch\n*** End Patch",
        "not a patch at all",
        "",
        pytest.param(
            _patch_command("*** Add File: a.py", "+x")
            + "\n"
            + _patch_command("*** Add File: b.py", "+y"),
            id="two-begin-patch-blocks",
        ),
        pytest.param(
            _patch_command("*** Add File: a.py", "+x") + "\n*** Add File: b.py",
            id="a-file-line-after-end-patch",
        ),
        pytest.param(
            "*** Begin Patch\nbad\n*** End Patch",
            id="a-line-the-grammar-does-not-admit-outside-any-header",
        ),
    ],
)
def test_hook_patch_paths_returns_empty_for_unrecognized_text(text: str) -> None:
    assert hook_input.hook_patch_paths(text) == ()


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
        "ruled 10 landings ago: refine again at the pull\n"
    )

    assert issue_claim.main(["--repo", "example/agent-claim", "next", "--json"]) == 0
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

    assert _status_json((first, second), None, ages, None) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["state"] == "CONFLICT"


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


def test_check_refuses_a_blockless_parent_before_the_next_check(
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
        f"REFUSED: pull request #12 has parent {REPOSITORY}#{PARENT_ISSUE} with a body "
        "malformed: agent-claim: no agent-claim block\n"
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

    assert run_check(CHECKED_ISSUE) == 1
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


def body_check_main(*, extra: tuple[str, ...] = ()) -> int:
    return issue_claim.main(["body", "--check", *extra])


def _body_template_skeleton(kind: str) -> str:
    """The one skeleton `body --template --kind KIND` must print, read from
    its own owner rather than recomputed here (Value Ownership)."""
    return board.BLOCK_CONTAINER_SKELETON if kind == "container" else board.BLOCK_CHILD_SKELETON


@pytest.mark.parametrize("kind", issue_claim.BODY_TEMPLATE_KINDS)
def test_body_template_prints_the_one_skeleton_owner_for_its_kind(
    capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    assert issue_claim.main(["body", "--template", "--kind", kind]) == 0
    assert capsys.readouterr().out == _body_template_skeleton(kind)


def test_body_template_defaults_to_the_task_skeleton(capsys: pytest.CaptureFixture[str]) -> None:
    assert issue_claim.main(["body", "--template"]) == 0
    assert capsys.readouterr().out == board.BLOCK_CHILD_SKELETON


@pytest.mark.parametrize("kind", issue_claim.BODY_TEMPLATE_KINDS)
def test_body_template_prepends_the_parent_line_cut_writes_for_a_fresh_child(
    capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    assert issue_claim.main(["body", "--template", "--kind", kind, "--parent", "79"]) == 0
    assert capsys.readouterr().out == f"Parent: #79\n\n{_body_template_skeleton(kind)}"


@pytest.mark.parametrize("kind", issue_claim.BODY_TEMPLATE_KINDS)
def test_body_template_round_trips_through_body_check_for_every_kind(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], kind: str
) -> None:
    """The printed skeleton is a recognized, valid block for every kind --
    read back exactly as `check <item>` reads `cut`'s own fresh child
    (`test_check_names_the_sections_an_incomplete_body_leaves_empty`'s
    `a-fresh-skeleton` case): incomplete, never malformed."""
    assert issue_claim.main(["body", "--template", "--kind", kind]) == 0
    printed = capsys.readouterr().out

    monkeypatch.setattr(sys, "stdin", io.StringIO(printed))
    assert body_check_main() == 1
    assert capsys.readouterr().err == "body incomplete: Now, Next, Done when\n"


def test_body_check_accepts_a_complete_block_with_no_defects(
    capsys: pytest.CaptureFixture[str],
) -> None:
    body_file = io.StringIO(agent_claim_body(MINIMAL_BLOCK_TOML))
    with pytest.MonkeyPatch.context() as monkeypatch:
        monkeypatch.setattr(sys, "stdin", body_file)
        assert body_check_main() == 0
    assert capsys.readouterr().out == "body ok\n"


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
    assert body_check_main() == 1
    assert "unknown top-level key record" in capsys.readouterr().err

    _write_state_ref_pin(tmp_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(body))
    assert body_check_main() == 0
    assert capsys.readouterr().out == "body ok\n"


def test_body_check_names_a_body_with_no_recognized_block_as_malformed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("no block\n"))
    assert body_check_main() == 1
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
    assert body_check_main() == 1
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
    assert body_check_main() == 1
    assert capsys.readouterr().err == (
        "body malformed: now: now is required\nbody malformed: done_when: done_when is required\n"
    )

    monkeypatch.setattr(sys, "stdin", io.StringIO(agent_claim_body(toml_text)))
    assert body_check_main(extra=("--json",)) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "defects": [
            "body malformed: now: now is required",
            "body malformed: done_when: done_when is required",
        ],
    }


def test_body_check_json_carries_the_defect_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO("no block\n"))
    assert body_check_main(extra=("--json",)) == 1
    assert json.loads(capsys.readouterr().out) == {
        "ok": False,
        "defects": ["body malformed: agent-claim: no agent-claim block"],
    }


def test_body_check_json_reports_ok_with_an_empty_defect_list(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(agent_claim_body(MINIMAL_BLOCK_TOML)))
    assert body_check_main(extra=("--json",)) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "defects": []}


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


@pytest.mark.parametrize(
    "extra",
    [
        pytest.param(("--kind", "task"), id="kind"),
        pytest.param(("--parent", "1"), id="parent"),
    ],
)
def test_body_refuses_kind_or_parent_together_with_check(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], extra: tuple[str, ...]
) -> None:
    monkeypatch.setattr(sys, "stdin", io.StringIO(agent_claim_body(MINIMAL_BLOCK_TOML)))
    assert body_check_main(extra=extra) == 2
    assert "--kind and --parent apply only to --template, not --check" in capsys.readouterr().err


def test_body_refuses_json_together_with_template(capsys: pytest.CaptureFixture[str]) -> None:
    assert issue_claim.main(["body", "--template", "--json"]) == 2
    assert "--json applies only to --check, not --template" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param(["body"], id="neither-mode"),
        pytest.param(["body", "--template", "--check"], id="both-modes"),
    ],
)
def test_body_requires_exactly_one_mode(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as refused:
        issue_claim.main(argv)

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


def test_cli_bootstrap_ignores_repo_and_a_non_github_remote(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`bootstrap` is forge-free (issue #245): it never resolves a forge
    target, so `--repo` and a canonical remote naming a different, even a
    non-GitHub, repository are no error for it -- only `status`, `protect`,
    and a lane `claim`/`rescope`/`release` share that guarantee too; an
    issue `claim` or `board` still checks Erwartung 6."""
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: "/repo")
    monkeypatch.setattr(checkout, "remote_url", lambda remote: "git@gitlab.com:other/repo.git")
    monkeypatch.setattr(store, "bootstrap", lambda *, worktree, remote: BASE)

    def unused(*_args: object, **_kwargs: object) -> forge.RepositoryId:
        pytest.fail("bootstrap must never resolve a forge target")

    monkeypatch.setattr(github, "discover_repository", unused)

    status = issue_claim.main(["--repo", "example/agent-claim", "bootstrap"])

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
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: "/repo")

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
        lambda: ("LICENSE", "README.md", "pyproject.toml", "src/agent_coordination/__init__.py"),
    )
    monkeypatch.setattr(issue_claim, "datetime", FixedDateTime)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    git_values = {
        ("branch", "--show-current"): "docs/lane-cleanup",
        ("rev-parse", "--show-toplevel"): "/repo",
        ("rev-parse", "HEAD"): BASE,
        ("rev-parse", "--git-dir"): "/repo/.git/worktrees/lane-cleanup",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main",
    }
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
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

    rescoped = issue_claim.main(["rescope", "--add", "README.md"])
    assert rescoped == 0
    lane_key = protocol.claim_key(protocol.LaneIdentity(), "docs/lane-cleanup")
    assert store.fetch_state(worktree=Path("."), remote="origin").claims[lane_key].scope == (
        "docs",
        "README.md",
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
    monkeypatch.setattr(checkout, "remote_url", lambda remote: "file:///srv/git/agent-claim.git")

    def unused(*_args: object, **_kwargs: object) -> forge.RepositoryId:
        pytest.fail("board must refuse the host before ever calling discover_repository")

    monkeypatch.setattr(github, "discover_repository", unused)

    status = issue_claim.main(["board"])

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err == "ERROR: no forge adapter for host file\n"


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

    status = issue_claim.main(["--repo", "example/agent-claim", "brief", "258"])

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

    status = issue_claim.main(["--repo", "example/agent-claim", "brief", "258"])

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

    status = issue_claim.main(["--repo", "example/agent-claim", "brief", "258"])

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

    status = issue_claim.main(["--repo", "example/agent-claim", "brief", "258", "--json"])

    assert status == 0
    assert json.loads(capsys.readouterr().out) == {
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


def test_cli_brief_refuses_a_non_github_canonical_remote_by_host(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`brief` is a forge command through the same `_LazyForge` gate `board`
    uses (issue #245): a canonical remote on any host but GitHub refuses by
    that host's own name, before ever calling `discover_repository`/`gh` --
    the same refusal `board` gives for the same remote."""
    monkeypatch.setattr(checkout, "remote_url", lambda remote: "file:///srv/git/agent-claim.git")

    def unused(*_args: object, **_kwargs: object) -> forge.RepositoryId:
        pytest.fail("brief must refuse the host before ever calling discover_repository")

    monkeypatch.setattr(github, "discover_repository", unused)

    status = issue_claim.main(["brief", "258"])

    captured = capsys.readouterr()
    assert status == 2
    assert captured.err == "ERROR: no forge adapter for host file\n"


@pytest.mark.parametrize(
    ("value", "number"),
    [("aco-3f9a2c", 0x3F9A2C), ("#42", 42), ("42", 42)],
)
def test_parse_item_ref_accepts_every_reference_syntax(value: str, number: int) -> None:
    """Issue #285 proof 5: `aco-xxxxxx` (hex-decoded), `#n`, and the bare
    number `n` are all one item reference."""
    assert issue_claim._parse_item_ref(value) == number

    rescoped = issue_claim._parser().parse_args(["rescope", value, "--add", "src"])

    assert rescoped.issue == number


@pytest.mark.parametrize("value", ["foo", "aco-xyz", "#"])
def test_parse_item_ref_refuses_anything_else(value: str) -> None:
    """Issue #285 proof 5: anything that is none of the three forms refuses
    by name rather than guessing."""
    with pytest.raises(ClaimUnavailableError, match="is not an item reference"):
        issue_claim._parse_item_ref(value)


def test_main_refuses_a_malformed_item_reference_before_ever_dispatching(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`_parse_item_ref` runs as an argparse `type=` inside `parse_args`, so
    its refusal must reach `main`'s own error rendering rather than an
    argparse usage error or an unhandled exception (issue #285)."""
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
    assert capsys.readouterr().out == f"{expected_id} · #42 · open · parent none\nBody text.\n"


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
        "item": items.format_item_id(42),
        "number": 42,
        "state": "open",
        "parent": None,
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
