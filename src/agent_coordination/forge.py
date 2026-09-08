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

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Protocol

from . import board
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


class ForgePartialChildCreationError(ForgeError):
    """`cut` created `child` under `parent`, but `step` failed to finish
    recording it there.

    Not atomic across `create_child`'s own two writes (the issue and its
    sub-issue relation), nor across `create_child` and the later block
    rewrite: retrying either would risk a second child, so the caller
    recovers `step` by hand instead and never re-runs `cut`. Raised by the
    GitHub adapter when its own relation write fails, and reused by
    `cli._cmd_cut` when the later block rewrite fails -- one type, so both
    failures are recovered the same way.
    """

    def __init__(self, *, child: int, parent: int, step: str, cause: Exception) -> None:
        self.child = child
        self.parent = parent
        self.step = step
        self.cause = cause
        super().__init__(f"created #{child} but failed to {step}: {cause}")


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
    `MISSING` and no landing.
    """

    state: ItemState
    title: str | None = None
    body: str | None = None
    is_landing: bool = False


@dataclass(frozen=True)
class Landing:
    """One pull/merge request read for its own sake, not for the board's stages."""

    number: int
    author: str
    body: str
    source_repository: RepositoryId
    source_branch: str
    target_branch: str
    merged: bool


class Capability(StrEnum):
    UNSUPPORTED = "unsupported"
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"


class ForgeOperation(StrEnum):
    """Every port operation; each member's value is its Protocol method name."""

    ITEM_REFERENCE = "item_reference"
    LANDING = "landing"
    PARENT_ISSUE = "parent_issue"
    LIST_CHILDREN = "list_children"
    DEFAULT_BRANCH = "default_branch"
    LIST_OPEN_BOARD_ISSUES = "list_open_board_issues"
    LIST_BOARD_DEPENDENCIES = "list_board_dependencies"
    LIST_OPEN_BOARD_PULL_REQUESTS = "list_open_board_pull_requests"
    LIST_RECENT_MERGED_BOARD_PULL_REQUESTS = "list_recent_merged_board_pull_requests"
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

    def landing(self, number: int) -> Landing: ...

    def parent_issue(self, number: int) -> board.ParentIssue | None: ...

    def list_children(self, number: int) -> tuple[board.ChildItem, ...]: ...

    def default_branch(self) -> str: ...

    def list_open_board_issues(self) -> tuple[board.Issue, ...]: ...

    def list_board_dependencies(self, number: int) -> tuple[board.IssueDependency, ...]: ...

    def list_open_board_pull_requests(self) -> tuple[board.PullRequest, ...]: ...

    def list_recent_merged_board_pull_requests(
        self, since: datetime
    ) -> tuple[board.PullRequest, ...]: ...


class ForgeWriter(ForgeReader, Protocol):
    """`ForgeReader` plus every operation that mutates forge state."""

    def create_child(self, *, parent: int, title: str, body: str, kind: board.ItemKind) -> int: ...

    def update_item_body(self, number: int, body: str) -> None: ...
