from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

import pytest

from agent_coordination import board, items, protocol
from agent_coordination.protocol import ClaimUnavailableError, MalformedStateTreeError

ITEM_ID = "aco-8f3a2c"


def _record(**overrides: object) -> dict[str, object]:
    """A valid `[record]` table's raw values, `overrides` replacing or
    adding keys -- the one scenario builder every test in this module
    starts from."""
    base: dict[str, object] = {
        "title": "Offline board",
        "state": "open",
        "kind": "task",
        "labels": ["backend"],
        "blocked_by": ["aco-000001"],
        "parent": "aco-000002",
        "origin": "github.com/example/agent-claim#42",
        "created_at": "2026-09-15T00:00:00Z",
        "updated_at": "2026-09-16T00:00:00Z",
    }
    base.update(overrides)
    return base


def _item_body(record: Mapping[str, object]) -> str:
    """One item file's full text: prose, then the same `agent-claim` fence
    `board.py` reads elsewhere, its interior rendered by `render_block` --
    never hand-serialized, so a test fixture and the real writer can never
    drift apart (issue #248)."""
    data = {"version": 1, "now": "N", "next": "X", "done_when": "D", "record": dict(record)}
    return f"Item body.\n\n```agent-claim\n{board.render_block(data)}```\n"


def _defect_message(defects: tuple[board.ContractDefect, ...], field: str) -> str:
    return next(defect.message for defect in defects if defect.field == field)


class TestRecordRoundTrip:
    def test_render_block_and_parse_body_round_trip_a_full_record(self) -> None:
        record = _record(closed_at=None)
        body = _item_body(record)

        parsed = board.parse_body(body, storage=board.Storage.STATE_REF)

        assert parsed.read_state is board.BodyReadState.VALID
        assert parsed.record is not None
        decoded = items.parse_item_record(ITEM_ID, parsed.record)
        assert decoded == items.ItemRecord(
            number=items.item_number(ITEM_ID),
            title="Offline board",
            state=items.RecordState.OPEN,
            kind="task",
            labels=("backend",),
            blocked_by=("aco-000001",),
            parent="aco-000002",
            origin="github.com/example/agent-claim#42",
            created_at="2026-09-15T00:00:00Z",
            updated_at="2026-09-16T00:00:00Z",
            closed_at=None,
        )

    def test_parse_item_record_defaults_omitted_optional_fields(self) -> None:
        minimal = {
            "title": "Minimal",
            "state": "open",
            "created_at": "2026-09-15T00:00:00Z",
            "updated_at": "2026-09-15T00:00:00Z",
        }
        body = _item_body(minimal)

        parsed = board.parse_body(body, storage=board.Storage.STATE_REF)

        assert parsed.record is not None
        decoded = items.parse_item_record(ITEM_ID, parsed.record)
        optional_fields = (
            decoded.kind,
            decoded.labels,
            decoded.blocked_by,
            decoded.parent,
            decoded.origin,
        )
        assert optional_fields == (None, (), (), None, None)

    def test_closed_record_round_trips_its_closed_at(self) -> None:
        record = _record(state="closed", closed_at="2026-09-16T12:00:00Z")
        body = _item_body(record)

        parsed = board.parse_body(body, storage=board.Storage.STATE_REF)

        assert parsed.record is not None
        decoded = items.parse_item_record(ITEM_ID, parsed.record)
        assert decoded.state is items.RecordState.CLOSED
        assert decoded.closed_at == "2026-09-16T12:00:00Z"


class TestRecordRefusedUnderGithub:
    def test_record_is_an_unknown_top_level_key_under_github_storage(self) -> None:
        body = _item_body(_record())

        parsed = board.parse_body(body)  # default storage is github

        assert parsed.read_state is board.BodyReadState.MALFORMED
        assert _defect_message(parsed.contract.defects, "record") == "unknown top-level key record"

    def test_record_is_never_populated_under_github_storage(self) -> None:
        # A body with no record table at all is exactly what every existing
        # GitHub-stored item already parses as; `.record` must stay `None`.
        body = '```agent-claim\nversion = 1\nnow = "N"\nnext = "X"\ndone_when = "D"\n```\n'

        parsed = board.parse_body(body)

        assert parsed.record is None


# `render_block` assumes already-valid Python values (it is a writer, never
# a validator, by its own docstring) and would crash on the wrong-shaped
# values these tests need. Every malformed case below instead assembles its
# `[record]` table from raw TOML text: valid lines by default, one
# overridden or added to trigger the one defect under test.
_VALID_RECORD_TOML_LINES = {
    "title": '"Offline board"',
    "state": '"open"',
    "created_at": '"2026-09-15T00:00:00Z"',
    "updated_at": '"2026-09-16T00:00:00Z"',
}


