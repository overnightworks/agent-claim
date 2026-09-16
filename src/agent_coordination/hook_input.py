"""Parses Codex's `apply_patch` patch-text grammar so `protect` can check
every file path a patch touches. Pure text parsing -- no filesystem,
process, network, or coordination knowledge -- kept out of `cli` (issue
#252) so the Codex patch grammar has one small, independently testable
owner rather than growing inside `protect`'s already large module.
"""

from __future__ import annotations

_UPDATE_FILE_PREFIX = "*** Update File: "
_ADD_FILE_PREFIX = "*** Add File: "
_DELETE_FILE_PREFIX = "*** Delete File: "
_MOVE_TO_PREFIX = "*** Move to: "
_FILE_LINE_PREFIXES = (_UPDATE_FILE_PREFIX, _ADD_FILE_PREFIX, _DELETE_FILE_PREFIX)


def hook_patch_paths(text: str) -> tuple[str, ...]:
    """Every file path an `apply_patch` patch text touches, in the order the
    patch lists them.

    An `*** Update File: <path>` line immediately followed by
    `*** Move to: <path>` -- the patch grammar's rename form -- contributes
    both paths. Returns an empty tuple when `text` carries none of that
    grammar: `protect` treats that the same as no path at all rather than
    falling back to a guess at a different shape.
    """
    lines = text.splitlines()
    paths: list[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        prefix = next(
            (candidate for candidate in _FILE_LINE_PREFIXES if line.startswith(candidate)), None
        )
        if prefix is not None:
            paths.append(line[len(prefix) :])
            if (
                prefix == _UPDATE_FILE_PREFIX
                and index + 1 < len(lines)
                and lines[index + 1].startswith(_MOVE_TO_PREFIX)
            ):
                index += 1
                paths.append(lines[index][len(_MOVE_TO_PREFIX) :])
        index += 1
    return tuple(paths)
