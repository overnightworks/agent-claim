"""Claim-ledger protocol: markers, claims, projections, and reconciliation."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Protocol, TypeVar

# The protocol core is configured by bootstrap --ledger before the one-time import.
LEDGER_ISSUE = 0
LEGACY_MARKER_PREFIX = "<!-- agent-claim:v1 "
MARKER_PREFIX = "<!-- agent-claim:v2 "
MARKER_SUFFIX = " -->"
# The tombstone action a 0.12.x (and older) client fences on. Not in
# `parse_claim_event`'s allow-list: that omission *is* the fence. The import
# reader skips it by reading `_marker_payload` and never calling
# `parse_claim_event` (issue #176, §2).
STATE_CUT_ACTION = "state_cut"
# Named refusal when a write-path command would otherwise create
# `refs/aco/state` as a side effect (issue #176 done-when 1). One owner so
# `apply`, `store.commit_transition`, and `protect` cannot drift.
MISSING_STATE_REF = (
    "the claim state ref does not exist yet; run bootstrap before claim, rescope, or release"
)
# Coordination-contract convention: the only branch prefixes an issueless lane claim
# may use, so a builder that forgot its issue number never gets a silent, unlabeled,
# non-projected claim instead of a loud refusal.
ISSUELESS_LANE_BRANCH_PREFIXES = ("docs/", "fix/")
TRUSTED_ASSOCIATIONS = frozenset({"OWNER", "MEMBER", "COLLABORATOR"})
CLAIM_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
RESOURCE_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,63}")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
BRANCH_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}")
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")
MAX_PROTOCOL_EVENTS = 4096
MAX_PROTOCOL_BYTES = 8 * 1024 * 1024
MAX_COMMENT_BYTES = 48 * 1024
MAX_SCOPE_ENTRIES = 256
MAX_SCOPE_PATH_LENGTH = 512
WIDE_SCOPE_PATH_LIMIT = 3
WIDE_SCOPE_SHARE_LIMIT = 0.25
# A tiny repository must not trip the share condition.
WIDE_SCOPE_SHARE_FLOOR = 12
# The first printable ASCII code point (space) and DEL bound the control
# characters a claim marker field, scope path, or outbound text may never
# contain -- each is meant to read as a single printable line.
ASCII_PRINTABLE_MIN = 0x20
ASCII_DEL = 0x7F


class ClaimError(RuntimeError):
    pass


class ClaimUnavailableError(ClaimError):
    pass


class InvalidClaimMarkerError(ClaimError):
    pass


LEDGER_BODY_MARKER = "<!-- agent-claim-ledger:v1 -->"


def configure_ledger(issue: int) -> None:
    """Bind the otherwise protocol-only core to this repository's ledger generation."""
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        raise ClaimError("ledger issue must be a positive integer")
    global LEDGER_ISSUE
    LEDGER_ISSUE = issue


@dataclass(frozen=True)
class IssueComment:
    identifier: int
    created_at: str
    updated_at: str
    body: str
    author_association: str
    url: str


@dataclass(frozen=True)
class IssueIdentity:
    """A claim scoped to one numbered GitHub issue."""

    issue: int

    def __post_init__(self) -> None:
        if isinstance(self.issue, bool) or not isinstance(self.issue, int) or self.issue < 1:
            raise ClaimError("issue identity must be a positive integer")


@dataclass(frozen=True)
class LaneIdentity:
    """A claim scoped to one issueless `docs/`/`fix/` lane.

    Carries no branch of its own: the lane name is owned entirely by the enclosing
    `LedgerActiveClaim`/`ClaimRequest.branch`, which every lane claim already has. A second
    branch-shaped field here would give the branch two owners that could drift apart.
    """


ClaimIdentity = IssueIdentity | LaneIdentity


@dataclass(frozen=True)
class ResourceHold:
    """One named scarce value held by a live claim until land or release."""

    name: str
    value: int


@dataclass(frozen=True)
class LedgerActiveClaim:
    """One claim id's current standing, derived from the whole ledger walk.

    `quarantined_by` is set when a later comment for this same claim id carried
    a field this reader's schema does not know (issue #136): the claim still
    reads. `release`/`pr-check` moved onto the store in this same slice and no
    longer consult it -- this dataclass and its whole aggregation walk are D-scheduled
    infrastructure the import reader (`bootstrap --ledger`) still needs whole, kept
    intact rather than picked apart function by function ahead of that deletion.
    """

    identity: ClaimIdentity
    claim_id: str
    agent: str
    role: str
    base: str
    branch: str
    scope: tuple[str, ...]
    comment: IssueComment
    resource: ResourceHold | None = None
    requested_resource: str | None = None
    whole_reason: str | None = None
    quarantined_by: UnreadableClaim | None = None


@dataclass(frozen=True)
class UnreadableClaim:
    """A trusted claim comment carrying a field this reader's schema does not know.

    A newer `agent-claim` wrote it; this reader fences the record itself and, when
    its `claim_id` matches a claim already active on the ledger, quarantines that
    claim too (issue #136) by setting `LedgerActiveClaim.quarantined_by` -- see there for
    what quarantine refuses. `claim_id` is `None` when the field that would
    normally identify it is itself missing or malformed -- the comment is still
    named by `comment_url` in that case, and it quarantines nothing. Missing a
    field this reader requires is a different, harder failure (a corrupt record,
    not a newer writer): `_strict_keys` still raises `InvalidClaimMarkerError` for
    that and never produces an `UnreadableClaim`.
    """

    claim_id: str | None
    comment_url: str
    unknown_fields: tuple[str, ...]


class UnreadableClaimError(ClaimError):
    """Internal signal that one trusted comment is an `UnreadableClaim`.

    Raised only by `_strict_keys` and caught only by `_aggregate_claim_events`,
    which turns it into an `UnreadableClaim` record on `ClaimLedgerAggregate`
    instead of letting it fail the whole ledger read. It must never escape past
    that one catch site uncaught.
    """

    def __init__(self, claim: UnreadableClaim) -> None:
        self.claim = claim
        super().__init__(
            f"trusted comment {claim.comment_url} unreadable, upgrade the installed tool"
        )


@dataclass(frozen=True)
class ClaimantRelease:
    identity: ClaimIdentity
    claim_id: str
    agent: str
    role: str
    reason: str
    comment: IssueComment


@dataclass(frozen=True)
class OverrideRelease:
    identity: ClaimIdentity
    claim_id: str
    agent: str
    role: str
    reason: str
    claim_comment_id: int
    comment: IssueComment


@dataclass(frozen=True)
class ClaimRescope:
    """Append-only scope replacement for a still-active claim.

    Keeps claim id, identity, agent, role, base, and branch. Old helpers that
    do not know this action fail loud on the whole ledger.

    `whole_reason=None` means "leave the claim's current whole-reason alone" --
    the wire marker simply omits the `whole` field, so an old reader that has
    never heard of clearing a reason still parses this event exactly as before.
    Explicitly dropping a reason back to unset is a third, distinct state a bare
    `None` cannot express (the marker already uses absence for "unchanged"), so
    `clear_whole_reason=True` carries it instead, wire-encoded as a small,
    separate `whole_clear` marker field that is present only for that one
    purpose (issue #136 finding: never both `whole_reason` and
    `clear_whole_reason=True` at once).
    """

    identity: ClaimIdentity
    claim_id: str
    agent: str
    role: str
    scope: tuple[str, ...]
    comment: IssueComment
    whole_reason: str | None = None
    clear_whole_reason: bool = False


@dataclass(frozen=True)
class LedgerSupersede:
    """Terminal event that freezes the whole ledger; always issue(ledger)-scoped.

    No lane variant exists on purpose (Entschieden #6): the ledger itself is the
    numbered issue configured by `configure_ledger`, and a lane claim can never
    own it, so `issue` stays a plain int rather than a `ClaimIdentity`.
    """

    issue: int
    claim_id: str
    agent: str
    role: str
    reason: str
    claim_comment_id: int
    successor_issue: int
    comment: IssueComment


ClaimEvent = LedgerActiveClaim | ClaimantRelease | OverrideRelease | ClaimRescope | LedgerSupersede


class LedgerSupersededError(ClaimError):
    def __init__(self, successor_issue: int, claim: LedgerActiveClaim):
        self.successor_issue = successor_issue
        self.claim = claim
        super().__init__(
            f"claim ledger #{LEDGER_ISSUE} is frozen; update and use successor #{successor_issue}"
        )


@dataclass(frozen=True)
class MergedRelease:
    """A claim released because the named pull request landed on the default branch."""

    pull_request: int

    @property
    def reason(self) -> str:
        return f"merged #{self.pull_request}"


@dataclass(frozen=True)
class AbandonedRelease:
    """A claim released without a landing, and why."""

    explanation: str

    @property
    def reason(self) -> str:
        return f"abandoned: {self.explanation}"


ReleaseOutcome = MergedRelease | AbandonedRelease


@dataclass(frozen=True)
class ClaimRequest:
    identity: ClaimIdentity
    agent: str
    role: str
    base: str
    branch: str
    scope: tuple[str, ...]
    claim_id: str
    out_of_order_reason: str | None = None
    whole_reason: str | None = None
    resource: str | None = None
    resource_value: int | None = None


@dataclass(frozen=True)
class RescopeRequest:
    """CLI-facing rescope input; converted to `RescopeIntent` before `apply`."""

    identity: ClaimIdentity
    agent: str
    add: tuple[str, ...]
    drop: tuple[str, ...]
    claim_id: str | None
    branch: str | None
    whole_reason: str | None = None


class ClaimReader(Protocol):
    """The remaining ledger-comment read: `bootstrap --ledger` import."""

    def list_protocol_candidates(self, issue: int) -> tuple[IssueComment, ...]: ...


class ClaimWriter(ClaimReader, Protocol):
    """`ClaimReader` plus posting a comment (step 6 receipt; import does not post)."""

    def post_comment(self, issue: int, body: str) -> str: ...


