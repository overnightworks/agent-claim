"""The body-block codec (issue #419, audit #365 finding 3): parses, validates,
renders, and names defects for one work-item body's typed `agent-claim`
fenced block -- the `[[expectation]]`/`[[slice]]` arrays, `scope`/`size`/
`whole`, and the state-ref-only `[record]` table. `board.py` imports this
codec for its own board-builder domain (`Contract`, `ParsedBody`,
`BodyReadState`, `Storage`, `ItemKind`, ... are all board-domain field types
too, read straight off a parsed body); `items.py` and `state_board.py`
import it for the same one schema `board.py` used to own alone. This module
never imports `board`, `items`, or `state_board` back (Layers contract):
`items` may import this module, never the reverse, so a value both this
codec and `items.py` need (`ORIGIN_PATTERN`) is owned here instead.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import date
from enum import StrEnum
from typing import cast

from . import metrics, protocol

# CommonMark fence delimiters: at most 3 leading spaces, then a run of 3+
# backticks or 3+ tildes. An OPENING delimiter may carry an info string after
# the run (` ```python `); a CLOSING delimiter may not — only trailing
# spaces/tabs are allowed after the run (` ``` `, never ` ```python `), so
# `Closing`'s stricter pattern requires nothing but whitespace to follow.
# A 4-space-indented code block (CommonMark's other fencing form) is not
# modeled here; see `board.py`'s `_live_text` for why that gap is safe.
FENCE_OPENING_PATTERN = re.compile(r"^ {0,3}(?P<run>`{3,}|~{3,})")
FENCE_CLOSING_PATTERN = re.compile(r"^ {0,3}(?P<run>`{3,}|~{3,})[ \t]*$")

# `cut`'s fresh child, in the one grammar the tool reads: every projection
# key present and empty, so `parse_body` reads it as `VALID` but
# `contract_complete=False` -- invisible to `next`, refused by `claim` --
# until the head fills it in. Empty strings, not omitted keys, which the
# block schema would refuse. No `source_slice` -- title, sub-issue relation,
# and GitHub history own provenance instead.
BLOCK_CHILD_SKELETON = '```agent-claim\nversion = 1\nnow = ""\nnext = ""\ndone_when = ""\n```\n'
# A fresh container carries no automatic parent-provenance the way `cut`
# gives a fresh child one, so its own skeleton states the prose the global
# contract requires when nothing blocks it (README "`Blocked by:` prose
# beside the block is documentation only") ahead of the same block schema
# `BLOCK_CHILD_SKELETON` already owns -- one owner for the projection keys,
# never a second schema for a container's own skeleton.
BLOCK_CONTAINER_SKELETON = f"Blocked by: nichts\n\n{BLOCK_CHILD_SKELETON}"

# The one fenced-block info string a repository pinned to `body_contract =
# "block"` (issue #150) reads as its typed work-item body -- any other
# fence's info string is ordinary documentation.
AGENT_CLAIM_FENCE_INFO = "agent-claim"
BLOCK_TOP_LEVEL_KEYS = frozenset(
    {
        "version",
        "now",
        "next",
        "done_when",
        "frozen_until",
        "scope",
        "size",
        "whole",
        "expectation",
        "slice",
    }
)
# A work item's own size class (issue #357), read straight from
# `metrics.Size` -- the one owner of the three letters and their order --
# rather than a second enum here. A top-level block key, not a `[record]`
# one: `[record]` is a `Storage.STATE_REF`-only table (BODY-15), while an
# estimate is offered for every item regardless of storage, and this
# module may never import `github`/`state_board` to give a GitHub-stored
# item a second write path.
SIZE_VALUES = frozenset(size.value for size in metrics.Size)
BLOCK_VERSION = 1
BLOCK_EXPECTATION_DEFAULTS = frozenset({"yes", "no", "later"})
# A ruling transcribes the operator's word (#240): "later" is a legitimate
# final answer -- an explicit, dated decision to defer -- not only a
# proposer's guessed default, so it rules exactly like "yes"/"no" (`rule
# --later` writes `ruling = "later"` the same way `--yes`/`--no` do).
BLOCK_EXPECTATION_RULINGS = frozenset({"yes", "no", "later"})
# The three optional card fields a proposer (`aco ask`) may attach to an
# `[[expectation]]` entry (issue #295): the operator-language question and
# example a card shows in place of `text`, and an inline-SVG picture. Absent
# entirely, a card falls back to `text` unchanged.
EXPECTATION_QUESTION_MAXIMUM_CHARACTERS = 160
EXPECTATION_PICTURE_MAXIMUM_BYTES = 8 * 1024


class Storage(StrEnum):
    """Where a repository's board and item data live (`.agent-claim/board.toml`
    `storage`, issue #248): `GITHUB` reads issues, `STATE_REF` reads
    `items/<id>.md` files in the tree of `refs/aco/state`. The pin decides
    which adapter `cli._resolved_forge_target` builds; it never guesses from
    the remote's own host."""

    GITHUB = "github"
    STATE_REF = "state-ref"


# A state-ref item file's own `[record]` table (issue #248): the identity
# and relations a GitHub issue would otherwise carry through its native
# type, sub-issue, and blocked-by relations. Legal only under
# `storage = "state-ref"` -- `_block_schema_defects` refuses it by name as
# an unknown top-level key under `storage = "github"` (decision record 0001
# §2: blockers and parentage on GitHub, never duplicated in the body).
RECORD_KEY = "record"
RECORD_STATES = frozenset({"open", "closed"})
RECORD_KEYS = frozenset(
    {
        "title",
        "state",
        "kind",
        "labels",
        "blocked_by",
        "parent",
        "origin",
        "created_at",
        "updated_at",
        "closed_at",
    }
)

# The one origin grammar (issue #316, parent #230): a forge name or
# host/owner/repo path -- one or more letter/digit/hyphen segments joined by
# `.` or `/` -- then `#` and the number that forge itself uses for the
# issue. Case-insensitive on the host/owner/repo part (issue #316 delta): a
# hand-written v2 ref may carry the forge's own capitalization, e.g.
# `github.com/OvernightWorks/x#1`. `items.parse_origin`'s argparse `type=`
# and the persisted `[record].origin` field (`_record_relation_defects`
# below) both validate against this one pattern, so a stored malformed
# origin is exactly as rejected as a malformed `--origin` flag. This is
# also the one owner `aco pull <forge>#<n>` (#230 slice 5) will later split
# `record.origin` back out through. Owned here, not in `items.py`, so this
# module's own `_record_relation_defects` never has to import a higher
# layer for one shared pattern (`items` may import `body`, never the
# reverse).
ORIGIN_PATTERN = re.compile(
    r"[a-z][a-z0-9-]*(?:[./][a-z][a-z0-9-]*)*#[1-9]\d*", re.ASCII | re.IGNORECASE
)
# The one hint text for a malformed origin, shared by `items.parse_origin`'s
# argparse refusal and `_record_relation_defects`' record defect, so a bad
# `--origin` flag and a bad stored `record.origin` read the same sentence
# rather than two independently worded rules for one grammar.
ORIGIN_GRAMMAR_HINT = "forge#n or host/owner/repo#n, e.g. gitlab#514"


class ItemKind(StrEnum):
    """An item's kind, read from the forge's native issue type -- the one
    owner for "is this a container" (decision record 0001 ruling D3, #112)."""

    TASK = "task"
    BUG = "bug"
    FEATURE = "feature"
    CONTAINER = "container"


