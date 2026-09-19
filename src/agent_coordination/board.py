"""Pure derivation and rendering for the read-only work board."""

from __future__ import annotations

import json
import re
import tomllib
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import cast

from . import items, protocol

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
# Printed by `render` (issue #248) whenever `Board.landings_derivable` is
# `False`: a board source that cannot list merged pull requests leaves
# `RECOVERY` and every `Stage.CODE_LANDED` row structurally empty, not
# temporarily so, and this line is the difference a reader cannot otherwise
# tell from the same "none" a proven-empty GitHub board would also print.
LANDINGS_NOT_DERIVABLE_LINE = (
    "landings are not derivable from this board source: recovery and code-landed stay empty"
)
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
# A fresh container carries no automatic parent-provenance the way `cut`
# gives a fresh child one, so its own skeleton states the prose the global
# contract requires when nothing blocks it (README "`Blocked by:` prose
# beside the block is documentation only") ahead of the same block schema
# `BLOCK_CHILD_SKELETON` already owns -- one owner for the projection keys,
# never a second schema for a container's own skeleton.
BLOCK_CONTAINER_SKELETON = f"Blocked by: nichts\n\n{BLOCK_CHILD_SKELETON}"
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
# RFC 3339 UTC, second precision -- the one timestamp shape this repository
# reads from a forge (github.TIMESTAMP_PATTERN) and now from a state-ref
# item's own record: duplicated here, not imported, because the Layers
# contract forbids `board` from depending on `github` (`github` depends on
# `board`, not the reverse).
RECORD_TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


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


def open_blocker_label(
    reference: IssueReference, repository: str, storage: Storage = Storage.GITHUB
) -> str:
    """How one entry of `BoardItem.open_blockers` (or `ChildItem.blocked_by`)
    is named against the board's own `repository`: a same-repository
    blocker prints `item_label`'s own id under `storage` (issue #292,
    Grok-Delta review of #300) -- unchanged `#n` under the default
    `Storage.GITHUB`; a foreign one is always the qualified `owner/repo#n`
    (`IssueReference.__str__`), since a foreign reference is never local to
    this repository's own storage pin."""
    if reference.repository != repository:
        return str(reference)
    return item_label(reference.number, storage)


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


def parse_item_reference(value: str) -> int:
    """One item reference -- `aco-xxxxxx` (`items.item_number`'s own hex
    decode), `#n`, or the bare integer `n` -- parsed to the number every
    forge port keys by (issue #285, decision D4: an id is identity, not just
    display, so a fresh id `item new` prints is something every other
    command can claim right back). The one owner for both every argparse
    slot that means an item (`cli`'s `type=`) and a trunk commit's
    `Work-Item:` trailer value (issue #304, `trunk_commit_classification`) --
    a git trailer never carries the `OWNER/REPO#n` form `WORK_ITEM_VALUE_PATTERN`
    accepts for a pull request body, since a commit is always local to the
    repository whose history it lands on."""
    if items.ITEM_ID_PATTERN.fullmatch(value) is not None:
        return items.item_number(value)
    digits = value.removeprefix("#")
    if digits.isdigit():
        return int(digits)
    raise protocol.ClaimUnavailableError(
        f"{value!r} is not an item reference; use aco-xxxxxx, #n, or the bare number n"
    )


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
class TrunkWorkItemClassification:
    """The work items one trunk commit's trailer block names as landed
    (issue #304). A trailer block may repeat `Work-Item:`; every named item
    is landed by that commit -- unlike a pull request body, which
    `parse_pull_request_classification` refuses past a single `Work-Item:`
    line, a commit's trailer block is already-landed history, not a
    contract this repository is still enforcing."""

    numbers: tuple[int, ...]

    def __str__(self) -> str:
        return "\n".join(f"Work-Item: #{number}" for number in self.numbers)


TrunkClassification = TrunkWorkItemClassification | NoItemClassification


