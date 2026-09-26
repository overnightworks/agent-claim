"""The forge port: repository identity, typed failures, and the read/write surface.

`ForgeReader`/`ForgeWriter` are the provider-neutral contract every adapter
(today: GitHub) implements; `ForgeOperation` names every operation on that
contract and `Capability` answers, per operation, whether an adapter can
perform it at all. The GitHub adapter never itself refuses an operation.
`cli._load_board_config` is the one caller that branches on a capability
answer -- reading a work-item body's dependencies (#150) requires
`LIST_BOARD_DEPENDENCIES` at `READ_ONLY` or better, so a forge without it
cannot serve a board at all.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from . import board
from .body import ItemKind
from .protocol import ClaimError


class ForgeError(ClaimError):
    """An unclassified forge failure."""


class ForgeUnsupportedError(ForgeError):
    """The forge cannot perform this operation at all."""


class ForgePermissionDeniedError(ForgeError):
    """The forge refused the operation as an authorization failure."""


class ForgeNotFoundError(ForgeError):
    """The forge reports that the named subject does not exist."""


class ForgeTransientError(ForgeError):
    """The forge failed in a way a retry might not."""


class ForgeMalformedResponseError(ForgeError):
    """The forge's response could not be parsed into the expected shape."""


class ForgeMergeConflictError(ForgeError):
    """`merge_landing` refused a pinned merge because the pull request
    changed since its head sha was read (HTTP 405/409): `aco land`'s own
    preflight already proved every other precondition, so a caller re-reads
    and re-runs rather than merging a commit it never actually validated."""


class ForgePartialCreationError(ForgeError):
    """The forge created issue `created`, but `step` did not finish it.

    The issue already exists, so a caller's refusal must name it and what is
    left (issue #444) rather than read as "nothing created". `step` is the
    failed step as a verb phrase, `partial_write`'s own `failed` value.
    """

    def __init__(self, message: str, *, created: int, step: str) -> None:
        self.created = created
        self.step = step
        super().__init__(message)


class ForgeIssueTypeNotSetError(ForgePartialCreationError):
    """GitHub created issue `created` without the organization's issue type
    `type_name` it was asked for: its REST create drops the type silently
    when the caller lacks push access (issue #444)."""

    def __init__(self, *, created: int, type_name: str) -> None:
        self.type_name = type_name
        super().__init__(
            f"created #{created} but GitHub did not set its type {type_name}",
            created=created,
            step=f"set #{created}'s type {type_name}",
        )


class ForgePartialChildCreationError(ForgePartialCreationError):
    """`cut` created `created` under `parent`, but `step` failed to finish
    recording it there.

    Not atomic across `create_child`'s own two writes (the issue and its
    sub-issue relation), nor across `create_child` and the later block
    rewrite -- but a repeat is safe (#260): re-run the same cut and it
    adopts the child -- an orphan open issue with no recorded parent, exactly
    what a failed relation write leaves behind -- instead of risking a
    second one, finishing whichever `step` failed. Raised by the GitHub
    adapter when its own relation write fails, and reused by
    `cli._cmd_cut` when the later block rewrite fails -- one type, so both
    failures recover the same way. `item new --parent` meets the same error
    through `create_child` but reports a by-hand recovery (ITEM-32), never
    a re-run.
    """

    def __init__(self, *, child: int, parent: int, step: str, cause: Exception) -> None:
        self.parent = parent
        self.cause = cause
        super().__init__(
            f"created #{child} but failed to {step}: {cause}", created=child, step=step
        )


@dataclass(frozen=True)
class RepositoryId:
    """A repository's identity: the port owns this shape, an adapter owns its syntax."""

    host: str
    namespace: tuple[str, ...]
    name: str

    @property
    def path(self) -> str:
        return "/".join((*self.namespace, self.name))

    def __str__(self) -> str:
        return self.path


