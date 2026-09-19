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
import secrets
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import TypeAlias, cast

from .protocol import ClaimUnavailableError, MalformedStateTreeError

# `aco-` plus six lowercase hex characters (issue #248, parent #230 ruling
# 15.09.2026): a short random id, never a counter, never reused. The
# filename an item lives at is exactly this id plus `.md` -- never a block
# key, so a rename of the file is the only way its id ever changes.
ITEM_ID_PATTERN = re.compile(r"aco-[0-9a-f]{6}")
ITEM_FILENAME_SUFFIX = ".md"
# `--origin`'s own grammar (issue #316, parent #230): a lowercase forge name
# and the number that forge itself uses for the issue -- the one shape
# `aco pull <forge>#<n>` (#230 slice 5) will later split back out of
# `record.origin`, so this is that read side's one owner too, not just the
# write side's.
ORIGIN_PATTERN = re.compile(r"[a-z][a-z0-9-]*#[1-9][0-9]*")
# Refuse rather than silently widen (issue #283, ruling 16.09.2026): an id
# never grows a seventh hex character just because the id space (16.7
# million values per repository) is filling up. Three tries against real
# randomness is already astronomically unlikely to collide even once; a
# fourth would only mask a broken randomness source.
_MAX_MINT_ATTEMPTS = 3
# The RFC 3339 UTC, second-precision shape `board.RECORD_TIMESTAMP_PATTERN`
# reads back -- named once here, the `[record]` timestamp fields' own write
# side, rather than each caller formatting its own `datetime`.
_RECORD_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%SZ"
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


def format_item_id(number: int) -> str:
    """`number`, encoded the way `item_number` decodes it back (issue
    #285): the one display id `aco item show`'s header prints under every
    storage pin, `github` included -- so an id this tool prints is always a
    valid reference back into `board.parse_item_reference`, regardless of
    which forge actually owns the number (decision D4, #230 cut 16.09.2026:
    an id is identity, not just display)."""
    return f"aco-{number:06x}"


def item_id_from_filename(filename: str) -> str:
    """The item id `filename` names, or a loud refusal: every file directly
    under `items/` must be `aco-<six hex>.md`, never anything else (issue
    #248, ruling "a broken tree is corrupt state")."""
    candidate = filename.removesuffix(ITEM_FILENAME_SUFFIX)
    if not filename.endswith(ITEM_FILENAME_SUFFIX) or ITEM_ID_PATTERN.fullmatch(candidate) is None:
        raise MalformedStateTreeError(f"items/{filename} is not a valid item file name")
    return candidate


def parse_origin(value: str) -> str:
    """`--origin`'s argparse `type=` (issue #316): `value` unchanged when it
    matches `ORIGIN_PATTERN`, a loud refusal before any write otherwise --
    the same `protocol.ClaimError` `board.parse_item_reference` already
    raises from an argparse `type=`, so `main`'s existing `parse_args` guard
    catches this one too without a second exception class to catch."""
    if ORIGIN_PATTERN.fullmatch(value) is None:
        raise ClaimUnavailableError(f"{value!r} is not an origin; use FORGE#N, e.g. gitlab#514")
    return value


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


def _random_hex_suffix() -> str:
    return secrets.token_hex(3)


def mint_item_id(
    existing: Iterable[str], *, random_hex: Callable[[], str] = _random_hex_suffix
) -> str:
    """A fresh, unpredictable item id (issue #283): `aco-` plus six
    lowercase hex characters from `random_hex` (`secrets.token_hex` by
    default) -- never a counter, never reused. Refuses by name after
    `_MAX_MINT_ATTEMPTS` collisions against `existing`'s ids rather than
    retrying forever or silently widening. `random_hex` is the one seam a
    test injects a collision stub through; production never overrides it.
    """
    known = frozenset(existing)
    for _attempt in range(_MAX_MINT_ATTEMPTS):
        candidate = f"aco-{random_hex()}"
        if candidate not in known:
            return candidate
    raise ClaimUnavailableError(
        f"could not mint a fresh item id in {_MAX_MINT_ATTEMPTS} attempts; retry"
    )


def format_record_timestamp(moment: datetime) -> str:
    """`moment`, in the RFC 3339 UTC second-precision shape every `[record]`
    timestamp field uses (`board.RECORD_TIMESTAMP_PATTERN`)."""
    return moment.astimezone(UTC).strftime(_RECORD_TIMESTAMP_FORMAT)


def record_table(record: ItemRecord) -> dict[str, object]:
    """`record`, turned back into the `[record]` table's TOML-ready dict
    shape `board.render_block`/`board._render_record` expect: the write-side
    mirror of `parse_item_record`, so the two directions share one field
    list and one owner for what an optional field's absence means (omitted
    entirely, never written empty)."""
    table: dict[str, object] = {
        "title": record.title,
        "state": record.state.value,
        "labels": list(record.labels),
        "blocked_by": list(record.blocked_by),
        "created_at": record.created_at,
        "updated_at": record.updated_at,
    }
    if record.kind is not None:
        table["kind"] = record.kind
    if record.parent is not None:
        table["parent"] = record.parent
    if record.origin is not None:
        table["origin"] = record.origin
    if record.closed_at is not None:
        table["closed_at"] = record.closed_at
    return table