def _has_control_character(text: str) -> bool:
    return any(
        ord(character) < ASCII_PRINTABLE_MIN or ord(character) == ASCII_DEL for character in text
    )


def _required_text(payload: dict[str, object], key: str, *, maximum: int = 512) -> str:
    value = payload.get(key)
    if not isinstance(value, str):
        raise InvalidClaimMarkerError(f"claim marker field {key!r} must be text")
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or len(normalized) > maximum
        or _has_control_character(normalized)
    ):
        raise InvalidClaimMarkerError(
            f"claim marker field {key!r} must be one bounded non-empty line"
        )
    return normalized


def _outbound_text(value: object, field: str, *, maximum: int) -> str:
    if not isinstance(value, str):
        raise ClaimError(f"{field} must be text")
    normalized = value.strip()
    if (
        not normalized
        or normalized != value
        or len(normalized) > maximum
        or _has_control_character(normalized)
    ):
        raise ClaimError(f"{field} must be one bounded non-empty line")
    return normalized


def _outbound_resource_name(value: object) -> str:
    name = _outbound_text(value, "resource", maximum=64)
    if RESOURCE_NAME_PATTERN.fullmatch(name) is None:
        raise ClaimError("resource is not a resource name")
    return name


def _required_issue(payload: dict[str, object]) -> int:
    issue = payload.get("issue")
    if isinstance(issue, bool) or not isinstance(issue, int) or issue < 1:
        raise InvalidClaimMarkerError("claim marker issue must be a positive integer")
    return issue


LANE_MARKER_KEY = "lane"


def _required_identity(payload: dict[str, object]) -> ClaimIdentity:
    """Dispatch a claim/release/override_release marker to its identity kind.

    Checks for the lane marker key before ever requiring `issue`, so a lane event
    never triggers `_required_issue`: the natural hard stop for an old reader (which
    always calls `_required_issue` unconditionally) happens exactly there instead of
    in a try/except that would silently skip the comment.
    """
    has_issue = "issue" in payload
    has_lane = LANE_MARKER_KEY in payload
    if has_issue and has_lane:
        raise InvalidClaimMarkerError("claim marker must not carry both issue and lane")
    if has_lane:
        if payload[LANE_MARKER_KEY] is not True:
            raise InvalidClaimMarkerError("claim marker lane field must be true")
        return LaneIdentity()
    return IssueIdentity(_required_issue(payload))


def _identity_marker_key(identity: ClaimIdentity) -> str:
    return LANE_MARKER_KEY if isinstance(identity, LaneIdentity) else "issue"


def _identity_summary(identity: ClaimIdentity, branch: str) -> str:
    """Human-readable subject for a claim error message."""
    return f"lane {branch!r}" if isinstance(identity, LaneIdentity) else f"issue #{identity.issue}"


def _valid_branch(payload: dict[str, object]) -> str:
    branch = _required_text(payload, "branch", maximum=255)
    segments = branch.split("/")
    if (
        BRANCH_PATTERN.fullmatch(branch) is None
        or branch.startswith("-")
        or branch.endswith(("/", "."))
        or ".." in branch
        or "//" in branch
        or "@{" in branch
        or any(
            not segment or segment.startswith(".") or segment.endswith((".", ".lock"))
            for segment in segments
        )
    ):
        raise InvalidClaimMarkerError(f"claim marker branch is not a safe Git ref: {branch!r}")
    return branch


SCOPE_ENTRIES_MUST_BE_CANONICAL = "claim scope entries must be canonical bounded paths"


def _scope_list_entries(scope: object) -> list[str]:
    """Expand a stored or CLI scope list into individual path strings.

    Each list entry may itself be comma-joined: `--scope a,b` equals
    `--scope a --scope b`. Existing ledger markers that stored one opaque
    comma-joined string are read the same way, so overlap detection covers
    them without rewriting the append-only comment.
    """
    if not isinstance(scope, list) or not scope:
        raise InvalidClaimMarkerError("claim marker scope must be a non-empty list")
    expanded: list[str] = []
    for raw_path in scope:
        if not isinstance(raw_path, str):
            raise InvalidClaimMarkerError("claim scope entries must be text")
        if raw_path.strip() != raw_path or not raw_path:
            raise InvalidClaimMarkerError(SCOPE_ENTRIES_MUST_BE_CANONICAL)
        pieces = [piece.strip() for piece in raw_path.split(",")]
        if any(not piece for piece in pieces):
            raise InvalidClaimMarkerError(SCOPE_ENTRIES_MUST_BE_CANONICAL)
        expanded.extend(pieces)
    if len(expanded) > MAX_SCOPE_ENTRIES:
        raise InvalidClaimMarkerError(f"claim marker scope exceeds {MAX_SCOPE_ENTRIES} entries")
    return expanded


def _valid_scope(scope: object) -> tuple[str, ...]:
    result: list[str] = []
    for path in _scope_list_entries(scope):
        if len(path) > MAX_SCOPE_PATH_LENGTH or "\\" in path or _has_control_character(path):
            raise InvalidClaimMarkerError(SCOPE_ENTRIES_MUST_BE_CANONICAL)
        parsed = PurePosixPath(path)
        windows_path = PureWindowsPath(path)
        if (
            path == "."
            or parsed.is_absolute()
            or windows_path.drive
            or windows_path.root
            or ".." in parsed.parts
            or path.startswith("~")
            or not parsed.parts
            or parsed.parts[0] == ".git"
            or str(parsed) != path
        ):
            raise InvalidClaimMarkerError(f"claim scope must be repository-relative: {path!r}")
        result.append(path)
    if len(set(result)) != len(result):
        raise InvalidClaimMarkerError("claim scope contains duplicate paths")
    return tuple(result)


class WideScopeReason(StrEnum):
    """Which of the three wide-scope conditions tripped -- named so a
    refusal can print the one that actually fired instead of restating the
    whole rule."""

    PATH_COUNT = "path_count"
    DIRECTORY = "directory"
    SHARE = "share"


@dataclass(frozen=True)
class WideScopeTrip:
    """The tripped condition, plus the numbers it was judged against -- a
    refusal renders these instead of recomputing them."""

    reason: WideScopeReason
    path_count: int
    directories: tuple[str, ...]
    covered_file_count: int
    versioned_file_count: int


def wide_scope_trip(
    scope: tuple[str, ...],
    *,
    directories: tuple[str, ...],
    covered_file_count: int,
    versioned_file_count: int,
) -> WideScopeTrip | None:
    """Which wide-scope condition fires first, in the rule's own priority
    order -- more than three paths, then any directory, then, once the
    repository has at least `WIDE_SCOPE_SHARE_FLOOR` versioned files, more
    than a quarter of them -- or `None` when scope is not wide."""

    def trip(reason: WideScopeReason) -> WideScopeTrip:
        return WideScopeTrip(
            reason, len(scope), directories, covered_file_count, versioned_file_count
        )

    if len(scope) > WIDE_SCOPE_PATH_LIMIT:
        return trip(WideScopeReason.PATH_COUNT)
    if directories:
        return trip(WideScopeReason.DIRECTORY)
    if versioned_file_count < WIDE_SCOPE_SHARE_FLOOR:
        return None
    if covered_file_count / versioned_file_count > WIDE_SCOPE_SHARE_LIMIT:
        return trip(WideScopeReason.SHARE)
    return None


def _optional_whole_reason(payload: dict[str, object]) -> str | None:
    if "whole" not in payload:
        return None
    return _required_text(payload, "whole", maximum=512)


WHOLE_CLEAR_MARKER_KEY = "whole_clear"


def _rescope_clears_whole_reason(payload: dict[str, object], comment: IssueComment) -> bool:
    """Whether a rescope marker explicitly drops its claim's whole-reason back to
    unset -- a third state a bare `whole_reason=None` cannot carry, since absence
    of `whole` already means "leave it alone" (see `ClaimRescope`)."""
    if WHOLE_CLEAR_MARKER_KEY not in payload:
        return False
    if payload[WHOLE_CLEAR_MARKER_KEY] is not True:
        raise InvalidClaimMarkerError(
            f"trusted comment {comment.url} {WHOLE_CLEAR_MARKER_KEY} field must be true"
        )
    return True


def _claim_id_if_parseable(payload: dict[str, object]) -> str | None:
    """Best-effort `claim_id` for an `UnreadableClaim`: only when it is itself
    well-formed, never validated any further than that."""
    raw = payload.get("claim_id")
    if isinstance(raw, str) and raw.strip() == raw and CLAIM_ID_PATTERN.fullmatch(raw):
        return raw
    return None


def _strict_keys(
    payload: dict[str, object], expected: frozenset[str], comment: IssueComment
) -> None:
    observed = frozenset(payload)
    if observed == expected:
        return
    missing = expected - observed
    if missing:
        raise InvalidClaimMarkerError(
            f"trusted comment {comment.url} claim fields differ: "
            f"expected {sorted(expected)}, got {sorted(observed)}"
        )
    # Every expected field is present; the mismatch is only fields this reader's
    # schema does not know. That is a newer writer, not a corrupt record: fence
    # this one claim instead of failing the whole ledger read (issue #136).
    raise UnreadableClaimError(
        UnreadableClaim(
            claim_id=_claim_id_if_parseable(payload),
            comment_url=comment.url,
            unknown_fields=tuple(sorted(observed - expected)),
        )
    )


def is_protocol_candidate(comment: IssueComment) -> bool:
    first_line = comment.body.partition("\n")[0]
    return comment.author_association in TRUSTED_ASSOCIATIONS and first_line.startswith(
        (LEGACY_MARKER_PREFIX, MARKER_PREFIX)
    )


