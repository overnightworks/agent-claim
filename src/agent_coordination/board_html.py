"""Render a static HTML board page from an already-projected `board.Board`.

Pure: no clock, no randomness, no `gh`/`git` call of its own. `cli.py`'s
`board --html` (issue #276) is the one caller -- it already holds every read
this module needs from `board.build_board`'s own inputs, `board.expectation_lines`
(#240), and the store's live claims, so building `BoardPage` and rendering it
costs nothing `board` was not already paying for.

The four sections follow the ruled picture (#234, #276), in fixed order:
"Wartet auf dich" (open `[[expectation]]` lines as cards, each with the
copyable `aco rule` command per outcome), "Lanes" (active claims with the
item's own Now/Next/Blocked by/Done when, verbatim from the body), "Themen"
(containers with their open children, then standalone items), and
"Landungen" (items `board` already classified `Stage.CODE_LANDED`, paired
with the merged pull request that plainly closes or declares them, when one
resolves -- or the one "nicht ableitbar" line when `landings_derivable` is
false). The three knowlagentic-only conventions (`Plain:`/`For you:`,
`Stage:`, `(lane: slug)`) are gone: a lane shows the item's own title and
contract text, nothing else.
"""

from __future__ import annotations

import html
import re
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import cast

from . import board

RULE_OUTCOMES: tuple[str, ...] = ("yes", "no", "later")


@dataclass(frozen=True)
class LaneClaimant:
    """Who holds an item's active claim, and where -- the fields `board.BoardItem`
    joins into one display string (`active_claim`, `"agent (role)"`) and the one
    field it does not carry at all (`branch`), split back out for the Lanes
    section's own columns."""

    agent: str
    role: str
    branch: str


@dataclass(frozen=True)
class ExpectationCard:
    """One still-open `[[expectation]]` line, ready for a "Wartet auf dich" card."""

    item: int
    item_title: str
    index: int
    text: str
    default: str


@dataclass(frozen=True)
class LaneCard:
    """One active claim, paired with the item's own contract fields verbatim."""

    item: int
    item_title: str
    agent: str
    role: str
    branch: str
    age: str
    now: str | None
    next: str | None
    blocked_by: str | None
    done_when: str | None


class TopicPartState(StrEnum):
    RUNNING = "running"
    YOU = "you"
    OPEN = "open"


_PART_STATE_LABEL: dict[TopicPartState, str] = {
    TopicPartState.RUNNING: "läuft",
    TopicPartState.YOU: "wartet auf dich",
    TopicPartState.OPEN: "offen",
}


@dataclass(frozen=True)
class TopicPart:
    number: int
    title: str | None
    state: TopicPartState
    blocked_by: str | None


@dataclass(frozen=True)
class Topic:
    item: int
    title: str
    closed: int
    total: int
    parts: tuple[TopicPart, ...]


@dataclass(frozen=True)
class LandedItem:
    item: int
    item_title: str
    pull_request_number: int | None
    pull_request_title: str | None


@dataclass(frozen=True)
class BoardPage:
    """Everything `render` shows, already derived by `build_page` -- `render`
    itself needs nothing beyond this, so a test can hand-build one without a
    live `board.Board`."""

    repository: str
    state_tip: str
    cards: tuple[ExpectationCard, ...]
    lanes: tuple[LaneCard, ...]
    topics: tuple[Topic, ...]
    landed: tuple[LandedItem, ...]
    landings_derivable: bool


def _open_expectation_defaults(body: str) -> dict[int, str]:
    """`index -> default` for `body`'s still-open `[[expectation]]` entries --
    the one field `board.expectation_lines` (#240) does not carry (`ruling`/
    `ruled_on` replace `default` the moment a line is ruled, never both at
    once -- `board.rule_expectation`). Called only for a body `board.py`
    already parsed valid: `build_page` filters to items with
    `expectation_progress.open > 0`, which a malformed body can never carry
    (`board._malformed_parsed_body` always reports `ExpectationProgress(0,
    0)`) -- so this reads the block straight rather than re-validating an
    invariant its one caller already guarantees."""
    entries = board.locate_agent_claim_block(body).data.get("expectation", [])
    return {
        position: cast(str, entry["default"])
        for position, entry in enumerate(cast(list[object], entries), start=1)
        if isinstance(entry, dict) and "default" in entry
    }


