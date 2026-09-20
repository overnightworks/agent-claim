"""Behavioral tests for `aco board --serve` (issue #280): a real
`http.client` request against a real bound loopback server, run in a thread,
wired through the exact production functions `_dispatch` uses
(`cli._board_server`, `cli._board_html_page`, `cli.rule_item`) over the same
`FakeForge` the `--html` tests (`test_board_html.py`, `test_cli.py`) already
use. `board --html`'s own static rendering stays covered by
`test_board_html.py`'s golden test -- `render(page)` with no `served`
argument is untouched by this module (proof 8)."""

from __future__ import annotations

import errno
import http.client
import io
import os
import socket
import stat
import sys
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from board_fixtures import board_issue, complete_contract, proposed_expectation
from cli_fixtures import stub_board_config_tracked
from test_cli import (
    FakeForge,
    _assert_json_refusal_object,
    _patch_store_write,
    _single_item_board_environment,
)

from agent_coordination import board, board_serve, checkout, forge, github, protocol, workspace
from agent_coordination import cli as issue_claim

OPEN_LINE_TEXT = "Brauchen wir Admin-Rechte?"
SERVED_ITEM = 10


@pytest.fixture(autouse=True)
def _stub_board_config_tracked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every `board --serve`/`rule` test reads a tracked `board.toml` by
    default (issue #314): `_board_config`'s `checkout.path_is_tracked` call
    now runs a real `git -C <directory>` against this module's `tmp_path`
    fixtures, which are never real git checkouts, so an unstubbed call would
    always fail closed with "not a git repository" before reaching the
    behaviour under test -- matching `test_cli.py`'s and `test_protect.py`'s
    own local autouse wrapper around the same shared helper."""
    stub_board_config_tracked(monkeypatch)


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
    # Issue #388: the token is now persisted to `${XDG_CONFIG_HOME}/aco/
    # board-token` rather than minted in memory -- every test that starts a
    # real server must point that root at `tmp_path`, or it would read and
    # write the real operator's own token file.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
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
    `board.rule_expectation`'s ` Anmerkung: <note>` suffix. Issue #388 proof
    2: the follow-up page no longer shows the line as an open card with its
    three buttons -- it shows the ruled state, the note, and the
    `aco ask` hint instead, inside the item's own collapsible history."""
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
    ruled_text = f"{OPEN_LINE_TEXT} Anmerkung: {note}"
    assert lines[0].text == ruled_text

    follow_up = served_board.get(token=token).body.decode("utf-8")
    assert '<div class="cards"><p class="empty">nichts</p></div>' in follow_up
    assert f'<li class="ruled"><span>{ruled_text}</span>' in follow_up
    ruled_state = f"ruled {outcome} {datetime.now(UTC).date().isoformat()}"
    assert f'<span class="ruled-state">{ruled_state}</span>' in follow_up
    assert f'<code>aco ask {SERVED_ITEM} --text "…"</code>' in follow_up


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


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def _mint_and_capture_url(
    capsys: pytest.CaptureFixture[str], port: int, *extra_arguments: str
) -> str:
    issue_claim.main(
        ["--repo", "example/agent-claim", "board", "--serve", "--port", str(port), *extra_arguments]
    )
    return capsys.readouterr().out.strip()


def test_board_serve_prints_the_same_url_on_a_second_start(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #388 proof 1: the token now lives in
    `${XDG_CONFIG_HOME}/aco/board-token` (0600), minted once rather than per
    start, so two starts on the same port print the identical URL."""
    _served_board_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(board_serve._BoardHTTPServer, "serve_forever", lambda self: None)
    port = _free_loopback_port()

    first_url = _mint_and_capture_url(capsys, port)
    second_url = _mint_and_capture_url(capsys, port)

    assert first_url == second_url
    token_path = workspace.default_board_token_path(os.environ)
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600


def test_new_token_mints_a_different_url(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #388 proof 1: `--new-token` replaces the persisted token, so the
    next printed URL differs from the one before it."""
    _served_board_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(board_serve._BoardHTTPServer, "serve_forever", lambda self: None)
    port = _free_loopback_port()

    first_url = _mint_and_capture_url(capsys, port)
    second_url = _mint_and_capture_url(capsys, port, "--new-token")

    assert first_url != second_url


def test_a_board_token_file_with_a_permissive_mode_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #388 proof 1: a token file an operator (or another tool) left
    group/other-readable refuses by name instead of being trusted -- it is
    the one secret this command holds."""
    _served_board_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(board_serve._BoardHTTPServer, "serve_forever", lambda self: None)
    _mint_and_capture_url(capsys, _free_loopback_port())
    token_path = workspace.default_board_token_path(os.environ)
    token_path.chmod(0o644)

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "board", "--serve", "--port", str(_free_loopback_port())]
    )

    assert exit_code == 2
    assert "must be private (mode 0600, found 0644)" in capsys.readouterr().err


def test_a_board_token_file_with_invalid_content_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #388: a token file whose content is not one 43-character
    `secrets.token_urlsafe(32)` value refuses by name -- a hand-edited or
    truncated file is never trusted into an authorization comparison."""
    _served_board_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(board_serve._BoardHTTPServer, "serve_forever", lambda self: None)
    _mint_and_capture_url(capsys, _free_loopback_port())
    token_path = workspace.default_board_token_path(os.environ)
    token_path.write_text("not-a-token\n", encoding="utf-8")
    token_path.chmod(0o600)

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "board", "--serve", "--port", str(_free_loopback_port())]
    )

    assert exit_code == 2
    assert (
        f"board token at {token_path} is not a valid token; pass --new-token"
        in capsys.readouterr().err
    )