@dataclass(frozen=True)
class SliceRow:
    """One `[[slice]]` entry of a body's `agent-claim` block: a slice its
    container still has to dispatch. `index` is exactly what `cut --row N`
    names it by, `title` exactly what `cut --title` must match. `scope`
    (issue #331) is the row's own optional `scope = [...]`, validated and
    canonicalized like the block's top-level field; `None` when the row
    names no paths of its own."""

    index: int
    title: str
    scope: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ContractDefect:
    field: str
    message: str


def body_defect_text(defect: ContractDefect) -> str:
    """The one rendering of a malformed body defect (issue #176 H3).

    `cut` and the board/claim checks share this; a second renderer is a
    defect. `check` in slice F reuses it.
    """
    return f"body malformed: {defect.field}: {defect.message}"


def contract_fields(contract: Contract) -> tuple[tuple[str, str | None], ...]:
    """The three projection keys in block order, paired with their current
    value -- the one place that knows both the names and the order, so a
    caller asking which are present (`_contract_summary`) and a caller
    asking which are missing (`missing_or_empty_sections`) never drift
    apart. A block body has no dependency key at all: its dependencies live
    on the forge, never in the body.
    """
    return (
        ("Now", contract.now),
        ("Next", contract.next),
        ("Done when", contract.done_when),
    )


@dataclass(frozen=True)
class Contract:
    now: str | None
    next: str | None
    done_when: str | None
    defects: tuple[ContractDefect, ...] = ()


class ExpectationState(StrEnum):
    NONE = "-"
    PROPOSED = "proposed"
    RULED = "ruled"


@dataclass(frozen=True)
class ExpectationProgress:
    open: int
    total: int


def opening_fence_delimiter(line: str) -> tuple[str, int] | None:
    match = FENCE_OPENING_PATTERN.match(line)
    if match is None:
        return None
    run = match.group("run")
    return run[0], len(run)


def closing_fence_delimiter(line: str) -> tuple[str, int] | None:
    match = FENCE_CLOSING_PATTERN.match(line)
    if match is None:
        return None
    run = match.group("run")
    return run[0], len(run)


class BodyReadState(StrEnum):
    """How `parse_body` read one issue's body.

    `MALFORMED` covers both a block whose schema was refused and a body
    with no recognized `agent-claim` block at all -- the latter carries the
    one defect `no agent-claim block` (issue #273: there is no third state
    for a body written before the block existed).
    """

    VALID = "valid"
    MALFORMED = "malformed"


@dataclass(frozen=True)
class ParsedBody:
    """The one typed read of a work-item body. Every consumer reads this
    instead of re-parsing the raw body."""

    contract: Contract
    contract_complete: bool
    projectionless: bool
    expectation_state: ExpectationState
    expectation_progress: ExpectationProgress
    ruling_date: date | None
    frozen_trigger: str | None
    # The block's own top-level `scope = [...]` (issue #331), exactly the
    # canonical (sorted, deduplicated) tuple `protocol.valid_scope` returns
    # -- the same function a live claim's own scope passes through, so the
    # two stay comparable regardless of typed order. `None` when the block
    # carries no `scope` key at all, never for an empty one
    # (`_block_scope_defects` refuses that before this projection is ever
    # built).
    scope: tuple[str, ...] | None
    slices: tuple[SliceRow, ...]
    read_state: BodyReadState
    # The block's own top-level `size` (issue #357), or `None` for a body
    # that names no size at all -- absent means "no estimate", never a
    # default class a reader would have to guess.
    size: metrics.Size | None = None
    # The block's own top-level `whole` reason (issue #399), or `None` when
    # the item names none -- `claim`/`start`'s own fallback for `--whole`
    # when the call itself names none, read here rather than re-parsed at
    # each call site.
    whole: str | None = None
    # The validated `[record]` table (issue #248), or `None` for every body
    # parsed under `Storage.GITHUB` and every state-ref body without one --
    # `items.py` is the one reader that ever looks at this field.
    record: Mapping[str, object] | None = None


def _line_ending(raw_line: str) -> str:
    if raw_line.endswith("\r\n"):
        return "\r\n"
    if raw_line.endswith("\n"):
        return "\n"
    return ""


def _line_without_ending(raw_line: str) -> str:
    ending = _line_ending(raw_line)
    return raw_line[: len(raw_line) - len(ending)] if ending else raw_line


def first_line(body: str) -> str:
    """`body`'s first line with its ending stripped, whether the body uses
    LF or CRLF -- a GitHub GET returns CRLF (`_line_ending` above), so a
    caller comparing against a fixed marker line must not split on a bare
    `"\\n"`."""
    lines = body.splitlines(keepends=True)
    return _line_without_ending(lines[0]) if lines else ""


def _agent_claim_fence_matches(body: str) -> list[tuple[int, int | None, str]]:
    """Every fence in `body` whose info string is exactly `agent-claim`
    (issue #150 §4): `(opening line index, closing line index or None when
    unclosed, interior text)`. Walks `body.splitlines(keepends=True)` --
    stripping only each line's own ending before matching the CommonMark
    fence patterns, so CRLF is recognized and every other byte, including
    the fence's own line endings, is preserved for the caller. Only one
    fence is ever open at a time, matching `_live_line_entries`: an
    already-open fence, recognized or not, blocks a new opening delimiter
    from being recognized until it closes.
    """
    lines = body.splitlines(keepends=True)
    matches: list[tuple[int, int | None, str]] = []
    index = 0
    open_start: int | None = None
    open_char = ""
    open_length = 0
    open_recognized = False
    while index < len(lines):
        bare = _line_without_ending(lines[index])
        if open_start is None:
            opening = FENCE_OPENING_PATTERN.match(bare)
            if opening is not None:
                run = opening.group("run")
                info = bare[opening.end() :].strip(" \t")
                open_start, open_char, open_length = index, run[0], len(run)
                open_recognized = info == AGENT_CLAIM_FENCE_INFO
            index += 1
            continue
        closing = FENCE_CLOSING_PATTERN.match(bare)
        if (
            closing is not None
            and closing.group("run")[0] == open_char
            and len(closing.group("run")) >= open_length
        ):
            if open_recognized:
                matches.append((open_start, index, "".join(lines[open_start + 1 : index])))
            open_start, open_recognized = None, False
        index += 1
    if open_start is not None and open_recognized:
        matches.append((open_start, None, ""))
    return matches


def _block_version_defect(data: dict[str, object]) -> ContractDefect | None:
    if "version" not in data:
        return ContractDefect("version", "version is required and must be 1")
    value = data["version"]
    if isinstance(value, bool) or value != BLOCK_VERSION:
        return ContractDefect("version", f"version must be exactly {BLOCK_VERSION}")
    return None


def _block_projection_defects(data: dict[str, object]) -> list[ContractDefect]:
    defects: list[ContractDefect] = []
    for key in ("now", "next", "done_when"):
        if key not in data:
            defects.append(ContractDefect(key, f"{key} is required"))
        elif not isinstance(data[key], str):
            defects.append(ContractDefect(key, f"{key} must be a string"))
    return defects


