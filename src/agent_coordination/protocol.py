"""Claim protocol: claim records, scope rules, and the `refs/aco/state` codec."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import PurePosixPath, PureWindowsPath
from types import MappingProxyType
from typing import Protocol, TypeVar

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
CLAIM_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
RESOURCE_NAME_PATTERN = re.compile(r"[A-Za-z][A-Za-z0-9._-]{0,63}")
COMMIT_PATTERN = re.compile(r"[0-9a-f]{40}")
BRANCH_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,254}")
REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9_.-]{1,100}")
MAX_SCOPE_ENTRIES = 256
MAX_SCOPE_PATH_LENGTH = 512
WIDE_SCOPE_PATH_LIMIT = 3
WIDE_SCOPE_SHARE_LIMIT = 0.25
# A tiny repository must not trip the share condition.
WIDE_SCOPE_SHARE_FLOOR = 12
# How many paths a truncated listing names in full before counting the rest --
# the dirty-tree refusal's and the claim overlap notice's shared shape for a
# list too long to print whole.
NAMED_PATH_OVERFLOW_LIMIT = 3
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
    claim record's `branch`, which every lane claim already has. A second
    branch-shaped field here would give the branch two owners that could drift apart.
    """


ClaimIdentity = IssueIdentity | LaneIdentity


@dataclass(frozen=True)
class ResourceHold:
    """One named scarce value held by a live claim until land or release."""

    name: str
    value: int


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


LANE_MARKER_KEY = "lane"


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
    """Validate a stored or CLI scope list: one list entry, one path.

    A path is taken verbatim, comma and all -- a repository-relative path
    that contains a comma is otherwise unrepresentable. A caller with more
    than one path repeats the flag (`--scope a --scope b`); nothing here
    joins or splits entries.
    """
    if not isinstance(scope, list) or not scope:
        raise InvalidClaimMarkerError("claim marker scope must be a non-empty list")
    entries: list[str] = []
    for raw_path in scope:
        if not isinstance(raw_path, str):
            raise InvalidClaimMarkerError("claim scope entries must be text")
        if raw_path.strip() != raw_path or not raw_path:
            raise InvalidClaimMarkerError(SCOPE_ENTRIES_MUST_BE_CANONICAL)
        entries.append(raw_path)
    if len(entries) > MAX_SCOPE_ENTRIES:
        raise InvalidClaimMarkerError(f"claim marker scope exceeds {MAX_SCOPE_ENTRIES} entries")
    return entries


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


