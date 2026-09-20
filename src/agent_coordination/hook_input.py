"""Parses two hook-payload command grammars so `protect` can check every
file path they touch: Codex's `apply_patch` patch text, and a Bash
`command`'s own write patterns (issue #380). Pure text parsing -- no
filesystem, process, network, or coordination knowledge -- kept out of
`cli` (issue #252) so each grammar has one small, independently testable
owner rather than growing inside `protect`'s already large module.
"""

from __future__ import annotations

import posixpath
from enum import Enum, auto
from typing import NamedTuple

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


class _Word(NamedTuple):
    """One token a POSIX shell would see in a Bash `command`.

    `text` is its already-dequoted, literal value. `is_operator` is `True`
    only for a real, unquoted shell operator (`;`, `|`, `&&`, `||`, `>`,
    `>>`, `<`, `<<`, `<<-`, `(`, `)`, or a newline) -- a quoted or
    backslash-escaped occurrence of the same characters is a plain word
    instead, never an operator (issue #380 delta, review finding 1: `echo
    '>' > f` must judge `f`, not the quoted `>`). `is_expandable` is `True`
    when an unquoted (or, inside double quotes, still-substituting) `$`,
    backtick, `~`, `*`, `?`, or `[` appears in the word -- something the
    shell would expand before running it, which this grammar can never
    resolve without executing the command (issue #380 delta, decision 3)."""

    text: str
    is_operator: bool
    is_expandable: bool


class _UnbalancedQuotingError(Exception):
    """Raised internally when a Bash `command` cannot be tokenized at all: an
    unterminated quote or a trailing, unescaped backslash."""


_SINGLE_QUOTE = "'"
_DOUBLE_QUOTE = '"'
_ESCAPE = "\\"
_HORIZONTAL_WHITESPACE = " \t"
_NEWLINE = "\n"
_OPERATOR_START_CHARS = frozenset(";|&<>()\n")
_TWO_CHAR_OPERATORS = frozenset({"&&", "||", ">>", "<<"})
_HEREDOC_OPERATORS = frozenset({"<<", "<<-"})
_HEREDOC_TAB_STRIP_OPERATOR = "<<-"
# Bash still expands `$name`/`` `cmd` `` inside double quotes; only a glob or
# home-directory character (`~`, `*`, `?`, `[`) is literal there.
_UNQUOTED_EXPANSION_TRIGGERS = frozenset("$`~*?[")
_DOUBLE_QUOTED_EXPANSION_TRIGGERS = frozenset("$`")
_DOUBLE_QUOTE_ESCAPABLE = frozenset('$`"\\')