def _block_frozen_until_defects(data: dict[str, object]) -> list[ContractDefect]:
    if "frozen_until" not in data:
        return []
    value = data["frozen_until"]
    if not isinstance(value, dict):
        return [
            ContractDefect(
                "frozen_until.trigger", "frozen_until must be a table with trigger and ruled_on"
            )
        ]
    defects: list[ContractDefect] = []
    trigger = value.get("trigger")
    if not isinstance(trigger, str) or not trigger.strip():
        defects.append(
            ContractDefect(
                "frozen_until.trigger", "frozen_until.trigger must be a non-empty string"
            )
        )
    ruled_on = value.get("ruled_on")
    if type(ruled_on) is not date:
        defects.append(
            ContractDefect(
                "frozen_until.ruled_on", "frozen_until.ruled_on must be a TOML local date"
            )
        )
    unknown = sorted(set(value) - {"trigger", "ruled_on"})
    defects.extend(
        ContractDefect(f"frozen_until.{key}", f"unknown key frozen_until.{key}") for key in unknown
    )
    return defects


SCOPE_MUST_NAME_A_PATH = "must name at least one path"


def _scope_value_defect(field_name: str, value: object) -> ContractDefect | None:
    """One `scope` field's own refusal -- the block's top-level `scope`
    (`field_name="scope"`) or one `[[slice]]` row's own
    (`field_name="slice[N].scope"`) -- issue #331. Called only once the
    caller already knows the key is present; a present value must be a
    non-empty list, refused with this module's own sentence, and every
    entry must pass `protocol.valid_scope` -- the one path grammar `claim`
    already owns, whose own refusal becomes the defect sentence verbatim
    rather than a second grammar invented here."""
    if not isinstance(value, list) or not value:
        return ContractDefect(field_name, f"{field_name} {SCOPE_MUST_NAME_A_PATH}")
    try:
        protocol.valid_scope(value)
    except protocol.InvalidClaimMarkerError as error:
        return ContractDefect(field_name, str(error))
    return None


def _block_scope_defects(data: dict[str, object]) -> list[ContractDefect]:
    if "scope" not in data:
        return []
    defect = _scope_value_defect("scope", data["scope"])
    return [defect] if defect is not None else []


def _block_size_defect(data: dict[str, object]) -> ContractDefect | None:
    if "size" not in data:
        return None
    value = data["size"]
    if isinstance(value, str) and value in SIZE_VALUES:
        return None
    return ContractDefect("size", "size must be S, M, or L")


def _block_whole_defect(data: dict[str, object]) -> ContractDefect | None:
    """The block's own top-level `whole = "<reason>"` (issue #399), the
    caller's own `--whole REASON` read from the item itself when a claim or
    a start names none: absent means no stored reason, never a default;
    present, it must be one non-blank sentence, the same bound `--whole`
    itself already enforces at the CLI boundary."""
    if "whole" not in data:
        return None
    value = data["whole"]
    if isinstance(value, str) and value.strip():
        return None
    return ContractDefect("whole", "whole must be a non-empty string")


def _canonical_scope(value: object) -> tuple[str, ...]:
    """`value`'s validated scope entries, in `protocol.valid_scope`'s own
    canonical (sorted, deduplicated) order -- the one scope normaliser a
    live claim's own scope is built through too, so a body's projected
    `scope` and a claim's recorded `scope` stay comparable as tuples
    (issue #331). Callable only once a schema check (`_block_scope_defects`,
    `_block_slice_entry_defects`) has already proven `value` valid."""
    return protocol.valid_scope(value)


def _record_timestamp_defect(value: object, key_name: str) -> ContractDefect | None:
    if not isinstance(value, str) or protocol.RFC3339_TIMESTAMP_PATTERN.fullmatch(value) is None:
        return ContractDefect(
            f"record.{key_name}", f"record.{key_name} must be an RFC 3339 UTC timestamp"
        )
    return None


def _record_identity_defects(value: dict[str, object]) -> list[ContractDefect]:
    """`[record]`'s own identity fields: `title`, `state`, `kind`, `labels`,
    `blocked_by` -- split from `_block_record_defects` only to stay under
    one function's branch budget; the two together are the whole table."""
    defects: list[ContractDefect] = []
    title = value.get("title")
    if not isinstance(title, str) or not title.strip():
        defects.append(ContractDefect("record.title", "record.title must be a non-empty string"))
    if value.get("state") not in RECORD_STATES:
        defects.append(ContractDefect("record.state", "record.state must be open or closed"))
    kind = value.get("kind")
    if kind is not None and kind not in set(ItemKind):
        defects.append(ContractDefect("record.kind", "record.kind must be a known item kind"))
    labels = value.get("labels", [])
    if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
        defects.append(ContractDefect("record.labels", "record.labels must be an array of strings"))
    blocked_by = value.get("blocked_by", [])
    if not isinstance(blocked_by, list) or not all(isinstance(item, str) for item in blocked_by):
        defects.append(
            ContractDefect("record.blocked_by", "record.blocked_by must be an array of item ids")
        )
    return defects


def _record_relation_defects(value: dict[str, object]) -> list[ContractDefect]:
    """`[record]`'s remaining fields: `parent`, `origin`, the three
    timestamps, and the unknown-key sweep."""
    defects: list[ContractDefect] = []
    parent = value.get("parent")
    if parent is not None and not isinstance(parent, str):
        defects.append(ContractDefect("record.parent", "record.parent must be an item id string"))
    origin = value.get("origin")
    if origin is not None and (
        not isinstance(origin, str) or ORIGIN_PATTERN.fullmatch(origin) is None
    ):
        defects.append(
            ContractDefect("record.origin", f"record.origin must be {ORIGIN_GRAMMAR_HINT}")
        )
    for key_name in ("created_at", "updated_at"):
        defect = _record_timestamp_defect(value.get(key_name), key_name)
        if defect is not None:
            defects.append(defect)
    closed_at = value.get("closed_at")
    if closed_at is not None:
        defect = _record_timestamp_defect(closed_at, "closed_at")
        if defect is not None:
            defects.append(defect)
    if value.get("state") == "closed" and closed_at is None:
        defects.append(
            ContractDefect(
                "record.closed_at", "record.closed_at is required when record.state is closed"
            )
        )
    unknown = sorted(set(value) - RECORD_KEYS)
    defects.extend(ContractDefect(f"record.{key}", f"unknown key record.{key}") for key in unknown)
    return defects


def _block_record_defects(data: dict[str, object]) -> list[ContractDefect]:
    """`[record]` (issue #248), valid only when this parse is storage-gated
    to allow it at all (`_block_schema_defects`'s caller): the identity and
    relations a GitHub issue would otherwise carry natively. `items.py`
    trusts every field's shape once this returns no defect -- it never
    re-validates what this function already checked."""
    if RECORD_KEY not in data:
        return []
    value = data[RECORD_KEY]
    if not isinstance(value, dict):
        return [ContractDefect(RECORD_KEY, f"{RECORD_KEY} must be a table")]
    return _record_identity_defects(value) + _record_relation_defects(value)