def _item_body_with_record_toml(overrides: Mapping[str, str]) -> str:
    fields = {**_VALID_RECORD_TOML_LINES, **overrides}
    record_lines = "\n".join(f"{key} = {value}" for key, value in fields.items())
    return (
        'Item body.\n\n```agent-claim\nversion = 1\nnow = "N"\nnext = "X"\n'
        f'done_when = "D"\n\n[record]\n{record_lines}\n```\n'
    )


class TestMalformedRecordDefects:
    @pytest.mark.parametrize(
        ("overrides", "field", "message"),
        [
            pytest.param(
                {"title": '"  "'},
                "record.title",
                "record.title must be a non-empty string",
                id="blank-title",
            ),
            pytest.param(
                {"state": '"merged"'},
                "record.state",
                "record.state must be open or closed",
                id="unknown-state",
            ),
            pytest.param(
                {"kind": '"epic"'},
                "record.kind",
                "record.kind must be a known item kind",
                id="unknown-kind",
            ),
            pytest.param(
                {"labels": '"backend"'},
                "record.labels",
                "record.labels must be an array of strings",
                id="labels-not-a-list",
            ),
            pytest.param(
                {"blocked_by": "[1]"},
                "record.blocked_by",
                "record.blocked_by must be an array of item ids",
                id="blocked-by-not-strings",
            ),
            pytest.param(
                {"parent": "7"},
                "record.parent",
                "record.parent must be an item id string",
                id="parent-not-a-string",
            ),
            pytest.param(
                {"origin": "7"},
                "record.origin",
                f"record.origin must be {items.ORIGIN_GRAMMAR_HINT}",
                id="origin-not-a-string",
            ),
            pytest.param(
                {"origin": '"not-an-origin"'},
                "record.origin",
                f"record.origin must be {items.ORIGIN_GRAMMAR_HINT}",
                id="origin-malformed-string",
            ),
            pytest.param(
                {"created_at": '"yesterday"'},
                "record.created_at",
                "record.created_at must be an RFC 3339 UTC timestamp",
                id="bad-created-at",
            ),
            pytest.param(
                {"state": '"closed"'},
                "record.closed_at",
                "record.closed_at is required when record.state is closed",
                id="closed-without-closed-at",
            ),
            pytest.param(
                {"state": '"closed"', "closed_at": '"yesterday"'},
                "record.closed_at",
                "record.closed_at must be an RFC 3339 UTC timestamp",
                id="closed-with-a-malformed-closed-at",
            ),
            pytest.param(
                {"extra": '"surprise"'},
                "record.extra",
                "unknown key record.extra",
                id="unknown-record-key",
            ),
        ],
    )
    def test_a_malformed_record_field_fails_loud(
        self, overrides: dict[str, str], field: str, message: str
    ) -> None:
        body = _item_body_with_record_toml(overrides)

        parsed = board.parse_body(body, storage=board.Storage.STATE_REF)

        assert parsed.read_state is board.BodyReadState.MALFORMED
        assert _defect_message(parsed.contract.defects, field) == message

    def test_a_non_table_record_fails_loud(self) -> None:
        # `render_block` always renders `record` as a `[record]` table;
        # build the non-table shape by hand, the one case it cannot produce.
        body = (
            'Item body.\n\n```agent-claim\nversion = 1\nnow = "N"\nnext = "X"\n'
            'done_when = "D"\nrecord = 1\n```\n'
        )

        parsed = board.parse_body(body, storage=board.Storage.STATE_REF)

        assert parsed.read_state is board.BodyReadState.MALFORMED
        assert _defect_message(parsed.contract.defects, "record") == "record must be a table"


class TestItemFilenames:
    @pytest.mark.parametrize(
        ("filename", "expected_id"),
        [
            pytest.param("aco-8f3a2c.md", "aco-8f3a2c", id="lowercase-hex"),
            pytest.param("aco-000000.md", "aco-000000", id="all-zero"),
        ],
    )
    def test_item_id_from_filename_reads_the_id(self, filename: str, expected_id: str) -> None:
        assert items.item_id_from_filename(filename) == expected_id

    @pytest.mark.parametrize(
        "filename",
        [
            pytest.param("aco-8f3a2c.txt", id="wrong-suffix"),
            pytest.param("aco-8F3A2C.md", id="uppercase-hex"),
            pytest.param("aco-8f3a2.md", id="too-short"),
            pytest.param("issue-42.md", id="wrong-prefix"),
        ],
    )
    def test_item_id_from_filename_refuses_a_malformed_name(self, filename: str) -> None:
        with pytest.raises(MalformedStateTreeError, match="is not a valid item file name"):
            items.item_id_from_filename(filename)


class TestItemNumber:
    @pytest.mark.parametrize(
        ("item_id", "expected_number"),
        [
            pytest.param("aco-000000", 0, id="zero"),
            pytest.param("aco-ffffff", 0xFFFFFF, id="max"),
            pytest.param("aco-8f3a2c", 0x8F3A2C, id="mixed"),
        ],
    )
    def test_item_number_reads_the_hex_suffix(self, item_id: str, expected_number: int) -> None:
        assert items.item_number(item_id) == expected_number

    def test_item_number_refuses_a_malformed_id(self) -> None:
        with pytest.raises(MalformedStateTreeError, match="is not a valid item id"):
            items.item_number("aco-zzzzzz")


