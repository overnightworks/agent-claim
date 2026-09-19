"""Behavioral tests for `board_html.py` (#276): a pure renderer over an
already-projected `board.Board`. `tests/test_cli.py` covers `board --html`'s
own wiring (writing stdout/a path, the exact reads it performs); this module
covers what the rendered page says."""

from __future__ import annotations

import re
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from board_fixtures import (
    _active_claim,
    block_dependency,
    board_issue,
    complete_contract,
    projected_board,
    proposed_expectation,
)

from agent_coordination import board, board_html, items

GOLDEN_PATH = Path(__file__).parent / "board_html_golden.html"


def _fixture_page(
    *, storage: board.Storage = board.Storage.GITHUB, lane_blocked: bool = False
) -> board_html.BoardPage:
    """A container (#100) with two children -- one closed, one open and
    blocked (#101, blocked by #50) -- one open `[[expectation]]` line on
    #101, one active claim on a standalone item (#102, optionally also
    blocked by #60 when `lane_blocked` -- issue #300 residual: proves the
    lane card, not just the topic part, carries `open_blocker_label`), one
    landing #103 is closed by (merged pull request #555), and one landing
    (#104) whose merged pull request (#556) only board.py's own private
    "lands" convention resolves -- `_closing_pull_request`'s honest
    residual. `lane_blocked` defaults to `False` so the GitHub golden page
    stays untouched; only the state-ref proof below turns it on."""
    child_dependency = block_dependency(50)
    open_child = board_issue(
        101,
        "Zugang klären",
        complete_contract(
            "Zugang beantragen.",
            now="Warten auf Rueckmeldung.",
            done_when="Zugang erteilt.",
            expectation=[proposed_expectation("Brauchen wir Admin-Rechte?", default="yes")],
        ),
        blocked_by_count=1,
    )
    container_issue = replace(
        board_issue(100, "Sammelitem", complete_contract("Kinder abarbeiten.")),
        kind=board.ItemKind.CONTAINER,
        children_closed=1,
        children_total=2,
    )
    claimed_item = board_issue(
        102,
        "Laufende Lane",
        complete_contract("Fertigstellen.", now="Am Bauen.", done_when="Gemergt."),
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
    # (not just close/fix/resolve), so this merged pull request sets #104's
    # stage `CODE_LANDED` -- but `_closing_pull_request` only reads the two
    # public conventions (a named residual), so #104 shows with no resolved
    # pull request.
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
        recent_merged_pull_requests=recent_merged_pull_requests,
        state_tip="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        storage=storage,
    )
    return board_html.build_page(projected, sources)


def _empty_page(*, landings_derivable: bool = True) -> board_html.BoardPage:
    return board_html.BoardPage(
        repository="acme/board",
        state_tip="",
        cards=(),
        lanes=(),
        topics=(),
        landed=(),
        landings_derivable=landings_derivable,
    )


def test_render_matches_the_golden_page_byte_for_byte() -> None:
    rendered = board_html.render(_fixture_page())
    assert rendered == GOLDEN_PATH.read_text(encoding="utf-8")


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
    rendered = board_html.render(_fixture_page(storage=board.Storage.STATE_REF, lane_blocked=True))
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


def test_landings_not_derivable_shows_the_one_line_instead_of_items() -> None:
    rendered = board_html.render(_empty_page(landings_derivable=False))
    assert "nicht ableitbar" in rendered
    # Landungen alone carries the "nicht ableitbar" line; the other three
    # empty sections still say "nichts".
    assert rendered.count("nichts") == 3


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


def test_a_landed_item_with_no_resolved_pull_request_still_shows() -> None:
    page = board_html.BoardPage(
        repository="acme/board",
        state_tip="",
        cards=(),
        lanes=(),
        topics=(),
        landed=(
            board_html.LandedItem(
                item=9,
                item_title="Unklar gemerged",
                pull_request_number=None,
                pull_request_title=None,
            ),
        ),
        landings_derivable=True,
    )
    rendered = board_html.render(page)
    assert "#9 Unklar gemerged" in rendered
    assert "PR nicht zugeordnet" in rendered


def test_a_trailer_landed_item_shows_with_its_state_ref_id_when_prs_are_unlistable() -> None:
    """Issue #304 review delta (Codex Sol): state-ref's own
    `landings_derivable=False` means it cannot list merged pull requests at
    all, but a trunk commit's own `Work-Item:` trailer still lands an item
    straight from local git history (`board.trunk_landed_work_items`) --
    that row shows with its `aco-...` id, date, and short sha instead of the
    "nicht ableitbar" line the missing pull-request capability alone would
    otherwise force."""
    landed_issue = board_issue(9, "Trailer gelandet", complete_contract("Verifizieren."))
    projected = board.build_board(
        board.BoardBuildInputs(
            issues=(landed_issue,),
            open_pull_requests=(),
            recent_merged_pull_requests=(),
            claims=(),
            config=board.BoardConfig(),
            repository="example/agent-claim",
            now=datetime(2026, 8, 30, tzinfo=UTC),
            trunk_landed_work_items=frozenset({9}),
            landings_derivable=False,
        )
    )
    sources = board_html.BoardSources(
        bodies={9: landed_issue.body},
        claimants={},
        recent_merged_pull_requests=(),
        state_tip="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        storage=board.Storage.STATE_REF,
        trunk_landed_items=(
            board_html.TrunkLandedItem(9, "cafefeedcafefeed", datetime(2026, 8, 30, tzinfo=UTC)),
        ),
    )

    rendered = board_html.render(board_html.build_page(projected, sources))

    item_id = items.format_item_id(9)
    assert "nicht ableitbar" not in rendered
    assert (
        f"<li>{item_id} Trailer gelandet &mdash; 2026-08-30 <code>cafefee</code></li>" in rendered
    )


def test_a_trunk_landed_item_with_no_pull_request_shows_beside_pr_rows() -> None:
    """A github-storage board still lands an item through a squash/merge
    commit's own `Work-Item:` trailer with no matching pull request body
    (`board.py`'s union, issue #304) -- that row renders with its date and
    short sha alongside a normally PR-resolved landing, never "PR nicht
    zugeordnet"."""
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
            trunk_landed_work_items=frozenset({105}),
        )
    )
    sources = board_html.BoardSources(
        bodies={103: pr_landed_issue.body, 105: trunk_landed_issue.body},
        claimants={},
        recent_merged_pull_requests=recent_merged_pull_requests,
        state_tip="deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        trunk_landed_items=(
            board_html.TrunkLandedItem(105, "1234567890abcdef", datetime(2026, 8, 20, tzinfo=UTC)),
        ),
    )

    rendered = board_html.render(board_html.build_page(projected, sources))

    assert "PR #555: Kleine Verbesserung landen" in rendered
    assert "<li>#105 Nur Trailer &mdash; 2026-08-20 <code>1234567</code></li>" in rendered


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