def scope_overlap_paths(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    """The concrete paths where two scopes meet, sorted and deduplicated.

    A scope path stands for itself and everything under it, so two paths meet
    when one is an ancestor (or equal) of the other; the meeting point named
    is always the deeper (more specific) of the two, since that is the real
    file or directory the overlap touches -- not the ancestor that merely
    covers it. A directory in `left` meeting a single file under it in
    `right` therefore names that file, and an identical path in both names
    that path once.
    """
    meeting: set[str] = set()
    for left_path in left:
        left_parts = PurePosixPath(left_path).parts
        for right_path in right:
            right_parts = PurePosixPath(right_path).parts
            if len(left_parts) <= len(right_parts):
                ancestor_parts, descendant_path, descendant_parts = (
                    left_parts,
                    right_path,
                    right_parts,
                )
            else:
                ancestor_parts, descendant_path, descendant_parts = (
                    right_parts,
                    left_path,
                    left_parts,
                )
            if descendant_parts[: len(ancestor_parts)] == ancestor_parts:
                meeting.add(descendant_path)
    return tuple(sorted(meeting))


def _scopes_overlap(left: tuple[str, ...], right: tuple[str, ...]) -> bool:
    return bool(scope_overlap_paths(left, right))


def named_with_overflow_count(
    paths: tuple[str, ...], *, limit: int = NAMED_PATH_OVERFLOW_LIMIT
) -> str:
    """Render `paths` as a comma-separated list, naming only the first `limit`
    and counting the rest -- too many to list is still a fact worth stating,
    just not one worth printing in full."""
    named = ", ".join(paths[:limit])
    remainder = len(paths) - limit
    if remainder > 0:
        named += f", and {remainder} more"
    return named


class ScopedClaim(Protocol):
    """The structural shape the conflict/overlap helpers need.

    Satisfied by both the CLI's `ClaimRequest` and the store's
    `ActiveClaim`/`ClaimIntent` pair (issue #176, slice C2), so one set of
    pure functions serves all of them instead of a nominal union that would
    grow every time a new claim-shaped record is added. Read-only
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
    by the claim record supplies the missing distinction without giving the
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

    `opened_commit` is the state-ref commit this claim_id was first
    introduced at; rescope never changes it.
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
    claims: Mapping[str, ActiveClaim] = field(default_factory=lambda: MappingProxyType({}))
    consumed_ids: frozenset[ClaimId] = field(default_factory=frozenset)
    resources: Mapping[str, ResourceRecord] = field(default_factory=lambda: MappingProxyType({}))


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


def _same_identity(left: ClaimIdentity, right: ClaimIdentity) -> bool:
    """Structural equality between two `ClaimIdentity` values: `IssueIdentity`
    compares its `issue` number, and `LaneIdentity` carries no field of its
    own, since every caller here compares branch separately."""
    if isinstance(left, IssueIdentity) and isinstance(right, IssueIdentity):
        return left.issue == right.issue
    return isinstance(left, LaneIdentity) and isinstance(right, LaneIdentity)


def _same_claimant(
    current: ActiveClaim, intent: ClaimIntent | RescopeIntent | ReleaseIntent
) -> bool:
    """Whether `intent` names the same claimant as `current`: same agent and
    role, compared as a tuple so "same claimant" stays one named domain
    concept everywhere it is checked (claim replay, rescope, release)."""
    return (current.agent, current.role) == (intent.agent, intent.role)


def _claimant_text(agent: str, role: str) -> str:
    """Render the `(agent, role)` tuple `_same_claimant` compares, for a
    refusal that must name which two claimants disagreed."""
    return f"{agent} ({role})"


def _claim_matches_intent(claim: ActiveClaim, intent: ClaimIntent) -> bool:
    """Whether `claim` is the exact live claim an interrupted, replayed
    `intent` would have produced (criterion 2): same identity, claimant,
    branch, and scope."""
    return (
        _same_identity(claim.identity, intent.identity)
        and _same_claimant(claim, intent)
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
    if not _same_claimant(current, intent):
        raise ClaimUnavailableError(
            "only the original claimant may rescope "
            f"(holder={_claimant_text(current.agent, current.role)!r}, "
            f"this session={_claimant_text(intent.agent, intent.role)!r})"
        )
    whole_reason = current.whole_reason if intent.whole_reason is None else intent.whole_reason
    new_claim = replace(current, scope=intent.scope, whole_reason=whole_reason)
    new_claims = {**state.claims, key: new_claim}
    return replace(state, claims=MappingProxyType(new_claims))


def _authorize_release(current: ActiveClaim, intent: ReleaseIntent) -> None:
    """Raises unless `intent` may release `current`: the original claimant,
    or an explicit coordinator override by role coordinator."""
    if intent.coordinator_override:
        if intent.role != COORDINATOR_ROLE:
            raise ClaimUnavailableError("a coordinator override requires role coordinator")
        return
    if not _same_claimant(current, intent):
        raise ClaimUnavailableError(
            "only the original claimant may release; use an explicit coordinator override "
            f"(holder={_claimant_text(current.agent, current.role)!r}, "
            f"this session={_claimant_text(intent.agent, intent.role)!r})"
        )


def _apply_release_intent(state: ClaimState, intent: ReleaseIntent) -> ClaimState:
    found = _live_claim_by_id(state, intent.claim_id)
    if found is None:
        raise ClaimUnavailableError(f"claim id {intent.claim_id!r} has no active claim to release")
    key, current = found
    _authorize_release(current, intent)
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
    the unit a writer writes, so a broken tree is corrupt state, never a
    single quarantinable claim).
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