def _marker_payload(comment: IssueComment) -> tuple[dict[str, object], bool] | None:
    if not is_protocol_candidate(comment):
        return None
    first_line = comment.body.partition("\n")[0]
    # No "neither prefix matched" exit here: `is_protocol_candidate` already
    # requires `first_line` to start with one of the two prefixes, and
    # `legacy` records exactly which one matched -- so `prefix` below is
    # always the one `first_line` was just shown to start with.
    legacy = first_line.startswith(LEGACY_MARKER_PREFIX)
    prefix = LEGACY_MARKER_PREFIX if legacy else MARKER_PREFIX
    if comment.created_at != comment.updated_at:
        raise InvalidClaimMarkerError(
            f"trusted protocol comment {comment.url} was edited after publication"
        )
    if not first_line.endswith(MARKER_SUFFIX):
        raise InvalidClaimMarkerError(
            f"trusted comment {comment.url} has an unterminated claim marker"
        )
    encoded = first_line[len(prefix) : -len(MARKER_SUFFIX)]
    try:
        payload = json.loads(encoded)
    except json.JSONDecodeError as error:
        raise InvalidClaimMarkerError(
            f"trusted comment {comment.url} has invalid claim JSON"
        ) from error
    if not isinstance(payload, dict):
        raise InvalidClaimMarkerError(
            f"trusted comment {comment.url} claim payload must be an object"
        )
    return payload, legacy


def _event_identity(payload: dict[str, object], comment: IssueComment) -> tuple[str, str, str]:
    claim_id = _required_text(payload, "claim_id", maximum=128)
    agent = _required_text(payload, "agent", maximum=128)
    role = _required_text(payload, "role", maximum=64)
    visible_lines = [line for line in comment.body.splitlines() if line.strip()]
    if not visible_lines or visible_lines[-1] != f"Agent: {agent} ({role})":
        raise InvalidClaimMarkerError(
            f"trusted protocol comment {comment.url} lacks its exact agent attribution"
        )
    if CLAIM_ID_PATTERN.fullmatch(claim_id) is None:
        raise InvalidClaimMarkerError(f"trusted comment {comment.url} has an invalid claim id")
    return claim_id, agent, role


def _valid_resource_name(name: str, *, field: str) -> str:
    if RESOURCE_NAME_PATTERN.fullmatch(name) is None:
        raise InvalidClaimMarkerError(f"{field} is not a resource name")
    return name


def _required_resource_hold(payload: dict[str, object], comment: IssueComment) -> ResourceHold:
    name = _required_text(payload, "resource", maximum=64)
    _valid_resource_name(name, field=f"trusted comment {comment.url} resource")
    value = payload.get("resource_value")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidClaimMarkerError(
            f"trusted comment {comment.url} resource_value must be a positive integer"
        )
    return ResourceHold(name, value)


def _parse_active_claim(
    payload: dict[str, object], comment: IssueComment, identity: ClaimIdentity, *, legacy: bool
) -> LedgerActiveClaim:
    expected = {"action", "agent", "base", "branch", "claim_id", "role", "scope"}
    if not legacy:
        expected.add(_identity_marker_key(identity))
    if "resource_value" in payload and "resource" not in payload:
        raise InvalidClaimMarkerError(
            f"trusted comment {comment.url} resource_value requires resource"
        )
    if "resource" in payload:
        expected.add("resource")
        if "resource_value" in payload:
            expected.add("resource_value")
    if "whole" in payload:
        expected.add("whole")
    _strict_keys(payload, frozenset(expected), comment)
    claim_id, agent, role = _event_identity(payload, comment)
    base = _required_text(payload, "base", maximum=40)
    if COMMIT_PATTERN.fullmatch(base) is None:
        raise InvalidClaimMarkerError(
            f"trusted comment {comment.url} base must be a full lowercase commit SHA"
        )
    requested_resource = None
    resource = None
    if "resource" in payload:
        requested_resource = _required_text(payload, "resource", maximum=64)
        _valid_resource_name(requested_resource, field=f"trusted comment {comment.url} resource")
        if "resource_value" in payload:
            resource = _required_resource_hold(payload, comment)
    return LedgerActiveClaim(
        identity=identity,
        claim_id=claim_id,
        agent=agent,
        role=role,
        base=base,
        branch=_valid_branch(payload),
        scope=_valid_scope(payload.get("scope")),
        comment=comment,
        resource=resource,
        requested_resource=requested_resource,
        whole_reason=_optional_whole_reason(payload),
    )


def _parse_claimant_release(
    payload: dict[str, object], comment: IssueComment, identity: ClaimIdentity, *, legacy: bool
) -> ClaimantRelease:
    expected = {"action", "agent", "claim_id", "reason", "role"}
    if not legacy:
        expected.add(_identity_marker_key(identity))
    _strict_keys(payload, frozenset(expected), comment)
    claim_id, agent, role = _event_identity(payload, comment)
    return ClaimantRelease(
        identity=identity,
        claim_id=claim_id,
        agent=agent,
        role=role,
        reason=_required_text(payload, "reason", maximum=512),
        comment=comment,
    )


def _required_comment_id(payload: dict[str, object], *, action: str) -> int:
    raw_comment_id = payload.get("claim_comment_id")
    if (
        isinstance(raw_comment_id, bool)
        or not isinstance(raw_comment_id, int)
        or raw_comment_id < 1
    ):
        raise InvalidClaimMarkerError(f"{action} requires a positive claim comment id")
    return raw_comment_id


def _parse_override_release(
    payload: dict[str, object], comment: IssueComment, identity: ClaimIdentity
) -> OverrideRelease:
    _strict_keys(
        payload,
        frozenset(
            {
                "action",
                "agent",
                "claim_comment_id",
                "claim_id",
                _identity_marker_key(identity),
                "reason",
                "role",
            }
        ),
        comment,
    )
    claim_id, agent, role = _event_identity(payload, comment)
    if role != "coordinator":
        raise InvalidClaimMarkerError("override releases require coordinator role")
    return OverrideRelease(
        identity=identity,
        claim_id=claim_id,
        agent=agent,
        role=role,
        reason=_required_text(payload, "reason", maximum=512),
        claim_comment_id=_required_comment_id(payload, action="override releases"),
        comment=comment,
    )


def _parse_ledger_supersede(
    payload: dict[str, object], comment: IssueComment, issue: int
) -> LedgerSupersede:
    _strict_keys(
        payload,
        frozenset(
            {
                "action",
                "agent",
                "claim_comment_id",
                "claim_id",
                "issue",
                "reason",
                "role",
                "successor_issue",
            }
        ),
        comment,
    )
    claim_id, agent, role = _event_identity(payload, comment)
    if role != "coordinator":
        raise InvalidClaimMarkerError("ledger supersede requires coordinator role")
    successor_issue = payload.get("successor_issue")
    if (
        isinstance(successor_issue, bool)
        or not isinstance(successor_issue, int)
        or successor_issue < 1
        or successor_issue <= LEDGER_ISSUE
    ):
        raise InvalidClaimMarkerError("ledger successor must be greater than the current ledger")
    return LedgerSupersede(
        issue=issue,
        claim_id=claim_id,
        agent=agent,
        role=role,
        reason=_required_text(payload, "reason", maximum=512),
        claim_comment_id=_required_comment_id(payload, action="ledger supersede"),
        successor_issue=successor_issue,
        comment=comment,
    )


def _parse_claim_rescope(
    payload: dict[str, object], comment: IssueComment, identity: ClaimIdentity
) -> ClaimRescope:
    expected = {
        "action",
        "agent",
        "claim_id",
        _identity_marker_key(identity),
        "role",
        "scope",
    }
    if "whole" in payload and WHOLE_CLEAR_MARKER_KEY in payload:
        raise InvalidClaimMarkerError(
            f"trusted comment {comment.url} rescope cannot both set and clear the whole reason"
        )
    if "whole" in payload:
        expected.add("whole")
    if WHOLE_CLEAR_MARKER_KEY in payload:
        expected.add(WHOLE_CLEAR_MARKER_KEY)
    _strict_keys(payload, frozenset(expected), comment)
    claim_id, agent, role = _event_identity(payload, comment)
    return ClaimRescope(
        identity=identity,
        claim_id=claim_id,
        agent=agent,
        role=role,
        scope=_valid_scope(payload.get("scope")),
        comment=comment,
        whole_reason=_optional_whole_reason(payload),
        clear_whole_reason=_rescope_clears_whole_reason(payload, comment),
    )


def parse_claim_event(comment: IssueComment) -> ClaimEvent | None:
    parsed_marker = _marker_payload(comment)
    if parsed_marker is None:
        return None
    payload, legacy = parsed_marker
    action = _required_text(payload, "action", maximum=32)
    if action not in {"claim", "release", "override_release", "rescope", "supersede"}:
        raise InvalidClaimMarkerError(
            f"trusted comment {comment.url} has unknown action {action!r}"
        )

    if legacy:
        if action not in {"claim", "release"}:
            raise InvalidClaimMarkerError("legacy claim markers cannot use this action")
        if LEDGER_ISSUE < 1:
            # IssueIdentity itself would reject 0 as "not a positive integer", which
            # would misreport this as a marker defect; it is a caller/setup defect.
            raise ClaimError(
                "legacy claim marker cannot be parsed before configure_ledger "
                "binds the ledger issue"
            )
        identity: ClaimIdentity = IssueIdentity(LEDGER_ISSUE)
    elif action == "supersede":
        # Supersede stays ledger-issue-only (Entschieden #6): no lane branching here,
        # so it returns straight from the issue number rather than routing through
        # `identity` -- the shared union below never carries a supersede action.
        return _parse_ledger_supersede(payload, comment, _required_issue(payload))
    else:
        identity = _required_identity(payload)
    if action == "claim":
        return _parse_active_claim(payload, comment, identity, legacy=legacy)
    if action == "release":
        return _parse_claimant_release(payload, comment, identity, legacy=legacy)
    if action == "override_release":
        return _parse_override_release(payload, comment, identity)
    return _parse_claim_rescope(payload, comment, identity)


