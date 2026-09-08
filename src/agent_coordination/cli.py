"""Coordinate coding-agent claims through this repository's own state ref."""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

from . import __version__, board, checkout, forge, github, protocol, store

ACO_AGENT_ENV = checkout.ACO_AGENT_ENV
CLAUDE_SESSION_ID_ENV = checkout.CLAUDE_SESSION_ID_ENV
GROK_SESSION_ID_ENV = checkout.GROK_SESSION_ID_ENV
ClaimError = protocol.ClaimError
ClaimRequest = protocol.ClaimRequest
ClaimUnavailableError = protocol.ClaimUnavailableError
InvalidClaimMarkerError = protocol.InvalidClaimMarkerError
IssueIdentity = protocol.IssueIdentity
LaneIdentity = protocol.LaneIdentity
ISSUELESS_LANE_BRANCH_PREFIXES = protocol.ISSUELESS_LANE_BRANCH_PREFIXES
_git_output = checkout._git_output
_resolved_agent = checkout._resolved_agent
_timestamp = board._timestamp
_validate_checkout = checkout._validate_checkout
claims_conflict = protocol.claims_conflict
claims_holding_path = protocol.claims_holding_path

DEFAULT_CLAIM_ROLE = "builder"
NEXT_PULL_DESCRIPTION = (
    "Pulling is not dispatching: an item whose expectations are still unruled is "
    "named here with refining as its first step, while dispatching a builder onto "
    "it waits for the operator's ruling."
)
CLAIM_DESCRIPTION = (
    "Refuses before the first edit unless the checkout is a linked, isolated "
    "worktree on a non-main branch, the tree is clean, and every --scope value is "
    "a repository-relative path."
)
WHOLE_HELP = (
    "one sentence why this wide scope does not split; required for more than "
    "three paths, any directory, or, once the repository has at least twelve "
    "versioned files, more than a quarter of them"
)


def _resolved_identity(issue: int | None, branch: str) -> protocol.ClaimIdentity:
    """Resolve the CLI's discriminated identity: an explicit issue, or a lane.

    Omitting the positional issue number means lane mode, derived from `branch`
    (the same checkout branch `--base`/`--branch` auto-fill and the release
    branch-matching fallback already use). Lane mode is refused outright unless
    `branch` follows the issueless-lane convention, so a builder who simply forgot
    the issue number never gets a silent, unlabeled, non-projected lane claim.
    """
    if issue is not None:
        return protocol.IssueIdentity(issue)
    if not branch.startswith(protocol.ISSUELESS_LANE_BRANCH_PREFIXES):
        prefixes = " or ".join(repr(prefix) for prefix in protocol.ISSUELESS_LANE_BRANCH_PREFIXES)
        raise protocol.ClaimError(
            f"branch {branch!r} is not an issueless lane; pass an issue number, or "
            f"check out a branch prefixed {prefixes}"
        )
    return protocol.LaneIdentity()


def _claim_subject(claim: protocol.ScopedClaim) -> str:
    return (
        f"lane {claim.branch}"
        if isinstance(claim.identity, protocol.LaneIdentity)
        else f"issue #{claim.identity.issue}"
    )


def _claim_age_fields(opened_at: datetime, now: datetime) -> tuple[str, bool]:
    """The rendered age and old-ness of a claim opened at `opened_at` (a
    commit's committer date -- issue #176, §1 -- not a ledger comment
    timestamp)."""
    age = now.astimezone(UTC) - opened_at.astimezone(UTC)
    return board.format_claim_age(age), board.claim_is_old(age)


def _claim_age_suffix(opened_at: datetime, now: datetime) -> str:
    rendered, old = _claim_age_fields(opened_at, now)
    return f" {rendered} old" if old else f" {rendered}"


def _scope_cost(versioned: tuple[str, ...], scope: tuple[str, ...]) -> tuple[int, int, float]:
    n = len(checkout.paths_under_scope(versioned, scope))
    total = len(versioned)
    share = 0.0 if total == 0 else n / total
    return n, total, share


def _optional_whole_reason(arguments: argparse.Namespace) -> str | None:
    raw = getattr(arguments, "whole", None)
    if raw is None:
        return None
    return protocol._outbound_text(raw, "whole reason", maximum=512)


def _wide_scope_condition(trip: protocol.WideScopeTrip) -> str:
    """The tripped condition in words, with the numbers it was judged
    against -- what the refusal names instead of restating the whole rule."""
    if trip.reason is protocol.WideScopeReason.PATH_COUNT:
        return f"{trip.path_count} paths exceeds three"
    if trip.reason is protocol.WideScopeReason.DIRECTORY:
        noun = "directory" if len(trip.directories) == 1 else "directories"
        return f"{len(trip.directories)} {noun} in scope ({', '.join(trip.directories)})"
    covered, total = trip.covered_file_count, trip.versioned_file_count
    percent = round(100 * covered / total)
    path_word = "path" if covered == 1 else "paths"
    return f"{covered} {path_word} of {total} versioned files ({percent} %) exceeds a quarter"


def _wide_scope_refusal(trip: protocol.WideScopeTrip) -> str:
    return f"scope is wide: {_wide_scope_condition(trip)}; pass --whole REASON"


def _reject_wide_scope(
    scope: tuple[str, ...],
    versioned: tuple[str, ...],
    whole_reason: str | None,
) -> tuple[int, int, float]:
    n, total, share = _scope_cost(versioned, scope)
    directories = checkout._scope_directories(scope)
    trip = protocol.wide_scope_trip(
        scope, directories=directories, covered_file_count=n, versioned_file_count=total
    )
    if trip is not None and whole_reason is None:
        raise protocol.ClaimError(_wide_scope_refusal(trip))
    return n, total, share


def _reject_ungrounded_comma_scope(
    scope: tuple[str, ...], versioned: tuple[str, ...], *, flag: str
) -> None:
    """Refuse a scope entry that contains a comma and matches no versioned
    file -- the shape `{flag} a.py,b.py` takes when passed as one flag from
    habit instead of one path per flag (issue #207). Stored verbatim, that
    single entry names a path nothing tracks, so the claim protects nothing:
    the lane's real files stay uncovered and no overlap check can ever fire
    for them.

    Never call this over `--drop`: a value the live claim already holds is a
    fact about the claim, not a typo about the checkout, and dropping a value
    the claim does not hold is already refused by `_combined_scope` with a
    truer sentence naming the claim rather than the checkout -- adding this
    check there would either block the very repair this refusal exists to
    leave open (dropping an already-claimed ungrounded value), or never fire
    at all (a not-yet-dropped value the claim lacks is refused first).

    Splitting on the comma would be the old, wrong fix (issue #201): it made
    a real comma-bearing filename unrepresentable. So a real comma-bearing
    path that names a versioned file, or a directory holding one, still
    passes here -- `paths_under_scope` matches either. So does a comma-free
    path that does not exist yet, since a lane routinely claims files it is
    about to create; only a comma with no match among versioned files is the
    signature this refuses.
    """
    for entry in scope:
        if "," in entry and not checkout.paths_under_scope(versioned, (entry,)):
            raise protocol.ClaimError(
                f"{entry!r} matches no versioned file; one {flag} path per flag, so its comma "
                f"is read literally -- repeat {flag} for a second path"
            )


def _touch_json(claim: protocol.ScopedClaim) -> dict[str, object]:
    return {
        **_identity_json(claim.identity),
        "claim_id": claim.claim_id,
        "agent": claim.agent,
        "scope": list(claim.scope),
    }


def _touch_line(own_scope: tuple[str, ...], claim: protocol.ScopedClaim) -> str:
    """One overlapping claim, named with the paths where its scope meets
    `own_scope` -- the fact a claimant needs to know they hold both scopes
    at once, not only the other item's name (issue #206)."""
    meeting = protocol.scope_overlap_paths(own_scope, claim.scope)
    return f"{_claim_subject(claim)} on {protocol.named_with_overflow_count(meeting)}"


def _touch_summary(own_scope: tuple[str, ...], touches: tuple[protocol.ScopedClaim, ...]) -> str:
    if not touches:
        return "overlaps no other open claims"
    return "overlaps " + ", ".join(_touch_line(own_scope, claim) for claim in touches)


def _claim_cost_line(
    n: int, total: int, own_scope: tuple[str, ...], touches: tuple[protocol.ScopedClaim, ...]
) -> str:
    percent = 0 if total == 0 else round(100 * n / total)
    return f"{n} of {total} versioned files ({percent}%); {_touch_summary(own_scope, touches)}"


def _request(arguments: argparse.Namespace) -> protocol.ClaimRequest:
    agent = protocol._outbound_text(checkout._resolved_agent(arguments.agent), "agent", maximum=128)
    role = protocol._outbound_text(arguments.role, "role", maximum=64)
    base = checkout._git_output(["rev-parse", "HEAD"]) if arguments.base is None else arguments.base
    if protocol.COMMIT_PATTERN.fullmatch(base) is None:
        raise protocol.ClaimError("base must be a full lowercase commit SHA")
    if arguments.branch is None:
        branch = checkout._git_output(["branch", "--show-current"])
    else:
        branch = arguments.branch
    branch = protocol._valid_branch({"branch": branch})
    issue = _optional_issue_number(arguments.issue)
    identity = _resolved_identity(issue, branch)
    claim_id = arguments.claim_id or uuid.uuid4().hex
    protocol.ClaimId(claim_id)
    whole_reason = _optional_whole_reason(arguments)
    resource = getattr(arguments, "resource", None)
    if resource is not None:
        resource = protocol._outbound_resource_name(resource)
    request = protocol.ClaimRequest(
        identity=identity,
        agent=agent,
        role=role,
        base=base,
        branch=branch,
        scope=protocol._valid_scope(arguments.scope),
        claim_id=claim_id,
        out_of_order_reason=arguments.out_of_order,
        whole_reason=whole_reason,
        resource=resource,
    )
    checkout._validate_checkout(request)
    return request


LANE_ISSUE_HELP = "omit for lane mode, derived from a docs/ or fix/ checkout branch"
JSON_HELP = "print the result as JSON instead of the human lines"
AGENT_HELP = (
    "the acting agent's name; filled from a non-empty ACO_AGENT, GROK_SESSION_ID or "
    "CLAUDE_SESSION_ID when omitted"
)
EXPECTED_CLAIM_ID_HELP = (
    "assert which claim you are acting on; the issue number or lane branch selects it, and "
    "a differing id is refused rather than redirected"
)
ROLE_ON_LIVE_CLAIM_HELP = (
    "the acting role; the selected claim's own role when omitted, and required to be "
    "coordinator with --coordinator-override"
)


def _add_bootstrap_parser(commands: argparse._SubParsersAction) -> None:
    commands.add_parser("bootstrap", help="create refs/aco/state if it does not exist yet")


