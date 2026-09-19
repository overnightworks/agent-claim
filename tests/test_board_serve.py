"""Behavioral tests for `aco board --serve` (issue #280): a real
`http.client` request against a real bound loopback server, run in a thread,
wired through the exact production functions `_dispatch` uses
(`cli._board_server`, `cli._board_html_page`, `cli.rule_item`) over the same
`FakeForge` the `--html` tests (`test_board_html.py`, `test_cli.py`) already
use. `board --html`'s own static rendering stays covered by
`test_board_html.py`'s golden test -- `render(page)` with no `served`
argument is untouched by this module (proof 8)."""

from __future__ import annotations

import http.client
import io
import socket
import sys
import threading
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from board_fixtures import board_issue, complete_contract, proposed_expectation
from test_cli import FakeForge, _patch_store_write, _single_item_board_environment

from agent_coordination import board, board_serve, checkout, forge, github, protocol
from agent_coordination import cli as issue_claim

OPEN_LINE_TEXT = "Brauchen wir Admin-Rechte?"
SERVED_ITEM = 10


class _ConsistentForge(FakeForge):
    """`FakeForge.update_item_body` (`test_cli.py`) only records a write in
    `item_bodies`; every other CLI test reads that dict directly rather than
    re-fetching, so it never needs `list_open_board_issues`/`item_reference`
    to reflect a prior write. `board --serve`'s own proof is exactly that
    reflection -- a click writes, the reloaded page shows it ruled, and a
    second click on the same line now sees it already ruled -- so this
    module's own fake keeps its read surfaces in sync with its write record,
    the one behaviour a real forge already gives for free."""

    def update_item_body(self, number: int, body: str) -> None:
        super().update_item_body(number, body)
        self.board_issues = tuple(
            replace(issue, body=body) if issue.number == number else issue
            for issue in self.board_issues
        )
        reference = self.issue_references.get(number)
        if reference is not None:
            self.issue_references[number] = replace(reference, body=body)


def _served_board_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> _ConsistentForge:
    client = _ConsistentForge()
    body = complete_contract(
        "Ship #10.", expectation=[proposed_expectation(OPEN_LINE_TEXT, default="later")]
    )
    client.board_issues = (board_issue(SERVED_ITEM, "Plain item", body),)
    # `rule_item` (extracted from `_cmd_rule`) reads a write target's body
    # through `client.item_reference`, not `list_open_board_issues` -- the
    # same split `_client_with_item` (`test_cli.py`'s own `rule` fixture)
    # already wires for the CLI path this module drives through the server.
    client.issue_references[SERVED_ITEM] = forge.ItemReference(
        forge.ItemState.OPEN, "Plain item", body
    )
    monkeypatch.setattr(github, "GitHubForge", lambda _repository: client)
    monkeypatch.setattr(checkout, "_git_output", lambda _arguments, **_kwargs: str(tmp_path))
    monkeypatch.setattr(checkout, "trunk_landings", lambda *_args, **_kwargs: ())
    _patch_store_write(monkeypatch)
    return client


@dataclass(frozen=True)
class _Response:
    status: int
    body: bytes
    location: str | None
    cache_control: str | None


def _request(
    server: board_serve.BoardServer, method: str, path: str, *, body: str | None = None
) -> _Response:
    address = server.httpd.server_address
    connection = http.client.HTTPConnection(str(address[0]), int(address[1]))
    try:
        headers = {"Content-Type": "application/x-www-form-urlencoded"} if body is not None else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        return _Response(
            status=response.status,
            body=response.read(),
            location=response.getheader("Location"),
            cache_control=response.getheader("Cache-Control"),
        )
    finally:
        connection.close()


@dataclass
class ServedBoard:
    server: board_serve.BoardServer
    client: FakeForge

    def get(self, *, token: str | None, refused: str | None = None) -> _Response:
        params = {}
        if token is not None:
            params[board_serve.TOKEN_FIELD] = token
        if refused is not None:
            params[board_serve.REFUSED_FIELD] = refused
        query = f"?{urlencode(params)}" if params else ""
        return _request(self.server, "GET", f"/{query}")

    def post_rule(self, fields: dict[str, str]) -> _Response:
        return _request(self.server, "POST", "/rule", body=urlencode(fields))


