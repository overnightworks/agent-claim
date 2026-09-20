"""The loopback HTTP transport for `aco board --serve` (issue #280).

This module owns bytes on the wire only: binding `127.0.0.1`, then resolving
a caller-supplied persistent token (issue #388: `workspace.board_token`
mints or reads it, `cli.py`'s `_board_server` is the one caller that
resolves it -- only after the bind, so a busy port never touches the token
file), routing exactly the two paths the ruled form names, and turning a
caller-supplied page renderer and rule writer into HTTP responses. It never
reads or writes board state itself -- `cli.py` stays the one owner of
"state -> page" (`board_html.render`) and "click -> ruled line"
(`body.rule_expectation` through the store); this module only carries their
calls over the socket, so it is also the one place in this package allowed
to touch `http.server` at all.
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from errno import EADDRINUSE
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import cast
from urllib.parse import parse_qs, quote, urlsplit

from . import protocol

LOOPBACK_HOST = "127.0.0.1"
TOKEN_FIELD = "t"
REFUSED_FIELD = "refused"
_ROOT_PATH = "/"
_RULE_PATH = "/rule"
_NO_STORE = "no-store"
_HTML_CONTENT_TYPE = "text/html; charset=utf-8"
_PLAIN_CONTENT_TYPE = "text/plain; charset=utf-8"
_FORBIDDEN_BODY = b"forbidden: missing or wrong token"
_NOT_FOUND_BODY = b"not found"
_BAD_REQUEST_BODY = b"bad request: item, line, and outcome are required"
_BAD_CONTENT_LENGTH_BODY = b"bad request: missing, invalid, or oversized Content-Length"
_MAX_CONTENT_LENGTH = 64 * 1024
"""64 KiB: generously covers the ruled form's few short hidden fields plus an
operator's note -- any larger claimed length is refused before ever reading
`rfile`, the same defensive posture as the non-digit and negative cases."""


@dataclass(frozen=True)
class RuleOutcome:
    """One `POST /rule` result, carried back across the redirect instead of
    a stack trace: `refusal` is the by-name sentence the caller's own rule
    writer raised (already ruled, out of range, a bad outcome, ...), or
    `None` once the line was written."""

    refusal: str | None


RenderPage = Callable[[str | None], str]
"""The one state -> page function `board --serve` calls fresh per `GET`:
`refused` is the last `POST /rule`'s refusal sentence, when the request
carries one, else `None`."""

RuleItem = Callable[[int, int, str, str | None], RuleOutcome]
"""The one write function `board --serve` calls per `POST /rule`: item
number, expectation line, outcome, and note -- the same shape `cli.py`'s own
`rule_item` (extracted from `_cmd_rule`) already takes."""


def _field(fields: Mapping[str, list[str]], name: str) -> str | None:
    values = fields.get(name)
    return values[0] if values else None


@dataclass(frozen=True)
class _RuleRequest:
    item: int
    line: int
    outcome: str
    note: str | None


def _is_plain_digit_string(raw: str) -> bool:
    """`True` only for ASCII decimal digits -- `str.isdigit()` alone also
    accepts non-ASCII digit characters (e.g. `"²"`) that `int()` then
    refuses, so every caller that feeds a header or form field into `int()`
    guards it through here first."""
    return raw.isascii() and raw.isdigit()


def _content_length(raw: str | None) -> int | None:
    """The request's `Content-Length` when it is a plain digit string within
    `_MAX_CONTENT_LENGTH` -- `None` for missing, non-digit, negative, or
    oversized values, which `do_POST` refuses `400` before `rfile.read` ever
    runs, instead of trusting a hostile or malformed header into `int()` and
    an unbounded read."""
    if raw is None or not _is_plain_digit_string(raw):
        return None
    length = int(raw)
    return length if length <= _MAX_CONTENT_LENGTH else None


def _parsed_rule_request(fields: Mapping[str, list[str]]) -> _RuleRequest | None:
    """The four `POST /rule` fields, or `None` when `item`/`line`/`outcome`
    is missing or `item`/`line` is not a plain digit string -- a malformed
    request no server-rendered form ever sends, answered `400` rather than
    trusted into `int()`."""
    item, line, outcome = _field(fields, "item"), _field(fields, "line"), _field(fields, "outcome")
    if item is None or line is None or outcome is None:
        return None
    if not _is_plain_digit_string(item) or not _is_plain_digit_string(line):
        return None
    return _RuleRequest(int(item), int(line), outcome, _field(fields, "note"))


class _BoardHTTPServer(ThreadingHTTPServer):
    """`ThreadingHTTPServer` carrying `board --serve`'s own state: the
    per-start token and the two caller-supplied functions every request
    reads. `_BoardRequestHandler` reaches this through `self.server` --
    `http.server` hands every handler its owning server -- instead of a
    closure, so both classes stay ordinary, whitelist-referenceable module
    members rather than a factory's local ones."""

    def __init__(
        self,
        address: tuple[str, int],
        token: str,
        render_page: RenderPage,
        rule_item: RuleItem,
    ) -> None:
        super().__init__(address, _BoardRequestHandler)
        self.token = token
        self.render_page = render_page
        self.rule_item = rule_item


