"""Pure derivation and rendering for the read-only work board."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import cast

from . import protocol

DEFAULT_PRIORITY_LABELS = ("security", "data", "ci", "product", "ux", "cleanup")
CONFIG_PATH = Path(".agent-claim/board.toml")
IDEA_REFINEMENT_STEP = "Problem neu prüfen und Item verfeinern"
RULING_OLD_AFTER_LANDINGS = 10
STALE_IDLE_DAYS = 7
# CommonMark fence delimiters: at most 3 leading spaces, then a run of 3+
# backticks or 3+ tildes. An OPENING delimiter may carry an info string after
# the run (` ```python `); a CLOSING delimiter may not — only trailing
# spaces/tabs are allowed after the run (` ``` `, never ` ```python `), so
# `Closing`'s stricter pattern requires nothing but whitespace to follow.
# A 4-space-indented code block (CommonMark's other fencing form) is not
# modeled here; see `_live_text` for why that gap is safe.
FENCE_OPENING_PATTERN = re.compile(r"^ {0,3}(?P<run>`{3,}|~{3,})")
FENCE_CLOSING_PATTERN = re.compile(r"^ {0,3}(?P<run>`{3,}|~{3,})[ \t]*$")
REFERENCE_PATTERN = re.compile(r"(?<!\w)#([1-9]\d*)", re.ASCII)
# One issue named the way GitHub names it across repositories: `OWNER/REPO#n`,
# or `#n` for the repository the text itself lives in. Every typed line below
# embeds this one grammar, so a shorthand and its qualified spelling always
# parse to the same reference.
QUALIFIED_REFERENCE = (
    rf"(?:(?P<repository>{protocol.REPOSITORY_PATTERN.pattern}))?#(?P<number>[1-9]\d*)"
)
# GitHub links a keyword to a reference only on one line, separated by
# horizontal space and ending at the reference: `Closes#7`, a keyword whose
# reference sits on the next line, and `#7suffix` all leave the issue open, so
# reading them as a closure would report a landing GitHub never performs.
KEYWORD_SEPARATOR = r"[ \t]*:?[ \t]+"
REFERENCE_BOUNDARY = r"(?![A-Za-z0-9_])"
# The keywords GitHub itself closes an issue on when a pull request merges.
# Nothing else retires an item, so this is what a landing's typed closing
# reference is checked against.
CLOSING_KEYWORDS = r"close(?:s|d)?|fix(?:es|ed)?|resolve(?:s|d)?"
CLOSING_REFERENCE_PATTERN = re.compile(
    rf"(?im)\b(?:{CLOSING_KEYWORDS}){KEYWORD_SEPARATOR}"
    rf"{QUALIFIED_REFERENCE}{REFERENCE_BOUNDARY}",
    re.ASCII,
)
# The board's stage heuristic also believes a pull request that says it landed
# or implemented an issue. GitHub closes on neither word, so this wider set
# answers "which issue did this pull request work on", never "which issue does
# it retire".
LANDING_CLAIM_PATTERN = re.compile(
    rf"(?im)\b(?:{CLOSING_KEYWORDS}|land(?:s|ed)?|implement(?:s|ed)?)"
    rf"{KEYWORD_SEPARATOR}{QUALIFIED_REFERENCE}{REFERENCE_BOUNDARY}",
    re.ASCII,
)
WORK_ITEM_KIND = "work-item"
CLASSIFICATION_LINE_PATTERN = re.compile(r"(?im)^(?P<kind>Work-Item|No-Item):(?P<value>[^\r\n]*)$")
WORK_ITEM_VALUE_PATTERN = re.compile(QUALIFIED_REFERENCE, re.ASCII)
RECOVERY_STEP = "close or re-project"
# A slice's pull request must never close its still-open epic — that would
# retire the epic before its remaining slices exist. This repository's
# established substitute is a whole line opening with one of these markers
# (observed verbatim in atelier-2 PRs #848 "Part of #79.", #960 "Refs #956
# and #80", #965/#967 "Refs #<n> ..."). Anchoring to the start of the line
# is what keeps a casual mid-paragraph mention — "as noted in #79's plan" —
# from ever counting; only a dedicated reference line does. This is still a
# syntactic marker, not a validated relation: GitHub has no structured field
# for a non-closing PR-to-issue link, and this repository's own children use
# it inconsistently (see `_touched_without_closing`'s docstring for the
# named residual and the corroboration this module still requires).
TOUCHES_WITHOUT_CLOSING_LINE_PATTERN = re.compile(
    r"(?im)^(?:Refs?|References?|Part of|Teil von)\b[:\s].*$"
)
CLAIM_OLD_AFTER = timedelta(hours=1)
# `cut`'s fresh child, in the one grammar the tool reads: every projection
# key present and empty, so `parse_body` reads it as `VALID` but
# `contract_complete=False` -- invisible to `next`, refused by `claim` --
# until the head fills it in. Empty strings, not omitted keys, which the
# block schema would refuse. No `source_slice` -- title, sub-issue relation,
# and GitHub history own provenance instead.
BLOCK_CHILD_SKELETON = '```agent-claim\nversion = 1\nnow = ""\nnext = ""\ndone_when = ""\n```\n'
# The three slice-title forms seen in atelier-2 (`#79`): a parenthetical
# after the real title (`(#962 Scheibe 4)`, `(#962 slice 4)`) or a leading
# German phrase (`Scheibe 4 von #962`).
_SLICE_TITLE_PARENTHETICAL_PATTERN = re.compile(
    r"\(#(?P<parent>[1-9]\d*)[ \t]+(?:Scheibe|slice)[ \t]+(?P<slice>[1-9]\d*)\)",
    re.IGNORECASE | re.ASCII,
)
_SLICE_TITLE_VON_PATTERN = re.compile(
    r"Scheibe[ \t]+(?P<slice>[1-9]\d*)[ \t]+von[ \t]+#(?P<parent>[1-9]\d*)",
    re.IGNORECASE | re.ASCII,
)
# The one fenced-block info string a repository pinned to `body_contract =
# "block"` (issue #150) reads as its typed work-item body -- any other
# fence's info string is ordinary documentation.
AGENT_CLAIM_FENCE_INFO = "agent-claim"
BLOCK_TOP_LEVEL_KEYS = frozenset(
    {"version", "now", "next", "done_when", "frozen_until", "expectation", "slice"}
)
BLOCK_VERSION = 1
BLOCK_EXPECTATION_DEFAULTS = frozenset({"yes", "no", "later"})
BLOCK_EXPECTATION_RULINGS = frozenset({"yes", "no"})


class ItemKind(StrEnum):
    """An item's kind, read from the forge's native issue type -- the one
    owner for "is this a container" (decision record 0001 ruling D3, #112)."""

    TASK = "task"
    BUG = "bug"
    FEATURE = "feature"
    CONTAINER = "container"


@dataclass(frozen=True)
class Issue:
    number: int
    title: str
    labels: tuple[str, ...]
    body: str
    created_at: str
    updated_at: str
    kind: ItemKind | None = None
    children_closed: int | None = None
    children_total: int | None = None
    blocked_by_count: int = 0


class BlockerState(StrEnum):
    """The state of one `blocked_by` dependency GitHub returns for an item."""

    OPEN = "open"
    CLOSED = "closed"


class ChildState(StrEnum):
    """The two states a sub-issue can be in.

    Its own enum, not the dependency relation's `BlockerState`: the two are
    separate port operations with separate response shapes, and the adapter
    fails loud on any state string it does not recognize -- which is exactly
    what the parent's open-children reading has always required, since an
    unrecognized state must never make a parent look childless.
    """

    OPEN = "open"
    CLOSED = "closed"


@dataclass(frozen=True)
class ChildItem:
    """One sub-issue, as the port returns it and as the board shows it.

    `blocked_by` is empty at the port boundary -- the adapter cannot know it
    -- and `build_board` fills it for open children from the dependencies it
    already read for the board, with no extra request.
    """

    number: int
    state: ChildState
    blocked_by: tuple[IssueReference, ...] = ()


@dataclass(frozen=True)
class ContainerProgress:
    closed: int
    total: int
    open_children: tuple[ChildItem, ...]


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    body: str
    head_ref_name: str
    merged_at: str | None = None


@dataclass(frozen=True)
class IssueReference:
    """One issue, always qualified: a same-repository `#n` is resolved at parse time."""

    repository: str
    number: int

    def __str__(self) -> str:
        return f"{self.repository}#{self.number}"


@dataclass(frozen=True)
class IssueDependency:
    """One `blocked_by` relation GitHub itself records for an issue (#150) --
    same- or foreign-repository, open or closed, issue or pull request. The
    board reads these instead of a `Blocked by:` body section once a
    repository is pinned to `body_contract = "block"`."""

    reference: IssueReference
    state: BlockerState
    is_pull_request: bool
    closed_at: datetime | None = None


def open_blocker_label(reference: IssueReference, repository: str) -> str:
    """How one entry of `BoardItem.open_blockers` (or `ChildItem.blocked_by`)
    is named against the board's own `repository`: a same-repository
    blocker is its bare local number, `#n`; a foreign one is the qualified
    `owner/repo#n` (`IssueReference.__str__`)."""
    return f"#{reference.number}" if reference.repository == repository else str(reference)


def _blocker_sort_key(reference: IssueReference, repository: str) -> tuple[int, str, int]:
    """Local references first, ascending by number; foreign references
    after them, ascending by `(repository, number)` (#150 §6) -- read
    directly off the typed reference, never re-parsed from a label."""
    return (0 if reference.repository == repository else 1, reference.repository, reference.number)


@dataclass(frozen=True)
class ParentIssue:
    """The issue GitHub records as an item's parent through its sub-issue relation."""

    reference: IssueReference
    body: str
    kind: ItemKind | None = None


class NoItemKind(StrEnum):
    DOCS = "docs"
    FIX = "fix"


@dataclass(frozen=True)
class WorkItemClassification:
    item: IssueReference

    def __str__(self) -> str:
        return f"Work-Item: {self.item}"


@dataclass(frozen=True)
class NoItemClassification:
    kind: NoItemKind

    def __str__(self) -> str:
        return f"No-Item: {self.kind.value}"


Classification = WorkItemClassification | NoItemClassification


@dataclass(frozen=True)
class ClassificationDefect:
    """Why a pull request's classification is not one this repository accepts."""

    message: str


@dataclass(frozen=True)
class SliceRow:
    """One `[[slice]]` entry of a body's `agent-claim` block: a slice its
    container still has to dispatch. `index` is exactly what `cut --row N`
    names it by, `title` exactly what `cut --title` must match."""

    index: int
    title: str


@dataclass(frozen=True)
class BoardConfig:
    priority_labels: tuple[str, ...] = DEFAULT_PRIORITY_LABELS
    idea_label: str | None = None
    # The remote `refs/aco/state` lives on (issue #176, §1); every store and
    # import refusal is phrased in its terms. The store's own default is
    # already "origin" (`store.DEFAULT_CANONICAL_REMOTE`) -- this is the one
    # place a repository overrides it.
    canonical_remote: str = "origin"


# The body pin (issue #150) is still a key this file defines, but no longer
# a setting: the typed `agent-claim` block is the one grammar, so `"block"`
# is its only legal value and there is nothing left for `BoardConfig` to
# carry (issue #204). It must stay *known* all the same -- five repositories
# pin it, and `_refuse_unknown_config_keys` lies on the path of every store
# command, so forgetting it here would refuse `claim` and `release` there.
BODY_CONTRACT_KEY = "body_contract"
BODY_CONTRACT_BLOCK = "block"
BODY_CONTRACT_PROSE = "prose"
# Every key `.agent-claim/board.toml` defines; anything else is a typo, and
# `_refuse_unknown_config_keys` names it rather than reading past it.
CONFIG_KEYS = frozenset({setting.name for setting in fields(BoardConfig)}) | {BODY_CONTRACT_KEY}


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


def _contract_fields(contract: Contract) -> tuple[tuple[str, str | None], ...]:
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


class Stage(StrEnum):
    TEXT_ONLY = "text-only"
    CODE_LANDED = "code-landed"
    IN_FLIGHT = "in-flight"


class ExpectationState(StrEnum):
    NONE = "-"
    PROPOSED = "proposed"
    RULED = "ruled"


@dataclass(frozen=True)
class ExpectationProgress:
    open: int
    total: int


@dataclass(frozen=True)
class BoardItem:
    number: int
    title: str
    labels: tuple[str, ...]
    kind: ItemKind | None
    priority_category: int
    priority_bucket: str
    priority_order: int
    container: ContainerProgress | None
    container_parent: int | None
    contract: Contract
    next_step: str | None
    contract_complete: bool
    projectionless_idea: bool
    expectation_state: ExpectationState
    expectation_progress: ExpectationProgress
    ruling_landings: int | None
    ruling_old: bool | None
    frozen_trigger: str | None
    open_blockers: tuple[IssueReference, ...]
    freed_on: datetime | None
    freed_days: int | None
    stage: Stage
    age_days: int
    idle_days: int
    active_claim: str | None
    claim_age: str | None
    claim_old: bool
    unblocks_count: int
    score: int
    actionable: bool
    actionable_reason: str | None
    read_state: BodyReadState


@dataclass(frozen=True)
class Board:
    """`items`, and therefore `ready_now`, are ordered by `board_rank`: critical
    (a configured critical label or a Bug), then blocker, then a container's
    completing last child, then the remaining labels and unlabelled --
    tie-broken by score, critical label index, container, and number.

    `ready_now`, `stale`, and `recovery` are filters over `items`; filtering
    never reorders, so `ready_now[0]` is always `items`' first actionable row
    — the same row a human reading `board` sees first. `next` relies on this.

    `recovery` holds the items a merged pull request declared as its work
    item while they stayed open: the landing happened, the bookkeeping did
    not.
    """

    items: tuple[BoardItem, ...]
    ready_now: tuple[BoardItem, ...]
    stale: tuple[BoardItem, ...]
    recovery: tuple[BoardItem, ...]
    uncut: tuple[UncutSlices, ...]
    repository: str
    requests: int


def _validated_priority_labels(raw: dict[str, object]) -> tuple[str, ...]:
    labels = raw.get("priority_labels")
    if labels is None:
        return DEFAULT_PRIORITY_LABELS
    if (
        not isinstance(labels, list)
        or not labels
        or not all(isinstance(label, str) and label.strip() == label and label for label in labels)
        or len(set(labels)) != len(labels)
    ):
        raise protocol.ClaimError(
            "board configuration priority_labels must be a non-empty list of unique labels"
        )
    return tuple(labels)


def _validated_idea_label(raw: dict[str, object]) -> str | None:
    idea_label = raw.get("idea_label")
    if idea_label is not None and (
        not isinstance(idea_label, str) or idea_label.strip() != idea_label or not idea_label
    ):
        raise protocol.ClaimError("board configuration idea_label must be a non-empty label")
    return idea_label


def _refuse_unpinned_body_contract(raw: dict[str, object], path: Path) -> None:
    """Refuse any body pin but the one grammar this tool reads (issue #204).

    An absent key means the block, so an unpinned repository needs no edit.
    `"prose"` is refused by name rather than as an unknown value: a
    repository still carrying it is not making a typo, it is asking for a
    reader that no longer exists.
    """
    pinned = raw.get(BODY_CONTRACT_KEY)
    if pinned is None or pinned == BODY_CONTRACT_BLOCK:
        return
    if pinned == BODY_CONTRACT_PROSE:
        raise protocol.ClaimError(
            f"board configuration {path} pins {BODY_CONTRACT_KEY} "
            f"{BODY_CONTRACT_PROSE!r}: prose bodies are no longer supported"
        )
    raise protocol.ClaimError(
        f"board configuration {path} {BODY_CONTRACT_KEY} must be {BODY_CONTRACT_BLOCK!r}"
    )


def _validated_canonical_remote(raw: dict[str, object], path: Path) -> str:
    canonical_remote_raw = raw.get("canonical_remote")
    if canonical_remote_raw is None:
        return "origin"
    if (
        isinstance(canonical_remote_raw, str)
        and canonical_remote_raw.strip() == canonical_remote_raw
        and canonical_remote_raw
    ):
        return canonical_remote_raw
    raise protocol.ClaimError(
        f"board configuration {path} canonical_remote must be a non-empty remote name"
    )


def _refuse_unknown_config_keys(raw: dict[str, object], path: Path) -> None:
    """Name a key this file does not define, the way the block parser names
    an unknown top-level key.

    Silence here is expensive: `priorty_labels = [...]` would leave the
    board ordered by the defaults, with nothing in any output saying the
    repository's own ladder was never read.
    """
    unknown = sorted(set(raw) - CONFIG_KEYS)
    if unknown:
        named = ", ".join(unknown)
        raise protocol.ClaimError(f"board configuration {path} has unknown top-level key {named}")


def load_config(path: Path = CONFIG_PATH) -> BoardConfig:
    if not path.exists():
        return BoardConfig()
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise protocol.ClaimError(f"cannot read board configuration {path}: {error}") from error
    _refuse_unknown_config_keys(raw, path)
    _refuse_unpinned_body_contract(raw, path)
    return BoardConfig(
        priority_labels=_validated_priority_labels(raw),
        idea_label=_validated_idea_label(raw),
        canonical_remote=_validated_canonical_remote(raw, path),
    )


def _opening_fence_delimiter(line: str) -> tuple[str, int] | None:
    match = FENCE_OPENING_PATTERN.match(line)
    if match is None:
        return None
    run = match.group("run")
    return run[0], len(run)


def _closing_fence_delimiter(line: str) -> tuple[str, int] | None:
    match = FENCE_CLOSING_PATTERN.match(line)
    if match is None:
        return None
    run = match.group("run")
    return run[0], len(run)


def _live_text(body: str) -> str:
    """The body's non-fenced lines, joined back in order — what GitHub
    renders as running text, and the only text this module reads a marker
    (a classification line, a `Refs #n` trailer) out of.

    Walks the body once carrying CommonMark fence state: a line opens a fence
    (an info string after the run is allowed, e.g. ` ```python `), and only a
    later line with the *same* fence character, a run at least as long, and
    nothing but trailing whitespace after the run closes it again — a line
    like ` ```python ` never closes a fence, even one opened with backticks,
    because CommonMark forbids an info string on a closing delimiter; it is
    read as fence content instead. An opened fence that never closes runs to
    the end of the document, exactly as GitHub renders it — so an operator
    who left a fence unclosed, or wrote an info string on what they meant as
    a close, sees the same code block the tool does; there is no invisible
    divergence. `#72`'s own body fences its example this way, and it must
    never itself read as live.

    Not modeled: a 4-space-indented code block (CommonMark's other fencing
    form). A marker written there is read as live — visible on `board`/`next`
    and correctable by fencing it properly, never a silent divergence.
    """
    lines: list[str] = []
    fence_char: str | None = None
    fence_length = 0
    for line in body.splitlines():
        if fence_char is None:
            opening = _opening_fence_delimiter(line)
            if opening is not None:
                fence_char, fence_length = opening
                continue
            lines.append(line)
            continue
        closing = _closing_fence_delimiter(line)
        if closing is not None and closing[0] == fence_char and closing[1] >= fence_length:
            fence_char, fence_length = None, 0
        # Still inside the fence (or just closed it): never scanned for a marker.
    return "\n".join(lines)


class BodyReadState(StrEnum):
    """How `parse_body` read one issue's body.

    `LEGACY` means no recognized `agent-claim` block was found at all -- not
    that some other grammar was recognized instead; there is no other
    grammar. `MALFORMED` means the block was found and its schema refused
    it.
    """

    VALID = "valid"
    LEGACY = "legacy"
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
    slices: tuple[SliceRow, ...]
    read_state: BodyReadState


def _line_ending(raw_line: str) -> str:
    if raw_line.endswith("\r\n"):
        return "\r\n"
    if raw_line.endswith("\n"):
        return "\n"
    return ""


def _line_without_ending(raw_line: str) -> str:
    ending = _line_ending(raw_line)
    return raw_line[: len(raw_line) - len(ending)] if ending else raw_line


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
            defects.append(ContractDefect(f"{prefix}.ruling", f"{prefix}.ruling must be yes or no"))
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
    unknown = sorted(set(entry) - {"text", "default", "ruling", "ruled_on"})
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
    unknown = sorted(set(entry) - {"index", "title"})
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


def _block_schema_defects(data: dict[str, object]) -> tuple[ContractDefect, ...]:
    defects: list[ContractDefect] = []
    version_defect = _block_version_defect(data)
    if version_defect is not None:
        defects.append(version_defect)
    defects.extend(_block_projection_defects(data))
    defects.extend(_block_frozen_until_defects(data))
    expectations, expectation_defect = _block_array_or_defect(data, "expectation")
    defects.append(expectation_defect) if expectation_defect else defects.extend(
        _block_expectation_defects(expectations)
    )
    slices, slice_defect = _block_array_or_defect(data, "slice")
    defects.append(slice_defect) if slice_defect else defects.extend(_block_slice_defects(slices))
    unknown = sorted(set(data) - BLOCK_TOP_LEVEL_KEYS)
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
        slices=(),
        read_state=BodyReadState.MALFORMED,
    )