def _add_status_parser(commands: argparse._SubParsersAction) -> None:
    status = commands.add_parser("status", help="show repository-wide build claims")
    status.add_argument(
        "issue", type=int, nargs="?", help="show only this issue's claims and the ones they overlap"
    )
    status.add_argument(
        "--path", metavar="PATH", help="list holders of this path instead of by issue"
    )
    status.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_board_parser(commands: argparse._SubParsersAction) -> None:
    board_command = commands.add_parser("board", help="project the open work board without writes")
    board_command.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_rulings_parser(commands: argparse._SubParsersAction) -> None:
    rulings_command = commands.add_parser(
        "rulings", help="list open expectation lines without writes"
    )
    rulings_command.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_next_parser(commands: argparse._SubParsersAction) -> None:
    next_command = commands.add_parser(
        "next",
        help="name the board's top-priority item to pull",
        description=NEXT_PULL_DESCRIPTION,
    )
    next_command.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_claim_parser(commands: argparse._SubParsersAction) -> None:
    claim = commands.add_parser(
        "claim",
        help="claim an issue and scope before editing",
        description=CLAIM_DESCRIPTION,
    )
    claim.add_argument(
        "issue",
        type=int,
        nargs="?",
        help=LANE_ISSUE_HELP,
    )
    claim.add_argument("--agent", help=AGENT_HELP)
    claim.add_argument(
        "--role",
        default=DEFAULT_CLAIM_ROLE,
        help=f"the claiming role; default {DEFAULT_CLAIM_ROLE}",
    )
    claim.add_argument(
        "--base",
        help="the full commit SHA this lane starts from; the current HEAD when omitted",
    )
    claim.add_argument(
        "--branch", help="the lane's branch; the current checkout branch when omitted"
    )
    claim.add_argument(
        "--scope",
        action="append",
        required=True,
        help="a repository-relative path; repeat --scope for more than one path",
    )
    claim.add_argument(
        "--claim-id",
        help=(
            "this claim's own id; generated when omitted, and repeating an identical claim "
            "with it returns the active claim instead of writing a second one"
        ),
    )
    claim.add_argument(
        "--out-of-order",
        metavar="REASON",
        help=(
            "refuses a claim without a reason when a higher-priority actionable item is "
            "free or an open blocker remains; records why"
        ),
    )
    claim.add_argument(
        "--whole",
        metavar="REASON",
        help=WHOLE_HELP,
    )
    claim.add_argument(
        "--resource",
        metavar="NAME",
        help="allocate the next free value of this named scarce resource and hold it",
    )
    claim.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_release_parser(commands: argparse._SubParsersAction) -> None:
    release = commands.add_parser("release", help="release a landed or abandoned claim")
    release.add_argument(
        "issue",
        type=int,
        nargs="?",
        help=LANE_ISSUE_HELP,
    )
    release.add_argument("--agent", help=AGENT_HELP)
    release.add_argument("--role", help=ROLE_ON_LIVE_CLAIM_HELP)
    outcome = release.add_mutually_exclusive_group(required=True)
    outcome.add_argument(
        "--merged",
        type=int,
        metavar="PULL_REQUEST",
        help="the pull request that landed this claim's item on the default branch",
    )
    outcome.add_argument(
        "--abandoned",
        metavar="REASON",
        help="why this claim ends without a landing",
    )
    release.add_argument("--claim-id", help=EXPECTED_CLAIM_ID_HELP)
    release.add_argument(
        "--coordinator-override",
        action="store_true",
        help="release another agent's claim as the coordinator; requires --role coordinator",
    )
    release.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_rescope_parser(commands: argparse._SubParsersAction) -> None:
    rescope = commands.add_parser(
        "rescope", help="add or drop paths on a live claim without releasing"
    )
    rescope.add_argument(
        "issue",
        type=int,
        nargs="?",
        help=LANE_ISSUE_HELP,
    )
    rescope.add_argument("--agent", help=AGENT_HELP)
    rescope.add_argument(
        "--add",
        action="append",
        help="a repository-relative path to add; repeat --add for more than one path",
    )
    rescope.add_argument(
        "--drop",
        action="append",
        help="a repository-relative path to drop; repeat --drop for more than one path",
    )
    rescope.add_argument("--claim-id", help=EXPECTED_CLAIM_ID_HELP)
    rescope.add_argument(
        "--whole",
        metavar="REASON",
        help=WHOLE_HELP,
    )
    rescope.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_cut_parser(commands: argparse._SubParsersAction) -> None:
    cut = commands.add_parser("cut", help="create a container's next slice as a fresh child issue")
    cut.add_argument("issue", type=int, help="the container to cut")
    cut.add_argument("--title", required=True, help="the fresh child issue's title")
    cut.add_argument(
        "--row",
        type=int,
        metavar="N",
        help="the slice table's # column value to cut; default is the first cuttable row",
    )
    cut.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_check_parser(commands: argparse._SubParsersAction) -> None:
    check = commands.add_parser(
        "check",
        help=(
            "read one number -- a pull request's work-item classification, or an "
            "issue's body contract; claims, labels and writes nothing"
        ),
    )
    check.add_argument(
        "number",
        type=int,
        help="the pull request or issue to read; the forge says which one it is",
    )
    check.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_protect_parser(commands: argparse._SubParsersAction) -> None:
    commands.add_parser("protect", help="deny PreToolUse writes without this session's live claim")


_SUBPARSER_BUILDERS: tuple[Callable[[argparse._SubParsersAction], None], ...] = (
    _add_bootstrap_parser,
    _add_status_parser,
    _add_board_parser,
    _add_rulings_parser,
    _add_next_parser,
    _add_claim_parser,
    _add_release_parser,
    _add_rescope_parser,
    _add_cut_parser,
    _add_check_parser,
    _add_protect_parser,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aco", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--repo", help="GitHub repository as OWNER/REPO")
    commands = parser.add_subparsers(dest="command", required=True)
    for add_subparser in _SUBPARSER_BUILDERS:
        add_subparser(commands)
    return parser


def _identity_json(identity: protocol.ClaimIdentity) -> dict[str, object]:
    """`issue`/`lane` pair for one claim's discriminated identity, for JSON output.

    A lane claim's name lives in the sibling `branch` field of the same JSON
    object, so `lane` stays a bare marker instead of duplicating it.
    """
    if isinstance(identity, protocol.LaneIdentity):
        return {"issue": None, "lane": True}
    return {"issue": identity.issue, "lane": None}


def _status_claims(
    claims: tuple[protocol.ActiveClaim, ...], issue: int | None
) -> tuple[tuple[protocol.ActiveClaim, ...], protocol.ClaimConflictIndex]:
    selected = tuple(
        claim
        for claim in claims
        if issue is None
        or (isinstance(claim.identity, protocol.IssueIdentity) and claim.identity.issue == issue)
    )
    index = protocol._claim_conflict_index(claims)
    if not selected:
        return (), index
    related_ids = (
        {claim.claim_id for claim in claims}
        if issue is None
        else protocol._related_claim_ids(index, selected)
    )
    related = tuple(claim for claim in claims if claim.claim_id in related_ids)
    return related, index


def _resource_fields(resource: protocol.ResourceHold | None) -> dict[str, object]:
    if resource is None:
        return {"resource": None, "resource_value": None}
    return {"resource": resource.name, "resource_value": resource.value}


def _overlap_subjects(
    claims_by_id: Mapping[str, protocol.ActiveClaim], peer_ids: set[str]
) -> list[dict[str, object]]:
    return [
        {
            **_identity_json(peer.identity),
            "claim_id": peer.claim_id,
            "agent": peer.agent,
        }
        for claim_id in sorted(peer_ids)
        if (peer := claims_by_id.get(claim_id)) is not None
    ]


def _overlap_note(
    claims_by_id: Mapping[str, protocol.ActiveClaim], peer_ids: set[str]
) -> str | None:
    peers = [claims_by_id[claim_id] for claim_id in sorted(peer_ids) if claim_id in claims_by_id]
    if not peers:
        return None
    return "overlaps " + ", ".join(f"{_claim_subject(claim)} ({claim.claim_id})" for claim in peers)


def _print_claim_status_lines(
    claim: protocol.ActiveClaim,
    claims_by_id: Mapping[str, protocol.ActiveClaim],
    index: protocol.ClaimConflictIndex,
    opened_at: datetime,
    observed_at: datetime,
) -> None:
    state = "CONFLICT" if claim.claim_id in index.conflict_ids else "CLAIMED"
    print(
        f"{state} {_claim_subject(claim)}: {claim.agent} ({claim.role}) "
        f"base={claim.base} branch={claim.branch} claim={claim.claim_id}"
        f"{_claim_age_suffix(opened_at, observed_at)}"
    )
    for path in claim.scope:
        print(f"  {path}")
    if claim.resource is not None:
        print(f"  resource {claim.resource.name}={claim.resource.value}")
    if claim.whole_reason is not None:
        print(f"  whole: {claim.whole_reason}")
    note = _overlap_note(claims_by_id, protocol._overlap_peer_ids(index, claim))
    if note is not None:
        print(f"  {note}")


def _print_related_claims(
    claims: tuple[protocol.ActiveClaim, ...],
    related: tuple[protocol.ActiveClaim, ...],
    index: protocol.ClaimConflictIndex,
    ages: Mapping[str, datetime],
    observed_at: datetime,
) -> int:
    claims_by_id: dict[str, protocol.ActiveClaim] = {claim.claim_id: claim for claim in claims}
    for claim in related:
        _print_claim_status_lines(claim, claims_by_id, index, ages[claim.claim_id], observed_at)
    return 2 if any(claim.claim_id in index.conflict_ids for claim in related) else 0


def _status(
    claims: tuple[protocol.ActiveClaim, ...],
    issue: int | None,
    ages: Mapping[str, datetime],
    now: datetime | None = None,
) -> int:
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    related, index = _status_claims(claims, issue)
    if related:
        return _print_related_claims(claims, related, index, ages, observed_at)
    subject = "repository" if issue is None else f"issue #{issue}"
    print(f"UNCLAIMED {subject}")
    return 0


def _status_json(
    claims: tuple[protocol.ActiveClaim, ...],
    issue: int | None,
    ages: Mapping[str, datetime],
    now: datetime | None = None,
) -> int:
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    related, index = _status_claims(claims, issue)
    if not related:
        state = "UNCLAIMED"
    elif any(claim.claim_id in index.conflict_ids for claim in related):
        state = "CONFLICT"
    else:
        state = "CLAIMED"
    claims_by_id: dict[str, protocol.ActiveClaim] = {claim.claim_id: claim for claim in claims}
    payload = {
        "issue": issue,
        "state": state,
        "claims": [
            {
                **_identity_json(claim.identity),
                "agent": claim.agent,
                "role": claim.role,
                "base": claim.base,
                "branch": claim.branch,
                "claim_id": claim.claim_id,
                "scope": list(claim.scope),
                **_resource_fields(claim.resource),
                **({"whole": claim.whole_reason} if claim.whole_reason is not None else {}),
                "overlaps": _overlap_subjects(
                    claims_by_id, protocol._overlap_peer_ids(index, claim)
                ),
                "state": "CONFLICT" if claim.claim_id in index.conflict_ids else "CLAIMED",
                "age": _claim_age_fields(ages[claim.claim_id], observed_at)[0],
                "old": _claim_age_fields(ages[claim.claim_id], observed_at)[1],
            }
            for claim in related
        ],
    }
    print(json.dumps(payload))
    return 2 if state == "CONFLICT" else 0


