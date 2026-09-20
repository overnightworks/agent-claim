"""Behavioral tests for `board_html.py` (#276): a pure renderer over an
already-projected `board.Board`. `tests/test_cli.py` covers `board --html`'s
own wiring (writing stdout/a path, the exact reads it performs); this module
covers what the rendered page says."""

from __future__ import annotations

import re
from dataclasses import fields, replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from board_fixtures import (
    _active_claim,
    block_dependency,
    board_issue,
    complete_contract,
    projected_board,
    proposed_expectation,
    ruled_expectation,
)

from agent_coordination import board, board_html, cli, items, metrics
from agent_coordination.body import ItemKind, Storage

GOLDEN_PATH = Path(__file__).parent / "board_html_golden.html"


def _fixture_page(
    *,
    storage: Storage = Storage.GITHUB,
    lane_blocked: bool = False,
    ruled_claimed_item_line: bool = False,
    ruled_child_line: bool = False,
) -> board_html.BoardPage:
    """A container (#100) with two children -- one closed, one open and
    blocked (#101, blocked by #50) -- one open `[[expectation]]` line on
    #101, one active claim on a standalone item (#102, optionally also
    blocked by #60 when `lane_blocked` -- issue #300 residual: proves the
    lane card, not just the topic part, carries `open_blocker_label`), one
    landing #103 closed by merged pull request #555, and one landing (#104)
    only board.py's own private "lands" convention resolves (pull request
    #556, issue #371: the Landungen view reuses that same wide matching, so
    both rows resolve to their own pull request, neither "PR nicht
    zugeordnet"). `lane_blocked` defaults to `False` so the GitHub golden page
    stays untouched; only the state-ref proof below turns it on.
    `ruled_claimed_item_line` (issue #388) defaults to `False` for the same
    reason: it adds one already-ruled `[[expectation]]` line to #102's own
    contract, moving its confirmation into #102's Topic history instead of
    a "Wartet auf dich" card -- only the ruled golden page below turns it
    on. `ruled_child_line` (issue #388, review finding on
    board_html.py:313) defaults to `False` for the same reason: it adds a
    second, already-ruled `[[expectation]]` line to container child #101's
    own contract, alongside its existing open one, moving its confirmation
    into #101's own `TopicPart` history nested inside container #100's
    topic -- a container child is never a `Topic` of its own, so this is
    the one other place a ruled line can render instead of disappearing.
    The
    container (#100) also carries a top-level `size = "M"` (issue #357),
    measured into a real (non-weak) estimate by its own one completed lane
    plus two more completed `M` lanes on items this fixture never lists at
    all (#997, #998) -- their size read back through `closed_item_sizes`
    (R2's closed-item join) rather than from `container_issue`'s own body,
    proving a class is never blind to an item that has since closed; a
    fourth, still-open lane on another unlisted item (#999) shows in the
    Messungen section as "1 Lanes ohne Ende" without ever touching a size
    class."""
    lane_events = (
        metrics.LaneEvent(
            item="100",
            size=None,
            container=None,
            claimed_at=datetime(2026, 8, 14, tzinfo=UTC),
            released_at=datetime(2026, 8, 14, 5, tzinfo=UTC),
            landed_at=None,
            rescopes=1,
        ),
        metrics.LaneEvent(
            item="997",
            size=None,
            container=None,
            claimed_at=datetime(2026, 8, 10, tzinfo=UTC),
            released_at=datetime(2026, 8, 10, 4, tzinfo=UTC),
            landed_at=None,
            rescopes=0,
        ),
        metrics.LaneEvent(
            item="998",
            size=None,
            container=None,
            claimed_at=datetime(2026, 8, 12, tzinfo=UTC),
            released_at=datetime(2026, 8, 12, 6, tzinfo=UTC),
            landed_at=None,
            rescopes=0,
        ),
        metrics.LaneEvent(
            item="999",
            size=None,
            container=None,
            claimed_at=datetime(2026, 8, 20, tzinfo=UTC),
            released_at=None,
            landed_at=None,
            rescopes=0,
        ),
    )
    child_dependency = block_dependency(50)
    open_child_expectation = [proposed_expectation("Brauchen wir Admin-Rechte?", default="yes")]
    if ruled_child_line:
        open_child_expectation.append(
            ruled_expectation("Ist das Onboarding-Ticket schon offen? Anmerkung: Ja, erledigt.")
        )
    open_child = board_issue(
        101,
        "Zugang klären",
        complete_contract(
            "Zugang beantragen.",
            now="Warten auf Rueckmeldung.",
            done_when="Zugang erteilt.",
            expectation=open_child_expectation,
        ),
        blocked_by_count=1,
    )
    container_issue = replace(
        board_issue(100, "Sammelitem", complete_contract("Kinder abarbeiten.", size="M")),
        kind=ItemKind.CONTAINER,
        children_closed=1,
        children_total=2,
    )
    claimed_item_expectation = (
        [ruled_expectation("Ist das Feature-Flag schon aktiv? Anmerkung: Ja, seit Montag.")]
        if ruled_claimed_item_line
        else []
    )
    claimed_item = board_issue(
        102,
        "Laufende Lane",
        complete_contract(
            "Fertigstellen.",
            now="Am Bauen.",
            done_when="Gemergt.",
            expectation=claimed_item_expectation,
        ),
        blocked_by_count=1 if lane_blocked else 0,
    )
    landed_item = board_issue(103, "Kleine Verbesserung", complete_contract("Verifizieren."))
    unresolved_landed_item = board_issue(104, "Randfall", complete_contract("Beobachten."))
    claim = _active_claim(
        agent="Codex Sol", role="builder", issue=102, branch="codex/issue-102-claims"
    )
    closing_pull_request = board.PullRequest(
        number=555,
        title="Kleine Verbesserung landen",
        body="Fixes #103.",
        head_ref_name="codex/issue-103-fix",
        merged_at="2026-08-19T00:00:00Z",
    )
    # `board.py`'s own private `LANDING_CLAIM_PATTERN` also credits "lands"
    # (not just close/fix/resolve), so this merged pull request both sets
    # #104's stage `CODE_LANDED` and resolves its own Landungen row (see
    # this fixture's own docstring above).
    landing_only_pull_request = board.PullRequest(
        number=556,
        title="Lands #104: Randfall",
        body="",
        head_ref_name="codex/issue-104-note",
        merged_at="2026-08-19T00:00:00Z",
    )
    recent_merged_pull_requests = (closing_pull_request, landing_only_pull_request)
    dependencies = {101: (child_dependency,)}
    if lane_blocked:
        dependencies[102] = (block_dependency(60),)
    projected = projected_board(
        (container_issue, open_child, claimed_item, landed_item, unresolved_landed_item),
        open_pull_requests=(),
        recent_merged_pull_requests=recent_merged_pull_requests,
        claims=(claim,),
        config=board.BoardConfig(),
        children={100: (board.ChildItem(101, board.ChildState.OPEN),)},
        dependencies=dependencies,
        now=datetime(2026, 8, 21, tzinfo=UTC),
        lane_events=lane_events,
        closed_item_sizes={997: metrics.Size.MEDIUM, 998: metrics.Size.MEDIUM},
    )
    bodies = {
        issue.number: issue.body
        for issue in (
            container_issue,
            open_child,
            claimed_item,
            landed_item,
            unresolved_landed_item,
        )
    }
    sources = board_html.BoardSources(
        bodies=bodies,
        claimants={102: board_html.LaneClaimant("Codex Sol", "builder", "codex/issue-102-claims")},
        state_tip="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        storage=storage,
    )
    return board_html.build_page(projected, sources)


