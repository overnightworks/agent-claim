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
        (("*** Add File: src/new_module.py", "+content"), ("src/new_module.py",)),
        (("*** Delete File: src/old_module.py",), ("src/old_module.py",)),
        (
            ("*** Update File: src/widget.py", "@@", "-old", "+new"),
            ("src/widget.py",),
        ),
        (
            (
                "*** Update File: src/widget.py",
                "*** Move to: src/renamed.py",
                "@@",
                "-old",
                "+new",
            ),
            ("src/widget.py", "src/renamed.py"),
        ),
        (
            (
                "*** Update File: src/widget.py",
                "@@",
                "-old",
                "+new",
                "*** Add File: src/new_module.py",
                "+content",
            ),
            ("src/widget.py", "src/new_module.py"),
        ),
        pytest.param(
            (
                "*** Add File: src/ok.py",
                "+x",
                "  *** Update File: docs/evil.md",
                "@@",
                "-a",
                "+b",
            ),
            ("src/ok.py", "docs/evil.md"),
            id="indented-header-after-add-is-a-real-header",
        ),
        pytest.param(
            (
                "*** Add File: src/ok.py",
                "+x",
                "\t*** Update File: docs/evil.md",
                "@@",
                "-a",
                "+b",
            ),
            ("src/ok.py", "docs/evil.md"),
            id="tab-indented-header-is-a-real-header",
        ),
        pytest.param(
            ("  *** Add File: src/ok.py", "+x"),
            ("src/ok.py",),
            id="indented-first-header-is-a-real-header",
        ),
        pytest.param(
            (
                "*** Update File: src/widget.py",
                "@@",
                "-old",
                "+new",
                " *** Update File: docs/evil.md",
            ),
            ("src/widget.py",),
            id="leading-space-header-inside-an-update-hunk-is-context-not-a-file",
        ),
        pytest.param(
            ('*** Add File: "src/ok.py"', "+x"),
            ('"src/ok.py"',),
            id="a-quoted-path-is-extracted-literally",
        ),
        pytest.param(
            ("*** Add File: ../outside.md", "+x"),
            ("../outside.md",),
            id="a-traversal-path-is-extracted-literally",
        ),
        pytest.param(
            ("*** Add File: /etc/passwd", "+x"),
            ("/etc/passwd",),
            id="an-absolute-path-is-extracted-literally",
        ),
        pytest.param(
            ("*** Add File: src/ok.py  ", "+x"),
            ("src/ok.py",),
            id="trailing-spaces-outside-an-update-hunk-are-trimmed-like-codex",
        ),
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


def test_hook_patch_paths_accepts_an_environment_id_line_after_begin_patch() -> None:
    """Codex's own streaming parser accepts `*** Environment ID: ...` right
    after `Begin Patch` (issue #237 finding 28b, corrected position:
    `begin_patch environment_id? hunk+ end_patch`); without it, every patch
    Codex prefixes this way was denied outright for lacking a path
    (`PATH_REQUIRED` in `cli._protect_write`)."""
    text = _patch_command(
        "*** Environment ID: 11111111-1111-1111-1111-111111111111",
        "*** Update File: src/widget.py",
        "@@",
        "-old",
        "+new",
    )

    assert hook_input.hook_patch_paths(text) == ("src/widget.py",)


def test_hook_patch_paths_rejects_an_environment_id_line_before_begin_patch() -> None:
    """The wrong position fails closed rather than being silently accepted:
    Codex's own grammar never places `Environment ID` before `Begin Patch`,
    so a patch text in that shape names no path at all."""
    text = "*** Environment ID: 11111111-1111-1111-1111-111111111111\n" + _patch_command(
        "*** Update File: src/widget.py", "@@", "-old", "+new"
    )

    assert hook_input.hook_patch_paths(text) == ()


@pytest.mark.parametrize(
    "text",
    [
        pytest.param(
            (
                "*** Begin Patch\r\n"
                "*** Update File: src/widget.py\r\n"
                "@@\r\n"
                "-old\r\n"
                "+new\r\n"
                "*** End Patch\r\n"
            ),
            id="all-crlf-line-endings-like-codex",
        ),
        pytest.param(
            "*** Begin Patch\r\n*** Update File: src/widget.py\n@@\r\n-old\n"
            "+new\r\n*** End Patch\n",
            id="mixed-line-endings-within-one-patch",
        ),
    ],
)
def test_hook_patch_paths_ignores_a_trailing_carriage_return_like_codex(text: str) -> None:
    """Issue #237 finding 28c: a patch is not guaranteed one consistent line
    ending throughout -- each line's own trailing `\\r` is stripped
    independently (`_update_hunk_line`/`_outside_update_hunk_line` already
    right-trim every line), so a patch mixing `\\r\\n` and `\\n` reads the
    same as one that is fully consistent."""
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