def _status_path(claims: tuple[protocol.ActiveClaim, ...], path: str) -> None:
    holders = protocol.claims_holding_path(claims, path)
    if not holders:
        print(f"UNCLAIMED {path}")
        return
    for claim in holders:
        print(
            f"CLAIMED {path} {_claim_subject(claim)}: {claim.agent} ({claim.role}) "
            f"claim={claim.claim_id}"
        )
        if claim.whole_reason is not None:
            print(f"  whole: {claim.whole_reason}")
    if len(holders) > 1:
        print(
            "overlap: "
            + ", ".join(f"{_claim_subject(claim)} ({claim.claim_id})" for claim in holders)
        )


def _status_path_json(claims: tuple[protocol.ActiveClaim, ...], path: str) -> int:
    holders = protocol.claims_holding_path(claims, path)
    state = "UNCLAIMED" if not holders else "CLAIMED"
    payload = {
        "path": path,
        "state": state,
        "claims": [
            {
                **_identity_json(claim.identity),
                "agent": claim.agent,
                "role": claim.role,
                "base": claim.base,
                "branch": claim.branch,
                "claim_id": claim.claim_id,
                "scope": list(claim.scope),
                **_resource_fields(claim.resource),
                **({"whole": claim.whole_reason} if claim.whole_reason is not None else {}),
                "state": "CLAIMED",
            }
            for claim in holders
        ],
    }
    print(json.dumps(payload))
    return 0


def _rescope_json(claimed: protocol.ActiveClaim) -> None:
    print(
        json.dumps(
            {
                **_identity_json(claimed.identity),
                "claim_id": claimed.claim_id,
                "agent": claimed.agent,
                "role": claimed.role,
                "base": claimed.base,
                "branch": claimed.branch,
                "scope": list(claimed.scope),
            }
        )
    )


@dataclass(frozen=True)
class ScopeVersioning:
    """How much of the claimed scope's Git history the checkout already has,
    for the `--json` claim payload's `versioned_files`/`share` fields."""

    versioned_files: int
    versioned_files_total: int
    share: float


def _claim_json(
    claimed: protocol.ActiveClaim,
    *,
    versioning: ScopeVersioning,
    touches: tuple[protocol.ActiveClaim, ...],
    checks: tuple[SliceCheck, ...],
) -> int:
    print(
        json.dumps(
            {
                **_identity_json(claimed.identity),
                "claim_id": claimed.claim_id,
                "agent": claimed.agent,
                "role": claimed.role,
                "base": claimed.base,
                "branch": claimed.branch,
                "scope": list(claimed.scope),
                **_resource_fields(claimed.resource),
                "versioned_files": versioning.versioned_files,
                "versioned_files_total": versioning.versioned_files_total,
                "share": versioning.share,
                "touches": [_touch_json(claim) for claim in touches],
                "checks": [check.as_json() for check in checks],
            }
        )
    )
    return 0


def _release_json(
    released: protocol.ActiveClaim,
    agent: str,
    role: str | None,
    outcome: protocol.ReleaseOutcome,
) -> None:
    print(
        json.dumps(
            {
                **_identity_json(released.identity),
                "branch": released.branch,
                "claim_id": released.claim_id,
                "agent": agent,
                "role": role if role is not None else released.role,
                "reason": outcome.reason,
            }
        )
    )


def _merged_pull_request_floor(issues: tuple[board.Issue, ...], now: datetime) -> datetime:
    """The earliest merge that could still matter to a currently open issue.

    A pull request can only touch or close an issue that already exists, so
    nothing merged before the oldest still-open issue was filed can ever
    change any open item's stage. Anchoring the query here — instead of an
    arbitrary fixed window — is what lets a slice's "Refs #N"/"Part of #N"
    landing keep crediting its still-open epic for as long as the epic stays
    open, rather than for a fixed number of days after which the credit
    silently reverts. Residual: the underlying query is still capped (see
    `GitHubForge.list_recent_merged_board_pull_requests`), so an epic
    old enough to have more merges than that cap between its filing and now
    can still lose credit for an early slice; this floor removes the
    fortnight-sized version of that gap, not every version of it.
    """
    if not issues:
        return now
    return min(_timestamp(issue.created_at) for issue in issues)


# A container's children are their own `gh list_children` subprocess call;
# an unbounded pool would spawn one worker per container on a large board.
# This caps that fan-out -- a stable invariant of this executor, not
# something an operator tunes. `_fetch_children` gives it a dedicated
# executor sized to exactly this constant, so the cap holds regardless of
# whether the three base board reads below have already finished.
BOARD_CHILD_FETCH_CONCURRENCY = 4


def _fetch_children(
    client: forge.BoardSource, container_numbers: tuple[int, ...]
) -> dict[int, tuple[board.ChildItem, ...]]:
    """Every container's children, at most `BOARD_CHILD_FETCH_CONCURRENCY`
    `gh` subprocesses at a time; excess containers queue behind it."""
    if not container_numbers:
        return {}
    workers = min(len(container_numbers), BOARD_CHILD_FETCH_CONCURRENCY)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            number: pool.submit(client.list_children, number) for number in container_numbers
        }
        return {number: future.result() for number, future in futures.items()}


def _fetch_dependencies(
    client: forge.BoardSource, issue_numbers: tuple[int, ...]
) -> dict[int, tuple[board.IssueDependency, ...]]:
    """Every named issue's `blocked_by` dependencies (#150), at most
    `BOARD_CHILD_FETCH_CONCURRENCY` `gh` subprocesses at a time -- run only
    after `_board`'s base reads and children wave have already finished, so
    peak concurrent `gh` subprocesses never exceeds today's 3+4."""
    if not issue_numbers:
        return {}
    workers = min(len(issue_numbers), BOARD_CHILD_FETCH_CONCURRENCY)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            number: pool.submit(client.list_board_dependencies, number) for number in issue_numbers
        }
        return {number: future.result() for number, future in futures.items()}


def _validated_dependencies(
    issues: tuple[board.Issue, ...], fetched: dict[int, tuple[board.IssueDependency, ...]]
) -> dict[int, tuple[board.IssueDependency, ...]]:
    """`fetched`, keyed by exactly the positive-`blocked_by_count` issues,
    each list checked against its own listing count (#150 §6): a length
    mismatch or a duplicated dependency is the same class of malformed
    forge response as a disagreeing container summary -- named loud rather
    than guessed through."""
    validated: dict[int, tuple[board.IssueDependency, ...]] = {}
    for issue in issues:
        if issue.blocked_by_count <= 0:
            continue
        dependencies = fetched.get(issue.number, ())
        distinct = {dependency.reference for dependency in dependencies}
        length = len(dependencies)
        if length != issue.blocked_by_count or len(distinct) != length:
            raise forge.ForgeMalformedResponseError(
                f"GitHub returned a malformed board blocked-by list for #{issue.number}: "
                f"listing total_blocked_by={issue.blocked_by_count}, detail length={length}"
            )
        validated[issue.number] = dependencies
    return validated


def _resolve_toplevel() -> Path:
    """The checkout's toplevel, for every command that resolves the
    repository's board configuration (#150) -- its priority ladder, its idea
    label, its canonical remote, and the body pin it is still checked
    against. Without a working tree there is no configuration to read, so
    the command refuses rather than running on guessed defaults (#178)."""
    try:
        return Path(checkout._git_output(["rev-parse", "--show-toplevel"]))
    except protocol.ClaimError as error:
        raise protocol.ClaimUnavailableError(
            "this command reads the repository's body contract from "
            ".agent-claim/board.toml and needs a checkout (a shallow one is "
            f"enough): {error}"
        ) from error


def _load_board_config(client: forge.BoardSource, toplevel: Path) -> board.BoardConfig:
    """The repository's board configuration, validated against what `client`
    can actually do (#150 §3): reading a body's dependencies requires
    `list_board_dependencies` at read-only or better, and the typed block is
    the one body grammar, so every repository needs it."""
    config = board.load_config(toplevel / board.CONFIG_PATH)
    if (
        client.capability(forge.ForgeOperation.LIST_BOARD_DEPENDENCIES)
        is forge.Capability.UNSUPPORTED
    ):
        raise protocol.ClaimError(
            "reading work-item bodies requires forge operation list_board_dependencies"
        )
    return config


def _board(
    client: forge.BoardSource,
    claims: tuple[protocol.ActiveClaim, ...],
    *,
    issues: tuple[board.Issue, ...] | None = None,
    claim_ages: Mapping[str, datetime] | None = None,
) -> board.Board:
    now = datetime.now(UTC)
    config = _load_board_config(client, _resolve_toplevel())
    if issues is None:
        issues = client.list_open_board_issues()
    since = _merged_pull_request_floor(issues, now)
    # A container whose own summary already says 0 (or carries no summary at
    # all, `children_total is None`) can never own an open child either way:
    # `_container_progress` returns no progress at all without both numbers,
    # and returns an empty open-children list when they're both 0 -- exactly
    # what an absent `children` entry already defaults to. Fetching its
    # detail list would cost a request `board` never needed (issue #168).
    container_numbers = tuple(
        issue.number
        for issue in issues
        if issue.kind is board.ItemKind.CONTAINER and issue.children_total
    )
    # Open and recently-merged pull requests and each container's children
    # are independent reads once `since` is known, so fetching them on
    # separate threads instead of one after another overlaps their `gh`
    # subprocess wait time. Children get their own executor
    # (`_fetch_children`) so their concurrency stays capped at
    # `BOARD_CHILD_FETCH_CONCURRENCY` even once these two base reads finish
    # and free their own pool's workers. The dependency wave runs afterward
    # instead (below), so peak concurrent `gh` subprocesses stays at 2+4,
    # then at most 4.
    with ThreadPoolExecutor(max_workers=2) as pool:
        open_pull_requests = pool.submit(client.list_open_board_pull_requests)
        merged_pull_requests = pool.submit(client.list_recent_merged_board_pull_requests, since)
        children = _fetch_children(client, container_numbers)
        pull_requests = (open_pull_requests.result(), merged_pull_requests.result())
    dependencies = _validated_dependencies(
        issues,
        _fetch_dependencies(
            client, tuple(issue.number for issue in issues if issue.blocked_by_count > 0)
        ),
    )
    return board.build_board(
        board.BoardBuildInputs(
            issues=issues,
            open_pull_requests=pull_requests[0],
            recent_merged_pull_requests=pull_requests[1],
            claims=claims,
            config=config,
            repository=client.repository.path,
            now=now,
            trunk_landings=checkout.trunk_landing_times(),
            children=children,
            dependencies=dependencies,
            requests=client.requests,
            claim_ages=claim_ages or {},
        )
    )