def _expectation_cards(
    item: board.BoardItem, body: str, *, storage: board.Storage
) -> tuple[ExpectationCard, ...]:
    defaults = _open_expectation_defaults(body)
    return tuple(
        ExpectationCard(item.number, item.title, line.index, line.text, defaults[line.index])
        for line in board.expectation_lines(body, storage=storage)
        if line.ruling is None
    )


def _lane_card(item: board.BoardItem, claimant: LaneClaimant, *, repository: str) -> LaneCard:
    blocked_by = ", ".join(
        board.open_blocker_label(reference, repository) for reference in item.open_blockers
    )
    return LaneCard(
        item=item.number,
        item_title=item.title,
        agent=claimant.agent,
        role=claimant.role,
        branch=claimant.branch,
        age=item.claim_age or "",
        now=item.contract.now,
        next=item.contract.next,
        blocked_by=blocked_by or None,
        done_when=item.contract.done_when,
    )


def _item_part_state(item: board.BoardItem) -> TopicPartState:
    """Whether an open item is worked on, waits on the operator, or sits
    untouched -- `item` is always open: `board.Board.items` never lists a
    closed one."""
    if item.active_claim:
        return TopicPartState.RUNNING
    proposed = item.expectation_state is board.ExpectationState.PROPOSED
    if proposed or item.expectation_progress.open > 0:
        return TopicPartState.YOU
    return TopicPartState.OPEN


def _topic_part(
    child: board.ChildItem, items_by_number: Mapping[int, board.BoardItem], *, repository: str
) -> TopicPart:
    """`child` is always open here: `board.py`'s own `_container_progress`
    filters `ContainerProgress.open_children` to `ChildState.OPEN` before
    this module ever sees it, and never exposes a closed child's number at
    all -- only the aggregate `closed`/`total` counts `_topics` reads
    straight off `item.container`."""
    item = items_by_number.get(child.number)
    state = TopicPartState.OPEN if item is None else _item_part_state(item)
    blocked_by = ", ".join(
        board.open_blocker_label(reference, repository) for reference in child.blocked_by
    )
    return TopicPart(child.number, item.title if item else None, state, blocked_by or None)


def _standalone_topic(item: board.BoardItem, *, repository: str) -> Topic:
    blocked_by = ", ".join(
        board.open_blocker_label(reference, repository) for reference in item.open_blockers
    )
    part = TopicPart(item.number, item.title, _item_part_state(item), blocked_by or None)
    return Topic(item=item.number, title=item.title, closed=0, total=1, parts=(part,))


def _topics(projected: board.Board) -> tuple[Topic, ...]:
    """Containers (with their currently open children -- `board.py` never
    exposes a closed child's number or title, so those count only toward
    `closed`/`total`), then standalone items, each its own single-part topic
    -- `projected.items` is already `board_rank`-ordered, so this preserves
    that order rather than re-deriving it."""
    items_by_number = {item.number: item for item in projected.items}
    topics: list[Topic] = []
    for item in projected.items:
        if item.container is not None:
            parts = tuple(
                _topic_part(child, items_by_number, repository=projected.repository)
                for child in item.container.open_children
            )
            topics.append(
                Topic(
                    item=item.number,
                    title=item.title,
                    closed=item.container.closed,
                    total=item.container.total,
                    parts=parts,
                )
            )
        elif item.container_parent is None:
            topics.append(_standalone_topic(item, repository=projected.repository))
    return tuple(topics)


def _closing_pull_request(
    number: int, pull_requests: tuple[board.PullRequest, ...], *, repository: str
) -> board.PullRequest | None:
    """The merged pull request that plainly closes or declares `number` its
    work item -- `board.closing_references`/`declared_work_items`, the two
    public conventions this repository's own pull requests use (a `Closes
    #N` line, or a `Work-Item: #N` trailer). Narrower than `board.py`'s own
    private `_associated_issues` (drops the "lands"/"implements" keyword and
    the un-closing epic-slice marker `_touched_without_closing` reads): those
    stay `board.py`'s own decision, so an epic's landed slices may show here
    with no resolved pull request rather than reproduce that private
    matching outside its own module."""
    for pull_request in pull_requests:
        closing = {
            reference.number
            for reference in board.closing_references(pull_request.body, repository)
            if reference.repository == repository
        }
        if number in closing or number in board.declared_work_items((pull_request,), repository):
            return pull_request
    return None