class _CommandScanner:
    """Turns a Bash `command` string into the `_Word` tokens a POSIX shell
    would see, skipping every heredoc body as inert data along the way
    (issue #380 delta, decision 2) -- the one piece of shell grammar this
    file's otherwise flat word/operator splitting needs to get right, since
    a heredoc body can contain text that merely looks like another write
    pattern (`cat > path <<EOF` / `rm docs/file` / `EOF`)."""

    def __init__(self, command: str) -> None:
        self._command = command
        self._length = len(command)
        self._position = 0

    def scan(self) -> tuple[_Word, ...]:
        words: list[_Word] = []
        pending_heredocs: list[tuple[str, bool]] = []
        while self._position < self._length:
            self._skip_horizontal_whitespace()
            if self._position >= self._length:
                break
            character = self._command[self._position]
            if character == _NEWLINE:
                self._position += 1
                if pending_heredocs:
                    self._skip_heredoc_bodies(pending_heredocs)
                    pending_heredocs = []
                words.append(_Word(_NEWLINE, is_operator=True, is_expandable=False))
                continue
            if character in _OPERATOR_START_CHARS:
                operator = self._read_operator()
                words.append(_Word(operator, is_operator=True, is_expandable=False))
                if operator in _HEREDOC_OPERATORS:
                    self._register_heredoc(operator, pending_heredocs)
                continue
            words.append(self._read_word())
        return tuple(words)

    def _skip_horizontal_whitespace(self) -> None:
        while (
            self._position < self._length
            and self._command[self._position] in _HORIZONTAL_WHITESPACE
        ):
            self._position += 1

    def _read_operator(self) -> str:
        two = self._command[self._position : self._position + 2]
        if two in _TWO_CHAR_OPERATORS:
            if two == "<<" and self._command[self._position + 2 : self._position + 3] == "-":
                self._position += 3
                return _HEREDOC_TAB_STRIP_OPERATOR
            self._position += 2
            return two
        one = self._command[self._position]
        self._position += 1
        return one

    def _register_heredoc(self, operator: str, pending: list[tuple[str, bool]]) -> None:
        """A heredoc redirect's own delimiter word, read right after it on
        the same line -- one that names no word at all (`cat <<` with
        nothing following) registers no pending body, since a malformed
        heredoc leaves nothing safe to skip."""
        self._skip_horizontal_whitespace()
        if self._position >= self._length:
            return
        if self._command[self._position] in _OPERATOR_START_CHARS:
            return
        delimiter = self._read_word()
        if delimiter.text:
            pending.append((delimiter.text, operator == _HEREDOC_TAB_STRIP_OPERATOR))

    def _skip_heredoc_bodies(self, pending: list[tuple[str, bool]]) -> None:
        for delimiter, strip_tabs in pending:
            self._position = self._skip_one_heredoc_body(self._position, delimiter, strip_tabs)

    def _skip_one_heredoc_body(self, start: int, delimiter: str, strip_tabs: bool) -> int:
        """Every heredoc-body line from `start` up to and including its own
        terminator line, or to the end of `self._command` when the
        terminator never appears (issue #380 delta): `position` strictly
        advances past a found newline each time around, so this always
        returns from inside the loop -- there is no well-formed input left
        over for a trailing fallback to handle."""
        position = start
        while True:
            newline_index = self._command.find(_NEWLINE, position)
            line_end = newline_index if newline_index != -1 else self._length
            line = self._command[position:line_end]
            candidate = line.lstrip("\t") if strip_tabs else line
            if candidate == delimiter:
                return line_end + 1 if newline_index != -1 else self._length
            if newline_index == -1:
                return self._length
            position = newline_index + 1

    def _read_word(self) -> _Word:
        characters: list[str] = []
        expandable = False
        quote: str | None = None
        while self._position < self._length:
            character = self._command[self._position]
            if quote is None:
                ended = self._read_unquoted_word_character(character, characters)
                if ended is None:
                    break
                expandable = expandable or ended
                if character in (_SINGLE_QUOTE, _DOUBLE_QUOTE):
                    quote = character
                continue
            if character == quote:
                quote = None
                self._position += 1
                continue
            if quote == _SINGLE_QUOTE:
                characters.append(character)
                self._position += 1
                continue
            expandable = self._read_double_quoted_character(characters) or expandable
        if quote is not None:
            raise _UnbalancedQuotingError
        return _Word("".join(characters), is_operator=False, is_expandable=expandable)

    def _read_unquoted_word_character(self, character: str, characters: list[str]) -> bool | None:
        """One unquoted character while reading a word: `None` when
        `character` ends the word (unconsumed), `True`/`False` otherwise for
        whether it made the word expandable. Opening a quote is signalled by
        appending nothing and letting the caller notice `character` is a
        quote mark; entering it still consumes the character here so the
        caller's own position bookkeeping stays in one place."""
        if (
            character in _HORIZONTAL_WHITESPACE
            or character == _NEWLINE
            or character in _OPERATOR_START_CHARS
        ):
            return None
        if character == _ESCAPE:
            if self._position + 1 >= self._length:
                raise _UnbalancedQuotingError
            characters.append(self._command[self._position + 1])
            self._position += 2
            return False
        self._position += 1
        if character in (_SINGLE_QUOTE, _DOUBLE_QUOTE):
            return False
        if character in _UNQUOTED_EXPANSION_TRIGGERS:
            characters.append(character)
            return True
        characters.append(character)
        return False

    def _read_double_quoted_character(self, characters: list[str]) -> bool:
        character = self._command[self._position]
        if (
            character == _ESCAPE
            and self._position + 1 < self._length
            and self._command[self._position + 1] in _DOUBLE_QUOTE_ESCAPABLE
        ):
            characters.append(self._command[self._position + 1])
            self._position += 2
            return False
        self._position += 1
        expandable = character in _DOUBLE_QUOTED_EXPANSION_TRIGGERS
        characters.append(character)
        return expandable


def _tokenize_command(command: str) -> tuple[_Word, ...]:
    """`command` split the way a POSIX shell would see its own words and
    operators, with quote and escape provenance kept on every word (issue
    #380 delta) rather than collapsed away, and every heredoc body skipped
    as data. Unbalanced quoting -- a command `protect` cannot even tokenize
    safely -- yields no tokens at all rather than a best-effort guess."""
    try:
        return _CommandScanner(command).scan()
    except _UnbalancedQuotingError:
        return ()