def _empty_measurements() -> board.Measurements:
    return board.Measurements(
        classes=(), unfinished=0, unparsed=0, since=None, as_of=date(2026, 8, 21)
    )


def _empty_page() -> board_html.BoardPage:
    return board_html.BoardPage(
        repository="acme/board",
        state_tip="",
        cards=(),
        lanes=(),
        topics=(),
        landed=(),
        measurements=_empty_measurements(),
    )


def test_render_matches_the_golden_page_byte_for_byte() -> None:
    rendered = board_html.render(_fixture_page())
    assert rendered == GOLDEN_PATH.read_text(encoding="utf-8")


RULED_GOLDEN_PATH = Path(__file__).parent / "board_html_golden_ruled.html"


def test_render_matches_the_golden_ruled_page_byte_for_byte() -> None:
    """Issue #388 proof 2: an already-ruled `[[expectation]]` line moves out
    of "Wartet auf dich" into its own item's Topic history -- ruling, date,
    text, and the `aco ask` hint, never a form or a button -- and every
    other section of the page stays exactly as `GOLDEN_PATH` renders it.
    Review finding on board_html.py:313: the same is true one level down,
    for a container *child*'s own ruled line (`ruled_child_line`) -- its
    history nests inside container #100's own topic instead of vanishing."""
    rendered = board_html.render(_fixture_page(ruled_claimed_item_line=True, ruled_child_line=True))
    assert rendered == RULED_GOLDEN_PATH.read_text(encoding="utf-8")


