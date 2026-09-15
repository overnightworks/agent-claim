"""Behavioral tests for the pure `agent-claim` block write path `board.py`
owns (#240): `rule_expectation`, `append_expectation`, and the
`expectation_lines` projection `rule --line`, `ask`, and `rulings` all
share. CLI wiring for `rule`/`ask`/`rulings` is covered in `tests/test_cli.py`."""

from __future__ import annotations

from datetime import date

import pytest

from agent_coordination import board, protocol

MINIMAL_BLOCK_TOML = 'version = 1\nnow = "N"\nnext = "X"\ndone_when = "D"\n'


def agent_claim_body(toml_text: str, *, fence: str = "```") -> str:
    """A body carrying one recognized `agent-claim` fence around
    `toml_text`, with ordinary prose before and after it -- the same shape
    `tests/test_cli.py`'s helper of the same name builds (#150 §4)."""
    return f"Prose before.\n\n{fence}agent-claim\n{toml_text}\n{fence}\n\nProse after.\n"


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