_COMMAND_SEPARATORS = frozenset({";", "&&", "||", "|"})
_FLAG_PREFIX = "-"
# `sed -i <script> <path>`: at least the script and one path.
_SED_SCRIPT_AND_PATH_COUNT = 2
_CD_COMMAND = "cd"
_CD_PREVIOUS_DIRECTORY = "-"
_DEV_NULL = "/dev/null"
_GIT_RESTORE_VALUE_OPTIONS = frozenset({"--source"})


def _is_command_separator(word: _Word) -> bool:
    """A real, unquoted `;`/`&&`/`||`/`|` operator, or a newline operator (a
    multi-line Bash `command` is one statement per physical line) -- never a
    quoted or escaped word that merely reads the same (issue #380 delta,
    decision 1)."""
    return word.is_operator and (word.text in _COMMAND_SEPARATORS or word.text == _NEWLINE)


def _is_flag(word: _Word) -> bool:
    return word.text.startswith(_FLAG_PREFIX)


def _command_end(tokens: tuple[_Word, ...], start: int) -> int:
    """The index just past the simple command starting at `start` -- its
    next command separator, or the end of `tokens`."""
    end = start
    while end < len(tokens) and not _is_command_separator(tokens[end]):
        end += 1
    return end


def _operand_span_end(tokens: tuple[_Word, ...], start: int, end: int) -> int:
    """Where a simple command's own operand words stop and its first
    redirection begins, bounded by `end` (the whole simple command, since a
    later `>`/`>>` in it is still judged, just not as one of its own
    operands) -- issue #380 delta, gate finding: `tee /tmp/log > /dev/null`
    must never let `tee`'s own operand scan swallow `>` and `/dev/null` as
    if they were its own arguments."""
    span_end = start
    while span_end < end and not tokens[span_end].is_operator:
        span_end += 1
    return span_end


def _non_flag_arguments(words: tuple[_Word, ...]) -> tuple[_Word, ...]:
    return tuple(word for word in words if not _is_flag(word))


def _skip_option_values(
    words: tuple[_Word, ...], *, value_options: frozenset[str]
) -> tuple[_Word, ...]:
    """`_non_flag_arguments`, extended to also drop the separate argument a
    `value_options` flag takes (`--source HEAD`) rather than only the flag
    token itself (`--source=HEAD` is already one token `_is_flag` drops
    whole) -- issue #380 delta, review finding: `git restore --source HEAD
    --staged f` must judge `f` alone, never `HEAD`."""
    result: list[_Word] = []
    skip_next = False
    for word in words:
        if skip_next:
            skip_next = False
            continue
        if _is_flag(word):
            skip_next = word.text in value_options
            continue
        result.append(word)
    return tuple(result)


def _match_redirect(tokens: tuple[_Word, ...], index: int) -> tuple[str, _Word, int] | None:
    """A real, unquoted `>`/`>>` operator immediately followed by its own
    plain-word target -- the one pattern that can appear anywhere in a
    simple command, not only at its start (`cat > path`, a heredoc's own
    `cat > path <<EOF` included). A fd-duplication form (`2>&1`, `>&2`) has
    no word target at all -- its own "target" is another operator token --
    so it never matches here (issue #380 delta, decision 5)."""
    token = tokens[index]
    is_redirect = token.text in (PATTERN_REDIRECT_OVERWRITE, PATTERN_REDIRECT_APPEND)
    if not token.is_operator or not is_redirect:
        return None
    if index + 1 >= len(tokens) or tokens[index + 1].is_operator:
        return None
    return token.text, tokens[index + 1], index + 2


def _match_tee(words: tuple[_Word, ...]) -> tuple[str, tuple[_Word, ...]] | None:
    if words[0].text != PATTERN_TEE:
        return None
    return PATTERN_TEE, _non_flag_arguments(words[1:])


def _match_move(words: tuple[_Word, ...]) -> tuple[str, tuple[_Word, ...]] | None:
    if words[0].text != PATTERN_MOVE:
        return None
    return PATTERN_MOVE, _non_flag_arguments(words[1:])


