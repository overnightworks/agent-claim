"""Direct `protocol.py` behavior: body/slice claim identities, scope and
branch validation, claim conflict/overlap, and the wide-scope trip rule.
Tests that drive these through `issue_claim.main([...])` stay in
`tests/test_cli.py` as CLI-wiring behavior; `protocol.apply` and its TOML
codecs are `tests/test_store.py`'s own (the store's pure counterpart)."""

from __future__ import annotations

import pytest
from board_fixtures import request

from agent_coordination import protocol
from agent_coordination.protocol import (
    ClaimError,
    ClaimRequest,
    ClaimUnavailableError,
    InvalidClaimMarkerError,
    claims_conflict,
)


@pytest.mark.parametrize("bad_issue", [0, -1, True])
def test_issue_identity_requires_a_positive_integer(bad_issue: int) -> None:
    with pytest.raises(ClaimError, match="issue identity must be a positive integer"):
        protocol.IssueIdentity(bad_issue)


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
