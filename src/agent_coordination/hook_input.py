"""Parses Codex's `apply_patch` patch-text grammar so `protect` can check
every file path a patch touches. Pure text parsing -- no filesystem,
process, network, or coordination knowledge -- kept out of `cli` (issue
#252) so the Codex patch grammar has one small, independently testable
owner rather than growing inside `protect`'s already large module.
"""

from __future__ import annotations

from enum import Enum, auto

_ENVIRONMENT_ID_PREFIX = "*** Environment ID: "
_BEGIN_PATCH_MARKER = "*** Begin Patch"
_END_PATCH_MARKER = "*** End Patch"
_END_OF_FILE_MARKER = "*** End of File"
_ADD_FILE_PREFIX = "*** Add File: "
_DELETE_FILE_PREFIX = "*** Delete File: "
_UPDATE_FILE_PREFIX = "*** Update File: "
_MOVE_TO_PREFIX = "*** Move to: "
_FILE_LINE_PREFIXES = (_ADD_FILE_PREFIX, _DELETE_FILE_PREFIX, _UPDATE_FILE_PREFIX)
_ADD_LINE_PREFIX = "+"
_REMOVE_LINE_PREFIX = "-"
_CONTEXT_LINE_PREFIX = " "
_HUNK_MARKER_PREFIX = "@@"
# The only line shapes Codex's hunk body admits once inside an Update File
# hunk (issue #237 finding 28a): a hunk-position marker, a context,
# addition, or removal line, or the literal end-of-file marker. Anything
# else is unrecognized -- `_update_hunk_line` used to accept it as inert
# content, which risks under-reporting a path a line we do not understand
# still causes Codex to touch.
_UPDATE_HUNK_CONTENT_PREFIXES = (
    _HUNK_MARKER_PREFIX,
    _CONTEXT_LINE_PREFIX,
    _ADD_LINE_PREFIX,
    _REMOVE_LINE_PREFIX,
)


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


def _file_header_outcome(
    header_line: str, prefix: str, paths: list[str]
) -> tuple[_PatchState, bool]:
    """A recognised `Add`/`Delete`/`Update File:` header's path and the
    state it leaves the parser in.

    A header naming no path at all (issue #237 finding 28c's "empty path")
    can never reach here: every prefix ends in the one space that would
    separate it from an empty path, and that trailing space is exactly what
    `.strip()`/`.rstrip()` removes from `header_line` before matching, so
    `_matched_file_prefix` never matches an empty-path header in the first
    place -- it falls through to the callers' own "unrecognized line"
    refusal instead.
    """
    paths.append(header_line[len(prefix) :])
    return _state_after_file_header(prefix)


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
        return _file_header_outcome(header_line, prefix, paths)
    if state is _PatchState.ADD_FILE and line.startswith(_ADD_LINE_PREFIX):
        return state, False
    return None


def _update_hunk_line(
    line: str, move_to_available: bool, paths: list[str]
) -> tuple[_PatchState, bool] | None:
    """Handles one line inside an Update hunk, where only an unindented
    header ends it (Codex right-trims only, so a leading space keeps a
    header-shaped line as diff context rather than a new file).

    Returns `None` for a line the hunk grammar does not admit at all (issue
    #237 finding 28a): neither a header, a `Move to`, nor one of the hunk's
    own content shapes (`_UPDATE_HUNK_CONTENT_PREFIXES`, or the literal
    `*** End of File` marker) -- swallowing it as inert content would risk
    missing a path Codex's own parser recognises differently.
    """
    header_line = line.rstrip()
    if header_line == _END_PATCH_MARKER:
        return _PatchState.ENDED, False
    prefix = _matched_file_prefix(header_line)
    if prefix is not None:
        return _file_header_outcome(header_line, prefix, paths)
    if move_to_available and header_line.startswith(_MOVE_TO_PREFIX):
        # Same reasoning as `_file_header_outcome`: `_MOVE_TO_PREFIX` ends in
        # the space an empty path would need, and `.rstrip()` already
        # removed it above, so this never matches an empty-path `Move to`
        # either -- it falls through to the final "unrecognized" refusal.
        paths.append(header_line[len(_MOVE_TO_PREFIX) :])
        return _PatchState.UPDATE_FILE, False
    if header_line == _END_OF_FILE_MARKER or header_line.startswith(_UPDATE_HUNK_CONTENT_PREFIXES):
        return _PatchState.UPDATE_FILE, False
    return None


def _patch_content_start(lines: list[str]) -> int | None:
    """The index of the first hunk line, past `Begin Patch` and its optional
    `Environment ID` line (issue #237 finding 28b), or `None` when `lines`
    does not open with that grammar at all.

    Codex's own streaming parser admits `begin_patch environment_id? hunk+
    end_patch`: `Environment ID` comes after `Begin Patch`, never before it
    -- without accepting it there, every patch Codex prefixes with its own
    environment id was denied outright for lacking a path.
    """
    if not lines or lines[0].strip() != _BEGIN_PATCH_MARKER:
        return None
    if len(lines) > 1 and lines[1].startswith(_ENVIRONMENT_ID_PREFIX):
        return 2
    return 1


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
    both paths. See `_patch_content_start` for the optional `Environment ID`
    line right after `Begin Patch`.

    Returns an empty tuple when `text` does not fully match that grammar --
    missing `Begin`/`End Patch`, content the grammar does not admit in its
    current state, or trailing lines after `End Patch` -- the same as no
    path at all, so `protect` fails closed rather than guessing a path list
    out of a patch it cannot confidently parse.
    """
    lines = text.split("\n")
    content_start = _patch_content_start(lines)
    if content_start is None:
        return ()

    paths: list[str] = []
    state: _PatchState = _PatchState.STARTED
    move_to_available = False

    for line in lines[content_start:]:
        if state is _PatchState.ENDED:
            if line.strip():
                return ()
            continue
        if state is _PatchState.UPDATE_FILE:
            outcome = _update_hunk_line(line, move_to_available, paths)
        else:
            outcome = _outside_update_hunk_line(line, state, paths)
        if outcome is None:
            return ()
        state, move_to_available = outcome

    return tuple(paths) if state is _PatchState.ENDED else ()
