"""Pure derivation and rendering for the read-only work board."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import asdict, dataclass, field, fields, replace
from datetime import UTC, date, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import cast

from . import items, metrics, protocol

# The body-block codec (issue #419, audit #365 finding 3): parsed, validated,
# and rendered in `body.py`, one layer below this module. Every reader
# imports the names it needs from `body` directly (operator ruling
# 16.09.2026, audit #370 finding 10); `board.py` itself imports only what
# its own board-builder code below actually calls.
from .body import (
    BodyReadState,
    Contract,
    ContractDefect,
    ExpectationProgress,
    ExpectationState,
    ItemKind,
    ParsedBody,
    SliceRow,
    Storage,
    body_defect_text,
    closing_fence_delimiter,
    missing_or_empty_sections,
    opening_fence_delimiter,
    parse_body,
)

DEFAULT_PRIORITY_LABELS = ("security", "data", "ci", "product", "ux", "cleanup")
CONFIG_PATH = Path(".agent-claim/board.toml")
IDEA_REFINEMENT_STEP = "Problem neu prüfen und Item verfeinern"
RULING_OLD_AFTER_LANDINGS = 10
STALE_IDLE_DAYS = 7
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


TrunkClassification = TrunkWorkItemClassification | NoItemClassification


def trunk_commit_classification(
    work_item_values: tuple[str, ...], no_item_values: tuple[str, ...]
) -> TrunkClassification | ClassificationDefect | None:
    """A trunk commit's classification from its own trailer block alone
    (issue #304): `work_item_values`/`no_item_values` are read through git's
    own trailer parsing (`%(trailers:key=...,valueonly)`), so a `Work-Item:`
    or `No-Item:` line elsewhere in the body -- prose, not a trailer --
    never reaches here. `None` means the commit's trailer block named
    neither: most trunk commits are not a dispatched slice's landing, and
    that is not a defect worth surfacing the way an in-flight pull request's
    malformed classification is.

    A block naming both `Work-Item:` and `No-Item:`, or repeating
    `No-Item:`, is contradictory rather than merely absent -- `check <pr>`
    already refuses the equivalent shape in a pull request body
    (`_single_classification_match`) -- so it refuses with a typed
    `ClassificationDefect` instead of letting `Work-Item:` win by ordering
    (issue #304 review, finding B2). A malformed `Work-Item:` value also
    returns a defect: that commit lands no items, while the rest of the
    trunk history remains readable."""
    if work_item_values and no_item_values:
        return ClassificationDefect(
            "carries both `Work-Item:` and `No-Item:` trailers; a landed commit is one or the other"
        )
    if len(no_item_values) > 1:
        return ClassificationDefect("carries more than one `No-Item:` trailer")
    if work_item_values:
        numbers: list[int] = []
        for value in work_item_values:
            try:
                numbers.append(parse_item_reference(value))
            except protocol.ClaimUnavailableError:
                return ClassificationDefect(
                    f"carries `Work-Item: {value}`; "
                    "a trunk trailer names #n, aco-xxxxxx, or the bare number n"
                )
        return TrunkWorkItemClassification(tuple(numbers))
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
class TrunkLandingItem:
    """One item a trunk commit's own trailer names as landed (issue #371):
    `sha`/`committed_at` identify that commit. Read once by the caller from
    `checkout.trunk_landings` (a layer this module may never import) and
    handed in as plain data -- one entry per named item, since a trailer
    block may repeat `Work-Item:` (`TrunkWorkItemClassification`). The one
    source `landing_rows` below drives the board's Landungen view from,
    under both storages."""

    item: int
    sha: str
    committed_at: datetime


@dataclass(frozen=True)
class TrunkLandingEvidence:
    """A `LandingRow`'s primary evidence (issue #371): the trailer-carrying
    trunk commit that named its item landed, read straight from local git
    history under both storages."""

    sha: str


@dataclass(frozen=True)
class PullRequestLandingEvidence:
    """A `LandingRow`'s `github`-only supplementary evidence (issue #371):
    the merged pull request that plainly closed, declared, or landed its
    item (LAND-41's closing/landing keywords; never a bare `Refs`/`Part of`
    touch, LAND-45), used only when no trunk commit's own trailer names
    that item at all -- an older squash landing whose commit message
    carries no trailer."""

    number: int


LandingEvidence = TrunkLandingEvidence | PullRequestLandingEvidence


@dataclass(frozen=True)
class LandingRow:
    """One row of the board's Landungen view (issue #371): `item` landed at
    `committed_at`, evidenced by `evidence`. This is the one projection
    `board`, `--json`, and `board --html` all read, so no two ever disagree
    about what landed and when -- independent of whether `item` is still
    open, unlike `Stage.CODE_LANDED`."""

    item: int
    committed_at: datetime
    evidence: LandingEvidence


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


class Stage(StrEnum):
    TEXT_ONLY = "text-only"
    CODE_LANDED = "code-landed"
    IN_FLIGHT = "in-flight"


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
    # The block's own top-level `scope = [...]` (issue #348), exactly
    # `ParsedBody.scope`'s canonical tuple -- `None` when the item names no
    # scope of its own, the case `next`'s `Run:` line and `parallel_set`'s
    # disjointness walk both have to name rather than guess through.
    scope: tuple[str, ...] | None
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
    # This item's own top-level `size` (issue #357), exactly `ParsedBody.size`
    # -- carried here too so a renderer can tell "no size at all" apart from
    # "sized, but its class has no measured lane yet", which `estimate`
    # alone (`None` in both cases) cannot.
    size: metrics.Size | None
    # Whether the body still carries any `[[slice]]` row (issue #399):
    # `scope is None` alone cannot tell a truly unscoped item apart from one
    # that names no top-level scope yet but still cuts one from a row, so
    # `_buildable` reads both rather than `scope` alone.
    has_slices: bool
    # This item's own size class's measured estimate (issue #357), or
    # `None` when the item names no size, or names one no measured lane has
    # reached yet -- an unmeasured class is exactly as unestimated as an
    # unsized item, never a guessed number.
    estimate: metrics.Estimate | None


@dataclass(frozen=True)
class SizeClassMeasurement:
    """One size class's own row in the board's measurements section (issue
    #357): `metrics.SizeClassStats`' median/p80/n/weak, plus the first and
    last measured lane's own timestamp -- dates `metrics.py` never computes
    (it holds no clock), so `board.py` joins them from the same lane events
    `metrics.measure` read to build `stats`."""

    stats: metrics.SizeClassStats
    first_event_at: datetime
    last_event_at: datetime


@dataclass(frozen=True)
class Measurements:
    """The board's own measured-lane section (issue #357): `classes` is
    `metrics.MetricsReport.classes` plus each class's own date range,
    `unfinished` is `metrics.MetricsReport.incomplete` (a still-open claim,
    counted but never measured), `unparsed` is
    `BoardBuildInputs.unparsed_lifecycle_commits` (a claim-shaped commit
    `store.claim_lifecycle` could not read at all, issue #357 R1), and
    `since` is the earliest lane event this build read at all -- `None`
    only when there is none, the one case the board's own "keine Messungen
    seit <Datum>" sentence reads a render-time date instead."""

    classes: tuple[SizeClassMeasurement, ...]
    unfinished: int
    unparsed: int
    since: datetime | None
    as_of: date


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

    `landings` is the Landungen view (issue #371): `landing_rows`' own
    projection over the trunk walk (plus, under `github`, its
    pull-request supplement), independent of `items`/`recovery` -- a row
    stands whether or not its item is still open.
    """

    items: tuple[BoardItem, ...]
    ready_now: tuple[BoardItem, ...]
    stale: tuple[BoardItem, ...]
    recovery: tuple[BoardItem, ...]
    landings: tuple[LandingRow, ...]
    uncut: tuple[UncutSlices, ...]
    repository: str
    requests: int
    measurements: Measurements


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


# The repository-owned rules a lane step's dispatch brief prints (issue
# #324): a repository used to carry these ~25 lines by hand in every
# dispatch, pasted fresh each time. `.agent-claim/brief.toml` gives it one
# tracked owner instead, read only when `aco brief --step` asks for it.
BRIEF_CONFIG_PATH = Path(".agent-claim/brief.toml")


class BriefStep(StrEnum):
    """The four lane steps a dispatch brief can print rules and checks for
    (issue #324) -- `aco brief --step`'s own choices, and `.agent-claim/
    brief.toml`'s four section names."""

    BUILD = "build"
    REVIEW = "review"
    FIX = "fix"
    LAND = "land"


@dataclass(frozen=True)
class BriefStepRules:
    """One `.agent-claim/brief.toml` section's own content: the repository's
    conduct sentences for this step (`rules`) and the exact commands that
    verify it (`checks`). Either list is empty, never absent, when the
    section names none -- a step no repository has opinions about yet is not
    a parse defect."""

    rules: tuple[str, ...] = ()
    checks: tuple[str, ...] = ()


@dataclass(frozen=True)
class BriefConfig:
    """`.agent-claim/brief.toml`'s own four sections, one `BriefStepRules`
    each -- the file `load_brief_config` reads and `for_step` indexes by the
    same `BriefStep` `aco brief --step` accepts."""

    build: BriefStepRules = field(default_factory=BriefStepRules)
    review: BriefStepRules = field(default_factory=BriefStepRules)
    fix: BriefStepRules = field(default_factory=BriefStepRules)
    land: BriefStepRules = field(default_factory=BriefStepRules)

    def for_step(self, step: BriefStep) -> BriefStepRules:
        return {
            BriefStep.BUILD: self.build,
            BriefStep.REVIEW: self.review,
            BriefStep.FIX: self.fix,
            BriefStep.LAND: self.land,
        }[step]


# Every top-level section `.agent-claim/brief.toml` defines; anything else is
# a typo, refused by name the same way `_refuse_unknown_config_keys` refuses
# one in `board.toml`.
BRIEF_CONFIG_STEPS = frozenset(step.value for step in BriefStep)
# The only two keys a `[build]`/`[review]`/`[fix]`/`[land]` table may carry.
BRIEF_STEP_KEYS = frozenset({"rules", "checks"})


def _validated_brief_string_list(
    raw: dict[str, object], path: Path, section: str, key: str
) -> tuple[str, ...]:
    values = raw.get(key)
    if values is None:
        return ()
    if not isinstance(values, list) or not all(
        isinstance(value, str) and value.strip() == value and value for value in values
    ):
        raise protocol.ClaimError(
            f"brief configuration {path} [{section}] {key} must be a list of non-empty strings"
        )
    return tuple(values)


def _validated_brief_step_rules(
    raw: dict[str, object], path: Path, step: BriefStep
) -> BriefStepRules:
    section = step.value
    section_raw = raw.get(section)
    if section_raw is None:
        return BriefStepRules()
    if not isinstance(section_raw, dict):
        raise protocol.ClaimError(f"brief configuration {path} [{section}] must be a table")
    unknown = sorted(set(section_raw) - BRIEF_STEP_KEYS)
    if unknown:
        named = ", ".join(unknown)
        raise protocol.ClaimError(f"brief configuration {path} [{section}] has unknown key {named}")
    return BriefStepRules(
        rules=_validated_brief_string_list(section_raw, path, section, "rules"),
        checks=_validated_brief_string_list(section_raw, path, section, "checks"),
    )


def _refuse_unknown_brief_config_keys(raw: dict[str, object], path: Path) -> None:
    unknown = sorted(set(raw) - BRIEF_CONFIG_STEPS)
    if unknown:
        named = ", ".join(unknown)
        raise protocol.ClaimError(f"brief configuration {path} has unknown top-level key {named}")


def load_brief_config(path: Path = BRIEF_CONFIG_PATH) -> BriefConfig | None:
    """The repository's own `.agent-claim/brief.toml` (issue #324), or `None`
    when it does not exist at `path` at all. Unlike `load_config`, absence
    is not a default to fall back on: `aco brief --step` refuses on it, so
    an agent asking for rules that were never written learns that instead
    of silently seeing none. Plain `aco brief` never calls this at all."""
    if not path.exists():
        return None
    try:
        with path.open("rb") as stream:
            raw = tomllib.load(stream)
    except (OSError, tomllib.TOMLDecodeError) as error:
        raise protocol.ClaimError(f"cannot read brief configuration {path}: {error}") from error
    _refuse_unknown_brief_config_keys(raw, path)
    return BriefConfig(
        build=_validated_brief_step_rules(raw, path, BriefStep.BUILD),
        review=_validated_brief_step_rules(raw, path, BriefStep.REVIEW),
        fix=_validated_brief_step_rules(raw, path, BriefStep.FIX),
        land=_validated_brief_step_rules(raw, path, BriefStep.LAND),
    )


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
            opening = opening_fence_delimiter(line)
            if opening is not None:
                fence_char, fence_length = opening
                continue
            lines.append(line)
            continue
        closing = closing_fence_delimiter(line)
        if closing is not None and closing[0] == fence_char and closing[1] >= fence_length:
            fence_char, fence_length = None, 0
        # Still inside the fence (or just closed it): never scanned for a marker.
    return "\n".join(lines)


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


def _merged_at(pull_request: PullRequest) -> datetime:
    """`pull_request`'s own merge date, parsed the way every other board
    timestamp is (issue #371): `recent_merged_pull_requests` names only
    already-merged pull requests, so a `None` here is the forge answering a
    listing it does not honor, not a shape this function ever papers over."""
    if pull_request.merged_at is None:
        raise protocol.ClaimError("a recently-merged pull request carries no merge date")
    return _timestamp(pull_request.merged_at)


def _pull_request_landing_rows(
    recent_merged_pull_requests: tuple[PullRequest, ...],
    already_landed: frozenset[int],
    repository: str,
) -> tuple[LandingRow, ...]:
    """`github`'s own supplement to the trunk walk (issue #371): one
    `LandingRow` per item a merged pull request plainly closes, declares, or
    lands -- LAND-41's own closing/landing keywords, never a `Refs`/`Part of`
    touch (LAND-45; a touch confers only the board's in-flight/landed
    *stage*, `_touched_without_closing`, not a Landungen row) -- that no
    trunk commit's trailer already names (`already_landed`, the trunk
    walk's own dedup, trailer path first). The first pull request naming a
    given item wins when more than one plausibly could, sorted by its own
    merge time (oldest first, so "first" means whichever pull request
    actually landed the item first) with the pull request number as a
    stable tie-break for two merged at the same instant -- never
    `recent_merged_pull_requests`' own possibly-unordered adapter order.
    `_merged_at` is read only for a pull request that names at least one
    item this way: one that never does carries no landing evidence and so
    is never required to carry a merge date either."""
    claims_by_pull_request = {
        pull_request: _associated_issues((pull_request,), repository) - already_landed
        for pull_request in recent_merged_pull_requests
    }
    rows: dict[int, LandingRow] = {}
    for pull_request in sorted(
        (pull_request for pull_request, claims in claims_by_pull_request.items() if claims),
        key=lambda pull_request: (_merged_at(pull_request), pull_request.number),
    ):
        claimed = claims_by_pull_request[pull_request] - rows.keys()
        if not claimed:
            continue
        committed_at = _merged_at(pull_request)
        evidence = PullRequestLandingEvidence(pull_request.number)
        for item in sorted(claimed):
            rows[item] = LandingRow(item, committed_at, evidence)
    return tuple(rows.values())


def _landing_row_tie_break_key(row: LandingRow) -> tuple[int, int, int]:
    """The stable, ascending order two rows sharing `committed_at` fall back
    to (issue #371): item number first, then evidence kind (a trunk
    trailer's row before a pull request's -- the trailer path always wins
    the same item, so this is unreachable today but keeps the key total
    rather than coincidentally sufficient), then pull request number.
    `landing_rows` negates every component below so its one
    `sorted(..., reverse=True)` pass resolves `committed_at` genuinely
    descending while this tie-break still comes out ascending -- avoiding
    a second, sorted-fed-into-sorted pass (Sonar python:S7508, issue
    #371)."""
    if isinstance(row.evidence, TrunkLandingEvidence):
        return (row.item, 0, 0)
    return (row.item, 1, row.evidence.number)


def landing_rows(
    trunk_landing_items: tuple[TrunkLandingItem, ...],
    recent_merged_pull_requests: tuple[PullRequest, ...],
    repository: str,
    storage: Storage,
) -> tuple[LandingRow, ...]:
    """The board's Landungen view (issue #371): one row per item a trunk
    commit's own trailer names as landed, across the trunk walk's own depth
    -- the one source under both storages, independent of whether that item
    is still open. `github` alone adds one more row per item a merged pull
    request plainly landed with no trailer of its own (`github`'s squash
    convention before this repository trailer-tagged every landing),
    deduplicated against the trunk rows, trailer path first. Newest first,
    so a reader sees the most recent landing at the top -- two rows landed
    at the same instant fall back to `_landing_row_tie_break_key`, negated
    into the same `sorted(..., reverse=True)` pass rather than a second,
    sorted-fed-into-sorted pass, so equal timestamps still resolve to one
    deterministic order."""
    trunk_rows = {
        entry.item: LandingRow(entry.item, entry.committed_at, TrunkLandingEvidence(entry.sha))
        for entry in trunk_landing_items
    }
    pull_request_rows = (
        _pull_request_landing_rows(recent_merged_pull_requests, frozenset(trunk_rows), repository)
        if storage is Storage.GITHUB
        else ()
    )
    return tuple(
        sorted(
            (*trunk_rows.values(), *pull_request_rows),
            key=lambda row: (
                row.committed_at,
                *(-component for component in _landing_row_tie_break_key(row)),
            ),
            reverse=True,
        )
    )


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
    estimate_by_number: Mapping[int, metrics.Estimate]


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
        scope=parsed.scope,
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
        size=parsed.size,
        has_slices=bool(parsed.slices),
        estimate=context.estimate_by_number.get(issue.number),
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
    # Item numbers a trunk commit's own trailer block already names as
    # landed (issue #304) -- independent of `trunk_landings` above, which
    # carries only each commit's timestamp for ruling-freshness, never its
    # classification. The caller's own rollup of `trunk_landing_items`
    # below (issue #371), which carries the same numbers with their sha.
    trunk_landed_work_items: frozenset[int] = frozenset()
    # Every item a trunk commit's own trailer names as landed, `sha` and all
    # (issue #371) -- the one source `landing_rows` builds the board's
    # Landungen view from, under both storages. Read once by the caller from
    # the same `checkout.trunk_landings` call that already feeds
    # `trunk_landed_work_items`/`landed_at_by_item` above; kept as its own
    # field rather than folded into those two, since neither carries `sha`.
    trunk_landing_items: tuple[TrunkLandingItem, ...] = ()
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
    # Every claim's own ref-history lifecycle (issue #357,
    # `store.claim_lifecycle`): `size`/`landed_at` still `None` exactly as
    # that reader leaves them -- `build_board` is the one place both are
    # already known (an open item's own current size; a trunk landing's own
    # date), so it joins them before ever calling `metrics.measure`.
    lane_events: tuple[metrics.LaneEvent, ...] = ()
    # A landed item's own commit date (issue #304's own trailer block,
    # `board.TrunkWorkItemClassification`), keyed by item number -- read
    # once by the caller from `checkout.trunk_landings` (a layer `board.py`
    # may never import) and handed in as plain data.
    landed_at_by_item: Mapping[int, datetime] = field(default_factory=dict)
    # Every completed lane's own item's *current* size, for an item that is
    # no longer among `issues` at all (issue #357 R2) -- closed, or vanished
    # -- read once by the caller through the forge/state for exactly the
    # numbers `lane_events` names outside `issues`, since a closed item's
    # own current size is still readable there even though `build_board`
    # never re-fetches its body itself. An open item's own size always
    # comes from `issues`/`parsed_bodies` instead; this map is consulted
    # only for a number that map does not carry.
    closed_item_sizes: Mapping[int, metrics.Size | None] = field(default_factory=dict)
    # Claim-shaped `refs/aco/state` commits `store.claim_lifecycle` could
    # not parse (issue #357 R1): older history predating its `item:`
    # trailer, or a foreign commit merely shaped like one -- counted here
    # rather than silently dropped, and shown beside `Measurements.unfinished`
    # in the board's own Messungen section.
    unparsed_lifecycle_commits: int = 0


def _item_number_or_none(value: str) -> int | None:
    return int(value) if value.isdigit() else None


def _joined_lane_event(
    event: metrics.LaneEvent,
    size_by_number: Mapping[int, metrics.Size | None],
    landed_at_by_item: Mapping[int, datetime],
) -> metrics.LaneEvent:
    """`event`, its `size` and `landed_at` filled in from this build's own
    already-fetched data (issue #357, closed items R2): a historical
    claim's size is read from its item's *current* size, open or closed --
    `size_by_number` is `build_board`'s own union of every open item's
    parsed size and `inputs.closed_item_sizes` (read once through the
    forge/state for exactly the numbers not among the open ones) -- and its
    landing date from the trunk commit that named it, when one already did.
    A lane claim (`docs/`/`fix/`, no issue number) never matches either
    mapping and is returned unchanged, still counted by `metrics.measure`
    but never sorted into a size class."""
    number = _item_number_or_none(event.item)
    if number is None:
        return event
    return replace(event, size=size_by_number.get(number), landed_at=landed_at_by_item.get(number))


def _class_measurement(
    stats: metrics.SizeClassStats, events: Iterable[metrics.LaneEvent]
) -> SizeClassMeasurement:
    measured = [
        event for event in events if event.size is stats.size and event.released_at is not None
    ]
    return SizeClassMeasurement(
        stats=stats,
        first_event_at=min(event.claimed_at for event in measured),
        last_event_at=max(cast(datetime, event.released_at) for event in measured),
    )


def _measurements(
    events: tuple[metrics.LaneEvent, ...],
    report: metrics.MetricsReport,
    observed_at: datetime,
    *,
    unparsed: int,
) -> Measurements:
    since = min((event.claimed_at for event in events), default=None)
    classes = tuple(_class_measurement(stats, events) for stats in report.classes)
    return Measurements(
        classes=classes,
        unfinished=report.incomplete,
        unparsed=unparsed,
        since=since,
        as_of=observed_at.date(),
    )


def _summed_lane_event(claims: Sequence[metrics.LaneEvent]) -> metrics.LaneEvent:
    """One item's own completed claims, summed into a single measured lane
    (issue #357 R2): their wall-clock durations added, never spanned or
    averaged, so an item worked across several claims -- a builder, then a
    fixer, each its own claim -- contributes exactly one sample to its size
    class rather than `len(claims)` independent ones that would inflate `n`
    and skew the median. `size`/`container`/`landed_at`/`item` are shared
    across every claim of one item (`_joined_lane_event` already set them
    from the same lookup), so the earliest claim's own values carry
    through unchanged; only `claimed_at`/`released_at` become a synthetic
    pair whose difference is the summed total, and `rescopes` sums too."""
    first = min(claims, key=lambda claim: claim.claimed_at)
    total_hours = sum(
        (cast(datetime, claim.released_at) - claim.claimed_at).total_seconds() / 3600
        for claim in claims
    )
    return replace(
        first,
        released_at=first.claimed_at + timedelta(hours=total_hours),
        rescopes=sum(claim.rescopes for claim in claims),
    )


def _measurement_feed(events: tuple[metrics.LaneEvent, ...]) -> tuple[metrics.LaneEvent, ...]:
    """`events`, prepared for `metrics.measure` (issue #357 R2): every
    item's own completed claims summed into one lane by `_summed_lane_event`
    -- still-open claims (`released_at is None`) pass through unchanged,
    since each is already its own still-running lane and
    `metrics.MetricsReport.incomplete` counts them without help from this
    function. `_measurements` itself still reads the caller's original,
    unsummed `events` for its own first/last measured-lane dates, so this
    synthetic feed is consulted only for the class statistics and
    per-item estimates `metrics.measure` computes."""
    completed_by_item: dict[str, list[metrics.LaneEvent]] = {}
    open_events: list[metrics.LaneEvent] = []
    for event in events:
        if event.released_at is None:
            open_events.append(event)
        else:
            completed_by_item.setdefault(event.item, []).append(event)
    summed = (_summed_lane_event(claims) for claims in completed_by_item.values())
    return (*summed, *open_events)


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
    size_by_number = {number: parsed.size for number, parsed in parsed_bodies.items()}
    historical_size_by_number = {**inputs.closed_item_sizes, **size_by_number}
    joined_events = tuple(
        _joined_lane_event(event, historical_size_by_number, inputs.landed_at_by_item)
        for event in inputs.lane_events
    )
    open_items = tuple(
        metrics.OpenItem(item=str(issue.number), size=size_by_number[issue.number], container=None)
        for issue in issues
    )
    report = metrics.measure(_measurement_feed(joined_events), open_items)
    measurements = _measurements(
        joined_events, report, observed_at, unparsed=inputs.unparsed_lifecycle_commits
    )
    estimate_by_number = {int(estimate.item): estimate for estimate in report.estimates}
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
        | _touched_without_closing(recent_merged_pull_requests)
        | inputs.trunk_landed_work_items,
        open_branches=frozenset(pr.head_ref_name for pr in open_pull_requests),
        open_pull_requests_supported=inputs.open_pull_requests_supported,
        trunk_landings=inputs.trunk_landings,
        container_progress=container_progress,
        child_container=child_container,
        repository=repository,
        estimate_by_number=estimate_by_number,
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
        landings=landing_rows(
            inputs.trunk_landing_items, recent_merged_pull_requests, repository, config.storage
        ),
        uncut=uncut,
        repository=repository,
        requests=inputs.requests,
        measurements=measurements,
    )


def _buildable(item: BoardItem) -> bool:
    """Whether `item` names a path `claim`/`start` could actually claim right
    now (issue #399): actionable, and naming either a top-level `scope` or at
    least one `[[slice]]` row to cut one from. `next`'s own top action still
    picks a scopeless, sliceless item -- printed `scope unknown` rather than
    refused (NEXT-03) -- so this predicate is asked only by the claim/start
    precedence check (`highest_scored_actionable`), never folded into
    `actionable`/`ready_now` themselves."""
    return item.actionable and (item.scope is not None or item.has_slices)


def highest_scored_actionable(board: Board) -> BoardItem | None:
    """The highest-ranked buildable row `claim`/`start`'s own precedence
    check (CLM-08) compares its target against -- `board`'s own top
    *buildable* row, not necessarily `next`'s own top action: an actionable
    item naming neither `scope` nor a slice row is one `next` still
    recommends (`scope unknown`), but never one this walk stops on, since
    claiming past it costs no `--out-of-order` (issue #399).

    `ready_now` is a filtered view of `items`, which `build_board` orders by
    `board_rank`; filtering preserves that order, so the first buildable
    element is `board`'s own top-ranked buildable row. Two commands over one
    board must not disagree on *order*, so this reads that order instead of
    maximizing score on its own — an unlabelled item with a higher score
    must never outrank a human's priority label.
    """
    return next((item for item in board.ready_now if _buildable(item)), None)


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


def _uncut_by_container(board: Board) -> dict[int, UncutSlices]:
    return {finding.item: finding for finding in board.uncut}


def _qualifying_actions(board: Board) -> Iterator[NextAction]:
    """Every row `next`'s family of readers can ever act on, in `board_rank`
    order (issue #348) -- not only the first: `next_action`, `parallel_set`,
    and `zero_cost_closes` all walk this one sequence instead of each
    re-deriving it, so the three stay in agreement by construction.

    Yields a row that is either an actionable non-container
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
    skipped, never blocking a later qualifying row.

    Whichever branch carries a command, it never carries `--row` (#151):
    `cut` without `--row` accepts every container a `CutSliceAction` names
    here, linking its first undispatched slice. `CloseContainerAction` never
    carries a command at all, whether or not its `Next` line still names
    work -- inventing one from prose that is not a slice title is #208.

    A `MALFORMED` container (#150) is skipped here exactly like one still
    holding an open child: its own finding already surfaces through
    `actionable_reason`/`SKIPPED`, and proposing to cut or close a body
    that could not be read would act on a guess this module never makes.
    """
    uncut_by_container = _uncut_by_container(board)
    for item in board.items:
        if item.actionable:
            yield WorkItemAction(item)
            continue
        container = item.container
        if item.kind is not ItemKind.CONTAINER or container is None or container.open_children:
            continue
        action = _container_next_action(item, container, uncut_by_container)
        if action is not None:
            yield action


def next_action(board: Board) -> NextAction | None:
    """The one action `next` recommends: the board's own top qualifying row
    -- the first of `_qualifying_actions`, whose docstring names what
    qualifies and why."""
    return next(_qualifying_actions(board), None)


def _action_number(action: NextAction) -> int:
    return action.item.number if isinstance(action, WorkItemAction) else action.container.number


def _action_scope(
    action: NextAction, uncut_by_container: Mapping[int, UncutSlices]
) -> tuple[str, ...] | None:
    """The scope one `NextAction` occupies for `parallel_set`'s disjointness
    accounting (issue #348): a work item's own top-level `scope`, or a cut
    proposal's row scope -- the same first uncut row `uncut_by_container`
    already named its `cut_title` from. `CloseContainerAction` always
    returns `()`: closing is a zero-cost action against no paths, never one
    `parallel_set` has to guard against. `None` means unknown -- the action
    names no scope of its own to check disjointness against."""
    if isinstance(action, WorkItemAction):
        return action.item.scope
    if isinstance(action, CutSliceAction):
        return uncut_by_container[action.container.number].rows[0].scope
    return ()


@dataclass(frozen=True)
class ParallelCandidate:
    """One free item `parallel_set` placed into the maximal disjoint set
    alongside the first action (issue #348): its own number, and the exact
    scope that earned it a place -- a work item's own top-level scope, or a
    cut proposal's row scope."""

    number: int
    scope: tuple[str, ...]


@dataclass(frozen=True)
class ParallelSet:
    """`next`'s own parallel-capacity projection (issue #348, Operator
    19.09.2026: "ist das die maximale Auslastung?"): a priority-preserving
    greedy walk of `_qualifying_actions`, occupying live claims' scopes and
    the first action's own scope before it starts, then placing each further
    candidate that stays disjoint from everything occupied so far and
    occupying it in turn -- board order already *is* this repository's
    priority order, so a greedy walk in that order is the packing this
    projection owes, not a globally optimal one. `scope_unknown` names every
    candidate the walk could not place either way, in board order, since a
    candidate with no scope of its own cannot be checked for disjointness.
    `first_scope_unknown` is set instead of computing anything at all when
    the first action itself names no scope -- the one case with no baseline
    to walk from -- and `candidates`/`scope_unknown` then stay empty."""

    candidates: tuple[ParallelCandidate, ...]
    scope_unknown: tuple[int, ...]
    first_scope_unknown: bool


def parallel_set(
    board: Board, live_claims: tuple[protocol.ScopedClaim, ...], action: NextAction | None
) -> ParallelSet:
    """The maximal set of further free items `next`'s first `action` can run
    alongside right now (issue #348) -- see `ParallelSet` for the packing
    rule. `CloseContainerAction` never competes for scope at all
    (`zero_cost_closes` names it instead) and is skipped outright, whether it
    is `action` itself (occupying nothing) or a later candidate. A
    `board.recovery` item -- landed but still open -- is `zero_cost_closes`'
    own domain too, never this walk's: it is skipped outright as a later
    candidate, so it neither claims a place in `candidates` nor occupies a
    scope that would silently crowd out a real free item behind it."""
    if action is None:
        return ParallelSet((), (), False)
    uncut_by_container = _uncut_by_container(board)
    first_scope = _action_scope(action, uncut_by_container)
    if first_scope is None:
        return ParallelSet((), (), True)
    occupied = [claim.scope for claim in live_claims]
    occupied.append(first_scope)
    first_number = _action_number(action)
    recovery_numbers = frozenset(item.number for item in board.recovery)
    candidates: list[ParallelCandidate] = []
    scope_unknown: list[int] = []
    for candidate in _qualifying_actions(board):
        if isinstance(candidate, CloseContainerAction):
            continue
        number = _action_number(candidate)
        if number == first_number or number in recovery_numbers:
            continue
        scope = _action_scope(candidate, uncut_by_container)
        if scope is None:
            scope_unknown.append(number)
            continue
        if any(protocol.scope_overlap_paths(scope, taken) for taken in occupied):
            continue
        candidates.append(ParallelCandidate(number, scope))
        occupied.append(scope)
    return ParallelSet(tuple(candidates), tuple(scope_unknown), False)


def zero_cost_closes(board: Board) -> tuple[int, ...]:
    """Every item `next` can close for free right now, regardless of which
    row ranks first (issue #348; #310 finding 29: "warum wurde #122 nicht
    geclosed? sollte aco das nicht feststellen?"): every childless container
    with no undispatched `[[slice]]` row -- `_qualifying_actions`'s own
    `CloseContainerAction` rows, not only the board's top-ranked one --
    union every landed-but-open item (`board.recovery`), in first-seen
    order. Neither needs a claim first."""
    closable_containers = (
        action.container.number
        for action in _qualifying_actions(board)
        if isinstance(action, CloseContainerAction)
    )
    recovery_numbers = (item.number for item in board.recovery)
    seen: set[int] = set()
    ordered: list[int] = []
    for number in (*closable_containers, *recovery_numbers):
        if number not in seen:
            seen.add(number)
            ordered.append(number)
    return tuple(ordered)


def closable_container_number(
    parent: ParentIssue, children: tuple[ChildItem, ...], storage: Storage
) -> int | None:
    """The container `release --merged`/`item close` should name as freshly
    closable once one of its children just landed (issue #348): `parent`'s
    own kind, `children`'s open count, and its own undispatched `[[slice]]`
    rows decide it exactly like `_qualifying_actions`'s own
    `CloseContainerAction` branch -- a non-container parent, one still
    holding another open child, or one with an uncut row is never named.
    Whether a parent relation exists at all is the caller's own read
    (`ParentIssue | None`, forge-specific); this function only ever decides
    once one is given, never re-checking a state its one caller already
    ruled out.
    """
    if parent.kind is not ItemKind.CONTAINER:
        return None
    if any(child.state is ChildState.OPEN for child in children):
        return None
    parsed = parse_body(parent.body, storage=storage)
    if parsed.read_state is not BodyReadState.VALID or parsed.slices:
        return None
    return parent.reference.number


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


# The shape one decoded JSON object takes -- one alias so `cast` names a
# real type instead of repeating the `"dict[str, object]"` string literal
# (python:S1192). `board_payload`'s own JSON-projection concern, unrelated
# to `body.py`'s identically-shaped alias for its TOML rendering -- kept
# local rather than shared across that boundary for two unrelated readers.
_JsonObject = dict[str, object]

# The shape a decoded homogeneous array of tables, or an `asdict`'d list of
# dataclasses, takes -- one alias so `cast` names a real type instead of
# repeating the string.
_JsonRows = list[_JsonObject]


def _project_blocker_references(entry: dict[str, object], key: str, repository: str) -> None:
    """Rewrite `entry[key]` (a list of `asdict`'d `IssueReference`s) into the
    pre-#150 local-int list plus a sibling `foreign_blockers` key (A2) -- the
    one projector `board_payload` uses for both `BoardItem.open_blockers`
    and each open child's `ChildItem.blocked_by`, never a mixed `int | str`
    list and never re-parsed from a label."""
    references = cast(_JsonRows, entry.pop(key))
    entry[key] = [
        reference["number"] for reference in references if reference["repository"] == repository
    ]
    entry["foreign_blockers"] = [
        f"{reference['repository']}#{reference['number']}"
        for reference in references
        if reference["repository"] != repository
    ]


def _project_uncut_row_scope(finding: dict[str, object]) -> None:
    """Drop a `None` `scope` from one uncut row's JSON dict rather than
    printing it (issue #331): the public shape before this lane was
    `{"index", "title"}` with no third key, and `board --json` still owes
    that to a row that names no scope of its own -- only a row that
    actually carries one gains the extra `"scope"` key, canonical and
    non-empty."""
    for row in cast(_JsonRows, finding["rows"]):
        if row["scope"] is None:
            del row["scope"]


def _project_landing_row(row: dict[str, object]) -> None:
    """One `LandingRow`'s JSON shape (issue #371): `committed_at` turned to
    ISO text like every other board timestamp, and `evidence` -- a nested
    `TrunkLandingEvidence`/`PullRequestLandingEvidence` dict after `asdict`
    -- flattened into `sha`/`pull_request`, exactly one of which is ever
    non-`null`, so a consumer never has to branch on which evidence
    dataclass produced a row."""
    row["committed_at"] = cast(datetime, row["committed_at"]).astimezone(UTC).isoformat()
    evidence = cast(_JsonObject, row.pop("evidence"))
    row["sha"] = evidence.get("sha")
    row["pull_request"] = evidence.get("number")


def _tuples_to_lists(value: object) -> object:
    """`asdict(board)` preserves every tuple-typed dataclass field as a
    tuple; a JSON array carries no such distinction, so `board_payload`
    walks the finished payload once more here and turns every tuple into a
    list -- the one place this module guarantees its own return is
    genuinely JSON-shaped, the same way every other command's hand-built
    `--json` payload in `cli.py` already is."""
    if isinstance(value, dict):
        return {key: _tuples_to_lists(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [_tuples_to_lists(item) for item in value]
    return value


def board_payload(board: Board) -> dict[str, object]:
    """`board`'s own `--json`/envelope payload (issue #412): every value
    already JSON-primitive -- dates and datetimes turned to ISO text,
    blocker references split and re-keyed, every tuple turned into a list
    -- so `cli.py`'s emitter can serialize it with a plain `json.dumps`,
    with no `default=` fallback of its own. The one owner both
    `board --json` and a future embedded read share; `board --html`/
    `--serve` render through `board_html.py` instead and never call this."""
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
    for row in cast(_JsonRows, payload["landings"]):
        _project_landing_row(row)
    for finding in cast(_JsonRows, payload["uncut"]):
        _project_uncut_row_scope(finding)
    _project_measurements(cast(_JsonObject, payload["measurements"]))
    return cast(_JsonObject, _tuples_to_lists(payload))


def _project_measurements(measurements: dict[str, object]) -> None:
    """`Measurements`' own `datetime`/`date` fields, turned into ISO text
    (issue #357) -- everything else in `payload["measurements"]` (`n`,
    `median_hours`, `p80_hours`, `weak`, the `Size` string) is already
    JSON-ready, since `metrics.py` carries no clock of its own."""
    measurements["as_of"] = cast(date, measurements["as_of"]).isoformat()
    since = cast("datetime | None", measurements["since"])
    measurements["since"] = None if since is None else since.astimezone(UTC).isoformat()
    for entry in cast(_JsonRows, measurements["classes"]):
        for key in ("first_event_at", "last_event_at"):
            entry[key] = cast(datetime, entry[key]).astimezone(UTC).isoformat()


NO_SIZE_CELL = "keine Größe"
WEAK_ESTIMATE_CELL = "schwach"
UNPARSED_TRAILER_SENTENCE = "Commits ohne lesbaren Item-Trailer"


def estimate_cell(item: BoardItem) -> str:
    """`item`'s own board cell (issue #357): `keine Größe` when the item
    names none at all, `schwach` when it does but its class has fewer than
    `metrics.WEAK_SAMPLE_THRESHOLD` measured lanes (zero included -- a class
    with no measured lane at all carries no `Estimate` either, exactly as
    weak as one that does but says so through `Estimate.weak`), else the
    measured median rounded to the hour."""
    if item.size is None:
        return NO_SIZE_CELL
    if item.estimate is None or item.estimate.weak:
        return WEAK_ESTIMATE_CELL
    estimate = item.estimate
    return f"~{round(estimate.median_hours)}h ({estimate.size.value}, n={estimate.n})"


def _class_measurement_line(entry: SizeClassMeasurement) -> str:
    stats = entry.stats
    weak = " (schwach)" if stats.weak else ""
    return (
        f"{stats.size.value}: n={stats.n}, median {round(stats.median_hours)}h, "
        f"p80 {round(stats.p80_hours)}h{weak}, "
        f"{entry.first_event_at.date().isoformat()}..{entry.last_event_at.date().isoformat()}"
    )


def measurements_lines(measurements: Measurements) -> list[str]:
    """`Measurements`, rendered as the board's own "Messungen"/"keine
    Messungen" lines (issue #357) -- the one text `render`, `board_html.py`,
    and `board --json`'s human-facing callers all read from, rather than
    each formatting `Measurements` its own way. `unfinished`/`unparsed`
    (BOARD-30) each append their own line whenever non-zero, independent of
    whether any class was itself measured -- an unparsed commit is never
    hidden just because nothing else could be measured."""
    as_of = measurements.as_of.isoformat()
    if measurements.classes:
        since = measurements.since.date().isoformat() if measurements.since is not None else as_of
        lines = [f"Messungen (Stand {as_of}, seit {since})"]
        lines.extend(_class_measurement_line(entry) for entry in measurements.classes)
    else:
        lines = [f"keine Messungen seit {as_of}"]
    if measurements.unfinished:
        lines.append(f"{measurements.unfinished} Lanes ohne Ende")
    if measurements.unparsed:
        lines.append(f"{measurements.unparsed} {UNPARSED_TRAILER_SENTENCE}")
    return lines


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


# git's own default abbreviation length -- a Landungen row's sha is evidence
# to look up, not a full identity, so the short form is enough (issue #371).
# The one owner: `board_html` renders the same evidence and imports this
# rather than keeping its own copy (issue #371 review finding R4).
SHORT_SHA_LENGTH = 7


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
