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


@pytest.mark.parametrize(
    ("command", "pairs"),
    [
        pytest.param(
            "cat > src/x.py <<EOF\ncontent\nEOF",
            ((hook_input.PATTERN_REDIRECT_OVERWRITE, "src/x.py"),),
            id="overwrite-redirect-with-a-heredoc-body",
        ),
        pytest.param(
            "echo hi >> docs/log.txt",
            ((hook_input.PATTERN_REDIRECT_APPEND, "docs/log.txt"),),
            id="append-redirect",
        ),
        pytest.param(
            "tee -a docs/log.txt",
            ((hook_input.PATTERN_TEE, "docs/log.txt"),),
            id="tee-skips-its-own-flag",
        ),
        pytest.param(
            "echo hi >",
            (),
            id="a-trailing-redirect-operator-names-no-target",
        ),
        pytest.param(
            "sed -i",
            (),
            id="sed-in-place-with-neither-a-script-nor-a-path",
        ),
        pytest.param(
            "sed -i 's/a/b/'",
            (),
            id="sed-in-place-with-a-script-but-no-path",
        ),
        pytest.param(
            "sed -i 's/a/b/' tests/t.py",
            ((hook_input.PATTERN_SED_IN_PLACE, "tests/t.py"),),
            id="sed-in-place-skips-its-own-script",
        ),
        pytest.param(
            "sed -i.bak 's/a/b/' tests/t.py",
            ((hook_input.PATTERN_SED_IN_PLACE, "tests/t.py"),),
            id="sed-in-place-with-a-backup-suffix",
        ),
        pytest.param(
            "sed -n 's/a/b/p' tests/t.py",
            (),
            id="sed-without-in-place-names-no-pattern",
        ),
        pytest.param(
            "mv src/a.py src/b.py",
            ((hook_input.PATTERN_MOVE, "src/a.py"), (hook_input.PATTERN_MOVE, "src/b.py")),
            id="mv-names-both-its-source-and-its-destination",
        ),
        pytest.param(
            "cp -r src/a.py src/b.py",
            ((hook_input.PATTERN_COPY, "src/b.py"),),
            id="cp-skips-its-own-flag-and-judges-only-its-destination",
        ),
        pytest.param(
            "rm -rf tests/t.py",
            ((hook_input.PATTERN_REMOVE, "tests/t.py"),),
            id="rm-skips-its-own-flag",
        ),
        pytest.param(
            "git checkout -- src/x.py",
            ((hook_input.PATTERN_GIT_CHECKOUT, "src/x.py"),),
            id="git-checkout-double-dash",
        ),
        pytest.param(
            "git checkout main",
            (),
            id="git-checkout-without-double-dash-names-no-path",
        ),
        pytest.param(
            "git restore src/x.py",
            ((hook_input.PATTERN_GIT_RESTORE, "src/x.py"),),
            id="git-restore",
        ),
        pytest.param(
            "grep foo bar.py | sed -i 's/a/b/' baz.py",
            ((hook_input.PATTERN_SED_IN_PLACE, "baz.py"),),
            id="a-pipeline-still-recognizes-its-own-write-stage",
        ),
        pytest.param(
            "rm a.py; mv b.py c.py",
            (
                (hook_input.PATTERN_REMOVE, "a.py"),
                (hook_input.PATTERN_MOVE, "b.py"),
                (hook_input.PATTERN_MOVE, "c.py"),
            ),
            id="semicolon-separates-two-simple-commands",
        ),
        pytest.param(
            "echo start\nrm docs/foo.py\necho done",
            ((hook_input.PATTERN_REMOVE, "docs/foo.py"),),
            id="a-multiline-command-is-one-statement-per-physical-line",
        ),
        pytest.param(
            "git status",
            (),
            id="a-read-only-git-subcommand-names-no-pattern",
        ),
        pytest.param(
            "grep foo bar.py",
            (),
            id="a-command-with-no-recognized-pattern-at-all",
        ),
        pytest.param(
            "python -c \"open('x', 'w').write('y')\"",
            (),
            id="an-opaque-script-invocation-stays-invisible-on-purpose",
        ),
        pytest.param(
            "echo 'unterminated",
            (),
            id="unbalanced-quoting-cannot-even-be-tokenized",
        ),
        pytest.param("", (), id="an-empty-command"),
        pytest.param(
            "echo '>' > f",
            ((hook_input.PATTERN_REDIRECT_OVERWRITE, "f"),),
            id="a-quoted-operator-is-data-not-a-redirect",
        ),
        pytest.param(
            "echo '>>' >> f",
            ((hook_input.PATTERN_REDIRECT_APPEND, "f"),),
            id="a-quoted-append-operator-is-data-not-a-redirect",
        ),
        pytest.param(
            'git commit -m "fix > 2"',
            (),
            id="a-quoted-operator-inside-a-commit-message-names-no-redirect",
        ),
        pytest.param(
            "printf x > $SCRATCH/file",
            (),
            id="an-unquoted-variable-expansion-is-never-judged",
        ),
        pytest.param(
            "cat > /tmp/x.py <<EOF\nrm docs/file\nEOF",
            ((hook_input.PATTERN_REDIRECT_OVERWRITE, "/tmp/x.py"),),
            id="a-heredoc-body-is-data-even-when-it-reads-like-a-write-pattern",
        ),
        pytest.param(
            "gh api repos/example/repo/pulls/1 --input - <<'JSON'\nrm docs/file\nJSON",
            (),
            id="a-quoted-heredoc-delimiter-still-skips-its-own-body",
        ),
        pytest.param(
            "cp README.md /tmp/x",
            ((hook_input.PATTERN_COPY, "/tmp/x"),),
            id="cp-judges-only-its-destination",
        ),
        pytest.param(
            "git restore --source HEAD --staged f",
            ((hook_input.PATTERN_GIT_RESTORE, "f"),),
            id="git-restore-skips-an-options-own-value-argument",
        ),
        pytest.param(
            "git restore --staged",
            (),
            id="git-restore-with-only-options-names-no-path",
        ),
        pytest.param(
            "tee /tmp/log > /dev/null",
            ((hook_input.PATTERN_TEE, "/tmp/log"),),
            id="tees-own-operand-never-swallows-a-trailing-redirect",
        ),
        pytest.param(
            "echo hi > /dev/null",
            (),
            id="a-redirect-to-dev-null-is-never-a-real-write",
        ),
        pytest.param(
            "pytest 2>&1",
            (),
            id="a-file-descriptor-duplication-is-never-a-real-write",
        ),
        pytest.param(
            "uv run --locked pytest -k test_thing",
            (),
            id="uv-run-names-no-pattern",
        ),
        pytest.param(
            "git worktree add ../issue-1 main",
            (),
            id="git-worktree-add-names-no-pattern",
        ),
        pytest.param(
            "git rebase origin/main",
            (),
            id="git-rebase-names-no-pattern",
        ),
        pytest.param(
            "aco protect",
            (),
            id="aco-itself-names-no-pattern",
        ),
        pytest.param(
            "rm f ",
            ((hook_input.PATTERN_REMOVE, "f"),),
            id="trailing-whitespace-after-the-last-word-is-not-a-new-token",
        ),
        pytest.param(
            "cat > /tmp/x <<-EOF\n\tcontent\n\tEOF",
            ((hook_input.PATTERN_REDIRECT_OVERWRITE, "/tmp/x"),),
            id="a-tab-strip-heredoc-still-skips-its-own-indented-body",
        ),
        pytest.param(
            "cat <<",
            (),
            id="a-heredoc-operator-naming-no-delimiter-at-all-registers-no-body",
        ),
        pytest.param(
            "cat << ; echo hi",
            (),
            id="a-heredoc-operator-immediately-followed-by-another-operator-names-no-delimiter",
        ),
        pytest.param(
            "cat > /tmp/x <<EOF\nsome content\nmore content",
            ((hook_input.PATTERN_REDIRECT_OVERWRITE, "/tmp/x"),),
            id="a-heredoc-whose-terminator-never-appears-skips-to-the-end",
        ),
        pytest.param(
            "rm a\\ b.txt",
            ((hook_input.PATTERN_REMOVE, "a b.txt"),),
            id="a-backslash-escaped-space-outside-quotes-stays-in-one-word",
        ),
        pytest.param(
            "rm a\\",
            (),
            id="a-trailing-unescaped-backslash-cannot-even-be-tokenized",
        ),
        pytest.param(
            'rm "a\\"b.txt"',
            ((hook_input.PATTERN_REMOVE, 'a"b.txt'),),
            id="a-backslash-escaped-quote-inside-double-quotes-stays-literal",
        ),
        pytest.param(
            "rm -- -f",
            ((hook_input.PATTERN_REMOVE, "-f"),),
            id="a-literal-end-of-options-marker-un-hides-a-dash-prefixed-operand",
        ),
        pytest.param(
            "cp -t /tmp README.md",
            ((hook_input.PATTERN_COPY, "/tmp"),),
            id="cp-target-directory-is-the-destination-even-though-it-comes-first",
        ),
        pytest.param(
            "sed -i -e p -e d f",
            ((hook_input.PATTERN_SED_IN_PLACE, "f"),),
            id="sed-in-place-skips-every-explicit-scripts-own-value",
        ),
        pytest.param(
            "sed --in-place=.bak f",
            ((hook_input.PATTERN_SED_IN_PLACE, "f"),),
            id="sed-long-in-place-with-a-suffix-and-one-operand-judges-it-as-the-file",
        ),
        pytest.param(
            "sed -i -l 80 -e p /tmp/file",
            ((hook_input.PATTERN_SED_IN_PLACE, "/tmp/file"),),
            id="sed-in-place-skips-its-own-line-length-value",
        ),
        pytest.param(
            "cp --target-directory=/tmp README.md",
            ((hook_input.PATTERN_COPY, "/tmp"),),
            id="cp-target-directory-long-form-is-the-destination",
        ),
        pytest.param(
            "cp -t/tmp README.md",
            ((hook_input.PATTERN_COPY, "/tmp"),),
            id="cp-target-directory-short-form-attached-is-the-destination",
        ),
        pytest.param(
            "git restore -s HEAD f",
            ((hook_input.PATTERN_GIT_RESTORE, "f"),),
            id="git-restore-short-source-flag-skips-its-own-value",
        ),
        pytest.param(
            "git restore --conflict merge f",
            ((hook_input.PATTERN_GIT_RESTORE, "f"),),
            id="git-restore-conflict-flag-skips-its-own-value",
        ),
        pytest.param(
            "git restore --pathspec-from-file p.txt",
            (),
            id="git-restore-pathspec-from-file-is-read-only-and-never-judged",
        ),
        pytest.param(
            "git restore -- -f",
            ((hook_input.PATTERN_GIT_RESTORE, "-f"),),
            id="git-restore-double-dash-un-hides-a-dash-prefixed-path-too",
        ),
        pytest.param(
            "git checkout -f -- f",
            ((hook_input.PATTERN_GIT_CHECKOUT, "f"),),
            id="git-checkout-force-flag-before-double-dash-is-still-recognized",
        ),
        pytest.param(
            "git checkout --ours -- f",
            ((hook_input.PATTERN_GIT_CHECKOUT, "f"),),
            id="git-checkout-ours-flag-before-double-dash-is-still-recognized",
        ),
        pytest.param(
            "git status # note; rm README.md",
            (),
            id="a-comment-hides-everything-after-it-on-the-same-line",
        ),
        pytest.param(
            "echo ok # note > README.md",
            (),
            id="a-comment-hides-an-apparent-redirect-on-the-same-line",
        ),
        pytest.param(
            "rm \\\nREADME.md",
            ((hook_input.PATTERN_REMOVE, "README.md"),),
            id="a-backslash-newline-joins-the-next-physical-line-first",
        ),
        pytest.param(
            "rm README.md \\\n&& echo done",
            ((hook_input.PATTERN_REMOVE, "README.md"),),
            id="a-backslash-newline-right-before-an-operator-names-no-spurious-word",
        ),
        pytest.param(
            "\\\nrm README.md",
            ((hook_input.PATTERN_REMOVE, "README.md"),),
            id="a-backslash-newline-at-the-very-start-of-the-command-joins-too",
        ),
        pytest.param(
            "r\\\nm README.md",
            ((hook_input.PATTERN_REMOVE, "README.md"),),
            id="a-backslash-newline-mid-word-joins-the-two-halves-into-one-word",
        ),
        pytest.param(
            "'rm' README.md",
            ((hook_input.PATTERN_REMOVE, "README.md"),),
            id="a-quoted-command-name-still-executes-and-is-recognized",
        ),
        pytest.param(
            "r\\m README.md",
            ((hook_input.PATTERN_REMOVE, "README.md"),),
            id="a-backslash-escaped-command-name-still-executes-and-is-recognized",
        ),
        pytest.param(
            'rm "a\\\nb"',
            ((hook_input.PATTERN_REMOVE, "ab"),),
            id="a-double-quoted-backslash-newline-is-removed-like-bash-removes-it",
        ),
        pytest.param(
            '"r\\\nm" -rf README.md',
            ((hook_input.PATTERN_REMOVE, "README.md"),),
            id="a-double-quoted-backslash-newline-inside-a-command-name-still-joins",
        ),
        pytest.param(
            "rm 'a\\\nb'",
            ((hook_input.PATTERN_REMOVE, "a\\\nb"),),
            id="a-single-quoted-backslash-newline-stays-literal-never-joined",
        ),
        pytest.param(
            'echo "# not a comment" > f',
            ((hook_input.PATTERN_REDIRECT_OVERWRITE, "f"),),
            id="a-quoted-hash-is-data-never-a-comment",
        ),
        pytest.param(
            "rm f #trailing",
            ((hook_input.PATTERN_REMOVE, "f"),),
            id="a-trailing-comment-does-not-hide-the-word-before-it",
        ),
        pytest.param(
            "cat <<EOF > f",
            ((hook_input.PATTERN_REDIRECT_OVERWRITE, "f"),),
            id="a-redirect-after-a-heredoc-operator-is-still-recognized",
        ),
        pytest.param(
            "sed -i -e 's/a/b/' -- f",
            ((hook_input.PATTERN_SED_IN_PLACE, "f"),),
            id="sed-in-place-honors-a-literal-end-of-options-marker-too",
        ),
        pytest.param(
            "git checkout -- .",
            ((hook_input.PATTERN_GIT_CHECKOUT, "."),),
            id="git-checkout-double-dash-judges-the-current-directory",
        ),
        pytest.param(
            "mv -- a b",
            ((hook_input.PATTERN_MOVE, "a"), (hook_input.PATTERN_MOVE, "b")),
            id="mv-honors-a-literal-end-of-options-marker",
        ),
        pytest.param(
            "tee -- f",
            ((hook_input.PATTERN_TEE, "f"),),
            id="tee-honors-a-literal-end-of-options-marker",
        ),
    ],
)
def test_hook_command_paths_recognizes_every_write_pattern(
    command: str, pairs: tuple[tuple[str, str], ...]
) -> None:
    assert hook_input.hook_command_paths(command) == pairs


