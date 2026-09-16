"""Parses Codex's `apply_patch` patch-text grammar so `protect` can check
every file path a patch touches. Pure text parsing -- no filesystem,
process, network, or coordination knowledge -- kept out of `cli` (issue
#252) so the Codex patch grammar has one small, independently testable
owner rather than growing inside `protect`'s already large module.
"""

from __future__ import annotations

from enum import Enum, auto

_BEGIN_PATCH_MARKER = "*** Begin Patch"
_END_PATCH_MARKER = "*** End Patch"
_ADD_FILE_PREFIX = "*** Add File: "
_DELETE_FILE_PREFIX = "*** Delete File: "
_UPDATE_FILE_PREFIX = "*** Update File: "
_MOVE_TO_PREFIX = "*** Move to: "
_FILE_LINE_PREFIXES = (_ADD_FILE_PREFIX, _DELETE_FILE_PREFIX, _UPDATE_FILE_PREFIX)
_ADD_LINE_PREFIX = "+"


class _PatchState(Enum):
    """Where a line sits in Codex's `apply_patch` grammar (mirrors its
    streaming parser's `StreamingParserMode`, narrowed to what deciding a
    file path needs)."""

    STARTED = auto()  # just past `Begin Patch`: only a header line is valid
    ADD_FILE = auto()  # inside an Add File block: a header or a `+` line
    DELETE_FILE = auto()  # inside a Delete File block: only a header line
    UPDATE_FILE = auto()  # inside an Update File hunk: see `_update_hunk_line`
    ENDED = auto()  # past `End Patch`: only blank lines are valid


def _matched_file_prefix(header_line: str) -> str | None:
    return next((prefix for prefix in _FILE_LINE_PREFIXES if header_line.startswith(prefix)), None)


def _state_after_file_header(prefix: str) -> tuple[_PatchState, bool]:
    """The state a recognised file header leaves the parser in, and whether
    a `*** Move to:` line may immediately follow (only true right after an
    Update File header, before any hunk content -- Codex's rename form)."""
    if prefix == _ADD_FILE_PREFIX:
        return _PatchState.ADD_FILE, False
    if prefix == _DELETE_FILE_PREFIX:
        return _PatchState.DELETE_FILE, False
    return _PatchState.UPDATE_FILE, True


def _outside_update_hunk_line(
    line: str, state: _PatchState, paths: list[str]
) -> tuple[_PatchState, bool] | None:
    """Handles one line while not inside an Update hunk, where Codex
    recognises a header after trimming both ends of the whole line. Returns
    the new `(state, move_to_available)` pair, or `None` when the line
    itself makes the whole patch unrecognized."""
    header_line = line.strip()
    if header_line == _END_PATCH_MARKER:
        return _PatchState.ENDED, False
    prefix = _matched_file_prefix(header_line)
    if prefix is not None:
        paths.append(header_line[len(prefix) :])
        return _state_after_file_header(prefix)
    if state is _PatchState.ADD_FILE and line.startswith(_ADD_LINE_PREFIX):
        return state, False
    return None


def _update_hunk_line(
    line: str, move_to_available: bool, paths: list[str]
) -> tuple[_PatchState, bool]:
    """Handles one line inside an Update hunk, where only an unindented
    header ends it (Codex right-trims only, so a leading space keeps a
    header-shaped line as diff context rather than a new file)."""
    header_line = line.rstrip()
    if header_line == _END_PATCH_MARKER:
        return _PatchState.ENDED, False
    prefix = _matched_file_prefix(header_line)
    if prefix is not None:
        paths.append(header_line[len(prefix) :])
        return _state_after_file_header(prefix)
    if move_to_available and header_line.startswith(_MOVE_TO_PREFIX):
        paths.append(header_line[len(_MOVE_TO_PREFIX) :])
        return _PatchState.UPDATE_FILE, False
    return _PatchState.UPDATE_FILE, False


def hook_patch_paths(text: str) -> tuple[str, ...]:
    """Every file path an `apply_patch` patch text touches, in the order the
    patch lists them.

    Mirrors the header-recognition rules of Codex's `apply_patch` streaming
    parser rather than a naive per-line prefix scan: outside an Update hunk
    a header is recognised after trimming both ends of the line, so an
    indented or tab-indented header still names a real file; inside an
    Update hunk only an unindented header ends it, so a leading-space line
    that merely looks like a header stays diff context. An
    `*** Update File: <path>` line immediately followed by
    `*** Move to: <path>` -- the patch grammar's rename form -- contributes
    both paths.

    Returns an empty tuple when `text` does not fully match that grammar --
    missing `Begin`/`End Patch`, content the grammar does not admit in its
    current state, or trailing lines after `End Patch` -- the same as no
    path at all, so `protect` fails closed rather than guessing a path list
    out of a patch it cannot confidently parse.
    """
    lines = text.split("\n")
    if not lines or lines[0].strip() != _BEGIN_PATCH_MARKER:
        return ()

    paths: list[str] = []
    state: _PatchState = _PatchState.STARTED
    move_to_available = False

    for line in lines[1:]:
        if state is _PatchState.ENDED:
            if line.strip():
                return ()
            continue
        if state is _PatchState.UPDATE_FILE:
            state, move_to_available = _update_hunk_line(line, move_to_available, paths)
            continue
        outcome = _outside_update_hunk_line(line, state, paths)
        if outcome is None:
            return ()
        state, move_to_available = outcome

    return tuple(paths) if state is _PatchState.ENDED else ()