class _BoardRequestHandler(BaseHTTPRequestHandler):
    def _board_server(self) -> _BoardHTTPServer:
        # `http.server` types `self.server` as the base `socketserver.BaseServer`;
        # `start` below is this handler's only constructor (through
        # `_BoardHTTPServer.__init__`'s own `RequestHandlerClass` argument), so
        # the narrowing is honest, not a suppression -- the same "cast after a
        # capability/construction check" doctrine `_LazyForge.writer()` follows.
        return cast(_BoardHTTPServer, self.server)

    def _respond(
        self,
        status: HTTPStatus,
        body: bytes,
        *,
        content_type: str = _PLAIN_CONTENT_TYPE,
        location: str | None = None,
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", _NO_STORE)
        if location is not None:
            self.send_header("Location", location)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _authorized(self, server: _BoardHTTPServer, candidate: str | None) -> bool:
        return candidate is not None and hmac.compare_digest(candidate, server.token)

    def do_GET(self) -> None:
        server = self._board_server()
        split = urlsplit(self.path)
        if split.path != _ROOT_PATH:
            self._respond(HTTPStatus.NOT_FOUND, _NOT_FOUND_BODY)
            return
        query = parse_qs(split.query)
        if not self._authorized(server, _field(query, TOKEN_FIELD)):
            self._respond(HTTPStatus.FORBIDDEN, _FORBIDDEN_BODY)
            return
        page = server.render_page(_field(query, REFUSED_FIELD))
        self._respond(HTTPStatus.OK, page.encode("utf-8"), content_type=_HTML_CONTENT_TYPE)

    def do_POST(self) -> None:
        server = self._board_server()
        if urlsplit(self.path).path != _RULE_PATH:
            self._respond(HTTPStatus.NOT_FOUND, _NOT_FOUND_BODY)
            return
        length = _content_length(self.headers.get("Content-Length"))
        if length is None:
            self._respond(HTTPStatus.BAD_REQUEST, _BAD_CONTENT_LENGTH_BODY)
            return
        fields = parse_qs(self.rfile.read(length).decode("utf-8"))
        if not self._authorized(server, _field(fields, TOKEN_FIELD)):
            self._respond(HTTPStatus.FORBIDDEN, _FORBIDDEN_BODY)
            return
        parsed = _parsed_rule_request(fields)
        if parsed is None:
            self._respond(HTTPStatus.BAD_REQUEST, _BAD_REQUEST_BODY)
            return
        outcome = server.rule_item(parsed.item, parsed.line, parsed.outcome, parsed.note)
        location = f"{_ROOT_PATH}?{TOKEN_FIELD}={server.token}"
        if outcome.refusal is not None:
            location = f"{location}&{REFUSED_FIELD}={quote(outcome.refusal)}"
        self._respond(HTTPStatus.SEE_OTHER, b"", location=location)

    def log_message(self, format: str, *_args: object) -> None:
        # The stdlib default writes every request line -- including this
        # form's `?t=<token>` query string -- to stderr; `board --serve`'s
        # only deliberate output is the one stdout URL line `_cmd_board_serve`
        # prints, so per-request logging is silenced rather than leaking the
        # token into a shared terminal or log file.
        return


@dataclass(frozen=True)
class BoardServer:
    """A bound, running loopback server: `url` is the one line `aco` prints
    (issue #280's ruled form); `httpd` is `serve_forever`/`shutdown`'s owner
    for the CLI's own Ctrl-C loop and for a test that closes it from another
    thread."""

    httpd: _BoardHTTPServer
    url: str
    token: str


ResolveToken = Callable[[], str]
"""`start`'s own token source (issue #388): called only after the socket is
already bound, so a busy port refuses before `cli._board_server`'s
`workspace.board_token` ever reads or mints the persistent token file --
`--new-token` against a busy port therefore changes nothing on disk."""


def start(
    *, port: int, resolve_token: ResolveToken, render_page: RenderPage, rule_item: RuleItem
) -> BoardServer:
    """Bind `127.0.0.1:port` (`port=0` picks an ephemeral one) and return the
    running server, already listening, authenticating every request against
    `resolve_token`'s result. A port already held by another process refuses
    by name, naming its PID when `/proc` can identify it, instead of a raw
    `OSError` -- `--restart` was considered and dropped in favor of this
    refusal plus an ordinary `kill`, since the token no longer changes on a
    fresh start anyway. Binding first, before `resolve_token` ever runs,
    means that refusal happens before any token file read or mint: a busy
    port is left exactly as it was."""
    try:
        httpd = _BoardHTTPServer((LOOPBACK_HOST, port), "", render_page, rule_item)
    except OSError as error:
        if error.errno != EADDRINUSE:
            raise
        raise protocol.ClaimError(_busy_port_refusal(port)) from error
    token = resolve_token()
    httpd.token = token
    bound_port = httpd.server_address[1]
    url = f"http://{LOOPBACK_HOST}:{bound_port}{_ROOT_PATH}?{TOKEN_FIELD}={token}"
    return BoardServer(httpd=httpd, url=url, token=token)


def _busy_port_refusal(port: int) -> str:
    pid = _pid_holding_loopback_port(port)
    if pid is None:
        return f"port {port} is already in use; the owning process could not be identified"
    return f"port {port} is already in use by PID {pid}"


def _pid_holding_loopback_port(port: int) -> int | None:
    """Best-effort: `board --serve` only ever binds `127.0.0.1`, so the
    occupant's own listening socket is one `/proc/net/tcp` row away, and its
    owning process one `/proc/<pid>/fd` symlink away -- `None` on anything
    that leaves either unreadable (a non-Linux kernel, a sandboxed `/proc`,
    a race where the occupant already exited), since a refusal that cannot
    back a PID names none rather than guessing one."""
    inode = _loopback_socket_inode(port)
    return None if inode is None else _pid_owning_socket_inode(inode)


_PROC_NET_TCP_LOCAL_ADDRESS_FIELD = 1
_PROC_NET_TCP_STATE_FIELD = 3
_PROC_NET_TCP_INODE_FIELD = 9
"""`/proc/net/tcp`'s own column layout: `sl local_address rem_address st
tx_queue:rx_queue tr:tm->when retrnsmt uid timeout inode`, 0-indexed after
`str.split()` -- the inode is the tenth column."""
_LOOPBACK_ADDRESS_HEX = "0100007F"
"""`/proc/net/tcp`'s own little-endian hex encoding of `127.0.0.1` --
`board --serve` only ever binds that address, so a row naming a different
local address, however matching the port, is never this process's own
listener."""
_TCP_LISTEN_STATE = "0A"
"""`/proc/net/tcp`'s own `st` code for `TCP_LISTEN` -- the state a bound,
listening server always carries; any other state on a matching port/address
is a stale or unrelated row, not the process actually holding the bind."""


def _loopback_socket_inode(port: int) -> str | None:
    local_address = f"{_LOOPBACK_ADDRESS_HEX}:{format(port, '04X')}"
    try:
        rows = Path("/proc/net/tcp").read_text(encoding="utf-8").splitlines()[1:]
    except OSError:
        return None
    for row in rows:
        fields = row.split()
        if (
            len(fields) > _PROC_NET_TCP_INODE_FIELD
            and fields[_PROC_NET_TCP_LOCAL_ADDRESS_FIELD] == local_address
            and fields[_PROC_NET_TCP_STATE_FIELD] == _TCP_LISTEN_STATE
        ):
            return fields[_PROC_NET_TCP_INODE_FIELD]
    return None


def _pid_owning_socket_inode(inode: str) -> int | None:
    target = f"socket:[{inode}]"
    try:
        candidates = tuple(entry for entry in os.listdir("/proc") if _is_plain_digit_string(entry))
    except OSError:
        return None
    for pid in candidates:
        if _process_owns_socket(pid, target):
            return int(pid)
    return None


def _process_owns_socket(pid: str, target: str) -> bool:
    try:
        descriptors = os.listdir(f"/proc/{pid}/fd")
    except OSError:
        return False
    for descriptor in descriptors:
        try:
            if os.readlink(f"/proc/{pid}/fd/{descriptor}") == target:
                return True
        except OSError:
            continue
    return False
