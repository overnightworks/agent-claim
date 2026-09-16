"""Pure codec for one state-ref item file's `[record]` table (issue #248).

An item file (`items/<id>.md` in the tree of `refs/aco/state`) is a
work-item body in the same `agent-claim`-block grammar `board.py` already
reads and writes, extended with a nested `[record]` table that exists only
under `storage = "state-ref"`: the identity and relations a GitHub issue
would otherwise carry through its native type, sub-issue, and blocked-by
relations. `board.py`'s schema (`parse_body`/`_block_record_defects`)
already validates that table's shape before this module ever sees it; this
module turns the validated raw values into `ItemRecord`, and turns an item
file's own name into its id and number.

This module sits below `board.py` in the Layers contract (`items` under
`board`): it must never import it. `state_board.py`, the adapter that
assembles `board.Issue`/`IssueDependency`/`ChildItem`/`ParentIssue` from
several `ItemRecord`s at once, sits above both.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias, cast

from .protocol import MalformedStateTreeError

# `aco-` plus six lowercase hex characters (issue #248, parent #230 ruling
# 15.09.2026): a short random id, never a counter, never reused. The
# filename an item lives at is exactly this id plus `.md` -- never a block
# key, so a rename of the file is the only way its id ever changes.
ITEM_ID_PATTERN = re.compile(r"aco-[0-9a-f]{6}")
ITEM_FILENAME_SUFFIX = ".md"
# `cast`'s type argument for every `ItemRecord` field `board.py`'s schema
# leaves optional: named once as a real type, not a repeated string literal,
# so the four call sites below share one owner.
_OptionalStr: TypeAlias = str | None


class RecordState(StrEnum):
    """An item's own `[record].state` (issue #248): `board.py`'s schema
    already restricts the raw value to these two; this is the typed read of
    it, distinct from `board.BlockerState`/`board.ChildState`, which name a
    *relation's target* state, not an item's own."""

    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True)
class ItemRecord:
    """One state-ref item's `[record]` table, decoded and typed.

    `number` is read from the item's file name, never a block key (issue
    #248): the id's hex suffix read as an integer -- deterministic,
    reversible, and never a counter -- so this record fits
    `board.Issue.number` and every other port type keyed by `int` without
    widening them. The id itself has exactly one owner, the file name
    (`state_board.py` keys its items by it); this record never carries a
    second copy.
    """

    number: int
    title: str
    state: RecordState
    kind: str | None
    labels: tuple[str, ...]
    blocked_by: tuple[str, ...]
    parent: str | None
    origin: str | None
    created_at: str
    updated_at: str
    closed_at: str | None


def item_number(item_id: str) -> int:
    """`item_id`'s hex suffix, read as an integer -- the value every port
    type keyed by `int` (`board.Issue.number`, `board.IssueReference.number`,
    ...) uses for a state-ref item. Raises `MalformedStateTreeError` for
    anything that is not a well-formed `aco-` id: called only for an item
    file's own name (`parse_item_record`'s `number` field below), never for
    a `blocked_by`/`parent` reference -- those stay opaque item-id strings,
    resolved by dictionary lookup against the already-parsed item set
    (`state_board.py`), and only that other item's own already-computed
    `number` is ever exposed."""
    if ITEM_ID_PATTERN.fullmatch(item_id) is None:
        raise MalformedStateTreeError(f"{item_id!r} is not a valid item id")
    return int(item_id.removeprefix("aco-"), 16)


def item_id_from_filename(filename: str) -> str:
    """The item id `filename` names, or a loud refusal: every file directly
    under `items/` must be `aco-<six hex>.md`, never anything else (issue
    #248, ruling "a broken tree is corrupt state")."""
    candidate = filename.removesuffix(ITEM_FILENAME_SUFFIX)
    if not filename.endswith(ITEM_FILENAME_SUFFIX) or ITEM_ID_PATTERN.fullmatch(candidate) is None:
        raise MalformedStateTreeError(f"items/{filename} is not a valid item file name")
    return candidate


def parse_item_record(item_id: str, record: Mapping[str, object]) -> ItemRecord:
    """`record` (already validated by `board.py`'s schema: `parse_body`
    returned `BodyReadState.VALID` and this is its `.record`) turned into a
    typed `ItemRecord`. Trusts every field's shape -- it never re-validates
    what the caller already checked."""
    labels = tuple(cast("list[str]", record.get("labels", [])))
    blocked_by = tuple(cast("list[str]", record.get("blocked_by", [])))
    return ItemRecord(
        number=item_number(item_id),
        title=cast(str, record["title"]).strip(),
        state=RecordState(cast(str, record["state"])),
        kind=cast(_OptionalStr, record.get("kind")),
        labels=labels,
        blocked_by=blocked_by,
        parent=cast(_OptionalStr, record.get("parent")),
        origin=cast(_OptionalStr, record.get("origin")),
        created_at=cast(str, record["created_at"]),
        updated_at=cast(str, record["updated_at"]),
        closed_at=cast(_OptionalStr, record.get("closed_at")),
    )