def _rulings(projected: board.Board, *, as_json: bool) -> None:
    items = tuple(
        sorted(
            (
                (item, item.expectation_progress)
                for item in projected.items
                if item.expectation_progress.open > 0
            ),
            key=lambda entry: (
                *board.board_rank(entry[0])[:2],
                entry[1].open,
                entry[0].number,
            ),
        )
    )
    if as_json:
        print(
            json.dumps(
                [
                    {
                        "number": item.number,
                        "title": item.title,
                        "open": progress.open,
                        "total": progress.total,
                    }
                    for item, progress in items
                ]
            )
        )
        return
    if not items:
        print("No open expectation lines.")
        return
    print(
        "\n".join(
            f"#{item.number} {progress.open}/{progress.total}: {item.title}"
            for item, progress in items
        )
    )


def _ruling_pull_hint(item: board.BoardItem) -> str | None:
    if item.expectation_state is board.ExpectationState.PROPOSED:
        return "Erwartungen ungeregelt, beim Ziehen zuerst refinen"
    if not item.ruling_old:
        return None
    return f"vor {item.ruling_landings} Landungen geregelt, beim Ziehen neu refinen"


def _next_action_command(action: board.WorkItemAction | board.CutSliceAction) -> str:
    """The exact `aco` invocation `_next` prints and `_next --json` carries
    as `command` -- one owner so text and JSON never name a different
    command for the same action. `close_container` has none: there is no
    command to run, and neither grammar invents one."""
    if isinstance(action, board.WorkItemAction):
        return f"aco claim {action.item.number} --scope <paths>"
    return f'aco cut {action.container.number} --title "{action.cut_title}"'


def _next_action_payload(action: board.NextAction) -> dict[str, object]:
    """The action-specific fields `_next_json` adds beyond `recovery`/`skipped`."""
    if isinstance(action, board.WorkItemAction):
        item = action.item
        payload: dict[str, object] = {
            "action": "work_item",
            "number": item.number,
            "score": item.score,
            "title": item.title,
            "next": item.next_step,
            "command": _next_action_command(action),
            "ruling_landings": item.ruling_landings,
            "ruling_old": item.ruling_old,
        }
        hint = _ruling_pull_hint(item)
        if hint is not None:
            payload["ruling_hint"] = hint
        return payload
    if isinstance(action, board.CutSliceAction):
        return {
            "action": "cut_slice",
            "number": action.container.number,
            "title": action.container.title,
            "slice": action.next_step,
            "cut_title": action.cut_title,
            "command": _next_action_command(action),
        }
    return {
        "action": "close_container",
        "number": action.container.number,
        "closed": action.container_progress.closed,
        "total": action.container_progress.total,
        "next_step": action.next_step,
    }


def _next_json(
    action: board.NextAction | None,
    skipped: tuple[board.BoardItem, ...],
    recovery: tuple[board.BoardItem, ...],
) -> int:
    payload: dict[str, object] = {
        "action": None,
        "recovery": [
            {
                "number": recovery_item.number,
                "title": recovery_item.title,
                "step": board.RECOVERY_STEP,
            }
            for recovery_item in recovery
        ],
        "skipped": [
            {"number": skipped_item.number, "reason": skipped_item.actionable_reason}
            for skipped_item in skipped
        ],
    }
    if action is not None:
        payload.update(_next_action_payload(action))
    print(json.dumps(payload))
    return 0


def _next_action_lines(action: board.NextAction) -> list[str]:
    """The action-specific lines `_next` prints before `SKIPPED`."""
    if isinstance(action, board.WorkItemAction):
        item = action.item
        lines = [
            f"#{item.number} score {item.score}: {item.title}",
            f"Next: {item.next_step}",
            f"Run: {_next_action_command(action)}",
            "<paths> cannot be derived; take the files to claim from the item body.",
        ]
        hint = _ruling_pull_hint(item)
        if hint is not None:
            lines.append(hint)
        return lines
    if isinstance(action, board.CutSliceAction):
        return [
            f"cut_slice #{action.container.number}: {action.next_step}",
            f"Next: {_next_action_command(action)}",
        ]
    if action.next_step is not None:
        return [f"close_container #{action.container.number}: {action.next_step}"]
    progress = action.container_progress
    return [
        f"close_container #{action.container.number}: "
        f"{progress.closed}/{progress.total} children closed, no Next work"
    ]


def _next(
    action: board.NextAction | None,
    skipped: tuple[board.BoardItem, ...],
    recovery: tuple[board.BoardItem, ...],
) -> int:
    """A landed-but-open item is named before anything new is pulled."""
    lines: list[str] = []
    if recovery:
        lines.append("RECOVERY")
        lines.extend(
            f"#{recovery_item.number}: {board.RECOVERY_STEP}" for recovery_item in recovery
        )
        lines.append("")
    lines.extend(_next_action_lines(action) if action is not None else ["No actionable item."])
    if skipped:
        skipped_lines = (
            f"#{skipped_item.number}: {skipped_item.actionable_reason}" for skipped_item in skipped
        )
        lines.extend(("", "SKIPPED", *skipped_lines))
    print("\n".join(lines))
    return 0


def _unworkable(projected: board.Board) -> tuple[board.BoardItem, ...]:
    return tuple(item for item in projected.items if not item.actionable)


@dataclass(frozen=True)
class SliceCheck:
    """One slice-rule finding — the `check` table `#79` rules.

    `slice`/`issue` carry whichever numbers the message names, so a `--json`
    caller can act on the finding without re-parsing `text`; either is
    `None` when the check has nothing of that kind to name.
    """

    level: str
    check: str
    text: str
    slice: int | None = None
    issue: int | None = None

    def render(self) -> str:
        prefix = "ERROR" if self.level == "error" else "WARNING"
        return f"{prefix}: {self.text}"

    def as_json(self) -> dict[str, object]:
        return {
            "level": self.level,
            "check": self.check,
            "text": self.text,
            "slice": self.slice,
            "issue": self.issue,
        }


def _fetch_issue_reference(client: forge.ForgeReader, number: int) -> forge.ItemReference:
    """The live state, title, and body of issue `number` from `client`.

    Called only for a claim target that the already-fetched open board
    didn't resolve as OPEN — a closed or missing issue never appears in
    `list_open_board_issues`, so those two states need their own targeted
    lookup; this is that lookup, kept to one issue at a time rather than a
    repository-wide query.
    """
    return client.item_reference(number)


def _issue_reference_state(
    client: forge.ForgeReader,
    open_by_number: dict[int, board.Issue],
    number: int,
) -> tuple[forge.ItemState, str | None, str | None]:
    open_issue = open_by_number.get(number)
    if open_issue is not None:
        return forge.ItemState.OPEN, open_issue.title, open_issue.body
    reference = _fetch_issue_reference(client, number)
    return reference.state, reference.title, reference.body


def _out_of_order_check(
    projected: board.Board, issue: int | None, out_of_order_reason: str | None
) -> SliceCheck | None:
    highest = board.highest_scored_actionable(projected)
    if highest is None or issue is None:
        return None
    claimed_item = next((item for item in projected.items if item.number == issue), None)
    if claimed_item is None or board.board_rank(highest) >= board.board_rank(claimed_item):
        return None
    return SliceCheck(
        "warning" if out_of_order_reason is not None else "error",
        "out-of-order",
        f"higher-priority actionable item #{highest.number} "
        f"(score {highest.score}) is free: {highest.title}; "
        "use --out-of-order REASON to proceed",
        issue=highest.number,
    )


def _blocked_check(
    item: board.BoardItem | None, out_of_order_reason: str | None, repository: str
) -> SliceCheck | None:
    if item is None or not item.open_blockers:
        return None
    blockers = ", ".join(
        board.open_blocker_label(reference, repository) for reference in item.open_blockers
    )
    return SliceCheck(
        "warning" if out_of_order_reason is not None else "error",
        "blocked",
        f"#{item.number} is blocked by {blockers} (open); "
        "pass --out-of-order REASON to claim it anyway",
        issue=item.number,
    )


def _parent_checks(
    client: forge.ForgeReader, repository: str, issue: int, title: str
) -> SliceCheck | None:
    """Warn when a slice-shaped title names a parent GitHub does not record as one."""
    match = board.slice_title_match(title)
    if match is None:
        return None
    slice_number, parent_issue = match
    parent = client.parent_issue(issue)
    if parent is not None and parent.reference == board.IssueReference(repository, parent_issue):
        return None
    return SliceCheck(
        "warning",
        "missing-parent",
        f"looks like slice {slice_number} of #{parent_issue} but is no sub-issue "
        f"of #{parent_issue}; the parent inherits nothing",
        slice=slice_number,
        issue=parent_issue,
    )


def _legacy_or_malformed_checks(item: board.BoardItem) -> tuple[SliceCheck, ...] | None:
    """The one refusal a legacy or malformed block body gets (#150) --
    every other body-contract check (blocker state, completeness) never
    runs, since neither the parsed projections nor the blocker set can be
    trusted once the body itself failed to read."""
    if item.read_state is board.BodyReadState.LEGACY:
        return (SliceCheck("error", "body-legacy", "body legacy", issue=item.number),)
    if item.read_state is board.BodyReadState.MALFORMED:
        return tuple(
            SliceCheck("error", "body-contract", board.body_defect_text(defect))
            for defect in item.contract.defects
        )
    return None


def _body_contract_checks(item: board.BoardItem) -> tuple[SliceCheck, ...]:
    legacy_or_malformed = _legacy_or_malformed_checks(item)
    if legacy_or_malformed is not None:
        return legacy_or_malformed
    contract = item.contract
    checks = [SliceCheck("error", "body-contract", defect.message) for defect in contract.defects]
    # Read the two atomic facts directly rather than `item.actionable_reason`:
    # that reason is the *first* one `_actionable_reason` finds (frozen,
    # claimed, blocked, then incomplete), so an item that is both blocked and
    # incomplete would report only "blocked" there -- masking the incomplete
    # body this check exists to name. A freshly `cut` child (an incomplete
    # but defect-free skeleton) is refused here exactly as it is invisible to
    # `next`, regardless of what else may also be true of it.
    if not item.contract_complete and not item.projectionless_idea:
        missing = ", ".join(board.missing_or_empty_sections(contract))
        checks.append(
            SliceCheck(
                "error",
                "body-incomplete",
                f"#{item.number} body incomplete: {missing}",
                issue=item.number,
            )
        )
    return tuple(checks)


@dataclass(frozen=True)
class BoardReferenceLookup:
    """The board client, its repository, and the currently open issues it can
    resolve `#reference`s against — what every cross-issue slice/parent check
    below needs to look a referenced issue up."""

    client: forge.ForgeReader
    repository: str
    open_by_number: dict[int, board.Issue]