def _landed_items(
    projected: board.Board, recent_merged_pull_requests: tuple[board.PullRequest, ...]
) -> tuple[LandedItem, ...]:
    entries: list[LandedItem] = []
    for item in projected.items:
        if item.stage is not board.Stage.CODE_LANDED:
            continue
        pull_request = _closing_pull_request(
            item.number, recent_merged_pull_requests, repository=projected.repository
        )
        entries.append(
            LandedItem(
                item.number,
                item.title,
                None if pull_request is None else pull_request.number,
                None if pull_request is None else pull_request.title,
            )
        )
    return tuple(entries)


@dataclass(frozen=True)
class BoardSources:
    """Everything `build_page` needs beyond the projected `board.Board`
    itself -- each already read by `board --html`'s own `board` fetch
    (issue #276): `bodies` (each open issue's body), `claimants` (the live
    store claims, keyed by the issue they hold), the merged pull requests
    `board` itself reads to classify `Stage.CODE_LANDED`, the live store's
    own tip, and the repository's storage pin (for `board.expectation_lines`
    et al.)."""

    bodies: Mapping[int, str]
    claimants: Mapping[int, LaneClaimant]
    recent_merged_pull_requests: tuple[board.PullRequest, ...]
    state_tip: str
    storage: board.Storage = board.Storage.GITHUB


def build_page(projected: board.Board, sources: BoardSources) -> BoardPage:
    """`BoardPage` from exactly what `cli._cmd_board`'s own reads already
    hold. No new fetch, no clock, no randomness."""
    cards = tuple(
        card
        for item in projected.items
        if item.expectation_progress.open > 0
        for card in _expectation_cards(
            item, sources.bodies.get(item.number, ""), storage=sources.storage
        )
    )
    lanes = tuple(
        _lane_card(item, sources.claimants[item.number], repository=projected.repository)
        for item in projected.items
        if item.number in sources.claimants
    )
    landed = (
        _landed_items(projected, sources.recent_merged_pull_requests)
        if projected.landings_derivable
        else ()
    )
    return BoardPage(
        repository=projected.repository,
        state_tip=sources.state_tip,
        cards=cards,
        lanes=lanes,
        topics=_topics(projected),
        landed=landed,
        landings_derivable=projected.landings_derivable,
    )


def _inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    return re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)


def _rule_command(item: int, index: int, outcome: str) -> str:
    return f"aco rule {item} --line {index} --{outcome}"


_DEFAULT_TAG = '<span class="tag">Vorgabe</span>'


def _render_rule_line(card: ExpectationCard, outcome: str) -> str:
    command = html.escape(_rule_command(card.item, card.index, outcome))
    is_default = outcome == card.default
    return (
        f'<li class="{"rec" if is_default else ""}">'
        f"<code>{command}</code>"
        f'<button type="button" class="copy" data-copy="{command}">Kopieren</button>'
        f"{_DEFAULT_TAG if is_default else ''}"
        f"</li>"
    )


def _render_card(card: ExpectationCard) -> str:
    lines = "".join(_render_rule_line(card, outcome) for outcome in RULE_OUTCOMES)
    return f"""
    <article class="card">
      <h3>#{card.item} {html.escape(card.item_title)}</h3>
      <p>{_inline(card.text)}</p>
      <ul class="rule-lines">{lines}</ul>
    </article>"""


def _fact_row(label: str, value: str | None) -> str:
    if not value:
        return ""
    return f"<div><dt>{label}</dt><dd>{_inline(value)}</dd></div>"


def _render_lane(lane: LaneCard) -> str:
    facts = "".join(
        (
            f"<div><dt>Agent</dt><dd>{html.escape(lane.agent)} "
            f"({html.escape(lane.role)})</dd></div>",
            f"<div><dt>Branch</dt><dd><code>{html.escape(lane.branch)}</code></dd></div>",
            f"<div><dt>Alter</dt><dd>{html.escape(lane.age)}</dd></div>",
            _fact_row("Now", lane.now),
            _fact_row("Next", lane.next),
            _fact_row("Blocked by", lane.blocked_by),
            _fact_row("Done when", lane.done_when),
        )
    )
    return f"""
    <article class="lane">
      <h3>#{lane.item} {html.escape(lane.item_title)}</h3>
      <dl class="facts">{facts}</dl>
    </article>"""