def _match_copy(words: tuple[_Word, ...]) -> tuple[str, tuple[_Word, ...]] | None:
    """`cp`'s own last non-flag operand is its destination -- the only one it
    actually writes; every earlier operand is a source it only reads (issue
    #380 delta, review finding: `cp README.md /tmp/x` must judge `/tmp/x`
    alone, never the untouched `README.md`)."""
    if words[0].text != PATTERN_COPY:
        return None
    operands = _non_flag_arguments(words[1:])
    return (PATTERN_COPY, operands[-1:]) if operands else None


def _match_remove(words: tuple[_Word, ...]) -> tuple[str, tuple[_Word, ...]] | None:
    if words[0].text != PATTERN_REMOVE:
        return None
    return PATTERN_REMOVE, _non_flag_arguments(words[1:])


def _match_sed_in_place(words: tuple[_Word, ...]) -> tuple[str, tuple[_Word, ...]] | None:
    """`sed -i` (or `-i<suffix>`, e.g. `-i.bak`): the first non-flag
    argument is `sed`'s own script, never a path, so only the ones after it
    are files. `sed` without `-i` reads and writes nothing (it prints to
    stdout), so it names no pattern at all."""
    if words[0].text != "sed":
        return None
    arguments = words[1:]
    if not any(_is_flag(word) and word.text.startswith("-i") for word in arguments):
        return None
    scripts_and_paths = [word for word in arguments if not _is_flag(word)]
    if len(scripts_and_paths) < _SED_SCRIPT_AND_PATH_COUNT:
        return None
    return PATTERN_SED_IN_PLACE, tuple(scripts_and_paths[1:])


_GIT_CHECKOUT_PREFIX_LENGTH = 3  # `git`, `checkout`, `--`
_GIT_RESTORE_PREFIX_LENGTH = 2  # `git`, `restore`


def _match_git_checkout(words: tuple[_Word, ...]) -> tuple[str, tuple[_Word, ...]] | None:
    """`git checkout -- <path>...`, the only `checkout` form that overwrites
    a working-tree path; `git checkout <branch>` names no path at all."""
    prefix = tuple(word.text for word in words[:_GIT_CHECKOUT_PREFIX_LENGTH])
    if len(words) < _GIT_CHECKOUT_PREFIX_LENGTH or prefix != ("git", "checkout", "--"):
        return None
    return PATTERN_GIT_CHECKOUT, words[_GIT_CHECKOUT_PREFIX_LENGTH:]


def _match_git_restore(words: tuple[_Word, ...]) -> tuple[str, tuple[_Word, ...]] | None:
    prefix = tuple(word.text for word in words[:_GIT_RESTORE_PREFIX_LENGTH])
    if len(words) < _GIT_RESTORE_PREFIX_LENGTH or prefix != ("git", "restore"):
        return None
    return PATTERN_GIT_RESTORE, _skip_option_values(
        words[_GIT_RESTORE_PREFIX_LENGTH:], value_options=_GIT_RESTORE_VALUE_OPTIONS
    )


_COMMAND_MATCHERS = (
    _match_tee,
    _match_move,
    _match_copy,
    _match_remove,
    _match_sed_in_place,
    _match_git_checkout,
    _match_git_restore,
)


def _first_command_match(words: tuple[_Word, ...]) -> tuple[str, tuple[_Word, ...]] | None:
    for matcher in _COMMAND_MATCHERS:
        matched = matcher(words)
        if matched is not None:
            return matched
    return None


def _resolved_operand_path(current_directory: str | None, text: str) -> str:
    """`text` (a Bash-recognized pattern's own relative or absolute operand)
    joined against `current_directory` -- the payload's own `cwd`, updated
    by every literal `cd` seen so far (issue #380 delta, decision 4). An
    already-absolute `text` is used as-is; a relative one with no known
    `current_directory` stays relative, so `protect` can still allow it
    outright (PROT-31) rather than deny a path this grammar cannot
    resolve."""
    if text.startswith("/") or current_directory is None:
        return text
    return posixpath.join(current_directory, text)


def _cd_target(words: tuple[_Word, ...]) -> _Word | None:
    """`cd`'s own single operand, or `None` when it has none at all (a bare
    `cd`, which changes to `$HOME` -- unresolvable, issue #380 delta,
    decision 4)."""
    operands = _non_flag_arguments(words[1:])
    return operands[0] if operands else None