def test_render_labels_cards_topics_and_lanes_with_state_ref_ids() -> None:
    """Issue #292 proof 3: under `storage = "state-ref"`, `board --html`
    shows `aco-xxxxxx` -- never `#n` -- in every topic, lane, card heading
    (a card's `item-tag` carries the item label since issue #295), a
    blocked topic part's own `blocked by` reference, and a blocked lane
    card's own `Blocked by` fact under its pin (issue #300, Codex delta:
    `BoardSources.storage` reaches `open_blocker_label` for `_lane_card`
    too, not just the topic parts around it); the `github` golden page
    above stays byte-identical, so only this storage's own rendering
    differs."""
    rendered = board_html.render(_fixture_page(storage=Storage.STATE_REF, lane_blocked=True))
    open_child_id = items.format_item_id(101)  # the card's topic part
    container_id = items.format_item_id(100)  # the container topic
    claimed_item_id = items.format_item_id(102)  # the lane
    blocker_id = items.format_item_id(50)  # #101's own blocker
    lane_blocker_id = items.format_item_id(60)  # the lane's own blocker

    assert f"<strong>{container_id} Sammelitem</strong>" in rendered
    assert f"<span>{open_child_id} Zugang klären (blocked by {blocker_id})</span>" in rendered
    lane_html = re.search(r'<article class="lane">.*?</article>', rendered, re.DOTALL)
    assert lane_html is not None
    assert f"<h3>{claimed_item_id} Laufende Lane</h3>" in lane_html.group()
    assert f"<dt>Blocked by</dt><dd>{lane_blocker_id}</dd>" in lane_html.group()
    assert f'<span class="item-tag">{open_child_id} Zugang klären</span>' in rendered
    for number in (50, 60, 100, 101, 102):
        assert f">#{number} " not in rendered
        assert f"#{number})" not in rendered


def test_an_empty_board_renders_all_four_headings_with_nichts() -> None:
    rendered = board_html.render(_empty_page())
    for heading in ("Wartet auf dich", "Lanes", "Themen", "Landungen"):
        assert heading in rendered
    assert rendered.count("nichts") == 4


def test_a_card_carries_the_exact_copyable_rule_command_per_outcome() -> None:
    rendered = board_html.render(_fixture_page())
    assert "<code>aco rule 101 --line 1 --yes</code>" in rendered
    assert 'data-copy="aco rule 101 --line 1 --yes"' in rendered
    assert "<code>aco rule 101 --line 1 --no</code>" in rendered
    assert "<code>aco rule 101 --line 1 --later</code>" in rendered


CARD_SVG = '<svg viewBox="0 0 10 10"><circle cx="5" cy="5" r="4"/></svg>'