def trunk_commit_classification(
    work_item_values: tuple[str, ...], no_item_values: tuple[str, ...]
) -> TrunkClassification | None:
    """A trunk commit's classification from its own trailer block alone
    (issue #304): `work_item_values`/`no_item_values` are read through git's
    own trailer parsing (`%(trailers:key=...,valueonly)`), so a `Work-Item:`
    or `No-Item:` line elsewhere in the body -- prose, not a trailer --
    never reaches here. `None` means the commit's trailer block named
    neither: most trunk commits are not a dispatched slice's landing, and
    that is not a defect worth surfacing the way an in-flight pull request's
    malformed classification is."""
    if work_item_values:
        return TrunkWorkItemClassification(
            tuple(parse_item_reference(value) for value in work_item_values)
        )
    if len(no_item_values) == 1 and no_item_values[0].lower() in {
        kind.value for kind in NoItemKind
    }:
        return NoItemClassification(NoItemKind(no_item_values[0].lower()))
    return None


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
    # Which adapter owns this repository's board and item data (issue #248):
    # `cli._resolved_forge_target` builds the one the pin names, never
    # guessed from the remote's own host. `github` is the default -- every
    # repository pinned today lives there.
    storage: Storage = Storage.GITHUB


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
    # False for a board source that cannot list merged pull requests at all
    # (issue #248): `recovery` and `Stage.CODE_LANDED` then stay
    # structurally empty rather than temporarily so, and `render`/
    # `board_json` say that plainly instead of rendering the same "none"
    # either way.
    landings_derivable: bool = True


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


def _validated_storage(raw: dict[str, object], path: Path) -> Storage:
    storage_raw = raw.get("storage")
    if storage_raw is None:
        return Storage.GITHUB
    if isinstance(storage_raw, str) and storage_raw in set(Storage):
        return Storage(storage_raw)
    raise protocol.ClaimError(
        f"board configuration {path} storage must be "
        f"{Storage.GITHUB.value!r} or {Storage.STATE_REF.value!r}"
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
        storage=_validated_storage(raw, path),
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
    slices: tuple[SliceRow, ...]
    read_state: BodyReadState
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


def _record_timestamp_defect(value: object, key_name: str) -> ContractDefect | None:
    if not isinstance(value, str) or RECORD_TIMESTAMP_PATTERN.fullmatch(value) is None:
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
    if origin is not None and not isinstance(origin, str):
        defects.append(ContractDefect("record.origin", "record.origin must be a string"))
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


def _block_schema_defects(data: dict[str, object], storage: Storage) -> tuple[ContractDefect, ...]:
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
    here is exactly what is still uncut."""
    return tuple(
        SliceRow(cast(int, entry["index"]), cast(str, entry["title"]))
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
        slices=_block_slices(data),
        read_state=BodyReadState.VALID,
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
        if "question" in expectation:
            lines.append(f"question = {_toml_string(expectation['question'])}")
        if "example" in expectation:
            lines.append(f"example = {_toml_string(expectation['example'])}")
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
                f"title = {_toml_string(entry['title'])}",
            )
        )
    return lines


def _render_record_array(values: object) -> str:
    return "[" + ", ".join(_toml_string(value) for value in cast("list[str]", values)) + "]"


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
        f"title = {_toml_string(record['title'])}",
        f"state = {_toml_string(record['state'])}",
    ]
    if record.get("kind") is not None:
        lines.append(f"kind = {_toml_string(record['kind'])}")
    lines.append(f"labels = {_render_record_array(record.get('labels', []))}")
    lines.append(f"blocked_by = {_render_record_array(record.get('blocked_by', []))}")
    if record.get("parent") is not None:
        lines.append(f"parent = {_toml_string(record['parent'])}")
    if record.get("origin") is not None:
        lines.append(f"origin = {_toml_string(record['origin'])}")
    lines.append(f"created_at = {_toml_string(record['created_at'])}")
    lines.append(f"updated_at = {_toml_string(record['updated_at'])}")
    if record.get("closed_at") is not None:
        lines.append(f"closed_at = {_toml_string(record['closed_at'])}")
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
    `default`. `question`/`example`/`picture` (issue #295) are the card's
    optional operator-language heading, illustration sentence, and inline
    SVG -- `None` when `aco ask` was not given them, in which case a card
    falls back to `text`."""

    index: int
    text: str
    ruling: str | None
    ruled_on: date | None
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
            question=cast(str | None, entry.get("question")),
            example=cast(str | None, entry.get("example")),
            picture=cast(str | None, entry.get("picture")),
        )
        for position, entry in enumerate(entries, start=1)
    )


def expectation_line_state(line: ExpectationLine) -> str:
    """`open`, or `ruled <ruling> <ruled_on>` -- the one state text
    `rulings` prints, in both its human and JSON forms."""
    if line.ruling is None:
        return "open"
    ruled_on = cast(date, line.ruled_on)
    return f"ruled {line.ruling} {ruled_on.isoformat()}"