def _slice_rule_checks(
    lookup: BoardReferenceLookup,
    issue: int,
    projected: board.Board,
    out_of_order_reason: str | None,
) -> tuple[SliceCheck, ...]:
    checks: list[SliceCheck] = []
    out_of_order = _out_of_order_check(projected, issue, out_of_order_reason)
    if out_of_order is not None:
        checks.append(out_of_order)
    item = next((item for item in projected.items if item.number == issue), None)
    if item is not None and item.kind is board.ItemKind.CONTAINER:
        checks.append(
            SliceCheck("error", "container", f"#{issue} is a container; claim a child", issue=issue)
        )
    blocked = _blocked_check(item, out_of_order_reason, lookup.repository)
    if blocked is not None:
        checks.append(blocked)
    state, title, _body = _issue_reference_state(lookup.client, lookup.open_by_number, issue)
    if state is forge.ItemState.CLOSED:
        checks.append(SliceCheck("error", "closed-issue", f"issue #{issue} is closed", issue=issue))
    elif state is forge.ItemState.MISSING:
        checks.append(
            SliceCheck("error", "missing-issue", f"issue #{issue} does not exist here", issue=issue)
        )
    if item is not None:
        checks.extend(_body_contract_checks(item))
    if title is not None:
        parent_check = _parent_checks(lookup.client, lookup.repository, issue, title)
        if parent_check is not None:
            checks.append(parent_check)
    return tuple(checks)


def _refuse_claim(json_mode: bool, issue: int | None, checks: tuple[SliceCheck, ...]) -> None:
    if json_mode:
        payload = {"refused": True, "issue": issue, "checks": [c.as_json() for c in checks]}
        print(json.dumps(payload))
        return
    for check in checks:
        print(check.render(), file=sys.stderr)


def _claim_defect(
    claims: tuple[protocol.ActiveClaim, ...],
    detail: forge.Landing,
    identity: protocol.ClaimIdentity,
) -> board.ClassificationDefect | None:
    """A landing declares only what its own head branch holds a live store claim on."""
    matching = next(
        (
            claim
            for claim in claims
            if claim.identity == identity and claim.branch == detail.source_branch
        ),
        None,
    )
    if matching is None:
        subject = (
            f"claim for #{identity.issue}"
            if isinstance(identity, protocol.IssueIdentity)
            else "issue-less lane claim"
        )
        return board.ClassificationDefect(
            f"has no active {subject} on branch {detail.source_branch!r}"
        )
    return None


def _no_item_defect(
    claims: tuple[protocol.ActiveClaim, ...],
    repository: str,
    detail: forge.Landing,
) -> board.ClassificationDefect | None:
    """Why this repository does not accept an issue-less landing as declared.

    A `No-Item` lane owns no issue, so it needs its own lane claim and may
    retire nothing: a closing reference here would close an item no claim and
    no `Work-Item:` line ever named.
    """
    claim_defect = _claim_defect(claims, detail, protocol.LaneIdentity())
    if claim_defect is not None:
        return claim_defect
    closing = board.closing_references(detail.body, repository)
    if closing:
        named = ", ".join(str(reference) for reference in sorted(closing, key=str))
        return board.ClassificationDefect(
            f"declares no work item but closes {named}; name it as the work item"
        )
    return None


@dataclass(frozen=True)
class _LandingCheckContext:
    """What every landing-classification helper needs beyond the pull
    request's own detail and the store's live claims: which forge to read
    and which repository owns the check -- grouped so the cluster of helpers
    below stays under the five-argument limit as claims moved from a
    `client`-read ledger walk to a separately threaded store snapshot."""

    client: forge.ForgeReader
    repository: str


@dataclass(frozen=True)
class _ParentRequirement:
    """What an item's parent demands of the pull request that lands the item.

    `last_child` says closing the parent is *permitted* -- this landing
    closes the parent's one remaining open child; `closing_required` narrows
    that to *required*, which holds only when the parent's own `Next` line
    names no further work.
    """

    reference: board.IssueReference
    closing_required: bool
    last_child: bool


def _parent_body_finding(reference: board.IssueReference, parsed: board.ParsedBody) -> str:
    """Why a legacy or malformed parent body refuses the last-child rule
    before its `Next` line is ever consulted (#150)."""
    if parsed.read_state is board.BodyReadState.LEGACY:
        return f"has parent {reference} with a legacy body"
    defect = parsed.contract.defects[0]
    return f"has parent {reference} with a {board.body_defect_text(defect)}"


def _parent_reference_defect(
    parent: board.ParentIssue, repository: str
) -> board.ClassificationDefect | None:
    """Whether the parent itself is even a same-repository container --
    checked before its body is read at all."""
    if parent.reference.repository != repository:
        return board.ClassificationDefect(
            f"has parent {parent.reference} in another repository, "
            "whose children this check cannot read"
        )
    if parent.kind is not board.ItemKind.CONTAINER:
        kind_text = parent.kind.value if parent.kind is not None else "unknown"
        return board.ClassificationDefect(
            f"has parent {parent.reference} of kind {kind_text}, which is not a "
            "container; only a container holds children"
        )
    return None


def _parent_requirement(
    context: _LandingCheckContext,
    item: board.IssueReference,
) -> _ParentRequirement | board.ClassificationDefect | None:
    """The parent's demand, read from GitHub's sub-issue relation and its
    body under the repository's own pin (#150).

    Closing a parent's last open child completes the parent only when the
    parent's own `Next` line names no further work; otherwise the container
    keeps dispatching slices, and the landing may close the parent (its one
    remaining child) but need not. A parent keeping other open children
    stays open, and must say what happens next.
    """
    parent = context.client.parent_issue(item.number)
    if parent is None:
        return None
    reference_defect = _parent_reference_defect(parent, context.repository)
    if reference_defect is not None:
        return reference_defect
    parsed_parent = board.parse_body(parent.body)
    if parsed_parent.read_state is not board.BodyReadState.VALID:
        return board.ClassificationDefect(_parent_body_finding(parent.reference, parsed_parent))
    remaining = tuple(
        child
        for child in context.client.list_children(parent.reference.number)
        if child.state is board.ChildState.OPEN and child.number != item.number
    )
    if not remaining:
        return _ParentRequirement(
            parent.reference,
            not board.has_further_work(parsed_parent.contract.next),
            True,
        )
    if not parsed_parent.contract.next:
        children = "child" if len(remaining) == 1 else "children"
        return board.ClassificationDefect(
            f"leaves parent {parent.reference} open with {len(remaining)} other open "
            f"{children}, whose body carries no Next line"
        )
    return _ParentRequirement(parent.reference, False, False)


def _closing_defect(
    detail: forge.Landing,
    repository: str,
    item: board.IssueReference,
    requirement: _ParentRequirement | None,
) -> board.ClassificationDefect | None:
    """Which issues this landing must close, and that it closes nothing else."""
    closing = board.closing_references(detail.body, repository)
    if item not in closing:
        return board.ClassificationDefect(f"carries no closing reference for its work item {item}")
    if (
        requirement is not None
        and requirement.closing_required
        and requirement.reference not in closing
    ):
        return board.ClassificationDefect(
            f"closes the last open child of parent {requirement.reference}; close the parent too"
        )
    permitted_parent = (
        {requirement.reference} if requirement is not None and requirement.last_child else set()
    )
    besides = tuple(sorted(closing - {item} - permitted_parent, key=str))
    if besides:
        named = ", ".join(str(reference) for reference in besides)
        return board.ClassificationDefect(
            f"closes {named} besides its work item {item}; a pull request lands one item"
        )
    return None


def _work_item_defect(
    context: _LandingCheckContext,
    claims: tuple[protocol.ActiveClaim, ...],
    detail: forge.Landing,
    item: board.IssueReference,
) -> board.ClassificationDefect | None:
    """Why this repository does not accept `item` as the landing pull request's work item."""
    if item.repository != context.repository:
        return board.ClassificationDefect(
            f"names work item {item} of another repository, which holds no claim here"
        )
    claim_defect = _claim_defect(claims, detail, protocol.IssueIdentity(item.number))
    if claim_defect is not None:
        return claim_defect
    requirement = _parent_requirement(context, item)
    if isinstance(requirement, board.ClassificationDefect):
        return requirement
    return _closing_defect(detail, context.repository, item, requirement)


def _checked_classification(
    context: _LandingCheckContext,
    claims: tuple[protocol.ActiveClaim, ...],
    detail: forge.Landing,
) -> board.Classification | board.ClassificationDefect:
    if detail.source_repository.path != context.repository:
        return board.ClassificationDefect(
            f"proposes a branch of {detail.source_repository}; cross-repository pull "
            "requests are not classified"
        )
    classification = board.parse_pull_request_classification(detail.body, context.repository)
    if isinstance(classification, board.ClassificationDefect):
        return classification
    default_branch = context.client.default_branch()
    if detail.target_branch != default_branch:
        return board.ClassificationDefect(
            f"targets {detail.target_branch!r}, not the default branch {default_branch!r}"
        )
    defect = (
        _no_item_defect(claims, context.repository, detail)
        if isinstance(classification, board.NoItemClassification)
        else _work_item_defect(context, claims, detail, classification.item)
    )
    return classification if defect is None else defect


class CheckKind(StrEnum):
    """Which subject one `check` run read -- the `--json` discriminator.

    Three values, because that is what the one dispatch request actually
    distinguishes: GitHub gives issues and pull requests a single number
    space, so a number that is not there was never proven to be either.
    """

    PULL_REQUEST = "pull_request"
    ISSUE = "issue"
    MISSING = "missing"


@dataclass(frozen=True)
class CheckOutcome:
    """One `check` answer: the line a human reads, and the reason a caller
    acts on -- `None` when the subject passed."""

    kind: CheckKind
    number: int
    line: str
    refused: str | None = None

    def as_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": self.refused is None,
            "kind": self.kind.value,
            "number": self.number,
        }
        if self.refused is not None:
            payload["refused"] = self.refused
        return payload

    def report(self, *, as_json: bool) -> int:
        if as_json:
            print(json.dumps(self.as_json()))
        else:
            print(self.line, file=sys.stderr if self.refused is not None else sys.stdout)
        return 1 if self.refused is not None else 0


def _pull_request_check(
    client: forge.ForgeReader,
    claims: tuple[protocol.ActiveClaim, ...],
    repository: str,
    number: int,
) -> CheckOutcome:
    detail = client.landing(number)
    context = _LandingCheckContext(client, repository)
    checked = _checked_classification(context, claims, detail)
    if isinstance(checked, board.ClassificationDefect):
        return CheckOutcome(
            CheckKind.PULL_REQUEST,
            detail.number,
            f"REFUSED: pull request #{detail.number} {checked.message}",
            checked.message,
        )
    return CheckOutcome(
        CheckKind.PULL_REQUEST,
        detail.number,
        f"PR #{detail.number} by {detail.author} declares {checked}",
    )


def _missing_number(repository: str, number: int) -> CheckOutcome:
    """A number neither mode can read, named without claiming which of the
    two it would have been."""
    finding = f"does not exist in {repository}"
    return CheckOutcome(CheckKind.MISSING, number, f"REFUSED: #{number} {finding}", finding)


