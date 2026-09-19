"""`hook_input.hook_patch_paths`: Codex's `apply_patch` patch-text grammar
(issue #237, audit findings 22-28 slice, finding 28). Split from
`tests/test_cli.py`'s own `hook_patch_paths` coverage so the grammar's own
module has one dedicated, independently runnable test file (its production
module already stands alone for exactly this reason, per its docstring).
"""

from __future__ import annotations

import pytest
from cli_fixtures import _patch_command

from agent_coordination import hook_input


@pytest.mark.parametrize(
    ("lines", "paths"),
    [
        pytest.param(
            (
                "*** Update File: src/widget.py",
                "@@",
                "-old",
                "+new",
                "*** End of File",
                "*** Add File: extra.py",
                "+z",
            ),
            ("src/widget.py", "extra.py"),
            id="a-header-right-after-end-of-file-is-still-a-real-header",
        ),
        pytest.param(
            (
                "*** Update File: a.py",
                "@@",
                " *** Update File: fake.py",
                "*** Update File: b.py",
                "@@",
                "-x",
                "+y",
            ),
            ("a.py", "b.py"),
            id="one-space-keeps-it-context-the-next-unindented-line-is-real",
        ),
        pytest.param(
            ("*** Update File: src/widget.py", "@@", "-old", "+new", "*** End of File"),
            ("src/widget.py",),
            id="end-of-file-marker-is-recognized-hunk-content",
        ),
    ],
)
def test_hook_patch_paths_extracts_every_file_line(
    lines: tuple[str, ...], paths: tuple[str, ...]
) -> None:
    assert hook_input.hook_patch_paths(_patch_command(*lines)) == paths


def test_hook_patch_paths_accepts_an_environment_id_line_before_begin_patch() -> None:
    """Codex's own streaming parser accepts `*** Environment ID: ...` as a
    valid start line (issue #237 finding 28b); without it, every patch Codex
    prefixes this way was denied outright for lacking a path (`PATH_REQUIRED`
    in `cli._protect_write`)."""
    text = "*** Environment ID: 11111111-1111-1111-1111-111111111111\n" + _patch_command(
        "*** Update File: src/widget.py", "@@", "-old", "+new"
    )

    assert hook_input.hook_patch_paths(text) == ("src/widget.py",)


def test_hook_patch_paths_ignores_mixed_line_endings_within_one_patch() -> None:
    """Issue #237 finding 28c: a patch is not guaranteed one consistent line
    ending throughout -- each line's own trailing `\\r` is stripped
    independently (`_update_hunk_line`/`_outside_update_hunk_line` already
    right-trim every line), so a patch mixing `\\r\\n` and `\\n` reads the
    same as one that is fully consistent, like the CRLF-only case already
    covered in `tests/test_cli.py`."""
    text = "*** Begin Patch\r\n*** Update File: src/widget.py\n@@\r\n-old\n+new\r\n*** End Patch\n"

    assert hook_input.hook_patch_paths(text) == ("src/widget.py",)


@pytest.mark.parametrize(
    "text",
    [
        "*** Begin Patch\n*** End Patch",
        "not a patch at all",
        "",
        pytest.param(
            _patch_command("*** Add File: a.py", "+x")
            + "\n"
            + _patch_command("*** Add File: b.py", "+y"),
            id="two-begin-patch-blocks",
        ),
        pytest.param(
            _patch_command("*** Add File: a.py", "+x") + "\n*** Add File: b.py",
            id="a-file-line-after-end-patch",
        ),
        pytest.param(
            "*** Begin Patch\nbad\n*** End Patch",
            id="a-line-the-grammar-does-not-admit-outside-any-header",
        ),
        pytest.param(
            _patch_command("*** Update File: src/widget.py", "@@", "bad", "+new"),
            id="an-unrecognized-line-inside-an-update-hunk-is-refused-not-swallowed",
        ),
        pytest.param(
            _patch_command("*** Move to: src/renamed.py"),
            id="move-to-before-any-update-file-header-is-refused",
        ),
        pytest.param(
            _patch_command("*** Add File: ", "+x"),
            id="a-header-with-an-empty-path-is-refused",
        ),
        pytest.param(
            _patch_command("*** Update File: src/widget.py", "*** Move to: ", "@@", "-old", "+new"),
            id="a-move-to-with-an-empty-path-is-refused",
        ),
        pytest.param(
            _patch_command(
                "*** Update File: a.py",
                "@@",
                "-x",
                "\u00a0*** Update File: b.py",
                "@@",
                "-y",
                "+z",
            ),
            id="unicode-whitespace-cannot-smuggle-an-unindented-header",
        ),
    ],
)
def test_hook_patch_paths_returns_empty_for_unrecognized_text(text: str) -> None:
    assert hook_input.hook_patch_paths(text) == ()