def _cd_directory_update(
    current_directory: str | None, words: tuple[_Word, ...]
) -> tuple[str | None, bool]:
    """The directory a `cd` statement leaves the rest of the statement list
    in, and whether its own target could be resolved at all. A missing
    operand, `cd -` (the previous directory), or an expandable target (a
    variable, for one) cannot be resolved without executing the shell, so
    the second element is `False` -- the caller's own cue to end judgement
    for the rest of the command outright, allowing it (issue #380 delta,
    decision 4), rather than keep judging paths against a directory that
    might now be anything."""
    target = _cd_target(words)
    if target is None or target.text == _CD_PREVIOUS_DIRECTORY or target.is_expandable:
        return current_directory, False
    return _resolved_operand_path(current_directory, target.text), True


def _judged_pairs(
    pattern: str, words: tuple[_Word, ...], *, current_directory: str | None
) -> tuple[tuple[str, str], ...]:
    """`(pattern, path)` for every `words` operand a shell would not need to
    expand first -- an expandable one (a variable, a glob, ...) is never
    judged and never denied (issue #380 delta, decision 3): `protect` cannot
    resolve it without running the command."""
    return tuple(
        (pattern, _resolved_operand_path(current_directory, word.text))
        for word in words
        if not word.is_expandable
    )


def _judged_redirect_pair(
    pattern: str, target: _Word, *, current_directory: str | None
) -> tuple[str, str] | None:
    """The one `(pattern, path)` pair a real redirect names, or `None` when
    its target is expandable (decision 3) or is exactly `/dev/null` -- never
    a real file a claim could cover, so naming it in a denial sentence would
    misdescribe the actual write (issue #380 delta, gate finding)."""
    if target.is_expandable or target.text == _DEV_NULL:
        return None
    return pattern, _resolved_operand_path(current_directory, target.text)


def hook_command_paths(command: str, *, cwd: str | None = None) -> tuple[tuple[str, str], ...]:
    """Every `(pattern, path)` pair a Bash `command`'s own recognized write
    patterns name, in the order they appear: a `>`/`>>` redirection
    (including a heredoc target such as `cat > path <<EOF`), `tee`,
    `sed -i`, `mv`, `cp` (its destination only), `rm`, `git checkout --`,
    and `git restore` (skipping its own options and their arguments). A
    relative path resolves against `cwd` -- the payload's own working
    directory -- updated by every literal `cd <path> &&` seen first in the
    same command (issue #380 delta, decision 4); an unresolvable `cd`
    target (a variable, `-`, or no operand at all) ends recognition for the
    rest of the command outright, the same as allowing it. An operand a
    shell would expand first (an unquoted `$name`, backtick, `~`, `*`, `?`,
    or `[`) is never judged either (decision 3): `protect` resolves and
    judges whatever plain-text path remains, this function only recognizes
    the pattern shape and joins it against the known directory (issue #380).

    Everything inside a recognized heredoc body -- between an unquoted
    `<<WORD`/`<<-WORD`/`<<'WORD'` and its terminator line -- is data, never
    scanned for a pattern of its own (decision 2): only the command line
    naming the heredoc is judged.

    A command naming none of these patterns -- or one this grammar cannot
    tokenize as a shell command at all (unbalanced quoting) -- yields no
    pairs, the same as a command `protect` allows outright: recognizing a
    write pattern here is a best-effort aid against forgetting the claim,
    never a security boundary. A `python -c ...` one-liner or an opaque
    script invocation stays invisible on purpose (this file's own module
    docstring; the `## Never` section of `specs/protect.spec.md`).
    """
    tokens = _tokenize_command(command)
    pairs: list[tuple[str, str]] = []
    current_directory = cwd
    index = 0
    at_command_start = True
    while index < len(tokens):
        if _is_command_separator(tokens[index]):
            at_command_start = True
            index += 1
            continue
        if at_command_start:
            end = _command_end(tokens, index)
            operand_end = _operand_span_end(tokens, index, end)
            words = tokens[index:operand_end]
            at_command_start = False
            if words and words[0].text == _CD_COMMAND:
                current_directory, resolved = _cd_directory_update(current_directory, words)
                if not resolved:
                    return tuple(pairs)
                index = operand_end
                continue
            matched = _first_command_match(words) if words else None
            if matched is not None:
                pattern, operand_words = matched
                pairs.extend(
                    _judged_pairs(pattern, operand_words, current_directory=current_directory)
                )
                index = operand_end
                continue
        redirect = _match_redirect(tokens, index)
        if redirect is None:
            index += 1
            continue
        pattern, target, index = redirect
        pair = _judged_redirect_pair(pattern, target, current_directory=current_directory)
        if pair is not None:
            pairs.append(pair)
    return tuple(pairs)