class ItemState(StrEnum):
    """A referenced work item's state, as seen from one repository."""

    OPEN = "open"
    CLOSED = "closed"
    MISSING = "missing"


@dataclass(frozen=True)
class ItemReference:
    """One referenced number, read once.

    `is_landing` says whether that number is a pull request rather than a
    plain issue, so a caller that must tell the two apart (`check`)
    distributes on this single read instead of probing with `landing`, which
    fails unclassified for an issue number. A number that does not exist is
    `MISSING` and no landing. `origin` is the foreign issue this item binds
    to (`FORGE#N`, issue #316, parent #230): populated from `record.origin`
    under `state-ref`, always `None` under `github` -- a GitHub issue is
    never itself bound to another forge's issue.
    """

    state: ItemState
    title: str | None = None
    body: str | None = None
    is_landing: bool = False
    origin: str | None = None


@dataclass(frozen=True)
class ClosedIssue:
    """One issue closed at or after a caller's cutoff: only the number and
    title `item new`'s and `cut`'s twin search compares (issue #444)."""

    number: int
    title: str


@dataclass(frozen=True)
class Landing:
    """One pull/merge request read for its own sake, not for the board's stages.

    `merge_commit` is the sha GitHub itself recorded as the merge (`None`
    until `merged` is true): `release --merged`'s own authority for what
    this landing closes (issue #397) is that commit's own trailer block,
    never this same read's mutable `body` -- a fixer can (and once did,
    Befund 41) edit the body after the merge without touching what actually
    landed.
    """

    number: int
    author: str
    body: str
    source_repository: RepositoryId
    source_branch: str
    target_branch: str
    merged: bool
    merge_commit: str | None


# GitHub's own open vocabularies for a pull request's `mergeable_state` and a
# completed check run's `conclusion` (`aco land`'s preflight, issue #405):
# each is owned by GitHub, can grow a new value without this tool's notice,
# and every value but the one this tool branches on is only ever echoed
# verbatim into a refusal -- never enumerated as a closed `StrEnum`, which
# would refuse a legitimate answer this tool has simply never seen before.
MERGEABLE_STATE_CLEAN = "clean"
CHECK_CONCLUSION_SUCCESS = "success"


@dataclass(frozen=True)
class CheckRun:
    """One CI check GitHub reports against a pull request's pinned head
    commit (`landing_readiness`, issue #405): `conclusion` is `None` while
    the check has not reached `status: "completed"`, GitHub's own
    conclusion string (`CHECK_CONCLUSION_SUCCESS` or another) once it has."""

    name: str
    conclusion: str | None


@dataclass(frozen=True)
class LandingReadiness:
    """Every fact `aco land`'s preflight judges before its first write
    (issue #405), read once so a race between this read and the merge
    itself is caught by the pinned `head_sha` rather than assumed away.
    `mergeable_state` is GitHub's own string (compare against
    `MERGEABLE_STATE_CLEAN`)."""

    number: int
    open: bool
    head_sha: str
    mergeable_state: str
    checks: tuple[CheckRun, ...]


class Capability(StrEnum):
    UNSUPPORTED = "unsupported"
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


class ForgeOperation(StrEnum):
    """Every port operation; each member's value is its Protocol method name."""

    ITEM_REFERENCE = "item_reference"
    ITEM_REFERENCES = "item_references"
    LANDING = "landing"
    PARENT_ISSUE = "parent_issue"
    LIST_CHILDREN = "list_children"
    DEFAULT_BRANCH = "default_branch"
    LIST_OPEN_BOARD_ISSUES = "list_open_board_issues"
    LIST_BOARD_DEPENDENCIES = "list_board_dependencies"
    LIST_OPEN_BOARD_PULL_REQUESTS = "list_open_board_pull_requests"
    LIST_RECENT_MERGED_BOARD_PULL_REQUESTS = "list_recent_merged_board_pull_requests"
    LIST_RECENTLY_CLOSED_ISSUES = "list_recently_closed_issues"
    LINK_CHILD = "link_child"
    CREATE_ISSUE = "create_issue"
    CREATE_CHILD = "create_child"
    UPDATE_ITEM_BODY = "update_item_body"