def expectation_line_summary(line: ExpectationLine) -> str:
    """`line.text` on one line, truncated to `EXPECTATION_LINE_TEXT_MAXIMUM`
    characters -- `rulings`' human form; `--json` carries the full text."""
    return _brief(line.text, maximum=EXPECTATION_LINE_TEXT_MAXIMUM)


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
        raise protocol.ClaimError(
            f"line {index} out of range: this item has {len(entries)} expectation line(s)"
        )
    entry = entries[index - 1]
    if "ruling" in entry:
        raise protocol.ClaimError(f"line {index} is already ruled; a changed ruling is a new line")
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
        raise protocol.ClaimError("expectation text must be a non-empty string")
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
            raise protocol.ClaimError(f"{key} {reason}")
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


def has_label(labels: tuple[str, ...], label: str | None) -> bool:
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
    open_pull_requests_supported: bool
    trunk_landings: tuple[datetime, ...]
    container_progress: dict[int, ContainerProgress]
    child_container: dict[int, int]
    repository: str


def _board_stage(
    issue: Issue, claim: protocol.ScopedClaim | None, context: _BoardBuildContext
) -> Stage:
    # A board source that cannot list open pull requests at all (issue #248,
    # `state-ref`) never populates `open_branches`, so a live claim can never
    # match it; its own honest in-flight signal is a live claim with a
    # branch, not a PR head this source structurally cannot see.
    in_flight = issue.number in context.in_flight_references or (
        claim is not None
        and bool(claim.branch)
        and (not context.open_pull_requests_supported or claim.branch in context.open_branches)
    )
    if in_flight:
        return Stage.IN_FLIGHT
    if issue.number in context.landed_references:
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
    stage = _board_stage(issue, claim, context)
    single_next = _single_concrete_next(contract.next)
    projectionless_idea = parsed.projectionless and has_label(issue.labels, config.idea_label)
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
            storage=config.storage,
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
    # The board source's own capability (issue #248), never the storage
    # pin: a source that cannot list pull requests at all -- `state-ref`
    # today, honestly, not by name -- still has an in-flight signal (a live
    # claim with a branch) but no landed one, so the two stay independent
    # booleans instead of one storage-shaped flag.
    open_pull_requests_supported: bool = True
    landings_derivable: bool = True


def build_board(inputs: BoardBuildInputs) -> Board:
    issues = inputs.issues
    open_pull_requests = inputs.open_pull_requests
    recent_merged_pull_requests = inputs.recent_merged_pull_requests
    config = inputs.config
    repository = inputs.repository
    observed_at = (inputs.now or datetime.now(UTC)).astimezone(UTC)
    parsed_bodies = {
        issue.number: parse_body(issue.body, storage=config.storage) for issue in issues
    }
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
        open_pull_requests_supported=inputs.open_pull_requests_supported,
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
        landings_derivable=inputs.landings_derivable,
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
    """`container` has no open child and a still-undispatched `[[slice]]`
    row: that row is the typed statement that there is something to cut, and
    is the only thing this action ever fires on (issue #208). `next_step` is
    the container's own words (its `Next` line when it still names work,
    else the row's own title) for the human-readable action line;
    `cut_title` is the exact string `cut` itself accepts for the printed
    `cut` command -- always the first uncut row's title, the entry `cut`
    without `--row` links. The two agree only when the `Next` line names no
    work of its own. A container with no uncut row is never a
    `CutSliceAction`, however much prose its `Next` line still carries: that
    prose is not a slice title, and printing it as one built an unrunnable
    `cut --title "<paragraph>"` from a container's whole sentence."""

    container: BoardItem
    container_progress: ContainerProgress
    next_step: str
    cut_title: str


@dataclass(frozen=True)
class CloseContainerAction:
    """`container` has no open child and no uncut slice row: there is
    nothing to cut, so this action never proposes a `cut` command (issue
    #208). `next_step` carries the container's own `Next` sentence when that
    line still names real work -- not a cut, since no slice row offers one --
    or `None` when it names none, the original "every child closed, nothing
    left" case that gives the class its name."""

    container: BoardItem
    container_progress: ContainerProgress
    next_step: str | None


NextAction = WorkItemAction | CutSliceAction | CloseContainerAction


