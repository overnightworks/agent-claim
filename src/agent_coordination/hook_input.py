"""Parses two hook-payload command grammars so `protect` can check every
file path they touch: Codex's `apply_patch` patch text, and a Bash
`command`'s own write patterns (issue #380). Pure text parsing -- no
filesystem, process, network, or coordination knowledge -- kept out of
`cli` (issue #252) so each grammar has one small, independently testable
owner rather than growing inside `protect`'s already large module.
"""

from __future__ import annotations

import shlex
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


# A Bash `command`'s own recognized write patterns (issue #380): a
# redirection, or one of a short list of file-mutating commands, each
# naming the pattern text `protect`'s denial sentence quotes. `PATTERN_MOVE`
# and `PATTERN_COPY` double as the token that names the command itself
# (`mv`, `cp`), since both already read the same either way.
PATTERN_REDIRECT_OVERWRITE = ">"
PATTERN_REDIRECT_APPEND = ">>"
PATTERN_TEE = "tee"
PATTERN_SED_IN_PLACE = "sed -i"
PATTERN_MOVE = "mv"
PATTERN_COPY = "cp"
PATTERN_REMOVE = "rm"
PATTERN_GIT_CHECKOUT = "git checkout --"
PATTERN_GIT_RESTORE = "git restore"

# Newline is a command separator exactly like `;` (a multi-line Bash
# `command` is one statement per physical line), but `shlex` treats it as
# plain whitespace by default and drops it entirely -- moving it out of
# `whitespace` and into `punctuation_chars` below is what turns it back
# into a token this grammar can see and split on.
_SHELL_PUNCTUATION = "();<>|&\n"
_COMMAND_SEPARATORS = frozenset({";", "&&", "||", "|"})
_FLAG_PREFIX = "-"
# `sed -i <script> <path>`: at least the script and one path.
_SED_SCRIPT_AND_PATH_COUNT = 2


def _tokenize_command(command: str) -> tuple[str, ...]:
    """`command` split the way a POSIX shell would see its own words and
    operators (`;`, `&&`, `||`, `|`, `>`, `>>`, `<<`, ...), each operator its
    own token rather than glued to the word beside it (a non-empty
    `punctuation_chars` already implies word-splitting on whitespace, so
    `whitespace_split` needs no separate setting). Unbalanced quoting -- a
    command `protect` cannot even tokenize safely -- yields no tokens at all
    rather than a best-effort guess."""
    lexer = shlex.shlex(command, posix=True, punctuation_chars=_SHELL_PUNCTUATION)
    lexer.whitespace = lexer.whitespace.replace("\n", "")
    try:
        return tuple(lexer)
    except ValueError:
        return ()


def _is_command_separator(token: str) -> bool:
    """A `;`/`&&`/`||`/`|` token, or a run of one or more newline tokens
    (blank lines between statements merge into one token; `token != ""`
    keeps an empty string, which `shlex` never emits, from vacuously
    matching)."""
    return token in _COMMAND_SEPARATORS or (token != "" and set(token) == {"\n"})


def _is_flag(token: str) -> bool:
    return token.startswith(_FLAG_PREFIX)


def _command_end(tokens: tuple[str, ...], start: int) -> int:
    """The index just past the simple command starting at `start` -- its
    next command separator, or the end of `tokens`."""
    end = start
    while end < len(tokens) and not _is_command_separator(tokens[end]):
        end += 1
    return end


def _non_flag_arguments(tokens: tuple[str, ...], start: int, end: int) -> tuple[str, ...]:
    return tuple(token for token in tokens[start:end] if not _is_flag(token))


def _match_redirect(tokens: tuple[str, ...], index: int) -> tuple[str, tuple[str, ...], int] | None:
    """A `>`/`>>` token immediately followed by its own target -- the one
    pattern that can appear anywhere in a simple command, not only at its
    start (`cat > path`, a heredoc's own `cat > path <<EOF` included)."""
    token = tokens[index]
    if token not in (PATTERN_REDIRECT_OVERWRITE, PATTERN_REDIRECT_APPEND):
        return None
    if index + 1 >= len(tokens):
        return None
    return token, (tokens[index + 1],), index + 2


def _match_tee(tokens: tuple[str, ...], start: int, end: int) -> tuple[str, tuple[str, ...]] | None:
    if tokens[start] != PATTERN_TEE:
        return None
    return PATTERN_TEE, _non_flag_arguments(tokens, start + 1, end)