class TestFormatItemId:
    @pytest.mark.parametrize(
        "item_id", ["aco-000000", "aco-ffffff", "aco-8f3a2c"], ids=["zero", "max", "mixed"]
    )
    def test_format_item_id_inverts_item_number(self, item_id: str) -> None:
        assert items.format_item_id(items.item_number(item_id)) == item_id


class TestMintItemId:
    def test_mint_item_id_matches_the_item_id_pattern(self) -> None:
        assert items.ITEM_ID_PATTERN.fullmatch(items.mint_item_id(()))

    def test_mint_item_id_skips_a_known_id_before_settling_on_a_fresh_one(self) -> None:
        candidates = iter(("aaaaaa", "aaaaaa", "bbbbbb"))

        minted = items.mint_item_id({"aco-aaaaaa"}, random_hex=lambda: next(candidates))

        assert minted == "aco-bbbbbb"

    def test_mint_item_id_refuses_after_three_collisions(self) -> None:
        with pytest.raises(ClaimUnavailableError, match="could not mint a fresh item id in 3"):
            items.mint_item_id({"aco-aaaaaa"}, random_hex=lambda: "aaaaaa")


class TestFormatRecordTimestamp:
    def test_format_record_timestamp_matches_the_record_pattern(self) -> None:
        formatted = items.format_record_timestamp(datetime(2026, 9, 16, 12, 30, 45, tzinfo=UTC))

        assert formatted == "2026-09-16T12:30:45Z"
        assert protocol.RFC3339_TIMESTAMP_PATTERN.fullmatch(formatted)


class TestRecordTable:
    @pytest.mark.parametrize(
        "overrides",
        [
            pytest.param({"closed_at": None}, id="open"),
            pytest.param({"state": "closed", "closed_at": "2026-09-16T12:00:00Z"}, id="closed"),
        ],
    )
    def test_record_table_round_trips_through_render_and_parse(
        self, overrides: dict[str, object]
    ) -> None:
        body = _item_body(_record(**overrides))
        parsed = board.parse_body(body, storage=board.Storage.STATE_REF)
        assert parsed.record is not None
        original = items.parse_item_record(ITEM_ID, parsed.record)

        rendered = _item_body(items.record_table(original))
        reparsed = board.parse_body(rendered, storage=board.Storage.STATE_REF)

        assert reparsed.record is not None
        assert items.parse_item_record(ITEM_ID, reparsed.record) == original

    def test_record_table_omits_every_unset_optional_field(self) -> None:
        minimal = items.ItemRecord(
            number=items.item_number(ITEM_ID),
            title="Minimal",
            state=items.RecordState.OPEN,
            kind=None,
            labels=(),
            blocked_by=(),
            parent=None,
            origin=None,
            created_at="2026-09-15T00:00:00Z",
            updated_at="2026-09-15T00:00:00Z",
            closed_at=None,
        )

        table = items.record_table(minimal)

        assert set(table) == {"title", "state", "labels", "blocked_by", "created_at", "updated_at"}


class TestParseOrigin:
    """The one origin grammar (issue #316), tested once against
    `parse_origin` -- the same `ORIGIN_PATTERN` `board._record_relation_defects`
    validates a persisted `record.origin` against, so this class is that
    grammar's one test."""

    @pytest.mark.parametrize(
        ("value", "valid"),
        [
            pytest.param("gitlab#514", True, id="forge-and-number"),
            pytest.param("github#1", True, id="single-digit"),
            pytest.param("gitea-self-hosted#42", True, id="hyphenated-forge-name"),
            pytest.param("github.com/example/agent-claim#42", True, id="host-owner-repo"),
            pytest.param("github.com/OvernightWorks/x#1", True, id="uppercase-host-owner-repo"),
            pytest.param("GitLab#514", True, id="uppercase-forge"),
            pytest.param("gitlab", False, id="no-number"),
            pytest.param("514", False, id="no-forge"),
            pytest.param("#5", False, id="no-forge-before-hash"),
            pytest.param("gitlab#", False, id="no-number-after-hash"),
            pytest.param("gitlab#x", False, id="non-digit-number"),
            pytest.param("gitlab 514", False, id="no-hash"),
            pytest.param("gitlab#0514", False, id="leading-zero"),
            pytest.param("gitlab #514", False, id="embedded-space"),
            pytest.param("", False, id="empty"),
            pytest.param("gitla\u212a#514", False, id="kelvin-sign-non-ascii-letter"),
        ],
    )
    def test_parse_origin_matches_the_one_origin_grammar(self, value: str, valid: bool) -> None:
        if valid:
            assert items.parse_origin(value) == value
            return
        with pytest.raises(ClaimUnavailableError, match="is not an origin"):
            items.parse_origin(value)