@pytest.fixture
def served_board(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[ServedBoard]:
    client = _served_board_environment(monkeypatch, tmp_path)
    parsed = issue_claim._parser().parse_args(["--repo", "example/agent-claim", "board", "--serve"])
    session = issue_claim._WriteSession(
        forge=issue_claim._LazyForge(parsed.repo), release_branch=None
    )
    server = issue_claim._board_server(parsed, session)
    thread = threading.Thread(target=server.httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield ServedBoard(server, client)
    finally:
        server.httpd.shutdown()
        server.httpd.server_close()
        thread.join(timeout=5)


def test_the_server_binds_127_0_0_1_only(served_board: ServedBoard) -> None:
    assert served_board.server.httpd.server_address[0] == "127.0.0.1"
    assert served_board.server.url.startswith("http://127.0.0.1:")


@pytest.mark.parametrize("token", [None, "wrong-token"])
def test_get_without_or_with_a_wrong_token_is_forbidden_with_no_card_content(
    served_board: ServedBoard, token: str | None
) -> None:
    response = served_board.get(token=token)
    assert response.status == 403
    assert b"Plain item" not in response.body
    assert OPEN_LINE_TEXT.encode() not in response.body


def test_get_with_the_valid_token_serves_one_form_per_card_with_a_note_field(
    served_board: ServedBoard,
) -> None:
    """Issue #295: a card carries exactly one `POST /rule` form and one
    `<textarea name="note">`, its three outcomes as submit buttons."""
    response = served_board.get(token=served_board.server.token)
    assert response.status == 200
    assert response.cache_control == "no-store"
    page = response.body.decode("utf-8")
    assert f"#{SERVED_ITEM} Plain item" in page
    assert f'<span class="item-tag">#{SERVED_ITEM} Plain item</span>' in page
    assert OPEN_LINE_TEXT in page
    assert page.count('<form method="post" action="/rule"') == 1
    token_field = f'<input type="hidden" name="t" value="{served_board.server.token}">'
    assert page.count(token_field) == 1
    assert page.count('<textarea name="note"') == 1
    assert page.count('<button type="submit" name="outcome" value="yes"') == 1
    assert page.count('<button type="submit" name="outcome" value="no"') == 1
    assert page.count('<button type="submit" name="outcome" value="later"') == 1
    # `proposed_expectation`'s own `default="later"` (this module's fixture)
    # is the one outcome `board_html._render_served_form` marks `rec`/`Vorgabe`.
    assert page.count('name="outcome" value="later" class="rec"') == 1
    assert page.count('<span class="tag">Vorgabe</span>') == 1


def test_a_get_and_a_post_leave_stderr_silent(
    served_board: ServedBoard, capsys: pytest.CaptureFixture[str]
) -> None:
    """`_BoardRequestHandler.log_message`'s override (issue #280) must
    swallow the stdlib's default per-request logging -- otherwise the ruled
    form's own `?t=<token>` query string would land on stderr with every
    `GET`, and `board --serve`'s only deliberate output would no longer be
    the one stdout URL line."""
    served_board.get(token=served_board.server.token)
    served_board.post_rule(
        {"t": served_board.server.token, "item": str(SERVED_ITEM), "line": "1", "outcome": "yes"}
    )

    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    ("outcome", "note"), [("yes", "Ja bitte"), ("later", "Erst nach dem Review")]
)
def test_post_rule_with_a_valid_token_writes_exactly_one_ruling_and_redirects(
    served_board: ServedBoard, outcome: str, note: str
) -> None:
    """Issue #295 proof 4: the single per-card form's `outcome` button
    (including `later`, the one served by the card's own submit button
    rather than a note-less command line) carries the note through to
    `board.rule_expectation`'s ` Anmerkung: <note>` suffix."""
    token = served_board.server.token
    response = served_board.post_rule(
        {"t": token, "item": str(SERVED_ITEM), "line": "1", "outcome": outcome, "note": note}
    )

    assert response.status == 303
    assert response.location == f"/?t={token}"
    lines = board.expectation_lines(served_board.client.item_bodies[SERVED_ITEM])
    assert len(lines) == 1
    assert lines[0].ruling == outcome
    assert lines[0].ruled_on == datetime.now(UTC).date()
    assert lines[0].text == f"{OPEN_LINE_TEXT} Anmerkung: {note}"

    follow_up = served_board.get(token=token)
    assert OPEN_LINE_TEXT.encode() not in follow_up.body


def test_post_rule_with_a_wrong_token_is_forbidden_and_writes_nothing(
    served_board: ServedBoard,
) -> None:
    response = served_board.post_rule(
        {"t": "wrong", "item": str(SERVED_ITEM), "line": "1", "outcome": "yes"}
    )

    assert response.status == 403
    assert served_board.client.item_bodies == {}


def test_post_rule_on_an_already_ruled_line_writes_nothing_and_shows_the_refusal(
    served_board: ServedBoard,
) -> None:
    token = served_board.server.token
    served_board.post_rule({"t": token, "item": str(SERVED_ITEM), "line": "1", "outcome": "yes"})
    ruled_body = served_board.client.item_bodies[SERVED_ITEM]

    second = served_board.post_rule(
        {"t": token, "item": str(SERVED_ITEM), "line": "1", "outcome": "no"}
    )

    assert second.status == 303
    assert served_board.client.item_bodies[SERVED_ITEM] == ruled_body
    assert second.location is not None
    refused_sentence = parse_qs(urlsplit(second.location).query)["refused"][0]
    assert "already ruled" in refused_sentence

    page = served_board.get(token=token, refused=refused_sentence)
    assert refused_sentence in page.body.decode("utf-8")


def test_an_unknown_path_is_not_found(served_board: ServedBoard) -> None:
    response = _request(served_board.server, "GET", "/unknown")
    assert response.status == 404


def test_post_to_an_unknown_path_is_not_found(served_board: ServedBoard) -> None:
    response = _request(served_board.server, "POST", "/unknown", body="")
    assert response.status == 404


@pytest.mark.parametrize(
    "fields",
    [
        {"t": "will-be-replaced", "line": "1", "outcome": "yes"},
        {"t": "will-be-replaced", "item": "10", "outcome": "yes"},
        {"t": "will-be-replaced", "item": "10", "line": "1"},
        {"t": "will-be-replaced", "item": "not-a-number", "line": "1", "outcome": "yes"},
        {"t": "will-be-replaced", "item": "10", "line": "not-a-number", "outcome": "yes"},
        {"t": "will-be-replaced", "item": "²", "line": "1", "outcome": "yes"},
        {"t": "will-be-replaced", "item": "10", "line": "²", "outcome": "yes"},
    ],
)
def test_post_rule_with_a_malformed_body_is_a_bad_request(
    served_board: ServedBoard, fields: dict[str, str]
) -> None:
    fields["t"] = served_board.server.token

    response = served_board.post_rule(fields)

    assert response.status == 400
    assert served_board.client.item_bodies == {}


def _raw_post_status(server: board_serve.BoardServer, content_length: str | None) -> int:
    """A `POST /rule` whose `Content-Length` header is exactly the caller's
    raw string (or omitted when `None`), sent over a bare socket --
    `http.client` computes its own correct header and refuses to be told
    otherwise, so a hostile or malformed value can only be produced this
    way."""
    host, port = str(server.httpd.server_address[0]), int(server.httpd.server_address[1])
    body = urlencode(
        {"t": server.token, "item": str(SERVED_ITEM), "line": "1", "outcome": "yes"}
    ).encode("ascii")
    length_header = f"Content-Length: {content_length}\r\n" if content_length is not None else ""
    request = (
        f"POST /rule HTTP/1.1\r\n"
        f"Host: {host}:{port}\r\n"
        f"Content-Type: application/x-www-form-urlencoded\r\n"
        f"{length_header}"
        f"Connection: close\r\n\r\n"
    ).encode("latin-1") + body
    with socket.create_connection((host, port), timeout=5) as connection:
        connection.sendall(request)
        response = connection.recv(65536)
    return int(response.split(b" ", 2)[1])


@pytest.mark.parametrize(
    "content_length",
    [None, "not-a-number", "-1", "²", str(board_serve._MAX_CONTENT_LENGTH + 1)],
)
def test_post_rule_with_a_missing_invalid_or_oversized_content_length_is_a_bad_request(
    served_board: ServedBoard, content_length: str | None
) -> None:
    status = _raw_post_status(served_board.server, content_length)

    assert status == 400
    assert served_board.client.item_bodies == {}


@pytest.mark.parametrize("conflicting_flag", ["--html", "--json"])
def test_serve_refuses_together_with_html_or_json(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, conflicting_flag: str
) -> None:
    _single_item_board_environment(monkeypatch, tmp_path)

    with pytest.raises(SystemExit):
        issue_claim.main(["--repo", "example/agent-claim", "board", "--serve", conflicting_flag])


def test_board_serve_dispatches_through_the_write_session_and_prints_the_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`board --serve` is a write command (issue #280): `_dispatch` must
    reach it through `_WriteSession` -- `session.forge.writer()` inside
    `_board_server` is what would refuse a state-ref repository, exactly
    like `aco rule` does -- never through the read-only `board` path.
    `serve_forever` is stubbed to return immediately so this test proves the
    wiring without blocking on the network."""
    _served_board_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(board_serve._BoardHTTPServer, "serve_forever", lambda self: None)

    exit_code = issue_claim.main(["--repo", "example/agent-claim", "board", "--serve"])

    assert exit_code == 0
    printed = capsys.readouterr().out.strip()
    assert printed.startswith("http://127.0.0.1:")
    assert f"{board_serve.TOKEN_FIELD}=" in printed


def test_board_serve_flushes_the_url_line_before_blocking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A log reader only ever sees bytes already flushed (issue #280):
    `capsys`'s capture stream is unbuffered and cannot show this bug, so
    this drives stdout through a real `TextIOWrapper` over a `BytesIO` with
    `write_through=False` -- `print(..., flush=True)` reaches the
    underlying buffer immediately, an unflushed `print` would not."""
    _served_board_environment(monkeypatch, tmp_path)
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, write_through=False))
    written_before_serve = b""

    def record_before_blocking(self: board_serve._BoardHTTPServer) -> None:
        nonlocal written_before_serve
        written_before_serve = raw.getvalue()

    monkeypatch.setattr(board_serve._BoardHTTPServer, "serve_forever", record_before_blocking)

    exit_code = issue_claim.main(["--repo", "example/agent-claim", "board", "--serve"])

    assert exit_code == 0
    assert written_before_serve.decode().strip().startswith("http://127.0.0.1:")