def _apply_terminal_event(
    event: ClaimantRelease | OverrideRelease | LedgerSupersede,
    active: dict[str, LedgerActiveClaim],
    acquired: dict[str, LedgerActiveClaim],
) -> bool:
    """Validate and apply a terminal event against the claim it targets.

    Returns whether the engine honored the event: accepted it as a real terminal
    event for the claim `acquired` currently holds for this id, whether or not
    popping `active` actually changed anything (an idempotent release retry, or a
    coordinator override after the claimant already released, still counts as
    honored). A `LedgerSupersede` that does not satisfy its narrow freeze window is
    never honored — it silently no-ops without being validated against any claim.
    """
    claimed = acquired.get(event.claim_id)
    if isinstance(event, LedgerSupersede):
        if (
            claimed is None
            or not isinstance(claimed.identity, IssueIdentity)
            or claimed.identity.issue != event.issue
            or claimed.identity.issue != LEDGER_ISSUE
            or event.claim_comment_id != claimed.comment.identifier
            or set(active) != {claimed.claim_id}
        ):
            return False
        raise LedgerSupersededError(event.successor_issue, claimed)
    if claimed is None:
        raise InvalidClaimMarkerError(
            f"claim id {event.claim_id!r} was released before it was acquired"
        )
    if claimed.identity != event.identity:
        raise InvalidClaimMarkerError(
            f"claim id {event.claim_id!r} release targets the wrong claim"
        )
    if isinstance(event, ClaimantRelease):
        if (claimed.agent, claimed.role) != (event.agent, event.role):
            raise InvalidClaimMarkerError(
                f"claim id {event.claim_id!r} can only be released by its claimant"
            )
    elif event.claim_comment_id != claimed.comment.identifier:
        raise InvalidClaimMarkerError(
            f"claim id {event.claim_id!r} terminal event targets the wrong claim comment"
        )
    active.pop(event.claim_id, None)
    return True


@dataclass(frozen=True)
class ClaimLedgerAggregate:
    """Non-raising result of one chronological walk over the ledger's claim events.

    `occurrences` holds every LedgerActiveClaim event seen for a claim id, in ledger order;
    a duplicated id has more than one. `terminated_by`, when present for a claim id,
    holds every terminal-event comment `_apply_terminal_event` honored for it — a
    release retry or a coordinator override that lands after the claimant already
    released both still count, since the engine validated and accepted each one.
    An inert or foreign terminal event (one `_apply_terminal_event` never honored,
    such as a `LedgerSupersede` posted outside its narrow freeze window) never shows
    up here. Because `acquired` (below) only ever binds a claim id to its first-ever
    occurrence, every honored termination belongs to `occurrences[claim_id][0]`;
    later occurrences of a duplicated id are never tracked as "acquired" and so can
    never absorb a terminal event themselves.

    `unreadable` holds one `UnreadableClaim` per trusted comment the walk could not
    parse because of an unknown field (issue #136); such a comment contributes no
    event of its own, so it never appears in `occurrences` or `terminated_by`. It
    can still reach `active` indirectly: when its `claim_id` names a claim that is
    already active, that claim is carried into `active` with `quarantined_by` set
    to this record instead of `None`.
    """

    active: tuple[LedgerActiveClaim, ...]
    seen_claim_ids: frozenset[str]
    duplicate_claim_ids: tuple[str, ...]
    occurrences: Mapping[str, tuple[LedgerActiveClaim, ...]]
    terminated_by: Mapping[str, tuple[IssueComment, ...]]
    unreadable: tuple[UnreadableClaim, ...]


def _record_claim_occurrence(
    event: LedgerActiveClaim,
    active: dict[str, LedgerActiveClaim],
    acquired: dict[str, LedgerActiveClaim],
    occurrences: dict[str, list[LedgerActiveClaim]],
    duplicate_claim_ids: list[str],
) -> None:
    occurrences.setdefault(event.claim_id, []).append(event)
    if event.claim_id in acquired:
        if event.claim_id not in duplicate_claim_ids:
            duplicate_claim_ids.append(event.claim_id)
        return
    acquired[event.claim_id] = event
    active[event.claim_id] = event


def _apply_claim_rescope_event(
    event: ClaimRescope,
    active: dict[str, LedgerActiveClaim],
    acquired: dict[str, LedgerActiveClaim],
) -> None:
    current = active.get(event.claim_id)
    if current is None:
        if event.claim_id in acquired:
            raise InvalidClaimMarkerError(
                f"claim id {event.claim_id!r} was rescoped after it was released"
            )
        raise InvalidClaimMarkerError(
            f"claim id {event.claim_id!r} was rescoped before it was acquired"
        )
    if current.identity != event.identity:
        raise InvalidClaimMarkerError(
            f"claim id {event.claim_id!r} rescope targets the wrong claim"
        )
    if (current.agent, current.role) != (event.agent, event.role):
        raise InvalidClaimMarkerError(
            f"claim id {event.claim_id!r} can only be rescoped by its claimant"
        )
    if event.clear_whole_reason:
        new_whole_reason = None
    elif event.whole_reason is not None:
        new_whole_reason = event.whole_reason
    else:
        new_whole_reason = current.whole_reason
    active[event.claim_id] = replace(current, scope=event.scope, whole_reason=new_whole_reason)


def _quarantine_active_claims(
    active: dict[str, LedgerActiveClaim], unreadable: list[UnreadableClaim]
) -> dict[str, LedgerActiveClaim]:
    """Attach the earliest matching `UnreadableClaim` to the active claim it names.

    A quarantined claim id has no live `LedgerActiveClaim` to attach to when the
    unreadable comment is itself the newer writer's `claim` (there was never a
    readable claim under that id); it only matters, and only changes anything
    here, when the id already names a claim this reader did parse -- e.g. a
    newer writer's `rescope` of an existing claim (issue #136 finding 1).
    """
    reasons: dict[str, UnreadableClaim] = {}
    for record in unreadable:
        if record.claim_id is not None:
            reasons.setdefault(record.claim_id, record)
    if not reasons:
        return active
    return {
        claim_id: (
            replace(claim, quarantined_by=reasons[claim_id]) if claim_id in reasons else claim
        )
        for claim_id, claim in active.items()
    }


def _aggregate_claim_events(comments: tuple[IssueComment, ...]) -> ClaimLedgerAggregate:
    """Walk the ledger once, tolerating a reused claim id instead of raising on sight.

    The strict reader, the pre/post acquire guards, and the reconcile repair pass all
    consume this single walk, so duplicate-claim-id detection and release status can
    never drift between two independently maintained parsers.
    """
    active: dict[str, LedgerActiveClaim] = {}
    acquired: dict[str, LedgerActiveClaim] = {}
    occurrences: dict[str, list[LedgerActiveClaim]] = {}
    terminated_by: dict[str, list[IssueComment]] = {}
    duplicate_claim_ids: list[str] = []
    unreadable: list[UnreadableClaim] = []
    ordered = sorted(comments, key=lambda comment: (comment.created_at, comment.identifier))
    for comment in ordered:
        try:
            event = parse_claim_event(comment)
        except UnreadableClaimError as error:
            unreadable.append(error.claim)
            continue
        if event is None:
            continue
        if isinstance(event, LedgerActiveClaim):
            _record_claim_occurrence(event, active, acquired, occurrences, duplicate_claim_ids)
            continue
        if isinstance(event, ClaimRescope):
            _apply_claim_rescope_event(event, active, acquired)
            continue
        if _apply_terminal_event(event, active, acquired):
            terminated_by.setdefault(event.claim_id, []).append(event.comment)

    occurrence_map = {claim_id: tuple(events) for claim_id, events in occurrences.items()}
    derived_active = _apply_derived_resource_holds(active, occurrence_map)
    quarantined_active = _quarantine_active_claims(derived_active, unreadable)
    return ClaimLedgerAggregate(
        active=tuple(
            sorted(
                quarantined_active.values(),
                key=lambda event: (event.comment.created_at, event.comment.identifier),
            )
        ),
        seen_claim_ids=frozenset(acquired),
        duplicate_claim_ids=tuple(duplicate_claim_ids),
        occurrences=MappingProxyType(occurrence_map),
        terminated_by=MappingProxyType(
            {
                claim_id: tuple(terminal_comments)
                for claim_id, terminal_comments in terminated_by.items()
            }
        ),
        unreadable=tuple(unreadable),
    )


def _assign_resource_values(
    derived: dict[str, LedgerActiveClaim], first_occurrences: list[LedgerActiveClaim], name: str
) -> tuple[int, ...]:
    """Occupy `name`'s posted values, then fill auto intents with the next free integer.

    Returns the occupied set for this name, including released autos and
    explicit posts -- the same first-occurrence walk the import reader
    writes to `resources/<name>.toml` (issue #176 done-when 2).
    """
    intents = sorted(
        (event for event in first_occurrences if event.requested_resource == name),
        key=lambda event: (event.comment.created_at, event.comment.identifier),
    )
    occupied: set[int] = set()
    for intent in intents:
        if intent.resource is not None:
            occupied.add(intent.resource.value)
            continue
        value = 1
        while value in occupied:
            value += 1
        occupied.add(value)
        current = derived.get(intent.claim_id)
        if current is None:
            continue
        derived[intent.claim_id] = replace(current, resource=ResourceHold(name, value))
    return tuple(sorted(occupied))


def _occupied_resources_from_occurrences(
    occurrences: Mapping[str, tuple[LedgerActiveClaim, ...]],
) -> dict[str, tuple[int, ...]]:
    """Occupied integers per resource name from the first-occurrence walk.

    Drives the import reader's `resources/<name>.toml` (done-when 2): a
    released auto and a released explicit post both stay occupied. The walk
    is `_assign_resource_values` itself, against an empty derived map so
    released claims still occupy without needing a live holder.
    """
    first_occurrences = [events[0] for events in occurrences.values()]
    names = sorted(
        {
            event.requested_resource
            for event in first_occurrences
            if event.requested_resource is not None
        }
    )
    derived: dict[str, LedgerActiveClaim] = {}
    return {name: _assign_resource_values(derived, first_occurrences, name) for name in names}