def test_a_card_with_question_example_and_picture_shows_them_and_the_full_sentence() -> None:
    """Issue #295 proof 3 (card with every optional field)."""
    page = replace(
        _empty_page(),
        cards=(
            board_html.ExpectationCard(
                item=7,
                item_title="Import vorbereiten",
                index=2,
                text="Brauchen wir für den Import Admin-Rechte auf dem Zielsystem?",
                default="yes",
                question="Admin-Rechte nötig?",
                example="Wie beim letzten Import, wo wir sudo brauchten.",
                picture=CARD_SVG,
            ),
        ),
    )
    rendered = board_html.render(page)
    assert '<span class="item-tag">#7 Import vorbereiten</span>' in rendered
    assert "<h3>Admin-Rechte nötig?</h3>" in rendered
    assert f"<figure>{CARD_SVG}</figure>" in rendered
    assert '<span class="tag">Beispiel</span> Wie beim letzten Import' in rendered
    assert "<code>aco rule 7 --line 2 --yes</code>" in rendered
    assert "<code>aco rule 7 --line 2 --no</code>" in rendered
    assert "<code>aco rule 7 --line 2 --later</code>" in rendered
    assert (
        "<details><summary>Der volle Satz</summary>"
        "<p>Brauchen wir für den Import Admin-Rechte auf dem Zielsystem?</p></details>"
    ) in rendered


def test_a_card_without_the_new_fields_shows_text_as_heading_with_no_figure_or_example() -> None:
    """Issue #295 proof 3 (card with no optional field, unchanged from before)."""
    page = replace(
        _empty_page(),
        cards=(
            board_html.ExpectationCard(
                item=7, item_title="Import vorbereiten", index=2, text="Frage?", default="yes"
            ),
        ),
    )
    rendered = board_html.render(page)
    assert '<span class="item-tag">#7 Import vorbereiten</span>' in rendered
    assert "<h3>Frage?</h3>" in rendered
    assert "<figure>" not in rendered
    assert "Beispiel" not in rendered
    assert "Der volle Satz" not in rendered


def test_css_never_sets_a_min_width_above_400px() -> None:
    rendered = board_html.render(_fixture_page())
    widths = [int(value) for value in re.findall(r"min-width:\s*(\d+)px", rendered)]
    assert all(width <= 400 for width in widths)


def test_a_landed_item_shows_its_pull_request_evidence() -> None:
    evidence = board.PullRequestLandingEvidence(41)
    page = replace(
        _empty_page(),
        landed=(board.LandingRow(9, datetime(2026, 8, 19, tzinfo=UTC), evidence),),
    )
    rendered = board_html.render(page)
    assert "<li>#9 2026-08-19 PR #41</li>" in rendered


def test_a_trailer_landed_item_shows_regardless_of_pull_request_capability() -> None:
    """Issue #371: a trunk commit's own `Work-Item:` trailer lands an item
    straight from local git history, independent of whether `github`
    storage's own pull-request supplement even applies -- that row shows
    with its `aco-...` id, date, and short sha, under `storage = state-ref`,
    which never lists a pull request at all."""
    landed_issue = board_issue(9, "Trailer gelandet", complete_contract("Verifizieren."))
    projected = board.build_board(
        board.BoardBuildInputs(
            issues=(landed_issue,),
            open_pull_requests=(),
            recent_merged_pull_requests=(),
            claims=(),
            config=board.BoardConfig(storage=Storage.STATE_REF),
            repository="example/agent-claim",
            now=datetime(2026, 8, 30, tzinfo=UTC),
            trunk_landing_items=(
                board.TrunkLandingItem(9, "cafefeedcafefeed", datetime(2026, 8, 30, tzinfo=UTC)),
            ),
        )
    )
    sources = board_html.BoardSources(
        bodies={9: landed_issue.body},
        claimants={},
        state_tip="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        storage=Storage.STATE_REF,
    )

    rendered = board_html.render(board_html.build_page(projected, sources))

    item_id = items.format_item_id(9)
    assert f"<li>{item_id} 2026-08-30 <code>cafefee</code></li>" in rendered


