"""Read-only forge adapter over `refs/aco/state`'s `items/` tree (issue #248).

`StateRefBoard` sits beside `github.GitHubForge` behind the same `forge`
port: both talk to a different backend for the same board data, and
neither imports the other. Unlike `GitHubForge`, this adapter performs no
IO of its own -- the Layers contract puts `store` above this module, so
`cli._state_ref_forge` reads `items/`'s raw bytes through
`store.read_item_files` and hands them to the constructor; every method
below is a pure projection over that already-fetched data.

Every writing operation (`CREATE_CHILD`, `UPDATE_ITEM_BODY`) and every
operation this adapter has no data for (`LANDING`, the two pull-request
listings) answers `Capability.UNSUPPORTED`. The two pull-request listings
still return an empty tuple rather than raising: `cli._board` calls them
unconditionally for every board read, and "no pull requests exist here" is
this adapter's honest answer, not a refusal. `Stage.CODE_LANDED` and
`Board.recovery` fall out of that same emptiness -- both stay empty until
#230 slice 6 adds merge-commit-derived landings (README, "Storage pin").
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from types import MappingProxyType

from . import board, forge, items
from .protocol import MalformedStateTreeError

STATE_REF_CAPABILITIES: Mapping[forge.ForgeOperation, forge.Capability] = MappingProxyType(
    {
        forge.ForgeOperation.ITEM_REFERENCE: forge.Capability.READ_ONLY,
        forge.ForgeOperation.PARENT_ISSUE: forge.Capability.READ_ONLY,
        forge.ForgeOperation.LIST_CHILDREN: forge.Capability.READ_ONLY,
        forge.ForgeOperation.DEFAULT_BRANCH: forge.Capability.READ_ONLY,
        forge.ForgeOperation.LIST_OPEN_BOARD_ISSUES: forge.Capability.READ_ONLY,
        forge.ForgeOperation.LIST_BOARD_DEPENDENCIES: forge.Capability.READ_ONLY,
        forge.ForgeOperation.LANDING: forge.Capability.UNSUPPORTED,
        forge.ForgeOperation.LIST_OPEN_BOARD_PULL_REQUESTS: forge.Capability.UNSUPPORTED,
        forge.ForgeOperation.LIST_RECENT_MERGED_BOARD_PULL_REQUESTS: forge.Capability.UNSUPPORTED,
        forge.ForgeOperation.CREATE_CHILD: forge.Capability.UNSUPPORTED,
        forge.ForgeOperation.UPDATE_ITEM_BODY: forge.Capability.UNSUPPORTED,
    }
)

NO_LANDINGS_YET = (
    "landings are not yet derived from the state ref; "
    "#230 slice 6 adds merge-commit-derived landings"
)


@dataclass(frozen=True)
class _DecodedItem:
    record: items.ItemRecord
    body: str


def _decoded_text(item_id: str, content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MalformedStateTreeError(f"item {item_id} is not valid UTF-8") from error


def _decode_item(item_id: str, content: bytes) -> _DecodedItem:
    """`content` turned into a `_DecodedItem`, or a loud refusal (ruling "a
    broken tree is corrupt state"): every item file must parse as a VALID
    `agent-claim` block carrying a `[record]` table, the same block grammar
    `board.py` already reads, gated open to `record` only under
    `Storage.STATE_REF`."""
    text = _decoded_text(item_id, content)
    parsed = board.parse_body(text, storage=board.Storage.STATE_REF)
    if parsed.read_state is not board.BodyReadState.VALID or parsed.record is None:
        raise MalformedStateTreeError(f"item {item_id} has a malformed agent-claim block")
    return _DecodedItem(record=items.parse_item_record(item_id, parsed.record), body=text)


def _item_kind(kind: str | None) -> board.ItemKind | None:
    return board.ItemKind(kind) if kind is not None else None


class StateRefBoard:
    """The `state-ref` storage pin's `BoardSource`/`ForgeReader` adapter."""

    def __init__(
        self,
        *,
        repository: forge.RepositoryId,
        default_branch: str,
        item_files: Mapping[str, bytes],
    ) -> None:
        self.repository = repository
        self._default_branch = default_branch
        self._items: dict[str, _DecodedItem] = {
            (item_id := items.item_id_from_filename(filename)): _decode_item(item_id, content)
            for filename, content in item_files.items()
        }
        self._by_number = {
            decoded.record.number: item_id for item_id, decoded in self._items.items()
        }

    @property
    def requests(self) -> int:
        """Always zero: every byte this adapter reads was already fetched
        by `cli._state_ref_forge` before construction (issue #248) -- no
        method below costs a further round trip."""
        return 0

    def capability(self, operation: forge.ForgeOperation) -> forge.Capability:
        return STATE_REF_CAPABILITIES[operation]

    def _decoded(self, number: int) -> _DecodedItem | None:
        item_id = self._by_number.get(number)
        return self._items.get(item_id) if item_id is not None else None

    def _issue(self, item_id: str) -> board.Issue:
        decoded = self._items[item_id]
        record = decoded.record
        kind = _item_kind(record.kind)
        children_closed = children_total = None
        if kind is board.ItemKind.CONTAINER:
            children = tuple(
                child for child in self._items.values() if child.record.parent == item_id
            )
            children_total = len(children)
            children_closed = sum(
                1 for child in children if child.record.state is items.RecordState.CLOSED
            )
        return board.Issue(
            record.number,
            record.title,
            record.labels,
            decoded.body,
            record.created_at,
            record.updated_at,
            kind,
            children_closed,
            children_total,
            len(record.blocked_by),
        )

    def item_reference(self, number: int) -> forge.ItemReference:
        decoded = self._decoded(number)
        if decoded is None:
            return forge.ItemReference(forge.ItemState.MISSING)
        state = (
            forge.ItemState.OPEN
            if decoded.record.state is items.RecordState.OPEN
            else forge.ItemState.CLOSED
        )
        return forge.ItemReference(state, decoded.record.title, decoded.body, False)

    def landing(self, number: int) -> forge.Landing:
        raise forge.ForgeUnsupportedError(NO_LANDINGS_YET)

    def parent_issue(self, number: int) -> board.ParentIssue | None:
        decoded = self._decoded(number)
        if decoded is None or decoded.record.parent is None:
            return None
        parent = self._items.get(decoded.record.parent)
        if parent is None:
            raise MalformedStateTreeError(
                f"item {decoded.record.parent} is referenced as a parent but does not exist"
            )
        return board.ParentIssue(
            board.IssueReference(self.repository.path, parent.record.number),
            parent.body,
            _item_kind(parent.record.kind),
        )

    def list_children(self, number: int) -> tuple[board.ChildItem, ...]:
        item_id = self._by_number.get(number)
        if item_id is None:
            return ()
        return tuple(
            board.ChildItem(child.record.number, board.ChildState(child.record.state.value))
            for child in self._items.values()
            if child.record.parent == item_id
        )

    def default_branch(self) -> str:
        return self._default_branch

    def list_open_board_issues(self) -> tuple[board.Issue, ...]:
        return tuple(
            self._issue(item_id)
            for item_id, decoded in self._items.items()
            if decoded.record.state is items.RecordState.OPEN
        )

    def list_board_dependencies(self, number: int) -> tuple[board.IssueDependency, ...]:
        decoded = self._decoded(number)
        if decoded is None:
            return ()
        dependencies: list[board.IssueDependency] = []
        for blocker_id in decoded.record.blocked_by:
            blocker = self._items.get(blocker_id)
            if blocker is None:
                raise MalformedStateTreeError(
                    f"item {blocker_id} is listed as a blocker but does not exist"
                )
            closed_at = None
            if blocker.record.closed_at is not None:
                closed_at = datetime.fromisoformat(blocker.record.closed_at).astimezone(UTC)
            dependencies.append(
                board.IssueDependency(
                    board.IssueReference(self.repository.path, blocker.record.number),
                    board.BlockerState(blocker.record.state.value),
                    False,
                    closed_at,
                )
            )
        return tuple(dependencies)

    def list_open_board_pull_requests(self) -> tuple[board.PullRequest, ...]:
        return ()

    def list_recent_merged_board_pull_requests(
        self, since: datetime
    ) -> tuple[board.PullRequest, ...]:
        # `since` stays the `BoardSource` protocol's own parameter name
        # (positional identity matters for structural conformance) even
        # though this adapter never filters by it: `state-ref` cannot list
        # merged pull requests at all, so every caller reads
        # `landings_derivable` first (issue #248) and treats this empty
        # tuple as "not derivable", never as "proven zero landings".
        del since
        return ()