def _block_expectation_variant_defects(
    prefix: str, entry: dict[str, object]
) -> list[ContractDefect]:
    has_default, has_ruling, has_ruled_on = (
        "default" in entry,
        "ruling" in entry,
        "ruled_on" in entry,
    )
    if has_default and (has_ruling or has_ruled_on):
        return [
            ContractDefect(
                f"{prefix}.default",
                f"{prefix} must be proposed (default) or ruled (ruling, ruled_on), not both",
            )
        ]
    if has_default:
        if entry["default"] not in BLOCK_EXPECTATION_DEFAULTS:
            return [
                ContractDefect(f"{prefix}.default", f"{prefix}.default must be yes, no, or later")
            ]
        return []
    if has_ruling or has_ruled_on:
        defects = []
        if entry.get("ruling") not in BLOCK_EXPECTATION_RULINGS:
            defects.append(
                ContractDefect(f"{prefix}.ruling", f"{prefix}.ruling must be yes, no, or later")
            )
        if type(entry.get("ruled_on")) is not date:
            defects.append(
                ContractDefect(f"{prefix}.ruled_on", f"{prefix}.ruled_on must be a TOML local date")
            )
        return defects
    return [
        ContractDefect(
            f"{prefix}.default", f"{prefix} must carry default, or both ruling and ruled_on"
        )
    ]


def _expectation_question_defect(value: object) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return "must be a non-empty string"
    if len(value) > EXPECTATION_QUESTION_MAXIMUM_CHARACTERS:
        return f"must be at most {EXPECTATION_QUESTION_MAXIMUM_CHARACTERS} characters"
    return None


def _expectation_example_defect(value: object) -> str | None:
    return None if isinstance(value, str) and value.strip() else "must be a non-empty string"


_EXPECTATION_PICTURE_EVENT_HANDLER_ATTRIBUTE = re.compile(r"[\s/]on[a-z]+\s*=", re.IGNORECASE)
# `/` joins `[\s:]` as a separator so `<a/href=…>` (slash instead of a space
# before the attribute) is still caught; the final alternative's class adds
# `\s` so a greedy `\s*` that backtracks into the gap between `=` and a
# quoted value (`href = "#x"`) cannot land on that whitespace and misread it
# as an unquoted external value -- both #300 residuals of #234's own rule.
_EXPECTATION_PICTURE_EXTERNAL_HREF = re.compile(
    r'(?:^|[\s:/])href\s*=\s*(?:"(?!#)|\'(?!#)|(?![\s"\'#]))', re.IGNORECASE
)
# SMIL can retarget `href` without ever writing `href=` itself (issue #300,
# residual of #234): `<animate attributeName="href" to="http://…">` swaps
# the target after the document loads, and `xlink:href` is the same escape
# under its namespaced spelling. Refused only when both attributes sit on the
# same element (Codex Terra review of #300): scanning each `<...>` tag on its
# own keeps an unrelated `attributeName`/`to` pair on a different element from
# falsely refusing the picture. Both attributes accept SMIL's own quoting
# forms -- double-quoted, single-quoted, or bare -- mirroring
# `_EXPECTATION_PICTURE_EXTERNAL_HREF`'s own unquoted-value check above.
_EXPECTATION_PICTURE_SMIL_HREF_ATTRIBUTE = re.compile(
    r"attributename\s*=\s*(?:\"(?:xlink:)?href\"|'(?:xlink:)?href'|(?:xlink:)?href(?=[\s/>]))",
    re.IGNORECASE,
)
# `values` lists a `;`-separated sequence of keyframes (SMIL's own syntax),
# so a rule that only reads the first character after `=` misses a later
# external segment such as `values="#a;http://evil.example"` (issue #300
# residual, Codex delta). The regex only captures the raw attribute value in
# each of SMIL's own quoting forms; `_expectation_picture_smil_external_target`
# below splits it on `;` and refuses if any trimmed segment does not start
# with `#`, so `to`/`from` (which never carry a `;`) are covered by the same
# one-segment case.
_EXPECTATION_PICTURE_SMIL_TARGET_ATTRIBUTE = re.compile(
    r"""\b(?:to|from|values)\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'>]*))""", re.IGNORECASE
)
_EXPECTATION_PICTURE_SVG_ELEMENT = re.compile(r"<[^<>]+>")


def _expectation_picture_smil_external_target(element: str) -> bool:
    """Whether `element` sets SMIL `to`, `from`, or `values` to any
    `;`-separated segment that does not start with `#` (issue #300
    residual): `values` can list several keyframes, so every segment is
    checked, not only the value's first character."""
    for match in _EXPECTATION_PICTURE_SMIL_TARGET_ATTRIBUTE.finditer(element):
        raw = next(group for group in match.groups() if group is not None)
        if any(not segment.strip().startswith("#") for segment in raw.split(";")):
            return True
    return False


def _expectation_picture_smil_external_href(value: str) -> bool:
    """Whether any single SVG element in `value` both retargets `href` (or
    `xlink:href`) via SMIL's `attributeName` and points it outside the
    document -- scanning element-by-element instead of across the whole
    document (issue #300, Codex Terra review)."""
    return any(
        _EXPECTATION_PICTURE_SMIL_HREF_ATTRIBUTE.search(element)
        and _expectation_picture_smil_external_target(element)
        for element in _EXPECTATION_PICTURE_SVG_ELEMENT.findall(value)
    )


def _expectation_picture_content_refusals(value: str) -> tuple[tuple[bool, str], ...]:
    """Every path an inline SVG can run script or reach outside the
    document, checked case-insensitively: `refused` paired with the
    sentence for the first one `value` matches, in this fixed order."""
    lowered = value.lower()
    return (
        ("<script" in lowered, "must not contain <script>"),
        ("<foreignobject" in lowered, "must not contain <foreignObject>"),
        (
            bool(_EXPECTATION_PICTURE_EVENT_HANDLER_ATTRIBUTE.search(value)),
            "must not contain an event-handler attribute",
        ),
        ("javascript:" in lowered, "must not contain a javascript: reference"),
        ("data:" in lowered, "must not contain a data: reference"),
        (
            bool(_EXPECTATION_PICTURE_EXTERNAL_HREF.search(value)),
            "must not reference an href outside the document",
        ),
        (
            _expectation_picture_smil_external_href(value),
            "must not animate href to an external target",
        ),
        ("url(" in lowered, "must not contain a url() reference"),
        ("<iframe" in lowered, "must not contain <iframe>"),
        ("<embed" in lowered, "must not contain <embed>"),
        ("<object" in lowered, "must not contain <object>"),
        ("srcdoc" in lowered, "must not contain srcdoc"),
    )


def _expectation_picture_defect(value: object) -> str | None:
    """The refusal sentence for an invalid `[[expectation]]` picture, or
    `None` for a valid one (issue #295): an inline SVG, rooted at `<svg`, at
    most `EXPECTATION_PICTURE_MAXIMUM_BYTES`, and free of every refusal
    `_expectation_picture_content_refusals` names. The one owner the body
    parser's defects and `append_expectation`'s pre-write refusal both
    call."""
    if not isinstance(value, str):
        return "must be a string"
    if len(value.encode("utf-8")) > EXPECTATION_PICTURE_MAXIMUM_BYTES:
        return f"must be at most {EXPECTATION_PICTURE_MAXIMUM_BYTES} bytes"
    if not value.strip().startswith("<svg"):
        return "must be inline SVG rooted at <svg>"
    for refused, reason in _expectation_picture_content_refusals(value):
        if refused:
            return reason
    return None


