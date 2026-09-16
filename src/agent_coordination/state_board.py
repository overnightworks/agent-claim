"""Forge adapter over `refs/aco/state`'s `items/` tree (issues #248, #283).

`StateRefBoard` sits beside `github.GitHubForge` behind the same `forge`
port: both talk to a different backend for the same board data, and
neither imports the other. Unlike `GitHubForge`, this adapter performs no
IO of its own -- the Layers contract puts `store` above this module, so
`cli._state_ref_forge` reads `items/`'s raw bytes and blob oids through
`store.read_item_files`/`ClaimState.items` and hands them to the
constructor; every read method below is a pure projection over that
already-fetched data. Every write (`create_item`, `create_child`,
`update_item_body`, `close_item`, `link_child`) instead calls the injected `ItemWriter`
port: one
compare-and-swap write to `items/<id>.md`, implemented in `cli.py` over
`store` (hash-object once, then one `commit_transition` with an
`ItemWriteIntent`, issue #279) -- so this module still never imports
`store` itself.

`LANDING` and the two pull-request listings answer `Capability.UNSUPPORTED`:
this adapter has no data for any of them. The two pull-request listings
still return an empty tuple rather than raising: `cli._board` calls them
unconditionally for every board read, and "no pull requests exist here" is
this adapter's honest answer, not a refusal. `Stage.CODE_LANDED` and
`Board.recovery` fall out of that same emptiness -- both stay empty until
#230 slice 6 adds merge-commit-derived landings (README, "Storage pin").
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Protocol, cast

from . import board, forge, items
from .protocol import ClaimUnavailableError, MalformedStateTreeError, ObjectId

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
        forge.ForgeOperation.LINK_CHILD: forge.Capability.READ_WRITE,
        forge.ForgeOperation.CREATE_CHILD: forge.Capability.READ_WRITE,
        forge.ForgeOperation.UPDATE_ITEM_BODY: forge.Capability.READ_WRITE,
    }
)


class ItemWriter(Protocol):
    """The write port every mutating `StateRefBoard` operation composes onto
    (issue #283): one CAS write to `items/<item_id>.md` -- `expected` the
    oid the caller's own already-read snapshot carries (`None` for "must not
    exist yet"), `content` the item's finished new bytes -- returning the
    freshly written blob's oid, so the caller can update its own in-memory
    state without a re-fetch. Implemented in `cli.py`, over `store`
    (hash-object once, then one `commit_transition` with an
    `ItemWriteIntent`, issue #279): this module may not import `store`
    itself (Layers contract), so every actual git call for an item write
    stays behind this one method.
    """

    def write_item(
        self, item_id: str, *, expected: ObjectId | None, content: bytes
    ) -> ObjectId: ...


NO_LANDINGS_YET = (
    "landings are not yet derived from the state ref; "
    "#230 slice 6 adds merge-commit-derived landings"
)


@dataclass(frozen=True)
class _DecodedItem:
    record: items.ItemRecord
    body: str
    oid: ObjectId


def _decoded_text(item_id: str, content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MalformedStateTreeError(f"item {item_id} is not valid UTF-8") from error


def _decode_item(item_id: str, content: bytes, oid: ObjectId) -> _DecodedItem:
    """`content` turned into a `_DecodedItem`, or a loud refusal (ruling "a
    broken tree is corrupt state"): every item file must parse as a VALID
    `agent-claim` block carrying a `[record]` table, the same block grammar
    `board.py` already reads, gated open to `record` only under
    `Storage.STATE_REF`."""
    text = _decoded_text(item_id, content)
    parsed = board.parse_body(text, storage=board.Storage.STATE_REF)
    if parsed.read_state is not board.BodyReadState.VALID or parsed.record is None:
        raise MalformedStateTreeError(f"item {item_id} has a malformed agent-claim block")
    return _DecodedItem(record=items.parse_item_record(item_id, parsed.record), body=text, oid=oid)


def _with_record(body: str, record: items.ItemRecord) -> str:
    """`body`'s `agent-claim` block, its `[record]` table replaced by
    `record`'s own fields, every other byte untouched -- the one place a
    write composes a fresh `[record]` table, shared by `create_child` (a
    brand new one) and `update_item_body` (an existing one with `updated_at`
    refreshed)."""
    located = board.locate_agent_claim_block(body)
    new_data = {**located.data, board.RECORD_KEY: items.record_table(record)}
    return board.replace_agent_claim_block(body, located, new_data)


def _item_kind(kind: str | None) -> board.ItemKind | None:
    return board.ItemKind(kind) if kind is not None else None


def _delivered_content_fields(
    body: str, stored: items.ItemRecord
) -> tuple[str, tuple[str, ...], tuple[str, ...]]:
    """`update_item_body`'s own owner split for `title`, `labels`,
    `blocked_by` (issue #287, `aco item edit`'s ruled record merge): taken
    from `body`'s own `[record]` table when the delivered body carries one
    valid -- read off the table's raw TOML presence, never
    `items.parse_item_record`'s own `.get(key, [])` default, which would
    turn an omitted `labels`/`blocked_by` into an emptied one rather than
    `stored`'s own value. A body with no `[record]` at all -- every
    `rule`/`ask`/`cut` write already composes one -- changes nothing here,
    exactly `update_item_body`'s behaviour before this issue. Every other
    record field (`parent`, `state`, `origin`, `kind`, the three
    timestamps) stays `stored`'s own regardless of what a delivered record
    names for it -- `update_item_body` itself, never this helper, owns
    that half of the split."""
    parsed = board.parse_body(body, storage=board.Storage.STATE_REF)
    delivered = parsed.record
    if parsed.read_state is not board.BodyReadState.VALID or delivered is None:
        return stored.title, stored.labels, stored.blocked_by
    title = cast(str, delivered["title"]).strip() if "title" in delivered else stored.title
    labels = (
        tuple(cast("list[str]", delivered["labels"])) if "labels" in delivered else stored.labels
    )
    blocked_by = (
        tuple(cast("list[str]", delivered["blocked_by"]))
        if "blocked_by" in delivered
        else stored.blocked_by
    )
    return title, labels, blocked_by


class StateRefBoard:
    """The `state-ref` storage pin's `BoardSource`/`ForgeReader`/`ForgeWriter`
    adapter."""

    def __init__(
        self,
        *,
        repository: forge.RepositoryId,
        default_branch: str,
        item_files: Mapping[str, bytes],
        item_oids: Mapping[str, ObjectId],
        writer: ItemWriter,
    ) -> None:
        self.repository = repository
        self._default_branch = default_branch
        self._writer = writer
        self._items: dict[str, _DecodedItem] = {
            (item_id := items.item_id_from_filename(filename)): _decode_item(
                item_id, content, item_oids[item_id]
            )
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

    def link_child(self, parent: int, child: int) -> None:
        """A no-op (issue #283): parentage has exactly one owner here,
        `record.parent`, already set by `create_child`'s own single write.
        GitHub's `link_child` recovers an orphan its own two-write
        `create_child` could leave behind (a created issue with no recorded
        sub-issue relation); a state-ref item write is one CAS write, so
        that half-finished state can never occur and there is nothing left
        to link."""
        del parent, child

    def _write_new_item(
        self, *, parent_id: str | None, title: str, body: str, kind: board.ItemKind
    ) -> str:
        """The one write every fresh state-ref item goes through (issues
        #283, #285): mint an id, compose its `[record]`, one CAS write, then
        fold the result into this instance's own view -- shared by
        `create_item` (`aco item new`, an optional parent) and `create_child`
        (`cut`, always one)."""
        new_id = items.mint_item_id(self._items.keys())
        now = items.format_record_timestamp(datetime.now(UTC))
        record = items.ItemRecord(
            number=items.item_number(new_id),
            title=title,
            state=items.RecordState.OPEN,
            kind=kind.value,
            labels=(),
            blocked_by=(),
            parent=parent_id,
            origin=None,
            created_at=now,
            updated_at=now,
            closed_at=None,
        )
        new_body = _with_record(body, record)
        new_oid = self._writer.write_item(new_id, expected=None, content=new_body.encode("utf-8"))
        self._items[new_id] = _DecodedItem(record=record, body=new_body, oid=new_oid)
        self._by_number[record.number] = new_id
        return new_id

    def create_item(
        self, *, title: str, body: str, kind: board.ItemKind, parent: int | None
    ) -> str:
        """`aco item new`'s own write path (issue #285): the same one write
        `create_child` performs, generalized to an optional parent -- so
        `cli.py` never grows a second way to create a state-ref item.
        Returns the freshly minted item id rather than `create_child`'s
        `.number`: called only from `cli.py`'s own state-ref-only `item new`
        path, which prints the id itself."""
        parent_id = None if parent is None else self._by_number[parent]
        return self._write_new_item(parent_id=parent_id, title=title, body=body, kind=kind)

    def create_child(self, *, parent: int, title: str, body: str, kind: board.ItemKind) -> int:
        item_id = self.create_item(title=title, body=body, kind=kind, parent=parent)
        return self._items[item_id].record.number

    def update_item_body(self, number: int, body: str) -> None:
        """`body`, written back to `number`'s item file (issues #283, #287):
        `title`, `labels`, `blocked_by` come from `body`'s own `[record]`
        table when it carries one (`_delivered_content_fields`'s own owner
        split -- `aco item edit`'s ruled record merge); every other record
        field stays this item's own already-read `current.record`, and
        `updated_at` always moves to now. The CAS write's `expected` is
        `current.oid`, this instance's own already-read snapshot -- never a
        re-read -- so a second writer holding the same stale oid refuses
        with issue #279's own sentence rather than merging or overwriting."""
        item_id = self._by_number[number]
        current = self._items[item_id]
        title, labels, blocked_by = _delivered_content_fields(body, current.record)
        updated_record = replace(
            current.record,
            title=title,
            labels=labels,
            blocked_by=blocked_by,
            updated_at=items.format_record_timestamp(datetime.now(UTC)),
        )
        new_body = _with_record(body, updated_record)
        new_oid = self._writer.write_item(
            item_id, expected=current.oid, content=new_body.encode("utf-8")
        )
        self._items[item_id] = _DecodedItem(record=updated_record, body=new_body, oid=new_oid)

    def close_item(self, number: int) -> str:
        """Closes `number`'s item record (issue #289): `state` moves to
        `CLOSED`, `closed_at` and `updated_at` both move to now, and every
        other field -- the body included -- stays byte-identical, in one CAS
        write over this instance's own already-read `current.oid` (the same
        oid discipline `update_item_body` uses, issue #279). The one place
        `state`/`closed_at` are ever composed -- `cli._cmd_item_close`
        delegates the whole write here rather than building its own record.
        Refuses loud, before any write, when the record already carries
        `state = CLOSED`: a second `close` on the same item is a stale
        caller, not an idempotent no-op, so it gets its own named date
        rather than a generic conflict. Returns the fresh `closed_at` for
        the CLI's own report line."""
        item_id = self._by_number[number]
        current = self._items[item_id]
        if current.record.state is items.RecordState.CLOSED:
            raise ClaimUnavailableError(
                f"#{number} is already closed (closed on {current.record.closed_at})"
            )
        now = items.format_record_timestamp(datetime.now(UTC))
        updated_record = replace(
            current.record, state=items.RecordState.CLOSED, closed_at=now, updated_at=now
        )
        new_body = _with_record(current.body, updated_record)
        new_oid = self._writer.write_item(
            item_id, expected=current.oid, content=new_body.encode("utf-8")
        )
        self._items[item_id] = _DecodedItem(record=updated_record, body=new_body, oid=new_oid)
        return now

    def item_oid(self, number: int) -> ObjectId:
        """This item's own current blob oid, straight off this adapter's
        already-read state (issue #287): `aco item edit --json`'s own
        output is the one caller that needs it -- never part of the generic
        `ForgeWriter` port, since no other forge in this repository stores
        one blob per item."""
        item_id = self._by_number[number]
        return self._items[item_id].oid