def _group_active_holders(
    derived: dict[str, LedgerActiveClaim],
) -> dict[tuple[str, int], list[LedgerActiveClaim]]:
    holders: dict[tuple[str, int], list[LedgerActiveClaim]] = {}
    for claim in derived.values():
        if claim.resource is not None:
            holders.setdefault((claim.resource.name, claim.resource.value), []).append(claim)
    return holders


def _strip_duplicate_holders(
    derived: dict[str, LedgerActiveClaim], holders: dict[tuple[str, int], list[LedgerActiveClaim]]
) -> None:
    """Among claims that live for the same (name, value), keep only the earliest holder.

    The loser here is always an explicit-value claim: `_assign_resource_values`
    walks intents for one name in the same chronological order this sorts by,
    so whichever claim first occupies a value is exactly this group's
    earliest member, and only the explicit branch there ever occupies a value
    without first checking it isn't already taken -- an auto intent always
    picks a value not yet occupied, so it can never itself be a later,
    colliding loser.
    """
    for held in holders.values():
        ordered = sorted(
            held, key=lambda claim: (claim.comment.created_at, claim.comment.identifier)
        )
        for loser in ordered[1:]:
            derived[loser.claim_id] = replace(loser, resource=None)


def _apply_derived_resource_holds(
    active: dict[str, LedgerActiveClaim],
    occurrences: Mapping[str, tuple[LedgerActiveClaim, ...]],
) -> dict[str, LedgerActiveClaim]:
    """Assign auto resource values from occupied first-occurrence intents.

    Walk first-occurrence intents for a name in ledger order. Explicit intents occupy
    their posted value, including after release. Auto intents take the next positive
    integer not already occupied; a released auto still occupies the integer it would
    have been assigned. Among still-active claims, only the earliest live (name, value)
    pair stays the holder — later live explicit posts of that pair are stripped.
    """
    derived = dict(active)
    first_occurrences = [events[0] for events in occurrences.values()]
    names = sorted(
        {
            event.requested_resource
            for event in first_occurrences
            if event.requested_resource is not None
        }
    )
    for name in names:
        _assign_resource_values(derived, first_occurrences, name)
    holders = _group_active_holders(derived)
    _strip_duplicate_holders(derived, holders)
    return derived


def _reject_duplicate_claim_ids(aggregate: ClaimLedgerAggregate) -> None:
    if aggregate.duplicate_claim_ids:
        raise InvalidClaimMarkerError(f"claim id {aggregate.duplicate_claim_ids[0]!r} was reused")


def active_claims(comments: tuple[IssueComment, ...]) -> tuple[LedgerActiveClaim, ...]:
    aggregate = _aggregate_claim_events(comments)
    _reject_duplicate_claim_ids(aggregate)
    return aggregate.active


def _unreadable_claim_reason(record: UnreadableClaim) -> str:
    """Shared refusal core naming one `UnreadableClaim`'s comment and unknown field
    names -- every fail-closed refusal this reader raises for it quotes this text
    verbatim (issue #136)."""
    subject = f"claim {record.claim_id!r}" if record.claim_id else "an unreadable claim"
    fields = ", ".join(sorted(record.unknown_fields))
    return f"{subject} at {record.comment_url} is unreadable (unknown fields: {fields})"


def _reject_unreadable_claims(aggregate: ClaimLedgerAggregate, *, action: str) -> None:
    """Fail closed (issue #136): an unreadable claim's true scope is unknowable to
    this reader, so no new `claim` or `rescope` can be proven not to overlap it."""
    blocker = next(iter(aggregate.unreadable), None)
    if blocker is None:
        return
    raise ClaimUnavailableError(
        f"{action} refused: {_unreadable_claim_reason(blocker)}; upgrade the installed "
        "tool before claiming a scope that could overlap it"
    )


def _scope_prefixes(paths: tuple[str, ...]) -> set[tuple[str, ...]]:
    prefixes: set[tuple[str, ...]] = set()
    for path in paths:
        parts = PurePosixPath(path).parts
        prefixes.update(parts[:length] for length in range(1, len(parts) + 1))
    return prefixes


def _scopes_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    left_paths = {PurePosixPath(path).parts for path in left}
    right_paths = {PurePosixPath(path).parts for path in right}
    return bool(
        left_paths.intersection(_scope_prefixes(right))
        or right_paths.intersection(_scope_prefixes(left))
    )


class ScopedClaim(Protocol):
    """The structural shape the conflict/overlap helpers need.

    Satisfied by both the ledger's `LedgerActiveClaim`/`ClaimRequest` pair and
    the store's `ActiveClaim`/`ClaimIntent` pair (issue #176, slice C2), so
    one set of pure functions serves both instead of a nominal union that
    would grow every time a new claim-shaped record is added. Read-only
    properties, not plain attributes: a frozen dataclass field is itself
    read-only, and `claim_id` varies covariantly (`str` here, `ClaimId` on
    the store's records) between the two implementations this protocol
    covers.
    """

    @property
    def identity(self) -> ClaimIdentity: ...
    @property
    def claim_id(self) -> str: ...
    @property
    def agent(self) -> str: ...
    @property
    def role(self) -> str: ...
    @property
    def branch(self) -> str: ...
    @property
    def scope(self) -> tuple[str, ...]: ...


def _identity_conflicts(left: ScopedClaim, right: ScopedClaim) -> bool:
    """Two claims share an identity: same issue number, or same lane branch.

    A lane claim and an issue claim never share an identity by themselves — only
    scope overlap can put them in conflict.
    """
    match left.identity, right.identity:
        case IssueIdentity(issue=left_issue), IssueIdentity(issue=right_issue):
            return left_issue == right_issue
        case LaneIdentity(), LaneIdentity():
            return left.branch == right.branch
        case _:
            return False


def claims_conflict(left: ScopedClaim, right: ScopedClaim) -> bool:
    """True when two claims share an issue or lane branch.

    Path overlap is advisory: it is a visible note, not a conflict.
    """
    return _identity_conflicts(left, right)


def claims_overlap(left: ScopedClaim, right: ScopedClaim) -> bool:
    return _scopes_overlap(left.scope, right.scope)


def claims_holding_path(claims: tuple[_ScopedClaimT, ...], path: str) -> tuple[_ScopedClaimT, ...]:
    target = _valid_scope([path])
    if len(target) != 1:
        raise ClaimError("status --path requires a single repository-relative path")
    return tuple(claim for claim in claims if _scopes_overlap(claim.scope, target))


_ScopedClaimT = TypeVar("_ScopedClaimT", bound=ScopedClaim)


def blocking_claims(
    claims: tuple[_ScopedClaimT, ...], candidate: ScopedClaim
) -> tuple[_ScopedClaimT, ...]:
    return tuple(
        claim
        for claim in claims
        if claim.claim_id != candidate.claim_id and claims_conflict(claim, candidate)
    )


def overlapping_claims(
    claims: tuple[_ScopedClaimT, ...], candidate: ScopedClaim
) -> tuple[_ScopedClaimT, ...]:
    return tuple(
        claim
        for claim in claims
        if claim.claim_id != candidate.claim_id and claims_overlap(claim, candidate)
    )


def conflicting_claims(
    claims: tuple[_ScopedClaimT, ...], candidate: ScopedClaim
) -> tuple[_ScopedClaimT, ...]:
    """Path-overlapping live claims, excluding identity. Used as the advisory note."""
    return overlapping_claims(claims, candidate)


IdentityKey = tuple[str, int | str]


def _identity_key(claim: ScopedClaim) -> IdentityKey:
    """Hashable index key for one claim's identity: an issue number or a lane branch.

    `LaneIdentity` instances all compare equal to each other, so indexing by
    identity alone would merge every lane into one bucket; the branch already owned
    by `LedgerActiveClaim.branch` supplies the missing distinction without giving the
    lane name a second owner.
    """
    if isinstance(claim.identity, LaneIdentity):
        return (LANE_MARKER_KEY, claim.branch)
    return ("issue", claim.identity.issue)


@dataclass(frozen=True)
class ClaimConflictIndex:
    conflict_ids: set[str]
    overlap_ids: set[str]
    claims_by_identity: dict[IdentityKey, set[str]]
    complete_paths: dict[tuple[str, ...], set[str]]
    descendant_paths: dict[tuple[str, ...], set[str]]


def _claim_conflict_index(claims: tuple[ScopedClaim, ...]) -> ClaimConflictIndex:
    """Index identities and paths once for status conflict and overlap notes."""
    conflict_ids: set[str] = set()
    overlap_ids: set[str] = set()
    claims_by_identity: dict[IdentityKey, set[str]] = {}
    complete_paths: dict[tuple[str, ...], set[str]] = {}
    descendant_paths: dict[tuple[str, ...], set[str]] = {}

    for claim in claims:
        same_identity = claims_by_identity.setdefault(_identity_key(claim), set())
        if same_identity:
            conflict_ids.add(claim.claim_id)
            conflict_ids.update(same_identity)
        same_identity.add(claim.claim_id)

        for path in claim.scope:
            parts = PurePosixPath(path).parts
            matches = set(descendant_paths.get(parts, ()))
            for length in range(1, len(parts) + 1):
                matches.update(complete_paths.get(parts[:length], ()))
            matches.discard(claim.claim_id)
            if matches:
                overlap_ids.add(claim.claim_id)
                overlap_ids.update(matches)

            complete_paths.setdefault(parts, set()).add(claim.claim_id)
            for length in range(1, len(parts) + 1):
                descendant_paths.setdefault(parts[:length], set()).add(claim.claim_id)

    return ClaimConflictIndex(
        conflict_ids,
        overlap_ids,
        claims_by_identity,
        complete_paths,
        descendant_paths,
    )


def _related_claim_ids(index: ClaimConflictIndex, selected: tuple[ScopedClaim, ...]) -> set[str]:
    related = {claim.claim_id for claim in selected}
    for claim in selected:
        related.update(index.claims_by_identity[_identity_key(claim)])
        related.update(_overlap_peer_ids(index, claim))
    return related