# The three optional `[[expectation]]` card fields (issue #295), each with
# its own refusal-sentence check -- shared, in this fixed order, by the body
# parser's defects (`_block_expectation_optional_field_defects`) and by
# `append_expectation`'s pre-write validation, so the rule is owned once.
_EXPECTATION_OPTIONAL_FIELDS: tuple[tuple[str, Callable[[object], str | None]], ...] = (
    ("question", _expectation_question_defect),
    ("example", _expectation_example_defect),
    ("picture", _expectation_picture_defect),
)
_EXPECTATION_KNOWN_KEYS = frozenset(
    {"text", "default", "ruling", "ruled_on", *(key for key, _ in _EXPECTATION_OPTIONAL_FIELDS)}
)


def _block_expectation_optional_field_defects(
    prefix: str, entry: dict[str, object]
) -> list[ContractDefect]:
    defects: list[ContractDefect] = []
    for key, check in _EXPECTATION_OPTIONAL_FIELDS:
        if key not in entry:
            continue
        reason = check(entry[key])
        if reason is not None:
            defects.append(ContractDefect(f"{prefix}.{key}", f"{prefix}.{key} {reason}"))
    return defects


def _block_expectation_entry_defects(index: int, entry: object) -> list[ContractDefect]:
    prefix = f"expectation[{index}]"
    if not isinstance(entry, dict):
        return [ContractDefect(prefix, f"{prefix} must be a table")]
    defects: list[ContractDefect] = []
    text = entry.get("text")
    if not isinstance(text, str) or not text.strip():
        defects.append(
            ContractDefect(f"{prefix}.text", f"{prefix}.text must be a non-empty string")
        )
    defects.extend(_block_expectation_variant_defects(prefix, entry))
    defects.extend(_block_expectation_optional_field_defects(prefix, entry))
    unknown = sorted(set(entry) - _EXPECTATION_KNOWN_KEYS)
    defects.extend(
        ContractDefect(f"{prefix}.{key}", f"unknown key {prefix}.{key}") for key in unknown
    )
    return defects


def _block_expectation_defects(entries: list[object]) -> list[ContractDefect]:
    return [
        defect
        for index, entry in enumerate(entries)
        for defect in _block_expectation_entry_defects(index, entry)
    ]


def _block_slice_entry_defects(
    index: int, entry: object, seen_indices: dict[int, int]
) -> list[ContractDefect]:
    prefix = f"slice[{index}]"
    if not isinstance(entry, dict):
        return [ContractDefect(prefix, f"{prefix} must be a table")]
    defects: list[ContractDefect] = []
    slice_index = entry.get("index")
    if not isinstance(slice_index, int) or isinstance(slice_index, bool) or slice_index <= 0:
        defects.append(
            ContractDefect(f"{prefix}.index", f"{prefix}.index must be a positive integer")
        )
    elif slice_index in seen_indices:
        defects.append(
            ContractDefect(
                f"{prefix}.index", f"{prefix}.index duplicates slice index {slice_index}"
            )
        )
    else:
        seen_indices[slice_index] = index
    title = entry.get("title")
    if not isinstance(title, str) or not title.strip():
        defects.append(
            ContractDefect(f"{prefix}.title", f"{prefix}.title must be a non-empty string")
        )
    if "scope" in entry:
        scope_defect = _scope_value_defect(f"{prefix}.scope", entry["scope"])
        if scope_defect is not None:
            defects.append(scope_defect)
    unknown = sorted(set(entry) - {"index", "title", "scope"})
    defects.extend(
        ContractDefect(f"{prefix}.{key}", f"unknown key {prefix}.{key}") for key in unknown
    )
    return defects


def _block_slice_defects(entries: list[object]) -> list[ContractDefect]:
    seen_indices: dict[int, int] = {}
    defects: list[ContractDefect] = []
    for index, entry in enumerate(entries):
        defects.extend(_block_slice_entry_defects(index, entry, seen_indices))
    return defects


def _block_array_or_defect(
    data: dict[str, object], key: str
) -> tuple[list[object], ContractDefect | None]:
    value = data.get(key, [])
    if not isinstance(value, list):
        return [], ContractDefect(key, f"{key} must be an array of tables")
    return value, None


def _block_schema_defects(data: dict[str, object], storage: Storage) -> tuple[ContractDefect, ...]:
    defects: list[ContractDefect] = []
    version_defect = _block_version_defect(data)
    if version_defect is not None:
        defects.append(version_defect)
    defects.extend(_block_projection_defects(data))
    defects.extend(_block_frozen_until_defects(data))
    defects.extend(_block_scope_defects(data))
    size_defect = _block_size_defect(data)
    if size_defect is not None:
        defects.append(size_defect)
    whole_defect = _block_whole_defect(data)
    if whole_defect is not None:
        defects.append(whole_defect)
    expectations, expectation_defect = _block_array_or_defect(data, "expectation")
    defects.append(expectation_defect) if expectation_defect else defects.extend(
        _block_expectation_defects(expectations)
    )
    slices, slice_defect = _block_array_or_defect(data, "slice")
    defects.append(slice_defect) if slice_defect else defects.extend(_block_slice_defects(slices))
    allowed_keys = BLOCK_TOP_LEVEL_KEYS
    if storage is Storage.STATE_REF:
        allowed_keys = allowed_keys | {RECORD_KEY}
        defects.extend(_block_record_defects(data))
    unknown = sorted(set(data) - allowed_keys)
    defects.extend(ContractDefect(key, f"unknown top-level key {key}") for key in unknown)
    return tuple(defects)


def _malformed_parsed_body(defects: tuple[ContractDefect, ...]) -> ParsedBody:
    return ParsedBody(
        contract=Contract(None, None, None, defects),
        contract_complete=False,
        projectionless=False,
        expectation_state=ExpectationState.NONE,
        expectation_progress=ExpectationProgress(0, 0),
        ruling_date=None,
        frozen_trigger=None,
        scope=None,
        slices=(),
        read_state=BodyReadState.MALFORMED,
    )


_NO_BLOCK_PARSED_BODY = _malformed_parsed_body(
    (ContractDefect(AGENT_CLAIM_FENCE_INFO, "no agent-claim block"),)
)


def _block_array(data: dict[str, object], key: str) -> list[object]:
    value = data.get(key)
    return value if isinstance(value, list) else []


def _block_expectation_dicts(data: dict[str, object]) -> list[dict[str, object]]:
    return [entry for entry in _block_array(data, "expectation") if isinstance(entry, dict)]


def _block_expectation_state(expectations: list[dict[str, object]]) -> ExpectationState:
    if not expectations:
        return ExpectationState.NONE
    if any("default" in entry for entry in expectations):
        return ExpectationState.PROPOSED
    return ExpectationState.RULED


def _block_expectation_progress(expectations: list[dict[str, object]]) -> ExpectationProgress:
    return ExpectationProgress(
        open=sum(1 for entry in expectations if "default" in entry), total=len(expectations)
    )


def _block_ruling_date(expectations: list[dict[str, object]]) -> date | None:
    ruled_dates = [
        entry["ruled_on"]
        for entry in expectations
        if "ruled_on" in entry and type(entry["ruled_on"]) is date
    ]
    return min(ruled_dates) if ruled_dates else None


def _block_frozen_trigger(data: dict[str, object]) -> str | None:
    value = data.get("frozen_until")
    trigger = value.get("trigger") if isinstance(value, dict) else None
    return trigger if isinstance(trigger, str) else None


