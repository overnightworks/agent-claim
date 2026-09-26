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
        forge.ForgeOperation.LIST_RECENTLY_CLOSED_ISSUES: forge.Capability.READ_ONLY,
        forge.ForgeOperation.LINK_CHILD: forge.Capability.READ_WRITE,
        # `item new` creates a state-ref item through `create_item`, which
        # mints its id and records its parent and origin in one write.
        forge.ForgeOperation.CREATE_ISSUE: forge.Capability.UNSUPPORTED,
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
NO_BARE_ISSUE = "a state-ref item is created by aco item new, never as a bare forge issue"
# PIN-16/PIN-17's sentences, completing `item <id> ...`.
_PARENT_MISSING = "is referenced as a parent but does not exist"
_BLOCKER_MISSING = "is listed as a blocker but does not exist"


@dataclass(frozen=True)
class _DecodedItem:
    record: items.ItemRecord
    body: str
    oid: ObjectId


@dataclass(frozen=True)
class _MalformedItem:
    """An item file whose bytes decode to no valid `agent-claim` block with
    a `[record]` table (issue #447): kept aside rather than refusing the
    whole store, so only a command that must read exactly this item
    refuses. `problem` completes the sentence `item <id> ...`; `oid` is the
    CAS `expected` a repairing `update_item_body` writes over."""

    problem: str
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


def _valid_record(text: str) -> Mapping[str, object] | None:
    """`text`'s own `[record]` table when its `agent-claim` block is VALID
    under `Storage.STATE_REF` -- the same block grammar `body.py` already
    reads, gated open to `record` only there -- else `None`."""
    parsed = parse_body(text, storage=Storage.STATE_REF)
    return parsed.record if parsed.read_state is BodyReadState.VALID else None


def _decode_item(item_id: str, content: bytes, oid: ObjectId) -> _DecodedItem | _MalformedItem:
    """`content` turned into a `_DecodedItem`, or set aside as a
    `_MalformedItem` (issue #447): every item file must be UTF-8 text whose
    block parses VALID with a `[record]` table; one that does not is
    corrupt state only for the commands that read exactly that item."""
    try:
        text = content.decode("utf-8")
    except UnicodeDecodeError:
        return _MalformedItem(problem="is not valid UTF-8", oid=oid)
    record = _valid_record(text)
    if record is None:
        return _MalformedItem(problem="has a malformed agent-claim block", oid=oid)
    return _DecodedItem(record=items.parse_item_record(item_id, record), body=text, oid=oid)