def _part_label(part: TopicPart) -> str:
    title = f" {html.escape(part.title)}" if part.title else ""
    blocked = f" (blocked by {html.escape(part.blocked_by)})" if part.blocked_by else ""
    return f"#{part.number}{title}{blocked}"


def _render_part(part: TopicPart) -> str:
    return (
        f'<li class="{part.state.value}"><span class="dot" aria-hidden="true"></span>'
        f"<span>{_part_label(part)}</span>"
        f'<span class="p-state">{_PART_STATE_LABEL[part.state]}</span></li>'
    )


def _render_topic(topic: Topic) -> str:
    share = 0 if topic.total == 0 else round(100 * topic.closed / topic.total)
    parts = "".join(_render_part(part) for part in topic.parts)
    return f"""
      <li>
        <details>
          <summary>
            <span class="t-name"><strong>#{topic.item} {html.escape(topic.title)}</strong></span>
            <span class="t-progress">
              <span class="bar" role="img" aria-label="{topic.closed} of {topic.total} done">
                <i style="width:{share}%"></i>
              </span>
              <span class="t-count">{topic.closed}/{topic.total}</span>
            </span>
          </summary>
          <ul class="parts">{parts}</ul>
        </details>
      </li>"""


def _render_landed(entry: LandedItem) -> str:
    if entry.pull_request_number is None:
        reference = "PR nicht zugeordnet"
    else:
        title = html.escape(entry.pull_request_title or "")
        reference = f"PR #{entry.pull_request_number}: {title}"
    return f"<li>#{entry.item} {html.escape(entry.item_title)} &mdash; {reference}</li>"


def _render_landed_section(page: BoardPage) -> str:
    if not page.landings_derivable:
        return f'<p class="empty">{LANDINGS_NOT_DERIVABLE_TEXT}</p>'
    if not page.landed:
        return _EMPTY_PARAGRAPH
    return f'<ul class="landed">{"".join(_render_landed(entry) for entry in page.landed)}</ul>'


def render(page: BoardPage) -> str:
    facts = "".join(
        (
            f"<div><dt>repository</dt><dd><code>{html.escape(page.repository)}</code></dd></div>",
            f"<div><dt>state tip</dt><dd>"
            f"<code>{html.escape(page.state_tip or '-')}</code></dd></div>",
        )
    )
    return PAGE.format(
        facts=facts,
        card_count=len(page.cards),
        cards="".join(_render_card(card) for card in page.cards) or _EMPTY_PARAGRAPH,
        lane_count=len(page.lanes),
        lanes="".join(_render_lane(lane) for lane in page.lanes) or _EMPTY_PARAGRAPH,
        topics="".join(_render_topic(topic) for topic in page.topics) or _EMPTY_TOPICS,
        landed_count=len(page.landed),
        landed=_render_landed_section(page),
    )


LANDINGS_NOT_DERIVABLE_TEXT = "nicht ableitbar"
_EMPTY_PARAGRAPH = '<p class="empty">nichts</p>'
_EMPTY_TOPICS = '<li class="empty">nichts</li>'