def _issue_line(number: int, finding: str) -> str:
    """The one shape every issue-mode answer takes."""
    return f"ISSUE #{number} {finding}"


def _refused_issue(number: int, finding: str) -> CheckOutcome:
    return CheckOutcome(CheckKind.ISSUE, number, _issue_line(number, finding), finding)


def _issue_check(
    client: forge.ForgeReader, repository: str, body: str, number: int
) -> CheckOutcome:
    """Whether this issue's body is the contract a builder can start from:
    readable, complete, and unblocked. Its dependencies come from GitHub's
    own `blocked_by` relation -- a body never states them itself."""
    parsed = board.parse_body(body)
    if parsed.read_state is board.BodyReadState.LEGACY:
        return _refused_issue(number, "body legacy")
    if parsed.read_state is board.BodyReadState.MALFORMED:
        return _refused_issue(number, board.body_defect_text(parsed.contract.defects[0]))
    missing = board.missing_or_empty_sections(parsed.contract)
    if missing:
        return _refused_issue(number, f"body incomplete: {', '.join(missing)}")
    blockers = board.open_dependency_blockers(client.list_board_dependencies(number), repository)
    if blockers:
        named = ", ".join(board.open_blocker_label(blocker, repository) for blocker in blockers)
        return _refused_issue(number, f"blocked by {named}")
    return CheckOutcome(CheckKind.ISSUE, number, _issue_line(number, "body ok"))


def _release_outcome(merged: int | None, abandoned: str | None) -> protocol.ReleaseOutcome:
    if merged is not None:
        return protocol.MergedRelease(merged)
    return protocol.AbandonedRelease(
        protocol._outbound_text(abandoned, "abandoned reason", maximum=512)
    )


def _verify_merged_release(
    client: forge.ForgeReader,
    repository: str,
    identity: protocol.ClaimIdentity,
    merged: protocol.MergedRelease,
) -> None:
    """Refuse a `--merged` release the landing itself does not support."""
    detail = client.landing(merged.pull_request)
    if not detail.merged:
        raise protocol.ClaimUnavailableError(f"pull request #{detail.number} is not merged")
    default_branch = client.default_branch()
    if detail.target_branch != default_branch:
        raise protocol.ClaimUnavailableError(
            f"pull request #{detail.number} merged into {detail.target_branch!r}, "
            f"not the default branch {default_branch!r}"
        )
    classification = board.parse_pull_request_classification(detail.body, repository)
    if isinstance(classification, board.ClassificationDefect):
        raise protocol.ClaimUnavailableError(
            f"pull request #{detail.number} {classification.message}"
        )
    if isinstance(identity, protocol.LaneIdentity):
        if isinstance(classification, board.WorkItemClassification):
            raise protocol.ClaimUnavailableError(
                f"pull request #{detail.number} names {classification.item}; "
                "an issue-less lane needs a No-Item line"
            )
        return
    item = board.IssueReference(repository, identity.issue)
    if not isinstance(classification, board.WorkItemClassification) or classification.item != item:
        raise protocol.ClaimUnavailableError(
            f"pull request #{detail.number} names {classification}, not work item #{identity.issue}"
        )
    reference = _fetch_issue_reference(client, identity.issue)
    if reference.state is not forge.ItemState.CLOSED:
        raise protocol.ClaimUnavailableError(
            f"work item #{identity.issue} is {reference.state.value}, not closed"
        )


def _canonical_remote_repository(canonical_remote: str) -> forge.RepositoryId:
    """The repository `canonical_remote`'s configured URL names (issue #176, §2).

    Refuses by name when the URL does not match GitHub's remote pattern --
    the same pattern `github.discover_repository` already parses with.
    """
    url = checkout.remote_url(canonical_remote)
    match = github.GITHUB_REMOTE_PATTERN.search(url)
    if match is None:
        raise protocol.ClaimError(
            f"canonical remote {canonical_remote!r} url {url!r} does not name a GitHub repository"
        )
    return forge.RepositoryId(github.GITHUB_HOST, (match.group(1),), match.group(2))


def _refuse_canonical_remote_mismatch(
    forge_target: forge.RepositoryId, canonical_remote: str
) -> None:
    """Every store command's shared refusal (Erwartung 6): the forge target
    (`--repo`, or whatever `discover_repository` resolved) must name the same
    repository the canonical remote's own URL points at, or nothing is read
    or written.
    """
    canonical_repository = _canonical_remote_repository(canonical_remote)
    if canonical_repository.path != forge_target.path:
        raise protocol.ClaimUnavailableError(
            f"forge target {forge_target.path} does not match canonical remote "
            f"{canonical_repository.path}; run aco from that repository's checkout"
        )


def _resolved_canonical_remote(repository: str | None, toplevel: Path) -> str:
    """Every store command's shared precondition (issue #176, Erwartung 6/7):
    read this repository's configured `canonical_remote` and refuse when the
    forge target does not name the same repository its URL points at."""
    config = board.load_config(toplevel / board.CONFIG_PATH)
    forge_target = github.discover_repository(repository, remote_url=checkout.origin_remote_url)
    _refuse_canonical_remote_mismatch(forge_target, config.canonical_remote)
    return config.canonical_remote


def _claim_ages(worktree: Path, state: protocol.ClaimState) -> dict[str, datetime]:
    """Each live claim's age in an already-fetched state (its `opened_commit`'s
    committer date, from the same fetched tip's ancestry -- §1 "Status age
    ..."). Split from `_fetched_claims_and_ages` so a caller that already
    holds a `ClaimState` (`claim`/`rescope`/`release`/`board`/`rulings`/
    `next`) never fetches twice just to get ages too.
    """
    if state.tip is None:
        return {}
    tip = state.tip
    return {
        claim.claim_id: store.committer_date(worktree=worktree, tip=tip, commit=claim.opened_commit)
        for claim in state.claims.values()
    }


def _fetched_claims_and_ages(
    worktree: Path, canonical_remote: str
) -> tuple[tuple[protocol.ActiveClaim, ...], dict[str, datetime]]:
    """The store's live claims, plus each one's age -- for a caller (`status`)
    that has not already fetched the state itself."""
    state = store.fetch_state(worktree=worktree, remote=canonical_remote)
    return tuple(state.claims.values()), _claim_ages(worktree, state)


def _store_observation(
    parsed: argparse.Namespace,
) -> tuple[Path, str, protocol.ClaimState]:
    """One fetch of `refs/aco/state` for a store command, after the shared
    forge-target / canonical-remote refusal."""
    canonical_remote = _resolved_canonical_remote(parsed.repo, _resolve_toplevel())
    worktree = Path.cwd()
    return worktree, canonical_remote, store.fetch_state(worktree=worktree, remote=canonical_remote)


def _require_state_ref(state: protocol.ClaimState) -> None:
    if state.tip is None:
        raise protocol.ClaimError(protocol.MISSING_STATE_REF)


def _transition_subject(action: str, identity: protocol.ClaimIdentity, branch: str) -> str:
    """The commit message's first line (§1 "Commit message"): `claim issue 42`,
    `rescope lane docs/lane-cleanup`, and so on."""
    if isinstance(identity, protocol.LaneIdentity):
        return f"{action} lane {branch}"
    return f"{action} issue {identity.issue}"


def _claim_intent_from_request(
    request: protocol.ClaimRequest, operation_id: str
) -> protocol.ClaimIntent:
    """`_request`'s validated `ClaimRequest`, converted to the store's own
    intent (issue #176, §1: `ClaimRequest` stays the CLI-facing input,
    `ClaimIntent` is what `apply` actually consumes)."""
    resource_name = None
    resource_value = None
    if request.resource is not None:
        resource_name = request.resource
        resource_value = request.resource_value
    return protocol.ClaimIntent(
        identity=request.identity,
        agent=request.agent,
        role=request.role,
        base=protocol.ObjectId(request.base),
        branch=request.branch,
        scope=request.scope,
        claim_id=protocol.ClaimId(request.claim_id),
        operation_id=operation_id,
        whole_reason=request.whole_reason,
        resource_name=resource_name,
        resource_value=resource_value,
    )


def _matching_store_claim(
    observed: protocol.ClaimState, request: protocol.ClaimRequest
) -> protocol.ActiveClaim | None:
    """The live store claim an interrupted, replayed `claim` invocation may
    reuse (criterion 2): same identity, agent, role, branch, scope, *and*
    claim id -- `apply` itself only replays on an exact claim-id match
    (§1 "Retry of an interrupted identical request"), so a CLI-level replay
    check that skips this slice's dispatch rules must use the same test, not
    the ledger's looser field-only match. Issueless lanes keep today's
    one-claim-per-branch contract; only a numbered item gets replay
    detection at all.
    """
    if not isinstance(request.identity, protocol.IssueIdentity):
        return None
    existing = observed.claims.get(protocol.claim_key(request.identity, request.branch))
    if (
        existing is not None
        and existing.claim_id == request.claim_id
        and existing.agent == request.agent
        and existing.role == request.role
        and existing.branch == request.branch
        and existing.scope == request.scope
    ):
        return existing
    return None


def _selected_store_claim(
    observed: protocol.ClaimState,
    identity: protocol.ClaimIdentity,
    branch: str | None,
    claim_id: str | None,
) -> protocol.ActiveClaim:
    """The one live store claim `rescope`/`release` names: at most one claim
    is ever live per identity (the store's own invariant), so this is a
    direct key lookup, never the ledger's filter-then-disambiguate walk.
    `claim_id`, when given, is a safety check against that one claim, not a
    selector among several -- there are never several.
    """
    if isinstance(identity, protocol.LaneIdentity) and not branch:
        raise protocol.ClaimUnavailableError(
            "lane release requires a non-empty current branch; check out the "
            "docs/ or fix/ lane branch, or pass an issue number"
        )
    selected = observed.claims.get(protocol.claim_key(identity, branch or ""))
    if selected is None or (claim_id is not None and selected.claim_id != claim_id):
        raise protocol.ClaimUnavailableError(
            f"{protocol._identity_summary(identity, branch or '')} has no active build claim"
        )
    return selected


MUTATING_HOOK_TOOLS = frozenset({"Edit", "MultiEdit", "Write", "search_replace", "write"})


def _hook_allow() -> int:
    print(json.dumps({"decision": "allow"}))
    return 0


def _hook_deny(reason: str) -> int:
    print(json.dumps({"decision": "deny", "reason": reason}))
    return 2


def _hook_payload() -> dict[str, object] | None:
    try:
        payload = json.loads(sys.stdin.read())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _hook_field(payload: dict[str, object], *keys: str) -> object:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


def _hook_path(tool_input: dict[str, object]) -> str | None:
    for key in ("path", "file_path", "filePath"):
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _protect_relative_path(raw_path: str, *, toplevel: Path) -> str | None:
    candidate = Path(raw_path)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    try:
        relative = candidate.resolve().relative_to(toplevel).as_posix()
        return protocol._valid_scope([relative])[0]
    except (protocol.InvalidClaimMarkerError, OSError, ValueError):
        return None


