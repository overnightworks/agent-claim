from __future__ import annotations

import io
import json
import os
import re
import runpy
import shlex
import subprocess
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

import pytest

from agent_claim import __version__, board, checkout, discovery, forge, github, process, protocol
from agent_claim import cli as issue_claim
from agent_claim.cli import (
    MAX_COMMENT_BYTES,
    ActiveClaim,
    ClaimantRelease,
    ClaimError,
    ClaimRequest,
    ClaimUnavailableError,
    DuplicateClaimConflictError,
    DuplicateClaimRepair,
    InvalidClaimMarkerError,
    IssueComment,
    IssueIdentity,
    LaneIdentity,
    LedgerSupersede,
    LedgerSupersededError,
    _status,
    acquire_claim,
    active_claims,
    claim_comment,
    claim_label,
    claims_conflict,
    claims_holding_path,
    is_protocol_candidate,
    parse_claim_event,
    reconcile_all_labels,
    reconcile_issue_label,
    release_claim,
    release_comment,
    repair_duplicate_claims,
    rescope_claim,
    supersede_comment,
    supersede_ledger,
)

issue_claim.configure_ledger(71)
LEDGER_ISSUE = 71
GitHubForge = github.GitHubForge

_LIVE_VERSIONED_PATHS = checkout.versioned_paths
_LIVE_TRUNK_LANDING_TIMES = checkout.trunk_landing_times
_LIVE_FETCH_ISSUE_REFERENCE = issue_claim._fetch_issue_reference

BASE = "a" * 40
REPOSITORY = "example/agent-claim"
LANDED = protocol.MergedRelease(12)


def ledger_item(
    number: int,
    *,
    body: str = issue_claim.LEDGER_BODY_MARKER,
    state: forge.ItemState = forge.ItemState.OPEN,
    locked: bool = True,
    author_is_trusted: bool = True,
    is_landing: bool = False,
) -> forge.LedgerItem:
    return forge.LedgerItem(number, state, locked, body, author_is_trusted, is_landing)


@pytest.mark.parametrize("bad_issue", [0, -1, True])
def test_configure_ledger_requires_a_positive_integer(bad_issue: int) -> None:
    with pytest.raises(ClaimError, match="ledger issue must be a positive integer"):
        protocol.configure_ledger(bad_issue)


@pytest.mark.parametrize("bad_issue", [0, -1, True])
def test_issue_identity_requires_a_positive_integer(bad_issue: int) -> None:
    with pytest.raises(ClaimError, match="issue identity must be a positive integer"):
        protocol.IssueIdentity(bad_issue)


def test_discovery_requires_a_locked_canonical_marker() -> None:
    client = FakeForge(ledger_items=[ledger_item(9), ledger_item(10)])
    assert issue_claim.discover_ledger(client) == 9

    unlocked = FakeForge(ledger_items=[ledger_item(2, locked=False)])
    with pytest.raises(ClaimUnavailableError, match="not locked"):
        issue_claim.discover_ledger(unlocked)


def test_discovery_refuses_other_machine_coordination_contract() -> None:
    client = FakeForge(ledger_items=[ledger_item(4, body="<!-- another-claim-ledger:v1 -->")])
    with pytest.raises(ClaimError, match="refusing to compete"):
        issue_claim.discover_ledger(client)


def test_untrusted_exact_and_arbitrary_markers_have_no_authority() -> None:
    client = FakeForge(
        ledger_items=[
            ledger_item(1, author_is_trusted=False),
            ledger_item(2),
            ledger_item(3, body="<!-- arbitrary-claim-ledger:v1 -->", author_is_trusted=False),
        ]
    )

    assert issue_claim.discover_ledger(client) == 2
    assert issue_claim.bootstrap_ledger(client) == 2
    states_by_number = {item.number: item.state for item in client.ledger_items}
    assert states_by_number[1] is forge.ItemState.OPEN
    assert states_by_number[3] is forge.ItemState.OPEN


def test_bootstrap_repairs_trusted_legacy_marker_and_closes_later_duplicate() -> None:
    client = FakeForge(ledger_items=[ledger_item(2, locked=False), ledger_item(3)])

    assert issue_claim.bootstrap_ledger(client) == 2
    items_by_number = {item.number: item for item in client.ledger_items}
    assert items_by_number[2].locked
    assert items_by_number[3].state is forge.ItemState.CLOSED
    assert 2 in client.ledger_labelled_issues
    assert issue_claim.LEDGER_LABEL in client.other_labels
    assert claim_label(2) in client.other_labels


def test_bootstrap_creates_and_locks_a_ledger_when_none_exists() -> None:
    client = FakeForge()

    created = issue_claim.bootstrap_ledger(client)

    assert created > 0
    items_by_number = {item.number: item for item in client.ledger_items}
    assert items_by_number[created].locked
    assert items_by_number[created].body.startswith(issue_claim.LEDGER_BODY_MARKER)


def test_bootstrap_ignores_an_untrusted_unlocked_marker() -> None:
    client = FakeForge(
        ledger_items=[ledger_item(1, locked=False, author_is_trusted=False), ledger_item(2)]
    )
    assert issue_claim.discover_ledger(client) == 2
    assert issue_claim.bootstrap_ledger(client) == 2
    items_by_number = {item.number: item for item in client.ledger_items}
    assert not items_by_number[1].locked


def test_bootstrap_refuses_other_machine_coordination_contract() -> None:
    client = FakeForge(ledger_items=[ledger_item(4, body="<!-- another-claim-ledger:v1 -->")])
    with pytest.raises(ClaimError) as excinfo:
        issue_claim.bootstrap_ledger(client)
    assert str(excinfo.value) == (
        "another coordination contract exists on issue(s) [4]; refusing to compete"
    )
    assert len(client.ledger_items) == 1


def test_discovery_finds_a_labelled_ledger_without_scanning_open_issues() -> None:
    """A labelled ledger answers from one atomic, label-filtered request;
    discovery must never fall back to scanning every open issue for it."""
    client = FakeForge(
        ledger_items=[ledger_item(2)],
        item_labels={2: frozenset({issue_claim.LEDGER_LABEL})},
    )

    assert issue_claim.discover_ledger(client) == 2
    assert len(client.list_items_calls) == 1


def test_discovery_finds_the_ledger_from_open_issues_without_full_history() -> None:
    """A repository with a huge closed-issue history must not pay for it:
    discovery resolves from the open-issue snapshot alone, never a scan that
    also reads closed issues."""
    client = FakeForge(
        ledger_items=[
            ledger_item(2),
            *(ledger_item(number, state=forge.ItemState.CLOSED) for number in range(100, 120)),
        ]
    )

    assert issue_claim.discover_ledger(client) == 2
    assert all(state is forge.ItemState.OPEN for state, _label in client.list_items_calls)


def test_discovery_reports_a_genuine_absence_when_the_open_count_is_stable() -> None:
    client = FakeForge()
    assert issue_claim.discover_ledger(client) is None


def test_discovery_refuses_to_report_absence_after_an_inconsistent_fetch() -> None:
    """Zero markers in a snapshot whose issue count already moved on is not
    proof of absence; it must fail loud instead of inviting `bootstrap`,
    which would create a second, competing ledger."""
    client = FakeForge(live_open_item_count=1)

    with pytest.raises(ClaimError, match="incomplete") as excinfo:
        issue_claim.discover_ledger(client)
    assert "run agent-claim bootstrap" not in str(excinfo.value)


def test_discovery_refuses_absence_over_a_multi_page_fallback_scan() -> None:
    """A page-boundary shift could hide an unlabelled ledger even when the
    live open-issue count happens to match; a listing the adapter itself
    reports as spanning more than one page can never prove absence, so this
    must fail loud regardless of what the counts say."""
    client = FakeForge(ledger_pages_fetched=2)

    with pytest.raises(ClaimError, match="could not establish ledger absence") as excinfo:
        issue_claim.discover_ledger(client)
    assert "run agent-claim bootstrap" not in str(excinfo.value)


def test_discovery_reports_absence_after_a_single_page_fallback_scan() -> None:
    client = FakeForge(ledger_pages_fetched=1)
    assert issue_claim.discover_ledger(client) is None


def test_discovery_fetch_failure_propagates_loudly_without_bootstrap_advice() -> None:
    client = FakeForge(list_items_error=ClaimError("GitHub issue coordination failed with exit 1"))

    with pytest.raises(ClaimError) as excinfo:
        issue_claim.discover_ledger(client)
    assert "bootstrap" not in str(excinfo.value)


def test_discovery_ignores_a_landing_pull_request_carrying_the_ledger_marker() -> None:
    """A merged/landing pull request can carry the same first-line marker as a
    ledger issue but must never be mistaken for one."""
    client = FakeForge(ledger_items=[ledger_item(3, is_landing=True), ledger_item(5)])
    assert issue_claim.discover_ledger(client) == 5


def test_bootstrap_fails_loud_when_the_created_ledger_does_not_reappear() -> None:
    """An eventual-consistency gap -- the freshly created ledger issue not yet
    visible to the very next listing -- must fail loud rather than claim a
    trusted candidate that was never actually observed."""
    client = _VanishingLedgerForge()
    with pytest.raises(ClaimError, match="did not expose a trusted ledger candidate"):
        issue_claim.bootstrap_ledger(client)


def comment(
    identifier: int,
    body: str,
    *,
    created_at: str | None = None,
    association: str = "OWNER",
) -> IssueComment:
    return IssueComment(
        identifier=identifier,
        created_at=created_at or f"2026-08-21T00:00:{identifier:02d}Z",
        updated_at=created_at or f"2026-08-21T00:00:{identifier:02d}Z",
        body=body,
        author_association=association,
        url=f"https://github.com/example/agent-claim/issues/71#issuecomment-{identifier}",
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


def rescope_request(
    identity: protocol.ClaimIdentity,
    agent: str,
    add: tuple[str, ...],
    drop: tuple[str, ...],
    claim_id: str | None,
    *,
    branch: str | None = None,
    whole_reason: str | None = None,
) -> protocol.RescopeRequest:
    return protocol.RescopeRequest(
        identity=identity,
        agent=agent,
        add=add,
        drop=drop,
        claim_id=claim_id,
        branch=branch,
        whole_reason=whole_reason,
    )


def release_context(
    identity: protocol.ClaimIdentity,
    agent: str,
    role: str | None,
    outcome: protocol.ReleaseOutcome,
    claim_id: str | None,
    *,
    branch: str | None = None,
    coordinator_override: bool = False,
) -> protocol.ReleaseContext:
    return protocol.ReleaseContext(
        identity=identity,
        agent=agent,
        role=role,
        outcome=outcome,
        claim_id=claim_id,
        branch=branch,
        coordinator_override=coordinator_override,
    )


def supersede_request(
    successor_issue: int,
    agent: str,
    role: str,
    reason: str,
    claim_id: str,
) -> protocol.SupersedeRequest:
    return protocol.SupersedeRequest(
        successor_issue=successor_issue,
        agent=agent,
        role=role,
        reason=reason,
        claim_id=claim_id,
    )


def projected_board(
    issues: tuple[board.Issue, ...],
    open_pull_requests: tuple[board.PullRequest, ...],
    recent_merged_pull_requests: tuple[board.PullRequest, ...],
    claims: tuple[ActiveClaim, ...],
    config: board.BoardConfig,
    *,
    repository: str = REPOSITORY,
    blocker_references: tuple[board.BlockerReference, ...] | None = None,
    now: datetime | None = None,
    trunk_landings: tuple[datetime, ...] = (),
    children: Mapping[int, tuple[board.ChildItem, ...]] = MappingProxyType({}),
) -> board.Board:
    """`board.build_board` for scenarios that do not turn on which repository is projected."""
    return board.build_board(
        board.BoardBuildInputs(
            issues=issues,
            open_pull_requests=open_pull_requests,
            recent_merged_pull_requests=recent_merged_pull_requests,
            claims=claims,
            config=config,
            repository=repository,
            blocker_references=blocker_references,
            now=now,
            trunk_landings=trunk_landings,
            children=children,
        )
    )


def _claims_client(*standing: ClaimRequest) -> FakeForge:
    return FakeForge(
        {
            LEDGER_ISSUE: [
                comment(index, claim_comment(claimed))
                for index, claimed in enumerate(standing, start=1)
            ]
        }
    )


@dataclass
class FakeForge:
    comments: dict[int, list[IssueComment]] = field(default_factory=dict)
    labels: set[int] = field(default_factory=set)
    other_labels: dict[str, set[int]] = field(default_factory=dict)
    ledger_labelled_issues: set[int] = field(default_factory=set)
    valid_successors: set[int] = field(default_factory=set)
    inject_before_next_ledger_post: IssueComment | None = None
    inject_after_next_ledger_post: IssueComment | None = None
    inject_during_next_add: IssueComment | None = None
    inject_during_next_remove: IssueComment | None = None
    fail_add_label: bool = False
    fail_remove_label: bool = False
    # 1-indexed: the Nth post_comment(LEDGER_ISSUE, ...) call raises instead of
    # posting -- simulates a compensating repair write itself failing (#136).
    fail_ledger_post_at_call: int | None = None
    ledger_post_call_count: int = field(default=0, init=False)
    board_issues: tuple[board.Issue, ...] = ()
    board_open_pull_requests: tuple[board.PullRequest, ...] = ()
    board_merged_pull_requests: tuple[board.PullRequest, ...] = ()
    board_blocker_references: tuple[board.BlockerReference, ...] | None = None
    repository: forge.RepositoryId = field(
        default_factory=lambda: github._repository_id(REPOSITORY)
    )
    default_branch_name: str = "main"
    landings: dict[int, forge.Landing] = field(default_factory=dict)
    parents: dict[int, board.ParentIssue] = field(default_factory=dict)
    children: dict[int, tuple[board.ChildItem, ...]] = field(default_factory=dict)
    closed_issues: set[int] = field(default_factory=set)
    issue_reference_lookups: list[int] = field(default_factory=list)
    ledger_items: list[forge.LedgerItem] = field(default_factory=list)
    live_open_item_count: int | None = None
    ledger_pages_fetched: int = 1
    item_labels: dict[int, frozenset[str]] = field(default_factory=dict)
    list_items_calls: list[tuple[forge.ItemState | None, str | None]] = field(default_factory=list)
    list_items_error: Exception | None = None
    created_children: list[tuple[int, str, str, board.ItemKind]] = field(default_factory=list)
    next_created_child_number: int = 900
    item_bodies: dict[int, str] = field(default_factory=dict)
    fail_update_item_body: bool = False
    fail_create_child_relation: bool = False
    capability_overrides: dict[forge.ForgeOperation, forge.Capability] = field(default_factory=dict)

    def capability(self, operation: forge.ForgeOperation) -> forge.Capability:
        return self.capability_overrides.get(operation, github.GITHUB_CAPABILITIES[operation])

    def list_items(
        self, *, state: forge.ItemState | None = None, label: str | None = None
    ) -> forge.Listing:
        self.list_items_calls.append((state, label))
        if self.list_items_error is not None:
            raise self.list_items_error
        items = tuple(
            item
            for item in self.ledger_items
            if (state is None or item.state is state)
            and (label is None or label in self.item_labels.get(item.number, frozenset()))
        )
        return forge.Listing(items, self.ledger_pages_fetched)

    def open_item_count(self) -> int:
        if self.live_open_item_count is not None:
            return self.live_open_item_count
        return sum(1 for item in self.ledger_items if item.state is forge.ItemState.OPEN)

    def ensure_label(self, name: str, *, colour: str, description: str) -> None:
        self.other_labels.setdefault(name, set())

    def create_item(self, *, title: str, body: str) -> int:
        number = max((item.number for item in self.ledger_items), default=0) + 1
        self.ledger_items.append(
            forge.LedgerItem(number, forge.ItemState.OPEN, False, body, True, False)
        )
        return number

    def lock_item(self, number: int) -> None:
        self.ledger_items = [
            replace(item, locked=True) if item.number == number else item
            for item in self.ledger_items
        ]

    def close_item(self, number: int) -> None:
        self.ledger_items = [
            replace(item, state=forge.ItemState.CLOSED) if item.number == number else item
            for item in self.ledger_items
        ]

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

    def list_protocol_candidates(self, issue: int) -> tuple[IssueComment, ...]:
        return tuple(
            entry for entry in self.comments.get(issue, []) if protocol.is_protocol_candidate(entry)
        )

    def post_comment(self, issue: int, body: str) -> str:
        if issue == protocol.LEDGER_ISSUE:
            self.ledger_post_call_count += 1
            if self.ledger_post_call_count == self.fail_ledger_post_at_call:
                raise ClaimError("ledger post failed (simulated)")
        if issue == protocol.LEDGER_ISSUE and self.inject_before_next_ledger_post is not None:
            self.comments.setdefault(protocol.LEDGER_ISSUE, []).append(
                self.inject_before_next_ledger_post
            )
            self.inject_before_next_ledger_post = None
        identifier = (
            max(
                (entry.identifier for entries in self.comments.values() for entry in entries),
                default=0,
            )
            + 1
        )
        posted = comment(identifier, body)
        self.comments.setdefault(issue, []).append(posted)
        if issue == protocol.LEDGER_ISSUE and self.inject_after_next_ledger_post is not None:
            self.comments.setdefault(protocol.LEDGER_ISSUE, []).append(
                self.inject_after_next_ledger_post
            )
            self.inject_after_next_ledger_post = None
        return posted.url

    def add_label(self, issue: int, label: str) -> None:
        if label == protocol.LEDGER_LABEL:
            self.ledger_labelled_issues.add(issue)
            return
        assert label == claim_label()
        if self.fail_add_label:
            raise ClaimError("label add failed")
        if self.inject_during_next_add is not None:
            self.comments.setdefault(protocol.LEDGER_ISSUE, []).append(self.inject_during_next_add)
            self.inject_during_next_add = None
        self.labels.add(issue)

    def remove_label(self, issue: int, label: str) -> None:
        assert label == claim_label()
        if self.fail_remove_label:
            raise ClaimError("label remove failed")
        if self.inject_during_next_remove is not None:
            self.comments.setdefault(protocol.LEDGER_ISSUE, []).append(
                self.inject_during_next_remove
            )
            injected = self.inject_during_next_remove_event
            assert not isinstance(injected, protocol.LedgerSupersede), (
                "this fake only injects issue-scoped claim events"
            )
            assert isinstance(injected.identity, protocol.IssueIdentity), (
                "this fake only injects issue-scoped claim events"
            )
            self.labels.add(injected.identity.issue)
            self.inject_during_next_remove = None
        self.labels.discard(issue)

    @property
    def inject_during_next_remove_event(self) -> protocol.ClaimEvent:
        assert self.inject_during_next_remove is not None
        event = parse_claim_event(self.inject_during_next_remove)
        assert event is not None
        return event

    def list_claimed_issues(self) -> tuple[int, ...]:
        return tuple(sorted(self.labels))

    def list_open_board_issues(self) -> tuple[board.Issue, ...]:
        return self.board_issues

    def landing(self, number: int) -> forge.Landing:
        detail = self.landings.get(number)
        if detail is None:
            raise ClaimError(f"GitHub has no pull request #{number}")
        return detail

    def item_reference(self, number: int) -> forge.ItemReference:
        self.issue_reference_lookups.append(number)
        state = forge.ItemState.CLOSED if number in self.closed_issues else forge.ItemState.OPEN
        return forge.ItemReference(state, "", "")

    def default_branch(self) -> str:
        return self.default_branch_name

    def parent_issue(self, number: int) -> board.ParentIssue | None:
        return self.parents.get(number)

    def list_children(self, number: int) -> tuple[board.ChildItem, ...]:
        return self.children.get(number, ())

    def list_board_blockers(self, numbers: frozenset[int]) -> tuple[board.BlockerReference, ...]:
        if self.board_blocker_references is not None:
            return self.board_blocker_references
        pull_request_numbers = {
            pull_request.number for pull_request in self.list_open_board_pull_requests()
        }
        return tuple(
            board.BlockerReference(
                number,
                board.BlockerState.OPEN,
                number in pull_request_numbers,
            )
            for number in sorted(numbers)
        )

    def list_open_board_pull_requests(self) -> tuple[board.PullRequest, ...]:
        return self.board_open_pull_requests

    def list_recent_merged_board_pull_requests(
        self, since: datetime
    ) -> tuple[board.PullRequest, ...]:
        return self.board_merged_pull_requests

    def validate_successor(self, issue: int) -> None:
        if issue not in self.valid_successors:
            raise ClaimUnavailableError(
                f"successor #{issue} must be an open, empty, collaborator-locked issue"
            )

    def upsert_projection(
        self,
        issue: int,
        body: str,
        *,
        create: bool = True,
        adopt_stale: bool = False,
    ) -> bool:
        entries = self.comments.setdefault(issue, [])
        all_projections = [
            entry
            for entry in entries
            if issue_claim.PROJECTION_MARKER_PATTERN.fullmatch(entry.body.partition("\n")[0])
            is not None
        ]
        projections = [
            entry
            for entry in all_projections
            if entry.body.partition("\n")[0] == issue_claim._projection_marker()
        ]
        adoptable_projections = [
            entry
            for entry in all_projections
            if (issue_claim._projection_ledger(entry) or 0) <= protocol.LEDGER_ISSUE
        ]
        has_newer_projection = any(
            (issue_claim._projection_ledger(entry) or 0) > protocol.LEDGER_ISSUE
            for entry in all_projections
        )
        if adopt_stale and adoptable_projections:
            projections = adoptable_projections
        if not projections:
            if has_newer_projection:
                raise ClaimError("owning issue has a projection from a newer ledger generation")
            if not create:
                return False
            self.post_comment(issue, body)
            projections = [self.comments[issue][-1]]
        owner, *duplicates = sorted(
            projections,
            key=lambda entry: (entry.created_at, entry.identifier),
        )
        owner_index = entries.index(owner)
        entries[owner_index] = replace(owner, body=body, updated_at=owner.created_at)
        duplicate_ids = {entry.identifier for entry in duplicates}
        entries[:] = [entry for entry in entries if entry.identifier not in duplicate_ids]
        return True

    def neutralize_claim_comment(self, comment_id: int, body: str) -> None:
        for entries in self.comments.values():
            for index, entry in enumerate(entries):
                if entry.identifier == comment_id:
                    # A real PATCH bumps updated_at; mirror that so the "was edited
                    # after publication" guard stays live for anything a caller
                    # neutralizes without also stripping its claim marker prefix.
                    edited_at = f"2026-08-22T00:00:{entry.identifier:02d}Z"
                    entries[index] = replace(entry, body=body, updated_at=edited_at)
                    return
        raise ClaimError(f"comment {comment_id} not found for neutralization")


class ReaderOnlyForge(FakeForge):
    """A `FakeForge` whose write operations fail the test instead of quietly
    succeeding -- the enforcement that a read-only command never writes,
    independent of the `ForgeReader`/`ForgeWriter` annotations (documentation
    only; nothing type-checks in CI)."""

    def post_comment(self, issue: int, body: str) -> str:
        pytest.fail("a read-only command must never post a comment")

    def add_label(self, issue: int, label: str) -> None:
        pytest.fail("a read-only command must never add a label")

    def remove_label(self, issue: int, label: str) -> None:
        pytest.fail("a read-only command must never remove a label")

    def upsert_projection(
        self,
        issue: int,
        body: str,
        *,
        create: bool = True,
        adopt_stale: bool = False,
    ) -> bool:
        pytest.fail("a read-only command must never upsert a projection")

    def neutralize_claim_comment(self, comment_id: int, body: str) -> None:
        pytest.fail("a read-only command must never neutralize a comment")

    def ensure_label(self, name: str, *, colour: str, description: str) -> None:
        pytest.fail("a read-only command must never ensure a label")

    def create_item(self, *, title: str, body: str) -> int:
        pytest.fail("a read-only command must never create an item")

    def lock_item(self, number: int) -> None:
        pytest.fail("a read-only command must never lock an item")

    def close_item(self, number: int) -> None:
        pytest.fail("a read-only command must never close an item")

    def create_child(self, *, parent: int, title: str, body: str, kind: board.ItemKind) -> int:
        pytest.fail("a read-only command must never create a child")

    def update_item_body(self, number: int, body: str) -> None:
        pytest.fail("a read-only command must never update an item body")


class _VanishingLedgerForge(FakeForge):
    """A `FakeForge` whose freshly created ledger issue never reappears in a
    following `list_items()` -- simulating a read-after-write consistency gap
    on the forge side."""

    def create_item(self, *, title: str, body: str) -> int:
        number = super().create_item(title=title, body=body)
        self.ledger_items = [item for item in self.ledger_items if item.number != number]
        return number


def test_forge_operation_exhaustiveness_matches_the_declared_reader_and_writer_methods() -> None:
    """Every `ForgeOperation` member names a `ForgeReader`/`ForgeWriter` method and
    nothing else; the count is pinned so a new operation cannot be added without
    its enum member, its capability entry, and this count bump."""
    declared_methods = {
        name
        for name in dir(forge.ForgeWriter)
        if not name.startswith("_") and name not in {"repository", "capability"}
    }
    assert {operation.value for operation in forge.ForgeOperation} == declared_methods
    assert len(forge.ForgeOperation) == 25
    assert set(github.GITHUB_CAPABILITIES) == set(forge.ForgeOperation)
    assert forge.Capability.UNSUPPORTED not in github.GITHUB_CAPABILITIES.values()


def test_only_the_github_adapter_speaks_gh_argv() -> None:
    """Source check, not a runtime guarantee: import-linter's module contract
    cannot see an argv string, so "only the adapter speaks `gh`" is proven by
    grepping every other module for the literal command name instead. Every
    module in the package is enumerated, not a fixed list, so a new module
    is covered the day it is added."""
    package = Path("src/agent_claim")
    other_modules = sorted(p for p in package.glob("*.py") if p.name != "github.py")
    assert len(other_modules) >= 7
    for module in other_modules:
        assert '"gh"' not in module.read_text(), f"{module.name} must not construct a gh argv"


def test_github_adapter_lists_items_with_state_and_label_filters() -> None:
    observed: list[list[str]] = []

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append(arguments)
        return json.dumps(
            {
                "number": 9,
                "state": "open",
                "locked": True,
                "body": issue_claim.LEDGER_BODY_MARKER,
                "author_association": "OWNER",
                "is_landing": False,
            }
        )

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    listing = client.list_items(state=forge.ItemState.OPEN, label=issue_claim.LEDGER_LABEL)

    assert listing == forge.Listing(
        (
            forge.LedgerItem(
                9, forge.ItemState.OPEN, True, issue_claim.LEDGER_BODY_MARKER, True, False
            ),
        ),
        1,
    )
    assert observed == [
        [
            "api",
            f"repos/{REPOSITORY}/issues?state=open&labels={issue_claim.LEDGER_LABEL}"
            "&per_page=100&page=1",
            "--jq",
            '.[] | {number,state,locked,body,author_association,is_landing:has("pull_request")}',
        ]
    ]


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(json.dumps("not-a-dict"), id="not-a-dict"),
        pytest.param(json.dumps({"number": True}), id="number-is-a-bool"),
        pytest.param(json.dumps({"number": 9, "state": "unknown"}), id="unknown-state"),
        pytest.param(
            json.dumps(
                {
                    "number": 9,
                    "state": "open",
                    "locked": "yes",
                    "body": "b",
                    "author_association": "OWNER",
                    "is_landing": False,
                }
            ),
            id="locked-not-a-bool",
        ),
    ],
)
def test_github_adapter_fails_loud_on_a_malformed_ledger_item(raw: str) -> None:
    client = GitHubForge(github._repository_id(REPOSITORY), run=lambda arguments: raw)

    with pytest.raises(ClaimError, match="malformed ledger issue"):
        client.list_items()


@pytest.mark.parametrize(
    ("total_items", "expected_pages_fetched", "expect_absence_confirmed"),
    [
        pytest.param(99, 1, True, id="99-items-one-page-confirms-absence"),
        pytest.param(100, 2, False, id="100-items-exact-multiple-still-costs-a-second-page"),
        pytest.param(101, 2, False, id="101-items-two-pages-cannot-confirm-absence"),
    ],
)
def test_github_adapter_reports_truthful_pages_fetched_and_discovery_decides_on_it(
    total_items: int, expected_pages_fetched: int, expect_absence_confirmed: bool
) -> None:
    """`pages_fetched` must never lie at an exact per-page multiple: 100 items
    still cost a second, empty page to prove nothing follows, and discovery's
    absence decision rests on exactly that count, not on a derived guess."""

    def ordinary_row(number: int) -> dict[str, object]:
        return {
            "number": number,
            "state": "open",
            "locked": False,
            "body": "ordinary open issue",
            "author_association": "OWNER",
            "is_landing": False,
        }

    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        endpoint = arguments[1]
        if "/issues?" not in endpoint:
            return str(total_items)
        if "labels=" in endpoint:
            return ""
        page = int(endpoint.rsplit("page=", 1)[1])
        start = (page - 1) * github.ISSUES_PER_PAGE
        end = min(start + github.ISSUES_PER_PAGE, total_items)
        rows = [ordinary_row(number) for number in range(start + 1, end + 1)]
        return "\n".join(json.dumps(row) for row in rows)

    client = GitHubForge(github._repository_id(REPOSITORY), run=run)

    listing = client.list_items(state=forge.ItemState.OPEN)

    assert listing.pages_fetched == expected_pages_fetched
    assert len(listing.items) == total_items

    if expect_absence_confirmed:
        assert issue_claim.discover_ledger(client) is None
    else:
        with pytest.raises(ClaimError, match="could not establish ledger absence"):
            issue_claim.discover_ledger(client)


def test_github_adapter_reads_the_open_item_count() -> None:
    observed: list[list[str]] = []

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append(arguments)
        return "7"

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    assert client.open_item_count() == 7
    assert observed == [["api", f"repos/{REPOSITORY}", "--jq", ".open_issues_count"]]


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param("not-a-number", id="unparsable"),
        pytest.param("-1", id="negative"),
    ],
)
def test_github_adapter_fails_loud_on_a_malformed_open_item_count(raw: str) -> None:
    client = GitHubForge(github._repository_id(REPOSITORY), run=lambda arguments: raw)

    with pytest.raises(ClaimError, match="malformed open-issue count"):
        client.open_item_count()


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


def test_github_adapter_ensures_a_label_definition() -> None:
    observed: list[list[str]] = []

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append(arguments)
        return ""

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    client.ensure_label("agent-claim:ledger", colour="6f42c1", description="canonical ledger")

    assert observed == [
        [
            "label",
            "create",
            "agent-claim:ledger",
            "--repo",
            REPOSITORY,
            "--color",
            "6f42c1",
            "--description",
            "canonical ledger",
            "--force",
        ]
    ]


def test_github_adapter_creates_an_item_and_returns_its_number() -> None:
    observed: list[tuple[list[str], bytes | None]] = []

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append((arguments, input_data))
        return json.dumps({"number": 42})

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    assert client.create_item(title="Agent claim ledger", body="body text") == 42
    assert observed == [
        (
            ["api", "--method", "POST", f"repos/{REPOSITORY}/issues", "--input", "-"],
            json.dumps({"title": "Agent claim ledger", "body": "body text"}).encode("utf-8"),
        )
    ]


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        pytest.param("not json", "invalid created-ledger JSON", id="invalid-json"),
        pytest.param(json.dumps({}), "did not return a created ledger number", id="missing-number"),
        pytest.param(
            json.dumps({"number": True}),
            "did not return a created ledger number",
            id="number-is-a-bool",
        ),
        pytest.param(
            json.dumps({"number": 0}), "did not return a created ledger number", id="non-positive"
        ),
    ],
)
def test_github_adapter_fails_loud_on_a_malformed_created_ledger(raw: str, match: str) -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY), run=lambda arguments, input_data=None: raw
    )

    with pytest.raises(ClaimError, match=match):
        client.create_item(title="Agent claim ledger", body="body text")


def test_github_adapter_locks_an_item() -> None:
    observed: list[list[str]] = []

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append(arguments)
        return ""

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    client.lock_item(11)

    assert observed == [["api", "--method", "PUT", f"repos/{REPOSITORY}/issues/11/lock"]]


def test_github_adapter_closes_an_item() -> None:
    observed: list[list[str]] = []

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append(arguments)
        return ""

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    client.close_item(11)

    assert observed == [["issue", "close", "11", "--repo", REPOSITORY]]


def test_github_adapter_neutralizes_a_claim_comment() -> None:
    observed: list[tuple[list[str], bytes | None]] = []

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append((arguments, input_data))
        return ""

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    client.neutralize_claim_comment(5, "new body")

    assert observed == [
        (
            [
                "api",
                "--method",
                "PATCH",
                f"repos/{REPOSITORY}/issues/comments/5",
                "--input",
                "-",
            ],
            json.dumps({"body": "new body"}).encode("utf-8"),
        )
    ]


@pytest.mark.parametrize(
    ("operation", "flag"),
    [
        pytest.param("add_label", "--add-label", id="add-label"),
        pytest.param("remove_label", "--remove-label", id="remove-label"),
    ],
)
def test_github_adapter_edits_an_issue_label(operation: str, flag: str) -> None:
    observed: list[list[str]] = []

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append(arguments)
        return ""

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)
    getattr(client, operation)(10, "agent-claim:active")

    assert observed == [["issue", "edit", "10", "--repo", REPOSITORY, flag, "agent-claim:active"]]


def test_github_adapter_creates_a_child_and_links_it_as_a_sub_issue() -> None:
    observed: list[tuple[list[str], bytes | None]] = []

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append((arguments, input_data))
        if arguments[2] == "POST" and arguments[3].endswith("/issues"):
            return json.dumps({"id": 555444, "number": 101})
        return ""

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    child = client.create_child(
        parent=79, title="Scheibe 4", body=board.CHILD_SKELETON, kind=board.ItemKind.TASK
    )

    assert child == 101
    assert observed == [
        (
            ["api", "--method", "POST", f"repos/{REPOSITORY}/issues", "--input", "-"],
            json.dumps({"title": "Scheibe 4", "body": board.CHILD_SKELETON, "type": "Task"}).encode(
                "utf-8"
            ),
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
    claimed = comment(1, claim_comment(request("cli-claim", issue=72, scope=("src",))))
    client = ReaderOnlyForge({LEDGER_ISSUE: [claimed]}, {72})
    client.board_issues = (board_issue(72, "Work", complete_contract("Ship it.")),)
    client.landings[12] = landing_pull_request(body="Work-Item: #72\n\nCloses #72")
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: "")
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    for argv in (
        ["status"],
        ["board"],
        ["next"],
        ["who", "src"],
        ["rulings"],
        ["pr-check", "--pr", "12"],
    ):
        issue_claim.main(["--repo", REPOSITORY, *argv])
        capsys.readouterr()


def _board_fixture_environment(monkeypatch: pytest.MonkeyPatch) -> list[list[str]]:
    issues_json = [
        {
            "number": 10,
            "title": "Security boundary",
            "labels": ["security"],
            "body": (
                "## Now\nInspect.\n\n## Next\nLand #10.\n\n## Blocked by\nNone."
                "\n\n## Done when\nMerged."
            ),
            "createdAt": "2026-08-10T00:00:00Z",
            "updatedAt": "2026-08-20T00:00:00Z",
        },
        {
            "number": 11,
            "title": "Product dependency",
            "labels": ["product"],
            "body": (
                "## Now\nImplement.\n\n## Next\nReview implementation.\n\n## Blocked by\n#10"
                "\n\n## Done when\nReleased."
            ),
            "createdAt": "2026-08-12T00:00:00Z",
            "updatedAt": "2026-08-20T00:00:00Z",
        },
        {
            "number": 12,
            "title": "Old notes",
            "labels": ["ux"],
            "body": "Unstructured notes.",
            "createdAt": "2026-08-01T00:00:00Z",
            "updatedAt": "2026-08-10T00:00:00Z",
        },
        {
            "number": 13,
            "title": "Cleanup landed",
            "labels": ["cleanup"],
            "body": (
                "## Now\nVerify.\n\n## Next\nClose issue.\n\n## Blocked by\nNone."
                "\n\n## Done when\nReleased."
            ),
            "createdAt": "2026-08-02T00:00:00Z",
            "updatedAt": "2026-08-19T00:00:00Z",
        },
        {
            "number": 14,
            "title": "Older cleanup",
            "labels": ["cleanup"],
            "body": "Unstructured notes.",
            "createdAt": "2026-08-02T00:00:00Z",
            "updatedAt": "2026-08-20T00:00:00Z",
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
    ledger_comment = {
        "id": 1,
        "created_at": "2026-08-20T12:00:00Z",
        "updated_at": "2026-08-20T12:00:00Z",
        "body": claim_comment(active),
        "author_association": "OWNER",
        "html_url": "https://github.com/example/agent-claim/issues/71#issuecomment-1",
    }
    repository = github._repository_id("example/agent-claim")
    observed: list[list[str]] = []

    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        assert input_data is None
        observed.append(arguments)
        endpoint = next((argument for argument in arguments if argument.startswith("repos/")), "")
        if "/comments?" in endpoint:
            # Comment pages are fetched in parallel: only the first page
            # carries the fixture row, every later page ends the fetch by
            # coming back short, exactly like a page past the real last one.
            page = int(endpoint.rsplit("page=", 1)[1])
            rows = [ledger_comment] if page == 1 else []
        elif "/issues?" in endpoint:
            rows = issues_json
        elif endpoint == f"repos/{repository}/issues/10":
            rows = [
                {
                    "number": 10,
                    "state": "open",
                    "closedAt": None,
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
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(issue_claim, "datetime", FixedDateTime)
    monkeypatch.setattr(github, "datetime", FixedDateTime)
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
    assert set(payload) == {"items", "ready_now", "stale", "recovery", "uncut"}
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
    assert fourteen["actionable_reason"] == "body incomplete"
    assert [item["number"] for item in payload["ready_now"]] == [10, 13]
    assert [item["number"] for item in payload["stale"]] == [12]
    assert next(item for item in payload["items"] if item["number"] == 12)["stage"] == "text-only"
    assert 11 not in [item["number"] for item in payload["ready_now"]]


def test_board_shows_open_and_total_instead_of_proposed(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    issues = (
        board_issue(10, "No expectations", complete_contract("Claim #10.")),
        board_issue(
            11,
            "Proposed expectations",
            complete_contract("Claim #11.")
            + "\n\n"
            + expectation_block(
                "- Name it. *(geregelt: ja)*",
                "- Settle it. *(Default: no)*",
            ),
        ),
        board_issue(
            12,
            "Ruled expectations",
            complete_contract("Claim #12.")
            + "\n\n"
            + expectation_block("- Name it. *(geregelt: ja)*"),
        ),
    )
    client = _claims_client()
    monkeypatch.setattr(client, "list_open_board_issues", lambda: issues)
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
) -> board.Issue:
    return board.Issue(
        number,
        title,
        labels,
        body,
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
    )


def complete_contract(next_step: str, *, blocked_by: str = "nichts") -> str:
    return (
        "## Now\nWork is ready.\n\n"
        f"## Next\n{next_step}\n\n"
        f"## Blocked by\n{blocked_by}\n\n"
        "## Done when\nThe work is merged."
    )


def expectation_block(*lines: str, heading: str = "Erwartung (refine-Lauf 28.08.2026)") -> str:
    return f"## {heading}\n" + "\n".join(lines)


def rulings_issue(
    number: int, title: str, *, open_lines: int, total_lines: int, labels: tuple[str, ...] = ()
) -> board.Issue:
    lines = (
        *(f"- Open decision {index}. *(Default: later)*" for index in range(open_lines)),
        *(
            f"- Settled decision {index}. *(geregelt: ja)*"
            for index in range(total_lines - open_lines)
        ),
    )
    return board_issue(
        number,
        title,
        complete_contract(f"Ship #{number}.") + "\n\n" + expectation_block(*lines),
        labels=labels,
    )


def slice_table(*rows: tuple[str, str, str, str]) -> str:
    """A `#79`-shaped slice table body: `#`, `Scheibe`, `Item`, `Hängt ab von`."""
    header = "| # | Scheibe | Item | Hängt ab von |\n|---|---|---|---|\n"
    return header + "".join(f"| {a} | {b} | {c} | {d} |\n" for a, b, c, d in rows)


def _configured_board_client(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    open_issues: tuple[board.Issue, ...] = (),
    open_pull_requests: tuple[board.PullRequest, ...] = (),
    standing: tuple[ClaimRequest, ...] = (),
) -> FakeForge:
    """A `FakeForge` client wired the way every board-reading claim test needs."""
    client = _claims_client(*standing)
    monkeypatch.setattr(client, "list_open_board_issues", lambda: open_issues)
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: open_pull_requests)
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())
    return client


@pytest.fixture
def open_blocker_references() -> Callable[[frozenset[int]], tuple[board.BlockerReference, ...]]:
    def references(numbers: frozenset[int]) -> tuple[board.BlockerReference, ...]:
        return tuple(
            board.BlockerReference(number, board.BlockerState.OPEN, False)
            for number in sorted(numbers)
        )

    return references


def _stub_issue_reference(
    monkeypatch: pytest.MonkeyPatch,
    states: dict[int, tuple[forge.ItemState, str, str]],
) -> None:
    """Overrides the autouse OPEN default for exactly the given issue numbers."""

    def fetch(client: object, number: int) -> forge.ItemReference:
        state, title, body = states[number]
        return forge.ItemReference(state, title, body)

    monkeypatch.setattr(issue_claim, "_fetch_issue_reference", fetch)


@pytest.mark.parametrize(
    ("issues", "claims", "arguments", "expected_exit", "expected_output"),
    [
        pytest.param(
            (
                board_issue(10, "Lower work", complete_contract("Claim #10.")),
                board_issue(11, "Top work", complete_contract("Claim #11.")),
                board_issue(12, "Depends on top", "## Blocked by\n#11"),
            ),
            (),
            ("next",),
            0,
            "#11 score 10: Top work\nNext: Claim #11.\n\nSKIPPED\n#12: blocked by #11\n",
            id="names_the_highest_scored_actionable_item",
        ),
        pytest.param(
            (
                board_issue(10, "Lower work", complete_contract("Claim #10.")),
                board_issue(11, "Top work", complete_contract("Claim #11.")),
                board_issue(12, "Depends on top", "## Blocked by\n#11"),
            ),
            (),
            ("next", "--json"),
            0,
            {
                "action": "work_item",
                "number": 11,
                "score": 10,
                "title": "Top work",
                "next": "Claim #11.",
                "recovery": [],
                "skipped": [{"number": 12, "reason": "blocked by #11"}],
                "ruling_landings": None,
                "ruling_old": None,
            },
            id="emits_the_highest_scored_actionable_item_as_json",
        ),
        pytest.param(
            (board_issue(10, "Incomplete", "## Now\nInvestigate."),),
            (),
            ("next",),
            3,
            "No actionable item.\n\nSKIPPED\n#10: body incomplete\n",
            id="names_an_incomplete_body_as_the_reason_nothing_is_pullable",
        ),
        pytest.param(
            (board_issue(10, "Claimed", complete_contract("Claim #10.")),),
            (request(issue=10),),
            ("next",),
            3,
            "No actionable item.\n\nSKIPPED\n#10: claimed\n",
            id="names_a_live_claim_as_the_reason_nothing_is_pullable",
        ),
        pytest.param(
            (
                board_issue(9, "Open blocker", complete_contract("Claim #9.")),
                board_issue(10, "Blocked", complete_contract("Claim #10.", blocked_by="#9")),
            ),
            (),
            ("next",),
            0,
            "#9 score 10: Open blocker\nNext: Claim #9.\n\nSKIPPED\n#10: blocked by #9\n",
            id="excludes_items_with_open_blockers",
        ),
    ],
)
def test_next_reports_the_highest_scored_actionable_item(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    issues: tuple[board.Issue, ...],
    claims: tuple[ClaimRequest, ...],
    arguments: tuple[str, ...],
    expected_exit: int,
    expected_output: str | dict[str, object],
) -> None:
    client = _claims_client(*claims)
    monkeypatch.setattr(client, "list_open_board_issues", lambda: issues)
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    assert issue_claim.main(["--repo", "example/agent-claim", *arguments]) == expected_exit
    rendered = capsys.readouterr().out

    if isinstance(expected_output, str):
        assert rendered == expected_output
    else:
        assert json.loads(rendered) == expected_output


PULLED_WITH_REFINING_FIRST = (
    "#10 score -10: Work\nNext: Claim #10.\nErwartungen ungeregelt, beim Ziehen zuerst refinen\n"
)


@pytest.mark.parametrize(
    ("expectations", "expected_state", "expected_exit", "expected_output"),
    [
        pytest.param(
            "",
            board.ExpectationState.NONE,
            0,
            "#10 score -10: Work\nNext: Claim #10.\n",
            id="no_expectation_block_remains_actionable",
        ),
        pytest.param(
            expectation_block("- Name it. *(Default: yes)*"),
            board.ExpectationState.PROPOSED,
            0,
            PULLED_WITH_REFINING_FIRST,
            id="proposed_expectations_are_pulled_with_refining_first",
        ),
        pytest.param(
            expectation_block("- Name it without a ruling."),
            board.ExpectationState.PROPOSED,
            0,
            PULLED_WITH_REFINING_FIRST,
            id="unmarked_expectations_are_pulled_with_refining_first",
        ),
        pytest.param(
            expectation_block("- Name it. *(geregelt: maybe)*"),
            board.ExpectationState.PROPOSED,
            0,
            PULLED_WITH_REFINING_FIRST,
            id="malformed_expectations_are_pulled_with_refining_first",
        ),
        pytest.param(
            expectation_block(
                "- Name it. *(geregelt: ja)*",
                "- Remove it. *(geregelt: NEIN, it stays)*",
            ),
            board.ExpectationState.RULED,
            0,
            "#10 score -10: Work\nNext: Claim #10.\n",
            id="fully_ruled_expectations_remain_actionable",
        ),
        pytest.param(
            expectation_block(
                "- Name it. *(geregelt: NEIN, not for this release)*",
                "- Remove it. *(Default: later)*",
                heading="Erwartungsliste",
            ),
            board.ExpectationState.PROPOSED,
            0,
            PULLED_WITH_REFINING_FIRST,
            id="mixed_expectations_are_pulled_with_refining_first",
        ),
    ],
)
def test_next_reports_expectation_state(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    expectations: str,
    expected_state: board.ExpectationState,
    expected_exit: int,
    expected_output: str,
) -> None:
    issue = board_issue(
        10,
        "Work",
        "\n\n".join(part for part in (complete_contract("Claim #10."), expectations) if part),
    )
    client = _claims_client()
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (issue,))
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    assert issue_claim.main(["--repo", "example/agent-claim", "next"]) == expected_exit
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
        complete_contract("Claim #11.")
        + "\n\n"
        + expectation_block("- Name it. *(Default: no)*", heading="Erwartungen"),
    )
    blocked = board_issue(
        12, "Waits for rulings", complete_contract("Claim #12.", blocked_by="#11")
    )
    claimed = board_issue(13, "Another lane", complete_contract("Claim #13."))
    client = _claims_client(request(issue=13))
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (unruled, blocked, claimed))
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    assert issue_claim.main(["--repo", "example/agent-claim", "next"]) == 0
    assert capsys.readouterr().out == (
        "#11 score 10: Needs rulings\n"
        "Next: Claim #11.\n"
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
        "ruling_landings": None,
        "ruling_old": None,
        "ruling_hint": "Erwartungen ungeregelt, beim Ziehen zuerst refinen",
        "recovery": [],
        "skipped": [
            {"number": 12, "reason": "blocked by #11"},
            {"number": 13, "reason": "claimed"},
        ],
    }


@pytest.mark.parametrize(
    ("blocked_by", "blocker_references", "open_pull_requests"),
    [
        pytest.param("#62 holds the files", (), (), id="prose"),
        pytest.param("70705e98f9f34fdf9a88fc758b4f3f74", (), (), id="claim-id"),
        pytest.param("codex/issue-90-claim-gate", (), (), id="branch"),
        pytest.param("PR #62", (), (), id="pull-request"),
        pytest.param("None", (), (), id="none"),
        pytest.param(
            "#9",
            (board.BlockerReference(9, board.BlockerState.MISSING, False),),
            (),
            id="missing-blocker",
        ),
        pytest.param(
            "#9",
            (
                board.BlockerReference(
                    9,
                    board.BlockerState.CLOSED,
                    False,
                    datetime(2026, 8, 20, tzinfo=UTC),
                ),
            ),
            (),
            id="closed-issue",
        ),
        pytest.param(
            "#62",
            (),
            (board.PullRequest(62, "Open pull request", "", "branch"),),
            id="open-pull-request",
        ),
    ],
)
def test_claim_refuses_non_issue_or_closed_blockers_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    blocked_by: str,
    blocker_references: tuple[board.BlockerReference, ...],
    open_pull_requests: tuple[board.PullRequest, ...],
) -> None:
    issue = board_issue(
        10,
        "Work",
        complete_contract("Claim #10.", blocked_by=blocked_by),
        labels=("security",),
    )
    client = _configured_board_client(
        monkeypatch,
        tmp_path,
        open_issues=(issue,),
        open_pull_requests=open_pull_requests,
    )
    client.board_blocker_references = blocker_references or None
    if blocker_references or open_pull_requests:
        monkeypatch.setattr(
            issue_claim,
            "_fetch_issue_reference",
            lambda _client, _number: pytest.fail("claim must reuse board blocker state"),
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
        == 2
    )

    assert "ERROR:" in capsys.readouterr().err
    assert client.comments[LEDGER_ISSUE] == []


def test_claim_accepts_a_body_with_no_blockers(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    issue = board_issue(
        10,
        "Work",
        complete_contract("Claim #10.", blocked_by="nichts"),
        labels=("security",),
    )
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(issue,))
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

    assert len(client.comments[LEDGER_ISSUE]) == 1
    assert "ERROR:" not in capsys.readouterr().err


@pytest.mark.parametrize(
    ("blocked_by", "expected_blockers"),
    [
        pytest.param("#9", "#9", id="single-open-blocker"),
        pytest.param("#9, #11", "#9, #11", id="two-open-blockers"),
    ],
)
def test_claim_refuses_an_open_issue_blocker_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    blocked_by: str,
    expected_blockers: str,
) -> None:
    blockers = tuple(
        board_issue(number, f"Blocker {number}", complete_contract(f"Claim #{number}."))
        for number in (9, 11)
        if f"#{number}" in blocked_by
    )
    issue = board_issue(
        10,
        "Work",
        complete_contract("Claim #10.", blocked_by=blocked_by),
        labels=("security",),
    )
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(issue, *blockers))
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
    assert client.comments[LEDGER_ISSUE] == []


def test_claim_allows_an_open_issue_blocker_with_out_of_order_and_records_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    reason = "Blocker #9 is stuck on review; unblocking manually."
    blocker = board_issue(9, "Blocker 9", complete_contract("Claim #9."))
    issue = board_issue(
        10,
        "Work",
        complete_contract("Claim #10.", blocked_by="#9"),
        labels=("security",),
    )
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(issue, blocker))
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
    comment_body = client.comments[LEDGER_ISSUE][-1].body
    assert f"Out-of-order reason: {reason}" in comment_body


def test_claim_refuses_duplicate_contract_fields_before_mutation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    issue = board_issue(
        10,
        "Work",
        complete_contract("Claim #10.") + "\n\n**Done when:** The old projection remains.",
    )
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(issue,))
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
            "text": "duplicate Done when projection field",
            "slice": None,
            "issue": None,
        }
    ]
    assert client.comments[LEDGER_ISSUE] == []


def test_claim_ignores_body_size_and_closed_next_references(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    issue = board_issue(
        10,
        "Work",
        complete_contract("#9 follow up.") + "\n\n" + "x" * 50_000,
    )
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(issue,))
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

    assert len(client.comments[LEDGER_ISSUE]) == 1
    assert "ERROR:" not in capsys.readouterr().err


def test_release_ignores_body_contract_defects(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _claims_client(request("held", issue=10, scope=("src/work.py",)))
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
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)

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
    client = _claims_client()
    unruled = board_issue(
        11,
        "Needs rulings",
        complete_contract("Claim #11.") + "\n\n" + expectation_block("- Name it. *(Default: yes)*"),
    )
    waiting = board_issue(
        12, "Waits for rulings", complete_contract("Claim #12.", blocked_by="#11")
    )
    ready = board_issue(10, "Ready work", complete_contract("Claim #10."))
    claimed_request = request(issue=10, scope=("src/work.py",))
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (ready, unruled, waiting))
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    assert client.comments[LEDGER_ISSUE] == []


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
    client = _claims_client()
    issues = (
        board_issue(10, "Lower work", complete_contract("Claim #10.")),
        board_issue(11, "Top work", complete_contract("Claim #11.")),
        board_issue(12, "Depends on top", "## Blocked by\n#11"),
    )
    claimed_request = request("out-of-order", issue=10, scope=("src/lower.py",))
    monkeypatch.setattr(client, "list_open_board_issues", lambda: issues)
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    assert client.comments[LEDGER_ISSUE] == []


def test_claim_allows_out_of_order_with_a_reason_and_records_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    client = _claims_client()
    issues = (
        board_issue(10, "Lower work", complete_contract("Claim #10.")),
        board_issue(11, "Top work", complete_contract("Claim #11.")),
        board_issue(12, "Depends on top", "## Blocked by\n#11"),
    )
    reason = "Urgent customer incident."
    claimed_request = replace(
        request("out-of-order", issue=10, scope=("src/lower.py",)),
        out_of_order_reason=reason,
    )
    monkeypatch.setattr(client, "list_open_board_issues", lambda: issues)
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    comment_body = client.comments[LEDGER_ISSUE][-1].body
    assert "Out-of-order reason: Urgent customer incident." in comment_body


def test_claim_refuses_for_a_higher_priority_item_even_at_a_lower_score(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`board`/`next` rank a labelled blocker ahead of an unlabelled item even
    when the blocker scores lower; the out-of-order refusal must agree, or
    claiming the unlabelled item would silently skip past the very item
    `next` would have named.
    """
    client = _claims_client()
    blocker = board_issue(
        50, "Prerequisite the operator prioritized", complete_contract("Unblock #52.")
    )
    dependent = board_issue(52, "Depends on the prerequisite", "## Blocked by\n#50")
    in_flight_unlabelled = board_issue(51, "In-flight, unlabelled", complete_contract("Ship it."))
    open_pull_request = board.PullRequest(200, "Fixes #51", "", "branch")
    claimed_request = request("lower-priority", issue=51, scope=("src/lower.py",))
    monkeypatch.setattr(
        client, "list_open_board_issues", lambda: (blocker, dependent, in_flight_unlabelled)
    )
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: (open_pull_request,))
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    assert client.comments[LEDGER_ISSUE] == []


def test_claim_json_refusal_reports_out_of_order_without_mutating(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    lower = board_issue(10, "Lower work", complete_contract("Claim #10."))
    top = board_issue(11, "Top work", complete_contract("Claim #11."))
    dependent = board_issue(12, "Depends on top", "## Blocked by\n#11")
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(lower, top, dependent))
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
    assert client.comments[LEDGER_ISSUE] == []


def test_claim_does_not_require_out_of_order_for_the_top_ranked_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    top = board_issue(10, "Top work", complete_contract("Claim #10."))
    lower = board_issue(11, "Lower work", complete_contract("Claim #11."))
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(top, lower))
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
    assert len(client.comments[LEDGER_ISSUE]) == 1


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
    client = _configured_board_client(monkeypatch, tmp_path)
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
    assert client.comments[LEDGER_ISSUE] == []


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
    """`cut`'s fresh child (`board.CHILD_SKELETON`) is defect-free but
    incomplete -- invisible to `next`, and now refused here too, exactly as
    ruled: `claim` requires a complete projection."""
    child = board_issue(101, "Scheibe 1", board.CHILD_SKELETON)
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
    dependent = board_issue(51, "Dependent", "## Now\nWork.\n\n## Blocked by\n#50")
    _configured_board_client(monkeypatch, tmp_path, open_issues=(blocker, dependent))
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


def test_claim_reports_incomplete_body_when_blocked_by_itself_is_missing(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    """A contract missing its own "Blocked by" section has no blocker to
    check at all -- `Contract.blocker_issues` reads that absence as "none
    named", not a crash -- so only the incompleteness itself is reported."""
    issue = board_issue(
        51, "Dependent", "## Now\nWork.\n\n## Next\nDo it.\n\n## Done when\nMerged."
    )
    _configured_board_client(monkeypatch, tmp_path, open_issues=(issue,))
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


def _cut_container_issue(body: str) -> board.Issue:
    return board.Issue(
        CUT_CONTAINER,
        "Epic",
        (),
        body,
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )


def test_cut_creates_a_child_and_links_the_first_cuttable_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    body = slice_table(("1", "Scheibe 1", "—", "—"))
    container = _cut_container_issue(body)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 0
    assert client.created_children == [
        (CUT_CONTAINER, "Scheibe 1", board.CHILD_SKELETON, board.ItemKind.TASK)
    ]
    child = client.next_created_child_number - 1
    assert client.item_bodies == {CUT_CONTAINER: slice_table(("1", "Scheibe 1", f"#{child}", "—"))}
    assert capsys.readouterr().out == f"CUT #{CUT_CONTAINER} row 1 -> #{child}\n"


def test_cut_selects_a_row_by_number(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    body = slice_table(("1", "Scheibe 1", "—", "—"), ("2", "Scheibe 2", "—", "—"))
    container = _cut_container_issue(body)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

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
    assert client.item_bodies[CUT_CONTAINER] == slice_table(
        ("1", "Scheibe 1", "—", "—"), ("2", "Scheibe 2", f"#{child}", "—")
    )


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


def test_cut_refuses_when_the_row_cannot_be_located(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`_cut_row` selects the row and `board.locate_slice_row` re-locates its
    span through a mirrored parse of the same body (#79); cut must refuse
    before any write rather than link a child into a guessed location if
    those two ever disagreed."""
    body = slice_table(("1", "Scheibe 1", "—", "—"))
    container = _cut_container_issue(body)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    monkeypatch.setattr(board, "locate_slice_row", lambda _body, _row_index: None)

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 2
    assert f"ERROR: #{CUT_CONTAINER}'s row 1 could not be located" in capsys.readouterr().err
    assert client.created_children == []
    assert client.item_bodies == {}


def test_cut_refuses_a_container_that_already_has_a_parent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    body = slice_table(("1", "Scheibe 1", "—", "—"))
    container = _cut_container_issue(body)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
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


def test_cut_refuses_a_row_when_no_cuttable_row_exists(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`--row N` names a row inside a table; a malformed-only table still
    refuses it by name (#151: only a bare `cut`, without `--row`, falls back
    to an untied child when nothing is cuttable)."""
    body = "| # | Scheibe | Item | Hängt ab von |\n|---|---|---|---|\n| x | Broken | — | — |\n"
    container = _cut_container_issue(body)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    exit_code = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "cut",
            str(CUT_CONTAINER),
            "--title",
            "Scheibe 1",
            "--row",
            "1",
        ]
    )

    assert exit_code == 2
    expected = (
        f"ERROR: #{CUT_CONTAINER} has no cuttable slice row; "
        'row "x": index must be a positive integer'
    )
    assert expected in capsys.readouterr().err
    assert client.created_children == []
    assert client.item_bodies == {}


def test_cut_refuses_a_row_already_cut_into_another_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`--row N` on a row already linked names which item it went to and
    which rows are still cuttable (like #122 on 06.09.2026)."""
    body = slice_table(
        ("4", "Landed slice", "#150", "—"),
        ("5", "Open slice", "—", "—"),
        ("6", "Another open slice", "—", "—"),
        ("7", "Yet another open slice", "—", "—"),
    )
    container = _cut_container_issue(body)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    exit_code = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "cut",
            str(CUT_CONTAINER),
            "--title",
            "Scheibe 4",
            "--row",
            "4",
        ]
    )

    assert exit_code == 2
    assert capsys.readouterr().err == (
        f"ERROR: #{CUT_CONTAINER} row 4 is already cut (#150); cuttable rows: 5, 6, 7\n"
    )
    assert client.created_children == []
    assert client.item_bodies == {}


def test_cut_refuses_a_row_when_the_whole_table_is_already_cut(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """No row left to cut names the whole cut range, not the requested row
    number (like #122 on 06.09.2026, once every row is linked)."""
    body = slice_table(
        ("4", "First slice", "#150", "—"),
        ("5", "Second slice", "#151", "—"),
        ("6", "Third slice", "#152", "—"),
        ("7", "Fourth slice", "#153", "—"),
    )
    container = _cut_container_issue(body)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    exit_code = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "cut",
            str(CUT_CONTAINER),
            "--title",
            "Scheibe 8",
            "--row",
            "8",
        ]
    )

    assert exit_code == 2
    assert capsys.readouterr().err == (
        f"ERROR: #{CUT_CONTAINER} has no uncut row; rows 4-7 are cut\n"
    )
    assert client.created_children == []
    assert client.item_bodies == {}


def test_cut_creates_an_untied_child_when_a_malformed_table_has_no_cuttable_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The bare-`cut` twin of the refusal above: without `--row`, a table
    with nothing cuttable left -- malformed rows included -- creates an
    untied child instead of refusing (#151)."""
    body = "| # | Scheibe | Item | Hängt ab von |\n|---|---|---|---|\n| x | Broken | — | — |\n"
    container = _cut_container_issue(body)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 0
    child = client.next_created_child_number - 1
    assert client.created_children == [
        (CUT_CONTAINER, "Scheibe 1", board.CHILD_SKELETON, board.ItemKind.TASK)
    ]
    assert client.item_bodies == {}
    assert capsys.readouterr().out == f"CUT #{CUT_CONTAINER} -> #{child}\n"


def test_cut_creates_an_untied_child_when_the_container_has_no_slice_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """A container with no slice table at all (#151, like #122 on
    06.09.2026) still gets its next slice cut -- the fresh child is created
    and related, but there is no row to link, so the container's own body
    stays exactly as it was."""
    container = _cut_container_issue(complete_contract("Scheibe 1"))
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 0
    child = client.next_created_child_number - 1
    assert client.created_children == [
        (CUT_CONTAINER, "Scheibe 1", board.CHILD_SKELETON, board.ItemKind.TASK)
    ]
    assert client.item_bodies == {}
    assert capsys.readouterr().out == f"CUT #{CUT_CONTAINER} -> #{child}\n"


def test_cut_refuses_a_row_when_the_container_has_no_slice_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """`--row` names a row inside a table; a container without one gets a
    refusal naming the missing table, never a guessed row (#151)."""
    container = _cut_container_issue(complete_contract("Scheibe 1"))
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    exit_code = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "cut",
            str(CUT_CONTAINER),
            "--title",
            "Scheibe 1",
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
    assert client.item_bodies == {}


@pytest.mark.parametrize(
    "operation", [forge.ForgeOperation.CREATE_CHILD, forge.ForgeOperation.UPDATE_ITEM_BODY]
)
def test_cut_refuses_when_the_forge_cannot_perform_a_required_write(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
    operation: forge.ForgeOperation,
) -> None:
    body = slice_table(("1", "Scheibe 1", "—", "—"))
    container = _cut_container_issue(body)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
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


def test_cut_names_the_created_child_when_linking_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    body = slice_table(("1", "Scheibe 1", "—", "—"))
    container = _cut_container_issue(body)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    client.fail_update_item_body = True

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 2
    child = client.next_created_child_number - 1
    assert client.created_children == [
        (CUT_CONTAINER, "Scheibe 1", board.CHILD_SKELETON, board.ItemKind.TASK)
    ]
    assert client.item_bodies == {}
    err = capsys.readouterr().err
    assert f"created #{child} but failed to link it" in err
    assert "do not re-run" in err


def test_cut_names_the_created_child_when_the_relation_post_fails(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The sub-issue relation POST is `create_child`'s own second write --
    also not atomic with the first, so a failure there must name the child
    exactly as a failed slice-table link does (#112 finding 3)."""
    body = slice_table(("1", "Scheibe 1", "—", "—"))
    container = _cut_container_issue(body)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))
    client.fail_create_child_relation = True

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "cut", str(CUT_CONTAINER), "--title", "Scheibe 1"]
    )

    assert exit_code == 2
    child = client.next_created_child_number - 1
    assert client.created_children == [
        (CUT_CONTAINER, "Scheibe 1", board.CHILD_SKELETON, board.ItemKind.TASK)
    ]
    assert client.item_bodies == {}
    err = capsys.readouterr().err
    assert f"created #{child} but failed to record #{child} as a sub-issue" in err
    assert "do not re-run" in err


def test_claim_json_refusal_carries_refused_issue_and_checks(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    client = _configured_board_client(monkeypatch, tmp_path)
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
    assert client.comments[LEDGER_ISSUE] == []


def test_claim_does_not_corridor_on_a_slice_table(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    body = complete_contract("Ship it.") + "\n\n" + slice_table(("1", "First slice", "—", "—"))
    target = board_issue(72, "Epic", body)
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(target,))
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
    assert len(client.comments[LEDGER_ISSUE]) == 1


def test_parse_slice_table_reads_each_item_cell_shape() -> None:
    body = slice_table(
        ("1", "Undispatched slice", "—", "—"),
        ("2", "Open slice", "#101", "—"),
        ("3", "Closed slice", "#102", "—"),
        ("4", "Missing slice", "#103", "—"),
        ("5", "Malformed slice", "not a link", "—"),
    )

    assert board.parse_slice_table(body) == (
        board.SliceTableRow(1, "Undispatched slice", "—", None),
        board.SliceTableRow(2, "Open slice", "#101", 101),
        board.SliceTableRow(3, "Closed slice", "#102", 102),
        board.SliceTableRow(4, "Missing slice", "#103", 103),
        board.SliceTableRow(5, "Malformed slice", "not a link", None),
    )


def test_parse_slice_table_marks_a_header_with_extra_columns_malformed() -> None:
    header_line = "| # | Scheibe | Item | Owner | Hängt ab von |"
    body = f"{header_line}\n|---|---|---|---|---|\n| 1 | First slice | — | me | — |\n"

    assert board.parse_slice_table(body) == (board.MalformedSliceTable(header_line),)


def test_parse_slice_table_marks_an_english_slice_header_malformed() -> None:
    header_line = "| # | Slice | Item | Hängt ab von |"
    body = f"{header_line}\n|---|---|---|---|\n| 1 | First slice | — | — |\n"

    assert board.parse_slice_table(body) == (board.MalformedSliceTable(header_line),)


def test_parse_slice_table_ignores_an_ordinary_hash_led_table() -> None:
    body = "| # | Name | Value | Notes |\n|---|---|---|---|\n| 1 | Alpha | 10 | ok |\n"

    assert board.parse_slice_table(body) == ()


def test_parse_slice_table_marks_a_row_with_the_wrong_shape_and_keeps_scanning() -> None:
    bad_row = "| x | Broken index | — | — |"
    body = (
        "| # | Scheibe | Item | Hängt ab von |\n"
        "|---|---|---|---|\n"
        f"{bad_row}\n"
        "| 2 | Second slice | — | — |\n"
    )

    assert board.parse_slice_table(body) == (
        board.MalformedSliceRow(bad_row, "x", "index must be a positive integer"),
        board.SliceTableRow(2, "Second slice", "—", None),
    )


def test_parse_slice_table_reads_every_table_in_the_body() -> None:
    body = (
        slice_table(("1", "First table's slice", "#101", "—"))
        + "\nSome prose between the two tables.\n\n"
        + slice_table(("1", "Second table's slice", "—", "—"))
    )

    assert board.parse_slice_table(body) == (
        board.SliceTableRow(1, "First table's slice", "#101", 101),
        board.SliceTableRow(1, "Second table's slice", "—", None),
    )


def test_slice_table_findings_classifies_cuttable_unlinkable_landed_and_malformed() -> None:
    bad_row = "| x | Broken index | — | — |"
    body = (
        slice_table(
            ("1", "Undispatched slice", "—", "—"),
            ("2", "Landed slice", "#101", "—"),
            ("3", "Malformed link slice", "not a link", "—"),
        )
        + f"{bad_row}\n"
    )

    findings = board.slice_table_findings(body)

    assert findings.cuttable == (board.SliceTableRow(1, "Undispatched slice", "—", None),)
    assert findings.unlinkable == (
        board.SliceTableRow(3, "Malformed link slice", "not a link", None),
    )
    assert findings.malformed == (
        board.MalformedSliceRow(bad_row, "x", "index must be a positive integer"),
    )


def test_uncut_slices_is_none_when_every_row_is_linked() -> None:
    container = board.Issue(
        79,
        "Container",
        (),
        slice_table(("1", "Landed slice", "#101", "—")),
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


def test_locate_and_link_slice_row_replaces_only_the_target_cell() -> None:
    body = slice_table(
        ("1", "First slice", "—", "—"),
        ("2", "Second slice", "—", "—"),
    )

    span = board.locate_slice_row(body, 2)

    assert span is not None
    linked = board.link_slice_row(body, span, 101)
    assert linked == slice_table(
        ("1", "First slice", "—", "—"),
        ("2", "Second slice", "#101", "—"),
    )
    assert linked.splitlines()[2] == "| 1 | First slice | — | — |"


def test_locate_slice_row_returns_none_for_an_absent_row_index() -> None:
    body = slice_table(("1", "Only slice", "—", "—"))

    assert board.locate_slice_row(body, 2) is None


def test_locate_slice_row_skips_ordinary_prose_and_a_near_miss_header() -> None:
    """Scanning for the real table must step past an ordinary line (no header
    match at all) and a near-miss header (looks like an attempt but is
    missing columns) without mistaking either for the genuine table."""
    body = (
        "Some ordinary prose line before the table.\n\n"
        "| # | Scheibe |\n"
        "|---|---|\n\n" + slice_table(("1", "Real slice", "—", "—"))
    )

    span = board.locate_slice_row(body, 1)

    assert span is not None
    assert body[span[0] : span[1]] == " — "


def test_locate_slice_row_skips_a_fenced_example() -> None:
    fenced = "```markdown\n" + slice_table(("1", "Example slice", "—", "—")) + "```\n"

    assert board.locate_slice_row(fenced, 1) is None


@pytest.mark.parametrize(
    ("body", "match"),
    [
        pytest.param(
            "no expectation heading here at all\n",
            "ruled expectations have no readable date",
            id="no-heading",
        ),
        pytest.param(
            "## Erwartungen 31.02.2026\n", r"invalid date 31\.02\.2026", id="invalid-calendar-date"
        ),
    ],
)
def test_parse_ruling_date_fails_loud_on_a_malformed_body(body: str, match: str) -> None:
    with pytest.raises(ClaimError, match=match):
        board.parse_ruling_date(body)


@pytest.mark.parametrize(
    "raw_timestamp",
    [
        pytest.param("not-a-timestamp", id="unparsable"),
        pytest.param("2026-08-20T00:00:00", id="missing-offset"),
    ],
)
def test_claim_age_fails_loud_on_a_malformed_github_timestamp(raw_timestamp: str) -> None:
    raised_argument_1 = datetime(2026, 8, 21, tzinfo=UTC)
    with pytest.raises(ClaimError, match="GitHub returned a malformed board timestamp"):
        board.claim_age(raw_timestamp, raised_argument_1)


def test_child_skeleton_is_an_incomplete_contract_with_no_defects() -> None:
    contract = board.parse_contract(board.CHILD_SKELETON)

    assert contract.complete is False
    assert contract.defects == ()
    assert contract.now is None
    assert contract.next is None
    assert contract.blocked_by == board.NO_BLOCKERS
    assert contract.done_when is None


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
    assert len(client.comments[LEDGER_ISSUE]) == 1


@pytest.mark.parametrize(
    ("issue", "claims", "blocker_is_open", "expected"),
    [
        pytest.param(
            board_issue(10, "Ready", complete_contract("Claim #10.")),
            (),
            True,
            (True, None),
            id="ready",
        ),
        pytest.param(
            board_issue(10, "Claimed", complete_contract("Claim #10.")),
            (request(issue=10),),
            True,
            (False, "claimed"),
            id="claimed",
        ),
        pytest.param(
            board_issue(10, "Blocked", complete_contract("Claim #10.", blocked_by="#9")),
            (),
            True,
            (False, "blocked by #9"),
            id="blocked",
        ),
        pytest.param(
            board_issue(10, "Unblocked", complete_contract("Claim #10.", blocked_by="#9")),
            (),
            False,
            (True, None),
            id="closed_blocker",
        ),
        pytest.param(
            board_issue(10, "Incomplete", "## Now\nInvestigate."),
            (),
            True,
            (False, "body incomplete"),
            id="incomplete",
        ),
        pytest.param(
            board_issue(
                10,
                "Frozen",
                complete_contract("Claim #10.")
                + "\n\nEingefroren bis: eine zweite Maschine bekommt einen Grund "
                "(Operator, 31.08.2026)",
            ),
            (),
            True,
            (False, "frozen: eine zweite Maschine bekommt einen Grund"),
            id="frozen",
        ),
        pytest.param(
            board_issue(
                10,
                "Frozen and claimed",
                complete_contract("Claim #10.")
                + "\n\nEingefroren bis: eine zweite Maschine bekommt einen Grund "
                "(Operator, 31.08.2026)",
            ),
            (request(issue=10),),
            True,
            (False, "frozen: eine zweite Maschine bekommt einen Grund"),
            id="frozen_takes_priority_over_claimed",
        ),
    ],
)
def test_board_reports_each_item_actionability_reason(
    issue: board.Issue,
    claims: tuple[ClaimRequest, ...],
    blocker_is_open: bool,
    expected: tuple[bool, str | None],
) -> None:
    blocker = board_issue(9, "Blocker", complete_contract("Claim #9."))
    projected = projected_board(
        (blocker, issue) if blocker_is_open else (issue,),
        (),
        (),
        tuple(
            claim
            for request_value in claims
            if (claim := parse_claim_event(comment(1, claim_comment(request_value)))) is not None
        ),
        board.BoardConfig(),
        blocker_references=(
            (
                board.BlockerReference(
                    9,
                    board.BlockerState.CLOSED,
                    False,
                    datetime(2026, 8, 20, tzinfo=UTC),
                ),
            )
            if not blocker_is_open
            else None
        ),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )
    item = next(item for item in projected.items if item.number == issue.number)

    actual = (item.actionable, item.actionable_reason)
    assert actual == expected


def test_board_collects_every_open_blocker_from_issue_list() -> None:
    blocked = board_issue(
        10,
        "Blocked",
        complete_contract(
            "Claim #10.",
            blocked_by="#790, #642",
        ),
    )
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
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )
    item = next(item for item in projected.items if item.number == 10)

    assert item.open_blockers == (642, 790)
    assert item.actionable is False
    assert item.actionable_reason == "blocked by #642, #790"


def test_board_treats_nichts_as_unblocked() -> None:
    issue = board_issue(10, "Ready", complete_contract("Claim #10.", blocked_by="nichts"))
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


FROZEN_LINE = "Eingefroren bis: eine zweite Maschine bekommt einen Grund (Operator, 31.08.2026)"


def test_frozen_item_leaves_actionable_and_thaws_when_the_line_is_removed() -> None:
    frozen_body = complete_contract("Claim #301.") + f"\n\n{FROZEN_LINE}"
    frozen = board_issue(301, "Highest scored", frozen_body)
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
    assert item.actionable_reason == "frozen: eine zweite Maschine bekommt einen Grund"
    assert item.frozen_trigger == "eine zweite Maschine bekommt einen Grund"
    assert item not in projected_while_frozen.ready_now
    assert board.highest_scored_actionable(projected_while_frozen) is None

    thawed_body = complete_contract("Claim #301.")
    thawed = board_issue(301, "Highest scored", thawed_body)
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
        301,
        "Highest scored",
        complete_contract("Claim #301.") + f"\n\n{FROZEN_LINE}",
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
    assert "frozen: eine zweite Maschine bekommt einen Grund" in frozen_row
    ready_now_section = rendered.split("READY NOW\n", 1)[1].split("\n\nSTALE", 1)[0]
    assert "#301" not in ready_now_section


def test_frozen_marker_without_a_valid_form_fails_loud() -> None:
    issue = board_issue(
        10,
        "Malformed freeze",
        complete_contract("Claim #10.") + "\n\nEingefroren bis: no operator or date",
    )

    raised_argument_1 = board.BoardConfig()
    raised_argument_2 = datetime(2026, 8, 21, tzinfo=UTC)
    with pytest.raises(ClaimError, match="Eingefroren bis"):
        projected_board(
            (issue,),
            (),
            (),
            (),
            raised_argument_1,
            now=raised_argument_2,
        )


def test_frozen_marker_syntax_documented_in_a_fence_is_not_a_live_marker() -> None:
    # Shaped like #72's own body: it fences the marker grammar as an example
    # with placeholders, which must never itself freeze the item that
    # introduced the mechanism.
    documented = board_issue(
        72,
        "Freeze marker proposal",
        complete_contract("Claim #72.") + "\n\n## Die Scheibe\n\n"
        "Ein parsebarer Einfrier-Vermerk im Item-Body — eine Zeile in der Art\n\n"
        "```\n"
        "Eingefroren bis: <Auslöser in einem Satz> (Operator, <Datum>)\n"
        "```\n\n"
        "— den `next` und `board` respektieren.",
    )
    projected = projected_board(
        (documented,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 31, tzinfo=UTC),
    )
    item = projected.items[0]

    assert board.frozen_trigger(documented.body) is None
    assert item.frozen_trigger is None
    assert item.actionable is True
    assert item.actionable_reason is None
    assert item in projected.ready_now


def test_frozen_marker_outside_a_fence_still_fails_loud_when_malformed() -> None:
    issue = board_issue(
        10,
        "Malformed freeze next to a fence",
        complete_contract("Claim #10.")
        + "\n\n```\nEingefroren bis: <trigger> (Operator, <Datum>)\n```\n\n"
        "Eingefroren bis: no operator or date",
    )

    raised_argument_1 = board.BoardConfig()
    raised_argument_2 = datetime(2026, 8, 31, tzinfo=UTC)
    with pytest.raises(ClaimError, match="Eingefroren bis"):
        projected_board(
            (issue,),
            (),
            (),
            (),
            raised_argument_1,
            now=raised_argument_2,
        )


def test_a_marker_swallowed_by_an_unclosed_fence_is_not_frozen() -> None:
    # An unclosed ~~~ fence runs to the end of the document per CommonMark, so
    # GitHub renders everything after it — including the two backtick lines
    # and the "marker" sitting between them — as one code block. The tool's
    # blindness here matches exactly what the operator sees in the issue UI:
    # no invisible divergence, so this is correctly read as not frozen.
    unclosed_fence_body = board_issue(
        10,
        "Unclosed fence",
        complete_contract("Claim #10.") + "\n\n## Notes\n\n"
        "~~~text\n"
        "placeholder\n"
        "```\n"
        "Eingefroren bis: real trigger candidate (Operator, 31.08.2026)\n"
        "```\n"
        "more text\n",
    )

    assert board.frozen_trigger(unclosed_fence_body.body) is None
    projected = projected_board(
        (unclosed_fence_body,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 31, tzinfo=UTC),
    )
    assert projected.items[0].actionable is True


def test_a_blockquoted_marker_still_freezes() -> None:
    # This repo already blockquotes operator rulings; a quoted freeze line
    # reads as the freeze itself, so over-freezing here is visible (SKIPPED
    # names it) rather than a silent, invisible un-freeze.
    quoted = board_issue(
        10,
        "Quoted ruling",
        complete_contract("Claim #10.")
        + "\n\n> Eingefroren bis: quoted real trigger (Operator, 31.08.2026)",
    )

    assert board.frozen_trigger(quoted.body) == "quoted real trigger"
    projected = projected_board(
        (quoted,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 31, tzinfo=UTC),
    )
    assert projected.items[0].actionable is False
    assert projected.items[0].actionable_reason == "frozen: quoted real trigger"


def test_a_tilde_fenced_example_is_not_a_live_marker() -> None:
    tilde_fenced = board_issue(
        10,
        "Tilde-fenced example",
        complete_contract("Claim #10.")
        + "\n\n~~~\nEingefroren bis: <trigger> (Operator, <Datum>)\n~~~\n",
    )

    assert board.frozen_trigger(tilde_fenced.body) is None
    projected = projected_board(
        (tilde_fenced,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 31, tzinfo=UTC),
    )
    assert projected.items[0].actionable is True


def test_an_info_stringed_delimiter_does_not_close_a_fence() -> None:
    # ```python carries an info string, so CommonMark/GitHub never read it as
    # a closing delimiter: the fence opened by ```text only closes at the
    # bare ``` on the next line, and the real marker after it is live prose.
    reopened_by_info_string = board_issue(
        10,
        "Info string does not close",
        complete_contract("Claim #10.") + "\n\n```text\nstuff\n```python\n```\n"
        "Eingefroren bis: real trigger (Operator, 31.08.2026)\n",
    )

    assert board.frozen_trigger(reopened_by_info_string.body) == "real trigger"
    projected = projected_board(
        (reopened_by_info_string,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 31, tzinfo=UTC),
    )
    assert projected.items[0].actionable is False
    assert projected.items[0].actionable_reason == "frozen: real trigger"


def test_an_info_stringed_middle_line_keeps_the_whole_block_one_fence() -> None:
    # Same shape, but the marker sits before the fence's only valid (bare)
    # closing line: GitHub renders ```text ... ``` as a single code block, so
    # the marker in the middle is fence content, never live.
    one_fence = board_issue(
        10,
        "Marker stays inside one fence",
        complete_contract("Claim #10.") + "\n\n```text\ninside\n```python\n"
        "Eingefroren bis: real trigger (Operator, 31.08.2026)\n```\n",
    )

    assert board.frozen_trigger(one_fence.body) is None
    projected = projected_board(
        (one_fence,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 31, tzinfo=UTC),
    )
    assert projected.items[0].actionable is True


def test_next_skips_a_frozen_item_and_names_it_as_such(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    frozen = board_issue(
        301,
        "Highest scored",
        complete_contract("Claim #301.") + f"\n\n{FROZEN_LINE}",
    )
    lower = board_issue(10, "Lower work", complete_contract("Claim #10."))
    client = _claims_client()
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (frozen, lower))
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    assert issue_claim.main(["--repo", "example/agent-claim", "next"]) == 0
    assert capsys.readouterr().out == (
        "#10 score -10: Lower work\n"
        "Next: Claim #10.\n"
        "\n"
        "SKIPPED\n"
        "#301: frozen: eine zweite Maschine bekommt einen Grund\n"
    )

    assert issue_claim.main(["--repo", "example/agent-claim", "next", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["number"] == 10
    assert payload["skipped"] == [
        {"number": 301, "reason": "frozen: eine zweite Maschine bekommt einen Grund"}
    ]


def test_claim_does_not_warn_about_a_frozen_higher_scored_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    client = _claims_client()
    frozen = board_issue(
        301,
        "Highest scored",
        complete_contract("Claim #301.") + f"\n\n{FROZEN_LINE}",
    )
    lower = board_issue(10, "Lower work", complete_contract("Claim #10."))
    claimed_request = request(issue=10, scope=("src/lower.py",))
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (frozen, lower))
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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


def test_board_keeps_the_first_projection_and_reports_duplicates() -> None:
    contract = board.parse_contract(
        "## Earlier section\n"
        "**Now:** An earlier section-local status.\n"
        "Next: An earlier section-local next step.\n"
        "**Blocked by:** #99\n"
        "Done when: The earlier section is complete.\n\n"
        "## Current projection\n"
        "**Now:** Fix the board parser.\n"
        "Next: Add a regression test.\n"
        "**Blocked by:** #47\n"
        "Done when: The review findings are resolved.\n"
    )

    assert contract == board.Contract(
        now="An earlier section-local status.",
        next="An earlier section-local next step.",
        blocked_by="#99",
        done_when="The earlier section is complete.",
        defects=(
            board.ContractDefect("Now", "duplicate Now projection field"),
            board.ContractDefect("Next", "duplicate Next projection field"),
            board.ContractDefect("Blocked by", "duplicate Blocked by projection field"),
            board.ContractDefect("Done when", "duplicate Done when projection field"),
        ),
    )


def test_board_ignores_fenced_projection_examples() -> None:
    contract = board.parse_contract(
        complete_contract("Claim #10.")
        + "\n\n```markdown\n"
        + "## Done when\n"
        + "This is only an example.\n"
        + "```"
    )

    assert contract.defects == ()


def test_board_reads_priority_configuration_from_the_checkout_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    open_blocker_references: Callable[[frozenset[int]], tuple[board.BlockerReference, ...]],
) -> None:
    toplevel = tmp_path / "checkout"
    configuration_directory = toplevel / ".agent-claim"
    configuration_directory.mkdir(parents=True)
    (configuration_directory / "board.toml").write_text('priority_labels = ["ux", "security"]\n')
    nested_directory = toplevel / "src" / "agent_claim"
    nested_directory.mkdir(parents=True)
    monkeypatch.chdir(nested_directory)
    observed: list[list[str]] = []

    def git_output(arguments: list[str]) -> str:
        observed.append(arguments)
        return str(toplevel)

    class BoardClient:
        repository = github._repository_id(REPOSITORY)

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

        def list_board_blockers(
            self, numbers: frozenset[int]
        ) -> tuple[board.BlockerReference, ...]:
            return open_blocker_references(numbers)

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
        "## Blocked by\n#20",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
    )

    projected = projected_board((blocker, product), (), (), (), board.BoardConfig(), now=now)

    assert [item.number for item in projected.items] == [20, 21]
    assert projected.items[0].unblocks_count == 1
    assert projected.items[1].open_blockers == (20,)


def test_board_never_counts_an_open_pull_request_as_a_blocker() -> None:
    dependent = board_issue(
        20, "Depends on a pull request", complete_contract("Ship it.", blocked_by="#86")
    )
    pull_request = board.BlockerReference(86, board.BlockerState.OPEN, True)

    projected = projected_board(
        (dependent,),
        (),
        (),
        (),
        board.BoardConfig(),
        blocker_references=(pull_request,),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    item = projected.items[0]
    assert item.open_blockers == ()
    assert item.actionable is True
    assert item.contract.defects == (
        board.ContractDefect("Blocked by", "blocker #86 is a pull request"),
    )


@pytest.mark.parametrize(
    ("blocker_references", "expected_freed_on"),
    [
        pytest.param(
            (
                board.BlockerReference(
                    10,
                    board.BlockerState.CLOSED,
                    False,
                    datetime(2026, 9, 1, tzinfo=UTC),
                ),
                board.BlockerReference(11, board.BlockerState.OPEN, False),
            ),
            None,
            id="one-blocker-remains-open",
        ),
        pytest.param(
            (
                board.BlockerReference(
                    10,
                    board.BlockerState.CLOSED,
                    False,
                    datetime(2026, 9, 1, tzinfo=UTC),
                ),
                board.BlockerReference(
                    11,
                    board.BlockerState.CLOSED,
                    False,
                    datetime(2026, 9, 3, tzinfo=UTC),
                ),
            ),
            datetime(2026, 9, 3, tzinfo=UTC),
            id="all-blockers-closed",
        ),
    ],
)
def test_board_records_the_latest_closed_issue_blocker(
    blocker_references: tuple[board.BlockerReference, ...], expected_freed_on: datetime | None
) -> None:
    freed = board_issue(20, "Freed", complete_contract("Ship it.", blocked_by="#10, #11"))
    unblocked = board_issue(21, "Never blocked", complete_contract("Ship it."))

    projected = projected_board(
        (freed, unblocked),
        (),
        (),
        (),
        board.BoardConfig(),
        blocker_references=blocker_references,
        now=datetime(2026, 9, 5, tzinfo=UTC),
    )
    by_number = {item.number: item for item in projected.items}

    assert by_number[20].freed_on == expected_freed_on
    assert by_number[21].freed_on is None


def test_board_reports_when_the_last_stale_blocker_closed() -> None:
    dependent = board_issue(20, "Freed", complete_contract("Ship it.", blocked_by="#10, #11"))
    blockers = (
        board.BlockerReference(
            10, board.BlockerState.CLOSED, False, datetime(2026, 9, 1, tzinfo=UTC)
        ),
        board.BlockerReference(
            11, board.BlockerState.CLOSED, False, datetime(2026, 9, 3, tzinfo=UTC)
        ),
    )

    projected = projected_board(
        (dependent,),
        (),
        (),
        (),
        board.BoardConfig(),
        blocker_references=blockers,
        now=datetime(2026, 9, 5, tzinfo=UTC),
    )

    item = json.loads(board.board_json(projected))["items"][0]
    assert item["freed_on"] == "2026-09-03"
    assert item["freed_days"] == 2


def test_build_board_refuses_when_github_omits_a_referenced_blocker() -> None:
    """A contract names a blocker, but the blocker snapshot GitHub actually
    returned does not include it at all -- never silently treat that as
    "no blocker", since that would let a slice through its own blocked-by
    gate."""
    dependent = board_issue(51, "Dependent", complete_contract("Ship it.", blocked_by="#9"))

    raised_argument_1 = board.BoardConfig()
    with pytest.raises(ClaimError, match="GitHub did not return blocker #9"):
        projected_board((dependent,), (), (), (), raised_argument_1, blocker_references=())


def test_build_board_refuses_a_closed_blocker_missing_closed_at() -> None:
    """A blocker GitHub reports closed but without a `closed_at` cannot be
    dated for the freed-on note; that is a malformed response, not a
    freshly-closed blocker with no timestamp yet."""
    dependent = board_issue(51, "Dependent", complete_contract("Ship it.", blocked_by="#9"))
    blockers = (board.BlockerReference(9, board.BlockerState.CLOSED, False),)

    raised_argument_1 = board.BoardConfig()
    with pytest.raises(ClaimError, match="GitHub did not return closed_at for blocker #9"):
        projected_board((dependent,), (), (), (), raised_argument_1, blocker_references=blockers)


def test_board_text_and_json_show_freed_on_and_freed_days() -> None:
    freed = board_issue(20, "Freed", complete_contract("Ship it.", blocked_by="#10"))
    blocked = board_issue(21, "Blocked", complete_contract("Ship it.", blocked_by="#11"))
    unblocked = board_issue(22, "Never blocked", complete_contract("Ship it."))
    blockers = (
        board.BlockerReference(
            10, board.BlockerState.CLOSED, False, datetime(2026, 9, 3, tzinfo=UTC)
        ),
        board.BlockerReference(11, board.BlockerState.OPEN, False),
    )

    projected = projected_board(
        (freed, blocked, unblocked),
        (),
        (),
        (),
        board.BoardConfig(),
        blocker_references=blockers,
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
        "## Blocked by\n#102",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
    )

    projected = projected_board(
        (container, last_child, blocker, dependent),
        (),
        (),
        (),
        board.BoardConfig(),
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
        "",
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
        "open_children": [{"number": 121, "state": "open", "blocked_by": []}],
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
    open_child = board_issue(121, "Open child", complete_contract("Ship it.", blocked_by="#130"))

    projected = projected_board(
        (container, blocker, open_child),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        children={120: (board.ChildItem(121, board.ChildState.OPEN),)},
    )

    assert "#120 0/1 closed; open: #121 (blocked by #130)" in board.render(projected)


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
        "## Now\nWork.\n\n## Next\nCut the next slice.\n\n"
        "## Blocked by\nnichts\n\n## Done when\nAll slices land.",
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
        "## Now\nWork.\n\n## Next\nkeiner\n\n## Blocked by\nnichts\n\n"
        "## Done when\nAll slices land.",
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
    an undispatched slice-table row (#112 finding 1)."""
    container = board.Issue(
        141,
        "Container",
        (),
        slice_table(("1", "Scheibe C", "—", "—")),
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


def test_board_json_and_render_report_an_uncut_slice_table_row() -> None:
    container = board.Issue(
        160,
        "Container",
        (),
        slice_table(("1", "Undispatched slice", "—", "—")),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    projected = projected_board(
        (container,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.uncut == (board.UncutSlices(160, ("Undispatched slice",)),)
    payload = json.loads(board.board_json(projected))
    assert payload["uncut"] == [{"item": 160, "rows": ["Undispatched slice"], "malformed": []}]
    assert "UNCUT\n#160: 1 rows (Undispatched slice)" in board.render(projected)


def test_board_json_and_render_name_a_malformed_row_by_its_cell_and_reason() -> None:
    """A malformed row (Container #79 with row id "B", 06.09.2026) is named
    by its `#` cell and reason -- text and JSON -- instead of only counted."""
    body = "| # | Scheibe | Item | Hängt ab von |\n|---|---|---|---|\n| B | Broken | — | — |\n"
    container = board.Issue(
        161,
        "Container",
        (),
        body,
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    projected = projected_board(
        (container,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    malformed_row = board.MalformedSliceRow(
        "| B | Broken | — | — |", "B", "index must be a positive integer"
    )
    assert projected.uncut == (board.UncutSlices(161, (), (malformed_row,)),)
    payload = json.loads(board.board_json(projected))
    assert payload["uncut"] == [
        {
            "item": 161,
            "rows": [],
            "malformed": [
                {
                    "line": "| B | Broken | — | — |",
                    "id_cell": "B",
                    "reason": "index must be a positive integer",
                }
            ],
        }
    ]
    assert 'UNCUT\n#161: row "B": index must be a positive integer' in board.render(projected)


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
        'Next: agent-claim cut 180 --title "Scheibe B — Kartenraster"\n'
    )


def test_next_prints_a_cut_command_that_cut_accepts_for_a_tableless_container(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """#151: `next`'s recommended `cut` command must never be one `cut`
    itself refuses -- exactly what #122 hit on 06.09.2026, whose container
    carried a `Next` line but no slice table."""
    container = board.Issue(
        183,
        "Epic",
        (),
        complete_contract("Scheibe D"),
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
    cut_arguments = shlex.split(command_line.removeprefix("Next: agent-claim "))

    cut_exit_code = issue_claim.main(["--repo", "example/agent-claim", *cut_arguments])

    assert cut_exit_code == 0
    child = client.next_created_child_number - 1
    assert capsys.readouterr().out == f"CUT #183 -> #{child}\n"


def test_next_prints_a_cut_command_that_cut_accepts_for_an_uncut_table_row(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The table-backed twin of the test above: a container whose own `Next`
    line is empty but whose slice table still carries an uncut row must also
    print a `cut` command that `cut` itself accepts (#151)."""
    container = board.Issue(
        184,
        "Epic",
        (),
        slice_table(("1", "Scheibe E", "—", "—")),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(container,))

    next_exit_code = issue_claim.main(["--repo", "example/agent-claim", "next"])
    assert next_exit_code == 0
    command_line = capsys.readouterr().out.splitlines()[1]
    cut_arguments = shlex.split(command_line.removeprefix("Next: agent-claim "))

    cut_exit_code = issue_claim.main(["--repo", "example/agent-claim", *cut_arguments])

    assert cut_exit_code == 0
    child = client.next_created_child_number - 1
    assert client.item_bodies == {184: slice_table(("1", "Scheibe E", f"#{child}", "—"))}


def test_next_prints_a_cut_command_that_cut_accepts_for_a_fully_linked_table(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    """The remaining #151 gap: a container whose slice table has every row
    already linked, but whose own `Next` line still names further work, must
    not have its `next`-printed `cut` command refused for lacking a cuttable
    row -- `cut` without `--row` creates an untied child instead, and the
    container's own body stays untouched."""
    container = board.Issue(
        185,
        "Epic",
        (),
        complete_contract("Weitere Aufgabe.")
        + "\n\n"
        + slice_table(("1", "Scheibe A", "#101", "—")),
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
    cut_arguments = shlex.split(command_line.removeprefix("Next: agent-claim "))

    cut_exit_code = issue_claim.main(["--repo", "example/agent-claim", *cut_arguments])

    assert cut_exit_code == 0
    child = client.next_created_child_number - 1
    assert client.created_children == [
        (185, "Weitere Aufgabe.", board.CHILD_SKELETON, board.ItemKind.TASK)
    ]
    assert client.item_bodies == {}
    assert capsys.readouterr().out == f"CUT #185 -> #{child}\n"


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


def test_next_names_the_boards_top_row_even_when_it_is_not_the_highest_score() -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    in_flight_unlabelled = board_issue(50, "In-flight, unlabelled", complete_contract("Ship it."))
    blocker = board_issue(
        51, "Prerequisite the operator prioritized", complete_contract("Unblock #52.")
    )
    dependent = board_issue(52, "Depends on the prerequisite", "## Blocked by\n#51")
    open_pull_request = board.PullRequest(200, "Fixes #50", "", "branch")

    projected = projected_board(
        (in_flight_unlabelled, blocker, dependent),
        (open_pull_request,),
        (),
        (),
        board.BoardConfig(),
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
    open_blocker_references: Callable[[frozenset[int]], tuple[board.BlockerReference, ...]],
) -> None:
    old_epic = replace(
        board_issue(70, "Epic open for months", complete_contract("Cut the next slice.")),
        created_at="2026-06-01T00:00:00Z",
    )
    recent_issue = board_issue(71, "Recently filed work", complete_contract("Ship it."))
    observed_since: list[datetime] = []

    class BoardClient:
        repository = github._repository_id(REPOSITORY)

        def list_open_board_issues(self) -> tuple[board.Issue, ...]:
            return (old_epic, recent_issue)

        def list_board_blockers(
            self, numbers: frozenset[int]
        ) -> tuple[board.BlockerReference, ...]:
            return open_blocker_references(numbers)

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


def test_board_loads_each_distinct_blocker_once(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    open_blocker_references: Callable[[frozenset[int]], tuple[board.BlockerReference, ...]],
) -> None:
    first = board_issue(80, "First", complete_contract("Ship it.", blocked_by="#90, #91"))
    second = board_issue(81, "Second", complete_contract("Ship it.", blocked_by="#90"))
    observed: list[frozenset[int]] = []

    class BoardClient:
        repository = github._repository_id(REPOSITORY)

        def list_open_board_issues(self) -> tuple[board.Issue, ...]:
            return (first, second)

        def list_board_blockers(
            self, numbers: frozenset[int]
        ) -> tuple[board.BlockerReference, ...]:
            observed.append(numbers)
            return open_blocker_references(numbers)

        def list_open_board_pull_requests(self) -> tuple[board.PullRequest, ...]:
            return ()

        def list_recent_merged_board_pull_requests(
            self, since: datetime
        ) -> tuple[board.PullRequest, ...]:
            return ()

        def list_children(self, number: int) -> tuple[board.ChildItem, ...]:
            return ()

    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    issue_claim._board(BoardClient(), ())

    assert observed == [frozenset({90, 91})]


def test_board_fetches_children_only_for_container_kinded_issues(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    open_blocker_references: Callable[[frozenset[int]], tuple[board.BlockerReference, ...]],
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

        def list_open_board_issues(self) -> tuple[board.Issue, ...]:
            return (container, plain)

        def list_board_blockers(
            self, numbers: frozenset[int]
        ) -> tuple[board.BlockerReference, ...]:
            return open_blocker_references(numbers)

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


def test_next_pulls_a_configured_projectionless_idea_with_refinement_step(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (tmp_path / ".agent-claim").mkdir()
    (tmp_path / ".agent-claim" / "board.toml").write_text('idea_label = "idea"\n')
    idea = board_issue(10, "Operator idea", "## Wunsch\nMake the board clearer.", labels=("idea",))
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(idea,))

    assert issue_claim.main(["--repo", "example/agent-claim", "next"]) == 0
    assert capsys.readouterr().out == (
        "#10 score -20: Operator idea\nNext: Problem neu prüfen und Item verfeinern\n"
    )

    assert issue_claim.main(["--repo", "example/agent-claim", "next", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "action": "work_item",
        "number": 10,
        "score": -20,
        "title": "Operator idea",
        "next": "Problem neu prüfen und Item verfeinern",
        "ruling_landings": None,
        "ruling_old": None,
        "recovery": [],
        "skipped": [],
    }
    assert client.comments[LEDGER_ISSUE] == []


def test_next_keeps_an_unlabelled_projectionless_item_skipped_with_an_active_idea_label(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    (tmp_path / ".agent-claim").mkdir()
    (tmp_path / ".agent-claim" / "board.toml").write_text('idea_label = "idea"\n')
    incomplete = board_issue(10, "Incomplete work", "## Wunsch\nInvestigate.")
    _configured_board_client(monkeypatch, tmp_path, open_issues=(incomplete,))

    assert issue_claim.main(["--repo", "example/agent-claim", "next"]) == 3
    assert capsys.readouterr().out == "No actionable item.\n\nSKIPPED\n#10: body incomplete\n"

    assert issue_claim.main(["--repo", "example/agent-claim", "next", "--json"]) == 3
    assert json.loads(capsys.readouterr().out) == {
        "recovery": [],
        "skipped": [{"number": 10, "reason": "body incomplete"}],
    }


def test_next_keeps_a_vision_labelled_projectionless_item_incomplete_without_configuration(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    idea = board_issue(10, "Operator vision", "## Wunsch\nInvestigate.", labels=("vision",))
    _configured_board_client(monkeypatch, tmp_path, open_issues=(idea,))

    assert issue_claim.main(["--repo", "example/agent-claim", "next"]) == 3
    assert capsys.readouterr().out == "No actionable item.\n\nSKIPPED\n#10: body incomplete\n"


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
    )


@pytest.mark.parametrize(
    ("body_suffix", "claims", "has_open_blocker", "expected_reason"),
    [
        pytest.param(
            f"\n\n{FROZEN_LINE}",
            (),
            False,
            "frozen: eine zweite Maschine bekommt einen Grund",
            id="frozen",
        ),
        pytest.param("", (request(issue=10, agent="Grok 4.6"),), False, "claimed", id="claimed"),
        pytest.param("\n\n## Blocked by\n#9", (), True, "blocked by #9", id="blocked"),
    ],
)
def test_a_configured_idea_keeps_freeze_claim_and_blocker_reasons(
    body_suffix: str,
    claims: tuple[ClaimRequest, ...],
    has_open_blocker: bool,
    expected_reason: str,
) -> None:
    idea = board_issue(
        10,
        "Operator idea",
        "## Wunsch\nMake the board clearer." + body_suffix,
        labels=("idea",),
    )
    blocker = board_issue(9, "Open blocker", complete_contract("Resolve the blocker."))
    active_claims = tuple(
        claim
        for claim_request in claims
        if isinstance(
            claim := parse_claim_event(comment(1, claim_comment(claim_request))), ActiveClaim
        )
    )
    projected = projected_board(
        (blocker, idea) if has_open_blocker else (idea,),
        (),
        (),
        active_claims,
        board.BoardConfig(idea_label="idea"),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )
    item = next(item for item in projected.items if item.number == idea.number)

    assert item not in projected.ready_now
    assert item.actionable_reason == expected_reason


def test_an_idea_without_a_priority_label_follows_the_regular_score_order() -> None:
    regular_work = board_issue(10, "Regular work", complete_contract("Ship the change."))
    idea = board_issue(11, "Operator idea", "## Wunsch\nMake the board clearer.", labels=("idea",))

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
        "## Wunsch\nImprove claims.",
        labels=("vision", "security"),
    )
    client = _configured_board_client(monkeypatch, tmp_path, open_issues=(lower, idea))
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
    assert client.comments[LEDGER_ISSUE] == []


def marker(payload: dict[str, object], *, legacy: bool = False, attributed: bool = True) -> str:
    version = "v1" if legacy else "v2"
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True)
    body = f"<!-- agent-claim:{version} {encoded} -->"
    agent = payload.get("agent")
    role = payload.get("role")
    if attributed and isinstance(agent, str) and isinstance(role, str):
        body += f"\n\nAgent: {agent} ({role})"
    return body


def release_event(claim: ActiveClaim, *, agent: str | None = None, role: str | None = None) -> str:
    return release_comment(
        claim,
        agent or claim.agent,
        role or claim.role,
        "landed",
    )


@pytest.mark.parametrize(
    ("lane", "expected_identity", "expected_branch"),
    [
        (False, IssueIdentity(71), "codex/issue-71-claims"),
        (True, LaneIdentity(), "docs/lane-claim-a"),
    ],
)
def test_claim_marker_round_trips_visible_contract(
    lane: bool, expected_identity: protocol.ClaimIdentity, expected_branch: str
) -> None:
    body = claim_comment(request(lane=lane))
    parsed = parse_claim_event(comment(1, body))

    assert isinstance(parsed, ActiveClaim)
    assert parsed.identity == expected_identity
    assert parsed.claim_id == "claim-a"
    assert parsed.base == BASE
    assert parsed.branch == expected_branch
    assert parsed.scope == ("docs/COORDINATION.md", "scripts/issue_claim.py")
    assert "Agent: Codex Sol (builder)" in body
    assert "Auto-Runner" in body


@pytest.mark.parametrize(
    ("body", "match"),
    [
        pytest.param(f"{protocol.MARKER_PREFIX}{{}}", "unterminated claim marker", id="no-suffix"),
        pytest.param(
            f"{protocol.MARKER_PREFIX}not-json{protocol.MARKER_SUFFIX}",
            "invalid claim JSON",
            id="invalid-json",
        ),
        pytest.param(
            f"{protocol.MARKER_PREFIX}[1,2,3]{protocol.MARKER_SUFFIX}",
            "claim payload must be an object",
            id="payload-not-an-object",
        ),
    ],
)
def test_marker_payload_fails_loud_on_a_malformed_marker(body: str, match: str) -> None:
    raised_argument_1 = comment(1, body)
    with pytest.raises(InvalidClaimMarkerError, match=match):
        parse_claim_event(raised_argument_1)


def test_required_text_refuses_a_non_string_marker_field() -> None:
    payload = _valid_claim_payload(claim_id=123)
    raised_argument_1 = comment(1, marker(payload))
    with pytest.raises(InvalidClaimMarkerError, match="claim marker field 'claim_id' must be text"):
        parse_claim_event(raised_argument_1)


def test_outbound_text_refuses_a_non_string_field() -> None:
    # `replace`'s `**changes` is typed `Any` in the standard library -- the
    # one boundary this malformed, non-`str` `agent` can enter through
    # without a field-by-field type lie.
    raised_argument_1 = replace(request(), agent=123)
    with pytest.raises(ClaimError, match="agent must be text"):
        claim_comment(raised_argument_1)


def test_outbound_resource_name_refuses_an_invalid_name() -> None:
    raised_argument_1 = request(resource="not valid!")
    with pytest.raises(ClaimError, match="resource is not a resource name"):
        claim_comment(raised_argument_1)


def _marker_payload_keys(body: str) -> frozenset[str]:
    first_line = body.partition("\n")[0]
    encoded = first_line[len(protocol.MARKER_PREFIX) : -len(protocol.MARKER_SUFFIX)]
    return frozenset(json.loads(encoded))


def test_lane_and_issue_claim_markers_use_different_key_sets() -> None:
    """Compatibility evidence for Entschieden #4: a pre-issue-38 reader always calls
    `_required_issue` on a non-legacy claim marker before dispatching on action; a
    lane marker never carries an `issue` key, so that reader fails loud on the whole
    ledger instead of silently skipping the comment it cannot understand."""
    issue_keys = _marker_payload_keys(claim_comment(request(lane=False)))
    lane_keys = _marker_payload_keys(claim_comment(request(lane=True)))

    assert "issue" in issue_keys
    assert "lane" not in issue_keys
    assert "lane" in lane_keys
    assert "issue" not in lane_keys
    assert issue_keys != lane_keys


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        pytest.param(
            {"action": "claim", "issue": 71, "lane": True},
            "must not carry both issue and lane",
            id="both-issue-and-lane",
        ),
        pytest.param(
            {"action": "claim", "lane": "yes"},
            "lane field must be true",
            id="lane-not-exactly-true",
        ),
        pytest.param(
            {"action": "claim"},
            "issue must be a positive integer",
            id="neither-issue-nor-lane",
        ),
    ],
)
def test_marker_identity_discriminator_refuses_ambiguous_or_missing_keys(
    payload: dict[str, object], match: str
) -> None:
    raised_argument_1 = comment(1, marker(payload))
    with pytest.raises(InvalidClaimMarkerError, match=match):
        parse_claim_event(raised_argument_1)


def test_protocol_parser_returns_action_specific_types() -> None:
    claimed = parse_claim_event(comment(1, claim_comment(request())))
    assert isinstance(claimed, ActiveClaim)

    released = parse_claim_event(comment(2, release_event(claimed)))
    assert isinstance(released, ClaimantRelease)
    assert released.reason == "landed"


def test_untrusted_claim_and_release_markers_are_ignored() -> None:
    claimed = parse_claim_event(comment(1, claim_comment(request())))
    assert isinstance(claimed, ActiveClaim)
    release = release_event(claimed)

    comments = (
        comment(1, claim_comment(request()), association="NONE"),
        comment(2, release, association="NONE"),
    )

    assert [parse_claim_event(entry) for entry in comments] == [None, None]
    assert active_claims(comments) == ()

    still_active = active_claims(
        (
            comment(1, claim_comment(request())),
            comment(2, release, association="NONE"),
        )
    )
    assert [claim.claim_id for claim in still_active] == ["claim-a"]


@pytest.mark.parametrize(
    "body",
    [
        "Review quotes <!-- agent-claim:v1 … --> as evidence.",
        "> <!-- agent-claim:v2 {} -->",
        "```html\n<!-- agent-claim:v2 {} -->\n```",
        "ordinary first line\n<!-- agent-claim:v2 {} -->",
    ],
)
def test_marker_is_protocol_only_as_the_exact_first_line(body: str) -> None:
    assert parse_claim_event(comment(1, body)) is None


def test_edited_protocol_comment_fails_loud() -> None:
    edited = comment(1, claim_comment(request()))
    edited = IssueComment(
        edited.identifier,
        edited.created_at,
        "2026-08-21T00:01:00Z",
        edited.body,
        edited.author_association,
        edited.url,
    )

    with pytest.raises(InvalidClaimMarkerError, match="edited after publication"):
        parse_claim_event(edited)


def test_fake_neutralize_claim_comment_bumps_updated_at_like_a_real_patch() -> None:
    """`FakeForge.neutralize_claim_comment` must mirror the real PATCH's effect on
    `updated_at`, so a comment edit that keeps a claim-marker-shaped first line still
    trips the "was edited after publication" guard in tests, not only in production."""
    claimed = comment(1, claim_comment(request()))
    client = FakeForge({LEDGER_ISSUE: [claimed]})

    client.neutralize_claim_comment(1, claimed.body)

    edited = client.comments[LEDGER_ISSUE][0]
    assert edited.updated_at != edited.created_at
    with pytest.raises(InvalidClaimMarkerError, match="edited after publication"):
        parse_claim_event(edited)


@pytest.mark.parametrize(
    "attribution",
    [None, "Agent: Other (builder)", "Agent: Codex Sol (reviewer)"],
)
def test_protocol_event_requires_exact_final_agent_attribution(
    attribution: str | None,
) -> None:
    payload = {
        "action": "claim",
        "agent": "Codex Sol",
        "base": BASE,
        "branch": "codex/issue-71-claims",
        "claim_id": "claim-a",
        "issue": 71,
        "role": "builder",
        "scope": ["AGENTS.md"],
    }
    body = marker(payload, attributed=False)
    if attribution is not None:
        body += f"\n\n{attribution}"

    raised_argument_1 = comment(1, body)
    with pytest.raises(InvalidClaimMarkerError, match="exact agent attribution"):
        parse_claim_event(raised_argument_1)


@pytest.mark.parametrize(
    "invalid",
    ["Codex\nSol", "Codex\x1fSol", " ", "x" * 129],
)
def test_outbound_comment_constructors_reject_controlled_identity_fields(
    invalid: str,
) -> None:
    claimed = parse_claim_event(comment(1, claim_comment(request())))
    assert isinstance(claimed, ActiveClaim)

    raised_argument_1 = replace(request(), agent=invalid)
    with pytest.raises(ClaimError, match="agent must be one bounded non-empty line"):
        claim_comment(raised_argument_1)
    with pytest.raises(ClaimError, match="agent must be one bounded non-empty line"):
        release_comment(claimed, invalid, "builder", "landed")
    with pytest.raises(ClaimError, match="agent must be one bounded non-empty line"):
        supersede_comment(claimed, 170, invalid, "coordinator", "rollover")

    raised_argument_1 = replace(request(), role=invalid)
    with pytest.raises(ClaimError, match="role must be one bounded non-empty line"):
        claim_comment(raised_argument_1)
    with pytest.raises(ClaimError, match="role must be one bounded non-empty line"):
        release_comment(claimed, "Codex Sol", invalid, "landed")
    with pytest.raises(ClaimError, match="role must be one bounded non-empty line"):
        supersede_comment(claimed, 170, "Codex Sol", invalid, "rollover")


@pytest.mark.parametrize(
    "invalid",
    ["landed\nwith detail", "landed\x1fdetail", " ", "x" * 513],
)
def test_outbound_comment_constructors_reject_controlled_reasons(invalid: str) -> None:
    claimed = parse_claim_event(comment(1, claim_comment(request())))
    assert isinstance(claimed, ActiveClaim)

    with pytest.raises(ClaimError, match="reason must be one bounded non-empty line"):
        release_comment(claimed, "Codex Sol", "builder", invalid)
    with pytest.raises(ClaimError, match="reason must be one bounded non-empty line"):
        supersede_comment(claimed, 170, "Codex Sol", "coordinator", invalid)


def test_legacy_bootstrap_claim_is_read_only_when_marker_is_first_line() -> None:
    legacy = marker(
        {
            "action": "claim",
            "agent": "Codex Sol",
            "base": BASE,
            "branch": "codex/issue-71-claims",
            "claim_id": "bootstrap",
            "role": "builder",
            "scope": ["AGENTS.md"],
        },
        legacy=True,
    )

    parsed = parse_claim_event(comment(1, legacy))

    assert isinstance(parsed, ActiveClaim)
    assert parsed.identity == IssueIdentity(LEDGER_ISSUE)
    assert parsed.claim_id == "bootstrap"


def test_parse_claim_event_refuses_an_unknown_action() -> None:
    raised_argument_1 = comment(1, marker({"action": "bogus"}))
    with pytest.raises(InvalidClaimMarkerError, match="has unknown action 'bogus'"):
        parse_claim_event(raised_argument_1)


def test_parse_claim_event_refuses_a_legacy_marker_using_a_v2_only_action() -> None:
    raised_argument_1 = comment(1, marker({"action": "rescope"}, legacy=True))
    with pytest.raises(
        InvalidClaimMarkerError, match="legacy claim markers cannot use this action"
    ):
        parse_claim_event(raised_argument_1)


def _valid_override_release_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "action": "override_release",
        "agent": "Fleet Coordinator",
        "claim_comment_id": 5,
        "claim_id": "claim-a",
        "issue": 71,
        "reason": "reviewed rollover ready",
        "role": "coordinator",
    }
    payload.update(overrides)
    return payload


def _valid_supersede_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "action": "supersede",
        "agent": "Fleet Coordinator",
        "claim_comment_id": 5,
        "claim_id": "claim-a",
        "issue": 71,
        "reason": "rollover",
        "role": "coordinator",
        "successor_issue": 170,
    }
    payload.update(overrides)
    return payload


def test_override_release_requires_coordinator_role() -> None:
    payload = _valid_override_release_payload(role="builder")
    raised_argument_1 = comment(1, marker(payload))
    with pytest.raises(InvalidClaimMarkerError, match="override releases require coordinator role"):
        parse_claim_event(raised_argument_1)


def test_ledger_supersede_requires_coordinator_role() -> None:
    payload = _valid_supersede_payload(role="builder")
    raised_argument_1 = comment(1, marker(payload))
    with pytest.raises(InvalidClaimMarkerError, match="ledger supersede requires coordinator role"):
        parse_claim_event(raised_argument_1)


@pytest.mark.parametrize(
    ("payload_builder", "match"),
    [
        pytest.param(
            _valid_override_release_payload,
            "override releases requires a positive claim comment id",
            id="override-release",
        ),
        pytest.param(
            _valid_supersede_payload,
            "ledger supersede requires a positive claim comment id",
            id="ledger-supersede",
        ),
    ],
)
def test_required_comment_id_rejects_a_non_positive_value(
    payload_builder: Callable[..., dict[str, object]], match: str
) -> None:
    payload = payload_builder(claim_comment_id=0)
    raised_argument_1 = comment(1, marker(payload))
    with pytest.raises(InvalidClaimMarkerError, match=match):
        parse_claim_event(raised_argument_1)


def test_legacy_marker_fails_loud_with_a_clear_message_before_ledger_is_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A legacy marker binds to `LEDGER_ISSUE`; parsing one before `configure_ledger`
    runs must report the real defect (caller/setup), not misreport it as if the
    marker itself carried an invalid issue number."""
    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 0)
    legacy = marker(
        {
            "action": "claim",
            "agent": "Codex Sol",
            "base": BASE,
            "branch": "codex/issue-71-claims",
            "claim_id": "bootstrap",
            "role": "builder",
            "scope": ["AGENTS.md"],
        },
        legacy=True,
    )

    raised_argument_1 = comment(1, legacy)
    with pytest.raises(ClaimError, match="before configure_ledger"):
        parse_claim_event(raised_argument_1)


@pytest.mark.parametrize(
    ("branch", "scope"),
    [
        ("../not-a-branch", ["src"]),
        ("topic//double", ["src"]),
        ("topic.lock", ["src"]),
        ("topic", ["/home/operator/repo"]),
        ("topic", ["C:\\Users\\operator\\secret.txt"]),
        ("topic", ["C:/Users/operator/secret.txt"]),
        ("topic", ["\\\\server\\share\\secret.txt"]),
        ("topic", ["../other-repo"]),
        ("topic", ["."]),
        ("topic", ["./src"]),
        ("topic", ["src//file.py"]),
        ("topic", [".git/config"]),
    ],
)
def test_invalid_branch_and_private_or_noncanonical_scope_fail_loud(
    branch: str, scope: list[str]
) -> None:
    payload = {
        "action": "claim",
        "agent": "Codex Sol",
        "base": BASE,
        "branch": branch,
        "claim_id": "claim-a",
        "issue": 71,
        "role": "builder",
        "scope": scope,
    }

    raised_argument_1 = comment(1, marker(payload))
    with pytest.raises(
        InvalidClaimMarkerError,
        match=(
            r"claim marker branch is not a safe Git ref|claim scope (entries must be "
            r"canonical bounded paths|must be repository-relative)"
        ),
    ):
        parse_claim_event(raised_argument_1)


def test_claim_comment_refuses_a_nul_byte_in_its_rendered_body() -> None:
    """A NUL byte can only arrive through a field `claim_comment` does not itself
    sanitize -- `scope` entries flow straight into the rendered body, so this is
    the one field that reaches `_validated_comment`'s NUL-byte guard unfiltered."""
    raised_argument_1 = request(scope=("src/new.py\x00",))
    with pytest.raises(ClaimError, match="contains a NUL byte"):
        claim_comment(raised_argument_1)


def test_supersede_comment_requires_an_issue_identified_claim() -> None:
    """Guardrail (Entschieden #6): supersede stays ledger-issue-only, never a
    lane -- reachable directly on this writer helper even though
    `supersede_ledger` itself only ever calls it with an issue-identified
    claim already validated as the ledger's own."""
    lane_claim = parse_claim_event(comment(1, claim_comment(request(lane=True))))
    assert isinstance(lane_claim, ActiveClaim)

    with pytest.raises(ClaimError, match="ledger supersede requires an issue-identified claim"):
        protocol.supersede_comment(lane_claim, 170, "Fleet Coordinator", "coordinator", "reviewed")


def test_missing_marker_fields_fail_loud() -> None:
    """A field this reader requires being absent is a corrupt record, not a newer
    writer (issue #136): it still fails the whole comment, verbatim as before."""
    unknown = {
        "action": "claim",
        "agent": "Codex Sol",
        "base": BASE,
        "branch": "topic",
        "claim_id": "claim-a",
        "issue": 71,
        "role": "builder",
        "scope": ["src"],
        "surprise": True,
    }

    raised_argument_1 = comment(2, marker({"action": "claim"}))
    with pytest.raises(
        InvalidClaimMarkerError, match="claim marker issue must be a positive integer"
    ):
        parse_claim_event(raised_argument_1)
    missing = {key: value for key, value in unknown.items() if key not in {"surprise", "scope"}}
    raised_argument_1 = comment(3, marker(missing))
    with pytest.raises(InvalidClaimMarkerError, match=r"fields differ(?!.*upgrade)"):
        parse_claim_event(raised_argument_1)


def _valid_claim_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "action": "claim",
        "agent": "Codex Sol",
        "base": BASE,
        "branch": "codex/issue-71-claims",
        "claim_id": "claim-a",
        "issue": 71,
        "role": "builder",
        "scope": ["src"],
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        pytest.param({"claim_id": "bad id"}, "has an invalid claim id", id="invalid-claim-id"),
        pytest.param(
            {"base": "not-a-valid-sha"},
            "must be a full lowercase commit SHA",
            id="invalid-base",
        ),
        pytest.param(
            {"resource_value": 1},
            "resource_value requires resource",
            id="resource-value-without-resource",
        ),
        pytest.param(
            {"resource": "not valid!", "resource_value": 1},
            "is not a resource name",
            id="invalid-resource-name",
        ),
        pytest.param(
            {"resource": "schema-hop", "resource_value": 0},
            "resource_value must be a positive integer",
            id="invalid-resource-value",
        ),
        pytest.param({"scope": None}, "non-empty list", id="scope-not-a-list"),
        pytest.param({"scope": []}, "non-empty list", id="scope-empty-list"),
        pytest.param({"scope": [123]}, "entries must be text", id="scope-entry-not-text"),
        pytest.param(
            {"scope": ["src"] * (protocol.MAX_SCOPE_ENTRIES + 1)},
            f"exceeds {protocol.MAX_SCOPE_ENTRIES} entries",
            id="scope-too-many-entries",
        ),
        pytest.param({"scope": ["src", "src"]}, "duplicate paths", id="scope-duplicate-paths"),
    ],
)
def test_parse_active_claim_fails_loud_on_malformed_fields(
    overrides: dict[str, object], match: str
) -> None:
    payload = _valid_claim_payload(**overrides)
    raised_argument_1 = comment(1, marker(payload))
    with pytest.raises(InvalidClaimMarkerError, match=match):
        parse_claim_event(raised_argument_1)


def test_unknown_marker_field_becomes_an_unreadable_claim_not_a_ledger_failure() -> None:
    """A trusted comment with every required field present, plus one this reader's
    schema does not know, is a newer `agent-claim` writer (issue #136): `parse_claim_event`
    signals it as an `UnreadableClaim`, distinct from the hard `InvalidClaimMarkerError`
    a corrupt (missing-field) record still raises. `surprise` stands in for a field a
    future minor release adds -- `whole` (#113) is already a known optional field."""
    payload = {
        "action": "claim",
        "agent": "Codex Sol",
        "base": BASE,
        "branch": "topic",
        "claim_id": "claim-a",
        "issue": 71,
        "role": "builder",
        "scope": ["src"],
        "surprise": True,
    }

    raised_argument_1 = comment(1, marker(payload))
    with pytest.raises(
        protocol.UnreadableClaimError, match="unreadable, upgrade the installed tool"
    ) as excinfo:
        parse_claim_event(raised_argument_1)
    unreadable = excinfo.value.claim
    assert unreadable.claim_id == "claim-a"
    assert unreadable.comment_url == raised_argument_1.url
    assert unreadable.unknown_fields == ("surprise",)


def test_unreadable_claim_has_no_claim_id_when_its_own_is_unparseable() -> None:
    """`claim_id` on an `UnreadableClaim` is a best-effort read: when the field
    that would normally identify it is itself missing or malformed, it stays
    `None` rather than a guess -- the comment is still named by its URL."""
    payload = {
        "action": "claim",
        "agent": "Codex Sol",
        "base": BASE,
        "branch": "topic",
        "claim_id": "bad id with spaces",
        "issue": 71,
        "role": "builder",
        "scope": ["src"],
        "surprise": True,
    }

    unreadable = protocol.unreadable_claims((comment(1, marker(payload)),))

    assert unreadable[0].claim_id is None


def test_aggregation_fences_an_unknown_field_comment_instead_of_failing_the_ledger() -> None:
    """The bug this issue fixes: one v0.11-shaped comment among v0.10 comments used to
    fail `active_claims` outright (`trusted comment ... claim fields differ`), which
    broke `board`/`next`/`pr-check` for every other lane too. It now becomes one
    `UnreadableClaim`, and every other comment still reads normally."""
    readable = claim_comment(request(issue=72, scope=("src",)))
    newer_writer = marker(
        {
            "action": "claim",
            "agent": "Grok 4.6",
            "base": BASE,
            "branch": "codex/issue-73-claims",
            "claim_id": "claim-b",
            "issue": 73,
            "role": "builder",
            "scope": ["docs"],
            "surprise": True,
        }
    )
    ledger = (comment(1, readable), comment(2, newer_writer))

    assert [claim.claim_id for claim in active_claims(ledger)] == ["claim-a"]
    unreadable = protocol.unreadable_claims(ledger)
    assert len(unreadable) == 1
    assert unreadable[0].claim_id == "claim-b"
    assert unreadable[0].unknown_fields == ("surprise",)


def test_an_unreadable_rescope_quarantines_its_still_readable_claim() -> None:
    """Finding 1 (issue #136): a claim posted normally, then rescoped by a newer
    writer whose rescope this reader cannot parse, stays active and readable --
    but `ActiveClaim.quarantined_by` now names the rescope comment that fences it.
    This internal attachment is what `release`/`pr-check` key their refusal off of
    (exercised through the CLI below); a raw dataclass field is not something the
    CLI surfaces directly, so it is pinned here instead."""
    claimed = claim_comment(request(issue=72, scope=("src",)))
    newer_rescope = marker(
        {
            "action": "rescope",
            "agent": "Codex Sol",
            "claim_id": "claim-a",
            "issue": 72,
            "role": "builder",
            "scope": ["src", "docs"],
            "surprise": True,
        }
    )
    ledger = (comment(1, claimed), comment(2, newer_rescope))

    standing = active_claims(ledger)

    assert [claim.claim_id for claim in standing] == ["claim-a"]
    quarantine = standing[0].quarantined_by
    assert quarantine is not None
    assert quarantine.claim_id == "claim-a"
    assert quarantine.unknown_fields == ("surprise",)
    # The claim's own scope is untouched: the unreadable rescope never applied.
    assert standing[0].scope == ("src",)


def test_release_must_come_from_original_claimant() -> None:
    claimed_body = claim_comment(request())
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    foreign_release = release_event(claimed, agent="Other", role="builder")

    raised_argument_1 = comment(1, claimed_body)
    raised_argument_2 = comment(2, foreign_release)
    with pytest.raises(InvalidClaimMarkerError, match="only be released by its claimant"):
        active_claims((raised_argument_1, raised_argument_2))


def test_coordinator_override_is_explicit_and_bound_to_claim_comment() -> None:
    claimed_body = claim_comment(request())
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    override = release_comment(
        claimed,
        "Codex Commissioner",
        "coordinator",
        "verified abandoned",
        coordinator_override=True,
    )

    assert active_claims((comment(1, claimed_body), comment(2, override))) == ()

    first_line = override.partition("\n")[0]
    payload = json.loads(first_line.removeprefix("<!-- agent-claim:v2 ").removesuffix(" -->"))
    payload["claim_comment_id"] = 999
    raised_argument_1 = comment(1, claimed_body)
    raised_argument_2 = comment(2, marker(payload))
    with pytest.raises(InvalidClaimMarkerError, match="wrong claim comment"):
        active_claims((raised_argument_1, raised_argument_2))


def test_release_refuses_a_mismatched_identity() -> None:
    """A release event whose own identity marker names a different issue than
    the claim it targets by claim_id must fail loud, never silently release
    the wrong claim."""
    claimed_body = claim_comment(request(issue=71))
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    wrong_identity_claim = replace(claimed, identity=IssueIdentity(72))
    released = release_event(wrong_identity_claim)

    raised_argument_1 = comment(1, claimed_body)
    raised_argument_2 = comment(2, released)
    with pytest.raises(InvalidClaimMarkerError, match="release targets the wrong claim"):
        active_claims((raised_argument_1, raised_argument_2))


def test_rescope_refuses_a_claim_id_rescoped_after_release() -> None:
    claimed_body = claim_comment(request())
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    released = release_event(claimed)
    rescope = protocol.rescope_comment(claimed, ("src",), claimed.agent, claimed.role)

    raised_argument_1 = comment(1, claimed_body)
    raised_argument_2 = comment(2, released)
    raised_argument_3 = comment(3, rescope)
    with pytest.raises(InvalidClaimMarkerError, match="was rescoped after it was released"):
        active_claims((raised_argument_1, raised_argument_2, raised_argument_3))


def test_rescope_refuses_a_claim_id_never_acquired() -> None:
    claimed_body = claim_comment(request())
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    rescope = protocol.rescope_comment(claimed, ("src",), claimed.agent, claimed.role)

    raised_argument_1 = comment(1, rescope)
    with pytest.raises(InvalidClaimMarkerError, match="was rescoped before it was acquired"):
        active_claims((raised_argument_1,))


def test_rescope_refuses_a_mismatched_identity() -> None:
    claimed_body = claim_comment(request(issue=71))
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    wrong_identity_claim = replace(claimed, identity=IssueIdentity(72))
    rescope = protocol.rescope_comment(wrong_identity_claim, ("src",), claimed.agent, claimed.role)

    raised_argument_1 = comment(1, claimed_body)
    raised_argument_2 = comment(2, rescope)
    with pytest.raises(InvalidClaimMarkerError, match="rescope targets the wrong claim"):
        active_claims((raised_argument_1, raised_argument_2))


def test_rescope_refuses_an_agent_other_than_the_claimant() -> None:
    claimed_body = claim_comment(request())
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    rescope = protocol.rescope_comment(claimed, ("src",), "Other Agent", claimed.role)

    raised_argument_1 = comment(1, claimed_body)
    raised_argument_2 = comment(2, rescope)
    with pytest.raises(InvalidClaimMarkerError, match="can only be rescoped by its claimant"):
        active_claims((raised_argument_1, raised_argument_2))


def test_active_claims_strict_reader_refuses_reused_claim_ids_and_orphan_releases() -> None:
    """`active_claims` (the strict reader behind status/claim/release) still refuses a
    poisoned ledger outright; only `acquire_claim`'s pre-post guard and `reconcile`'s
    tolerant repair pass are allowed to treat a duplicate claim id as recoverable."""
    claimed_body = claim_comment(request())
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    released = release_event(claimed)

    raised_argument_1 = comment(1, claimed_body)
    raised_argument_2 = comment(2, released)
    raised_argument_3 = comment(3, claimed_body)
    with pytest.raises(InvalidClaimMarkerError, match="was reused"):
        active_claims(
            (
                raised_argument_1,
                raised_argument_2,
                raised_argument_3,
            )
        )
    raised_argument_1 = comment(1, released)
    with pytest.raises(InvalidClaimMarkerError, match="before it was acquired"):
        active_claims((raised_argument_1,))


def test_duplicate_claimant_releases_are_idempotent() -> None:
    claimed_body = claim_comment(request())
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    first_release = release_comment(claimed, "Codex Sol", "builder", "landed")
    second_release = release_comment(claimed, "Codex Sol", "builder", "landed retry")

    assert (
        active_claims(
            (
                comment(1, claimed_body),
                comment(2, first_release),
                comment(3, second_release),
            )
        )
        == ()
    )


@pytest.mark.parametrize("override_first", [False, True])
def test_claimant_and_coordinator_release_race_is_idempotent(
    override_first: bool,
) -> None:
    claimed_body = claim_comment(request())
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    claimant = release_comment(claimed, "Codex Sol", "builder", "landed")
    coordinator = release_comment(
        claimed,
        "Fleet Coordinator",
        "coordinator",
        "verified handoff",
        coordinator_override=True,
    )
    releases = (coordinator, claimant) if override_first else (claimant, coordinator)

    assert (
        active_claims(
            (
                comment(1, claimed_body),
                comment(2, releases[0]),
                comment(3, releases[1]),
            )
        )
        == ()
    )


def test_supersede_atomically_terminates_the_only_ledger_claim() -> None:
    claimed_body = claim_comment(request(issue=LEDGER_ISSUE))
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    frozen = supersede_comment(
        claimed,
        170,
        "Fleet Coordinator",
        "coordinator",
        "reviewed rollover ready to land",
    )
    parsed = parse_claim_event(comment(2, frozen))
    assert isinstance(parsed, LedgerSupersede)
    assert parsed.successor_issue == 170

    raised_argument_1 = comment(1, claimed_body)
    raised_argument_2 = comment(2, frozen)
    with pytest.raises(LedgerSupersededError, match="successor #170"):
        active_claims((raised_argument_1, raised_argument_2))
    late_claim = comment(
        3,
        claim_comment(request("late", issue=72, scope=("frontend",))),
    )
    raised_argument_1 = comment(1, claimed_body)
    raised_argument_2 = comment(2, frozen)
    with pytest.raises(LedgerSupersededError, match="successor #170"):
        active_claims((raised_argument_1, raised_argument_2, late_claim))


def test_supersede_is_an_inert_rejected_event_while_another_lane_is_active() -> None:
    rollover_body = claim_comment(request(issue=LEDGER_ISSUE, scope=("docs",)))
    rollover = parse_claim_event(comment(1, rollover_body))
    assert isinstance(rollover, ActiveClaim)
    other = comment(
        2,
        claim_comment(request("other", issue=72, scope=("frontend",))),
    )
    frozen = comment(
        3,
        supersede_comment(
            rollover,
            170,
            "Fleet Coordinator",
            "coordinator",
            "not actually drained",
        ),
    )

    observed = active_claims((comment(1, rollover_body), other, frozen))

    assert [claim.claim_id for claim in observed] == [rollover.claim_id, "other"]


def test_supersede_command_posts_terminal_event_and_observes_freeze() -> None:
    client = FakeForge(valid_successors={170})
    acquired = acquire_claim(client, request(issue=LEDGER_ISSUE))

    selected = supersede_ledger(
        client,
        supersede_request(
            170, "Fleet Coordinator", "coordinator", "reviewed successor ready", acquired.claim_id
        ),
    )

    assert selected == acquired
    assert LEDGER_ISSUE not in client.labels
    raised_argument_1 = client.list_protocol_candidates(LEDGER_ISSUE)
    with pytest.raises(LedgerSupersededError, match="successor #170"):
        active_claims(raised_argument_1)


def test_supersede_reraises_a_pre_existing_supersede_that_does_not_match_this_request() -> None:
    """A ledger already superseded by a *different* request (a different
    successor issue or claim id) is not this request's own idempotent retry
    -- it is a genuine conflict, and the original error must propagate."""
    claimed_body = claim_comment(request(issue=LEDGER_ISSUE))
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    foreign_supersede = supersede_comment(
        claimed, 170, "Other Coordinator", "coordinator", "already superseded"
    )
    client = FakeForge({LEDGER_ISSUE: [comment(1, claimed_body), comment(2, foreign_supersede)]})

    mismatched_request = supersede_request(
        170, "Fleet Coordinator", "coordinator", "different reason", "a-different-claim-id"
    )
    with pytest.raises(LedgerSupersededError, match="successor #170"):
        supersede_ledger(client, mismatched_request)


def test_supersede_reraises_when_a_foreign_supersede_wins_the_post_mutation_race() -> None:
    """Analogous to the pre-existing case above, but the foreign supersede
    lands during this request's own post -- the post-mutation re-check must
    still recognize it as someone else's event, not this request's own."""
    client = FakeForge(valid_successors={170, 999})
    acquired = acquire_claim(client, request(issue=LEDGER_ISSUE))
    foreign_supersede = supersede_comment(
        acquired, 999, "Other Coordinator", "coordinator", "a different rollover"
    )
    client.inject_before_next_ledger_post = comment(
        50, foreign_supersede, created_at="2026-08-21T00:00:01Z"
    )

    raised_argument_1 = supersede_request(
        170, "Fleet Coordinator", "coordinator", "reviewed successor ready", acquired.claim_id
    )
    with pytest.raises(LedgerSupersededError, match="successor #999"):
        supersede_ledger(client, raised_argument_1)


def test_supersede_race_loses_cleanly_without_poisoning_the_ledger() -> None:
    client = FakeForge(valid_successors={170})
    acquired = acquire_claim(
        client,
        request(issue=LEDGER_ISSUE, scope=("docs",)),
    )
    competitor = comment(
        50,
        claim_comment(request("other", issue=72, scope=("frontend",))),
        created_at="2026-08-21T00:00:01Z",
    )
    client.inject_before_next_ledger_post = competitor

    race_supersede_request = supersede_request(
        170, "Fleet Coordinator", "coordinator", "race should reject", acquired.claim_id
    )
    with pytest.raises(ClaimError, match="not observed"):
        supersede_ledger(client, race_supersede_request)

    observed = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert {claim.claim_id for claim in observed} == {acquired.claim_id, "other"}


def test_supersede_label_failure_can_be_retried_without_reposting_event() -> None:
    client = FakeForge(valid_successors={170}, fail_remove_label=True)
    acquired = acquire_claim(client, request(issue=LEDGER_ISSUE))

    label_failure_supersede_request = supersede_request(
        170,
        "Fleet Coordinator",
        "coordinator",
        "reviewed successor ready",
        acquired.claim_id,
    )
    with pytest.raises(ClaimError, match="label remove failed"):
        supersede_ledger(client, label_failure_supersede_request)
    protocol_count = len(client.list_protocol_candidates(LEDGER_ISSUE))
    assert LEDGER_ISSUE in client.labels

    client.fail_remove_label = False
    client.valid_successors.clear()  # The successor may already have accepted new claims.
    supersede_ledger(
        client,
        supersede_request(
            170, "Fleet Coordinator", "coordinator", "reviewed successor ready", acquired.claim_id
        ),
    )

    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == protocol_count
    assert LEDGER_ISSUE not in client.labels


def test_supersede_refuses_an_unverified_successor_before_posting() -> None:
    client = FakeForge()
    acquired = acquire_claim(client, request(issue=LEDGER_ISSUE))
    protocol_count = len(client.list_protocol_candidates(LEDGER_ISSUE))

    unverified_successor_request = supersede_request(
        999999, "Fleet Coordinator", "coordinator", "invalid successor", acquired.claim_id
    )
    with pytest.raises(ClaimUnavailableError, match="open, empty, collaborator-locked"):
        supersede_ledger(client, unverified_successor_request)

    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == protocol_count


def test_supersede_requires_a_higher_numbered_successor() -> None:
    client = FakeForge(valid_successors={70})
    acquired = acquire_claim(client, request(issue=LEDGER_ISSUE))
    protocol_count = len(client.list_protocol_candidates(LEDGER_ISSUE))

    with pytest.raises(ClaimError, match="greater than the current ledger"):
        supersede_comment(
            acquired,
            70,
            "Fleet Coordinator",
            "coordinator",
            "invalid rollover",
        )
    rollover_supersede_request = supersede_request(
        70, "Fleet Coordinator", "coordinator", "invalid rollover", acquired.claim_id
    )
    with pytest.raises(ClaimUnavailableError, match="greater than the current ledger"):
        supersede_ledger(client, rollover_supersede_request)

    raised_argument_1 = comment(
        2,
        marker(
            {
                "action": "supersede",
                "agent": "Fleet Coordinator",
                "claim_comment_id": acquired.comment.identifier,
                "claim_id": acquired.claim_id,
                "issue": LEDGER_ISSUE,
                "reason": "invalid rollover",
                "role": "coordinator",
                "successor_issue": 70,
            }
        ),
    )
    with pytest.raises(InvalidClaimMarkerError, match="greater than the current ledger"):
        parse_claim_event(raised_argument_1)

    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == protocol_count


def test_scope_overlap_is_repository_wide_and_path_aware() -> None:
    left = request(issue=71, scope=("frontend/src",))
    nested = request("claim-b", issue=72, scope=("frontend/src/lib/player.ts",))
    sibling = request("claim-c", issue=73, scope=("frontend/tests",))

    assert not claims_conflict(left, nested)
    assert protocol.claims_overlap(left, nested)
    assert not claims_conflict(left, sibling)
    assert not protocol.claims_overlap(left, sibling)


def test_comma_joined_scope_marker_is_read_as_distinct_paths() -> None:
    parsed = parse_claim_event(
        comment(
            1,
            marker(
                {
                    "action": "claim",
                    "agent": "Codex Sol",
                    "base": BASE,
                    "branch": "codex/issue-71-claims",
                    "claim_id": "claim-a",
                    "issue": 71,
                    "role": "builder",
                    "scope": ["docs/PRODUCT.md,src/atelier2/adapters/dbos/run_transitions.py"],
                }
            ),
        )
    )

    assert isinstance(parsed, ActiveClaim)
    assert parsed.scope == (
        "docs/PRODUCT.md",
        "src/atelier2/adapters/dbos/run_transitions.py",
    )


def test_comma_joined_scope_on_another_issue_is_an_overlap_note_not_a_refusal() -> None:
    incumbent = comment(
        1,
        marker(
            {
                "action": "claim",
                "agent": "Codex Sol",
                "base": BASE,
                "branch": "codex/issue-72-claims",
                "claim_id": "joined",
                "issue": 72,
                "role": "builder",
                "scope": ["docs/PRODUCT.md,src/atelier2/adapters/dbos/run_transitions.py"],
            }
        ),
    )
    client = FakeForge({LEDGER_ISSUE: [incumbent]}, {72})

    acquired = acquire_claim(
        client,
        request("challenger", "Grok 4.6", issue=73, scope=("docs/PRODUCT.md",)),
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert {claim.claim_id for claim in standing} == {"joined", acquired.claim_id}
    assert [claim.claim_id for claim in protocol.overlapping_claims(standing, acquired)] == [
        "joined"
    ]


def test_comma_joined_scope_with_spaces_equals_repeated_entries() -> None:
    parsed = parse_claim_event(
        comment(
            1,
            marker(
                {
                    "action": "claim",
                    "agent": "Codex Sol",
                    "base": BASE,
                    "branch": "codex/issue-71-claims",
                    "claim_id": "claim-a",
                    "issue": 71,
                    "role": "builder",
                    "scope": ["docs/PRODUCT.md, src/widget.py"],
                }
            ),
        )
    )

    assert isinstance(parsed, ActiveClaim)
    assert parsed.scope == ("docs/PRODUCT.md", "src/widget.py")


@pytest.mark.parametrize(
    "scope",
    [
        ["docs/PRODUCT.md,"],
        [",src/widget.py"],
        ["docs/PRODUCT.md,,src/widget.py"],
        [" docs/PRODUCT.md"],
        ["docs/PRODUCT.md "],
    ],
)
def test_comma_joined_scope_refuses_empty_or_padded_entries(scope: list[str]) -> None:
    payload = {
        "action": "claim",
        "agent": "Codex Sol",
        "base": BASE,
        "branch": "codex/issue-71-claims",
        "claim_id": "claim-a",
        "issue": 71,
        "role": "builder",
        "scope": scope,
    }

    raised_argument_1 = comment(1, marker(payload))
    with pytest.raises(InvalidClaimMarkerError, match="canonical bounded paths"):
        parse_claim_event(raised_argument_1)


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
    claims: list[ActiveClaim] = []
    for claim_index in range(50):
        parsed = parse_claim_event(
            comment(
                claim_index + 1,
                claim_comment(
                    request(
                        f"claim-{claim_index}",
                        issue=claim_index + 100,
                        scope=tuple(
                            f"area-{claim_index}/path-{scope_index}" for scope_index in range(32)
                        ),
                    )
                ),
                created_at="2026-08-21T00:00:00Z",
            )
        )
        assert isinstance(parsed, ActiveClaim)
        claims.append(parsed)

    def scope_pair_scan(*args, **kwargs):
        pytest.fail("status must use its single scope index")

    monkeypatch.setattr(protocol, "claims_conflict", scope_pair_scan)

    assert _status(tuple(claims), None) == 0
    assert capsys.readouterr().out.count("CLAIMED") == 50
    assert _status(tuple(claims), 100) == 0
    assert capsys.readouterr().out.count("CLAIMED") == 1


def test_existing_scope_on_another_issue_is_posted_as_an_overlap() -> None:
    incumbent = comment(1, claim_comment(request(issue=71, scope=("shared",))))
    client = FakeForge({LEDGER_ISSUE: [incumbent]}, {71})

    acquired = acquire_claim(
        client,
        request("challenger", "Grok 4.6", issue=72, scope=("shared/file.py",)),
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert {claim.claim_id for claim in standing} == {"claim-a", acquired.claim_id}


def test_rescope_adds_a_path_without_changing_claim_id_or_base() -> None:
    client = FakeForge()
    acquired = acquire_claim(client, request(issue=72, scope=("src/widget.py",)))

    updated = rescope_claim(
        client,
        rescope_request(IssueIdentity(72), "Codex Sol", ("src/new.py",), (), acquired.claim_id),
    )

    assert updated.claim_id == acquired.claim_id
    assert updated.base == acquired.base
    assert updated.branch == acquired.branch
    assert updated.agent == acquired.agent
    assert updated.scope == ("src/widget.py", "src/new.py")
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert [claim.scope for claim in standing] == [("src/widget.py", "src/new.py")]


@dataclass
class _ReadAfterWriteForge(FakeForge):
    """A `FakeForge` whose second `list_protocol_candidates()` call -- the
    post-write check `acquire_claim`/`rescope_claim` both make right after
    posting -- is stale, simulating a read-after-write consistency gap.
    `hide_claim=True` drops the claim entirely (the id itself looks never
    to have existed there yet); otherwise only the just-posted comment is
    hidden, so a still-live rescope target is found but still shows its
    pre-rescope scope."""

    hide_claim: bool = False
    _list_calls: int = field(default=0, init=False)

    def list_protocol_candidates(self, issue: int) -> tuple[IssueComment, ...]:
        candidates = super().list_protocol_candidates(issue)
        self._list_calls += 1
        if self._list_calls == 2:
            return () if self.hide_claim else candidates[:-1]
        return candidates


def test_acquire_claim_refuses_when_the_claim_id_never_reappears_after_posting() -> None:
    client = _ReadAfterWriteForge(hide_claim=True)

    raised_argument_1 = request(issue=72, scope=("src/widget.py",))
    with pytest.raises(ClaimError, match="did not expose the posted claim id"):
        acquire_claim(client, raised_argument_1)


def test_rescope_refuses_when_the_claim_id_never_reappears_after_posting() -> None:
    setup = FakeForge()
    acquired = acquire_claim(setup, request(issue=72, scope=("src/widget.py",)))
    client = _ReadAfterWriteForge(comments=setup.comments, hide_claim=True)

    raised_argument_1 = rescope_request(
        acquired.identity, acquired.agent, ("src/new.py",), (), acquired.claim_id
    )
    with pytest.raises(ClaimError, match="did not expose the rescoped claim id"):
        rescope_claim(client, raised_argument_1)


def test_rescope_refuses_when_the_new_scope_never_reappears_after_posting() -> None:
    setup = FakeForge()
    acquired = acquire_claim(setup, request(issue=72, scope=("src/widget.py",)))
    client = _ReadAfterWriteForge(comments=setup.comments, hide_claim=False)

    raised_argument_1 = rescope_request(
        acquired.identity, acquired.agent, ("src/new.py",), (), acquired.claim_id
    )
    with pytest.raises(ClaimError, match="did not observe the posted rescope"):
        rescope_claim(client, raised_argument_1)


def test_rescope_drop_and_add_replace_paths_atomically() -> None:
    client = FakeForge()
    acquired = acquire_claim(client, request(issue=72, scope=("src/old.py", "src/keep.py")))

    updated = rescope_claim(
        client,
        rescope_request(
            IssueIdentity(72), "Codex Sol", ("src/new.py",), ("src/old.py",), acquired.claim_id
        ),
    )

    assert updated.claim_id == acquired.claim_id
    assert updated.scope == ("src/keep.py", "src/new.py")


def test_rescope_refuses_an_identity_with_no_active_claim_at_all() -> None:
    client = FakeForge()

    raised_argument_1 = rescope_request(IssueIdentity(72), "Codex Sol", ("src/new.py",), (), None)
    with pytest.raises(ClaimUnavailableError, match="has no active build claim"):
        rescope_claim(client, raised_argument_1)


def test_rescope_refuses_a_claim_id_that_names_no_standing_claim() -> None:
    client = FakeForge()
    acquire_claim(client, request(issue=72, scope=("src/widget.py",)))

    raised_argument_1 = rescope_request(
        IssueIdentity(72), "Codex Sol", ("src/new.py",), (), "not-a-real-claim-id"
    )
    with pytest.raises(ClaimUnavailableError, match="has no active claim 'not-a-real-claim-id'"):
        rescope_claim(client, raised_argument_1)


def test_rescope_refuses_a_checkout_branch_that_does_not_match_the_claim() -> None:
    client = FakeForge()
    acquired = acquire_claim(client, request(issue=72, scope=("src/widget.py",)))

    raised_argument_1 = rescope_request(
        IssueIdentity(72),
        acquired.agent,
        ("src/new.py",),
        (),
        acquired.claim_id,
        branch="some-other-branch",
    )
    with pytest.raises(ClaimUnavailableError, match="does not match checkout branch"):
        rescope_claim(client, raised_argument_1)


def test_rescope_adds_a_path_held_by_another_issue() -> None:
    client = FakeForge()
    acquire_claim(client, request(issue=72, scope=("src/widget.py",)))
    other = acquire_claim(
        client, request("claim-b", "Grok 4.6", issue=73, scope=("docs/PRODUCT.md",))
    )

    updated = rescope_claim(
        client, rescope_request(IssueIdentity(72), "Codex Sol", ("docs/PRODUCT.md",), (), "claim-a")
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    scopes = {claim.claim_id: claim.scope for claim in standing}
    assert updated.scope == ("src/widget.py", "docs/PRODUCT.md")
    assert scopes["claim-a"] == ("src/widget.py", "docs/PRODUCT.md")
    assert scopes[other.claim_id] == ("docs/PRODUCT.md",)


def test_rescope_drops_an_unrelated_path_when_the_remainder_already_overlaps() -> None:
    client = _claims_client(
        request(issue=72, scope=("docs/product", "tests/tooling")),
        request("claim-b", "Grok 4.6", issue=73, scope=("docs/product",)),
    )

    updated = rescope_claim(
        client, rescope_request(IssueIdentity(72), "Codex Sol", (), ("tests/tooling",), "claim-a")
    )

    assert updated.claim_id == "claim-a"
    assert updated.scope == ("docs/product",)
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    scopes = {claim.claim_id: claim.scope for claim in standing}
    assert scopes["claim-a"] == ("docs/product",)
    assert scopes["claim-b"] == ("docs/product",)


def test_rescope_adds_a_held_path_when_the_remainder_already_overlaps() -> None:
    client = _claims_client(
        request(issue=72, scope=("docs/product", "tests/tooling")),
        request(
            "claim-b",
            "Grok 4.6",
            issue=73,
            scope=("docs/product", "src/held.py"),
        ),
    )

    updated = rescope_claim(
        client, rescope_request(IssueIdentity(72), "Codex Sol", ("src/held.py",), (), "claim-a")
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    scopes = {claim.claim_id: claim.scope for claim in standing}
    assert updated.scope == ("docs/product", "tests/tooling", "src/held.py")
    assert scopes["claim-a"] == ("docs/product", "tests/tooling", "src/held.py")
    assert scopes["claim-b"] == ("docs/product", "src/held.py")


def test_rescope_refuses_dropping_a_path_it_does_not_hold() -> None:
    client = FakeForge()
    acquire_claim(client, request(issue=72, scope=("src/widget.py",)))

    identity = IssueIdentity(72)
    drop_unheld_path_request = rescope_request(
        identity, "Codex Sol", (), ("docs/PRODUCT.md",), "claim-a"
    )
    with pytest.raises(ClaimUnavailableError, match=re.escape("cannot drop 'docs/PRODUCT.md'")):
        rescope_claim(client, drop_unheld_path_request)


def test_rescope_refuses_an_empty_or_unchanged_scope() -> None:
    client = FakeForge()
    acquire_claim(client, request(issue=72, scope=("src/widget.py",)))

    identity = IssueIdentity(72)
    empty_scope_request = rescope_request(identity, "Codex Sol", (), ("src/widget.py",), "claim-a")
    with pytest.raises(ClaimUnavailableError, match="non-empty scope"):
        rescope_claim(client, empty_scope_request)
    identity = IssueIdentity(72)
    unchanged_scope_request = rescope_request(
        identity, "Codex Sol", ("src/widget.py",), (), "claim-a"
    )
    with pytest.raises(ClaimUnavailableError, match="does not change"):
        rescope_claim(client, unchanged_scope_request)


def test_rescope_refuses_a_foreign_agent() -> None:
    client = FakeForge()
    acquire_claim(client, request(issue=72, scope=("src/widget.py",)))

    identity = IssueIdentity(72)
    foreign_agent_request = rescope_request(identity, "Grok 4.6", ("src/new.py",), (), "claim-a")
    with pytest.raises(ClaimUnavailableError, match="only the original claimant"):
        rescope_claim(client, foreign_agent_request)


@pytest.mark.parametrize(
    ("competitor_id", "created_at"),
    [
        pytest.param(
            "earlier",
            "2026-08-20T23:59:59Z",
            id="older-competitor",
        ),
        pytest.param(
            "later",
            "2026-08-21T00:00:50Z",
            id="newer-competitor",
        ),
    ],
)
def test_rescope_keeps_an_added_path_that_another_claim_also_holds(
    competitor_id: str, created_at: str
) -> None:
    client = FakeForge()
    acquire_claim(client, request(issue=72, scope=("src/widget.py",)))
    competitor = comment(
        50,
        claim_comment(request(competitor_id, "Grok 4.6", issue=73, scope=("src/new.py",))),
        created_at=created_at,
    )
    client.inject_before_next_ledger_post = competitor

    updated = rescope_claim(
        client, rescope_request(IssueIdentity(72), "Codex Sol", ("src/new.py",), (), "claim-a")
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    scopes = {claim.claim_id: claim.scope for claim in standing}
    assert set(scopes) == {"claim-a", competitor_id}
    assert updated.scope == ("src/widget.py", "src/new.py")
    assert scopes["claim-a"] == ("src/widget.py", "src/new.py")
    assert scopes[competitor_id] == ("src/new.py",)


def test_who_reports_the_claim_holding_a_path() -> None:
    first = parse_claim_event(
        comment(1, claim_comment(request(issue=72, scope=("docs/PRODUCT.md",))))
    )
    second = parse_claim_event(
        comment(2, claim_comment(request("claim-b", issue=73, scope=("src/widget.py",))))
    )
    assert isinstance(first, ActiveClaim)
    assert isinstance(second, ActiveClaim)
    claims = (first, second)

    assert claims_holding_path(claims, "docs/PRODUCT.md") == (first,)
    assert claims_holding_path(claims, "src/widget.py") == (second,)
    assert claims_holding_path(claims, "README.md") == ()


def test_who_reports_a_directory_claim_for_a_descendant_path() -> None:
    parent = parse_claim_event(comment(1, claim_comment(request(issue=72, scope=("docs",)))))
    assert isinstance(parent, ActiveClaim)

    assert claims_holding_path((parent,), "docs/decisions/one.md") == (parent,)


def test_who_refuses_a_comma_joined_path() -> None:
    with pytest.raises(ClaimError, match="single repository-relative path"):
        claims_holding_path((), "docs/PRODUCT.md,src/widget.py")


def test_disjoint_issues_can_be_claimed_and_are_projected() -> None:
    client = FakeForge()

    first = acquire_claim(client, request(issue=72, scope=("frontend",)))
    second = acquire_claim(
        client,
        request("claim-b", "Grok 4.6", issue=73, scope=("src",)),
    )

    assert {issue_number(first.identity), issue_number(second.identity)} == {72, 73}
    assert client.labels == {72, 73}
    assert "🔒 **Claimed**" in client.comments[72][0].body
    assert "🔒 **Claimed**" in client.comments[73][0].body


def test_owning_issue_projection_uses_the_configured_ledger_number(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    claimed = parse_claim_event(comment(1, claim_comment(request(issue=72))))
    assert isinstance(claimed, ActiveClaim)
    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 170)

    projection = issue_claim._active_projection(claimed)

    assert "ledger=170" in projection.partition("\n")[0]
    assert "ledger=71" not in projection.partition("\n")[0]
    assert claim_label() == "agent-claim:active:170"


def test_same_issue_refuses_a_second_claim_even_with_disjoint_scope() -> None:
    incumbent = comment(1, claim_comment(request(issue=72, scope=("frontend",))))
    client = FakeForge({LEDGER_ISSUE: [incumbent]}, {72})

    raised_argument_1 = request("claim-b", "Grok 4.6", issue=72, scope=("src",))
    with pytest.raises(ClaimUnavailableError, match="issue #72"):
        acquire_claim(
            client,
            raised_argument_1,
        )


def test_same_lane_refuses_a_second_claim_even_with_disjoint_scope() -> None:
    client = FakeForge()
    acquire_claim(client, request(lane=True, branch="docs/lane-a", scope=("frontend",)))

    raised_argument_1 = request(
        "claim-b", "Grok 4.6", lane=True, branch="docs/lane-a", scope=("src",)
    )
    with pytest.raises(ClaimUnavailableError, match="lane 'docs/lane-a'"):
        acquire_claim(
            client,
            raised_argument_1,
        )


def test_lane_and_issue_claim_with_overlapping_scope_both_stay_live() -> None:
    client = FakeForge()
    acquire_claim(client, request(issue=72, scope=("shared",)))

    lane = acquire_claim(
        client,
        request("claim-b", "Grok 4.6", lane=True, branch="docs/lane-a", scope=("shared/file.py",)),
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert {claim.claim_id for claim in standing} == {"claim-a", lane.claim_id}


def test_acquire_claim_refuses_reusing_an_active_claim_id_before_posting() -> None:
    incumbent = comment(1, claim_comment(request("claim-a", issue=72, scope=("old",))))
    client = FakeForge({LEDGER_ISSUE: [incumbent]}, {72})

    raised_argument_1 = request("claim-a", "Codex Sol", issue=72, scope=("old", "new"))
    with pytest.raises(ClaimUnavailableError, match="claim id 'claim-a' is already"):
        acquire_claim(
            client,
            raised_argument_1,
        )

    assert client.comments[LEDGER_ISSUE] == [incumbent]


def test_acquire_claim_refuses_reusing_a_released_claim_id_before_posting() -> None:
    claimed_body = claim_comment(request("claim-a", issue=72))
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    entries = [comment(1, claimed_body), comment(2, release_event(claimed))]
    client = FakeForge({LEDGER_ISSUE: list(entries)})

    raised_argument_1 = request("claim-a", "Grok 4.6", issue=73, scope=("fresh",))
    with pytest.raises(ClaimUnavailableError, match="claim id 'claim-a' is already"):
        acquire_claim(
            client,
            raised_argument_1,
        )

    assert client.comments[LEDGER_ISSUE] == entries


def test_acquire_claim_translates_a_same_claim_id_post_race_into_a_clear_error() -> None:
    client = FakeForge()
    client.inject_after_next_ledger_post = comment(
        2,
        claim_comment(request("claim-a", "Grok 4.6", issue=73, scope=("elsewhere",))),
    )

    raised_argument_1 = request("claim-a", "Codex Sol", issue=72, scope=("mine",))
    with pytest.raises(ClaimUnavailableError, match="claim race detected"):
        acquire_claim(client, raised_argument_1)


def test_acquire_claim_loses_an_identity_race_to_an_earlier_competitor() -> None:
    """Two different claim ids can both legitimately land for the same issue
    in a genuine post-mutation race (unlike the same-claim-id race above);
    whichever comment is chronologically earliest wins, and the loser
    compensates with a release instead of leaving two live claims."""
    client = FakeForge()
    client.inject_after_next_ledger_post = comment(
        2,
        claim_comment(request("claim-b", "Grok 4.6", issue=72, scope=("elsewhere",))),
        created_at="2026-08-21T00:00:00Z",
    )

    raised_argument_1 = request("claim-a", "Codex Sol", issue=72, scope=("mine",))
    with pytest.raises(ClaimUnavailableError, match="claim race lost to"):
        acquire_claim(client, raised_argument_1)

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert {claim.claim_id for claim in standing} == {"claim-b"}


def test_acquire_claim_wins_an_identity_race_against_a_later_competitor() -> None:
    client = FakeForge()
    client.inject_after_next_ledger_post = comment(
        2,
        claim_comment(request("claim-b", "Grok 4.6", issue=72, scope=("elsewhere",))),
        created_at="2026-08-21T00:00:02Z",
    )

    acquired = acquire_claim(client, request("claim-a", "Codex Sol", issue=72, scope=("mine",)))

    assert acquired.claim_id == "claim-a"


def unreadable_ledger_comment(identifier: int, claim_id: str = "claim-b") -> IssueComment:
    """A claim comment shaped like a newer writer's -- every required field present,
    plus one (`surprise`) this reader's schema does not know (issue #136)."""
    return comment(
        identifier,
        marker(
            {
                "action": "claim",
                "agent": "Grok 4.6",
                "base": BASE,
                "branch": "codex/issue-73-claims",
                "claim_id": claim_id,
                "issue": 73,
                "role": "builder",
                "scope": ["docs"],
                "surprise": True,
            }
        ),
    )


def test_release_of_the_unreadable_claim_itself_is_refused() -> None:
    """Fail closed (issue #136): a claim comment this reader cannot parse never
    becomes an `ActiveClaim`, so releasing its claim id hits the ordinary
    no-active-claim refusal instead of trusting an unverified identity/agent/role.
    (`claim`/`rescope`'s own fail-closed refusal, and a quarantined claim's
    `release`/`pr-check` refusal, are exercised through the public CLI entry
    points below -- the CLI can show the same refusal text these raise.)"""
    client = FakeForge({LEDGER_ISSUE: [unreadable_ledger_comment(1)]})

    unreadable_release_context = release_context(
        IssueIdentity(73), "Grok 4.6", "builder", LANDED, "claim-b"
    )
    with pytest.raises(ClaimUnavailableError, match="no active build claim"):
        release_claim(client, unreadable_release_context)


def test_cross_issue_scope_race_keeps_both_overlapping_claims() -> None:
    client = FakeForge()
    earlier = comment(
        100,
        claim_comment(request("earlier", "Grok 4.6", issue=72, scope=("shared/file.py",))),
        created_at="2026-08-20T23:59:59Z",
    )
    client.inject_after_next_ledger_post = earlier

    later = acquire_claim(
        client,
        request("later", "Codex Sol", issue=73, scope=("shared",)),
    )

    standing = active_claims(tuple(client.comments[LEDGER_ISSUE]))
    assert {claim.claim_id for claim in standing} == {"earlier", later.claim_id}
    assert client.labels == {73}


def test_release_refuses_a_lane_identity_without_a_branch() -> None:
    """`_claims_for_identity` is protocol.py's own defense, independent of
    cli.py's earlier branch gate: calling `release_claim` directly with a
    lane identity and no branch must still fail loud here too."""
    client = FakeForge()

    raised_argument_1 = release_context(
        LaneIdentity(), "Codex Sol", "builder", LANDED, None, branch=""
    )
    with pytest.raises(
        ClaimUnavailableError, match="lane release requires a non-empty current branch"
    ):
        release_claim(client, raised_argument_1)


def test_release_removes_projection_only_after_claim_is_gone() -> None:
    client = FakeForge()
    acquired = acquire_claim(client, request(issue=72))
    projection_id = client.comments[72][0].identifier

    released = release_claim(
        client,
        release_context(IssueIdentity(72), "Codex Sol", "builder", LANDED, acquired.claim_id),
    )

    assert released.claim_id == "claim-a"
    assert active_claims(tuple(client.comments[LEDGER_ISSUE])) == ()
    assert client.labels == set()
    assert len(client.comments[72]) == 1
    assert client.comments[72][0].identifier == projection_id
    assert "🔓 **Unclaimed** · merged #12" in client.comments[72][0].body


def test_release_reconciliation_keeps_a_successor_claim_projection_active() -> None:
    client = FakeForge()
    acquired = acquire_claim(client, request(issue=72, scope=("old",)))
    successor = comment(
        4,
        claim_comment(request("successor", "Grok 4.6", issue=72, scope=("new",))),
    )
    client.inject_during_next_remove = successor

    release_claim(
        client,
        release_context(IssueIdentity(72), "Codex Sol", "builder", LANDED, acquired.claim_id),
    )

    projection = client.comments[72][0]
    assert len(client.comments[72]) == 1
    assert "🔒 **Claimed**" in projection.body
    assert "Grok 4.6" in projection.body
    assert "codex/issue-72-claims" in projection.body
    assert "🔓 **Unclaimed**" not in projection.body
    assert client.labels == {72}


def test_projection_is_minimal_and_reuses_one_trusted_comment() -> None:
    client = FakeForge()
    acquired = acquire_claim(client, request(issue=72, scope=("private/path",)))
    first_projection = client.comments[72][0]
    duplicate = replace(first_projection, identifier=first_projection.identifier + 100)
    client.comments[72].append(duplicate)

    client.upsert_projection(72, issue_claim._active_projection(acquired))

    assert len(client.comments[72]) == 1
    projection = client.comments[72][0]
    assert projection.identifier == first_projection.identifier
    assert "private/path" not in projection.body
    assert acquired.base not in projection.body
    assert acquired.branch in projection.body


def test_reconcile_does_not_create_projection_for_never_claimed_issue() -> None:
    client = FakeForge()

    reconcile_issue_label(client, 999)

    assert client.comments.get(999, []) == []
    assert client.labels == set()


def test_claim_labels_are_isolated_by_ledger_generation() -> None:
    assert claim_label(71) == "agent-claim:active:71"
    assert claim_label(170) == "agent-claim:active:170"
    assert claim_label(71) != claim_label(170)


def test_successor_adopts_old_projection_but_old_helper_cannot_mutate_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 71)
    old_projection = comment(1, issue_claim._unclaimed_projection())
    old_duplicate = replace(old_projection, identifier=2)
    client = FakeForge({72: [old_projection, old_duplicate]})

    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 170)
    successor_body = issue_claim._active_projection(
        ActiveClaim(
            IssueIdentity(72),
            "successor",
            "Codex Sol",
            "builder",
            BASE,
            "codex/issue-72-claims",
            ("scripts/issue_claim.py",),
            comment(3, claim_comment(request("successor", issue=72))),
        )
    )
    assert client.upsert_projection(72, successor_body, adopt_stale=True)
    assert len(client.comments[72]) == 1
    assert "ledger=170" in client.comments[72][0].body.partition("\n")[0]

    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 71)
    raised_argument_1 = issue_claim._unclaimed_projection()
    with pytest.raises(ClaimError, match="newer ledger generation"):
        client.upsert_projection(72, raised_argument_1, create=False)
    assert len(client.comments[72]) == 1
    assert "ledger=170" in client.comments[72][0].body.partition("\n")[0]


def test_release_refuses_foreign_actor_without_explicit_override() -> None:
    client = FakeForge()
    acquired = acquire_claim(client, request(issue=72))

    identity = IssueIdentity(72)
    takeover_release = protocol.AbandonedRelease("takeover")
    foreign_actor_release_context = release_context(
        identity, "Other", "builder", takeover_release, acquired.claim_id
    )
    with pytest.raises(ClaimUnavailableError, match="original claimant"):
        release_claim(client, foreign_actor_release_context)


@pytest.mark.parametrize("role", ["builder", "reviewer"])
def test_release_claim_omitted_id_posts_the_outcome_using_selected_role(role: str) -> None:
    client = _claims_client(
        request("mine", "Ada", issue=72, role=role, branch="lane-72", scope=("src",))
    )

    released = release_claim(
        client, release_context(IssueIdentity(72), "Ada", None, LANDED, None, branch="lane-72")
    )
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][-1])

    assert released.claim_id == "mine"
    assert isinstance(posted, ClaimantRelease)
    assert posted.role == role
    assert posted.reason == LANDED.reason
    assert posted.agent == "Ada"
    assert active_claims(tuple(client.comments[LEDGER_ISSUE])) == ()


def test_release_claim_omitted_id_releases_when_foreign_peer_exists_on_issue() -> None:
    client = _claims_client(
        request("mine", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",)),
        request(
            "theirs",
            "Other",
            issue=72,
            role="builder",
            branch="other-lane",
            scope=("docs",),
        ),
    )

    released = release_claim(
        client, release_context(IssueIdentity(72), "Ada", None, LANDED, None, branch="lane-72")
    )
    standing = active_claims(tuple(client.comments[LEDGER_ISSUE]))
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][-1])

    assert released.claim_id == "mine"
    assert [claim.claim_id for claim in standing] == ["theirs"]
    assert isinstance(posted, ClaimantRelease)
    assert posted.role == "reviewer"
    assert posted.reason == LANDED.reason


def test_release_claim_omitted_id_uniqueness_is_issue_scoped() -> None:
    client = _claims_client(
        request("on-72", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",)),
        request("on-73", "Ada", issue=73, role="reviewer", branch="lane-72", scope=("docs",)),
    )

    released = release_claim(
        client, release_context(IssueIdentity(72), "Ada", None, LANDED, None, branch="lane-72")
    )
    standing = active_claims(tuple(client.comments[LEDGER_ISSUE]))

    assert released.claim_id == "on-72"
    assert [claim.claim_id for claim in standing] == ["on-73"]


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
        (
            "Ada",
            "other-lane",
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
        (
            "Ada",
            "lane-72",
            (
                request("one", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",)),
                request("two", "Ada", issue=72, role="builder", branch="lane-72", scope=("docs",)),
            ),
        ),
    ],
)
def test_release_claim_omitted_id_fails_closed_for_wrong_agent_branch_or_two_matches(
    agent: str, branch: str, standing: tuple[ClaimRequest, ...]
) -> None:
    client = _claims_client(*standing)
    protocol_count = len(client.list_protocol_candidates(LEDGER_ISSUE))

    identity = IssueIdentity(72)
    ambiguous_release_context = release_context(identity, agent, None, LANDED, None, branch=branch)
    with pytest.raises(ClaimUnavailableError, match="pass --claim-id") as raised:
        release_claim(client, ambiguous_release_context)

    assert "conflicting claims" not in str(raised.value)
    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == protocol_count


def test_release_claim_explicit_id_ignores_branch() -> None:
    client = _claims_client(
        request("mine", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",))
    )

    released = release_claim(
        client, release_context(IssueIdentity(72), "Ada", None, LANDED, "mine", branch="other-lane")
    )
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][-1])

    assert released.claim_id == "mine"
    assert isinstance(posted, ClaimantRelease)
    assert posted.role == "reviewer"
    assert posted.reason == LANDED.reason
    assert active_claims(tuple(client.comments[LEDGER_ISSUE])) == ()


def test_release_claim_omitted_id_requires_branch_and_does_not_call_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unused(arguments: list[str]) -> str:
        pytest.fail("release_claim must not call git")

    monkeypatch.setattr(checkout, "_git_output", unused)
    client = _claims_client(
        request("mine", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",))
    )

    identity = IssueIdentity(72)
    branchless_release_context = release_context(identity, "Ada", None, LANDED, None)
    with pytest.raises(ClaimUnavailableError, match="current branch"):
        release_claim(client, branchless_release_context)
    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == 1

    released = release_claim(
        client, release_context(IssueIdentity(72), "Ada", None, LANDED, None, branch="lane-72")
    )
    assert released.claim_id == "mine"


@pytest.mark.parametrize("role", ["builder", None])
def test_release_claim_override_fails_before_ledger_without_the_coordinator_role(
    role: str | None,
) -> None:
    client = FakeForge()

    identity = IssueIdentity(72)
    early_override_release_context = release_context(
        identity, "Ada", role, LANDED, "mine", coordinator_override=True
    )
    with pytest.raises(ClaimUnavailableError, match="--role coordinator"):
        release_claim(client, early_override_release_context)

    assert client.comments == {}


def test_label_reconciliation_heals_claim_posted_during_release_remove() -> None:
    old_claim_body = claim_comment(request("old", issue=72, scope=("old",)))
    old_claim = parse_claim_event(comment(1, old_claim_body))
    assert isinstance(old_claim, ActiveClaim)
    release_body = release_event(old_claim)
    new_claim_comment = comment(
        3,
        claim_comment(request("new", issue=72, scope=("new",))),
    )
    client = FakeForge(
        {LEDGER_ISSUE: [comment(1, old_claim_body), comment(2, release_body)]},
        {72},
        inject_during_next_remove=new_claim_comment,
    )

    reconcile_issue_label(client, 72)

    assert [
        claim.claim_id for claim in active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    ] == ["new"]
    assert client.labels == {72}


@dataclass
class _FlappingClaimForge(FakeForge):
    """A `FakeForge` whose ledger claim for one issue is a different claim id
    on every read -- simulating an issue whose claim keeps flapping (claim,
    release, claim again) faster than `reconcile_issue_label`'s own bounded
    retries can ever catch a stable snapshot."""

    flapping_issue: int = 0
    _call: int = field(default=0, init=False)

    def list_protocol_candidates(self, issue: int) -> tuple[IssueComment, ...]:
        self._call += 1
        body = claim_comment(
            request(f"claim-{self._call}", issue=self.flapping_issue, scope=("src",))
        )
        return (comment(self._call, body),)


def test_reconcile_issue_label_fails_loud_when_the_claim_keeps_flapping() -> None:
    client = _FlappingClaimForge(flapping_issue=72)

    with pytest.raises(ClaimError, match="claim label changed repeatedly during reconciliation"):
        reconcile_issue_label(client, 72)


def test_label_failure_is_loud_while_comment_truth_remains() -> None:
    client = FakeForge(fail_add_label=True)

    raised_argument_1 = request(issue=72)
    with pytest.raises(ClaimError, match="label add failed"):
        acquire_claim(client, raised_argument_1)

    assert [
        issue_number(claim.identity)
        for claim in active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    ] == [72]


def test_reconcile_all_repairs_active_and_stale_labels() -> None:
    active = comment(1, claim_comment(request(issue=72)))
    client = FakeForge({LEDGER_ISSUE: [active]}, {73})

    observed = reconcile_all_labels(client)

    assert observed == (72,)
    assert client.labels == {72}


def test_reconcile_labels_the_ledger_when_it_carries_no_label_yet() -> None:
    """Discovery trusts LEDGER_LABEL on the ledger issue to answer atomically
    (#74); reconcile is what backfills it onto an older, unlabelled ledger."""
    client = FakeForge()

    reconcile_all_labels(client)

    assert client.ledger_labelled_issues == {LEDGER_ISSUE}


def test_reconcile_all_labels_ignores_lane_claims_on_a_mixed_ledger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A lane claim owns no GitHub issue, so reconcile must never label or project
    it — only the issue claim on the same mixed ledger keeps its usual behaviour."""
    client = FakeForge()
    acquire_claim(client, request(issue=72, scope=("backend",)))
    acquire_claim(
        client,
        request("lane-claim", "Grok 4.6", lane=True, branch="docs/lane-a", scope=("docs",)),
    )

    lane_calls: list[tuple[str, int]] = []
    original_add_label = client.add_label
    original_remove_label = client.remove_label
    original_upsert_projection = client.upsert_projection
    # reconcile always backfills LEDGER_LABEL onto the ledger issue itself
    # (#74); that is not a lane call, so it is excluded here alongside 72.
    non_lane_issues = {72, LEDGER_ISSUE}

    def add_label(issue: int, label: str) -> None:
        if issue not in non_lane_issues:
            lane_calls.append(("add_label", issue))
        return original_add_label(issue, label)

    def remove_label(issue: int, label: str) -> None:
        if issue not in non_lane_issues:
            lane_calls.append(("remove_label", issue))
        return original_remove_label(issue, label)

    def upsert_projection(
        issue: int, body: str, *, create: bool = True, adopt_stale: bool = False
    ) -> bool:
        if issue != 72:
            lane_calls.append(("upsert_projection", issue))
        return original_upsert_projection(issue, body, create=create, adopt_stale=adopt_stale)

    monkeypatch.setattr(client, "add_label", add_label)
    monkeypatch.setattr(client, "remove_label", remove_label)
    monkeypatch.setattr(client, "upsert_projection", upsert_projection)

    observed = reconcile_all_labels(client)

    assert observed == (72,)
    assert client.labels == {72}
    assert lane_calls == []


def test_reconcile_repairs_a_duplicate_claim_id_and_restores_strict_reads() -> None:
    older_body = claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("old",)))
    newer_body = claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("new",)))
    client = FakeForge({LEDGER_ISSUE: [comment(1, older_body), comment(2, newer_body)]})

    raised_argument_1 = client.list_protocol_candidates(LEDGER_ISSUE)
    with pytest.raises(InvalidClaimMarkerError, match="was reused"):
        active_claims(raised_argument_1)

    repaired = repair_duplicate_claims(client)

    assert repaired == (
        DuplicateClaimRepair(
            claim_id="claim-a", superseded_comment_ids=(1,), survivor_comment_id=2
        ),
    )
    survivors = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert [claim.comment.identifier for claim in survivors] == [2]
    assert not is_protocol_candidate(client.comments[LEDGER_ISSUE][0])
    assert "SUPERSEDED" in client.comments[LEDGER_ISSUE][0].body
    assert "claim-a" in client.comments[LEDGER_ISSUE][0].body

    assert reconcile_all_labels(client) == (72,)
    assert client.labels == {72}


@pytest.mark.parametrize(
    ("older_agent", "release_before_reuse", "newer_agent", "expect_repaired"),
    [
        pytest.param("Codex Sol", False, "Codex Sol", True, id="same_agent_unreleased"),
        pytest.param("Codex Sol", True, "Grok 4.6", True, id="released_id_reuse"),
        pytest.param("Codex Sol", False, "Grok 4.6", False, id="cross_agent_unreleased"),
    ],
)
def test_repair_duplicate_claims_only_auto_resolves_the_safe_cases(
    older_agent: str,
    release_before_reuse: bool,
    newer_agent: str,
    expect_repaired: bool,
) -> None:
    older_body = claim_comment(request("claim-a", older_agent, issue=72, scope=("old",)))
    older_claim = parse_claim_event(comment(1, older_body))
    assert isinstance(older_claim, ActiveClaim)
    entries = [comment(1, older_body)]
    if release_before_reuse:
        entries.append(comment(2, release_event(older_claim)))
    entries.append(
        comment(
            len(entries) + 1,
            claim_comment(request("claim-a", newer_agent, issue=72, scope=("new",))),
        )
    )
    client = FakeForge({LEDGER_ISSUE: entries})

    if not expect_repaired:
        before = list(client.comments[LEDGER_ISSUE])
        with pytest.raises(DuplicateClaimConflictError, match="claim id 'claim-a'"):
            repair_duplicate_claims(client)
        assert client.comments[LEDGER_ISSUE] == before
        return

    repaired = repair_duplicate_claims(client)

    expected_superseded_ids = (1, 2) if release_before_reuse else (1,)
    assert repaired == (
        DuplicateClaimRepair(
            claim_id="claim-a",
            superseded_comment_ids=expected_superseded_ids,
            survivor_comment_id=entries[-1].identifier,
        ),
    )
    survivors = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert [(claim.claim_id, claim.agent) for claim in survivors] == [("claim-a", newer_agent)]
    assert not is_protocol_candidate(client.comments[LEDGER_ISSUE][0])
    if release_before_reuse:
        assert not is_protocol_candidate(client.comments[LEDGER_ISSUE][1])


def test_repair_duplicate_claims_ignores_an_inert_ledger_supersede_as_a_release() -> None:
    """A `LedgerSupersede` event only really terminates a claim when
    `_apply_terminal_event` honors it (coordinator role, right ledger issue, right
    claim comment id, and it was the ledger's only active claim). One that misses
    any of those conditions is inert and must not be read as a release by repair,
    even though it parses cleanly and names the right claim id."""
    original = comment(1, claim_comment(request("claim-a", "Codex Sol", issue=LEDGER_ISSUE)))
    original_claim = parse_claim_event(original)
    assert isinstance(original_claim, ActiveClaim)
    other_active_claim = comment(2, claim_comment(request("other", "Codex Sol", issue=72)))
    inert_supersede = comment(
        3,
        supersede_comment(original_claim, 170, "Fleet Coordinator", "coordinator", "rollover"),
    )
    reused = comment(
        4, claim_comment(request("claim-a", "Grok 4.6", issue=LEDGER_ISSUE, scope=("new",)))
    )
    client = FakeForge({LEDGER_ISSUE: [original, other_active_claim, inert_supersede, reused]})
    before = list(client.comments[LEDGER_ISSUE])

    with pytest.raises(DuplicateClaimConflictError, match="claim id 'claim-a'"):
        repair_duplicate_claims(client)

    assert client.comments[LEDGER_ISSUE] == before


def test_repair_duplicate_claims_attributes_a_late_release_to_the_original_occurrence() -> None:
    """`claim x (A) -> claim x (B, duplicate) -> release x (A)`: the release names
    the original claimant and must close the FIRST occurrence, letting the safe
    already-released repair apply, regardless of which agent posted the duplicate."""
    original = comment(1, claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("old",))))
    original_claim = parse_claim_event(original)
    assert isinstance(original_claim, ActiveClaim)
    duplicate = comment(2, claim_comment(request("claim-a", "Grok 4.6", issue=73, scope=("new",))))
    late_release = comment(3, release_event(original_claim))
    client = FakeForge({LEDGER_ISSUE: [original, duplicate, late_release]})

    repaired = repair_duplicate_claims(client)

    assert repaired == (
        DuplicateClaimRepair(
            claim_id="claim-a", superseded_comment_ids=(1, 3), survivor_comment_id=2
        ),
    )
    survivors = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert [(issue_number(claim.identity), claim.agent) for claim in survivors] == [
        (73, "Grok 4.6")
    ]


@pytest.mark.parametrize(
    "second_release_body",
    [
        pytest.param(
            lambda claim: release_comment(claim, claim.agent, claim.role, "landed retry"),
            id="release_retry",
        ),
        pytest.param(
            lambda claim: release_comment(
                claim,
                "Fleet Coordinator",
                "coordinator",
                "verified handoff",
                coordinator_override=True,
            ),
            id="claimant_then_coordinator_override",
        ),
    ],
)
def test_repair_duplicate_claims_neutralizes_every_honored_terminal_comment(
    second_release_body,
) -> None:
    """A claim id can legitimately carry more than one honored terminal comment (an
    idempotent release retry, or a claimant release followed by a coordinator
    override). Repair must neutralize ALL of them, not only the one whose pop
    actually emptied `active` — otherwise the surviving terminal comment is left
    referencing a claim that repair just made invisible, and the ledger stays dead."""
    original = comment(1, claim_comment(request("claim-a", "Codex Sol", issue=72)))
    original_claim = parse_claim_event(original)
    assert isinstance(original_claim, ActiveClaim)
    first_release = comment(2, release_event(original_claim))
    second_release = comment(3, second_release_body(original_claim))
    reused = comment(4, claim_comment(request("claim-a", "Grok 4.6", issue=73, scope=("fresh",))))
    client = FakeForge({LEDGER_ISSUE: [original, first_release, second_release, reused]})

    repaired = repair_duplicate_claims(client)

    assert repaired == (
        DuplicateClaimRepair(
            claim_id="claim-a", superseded_comment_ids=(1, 2, 3), survivor_comment_id=4
        ),
    )
    survivors = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert [(issue_number(claim.identity), claim.agent) for claim in survivors] == [
        (73, "Grok 4.6")
    ]
    # A truly clean repair: nothing left for a second reconcile pass to find or fix.
    assert repair_duplicate_claims(client) == ()


def test_repair_duplicate_claims_validates_every_lifecycle_before_writing_any() -> None:
    first = comment(1, claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("old",))))
    middle = comment(2, claim_comment(request("claim-a", "Grok 4.6", issue=72, scope=("mid",))))
    newest = comment(3, claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("new",))))
    client = FakeForge({LEDGER_ISSUE: [first, middle, newest]})
    before = list(client.comments[LEDGER_ISSUE])

    with pytest.raises(DuplicateClaimConflictError, match="claim id 'claim-a'"):
        repair_duplicate_claims(client)

    assert client.comments[LEDGER_ISSUE] == before


def test_repair_duplicate_claims_leaves_other_duplicate_ids_untouched_when_one_conflicts() -> None:
    safe_older = comment(
        1, claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("old",)))
    )
    safe_newer = comment(
        2, claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("new",)))
    )
    conflict_older = comment(
        3, claim_comment(request("claim-b", "Codex Sol", issue=73, scope=("x",)))
    )
    conflict_newer = comment(
        4, claim_comment(request("claim-b", "Grok 4.6", issue=73, scope=("y",)))
    )
    client = FakeForge({LEDGER_ISSUE: [safe_older, safe_newer, conflict_older, conflict_newer]})
    before = list(client.comments[LEDGER_ISSUE])

    with pytest.raises(DuplicateClaimConflictError, match="claim id 'claim-b'"):
        repair_duplicate_claims(client)

    assert client.comments[LEDGER_ISSUE] == before


def test_repair_duplicate_claims_same_agent_cross_issue_keeps_only_the_newer_lane() -> None:
    """Documented tradeoff: same-agent keep-newest is not scoped to one issue. A
    same-agent duplicate spanning two issues still only keeps the newer issue's
    lane; the older issue's still-active claim is silently ended, not preserved."""
    older = comment(1, claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("old",))))
    newer = comment(2, claim_comment(request("claim-a", "Codex Sol", issue=73, scope=("new",))))
    client = FakeForge({LEDGER_ISSUE: [older, newer]}, {72, 73})

    repaired = repair_duplicate_claims(client)

    assert repaired == (
        DuplicateClaimRepair(
            claim_id="claim-a", superseded_comment_ids=(1,), survivor_comment_id=2
        ),
    )
    survivors = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert [(issue_number(claim.identity), claim.claim_id) for claim in survivors] == [
        (73, "claim-a")
    ]

    assert reconcile_all_labels(client) == (73,)
    assert client.labels == {73}


def test_stale_reconcile_removes_label_when_supersede_wins_midflight() -> None:
    claimed_body = claim_comment(request(issue=LEDGER_ISSUE))
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    frozen = comment(
        2,
        supersede_comment(
            claimed,
            170,
            "Fleet Coordinator",
            "coordinator",
            "reviewed rollover ready",
        ),
    )
    client = FakeForge(
        {LEDGER_ISSUE: [comment(1, claimed_body)]},
        inject_during_next_add=frozen,
    )

    with pytest.raises(LedgerSupersededError):
        reconcile_issue_label(client, LEDGER_ISSUE)

    assert LEDGER_ISSUE not in client.labels
    raised_argument_1 = client.list_protocol_candidates(LEDGER_ISSUE)
    with pytest.raises(LedgerSupersededError, match="successor #170"):
        active_claims(raised_argument_1)


def test_old_reconcile_clears_only_its_generation_label_after_freeze() -> None:
    claimed_body = claim_comment(request(issue=LEDGER_ISSUE))
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    frozen = supersede_comment(
        claimed,
        170,
        "Fleet Coordinator",
        "coordinator",
        "reviewed rollover ready",
    )
    client = FakeForge(
        {LEDGER_ISSUE: [comment(1, claimed_body), comment(2, frozen)]},
        {LEDGER_ISSUE, 72},
        {claim_label(170): {170}},
    )

    with pytest.raises(LedgerSupersededError):
        reconcile_all_labels(client)
    assert client.labels == set()
    assert client.other_labels == {claim_label(170): {170}}

    client.labels.update({LEDGER_ISSUE, 170})
    with pytest.raises(LedgerSupersededError):
        reconcile_issue_label(client, 170)
    assert client.labels == {LEDGER_ISSUE}
    assert client.other_labels == {claim_label(170): {170}}


def test_paused_old_release_fails_frozen_without_mutating_successor_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 71)
    client = FakeForge(valid_successors={170})
    old_claim = acquire_claim(client, request("old", issue=72, scope=("old",)))
    client.post_comment(
        71,
        release_comment(old_claim, "Codex Sol", "builder", "landed"),
    )
    rollover = acquire_claim(
        client,
        request("rollover", issue=71, scope=("docs/COORDINATION.md",)),
    )
    supersede_ledger(
        client,
        supersede_request(
            170, "Fleet Coordinator", "coordinator", "reviewed successor ready", rollover.claim_id
        ),
    )

    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 170)
    acquire_claim(
        client,
        request("successor", "Grok 4.6", issue=72, scope=("new",)),
    )
    successor_projection = client.comments[72][0].body
    client.other_labels[claim_label(170)] = set(client.labels)
    client.labels.clear()

    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 71)
    with pytest.raises(LedgerSupersededError, match="successor #170"):
        reconcile_issue_label(client, 72)

    assert client.comments[72][0].body == successor_projection
    assert client.other_labels == {claim_label(170): {72}}


def test_status_reports_repository_scope_overlaps_as_notes(
    capsys: pytest.CaptureFixture[str],
) -> None:
    first = parse_claim_event(comment(1, claim_comment(request(issue=72, scope=("shared",)))))
    second = parse_claim_event(
        comment(
            2,
            claim_comment(request("claim-b", issue=73, scope=("shared/file.py",))),
        )
    )
    assert isinstance(first, ActiveClaim)
    assert isinstance(second, ActiveClaim)

    exit_code = _status((first, second), None)

    assert exit_code == 0
    rendered = capsys.readouterr().out
    assert rendered.count("CLAIMED") == 2
    assert "CONFLICT" not in rendered
    assert "overlaps issue #73 (claim-b)" in rendered
    assert "overlaps issue #72 (claim-a)" in rendered
    assert _status((first, second), 72) == 0
    issue_rendered = capsys.readouterr().out
    assert issue_rendered.count("CLAIMED") == 2
    assert "overlaps issue #73 (claim-b)" in issue_rendered


def test_status_notes_a_scope_that_is_claimed_after_its_descendant(
    capsys: pytest.CaptureFixture[str],
) -> None:
    descendant = parse_claim_event(
        comment(1, claim_comment(request(issue=72, scope=("shared/file.py",))))
    )
    parent = parse_claim_event(
        comment(2, claim_comment(request("claim-b", issue=73, scope=("shared",))))
    )
    assert isinstance(descendant, ActiveClaim)
    assert isinstance(parent, ActiveClaim)

    assert _status((descendant, parent), None) == 0
    rendered = capsys.readouterr().out
    assert rendered.count("CLAIMED") == 2
    assert "CONFLICT" not in rendered


def test_github_comment_reader_fetches_pages_concurrently_until_a_short_page(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ordinary_rows = [
        {
            "id": 10,
            "created_at": "2026-08-21T01:00:00Z",
            "updated_at": "2026-08-21T01:00:00Z",
            "body": "ordinary prose",
            "author_association": "OWNER",
            "html_url": "https://github.com/example/agent-claim/issues/71#issuecomment-10",
        },
        {
            "id": 11,
            "created_at": "2026-08-21T02:00:00Z",
            "updated_at": "2026-08-21T02:00:00Z",
            "body": "more ordinary prose",
            "author_association": "MEMBER",
            "html_url": "https://github.com/example/agent-claim/issues/71#issuecomment-11",
        },
    ]
    protocol_row = {
        "id": 12,
        "created_at": "2026-08-21T03:00:00Z",
        "updated_at": "2026-08-21T03:00:00Z",
        "body": claim_comment(request()),
        "author_association": "OWNER",
        "html_url": "https://github.com/example/agent-claim/issues/71#issuecomment-12",
    }
    monkeypatch.setattr(github, "COMMENTS_PER_PAGE", 2)
    calls: list[list[str]] = []

    def by_page(arguments: list[str]) -> str:
        calls.append(arguments)
        # Page 1 fills the (monkeypatched) 2-row page exactly, so a real
        # fetch would keep going; page 2 comes back short, which is what
        # ends it; every later page in the same concurrent batch is past
        # the end, exactly like a real page past GitHub's last one.
        page = int(arguments[1].rsplit("page=", 1)[1])
        rows = ordinary_rows if page == 1 else [protocol_row] if page == 2 else []
        return "\n".join(map(json.dumps, rows))

    client = GitHubForge(github._repository_id("example/agent-claim"), run=by_page)

    observed = client.list_protocol_candidates(71)

    assert [entry.identifier for entry in observed] == [12]
    assert observed[0].body == protocol_row["body"]
    assert not any("--paginate" in call for call in calls)
    assert any("page=2" in call[1] for call in calls)


@pytest.mark.parametrize(
    ("state", "closed_at", "is_pull_request", "expected_state", "expected_closed_at"),
    [
        pytest.param(
            "closed",
            "2026-09-03T12:00:00Z",
            False,
            board.BlockerState.CLOSED,
            datetime(2026, 9, 3, 12, tzinfo=UTC),
            id="closed-issue",
        ),
        pytest.param(
            "open",
            None,
            True,
            board.BlockerState.OPEN,
            None,
            id="open-pull-request",
        ),
    ],
)
def test_github_reads_blocker_state_and_pull_request_kind(
    monkeypatch: pytest.MonkeyPatch,
    state: str,
    closed_at: str | None,
    is_pull_request: bool,
    expected_state: board.BlockerState,
    expected_closed_at: datetime | None,
) -> None:
    observed: list[list[str]] = []

    def run(arguments: list[str]) -> str:
        observed.append(arguments)
        return json.dumps(
            {
                "number": 86,
                "state": state,
                "closedAt": closed_at,
                "isPullRequest": is_pull_request,
            }
        )

    client = GitHubForge(github._repository_id("example/agent-claim"), run=run)

    assert client.list_board_blockers(frozenset({86})) == (
        board.BlockerReference(
            86,
            expected_state,
            is_pull_request,
            expected_closed_at,
        ),
    )
    assert observed == [
        [
            "api",
            "repos/example/agent-claim/issues/86",
            "--jq",
            '{number,state,closedAt:.closed_at,isPullRequest:has("pull_request")}',
        ]
    ]


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

    assert client.capability(forge.ForgeOperation.LIST_ITEMS) is forge.Capability.READ_ONLY
    assert client.capability(forge.ForgeOperation.CREATE_ITEM) is forge.Capability.READ_WRITE


def test_github_adapter_item_reference_reads_state_title_and_body() -> None:
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda _arguments: json.dumps({"state": "open", "title": "Work", "body": "Do it."}),
    )

    assert client.item_reference(10) == forge.ItemReference(forge.ItemState.OPEN, "Work", "Do it.")


def test_github_adapter_item_reference_reads_a_closed_issue_with_no_body() -> None:
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda _arguments: json.dumps({"state": "closed", "title": "Work", "body": None}),
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


@pytest.mark.parametrize("state", ["missing", "unknown"])
def test_github_rejects_blocker_states_the_api_cannot_return(state: str) -> None:
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda _arguments: json.dumps(
            {"number": 86, "state": state, "closedAt": None, "isPullRequest": False}
        ),
    )

    raised_argument_1 = frozenset({86})
    with pytest.raises(ClaimError, match="malformed board blocker"):
        client.list_board_blockers(raised_argument_1)


def test_github_marks_a_missing_blocker_only_after_a_404() -> None:
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda _arguments: (_ for _ in ()).throw(
            forge.ForgeNotFoundError("GitHub API failed: HTTP 404")
        ),
    )

    assert client.list_board_blockers(frozenset({86})) == (
        board.BlockerReference(86, board.BlockerState.MISSING, False),
    )


def test_github_list_board_blockers_is_empty_for_no_numbers() -> None:
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda _arguments: pytest.fail("no blocker should be queried"),
    )

    assert client.list_board_blockers(frozenset()) == ()


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(json.dumps([]), id="no-values"),
        pytest.param(json.dumps(["not-a-dict"]), id="not-a-dict"),
    ],
)
def test_github_board_blocker_fails_loud_on_a_malformed_shape(raw: str) -> None:
    client = GitHubForge(github._repository_id("example/agent-claim"), run=lambda _arguments: raw)

    raised_argument_1 = frozenset({86})
    with pytest.raises(ClaimError, match="malformed board blocker"):
        client.list_board_blockers(raised_argument_1)


def test_github_board_blocker_fails_loud_on_an_uncalendared_closed_timestamp() -> None:
    """`closedAt` can pass the timestamp-shape check (digits in the right
    places) while still naming no real calendar date; `datetime.fromisoformat`
    itself is the second, calendar-aware check that catches that."""
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda _arguments: json.dumps(
            {
                "number": 86,
                "state": "closed",
                "closedAt": "9999-99-99T00:00:00Z",
                "isPullRequest": False,
            }
        ),
    )

    raised_argument_1 = frozenset({86})
    with pytest.raises(ClaimError, match="malformed board blocker"):
        client.list_board_blockers(raised_argument_1)


def _comment_row(identifier: int, body: str = "ordinary prose") -> dict[str, object]:
    stamp = f"2026-08-21T{identifier:02d}:00:00Z"
    return {
        "id": identifier,
        "created_at": stamp,
        "updated_at": stamp,
        "body": body,
        "author_association": "OWNER",
        "html_url": (f"https://github.com/example/agent-claim/issues/71#issuecomment-{identifier}"),
    }


def test_github_comment_reader_accepts_pretty_and_ansi_json() -> None:
    first = _comment_row(10, claim_comment(request()))
    second = _comment_row(11, "ordinary prose")
    pretty = json.dumps(first, indent=2) + "\n" + json.dumps(second, indent=2)
    colored = f"\x1b[32m{pretty}\x1b[0m"
    client = GitHubForge(
        github._repository_id("example/agent-claim"), run=lambda arguments: colored
    )

    observed = client.list_protocol_candidates(71)

    assert [entry.identifier for entry in observed] == [10]
    assert observed[0].body == first["body"]


def test_github_comment_reader_accepts_concatenated_pretty_json_objects() -> None:
    first = _comment_row(10, claim_comment(request()))
    second = _comment_row(11, claim_comment(request("claim-b", issue=72)))
    raw = json.dumps(first, indent=2) + json.dumps(second, indent=2)
    client = GitHubForge(github._repository_id("example/agent-claim"), run=lambda arguments: raw)

    observed = client.list_protocol_candidates(71)

    assert [entry.identifier for entry in observed] == [10, 11]


def test_github_comment_reader_refuses_a_ledger_past_its_comment_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "MAX_LEDGER_COMMENTS", 1)
    rows = [_comment_row(10, "ordinary prose"), _comment_row(11, "ordinary prose")]
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda arguments: "\n".join(json.dumps(row) for row in rows),
    )

    with pytest.raises(ClaimError, match="claim ledger page limit reached"):
        client.list_protocol_candidates(71)


def test_github_comment_reader_refuses_a_ledger_past_its_protocol_event_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(github, "MAX_PROTOCOL_EVENTS", 1)
    rows = [
        _comment_row(10, claim_comment(request("claim-a", issue=72))),
        _comment_row(11, claim_comment(request("claim-b", issue=73))),
    ]
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda arguments: "\n".join(json.dumps(row) for row in rows),
    )

    with pytest.raises(ClaimError, match="claim ledger protocol limit reached"):
        client.list_protocol_candidates(71)


def test_github_projection_reader_refuses_an_issue_past_its_comment_page_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_projection_comments` walks pages until a short one ends the fetch;
    an issue whose comments never run out before `MAX_LEDGER_PAGES` must
    fail loud and ask for the documented rollover, not loop forever."""
    monkeypatch.setattr(github, "MAX_LEDGER_PAGES", 1)
    full_page = [
        {**_comment_row(1, "ordinary prose"), "id": index}
        for index in range(1, github.COMMENTS_PER_PAGE + 1)
    ]
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda arguments: "\n".join(json.dumps(row) for row in full_page),
    )

    raised_argument_1 = issue_claim._unclaimed_projection()
    with pytest.raises(ClaimError, match="owning issue comment limit reached"):
        client.upsert_projection(72, raised_argument_1)


def test_github_projection_reader_returns_normally_on_a_short_page() -> None:
    """The common case, exercised through the real (unmocked) implementation
    rather than the `_projection_comments` fake every other `upsert_projection`
    test injects: a short page ends the fetch and posting proceeds."""
    body = issue_claim._unclaimed_projection()
    posted: dict[str, str | None] = {"body": None}

    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        if arguments[0] == "issue" and arguments[1] == "comment":
            posted["body"] = input_data.decode("utf-8") if input_data else ""
            return "https://github.com/example/agent-claim/issues/72#issuecomment-10"
        if posted["body"] is None:
            return ""
        return json.dumps(_comment_row(10, posted["body"]))

    client = GitHubForge(github._repository_id("example/agent-claim"), run=run)

    assert client.upsert_projection(72, body)
    assert posted["body"] == body


def test_github_comment_reader_paginates_in_concurrent_batches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A single-page listing (the common case) is already covered; a ledger
    whose first concurrent batch is itself still full must ask for a second
    batch rather than assuming one batch is always enough."""
    monkeypatch.setattr(github, "PARALLEL_FETCH_CONCURRENCY", 1)
    full_page = [_comment_row(1, "ordinary prose") for _ in range(github.COMMENTS_PER_PAGE)]

    def run(arguments: list[str]) -> str:
        page = int(arguments[1].rsplit("page=", 1)[1])
        if page <= 2:
            return "\n".join(json.dumps(row) for row in full_page)
        return ""

    client = GitHubForge(github._repository_id("example/agent-claim"), run=run)

    assert client.list_protocol_candidates(71) == ()


def test_github_comment_reader_warns_once_past_the_rollover_threshold(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(github, "LEDGER_ROLLOVER_WARNING_COMMENTS", 2)
    rows = [_comment_row(10, "ordinary prose"), _comment_row(11, "ordinary prose")]
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda arguments: "\n".join(json.dumps(row) for row in rows),
    )

    client.list_protocol_candidates(71)
    assert "WARNING: claim ledger has 2 comments" in capsys.readouterr().err

    client.list_protocol_candidates(71)
    assert capsys.readouterr().err == ""


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
    calls: list[list[str]] = []

    def git(arguments: list[str]) -> str:
        calls.append(arguments)
        return "git@github.com:owner/repository.git"

    monkeypatch.setattr(checkout, "_git_output", git)

    assert checkout.origin_remote_url() == "git@github.com:owner/repository.git"
    assert calls == [["config", "--get", "remote.origin.url"]]


def test_fake_and_github_adapters_expose_only_common_protocol_candidates() -> None:
    trusted = comment(1, claim_comment(request()))
    prose = comment(2, "ordinary prose")
    untrusted = comment(3, claim_comment(request("untrusted")), association="NONE")
    fake = FakeForge({LEDGER_ISSUE: [trusted, prose, untrusted]})
    assert fake.list_protocol_candidates(LEDGER_ISSUE) == (trusted,)

    rows = [
        {
            "id": entry.identifier,
            "created_at": entry.created_at,
            "updated_at": entry.updated_at,
            "body": entry.body,
            "author_association": entry.author_association,
            "html_url": entry.url,
        }
        for entry in (trusted, prose, untrusted)
    ]
    real_client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda arguments: "\n".join(map(json.dumps, rows)),
    )

    assert real_client.list_protocol_candidates(LEDGER_ISSUE) == (trusted,)


def test_comment_size_is_bounded_before_any_adapter_post() -> None:
    widest_scope = tuple(f"p{index:03d}-" + "x" * 507 for index in range(256))

    raised_argument_1 = request(scope=widest_scope)
    with pytest.raises(ClaimError, match=str(MAX_COMMENT_BYTES)):
        claim_comment(raised_argument_1)


def test_github_comment_body_uses_stdin_instead_of_process_argument() -> None:
    observed: dict[str, object] = {}

    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed["arguments"] = arguments
        observed["input"] = input_data
        return "https://github.com/example/agent-claim/issues/71#issuecomment-1"

    client = GitHubForge(github._repository_id("example/agent-claim"), run=run)
    body = claim_comment(request())

    client.post_comment(LEDGER_ISSUE, body)

    arguments = observed["arguments"]
    assert isinstance(arguments, list)
    assert body not in arguments
    assert arguments[-2:] == ["--body-file", "-"]
    assert observed["input"] == body.encode()


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


def test_github_projection_update_patches_one_comment_and_deletes_duplicates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = comment(10, issue_claim._unclaimed_projection())
    duplicate = replace(first, identifier=11)
    observed: list[tuple[list[str], bytes | None]] = []

    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append((arguments, input_data))
        return ""

    client = GitHubForge(github._repository_id("example/agent-claim"), run=run)
    monkeypatch.setattr(client, "_projection_comments", lambda issue: (first, duplicate))
    body = issue_claim._active_projection(
        ActiveClaim(
            IssueIdentity(72),
            "claim-a",
            "Codex Sol",
            "builder",
            BASE,
            "codex/issue-72-claims",
            ("scripts/issue_claim.py",),
            comment(9, claim_comment(request(issue=72))),
        )
    )

    assert client.upsert_projection(72, body)
    assert observed[0][0] == [
        "api",
        "--method",
        "PATCH",
        "repos/example/agent-claim/issues/comments/10",
        "--input",
        "-",
    ]
    assert observed[0][1] == json.dumps({"body": body}).encode("utf-8")
    assert observed[1] == (
        [
            "api",
            "--method",
            "DELETE",
            "repos/example/agent-claim/issues/comments/11",
        ],
        None,
    )


def test_github_projection_update_does_not_create_on_a_never_claimed_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = GitHubForge(github._repository_id("example/agent-claim"))
    monkeypatch.setattr(client, "_projection_comments", lambda issue: ())
    monkeypatch.setattr(
        client,
        "post_comment",
        lambda issue, body: pytest.fail("reconcile must not create a projection"),
    )

    assert not client.upsert_projection(999, issue_claim._unclaimed_projection(), create=False)


def test_github_projection_update_creates_when_none_exists(monkeypatch: pytest.MonkeyPatch) -> None:
    body = issue_claim._unclaimed_projection()
    posted: list[tuple[int, str]] = []
    created = comment(10, body)

    def fake_post_comment(issue: int, projection_body: str) -> str:
        posted.append((issue, projection_body))
        return created.url

    calls = {"n": 0}

    def fake_projection_comments(issue: int) -> tuple[IssueComment, ...]:
        calls["n"] += 1
        return (created,) if calls["n"] > 1 else ()

    client = GitHubForge(github._repository_id("example/agent-claim"))
    monkeypatch.setattr(client, "post_comment", fake_post_comment)
    monkeypatch.setattr(client, "_projection_comments", fake_projection_comments)

    assert client.upsert_projection(999, body)
    assert posted == [(999, body)]


def test_github_projection_update_fails_loud_when_the_post_never_shows_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A read-after-write consistency gap: the projection comment was posted,
    but the very next listing of the issue's own comments still does not
    show it -- this must fail loud rather than claim the projection is live."""
    posted: list[int] = []
    client = GitHubForge(github._repository_id("example/agent-claim"))
    monkeypatch.setattr(client, "post_comment", lambda issue, body: posted.append(issue) or "url")
    monkeypatch.setattr(client, "_projection_comments", lambda issue: ())

    raised_argument_1 = issue_claim._unclaimed_projection()
    with pytest.raises(ClaimError, match=r"issue #999 did not expose its posted claim projection"):
        client.upsert_projection(999, raised_argument_1)

    assert posted == [999]


def test_github_successor_adopts_stale_projection_but_old_generation_skips_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 71)
    stale = comment(10, issue_claim._unclaimed_projection())
    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 171)
    future = comment(11, issue_claim._unclaimed_projection())
    observed: list[tuple[list[str], bytes | None]] = []

    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append((arguments, input_data))
        return ""

    client = GitHubForge(github._repository_id("example/agent-claim"), run=run)
    monkeypatch.setattr(client, "_projection_comments", lambda issue: (stale, future))
    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 170)
    successor_body = issue_claim._unclaimed_projection()

    assert client.upsert_projection(72, successor_body, adopt_stale=True)
    assert observed == [
        (
            [
                "api",
                "--method",
                "PATCH",
                "repos/example/agent-claim/issues/comments/10",
                "--input",
                "-",
            ],
            json.dumps({"body": successor_body}).encode("utf-8"),
        )
    ]

    monkeypatch.setattr(protocol, "LEDGER_ISSUE", 71)
    successor = replace(stale, body=successor_body)
    monkeypatch.setattr(client, "_projection_comments", lambda issue: (successor,))
    observed.clear()
    raised_argument_1 = issue_claim._unclaimed_projection()
    with pytest.raises(ClaimError, match="newer ledger generation"):
        client.upsert_projection(72, raised_argument_1, create=False)
    assert observed == []


def test_github_claimed_issue_query_is_scoped_to_this_ledger_generation() -> None:
    observed: list[str] = []

    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        assert input_data is None
        observed.extend(arguments)
        return "72\n73"

    client = GitHubForge(github._repository_id("example/agent-claim"), run=run)

    assert client.list_claimed_issues() == (72, 73)
    assert (
        f"repos/example/agent-claim/issues?state=all&labels={claim_label()}&per_page=100"
        in observed
    )
    assert "--paginate" in observed


def test_github_claimed_issues_fails_loud_on_a_malformed_entry() -> None:
    client = GitHubForge(
        github._repository_id("example/agent-claim"), run=lambda arguments: "72\ntrue"
    )

    with pytest.raises(ClaimError, match="malformed claimed-issue"):
        client.list_claimed_issues()


def test_github_successor_must_exist_open_empty_locked_and_not_be_a_pr() -> None:
    repository = github._repository_id("example/agent-claim")
    valid = {
        "number": 170,
        "state": "open",
        "locked": True,
        "comments": 0,
        "is_pull_request": False,
    }
    client = GitHubForge(repository, run=lambda arguments: json.dumps(valid))

    client.validate_successor(170)

    for key, value in (
        ("number", 999999),
        ("state", "closed"),
        ("locked", False),
        ("comments", 1),
        ("is_pull_request", True),
    ):
        invalid = {**valid, key: value}
        invalid_client = GitHubForge(repository, run=lambda arguments, row=invalid: json.dumps(row))
        with pytest.raises(ClaimUnavailableError, match="open, empty, collaborator-locked"):
            invalid_client.validate_successor(170)


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(json.dumps([]), id="no-values"),
        pytest.param(json.dumps(["not-a-dict"]), id="not-a-dict"),
    ],
)
def test_github_successor_fails_loud_on_a_malformed_shape(raw: str) -> None:
    client = GitHubForge(github._repository_id("example/agent-claim"), run=lambda arguments: raw)

    with pytest.raises(ClaimError, match="malformed successor issue"):
        client.validate_successor(170)


@pytest.mark.parametrize(
    "raw",
    [
        "not-json",
        json.dumps([]),
        json.dumps({"id": "wrong"}),
        json.dumps(
            {
                "id": 1,
                "created_at": "not-time",
                "updated_at": "not-time",
                "body": "body",
                "author_association": "OWNER",
                "html_url": "https://github.com/example",
            }
        ),
    ],
)
def test_github_comment_reader_wraps_invalid_json_and_schema(raw: str) -> None:
    client = GitHubForge(github._repository_id("example/agent-claim"), run=lambda arguments: raw)

    with pytest.raises(ClaimError):
        client.list_protocol_candidates(71)


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
    assert capsys.readouterr().out == f"agent-claim {__version__}\n"


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
        issue_claim.AGENT_CLAIM_AGENT_ENV,
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
    monkeypatch.setattr(discovery, "discover_ledger", unused)


def _forbid_git_fill(monkeypatch: pytest.MonkeyPatch) -> None:
    def unused(arguments: list[str]) -> str:
        pytest.fail("agent identity must be resolved before git fill")

    monkeypatch.setattr(checkout, "_git_output", unused)


def _patch_release_session(
    monkeypatch: pytest.MonkeyPatch,
    client: FakeForge,
    *,
    agent: str = "Ada",
    branch: str | None = "lane-72",
    forbid_git: bool = False,
) -> None:
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: agent})
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    if forbid_git:

        def unused(arguments: list[str]) -> str:
            pytest.fail("explicit --claim-id must not inspect checkout branch")

        monkeypatch.setattr(checkout, "_git_output", unused)
        return
    git_values = {("branch", "--show-current"): branch or ""}
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])


def _assert_missing_identity_message(message: str) -> None:
    assert "--agent" in message
    assert issue_claim.AGENT_CLAIM_AGENT_ENV in message
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


@pytest.mark.parametrize(
    "arguments",
    [
        ["claim", "42", "--agent", "Ada", "--role", "builder"],
        [
            "supersede",
            "170",
            "--agent",
            "Ada",
            "--reason",
            "landed",
            "--claim-id",
            "cli-claim",
        ],
    ],
)
def test_claim_still_requires_scope_and_supersede_still_requires_role(
    arguments: list[str],
) -> None:
    parser = issue_claim._parser()
    with pytest.raises(SystemExit) as exited:
        parser.parse_args(arguments)

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
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    assert isinstance(posted, ActiveClaim)
    assert posted.role == role


def test_cli_claim_empty_role_fails_closed_without_posting_builder(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    assert client.list_protocol_candidates(LEDGER_ISSUE) == ()
    assert active_claims(tuple(client.comments.get(LEDGER_ISSUE, ()))) == ()


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


def test_supersede_still_requires_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_agent_identity_env(monkeypatch)
    parser = issue_claim._parser()
    with pytest.raises(SystemExit) as exited:
        parser.parse_args(
            [
                "supersede",
                "170",
                "--role",
                "coordinator",
                "--reason",
                "reviewed successor ready",
                "--claim-id",
                "cli-claim",
            ]
        )

    assert exited.value.code == 2


@pytest.mark.parametrize(
    ("explicit", "environ", "agent"),
    [
        (
            "Ada",
            {
                "AGENT_CLAIM_AGENT": "Other",
                "GROK_SESSION_ID": "grok-session",
                "CLAUDE_SESSION_ID": "claude-session",
            },
            "Ada",
        ),
        (None, {"AGENT_CLAIM_AGENT": "Ada"}, "Ada"),
        (None, {"AGENT_CLAIM_AGENT": "", "GROK_SESSION_ID": "sess-1"}, "Grok sess-1"),
        (
            None,
            {"GROK_SESSION_ID": "sess-1", "CLAUDE_SESSION_ID": "sess-2"},
            "Grok sess-1",
        ),
        (None, {"CLAUDE_SESSION_ID": "sess-2"}, "Claude sess-2"),
        (
            None,
            {
                "AGENT_CLAIM_AGENT": "",
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
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    assert issue_claim.main(["--repo", "example/agent-claim", *command]) == 0
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    assert isinstance(posted, ActiveClaim)
    assert posted.agent == agent


@pytest.mark.parametrize(
    ("explicit", "environ"),
    [
        ("", {"AGENT_CLAIM_AGENT": "Ada"}),
        (None, {"AGENT_CLAIM_AGENT": " ", "GROK_SESSION_ID": "sess-1"}),
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
            "AGENT_CLAIM_AGENT": "",
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
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    assert isinstance(posted, ActiveClaim)
    assert posted.agent == "Grok session-1"
    assert active_claims(tuple(client.comments[LEDGER_ISSUE])) == ()


def test_cli_two_session_claimants_cannot_release_without_extra_comment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_agent_identity_env(monkeypatch, {"GROK_SESSION_ID": "session-1"})
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    protocol_count = len(client.list_protocol_candidates(LEDGER_ISSUE))
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
    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == protocol_count
    standing = active_claims(tuple(client.comments[LEDGER_ISSUE]))
    assert [claim.agent for claim in standing] == ["Grok session-1"]


@pytest.mark.parametrize("role", ["builder", "reviewer"])
def test_cli_release_omitted_flags_posts_the_outcome_using_selected_claim_role(
    monkeypatch: pytest.MonkeyPatch, role: str
) -> None:
    client = _claims_client(
        request("mine", "Ada", issue=72, role=role, branch="lane-72", scope=("src",))
    )
    _patch_release_session(monkeypatch, client)

    released = issue_claim.main(
        ["--repo", "example/agent-claim", "release", "72", "--abandoned", "stopped"]
    )
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][-1])

    assert released == 0
    assert isinstance(posted, ClaimantRelease)
    assert posted.claim_id == "mine"
    assert posted.role == role
    assert posted.reason == "abandoned: stopped"
    assert posted.agent == "Ada"
    assert active_claims(tuple(client.comments[LEDGER_ISSUE])) == ()


def test_cli_release_omitted_claim_id_releases_when_foreign_peer_exists_on_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _claims_client(
        request("mine", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",)),
        request(
            "theirs",
            "Other",
            issue=72,
            role="builder",
            branch="other-lane",
            scope=("docs",),
        ),
    )
    _patch_release_session(monkeypatch, client)

    released = issue_claim.main(
        ["--repo", "example/agent-claim", "release", "72", "--abandoned", "stopped"]
    )
    standing = active_claims(tuple(client.comments[LEDGER_ISSUE]))
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][-1])

    assert released == 0
    assert [claim.claim_id for claim in standing] == ["theirs"]
    assert isinstance(posted, ClaimantRelease)
    assert posted.role == "reviewer"
    assert posted.reason == "abandoned: stopped"


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
        (
            "Ada",
            "other-lane",
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
        (
            "Ada",
            "lane-72",
            (
                request("one", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",)),
                request("two", "Ada", issue=72, role="builder", branch="lane-72", scope=("docs",)),
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
    client = _claims_client(*standing)
    _patch_release_session(monkeypatch, client, agent=agent, branch=branch)
    protocol_count = len(client.list_protocol_candidates(LEDGER_ISSUE))

    released = issue_claim.main(
        ["--repo", "example/agent-claim", "release", "72", "--abandoned", "stopped"]
    )
    captured = capsys.readouterr()

    assert released == 2
    assert captured.out == ""
    assert "ERROR:" in captured.err
    assert "pass --claim-id" in captured.err
    assert "conflicting claims" not in captured.err
    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == protocol_count


def test_cli_release_explicit_claim_id_ignores_checkout_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _claims_client(
        request("mine", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",))
    )
    _patch_release_session(monkeypatch, client, forbid_git=True)

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
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][-1])

    assert released == 0
    assert isinstance(posted, ClaimantRelease)
    assert posted.role == "reviewer"
    assert posted.reason == "abandoned: stopped"


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
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Ada"})
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
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Ada"})
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
    monkeypatch.setattr(discovery, "discover_ledger", lambda client: LEDGER_ISSUE)
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
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    assert isinstance(posted, ActiveClaim)
    assert posted.base == BASE
    assert posted.branch == "codex/issue-72"
    assert posted.scope == ("src",)


def test_cli_status_claim_release_and_adapter_error_exit_codes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda client: LEDGER_ISSUE)
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
    status = issue_claim.main(["--repo", "example/agent-claim", "status", "72"])
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

    assert (claimed, status, released) == (0, 0, 0)
    assert "CLAIMED issue #72" in capsys.readouterr().out

    monkeypatch.setattr(
        github,
        "GitHubForge",
        lambda repository: (_ for _ in ()).throw(ClaimError("adapter failed")),
    )
    assert issue_claim.main(["--repo", "example/agent-claim", "status"]) == 2
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
            "src/agent_claim/__init__.py",
        ),
    )


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


def _patch_status_cli(
    monkeypatch: pytest.MonkeyPatch,
    client: FakeForge,
    *,
    ledger: int | None = LEDGER_ISSUE,
) -> None:
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: ledger)
    monkeypatch.setattr(
        checkout,
        "versioned_paths",
        lambda: (
            "LICENSE",
            "README.md",
            "pyproject.toml",
            "src/agent_claim/__init__.py",
        ),
    )
    monkeypatch.setattr(issue_claim, "datetime", FixedDateTime)


def test_cli_reconcile_repairs_a_poisoned_ledger_and_status_reads_it_afterwards(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    older_body = claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("old",)))
    newer_body = claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("new",)))
    client = FakeForge({LEDGER_ISSUE: [comment(1, older_body), comment(2, newer_body)]})
    _patch_status_cli(monkeypatch, client)

    assert issue_claim.main(["--repo", "example/agent-claim", "reconcile"]) == 0
    reconcile_out = capsys.readouterr().out
    assert "REPAIRED claim 'claim-a': superseded #1 -> survivor #2" in reconcile_out
    assert "RECONCILED #72" in reconcile_out

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72"]) == 0
    assert "CLAIMED issue #72" in capsys.readouterr().out


def test_cli_reconcile_targeted_issue_succeeds_on_a_mixed_ledger(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A targeted `reconcile <issue>` filters its own summary line by identity kind
    (cli.py's `isinstance(claim.identity, IssueIdentity)` guard); a lane claim
    coexisting on the same ledger must not make that filter raise."""
    issue_body = claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("backend",)))
    lane_body = claim_comment(
        request("lane-claim", "Grok 4.6", lane=True, branch="docs/lane-a", scope=("docs",))
    )
    client = FakeForge({LEDGER_ISSUE: [comment(1, issue_body), comment(2, lane_body)]})
    _patch_status_cli(monkeypatch, client)

    assert issue_claim.main(["--repo", "example/agent-claim", "reconcile", "72"]) == 0
    assert "RECONCILED #72" in capsys.readouterr().out
    assert client.labels == {72}


def test_cli_reconcile_refuses_a_cross_agent_duplicate_with_exit_code_two(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    older_body = claim_comment(request("claim-a", "Codex Sol", issue=72, scope=("old",)))
    newer_body = claim_comment(request("claim-a", "Grok 4.6", issue=72, scope=("new",)))
    client = FakeForge({LEDGER_ISSUE: [comment(1, older_body), comment(2, newer_body)]})
    _patch_status_cli(monkeypatch, client)

    assert issue_claim.main(["--repo", "example/agent-claim", "reconcile"]) == 2
    assert "claim id 'claim-a'" in capsys.readouterr().err


def test_cli_reconcile_still_clears_stale_labels_when_ledger_is_frozen(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    claimed_body = claim_comment(request(issue=LEDGER_ISSUE))
    claimed = parse_claim_event(comment(1, claimed_body))
    assert isinstance(claimed, ActiveClaim)
    frozen_body = supersede_comment(
        claimed, 170, "Fleet Coordinator", "coordinator", "reviewed rollover ready"
    )
    client = FakeForge(
        {LEDGER_ISSUE: [comment(1, claimed_body), comment(2, frozen_body)]},
        {LEDGER_ISSUE, 72},
    )
    _patch_status_cli(monkeypatch, client)

    assert issue_claim.main(["--repo", "example/agent-claim", "reconcile"]) == 2
    assert "frozen" in capsys.readouterr().err
    assert client.labels == set()


def test_cli_bootstrap_creates_and_prints_the_ledger(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)

    status = issue_claim.main(["--repo", "example/agent-claim", "bootstrap"])

    assert status == 0
    created = next(iter(client.ledger_items)).number
    assert capsys.readouterr().out == f"LEDGER #{created}\n"
    assert issue_claim.LEDGER_LABEL in client.other_labels


def test_cli_status_empty_ledger_prints_ledger_then_unclaimed_repository(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_cli(monkeypatch, FakeForge())

    assert issue_claim.main(["--repo", "example/agent-claim", "status"]) == 0
    assert capsys.readouterr().out == (f"LEDGER #{LEDGER_ISSUE}\nUNCLAIMED repository\n")


def test_cli_status_issue_with_no_claim_prints_ledger_then_unclaimed_issue(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_cli(monkeypatch, FakeForge())

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72"]) == 0
    assert capsys.readouterr().out == (f"LEDGER #{LEDGER_ISSUE}\nUNCLAIMED issue #72\n")


def test_cli_status_after_claim_prints_ledger_then_claimed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_cli(monkeypatch, FakeForge())
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
        == 0
    )
    capsys.readouterr()

    status = issue_claim.main(["--repo", "example/agent-claim", "status", "72"])
    assert status == 0
    assert capsys.readouterr().out == (
        f"LEDGER #{LEDGER_ISSUE}\n"
        f"CLAIMED issue #72: Codex Sol (builder) base={BASE} "
        "branch=codex/issue-72 claim=cli-claim 0h 0m\n"
        "  src\n"
    )


def test_cli_status_prints_the_resource_line_for_an_allocated_hold(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _claims_client(
        request("hop-1", "Ada", issue=72, scope=("src",), resource="schema-hop", resource_value=1)
    )
    _patch_status_cli(monkeypatch, client)

    status = issue_claim.main(["--repo", "example/agent-claim", "status", "72"])

    assert status == 0
    assert "  resource schema-hop=1\n" in capsys.readouterr().out


def test_cli_lane_claim_status_and_release_round_trip_without_issue_number(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The full `Done when` #1 story: a docs/ checkout claims, is visible in
    `status`, and releases again — all without ever passing an issue number."""
    _set_agent_identity_env(monkeypatch, {"AGENT_CLAIM_AGENT": "Codex Sol"})
    client = FakeForge()
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    git_values = {("branch", "--show-current"): "docs/lane-cleanup"}
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

    assert issue_claim.main(["--repo", "example/agent-claim", "status"]) == 0
    assert capsys.readouterr().out == (
        f"LEDGER #{LEDGER_ISSUE}\n"
        f"CLAIMED lane docs/lane-cleanup: Codex Sol (builder) base={BASE} "
        "branch=docs/lane-cleanup claim=cli-lane-claim 0h 0m\n"
        "  docs\n"
    )

    released = issue_claim.main(
        ["--repo", "example/agent-claim", "release", "--abandoned", "stopped"]
    )
    assert released == 0
    assert active_claims(tuple(client.comments[LEDGER_ISSUE])) == ()


@pytest.mark.parametrize("command", ["claim", "release"])
def test_cli_lane_mode_refuses_a_non_conventional_branch(
    command: str,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_agent_identity_env(monkeypatch, {"AGENT_CLAIM_AGENT": "Codex Sol"})
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
    assert client.comments == {}


def test_cli_release_requires_a_non_empty_current_branch_without_an_issue(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_agent_identity_env(monkeypatch, {"AGENT_CLAIM_AGENT": "Codex Sol"})
    client = FakeForge()
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: "")

    status = issue_claim.main(
        ["--repo", "example/agent-claim", "release", "--abandoned", "stopped"]
    )

    assert status == 2
    assert "lane release requires a non-empty current branch" in capsys.readouterr().err


def test_cli_status_overlapping_protocol_comments_print_ledger_then_notes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge(
        {
            LEDGER_ISSUE: [
                comment(1, claim_comment(request(issue=72, scope=("shared",)))),
                comment(
                    2,
                    claim_comment(
                        request("claim-b", "Grok 4.6", issue=73, scope=("shared/file.py",))
                    ),
                ),
            ]
        }
    )
    _patch_status_cli(monkeypatch, client)

    status = issue_claim.main(["--repo", "example/agent-claim", "status"])
    assert status == 0
    assert capsys.readouterr().out == (
        f"LEDGER #{LEDGER_ISSUE}\n"
        f"CLAIMED issue #72: Codex Sol (builder) base={BASE} "
        "branch=codex/issue-72-claims claim=claim-a 0h 0m\n"
        "  shared\n"
        "  overlaps issue #73 (claim-b)\n"
        f"CLAIMED issue #73: Grok 4.6 (builder) base={BASE} "
        "branch=codex/issue-73-claims claim=claim-b 0h 0m\n"
        "  shared/file.py\n"
        "  overlaps issue #72 (claim-a)\n"
    )


def test_cli_status_shows_an_unreadable_claim_alongside_readable_ones(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Issue #136: a v0.11-shaped comment among v0.10 comments no longer fails
    `status` outright; it is named as unreadable, with its unknown fields, next to
    every claim this reader still understands."""
    client = FakeForge(
        {
            LEDGER_ISSUE: [
                comment(1, claim_comment(request(issue=72, scope=("shared",)))),
                unreadable_ledger_comment(2),
            ]
        }
    )
    _patch_status_cli(monkeypatch, client)

    status = issue_claim.main(["--repo", "example/agent-claim", "status"])
    assert status == 0
    assert capsys.readouterr().out == (
        f"LEDGER #{LEDGER_ISSUE}\n"
        f"CLAIMED issue #72: Codex Sol (builder) base={BASE} "
        "branch=codex/issue-72-claims claim=claim-a 0h 0m\n"
        "  shared\n"
        "UNREADABLE claim claim-b: unreadable, upgrade the installed tool\n"
        "  fields: surprise\n"
        f"  {unreadable_ledger_comment(2).url}\n"
    )


def test_cli_status_json_lists_an_unreadable_claim(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge({LEDGER_ISSUE: [unreadable_ledger_comment(1)]})
    _patch_status_cli(monkeypatch, client)

    status = issue_claim.main(["--repo", "example/agent-claim", "status", "--json"])
    assert status == 0
    assert capsys.readouterr().out == (
        json.dumps(
            {
                "ledger": LEDGER_ISSUE,
                "issue": None,
                "state": "UNCLAIMED",
                "claims": [],
                "unreadable": [
                    {
                        "claim_id": "claim-b",
                        "comment_url": unreadable_ledger_comment(1).url,
                        "fields": ["surprise"],
                        "note": "unreadable, upgrade the installed tool",
                    }
                ],
            }
        )
        + "\n"
    )


def test_cli_board_and_next_succeed_with_an_unreadable_claim_on_the_ledger(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Read-only commands answer normally even though one comment is unreadable
    (issue #136); only a `claim`/`rescope` fails closed on it."""
    client = FakeForge({LEDGER_ISSUE: [unreadable_ledger_comment(1)]})
    _patch_status_cli(monkeypatch, client)

    assert issue_claim.main(["--repo", "example/agent-claim", "board"]) == 0
    capsys.readouterr()
    assert issue_claim.main(["--repo", "example/agent-claim", "next"]) == 3


def test_cli_who_succeeds_with_an_unreadable_claim_on_the_ledger(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`who` is read-only (issue #136): it still finds the readable holder of a
    path even while an unrelated comment on the ledger is unreadable."""
    client = FakeForge(
        {
            LEDGER_ISSUE: [
                comment(1, claim_comment(request(issue=72, scope=("shared",)))),
                unreadable_ledger_comment(2),
            ]
        }
    )
    _patch_status_cli(monkeypatch, client)

    assert issue_claim.main(["--repo", "example/agent-claim", "who", "shared/file.py"]) == 0
    assert (
        "CLAIMED shared/file.py issue #72: Codex Sol (builder) claim=claim-a"
        in capsys.readouterr().out
    )


def test_cli_claim_refuses_while_an_unreadable_claim_stands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Fail closed through the public entry point (issue #136): this reader cannot
    tell whether the unreadable claim's true scope overlaps the request, so
    `claim` refuses and names it -- even for an unrelated issue and scope."""
    client = FakeForge({LEDGER_ISSUE: [unreadable_ledger_comment(1)]})
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    status = issue_claim.main(
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
    captured = capsys.readouterr()

    assert status == 2
    assert "ERROR: claim refused: claim 'claim-b'" in captured.err
    assert "unknown fields: surprise" in captured.err
    assert active_claims(tuple(client.comments[LEDGER_ISSUE])) == ()


def test_cli_rescope_requires_a_non_empty_current_branch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Rescope always operates on the checked-out worktree's own claim, so a
    detached or branchless checkout must refuse even when an issue number is
    also given -- unlike release, it never falls back to the issue alone."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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


def test_cli_rescope_refuses_while_an_unreadable_claim_stands(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    acquire_claim(client, request(issue=72, branch="codex/issue-72", scope=("src/widget.py",)))
    client.comments[LEDGER_ISSUE].append(unreadable_ledger_comment(2))
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout()
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        ["--repo", "example/agent-claim", "rescope", "72", "--add", "src/new.py"]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert "ERROR: rescope refused: claim 'claim-b'" in captured.err
    assert "unknown fields: surprise" in captured.err
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert standing[0].scope == ("src/widget.py",)


def test_cli_readable_then_unreadable_rescope_quarantines_release_and_pr_check(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The lifecycle finding 1 fixes: a claim posted normally is later rescoped by
    a newer writer whose rescope this reader cannot parse. `status`/`board`/`next`
    still see the claim, but it is quarantined: its own-branch `pr-check` and its
    `release` both refuse it, naming the rescope comment, leaving the ledger
    untouched."""
    claimed_request = request(
        "landing", "Codex Sol", issue=72, branch="codex/issue-72-claims", scope=("src",)
    )
    client = _claims_client(claimed_request)
    client.comments[LEDGER_ISSUE].append(
        comment(
            2,
            marker(
                {
                    "action": "rescope",
                    "agent": "Codex Sol",
                    "claim_id": "landing",
                    "issue": 72,
                    "role": "builder",
                    "scope": ["src", "docs"],
                    "surprise": True,
                }
            ),
        )
    )
    _patch_status_cli(monkeypatch, client)
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(["--repo", "example/agent-claim", "status", "72"])
    assert status == 0
    status_out = capsys.readouterr().out
    assert "CLAIMED issue #72: Codex Sol (builder)" in status_out
    assert "UNREADABLE claim landing: unreadable, upgrade the installed tool" in status_out

    assert issue_claim.main(["--repo", "example/agent-claim", "board"]) == 0
    capsys.readouterr()
    assert issue_claim.main(["--repo", "example/agent-claim", "next"]) == 3

    client.landings[12] = landing_pull_request(body=f"Work-Item: {REPOSITORY}#72\n\nCloses #72")
    refused = run_pr_check()
    captured = capsys.readouterr()
    assert refused == 1
    assert (
        "REFUSED: pull request #12 has a quarantined claim on branch "
        "'codex/issue-72-claims': claim 'landing'" in captured.err
    )
    assert "unknown fields: surprise" in captured.err

    released = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "release",
            "72",
            "--claim-id",
            "landing",
            "--abandoned",
            "stopped",
        ]
    )
    captured = capsys.readouterr()
    assert released == 2
    assert "ERROR: release refused: claim 'landing'" in captured.err
    assert "unknown fields: surprise" in captured.err
    standing = active_claims(tuple(client.comments[LEDGER_ISSUE]))
    assert [claim.claim_id for claim in standing] == ["landing"]


def test_cli_claim_race_releases_the_claim_when_an_unreadable_comment_appears_while_posting(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Finding 2 (issue #136): the pre-post check cannot see a comment that lands
    concurrently. When one appears right after this claim's own post, `claim`
    compensates like any other post-mutation race -- it releases its own new
    claim -- so a command reporting failure never leaves a live claim behind."""
    client = FakeForge()
    client.inject_after_next_ledger_post = unreadable_ledger_comment(2, claim_id="claim-c")
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    status = issue_claim.main(
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
    captured = capsys.readouterr()

    assert status == 2
    assert "ERROR: claim refused: claim 'claim-c'" in captured.err
    assert "appeared while posting" in captured.err
    assert active_claims(tuple(client.comments[LEDGER_ISSUE])) == ()


def test_cli_rescope_race_reverts_the_scope_when_an_unreadable_comment_appears_while_posting(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Finding 2 (issue #136): the same post-mutation race for `rescope`. It
    cannot release the claim (that would undo more than this mutation), so it
    compensates with another rescope back to the pre-rescope scope, so the new
    scope from a failing `rescope` never stays live."""
    client = FakeForge()
    acquire_claim(client, request(issue=72, branch="codex/issue-72", scope=("src/widget.py",)))
    client.inject_after_next_ledger_post = unreadable_ledger_comment(99, claim_id="claim-c")
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout()
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        ["--repo", "example/agent-claim", "rescope", "72", "--add", "src/new.py"]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert "ERROR: rescope refused: claim 'claim-c'" in captured.err
    assert "appeared while posting" in captured.err
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert standing[0].scope == ("src/widget.py",)


def test_cli_rescope_race_clears_the_whole_reason_it_just_set(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Finding 1 (delta review): None -> reason -> race -> None. A rescope that
    gives a claim its first-ever whole reason races an unreadable comment; the
    automatic revert must clear the reason back to unset. A plain
    `whole_reason=None` on the revert would not do that -- absence already means
    "leave the current reason alone" (the sticky contract every ordinary rescope
    relies on), so clearing needs its own explicit signal."""
    client = FakeForge()
    acquire_claim(client, request(issue=72, branch="codex/issue-72", scope=("src/widget.py",)))
    assert active_claims(client.list_protocol_candidates(LEDGER_ISSUE))[0].whole_reason is None
    client.inject_after_next_ledger_post = unreadable_ledger_comment(99, claim_id="claim-c")
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout()
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "rescope",
            "72",
            "--add",
            "src/new.py",
            "--whole",
            "widening for a spike",
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert "ERROR: rescope refused: claim 'claim-c'" in captured.err
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert standing[0].scope == ("src/widget.py",)
    assert standing[0].whole_reason is None


def _executed_recovery_command(captured_err: str) -> list[str]:
    """Pull the exact repair command a `CompensationFailedError` printed and
    turn it into the argv `issue_claim.main` expects (issue #136 delta review:
    the test proves the printed text is actually runnable, not merely present).
    """
    match = re.search(r"RECOVERY: run `([^`]+)` to finish the repair", captured_err)
    assert match is not None, captured_err
    argv = shlex.split(match.group(1))
    assert argv[0] == "agent-claim"
    return argv[1:]


def test_cli_claim_race_reports_a_recovery_warning_when_the_compensating_release_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Finding 2 (delta review): when the race-compensating release itself fails
    to post, the just-claimed id stays live and untracked by any refusal message
    -- the CLI must say so explicitly, naming the live claim and a ready repair
    command, rather than printing the compensating write's own generic error.
    The printed command is then actually run, proving it is not merely refused
    text -- it releases the stuck claim for real."""
    client = FakeForge()
    client.inject_after_next_ledger_post = unreadable_ledger_comment(2, claim_id="claim-c")
    client.fail_ledger_post_at_call = 2
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    status = issue_claim.main(
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
    captured = capsys.readouterr()

    assert status == 2
    assert (
        "ERROR: claim 'cli-claim' is still live; its automatic repair failed to "
        "post: ledger post failed (simulated)" in captured.err
    )
    assert (
        "RECOVERY: run `agent-claim release 72 --claim-id cli-claim --agent "
        "'Codex Sol' --role builder --abandoned 'claim race lost'` to finish "
        "the repair" in captured.err
    )
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert [claim.claim_id for claim in standing] == ["cli-claim"]

    repair_argv = _executed_recovery_command(captured.err)
    client.fail_ledger_post_at_call = None
    repaired = issue_claim.main(["--repo", "example/agent-claim", *repair_argv])

    assert repaired == 0
    assert active_claims(client.list_protocol_candidates(LEDGER_ISSUE)) == ()


def test_cli_rescope_race_reports_a_recovery_warning_when_the_reverting_rescope_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Finding 2 (delta review): the same recovery warning for `rescope`, when
    the compensating revert itself cannot be posted -- the widened scope stays
    live, and the CLI names the one repair that always works (a release, since
    a manual rescope retry would itself be refused by the same unreadable
    comment) plus a hint to re-claim the pre-race scope. The printed command is
    then actually run against the same ledger, proving it truly releases the
    claim the failed automatic revert left stuck."""
    client = FakeForge()
    acquire_claim(client, request(issue=72, branch="codex/issue-72", scope=("src/widget.py",)))
    client.inject_after_next_ledger_post = unreadable_ledger_comment(99, claim_id="claim-c")
    client.ledger_post_call_count = 0
    client.fail_ledger_post_at_call = 2
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout()
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        ["--repo", "example/agent-claim", "rescope", "72", "--add", "src/new.py"]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert (
        "ERROR: claim 'claim-a' is still live; its automatic repair failed to "
        "post: ledger post failed (simulated)" in captured.err
    )
    assert (
        "RECOVERY: run `agent-claim release 72 --claim-id claim-a --agent "
        "'Codex Sol' --role builder --abandoned 'claim race lost'` to finish "
        "the repair" in captured.err
    )
    assert "RECOVERY: then re-claim its pre-race scope: src/widget.py" in captured.err
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert standing[0].scope == ("src/widget.py", "src/new.py")

    repair_argv = _executed_recovery_command(captured.err)
    client.fail_ledger_post_at_call = None
    repaired = issue_claim.main(["--repo", "example/agent-claim", *repair_argv])

    assert repaired == 0
    assert active_claims(client.list_protocol_candidates(LEDGER_ISSUE)) == ()


def test_cli_claim_race_same_id_repair_uses_coordinator_override_for_the_quarantined_claim(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Finding 1 (third delta review): when the racing unreadable comment names
    this reader's own just-claimed id, that claim is quarantined -- a plain
    release now refuses it too, so the printed repair must use the documented
    coordinator-override exception instead. The command is then actually run,
    and the bypassed quarantine refusal is printed as a warning, not lost."""
    client = FakeForge()
    client.inject_after_next_ledger_post = unreadable_ledger_comment(2, claim_id="cli-claim")
    client.fail_ledger_post_at_call = 2
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    status = issue_claim.main(
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
    captured = capsys.readouterr()

    assert status == 2
    assert (
        "RECOVERY: run `agent-claim release 72 --claim-id cli-claim --agent "
        "'Codex Sol' --role coordinator --coordinator-override --abandoned "
        "'claim race lost'` to finish the repair" in captured.err
    )
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert [claim.claim_id for claim in standing] == ["cli-claim"]
    assert standing[0].quarantined_by is not None

    repair_argv = _executed_recovery_command(captured.err)
    client.fail_ledger_post_at_call = None
    repaired = issue_claim.main(["--repo", "example/agent-claim", *repair_argv])
    repaired_captured = capsys.readouterr()

    assert repaired == 0
    assert "WARNING: this claim was quarantined:" in repaired_captured.err
    assert active_claims(client.list_protocol_candidates(LEDGER_ISSUE)) == ()


def test_cli_claim_race_repair_for_a_lane_claim_names_its_checkout_branch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Finding 2 (third delta review): `release` has no `--branch` selector and
    always derives a lane claim from the current checkout, regardless of
    `--claim-id` -- unlike an issue claim, a lane claim's repair command cannot
    be made checkout-independent, so it also names the branch to run it from.
    Executed with that branch resolvable in the fake, it releases the claim."""
    client = FakeForge()
    client.inject_after_next_ledger_post = unreadable_ledger_comment(2, claim_id="claim-c")
    client.fail_ledger_post_at_call = 2
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = {("branch", "--show-current"): "docs/lane-cleanup"}
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    status = issue_claim.main(
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
    captured = capsys.readouterr()

    assert status == 2
    assert (
        "RECOVERY: run `agent-claim release --claim-id cli-lane-claim --agent "
        "'Codex Sol' --role builder --abandoned 'claim race lost'` to finish "
        "the repair" in captured.err
    )
    assert "RECOVERY: run from the lane's checkout (branch docs/lane-cleanup)" in captured.err

    repair_argv = _executed_recovery_command(captured.err)
    client.fail_ledger_post_at_call = None
    repaired = issue_claim.main(["--repo", "example/agent-claim", *repair_argv])

    assert repaired == 0
    assert active_claims(client.list_protocol_candidates(LEDGER_ISSUE)) == ()


def test_cli_rescope_race_repair_hint_includes_the_pre_race_whole_reason(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Finding 3 (third delta review): the rescope race's re-claim hint restores
    scope only; when the pre-race claim already had a whole reason, dropping it
    from the hint would silently lose it after the suggested repair."""
    client = FakeForge()
    acquire_claim(
        client,
        request(
            issue=72,
            branch="codex/issue-72",
            scope=("src/widget.py",),
            whole_reason="widen for launch",
        ),
    )
    client.inject_after_next_ledger_post = unreadable_ledger_comment(99, claim_id="claim-c")
    client.ledger_post_call_count = 0
    client.fail_ledger_post_at_call = 2
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout()
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(
        ["--repo", "example/agent-claim", "rescope", "72", "--add", "src/new.py"]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert (
        "RECOVERY: then re-claim its pre-race scope: src/widget.py --whole "
        "'widen for launch'" in captured.err
    )


def test_cli_status_hard_fails_on_a_comment_missing_a_required_field_plus_an_extra_one(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Missing a required field is a corrupt record, not a newer writer, even when
    an unrecognized field is also present (issue #136): the CLI still hard-fails
    the whole ledger read with the old message, never treating it as unreadable."""
    corrupt = comment(
        1,
        marker(
            {
                "action": "claim",
                "agent": "Codex Sol",
                "branch": "topic",
                "claim_id": "claim-a",
                "issue": 71,
                "role": "builder",
                "scope": ["src"],
                "surprise": True,
            }
        ),
    )
    client = FakeForge({LEDGER_ISSUE: [corrupt]})
    _patch_status_cli(monkeypatch, client)

    status = issue_claim.main(["--repo", "example/agent-claim", "status"])
    captured = capsys.readouterr()

    assert status == 2
    assert "fields differ" in captured.err
    assert "upgrade" not in captured.err


def test_cli_status_without_ledger_errors_and_prints_no_ledger_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_cli(monkeypatch, FakeForge(), ledger=None)

    assert issue_claim.main(["--repo", "example/agent-claim", "status"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR:" in captured.err
    assert "no agent-claim ledger exists" in captured.err
    assert "LEDGER" not in captured.out


def test_status_direct_empty_claims_prints_unclaimed_repository_without_ledger(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert _status((), None) == 0
    assert capsys.readouterr().out == "UNCLAIMED repository\n"


def test_cli_status_json_empty_ledger_prints_unclaimed_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_cli(monkeypatch, FakeForge())

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "--json"]) == 0
    assert (
        capsys.readouterr().out
        == json.dumps(
            {
                "ledger": LEDGER_ISSUE,
                "issue": None,
                "state": "UNCLAIMED",
                "claims": [],
                "unreadable": [],
            }
        )
        + "\n"
    )


def test_cli_status_json_issue_with_no_claim_prints_unclaimed_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_cli(monkeypatch, FakeForge())

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72", "--json"]) == 0
    assert (
        capsys.readouterr().out
        == json.dumps(
            {
                "ledger": LEDGER_ISSUE,
                "issue": 72,
                "state": "UNCLAIMED",
                "claims": [],
                "unreadable": [],
            }
        )
        + "\n"
    )


def test_cli_status_json_after_claim_prints_claimed_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_cli(monkeypatch, FakeForge())
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
        == 0
    )
    capsys.readouterr()

    status = issue_claim.main(["--repo", "example/agent-claim", "status", "72", "--json"])
    assert status == 0
    assert (
        capsys.readouterr().out
        == json.dumps(
            {
                "ledger": LEDGER_ISSUE,
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
                "unreadable": [],
            }
        )
        + "\n"
    )


def test_cli_status_json_overlapping_protocol_comments_print_claimed_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge(
        {
            LEDGER_ISSUE: [
                comment(1, claim_comment(request(issue=72, scope=("shared",)))),
                comment(
                    2,
                    claim_comment(
                        request("claim-b", "Grok 4.6", issue=73, scope=("shared/file.py",))
                    ),
                ),
            ]
        }
    )
    _patch_status_cli(monkeypatch, client)

    status = issue_claim.main(["--repo", "example/agent-claim", "status", "--json"])
    assert status == 0
    assert (
        capsys.readouterr().out
        == json.dumps(
            {
                "ledger": LEDGER_ISSUE,
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
                "unreadable": [],
            }
        )
        + "\n"
    )


def test_cli_status_json_issue_on_overlap_prints_related_claimed_object(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge(
        {
            LEDGER_ISSUE: [
                comment(1, claim_comment(request(issue=72, scope=("shared",)))),
                comment(
                    2,
                    claim_comment(
                        request("claim-b", "Grok 4.6", issue=73, scope=("shared/file.py",))
                    ),
                ),
            ]
        }
    )
    _patch_status_cli(monkeypatch, client)

    status = issue_claim.main(["--repo", "example/agent-claim", "status", "72", "--json"])
    assert status == 0
    assert (
        capsys.readouterr().out
        == json.dumps(
            {
                "ledger": LEDGER_ISSUE,
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
                "unreadable": [],
            }
        )
        + "\n"
    )


def test_cli_status_json_reports_conflict_state_for_duplicate_issue_claims(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Two live claims on the same issue are a genuine conflict (same identity),
    unlike the merely path-overlapping claims on different issues above --
    both the top-level state and each claim's own state must say so."""
    client = _claims_client(
        request("claim-a", "Ada", issue=72, scope=("src/a.py",)),
        request("claim-b", "Grok 4.6", issue=72, scope=("src/b.py",)),
    )
    _patch_status_cli(monkeypatch, client)

    status = issue_claim.main(["--repo", "example/agent-claim", "status", "72", "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert status == 2
    assert payload["state"] == "CONFLICT"
    assert {claim["claim_id"]: claim["state"] for claim in payload["claims"]} == {
        "claim-a": "CONFLICT",
        "claim-b": "CONFLICT",
    }


def test_cli_status_json_without_ledger_errors_and_prints_no_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _patch_status_cli(monkeypatch, FakeForge(), ledger=None)

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "--json"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "ERROR:" in captured.err
    assert "no agent-claim ledger exists" in captured.err


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
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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

    assert claimed == 0
    assert capsys.readouterr().out == (
        "CLAIMED issue #72: cli-claim "
        "https://github.com/example/agent-claim/issues/71#issuecomment-1\n"
        "1 of 4 versioned files (25%); overlaps no other open claims\n"
    )


def test_cli_claim_replay_reports_the_matching_live_claim_after_an_interrupted_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    existing = request("live-claim", "Ada", issue=72, branch="codex/issue-72", scope=("src",))
    client = _claims_client(existing)
    _patch_status_cli(monkeypatch, client)
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
    ]

    assert issue_claim.main(arguments) == 0
    assert capsys.readouterr().out == (
        "CLAIMED issue #72: live-claim "
        "https://github.com/example/agent-claim/issues/71#issuecomment-1\n"
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
    assert len(client.comments[LEDGER_ISSUE]) == 1


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
    client = _claims_client(
        request("live-claim", "Ada", issue=72, branch="codex/issue-72", scope=("src",))
    )
    _patch_status_cli(monkeypatch, client)
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
    assert len(client.comments[LEDGER_ISSUE]) == 1


def test_cli_claim_replay_skips_out_of_order_for_the_matching_lower_priority_item(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    existing = request("live-claim", "Ada", issue=10, branch="codex/issue-10", scope=("src",))
    client = _claims_client(existing)
    client.board_issues = (
        board_issue(10, "Lower work", complete_contract("Claim #10.")),
        board_issue(11, "Top work", complete_contract("Claim #11.")),
        board_issue(12, "Depends on top", "## Blocked by\n#11"),
    )
    _patch_status_cli(monkeypatch, client)
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
            ]
        )
        == 0
    )

    assert "out-of-order" not in capsys.readouterr().out
    assert len(client.comments[LEDGER_ISSUE]) == 1


def test_cli_claim_replay_does_not_bypass_out_of_order_for_another_agent(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    existing = request("live-claim", "Ada", issue=10, branch="codex/issue-10", scope=("src",))
    client = _claims_client(existing)
    client.board_issues = (
        board_issue(10, "Lower work", complete_contract("Claim #10.")),
        board_issue(11, "Top work", complete_contract("Claim #11.")),
        board_issue(12, "Depends on top", "## Blocked by\n#11"),
    )
    _patch_status_cli(monkeypatch, client)
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
    assert len(client.comments[LEDGER_ISSUE]) == 1


def test_cli_claim_replay_does_not_resurrect_a_released_claim(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    existing = request("released-claim", "Ada", issue=72, branch="codex/issue-72", scope=("src",))
    client = _claims_client(existing)
    released = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))[0]
    client.post_comment(
        LEDGER_ISSUE,
        release_comment(released, "Ada", "builder", "landed"),
    )
    _patch_status_cli(monkeypatch, client)
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
    assert len(client.comments[LEDGER_ISSUE]) == 2


def test_cli_comma_joined_scope_is_stored_as_distinct_paths_and_overlaps(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    claimed = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "ReproAgentA",
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "docs/PRODUCT.md,src/atelier2/adapters/dbos/run_transitions.py",
            "--claim-id",
            "joined",
        ]
    )

    assert claimed == 0
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    assert isinstance(posted, ActiveClaim)
    assert posted.scope == (
        "docs/PRODUCT.md",
        "src/atelier2/adapters/dbos/run_transitions.py",
    )

    second = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "73",
            "--agent",
            "ReproAgentB",
            "--role",
            "builder",
            "--base",
            BASE,
            "--branch",
            "codex/issue-73",
            "--scope",
            "docs/PRODUCT.md",
            "--claim-id",
            "single",
        ]
    )
    captured = capsys.readouterr()

    assert second == 0
    assert "CLAIMED issue #73: single " in captured.out
    assert "overlaps issue #72 (joined)" in captured.out
    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == 2


def test_cli_comma_joined_scope_flag_equals_repeated_scope_flags(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

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
    first = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    second = parse_claim_event(repeated_client.comments[LEDGER_ISSUE][0])
    assert isinstance(first, ActiveClaim)
    assert isinstance(second, ActiveClaim)
    assert first.scope == second.scope == ("docs/PRODUCT.md", "src/widget.py")


def test_cli_rescope_adds_a_path_without_matching_head_or_a_clean_tree(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    acquired = acquire_claim(
        client,
        request(issue=72, branch="codex/issue-72", scope=("src/widget.py",)),
    )
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout(head="b" * 40, dirty=" M file")
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})

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
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert standing[0].claim_id == acquired.claim_id
    assert standing[0].base == BASE
    assert standing[0].scope == ("src/widget.py", "src/new.py")


def test_cli_rescope_json_prints_updated_scope_and_same_claim_id(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    acquire_claim(
        client,
        request("cli-claim", "Ada", issue=72, branch="codex/issue-72", scope=("src/widget.py",)),
    )
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout()
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Ada"})

    status = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "rescope",
            "72",
            "--add",
            "docs/PRODUCT.md,src/new.py",
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


def test_cli_rescope_without_add_or_drop_is_an_error(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    acquire_claim(
        client,
        request(issue=72, branch="codex/issue-72", scope=("src/widget.py",)),
    )
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout()
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(["--repo", "example/agent-claim", "rescope", "72"])
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "ERROR:" in captured.err
    assert "--add" in captured.err or "rescope requires" in captured.err


def test_cli_rescope_refuses_primary_checkout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    acquire_claim(
        client,
        request(issue=72, branch="codex/issue-72", scope=("src/widget.py",)),
    )
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout(git_directory="/repo/.git", common_directory="/repo/.git")
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})

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
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    assert LEDGER_ISSUE not in client.comments


def test_cli_claim_wide_scope_refusal_names_the_directory(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A directory-tripped refusal names the directory, not the whole rule."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    assert LEDGER_ISSUE not in client.comments


def test_cli_who_prints_the_claim_holding_a_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _claims_client(
        request("mine", "Ada", issue=72, scope=("docs/PRODUCT.md", "src/widget.py"))
    )
    _patch_status_cli(monkeypatch, client)

    claimed = issue_claim.main(["--repo", "example/agent-claim", "who", "docs/PRODUCT.md"])
    free = issue_claim.main(["--repo", "example/agent-claim", "who", "README.md"])
    claimed_out = capsys.readouterr().out

    assert claimed == 0
    assert free == 0
    assert "CLAIMED docs/PRODUCT.md issue #72: Ada (builder) claim=mine" in claimed_out
    assert "UNCLAIMED README.md" in claimed_out


def test_cli_who_json_prints_holder_or_unclaimed(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _claims_client(request("mine", "Ada", issue=72, scope=("docs",)))
    _patch_status_cli(monkeypatch, client)

    descendant = issue_claim.main(
        ["--repo", "example/agent-claim", "who", "docs/decisions/one.md", "--json"]
    )
    claimed = json.loads(capsys.readouterr().out)
    free = issue_claim.main(["--repo", "example/agent-claim", "who", "src/widget.py", "--json"])
    unclaimed = json.loads(capsys.readouterr().out)

    assert descendant == 0
    assert claimed["state"] == "CLAIMED"
    assert claimed["path"] == "docs/decisions/one.md"
    assert claimed["claims"][0]["claim_id"] == "mine"
    assert free == 0
    assert unclaimed == {
        "ledger": LEDGER_ISSUE,
        "path": "src/widget.py",
        "state": "UNCLAIMED",
        "claims": [],
    }


def test_cli_rescope_refuses_adding_a_directory_without_whole(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    acquire_claim(
        client,
        request(issue=72, branch="codex/issue-72", scope=("src/widget.py",)),
    )
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout()
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: paths)
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})

    status = issue_claim.main(["--repo", "example/agent-claim", "rescope", "72", "--add", "docs"])
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "scope is wide" in captured.err
    assert "--whole" in captured.err
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert standing[0].scope == ("src/widget.py",)


def test_cli_claim_share_above_a_quarter_requires_whole(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
            "LICENSE",
            "--scope",
            "README.md",
            "--claim-id",
            "wide",
        ]
    )
    captured = capsys.readouterr()

    assert status == 2
    assert captured.out == ""
    assert "scope is wide" in captured.err
    assert "--whole" in captured.err
    assert LEDGER_ISSUE not in client.comments


def test_cli_claim_wide_scope_refusal_names_the_share(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A share-tripped refusal names the covered/versioned counts and the
    percentage, not the whole rule."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
            "LICENSE",
            "--scope",
            "README.md",
            "--claim-id",
            "named-share",
        ]
    )

    assert status == 2
    assert capsys.readouterr().err == (
        "ERROR: scope is wide: 2 paths of 4 versioned files (50 %) exceeds a quarter; "
        "pass --whole REASON\n"
    )


def test_cli_claim_share_above_a_quarter_succeeds_with_whole(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
            "LICENSE",
            "--scope",
            "README.md",
            "--whole",
            "cover two files",
            "--claim-id",
            "wide",
            "--json",
        ]
    )
    payload = json.loads(capsys.readouterr().out)

    assert status == 0
    assert payload["versioned_files"] == 2
    assert payload["versioned_files_total"] == 4
    assert payload["share"] == 0.5
    assert payload["touches"] == []
    assert "- Whole: cover two files" in client.comments[LEDGER_ISSUE][0].body


def test_cli_claim_share_at_a_quarter_does_not_need_whole(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
            "--claim-id",
            "quarter",
        ]
    )

    assert status == 0
    assert capsys.readouterr().out.endswith(
        "1 of 4 versioned files (25%); overlaps no other open claims\n"
    )


def test_cli_claim_reports_the_claim_when_the_post_claim_reconcile_fails(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The label/projection reconcile that follows a winning claim post can
    itself fail; the claim above is already live on the ledger (the earlier
    race checks all passed), so this must never read as a refusal — the
    operator must see the claim id and an explicit "the claim exists"
    message, even under --json where there is no well-formed claim payload
    left to emit."""
    client = FakeForge(fail_add_label=True)
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())

    exit_code = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "claim",
            "72",
            "--agent",
            "Codex Sol",
            "--base",
            BASE,
            "--branch",
            "codex/issue-72",
            "--scope",
            "src/work.py",
            "--claim-id",
            "flaky-reconcile",
            "--json",
        ]
    )

    assert exit_code == 2
    captured = capsys.readouterr()
    assert "CLAIMED issue #72: flaky-reconcile" in captured.out
    assert "ERROR: the claim above exists, but the post-claim reconcile failed" in captured.err
    posted = active_claims(tuple(client.comments[LEDGER_ISSUE]))
    assert any(claim.claim_id == "flaky-reconcile" for claim in posted)


def test_cli_claim_touches_stay_empty_beside_a_disjoint_standing_claim(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _claims_client(request("claim-a", "Ada", issue=73, scope=("LICENSE",)))
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    client = _claims_client(request("claim-a", "Ada", issue=73, scope=("src",)))
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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


def test_claim_cost_lists_an_overlapping_standing_claim_as_a_touch() -> None:
    standing = parse_claim_event(
        comment(1, claim_comment(request("claim-a", issue=55, scope=("src",))))
    )
    lane = parse_claim_event(
        comment(
            2,
            claim_comment(
                request("claim-b", "Grok 4.6", lane=True, branch="docs/foo", scope=("docs",))
            ),
        )
    )
    assert isinstance(standing, ActiveClaim)
    assert isinstance(lane, ActiveClaim)
    overlapping = protocol.conflicting_claims(
        (standing, lane), request("challenger", issue=56, scope=("src/widget.py",))
    )
    both = protocol.conflicting_claims(
        (standing, lane), request("wide", issue=56, scope=("src", "docs"))
    )

    assert [claim.claim_id for claim in overlapping] == ["claim-a"]
    assert issue_claim._touch_summary(overlapping) == "overlaps issue #55 (claim-a)"
    assert issue_claim._touch_summary(both) == (
        "overlaps issue #55 (claim-a), lane docs/foo (claim-b)"
    )
    assert issue_claim._touch_summary(()) == "overlaps no other open claims"


def test_claim_age_old_compares_real_age_against_the_threshold() -> None:
    just_over_an_hour = timedelta(seconds=3601)
    exactly_one_hour = timedelta(hours=1)
    sixty_one_minutes = timedelta(seconds=3660)

    assert board.format_claim_age(just_over_an_hour) == "1h 0m"
    assert board.claim_is_old(just_over_an_hour) is True
    assert board.format_claim_age(sixty_one_minutes) == "1h 1m"
    assert board.claim_is_old(sixty_one_minutes) is True
    assert board.claim_is_old(exactly_one_hour) is False


def test_status_and_board_show_claim_age_from_the_claim_comment(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    claimed = request("mine", "Ada", issue=72, branch="codex/issue-72", scope=("src",))
    fresh = comment(1, claim_comment(claimed), created_at="2026-08-20T23:30:00Z")
    client = FakeForge({LEDGER_ISSUE: [fresh]})
    client.board_issues = (board_issue(72, "Work", complete_contract("Claim #72.")),)
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72"]) == 0
    status_out = capsys.readouterr().out
    assert " 0h 30m\n" in status_out
    assert " old" not in status_out.split("CLAIMED", 1)[1]

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["claims"][0]["age"] == "0h 30m"
    assert payload["claims"][0]["old"] is False

    assert issue_claim.main(["--repo", "example/agent-claim", "board"]) == 0
    assert "Ada (builder) 0h 30m" in capsys.readouterr().out
    assert issue_claim.main(["--repo", "example/agent-claim", "board", "--json"]) == 0
    item = next(row for row in json.loads(capsys.readouterr().out)["items"] if row["number"] == 72)
    assert item["claim_age"] == "0h 30m"
    assert item["claim_old"] is False


def test_status_and_board_mark_a_claim_old_after_sixty_one_minutes(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    claimed = request("mine", "Ada", issue=72, branch="codex/issue-72", scope=("src",))
    old = comment(1, claim_comment(claimed), created_at="2026-08-20T22:59:00Z")
    client = FakeForge({LEDGER_ISSUE: [old]})
    client.board_issues = (board_issue(72, "Work", complete_contract("Claim #72.")),)
    _patch_status_cli(monkeypatch, client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landing_times", lambda: ())

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72"]) == 0
    assert " 1h 1m old\n" in capsys.readouterr().out

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["claims"][0]["age"] == "1h 1m"
    assert payload["claims"][0]["old"] is True

    assert issue_claim.main(["--repo", "example/agent-claim", "board"]) == 0
    assert "Ada (builder) 1h 1m old" in capsys.readouterr().out


def test_claim_age_uses_the_claim_comment_not_a_later_rescope(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    claimed = request("mine", "Ada", issue=72, branch="codex/issue-72", scope=("src",))
    claim_event = comment(1, claim_comment(claimed), created_at="2026-08-20T23:30:00Z")
    parsed = parse_claim_event(claim_event)
    assert isinstance(parsed, ActiveClaim)
    rescope_event = comment(
        2,
        protocol.rescope_comment(parsed, ("src", "LICENSE"), "Ada", "builder"),
        created_at="2026-08-20T23:59:00Z",
    )
    client = FakeForge({LEDGER_ISSUE: [claim_event, rescope_event]})
    _patch_status_cli(monkeypatch, client)

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72"]) == 0
    out = capsys.readouterr().out
    assert " 0h 30m\n" in out
    assert " old" not in out.split("CLAIMED", 1)[1]


def test_rescope_whole_reason_lifecycle_none_reason_kept_then_cleared() -> None:
    """The full None -> reason -> (kept across an unrelated later rescope) ->
    cleared lifecycle for `whole_reason`, proven through `active_claims` -- the
    ledger's real reader (issue #136 delta review). Omitting `whole` on an
    ordinary rescope leaves an existing reason alone; only the dedicated
    `rescope_clear_whole_reason_comment` clears it back to unset."""
    claim_event = comment(1, claim_comment(request(issue=72, scope=("src",))))
    claimed = parse_claim_event(claim_event)
    assert isinstance(claimed, ActiveClaim)
    assert claimed.whole_reason is None

    set_reason_event = comment(
        2,
        protocol.rescope_comment(
            claimed, ("src",), "Codex Sol", "builder", whole_reason="a reason"
        ),
    )
    with_reason = active_claims((claim_event, set_reason_event))
    assert with_reason[0].whole_reason == "a reason"

    unrelated_rescope_event = comment(
        3, protocol.rescope_comment(with_reason[0], ("src", "docs"), "Codex Sol", "builder")
    )
    kept = active_claims((claim_event, set_reason_event, unrelated_rescope_event))
    assert kept[0].whole_reason == "a reason"
    assert kept[0].scope == ("src", "docs")

    cleared_event = comment(
        4,
        protocol.rescope_clear_whole_reason_comment(kept[0], ("src",), "Codex Sol", "builder"),
    )
    cleared = active_claims((claim_event, set_reason_event, unrelated_rescope_event, cleared_event))
    assert cleared[0].whole_reason is None
    assert cleared[0].scope == ("src",)


def test_parse_claim_rescope_rejects_a_marker_with_both_whole_and_whole_clear() -> None:
    """The parser still refuses this wire-level combination even though this
    reader's own writer can no longer construct it (issue #136 delta review):
    `rescope_comment` and `rescope_clear_whole_reason_comment` are now separate
    functions, so setting and clearing at once is structurally impossible from
    this writer, but a marker from another writer -- or a hand-crafted one --
    could still carry both keys."""
    payload = {
        "action": "rescope",
        "agent": "Codex Sol",
        "claim_id": "claim-a",
        "issue": 72,
        "role": "builder",
        "scope": ["src"],
        "whole": "a reason",
        "whole_clear": True,
    }
    both_set_and_clear_comment = comment(1, marker(payload))
    with pytest.raises(InvalidClaimMarkerError, match="cannot both set and clear"):
        parse_claim_event(both_set_and_clear_comment)


def test_parse_claim_rescope_requires_whole_clear_to_be_exactly_true() -> None:
    payload = {
        "action": "rescope",
        "agent": "Codex Sol",
        "claim_id": "claim-a",
        "issue": 72,
        "role": "builder",
        "scope": ["src"],
        "whole_clear": "yes",
    }

    raised_argument_1 = comment(1, marker(payload))
    with pytest.raises(InvalidClaimMarkerError, match="whole_clear field must be true"):
        parse_claim_event(raised_argument_1)


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
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    assert LEDGER_ISSUE not in client.comments


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
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    assert LEDGER_ISSUE not in client.comments


def test_cli_lane_directory_without_whole_is_wide(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _set_agent_identity_env(monkeypatch, {"AGENT_CLAIM_AGENT": "Ada"})
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    assert LEDGER_ISSUE not in client.comments


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
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    assert LEDGER_ISSUE not in client.comments


def test_cli_rescope_add_that_raises_combined_share_requires_whole(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    acquire_claim(
        client,
        request(issue=72, branch="codex/issue-72", scope=("src",)),
    )
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout()
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})

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
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert standing[0].scope == ("src",)


def test_cli_rescope_persists_whole_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeForge()
    acquire_claim(
        client,
        request(issue=72, branch="codex/issue-72", scope=("src/widget.py",)),
    )
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout()
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: paths)
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})

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
    bodies = [entry.body for entry in client.comments[LEDGER_ISSUE]]
    assert any("- Whole: widen to the docs tree" in body for body in bodies)
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert standing[0].scope == ("src/widget.py", "docs")
    assert standing[0].whole_reason == "widen to the docs tree"


def test_scope_is_wide_for_more_than_three_paths_any_directory_or_a_share_above_a_quarter() -> None:
    three = ("a.py", "b.py", "c.py")
    four = (*three, "d.py")
    assert (
        protocol.scope_is_wide(three, directories=(), covered_file_count=3, versioned_file_count=20)
        is False
    )
    assert (
        protocol.scope_is_wide(four, directories=(), covered_file_count=4, versioned_file_count=20)
        is True
    )
    assert (
        protocol.scope_is_wide(
            ("docs",), directories=("docs",), covered_file_count=1, versioned_file_count=20
        )
        is True
    )
    assert (
        protocol.scope_is_wide(
            ("a.py",), directories=(), covered_file_count=1, versioned_file_count=4
        )
        is False
    )
    assert (
        protocol.scope_is_wide(
            ("a.py", "b.py"), directories=(), covered_file_count=2, versioned_file_count=4
        )
        is True
    )
    assert (
        protocol.scope_is_wide(
            ("a.py",), directories=(), covered_file_count=0, versioned_file_count=0
        )
        is False
    )


def test_wide_scope_trip_names_the_condition_in_the_rule_s_priority_order() -> None:
    """`scope_is_wide` is `wide_scope_trip(...) is not None` -- one rule, one
    owner -- and a path-count trip outranks a directory trip that would also
    fire, exactly as `scope_is_wide` already prioritizes them."""
    four = ("a.py", "b.py", "c.py", "d.py")
    assert protocol.wide_scope_trip(
        four, directories=("a.py",), covered_file_count=4, versioned_file_count=20
    ) == protocol.WideScopeTrip(protocol.WideScopeReason.PATH_COUNT, 4, ("a.py",), 4, 20)
    assert protocol.wide_scope_trip(
        ("docs",), directories=("docs",), covered_file_count=1, versioned_file_count=20
    ) == protocol.WideScopeTrip(protocol.WideScopeReason.DIRECTORY, 1, ("docs",), 1, 20)
    assert protocol.wide_scope_trip(
        ("a.py", "b.py"), directories=(), covered_file_count=2, versioned_file_count=4
    ) == protocol.WideScopeTrip(protocol.WideScopeReason.SHARE, 2, (), 2, 4)
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
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    assert isinstance(posted, ActiveClaim)
    assert posted.scope == ("new_a.py", "new_b.py", "new_c.py")
    assert posted.whole_reason is None


def test_cli_claim_refuses_four_named_paths_without_whole(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    assert LEDGER_ISSUE not in client.comments


def test_cli_claim_wide_scope_refusal_names_the_path_count(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A path-count-tripped refusal names the count and the limit, not the
    whole rule."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    acquire_claim(
        client,
        request(issue=72, branch="codex/issue-72", scope=("new_a.py",)),
    )
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    git_values = _git_checkout()
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: git_values[tuple(arguments)])
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    _set_agent_identity_env(monkeypatch, {issue_claim.AGENT_CLAIM_AGENT_ENV: "Codex Sol"})

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
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert standing[0].scope == ("new_a.py",)


def test_cli_claim_persists_whole_reason_and_status_and_who_show_it(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    assert isinstance(posted, ActiveClaim)
    assert posted.whole_reason == reason
    assert f"- Whole: {reason}" in client.comments[LEDGER_ISSUE][0].body

    _patch_status_cli(monkeypatch, client)
    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72"]) == 0
    status_out = capsys.readouterr().out
    assert f"  whole: {reason}" in status_out

    assert issue_claim.main(["--repo", "example/agent-claim", "status", "72", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["claims"][0]["whole"] == reason

    assert issue_claim.main(["--repo", "example/agent-claim", "who", "new_a.py"]) == 0
    who_out = capsys.readouterr().out
    assert f"  whole: {reason}" in who_out

    assert issue_claim.main(["--repo", "example/agent-claim", "who", "new_a.py", "--json"]) == 0
    who_payload = json.loads(capsys.readouterr().out)
    assert who_payload["claims"][0]["whole"] == reason


def test_cli_claim_allows_a_directory_scope_with_whole(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    assert isinstance(posted, ActiveClaim)
    assert posted.scope == ("docs",)
    assert posted.whole_reason == "rewrite the docs tree"
    assert "- Whole: rewrite the docs tree" in client.comments[LEDGER_ISSUE][0].body


def test_cli_release_without_json_prints_the_released_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _claims_client(
        request("mine", "Ada", issue=72, role="reviewer", branch="lane-72", scope=("src",))
    )
    _patch_release_session(monkeypatch, client)

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
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
                "url": "https://github.com/example/agent-claim/issues/71#issuecomment-1",
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
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    assert isinstance(posted, ActiveClaim)
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
    client = _claims_client(
        request(
            "mine", "Ada", issue=72, lane=lane, role=standing_role, branch=branch, scope=("src",)
        )
    )
    # Lane mode always derives its branch from the checkout, even with an explicit
    # --claim-id (Entschieden #2: LaneIdentity carries no branch of its own), so git
    # is only forbidden for the issue-mode explicit-claim-id case.
    forbid_git = bool(issue_argument) and "--claim-id" in flags
    _patch_release_session(monkeypatch, client, agent=agent, branch=branch, forbid_git=forbid_git)

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
    assert active_claims(tuple(client.comments[LEDGER_ISSUE])) == ()


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
def test_cli_claim_and_release_json_errors_print_no_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    _patch_status_cli(monkeypatch, FakeForge(), ledger=None)

    assert issue_claim.main(["--repo", "example/agent-claim", *arguments]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("ERROR:")
    assert "no agent-claim ledger exists" in captured.err


def test_cli_claim_json_conflict_errors_without_success_json(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = _claims_client(request(issue=72, scope=("src",)))
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
    monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
    protocol_count = len(client.list_protocol_candidates(LEDGER_ISSUE))

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
    assert captured.out == ""
    assert captured.err.startswith("ERROR:")
    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == protocol_count


def test_cli_supersede_freezes_the_drained_ledger_and_prints_the_contract_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    client = FakeForge(valid_successors={170})
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda client: LEDGER_ISSUE)
    acquired = acquire_claim(client, request(issue=LEDGER_ISSUE))

    frozen = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "supersede",
            "170",
            "--agent",
            "Fleet Coordinator",
            "--role",
            "coordinator",
            "--reason",
            "reviewed successor ready",
            "--claim-id",
            acquired.claim_id,
        ]
    )

    captured = capsys.readouterr()
    assert frozen == 0
    assert captured.out == (
        f"SUPERSEDED ledger #{LEDGER_ISSUE} successor #170: {acquired.claim_id}\n"
    )
    assert LEDGER_ISSUE not in client.labels
    assert "not available in v0.1" not in captured.out
    assert "not available in v0.1" not in captured.err
    raised_argument_1 = client.list_protocol_candidates(LEDGER_ISSUE)
    with pytest.raises(LedgerSupersededError, match="successor #170"):
        active_claims(raised_argument_1)


@pytest.mark.parametrize("failure", ["builder", "drain"])
def test_cli_supersede_fails_closed_without_mutating_protocol_candidates(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    failure: str,
) -> None:
    client = FakeForge(valid_successors={170})
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda client: LEDGER_ISSUE)
    acquired = acquire_claim(client, request(issue=LEDGER_ISSUE))
    if failure == "drain":
        acquire_claim(client, request("other", issue=72, scope=("frontend",)))
    protocol_count = len(client.list_protocol_candidates(LEDGER_ISSUE))

    frozen = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "supersede",
            "170",
            "--agent",
            "Fleet Coordinator",
            "--role",
            "builder" if failure == "builder" else "coordinator",
            "--reason",
            "reviewed successor ready",
            "--claim-id",
            acquired.claim_id,
        ]
    )

    captured = capsys.readouterr()
    assert frozen == 2
    assert captured.err.startswith("ERROR:")
    assert len(client.list_protocol_candidates(LEDGER_ISSUE)) == protocol_count


def forbid_github_for_policy(monkeypatch: pytest.MonkeyPatch) -> None:
    def unused(*args, **kwargs):
        pytest.fail("policy must not use GitHub")

    monkeypatch.setattr(github, "GitHubForge", unused)
    monkeypatch.setattr(github, "discover_repository", unused)
    monkeypatch.setattr(discovery, "discover_ledger", unused)


@pytest.mark.parametrize(
    "arguments",
    [
        ["policy", "--print"],
        ["--repo", "OWNER/REPO", "policy", "--print"],
    ],
)
def test_cli_policy_print_emits_the_locked_loader_without_github(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    arguments: list[str],
) -> None:
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    forbid_github_for_policy(monkeypatch)

    assert issue_claim.main(arguments) == 0
    captured = capsys.readouterr()
    assert captured.out == (
        "<!-- agent-claim-policy:v1 -->\n"
        "Before the first edit in a Git repository, use live `agent-claim`: "
        "`status`, then `claim` the issue and write scope. `bootstrap` only when "
        "neither a coordination/claim contract nor a ledger exists. `release` after "
        "landing or abandoning the lane. Missing `gh` or network is a failure, "
        "never coordinated success. Read-only review stays free. Do not invent a "
        "second board.\n"
    )
    assert captured.err == ""
    assert list(home.iterdir()) == []
    assert list(work.iterdir()) == []


def test_cli_module_entry_point_exits_with_mains_return_code(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`python -m agent_claim.cli` and the installed console script run the
    `if __name__ == "__main__":` guard, not `main()` as a library call --
    exercise that guard directly rather than only ever calling `main()`."""
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    forbid_github_for_policy(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["agent-claim", "policy", "--print"])

    with pytest.warns(RuntimeWarning, match="agent_claim.cli"), pytest.raises(SystemExit) as exited:
        runpy.run_module("agent_claim.cli", run_name="__main__")

    assert exited.value.code == 0
    assert capsys.readouterr().out.startswith("<!-- agent-claim-policy:v1 -->\n")


def test_cli_policy_without_print_is_an_argparse_error(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    home = tmp_path / "home"
    work = tmp_path / "work"
    home.mkdir()
    work.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(work)
    forbid_github_for_policy(monkeypatch)

    with pytest.raises(SystemExit) as exited:
        issue_claim.main(["policy"])

    assert exited.value.code == 2
    assert list(home.iterdir()) == []
    assert list(work.iterdir()) == []


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


def _patch_protect_claim(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agent: str = "Grok sess-1",
    scope: tuple[str, ...] = ("src",),
    branch: str = "codex/issue-72-claims",
    lane: bool = False,
) -> FakeForge:
    """The client is a `ReaderOnlyForge`: `protect` reads live claims and never
    writes, so every test built on this helper is also proof of that.
    """
    claimed = comment(
        1,
        claim_comment(
            replace(
                request("cli-claim", agent, issue=72, lane=lane, scope=scope),
                branch=branch,
            )
        ),
    )
    client = ReaderOnlyForge({LEDGER_ISSUE: [claimed]}, {72})
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    return client


def _forbid_protect_git_github_and_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    def unused(*args, **kwargs):
        pytest.fail("this protect path must not use identity, git, or GitHub")

    monkeypatch.setattr(checkout, "_resolved_agent", unused)
    monkeypatch.setattr(checkout, "_git_output", unused)
    monkeypatch.setattr(github, "GitHubForge", unused)
    monkeypatch.setattr(github, "discover_repository", unused)
    monkeypatch.setattr(discovery, "discover_ledger", unused)
    monkeypatch.setattr(protocol, "configure_ledger", unused)


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


def test_protect_allowed_write_resolves_identity_then_git_then_github(
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

    claimed = comment(
        1,
        claim_comment(
            replace(
                request("cli-claim", "Grok sess-1", issue=72, scope=("src",)),
                branch="codex/issue-72-claims",
            )
        ),
    )
    client = ReaderOnlyForge({LEDGER_ISSUE: [claimed]}, {72})

    def github_forge(repository: object) -> ReaderOnlyForge:
        calls.append("github")
        return client

    def discover_ledger(_client: object) -> int:
        calls.append("github")
        return LEDGER_ISSUE

    monkeypatch.setattr(github, "GitHubForge", github_forge)
    monkeypatch.setattr(discovery, "discover_ledger", discover_ledger)

    assert (
        _protect_main(
            monkeypatch,
            {"toolName": "write", "toolInput": {"path": "src/widget.py"}},
        )
        == 0
    )
    _assert_protect_decision(capsys, decision="allow")
    assert calls == ["identity", "git", "git", "git", "git", "github", "github"]


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
    target = work / "src" / "agent_claim" / "cli.py"

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


def test_protect_missing_ledger_denies_claim_first_without_configure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    monkeypatch.setattr(github, "GitHubForge", lambda repository: FakeForge())
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: None)

    def unused_configure(issue: int) -> None:
        pytest.fail("missing ledger must not configure_ledger")

    monkeypatch.setattr(protocol, "configure_ledger", unused_configure)

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


def test_protect_ledger_error_denies_json_without_error_prefix(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    _isolate_protect_home(monkeypatch, tmp_path)
    work = tmp_path / "work"
    _set_agent_identity_env(monkeypatch, {issue_claim.GROK_SESSION_ID_ENV: "sess-1"})
    _patch_protect_git(monkeypatch, work)
    monkeypatch.setattr(github, "GitHubForge", lambda repository: FakeForge())

    def failed(_client):
        raise ClaimError("adapter failed")

    monkeypatch.setattr(discovery, "discover_ledger", failed)

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
    monkeypatch.setattr(github, "GitHubForge", lambda repository: FakeForge())

    def crashed(_client):
        raise RuntimeError("write path crashed")

    monkeypatch.setattr(discovery, "discover_ledger", crashed)

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


def test_two_lanes_may_claim_the_same_file() -> None:
    client = FakeForge()
    first = acquire_claim(client, request(issue=72, scope=("src/widget.py",)))
    second = acquire_claim(
        client, request("claim-b", "Grok 4.6", issue=73, scope=("src/widget.py",))
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert {claim.claim_id for claim in standing} == {first.claim_id, second.claim_id}
    holders = claims_holding_path(standing, "src/widget.py")
    assert {claim.claim_id for claim in holders} == {first.claim_id, second.claim_id}


def test_many_lanes_may_claim_the_same_directory() -> None:
    client = FakeForge()
    acquired = [
        acquire_claim(
            client,
            request(f"claim-{index}", f"Agent {index}", issue=100 + index, scope=("src",)),
        )
        for index in range(8)
    ]

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert len(standing) == 8
    assert {claim.claim_id for claim in standing} == {claim.claim_id for claim in acquired}


def test_same_issue_still_refuses_a_second_live_claim() -> None:
    client = FakeForge()
    acquire_claim(client, request(issue=72, scope=("src/a.py",)))

    raised_argument_1 = request("claim-b", "Grok 4.6", issue=72, scope=("src/b.py",))
    with pytest.raises(ClaimUnavailableError, match="issue #72 is claimed"):
        acquire_claim(client, raised_argument_1)


def test_claim_comment_refuses_a_non_positive_resource_value() -> None:
    raised_argument_1 = request(resource="schema-hop", resource_value=0)
    with pytest.raises(ClaimError, match="resource value must be a positive integer"):
        claim_comment(raised_argument_1)


def test_acquire_claim_refuses_a_resource_value_without_a_resource_name() -> None:
    raised_argument_1 = FakeForge()
    raised_argument_2 = request(resource_value=5)
    with pytest.raises(ClaimError, match="resource value requires a resource name"):
        acquire_claim(raised_argument_1, raised_argument_2)


def test_acquire_claim_refuses_a_non_positive_resource_value() -> None:
    raised_argument_1 = FakeForge()
    raised_argument_2 = request(resource="schema-hop", resource_value=0)
    with pytest.raises(ClaimError, match="resource value must be a positive integer"):
        acquire_claim(raised_argument_1, raised_argument_2)


def test_resource_allocates_unique_values_in_sequence() -> None:
    client = FakeForge()
    first = acquire_claim(client, request(issue=72, scope=("src/a.py",), resource="schema-hop"))
    second = acquire_claim(
        client,
        request("claim-b", "Grok 4.6", issue=73, scope=("src/b.py",), resource="schema-hop"),
    )

    assert first.resource == protocol.ResourceHold("schema-hop", 1)
    assert second.resource == protocol.ResourceHold("schema-hop", 2)
    first_body = client.comments[LEDGER_ISSUE][0].body
    assert "- Resource: `schema-hop`" in first_body
    assert "`schema-hop` =" not in first_body
    assert "resource_value" not in _marker_payload_keys(first_body)


def test_auto_resource_after_live_explicit_two_holds_one_not_none() -> None:
    client = FakeForge()
    explicit = acquire_claim(
        client,
        request(issue=72, scope=("src/a.py",), resource="schema-hop", resource_value=2),
    )
    auto = acquire_claim(
        client,
        request("claim-b", "Grok 4.6", issue=73, scope=("src/b.py",), resource="schema-hop"),
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    holds = {claim.resource for claim in standing}
    assert explicit.resource == protocol.ResourceHold("schema-hop", 2)
    assert auto.resource == protocol.ResourceHold("schema-hop", 1)
    assert None not in holds
    assert holds == {
        protocol.ResourceHold("schema-hop", 1),
        protocol.ResourceHold("schema-hop", 2),
    }


def test_resource_refuses_a_second_live_hold_of_the_same_value() -> None:
    client = FakeForge()
    acquire_claim(
        client,
        request(issue=72, scope=("src/a.py",), resource="schema-hop", resource_value=4),
    )

    raised_argument_1 = request(
        "claim-b",
        "Grok 4.6",
        issue=73,
        scope=("src/b.py",),
        resource="schema-hop",
        resource_value=4,
    )
    with pytest.raises(ClaimUnavailableError, match="schema-hop 4 is held by Codex Sol"):
        acquire_claim(
            client,
            raised_argument_1,
        )


def test_releasing_a_resource_drops_the_hold_and_keeps_later_values_unique() -> None:
    client = FakeForge()
    first = acquire_claim(client, request(issue=72, scope=("src/a.py",), resource="schema-hop"))
    release_claim(
        client,
        release_context(
            IssueIdentity(72),
            "Codex Sol",
            "builder",
            protocol.AbandonedRelease("stopped"),
            first.claim_id,
        ),
    )
    acquire_claim(
        client,
        request("claim-b", "Grok 4.6", issue=73, scope=("src/b.py",), resource="schema-hop"),
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert [claim.resource for claim in standing] == [protocol.ResourceHold("schema-hop", 2)]


def test_resource_race_later_auto_succeeds_with_the_next_value() -> None:
    client = FakeForge()
    earlier = comment(
        100,
        claim_comment(
            request(
                "earlier",
                "Grok 4.6",
                issue=72,
                scope=("src/a.py",),
                resource="schema-hop",
                resource_value=1,
            )
        ),
        created_at="2026-08-20T23:59:59Z",
    )
    client.inject_after_next_ledger_post = earlier

    later = acquire_claim(
        client,
        request("later", "Codex Sol", issue=73, scope=("src/b.py",), resource="schema-hop"),
    )

    standing = active_claims(tuple(client.comments[LEDGER_ISSUE]))
    by_id = {claim.claim_id: claim for claim in standing}
    assert later.resource == protocol.ResourceHold("schema-hop", 2)
    assert by_id["earlier"].resource == protocol.ResourceHold("schema-hop", 1)
    assert by_id["later"].resource == protocol.ResourceHold("schema-hop", 2)


def test_resource_race_explicit_value_still_fails_closed() -> None:
    client = FakeForge()
    earlier = comment(
        100,
        claim_comment(
            request(
                "earlier",
                "Grok 4.6",
                issue=72,
                scope=("src/a.py",),
                resource="schema-hop",
                resource_value=1,
            )
        ),
        created_at="2026-08-20T23:59:59Z",
    )
    client.inject_after_next_ledger_post = earlier

    raised_argument_1 = request(
        "later",
        "Codex Sol",
        issue=73,
        scope=("src/b.py",),
        resource="schema-hop",
        resource_value=1,
    )
    with pytest.raises(ClaimUnavailableError, match=re.escape("schema-hop 1 is held by Grok 4.6")):
        acquire_claim(
            client,
            raised_argument_1,
        )


def test_two_intents_for_the_same_value_leave_exactly_one_holder() -> None:
    client = FakeForge(
        {
            LEDGER_ISSUE: [
                comment(
                    1,
                    claim_comment(
                        request(
                            issue=72,
                            scope=("src/a.py",),
                            resource="schema-hop",
                            resource_value=1,
                        )
                    ),
                ),
                comment(
                    2,
                    claim_comment(
                        request(
                            "claim-b",
                            "Grok 4.6",
                            issue=73,
                            scope=("src/b.py",),
                            resource="schema-hop",
                            resource_value=1,
                        )
                    ),
                ),
            ]
        }
    )

    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    holds = [claim.resource for claim in standing if claim.resource is not None]
    assert holds == [protocol.ResourceHold("schema-hop", 1)]
    assert {claim.claim_id for claim in standing} == {"claim-a", "claim-b"}


def test_resource_loser_that_dies_before_retry_is_not_a_holder() -> None:
    client = FakeForge(
        {
            LEDGER_ISSUE: [
                comment(
                    1,
                    claim_comment(
                        request(
                            "first",
                            issue=72,
                            scope=("src/a.py",),
                            resource="schema-hop",
                            resource_value=1,
                        )
                    ),
                ),
                comment(
                    2,
                    claim_comment(
                        request(
                            "loser",
                            "Grok 4.6",
                            issue=73,
                            scope=("src/b.py",),
                            resource="schema-hop",
                            resource_value=1,
                        )
                    ),
                ),
            ]
        }
    )

    standing = active_claims(tuple(client.comments[LEDGER_ISSUE]))
    holders = [
        claim for claim in standing if claim.resource == protocol.ResourceHold("schema-hop", 1)
    ]
    assert [claim.claim_id for claim in holders] == ["first"]
    assert {claim.claim_id for claim in standing} == {"first", "loser"}
    assert all("## RELEASE" not in entry.body for entry in client.comments[LEDGER_ISSUE])


def test_cli_claim_resource_prints_the_allocated_value(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
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
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][0])
    assert isinstance(posted, ActiveClaim)
    assert posted.resource is None
    assert posted.requested_resource == "schema-hop"
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    assert [claim.resource for claim in standing] == [protocol.ResourceHold("schema-hop", 1)]


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
    assert "overlaps issue #72 (dir-a)" in claimed

    status = issue_claim.main(["--repo", "example/agent-claim", "status"])
    rendered = capsys.readouterr().out
    assert status == 0
    assert "CONFLICT" not in rendered
    assert "CLAIMED issue #72" in rendered
    assert "CLAIMED issue #73" in rendered
    assert "overlaps issue #73 (dir-b)" in rendered
    assert "overlaps issue #72 (dir-a)" in rendered

    who = issue_claim.main(["--repo", "example/agent-claim", "who", "src"])
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
    assert "overlaps issue #72 (file-a)" in claimed

    status = issue_claim.main(["--repo", "example/agent-claim", "status"])
    rendered = capsys.readouterr().out
    assert status == 0
    assert "CONFLICT" not in rendered
    assert "CLAIMED issue #72" in rendered
    assert "CLAIMED issue #73" in rendered

    who = issue_claim.main(["--repo", "example/agent-claim", "who", "src/widget.py"])
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
    client.inject_after_next_ledger_post = comment(
        100,
        claim_comment(
            request(
                "earlier",
                "Grok 4.6",
                issue=72,
                scope=("src/a.py",),
                resource="schema-hop",
            )
        ),
        created_at="2026-08-20T23:59:59Z",
    )

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
    standing = active_claims(client.list_protocol_candidates(LEDGER_ISSUE))
    holds = sorted(
        claim.resource.value
        for claim in standing
        if claim.resource is not None and claim.resource.name == "schema-hop"
    )

    assert status == 0
    assert payload["resource_value"] == 2
    assert holds == [1, 2]


def test_who_lists_every_holder_without_calling_overlap_an_error(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = _claims_client(
        request("mine", "Ada", issue=72, scope=("src/widget.py",)),
        request("theirs", "Grok 4.6", issue=73, scope=("src/widget.py",)),
    )
    _patch_status_cli(monkeypatch, client)

    status = issue_claim.main(["--repo", "example/agent-claim", "who", "src/widget.py"])
    rendered = capsys.readouterr().out

    assert status == 0
    assert "CONFLICT" not in rendered
    assert "CLAIMED src/widget.py issue #72" in rendered
    assert "CLAIMED src/widget.py issue #73" in rendered
    assert "overlap: issue #72 (mine), issue #73 (theirs)" in rendered


def test_ruled_expectations_without_a_date_fail_loud() -> None:
    issue = board_issue(
        10,
        "Undated",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block("- Name it. *(geregelt: ja)*", heading="Erwartung"),
    )

    raised_argument_1 = board.BoardConfig()
    raised_argument_2 = datetime(2026, 8, 21, tzinfo=UTC)
    with pytest.raises(ClaimError, match="no readable date"):
        projected_board(
            (issue,),
            (),
            (),
            (),
            raised_argument_1,
            now=raised_argument_2,
        )


def test_proposed_expectations_have_neither_fresh_nor_old() -> None:
    issue = board_issue(
        10,
        "Proposed",
        complete_contract("Claim #10.") + "\n\n" + expectation_block("- Name it. *(Default: yes)*"),
    )
    projected = projected_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.items[0].expectation_state is board.ExpectationState.PROPOSED
    assert projected.items[0].ruling_landings is None
    assert projected.items[0].ruling_old is None


def test_a_ruled_heading_rules_a_block_of_prose_lines() -> None:
    """Issue #78: the heading carries the ruling, so prose lines below it are fine."""
    issue = board_issue(
        10,
        "Ruled by heading",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block(
            "Rueckspiegel: so habe ich dich verstanden, in eigenen Worten.",
            "1. **Ein Arbeitspunkt entsteht sichtbar.** *(geregelt: ja)*",
            "   Wenn du das Projekt anbindest, erscheint der Punkt in der Warteschlange.",
            "   Sagst du nein, bleibt er unsichtbar.",
            heading="Erwartungen (refine-Lauf 27.08.2026 — GEREGELT: Operator 27.08.2026)",
        ),
    )
    projected = projected_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.items[0].expectation_state is board.ExpectationState.RULED


def test_a_proposal_marker_under_a_ruled_heading_still_surfaces_as_proposed() -> None:
    """Issue #78: an explicit still-open line contradicts its ruled heading and wins.

    A ruled heading over a line explicitly marked as a proposal is a
    contradiction to surface, not to swallow — the same silence-never-rules
    guarantee from #62 applies to an explicit contradiction, not only to an
    unmarked line.
    """
    issue = board_issue(
        10,
        "Contradicts its heading",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block(
            "Rueckspiegel: so habe ich dich verstanden, in eigenen Worten.",
            "1. **Etwas Geregeltes.** *(geregelt: ja)*",
            "2. **Etwas noch Offenes.** *(Default: later)*",
            heading="Erwartungen (refine-Lauf 27.08.2026 — GEREGELT: Operator 27.08.2026)",
        ),
    )
    projected = projected_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.items[0].expectation_state is board.ExpectationState.PROPOSED


def test_prose_without_a_ruled_heading_still_reads_as_proposed() -> None:
    """Negative guard for #78: without the heading marker, #62's per-line rule still stands."""
    issue = board_issue(
        10,
        "Unruled prose",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block(
            "Rueckspiegel: so habe ich dich verstanden, in eigenen Worten.",
            "1. **Ein Arbeitspunkt entsteht sichtbar.** *(geregelt: ja)*",
            "   Wenn du das Projekt anbindest, erscheint der Punkt in der Warteschlange.",
            heading="Erwartungen (refine-Lauf 27.08.2026)",
        ),
    )
    projected = projected_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.items[0].expectation_state is board.ExpectationState.PROPOSED


def test_one_unruled_block_among_ruled_ones_keeps_the_item_proposed() -> None:
    """Issue #78: the code only ever read the first expectation heading; a body with
    several `## Erwartungen…` blocks must reflect every one of them, not just the first.
    """
    issue = board_issue(
        10,
        "Three expectation blocks",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block(
            "- Name it. *(geregelt: ja)*",
            heading="Erwartungen (GEREGELT: Operator 27.08.2026)",
        )
        + "\n\n"
        + expectation_block(
            "- Name it. *(geregelt: ja)*",
            heading="Erwartungen des Pulls (GEREGELT: Operator 31.08.2026)",
        )
        + "\n\n"
        + expectation_block(
            "- Name it without a ruling.",
            heading="Erwartungen aus echter Benutzung",
        ),
    )
    projected = projected_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.items[0].expectation_state is board.ExpectationState.PROPOSED


def test_expectation_progress_counts_open_and_total_lines_across_blocks() -> None:
    body = (
        expectation_block(
            "- Create it. *(geregelt: ja)*",
            "- Change it without a ruling.",
            "Explanation prose is not an expectation line.",
            heading="Erwartungen (GEREGELT: Operator 27.08.2026)",
        )
        + "\n\n"
        + expectation_block(
            "1. Remove it. *(geregelt: NEIN)*",
            "2. Keep it. *(geregelt: maybe)*",
            "3. Scale it. *(geregelt: ja)*",
            heading="Erwartungen aus echter Benutzung",
        )
    )

    assert board.expectation_state(body) is board.ExpectationState.PROPOSED
    assert board.expectation_progress(body) == board.ExpectationProgress(open=2, total=5)


def test_a_new_line_without_its_own_marker_stays_proposed_under_a_ruled_heading() -> None:
    """Codex review of #78 (finding 1): a ruled heading only excuses prose, tables,
    examples and sub-headings — not a list item shaped like an expectation
    line (RULED_EXPECTATION_PATTERN/PROPOSED_EXPECTATION_PATTERN are both
    written against that shape) that was added later without carrying its
    own ruled marker. That is silence wearing the heading's ruling, which
    #62 excludes.
    """
    issue = board_issue(
        10,
        "New line under an old ruling",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block(
            "- Name it. *(geregelt: ja)*",
            "- Some new expectation added after the ruling.",
            heading="Erwartungen (GEREGELT: Operator 27.08.2026)",
        ),
    )
    projected = projected_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.items[0].expectation_state is board.ExpectationState.PROPOSED


def test_prose_and_a_table_row_stay_ruled_under_a_ruled_heading() -> None:
    """Positive control for finding 1: only expectation-shaped lines need their own marker."""
    issue = board_issue(
        10,
        "Prose and a table row under a ruling",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block(
            "- Name it. *(geregelt: ja)*",
            "Beispiel: so sieht die Anwendung im Alltag aus.",
            "| Spalte A | Spalte B |",
            "| -------- | -------- |",
            "| Wert 1   | Wert 2   |",
            heading="Erwartungen (GEREGELT: Operator 27.08.2026)",
        ),
    )
    projected = projected_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.items[0].expectation_state is board.ExpectationState.RULED


def test_a_ruled_heading_with_no_lines_beneath_it_reads_as_proposed() -> None:
    """Codex review of #78 (finding 3): a ruling over nothing is not a ruling."""
    issue = board_issue(
        10,
        "Ruled heading, empty block",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block(heading="Erwartungen (GEREGELT: Operator 27.08.2026)"),
    )
    projected = projected_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.items[0].expectation_state is board.ExpectationState.PROPOSED


def test_a_hyphenated_ja_nein_contradiction_is_not_ruled() -> None:
    """Codex review of #78 (RULED_EXPECTATION_PATTERN boundary): a hyphen glues two

    contradicting words together (`ja-nein`) rather than separating a
    keyword from its justification; the pattern's trailing-text boundary
    excludes it on purpose.
    """
    issue = board_issue(
        10,
        "Hyphenated contradiction after ja",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block(
            "- Name it. *(geregelt: ja-nein)*",
            heading="Erwartungen (GEREGELT: Operator 27.08.2026)",
        ),
    )
    projected = projected_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.items[0].expectation_state is board.ExpectationState.PROPOSED


def test_a_hyphenated_nein_ja_contradiction_is_not_ruled() -> None:
    """Codex review of #78 (RULED_EXPECTATION_PATTERN boundary): the same hyphen guard

    applies symmetrically to `NEIN-ja`.
    """
    issue = board_issue(
        10,
        "Hyphenated contradiction after NEIN",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block(
            "- Remove it. *(geregelt: NEIN-ja)*",
            heading="Erwartungen (GEREGELT: Operator 27.08.2026)",
        ),
    )
    projected = projected_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.items[0].expectation_state is board.ExpectationState.PROPOSED


def test_ja_with_an_owner_reference_still_rules() -> None:
    """Positive control: the real #79 convention (`ja — Owner ist #567`) still rules."""
    issue = board_issue(
        10,
        "Ja with an owner reference",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block(
            "- Name it. *(geregelt: ja — Owner ist #567)*",
            heading="Erwartungen (GEREGELT: Operator 27.08.2026)",
        ),
    )
    projected = projected_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.items[0].expectation_state is board.ExpectationState.RULED


def test_nein_with_a_reason_still_rules() -> None:
    """Positive control: the established `NEIN, weil …` convention still rules."""
    issue = board_issue(
        10,
        "NEIN with a reason",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block(
            "- Remove it. *(geregelt: NEIN, weil es woanders geregelt ist)*",
            heading="Erwartungen (GEREGELT: Operator 27.08.2026)",
        ),
    )
    projected = projected_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.items[0].expectation_state is board.ExpectationState.RULED


def test_a_ruling_is_old_after_ten_trunk_landings() -> None:
    issue = board_issue(
        10,
        "Ruled",
        complete_contract("Claim #10.") + "\n\n" + expectation_block("- Name it. *(geregelt: ja)*"),
    )
    landings = tuple(datetime(2026, 8, 29, hour, tzinfo=UTC) for hour in range(10))
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 30, tzinfo=UTC),
        trunk_landings=landings,
    )
    item = projected.items[0]

    assert item.ruling_landings == 10
    assert item.ruling_old is True
    assert "ruled 10 old" in board.render(projected)


def test_one_trunk_landing_does_not_make_a_ruling_old() -> None:
    issue = board_issue(
        10,
        "Ruled",
        complete_contract("Claim #10.") + "\n\n" + expectation_block("- Name it. *(geregelt: ja)*"),
    )
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 30, tzinfo=UTC),
        trunk_landings=(datetime(2026, 8, 29, tzinfo=UTC),),
    )

    assert projected.items[0].ruling_landings == 1
    assert projected.items[0].ruling_old is False


def test_same_day_trunk_landings_do_not_age_a_date_only_ruling() -> None:
    issue = board_issue(
        10,
        "Ruled",
        complete_contract("Claim #10.") + "\n\n" + expectation_block("- Name it. *(geregelt: ja)*"),
    )
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 28, tzinfo=UTC),
        trunk_landings=(datetime(2026, 8, 28, 23, tzinfo=UTC),),
    )

    assert projected.items[0].ruling_landings == 0
    assert projected.items[0].ruling_old is False


def test_operator_ruling_date_wins_over_another_heading_date() -> None:
    issue = board_issue(
        10,
        "Ruled",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block(
            "- Name it. *(geregelt: ja)*",
            heading="Erwartungen (refine-Lauf 01.08.2026 - GEREGELT: Operator 28.08.2026)",
        ),
    )
    landings = (
        datetime(2026, 8, 15, tzinfo=UTC),
        datetime(2026, 8, 29, tzinfo=UTC),
    )
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 30, tzinfo=UTC),
        trunk_landings=landings,
    )

    assert projected.items[0].ruling_landings == 1


def test_distinct_heading_dates_without_an_operator_date_fail_loud() -> None:
    issue = board_issue(
        10,
        "Ambiguous",
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block(
            "- Name it. *(geregelt: ja)*",
            heading="Erwartungen (01.08.2026 and 28.08.2026)",
        ),
    )

    raised_argument_1 = board.BoardConfig()
    raised_argument_2 = datetime(2026, 8, 21, tzinfo=UTC)
    with pytest.raises(ClaimError, match="more than one date"):
        projected_board(
            (issue,),
            (),
            (),
            (),
            raised_argument_1,
            now=raised_argument_2,
        )


def test_next_names_an_old_ruling_when_the_item_is_pulled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    issue = board_issue(
        10,
        "Work",
        complete_contract("Claim #10.") + "\n\n" + expectation_block("- Name it. *(geregelt: ja)*"),
    )
    client = _claims_client()
    monkeypatch.setattr(client, "list_open_board_issues", lambda: (issue,))
    monkeypatch.setattr(client, "list_open_board_pull_requests", lambda: ())
    monkeypatch.setattr(client, "list_recent_merged_board_pull_requests", lambda _since: ())
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments: str(tmp_path))
    monkeypatch.setattr(
        checkout,
        "trunk_landing_times",
        lambda: tuple(datetime(2026, 8, 29, hour, tzinfo=UTC) for hour in range(10)),
    )

    assert issue_claim.main(["--repo", "example/agent-claim", "next"]) == 0
    assert capsys.readouterr().out == (
        "#10 score -10: Work\n"
        "Next: Claim #10.\n"
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
        complete_contract("Claim #10.")
        + "\n\n"
        + expectation_block(
            "- Name it. *(geregelt: ja)*",
            heading="Erwartung (refine-Lauf 28.08.2026)",
        ),
    )
    old = board_issue(
        11,
        "Old",
        complete_contract("Claim #11.")
        + "\n\n"
        + expectation_block(
            "- Name it. *(geregelt: ja)*",
            heading="Erwartung (refine-Lauf 01.08.2026)",
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
    first = parse_claim_event(comment(1, claim_comment(request(issue=72, scope=("src/a.py",)))))
    second = parse_claim_event(
        comment(
            2,
            claim_comment(request("claim-b", "Grok 4.6", issue=72, scope=("src/b.py",))),
        )
    )
    assert isinstance(first, ActiveClaim)
    assert isinstance(second, ActiveClaim)

    assert _status((first, second), None) == 2
    rendered = capsys.readouterr().out
    assert rendered.count("CONFLICT") == 2


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
    assert not Path("src/agent_claim").joinpath("single_writer.py").exists()
    text = Path("src/agent_claim/protocol.py").read_text()
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


def pr_check_client(
    monkeypatch: pytest.MonkeyPatch,
    detail: forge.Landing,
    *,
    standing: tuple[ClaimRequest, ...] = (),
) -> FakeForge:
    """A client serving one pull request and the claims that back it."""
    claims = standing or (
        request("landing", issue=WORK_ITEM_ISSUE, branch=LANDING_BRANCH, scope=("src",)),
    )
    client = _claims_client(*claims)
    client.landings[detail.number] = detail
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(discovery, "discover_ledger", lambda _client: LEDGER_ISSUE)
    return client


def run_pr_check(number: int = 12) -> int:
    return issue_claim.main(["--repo", REPOSITORY, "pr-check", "--pr", str(number)])


def test_pr_check_accepts_a_claimed_work_item_that_the_pull_request_closes(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pr_check_client(
        monkeypatch,
        landing_pull_request(
            body=f"Work-Item: {REPOSITORY}#{WORK_ITEM_ISSUE}\n\nCloses #{WORK_ITEM_ISSUE}"
        ),
    )

    assert run_pr_check() == 0
    assert capsys.readouterr().out == (
        f"PR #12 by ada declares Work-Item: {REPOSITORY}#{WORK_ITEM_ISSUE}\n"
    )


def test_pr_check_succeeds_for_another_branch_with_an_unreadable_claim_on_the_ledger(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #136: a comment this reader cannot parse fences only its own claim;
    `pr-check` for a different branch's pull request still succeeds."""
    client = pr_check_client(
        monkeypatch,
        landing_pull_request(
            body=f"Work-Item: {REPOSITORY}#{WORK_ITEM_ISSUE}\n\nCloses #{WORK_ITEM_ISSUE}"
        ),
    )
    client.comments[LEDGER_ISSUE].append(unreadable_ledger_comment(99))

    assert run_pr_check() == 0
    assert capsys.readouterr().out == (
        f"PR #12 by ada declares Work-Item: {REPOSITORY}#{WORK_ITEM_ISSUE}\n"
    )


def test_pr_check_reads_the_same_work_item_from_shorthand_and_qualified_lines(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pr_check_client(
        monkeypatch,
        landing_pull_request(
            body=f"Work-Item: #{WORK_ITEM_ISSUE}\n\nCloses {REPOSITORY}#{WORK_ITEM_ISSUE}"
        ),
    )

    assert run_pr_check() == 0
    assert capsys.readouterr().out == (
        f"PR #12 by ada declares Work-Item: {REPOSITORY}#{WORK_ITEM_ISSUE}\n"
    )


def test_pr_check_accepts_an_issueless_documentation_pull_request(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pr_check_client(
        monkeypatch,
        landing_pull_request(
            body="No-Item: docs\n\nTidy the README.",
            head_ref_name=DOCUMENTATION_LANE_BRANCH,
        ),
        standing=(documentation_lane_claim(),),
    )

    assert run_pr_check() == 0
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
def test_pr_check_refuses_an_issueless_pull_request_without_its_lane_claim(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    standing: tuple[ClaimRequest, ...],
    reason: str,
) -> None:
    pr_check_client(
        monkeypatch,
        landing_pull_request(
            body="No-Item: docs\n\nTidy the README.",
            head_ref_name=DOCUMENTATION_LANE_BRANCH,
        ),
        standing=standing,
    )

    assert run_pr_check() == 1
    assert capsys.readouterr().err == f"REFUSED: pull request #12 {reason}\n"


def test_pr_check_refuses_an_issueless_pull_request_that_closes_an_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pr_check_client(
        monkeypatch,
        landing_pull_request(
            body=f"No-Item: fix\n\nCloses #{WORK_ITEM_ISSUE}",
            head_ref_name=DOCUMENTATION_LANE_BRANCH,
        ),
        standing=(documentation_lane_claim(),),
    )

    assert run_pr_check() == 1
    assert capsys.readouterr().err == (
        f"REFUSED: pull request #12 declares no work item but closes "
        f"{REPOSITORY}#{WORK_ITEM_ISSUE}; name it as the work item\n"
    )


def test_pr_check_refuses_a_pull_request_proposing_another_repositorys_branch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pr_check_client(
        monkeypatch,
        landing_pull_request(
            body=f"Work-Item: #{WORK_ITEM_ISSUE}\n\nCloses #{WORK_ITEM_ISSUE}",
            head_repository="fork/agent-claim",
        ),
    )

    assert run_pr_check() == 1
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
            f"Work-Item: #{LEDGER_ISSUE}\n\nCloses #{LEDGER_ISSUE}",
            f"names the claim ledger #{LEDGER_ISSUE} as its work item",
            id="ledger-as-work-item",
        ),
        pytest.param(
            "Work-Item: other/repo#5\n\nCloses other/repo#5",
            "names work item other/repo#5 of another repository, which holds no claim here",
            id="foreign-work-item",
        ),
    ],
)
def test_pr_check_refuses_a_pull_request_body_with_one_line(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    body: str,
    reason: str,
) -> None:
    pr_check_client(monkeypatch, landing_pull_request(body=body))

    assert run_pr_check() == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == f"REFUSED: pull request #12 {reason}\n"


def test_pr_check_refuses_a_work_item_without_a_claim_on_the_head_branch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pr_check_client(
        monkeypatch,
        landing_pull_request(body="Work-Item: #72\n\nCloses #72"),
        standing=(request("elsewhere", issue=72, branch="codex/other-lane", scope=("src",)),),
    )

    assert run_pr_check() == 1
    assert capsys.readouterr().err == (
        f"REFUSED: pull request #12 has no active claim for #72 on branch {LANDING_BRANCH!r}\n"
    )


def test_pr_check_refuses_a_pull_request_that_does_not_target_the_default_branch(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = pr_check_client(
        monkeypatch,
        landing_pull_request(body="Work-Item: #72\n\nCloses #72", base_ref_name="release"),
    )
    client.default_branch_name = "trunk"

    assert run_pr_check() == 1
    assert capsys.readouterr().err == (
        "REFUSED: pull request #12 targets 'release', not the default branch 'trunk'\n"
    )


def test_pr_check_reads_a_fenced_classification_line_as_documentation(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    pr_check_client(
        monkeypatch,
        landing_pull_request(body="Documents the convention:\n\n```\nWork-Item: #72\n```\n"),
    )

    assert run_pr_check() == 1
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
    client = _claims_client(
        request(
            "landing",
            "Ada",
            issue=None if lane else WORK_ITEM_ISSUE,
            branch=branch,
            scope=("src",),
        )
    )
    client.landings[12] = landing_pull_request(
        body=body, merged=merged, base_ref_name=base_ref_name, head_ref_name=branch
    )
    _patch_release_session(monkeypatch, client, branch=branch)
    return client


def test_release_merged_records_the_pull_request_that_landed_the_item(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")
    client.closed_issues.add(WORK_ITEM_ISSUE)
    monkeypatch.setattr(issue_claim, "_fetch_issue_reference", _LIVE_FETCH_ISSUE_REFERENCE)

    assert issue_claim.main(["--repo", REPOSITORY, "release", "72", "--merged", "12"]) == 0

    assert client.issue_reference_lookups == [WORK_ITEM_ISSUE]
    posted = parse_claim_event(client.comments[LEDGER_ISSUE][-1])
    assert isinstance(posted, ClaimantRelease)
    assert posted.reason == "merged #12"
    assert active_claims(tuple(client.comments[LEDGER_ISSUE])) == ()


def test_release_merged_accepts_an_issueless_lane_that_landed_without_an_item(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = merged_release_client(monkeypatch, body="No-Item: docs", lane=True)

    assert issue_claim.main(["--repo", REPOSITORY, "release", "--merged", "12"]) == 0

    posted = parse_claim_event(client.comments[LEDGER_ISSUE][-1])
    assert isinstance(posted, ClaimantRelease)
    assert posted.reason == "merged #12"


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
    client = merged_release_client(
        monkeypatch,
        body=scenario.body,
        merged=scenario.merged,
        base_ref_name=scenario.base_ref_name,
    )
    _stub_issue_reference(monkeypatch, {WORK_ITEM_ISSUE: (forge.ItemState.CLOSED, "", "")})

    assert issue_claim.main(["--repo", REPOSITORY, "release", "72", "--merged", "12"]) == 2
    assert capsys.readouterr().err == f"ERROR: {reason}\n"
    assert active_claims(tuple(client.comments[LEDGER_ISSUE])) != ()


def test_release_merged_refuses_while_the_work_item_is_still_open(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")

    assert issue_claim.main(["--repo", REPOSITORY, "release", "72", "--merged", "12"]) == 2
    assert capsys.readouterr().err == "ERROR: work item #72 is open, not closed\n"
    assert active_claims(tuple(client.comments[LEDGER_ISSUE])) != ()


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
    client = merged_release_client(monkeypatch, body="Work-Item: #72\n\nCloses #72")

    assert (
        issue_claim.main(["--repo", REPOSITORY, "release", "72", "--abandoned", "overtaken by #80"])
        == 0
    )

    posted = parse_claim_event(client.comments[LEDGER_ISSUE][-1])
    assert isinstance(posted, ClaimantRelease)
    assert posted.reason == "abandoned: overtaken by #80"


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


def parented_pr_check_client(
    monkeypatch: pytest.MonkeyPatch,
    *,
    body: str,
    parent_body: str,
    open_children: tuple[board.IssueReference, ...],
    parent_repository: str = REPOSITORY,
    parent_kind: board.ItemKind | None = board.ItemKind.CONTAINER,
) -> FakeForge:
    client = pr_check_client(monkeypatch, landing_pull_request(body=body))
    client.parents[WORK_ITEM_ISSUE] = board.ParentIssue(
        board.IssueReference(parent_repository, PARENT_ISSUE), parent_body, parent_kind
    )
    client.children[PARENT_ISSUE] = tuple(
        board.ChildItem(reference.number, board.ChildState.OPEN) for reference in open_children
    )
    return client


def test_pr_check_requires_the_parent_to_close_with_its_last_open_child(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Closing is required only when the parent's own `Next` line names no
    further work -- `complete_contract("keiner")` is exactly that."""
    parented_pr_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72",
        parent_body=complete_contract("keiner"),
        open_children=(board.IssueReference(REPOSITORY, WORK_ITEM_ISSUE),),
    )

    assert run_pr_check() == 1
    assert capsys.readouterr().err == (
        f"REFUSED: pull request #12 closes the last open child of parent "
        f"{REPOSITORY}#{PARENT_ISSUE}; close the parent too\n"
    )


def test_pr_check_accepts_a_last_child_landing_when_the_parent_still_has_next_work(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ruled example: the container's own `Next` line still names work, so
    the landing may pass without closing it -- a container with a single
    dispatched child is the normal case, not the end."""
    parented_pr_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72",
        parent_body=complete_contract("Cut the next slice."),
        open_children=(board.IssueReference(REPOSITORY, WORK_ITEM_ISSUE),),
    )

    assert run_pr_check() == 0
    assert capsys.readouterr().out == (
        f"PR #12 by ada declares Work-Item: {REPOSITORY}#{WORK_ITEM_ISSUE}\n"
    )


def test_pr_check_permits_but_does_not_require_closing_a_parent_with_further_next_work(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parented_pr_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72\nCloses #79",
        parent_body=complete_contract("Cut the next slice."),
        open_children=(board.IssueReference(REPOSITORY, WORK_ITEM_ISSUE),),
    )

    assert run_pr_check() == 0


def test_pr_check_refuses_a_parent_that_is_not_a_container(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parented_pr_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72",
        parent_body=complete_contract("keiner"),
        open_children=(),
        parent_kind=board.ItemKind.TASK,
    )

    assert run_pr_check() == 1
    assert capsys.readouterr().err == (
        f"REFUSED: pull request #12 has parent {REPOSITORY}#{PARENT_ISSUE} of kind task, "
        "which is not a container; only a container holds children\n"
    )


def test_pr_check_accepts_a_landing_that_closes_its_completed_parent(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parented_pr_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72\nCloses #79",
        parent_body="## Now\nEpic.",
        open_children=(board.IssueReference(REPOSITORY, WORK_ITEM_ISSUE),),
    )

    assert run_pr_check() == 0
    assert capsys.readouterr().out == (
        f"PR #12 by ada declares Work-Item: {REPOSITORY}#{WORK_ITEM_ISSUE}\n"
    )


def test_pr_check_requires_a_next_line_on_a_parent_that_keeps_other_children(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parented_pr_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72",
        parent_body="## Now\nEpic without a next step.",
        open_children=(
            board.IssueReference(REPOSITORY, WORK_ITEM_ISSUE),
            board.IssueReference(REPOSITORY, 73),
        ),
    )

    assert run_pr_check() == 1
    assert capsys.readouterr().err == (
        f"REFUSED: pull request #12 leaves parent {REPOSITORY}#{PARENT_ISSUE} open with "
        "1 other open child, whose body carries no Next line\n"
    )


def test_pr_check_accepts_a_landing_whose_parent_says_what_comes_next(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parented_pr_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72",
        parent_body=complete_contract("Dispatch slice 4."),
        open_children=(
            board.IssueReference(REPOSITORY, WORK_ITEM_ISSUE),
            board.IssueReference(REPOSITORY, 73),
        ),
    )

    assert run_pr_check() == 0
    assert capsys.readouterr().out == (
        f"PR #12 by ada declares Work-Item: {REPOSITORY}#{WORK_ITEM_ISSUE}\n"
    )


def test_pr_check_refuses_to_close_a_parent_that_keeps_other_children(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parented_pr_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72\nCloses #79",
        parent_body=complete_contract("Dispatch slice 4."),
        open_children=(
            board.IssueReference(REPOSITORY, WORK_ITEM_ISSUE),
            board.IssueReference(REPOSITORY, 73),
        ),
    )

    assert run_pr_check() == 1
    assert capsys.readouterr().err == (
        f"REFUSED: pull request #12 closes {REPOSITORY}#{PARENT_ISSUE} besides its work "
        f"item {REPOSITORY}#{WORK_ITEM_ISSUE}; a pull request lands one item\n"
    )


def test_pr_check_refuses_a_parent_recorded_in_another_repository(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    parented_pr_check_client(
        monkeypatch,
        body="Work-Item: #72\n\nCloses #72",
        parent_body=complete_contract("Cut the next slice."),
        open_children=(),
        parent_repository="other/repo",
    )

    assert run_pr_check() == 1
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
    ledger = board_issue(LEDGER_ISSUE, "Claim ledger", "")
    merged = board.PullRequest(
        140,
        "Lands the slice",
        "Work-Item: #90\n\nCloses #90",
        "branch",
        "2026-08-20T00:00:00Z",
    )
    ledger_pull_request = board.PullRequest(
        141,
        "Ledger housekeeping",
        f"Work-Item: #{LEDGER_ISSUE}\n\nCloses #{LEDGER_ISSUE}",
        "branch",
        "2026-08-20T00:00:00Z",
    )

    projected = projected_board(
        (landed, ledger),
        (),
        (merged, ledger_pull_request),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert [item.number for item in projected.recovery] == [90]
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


def test_pr_check_accepts_a_body_naming_work_github_does_not_close_on(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """`Implements #80` retires nothing on GitHub, so it is no closing reference."""
    pr_check_client(
        monkeypatch,
        landing_pull_request(body="Work-Item: #72\n\nCloses #72\n\nImplements #80"),
    )

    assert run_pr_check() == 0
    assert capsys.readouterr().out == (
        f"PR #12 by ada declares Work-Item: {REPOSITORY}#{WORK_ITEM_ISSUE}\n"
    )


def test_a_non_ascii_digit_in_a_hash_reference_is_not_an_issue_number() -> None:
    arabic_three = board.parse_contract("## Blocked by\n#٣")
    mixed = board.parse_contract("## Blocked by\n#1٣")

    assert arabic_three.blocker_issues == frozenset()
    assert mixed.blocker_issues == frozenset()

    qualified = board.closing_references(f"Closes {REPOSITORY}#٣", REPOSITORY)
    work_item = board.parse_pull_request_classification("Work-Item: #٣", REPOSITORY)
    assert qualified == frozenset()
    assert isinstance(work_item, board.ClassificationDefect)


def test_a_nested_quoted_frozen_line_still_parses_and_an_indented_line_does_not() -> None:
    quoted = "> > **Eingefroren bis:** 2026-09-30 (Operator, 30.09.2026)"
    indented = "    **Eingefroren bis:** 2026-09-30 (Operator, 30.09.2026)"

    assert board.frozen_trigger(quoted) == "2026-09-30"
    assert board.frozen_trigger(indented) is None


def test_a_frozen_line_indented_by_three_spaces_parses_like_an_unindented_one() -> None:
    unindented = "**Eingefroren bis:** 2026-09-30 (Operator, 30.09.2026)"
    indented = "   **Eingefroren bis:** 2026-09-30 (Operator, 30.09.2026)"

    assert board.frozen_trigger(unindented) == "2026-09-30"
    assert board.frozen_trigger(indented) == board.frozen_trigger(unindented)


def test_a_frozen_line_accepts_three_spaces_around_quote_markers_but_not_four() -> None:
    three_before_first = "   > **Eingefroren bis:** 2026-09-30 (Operator, 30.09.2026)"
    three_between = ">   > **Eingefroren bis:** 2026-09-30 (Operator, 30.09.2026)"
    four_between = ">    > **Eingefroren bis:** 2026-09-30 (Operator, 30.09.2026)"

    assert board.frozen_trigger(three_before_first) == "2026-09-30"
    assert board.frozen_trigger(three_between) == "2026-09-30"
    assert board.frozen_trigger(four_between) is None
