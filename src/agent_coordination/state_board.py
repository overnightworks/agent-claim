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
this adapter's honest answer, not a refusal. `Board.recovery` -- purely
pull-request-body-declared -- stays empty from that same emptiness.
`Stage.CODE_LANDED` and the board's own Landungen view do not: both read
`checkout.trunk_landings`'s trailer block straight from local git history
instead (issues #304, #371), independent of this adapter's own missing
pull-request data.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Protocol, cast

from . import board, forge, items
from .body import (
    RECORD_KEY,
    BodyReadState,
    ItemKind,
    Storage,
    locate_agent_claim_block,
    parse_body,
    replace_agent_claim_block,
)
from .protocol import ClaimUnavailableError, MalformedStateTreeError, ObjectId

STATE_REF_CAPABILITIES: Mapping[forge.ForgeOperation, forge.Capability] = MappingProxyType(
    {
        forge.ForgeOperation.ITEM_REFERENCE: forge.Capability.READ_ONLY,
        forge.ForgeOperation.ITEM_REFERENCES: forge.Capability.READ_ONLY,
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


@dataclass(frozen=True)
class LandingWrite:
    """One item's own close write, composed but not yet applied (issue
    #359): `item_id`/`expected`/`content` are exactly the CAS write
    `close_item` performs immediately today, staged instead so `release
    --merged <sha|empty>` can hash `content` into a blob itself and fold
    the result into one atomic `protocol.LandingIntent` alongside the claim
    it releases. `record` is the item's own already-closed record, carried
    along so `mark_landed` can fold the committed write back into this
    instance's in-memory view without re-decoding `content`."""

    item_id: str
    expected: ObjectId
    content: bytes
    record: items.ItemRecord


def _decoded_text(item_id: str, content: bytes) -> str:
    try:
        return content.decode("utf-8")
    except UnicodeDecodeError as error:
        raise MalformedStateTreeError(f"item {item_id} is not valid UTF-8") from error


def _decode_item(item_id: str, content: bytes, oid: ObjectId) -> _DecodedItem:
    """`content` turned into a `_DecodedItem`, or a loud refusal (ruling "a
    broken tree is corrupt state"): every item file must parse as a VALID
    `agent-claim` block carrying a `[record]` table, the same block grammar
    `body.py` already reads, gated open to `record` only under
    `Storage.STATE_REF`."""
    text = _decoded_text(item_id, content)
    parsed = parse_body(text, storage=Storage.STATE_REF)
    if parsed.read_state is not BodyReadState.VALID or parsed.record is None:
        raise MalformedStateTreeError(f"item {item_id} has a malformed agent-claim block")
    return _DecodedItem(record=items.parse_item_record(item_id, parsed.record), body=text, oid=oid)


def _with_record(body: str, record: items.ItemRecord) -> str:
    """`body`'s `agent-claim` block, its `[record]` table replaced by
    `record`'s own fields, every other byte untouched -- the one place a
    write composes a fresh `[record]` table, shared by `create_child` (a
    brand new one), `update_item_body` (an existing one with `updated_at`
    refreshed), and `close_item` (an existing one moved to `CLOSED`)."""
    located = locate_agent_claim_block(body)
    new_data = {**located.data, RECORD_KEY: items.record_table(record)}
    return replace_agent_claim_block(body, located, new_data)


def _item_kind(kind: str | None) -> ItemKind | None:
    return ItemKind(kind) if kind is not None else None


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
    parsed = parse_body(body, storage=Storage.STATE_REF)
    delivered = parsed.record
    if parsed.read_state is not BodyReadState.VALID or delivered is None:
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
        if kind is ItemKind.CONTAINER:
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
        return forge.ItemReference(
            state, decoded.record.title, decoded.body, False, decoded.record.origin
        )

    def item_references(self, numbers: Iterable[int]) -> Mapping[int, forge.ItemReference]:
        """Every one of `numbers`, read straight from the already-fetched
        `items/` tree (issue #440): this adapter performs no IO of its own at
        all (module docstring), so there is no round trip here to batch --
        `item_reference` per number costs the same as this already does."""
        return {number: self.item_reference(number) for number in numbers}

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
        # merged pull requests at all, so this empty tuple is this
        # adapter's own honest answer -- `Board.recovery` (purely
        # pull-request-declared) stays empty from it, while `Stage.CODE_LANDED`
        # and the board's own Landungen view read the trunk walk instead
        # (issue #371) and never depend on this listing at all.
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
        self,
        *,
        parent_id: str | None,
        title: str,
        body: str,
        kind: ItemKind,
        origin: str | None = None,
    ) -> str:
        """The one write every fresh state-ref item goes through (issues
        #283, #285, #316): mint an id, compose its `[record]`, one CAS
        write, then fold the result into this instance's own view -- shared
        by `create_item` (`aco item new`, an optional parent and origin) and
        `create_child` (`cut`, always one, never an origin -- a cut child is
        always this repository's own item)."""
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
            origin=origin,
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
        self,
        *,
        title: str,
        body: str,
        kind: ItemKind,
        parent: int | None,
        origin: str | None = None,
    ) -> str:
        """`aco item new`'s own write path (issues #285, #316): the same one
        write `create_child` performs, generalized to an optional parent and
        origin -- so `cli.py` never grows a second way to create a state-ref
        item. `origin` binds this item to a foreign forge issue
        (`--origin FORGE#N`, already grammar-checked by `items.parse_origin`
        before this is ever called) without aco governing that forge at all
        -- #230's own concept, "the forge is pulled, never governed."
        Returns the freshly minted item id rather than `create_child`'s
        `.number`: called only from `cli.py`'s own state-ref-only `item new`
        path, which prints the id itself."""
        parent_id = None if parent is None else self._by_number[parent]
        return self._write_new_item(
            parent_id=parent_id, title=title, body=body, kind=kind, origin=origin
        )

    def create_child(self, *, parent: int, title: str, body: str, kind: ItemKind) -> int:
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

    def _closing_write(self, number: int) -> LandingWrite:
        """`number`'s own close write, composed but not written (issues
        #289, #359): `state` moves to `CLOSED`, `closed_at` and `updated_at`
        both move to now, and every other field -- the body included --
        stays byte-identical. Refuses loud, before any write, when the
        record already carries `state = CLOSED`: a second close on the same
        item is a stale caller, not an idempotent no-op, so it gets its own
        named date rather than a generic conflict. Shared by `close_item`
        (which writes this immediately, on its own) and `prepare_landing`
        (which hands it to `release --merged <sha>`'s own atomic
        `protocol.LandingIntent` instead, issue #359)."""
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
        return LandingWrite(
            item_id=item_id,
            expected=current.oid,
            content=new_body.encode("utf-8"),
            record=updated_record,
        )

    def close_item(self, number: int) -> str:
        """Closes `number`'s item record (issue #289) in one CAS write over
        this instance's own already-read `current.oid` (the same oid
        discipline `update_item_body` uses, issue #279) -- the one place
        `state`/`closed_at` are ever composed for an immediate close;
        `cli._cmd_item_close` delegates the whole write here rather than
        building its own record. Returns the fresh `closed_at` for the
        CLI's own report line."""
        write = self._closing_write(number)
        new_oid = self._writer.write_item(
            write.item_id, expected=write.expected, content=write.content
        )
        self._items[write.item_id] = _DecodedItem(
            record=write.record, body=write.content.decode("utf-8"), oid=new_oid
        )
        assert write.record.closed_at is not None  # `_closing_write` just set it
        return write.record.closed_at

    def prepare_landing(self, number: int) -> LandingWrite:
        """`number`'s own close write, staged but not applied (issue #359):
        `release --merged <sha|empty>` under `storage = "state-ref"` hashes
        `content` into a blob itself (`store.hash_blob` -- this module may
        not import `store`, the Layers contract) and folds the resulting
        oid into one atomic `protocol.LandingIntent` alongside the claim it
        releases, instead of `close_item`'s own immediate, separately
        committed write. Call `mark_landed` with the result once that
        transition actually commits, to fold it into this instance's own
        in-memory view -- this method itself writes nothing."""
        return self._closing_write(number)

    def mark_landed(self, write: LandingWrite, oid: ObjectId) -> None:
        """Folds an atomic landing's already-committed close (issue #359)
        into this instance's own in-memory view, the same update
        `close_item` performs after its own separate write -- but writes
        nothing itself: `release --merged <sha>`'s own transition already
        committed `write.content` at `oid`, atomically with the claim
        release."""
        self._items[write.item_id] = _DecodedItem(
            record=write.record, body=write.content.decode("utf-8"), oid=oid
        )

    def item_oid(self, number: int) -> ObjectId:
        """This item's own current blob oid, straight off this adapter's
        already-read state (issue #287): `aco item edit --json`'s own
        output is the one caller that needs it -- never part of the generic
        `ForgeWriter` port, since no other forge in this repository stores
        one blob per item."""
        item_id = self._by_number[number]
        return self._items[item_id].oid