def _match_move_or_copy(
    tokens: tuple[str, ...], start: int, end: int
) -> tuple[str, tuple[str, ...]] | None:
    if tokens[start] not in (PATTERN_MOVE, PATTERN_COPY):
        return None
    return tokens[start], _non_flag_arguments(tokens, start + 1, end)


def _match_remove(
    tokens: tuple[str, ...], start: int, end: int
) -> tuple[str, tuple[str, ...]] | None:
    if tokens[start] != PATTERN_REMOVE:
        return None
    return PATTERN_REMOVE, _non_flag_arguments(tokens, start + 1, end)


def _match_sed_in_place(
    tokens: tuple[str, ...], start: int, end: int
) -> tuple[str, tuple[str, ...]] | None:
    """`sed -i` (or `-i<suffix>`, e.g. `-i.bak`): the first non-flag
    argument is `sed`'s own script, never a path, so only the ones after it
    are files. `sed` without `-i` reads and writes nothing (it prints to
    stdout), so it names no pattern at all."""
    if tokens[start] != "sed":
        return None
    arguments = tokens[start + 1 : end]
    if not any(_is_flag(token) and token.startswith("-i") for token in arguments):
        return None
    scripts_and_paths = [token for token in arguments if not _is_flag(token)]
    if len(scripts_and_paths) < _SED_SCRIPT_AND_PATH_COUNT:
        return None
    return PATTERN_SED_IN_PLACE, tuple(scripts_and_paths[1:])


def _match_git_checkout(
    tokens: tuple[str, ...], start: int, end: int
) -> tuple[str, tuple[str, ...]] | None:
    """`git checkout -- <path>...`, the only `checkout` form that overwrites
    a working-tree path; `git checkout <branch>` names no path at all."""
    if tokens[start : start + 3] != ("git", "checkout", "--"):
        return None
    return PATTERN_GIT_CHECKOUT, tokens[start + 3 : end]


def _match_git_restore(
    tokens: tuple[str, ...], start: int, end: int
) -> tuple[str, tuple[str, ...]] | None:
    if tokens[start : start + 2] != ("git", "restore"):
        return None
    return PATTERN_GIT_RESTORE, _non_flag_arguments(tokens, start + 2, end)


_COMMAND_MATCHERS = (
    _match_tee,
    _match_move_or_copy,
    _match_remove,
    _match_sed_in_place,
    _match_git_checkout,
    _match_git_restore,
)


def _first_command_match(
    tokens: tuple[str, ...], start: int, end: int
) -> tuple[str, tuple[str, ...]] | None:
    for matcher in _COMMAND_MATCHERS:
        matched = matcher(tokens, start, end)
        if matched is not None:
            return matched
    return None


def hook_command_paths(command: str) -> tuple[tuple[str, str], ...]:
    """Every `(pattern, path)` pair a Bash `command`'s own recognized write
    patterns name, in the order they appear: a `>`/`>>` redirection
    (including a heredoc target such as `cat > path <<EOF`), `tee`,
    `sed -i`, `mv`, `cp`, `rm`, `git checkout --`, and `git restore`. A path
    is whatever token the command's own grammar puts there -- absolute or
    relative, real or not; `protect` resolves and judges it, this function
    only recognizes the pattern shape (issue #380).

    A command naming none of these patterns -- or one `shlex` cannot
    tokenize as a shell command at all -- yields no pairs, the same as a
    command `protect` allows outright: recognizing a write pattern here is
    a best-effort aid against forgetting the claim, never a security
    boundary. A `python -c ...` one-liner or an opaque script invocation
    stays invisible on purpose (this file's own module docstring; the
    `## Never` section of `specs/protect.spec.md`).
    """
    tokens = _tokenize_command(command)
    pairs: list[tuple[str, str]] = []
    index = 0
    at_command_start = True
    while index < len(tokens):
        token = tokens[index]
        if _is_command_separator(token):
            at_command_start = True
            index += 1
            continue
        if at_command_start:
            end = _command_end(tokens, index)
            matched = _first_command_match(tokens, index, end)
            at_command_start = False
            if matched is not None:
                pattern, paths = matched
                pairs.extend((pattern, path) for path in paths)
                index = end
                continue
        redirect = _match_redirect(tokens, index)
        if redirect is None:
            index += 1
            continue
        pattern, paths, index = redirect
        pairs.append((pattern, paths[0]))
    return tuple(pairs)