@pytest.mark.parametrize(
    ("command", "cwd", "pairs"),
    [
        pytest.param(
            "cd /tmp && printf x > log",
            None,
            ((hook_input.PATTERN_REDIRECT_OVERWRITE, "/tmp/log"),),
            id="a-literal-absolute-cd-changes-the-directory-for-what-follows",
        ),
        pytest.param(
            "cd sub && rm f",
            "/work",
            ((hook_input.PATTERN_REMOVE, "/work/sub/f"),),
            id="a-literal-relative-cd-joins-onto-the-payloads-own-cwd",
        ),
        pytest.param(
            "rm f",
            "/work",
            ((hook_input.PATTERN_REMOVE, "/work/f"),),
            id="cwd-alone-resolves-a-relative-path-with-no-cd-at-all",
        ),
        pytest.param(
            "cd $VAR && rm f",
            "/work",
            (),
            id="an-expandable-cd-target-ends-judgement-for-the-rest-of-the-command",
        ),
        pytest.param(
            "cd - && rm f",
            "/work",
            (),
            id="cd-dash-cannot-be-resolved-and-ends-judgement",
        ),
        pytest.param(
            "cd && rm f",
            "/work",
            (),
            id="a-bare-cd-with-no-operand-cannot-be-resolved-and-ends-judgement",
        ),
        pytest.param(
            "(cd sub; rm f)",
            "/work",
            ((hook_input.PATTERN_REMOVE, "/work/sub/f"),),
            id="a-parenthesised-group-keeps-its-own-cd-that-reverts-at-the-close-paren",
        ),
        pytest.param(
            "(cd sub; rm f); rm g",
            "/work",
            ((hook_input.PATTERN_REMOVE, "/work/sub/f"), (hook_input.PATTERN_REMOVE, "/work/g")),
            id="a-groups-own-cd-never-leaks-past-its-own-close-paren",
        ),
        pytest.param(
            "cd sub | rm f",
            "/work",
            ((hook_input.PATTERN_REMOVE, "/work/f"),),
            id="a-cd-piped-into-another-command-never-changes-the-directory",
        ),
        pytest.param(
            "cd /tmp | rm x",
            None,
            ((hook_input.PATTERN_REMOVE, "x"),),
            id="a-piped-cd-never-changes-the-parent-shells-directory",
        ),
    ],
)
def test_hook_command_paths_tracks_cd_across_the_statement_list(
    command: str, cwd: str | None, pairs: tuple[tuple[str, str], ...]
) -> None:
    assert hook_input.hook_command_paths(command, cwd=cwd) == pairs


@pytest.mark.parametrize("command", [1, None], ids=["int", "none"])
def test_hook_command_paths_raises_for_a_non_string_command(command: object) -> None:
    """`hook_command_paths` is typed `str`, not `str | None`: a non-string
    caller error is a defect, not a value this pure boundary quietly
    tolerates -- `cli._protect_bash` is the one caller, and it already
    guards this before ever calling in (head ruling, issue #380 round 4:
    the `TypeError` is correct here; the CLI boundary is the one that must
    never crash, and it is tested separately in `tests/test_protect.py`)."""
    with pytest.raises(TypeError):
        hook_input.hook_command_paths(command)  # type: ignore[arg-type]