PAGE = """<title>agent-claim Board</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,600;12..96,700&family=IBM+Plex+Mono:wght@400;500&family=IBM+Plex+Sans:wght@400;500;600&display=swap">
<style>
:root {{
  --ground: #EDF0F3; --surface: #FBFCFD; --sunk: #E2E7EC; --ink: #16202A; --muted: #566271;
  --rule: #D3DAE1; --accent: #2B59C3; --accent-soft: #DCE5F8;
  --done: #2F7D4F; --done-soft: #DCEFE3; --work: #A86A12; --work-soft: #F6E7CC;
  --you: #6E45B0; --you-soft: #EEE6F9; --open: #7A8694; --open-soft: #E6EAEE;
  --display: "Bricolage Grotesque", "Segoe UI", system-ui, sans-serif;
  --body: "IBM Plex Sans", system-ui, -apple-system, "Segoe UI", sans-serif;
  --mono: "IBM Plex Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
}}
@media (prefers-color-scheme: dark) {{
  :root:not([data-theme="light"]) {{
    --ground: #0F151B; --surface: #161E26; --sunk: #1E2832; --ink: #E4EAF0; --muted: #97A3B1;
    --rule: #2A3541; --accent: #88A8F3; --accent-soft: #1F2C47;
    --done: #62BF88; --done-soft: #17301F; --work: #E2AA4C; --work-soft: #3A2A10;
    --you: #BA9DEC; --you-soft: #2A2140; --open: #8491A0; --open-soft: #222C36;
  }}
}}
:root[data-theme="dark"] {{
  --ground: #0F151B; --surface: #161E26; --sunk: #1E2832; --ink: #E4EAF0; --muted: #97A3B1;
  --rule: #2A3541; --accent: #88A8F3; --accent-soft: #1F2C47;
  --done: #62BF88; --done-soft: #17301F; --work: #E2AA4C; --work-soft: #3A2A10;
  --you: #BA9DEC; --you-soft: #2A2140; --open: #8491A0; --open-soft: #222C36;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0; background: var(--ground); color: var(--ink); font: 15px/1.55 var(--body);
}}
.wrap {{
  max-width: 1120px; margin: 0 auto; padding-inline: clamp(16px, 4vw, 40px);
  padding-block: 28px 72px; display: grid; gap: 44px;
}}
code {{
  font: 0.86em/1.3 var(--mono); background: var(--sunk); padding: 0.08em 0.35em;
  border-radius: 4px; overflow-wrap: anywhere;
}}
h1, h2, h3 {{ font-family: var(--display); text-wrap: balance; margin: 0; }}
h2 {{
  font-size: 1.4rem; font-weight: 700; letter-spacing: -0.01em; display: flex;
  flex-wrap: wrap; align-items: baseline; gap: 4px 12px;
}}
h2 small {{ font: 500 0.82rem var(--body); color: var(--muted); letter-spacing: 0.01em; }}
section {{ display: grid; gap: 16px; }}
summary {{ cursor: pointer; }}
summary:focus-visible {{
  outline: 2px solid var(--accent); outline-offset: 3px; border-radius: 6px;
}}
.eyebrow {{
  margin: 0; font-size: 0.75rem; font-weight: 600; letter-spacing: 0.08em;
  text-transform: uppercase; color: var(--accent);
}}
.mast {{ display: grid; gap: 18px; padding-bottom: 24px; border-bottom: 1px solid var(--rule); }}
.mast h1 {{
  font-size: clamp(2.2rem, 5vw, 3.3rem); font-weight: 700; letter-spacing: -0.03em;
  line-height: 1;
}}
.mast-facts {{ display: flex; flex-wrap: wrap; gap: 14px 32px; margin: 0; }}
.mast-facts div {{ display: grid; gap: 2px; }}
.mast-facts dt {{
  font-size: 0.72rem; font-weight: 600; letter-spacing: 0.07em; text-transform: uppercase;
  color: var(--muted);
}}
.mast-facts dd {{ margin: 0; font: 500 1.1rem/1.2 var(--body); }}
.mast-facts dd code {{ font-size: 1rem; background: var(--accent-soft); color: var(--accent); }}
.empty {{ margin: 0; color: var(--muted); }}

.you h2 {{ color: var(--you); }}
.cards {{
  display: grid; grid-template-columns: repeat(auto-fit, minmax(min(100%, 330px), 1fr));
  gap: 14px;
}}
.card {{
  background: var(--surface); border: 1px solid color-mix(in srgb, var(--you) 40%, var(--rule));
  border-radius: 12px; padding: 18px 20px; display: grid; gap: 12px; align-content: start;
}}
.card h3 {{ font-size: 1.05rem; font-weight: 600; line-height: 1.3; }}
.rule-lines {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 8px; }}
.rule-lines li {{
  display: flex; flex-wrap: wrap; align-items: center; gap: 8px; padding: 8px 10px;
  border-radius: 8px; background: var(--sunk);
}}
.rule-lines li.rec {{ background: var(--you-soft); outline: 1.5px solid var(--you); }}
.rule-lines .tag {{
  margin-left: auto; font-size: 0.7rem; font-weight: 600; letter-spacing: 0.06em;
  text-transform: uppercase; color: var(--you);
}}
.copy {{
  font: 600 0.78rem var(--body); color: var(--you); background: var(--surface);
  border: 1.5px solid var(--you); border-radius: 999px; padding: 3px 12px; cursor: pointer;
}}
.copy:hover {{ background: var(--you); color: var(--surface); }}
.copy:focus-visible {{ outline: 2px solid var(--accent); outline-offset: 2px; }}
.copy.copied {{ background: var(--done); border-color: var(--done); color: var(--surface); }}

.topics {{ list-style: none; margin: 0; padding: 0; display: grid; }}
.topics > li {{ border-top: 1px solid var(--rule); }}
.topics > li:last-child {{ border-bottom: 1px solid var(--rule); }}
.topics summary {{
  list-style: none; display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 16rem);
  gap: 8px 28px; align-items: center; padding: 13px 0;
}}
.topics summary::-webkit-details-marker {{ display: none; }}
.topics summary:hover .t-name strong {{ color: var(--accent); }}
.t-name strong::after {{ content: " +"; color: var(--muted); font-weight: 400; }}
.topics details[open] .t-name strong::after {{ content: " \\2212"; }}
.t-progress {{
  display: grid; grid-template-columns: minmax(0, 1fr) 3.4em; gap: 12px; align-items: center;
}}
.bar {{
  display: block; height: 8px; border-radius: 4px; background: var(--sunk); overflow: hidden;
}}
.bar i {{ display: block; height: 100%; background: var(--accent); border-radius: 4px; }}
.t-count {{ font: 500 0.85rem var(--mono); text-align: right; }}
.parts {{
  list-style: none; margin: 0 0 16px; padding: 12px 16px; display: grid; gap: 7px;
  background: var(--surface); border-radius: 10px;
}}
.parts li {{
  display: grid; grid-template-columns: 10px minmax(0, 1fr) auto; gap: 12px;
  align-items: baseline; font-size: 0.92rem;
}}
.dot {{
  width: 10px; height: 10px; border-radius: 50%; background: var(--open-soft);
  border: 2px solid var(--open); align-self: center;
}}
.parts .running .dot {{ background: var(--work); border-color: var(--work); }}
.parts .you .dot {{ background: var(--you); border-color: var(--you); }}
.p-state {{
  font-size: 0.74rem; font-weight: 600; letter-spacing: 0.03em; color: var(--muted);
  white-space: nowrap;
}}
.parts .running .p-state {{ color: var(--work); }}
.parts .you .p-state {{ color: var(--you); }}

.lanes {{ display: grid; gap: 12px; }}
.lane {{
  background: var(--surface); border: 1px solid var(--rule); border-radius: 12px;
  padding: 18px 20px; display: grid; gap: 10px;
}}
.lane h3 {{ font-size: 1.05rem; font-weight: 600; line-height: 1.3; }}
.facts {{ margin: 0; display: grid; gap: 8px; }}
.facts div {{ display: grid; grid-template-columns: 7.5em minmax(0, 1fr); gap: 12px; }}
.facts dt {{
  font-size: 0.7rem; font-weight: 600; letter-spacing: 0.07em; text-transform: uppercase;
  color: var(--muted); padding-top: 0.25em;
}}
.facts dd {{ margin: 0; }}

.landed {{ list-style: none; margin: 0; padding: 0; display: grid; gap: 6px; }}
.landed li {{ padding: 8px 0; border-top: 1px solid var(--rule); font-size: 0.92rem; }}
.landed li:last-child {{ border-bottom: 1px solid var(--rule); }}

@media (max-width: 560px) {{
  .facts div {{ grid-template-columns: minmax(0, 1fr); gap: 0; }}
  .parts li {{ grid-template-columns: 10px minmax(0, 1fr); }}
  .p-state {{ grid-column: 2; }}
  .topics summary {{ grid-template-columns: minmax(0, 1fr); }}
}}
</style>
<main class="wrap">
  <header class="mast">
    <p class="eyebrow">agent-claim &middot; the aco coordination tool</p>
    <h1>Board</h1>
    <dl class="mast-facts">{facts}</dl>
  </header>

  <section class="you" aria-labelledby="you">
    <h2 id="you">Wartet auf dich <small>{card_count}</small></h2>
    <div class="cards">{cards}</div>
  </section>

  <section aria-labelledby="lanes">
    <h2 id="lanes">Lanes <small>{lane_count}</small></h2>
    <div class="lanes">{lanes}</div>
  </section>

  <section aria-labelledby="topics">
    <h2 id="topics">Themen</h2>
    <ul class="topics">{topics}</ul>
  </section>

  <section aria-labelledby="landed">
    <h2 id="landed">Landungen <small>{landed_count}</small></h2>
    {landed}
  </section>
</main>
<script>
document.querySelectorAll("[data-copy]").forEach((button) => {{
  button.addEventListener("click", () => {{
    navigator.clipboard.writeText(button.dataset.copy || "");
    button.classList.add("copied");
  }});
}});
</script>
"""