_LEGACY_PARSED_BODY = ParsedBody(
    contract=Contract(None, None, None, ()),
    contract_complete=False,
    projectionless=False,
    expectation_state=ExpectationState.NONE,
    expectation_progress=ExpectationProgress(0, 0),
    ruling_date=None,
    frozen_trigger=None,
    slices=(),
    read_state=BodyReadState.LEGACY,
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
    here is exactly what is still uncut."""
    return tuple(
        SliceRow(cast(int, entry["index"]), cast(str, entry["title"]))
        for entry in _block_array(data, "slice")
        if isinstance(entry, dict)
        and isinstance(entry.get("index"), int)
        and isinstance(entry.get("title"), str)
    )


def _valid_block_parsed_body(data: dict[str, object]) -> ParsedBody:
    now, next_value, done_when = (
        cast(str, data["now"]).strip(),
        cast(str, data["next"]).strip(),
        cast(str, data["done_when"]).strip(),
    )
    expectations = _block_expectation_dicts(data)
    expectation_state = _block_expectation_state(expectations)
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
        slices=_block_slices(data),
        read_state=BodyReadState.VALID,
    )


def parse_body(body: str) -> ParsedBody:
    """The one read of a work-item body (issue #150, narrowed to one grammar
    by #204): the typed `agent-claim` block. Every consumer reads the
    returned `ParsedBody` instead of re-parsing the raw body. Human prose
    around the block is never parsed -- another repository may own its own
    section headings in the same body."""
    fences = _agent_claim_fence_matches(body)
    if not fences:
        return _LEGACY_PARSED_BODY
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
    defects = _block_schema_defects(data)
    if defects:
        return _malformed_parsed_body(defects)
    return _valid_block_parsed_body(data)


@dataclass(frozen=True)
class LocatedBlock:
    """A valid `agent-claim` block's decoded TOML, plus the byte-exact span
    of its interior -- between the fence lines, which stay byte-identical --
    and the newline convention new interior lines are rendered with (#150
    §4/§7). Callable only on a body `parse_body` already read as `VALID`;
    `cut` refuses a legacy or malformed target before ever calling this."""

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


_TOML_BASIC_STRING_ESCAPES = {
    "\\": "\\\\",
    '"': '\\"',
    "\b": "\\b",
    "\t": "\\t",
    "\n": "\\n",
    "\f": "\\f",
    "\r": "\\r",
}
# The shape a decoded homogeneous array of tables (`[[expectation]]`,
# `[[slice]]`) or an `asdict`'d list of dataclasses takes -- one alias so
# `cast` names a real type instead of repeating the string.
_JsonRows = list[dict[str, object]]


def _toml_string(value: object) -> str:
    """A TOML basic string for `value` -- the writer's one escaping path,
    matching what `tomllib.loads` (the reader) accepts back unchanged."""
    escaped = "".join(_TOML_BASIC_STRING_ESCAPES.get(char, char) for char in cast(str, value))
    return f'"{escaped}"'


def _render_frozen_until(data: Mapping[str, object]) -> list[str]:
    frozen_until = data.get("frozen_until")
    if not isinstance(frozen_until, dict):
        return []
    ruled_on = cast(date, frozen_until["ruled_on"])
    return [
        "",
        f"frozen_until = {{ trigger = {_toml_string(frozen_until['trigger'])}, "
        f"ruled_on = {ruled_on.isoformat()} }}",
    ]


def _render_expectations(data: Mapping[str, object]) -> list[str]:
    lines: list[str] = []
    for expectation in cast(_JsonRows, data.get("expectation", [])):
        lines.extend(("", "[[expectation]]", f"text = {_toml_string(expectation['text'])}"))
        if "default" in expectation:
            lines.append(f"default = {_toml_string(expectation['default'])}")
        else:
            ruled_on = cast(date, expectation["ruled_on"])
            lines.append(f"ruling = {_toml_string(expectation['ruling'])}")
            lines.append(f"ruled_on = {ruled_on.isoformat()}")
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
                f"title = {_toml_string(entry['title'])}",
            )
        )
    return lines


def render_block(data: Mapping[str, object], newline: str = "\n") -> str:
    """The canonical `agent-claim` block interior for `data` (#150 §4):
    schema key order, TOML-safe strings, unquoted dates, ending in
    `newline` so a following fence line starts clean. Production caller:
    block-mode `cut`; there is no standalone validator."""
    lines = [f"version = {data['version']}"]
    lines.extend(f"{key} = {_toml_string(data[key])}" for key in ("now", "next", "done_when"))
    lines.extend(_render_frozen_until(data))
    lines.extend(_render_expectations(data))
    lines.extend(_render_slices(data))
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


def missing_or_empty_sections(contract: Contract) -> tuple[str, ...]:
    """Every projection key a body-incomplete refusal names: an absent key
    and a fresh skeleton's empty string both count, even though both stay
    legitimate CONTRACT-column *presence* (`_contract_summary` is
    `None`-only for that column, matching #150 §5's rule that a block
    skeleton still shows `Now, Next, Done when`)."""
    return tuple(name for name, value in _contract_fields(contract) if not value)


@dataclass(frozen=True)
class UncutSlices:
    """One item's still-undispatched slices, as `board` reports them."""

    item: int
    rows: tuple[SliceRow, ...]


def _uncut_slices(issue_number: int, slices: tuple[SliceRow, ...]) -> UncutSlices | None:
    return UncutSlices(issue_number, slices) if slices else None


def _issue_reference(match: re.Match[str], repository: str) -> IssueReference:
    return IssueReference(match.group("repository") or repository, int(match.group("number")))


def _references_matching(
    pattern: re.Pattern[str], text: str, repository: str
) -> frozenset[IssueReference]:
    """Routed through `_live_text` for the same reason every other marker in
    this module is: a fenced example of the closing-keyword convention
    ("Fixes #64" inside a code block, say) must document the syntax without
    silently closing #64.
    """
    return frozenset(
        _issue_reference(match, repository) for match in pattern.finditer(_live_text(text))
    )


def closing_references(text: str, repository: str) -> frozenset[IssueReference]:
    """Every issue merging this text closes, by GitHub's own keywords."""
    return _references_matching(CLOSING_REFERENCE_PATTERN, text, repository)


def _single_classification_match(
    matches: tuple[re.Match[str], ...],
) -> re.Match[str] | ClassificationDefect:
    """The one classification line a body must carry, or why it doesn't have one."""
    if len(matches) == 0:
        return ClassificationDefect("carries no `Work-Item:` or `No-Item:` line")
    work_items = tuple(match for match in matches if match.group("kind").lower() == WORK_ITEM_KIND)
    if len(work_items) > 1:
        named = " and ".join(match.group("value").strip(" \t") for match in work_items[:2])
        return ClassificationDefect(f"names two work items, {named}; split it")
    if len(matches) > 1:
        return ClassificationDefect(
            f"carries {len(matches)} classification lines; exactly one is required"
        )
    return matches[0]


def _work_item_classification(value: str, repository: str) -> Classification | ClassificationDefect:
    reference = WORK_ITEM_VALUE_PATTERN.fullmatch(value)
    if reference is None:
        return ClassificationDefect(
            f"carries `Work-Item: {value}`; a work item reads OWNER/REPO#n or #n"
        )
    return WorkItemClassification(_issue_reference(reference, repository))


def _no_item_classification(value: str) -> Classification | ClassificationDefect:
    if value.lower() not in {kind.value for kind in NoItemKind}:
        return ClassificationDefect(
            f"carries `No-Item: {value}`; an issue-less pull request is docs or fix"
        )
    return NoItemClassification(NoItemKind(value.lower()))


def parse_pull_request_classification(
    body: str, repository: str
) -> Classification | ClassificationDefect:
    """The one `Work-Item:`/`No-Item:` line a pull request body must carry.

    A pull request either lands one work item and closes it, or declares
    itself issue-less documentation or a fix. Nothing else in a body names an
    item: a dispatched slice is its own item, and its pull request closes it.
    """
    matches = tuple(CLASSIFICATION_LINE_PATTERN.finditer(_live_text(body)))
    selected = _single_classification_match(matches)
    if isinstance(selected, ClassificationDefect):
        return selected
    value = selected.group("value").strip(" \t")
    if selected.group("kind").lower() == WORK_ITEM_KIND:
        return _work_item_classification(value, repository)
    return _no_item_classification(value)


def declared_work_items(pull_requests: tuple[PullRequest, ...], repository: str) -> frozenset[int]:
    """The issues of `repository` that these pull requests declare as their work item."""
    declared: set[int] = set()
    for pull_request in pull_requests:
        classification = parse_pull_request_classification(pull_request.body, repository)
        if (
            isinstance(classification, WorkItemClassification)
            and classification.item.repository == repository
        ):
            declared.add(classification.item.number)
    return frozenset(declared)


def slice_title_match(title: str) -> tuple[int, int] | None:
    """`(slice number, parent issue)` when `title` looks like a dispatched slice.

    Matches the three forms `#79` names: `(#<n> Scheibe <k>)`, `(#<n> slice
    <k>)`, and `Scheibe <k> von #<n>`. A title carrying none of them returns
    None — the heuristic simply has nothing to check.
    """
    match = _SLICE_TITLE_PARENTHETICAL_PATTERN.search(title) or _SLICE_TITLE_VON_PATTERN.search(
        title
    )
    if match is None:
        return None
    return int(match.group("slice")), int(match.group("parent"))


def landings_since(trunk_landings: tuple[datetime, ...], ruling: date) -> int:
    start = datetime(ruling.year, ruling.month, ruling.day, tzinfo=UTC) + timedelta(days=1)
    return sum(1 for moment in trunk_landings if moment >= start)


def _ruling_freshness_from(
    ruling_date: date | None, trunk_landings: tuple[datetime, ...]
) -> tuple[int | None, bool | None]:
    """`ruling_landings`/`ruling_old` from an already-resolved ruling date --
    the one place `_board_item` reads freshness, for either mode, since
    `ParsedBody.ruling_date` is already `None` except when `RULED` (#150)."""
    if ruling_date is None:
        return None, None
    count = landings_since(trunk_landings, ruling_date)
    return count, count >= RULING_OLD_AFTER_LANDINGS


def _references(text: str) -> frozenset[int]:
    return frozenset(int(number) for number in REFERENCE_PATTERN.findall(text))


def open_dependency_blockers(
    dependencies: tuple[IssueDependency, ...], repository: str
) -> tuple[IssueReference, ...]:
    """An item's `open_blockers` (#150 §6): every open `blocked_by`
    dependency GitHub records for it, same- or foreign-repository, issue or
    pull request alike."""
    references = (
        dependency.reference for dependency in dependencies if dependency.state is BlockerState.OPEN
    )
    return tuple(sorted(references, key=lambda reference: _blocker_sort_key(reference, repository)))


def _dependency_freed_on(
    dependencies: tuple[IssueDependency, ...], repository: str
) -> datetime | None:
    """An item's `freed_on` (#150 §6): only same-repository dependencies
    can free an item -- a foreign dependency, open or closed, is dropped
    here and never blocks freedom on its own repository being unreachable."""
    local = tuple(
        dependency for dependency in dependencies if dependency.reference.repository == repository
    )
    if not local or any(dependency.state is not BlockerState.CLOSED for dependency in local):
        return None
    return max(
        (dependency.closed_at for dependency in local if dependency.closed_at is not None),
        default=None,
    )


def _floored_claim_minutes(age: timedelta) -> int:
    return max(0, int(age.total_seconds())) // 60


def format_claim_age(age: timedelta) -> str:
    hours, minutes = divmod(_floored_claim_minutes(age), 60)
    return f"{hours}h {minutes}m"


def claim_is_old(age: timedelta) -> bool:
    return age > CLAIM_OLD_AFTER


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise protocol.ClaimError("GitHub returned a malformed board timestamp") from error
    if parsed.tzinfo is None:
        raise protocol.ClaimError("GitHub returned a malformed board timestamp")
    return parsed.astimezone(UTC)


def _single_concrete_next(value: str | None) -> bool:
    if value is None:
        return False
    lines = tuple(line.strip(" -\t") for line in value.splitlines() if line.strip())
    return len(lines) == 1 and lines[0].casefold() not in {"tbd", "todo", "unknown"}


# A container's `Next` line has its own small set of "nothing left"
# spellings -- German and English, ASCII only. `check`'s last-child rule and
# `next`'s cut_slice/close_container split both read a `Next` line the same
# way. `""` belongs to it because a fresh skeleton writes `next = ""` and
# that value stays the empty string (never mapped to `None`, so CONTRACT
# still shows the key, #150 §5).
_NO_FURTHER_WORK_VALUES = frozenset({"keiner", "keine", "nichts", "none", "-", ""})


def has_further_work(next_line: str | None) -> bool:
    """Whether a container's own `Next` line still names work to dispatch."""
    return next_line is not None and next_line.casefold() not in _NO_FURTHER_WORK_VALUES


def _claim_by_issue(
    claims: tuple[protocol.ScopedClaim, ...],
) -> dict[int, protocol.ScopedClaim]:
    return {
        claim.identity.issue: claim
        for claim in claims
        if isinstance(claim.identity, protocol.IssueIdentity)
    }


def _priority_index(labels: tuple[str, ...], config: BoardConfig) -> int | None:
    priorities = {label.casefold(): index for index, label in enumerate(config.priority_labels)}
    matches = (priorities[label.casefold()] for label in labels if label.casefold() in priorities)
    return min(matches, default=None)


def _has_label(labels: tuple[str, ...], label: str | None) -> bool:
    return label is not None and any(item.casefold() == label.casefold() for item in labels)


CRITICAL_CATEGORY = 0
BLOCKER_CATEGORY = 1
COMPLETION_CATEGORY = 2
FIRST_LABEL_CATEGORY = 3


@dataclass(frozen=True)
class PriorityRank:
    """Where one item sits in `board_rank`'s order: its category, the bucket
    name `render` shows, and its order -- the configured label index inside
    the critical category only, `0` everywhere else."""

    category: int
    bucket: str
    order: int


def _priority_bucket(
    labels: tuple[str, ...],
    config: BoardConfig,
    unblocks_count: int,
    *,
    kind: ItemKind | None,
    completes_container: bool,
) -> PriorityRank:
    """The one function that decides where an item sits.

    Ladder, with the defaults `("security","data","ci","product","ux","cleanup")`
    and `critical_span = 3`: the item's critical label or a Bug's native kind
    (category 0, score-competing among themselves); a blocker (1); a
    container's last open child once a sibling has closed (2, "completion" --
    never above a critical item or a real blocker); the item's own
    non-critical label (3+); unlabelled (last). A Bug carrying a non-critical
    label still ranks critical -- only a Bug carrying no label at all reaches
    this function's second branch.
    """
    index = _priority_index(labels, config)
    critical_span = min(3, len(config.priority_labels))
    if index is not None and index < critical_span:
        return PriorityRank(CRITICAL_CATEGORY, config.priority_labels[index], index)
    if kind is ItemKind.BUG:
        return PriorityRank(CRITICAL_CATEGORY, "bug", len(config.priority_labels))
    if unblocks_count:
        return PriorityRank(BLOCKER_CATEGORY, "blocker", 0)
    if completes_container:
        return PriorityRank(COMPLETION_CATEGORY, "last-child", 0)
    if index is not None:
        return PriorityRank(
            FIRST_LABEL_CATEGORY + index - critical_span, config.priority_labels[index], 0
        )
    return PriorityRank(
        FIRST_LABEL_CATEGORY + len(config.priority_labels) - critical_span, "unlabelled", 0
    )


def _associated_issues(pull_requests: tuple[PullRequest, ...], repository: str) -> frozenset[int]:
    """Issues of `repository` that these pull requests close or claim to land."""
    return frozenset(
        reference.number
        for pull_request in pull_requests
        for reference in _references_matching(
            LANDING_CLAIM_PATTERN, f"{pull_request.title}\n{pull_request.body}", repository
        )
        if reference.repository == repository
    )


def _touched_without_closing(pull_requests: tuple[PullRequest, ...]) -> frozenset[int]:
    """Issues a pull request advances without closing — an epic's slices, typically.

    The coordination contract requires a slice to become its own item at
    dispatch, so an epic's work lands through its children's pull requests,
    which deliberately avoid a closing keyword against the epic itself (see
    `TOUCHES_WITHOUT_CLOSING_LINE_PATTERN`). Without this, an epic that is
    cut correctly can never earn a landed or in-flight stage.

    Named residual: this is a syntactic marker, not a validated parent-child
    relation. `unblocks`/`open_blockers` (this module's one real relation)
    only connect two issues through a structured `Blocked by` field; GitHub
    exposes no equivalent structured field for a non-closing PR-to-issue
    link, and this repository's own children reference their epic through
    inconsistent free text (a title suffix, a "Nachbarn" list, a "Refs"/"Part
    of" line) — there is no honest typed relation here to check against. A
    foreign pull request that writes a dedicated, single "Refs #N" line for
    an unrelated reason still confers a stage; that risk is real and is not
    eliminated below, only narrowed. The one real narrowing available:
    every observed genuine slice-to-epic reference (#848, #960, #965) names
    its epic a second time elsewhere in the same pull request, in
    substantive prose — never only in the trailer line — so a marker with no
    corroborating mention elsewhere in the text is dropped. Fenced code
    blocks are never live text (`_live_text`), matching every other marker
    this module reads.
    """
    touched: set[int] = set()
    for pull_request in pull_requests:
        live = _live_text(f"{pull_request.title}\n{pull_request.body}")
        marked = frozenset(
            number
            for line in TOUCHES_WITHOUT_CLOSING_LINE_PATTERN.findall(live)
            for number in _references(line)
        )
        if not marked:
            continue
        corroborated = _references(TOUCHES_WITHOUT_CLOSING_LINE_PATTERN.sub("", live))
        touched |= marked & corroborated
    return frozenset(touched)


def board_rank(item: BoardItem) -> tuple[int, int, int, int, int]:
    """The one order `items`, `ready_now`, and every "is X ahead of Y" comparison share.

    `build_board` sorts by this key; any caller that needs to know whether
    one item outranks another — the out-of-order warning, for instance —
    reads this instead of re-deriving its own notion of "ahead", which is
    exactly how `board` and `next` fell out of agreement before.

    `priority_order` only reorders inside the critical category (§2): a Bug
    and a labelled critical item at equal score still resolve by label index
    there, byte-for-byte as before this category was widened. `container_parent`
    falls back to the item's own number, so outside the completion category
    every group has exactly one member and the tuple degenerates to today's
    number tie-break.
    """
    return (
        item.priority_category,
        -item.score,
        item.priority_order,
        item.container_parent if item.container_parent is not None else item.number,
        item.number,
    )


@dataclass(frozen=True)
class _BoardBuildContext:
    """Per-run board state that every issue's `BoardItem` is derived against."""

    contracts: dict[int, Contract]
    parsed_bodies: dict[int, ParsedBody]
    blockers: dict[int, tuple[IssueReference, ...]]
    freed_on: dict[int, datetime | None]
    unblocks: dict[int, int]
    claims_by_issue: dict[int, protocol.ScopedClaim]
    claim_ages: Mapping[str, datetime]
    in_flight_references: frozenset[int]
    landed_references: frozenset[int]
    open_branches: frozenset[str]
    trunk_landings: tuple[datetime, ...]
    container_progress: dict[int, ContainerProgress]
    child_container: dict[int, int]
    repository: str


def _board_stage(
    issue: Issue,
    claim: protocol.ScopedClaim | None,
    *,
    in_flight_references: frozenset[int],
    landed_references: frozenset[int],
    open_branches: frozenset[str],
) -> Stage:
    in_flight = issue.number in in_flight_references or (
        claim is not None and claim.branch in open_branches
    )
    if in_flight:
        return Stage.IN_FLIGHT
    if issue.number in landed_references:
        return Stage.CODE_LANDED
    return Stage.TEXT_ONLY


def _claim_projection(
    claim: protocol.ScopedClaim | None,
    claim_ages: Mapping[str, datetime],
    observed_at: datetime,
) -> tuple[str | None, str | None, bool]:
    """The (active_claim, claim_age, claim_old) trio a `BoardItem` shows for
    `claim`. `opened_at` is the caller's own git-history read (issue #176,
    §1) -- never derived here, so this stays a pure function of its inputs.
    """
    if claim is None:
        return None, None, False
    opened_at = claim_ages[claim.claim_id]
    age = observed_at.astimezone(UTC) - opened_at.astimezone(UTC)
    return f"{claim.agent} ({claim.role})", format_claim_age(age), claim_is_old(age)


def _board_score(stage: Stage, unblocks_count: int, single_next: bool) -> int:
    score = 20 * unblocks_count
    score += {Stage.IN_FLIGHT: 30, Stage.CODE_LANDED: 20, Stage.TEXT_ONLY: -20}[stage]
    score += 10 if single_next else 0
    return score


def _container_progress(
    issue: Issue,
    children: Mapping[int, tuple[ChildItem, ...]],
    blockers: dict[int, tuple[IssueReference, ...]],
) -> ContainerProgress | None:
    """`issue`'s own container progress, or `None` when it isn't a container
    the forge reports numbers for -- a container whose type support is
    absent (no `kind`, no counts) is treated as an ordinary item, never
    guessed at from a partial read.

    The summary (`children_closed`/`children_total`) and the open-children
    list come from two different reads (the issue page and `list_children`),
    so they can disagree -- a stale summary, a paginated list that lost a
    row. `closed == total` must mean no open child, and an open child must
    mean `closed < total`; any other combination is a malformed board this
    function never guesses through, since guessing would let `next` close a
    container that still has work or `board` hide one that doesn't.
    """
    if (
        issue.kind is not ItemKind.CONTAINER
        or issue.children_closed is None
        or issue.children_total is None
    ):
        return None
    open_children = tuple(
        replace(child, blocked_by=blockers.get(child.number, ()))
        for child in children.get(issue.number, ())
        if child.state is ChildState.OPEN
    )
    if bool(open_children) == (issue.children_closed == issue.children_total):
        raise protocol.ClaimError(f"GitHub returned a malformed board container #{issue.number}")
    return ContainerProgress(issue.children_closed, issue.children_total, open_children)


def _completes_container(
    issue_number: int,
    child_container: dict[int, int],
    container_progress: dict[int, ContainerProgress],
) -> bool:
    """Whether `issue_number` is the one open child left in its container,
    once at least one sibling has already closed (the completion boost)."""
    container_number = child_container.get(issue_number)
    if container_number is None:
        return False
    progress = container_progress[container_number]
    return progress.closed >= 1 and len(progress.open_children) == 1


def _board_item(
    issue: Issue, context: _BoardBuildContext, config: BoardConfig, observed_at: datetime
) -> BoardItem:
    contract = context.contracts[issue.number]
    parsed = context.parsed_bodies[issue.number]
    freed_at = context.freed_on[issue.number]
    ruling_landings, ruling_old = _ruling_freshness_from(parsed.ruling_date, context.trunk_landings)
    frozen = parsed.frozen_trigger
    claim = context.claims_by_issue.get(issue.number)
    stage = _board_stage(
        issue,
        claim,
        in_flight_references=context.in_flight_references,
        landed_references=context.landed_references,
        open_branches=context.open_branches,
    )
    single_next = _single_concrete_next(contract.next)
    projectionless_idea = parsed.projectionless and _has_label(issue.labels, config.idea_label)
    next_step = IDEA_REFINEMENT_STEP if projectionless_idea else contract.next
    unblocks_count = context.unblocks[issue.number]
    container_parent = context.child_container.get(issue.number)
    completes_container = _completes_container(
        issue.number, context.child_container, context.container_progress
    )
    rank = _priority_bucket(
        issue.labels,
        config,
        unblocks_count,
        kind=issue.kind,
        completes_container=completes_container,
    )
    active_claim, claim_age_text, claim_old = _claim_projection(
        claim, context.claim_ages, observed_at
    )
    open_blockers = context.blockers[issue.number]
    container_progress = context.container_progress.get(issue.number)
    actionable_reason = _actionable_reason(
        _ActionabilityFacts(
            kind=issue.kind,
            frozen_trigger=frozen,
            active_claim=active_claim,
            open_blockers=open_blockers,
            repository=context.repository,
            contract=contract,
            contract_complete=parsed.contract_complete,
            projectionless_idea=projectionless_idea,
            read_state=parsed.read_state,
            malformed_defect=(
                contract.defects[0] if parsed.read_state is BodyReadState.MALFORMED else None
            ),
        )
    )
    return BoardItem(
        number=issue.number,
        title=issue.title,
        labels=issue.labels,
        kind=issue.kind,
        priority_category=rank.category,
        priority_bucket=rank.bucket,
        priority_order=rank.order,
        container=container_progress,
        container_parent=container_parent,
        contract=contract,
        next_step=next_step,
        contract_complete=parsed.contract_complete,
        projectionless_idea=projectionless_idea,
        expectation_state=parsed.expectation_state,
        expectation_progress=parsed.expectation_progress,
        ruling_landings=ruling_landings,
        ruling_old=ruling_old,
        frozen_trigger=frozen,
        open_blockers=open_blockers,
        freed_on=freed_at,
        freed_days=(None if freed_at is None else max(0, (observed_at - freed_at).days)),
        stage=stage,
        age_days=max(0, (observed_at - _timestamp(issue.created_at)).days),
        idle_days=max(0, (observed_at - _timestamp(issue.updated_at)).days),
        active_claim=active_claim,
        claim_age=claim_age_text,
        claim_old=claim_old,
        unblocks_count=unblocks_count,
        score=_board_score(stage, unblocks_count, single_next),
        actionable=actionable_reason is None,
        actionable_reason=actionable_reason,
        read_state=parsed.read_state,
    )


@dataclass(frozen=True)
class BoardBuildInputs:
    issues: tuple[Issue, ...]
    open_pull_requests: tuple[PullRequest, ...]
    recent_merged_pull_requests: tuple[PullRequest, ...]
    claims: tuple[protocol.ScopedClaim, ...]
    config: BoardConfig
    repository: str
    now: datetime | None = None
    trunk_landings: tuple[datetime, ...] = ()
    children: Mapping[int, tuple[ChildItem, ...]] = field(default_factory=dict)
    dependencies: Mapping[int, tuple[IssueDependency, ...]] = field(default_factory=dict)
    requests: int = 0
    # Each live claim's age (issue #176, §1): a committer date the caller
    # already read from the store's git history, since board.py's own build
    # stays pure and never reaches for git itself. Keyed by claim_id.
    claim_ages: Mapping[str, datetime] = field(default_factory=dict)


def build_board(inputs: BoardBuildInputs) -> Board:
    issues = inputs.issues
    open_pull_requests = inputs.open_pull_requests
    recent_merged_pull_requests = inputs.recent_merged_pull_requests
    config = inputs.config
    repository = inputs.repository
    observed_at = (inputs.now or datetime.now(UTC)).astimezone(UTC)
    parsed_bodies = {issue.number: parse_body(issue.body) for issue in issues}
    contracts = {number: parsed.contract for number, parsed in parsed_bodies.items()}
    blockers: dict[int, tuple[IssueReference, ...]] = {
        issue.number: open_dependency_blockers(
            inputs.dependencies.get(issue.number, ()), repository
        )
        for issue in issues
    }
    unblocks = {
        issue.number: sum(
            IssueReference(repository, issue.number) in other_blockers
            for other_blockers in blockers.values()
        )
        for issue in issues
    }
    container_progress = {
        issue.number: progress
        for issue in issues
        if (progress := _container_progress(issue, inputs.children, blockers)) is not None
    }
    child_container = {
        child.number: container_number
        for container_number, progress in container_progress.items()
        for child in progress.open_children
    }
    context = _BoardBuildContext(
        contracts=contracts,
        parsed_bodies=parsed_bodies,
        blockers=blockers,
        freed_on={
            issue.number: _dependency_freed_on(
                inputs.dependencies.get(issue.number, ()), repository
            )
            for issue in issues
        },
        unblocks=unblocks,
        claims_by_issue=_claim_by_issue(inputs.claims),
        claim_ages=inputs.claim_ages,
        in_flight_references=_associated_issues(open_pull_requests, repository)
        | _touched_without_closing(open_pull_requests),
        landed_references=_associated_issues(recent_merged_pull_requests, repository)
        | _touched_without_closing(recent_merged_pull_requests),
        open_branches=frozenset(pr.head_ref_name for pr in open_pull_requests),
        trunk_landings=inputs.trunk_landings,
        container_progress=container_progress,
        child_container=child_container,
        repository=repository,
    )
    landed_work_items = declared_work_items(recent_merged_pull_requests, repository)
    ordered = tuple(
        sorted(
            (_board_item(issue, context, config, observed_at) for issue in issues),
            key=board_rank,
        )
    )
    per_issue_slices = (
        _uncut_slices(issue.number, parsed_bodies[issue.number].slices) for issue in issues
    )
    uncut = tuple(
        sorted(
            (finding for finding in per_issue_slices if finding is not None),
            key=lambda finding: finding.item,
        )
    )
    return Board(
        items=ordered,
        ready_now=tuple(item for item in ordered if item.actionable),
        stale=tuple(
            item
            for item in ordered
            if item.idle_days > STALE_IDLE_DAYS and item.stage is Stage.TEXT_ONLY
        ),
        recovery=tuple(item for item in ordered if item.number in landed_work_items),
        uncut=uncut,
        repository=repository,
        requests=inputs.requests,
    )


def highest_scored_actionable(board: Board) -> BoardItem | None:
    """The one item `next` recommends — always `board`'s own top row.

    `ready_now` is a filtered view of `items`, which `build_board` orders by
    `board_rank`; filtering preserves that order, so its first element is
    `board`'s own top-ranked actionable row. Two commands over one board must
    not disagree, so this reads that order instead of maximizing score on its
    own — an unlabelled item with a higher score must never outrank a human's
    priority label.
    """
    return next(iter(board.ready_now), None)


@dataclass(frozen=True)
class WorkItemAction:
    """Claim `item` -- today's `next` target, unchanged."""

    item: BoardItem


@dataclass(frozen=True)
class CutSliceAction:
    """`container` has no open child and still names work: `next_step` is
    the container's own words (its `Next` line, or the first uncut slice
    title when it has none) for the human-readable action line; `cut_title`
    is the exact string `cut` itself accepts for the printed `cut` command --
    the first uncut `[[slice]]` row's title when one exists (that is the
    entry `cut` without `--row` links), else `next_step`. The two agree only
    when no uncut row exists."""

    container: BoardItem
    container_progress: ContainerProgress
    next_step: str
    cut_title: str


@dataclass(frozen=True)
class CloseContainerAction:
    """`container` has no open child and no further `Next` work: close it."""

    container: BoardItem
    container_progress: ContainerProgress


NextAction = WorkItemAction | CutSliceAction | CloseContainerAction


def next_action(board: Board) -> NextAction | None:
    """The one action `next` recommends: the board's own top qualifying row.

    Walks `items` in `board_rank` order -- the same order `board` shows --
    and returns the first row that is either an actionable non-container
    (`WorkItemAction`; a container is never actionable, so this branch never
    fires for one) or a container with no open child (`CutSliceAction` when
    its own `Next` line still names work or its block still carries an
    undispatched `[[slice]]`, else `CloseContainerAction`). `_container_progress`
    already fails loud on a container whose summary disagrees with its
    open-children list, so "no open child" here reliably means every
    created child has closed. Every other row -- blocked, claimed,
    incomplete, or a container still holding an open child -- is skipped,
    never blocking a lower-ranked qualifying row.

    Whichever branch fires, the printed command never carries `--row` (#151):
    `cut` without `--row` accepts every container `next` names here, linking
    its first undispatched slice when one exists and otherwise creating an
    untied child.

    A `LEGACY` or `MALFORMED` container (#150) is skipped here exactly like
    one still holding an open child: its own finding already surfaces
    through `actionable_reason`/`SKIPPED`, and proposing to cut or close a
    body that could not be read would act on a guess this module never
    makes.
    """
    uncut_by_container = {finding.item: finding for finding in board.uncut}
    for item in board.items:
        if item.actionable:
            return WorkItemAction(item)
        container = item.container
        if item.kind is not ItemKind.CONTAINER or container is None or container.open_children:
            continue
        action = _container_next_action(item, container, uncut_by_container)
        if action is not None:
            return action
    return None


def _container_next_action(
    item: BoardItem, container: ContainerProgress, uncut_by_container: dict[int, UncutSlices]
) -> NextAction | None:
    """The action a childless container qualifies for, or `None` to skip it:
    a non-`VALID` body names its own finding elsewhere and is never guessed
    through."""
    if item.read_state is not BodyReadState.VALID:
        return None
    next_line = item.contract.next
    uncut = uncut_by_container.get(item.number)
    if uncut is not None:
        cut_title = uncut.rows[0].title
        next_step = (
            next_line if next_line is not None and has_further_work(next_line) else cut_title
        )
        return CutSliceAction(item, container, next_step, cut_title)
    if next_line is not None and has_further_work(next_line):
        return CutSliceAction(item, container, next_line, next_line)
    return CloseContainerAction(item, container)


def _project_blocker_references(entry: dict[str, object], key: str, repository: str) -> None:
    """Rewrite `entry[key]` (a list of `asdict`'d `IssueReference`s) into the
    pre-#150 local-int list plus a sibling `foreign_blockers` key (A2) -- the
    one projector `board_json` uses for both `BoardItem.open_blockers` and
    each open child's `ChildItem.blocked_by`, never a mixed `int | str` list
    and never re-parsed from a label."""
    references = cast(_JsonRows, entry.pop(key))
    entry[key] = [
        reference["number"] for reference in references if reference["repository"] == repository
    ]
    entry["foreign_blockers"] = [
        f"{reference['repository']}#{reference['number']}"
        for reference in references
        if reference["repository"] != repository
    ]


def board_json(board: Board) -> str:
    payload = asdict(board)
    repository = payload.pop("repository")
    for group in ("items", "ready_now", "stale", "recovery"):
        for item in payload[group]:
            freed_on = item["freed_on"]
            item["freed_on"] = (
                None if freed_on is None else freed_on.astimezone(UTC).date().isoformat()
            )
            item.pop("read_state")
            _project_blocker_references(item, "open_blockers", repository)
            container = item["container"]
            if container is not None:
                for child in container["open_children"]:
                    _project_blocker_references(child, "blocked_by", repository)
    return json.dumps(payload, default=lambda value: value.value)


def _kind_cell(item: BoardItem) -> str:
    if item.kind is None:
        return "-"
    if item.container is not None:
        return f"{item.kind.value} {item.container.closed}/{item.container.total}"
    return item.kind.value


def render(board: Board) -> str:
    rows = [
        (
            "SCORE",
            "ISSUE",
            "KIND",
            "PRIORITY",
            "STAGE",
            "CONTRACT",
            "EXPECT",
            "NEXT",
            "AGE",
            "IDLE",
            "FREED",
            "CLAIM",
            "ACTIONABLE",
            "BLOCKERS",
            "UNBLOCKS",
            "TITLE",
        ),
        *(
            (
                str(item.score),
                f"#{item.number}",
                _kind_cell(item),
                item.priority_bucket,
                item.stage.value,
                _contract_summary(item.contract),
                _expectation_cell(item),
                _brief(item.next_step),
                str(item.age_days),
                str(item.idle_days),
                _freed_cell(item),
                _claim_cell(item),
                "yes" if item.actionable else f"no: {item.actionable_reason}",
                ",".join(
                    open_blocker_label(reference, board.repository)
                    for reference in item.open_blockers
                )
                or "-",
                str(item.unblocks_count),
                item.title,
            )
            for item in board.items
        ),
    ]
    widths = tuple(max(len(row[index]) for row in rows) for index in range(len(rows[0])))
    table = "\n".join(
        "  ".join(value.ljust(widths[index]) for index, value in enumerate(row)).rstrip()
        for row in rows
    )
    ready = ", ".join(f"#{item.number}" for item in board.ready_now) or "none"
    stale = ", ".join(f"#{item.number}" for item in board.stale) or "none"
    recovery = ", ".join(f"#{item.number}" for item in board.recovery) or "none"
    containers = "\n".join(_container_lines(board)) or "none"
    uncut = "\n".join(_uncut_line(finding) for finding in board.uncut) or "none"
    return (
        f"{table}\n\nREADY NOW\n{ready}\n\nSTALE\n{stale}\n\nRECOVERY ({RECOVERY_STEP})\n{recovery}"
        f"\n\nCONTAINERS\n{containers}\n\nUNCUT\n{uncut}\n\nrequests: {board.requests}"
    )


def _open_child_cell(child: ChildItem, repository: str) -> str:
    if not child.blocked_by:
        return f"#{child.number}"
    blockers = ", ".join(
        open_blocker_label(reference, repository) for reference in child.blocked_by
    )
    return f"#{child.number} (blocked by {blockers})"


def _container_line(number: int, container: ContainerProgress, repository: str) -> str:
    open_children = ", ".join(
        _open_child_cell(child, repository) for child in container.open_children
    )
    return f"#{number} {container.closed}/{container.total} closed; open: {open_children or 'none'}"


def _container_lines(board: Board) -> list[str]:
    return [
        _container_line(item.number, item.container, board.repository)
        for item in board.items
        if item.container is not None
    ]


def _uncut_line(finding: UncutSlices) -> str:
    indices = ", ".join(str(row.index) for row in finding.rows)
    return f"#{finding.item}: rows {indices} uncut"


def _contract_summary(contract: Contract) -> str:
    present = (name for name, value in _contract_fields(contract) if value is not None)
    return ", ".join(present) or "-"


@dataclass(frozen=True)
class _ActionabilityFacts:
    """Everything `_actionable_reason` decides on -- one owner for why an
    item cannot be claimed right now, bundled so the container rule sits
    beside every other reason instead of a special case at each call site."""

    kind: ItemKind | None
    frozen_trigger: str | None
    active_claim: str | None
    open_blockers: tuple[IssueReference, ...]
    repository: str
    contract: Contract
    contract_complete: bool
    projectionless_idea: bool
    read_state: BodyReadState = BodyReadState.VALID
    malformed_defect: ContractDefect | None = None


def _read_state_actionable_reason(facts: _ActionabilityFacts) -> str | None:
    """The one refusal a legacy or malformed body gets, ahead of every other
    reason -- including the container rule, so a container whose body
    itself cannot be read is never offered as "claim a child" (#150 §5)."""
    if facts.read_state is BodyReadState.LEGACY:
        return "body legacy"
    if facts.read_state is BodyReadState.MALFORMED and facts.malformed_defect is not None:
        return body_defect_text(facts.malformed_defect)
    return None


def _claim_or_completeness_reason(facts: _ActionabilityFacts) -> str | None:
    if facts.frozen_trigger is not None:
        return f"frozen: {facts.frozen_trigger}"
    if facts.active_claim is not None:
        return "claimed"
    if facts.open_blockers:
        return "blocked by " + ", ".join(
            open_blocker_label(reference, facts.repository) for reference in facts.open_blockers
        )
    if not facts.contract_complete and not facts.projectionless_idea:
        missing = ", ".join(missing_or_empty_sections(facts.contract))
        return f"body incomplete: {missing}"
    return None


def _actionable_reason(facts: _ActionabilityFacts) -> str | None:
    read_state_reason = _read_state_actionable_reason(facts)
    if read_state_reason is not None:
        return read_state_reason
    if facts.kind is ItemKind.CONTAINER:
        return "container; claim a child"
    return _claim_or_completeness_reason(facts)


def _expectation_cell(item: BoardItem) -> str:
    if item.expectation_state is ExpectationState.NONE:
        return "-"
    if item.expectation_state is ExpectationState.PROPOSED:
        return f"{item.expectation_progress.open}/{item.expectation_progress.total}"
    count = 0 if item.ruling_landings is None else item.ruling_landings
    suffix = " old" if item.ruling_old else ""
    return f"ruled {count}{suffix}"


def _claim_cell(item: BoardItem) -> str:
    if item.active_claim is None:
        return "-"
    suffix = " old" if item.claim_old else ""
    return f"{item.active_claim} {item.claim_age}{suffix}"


def _freed_cell(item: BoardItem) -> str:
    if item.freed_on is None or item.freed_days is None:
        return "-"
    freed_date = item.freed_on.astimezone(UTC).date().isoformat()
    return f"{freed_date} ({item.freed_days} d)"


def _brief(value: str | None, *, maximum: int = 48) -> str:
    # An absent value and `""` (a block skeleton's unfilled `next`, #150 §5)
    # render identically: a fresh child shows nothing in this column.
    if value is None or not value.strip():
        return "-"
    one_line = " ".join(value.split())
    return one_line if len(one_line) <= maximum else one_line[: maximum - 1] + "…"