PATH_REQUIRED = "path required"


def _protect_hook_path(payload: dict[str, object]) -> str | None:
    tool_input = _hook_field(payload, "toolInput", "tool_input")
    if not isinstance(tool_input, dict):
        return None
    return _hook_path(tool_input)


def _protect_checkout_refusal(branch: str) -> str | None:
    if branch in {"main", "master"}:
        return "not main"
    git_directory = Path(checkout._git_output(["rev-parse", "--git-dir"])).resolve()
    common_directory = Path(checkout._git_output(["rev-parse", "--git-common-dir"])).resolve()
    if git_directory == common_directory:
        return "worktree"
    return None


def _protect_store_verdict(agent: str, branch: str, relative: str, canonical_remote: str) -> int:
    """`protect`'s live snapshot (issue #176, §1): one fetch, no positive cache
    (D2) -- allow only when this session's agent and branch hold a scope
    overlapping the tool path. Every store read failure (unreachable, auth,
    malformed tree, lineage break) denies with the same named text (Erwartung
    8) instead of the generic 'claim first', which would send the agent
    toward a command that cannot fix a transient fetch failure.
    """
    try:
        state = store.fetch_state(worktree=Path.cwd(), remote=canonical_remote)
    except protocol.ClaimError as error:
        return _hook_deny(f"cannot reach {store.STATE_REF}: {error}")
    if state.tip is None:
        return _hook_deny(f"cannot reach {store.STATE_REF}: {protocol.MISSING_STATE_REF}")
    for claim in state.claims.values():
        if (
            claim.agent == agent
            and claim.branch == branch
            and protocol._scopes_overlap(claim.scope, (relative,))
        ):
            return _hook_allow()
    return _hook_deny("claim first")


def _protect_write(repository: str | None, payload: dict[str, object]) -> int:
    raw_path = _protect_hook_path(payload)
    if raw_path is None:
        return _hook_deny(PATH_REQUIRED)
    agent = checkout._resolved_agent(None)
    branch = checkout._git_output(["branch", "--show-current"])
    refusal = _protect_checkout_refusal(branch)
    if refusal is not None:
        return _hook_deny(refusal)
    toplevel = _resolve_toplevel().resolve()
    relative = _protect_relative_path(raw_path, toplevel=toplevel)
    if relative is None:
        return _hook_deny(PATH_REQUIRED)
    canonical_remote = _resolved_canonical_remote(repository, toplevel)
    return _protect_store_verdict(agent, branch, relative, canonical_remote)


def _protect(repository: str | None) -> int:
    # Grok fail-opens on crash or non-JSON hook output; deny instead of raising.
    try:
        payload = _hook_payload()
        if payload is None:
            return _hook_deny("invalid hook payload")
        tool_name = _hook_field(payload, "toolName", "tool_name")
        if not isinstance(tool_name, str):
            return _hook_deny("invalid hook payload")
        if tool_name not in MUTATING_HOOK_TOOLS:
            return _hook_allow()
        return _protect_write(repository, payload)
    except Exception as error:
        return _hook_deny(str(error))


def _optional_issue_number(value: int | None) -> int | None:
    return None if value is None else int(value)


@dataclass(frozen=True)
class _ReadSession:
    """What a dispatched read-only subcommand needs beyond its parsed arguments."""

    forge: forge.ForgeReader


@dataclass(frozen=True)
class _WriteSession:
    """What a dispatched write subcommand needs beyond its parsed arguments."""

    forge: forge.ForgeWriter
    release_branch: str | None


def _rescope_command(parsed: argparse.Namespace) -> protocol.RescopeRequest:
    branch = checkout._git_output(["branch", "--show-current"])
    if not branch:
        raise protocol.ClaimUnavailableError(
            "rescope requires a non-empty current branch; "
            "check out the claim branch, or pass an issue number"
        )
    checkout._validate_worktree_branch(branch, repair=checkout.WorktreeRepair.RETURN_TO_CLAIM)
    identity = _resolved_identity(_optional_issue_number(parsed.issue), branch)
    return protocol.RescopeRequest(
        identity=identity,
        agent=parsed.agent,
        add=protocol._valid_scope(parsed.add) if parsed.add else (),
        drop=protocol._valid_scope(parsed.drop) if parsed.drop else (),
        claim_id=parsed.claim_id,
        branch=branch,
        whole_reason=_optional_whole_reason(parsed),
    )


def _cmd_check(parsed: argparse.Namespace, session: _ReadSession) -> int:
    """One number, one dispatch request into one of three answers: a pull
    request to classify, an issue whose body contract to read, or a number
    that is in neither number space. Only the pull-request side needs the
    live claims, so the issue side never fetches the state ref."""
    number = int(parsed.number)
    client = session.forge
    repository = client.repository.path
    # Read for its refusals only: a repository pinned to a grammar this tool
    # no longer reads, or a forge that cannot answer `blocked_by`, must fail
    # here rather than hand back a half-read answer.
    _load_board_config(client, _resolve_toplevel())
    reference = client.item_reference(number)
    if reference.state is forge.ItemState.MISSING:
        outcome = _missing_number(repository, number)
    elif reference.is_landing:
        _worktree, _remote, observed = _store_observation(parsed)
        outcome = _pull_request_check(client, tuple(observed.claims.values()), repository, number)
    else:
        outcome = _issue_check(client, repository, reference.body or "", number)
    return outcome.report(as_json=parsed.json)


def _cmd_status(parsed: argparse.Namespace) -> int:
    """`status` reads live claims from the store directly (issue #176),
    dispatched straight from `main` -- it never needs `_dispatch`'s ledger
    resolution (a cut-over repository may have no ledger issue left at all).
    """
    canonical_remote = _resolved_canonical_remote(parsed.repo, _resolve_toplevel())
    claims, ages = _fetched_claims_and_ages(Path.cwd(), canonical_remote)
    if parsed.path is not None:
        if parsed.json:
            return _status_path_json(claims, parsed.path)
        _status_path(claims, parsed.path)
        return 0
    issue = _optional_issue_number(parsed.issue)
    now = datetime.now(UTC)
    if parsed.json:
        return _status_json(claims, issue, ages, now=now)
    return _status(claims, issue, ages, now=now)


def _observed_board(
    parsed: argparse.Namespace,
    session: _ReadSession,
    *,
    issues: tuple[board.Issue, ...] | None = None,
) -> board.Board:
    """`board`/`rulings`/`next` share this: the store's live claims, projected
    onto forge board data (issue #176 -- claims no longer come from the
    ledger; the forge is still the board's own data source)."""
    worktree, _remote, observed = _store_observation(parsed)
    return _board(
        session.forge,
        tuple(observed.claims.values()),
        issues=issues,
        claim_ages=_claim_ages(worktree, observed),
    )


def _cmd_board(parsed: argparse.Namespace, session: _ReadSession) -> None:
    projected = _observed_board(parsed, session)
    print(board.board_json(projected) if parsed.json else board.render(projected))


def _cmd_rulings(parsed: argparse.Namespace, session: _ReadSession) -> None:
    issues = session.forge.list_open_board_issues()
    projected = _observed_board(parsed, session, issues=issues)
    _rulings(projected, as_json=parsed.json)


def _next_action_container_number(action: board.NextAction | None) -> int | None:
    """The container `action` targets, when it targets one -- excluded from
    `SKIPPED` below since a container is always non-actionable itself."""
    if isinstance(action, board.CutSliceAction | board.CloseContainerAction):
        return action.container.number
    return None


def _cmd_next(parsed: argparse.Namespace, session: _ReadSession) -> int:
    projected = _observed_board(parsed, session)
    action = board.next_action(projected)
    chosen_container = _next_action_container_number(action)
    skipped = tuple(item for item in _unworkable(projected) if item.number != chosen_container)
    recovery = projected.recovery
    if action is None:
        if parsed.json:
            _next_json(None, skipped, recovery)
        else:
            _next(None, skipped, recovery)
        return 3
    if parsed.json:
        return _next_json(action, skipped, recovery)
    return _next(action, skipped, recovery)


def _cmd_rescope(parsed: argparse.Namespace, _session: _WriteSession) -> None:
    requested = _rescope_command(parsed)
    worktree, canonical_remote, observed = _store_observation(parsed)
    _require_state_ref(observed)
    selected = _selected_store_claim(
        observed, requested.identity, requested.branch, requested.claim_id
    )
    if requested.agent != selected.agent:
        raise protocol.ClaimUnavailableError(
            "only the original claimant may rescope "
            f"(holder={protocol._claimant_text(selected.agent, selected.role)!r}, "
            f"this session={protocol._claimant_text(requested.agent, selected.role)!r})"
        )
    versioned = checkout.versioned_paths()
    _reject_ungrounded_comma_scope(requested.add, versioned, flag="--add")
    combined = protocol._combined_scope(selected.scope, requested.add, requested.drop)
    _reject_wide_scope(combined, versioned, requested.whole_reason or selected.whole_reason)
    intent = protocol.RescopeIntent(
        claim_id=selected.claim_id,
        agent=requested.agent,
        role=selected.role,
        scope=combined,
        operation_id=uuid.uuid4().hex,
        whole_reason=requested.whole_reason,
    )
    new_state = store.commit_transition(
        worktree=worktree,
        remote=canonical_remote,
        subject=_transition_subject("rescope", selected.identity, selected.branch),
        intent=intent,
    )
    rescoped = new_state.claims[protocol.claim_key(selected.identity, selected.branch)]
    if parsed.json:
        _rescope_json(rescoped)
        return
    print(f"RESCOPED {_claim_subject(rescoped)}: {rescoped.claim_id}")


def _cmd_claim(parsed: argparse.Namespace, session: _WriteSession) -> int:
    client = session.forge
    requested = _request(parsed)
    versioned = checkout.versioned_paths()
    _reject_ungrounded_comma_scope(requested.scope, versioned, flag="--scope")
    n, total, share = _reject_wide_scope(requested.scope, versioned, requested.whole_reason)
    worktree, canonical_remote, observed = _store_observation(parsed)
    _require_state_ref(observed)
    checks: tuple[SliceCheck, ...] = ()
    target_issue: int | None = None
    replayed = None
    if isinstance(requested.identity, protocol.IssueIdentity):
        target_issue = requested.identity.issue
        replayed = _matching_store_claim(observed, requested)
        if replayed is None:
            open_issues = client.list_open_board_issues()
            open_by_number = {issue.number: issue for issue in open_issues}
            projected = _board(
                client,
                tuple(observed.claims.values()),
                issues=open_issues,
                claim_ages=_claim_ages(worktree, observed),
            )
            checks = _slice_rule_checks(
                BoardReferenceLookup(client, client.repository.path, open_by_number),
                target_issue,
                projected,
                requested.out_of_order_reason,
            )
    if any(check.level == "error" for check in checks):
        _refuse_claim(parsed.json, target_issue, checks)
        return 2
    for check in checks:
        print(check.render(), file=sys.stderr if parsed.json else sys.stdout)
    if replayed is None:
        intent = _claim_intent_from_request(requested, uuid.uuid4().hex)
        new_state = store.commit_transition(
            worktree=worktree,
            remote=canonical_remote,
            subject=_transition_subject("claim", requested.identity, requested.branch),
            intent=intent,
        )
        claimed = new_state.claims[protocol.claim_key(requested.identity, requested.branch)]
        live = tuple(new_state.claims.values())
    else:
        claimed = replayed
        live = tuple(observed.claims.values())
    touches = protocol.conflicting_claims(live, claimed)
    if parsed.json:
        return _claim_json(
            claimed,
            versioning=ScopeVersioning(n, total, share),
            touches=touches,
            checks=checks,
        )
    print(f"CLAIMED {_claim_subject(claimed)}: {claimed.claim_id}")
    print(_claim_cost_line(n, total, requested.scope, touches))
    return 0