def _block_slices(data: dict[str, object]) -> tuple[SliceRow, ...]:
    """Every still-undispatched `[[slice]]` entry: `cut` removes an entry
    from the block at the moment it links a child to it, so whatever is left
    here is exactly what is still uncut. Each row's own `scope` (issue #331)
    is canonicalized the same way the block's top-level one is."""
    return tuple(
        SliceRow(
            cast(int, entry["index"]),
            cast(str, entry["title"]),
            _canonical_scope(entry["scope"]) if "scope" in entry else None,
        )
        for entry in _block_array(data, "slice")
        if isinstance(entry, dict)
        and isinstance(entry.get("index"), int)
        and isinstance(entry.get("title"), str)
    )


def _valid_block_parsed_body(data: dict[str, object], storage: Storage) -> ParsedBody:
    now, next_value, done_when = (
        cast(str, data["now"]).strip(),
        cast(str, data["next"]).strip(),
        cast(str, data["done_when"]).strip(),
    )
    expectations = _block_expectation_dicts(data)
    expectation_state = _block_expectation_state(expectations)
    record = data.get(RECORD_KEY) if storage is Storage.STATE_REF else None
    return ParsedBody(
        contract=Contract(now, next_value, done_when, ()),
        contract_complete=bool(now and next_value and done_when),
        projectionless=not (now or next_value or done_when),
        expectation_state=expectation_state,
        expectation_progress=_block_expectation_progress(expectations),
        ruling_date=(
            _block_ruling_date(expectations)
            if expectation_state is ExpectationState.RULED
            else None
        ),
        frozen_trigger=_block_frozen_trigger(data),
        scope=_canonical_scope(data["scope"]) if "scope" in data else None,
        slices=_block_slices(data),
        read_state=BodyReadState.VALID,
        size=metrics.Size(data["size"]) if "size" in data else None,
        whole=cast(str, data["whole"]) if "whole" in data else None,
        record=cast("Mapping[str, object] | None", record),
    )


def parse_body(body: str, *, storage: Storage = Storage.GITHUB) -> ParsedBody:
    """The one read of a work-item body (issue #150, narrowed to one grammar
    by #204): the typed `agent-claim` block. Every consumer reads the
    returned `ParsedBody` instead of re-parsing the raw body. Human prose
    around the block is never parsed -- another repository may own its own
    section headings in the same body.

    `storage` gates the one storage-specific extension, `[record]` (issue
    #248): legal, and validated, only under `Storage.STATE_REF`; an unknown
    top-level key under `Storage.GITHUB`, the default every existing caller
    keeps reading with.
    """
    fences = _agent_claim_fence_matches(body)
    if not fences:
        return _NO_BLOCK_PARSED_BODY
    if len(fences) > 1:
        return _malformed_parsed_body(
            (
                ContractDefect(
                    AGENT_CLAIM_FENCE_INFO, "multiple agent-claim blocks; exactly one is allowed"
                ),
            )
        )
    _start, end, content = fences[0]
    if end is None:
        return _malformed_parsed_body(
            (ContractDefect(AGENT_CLAIM_FENCE_INFO, "unclosed agent-claim block"),)
        )
    try:
        data = tomllib.loads(content)
    except tomllib.TOMLDecodeError as error:
        return _malformed_parsed_body(
            (
                ContractDefect(
                    AGENT_CLAIM_FENCE_INFO, f"agent-claim block is not valid TOML: {error}"
                ),
            )
        )
    defects = _block_schema_defects(data, storage)
    if defects:
        return _malformed_parsed_body(defects)
    return _valid_block_parsed_body(data, storage)


@dataclass(frozen=True)
class LocatedBlock:
    """A valid `agent-claim` block's decoded TOML, plus the byte-exact span
    of its interior -- between the fence lines, which stay byte-identical --
    and the newline convention new interior lines are rendered with (#150
    §4/§7). Callable only on a body `parse_body` already read as `VALID`;
    `cut` refuses a malformed target before ever calling this."""

    data: dict[str, object]
    content_start: int
    content_end: int
    newline: str


def locate_agent_claim_block(body: str) -> LocatedBlock:
    lines = body.splitlines(keepends=True)
    matches = _agent_claim_fence_matches(body)
    if not matches:
        raise protocol.ClaimError("locate_agent_claim_block found no recognized agent-claim fence")
    start_line, end_line, content = matches[0]
    if end_line is None:
        raise protocol.ClaimError("locate_agent_claim_block found no closed agent-claim fence")
    content_start = sum(len(line) for line in lines[: start_line + 1])
    content_end = sum(len(line) for line in lines[:end_line])
    newline = _line_ending(lines[start_line]) or "\n"
    return LocatedBlock(tomllib.loads(content), content_start, content_end, newline)


# The shape one decoded JSON object takes -- one alias so `cast` names a
# real type instead of repeating the `"dict[str, object]"` string literal
# (python:S1192).
_JsonObject = dict[str, object]

# The shape a decoded homogeneous array of tables (`[[expectation]]`,
# `[[slice]]`) or an `asdict`'d list of dataclasses takes -- one alias so
# `cast` names a real type instead of repeating the string.
_JsonRows = list[_JsonObject]

# `protocol.toml_string` is this repository's one TOML basic-string writer
# (issue #378): it lives below this module in the Layers contract, so it is
# imported rather than kept as a second escape table here.

_TOML_MULTILINE_STRING_ESCAPES = {"\\": "\\\\", '"': '\\"'}


def _toml_multiline_string(value: str) -> str:
    """A TOML multi-line basic string for `value` -- an `[[expectation]]`
    `picture`'s inline SVG (issue #295), which needs literal newlines a
    single-line basic string cannot hold. Backslashes and quotes are
    escaped so no run of the content can be mistaken for the closing
    `\"\"\"`; raw newlines stay literal. `tomllib.loads` reads it back to
    `value` unchanged -- the leading newline right after the opening
    delimiter is the one TOML trims automatically, so none is added here."""
    escaped = "".join(_TOML_MULTILINE_STRING_ESCAPES.get(char, char) for char in value)
    return f'"""\n{escaped}"""'


def _render_frozen_until(data: Mapping[str, object]) -> list[str]:
    frozen_until = data.get("frozen_until")
    if not isinstance(frozen_until, dict):
        return []
    ruled_on = cast(date, frozen_until["ruled_on"])
    return [
        "",
        f"frozen_until = {{ trigger = {protocol.toml_string(frozen_until['trigger'])}, "
        f"ruled_on = {ruled_on.isoformat()} }}",
    ]


def _render_scope_array(values: object) -> str:
    """`values`'s scope entries as a canonical TOML array: the one rendering
    `_render_scope` (top-level) and `_render_slices` (per row) both call,
    routed through `protocol.valid_scope` -- the one scope canonicalizer
    (issue #331 REVISE finding 2), rather than a second sort/dedupe owner
    here. Callable only once a schema check has already proven `values`
    valid, so this never itself refuses a duplicate."""
    entries = protocol.valid_scope(values)
    return "[" + ", ".join(protocol.toml_string(value) for value in entries) + "]"


def _render_scope(data: Mapping[str, object]) -> list[str]:
    if "scope" not in data:
        return []
    return ["", f"scope = {_render_scope_array(data['scope'])}"]


def _render_size(data: Mapping[str, object]) -> list[str]:
    if "size" not in data:
        return []
    return ["", f"size = {protocol.toml_string(data['size'])}"]


def _render_whole(data: Mapping[str, object]) -> list[str]:
    if "whole" not in data:
        return []
    return ["", f"whole = {protocol.toml_string(data['whole'])}"]