def test_a_symlinked_board_token_file_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #388: a token path that is a symlink refuses -- opening it
    `O_NOFOLLOW` means a symlink swap can never win a race with a read."""
    _served_board_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(board_serve._BoardHTTPServer, "serve_forever", lambda self: None)
    _mint_and_capture_url(capsys, _free_loopback_port())
    token_path = workspace.default_board_token_path(os.environ)
    real_token = tmp_path / "real-token"
    real_token.write_text(token_path.read_text(encoding="utf-8"), encoding="utf-8")
    real_token.chmod(0o600)
    token_path.unlink()
    token_path.symlink_to(real_token)

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "board", "--serve", "--port", str(_free_loopback_port())]
    )

    assert exit_code == 2
    assert (
        f"board token at {token_path} is not a valid token; pass --new-token"
        in capsys.readouterr().err
    )


def test_a_group_writable_board_token_directory_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #388: `~/.config/aco` left group-writable by hand refuses,
    naming the path and the actual mode, before the socket is ever bound --
    an existing directory is checked exactly like a freshly created one."""
    _served_board_environment(monkeypatch, tmp_path)
    token_directory = workspace.default_board_token_path(os.environ).parent
    token_directory.mkdir(parents=True, exist_ok=True)
    token_directory.chmod(0o770)

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "board", "--serve", "--port", str(_free_loopback_port())]
    )

    assert exit_code == 2
    assert (
        f"board token directory {token_directory} must be private and owned by this user "
        "(found mode 0770)" in capsys.readouterr().err
    )


def test_a_read_only_group_and_world_board_token_directory_is_accepted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #388 (round 3): BOARD-41 only guards the WRITE bit -- `0755`,
    an ordinary `umask 022` directory merely readable/executable by the
    group and others, is not itself unsafe and a first start succeeds."""
    _served_board_environment(monkeypatch, tmp_path)
    token_directory = workspace.default_board_token_path(os.environ).parent
    token_directory.mkdir(parents=True, exist_ok=True)
    token_directory.chmod(0o755)
    monkeypatch.setattr(board_serve._BoardHTTPServer, "serve_forever", lambda self: None)

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "board", "--serve", "--port", str(_free_loopback_port())]
    )

    assert exit_code == 0
    assert capsys.readouterr().out.strip().startswith("http://127.0.0.1:")


def test_a_group_writable_0775_board_token_directory_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #388 (round 3): `0775` carries the group WRITE bit `0755`
    lacks, so it refuses exactly like the already-covered `0770` case."""
    _served_board_environment(monkeypatch, tmp_path)
    token_directory = workspace.default_board_token_path(os.environ).parent
    token_directory.mkdir(parents=True, exist_ok=True)
    token_directory.chmod(0o775)

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "board", "--serve", "--port", str(_free_loopback_port())]
    )

    assert exit_code == 2
    assert "must be private and owned by this user" in capsys.readouterr().err