def _overlap_peer_ids(index: ClaimConflictIndex, claim: ScopedClaim) -> set[str]:
    related: set[str] = set()
    for path in claim.scope:
        parts = PurePosixPath(path).parts
        related.update(index.descendant_paths.get(parts, ()))
        for length in range(1, len(parts) + 1):
            related.update(index.complete_paths.get(parts[:length], ()))
    related.discard(claim.claim_id)
    return related


def _combined_scope(
    current: tuple[str, ...], add: tuple[str, ...], drop: tuple[str, ...]
) -> tuple[str, ...]:
    current_set = set(current)
    missing = next((path for path in drop if path not in current_set), None)
    if missing is not None:
        raise ClaimUnavailableError(f"cannot drop {missing!r}; it is not in this claim's scope")
    drop_set = set(drop)
    kept = tuple(path for path in current if path not in drop_set)
    added = tuple(path for path in add if path not in kept)
    if not kept and not added:
        raise ClaimUnavailableError("rescope must leave a non-empty scope")
    combined = kept + added
    if combined == current:
        raise ClaimUnavailableError("rescope does not change the claim scope")
    return _valid_scope(list(combined))


def _require_coordinator_override(role: str | None) -> None:
    if role != "coordinator":
        raise ClaimUnavailableError("a coordinator override requires --role coordinator")


# --- refs/aco/state: the claim-state tree (issue #164, slice C1) ----------
#
# `store.py` owns the git transport (ls-remote, fetch, plumbing, push,
# retry); this module stays pure and owns only the tree's types and its
# `schema.toml` codec. C1 writes and reads exactly one file, `schema.toml`;
# `claims/`, `ids/`, and `resources/` are C2's `apply`.

SUPPORTED_STATE_SCHEMA_VERSION = 1


class UnsupportedStateSchemaError(ClaimError):
    """`schema.toml` names a version this client does not speak."""


class MalformedStateTreeError(ClaimError):
    """The fetched state tree is not a well-formed `schema.toml`-only tree."""


class StateLineageError(ClaimError):
    """A worktree's own last-observed tip is not an ancestor of the fetched tip.

    Distinct from `MalformedStateTreeError`: the tree itself may parse fine --
    it is this client's history of the ref that no longer lines up, which
    `git push --force` recovery (documented, never automatic) is the only
    sanctioned way to cause.
    """


class PushRejectedError(ClaimError):
    """The store's push boundary did not observably land the pushed commit.

    Raised for an ordinary non-fast-forward rejection and for a lost response
    after the remote actually advanced -- the retry loop in `store.py`
    handles both by re-fetching and looking for its own `operation_id`
    (criterion 3), so the two causes need no separate types.
    """


class ObjectId(str):
    """A runtime-validated 40-character lowercase hex git object id.

    A plain `NewType` would still accept any string at runtime; every value
    that reaches here comes from parsing git's own output or an untrusted
    fetch, so the validation belongs on construction, not on trust.
    """

    def __new__(cls, value: str) -> ObjectId:
        if COMMIT_PATTERN.fullmatch(value) is None:
            raise MalformedStateTreeError(f"not a git object id: {value!r}")
        return super().__new__(cls, value)


class ClaimId(str):
    """A runtime-validated claim id (`CLAIM_ID_PATTERN`), the store's own key.

    Same rationale as `ObjectId`: every value reaching here comes from a CLI
    argument, a generated `uuid4().hex`, or an untrusted fetched tree, never
    from a value this module already trusts.
    """

    def __new__(cls, value: str) -> ClaimId:
        if CLAIM_ID_PATTERN.fullmatch(value) is None:
            raise ClaimError(f"not a valid claim id: {value!r}")
        return super().__new__(cls, value)


COORDINATOR_ROLE = "coordinator"


@dataclass(frozen=True)
class ActingIdentity:
    """The agent and role acting on a transition (criterion 10: identity
    carries both). `role` stays an open string -- `COORDINATOR_ROLE` is the
    one value `apply` treats specially, for a coordinator override."""

    agent: str
    role: str


# --- Claim key codec (criterion 10, issue #176 slice C2) -------------------
#
# One path segment, no `/`, collision-free and reversible -- the only owner
# of a claims/resources/ids tree entry's file name. `apply` is the codec's
# first production caller; `store.py` calls `claim_key`/`parse_claim_key`
# when it writes or reads a `claims/<key>.toml` tree entry.

_ISSUE_KEY_PREFIX = "issue-"
_LANE_KEY_PREFIX = "lane-"
_LANE_KEY_UNRESERVED = frozenset(
    b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
)
_PERCENT_ESCAPE_PATTERN = re.compile("[0-9A-Fa-f]{2}")


def _percent_encode_branch(branch: str) -> str:
    """RFC 3986 percent-encoding with an empty safe set: every byte but the
    unreserved set becomes `%HH`, so `/` and `%` themselves are always
    escaped and can never reappear literally in the encoded key."""
    encoded = bytearray()
    for byte in branch.encode("utf-8"):
        if byte in _LANE_KEY_UNRESERVED:
            encoded.append(byte)
        else:
            encoded.extend(f"%{byte:02X}".encode("ascii"))
    return encoded.decode("ascii")


def _percent_decode_branch(encoded: str) -> str:
    raw = bytearray()
    index = 0
    while index < len(encoded):
        character = encoded[index]
        if character != "%":
            raw.extend(character.encode("utf-8"))
            index += 1
            continue
        hex_digits = encoded[index + 1 : index + 3]
        if not _PERCENT_ESCAPE_PATTERN.fullmatch(hex_digits):
            raise MalformedStateTreeError(f"claim key has a malformed percent-escape: {encoded!r}")
        raw.append(int(hex_digits, 16))
        index += 3
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MalformedStateTreeError(f"claim key does not decode as utf-8: {encoded!r}") from error


def claim_key(identity: ClaimIdentity, branch: str) -> str:
    """The one collision-free, reversible tree-entry name for this identity.

    `issue-{n}` for an issue claim; `lane-{percent-encoded branch}` for a
    lane claim. The two prefixes can never collide with each other's payload
    (`issue-1` vs a lane branch literally named `issue-1`, which encodes to
    `lane-issue-1`), because `parse_claim_key` below dispatches on the
    prefix alone before ever looking at the payload.
    """
    if isinstance(identity, IssueIdentity):
        return f"{_ISSUE_KEY_PREFIX}{identity.issue}"
    return f"{_LANE_KEY_PREFIX}{_percent_encode_branch(branch)}"


def parse_claim_key(key: str) -> ClaimIdentity:
    """Invert `claim_key`. Refuses any prefix but the two the codec writes."""
    if key.startswith(_ISSUE_KEY_PREFIX):
        digits = key[len(_ISSUE_KEY_PREFIX) :]
        if not digits or not digits.isdigit() or digits[0] == "0":
            raise MalformedStateTreeError(f"claim key has a malformed issue number: {key!r}")
        return IssueIdentity(int(digits))
    if key.startswith(_LANE_KEY_PREFIX):
        # Validate only; the decoded branch itself lives in the claim file.
        _percent_decode_branch(key[len(_LANE_KEY_PREFIX) :])
        return LaneIdentity()
    raise MalformedStateTreeError(f"claim key has neither the issue nor lane prefix: {key!r}")


@dataclass(frozen=True)
class ResourceRecord:
    """`resources/<name>.toml`: the never-reuse set for one resource name.

    `occupied` never shrinks: a released value stays in it forever (today's
    contract), so neither an auto nor an explicit intent can ever reassign a
    value this resource has already given out.
    """

    name: str
    occupied: tuple[int, ...]


@dataclass(frozen=True)
class ActiveClaim:
    """One live claim as the store's `apply` maintains it (issue #176, §1).

    Distinct from the ledger's `LedgerActiveClaim`: no `comment`,
    `requested_resource`, or `quarantined_by` -- those were comment-schema
    concerns the git-tree store has no equivalent of. `opened_commit` is the
    state-ref commit this claim_id was first introduced at; rescope never
    changes it.
    """

    identity: ClaimIdentity
    claim_id: ClaimId
    agent: str
    role: str
    base: ObjectId
    branch: str
    scope: tuple[str, ...]
    opened_commit: ObjectId
    resource: ResourceHold | None = None
    whole_reason: str | None = None


@dataclass(frozen=True)
class ClaimState:
    """The claim-state tree as observed at one `refs/aco/state` commit.

    `tip` is `None` only for `EMPTY_STATE`, standing in for a ref that does
    not exist yet. `claims`, `consumed_ids`, and `resources` are C2's
    addition alongside `apply`: immutable collections (`MappingProxyType`,
    `frozenset`) inside this frozen state, never mutated in place -- every
    transition builds and returns a whole new `ClaimState`.
    """

    tip: ObjectId | None
    claims: Mapping[str, ActiveClaim] = MappingProxyType({})
    consumed_ids: frozenset[ClaimId] = frozenset()
    resources: Mapping[str, ResourceRecord] = MappingProxyType({})


EMPTY_STATE = ClaimState(tip=None)


@dataclass(frozen=True)
class OperationAlreadyApplied:
    """The push-retry loop found this `operation_id` already on the fetched tip.

    Not an error: a concurrent writer's push landed, so this client's own
    request is already satisfied and must not be re-applied.
    """

    tip: ObjectId


def serialize_empty_schema_toml() -> str:
    """The only `schema.toml` content C1 ever writes."""
    return f"version = {SUPPORTED_STATE_SCHEMA_VERSION}\n"