def _raise_keyboard_interrupt(self: board_serve._BoardHTTPServer) -> None:
    raise KeyboardInterrupt


def test_board_serve_exits_cleanly_on_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Ctrl-C during `serve_forever` is how an operator stops `board --serve`
    (issue #280): `_cmd_board_serve`'s `except KeyboardInterrupt: pass` must
    exit `0` with only the one URL line already printed, never a
    traceback."""
    _served_board_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(board_serve._BoardHTTPServer, "serve_forever", _raise_keyboard_interrupt)

    exit_code = issue_claim.main(["--repo", "example/agent-claim", "board", "--serve"])

    assert exit_code == 0
    captured = capsys.readouterr()
    assert captured.err == ""
    assert captured.out.count("\n") == 1
    assert captured.out.strip().startswith("http://127.0.0.1:")


def test_rule_item_refuses_an_already_ruled_line_by_name(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`board_serve.py` never re-validates a refusal reason itself (issue
    #280): `cli.rule_item` is the one owner, and its `protocol.ClaimError`
    is what `_board_server`'s `post_rule` closure turns into a
    `board_serve.RuleOutcome`."""
    client = _served_board_environment(monkeypatch, tmp_path)
    body = client.issue_references[SERVED_ITEM].body
    assert body is not None
    once_ruled = board.rule_expectation(body, 1, "yes", datetime.now(UTC).date())
    client.issue_references[SERVED_ITEM] = forge.ItemReference(
        forge.ItemState.OPEN, "Plain item", once_ruled
    )

    with pytest.raises(protocol.ClaimError, match="already ruled"):
        issue_claim.rule_item(client, SERVED_ITEM, 1, "no", None)