def _render_expectations(data: Mapping[str, object]) -> list[str]:
    lines: list[str] = []
    for expectation in cast(_JsonRows, data.get("expectation", [])):
        lines.extend(("", "[[expectation]]", f"text = {protocol.toml_string(expectation['text'])}"))
        if "default" in expectation:
            lines.append(f"default = {protocol.toml_string(expectation['default'])}")
        else:
            ruled_on = cast(date, expectation["ruled_on"])
            lines.append(f"ruling = {protocol.toml_string(expectation['ruling'])}")
            lines.append(f"ruled_on = {ruled_on.isoformat()}")
        if "question" in expectation:
            lines.append(f"question = {protocol.toml_string(expectation['question'])}")
        if "example" in expectation:
            lines.append(f"example = {protocol.toml_string(expectation['example'])}")
        if "picture" in expectation:
            lines.append(f"picture = {_toml_multiline_string(cast(str, expectation['picture']))}")
    return lines


def _render_slices(data: Mapping[str, object]) -> list[str]:
    if "slice" not in data:
        return []
    slices = cast(_JsonRows, data["slice"])
    if not slices:
        return ["", "slice = []"]
    lines: list[str] = []
    for entry in slices:
        lines.extend(
            (
                "",
                "[[slice]]",
                f"index = {entry['index']}",
                f"title = {protocol.toml_string(entry['title'])}",
            )
        )
        if "scope" in entry:
            lines.append(f"scope = {_render_scope_array(entry['scope'])}")
    return lines


def _render_record_array(values: object) -> str:
    entries = cast("list[str]", values)
    return "[" + ", ".join(protocol.toml_string(value) for value in entries) + "]"


def _render_record(data: Mapping[str, object]) -> list[str]:
    """`[record]` (issue #248), rendered only when `data` carries one --
    every GitHub-stored block never does. Optional fields (`kind`, `parent`,
    `origin`, `closed_at`) are omitted entirely rather than written `= ""`,
    matching how `parse_body`/`_block_record_defects` read their absence as
    `None`, never as an empty string."""
    if RECORD_KEY not in data:
        return []
    record = cast(Mapping[str, object], data[RECORD_KEY])
    lines = [
        "",
        f"[{RECORD_KEY}]",
        f"title = {protocol.toml_string(record['title'])}",
        f"state = {protocol.toml_string(record['state'])}",
    ]
    if record.get("kind") is not None:
        lines.append(f"kind = {protocol.toml_string(record['kind'])}")
    lines.append(f"labels = {_render_record_array(record.get('labels', []))}")
    lines.append(f"blocked_by = {_render_record_array(record.get('blocked_by', []))}")
    if record.get("parent") is not None:
        lines.append(f"parent = {protocol.toml_string(record['parent'])}")
    if record.get("origin") is not None:
        lines.append(f"origin = {protocol.toml_string(record['origin'])}")
    lines.append(f"created_at = {protocol.toml_string(record['created_at'])}")
    lines.append(f"updated_at = {protocol.toml_string(record['updated_at'])}")
    if record.get("closed_at") is not None:
        lines.append(f"closed_at = {protocol.toml_string(record['closed_at'])}")
    return lines


def render_block(data: Mapping[str, object], newline: str = "\n") -> str:
    """The canonical `agent-claim` block interior for `data` (#150 §4):
    schema key order, TOML-safe strings, unquoted dates, ending in
    `newline` so a following fence line starts clean. Production caller:
    block-mode `cut`; there is no standalone validator."""
    lines = [f"version = {data['version']}"]
    lines.extend(
        f"{key} = {protocol.toml_string(data[key])}" for key in ("now", "next", "done_when")
    )
    lines.extend(_render_frozen_until(data))
    lines.extend(_render_scope(data))
    lines.extend(_render_size(data))
    lines.extend(_render_whole(data))
    lines.extend(_render_expectations(data))
    lines.extend(_render_slices(data))
    lines.extend(_render_record(data))
    return newline.join((*lines, ""))


def replace_agent_claim_block(body: str, located: LocatedBlock, data: Mapping[str, object]) -> str:
    """`body` with its one `agent-claim` block's interior replaced by
    `render_block(data, located.newline)` -- pure, changing only that span
    and preserving every other byte, fence lines included."""
    return (
        body[: located.content_start]
        + render_block(data, located.newline)
        + body[located.content_end :]
    )


EXPECTATION_LINE_TEXT_MAXIMUM = 100


@dataclass(frozen=True)
class ExpectationLine:
    """One `[[expectation]]` entry as `rule --line`, `ask`, and `rulings`
    see it: `index` is its 1-based position in block order -- what `rule
    --line` accepts and what `rulings` prints -- `text` its full prose, and
    `ruling`/`ruled_on` present only once a `rule` call has replaced its
    `default`. `default` is the line's own default (`str | None`, `None`
    once ruled) -- `board_html`'s open-line cards read it from here rather
    than re-parsing the block. `question`/`example`/`picture` (issue #295)
    are the card's optional operator-language heading, illustration
    sentence, and inline SVG -- `None` when `aco ask` was not given them,
    in which case a card falls back to `text`."""

    index: int
    text: str
    ruling: str | None
    ruled_on: date | None
    default: str | None = None
    question: str | None = None
    example: str | None = None
    picture: str | None = None


def expectation_lines(
    body: str, *, storage: Storage = Storage.GITHUB
) -> tuple[ExpectationLine, ...]:
    """Every `[[expectation]]` entry of `body`'s `agent-claim` block, in
    block order -- the one projection `rulings`, `rule --line`, and `ask`'s
    fresh index all share, so a printed index always matches what `rule`
    accepts. Empty for a body with no block, no expectations, or one
    `parse_body` reads as MALFORMED -- a malformed body's lines are not
    addressable until it is fixed by hand. `storage` is forwarded to
    `parse_body` unchanged (issue #248)."""
    if parse_body(body, storage=storage).read_state is not BodyReadState.VALID:
        return ()
    entries = _block_expectation_dicts(locate_agent_claim_block(body).data)
    return tuple(
        ExpectationLine(
            index=position,
            text=cast(str, entry["text"]),
            ruling=cast(str | None, entry.get("ruling")),
            ruled_on=cast("date | None", entry.get("ruled_on")),
            default=cast(str | None, entry.get("default")),
            question=cast(str | None, entry.get("question")),
            example=cast(str | None, entry.get("example")),
            picture=cast(str | None, entry.get("picture")),
        )
        for position, entry in enumerate(entries, start=1)
    )


def expectation_line_state(line: ExpectationLine) -> str:
    """`open`, or `ruled <ruling> <ruled_on>` -- the one state text
    `rulings`' human form prints; `--json` carries `ruling`/`ruled_on`
    directly and does not call this (issue #379)."""
    if line.ruling is None:
        return "open"
    ruled_on = cast(date, line.ruled_on)
    return f"ruled {line.ruling} {ruled_on.isoformat()}"


def expectation_line_summary(line: ExpectationLine) -> str:
    """`line.text` on one line, truncated to `EXPECTATION_LINE_TEXT_MAXIMUM`
    characters -- `rulings`' human form; `--json` carries the full text."""
    one_line = " ".join(line.text.split())
    maximum = EXPECTATION_LINE_TEXT_MAXIMUM
    return one_line if len(one_line) <= maximum else one_line[: maximum - 1] + "…"