def _cmd_release(parsed: argparse.Namespace, session: _WriteSession) -> None:
    client = session.forge
    issue = _optional_issue_number(parsed.issue)
    identity = _resolved_identity(issue, session.release_branch or "")
    merged = None if parsed.merged is None else int(parsed.merged)
    outcome = _release_outcome(merged, parsed.abandoned)
    if isinstance(outcome, protocol.MergedRelease):
        _verify_merged_release(client, client.repository.path, identity, outcome)
    worktree, canonical_remote, observed = _store_observation(parsed)
    _require_state_ref(observed)
    selected = _selected_store_claim(observed, identity, session.release_branch, parsed.claim_id)
    role = parsed.role
    if not parsed.coordinator_override:
        if role is None:
            role = selected.role
        if (parsed.agent, role) != (selected.agent, selected.role):
            raise protocol.ClaimUnavailableError(
                "only the original claimant may release; use an explicit coordinator override "
                f"(holder={protocol._claimant_text(selected.agent, selected.role)!r}, "
                f"this session={protocol._claimant_text(parsed.agent, role)!r})"
            )
    resolved_role = role if role is not None else selected.role
    intent = protocol.ReleaseIntent(
        claim_id=selected.claim_id,
        agent=parsed.agent,
        role=resolved_role,
        outcome=outcome,
        operation_id=uuid.uuid4().hex,
        coordinator_override=parsed.coordinator_override,
    )
    store.commit_transition(
        worktree=worktree,
        remote=canonical_remote,
        subject=_transition_subject("release", selected.identity, selected.branch),
        intent=intent,
    )
    if parsed.json:
        _release_json(selected, parsed.agent, resolved_role, outcome)
        return
    print(f"RELEASED {_claim_subject(selected)}: {selected.claim_id}")


def _cut_target(client: forge.ForgeWriter, number: int) -> board.Issue:
    """The open container `cut` targets, or why it refuses before any write."""
    open_issues = client.list_open_board_issues()
    target = next((issue for issue in open_issues if issue.number == number), None)
    if target is None:
        raise protocol.ClaimUnavailableError(f"#{number} is not an open container")
    if target.kind is not board.ItemKind.CONTAINER:
        raise protocol.ClaimUnavailableError(f"#{number} is not a container")
    parent = client.parent_issue(number)
    if parent is not None:
        raise protocol.ClaimUnavailableError(
            f"#{number} is itself a child of {parent.reference}; "
            "nested containers are not supported"
        )
    return target


def _link_created_child(
    client: forge.ForgeWriter, container: int, new_body: str, child: int, step: str
) -> None:
    """Write `new_body` (`container`'s `agent-claim` block, the
    just-created `child`'s slice entry already removed from it) back to
    `container`.

    Not atomic with `create_child` -- GitHub has no transaction across the
    two writes. A failure here still leaves the created child behind, so it
    raises the same `forge.ForgePartialChildCreationError` a failed relation
    write inside `create_child` itself would -- one type, so `_cmd_cut`
    renders one recovery message for either, `step` naming the exact manual
    repair.
    """
    try:
        client.update_item_body(container, new_body)
    except protocol.ClaimError as error:
        raise forge.ForgePartialChildCreationError(
            child=child, parent=container, step=step, cause=error
        ) from error


def _print_cut_result(number: int, row_index: int | None, child: int, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"container": number, "row": row_index, "child": child}))
        return
    suffix = "" if row_index is None else f" row {row_index}"
    print(f"CUT #{number}{suffix} -> #{child}")


def _block_slice_entries(data: Mapping[str, object]) -> list[dict[str, object]]:
    value = data.get("slice")
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _slice_row(entry: dict[str, object]) -> board.SliceRow:
    return board.SliceRow(cast(int, entry["index"]), cast(str, entry["title"]))


def _cut_link(
    number: int, data: Mapping[str, object], row_number: int | None
) -> board.SliceRow | None:
    """Which `[[slice]]` entry `cut` links its fresh child to (#150 §7):
    without `--row`, the first entry when the block carries one; with `--row
    N`, the entry `N` names, refusing by name when the block has no `slice`
    key at all or no such row left (every remaining entry is cuttable: a
    linked entry is removed from `data["slice"]` at the moment it is cut)."""
    entries = _block_slice_entries(data)
    if row_number is None:
        return _slice_row(entries[0]) if entries else None
    if "slice" not in data:
        raise protocol.ClaimUnavailableError(
            f"#{number} has no slice table; --row needs one to select a row from"
        )
    match = next((entry for entry in entries if entry["index"] == row_number), None)
    if match is None:
        cuttable = ", ".join(str(entry["index"]) for entry in entries) or "none"
        raise protocol.ClaimUnavailableError(
            f"#{number} has no row {row_number}; cuttable rows: {cuttable}"
        )
    return _slice_row(match)


def _require_matching_title(number: int, link: board.SliceRow, title: str) -> None:
    if title != link.title:
        raise protocol.ClaimUnavailableError(
            f"#{number}'s slice {link.index} is titled {link.title!r}; "
            "--title must match it exactly"
        )


def _cut_target_block(number: int, target: board.Issue) -> board.LocatedBlock:
    parsed = board.parse_body(target.body)
    if parsed.read_state is board.BodyReadState.LEGACY:
        raise protocol.ClaimUnavailableError(
            f"#{number} body legacy; cut needs a valid agent-claim block"
        )
    if parsed.read_state is board.BodyReadState.MALFORMED:
        defect = parsed.contract.defects[0]
        raise protocol.ClaimUnavailableError(
            f"#{number} {board.body_defect_text(defect)}; cut needs a valid agent-claim block"
        )
    return board.locate_agent_claim_block(target.body)


def _cut_slice(client: forge.ForgeWriter, target: board.Issue, parsed: argparse.Namespace) -> int:
    number = target.number
    located = _cut_target_block(number, target)
    link = _cut_link(number, located.data, parsed.row)
    if link is not None:
        _require_matching_title(number, link, parsed.title)
    try:
        child = client.create_child(
            parent=number,
            title=parsed.title,
            body=board.BLOCK_CHILD_SKELETON,
            kind=board.ItemKind.TASK,
        )
        if link is not None:
            remaining = [
                entry
                for entry in _block_slice_entries(located.data)
                if entry["index"] != link.index
            ]
            new_data = {**located.data, "slice": remaining}
            new_body = board.replace_agent_claim_block(target.body, located, new_data)
            step = f"remove row {link.index} from #{number}'s agent-claim block"
            _link_created_child(client, number, new_body, child, step)
    except forge.ForgePartialChildCreationError as error:
        raise protocol.ClaimUnavailableError(
            f"created #{error.child} but failed to {error.step}: {error.cause}; "
            "do not re-run -- finish it by hand"
        ) from error
    _print_cut_result(number, None if link is None else link.index, child, as_json=parsed.json)
    return 0


def _cmd_cut(parsed: argparse.Namespace, session: _WriteSession) -> int:
    client = session.forge
    number = int(parsed.issue)
    for operation in (forge.ForgeOperation.CREATE_CHILD, forge.ForgeOperation.UPDATE_ITEM_BODY):
        if client.capability(operation) is not forge.Capability.READ_WRITE:
            raise protocol.ClaimUnavailableError(
                f"this forge cannot {operation.value}; cut the slice by hand"
            )
    _load_board_config(client, _resolve_toplevel())
    return _cut_slice(client, _cut_target(client, number), parsed)


_READ_HANDLERS: dict[str, Callable[[argparse.Namespace, _ReadSession], int | None]] = {
    "check": _cmd_check,
    "board": _cmd_board,
    "rulings": _cmd_rulings,
    "next": _cmd_next,
}
_WRITE_HANDLERS: dict[str, Callable[[argparse.Namespace, _WriteSession], int | None]] = {
    "rescope": _cmd_rescope,
    "claim": _cmd_claim,
    "release": _cmd_release,
    "cut": _cmd_cut,
}


def _release_branch_for(parsed: argparse.Namespace) -> str | None:
    if parsed.coordinator_override:
        protocol._require_coordinator_override(parsed.role)
    if parsed.issue is not None and parsed.claim_id is not None:
        return None
    release_branch = checkout._git_output(["branch", "--show-current"])
    if release_branch:
        return release_branch
    if parsed.issue is None:
        raise protocol.ClaimUnavailableError(
            "lane release requires a non-empty current branch; "
            "check out the docs/ or fix/ lane branch, or pass "
            "an issue number"
        )
    raise protocol.ClaimUnavailableError(
        "release without --claim-id requires a non-empty current branch; pass --claim-id"
    )


def _bootstrap_state(parsed: argparse.Namespace) -> int:
    """Create `refs/aco/state` if proven absent; report the existing tip
    untouched when it is already there."""
    canonical_remote = _resolved_canonical_remote(parsed.repo, _resolve_toplevel())
    print(store.bootstrap(worktree=Path.cwd(), remote=canonical_remote))
    return 0


def _dispatch(parsed: argparse.Namespace) -> int:
    if parsed.command in {"claim", "release", "rescope"}:
        parsed.agent = checkout._resolved_agent(parsed.agent)
    release_branch = _release_branch_for(parsed) if parsed.command == "release" else None
    repository = github.discover_repository(parsed.repo, remote_url=checkout.origin_remote_url)
    forge_handle = github.GitHubForge(repository)
    if parsed.command == "bootstrap":
        return _bootstrap_state(parsed)
    if parsed.command in _READ_HANDLERS:
        result = _READ_HANDLERS[parsed.command](parsed, _ReadSession(forge=forge_handle))
    else:
        result = _WRITE_HANDLERS[parsed.command](
            parsed, _WriteSession(forge=forge_handle, release_branch=release_branch)
        )
    return 0 if result is None else result


def main(arguments: list[str] | None = None) -> int:
    parsed = _parser().parse_args(arguments)
    if parsed.command == "protect":
        return _protect(parsed.repo)
    try:
        if parsed.command == "status":
            return _cmd_status(parsed)
        return _dispatch(parsed)
    except protocol.ClaimError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        if getattr(parsed, "json", False):
            print(json.dumps({"ok": False, "error": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