def test_serve_refuses_when_the_token_directory_is_owner_unwritable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #388 (round 3): an owner-unwritable but otherwise private
    `~/.config/aco` (`0500`) passes the directory check -- it is neither
    group/world-writable nor a symlink -- but the first mint into it
    refuses naming the token path instead of a raw `OSError` or
    `_atomic_write`'s unrelated "login recovery state" wording."""
    _served_board_environment(monkeypatch, tmp_path)
    token_path = workspace.default_board_token_path(os.environ)
    token_directory = token_path.parent
    token_directory.mkdir(parents=True, exist_ok=True)
    token_directory.chmod(0o500)

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "board", "--serve", "--port", str(_free_loopback_port())]
    )

    assert exit_code == 2
    assert f"board token at {token_path} is not a valid token" in capsys.readouterr().err


def test_new_token_refuses_when_the_token_directory_is_owner_unwritable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #388 (round 3): `--new-token`'s own mint hits the same
    owner-unwritable directory as a first mint, through `_mint_board_token`
    rather than `_first_board_token` -- it must refuse naming the token
    path too, never `_atomic_write`'s unrelated "login recovery state"
    wording."""
    _served_board_environment(monkeypatch, tmp_path)
    monkeypatch.setattr(board_serve._BoardHTTPServer, "serve_forever", lambda self: None)
    token_path = workspace.default_board_token_path(os.environ)
    _mint_and_capture_url(capsys, _free_loopback_port())
    token_path.parent.chmod(0o500)

    exit_code = issue_claim.main(
        [
            "--repo",
            "example/agent-claim",
            "board",
            "--serve",
            "--new-token",
            "--port",
            str(_free_loopback_port()),
        ]
    )

    assert exit_code == 2
    assert f"board token at {token_path} is not a valid token" in capsys.readouterr().err


def test_a_symlinked_board_token_directory_refuses(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Issue #388: `~/.config/aco` itself being a symlink refuses, the same
    as a symlinked token file -- neither is ever followed."""
    _served_board_environment(monkeypatch, tmp_path)
    config_home = Path(os.environ["XDG_CONFIG_HOME"])
    config_home.mkdir(parents=True, exist_ok=True)
    real_directory = tmp_path / "real-aco"
    real_directory.mkdir(mode=0o700)
    (config_home / "aco").symlink_to(real_directory)

    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "board", "--serve", "--port", str(_free_loopback_port())]
    )

    assert exit_code == 2
    assert "must be private and owned by this user" in capsys.readouterr().err