def next_action(board: Board) -> NextAction | None:
    """The one action `next` recommends: the board's own top qualifying row.

    Walks `items` in `board_rank` order -- the same order `board` shows --
    and returns the first row that is either an actionable non-container
    (`WorkItemAction`; a container is never actionable, so this branch never
    fires for one) or a container with no open child (`CutSliceAction` when
    its block still carries an undispatched `[[slice]]` row, else
    `CloseContainerAction` -- whether or not its own `Next` line still names
    work; an empty slice table is the typed statement that there is nothing
    to cut, and #208 is what happened when a fallback ignored it).
    `_container_progress` already fails loud on a container whose summary
    disagrees with its open-children list, so "no open child" here reliably
    means every created child has closed. Every other row -- blocked,
    claimed, incomplete, or a container still holding an open child -- is
    skipped, never blocking a lower-ranked qualifying row.

    Whichever branch prints a command, it never carries `--row` (#151): `cut`
    without `--row` accepts every container a `CutSliceAction` names here,
    linking its first undispatched slice. `CloseContainerAction` never
    prints a command at all, whether or not its `Next` line still names
    work -- inventing one from prose that is not a slice title is #208.

    A `MALFORMED` container (#150) is skipped here exactly like one still
    holding an open child: its own finding already surfaces through
    `actionable_reason`/`SKIPPED`, and proposing to cut or close a body
    that could not be read would act on a guess this module never makes.
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
    through.

    An uncut `[[slice]]` row is the only thing that makes this a
    `CutSliceAction` (#208): a container's `Next` line naming further work is
    not, by itself, a slice to cut, so an empty slice table -- the typed
    statement that there is nothing here to cut -- always lands in
    `CloseContainerAction`, carrying that `Next` sentence as `next_step`
    instead of a fabricated `cut` title."""
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
    further_work = next_line if next_line is not None and has_further_work(next_line) else None
    return CloseContainerAction(item, container, further_work)


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


def item_label(number: int, storage: Storage) -> str:
    """The one display form of `number` any narrative output prints under
    `storage` (issue #292): `items.format_item_id`'s `aco-xxxxxx` under
    `storage = STATE_REF` -- an id `parse_item_reference` already accepts
    right back, so what a command prints is what the next command takes --
    unchanged `#n` under `storage = GITHUB`. `board` is the lowest layer
    that may import `items` (the Layers contract), and both `cli` and
    `board_html` already import `board`, so this is the one owner both call
    into rather than each keeping its own copy."""
    if storage is Storage.STATE_REF:
        return items.format_item_id(number)
    return f"#{number}"


def render(board: Board, *, storage: Storage = Storage.GITHUB) -> str:
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
                item_label(item.number, storage),
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
                    open_blocker_label(reference, board.repository, storage)
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
    ready = ", ".join(item_label(item.number, storage) for item in board.ready_now) or "none"
    stale = ", ".join(item_label(item.number, storage) for item in board.stale) or "none"
    recovery = ", ".join(item_label(item.number, storage) for item in board.recovery) or "none"
    containers = "\n".join(_container_lines(board, storage)) or "none"
    uncut = "\n".join(_uncut_line(finding, storage) for finding in board.uncut) or "none"
    landings_note = "" if board.landings_derivable else f"\n{LANDINGS_NOT_DERIVABLE_LINE}"
    return (
        f"{table}\n\nREADY NOW\n{ready}\n\nSTALE\n{stale}\n\nRECOVERY ({RECOVERY_STEP})\n{recovery}"
        f"{landings_note}"
        f"\n\nCONTAINERS\n{containers}\n\nUNCUT\n{uncut}\n\nrequests: {board.requests}"
    )


def _open_child_cell(child: ChildItem, repository: str, storage: Storage) -> str:
    if not child.blocked_by:
        return item_label(child.number, storage)
    blockers = ", ".join(
        open_blocker_label(reference, repository, storage) for reference in child.blocked_by
    )
    return f"{item_label(child.number, storage)} (blocked by {blockers})"


def _container_line(
    number: int, container: ContainerProgress, repository: str, storage: Storage
) -> str:
    open_children = ", ".join(
        _open_child_cell(child, repository, storage) for child in container.open_children
    )
    label = item_label(number, storage)
    return f"{label} {container.closed}/{container.total} closed; open: {open_children or 'none'}"


def _container_lines(board: Board, storage: Storage) -> list[str]:
    return [
        _container_line(item.number, item.container, board.repository, storage)
        for item in board.items
        if item.container is not None
    ]


def _uncut_line(finding: UncutSlices, storage: Storage) -> str:
    indices = ", ".join(str(row.index) for row in finding.rows)
    return f"{item_label(finding.item, storage)}: rows {indices} uncut"


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
    storage: Storage
    contract: Contract
    contract_complete: bool
    projectionless_idea: bool
    read_state: BodyReadState = BodyReadState.VALID
    malformed_defect: ContractDefect | None = None


def _read_state_actionable_reason(facts: _ActionabilityFacts) -> str | None:
    """The one refusal a malformed body gets, ahead of every other reason --
    including the container rule, so a container whose body itself cannot
    be read is never offered as "claim a child" (#150 §5)."""
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
            open_blocker_label(reference, facts.repository, facts.storage)
            for reference in facts.open_blockers
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