def _malformed_item_refusal(item_id: str, malformed: _MalformedItem) -> MalformedStateTreeError:
    return MalformedStateTreeError(
        f"item {item_id} {malformed.problem}; repair it with aco item edit {item_id} "
        "and a body whose agent-claim block carries a valid [record]"
    )


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
        self._items: dict[str, _DecodedItem] = {}
        self._malformed: dict[str, _MalformedItem] = {}
        for filename, content in item_files.items():
            item_id = items.item_id_from_filename(filename)
            decoded = _decode_item(item_id, content, item_oids[item_id])
            if isinstance(decoded, _MalformedItem):
                self._malformed[item_id] = decoded
            else:
                self._items[item_id] = decoded
        self._by_number = {
            items.item_number(item_id): item_id for item_id in (*self._items, *self._malformed)
        }

    @property
    def requests(self) -> int:
        """Always zero: every byte this adapter reads was already fetched
        by `cli._state_ref_forge` before construction (issue #248) -- no
        method below costs a further round trip."""
        return 0

    def capability(self, operation: forge.ForgeOperation) -> forge.Capability:
        return STATE_REF_CAPABILITIES[operation]

    def holds(self, number: int) -> bool:
        """Whether `items/` carries `number` at all, malformed or not: the
        one existence check `aco item edit` needs, since it is also the
        repair path for a malformed item every other read refuses."""
        return number in self._by_number

    def require_well_formed(self) -> None:
        """Refuses with the lowest malformed item's own sentence and repair
        while `items/` holds any (issue #447): a malformed item's parent,
        state, and blockers are unknown, so every answer that enumerates the
        whole store -- the board `board`/`next`/`rulings`/`cut` project, a
        container's children, what `item close` freed -- would guess past
        it."""
        if self._malformed:
            item_id = min(self._malformed)
            raise _malformed_item_refusal(item_id, self._malformed[item_id])

    def _decoded(self, number: int) -> _DecodedItem | None:
        item_id = self._by_number.get(number)
        return None if item_id is None else self._related(item_id, missing="does not exist")

    def _related(self, item_id: str, *, missing: str) -> _DecodedItem:
        """`item_id`'s decoded item, or a refusal by name: a malformed one
        names its repair (issue #447), an unknown one completes `item <id>`
        with `missing`."""
        malformed = self._malformed.get(item_id)
        if malformed is not None:
            raise _malformed_item_refusal(item_id, malformed)
        decoded = self._items.get(item_id)
        if decoded is None:
            raise MalformedStateTreeError(f"item {item_id} {missing}")
        return decoded

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

    def parent_number(self, number: int) -> int | None:
        """`number`'s recorded parent, off its own record alone: never
        decoding that parent, so `item show` still names a malformed one
        (issue #447), while PIN-16 still refuses one `items/` lacks."""
        decoded = self._decoded(number)
        if decoded is None or decoded.record.parent is None:
            return None
        parent_id = decoded.record.parent
        if parent_id not in self._items and parent_id not in self._malformed:
            raise MalformedStateTreeError(f"item {parent_id} {_PARENT_MISSING}")
        return items.item_number(parent_id)

    def parent_issue(self, number: int) -> board.ParentIssue | None:
        parent_number = self.parent_number(number)
        if parent_number is None:
            return None
        parent = self._related(self._by_number[parent_number], missing=_PARENT_MISSING)
        return board.ParentIssue(
            board.IssueReference(self.repository.path, parent.record.number),
            parent.body,
            _item_kind(parent.record.kind),
        )

    def list_children(self, number: int) -> tuple[board.ChildItem, ...]:
        self.require_well_formed()
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
        self.require_well_formed()
        return tuple(
            self._issue(item_id)
            for item_id, decoded in self._items.items()
            if decoded.record.state is items.RecordState.OPEN
        )

    def open_item_titles(self) -> tuple[tuple[int, str], ...]:
        """Every open item's number and title, the open half of `item new`'s
        twin search: unlike `list_open_board_issues` it never refuses on a
        malformed item (issue #447), so `item new` still runs beside one."""
        return tuple(
            (decoded.record.number, decoded.record.title)
            for decoded in self._items.values()
            if decoded.record.state is items.RecordState.OPEN
        )

    def list_board_dependencies(self, number: int) -> tuple[board.IssueDependency, ...]:
        decoded = self._decoded(number)
        if decoded is None:
            return ()
        dependencies: list[board.IssueDependency] = []
        for blocker_id in decoded.record.blocked_by:
            blocker = self._related(blocker_id, missing=_BLOCKER_MISSING)
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

    def list_recently_closed_issues(self, since: datetime) -> tuple[forge.ClosedIssue, ...]:
        cutoff = since.astimezone(UTC)
        return tuple(
            forge.ClosedIssue(decoded.record.number, decoded.record.title)
            for decoded in self._items.values()
            if decoded.record.closed_at is not None
            and datetime.fromisoformat(decoded.record.closed_at) >= cutoff
        )

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
        new_id = items.mint_item_id(self._by_number.values())
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

    def create_issue(self, *, title: str, body: str, kind: ItemKind) -> int:
        """Unsupported (`STATE_REF_CAPABILITIES`): `create_item` mints a
        state-ref item's id and records its parent and origin in one write."""
        raise forge.ForgeUnsupportedError(NO_BARE_ISSUE)

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
        with issue #279's own sentence rather than merging or overwriting.
        A malformed item (issue #447) has no stored record to merge into:
        `body`'s own complete `[record]` repairs it once its relations
        resolve (`_refuse_unresolved_repair`), else it refuses by name."""
        item_id = self._by_number[number]
        now = items.format_record_timestamp(datetime.now(UTC))
        malformed = self._malformed.get(item_id)
        if malformed is None:
            current = self._items[item_id]
            title, labels, blocked_by = _delivered_content_fields(body, current.record)
            updated_record = replace(
                current.record, title=title, labels=labels, blocked_by=blocked_by, updated_at=now
            )
            expected = current.oid
        else:
            delivered = _valid_record(body)
            if delivered is None:
                raise _malformed_item_refusal(item_id, malformed)
            updated_record = replace(items.parse_item_record(item_id, delivered), updated_at=now)
            self._refuse_unresolved_repair(updated_record)
            expected = malformed.oid
        new_body = _with_record(body, updated_record)
        new_oid = self._writer.write_item(
            item_id, expected=expected, content=new_body.encode("utf-8")
        )
        self._malformed.pop(item_id, None)
        self._items[item_id] = _DecodedItem(record=updated_record, body=new_body, oid=new_oid)

    def _refuse_unresolved_repair(self, record: items.ItemRecord) -> None:
        """A repair's own `[record]` is written whole (issue #447), so its
        relations must resolve first: its parent and every blocker name a
        readable item -- never a missing one (PIN-16/PIN-17's sentences), a
        malformed one, or itself -- and it stays open, since only `item close`
        guards a close against a live claim."""
        item_id = items.format_item_id(record.number)
        if record.state is items.RecordState.CLOSED:
            raise ClaimUnavailableError(
                f'a repair records state = "open"; close {item_id} afterwards '
                f"with aco item close {item_id}"
            )
        if record.parent is not None:
            self._related(record.parent, missing=_PARENT_MISSING)
        for blocker_id in record.blocked_by:
            self._related(blocker_id, missing=_BLOCKER_MISSING)

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