def parse_schema_toml(content: str, *, tip: ObjectId) -> ClaimState:
    """Parse one fetched commit's `schema.toml` blob into its `ClaimState`.

    Raises `MalformedStateTreeError` for anything but exactly one integer
    `version` key, and `UnsupportedStateSchemaError` for a version this
    client does not speak -- kept distinct from a malformed tree because a
    later, genuinely well-formed schema is a new cut, not a corrupt read.
    """
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as error:
        raise MalformedStateTreeError(f"malformed schema.toml at {tip}: {error}") from error
    if set(data) != {"version"}:
        raise MalformedStateTreeError(f"schema.toml at {tip} must contain exactly 'version'")
    version = data["version"]
    if isinstance(version, bool) or not isinstance(version, int):
        raise MalformedStateTreeError(f"schema.toml version must be an integer, got {version!r}")
    if version != SUPPORTED_STATE_SCHEMA_VERSION:
        raise UnsupportedStateSchemaError(f"unsupported state schema version {version}")
    return ClaimState(tip=tip)


# --- Claim transitions: intents and `apply` (issue #176, slice C2) ---------
#
# `apply` is the sole writer of `ClaimState.claims`/`consumed_ids`/`resources`.
# It is pure: no git, no clock, no randomness. `store.py`'s commit loop is its
# one production caller, and refuses every write-path command before this
# ever runs against `EMPTY_STATE` -- only `bootstrap` may create the ref
# itself (done-when: a missing ref is never created as a side effect of a
# claim/rescope/release/override/takeover/resource/protect call).


@dataclass(frozen=True)
class ClaimIntent:
    """A `claim` transition: adds `claims/<key>.toml`, `ids/<claim_id>`, and
    maybe creates or updates `resources/<name>.toml`. Any role may claim;
    identity and resource uniqueness are enforced by `apply` itself."""

    identity: ClaimIdentity
    agent: str
    role: str
    base: ObjectId
    branch: str
    scope: tuple[str, ...]
    claim_id: ClaimId
    operation_id: str
    whole_reason: str | None = None
    resource_name: str | None = None
    resource_value: int | None = None


@dataclass(frozen=True)
class RescopeIntent:
    """Replaces `claims/<key>.toml`'s scope (and optionally its whole-reason).
    Only the claimant `(agent, role)` may rescope its own claim."""

    claim_id: ClaimId
    agent: str
    role: str
    scope: tuple[str, ...]
    operation_id: str
    whole_reason: str | None = None
    clear_whole_reason: bool = False


@dataclass(frozen=True)
class ReleaseIntent:
    """Deletes `claims/<key>.toml`; `ids/` and `resources/` are unchanged (a
    released claim id and a released resource value are both terminal:
    never reused). The claimant may release its own claim; a coordinator may
    release any claim with `coordinator_override=True`.

    `outcome` (`MergedRelease` | `AbandonedRelease`) carries no tree effect
    of its own -- `apply` never inspects it -- but is carried on the intent
    for the commit message `store.py` builds from this transition, and for
    `MergedRelease`'s own pre-`apply` forge verification (criterion 9,
    unchanged from today: failure there never reaches `apply` at all).
    """

    claim_id: ClaimId
    agent: str
    role: str
    outcome: ReleaseOutcome
    operation_id: str
    coordinator_override: bool = False


ClaimTransitionIntent = ClaimIntent | RescopeIntent | ReleaseIntent


def _claim_matches_intent(claim: ActiveClaim, intent: ClaimIntent) -> bool:
    """Whether `claim` is the exact live claim an interrupted, replayed
    `intent` would have produced (criterion 2): same identity, agent, role,
    branch, and scope. Compared as `ActingIdentity` pairs, not raw tuples,
    so "same claimant" stays one named domain concept everywhere it is
    checked (here, and in rescope/release below)."""
    return (
        claim.identity == intent.identity
        and ActingIdentity(claim.agent, claim.role) == ActingIdentity(intent.agent, intent.role)
        and claim.branch == intent.branch
        and claim.scope == intent.scope
    )


def _auto_resource_value(occupied: set[int]) -> int:
    value = 1
    while value in occupied:
        value += 1
    return value


def _explicit_resource_conflict(state: ClaimState, name: str, value: int) -> ClaimUnavailableError:
    holder = next(
        (claim for claim in state.claims.values() if claim.resource == ResourceHold(name, value)),
        None,
    )
    if holder is not None:
        return ClaimUnavailableError(
            f"{name} {value} is held by {holder.agent} ({holder.role}) on "
            f"{_identity_summary(holder.identity, holder.branch)}"
        )
    return ClaimUnavailableError(f"{name} {value} was already consumed and cannot be reused")


def _resolved_resource(
    state: ClaimState, intent: ClaimIntent
) -> tuple[ResourceHold | None, ResourceRecord | None]:
    """The hold `intent` acquires and the updated record for its resource, or
    `(None, None)` when it names no resource.

    Auto intent (`resource_value` omitted): the least positive integer not
    yet in `occupied`. Explicit intent: refuse when the value is already in
    `occupied` -- live or released, since a released value is never reused
    (§1 "one allocation owner: this file is the never-reuse set") -- naming
    the current holder when there is one.
    """
    if intent.resource_name is None:
        if intent.resource_value is not None:
            raise ClaimError("resource value requires a resource name")
        return None, None
    name = intent.resource_name
    record = state.resources.get(name, ResourceRecord(name=name, occupied=()))
    occupied = set(record.occupied)
    if intent.resource_value is None:
        value = _auto_resource_value(occupied)
    else:
        value = intent.resource_value
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise ClaimError("resource value must be a positive integer")
        if value in occupied:
            raise _explicit_resource_conflict(state, name, value)
    return ResourceHold(name, value), ResourceRecord(name, tuple(sorted(occupied | {value})))


def _live_claim_by_id(state: ClaimState, claim_id: ClaimId) -> tuple[str, ActiveClaim] | None:
    """The `(tree key, claim)` pair for `claim_id`, or `None`.

    `ClaimState.claims` is keyed by the claim-key codec (`issue-42`,
    `lane-...`), not by `claim_id` -- at most one claim is ever live per
    identity (an identity conflict refuses a second), so this linear scan
    over the (small, live-claims-only) mapping is the one place that still
    needs to go from a bare `claim_id`, which is all `RescopeIntent` and
    `ReleaseIntent` carry, back to its tree key.
    """
    return next(
        ((key, claim) for key, claim in state.claims.items() if claim.claim_id == claim_id), None
    )


def _apply_claim_intent(state: ClaimState, intent: ClaimIntent) -> ClaimState:
    if state.tip is None:
        # The one command allowed to turn `EMPTY_STATE` into real content is
        # `bootstrap` itself, which never calls `apply` -- every other write
        # path funnels through here, so this is the one place that must
        # refuse instead of silently creating `refs/aco/state` as a side
        # effect (issue #176 slice-review finding 1).
        raise ClaimError(MISSING_STATE_REF)
    live = _live_claim_by_id(state, intent.claim_id)
    if intent.claim_id in state.consumed_ids:
        if live is not None and _claim_matches_intent(live[1], intent):
            return state
        raise ClaimUnavailableError(
            f"claim id {intent.claim_id!r} is already on this ledger, active or "
            "released; release it, then claim again with a fresh claim id"
        )
    blocked_by = blocking_claims(tuple(state.claims.values()), intent)
    if blocked_by:
        owner = blocked_by[0]
        raise ClaimUnavailableError(
            f"{_identity_summary(intent.identity, intent.branch)} is claimed by "
            f"{owner.agent} ({owner.role}) on {_identity_summary(owner.identity, owner.branch)} "
            f"branch {owner.branch}"
        )
    resource, resource_record = _resolved_resource(state, intent)
    new_claim = ActiveClaim(
        identity=intent.identity,
        claim_id=intent.claim_id,
        agent=intent.agent,
        role=intent.role,
        base=intent.base,
        branch=intent.branch,
        scope=intent.scope,
        opened_commit=state.tip,
        resource=resource,
        whole_reason=intent.whole_reason,
    )
    new_claims = {**state.claims, claim_key(intent.identity, intent.branch): new_claim}
    new_resources = dict(state.resources)
    if resource_record is not None:
        new_resources[resource_record.name] = resource_record
    return replace(
        state,
        claims=MappingProxyType(new_claims),
        consumed_ids=state.consumed_ids | {intent.claim_id},
        resources=MappingProxyType(new_resources),
    )


def _apply_rescope_intent(state: ClaimState, intent: RescopeIntent) -> ClaimState:
    found = _live_claim_by_id(state, intent.claim_id)
    if found is None:
        raise ClaimUnavailableError(f"claim id {intent.claim_id!r} has no active claim to rescope")
    key, current = found
    if ActingIdentity(current.agent, current.role) != ActingIdentity(intent.agent, intent.role):
        raise ClaimUnavailableError("only the original claimant may rescope")
    if intent.clear_whole_reason:
        whole_reason = None
    elif intent.whole_reason is not None:
        whole_reason = intent.whole_reason
    else:
        whole_reason = current.whole_reason
    new_claim = replace(current, scope=intent.scope, whole_reason=whole_reason)
    new_claims = {**state.claims, key: new_claim}
    return replace(state, claims=MappingProxyType(new_claims))


def _apply_release_intent(state: ClaimState, intent: ReleaseIntent) -> ClaimState:
    found = _live_claim_by_id(state, intent.claim_id)
    if found is None:
        raise ClaimUnavailableError(f"claim id {intent.claim_id!r} has no active claim to release")
    key, current = found
    if intent.coordinator_override:
        if intent.role != COORDINATOR_ROLE:
            raise ClaimUnavailableError("a coordinator override requires role coordinator")
    elif ActingIdentity(current.agent, current.role) != ActingIdentity(intent.agent, intent.role):
        raise ClaimUnavailableError(
            "only the original claimant may release; use an explicit coordinator override"
        )
    new_claims = {
        existing_key: claim for existing_key, claim in state.claims.items() if existing_key != key
    }
    return replace(state, claims=MappingProxyType(new_claims))


def apply(state: ClaimState, intent: ClaimTransitionIntent) -> ClaimState:
    """The pure claim-state transition (issue #176, §1): the sole writer of
    `ClaimState.claims`/`consumed_ids`/`resources`. Assumes `state.tip` is
    already real -- `store.py` never calls this against `EMPTY_STATE`."""
    if isinstance(intent, ClaimIntent):
        return _apply_claim_intent(state, intent)
    if isinstance(intent, RescopeIntent):
        return _apply_rescope_intent(state, intent)
    return _apply_release_intent(state, intent)