class BoardSource(Protocol):
    """The read surface `_board` actually calls: the repository identity, its
    capability answers, and the board list operations, not every
    `ForgeReader` operation. Every `ForgeReader` already satisfies it
    structurally; a board-only fake needs nothing more.
    """

    @property
    def repository(self) -> RepositoryId: ...

    @property
    def requests(self) -> int:
        """Every call this adapter has made so far through its one counted
        chokepoint (`GitHubForge._run`; a fake's own equivalent) -- read once
        a `board` run's reads are all in, never reset mid-run, so it always
        answers "how many round trips did this cost" (issue #168)."""
        ...

    def capability(self, operation: ForgeOperation) -> Capability: ...

    def list_open_board_issues(self) -> tuple[board.Issue, ...]: ...

    def list_board_dependencies(self, number: int) -> tuple[board.IssueDependency, ...]: ...

    def list_open_board_pull_requests(self) -> tuple[board.PullRequest, ...]: ...

    def list_recent_merged_board_pull_requests(
        self, since: datetime
    ) -> tuple[board.PullRequest, ...]: ...

    def list_children(self, number: int) -> tuple[board.ChildItem, ...]: ...


class ForgeReader(Protocol):
    @property
    def repository(self) -> RepositoryId: ...

    @property
    def requests(self) -> int:
        """See `BoardSource.requests`."""
        ...

    def capability(self, operation: ForgeOperation) -> Capability: ...

    def item_reference(self, number: int) -> ItemReference: ...

    def item_references(self, numbers: Iterable[int]) -> Mapping[int, ItemReference]:
        """Every one of `numbers`, read in as few round trips as the adapter
        can manage rather than one per number (issue #440): a closed item's
        own board-estimate size is the one caller today (`cli._closed_item_
        sizes`), and its own history only ever grows, so a per-number round
        trip there stays the board's dominant cost as a repository ages. A
        number this adapter cannot resolve at all reads exactly like
        `item_reference`'s own single-read case -- `ItemState.MISSING`, no
        body -- never dropped from the result silently."""
        ...

    def landing(self, number: int) -> Landing: ...

    def parent_issue(self, number: int) -> board.ParentIssue | None: ...

    def parent_number(self, number: int) -> int | None:
        """The number of `number`'s parent without reading that parent's
        body -- all `item show`'s header names, so a parent no other read
        can decode never stops it (issue #447)."""
        ...

    def list_children(self, number: int) -> tuple[board.ChildItem, ...]: ...

    def default_branch(self) -> str: ...

    def list_open_board_issues(self) -> tuple[board.Issue, ...]: ...

    def list_board_dependencies(self, number: int) -> tuple[board.IssueDependency, ...]: ...

    def list_open_board_pull_requests(self) -> tuple[board.PullRequest, ...]: ...

    def list_recent_merged_board_pull_requests(
        self, since: datetime
    ) -> tuple[board.PullRequest, ...]: ...

    def list_recently_closed_issues(self, since: datetime) -> tuple[ClosedIssue, ...]:
        """Every issue closed at or after `since`, never a pull request --
        the closed half of the twin search (issue #444)."""
        ...


class ForgeWriter(ForgeReader, Protocol):
    """`ForgeReader` plus every operation that mutates forge state."""

    def link_child(self, parent: int, child: int) -> None: ...

    def create_issue(self, *, title: str, body: str, kind: ItemKind) -> int:
        """A fresh issue of `kind`, linked to no parent (`item new`, issue
        #444); `create_child` is the same write plus the parent relation."""
        ...

    def create_child(self, *, parent: int, title: str, body: str, kind: ItemKind) -> int: ...

    def update_item_body(self, number: int, body: str) -> None: ...