def test_read_board_token_refuses_a_non_regular_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #388: `_read_board_token`'s own `fstat` check refuses a file
    that opened fine but is not a regular file, caught on the open
    descriptor rather than a separate, racy `lstat`."""
    token_path = tmp_path / "board-token"
    token_path.write_text("x" * 43 + "\n", encoding="utf-8")
    token_path.chmod(0o600)

    def _fstat_not_regular(_fd: int) -> object:
        return SimpleNamespace(st_mode=stat.S_IFCHR | 0o600, st_uid=os.getuid())

    monkeypatch.setattr(os, "fstat", _fstat_not_regular)

    with pytest.raises(workspace.WorkspaceError, match="is not a valid token"):
        workspace._read_board_token(token_path)


@pytest.mark.parametrize(
    ("content", "patch_fstat"),
    [
        pytest.param(
            b"x" * 43 + b"\n",
            lambda monkeypatch: monkeypatch.setattr(
                os,
                "fstat",
                lambda _fd: SimpleNamespace(st_mode=stat.S_IFREG | 0o600, st_uid=os.getuid() + 1),
            ),
            id="wrong-owner",
        ),
        pytest.param(b"!" * 43 + b"\n", lambda _monkeypatch: None, id="non-url-safe-content"),
        pytest.param(b"\xff" * 43 + b"\n", lambda _monkeypatch: None, id="invalid-utf8"),
    ],
)
def test_read_board_token_refuses_untrusted_or_malformed_content(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    content: bytes,
    patch_fstat: Callable[[pytest.MonkeyPatch], None],
) -> None:
    """Issue #388 (round 3): the required matrix over a file `_read_board_
    token` must never trust -- owned by someone else, correctly sized but
    not one url-safe token, or bytes that are not valid UTF-8 at all --
    every one refusing the same named way rather than raising a bare
    `UnicodeDecodeError` or trusting a faked owner."""
    token_path = tmp_path / "board-token"
    token_path.write_bytes(content)
    token_path.chmod(0o600)
    patch_fstat(monkeypatch)

    with pytest.raises(workspace.WorkspaceError, match="is not a valid token"):
        workspace._read_board_token(token_path)


def test_read_board_token_refuses_a_fifo_without_hanging(tmp_path: Path) -> None:
    """Issue #388 (round 3): `O_NONBLOCK` keeps a FIFO left at this path
    from ever blocking `--serve` startup on a writer that may never arrive
    -- `fstat`'s own `S_ISREG` check refuses it by name instead. Driven on
    a thread with a timeout rather than `pytest.raises` directly, so a
    regression that reintroduces the blocking open fails this test instead
    of hanging the suite."""
    token_path = tmp_path / "board-token"
    os.mkfifo(token_path, mode=0o600)
    outcome: list[BaseException | None] = []

    def _read() -> None:
        try:
            workspace._read_board_token(token_path)
        except Exception as error:
            outcome.append(error)
        else:
            outcome.append(None)

    reader = threading.Thread(target=_read, daemon=True)
    reader.start()
    reader.join(timeout=5)

    assert not reader.is_alive(), "reading a FIFO must not block startup"
    assert len(outcome) == 1
    assert isinstance(outcome[0], workspace.WorkspaceError)
    assert "is not a valid token" in str(outcome[0])


def test_two_concurrent_first_starts_converge_on_one_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #388 proof 1 (round 3): two threads racing `_first_board_
    token` for the same, not-yet-existing file publish exactly one token.
    `os.link` is synchronized to fire only once both threads have finished
    writing their own fully-fsynced temporary file, forcing the exact
    interleaving the previous `O_CREAT | O_EXCL` mint mishandled -- the
    loser must read the winner's complete file back, never an empty one."""
    token_path = tmp_path / "board-token"
    link_barrier = threading.Barrier(2)
    real_link = os.link

    def _synchronized_link(source: str, destination: str) -> None:
        link_barrier.wait(timeout=5)
        real_link(source, destination)

    monkeypatch.setattr(os, "link", _synchronized_link)
    tokens: list[str] = []
    lock = threading.Lock()

    def _mint() -> None:
        token = workspace._first_board_token(token_path)
        with lock:
            tokens.append(token)

    threads = [threading.Thread(target=_mint) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert len(tokens) == 2
    assert tokens[0] == tokens[1]
    assert stat.S_IMODE(token_path.stat().st_mode) == 0o600
    assert workspace._read_board_token(token_path) == tokens[0]


def test_board_token_reads_an_existing_token_without_directory_write_access(
    tmp_path: Path,
) -> None:
    """Issue #388 (round 4): BOARD-32's own "minted only when missing" --
    an ordinary start with a token already on disk reads it back without
    ever needing write access to its own directory, so a directory an
    operator later locks down to owner-read-only (`0500`) still serves the
    same stable URL instead of attempting, and failing, a mint."""
    token_directory = tmp_path / "aco"
    token_path = token_directory / "board-token"
    minted = workspace.board_token(token_path)
    token_directory.chmod(0o500)

    try:
        assert workspace.board_token(token_path) == minted
    finally:
        token_directory.chmod(0o700)


def test_first_board_token_returns_the_token_even_when_cleanup_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #388 (round 4): the mint's own temporary file failing to
    unlink after a successful `os.link` publish must never replace that
    already-successful result with a raw `OSError` -- the temporary file is
    disposable litter once the real token is published and readable."""
    token_path = tmp_path / "board-token"

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated cleanup failure")

    monkeypatch.setattr(os, "unlink", _raise)

    token = workspace._first_board_token(token_path)

    assert workspace._read_board_token(token_path) == token


def test_first_board_token_refuses_when_link_fails_for_a_reason_other_than_exists(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #388: `os.link` failing with anything other than `EEXIST` (a
    read-only filesystem, a cross-device rename) is a real mint failure, not
    another writer having already won -- it refuses by name and still
    discards its own temporary file rather than leaving `.board-token.*`
    litter behind."""
    token_path = tmp_path / "board-token"

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise PermissionError("simulated link failure")

    monkeypatch.setattr(os, "link", _raise)

    with pytest.raises(workspace.WorkspaceError, match="is not a valid token"):
        workspace._first_board_token(token_path)

    assert list(tmp_path.iterdir()) == []


def test_mint_board_token_refuses_when_replace_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #388: `--new-token`'s own `os.replace` failing (an
    owner-unwritable directory discovered only at publish time, a full
    disk) refuses by name instead of raising a raw `OSError`, and still
    discards the temporary file it could not publish."""
    token_path = tmp_path / "board-token"

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", _raise)

    with pytest.raises(workspace.WorkspaceError, match="is not a valid token"):
        workspace._mint_board_token(token_path, "a" * 43)

    assert list(tmp_path.iterdir()) == []


def test_read_board_token_refuses_when_fstat_raises(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #388: `os.fstat` itself raising (not merely reporting an
    untrusted owner or mode) is caught by the same named refusal as every
    other read failure on the open descriptor, rather than a raw
    `OSError`."""
    token_path = tmp_path / "board-token"
    token_path.write_text("a" * 43 + "\n", encoding="utf-8")
    token_path.chmod(0o600)

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated fstat failure")

    monkeypatch.setattr(os, "fstat", _raise)

    with pytest.raises(workspace.WorkspaceError, match="is not a valid token"):
        workspace._read_board_token(token_path)


def test_write_temporary_token_file_removes_its_own_temp_file_on_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #388 (round 4): a write/flush/fsync/chmod failure after
    `mkstemp` removes its own temporary file (best effort) before
    re-raising, so a failed mint never leaves a `.board-token.*` file
    behind for the directory's owner to find."""

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated disk failure")

    monkeypatch.setattr(os, "fsync", _raise)

    with pytest.raises(OSError, match="simulated disk failure"):
        workspace._write_temporary_token_file(tmp_path, "board-token", b"x" * 43 + b"\n")

    assert list(tmp_path.iterdir()) == []


def test_ensure_board_token_directory_refuses_when_mkdir_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Issue #388: a directory that cannot be created (permission denied,
    read-only filesystem) refuses by name instead of raising a raw
    `OSError`."""
    target = tmp_path / "aco"

    def _raise(*_args: object, **_kwargs: object) -> None:
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "mkdir", _raise)

    with pytest.raises(workspace.WorkspaceError) as excinfo:
        workspace._ensure_board_token_directory(target)
    assert str(excinfo.value) == f"cannot create board token directory {target}"


def test_new_token_without_serve_refuses(capsys: pytest.CaptureFixture[str]) -> None:
    """Issue #388: `--new-token` outside `--serve` refuses instead of
    silently doing nothing -- there is no writer session to mint through."""
    exit_code = issue_claim.main(["--repo", "example/agent-claim", "board", "--new-token"])
    captured = capsys.readouterr()

    assert exit_code == 2
    assert "--new-token requires --serve" in captured.err


def test_new_token_without_serve_reports_invalid_usage_under_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """BOARD-39 (issue #412): the same refusal, now through the emitter --
    `invalid_usage`, never the broad `unavailable` every other `board`
    refusal falls to."""
    exit_code = issue_claim.main(
        ["--repo", "example/agent-claim", "board", "--new-token", "--json"]
    )
    captured = capsys.readouterr()

    assert exit_code == 2
    assert captured.err == "ERROR: --new-token requires --serve\n"
    _assert_json_refusal_object(captured.err, captured.out, reason="invalid_usage")


def _noop_render_page(_refused: str | None) -> str:
    return ""


def _noop_rule_item(*_args: object) -> board_serve.RuleOutcome:
    return board_serve.RuleOutcome(refusal=None)


def test_start_refuses_a_busy_port_naming_the_pid() -> None:
    """Issue #388 proof 3: a port another process already holds refuses by
    name instead of raising a raw `OSError`, naming that process's own PID
    -- `--restart` is dropped in favor of this plus an ordinary `kill`."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy_port = blocker.getsockname()[1]
        expected_refusal = f"port {busy_port} is already in use by PID {os.getpid()}"

        with pytest.raises(protocol.ClaimError, match=expected_refusal):
            board_serve.start(
                port=busy_port,
                resolve_token=lambda: "probe-token",
                render_page=_noop_render_page,
                rule_item=_noop_rule_item,
            )


def test_start_refuses_a_busy_port_before_resolving_the_token() -> None:
    """Issue #388 decision: `start` binds before it ever calls
    `resolve_token`, so a busy port refuses without touching the persistent
    token file -- `--new-token` against a busy port changes nothing."""

    def _fail_to_resolve() -> str:
        raise AssertionError("resolve_token must not run when the port is busy")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy_port = blocker.getsockname()[1]

        with pytest.raises(protocol.ClaimError):
            board_serve.start(
                port=busy_port,
                resolve_token=_fail_to_resolve,
                render_page=_noop_render_page,
                rule_item=_noop_rule_item,
            )


def test_start_reraises_an_os_error_that_is_not_address_in_use(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`start`'s busy-port refusal (BOARD-35) only replaces `EADDRINUSE`;
    any other bind failure still raises through as a plain `OSError`."""

    def _raise_permission_denied(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EACCES, "denied")

    monkeypatch.setattr(board_serve, "_BoardHTTPServer", _raise_permission_denied)

    with pytest.raises(OSError, match="denied"):
        board_serve.start(
            port=0,
            resolve_token=lambda: "probe-token",
            render_page=_noop_render_page,
            rule_item=_noop_rule_item,
        )


def test_busy_port_refusal_names_no_pid_when_proc_is_unreadable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """BOARD-35's fallback sentence, driven through the CLI end to end: a
    real second process holds the port, but an unreadable `/proc/net/tcp`
    (simulated by monkeypatching the path this module reads, never by
    calling a private helper directly) means the occupant's PID cannot be
    found, so the refusal names none rather than guessing one."""
    _served_board_environment(monkeypatch, tmp_path)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy_port = blocker.getsockname()[1]

        def _raise(*_args: object, **_kwargs: object) -> str:
            raise OSError("no /proc")

        monkeypatch.setattr(Path, "read_text", _raise)

        exit_code = issue_claim.main(
            [
                "--repo",
                "example/agent-claim",
                "board",
                "--serve",
                "--port",
                str(busy_port),
            ]
        )

    assert exit_code == 2
    assert capsys.readouterr().err.strip() == (
        f"ERROR: port {busy_port} is already in use; the owning process could not be identified"
    )


def test_pid_owning_socket_inode_returns_none_for_an_orphan_inode() -> None:
    """No live process owns an inode nobody's `/proc/<pid>/fd` links to."""
    assert board_serve._pid_owning_socket_inode("999999999999") is None


def test_loopback_socket_inode_returns_none_when_no_row_matches() -> None:
    """A readable `/proc/net/tcp` with no `LISTEN` row for the port names no
    inode -- the port is simply free, not merely unreadable."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind((board_serve.LOOPBACK_HOST, 0))
        free_port = probe.getsockname()[1]

    assert board_serve._loopback_socket_inode(free_port) is None


def test_loopback_socket_inode_returns_none_when_proc_net_tcp_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*_args: object, **_kwargs: object) -> None:
        raise OSError("no /proc")

    monkeypatch.setattr(Path, "read_text", _raise)

    assert board_serve._loopback_socket_inode(12345) is None


def test_pid_owning_socket_inode_returns_none_when_proc_is_unreadable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise(*_args: object, **_kwargs: object) -> None:
        raise OSError("no /proc")

    monkeypatch.setattr(os, "listdir", _raise)

    assert board_serve._pid_owning_socket_inode("1") is None


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