class ExpectationAlreadyRuledError(protocol.ClaimError):
    """`rule_expectation`'s own already-ruled refusal (issue #396) -- a
    distinct type from `ExpectationOutOfRangeError` so a `--json`-emitting
    caller can choose `already_ruled` without parsing the refusal prose."""


class ExpectationOutOfRangeError(protocol.ClaimError):
    """`rule_expectation`'s own out-of-range `--line` refusal (issue #396)."""


def rule_expectation(
    body: str, index: int, ruling: str, ruled_on: date, *, note: str | None = None
) -> str:
    """`body` with its `index`-th (1-based, block order) `[[expectation]]`
    entry moved from proposed to ruled: `default` falls, `ruling` and
    `ruled_on` take its place. Byte-preserving outside that one entry
    (`locate_agent_claim_block` -> `replace_agent_claim_block`, #150 §4/§7 --
    the same pair `cut` writes through). `note`, when given, is appended to
    the line's own text as ` Anmerkung: <note>`: the schema has no dedicated
    note field, and the ruled line's own text is the one place a
    transcribed remark belongs.

    Refuses an already-ruled entry by name -- a changed ruling is a new
    line, never an overwrite -- and an out-of-range index. This is also the
    write path `board --serve` (#234) will call from a click, so it
    validates `ruling` itself rather than trust only its CLI caller.
    """
    if ruling not in BLOCK_EXPECTATION_RULINGS:
        raise protocol.ClaimError(
            f"ruling must be one of {', '.join(sorted(BLOCK_EXPECTATION_RULINGS))}"
        )
    located = locate_agent_claim_block(body)
    entries = _block_expectation_dicts(located.data)
    if not 1 <= index <= len(entries):
        raise ExpectationOutOfRangeError(
            f"line {index} out of range: this item has {len(entries)} expectation line(s)"
        )
    entry = entries[index - 1]
    if "ruling" in entry:
        raise ExpectationAlreadyRuledError(
            f"line {index} is already ruled; a changed ruling is a new line"
        )
    text = cast(str, entry["text"]) if note is None else f"{entry['text']} Anmerkung: {note}"
    ruled_entry: dict[str, object] = {"text": text, "ruling": ruling, "ruled_on": ruled_on}
    new_entries = [*entries[: index - 1], ruled_entry, *entries[index:]]
    new_data = {**located.data, "expectation": new_entries}
    return replace_agent_claim_block(body, located, new_data)


@dataclass(frozen=True)
class ExpectationCardFields:
    """The three optional `[[expectation]]` card fields `aco ask` may
    attach (issue #295), grouped so a caller passes one value instead of
    three positional strings that must stay paired: `question` and
    `example`, one operator-language sentence each, and `picture`, an
    inline SVG. Each defaults to absent, matching a line with none of
    them -- the card then falls back to `text`."""

    question: str | None = None
    example: str | None = None
    picture: str | None = None


class ExpectationTextError(protocol.ClaimError):
    """`append_expectation`'s own blank-`text` refusal (issue #396) -- a
    distinct type from `ExpectationFieldError` so a `--json`-emitting caller
    can choose `invalid_expectation` without parsing the refusal prose."""


class ExpectationFieldError(protocol.ClaimError):
    """`append_expectation`'s own optional-card-field refusal -- `question`,
    `example`, or `picture` failing its own content rule (issue #396) --
    carrying the failing `field` by name so a `--json`-emitting caller can
    choose `invalid_expectation` (`question`, `example`) or `invalid_picture`
    (`picture`) without parsing the refusal prose."""

    def __init__(self, field: str, message: str) -> None:
        super().__init__(message)
        self.field = field


def append_expectation(
    body: str, text: str, default: str, *, card: ExpectationCardFields | None = None
) -> str:
    """`body` with one fresh proposed `[[expectation]]` entry appended:
    `text` verbatim, `default` as given, plus whichever of `card`'s
    optional fields (issue #295) `aco ask` was given -- each validated by
    the same check `_block_expectation_optional_field_defects` reads a
    stored body with, so a value this refuses can never be written.
    Byte-preserving outside the appended entry, the same write path as
    `rule_expectation`."""
    if not text.strip():
        raise ExpectationTextError("expectation text must be a non-empty string")
    if default not in BLOCK_EXPECTATION_DEFAULTS:
        raise protocol.ClaimError(
            f"default must be one of {', '.join(sorted(BLOCK_EXPECTATION_DEFAULTS))}"
        )
    provided = asdict(card if card is not None else ExpectationCardFields())
    entry: dict[str, object] = {"text": text, "default": default}
    for key, check in _EXPECTATION_OPTIONAL_FIELDS:
        value = provided[key]
        if value is None:
            continue
        reason = check(value)
        if reason is not None:
            raise ExpectationFieldError(key, f"{key} {reason}")
        entry[key] = value
    located = locate_agent_claim_block(body)
    entries = _block_expectation_dicts(located.data)
    new_entries = [*entries, entry]
    new_data = {**located.data, "expectation": new_entries}
    return replace_agent_claim_block(body, located, new_data)


def missing_or_empty_sections(contract: Contract) -> tuple[str, ...]:
    """Every projection key a body-incomplete refusal names: an absent key
    and a fresh skeleton's empty string both count, even though both stay
    legitimate CONTRACT-column *presence* (`_contract_summary` is
    `None`-only for that column, matching #150 §5's rule that a block
    skeleton still shows `Now, Next, Done when`)."""
    return tuple(name for name, value in contract_fields(contract) if not value)


class BodyShapeVerdict(StrEnum):
    """Whether a body's own shape is one a builder can start from (issue
    #404): the third state beyond `BodyReadState.VALID`/`MALFORMED` -- a
    schema-valid block that still leaves a required section unfilled.
    `check <item>` and `body --check` both need to tell `malformed` from
    `incomplete` apart for their own `--json` `reason`, never by sniffing a
    defect sentence's own prefix."""

    VALID = BodyReadState.VALID.value
    MALFORMED = BodyReadState.MALFORMED.value
    INCOMPLETE = "incomplete"


@dataclass(frozen=True)
class BodyShapeCheck:
    """One body's own shape verdict and defect sentences (issue #404): the
    single read `check <item>` and `body --check` both share, so neither
    writes a second rendering of `body_defect_text`'s or the incomplete
    sentence's own text."""

    verdict: BodyShapeVerdict
    defects: tuple[str, ...]


def body_shape_check(body: str, *, storage: Storage = Storage.GITHUB) -> BodyShapeCheck:
    """Every finding a body's own shape can carry without asking a forge
    anything -- malformed (one sentence per schema defect) or incomplete
    (one joined sentence) -- paired with the verdict a caller's `--json`
    `reason` reads. `storage` gates the one storage-specific extension,
    `[record]` (issue #248)."""
    parsed = parse_body(body, storage=storage)
    if parsed.read_state is BodyReadState.MALFORMED:
        defects = tuple(body_defect_text(defect) for defect in parsed.contract.defects)
        return BodyShapeCheck(BodyShapeVerdict.MALFORMED, defects)
    missing = missing_or_empty_sections(parsed.contract)
    if missing:
        return BodyShapeCheck(
            BodyShapeVerdict.INCOMPLETE, (f"body incomplete: {', '.join(missing)}",)
        )
    return BodyShapeCheck(BodyShapeVerdict.VALID, ())
