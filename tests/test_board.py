"""Behavioral tests for the pure `agent-claim` block write path `board.py`
owns (#240): `rule_expectation`, `append_expectation`, and the
`expectation_lines` projection `rule --line`, `ask`, and `rulings` all
share. CLI wiring for `rule`/`ask`/`rulings` is covered in `tests/test_cli.py`."""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from board_fixtures import (
    FROZEN_TRIGGER,
    FROZEN_UNTIL,
    MINIMAL_BLOCK_TOML,
    REPOSITORY,
    _store_claim_from_request,
    agent_claim_body,
    block_dependency,
    blocked_issue,
    board_issue,
    complete_contract,
    idea_body,
    projected_board,
    proposed_expectation,
    request,
    ruled_expectation,
    slice_entries,
)

from agent_coordination import board, protocol
from agent_coordination.protocol import ClaimError, ClaimRequest

# A real expectation sentence from this repository's own issue #230 (#240's
# brief: take a real body as the fixture template rather than a synthetic
# one) -- German prose, an em dash, and multiple sentences, none of which
# need TOML escaping. The block interior itself is rendered through
# `board.render_block`, the production serializer, never hand-typed.
ISSUE_230_EXPECTATION_TEXT = (
    "Ein gezogenes Forge-Issue wird von aco nie verändert, geschlossen oder "
    "umgehängt; nur ein Marker-Kommentar, wenn der Spiegel eingeschaltet ist. "
    "Beispiel: aco pull github#123, dann eine Woche Lane-Arbeit — das Issue "
    "auf GitHub sieht aus wie vorher, bis der PR es schließt. Gegenbeispiel: "
    "aco schreibt Now/Next in den GitHub-Body eines fremden Issues. Berührt "
    "Rechte anderer."
)
ISSUE_230_PROSE = (
    "Caller: Felix, Ruling 15.09.2026 („aco ist gerade auf github, aber warum "
    "soll es nicht auch einfach wie markdown gehen?“). Neighbours: #238, #237.\n\n"
    "Blocked by: #231, #241\n\n"
)


def issue_230_body(*, default: str = "later") -> str:
    """A realistic excerpt of issue #230's own body: its real prose and
    `Blocked by` line, then a block carrying one still-*proposed* line built
    from its real (later-ruled) expectation text -- `default` lets a test
    ask for a body that is already fully ruled instead."""
    interior = board.render_block(
        {
            "version": 1,
            "now": "Konzept v3 (15.09.2026) nach Plan-Review und Regel-Gegen-Check.",
            "next": "Scheibe 1 ist #240, wartet auf #238.",
            "done_when": "Ein Repository ohne GitHub-Remote läuft vollständig aus refs/aco/state.",
            "expectation": [{"text": ISSUE_230_EXPECTATION_TEXT, "default": default}],
        }
    ).rstrip("\n")
    return f"{ISSUE_230_PROSE}```agent-claim\n{interior}\n```\n\nProse after.\n"


# --- rule_expectation ---


@pytest.mark.parametrize("ruling", ["yes", "no", "later"])
def test_rule_expectation_rules_a_proposed_line(ruling: str) -> None:
    body = agent_claim_body(
        f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Ship it?"\ndefault = "later"\n'
    )

    new_body = board.rule_expectation(body, 1, ruling, date(2026, 9, 15))

    assert board.expectation_lines(new_body) == (
        board.ExpectationLine(1, "Ship it?", ruling, date(2026, 9, 15)),
    )
    assert board.parse_body(new_body).expectation_state is board.ExpectationState.RULED


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_rule_expectation_preserves_every_byte_outside_the_ruled_line(newline: str) -> None:
    body = issue_230_body().replace("\n", newline)
    located = board.locate_agent_claim_block(body)
    prefix, suffix = body[: located.content_start], body[located.content_end :]

    new_body = board.rule_expectation(body, 1, "yes", date(2026, 9, 15))

    assert new_body.startswith(prefix)
    assert new_body.endswith(suffix)
    assert board.expectation_lines(new_body) == (
        board.ExpectationLine(1, ISSUE_230_EXPECTATION_TEXT, "yes", date(2026, 9, 15)),
    )


def test_rule_expectation_refuses_an_already_ruled_line() -> None:
    body = agent_claim_body(
        f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Ship it?"\n'
        'ruling = "yes"\nruled_on = 2026-09-01\n'
    )

    ruled_at = date(2026, 9, 15)

    with pytest.raises(protocol.ClaimError, match="line 1 is already ruled"):
        board.rule_expectation(body, 1, "no", ruled_at)


@pytest.mark.parametrize("index", [0, 2])
def test_rule_expectation_refuses_an_out_of_range_line(index: int) -> None:
    body = agent_claim_body(
        f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Ship it?"\ndefault = "later"\n'
    )

    ruled_at = date(2026, 9, 15)

    with pytest.raises(protocol.ClaimError, match="out of range"):
        board.rule_expectation(body, index, "yes", ruled_at)


def test_rule_expectation_refuses_an_unknown_ruling_value() -> None:
    body = agent_claim_body(
        f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Ship it?"\ndefault = "later"\n'
    )

    ruled_at = date(2026, 9, 15)

    with pytest.raises(protocol.ClaimError, match="ruling must be"):
        board.rule_expectation(body, 1, "maybe", ruled_at)


def test_rule_expectation_appends_a_note_to_the_ruled_line_text() -> None:
    body = agent_claim_body(
        f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "Ship it?"\ndefault = "later"\n'
    )

    new_body = board.rule_expectation(body, 1, "yes", date(2026, 9, 15), note="Ja, sofort.")

    assert board.expectation_lines(new_body)[0].text == "Ship it? Anmerkung: Ja, sofort."


# --- append_expectation ---


@pytest.mark.parametrize("default", ["yes", "no", "later"])
def test_append_expectation_adds_a_proposed_line(default: str) -> None:
    body = agent_claim_body(MINIMAL_BLOCK_TOML)

    new_body = board.append_expectation(body, "New question?", default)

    assert board.expectation_lines(new_body) == (
        board.ExpectationLine(1, "New question?", None, None),
    )
    entries = board.locate_agent_claim_block(new_body).data["expectation"]
    assert entries == [{"text": "New question?", "default": default}]
    assert board.parse_body(new_body).expectation_state is board.ExpectationState.PROPOSED


def test_append_expectation_appends_after_existing_lines() -> None:
    body = agent_claim_body(
        f'{MINIMAL_BLOCK_TOML}[[expectation]]\ntext = "First"\ndefault = "yes"\n'
    )

    new_body = board.append_expectation(body, "Second", "no")

    assert board.expectation_lines(new_body) == (
        board.ExpectationLine(1, "First", None, None),
        board.ExpectationLine(2, "Second", None, None),
    )


@pytest.mark.parametrize("newline", ["\n", "\r\n"], ids=["lf", "crlf"])
def test_append_expectation_preserves_every_byte_outside_the_appended_line(newline: str) -> None:
    body = issue_230_body(default="yes").replace("\n", newline)
    located = board.locate_agent_claim_block(body)
    prefix, suffix = body[: located.content_start], body[located.content_end :]

    new_body = board.append_expectation(body, "New question?", "yes")

    assert new_body.startswith(prefix)
    assert new_body.endswith(suffix)
    assert board.expectation_lines(new_body)[-1] == board.ExpectationLine(
        2, "New question?", None, None
    )


def test_append_expectation_refuses_empty_text() -> None:
    body = agent_claim_body(MINIMAL_BLOCK_TOML)

    with pytest.raises(protocol.ClaimError, match="non-empty"):
        board.append_expectation(body, "   ", "yes")


def test_append_expectation_refuses_an_unknown_default() -> None:
    body = agent_claim_body(MINIMAL_BLOCK_TOML)

    with pytest.raises(protocol.ClaimError, match="default must be"):
        board.append_expectation(body, "New question?", "maybe")


# --- expectation_lines / expectation_line_state / expectation_line_summary ---


def test_expectation_lines_reports_index_text_ruling_and_ruled_on() -> None:
    body = agent_claim_body(
        f"{MINIMAL_BLOCK_TOML}"
        '[[expectation]]\ntext = "Open one"\ndefault = "later"\n'
        '[[expectation]]\ntext = "Settled one"\nruling = "no"\nruled_on = 2026-09-01\n'
    )

    assert board.expectation_lines(body) == (
        board.ExpectationLine(1, "Open one", None, None),
        board.ExpectationLine(2, "Settled one", "no", date(2026, 9, 1)),
    )


@pytest.mark.parametrize(
    "body",
    [
        "No agent-claim fence at all.",
        agent_claim_body('version = 2\nnow = "N"\nnext = "X"\ndone_when = "D"\n'),
    ],
    ids=["legacy", "malformed"],
)
def test_expectation_lines_is_empty_for_an_unaddressable_body(body: str) -> None:
    assert board.expectation_lines(body) == ()


def test_expectation_line_state_names_open_or_the_ruling_and_date() -> None:
    open_line = board.ExpectationLine(1, "Open one", None, None)
    ruled_line = board.ExpectationLine(2, "Settled one", "no", date(2026, 9, 1))

    assert board.expectation_line_state(open_line) == "open"
    assert board.expectation_line_state(ruled_line) == "ruled no 2026-09-01"