# --- `claims/<key>.toml` and `resources/<name>.toml` codecs -----------------
#
# Hand-written, not a TOML-writing library: every field reaching here already
# passed `_valid_branch`/`_valid_scope`/`CLAIM_ID_PATTERN`/`ObjectId`, which
# between them exclude control characters and backslashes, so the one
# genuinely free-form input -- a scope path -- only ever needs `"` escaped to
# stay a valid TOML basic string. A whole dependency for that single escape
# would remove less complexity than it adds.

_TOML_STRING_ESCAPES = {"\\": "\\\\", '"': '\\"'}


def _toml_string(value: str) -> str:
    escaped = "".join(_TOML_STRING_ESCAPES.get(character, character) for character in value)
    return f'"{escaped}"'


def _toml_string_array(values: tuple[str, ...]) -> str:
    return "[" + ", ".join(_toml_string(value) for value in values) + "]"


def _toml_int_array(values: tuple[int, ...]) -> str:
    return "[" + ", ".join(str(value) for value in values) + "]"


def serialize_claim_toml(claim: ActiveClaim) -> str:
    """The `claims/<key>.toml` content for one live claim (§1)."""
    lines = [
        f"claim_id = {_toml_string(claim.claim_id)}",
        f"agent = {_toml_string(claim.agent)}",
        f"role = {_toml_string(claim.role)}",
        f"base = {_toml_string(claim.base)}",
        f"branch = {_toml_string(claim.branch)}",
        f"scope = {_toml_string_array(claim.scope)}",
        f"opened_commit = {_toml_string(claim.opened_commit)}",
    ]
    if claim.whole_reason is not None:
        lines.append(f"whole_reason = {_toml_string(claim.whole_reason)}")
    if claim.resource is not None:
        lines.append(f"resource_name = {_toml_string(claim.resource.name)}")
        lines.append(f"resource_value = {claim.resource.value}")
    return "\n".join(lines) + "\n"


_CLAIM_TOML_REQUIRED_KEYS = frozenset(
    {"claim_id", "agent", "role", "base", "branch", "scope", "opened_commit"}
)
_CLAIM_TOML_OPTIONAL_KEYS = frozenset({"whole_reason", "resource_name", "resource_value"})
_CLAIM_TOML_KEYS = _CLAIM_TOML_REQUIRED_KEYS | _CLAIM_TOML_OPTIONAL_KEYS


def _claim_toml_text(
    data: Mapping[str, object], field_name: str, *, key: str, tip: ObjectId
) -> str:
    value = data.get(field_name)
    if not isinstance(value, str) or not value:
        raise MalformedStateTreeError(
            f"claim file {key}.toml at {tip} field {field_name!r} must be non-empty text"
        )
    return value


def _claim_toml_scope(data: Mapping[str, object], *, key: str, tip: ObjectId) -> tuple[str, ...]:
    raw = data.get("scope")
    if not isinstance(raw, list) or not raw or any(not isinstance(entry, str) for entry in raw):
        raise MalformedStateTreeError(
            f"claim file {key}.toml at {tip} field 'scope' must be a non-empty list of text"
        )
    return tuple(raw)


def _claim_toml_resource(
    data: Mapping[str, object], *, key: str, tip: ObjectId
) -> ResourceHold | None:
    if "resource_name" not in data:
        if "resource_value" in data:
            raise MalformedStateTreeError(
                f"claim file {key}.toml at {tip} has resource_value without resource_name"
            )
        return None
    name = _claim_toml_text(data, "resource_name", key=key, tip=tip)
    value = data.get("resource_value")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise MalformedStateTreeError(
            f"claim file {key}.toml at {tip} field 'resource_value' must be a positive integer"
        )
    return ResourceHold(name, value)


def parse_claim_toml(content: str, *, key: str, tip: ObjectId) -> ActiveClaim:
    """Parse one fetched `claims/<key>.toml` blob into its `ActiveClaim`.

    A malformed claim file fails the whole read loud (ruling: a commit is
    the unit a writer writes, so a broken tree is corrupt state, not a
    single quarantinable claim the way an unknown ledger-comment field was).
    """
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as error:
        raise MalformedStateTreeError(
            f"malformed claim file {key}.toml at {tip}: {error}"
        ) from error
    observed_keys = frozenset(data)
    if not observed_keys <= _CLAIM_TOML_KEYS:
        raise MalformedStateTreeError(
            f"claim file {key}.toml at {tip} has unknown keys: "
            f"{sorted(observed_keys - _CLAIM_TOML_KEYS)}"
        )
    missing = _CLAIM_TOML_REQUIRED_KEYS - observed_keys
    if missing:
        raise MalformedStateTreeError(
            f"claim file {key}.toml at {tip} is missing {sorted(missing)}"
        )
    identity = parse_claim_key(key)
    claim_id_text = _claim_toml_text(data, "claim_id", key=key, tip=tip)
    if CLAIM_ID_PATTERN.fullmatch(claim_id_text) is None:
        raise MalformedStateTreeError(f"claim file {key}.toml at {tip} has an invalid claim id")
    base_text = _claim_toml_text(data, "base", key=key, tip=tip)
    opened_commit_text = _claim_toml_text(data, "opened_commit", key=key, tip=tip)
    commit_fields_valid = (
        COMMIT_PATTERN.fullmatch(base_text) is not None
        and COMMIT_PATTERN.fullmatch(opened_commit_text) is not None
    )
    if not commit_fields_valid:
        raise MalformedStateTreeError(f"claim file {key}.toml at {tip} has a malformed commit id")
    whole_reason = data.get("whole_reason")
    if whole_reason is not None and not isinstance(whole_reason, str):
        raise MalformedStateTreeError(
            f"claim file {key}.toml at {tip} field 'whole_reason' must be text"
        )
    return ActiveClaim(
        identity=identity,
        claim_id=ClaimId(claim_id_text),
        agent=_claim_toml_text(data, "agent", key=key, tip=tip),
        role=_claim_toml_text(data, "role", key=key, tip=tip),
        base=ObjectId(base_text),
        branch=_claim_toml_text(data, "branch", key=key, tip=tip),
        scope=_claim_toml_scope(data, key=key, tip=tip),
        opened_commit=ObjectId(opened_commit_text),
        resource=_claim_toml_resource(data, key=key, tip=tip),
        whole_reason=whole_reason,
    )


def serialize_resource_toml(record: ResourceRecord) -> str:
    """The `resources/<name>.toml` content for one resource's occupied set (§1)."""
    return f"occupied = {_toml_int_array(record.occupied)}\n"


def parse_resource_toml(content: str, *, name: str, tip: ObjectId) -> ResourceRecord:
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as error:
        raise MalformedStateTreeError(
            f"malformed resource file {name}.toml at {tip}: {error}"
        ) from error
    if set(data) != {"occupied"}:
        raise MalformedStateTreeError(
            f"resource file {name}.toml at {tip} must contain exactly 'occupied'"
        )
    occupied = data["occupied"]
    if not isinstance(occupied, list) or any(
        isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in occupied
    ):
        raise MalformedStateTreeError(
            f"resource file {name}.toml at {tip} field 'occupied' must be positive integers"
        )
    return ResourceRecord(name=name, occupied=tuple(occupied))


def _comment_is_state_cut(comment: IssueComment) -> bool:
    """Whether `comment`'s marker action is the tombstone, read without
    `parse_claim_event` so a mid-ledger `state_cut` cannot abort the walk
    (issue #176, §2 skip mechanism)."""
    parsed = _marker_payload(comment)
    if parsed is None:
        return False
    return parsed[0].get("action") == STATE_CUT_ACTION


def _store_claim_from_ledger(claim: LedgerActiveClaim, opened_commit: ObjectId) -> ActiveClaim:
    """The store record for one still-active ledger claim at import.

    `opened_commit` is the import parent (age resets at the cut -- named
    cost). `comment` / `requested_resource` / `quarantined_by` stay on the
    ledger form and do not cross (ruling 9d).
    """
    return ActiveClaim(
        identity=claim.identity,
        claim_id=ClaimId(claim.claim_id),
        agent=claim.agent,
        role=claim.role,
        base=ObjectId(claim.base),
        branch=claim.branch,
        scope=claim.scope,
        opened_commit=opened_commit,
        resource=claim.resource,
        whole_reason=claim.whole_reason,
    )


def state_from_ledger_aggregate(
    comments: tuple[IssueComment, ...], *, opened_commit: ObjectId
) -> ClaimState:
    """Pure import reader (issue #176, §2): pre-filter `state_cut`, then the
    current aggregator. Never calls `parse_claim_event` on a tombstone, and
    `_aggregate_claim_events` still catches only `UnreadableClaimError`.

    Occupied resource values come from the same first-occurrence walk as
    live holds, including released autos and explicit posts (done-when 2).
    Claim ids are `ClaimLedgerAggregate.seen_claim_ids`. Does not invent
    ids or occupied integers.
    """
    filtered = tuple(comment for comment in comments if not _comment_is_state_cut(comment))
    aggregate = _aggregate_claim_events(filtered)
    _reject_duplicate_claim_ids(aggregate)
    _reject_unreadable_claims(aggregate, action="import")
    occupied = _occupied_resources_from_occurrences(aggregate.occurrences)
    claims = {
        claim_key(claim.identity, claim.branch): _store_claim_from_ledger(claim, opened_commit)
        for claim in aggregate.active
    }
    return ClaimState(
        tip=opened_commit,
        claims=MappingProxyType(claims),
        consumed_ids=frozenset(ClaimId(claim_id) for claim_id in aggregate.seen_claim_ids),
        resources=MappingProxyType(
            {name: ResourceRecord(name, values) for name, values in occupied.items()}
        ),
    )