def test_a_trunk_landed_item_with_no_pull_request_shows_beside_pr_rows() -> None:
    """A github-storage board still lands an item through a squash/merge
    commit's own `Work-Item:` trailer with no matching pull request body
    (`board.py`'s union, issue #304) -- that row renders with its date and
    short sha alongside a normally PR-resolved landing, issue #371's own
    dedup keeping each item to exactly one row."""
    pr_landed_issue = board_issue(103, "Kleine Verbesserung", complete_contract("Verifizieren."))
    trunk_landed_issue = board_issue(105, "Nur Trailer", complete_contract("Beobachten."))
    closing_pull_request = board.PullRequest(
        number=555,
        title="Kleine Verbesserung landen",
        body="Fixes #103.",
        head_ref_name="codex/issue-103-fix",
        merged_at="2026-08-19T00:00:00Z",
    )
    recent_merged_pull_requests = (closing_pull_request,)
    projected = board.build_board(
        board.BoardBuildInputs(
            issues=(pr_landed_issue, trunk_landed_issue),
            open_pull_requests=(),
            recent_merged_pull_requests=recent_merged_pull_requests,
            claims=(),
            config=board.BoardConfig(),
            repository="example/agent-claim",
            now=datetime(2026, 8, 21, tzinfo=UTC),
            trunk_landing_items=(
                board.TrunkLandingItem(105, "1234567890abcdef", datetime(2026, 8, 20, tzinfo=UTC)),
            ),
        )
    )
    sources = board_html.BoardSources(
        bodies={103: pr_landed_issue.body, 105: trunk_landed_issue.body},
        claimants={},
        state_tip="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
    )

    rendered = board_html.render(board_html.build_page(projected, sources))

    assert "<li>#103 2026-08-19 PR #555</li>" in rendered
    assert "<li>#105 2026-08-20 <code>1234567</code></li>" in rendered


@pytest.mark.parametrize(
    ("default", "recommended_flag"),
    [("yes", "--yes"), ("no", "--no"), ("later", "--later")],
)
def test_the_default_outcome_is_marked_recommended(default: str, recommended_flag: str) -> None:
    page = replace(
        _empty_page(),
        cards=(
            board_html.ExpectationCard(
                item=7, item_title="Import vorbereiten", index=2, text="Frage?", default=default
            ),
        ),
    )
    rendered = board_html.render(page)
    rule_lines = re.findall(r'<li class="([^"]*)">(.*?)</li>', rendered)
    for css_class, body in rule_lines:
        if css_class == "rec":
            assert f"aco rule 7 --line 2 {recommended_flag}" in body
            assert "Vorgabe" in body
        else:
            assert "Vorgabe" not in body


def test_landings_derivable_and_its_not_derivable_line_are_fully_retired() -> None:
    """Issue #371, Beweis 3 (review finding R3): the trunk walk is always
    present, so the capability flag, its "nicht ableitbar" line, and every
    name the old per-caller Landungen assembly needed are gone from every
    module and every JSON/HTML shape that used to carry them -- a structural
    stand-in for a grep, since a stray reintroduction would otherwise slip
    back in silently."""
    assert not hasattr(board, "LANDINGS_NOT_DERIVABLE_LINE")
    assert not hasattr(board_html, "LANDINGS_NOT_DERIVABLE_TEXT")
    assert not hasattr(board_html, "TrunkLandedItem")
    assert not hasattr(board_html, "LandedItem")
    assert not hasattr(board_html, "_closing_pull_request")
    assert not hasattr(board_html, "_landed_items")
    assert not hasattr(cli, "_BoardFetch")
    assert "landings_derivable" not in {field.name for field in fields(board.Board)}
    assert "landings_derivable" not in {field.name for field in fields(board.BoardBuildInputs)}
    assert "landings_derivable" not in {field.name for field in fields(board_html.BoardPage)}
    board_sources_fields = {field.name for field in fields(board_html.BoardSources)}
    assert "recent_merged_pull_requests" not in board_sources_fields
    assert "trunk_landed_items" not in board_sources_fields