def test_expectation_line_summary_truncates_long_text() -> None:
    line = board.ExpectationLine(1, "x" * 150, None, None)

    summary = board.expectation_line_summary(line)

    assert len(summary) == board.EXPECTATION_LINE_TEXT_MAXIMUM
    assert summary.endswith("…")


def test_expectation_line_summary_keeps_short_text_unchanged() -> None:
    line = board.ExpectationLine(1, "Short.", None, None)

    assert board.expectation_line_summary(line) == "Short."


def _slice_pull_request_body(epic: int) -> str:
    """A genuine slice-to-epic pull request body, in the shape observed
    verbatim in atelier-2's #848 and #960: the epic is named twice, once in
    substantive prose and again in a dedicated, non-closing trailer line.
    Both mentions are required — see
    `test_a_dedicated_reference_line_without_corroboration_confers_no_stage`
    for why a single, uncorroborated trailer line is not enough on its own.
    """
    return f"Ships one slice of epic #{epic}'s plan.\n\nPart of #{epic}."


def test_render_block_round_trips_every_field() -> None:
    toml_text = (
        f"{MINIMAL_BLOCK_TOML}"
        'frozen_until = { trigger = "named trigger", ruled_on = 2026-09-06 }\n'
        '[[expectation]]\ntext = "Proposed"\ndefault = "later"\n'
        '[[expectation]]\ntext = "Ruled"\nruling = "yes"\nruled_on = 2026-09-05\n'
        '[[slice]]\nindex = 4\ntitle = "Block contract in issue bodies"\n'
    )
    located = board.locate_agent_claim_block(agent_claim_body(toml_text))

    reparsed = tomllib.loads(board.render_block(located.data))

    assert reparsed == located.data


def test_render_block_escapes_quotes_and_backslashes() -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}[[slice]]\nindex = 1\ntitle = "Quote \\" and back\\\\slash"\n'
    located = board.locate_agent_claim_block(agent_claim_body(toml_text))

    reparsed = tomllib.loads(board.render_block(located.data))

    assert reparsed == located.data


def test_replace_agent_claim_block_preserves_crlf_and_surrounding_bytes() -> None:
    body = (
        "Prose before.\r\n\r\n"
        "```agent-claim\r\n"
        'version = 1\r\nnow = "N"\r\nnext = "X"\r\ndone_when = "D"\r\n'
        "```\r\n\r\nProse after.\r\n"
    )
    located = board.locate_agent_claim_block(body)
    new_data = {**located.data, "now": "Changed"}

    new_body = board.replace_agent_claim_block(body, located, new_data)

    assert new_body.startswith("Prose before.\r\n\r\n```agent-claim\r\n")
    assert new_body.endswith("```\r\n\r\nProse after.\r\n")
    assert '\nnow = "Changed"\r\n' in new_body
    assert board.parse_body(new_body).contract.now == "Changed"


def test_render_block_emits_an_empty_slice_array_after_removing_the_final_entry() -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}[[slice]]\nindex = 1\ntitle = "Only slice"\n'
    located = board.locate_agent_claim_block(agent_claim_body(toml_text))
    new_data = {**located.data, "slice": []}

    rendered = board.render_block(new_data)

    assert "slice = []" in rendered
    assert tomllib.loads(rendered)["slice"] == []


def test_uncut_is_empty_when_the_block_carries_no_slice_entry() -> None:
    container = board.Issue(
        79,
        "Container",
        (),
        agent_claim_body(MINIMAL_BLOCK_TOML),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )

    projected = projected_board(
        (container,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.uncut == ()


@pytest.mark.parametrize(
    ("next_line", "expected"),
    [
        pytest.param(None, False, id="no-next-line"),
        pytest.param("keiner", False, id="german-none"),
        pytest.param("Keine", False, id="german-none-casefolded"),
        pytest.param("nichts", False, id="no-blockers-spelling"),
        pytest.param("none", False, id="english-none"),
        pytest.param("-", False, id="dash"),
        pytest.param("Cut the next slice.", True, id="concrete-work"),
    ],
)
def test_has_further_work(next_line: str | None, expected: bool) -> None:
    assert board.has_further_work(next_line) is expected


@pytest.mark.parametrize(
    "raw_timestamp",
    [
        pytest.param("not-a-timestamp", id="unparsable"),
        pytest.param("2026-08-20T00:00:00", id="missing-offset"),
    ],
)
def test_timestamp_fails_loud_on_a_malformed_github_timestamp(raw_timestamp: str) -> None:
    """`board._timestamp` backs an issue's `age_days`/`idle_days` (its
    `created_at`/`updated_at`); GitHub's own timestamp shape is the only
    thing it ever trusts."""
    with pytest.raises(ClaimError, match="GitHub returned a malformed board timestamp"):
        board._timestamp(raw_timestamp)


def test_child_skeleton_is_an_incomplete_contract_with_no_defects() -> None:
    parsed = board.parse_body(board.BLOCK_CHILD_SKELETON)

    assert parsed.read_state is board.BodyReadState.VALID
    assert parsed.contract_complete is False
    assert parsed.contract == board.Contract("", "", "", ())


@pytest.mark.parametrize(
    ("issue", "claims", "dependencies", "expected"),
    [
        pytest.param(
            board_issue(10, "Ready", complete_contract("Claim #10.")),
            (),
            (),
            (True, None),
            id="ready",
        ),
        pytest.param(
            board_issue(10, "Claimed", complete_contract("Claim #10.")),
            (request(issue=10),),
            (),
            (False, "claimed"),
            id="claimed",
        ),
        pytest.param(
            board_issue(10, "Blocked", complete_contract("Claim #10."), blocked_by_count=1),
            (),
            (block_dependency(9),),
            (False, "blocked by #9"),
            id="blocked",
        ),
        pytest.param(
            board_issue(10, "Unblocked", complete_contract("Claim #10."), blocked_by_count=1),
            (),
            (
                block_dependency(
                    9,
                    state=board.BlockerState.CLOSED,
                    closed_at=datetime(2026, 8, 20, tzinfo=UTC),
                ),
            ),
            (True, None),
            id="closed_dependency",
        ),
        pytest.param(
            board_issue(10, "Incomplete", agent_claim_body('version = 1\nnow = "Investigate."\n')),
            (),
            (),
            (False, "body malformed: next: next is required"),
            id="malformed",
        ),
        pytest.param(
            board_issue(
                10,
                "Half-filled skeleton",
                complete_contract("", done_when=""),
            ),
            (),
            (),
            (False, "body incomplete: Next, Done when"),
            id="incomplete",
        ),
        pytest.param(
            board_issue(10, "Frozen", complete_contract("Claim #10.", frozen_until=FROZEN_UNTIL)),
            (),
            (),
            (False, f"frozen: {FROZEN_TRIGGER}"),
            id="frozen",
        ),
        pytest.param(
            board_issue(
                10,
                "Frozen and claimed",
                complete_contract("Claim #10.", frozen_until=FROZEN_UNTIL),
            ),
            (request(issue=10),),
            (),
            (False, f"frozen: {FROZEN_TRIGGER}"),
            id="frozen_takes_priority_over_claimed",
        ),
    ],
)
def test_board_reports_each_item_actionability_reason(
    issue: board.Issue,
    claims: tuple[ClaimRequest, ...],
    dependencies: tuple[board.IssueDependency, ...],
    expected: tuple[bool, str | None],
) -> None:
    blocker = board_issue(9, "Blocker", complete_contract("Claim #9."))
    projected = projected_board(
        (blocker, issue),
        (),
        (),
        tuple(_store_claim_from_request(request_value) for request_value in claims),
        board.BoardConfig(),
        dependencies={issue.number: dependencies},
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )
    item = next(item for item in projected.items if item.number == issue.number)

    actual = (item.actionable, item.actionable_reason)
    assert actual == expected


def test_board_names_every_open_dependency_in_order() -> None:
    blocked, blocked_by = blocked_issue(10, "Blocked", block_dependency(790), block_dependency(642))
    projected = projected_board(
        (
            blocked,
            board_issue(642, "P3", complete_contract("Claim #642.")),
            board_issue(790, "Review", complete_contract("Claim #790.")),
        ),
        (),
        (),
        (),
        board.BoardConfig(),
        dependencies=blocked_by,
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )
    item = next(item for item in projected.items if item.number == 10)

    assert item.open_blockers == (
        board.IssueReference(REPOSITORY, 642),
        board.IssueReference(REPOSITORY, 790),
    )
    assert item.actionable is False
    assert item.actionable_reason == "blocked by #642, #790"


def test_board_treats_an_item_with_no_dependency_as_unblocked() -> None:
    issue = board_issue(10, "Ready", complete_contract("Claim #10."))
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert projected.items[0].open_blockers == ()
    assert projected.items[0].actionable is True
    assert projected.items[0].actionable_reason is None


def test_frozen_item_leaves_actionable_and_thaws_when_the_marker_is_removed() -> None:
    frozen = board_issue(
        301, "Highest scored", complete_contract("Claim #301.", frozen_until=FROZEN_UNTIL)
    )
    projected_while_frozen = projected_board(
        (frozen,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 31, tzinfo=UTC),
    )
    item = projected_while_frozen.items[0]

    assert item.actionable is False
    assert item.actionable_reason == f"frozen: {FROZEN_TRIGGER}"
    assert item.frozen_trigger == FROZEN_TRIGGER
    assert item not in projected_while_frozen.ready_now
    assert board.highest_scored_actionable(projected_while_frozen) is None

    thawed = board_issue(301, "Highest scored", complete_contract("Claim #301."))
    projected_after_thaw = projected_board(
        (thawed,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 31, tzinfo=UTC),
    )
    thawed_item = projected_after_thaw.items[0]

    assert thawed_item.actionable is True
    assert thawed_item.actionable_reason is None
    assert thawed_item.frozen_trigger is None
    assert thawed_item in projected_after_thaw.ready_now
    assert board.highest_scored_actionable(projected_after_thaw) is thawed_item
    # The frozen marker alone changes actionability, never the score itself.
    assert item.score == thawed_item.score


def test_frozen_item_score_stays_visible_on_the_rendered_board() -> None:
    frozen = board_issue(
        301, "Highest scored", complete_contract("Claim #301.", frozen_until=FROZEN_UNTIL)
    )
    projected = projected_board(
        (frozen,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 31, tzinfo=UTC),
    )
    item = projected.items[0]
    rendered = board.render(projected)

    frozen_row = next(line for line in rendered.splitlines() if "#301" in line)
    assert str(item.score) in frozen_row
    assert f"frozen: {FROZEN_TRIGGER}" in frozen_row
    ready_now_section = rendered.split("READY NOW\n", 1)[1].split("\n\nSTALE", 1)[0]
    assert "#301" not in ready_now_section


def test_a_second_agent_claim_fence_inside_a_documentation_fence_is_not_read() -> None:
    """A body may document the block grammar in a fenced example; only one
    fence is ever open at a time, so the inner delimiter never opens a second
    recognized block and the real one stays the only read (#150 §4)."""
    body = (
        agent_claim_body(MINIMAL_BLOCK_TOML)
        + '\n~~~\n```agent-claim\nversion = 1\nnow = "Example only."\n```\n~~~\n'
    )

    parsed = board.parse_body(body)

    assert parsed.read_state is board.BodyReadState.VALID
    assert parsed.contract.now == "N"


@pytest.mark.parametrize(
    ("updated_at", "expected_stale"),
    [
        ("2026-08-14T00:00:00Z", False),
        ("2026-08-13T00:00:00Z", True),
    ],
)
def test_board_marks_text_only_items_stale_only_after_seven_idle_days(
    updated_at: str, expected_stale: bool
) -> None:
    issue = board.Issue(22, "Idle issue", (), "", "2026-08-01T00:00:00Z", updated_at)

    projected = projected_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert [item.number for item in projected.stale] == ([22] if expected_stale else [])


def test_board_ranks_a_real_blocker_ahead_of_a_blocked_product_item() -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    blocker = board.Issue(
        20,
        "Unlabelled prerequisite",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
    )
    product = board.Issue(
        21,
        "Product work",
        ("product",),
        complete_contract("Ship it."),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        blocked_by_count=1,
    )

    projected = projected_board(
        (blocker, product),
        (),
        (),
        (),
        board.BoardConfig(),
        dependencies={21: (block_dependency(20),)},
        now=now,
    )

    assert [item.number for item in projected.items] == [20, 21]
    assert projected.items[0].unblocks_count == 1
    assert projected.items[1].open_blockers == (board.IssueReference(REPOSITORY, 20),)


@pytest.mark.parametrize(
    ("dependencies", "expected_freed_on"),
    [
        pytest.param(
            (
                block_dependency(
                    10,
                    state=board.BlockerState.CLOSED,
                    closed_at=datetime(2026, 9, 1, tzinfo=UTC),
                ),
                block_dependency(11),
            ),
            None,
            id="one-dependency-remains-open",
        ),
        pytest.param(
            (
                block_dependency(
                    10,
                    state=board.BlockerState.CLOSED,
                    closed_at=datetime(2026, 9, 1, tzinfo=UTC),
                ),
                block_dependency(
                    11,
                    state=board.BlockerState.CLOSED,
                    closed_at=datetime(2026, 9, 3, tzinfo=UTC),
                ),
            ),
            datetime(2026, 9, 3, tzinfo=UTC),
            id="all-dependencies-closed",
        ),
    ],
)
def test_board_records_the_latest_closed_dependency(
    dependencies: tuple[board.IssueDependency, ...], expected_freed_on: datetime | None
) -> None:
    freed, blocked_by = blocked_issue(20, "Freed", *dependencies)
    unblocked = board_issue(21, "Never blocked", complete_contract("Ship it."))

    projected = projected_board(
        (freed, unblocked),
        (),
        (),
        (),
        board.BoardConfig(),
        dependencies=blocked_by,
        now=datetime(2026, 9, 5, tzinfo=UTC),
    )
    by_number = {item.number: item for item in projected.items}

    assert by_number[20].freed_on == expected_freed_on
    assert by_number[21].freed_on is None


def test_board_reports_when_the_last_stale_dependency_closed() -> None:
    dependent, blocked_by = blocked_issue(
        20,
        "Freed",
        block_dependency(
            10, state=board.BlockerState.CLOSED, closed_at=datetime(2026, 9, 1, tzinfo=UTC)
        ),
        block_dependency(
            11, state=board.BlockerState.CLOSED, closed_at=datetime(2026, 9, 3, tzinfo=UTC)
        ),
    )

    projected = projected_board(
        (dependent,),
        (),
        (),
        (),
        board.BoardConfig(),
        dependencies=blocked_by,
        now=datetime(2026, 9, 5, tzinfo=UTC),
    )

    item = json.loads(board.board_json(projected))["items"][0]
    assert item["freed_on"] == "2026-09-03"
    assert item["freed_days"] == 2


def test_board_text_and_json_show_freed_on_and_freed_days() -> None:
    freed, freed_dependencies = blocked_issue(
        20,
        "Freed",
        block_dependency(
            10, state=board.BlockerState.CLOSED, closed_at=datetime(2026, 9, 3, tzinfo=UTC)
        ),
    )
    blocked, blocked_dependencies = blocked_issue(21, "Blocked", block_dependency(11))
    unblocked = board_issue(22, "Never blocked", complete_contract("Ship it."))

    projected = projected_board(
        (freed, blocked, unblocked),
        (),
        (),
        (),
        board.BoardConfig(),
        dependencies={**freed_dependencies, **blocked_dependencies},
        now=datetime(2026, 9, 5, tzinfo=UTC),
    )

    rendered = board.render(projected)
    header, *rows = rendered.splitlines()
    freed_start = header.index("FREED")
    claim_start = header.index("CLAIM")

    def freed_cell(title: str) -> str:
        row = next(line for line in rows if line.endswith(title))
        return row[freed_start:claim_start].strip()

    assert freed_cell("Freed") == "2026-09-03 (2 d)"
    assert freed_cell("Blocked") == "-"
    assert freed_cell("Never blocked") == "-"

    items = {item["number"]: item for item in json.loads(board.board_json(projected))["items"]}
    assert items[20]["freed_on"] == "2026-09-03"
    assert items[20]["freed_days"] == 2
    assert items[21]["freed_on"] is None
    assert items[21]["freed_days"] is None
    assert items[22]["freed_on"] is None
    assert items[22]["freed_days"] is None


def test_board_category_order_keeps_ci_ahead_of_a_high_scoring_blocker() -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    ci = board.Issue(30, "CI", ("ci",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z")
    blocker = board.Issue(31, "Blocker", (), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z")
    dependent = board.Issue(
        32,
        "Dependent",
        (),
        "## Blocked by\n#31",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
    )
    open_pull_request = board.PullRequest(90, "Fixes #31", "", "branch")

    projected = projected_board(
        (ci, blocker, dependent),
        (open_pull_request,),
        (),
        (),
        board.BoardConfig(),
        now=now,
    )

    assert [item.number for item in projected.items[:2]] == [30, 31]
    assert projected.items[1].score > projected.items[0].score


def test_board_ranks_a_labelled_critical_item_ahead_of_a_bug_at_equal_score() -> None:
    """Both stay in the critical category (0), but the configured label's
    index still tie-breaks ahead of an unlabelled Bug's -- the same order
    the critical category has always used inside itself. The Bug carries the
    lower issue number, so a naive number tie-break (the Bug ladder removed)
    would flip this to `[1, 30]`."""
    now = datetime(2026, 8, 21, tzinfo=UTC)
    bug = board.Issue(
        1,
        "A fresh bug",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.BUG,
    )
    ci = board.Issue(30, "CI work", ("ci",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z")

    projected = projected_board((ci, bug), (), (), (), board.BoardConfig(), now=now)

    assert [item.number for item in projected.items] == [30, 1]
    assert projected.items[0].score == projected.items[1].score
    assert projected.items[0].priority_category == projected.items[1].priority_category


def test_board_ranks_a_bug_last_inside_the_critical_category() -> None:
    """The Bug and a non-critical product competitor both carry the lowest
    issue numbers here, so a naive number tie-break (the Bug ladder removed)
    would rank them `[1, 2, 40, 41, 42]` instead."""
    now = datetime(2026, 8, 21, tzinfo=UTC)
    bug = board.Issue(
        1,
        "A fresh bug",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.BUG,
    )
    product = board.Issue(
        2, "Product work", ("product",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z"
    )
    security = board.Issue(
        40, "Security", ("security",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z"
    )
    data = board.Issue(41, "Data", ("data",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z")
    ci = board.Issue(42, "CI", ("ci",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z")

    projected = projected_board(
        (security, data, ci, bug, product), (), (), (), board.BoardConfig(), now=now
    )

    assert [item.number for item in projected.items] == [40, 41, 42, 1, 2]
    assert [item.priority_category for item in projected.items[:4]] == [0, 0, 0, 0]
    assert projected.items[4].priority_category > 0


def test_board_ranks_a_bug_ahead_of_a_higher_scoring_product_item() -> None:
    """Category always wins over score: a fresh Bug (category 0) outranks an
    in-flight product item (category 3) even though the product item scores
    higher and carries the lower issue number -- neither a score- nor a
    number-based sort would save this."""
    now = datetime(2026, 8, 21, tzinfo=UTC)
    product = board.Issue(
        2, "Product work", ("product",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z"
    )
    bug = board.Issue(
        40,
        "A fresh bug",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.BUG,
    )
    in_flight_pull_request = board.PullRequest(90, "Fixes #2", "", "branch")

    projected = projected_board(
        (product, bug), (in_flight_pull_request,), (), (), board.BoardConfig(), now=now
    )

    assert [item.number for item in projected.items] == [40, 2]
    assert projected.items[1].score > projected.items[0].score


def test_board_ranks_a_blocker_ahead_of_a_last_open_child() -> None:
    """The completion boost (category 2) never outranks a real blocker (1)."""
    now = datetime(2026, 8, 21, tzinfo=UTC)
    container = board.Issue(
        100,
        "Container",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=1,
        children_total=2,
    )
    last_child = board.Issue(
        101, "Last open child", (), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z"
    )
    blocker = board.Issue(
        102, "Unblocks other work", (), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z"
    )
    dependent = board.Issue(
        103,
        "Depends on the blocker",
        (),
        complete_contract("Ship it."),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        blocked_by_count=1,
    )

    projected = projected_board(
        (container, last_child, blocker, dependent),
        (),
        (),
        (),
        board.BoardConfig(),
        dependencies={103: (block_dependency(102),)},
        now=now,
        children={100: (board.ChildItem(101, board.ChildState.OPEN),)},
    )
    by_number = {item.number: item for item in projected.items}

    assert by_number[101].priority_bucket == "last-child"
    assert by_number[102].priority_bucket == "blocker"
    assert projected.items.index(by_number[102]) < projected.items.index(by_number[101])


def test_completion_boost_requires_at_least_one_closed_sibling() -> None:
    container = board.Issue(
        110,
        "Container",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=1,
    )
    only_child = board.Issue(
        111, "Only child", (), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z"
    )

    projected = projected_board(
        (container, only_child),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        children={110: (board.ChildItem(111, board.ChildState.OPEN),)},
    )

    child_item = next(item for item in projected.items if item.number == 111)
    assert child_item.priority_bucket == "unlabelled"


def test_board_shows_container_progress_and_refuses_it_as_actionable() -> None:
    container = board.Issue(
        120,
        "Container",
        (),
        complete_contract("Cut the next slice."),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=1,
        children_total=2,
    )
    open_child = board_issue(121, "Open child", complete_contract("Ship it."))

    projected = projected_board(
        (container, open_child),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        children={120: (board.ChildItem(121, board.ChildState.OPEN),)},
    )

    container_item = next(item for item in projected.items if item.number == 120)
    assert container_item.actionable is False
    assert container_item.actionable_reason == "container; claim a child"
    assert container_item not in projected.ready_now
    assert container_item.container == board.ContainerProgress(
        1, 2, (board.ChildItem(121, board.ChildState.OPEN, blocked_by=()),)
    )

    rendered = board.render(projected)
    header = rendered.splitlines()[0]
    assert "KIND" in header
    assert "container 1/2" in rendered
    assert "CONTAINERS" in rendered
    assert "#120 1/2 closed; open: #121" in rendered

    payload = json.loads(board.board_json(projected))
    container_json = next(item for item in payload["items"] if item["number"] == 120)
    child_json = next(item for item in payload["items"] if item["number"] == 121)
    assert container_json["kind"] == "container"
    assert container_json["container"] == {
        "closed": 1,
        "total": 2,
        "open_children": [
            {"number": 121, "state": "open", "blocked_by": [], "foreign_blockers": []}
        ],
    }
    assert container_json["container_parent"] is None
    assert child_json["kind"] is None
    assert child_json["container_parent"] == 120
    assert child_json["priority_order"] == 0


def test_board_shows_a_container_child_blocked_by_another_open_issue() -> None:
    """An open container child can itself be blocked; the container's own
    open-children note must show that, not just the bare child number."""
    container = board.Issue(
        120,
        "Container",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=1,
    )
    blocker = board_issue(130, "Blocker", complete_contract("Ship it."))
    open_child, child_dependencies = blocked_issue(
        121, "Open child", block_dependency(130), next_step="Ship it."
    )

    projected = projected_board(
        (container, blocker, open_child),
        (),
        (),
        (),
        board.BoardConfig(),
        dependencies=child_dependencies,
        now=datetime(2026, 8, 21, tzinfo=UTC),
        children={120: (board.ChildItem(121, board.ChildState.OPEN),)},
    )

    assert "#120 0/1 closed; open: #121 (blocked by #130)" in board.render(projected)

    payload = json.loads(board.board_json(projected))
    container_json = next(item for item in payload["items"] if item["number"] == 120)
    assert container_json["container"]["open_children"] == [
        {"number": 121, "state": "open", "blocked_by": [130], "foreign_blockers": []}
    ]


def test_board_json_splits_a_container_childs_foreign_blocker() -> None:
    """`board --json`'s `container.open_children[].blocked_by` projects the
    same way `BoardItem.open_blockers` does (#150 A2): local ints, with the
    qualified foreign references in a sibling `foreign_blockers` key."""
    container = board.Issue(
        120,
        "Container",
        (),
        agent_claim_body(MINIMAL_BLOCK_TOML),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=1,
    )
    open_child = board.Issue(
        121,
        "Open child",
        (),
        agent_claim_body(MINIMAL_BLOCK_TOML),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        blocked_by_count=1,
    )
    dependencies = {
        121: (block_dependency(3), block_dependency(9, repository="overnightworks/other-repo"))
    }

    projected = projected_board(
        (container, open_child),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        children={120: (board.ChildItem(121, board.ChildState.OPEN),)},
        dependencies=dependencies,
    )

    payload = json.loads(board.board_json(projected))
    container_json = next(item for item in payload["items"] if item["number"] == 120)
    open_children = container_json["container"]["open_children"]

    assert open_children == [
        {
            "number": 121,
            "state": "open",
            "blocked_by": [3],
            "foreign_blockers": ["overnightworks/other-repo#9"],
        }
    ]


def test_board_kind_cell_shows_a_plain_kind_for_a_non_container_item() -> None:
    """`_kind_cell` names a real kind (task/bug/feature) plainly, without the
    container's "closed/total" progress suffix that only a container gets."""
    task = board.Issue(
        90,
        "Fix the thing",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.TASK,
    )

    projected = projected_board((task,), (), (), (), board.BoardConfig())

    header, row = board.render(projected).splitlines()[:2]
    kind_start = header.index("KIND")
    assert row[kind_start:].startswith("task")


def test_board_json_carries_a_nonzero_priority_order_for_a_critical_label() -> None:
    security_item = board.Issue(
        60, "Security work", ("security",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z"
    )
    ux_item = board.Issue(
        61, "UX work", ("ux",), "", "2026-08-20T00:00:00Z", "2026-08-20T00:00:00Z"
    )

    projected = projected_board(
        (security_item, ux_item),
        (),
        (),
        (),
        board.BoardConfig(priority_labels=("ux", "security")),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )
    payload = json.loads(board.board_json(projected))
    by_number = {item["number"]: item for item in payload["items"]}

    assert by_number[60]["priority_order"] == 1
    assert by_number[61]["priority_order"] == 0


def test_next_action_names_the_top_actionable_work_item() -> None:
    item = board_issue(10, "Top work", complete_contract("Claim #10."))
    projected = projected_board(
        (item,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    action = board.next_action(projected)

    assert isinstance(action, board.WorkItemAction)
    assert action.item.number == 10


def test_next_action_never_cuts_a_container_whose_slice_table_is_empty() -> None:
    """Issue #208: an empty `[[slice]]` table is the typed statement that
    there is nothing here to cut, even when the container's own `Next` line
    still names real work. `next_action` must not fall back to building a
    `CutSliceAction` (and an unrunnable `cut --title "<paragraph>"`) out of
    that prose -- it reports the container and its own sentence through
    `CloseContainerAction` instead, exactly like a container with nothing
    left, just with `next_step` carrying the sentence rather than `None`."""
    container = board.Issue(
        130,
        "Container",
        (),
        complete_contract("Cut the next slice.", done_when="All slices land."),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=2,
        children_total=2,
    )
    projected = projected_board(
        (container,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    action = board.next_action(projected)

    assert isinstance(action, board.CloseContainerAction)
    assert action.container.number == 130
    assert action.next_step == "Cut the next slice."


def test_next_action_closes_a_container_with_no_open_child_and_no_further_work() -> None:
    container = board.Issue(
        140,
        "Container",
        (),
        complete_contract("keiner", done_when="All slices land."),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=3,
        children_total=3,
    )
    projected = projected_board(
        (container,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    action = board.next_action(projected)

    assert isinstance(action, board.CloseContainerAction)
    assert action.container.number == 140
    assert action.container_progress == board.ContainerProgress(3, 3, ())
    assert action.next_step is None


def test_next_action_cuts_a_container_with_an_uncut_row_and_no_further_next_work() -> None:
    """An empty `Next` line alone must not close a container that still has
    an undispatched `[[slice]]` entry (#112 finding 1)."""
    container = board.Issue(
        141,
        "Container",
        (),
        complete_contract("", slice=slice_entries("Scheibe C")),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=1,
        children_total=1,
    )
    projected = projected_board(
        (container,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    action = board.next_action(projected)

    assert isinstance(action, board.CutSliceAction)
    assert action.container.number == 141
    assert action.next_step == "Scheibe C"


def test_container_progress_raises_when_an_open_child_contradicts_a_closed_summary() -> None:
    container = board.Issue(
        190,
        "Container",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=2,
        children_total=2,
    )
    raised_argument_1 = board.BoardConfig()
    raised_argument_2 = datetime(2026, 8, 21, tzinfo=UTC)
    raised_argument_3 = {190: (board.ChildItem(191, board.ChildState.OPEN),)}

    with pytest.raises(protocol.ClaimError, match=r"malformed board container #190"):
        projected_board(
            (container,),
            (),
            (),
            (),
            raised_argument_1,
            now=raised_argument_2,
            children=raised_argument_3,
        )


def test_container_progress_raises_when_no_open_child_contradicts_an_unclosed_summary() -> None:
    container = board.Issue(
        191,
        "Container",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=1,
        children_total=2,
    )
    raised_argument_1 = board.BoardConfig()
    raised_argument_2 = datetime(2026, 8, 21, tzinfo=UTC)

    with pytest.raises(protocol.ClaimError, match=r"malformed board container #191"):
        projected_board((container,), (), (), (), raised_argument_1, now=raised_argument_2)


def test_board_json_and_render_report_an_uncut_slice_entry() -> None:
    container = board.Issue(
        160,
        "Container",
        (),
        complete_contract("Cut it.", slice=slice_entries("Undispatched slice")),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    projected = projected_board(
        (container,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.uncut == (board.UncutSlices(160, (board.SliceRow(1, "Undispatched slice"),)),)
    payload = json.loads(board.board_json(projected))
    assert payload["uncut"] == [
        {"item": 160, "rows": [{"index": 1, "title": "Undispatched slice"}]}
    ]
    assert "UNCUT\n#160: rows 1 uncut" in board.render(projected)


def test_board_json_and_render_name_several_uncut_rows_by_index() -> None:
    """`#122`'s own shape (06.09.2026): several still-open rows are named
    by index, not by re-deriving it from a name string."""
    container = board.Issue(
        122,
        "Container",
        (),
        complete_contract(
            "Cut them.",
            slice=slice_entries("Fifth slice", "Sixth slice", "Seventh slice", first_index=5),
        ),
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    projected = projected_board(
        (container,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.uncut == (
        board.UncutSlices(
            122,
            (
                board.SliceRow(5, "Fifth slice"),
                board.SliceRow(6, "Sixth slice"),
                board.SliceRow(7, "Seventh slice"),
            ),
        ),
    )
    payload = json.loads(board.board_json(projected))
    assert payload["uncut"] == [
        {
            "item": 122,
            "rows": [
                {"index": 5, "title": "Fifth slice"},
                {"index": 6, "title": "Sixth slice"},
                {"index": 7, "title": "Seventh slice"},
            ],
        }
    ]
    assert "UNCUT\n#122: rows 5, 6, 7 uncut" in board.render(projected)


def test_next_action_skips_a_container_that_still_holds_an_open_child() -> None:
    container = board.Issue(
        150,
        "Container",
        (),
        "",
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=1,
    )
    projected = projected_board(
        (container,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        children={150: (board.ChildItem(151, board.ChildState.OPEN),)},
    )

    assert board.next_action(projected) is None


def test_next_names_the_boards_top_row_even_when_it_is_not_the_highest_score() -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    in_flight_unlabelled = board_issue(50, "In-flight, unlabelled", complete_contract("Ship it."))
    blocker = board_issue(
        51, "Prerequisite the operator prioritized", complete_contract("Unblock #52.")
    )
    dependent, dependent_blockers = blocked_issue(
        52, "Depends on the prerequisite", block_dependency(51), next_step="Ship it."
    )
    open_pull_request = board.PullRequest(200, "Fixes #50", "", "branch")

    projected = projected_board(
        (in_flight_unlabelled, blocker, dependent),
        (open_pull_request,),
        (),
        (),
        board.BoardConfig(),
        dependencies=dependent_blockers,
        now=now,
    )
    by_number = {item.number: item for item in projected.items}

    # #50 outscores #51 on raw score alone; #51 still leads because it carries
    # the higher-priority "blocker" bucket (it unblocks #52) that `board`
    # already sorts on ahead of score.
    assert by_number[50].score > by_number[51].score
    assert projected.items[0].number == 51

    recommended = board.highest_scored_actionable(projected)
    assert recommended is not None
    assert recommended.number == 51


def test_an_epic_inherits_the_landed_stage_of_a_slice_that_did_not_close_it() -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    epic = board_issue(
        60, "Epic cut into dispatched slices", complete_contract("Cut the next slice.")
    )
    slice_pull_request = board.PullRequest(120, "Slice 1", _slice_pull_request_body(60), "branch")

    projected = projected_board(
        (epic,), (), (slice_pull_request,), (), board.BoardConfig(), now=now
    )

    assert projected.items[0].stage is board.Stage.CODE_LANDED


def test_an_epic_is_in_flight_while_an_open_slice_touches_it_without_closing_it() -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    epic = board_issue(
        62, "Epic cut into dispatched slices", complete_contract("Cut the next slice.")
    )
    open_slice_pull_request = board.PullRequest(
        122, "Slice 1", _slice_pull_request_body(62), "branch"
    )

    projected = projected_board(
        (epic,), (open_slice_pull_request,), (), (), board.BoardConfig(), now=now
    )

    assert projected.items[0].stage is board.Stage.IN_FLIGHT


def test_a_pull_request_merely_mentioning_the_epic_number_confers_no_stage() -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    epic = board_issue(
        61, "Epic untouched by this pull request", complete_contract("Cut the next slice.")
    )
    unrelated_pull_request = board.PullRequest(
        121,
        "Unrelated fix",
        "This closes a bug that was discovered while reading #61's plan.",
        "branch",
    )

    projected = projected_board(
        (epic,), (), (unrelated_pull_request,), (), board.BoardConfig(), now=now
    )

    assert projected.items[0].stage is board.Stage.TEXT_ONLY


def test_a_dedicated_reference_line_without_corroboration_confers_no_stage() -> None:
    """A foreign pull request can still write a dedicated `Refs #N` line for an
    unrelated reason; this tool has no typed parentage relation to rule that
    out (see `_touched_without_closing`'s docstring). The one thing it can
    require is that the epic is discussed, not just named once in a trailer —
    dropping this drops the false positive without dropping the two real
    landings above, which both name their epic a second time.
    """
    now = datetime(2026, 8, 21, tzinfo=UTC)
    epic = board_issue(
        63, "Epic named only once, in passing", complete_contract("Cut the next slice.")
    )
    drive_by_pull_request = board.PullRequest(123, "Unrelated cleanup", "Refs #63.", "branch")

    projected = projected_board(
        (epic,), (), (drive_by_pull_request,), (), board.BoardConfig(), now=now
    )

    assert projected.items[0].stage is board.Stage.TEXT_ONLY


def test_a_reference_line_inside_a_fenced_code_block_confers_no_stage() -> None:
    now = datetime(2026, 8, 21, tzinfo=UTC)
    epic = board_issue(
        64, "Epic quoted inside an example, not touched", complete_contract("Cut the next slice.")
    )
    fenced_pull_request = board.PullRequest(
        124,
        "Documents the marker syntax",
        "Example of the convention:\n\n```\nPart of #64.\n```\n\nSee also #64 above.",
        "branch",
    )

    projected = projected_board(
        (epic,), (), (fenced_pull_request,), (), board.BoardConfig(), now=now
    )

    assert projected.items[0].stage is board.Stage.TEXT_ONLY


def test_a_fenced_closing_keyword_confers_no_stage() -> None:
    """The closing-keyword path (`_associated_issues`) must read the body the
    same way `_touched_without_closing` already does: a fenced example of the
    `Fixes #N` convention documents the syntax, it does not close #65.
    """
    now = datetime(2026, 8, 21, tzinfo=UTC)
    issue = board_issue(
        65, "Issue documented, never actually closed", complete_contract("Cut the next slice.")
    )
    fenced_pull_request = board.PullRequest(
        125,
        "Documents the closing-keyword syntax",
        "Example of the convention:\n\n```\nFixes #65.\n```\n\nNot itself a closing PR.",
        "branch",
    )

    projected = projected_board(
        (issue,), (), (fenced_pull_request,), (), board.BoardConfig(), now=now
    )

    assert projected.items[0].stage is board.Stage.TEXT_ONLY


def test_board_configuration_requires_unique_ordered_labels(tmp_path: Path) -> None:
    config_path = tmp_path / "board.toml"
    config_path.write_text('priority_labels = ["ux", "security"]\n')
    assert board.load_config(config_path).priority_labels == ("ux", "security")

    config_path.write_text("priority_labels = []\n")
    with pytest.raises(ClaimError, match="priority_labels"):
        board.load_config(config_path)


def test_board_configuration_fails_loud_on_unparsable_toml(tmp_path: Path) -> None:
    config_path = tmp_path / "board.toml"
    config_path.write_text("this is not valid toml =\n")

    with pytest.raises(ClaimError, match=f"cannot read board configuration {config_path}"):
        board.load_config(config_path)


def test_board_configuration_reads_and_validates_the_idea_label(tmp_path: Path) -> None:
    config_path = tmp_path / "board.toml"
    config_path.write_text('priority_labels = ["ux", "security"]\nidea_label = "idea"\n')

    assert board.load_config(config_path) == board.BoardConfig(("ux", "security"), "idea")

    config_path.write_text('idea_label = ""\n')
    with pytest.raises(ClaimError, match="idea_label"):
        board.load_config(config_path)


def test_board_configuration_keeps_body_contract_as_a_known_block_only_key(
    tmp_path: Path,
) -> None:
    """Issue #204: the pin survives with one legal value. A repository that
    carries `"block"` loads unchanged, an absent key means the block, and
    `"prose"` is refused by name -- never as an unknown key, which would
    refuse every store command in the repositories that still pin it."""
    config_path = tmp_path / "board.toml"
    assert board.load_config(config_path) == board.BoardConfig()

    config_path.write_text('body_contract = "block"\n')
    assert board.load_config(config_path) == board.BoardConfig()

    config_path.write_text('body_contract = "prose"\n')
    with pytest.raises(ClaimError) as refused_prose:
        board.load_config(config_path)
    assert str(refused_prose.value) == (
        f"board configuration {config_path} pins body_contract 'prose': "
        "prose bodies are no longer supported"
    )

    config_path.write_text('body_contract = "sideways"\n')
    with pytest.raises(
        ClaimError, match=f"{re.escape(str(config_path))} body_contract must be 'block'"
    ):
        board.load_config(config_path)

    config_path.write_text("body_contract = true\n")
    with pytest.raises(ClaimError, match="body_contract must be 'block'"):
        board.load_config(config_path)


def test_board_configuration_reads_and_validates_canonical_remote(tmp_path: Path) -> None:
    config_path = tmp_path / "board.toml"
    assert board.load_config(config_path).canonical_remote == "origin"

    config_path.write_text('canonical_remote = "upstream"\n')
    assert board.load_config(config_path).canonical_remote == "upstream"

    config_path.write_text('canonical_remote = ""\n')
    with pytest.raises(ClaimError, match="canonical_remote must be a non-empty remote name"):
        board.load_config(config_path)

    config_path.write_text("canonical_remote = true\n")
    with pytest.raises(ClaimError, match="canonical_remote must be a non-empty remote name"):
        board.load_config(config_path)


def test_board_configuration_reads_and_validates_storage(tmp_path: Path) -> None:
    config_path = tmp_path / "board.toml"
    assert board.load_config(config_path).storage is board.Storage.GITHUB

    config_path.write_text('storage = "state-ref"\n')
    assert board.load_config(config_path).storage is board.Storage.STATE_REF

    config_path.write_text('storage = "gitlab"\n')
    with pytest.raises(ClaimError, match="storage must be 'github' or 'state-ref'"):
        board.load_config(config_path)


def test_board_configuration_refuses_an_unknown_key_by_name(tmp_path: Path) -> None:
    """A typo would otherwise leave the setting at its default, with
    nothing in any output saying the file's own value was never read."""
    config_path = tmp_path / "board.toml"
    config_path.write_text('body_contarct = "block"\n')

    with pytest.raises(ClaimError) as refused:
        board.load_config(config_path)

    assert str(refused.value) == (
        f"board configuration {config_path} has unknown top-level key body_contarct"
    )

    config_path.write_text('bodies = "block"\nannotation = "x"\n')
    with pytest.raises(ClaimError, match="unknown top-level key annotation, bodies"):
        board.load_config(config_path)


def test_board_configuration_accepts_every_key_it_defines(tmp_path: Path) -> None:
    config_path = tmp_path / "board.toml"
    config_path.write_text(
        'priority_labels = ["ux"]\nidea_label = "idea"\n'
        'body_contract = "block"\ncanonical_remote = "upstream"\n'
        'storage = "state-ref"\n'
    )

    assert board.load_config(config_path) == board.BoardConfig(
        ("ux",), "idea", "upstream", board.Storage.STATE_REF
    )


def test_parse_body_reads_a_valid_minimal_block() -> None:
    parsed = board.parse_body(agent_claim_body(MINIMAL_BLOCK_TOML))

    assert parsed.read_state is board.BodyReadState.VALID
    assert parsed.contract == board.Contract("N", "X", "D", ())
    assert parsed.contract_complete is True


def test_parse_body_reads_a_skeleton_block_as_incomplete_but_valid() -> None:
    skeleton = 'version = 1\nnow = ""\nnext = ""\ndone_when = ""\n'

    parsed = board.parse_body(agent_claim_body(skeleton))

    assert parsed.read_state is board.BodyReadState.VALID
    assert parsed.contract_complete is False
    assert parsed.projectionless is True


@pytest.mark.parametrize(
    ("toml_text", "missing"),
    [
        pytest.param(MINIMAL_BLOCK_TOML, (), id="a-filled-block-is-complete"),
        pytest.param(
            'version = 1\nnow = "N"\nnext = ""\ndone_when = ""\n',
            ("Next", "Done when"),
            id="a-half-filled-skeleton-names-only-its-empty-keys",
        ),
    ],
)
def test_missing_or_empty_sections_never_names_a_dependency_key(
    toml_text: str, missing: tuple[str, ...]
) -> None:
    """A body carries no dependency key at all -- dependencies live on the
    forge -- so naming one would refuse every correctly migrated body."""
    contract = board.parse_body(agent_claim_body(toml_text)).contract

    assert board.missing_or_empty_sections(contract) == missing


def test_parse_body_treats_a_fenceless_body_as_legacy() -> None:
    parsed = board.parse_body("## Now\nOld prose.\n")

    assert parsed.read_state is board.BodyReadState.LEGACY
    assert parsed.contract == board.Contract(None, None, None, ())


def test_parse_body_refuses_multiple_agent_claim_blocks() -> None:
    body = agent_claim_body(MINIMAL_BLOCK_TOML) + agent_claim_body(MINIMAL_BLOCK_TOML)

    parsed = board.parse_body(body)

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert parsed.contract.defects[0].field == "agent-claim"


def test_parse_body_refuses_an_unclosed_agent_claim_block() -> None:
    parsed = board.parse_body("```agent-claim\nversion = 1\n")

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert parsed.contract.defects == (
        board.ContractDefect("agent-claim", "unclosed agent-claim block"),
    )


def test_parse_body_refuses_invalid_toml() -> None:
    parsed = board.parse_body(agent_claim_body("this is not toml ="))

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert parsed.contract.defects[0].field == "agent-claim"


def test_parse_body_orders_schema_defects_deterministically() -> None:
    toml_text = (
        "now = 1\n"
        "unexpected = 1\n"
        'frozen_until = { trigger = "", ruled_on = "2026-09-06", odd = 1 }\n'
        "[[expectation]]\n"
        'text = ""\n'
        "[[slice]]\n"
        'title = ""\n'
        "weird = 1\n"
    )

    parsed = board.parse_body(agent_claim_body(toml_text))

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert [defect.field for defect in parsed.contract.defects] == [
        "version",
        "now",
        "next",
        "done_when",
        "frozen_until.trigger",
        "frozen_until.ruled_on",
        "frozen_until.odd",
        "expectation[0].text",
        "expectation[0].default",
        "slice[0].index",
        "slice[0].title",
        "slice[0].weird",
        "unexpected",
    ]


@pytest.mark.parametrize(
    ("entry_toml", "expected_field"),
    [
        pytest.param('text = "E"\ndefault = "maybe"\n', "expectation[0].default", id="bad-default"),
        pytest.param(
            'text = "E"\nruling = "maybe"\nruled_on = 2026-09-06\n',
            "expectation[0].ruling",
            id="bad-ruling",
        ),
        pytest.param(
            'text = "E"\ndefault = "yes"\nruling = "yes"\nruled_on = 2026-09-06\n',
            "expectation[0].default",
            id="both-default-and-ruling",
        ),
        pytest.param('text = "E"\n', "expectation[0].default", id="neither"),
        pytest.param(
            'text = "E"\nruling = "yes"\nruled_on = "not-a-date"\n',
            "expectation[0].ruled_on",
            id="bad-ruled-on",
        ),
    ],
)
def test_parse_body_validates_the_expectation_variant_union(
    entry_toml: str, expected_field: str
) -> None:
    toml_text = f"{MINIMAL_BLOCK_TOML}[[expectation]]\n{entry_toml}"

    parsed = board.parse_body(agent_claim_body(toml_text))

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert parsed.contract.defects[0].field == expected_field


def test_parse_body_ruling_date_is_the_oldest_across_non_monotonic_expectations() -> None:
    toml_text = (
        f"{MINIMAL_BLOCK_TOML}"
        '[[expectation]]\ntext = "A"\nruling = "yes"\nruled_on = 2026-09-10\n'
        '[[expectation]]\ntext = "B"\nruling = "yes"\nruled_on = 2026-08-01\n'
    )

    parsed = board.parse_body(agent_claim_body(toml_text))

    assert parsed.read_state is board.BodyReadState.VALID
    assert parsed.expectation_state is board.ExpectationState.RULED
    assert parsed.ruling_date == date(2026, 8, 1)


def test_parse_body_refuses_a_frozen_until_that_is_not_a_table() -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}frozen_until = "not a table"\n'

    parsed = board.parse_body(agent_claim_body(toml_text))

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert parsed.contract.defects[0].field == "frozen_until.trigger"


def test_parse_body_refuses_a_non_table_expectation_entry() -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}expectation = ["oops"]\n'

    parsed = board.parse_body(agent_claim_body(toml_text))

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert parsed.contract.defects[0].field == "expectation[0]"


def test_parse_body_refuses_a_non_table_slice_entry() -> None:
    toml_text = f'{MINIMAL_BLOCK_TOML}slice = ["oops"]\n'

    parsed = board.parse_body(agent_claim_body(toml_text))

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert parsed.contract.defects[0].field == "slice[0]"


def test_parse_body_refuses_a_duplicate_slice_index() -> None:
    toml_text = (
        f'{MINIMAL_BLOCK_TOML}[[slice]]\nindex = 1\ntitle = "First"\n'
        '[[slice]]\nindex = 1\ntitle = "Second"\n'
    )

    parsed = board.parse_body(agent_claim_body(toml_text))

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert parsed.contract.defects[0].field == "slice[1].index"


@pytest.mark.parametrize(
    ("key", "malformed_toml"),
    [
        pytest.param("expectation", 'expectation = "oops"\n', id="expectation-not-a-list"),
        pytest.param("slice", 'slice = "oops"\n', id="slice-not-a-list"),
    ],
)
def test_parse_body_refuses_a_top_level_array_key_that_is_not_a_list(
    key: str, malformed_toml: str
) -> None:
    parsed = board.parse_body(agent_claim_body(f"{MINIMAL_BLOCK_TOML}{malformed_toml}"))

    assert parsed.read_state is board.BodyReadState.MALFORMED
    assert parsed.contract.defects[0].field == key


def test_parse_body_handles_a_body_with_no_trailing_newline() -> None:
    """`_line_ending` (used while walking every line for a fenced block)
    must also return `""` for the last line of a body that ends without a
    newline at all -- an ordinary GitHub body shape, not just a CRLF/LF one."""
    body = agent_claim_body(MINIMAL_BLOCK_TOML).rstrip("\n") + "\nProse with no trailing newline"

    parsed = board.parse_body(body)

    assert parsed.read_state is board.BodyReadState.VALID


def test_locate_agent_claim_block_fails_loud_with_no_recognized_fence() -> None:
    with pytest.raises(ClaimError, match="found no recognized agent-claim fence"):
        board.locate_agent_claim_block("## Now\nOld prose.\n")


def test_locate_agent_claim_block_fails_loud_with_an_unclosed_fence() -> None:
    with pytest.raises(ClaimError, match="found no closed agent-claim fence"):
        board.locate_agent_claim_block("```agent-claim\nversion = 1\n")


def test_parse_body_reads_an_emptied_slice_array_as_nothing_left_to_cut() -> None:
    toml_text = f"{MINIMAL_BLOCK_TOML}slice = []\n"

    parsed = board.parse_body(agent_claim_body(toml_text))

    assert parsed.read_state is board.BodyReadState.VALID
    assert parsed.slices == ()


def test_parse_body_reads_slice_entries_as_still_uncut() -> None:
    toml_text = (
        f'{MINIMAL_BLOCK_TOML}[[slice]]\nindex = 4\ntitle = "Block contract in issue bodies"\n'
    )

    parsed = board.parse_body(agent_claim_body(toml_text))

    assert parsed.slices == (board.SliceRow(4, "Block contract in issue bodies"),)


def test_parse_body_recognizes_a_crlf_fenced_block() -> None:
    body = (
        "Prose before.\r\n\r\n"
        "```agent-claim\r\n"
        'version = 1\r\nnow = "N"\r\nnext = "X"\r\ndone_when = "D"\r\n'
        "```\r\n\r\nProse after.\r\n"
    )

    parsed = board.parse_body(body)

    assert parsed.read_state is board.BodyReadState.VALID
    assert parsed.contract == board.Contract("N", "X", "D", ())


def test_next_action_skips_a_legacy_childless_container() -> None:
    body = "## Now\nStill going.\n\n## Next\nDo the thing.\n"
    container = replace(
        board_issue(210, "Legacy container", body),
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    projected = projected_board(
        (container,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert board.next_action(projected) is None
    item = next(item for item in projected.items if item.number == 210)
    assert item.actionable_reason == "body legacy"


def test_next_action_skips_a_malformed_childless_container() -> None:
    body = agent_claim_body('version = 2\nnow = "N"\nnext = "X"\ndone_when = "D"\n')
    container = replace(
        board_issue(211, "Malformed container", body),
        kind=board.ItemKind.CONTAINER,
        children_closed=0,
        children_total=0,
    )
    projected = projected_board(
        (container,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert board.next_action(projected) is None
    item = next(item for item in projected.items if item.number == 211)
    assert item.actionable_reason == "body malformed: version: version must be exactly 1"


def test_render_shows_projection_presence_and_dash_next_for_a_valid_block_skeleton() -> None:
    skeleton = 'version = 1\nnow = ""\nnext = ""\ndone_when = ""\n'
    issue = board_issue(220, "Skeleton", agent_claim_body(skeleton))
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    line = next(line for line in board.render(projected).splitlines() if f"#{issue.number}" in line)
    cells = re.split(r"\s{2,}", line.strip())

    assert cells[5] == "Now, Next, Done when"
    assert cells[7] == "-"


def test_a_complete_block_item_is_body_complete_with_no_dependency_projection() -> None:
    issue = board_issue(230, "Complete block item", agent_claim_body(MINIMAL_BLOCK_TOML))
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    item = next(item for item in projected.items if item.number == 230)

    assert item.contract_complete is True
    assert item.open_blockers == ()


def test_board_reports_open_local_and_foreign_dependencies_as_blockers() -> None:
    issue = board_issue(300, "Depends on two", agent_claim_body(MINIMAL_BLOCK_TOML))
    dependencies = {
        300: (
            block_dependency(3),
            block_dependency(7, repository="overnightworks/other-repo"),
        )
    }
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        dependencies=dependencies,
    )

    item = next(item for item in projected.items if item.number == 300)

    assert item.open_blockers == (
        board.IssueReference(REPOSITORY, 3),
        board.IssueReference("overnightworks/other-repo", 7),
    )
    assert item.actionable_reason == "blocked by #3, overnightworks/other-repo#7"


def test_board_json_splits_local_and_foreign_blockers_only_in_block_mode() -> None:
    """A2 (#150): `open_blockers` keeps its pre-#150 local-int-only shape;
    `foreign_blockers` is a separate key, present only under the block pin."""
    issue = board_issue(300, "Depends on two", agent_claim_body(MINIMAL_BLOCK_TOML))
    dependencies = {
        300: (
            block_dependency(3),
            block_dependency(7, repository="overnightworks/other-repo"),
        )
    }
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        dependencies=dependencies,
    )

    payload = json.loads(board.board_json(projected))
    item = next(item for item in payload["items"] if item["number"] == 300)

    assert item["open_blockers"] == [3]
    assert item["foreign_blockers"] == ["overnightworks/other-repo#7"]


def test_board_json_carries_an_empty_foreign_blockers_list_without_a_foreign_dependency() -> None:
    issue, blocked_by = blocked_issue(10, "Local only", block_dependency(642))
    other = board_issue(642, "Blocker", complete_contract("Ship it."))
    projected = projected_board(
        (issue, other),
        (),
        (),
        (),
        board.BoardConfig(),
        dependencies=blocked_by,
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    payload = json.loads(board.board_json(projected))
    item = next(item for item in payload["items"] if item["number"] == 10)

    assert item["open_blockers"] == [642]
    assert item["foreign_blockers"] == []


def test_board_never_frees_on_a_closed_foreign_dependency_alone() -> None:
    issue = board_issue(302, "Foreign-only", agent_claim_body(MINIMAL_BLOCK_TOML))
    dependencies = {
        302: (
            block_dependency(
                9,
                repository="overnightworks/other-repo",
                state=board.BlockerState.CLOSED,
                closed_at=datetime(2026, 8, 20, tzinfo=UTC),
            ),
        )
    }
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        dependencies=dependencies,
    )

    item = next(item for item in projected.items if item.number == 302)

    assert item.freed_on is None
    assert item.open_blockers == ()


def test_board_treats_a_same_repository_pull_request_dependency_like_any_other() -> None:
    """Block mode has no `blocker-is-a-PR` check (prose-only): a same-
    repository PR dependency follows its own open/closed state."""
    issue = board_issue(303, "PR blocker", agent_claim_body(MINIMAL_BLOCK_TOML))
    dependencies = {303: (block_dependency(88, is_pull_request=True),)}
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
        dependencies=dependencies,
    )

    item = next(item for item in projected.items if item.number == 303)

    assert item.open_blockers == (board.IssueReference(REPOSITORY, 88),)


@pytest.mark.parametrize(
    ("frozen_until", "claims", "dependencies", "expected_reason"),
    [
        pytest.param(FROZEN_UNTIL, (), (), f"frozen: {FROZEN_TRIGGER}", id="frozen"),
        pytest.param(None, (request(issue=10, agent="Grok 4.6"),), (), "claimed", id="claimed"),
        pytest.param(None, (), (block_dependency(9),), "blocked by #9", id="blocked"),
    ],
)
def test_a_configured_idea_keeps_freeze_claim_and_blocker_reasons(
    frozen_until: dict[str, object] | None,
    claims: tuple[ClaimRequest, ...],
    dependencies: tuple[board.IssueDependency, ...],
    expected_reason: str,
) -> None:
    wish = "## Wunsch\nMake the board clearer.\n\n"
    block_entries: dict[str, object] = (
        {} if frozen_until is None else {"frozen_until": frozen_until}
    )
    idea = board_issue(
        10,
        "Operator idea",
        wish + complete_contract("", now="", done_when="", **block_entries),
        labels=("idea",),
        blocked_by_count=len(dependencies),
    )
    blocker = board_issue(9, "Open blocker", complete_contract("Resolve the blocker."))
    projected = projected_board(
        (blocker, idea),
        (),
        (),
        tuple(_store_claim_from_request(claim_request) for claim_request in claims),
        board.BoardConfig(idea_label="idea"),
        dependencies={idea.number: dependencies},
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )
    item = next(item for item in projected.items if item.number == idea.number)

    assert item not in projected.ready_now
    assert item.actionable_reason == expected_reason


def test_an_idea_without_a_priority_label_follows_the_regular_score_order() -> None:
    regular_work = board_issue(10, "Regular work", complete_contract("Ship the change."))
    idea = board_issue(11, "Operator idea", idea_body("Make the board clearer."), labels=("idea",))

    projected = projected_board(
        (idea, regular_work),
        (),
        (),
        (),
        board.BoardConfig(idea_label="idea"),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert [item.number for item in projected.items] == [regular_work.number, idea.number]
    assert [item.priority_bucket for item in projected.items] == ["unlabelled", "unlabelled"]
    assert [item.score for item in projected.items] == [-10, -20]
    assert board.highest_scored_actionable(projected) == projected.items[0]


def test_claim_age_old_compares_real_age_against_the_threshold() -> None:
    just_over_an_hour = timedelta(seconds=3601)
    exactly_one_hour = timedelta(hours=1)
    sixty_one_minutes = timedelta(seconds=3660)

    assert board.format_claim_age(just_over_an_hour) == "1h 0m"
    assert board.claim_is_old(just_over_an_hour) is True
    assert board.format_claim_age(sixty_one_minutes) == "1h 1m"
    assert board.claim_is_old(sixty_one_minutes) is True
    assert board.claim_is_old(exactly_one_hour) is False


def test_proposed_expectations_have_neither_fresh_nor_old() -> None:
    issue = board_issue(
        10,
        "Proposed",
        complete_contract("Claim #10.", expectation=[proposed_expectation("Name it.")]),
    )
    projected = projected_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.items[0].expectation_state is board.ExpectationState.PROPOSED
    assert projected.items[0].ruling_landings is None
    assert projected.items[0].ruling_old is None


def test_one_unruled_entry_among_ruled_ones_keeps_the_item_proposed() -> None:
    issue = board_issue(
        10,
        "Three expectations",
        complete_contract(
            "Claim #10.",
            expectation=[
                ruled_expectation("Create it."),
                ruled_expectation("Change it.", ruling="no"),
                proposed_expectation("Remove it."),
            ],
        ),
    )
    projected = projected_board(
        (issue,), (), (), (), board.BoardConfig(), now=datetime(2026, 8, 21, tzinfo=UTC)
    )

    assert projected.items[0].expectation_state is board.ExpectationState.PROPOSED


def test_expectation_progress_counts_open_and_total_entries() -> None:
    body = complete_contract(
        "Claim #10.",
        expectation=[
            ruled_expectation("Create it."),
            proposed_expectation("Change it."),
            ruled_expectation("Remove it.", ruling="no"),
            proposed_expectation("Scale it.", default="no"),
            ruled_expectation("Keep it."),
        ],
    )

    parsed = board.parse_body(body)

    assert parsed.expectation_state is board.ExpectationState.PROPOSED
    assert parsed.expectation_progress == board.ExpectationProgress(open=2, total=5)


@pytest.mark.parametrize(
    ("now", "trunk_landings", "expected_landings", "expected_old"),
    [
        pytest.param(
            datetime(2026, 8, 30, tzinfo=UTC),
            tuple(datetime(2026, 8, 29, hour, tzinfo=UTC) for hour in range(10)),
            10,
            True,
            id="ten-landings-age-a-ruling",
        ),
        pytest.param(
            datetime(2026, 8, 30, tzinfo=UTC),
            (datetime(2026, 8, 29, tzinfo=UTC),),
            1,
            False,
            id="one-landing-does-not",
        ),
        pytest.param(
            datetime(2026, 8, 28, tzinfo=UTC),
            (datetime(2026, 8, 28, 23, tzinfo=UTC),),
            0,
            False,
            id="a-same-day-landing-does-not-age-the-ruling",
        ),
    ],
)
def test_ruling_freshness_counts_trunk_landings_after_the_ruled_on_date(
    now: datetime,
    trunk_landings: tuple[datetime, ...],
    expected_landings: int,
    expected_old: bool,
) -> None:
    issue = board_issue(
        10,
        "Ruled",
        complete_contract("Claim #10.", expectation=[ruled_expectation("Name it.")]),
    )
    projected = projected_board(
        (issue,),
        (),
        (),
        (),
        board.BoardConfig(),
        now=now,
        trunk_landings=trunk_landings,
    )
    item = projected.items[0]

    assert item.ruling_landings == expected_landings
    assert item.ruling_old is expected_old
    assert f"ruled {expected_landings}" in board.render(projected)


def test_each_item_carries_its_own_ruling_age() -> None:
    fresh = board_issue(
        10,
        "Fresh",
        complete_contract("Claim #10.", expectation=[ruled_expectation("Name it.")]),
    )
    old = board_issue(
        11,
        "Old",
        complete_contract(
            "Claim #11.",
            expectation=[ruled_expectation("Name it.", ruled_on=date(2026, 8, 1))],
        ),
    )
    landings = tuple(datetime(2026, 8, 10 + index, tzinfo=UTC) for index in range(12))
    projected = projected_board(
        (fresh, old),
        (),
        (),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 30, tzinfo=UTC),
        trunk_landings=landings,
    )
    by_number = {item.number: item for item in projected.items}

    assert by_number[10].ruling_old is False
    assert by_number[11].ruling_old is True
    assert by_number[11].ruling_landings == 12


@pytest.mark.parametrize(
    ("body", "closed"),
    [
        pytest.param("Closes #72", (72,), id="keyword-space-reference"),
        pytest.param("Fixes: #72", (72,), id="colon-then-space"),
        pytest.param("resolved  #72.", (72,), id="sentence-punctuation-ends-it"),
        pytest.param(f"Closes {REPOSITORY}#72, then rest", (72,), id="qualified-reference"),
        pytest.param("Closes#72", (), id="no-space-after-the-keyword"),
        pytest.param("Closes:#72", (), id="colon-without-space"),
        pytest.param("Closes\n#72", (), id="reference-on-the-next-line"),
        pytest.param("Closes #72suffix", (), id="reference-runs-into-a-word"),
        pytest.param("Lands #72", (), id="keyword-github-never-closes-on"),
    ],
)
def test_a_closing_reference_follows_githubs_own_syntax(body: str, closed: tuple[int, ...]) -> None:
    assert board.closing_references(body, REPOSITORY) == frozenset(
        board.IssueReference(REPOSITORY, number) for number in closed
    )


def test_a_closing_reference_to_another_repository_confers_no_stage() -> None:
    issue = board_issue(65, "Same number, other repository", complete_contract("Cut it."))
    foreign = board.PullRequest(
        130, "Lands elsewhere", "Fixes other/repo#65", "branch", "2026-08-20T00:00:00Z"
    )

    projected = projected_board(
        (issue,),
        (),
        (foreign,),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert projected.items[0].stage is board.Stage.TEXT_ONLY


def test_board_recovers_an_open_item_a_merged_pull_request_already_landed() -> None:
    landed = board_issue(90, "Landed but open", complete_contract("Close it."))
    also_landed = board_issue(91, "Also landed but open", "")
    merged = board.PullRequest(
        140,
        "Lands the slice",
        "Work-Item: #90\n\nCloses #90",
        "branch",
        "2026-08-20T00:00:00Z",
    )
    also_merged = board.PullRequest(
        141,
        "Lands the other slice",
        "Work-Item: #91\n\nCloses #91",
        "branch",
        "2026-08-20T00:00:00Z",
    )

    projected = projected_board(
        (landed, also_landed),
        (),
        (merged, also_merged),
        (),
        board.BoardConfig(),
        now=datetime(2026, 8, 21, tzinfo=UTC),
    )

    assert [item.number for item in projected.recovery] == [90, 91]
    assert f"RECOVERY ({board.RECOVERY_STEP})\n#90" in board.render(projected)


def test_a_non_ascii_digit_in_a_hash_reference_is_not_an_issue_number() -> None:
    qualified = board.closing_references(f"Closes {REPOSITORY}#٣", REPOSITORY)
    work_item = board.parse_pull_request_classification("Work-Item: #٣", REPOSITORY)
    assert qualified == frozenset()
    assert isinstance(work_item, board.ClassificationDefect)


def test_body_defect_text_is_the_shared_renderer() -> None:
    defect = board.ContractDefect("now", "missing")
    assert board.body_defect_text(defect) == "body malformed: now: missing"
