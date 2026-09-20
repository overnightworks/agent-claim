"""Direct `checkout.py` behavior: remote/worktree/branch validation, the
`_git_output` boundary and its `directory` (issue #314), `resolve_path_checkout`
(the path-based checkout resolver `protect` and `rescope` share), dirty-path
reading, `versioned_paths`, trunk landings, and remote-location parsing.
`_validate_checkout` -- the precondition every `claim` call site in `cli.py`
uses -- is exercised directly. Tests that drive these through
`issue_claim.main([...])` stay in `tests/test_cli.py` as CLI-wiring behavior."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from board_fixtures import BASE, request
from cli_fixtures import (
    _fallback_git_output,
    _git_checkout,
    _push_repository_trunk,
    _real_git,
    _real_repository_with_bare_remote,
    _set_agent_identity_env,
    _stub_one_git_call,
)

from agent_coordination import board, checkout, process
from agent_coordination.protocol import ClaimError, ClaimRequest

_LIVE_VERSIONED_PATHS = checkout.versioned_paths
_LIVE_TRUNK_LANDINGS = checkout.trunk_landings
_LIVE_REMOTE_URL = checkout.remote_url


def test_origin_remote_url_reads_the_git_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkout, "remote_url", _LIVE_REMOTE_URL)
    calls: list[list[str]] = []

    def git(arguments: list[str]) -> str:
        calls.append(arguments)
        return "git@github.com:owner/repository.git"

    monkeypatch.setattr(checkout, "_git_output", git)

    assert checkout.origin_remote_url() == "git@github.com:owner/repository.git"
    assert calls == [["config", "--get", "remote.origin.url"]]


def test_remote_url_reads_any_named_remote(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(checkout, "remote_url", _LIVE_REMOTE_URL)
    calls: list[list[str]] = []

    def git(arguments: list[str]) -> str:
        calls.append(arguments)
        return "git@github.com:owner/repository.git"

    monkeypatch.setattr(checkout, "_git_output", git)

    assert checkout.remote_url("upstream") == "git@github.com:owner/repository.git"
    assert calls == [["config", "--get", "remote.upstream.url"]]


def test_scope_directories_detects_a_git_tree(monkeypatch: pytest.MonkeyPatch) -> None:
    def git(arguments: list[str], **_kwargs: object) -> str:
        if arguments == ["cat-file", "-t", "HEAD:docs"]:
            return "tree"
        if arguments == ["cat-file", "-t", "HEAD:README.md"]:
            return "blob"
        raise ClaimError("not a git object")

    monkeypatch.setattr(checkout, "_git_output", git)

    assert checkout._scope_directories(("docs", "README.md")) == ("docs",)


def test_scope_directories_detects_an_untracked_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    (tmp_path / "scratch").mkdir()
    (tmp_path / "file.py").write_text("x\n")

    def git(arguments: list[str], **_kwargs: object) -> str:
        if arguments[:2] == ["cat-file", "-t"]:
            raise ClaimError("not in HEAD")
        if arguments == ["rev-parse", "--show-toplevel"]:
            return str(tmp_path)
        raise ClaimError("unexpected git")

    monkeypatch.setattr(checkout, "_git_output", git)

    assert checkout._scope_directories(("scratch", "file.py")) == ("scratch",)


def test_paths_under_scope_matches_prefix_or_exact_entry() -> None:
    paths = ("LICENSE", "src/a.py", "src/b.py", "docs/a.md")

    assert checkout.paths_under_scope(paths, ("src",)) == ("src/a.py", "src/b.py")
    assert checkout.paths_under_scope(paths, ("LICENSE",)) == ("LICENSE",)
    assert checkout.paths_under_scope(paths, ("src/a.py", "docs")) == ("src/a.py", "docs/a.md")
    assert checkout.paths_under_scope(paths, ("missing",)) == ()


def test_checkout_validation_binds_clean_head_and_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    values = {
        ("rev-parse", "HEAD"): BASE,
        ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main",
        ("branch", "--show-current"): "codex/issue-71-claims",
        ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        ("status", "--porcelain"): "",
    }
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: values[tuple(arguments)]
    )

    checkout._validate_checkout(request())


@pytest.mark.parametrize(
    ("candidate", "values", "message"),
    [
        (
            request(),
            {
                ("rev-parse", "HEAD"): "b" * 40,
                ("branch", "--show-current"): "codex/issue-71-claims",
                ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
                ("rev-parse", "--git-common-dir"): "/repo/.git",
                ("status", "--porcelain"): "",
            },
            "does not match checkout HEAD",
        ),
        (
            request(),
            {
                ("rev-parse", "HEAD"): BASE,
                ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main",
                ("branch", "--show-current"): "other",
                ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
                ("rev-parse", "--git-common-dir"): "/repo/.git",
                ("status", "--porcelain"): "",
            },
            "does not match checkout branch",
        ),
        (
            request(),
            {
                ("rev-parse", "HEAD"): BASE,
                ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main",
                ("branch", "--show-current"): "codex/issue-71-claims",
                ("rev-parse", "--git-dir"): "/repo/.git",
                ("rev-parse", "--git-common-dir"): "/repo/.git",
                ("status", "--porcelain"): "",
            },
            "linked isolated worktree",
        ),
        (
            request(),
            {
                ("rev-parse", "HEAD"): BASE,
                ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main",
                ("branch", "--show-current"): "codex/issue-71-claims",
                ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
                ("rev-parse", "--git-common-dir"): "/repo/.git",
                ("status", "--porcelain"): " M file",
            },
            "before the first worktree edit",
        ),
    ],
)
def test_checkout_validation_rejects_false_or_late_claims(
    monkeypatch: pytest.MonkeyPatch,
    candidate: ClaimRequest,
    values: dict[tuple[str, ...], str],
    message: str,
) -> None:
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: values[tuple(arguments)]
    )

    with pytest.raises(ClaimError, match=message):
        checkout._validate_checkout(candidate)


def test_checkout_validation_names_the_base_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    """The base-mismatch refusal names both SHAs (unchanged) and the repair
    an agent reading it needs: omitting `--base` binds it to checkout HEAD."""
    values = {
        ("rev-parse", "HEAD"): "b" * 40,
        ("branch", "--show-current"): "codex/issue-71-claims",
        ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        ("status", "--porcelain"): "",
    }
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: values[tuple(arguments)]
    )
    candidate = request()

    with pytest.raises(ClaimError) as error:
        checkout._validate_checkout(candidate)

    assert str(error.value) == (
        f"claim base {BASE} does not match checkout HEAD {'b' * 40}; "
        "omit --base to use checkout HEAD"
    )


@pytest.mark.parametrize(
    ("branch", "origin_head"),
    [
        pytest.param("main", "refs/remotes/origin/main", id="hardcoded-main"),
        pytest.param("trunk", "refs/remotes/origin/trunk", id="repository-default-trunk"),
    ],
)
def test_checkout_validation_names_the_isolated_worktree_recipe_for_the_default_branch(
    monkeypatch: pytest.MonkeyPatch,
    branch: str,
    origin_head: str,
) -> None:
    """Claiming from a checkout of the repository's default branch names the
    exact `git worktree add` recipe (#52), not just the rule it violates --
    whether that default is the hardcoded `main` or one read from
    `origin/HEAD` (issue #238: a repository whose default is `trunk` refuses
    a claim from `trunk` the same way)."""
    values = {
        ("rev-parse", "HEAD"): BASE,
        ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): origin_head,
    }
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: values[tuple(arguments)]
    )
    candidate = request(branch=branch)

    with pytest.raises(ClaimError) as error:
        checkout._validate_checkout(candidate)

    assert str(error.value) == (
        "build claims require an isolated non-main worktree branch; "
        f"run {checkout.ISOLATED_WORKTREE_RECIPE}"
    )


def test_checkout_validation_names_the_isolated_worktree_recipe_for_a_shared_checkout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A checkout whose git-dir is the shared common dir (not a linked
    worktree) names the same recipe as the trunk-branch refusal above."""
    values = {
        ("rev-parse", "HEAD"): BASE,
        ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main",
        ("branch", "--show-current"): "codex/issue-71-claims",
        ("rev-parse", "--git-dir"): "/repo/.git",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        ("status", "--porcelain"): "",
    }
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: values[tuple(arguments)]
    )
    candidate = request()

    with pytest.raises(ClaimError) as error:
        checkout._validate_checkout(candidate)

    assert str(error.value) == (
        "build claims require a linked isolated worktree checkout; "
        f"run {checkout.ISOLATED_WORKTREE_RECIPE}"
    )


def test_checkout_validation_names_the_first_three_dirty_paths_and_the_rest_as_a_count(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dirty-tree refusal used to discard `git status --porcelain`'s
    list entirely; it now names the first three changed paths and how many
    more there are (#52)."""
    porcelain = "\n".join(
        [" M src/a.py", " M src/b.py", "?? src/c.py", " M src/d.py", " M src/e.py"]
    )
    values = {
        ("rev-parse", "HEAD"): BASE,
        ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main",
        ("branch", "--show-current"): "codex/issue-71-claims",
        ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        ("status", "--porcelain"): porcelain,
    }
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: values[tuple(arguments)]
    )
    candidate = request()

    with pytest.raises(ClaimError) as error:
        checkout._validate_checkout(candidate)

    assert str(error.value) == (
        "claim must be acquired before the first worktree edit: "
        "src/a.py, src/b.py, src/c.py, and 2 more"
    )


def test_checkout_validation_names_every_dirty_path_when_three_or_fewer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No trailing count when every changed path already fits in the first
    three named."""
    values = {
        ("rev-parse", "HEAD"): BASE,
        ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main",
        ("branch", "--show-current"): "codex/issue-71-claims",
        ("rev-parse", "--git-dir"): "/repo/.git/worktrees/issue-71",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
        ("status", "--porcelain"): " M src/a.py",
    }
    monkeypatch.setattr(
        checkout, "_git_output", lambda arguments, **_kwargs: values[tuple(arguments)]
    )
    candidate = request()

    with pytest.raises(ClaimError) as error:
        checkout._validate_checkout(candidate)

    assert str(error.value) == "claim must be acquired before the first worktree edit: src/a.py"


def _scratch_git_repository(tmp_path: Path) -> Path:
    """An initialized repository with one committed, tracked file -- for
    tests that drive `_dirty_paths` against real `git status --porcelain`
    output instead of a fake standing in for `_git_output` itself."""
    repository = tmp_path / "repo"
    repository.mkdir()
    _real_git(repository, "init", "-q", "-b", "main")
    _real_git(repository, "config", "user.name", "Test")
    _real_git(repository, "config", "user.email", "test@example.com")
    (repository / "README.md").write_text("hello\n")
    _real_git(repository, "add", "README.md")
    _real_git(repository, "commit", "-q", "-m", "initial")
    return repository


def test_dirty_paths_reads_a_modified_tracked_file_from_real_git_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression for the truncated-name bug (#52 follow-up): `_git_output`
    used to `.strip()` its whole decoded output, which ate the leading space
    of a modified file's ` M path` porcelain line before `_dirty_paths`
    sliced off the fixed three-character status prefix -- `README.md` came
    back as `EADME.md`. A fake that hands `_dirty_paths` a hand-typed string
    with the leading space intact cannot catch this; only the real reader
    against real git output can."""
    repository = _scratch_git_repository(tmp_path)
    (repository / "README.md").write_text("hello\nmodified\n")
    monkeypatch.chdir(repository)

    assert checkout._dirty_paths(checkout._git_output(["status", "--porcelain"])) == ("README.md",)


def test_dirty_paths_reads_an_untracked_file_from_real_git_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The untracked `?? path` line has no leading space to lose, which is
    why the truncation bug above went unnoticed."""
    repository = _scratch_git_repository(tmp_path)
    (repository / "extra.txt").write_text("new\n")
    monkeypatch.chdir(repository)

    assert checkout._dirty_paths(checkout._git_output(["status", "--porcelain"])) == ("extra.txt",)


def test_dirty_paths_reads_a_rename_from_real_git_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_dirty_paths`'s docstring claims a rename's `old -> new` line is
    handled; prove it against a real rename rather than a hand-typed line
    that could not tell us whether the code actually handles it."""
    repository = _scratch_git_repository(tmp_path)
    _real_git(repository, "mv", "README.md", "RENAMED.md")
    monkeypatch.chdir(repository)

    assert checkout._dirty_paths(checkout._git_output(["status", "--porcelain"])) == (
        "README.md -> RENAMED.md",
    )


def test_git_output_directory_runs_git_dash_c_in_that_directory(tmp_path: Path) -> None:
    """`directory` (issue #314) must reach the real `git` process as `-C`,
    not merely be accepted and ignored -- the one fact every path-based
    resolution downstream of `_git_output` depends on."""
    repository = _scratch_git_repository(tmp_path)

    assert checkout._git_output(["rev-parse", "--show-toplevel"], directory=repository) == str(
        repository.resolve()
    )


def test_git_output_denies_loud_on_a_non_standard_os_error_launching_git(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`run_captured` translates a missing executable and a timeout to their
    own typed errors, but leaves every other OS-level launch failure --
    permission denied, out of file descriptors, `-C` naming a non-directory
    -- as a raw `OSError` (issue #314 gate G's follow-up). Every
    `_git_output` caller judges a checkout for a security decision, so this
    must fail closed with `ClaimError` too, never an uncaught traceback out
    of `protect`'s hook boundary."""

    def raises_os_error(command: list[str], **_kwargs: object) -> object:
        raise PermissionError("denied")

    monkeypatch.setattr(process, "run_captured", raises_os_error)

    with pytest.raises(ClaimError, match="git failed to launch: denied"):
        checkout._git_output(["rev-parse", "--verify", "HEAD"])


def _repo_with_linked_worktree(tmp_path: Path) -> tuple[Path, Path]:
    """A real repository with a linked, isolated worktree on a feature
    branch (issue #314) -- `main` is the shared checkout, `worktree` is what
    a claim actually builds in, via the same `git worktree add` recipe
    `checkout.ISOLATED_WORKTREE_RECIPE` documents."""
    main = _scratch_git_repository(tmp_path)
    worktree = tmp_path / "repo-worktrees" / "issue-1-widget"
    worktree.parent.mkdir(parents=True)
    _real_git(main, "worktree", "add", "-q", str(worktree), "-b", "codex/issue-1-widget")
    return main, worktree


def test_resolve_path_checkout_reads_the_linked_worktree_owning_a_directory(
    tmp_path: Path,
) -> None:
    _main, worktree = _repo_with_linked_worktree(tmp_path)
    (worktree / "src").mkdir()

    resolved = checkout.resolve_path_checkout(worktree / "src")

    assert resolved == checkout.PathCheckout(
        toplevel=worktree.resolve(),
        branch="codex/issue-1-widget",
        kind=checkout.CheckoutKind.LINKED_WORKTREE,
        common_directory=(tmp_path / "repo" / ".git").resolve(),
        has_commit=True,
    )


def test_resolve_path_checkout_reads_the_main_checkout_owning_a_directory(
    tmp_path: Path,
) -> None:
    main, _worktree = _repo_with_linked_worktree(tmp_path)

    resolved = checkout.resolve_path_checkout(main)

    assert resolved == checkout.PathCheckout(
        toplevel=main.resolve(),
        branch="main",
        kind=checkout.CheckoutKind.MAIN,
        common_directory=(main / ".git").resolve(),
        has_commit=True,
    )


def test_resolve_path_checkout_is_independent_of_the_calling_process_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point of issue #314: resolving from `directory` rather than
    an implicit process cwd means the same directory resolves the same way
    regardless of where the test process itself is standing."""
    _main, worktree = _repo_with_linked_worktree(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    resolved = checkout.resolve_path_checkout(worktree)

    assert resolved is not None
    assert resolved.kind is checkout.CheckoutKind.LINKED_WORKTREE
    assert resolved.branch == "codex/issue-1-widget"


def test_resolve_path_checkout_denies_a_directory_outside_every_repository(
    tmp_path: Path,
) -> None:
    outside = tmp_path / "not-a-repo"
    outside.mkdir()

    assert checkout.resolve_path_checkout(outside) is None


_ISOLATED_NON_MAIN_BRANCH_SENTENCE = (
    "build claims require an isolated non-main worktree branch; "
    "run this command from this claim's own worktree, not the primary checkout"
)

_FOREIGN_TOPLEVEL = Path("/foreign-worktree")


@pytest.mark.parametrize(
    ("toplevel", "branch", "kind", "origin_head", "expected"),
    [
        pytest.param(
            Path("/repo"),
            "main",
            checkout.CheckoutKind.LINKED_WORKTREE,
            "refs/remotes/origin/main",
            _ISOLATED_NON_MAIN_BRANCH_SENTENCE,
            id="trunk-branch-names-none",
        ),
        pytest.param(
            Path("/repo"),
            "codex/issue-211-worktree-repair-sentence",
            checkout.CheckoutKind.MAIN,
            "refs/remotes/origin/main",
            "build claims require a linked isolated worktree checkout; "
            "run this command from this claim's own worktree on "
            "'codex/issue-211-worktree-repair-sentence', not the primary checkout",
            id="known-branch-named",
        ),
        pytest.param(
            Path("/repo"),
            "master",
            checkout.CheckoutKind.LINKED_WORKTREE,
            "refs/remotes/origin/master",
            _ISOLATED_NON_MAIN_BRANCH_SENTENCE,
            id="master-default-branch",
        ),
        pytest.param(
            Path("/repo"),
            "trunk",
            checkout.CheckoutKind.LINKED_WORKTREE,
            "refs/remotes/origin/trunk",
            _ISOLATED_NON_MAIN_BRANCH_SENTENCE,
            id="trunk-default-branch",
        ),
        pytest.param(
            _FOREIGN_TOPLEVEL,
            "trunk",
            checkout.CheckoutKind.LINKED_WORKTREE,
            "refs/remotes/origin/trunk",
            _ISOLATED_NON_MAIN_BRANCH_SENTENCE,
            id="foreign-checkout-default-differs-from-repo",
        ),
        pytest.param(
            Path("/repo"),
            "codex/issue-72-widget",
            checkout.CheckoutKind.LINKED_WORKTREE,
            None,
            checkout.DEFAULT_BRANCH_UNKNOWN_REASON,
            id="unresolved-origin-head",
        ),
    ],
)
def test_refuse_shared_checkout_matrix(
    monkeypatch: pytest.MonkeyPatch,
    toplevel: Path,
    branch: str,
    kind: checkout.CheckoutKind,
    origin_head: str | None,
    expected: str,
) -> None:
    """`rescope`'s own worktree-isolation refusal (issue #314 repeat gate,
    finding 3) resolves the repository's default branch the same way
    `protect` does -- not just the hardcoded `main`/`master` fallback, but
    any `origin/HEAD` a real clone can record (`master`, `trunk`), and never
    falls back to that guess when `origin/HEAD` cannot be resolved at all.
    `origin_head` is read *from `path_checkout.toplevel`* (issue #314): the
    `foreign-checkout-default-differs-from-repo` row registers `/repo`'s own
    default as `main` alongside `_FOREIGN_TOPLEVEL`'s own default as `trunk`
    in the same fake, so a resolver that accidentally asked `/repo` instead
    of the payload's own resolved toplevel would compare branch `trunk`
    against default `main`, never raise, and fail this row outright --
    unlike a fake with one single, ambient default that could never catch
    that mistake.

    `RETURN_TO_CLAIM` is used throughout: `rescope` acts on a claim whose
    worktree already exists, so recommending the `git worktree add` recipe
    would build a second, foreign one. On the trunk branch no other branch
    is known here to name, so `RETURN_TO_CLAIM` points back at the claim's
    own worktree without inventing one; checked out directly on a real
    branch inside the shared (non-linked) checkout, that branch is already
    known -- it is the same branch the caller resolved its identity from --
    so `RETURN_TO_CLAIM` names it instead of leaving the sentence
    branch-less."""
    origin_head_by_toplevel: dict[Path, str] = {}
    if toplevel != Path("/repo"):
        # The foreign-checkout row proves directory-scoped resolution: `/repo`
        # keeps its own default registered here too, so a resolver that
        # accidentally read `/repo` instead of the payload's own toplevel
        # sees a real (wrong) answer rather than an absent-key crash that
        # would pass for an unrelated reason.
        origin_head_by_toplevel[Path("/repo")] = "refs/remotes/origin/main"
    if origin_head is not None:
        origin_head_by_toplevel[toplevel] = origin_head

    def git(arguments: list[str], *, directory: Path | None = None) -> str:
        if arguments != ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"]:
            raise AssertionError(f"unexpected git read: {arguments}")
        if directory not in origin_head_by_toplevel:
            raise ClaimError("unknown git failure")
        return origin_head_by_toplevel[directory]

    monkeypatch.setattr(checkout, "_git_output", git)
    path_checkout = checkout.PathCheckout(
        toplevel=toplevel,
        branch=branch,
        kind=kind,
        common_directory=toplevel / ".git",
        has_commit=True,
    )

    with pytest.raises(ClaimError) as error:
        checkout._refuse_shared_checkout(
            path_checkout, repair=checkout.WorktreeRepair.RETURN_TO_CLAIM
        )

    assert str(error.value) == expected


@pytest.mark.parametrize(
    ("branch", "denied"),
    [("main", True), ("master", True), ("trunk", False)],
)
@pytest.mark.parametrize("origin_head_empty", [False, True], ids=["raises", "empty"])
def test_claim_default_branch_fallback_denies_only_main_and_master(
    monkeypatch: pytest.MonkeyPatch,
    origin_head_empty: bool,
    branch: str,
    denied: bool,
) -> None:
    """When `origin/HEAD` cannot be resolved, `claim`'s fallback (issue #238,
    Grok review) still denies exactly the historical `{"main", "master"}`
    guess and nothing else -- `trunk` is not treated as default without a
    resolved `origin/HEAD`, so deleting `DEFAULT_BRANCH_FALLBACK` would fail
    this test by letting `main`/`master` through instead. Proven with both
    the fake's raising shape (git's real behaviour, measured locally) and an
    empty resolved name, so both routes to "unresolved" are pinned."""
    values = _git_checkout(branch=branch)
    monkeypatch.setattr(
        checkout, "_git_output", _fallback_git_output(values, origin_head_empty=origin_head_empty)
    )
    candidate = request(branch=branch)

    if not denied:
        checkout._validate_checkout(candidate)
        return

    with pytest.raises(ClaimError, match="isolated non-main worktree branch"):
        checkout._validate_checkout(candidate)


def test_versioned_paths_reads_nul_terminated_ls_files_without_stripping(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[list[str]] = []

    def run(arguments, **kwargs):
        observed.append(arguments)
        return subprocess.CompletedProcess(
            arguments, 0, stdout=b" foo.py\0bar.py\0 foo.py\0", stderr=b""
        )

    monkeypatch.setattr(subprocess, "run", run)

    assert _LIVE_VERSIONED_PATHS() == (" foo.py", "bar.py")
    assert observed == [["git", "ls-files", "-z", "--full-name"]]


@pytest.mark.parametrize(
    "git_call",
    [
        pytest.param(_LIVE_VERSIONED_PATHS, id="versioned-paths"),
        pytest.param(checkout.origin_remote_url, id="origin-remote-url"),
        pytest.param(
            lambda: checkout.path_is_tracked(board.CONFIG_PATH.as_posix()), id="path-is-tracked"
        ),
    ],
)
@pytest.mark.parametrize(
    ("raised", "match"),
    [
        pytest.param(
            FileNotFoundError("git"), "git is required for issue claims", id="missing-executable"
        ),
        pytest.param(
            subprocess.TimeoutExpired(["git"], process.DEFAULT_TIMEOUT_SECONDS),
            "git timed out while validating the build checkout",
            id="timed-out",
        ),
    ],
)
def test_checkout_git_calls_fail_loud_when_git_is_missing_or_times_out(
    monkeypatch: pytest.MonkeyPatch,
    git_call: Callable[[], object],
    raised: Exception,
    match: str,
) -> None:
    """`versioned_paths`, `origin_remote_url`, and `path_is_tracked` -- all
    direct `subprocess.run` callers (`_git_output` backs `origin_remote_url`)
    -- must translate a missing executable or a timeout to the same
    `ClaimError` text."""
    # `_stub_canonical_remote` (autouse) replaces `checkout.remote_url` with a
    # fixed string so every other store-command test skips a real git call;
    # `origin_remote_url` looks that name up dynamically, so this test must
    # restore the live implementation to actually reach `subprocess.run`.
    monkeypatch.setattr(checkout, "remote_url", _LIVE_REMOTE_URL)

    def fails(*_arguments, **_kwargs):
        raise raised

    monkeypatch.setattr(subprocess, "run", fails)
    with pytest.raises(ClaimError, match=match):
        git_call()


@pytest.mark.parametrize(
    "git_call",
    [
        pytest.param(_LIVE_VERSIONED_PATHS, id="versioned-paths"),
        pytest.param(
            lambda: checkout.path_is_tracked(board.CONFIG_PATH.as_posix()), id="path-is-tracked"
        ),
    ],
)
def test_checkout_git_calls_fail_loud_on_a_nonzero_git_exit(
    monkeypatch: pytest.MonkeyPatch, git_call: Callable[[], object]
) -> None:
    """`versioned_paths` and `path_is_tracked` both translate a real git
    failure exit -- 128 here, outside a git repository -- to the same
    `ClaimError` detail (issue #315 review): `path_is_tracked`'s own exit-1
    "not tracked" reading is proven separately by
    `test_path_is_tracked_reads_the_git_ls_files_exit_status`."""

    def failed(arguments, **_kwargs):
        return subprocess.CompletedProcess(
            arguments, 128, stdout=b"", stderr=b"fatal: not a git repository\n"
        )

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(ClaimError, match="fatal: not a git repository"):
        git_call()


@pytest.mark.parametrize(
    ("exit_status", "expected"),
    [
        pytest.param(0, True, id="tracked"),
        pytest.param(1, False, id="untracked-or-ignored"),
    ],
)
def test_path_is_tracked_reads_the_git_ls_files_exit_status(
    monkeypatch: pytest.MonkeyPatch, exit_status: int, expected: bool
) -> None:
    """`git ls-files --error-unmatch` exits 0 for a path git tracks and 1
    for any path it does not -- absent, merely untracked, and ignored alike
    (issue #315): the caller never has to tell those apart."""
    board_config_path = board.CONFIG_PATH.as_posix()
    observed: list[list[str]] = []

    def run(arguments, **_kwargs):
        observed.append(arguments)
        return subprocess.CompletedProcess(arguments, exit_status, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", run)

    assert checkout.path_is_tracked(board_config_path) is expected
    assert observed == [["git", "ls-files", "--error-unmatch", "--", board_config_path]]


def _untracked_board_config(repository: Path) -> None:
    config = repository / board.CONFIG_PATH.as_posix()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text("version = 1\n")


def _write_gitignore_for_dot_directories(repository: Path) -> None:
    (repository / ".gitignore").write_text(".*/\n")
    _real_git(repository, "add", ".gitignore")
    _real_git(repository, "commit", "-q", "-m", "ignore dot directories")


def _ignored_board_config(repository: Path) -> None:
    _write_gitignore_for_dot_directories(repository)
    _untracked_board_config(repository)


def _tracked_board_config(repository: Path) -> None:
    _untracked_board_config(repository)
    _real_git(repository, "add", board.CONFIG_PATH.as_posix())
    _real_git(repository, "commit", "-q", "-m", "add board config")


def _tracked_but_ignored_board_config(repository: Path) -> None:
    _write_gitignore_for_dot_directories(repository)
    _untracked_board_config(repository)
    _real_git(repository, "add", "-f", board.CONFIG_PATH.as_posix())
    _real_git(repository, "commit", "-q", "-m", "add board config despite ignore")


@pytest.mark.parametrize(
    ("setup", "expected"),
    [
        pytest.param(lambda _repository: None, False, id="absent"),
        pytest.param(_untracked_board_config, False, id="untracked"),
        pytest.param(_ignored_board_config, False, id="ignored-via-gitignore"),
        pytest.param(_tracked_board_config, True, id="tracked"),
        pytest.param(_tracked_but_ignored_board_config, True, id="tracked-but-ignored"),
    ],
)
def test_path_is_tracked_reads_real_git_index_and_ignore_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    setup: Callable[[Path], None],
    expected: bool,
) -> None:
    """`path_is_tracked` against real git filesystem/index state, not a
    hand-typed exit code (issue #315 review): absent, merely untracked, and
    `.gitignore`-ignored (`.*/`, the pattern that hid `.agent-claim/` in the
    field checkout the issue reports) all read `False`; a tracked file reads
    `True` even when a later `.gitignore` pattern would also match it, since
    `git ls-files --error-unmatch` answers from the index, not the ignore
    rules -- the `git add -f` repair this issue's refusal names must keep
    working after it runs."""
    repository = _scratch_git_repository(tmp_path)
    setup(repository)
    monkeypatch.chdir(repository)

    assert checkout.path_is_tracked(board.CONFIG_PATH.as_posix()) is expected


def _fake_trunk_log_record(*fields: str) -> str:
    """One fake `git log -z` trunk-landing record: `fields` joined by
    `checkout._TRUNK_LANDING_FIELD_SEPARATOR`, terminated by that same
    separator -- real `git log -z` framing, where the record terminator and
    the field separator are the same NUL byte."""
    separator = checkout._TRUNK_LANDING_FIELD_SEPARATOR
    return separator.join(fields) + separator


def test_trunk_landings_read_the_named_remotes_trunk_not_the_work_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[list[str]] = []

    def git_output(arguments: list[str], **_kwargs: object) -> str:
        observed.append(arguments)
        if arguments[:3] == ["symbolic-ref", "--quiet", "refs/remotes/hub/HEAD"]:
            return "refs/remotes/hub/main"
        if arguments[0] == "log":
            assert arguments[-3:] == ["-n", "20", "refs/remotes/hub/main"]
            return _fake_trunk_log_record(
                "sha1", "2026-08-29T00:00:00+00:00", "", ""
            ) + _fake_trunk_log_record("sha2", "2026-08-30T00:00:00Z", "#10", "")
        raise AssertionError(arguments)

    monkeypatch.setattr(checkout, "_git_output", git_output)
    landings = _LIVE_TRUNK_LANDINGS("hub", 20)

    assert landings == (
        checkout.TrunkLanding("sha1", datetime(2026, 8, 29, tzinfo=UTC), None),
        checkout.TrunkLanding(
            "sha2", datetime(2026, 8, 30, tzinfo=UTC), board.TrunkWorkItemClassification((10,))
        ),
    )
    # Issue #304 proof 4: `hub`, never a hardcoded `origin`, reaches every
    # git call this read makes.
    assert not any("origin" in argument for call in observed for argument in call)


def test_trunk_landings_with_fetch_refreshes_the_remote_first(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #397: `fetch=True` -- `release --merged <pr>`'s own
    merge-commit verification under `storage = github` -- refreshes
    `remote`'s remote-tracking ref before the walk, so a commit GitHub just
    reported merged is visible even when nothing else in this checkout
    fetched it yet."""
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    clone = tmp_path / "clone"
    _real_git(tmp_path, "clone", "-q", str(tmp_path / "remote.git"), str(clone))
    (repo / "feature.txt").write_text("feature\n")
    _real_git(repo, "add", "feature.txt")
    _real_git(repo, "commit", "-q", "-m", "later work", "-m", "Work-Item: #10")
    _push_repository_trunk(repo, "origin")
    monkeypatch.chdir(clone)

    landings = _LIVE_TRUNK_LANDINGS("origin", 20, fetch=True)

    assert [landing.classification for landing in landings] == [
        None,
        board.TrunkWorkItemClassification((10,)),
    ]


def test_trunk_landings_with_fetch_fails_loud_when_the_fetch_itself_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    monkeypatch.chdir(repo)
    _stub_one_git_call(
        monkeypatch, ["fetch", "origin"], exit_status=1, stderr="fatal: could not read from remote"
    )

    with pytest.raises(ClaimError, match="could not read from remote"):
        checkout.trunk_landings("origin", 20, fetch=True)


def test_trunk_ref_fails_loud_when_no_candidate_branch_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither the symbolic ref nor any of the default-branch-name candidates
    resolving must fail loud rather than silently ruling every candidate's age
    as unknown."""

    def git_output(_arguments: list[str], **_kwargs: object) -> str:
        raise ClaimError("fatal: not a git repository")

    monkeypatch.setattr(checkout, "_git_output", git_output)
    with pytest.raises(ClaimError, match="cannot determine the main branch for ruling age"):
        _LIVE_TRUNK_LANDINGS("hub", 20)


def test_trunk_ref_falls_back_to_the_local_branch_name_when_remote_head_was_never_recorded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A clone that never ran `git remote set-head` still resolves through
    the historical `{main, master}` guess (issue #238), generalized to the
    caller's own remote name rather than `origin` alone (issue #304)."""

    def git_output(arguments: list[str], **_kwargs: object) -> str:
        if arguments == ["symbolic-ref", "--quiet", "refs/remotes/hub/HEAD"]:
            raise ClaimError("unknown git failure")
        if arguments == ["rev-parse", "--verify", "refs/remotes/hub/main"]:
            raise ClaimError("fatal: no such ref")
        if arguments == ["rev-parse", "--verify", "refs/remotes/hub/master"]:
            raise ClaimError("fatal: no such ref")
        if arguments == ["rev-parse", "--verify", "main"]:
            return "deadbeef"
        if arguments[0] == "log":
            assert arguments[-1] == "main"
            return ""
        raise AssertionError(arguments)

    monkeypatch.setattr(checkout, "_git_output", git_output)
    assert _LIVE_TRUNK_LANDINGS("hub", 20) == ()


def test_trunk_landings_is_empty_when_trunk_has_no_first_parent_landings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def git_output(arguments: list[str], **_kwargs: object) -> str:
        if arguments[:3] == ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"]:
            return "refs/remotes/origin/main"
        if arguments[0] == "log":
            return ""
        raise AssertionError(arguments)

    monkeypatch.setattr(checkout, "_git_output", git_output)
    assert _LIVE_TRUNK_LANDINGS("origin", 20) == ()


@pytest.mark.parametrize(
    "raw_commit_time",
    [
        pytest.param("not-a-timestamp", id="unparsable"),
        pytest.param("2026-08-29T00:00:00", id="missing-offset"),
    ],
)
def test_trunk_landings_fails_loud_on_a_malformed_commit_timestamp(
    monkeypatch: pytest.MonkeyPatch, raw_commit_time: str
) -> None:
    """Neither an unparsable `%cI` line nor one git left offset-naive (both
    would only occur if git itself misbehaved) may silently produce a wrong
    ruling age; both fail loud with the same diagnostic."""

    def git_output(arguments: list[str], **_kwargs: object) -> str:
        if arguments[:3] == ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"]:
            return "refs/remotes/origin/main"
        if arguments[0] == "log":
            return _fake_trunk_log_record("sha1", raw_commit_time, "", "")
        raise AssertionError(arguments)

    monkeypatch.setattr(checkout, "_git_output", git_output)
    with pytest.raises(ClaimError, match="git returned a malformed trunk landing timestamp"):
        _LIVE_TRUNK_LANDINGS("origin", 20)


def test_trunk_landings_fails_loud_on_a_log_stream_that_is_not_nul_framed_records(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A raw `git log -z` stream always ends in the same NUL that separates
    each record's own four fields (`_fake_trunk_log_record`); anything else
    -- here, a caller that fed back plain newline-joined text -- is git (or
    the fake) misbehaving, not a shape this reads silently."""

    def git_output(arguments: list[str], **_kwargs: object) -> str:
        if arguments[:3] == ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"]:
            return "refs/remotes/origin/main"
        if arguments[0] == "log":
            return "sha1\x00not-nul-terminated"
        raise AssertionError(arguments)

    monkeypatch.setattr(checkout, "_git_output", git_output)
    with pytest.raises(ClaimError, match="git returned a malformed trunk landing log"):
        _LIVE_TRUNK_LANDINGS("origin", 20)


def _minimal_pushed_repository(tmp_path: Path) -> Path:
    """A `hub`-remote worktree with one `main`, empty of any commit -- the
    common setup every real-`git` trunk-landing test that doesn't need the
    shared five-proof history (`_trunk_history_repository`) builds on."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _real_git(remote, "init", "-q", "--bare", "-b", "main")
    repo = tmp_path / "repo"
    repo.mkdir()
    _real_git(repo, "init", "-q", "-b", "main")
    _real_git(repo, "config", "user.name", "Test")
    _real_git(repo, "config", "user.email", "test@example.com")
    _real_git(repo, "config", "commit.gpgsign", "false")
    _real_git(repo, "remote", "add", "hub", str(remote))
    return repo


def _push_to_hub(repo: Path) -> None:
    _real_git(repo, "push", "-q", "hub", "main")
    _real_git(repo, "remote", "set-head", "hub", "main")


@pytest.mark.parametrize(
    "byte",
    ["\x1f", "\x1e", "\x01"],
    ids=["unit-separator", "record-separator", "start-of-heading"],
)
def test_trunk_landings_classifies_a_control_byte_inside_a_trailer_value_as_one_literal_defect(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, byte: str
) -> None:
    """Issue #304 review, finding B1: git never escapes `\\x1f`, `\\x1e`, or
    `\\x01` inside a trailer value -- exactly the bytes the historical
    `\\x1f`-separated framing used to split repeated trailer values -- so a
    value that happens to contain one of them must read back as the one
    literal value git actually recorded, never as two fabricated work items.
    The NUL/newline framing reads it as data: `trunk_commit_classification`
    then reports that single literal value as a `ClassificationDefect`
    (LAND-03) rather than raising and aborting the whole read -- the commit
    lands neither #12 nor #13."""
    repo = _minimal_pushed_repository(tmp_path)
    (repo / "f.txt").write_text("content\n")
    _real_git(repo, "add", "f.txt")
    value = f"#12{byte}#13"
    _real_git(repo, "commit", "-q", "-m", "change", "-m", f"Work-Item: {value}")
    _push_to_hub(repo)
    monkeypatch.chdir(repo)

    [landing] = checkout.trunk_landings("hub", 20)

    assert landing.classification == board.ClassificationDefect(
        f"carries `Work-Item: {value}`; a trunk trailer names #n, aco-xxxxxx, or the bare number n"
    )


def _trunk_history_repository(tmp_path: Path) -> Path:
    """A worktree pushed to a `hub` remote (never `origin`, issue #304
    proof 4) whose `main` carries, in first-parent order: a plain initial
    commit, a merge commit trailer-classified `Work-Item: #10`, a squash
    commit whose trailer block repeats `Work-Item:` twice, a commit landed
    through a real `git rebase` and trailer-classified `No-Item: docs`, and
    a commit whose `Work-Item:` line sits in prose, never its own trailer
    block. A `sidebranch` ref never joins that first-parent line. One
    history serves every one of the five proofs at once, since building a
    real git repository per proof would only repeat the same setup."""
    repo, _remote = _real_repository_with_bare_remote(tmp_path, remote_name="hub")
    (repo / "base.txt").write_text("base\n")
    _real_git(repo, "add", "base.txt")
    _real_git(repo, "commit", "-q", "-m", "initial")

    # A merge commit whose own message carries the trailer block.
    _real_git(repo, "checkout", "-q", "-b", "feature")
    (repo / "feature.txt").write_text("feature\n")
    _real_git(repo, "add", "feature.txt")
    _real_git(repo, "commit", "-q", "-m", "feature work")
    _real_git(repo, "checkout", "-q", "main")
    _real_git(
        repo, "merge", "-q", "--no-ff", "-m", "Merge feature", "-m", "Work-Item: #10", "feature"
    )

    # A squash commit whose trailer block repeats `Work-Item:` -- every
    # named item lands (issue #304).
    _real_git(repo, "checkout", "-q", "-b", "squashed")
    (repo / "squash.txt").write_text("one\n")
    _real_git(repo, "add", "squash.txt")
    _real_git(repo, "commit", "-q", "-m", "squash step 1")
    (repo / "squash.txt").write_text("one\ntwo\n")
    _real_git(repo, "add", "squash.txt")
    _real_git(repo, "commit", "-q", "-m", "squash step 2")
    _real_git(repo, "checkout", "-q", "main")
    _real_git(repo, "merge", "-q", "--squash", "squashed")
    _real_git(repo, "commit", "-q", "-m", "Squash landing", "-m", "Work-Item: #11\nWork-Item: #12")

    # A commit landed through a real rebase, its trailer block preserved.
    _real_git(repo, "checkout", "-q", "-b", "docslane", "feature")
    (repo / "docs.txt").write_text("docs\n")
    _real_git(repo, "add", "docs.txt")
    _real_git(repo, "commit", "-q", "-m", "docs change", "-m", "No-Item: docs")
    _real_git(repo, "rebase", "-q", "main")
    _real_git(repo, "checkout", "-q", "main")
    _real_git(repo, "merge", "-q", "--ff-only", "docslane")

    # A `Work-Item:` line in prose, never its own trailer block -- not a
    # landing (issue #304 proof 2).
    (repo / "prose.txt").write_text("prose\n")
    _real_git(repo, "add", "prose.txt")
    _real_git(repo, "commit", "-q", "-m", "Prose change", "-m", "Explanation prose.\nWork-Item: #7")

    # A side branch that never joins the trunk's first-parent line
    # (issue #304 proof 3).
    _real_git(repo, "checkout", "-q", "-b", "sidebranch")
    (repo / "side.txt").write_text("side\n")
    _real_git(repo, "add", "side.txt")
    _real_git(repo, "commit", "-q", "-m", "side change", "-m", "Work-Item: #99")
    _real_git(repo, "checkout", "-q", "main")

    _push_repository_trunk(repo, "hub")
    return repo


def test_trunk_landings_classify_merge_squash_and_rebase_commits_from_their_trailer_block_alone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #304 proofs 1-3, against a real `file://`-reachable remote with
    real merge, squash, and rebase history."""
    repo = _trunk_history_repository(tmp_path)
    monkeypatch.chdir(repo)

    landings = checkout.trunk_landings("hub", 20)

    assert [landing.classification for landing in landings] == [
        None,  # the plain initial commit
        board.TrunkWorkItemClassification((10,)),  # the merge commit
        board.TrunkWorkItemClassification((11, 12)),  # the squash commit
        board.NoItemClassification(board.NoItemKind.DOCS),  # the rebased commit
        None,  # `Work-Item:` in prose, not a trailer (proof 2)
    ]
    side_sha = _real_git(repo, "rev-parse", "sidebranch").stdout.strip()
    assert side_sha not in {landing.sha for landing in landings}  # proof 3

    # `depth` bounds the walk to the most recent commits, oldest of those first.
    assert [landing.classification for landing in checkout.trunk_landings("hub", 2)] == [
        board.NoItemClassification(board.NoItemKind.DOCS),
        None,
    ]


def test_trunk_landings_read_the_configured_remote_never_a_hardcoded_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #304 proof 4: a repository carrying both an `origin` remote
    (behind by one commit) and its actual canonical `hub` remote reads
    whichever one the caller names -- proving the remote is a real
    parameter, never a hardcoded `origin`, rather than merely asserting the
    literal is absent from the source."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _real_git(repo, "init", "-q", "-b", "main")
    _real_git(repo, "config", "user.name", "Test")
    _real_git(repo, "config", "user.email", "test@example.com")
    _real_git(repo, "config", "commit.gpgsign", "false")
    (repo / "f.txt").write_text("0\n")
    _real_git(repo, "add", "f.txt")
    _real_git(repo, "commit", "-q", "-m", "initial")

    origin_remote = tmp_path / "origin.git"
    origin_remote.mkdir()
    _real_git(origin_remote, "init", "-q", "--bare", "-b", "main")
    _real_git(repo, "remote", "add", "origin", str(origin_remote))
    _real_git(repo, "push", "-q", "origin", "main")
    _real_git(repo, "remote", "set-head", "origin", "main")

    (repo / "f.txt").write_text("1\n")
    _real_git(repo, "add", "f.txt")
    _real_git(repo, "commit", "-q", "-m", "second commit")

    hub_remote = tmp_path / "hub.git"
    hub_remote.mkdir()
    _real_git(hub_remote, "init", "-q", "--bare", "-b", "main")
    _real_git(repo, "remote", "add", "hub", str(hub_remote))
    _real_git(repo, "push", "-q", "hub", "main")
    _real_git(repo, "remote", "set-head", "hub", "main")

    monkeypatch.chdir(repo)

    assert len(checkout.trunk_landings("origin", 20)) == 1
    assert len(checkout.trunk_landings("hub", 20)) == 2


@pytest.mark.parametrize(
    ("url", "location"),
    [
        pytest.param(
            "git@github.com:owner/repo.git",
            checkout.RemoteLocation("github.com", "owner/repo"),
            id="ssh-scp",
        ),
        pytest.param(
            "ssh://git@github.com/owner/repo.git",
            checkout.RemoteLocation("github.com", "owner/repo"),
            id="ssh-url",
        ),
        pytest.param(
            "ssh://git@github.com:2222/owner/repo",
            checkout.RemoteLocation("github.com", "owner/repo"),
            id="ssh-url-with-port",
        ),
        pytest.param(
            "https://github.com/owner/repo.git",
            checkout.RemoteLocation("github.com", "owner/repo"),
            id="https",
        ),
        pytest.param(
            "https://github.com/owner/repo",
            checkout.RemoteLocation("github.com", "owner/repo"),
            id="https-no-suffix",
        ),
        pytest.param(
            "file:///srv/git/repo.git",
            checkout.RemoteLocation("file", "/srv/git/repo"),
            id="file",
        ),
        pytest.param(
            "file:///srv/git/repo",
            checkout.RemoteLocation("file", "/srv/git/repo"),
            id="file-no-suffix",
        ),
    ],
)
def test_parse_remote_location_normalizes_every_remote_shape(
    url: str, location: checkout.RemoteLocation
) -> None:
    assert checkout.parse_remote_location(url) == location


def test_parse_remote_location_refuses_an_unrecognized_shape() -> None:
    with pytest.raises(ClaimError, match="names no recognized host"):
        checkout.parse_remote_location("not-a-remote-url")


# `start`'s own worktree/branch naming and git plumbing (issue #322): a
# slug/prefix pure-function pair, plus the create/resume/remove/ancestry
# helpers `_cmd_start`/`release --merged`'s own cleanup share, each proven
# directly against real git rather than through the full CLI (that wiring
# lives in `tests/test_cli.py`).


@pytest.mark.parametrize(
    ("title", "slug"),
    [
        pytest.param("  Fix the Login Bug!! ", "fix-the-login-bug", id="punctuation-collapses"),
        pytest.param("Add __init__.py", "add-init-py", id="underscores-and-dots-collapse"),
    ],
)
def test_slug_from_title_lowercases_and_collapses_punctuation(title: str, slug: str) -> None:
    assert checkout.slug_from_title(title) == slug


def test_slug_from_title_truncates_to_forty_characters_with_no_trailing_hyphen() -> None:
    title = "a" * 45 + " tail words that overflow the forty character limit"

    assert checkout.slug_from_title(title) == "a" * 40


def test_slug_from_title_refuses_when_nothing_survives() -> None:
    with pytest.raises(ClaimError, match="no usable slug"):
        checkout.slug_from_title("!!! ??? ...")


def test_validate_slug_accepts_a_value_matching_the_derived_shape() -> None:
    assert checkout.validate_slug("fresh-slug-42") == "fresh-slug-42"


@pytest.mark.parametrize(
    "slug",
    [
        pytest.param("Fresh-Slug", id="uppercase"),
        pytest.param("-fresh-slug", id="leading-hyphen"),
        pytest.param("fresh-slug-", id="trailing-hyphen"),
        pytest.param("fresh--slug", id="doubled-hyphen"),
        pytest.param("a" * 41, id="too-long"),
        pytest.param("", id="empty"),
    ],
)
def test_validate_slug_refuses_a_value_the_derived_rule_would_never_produce(slug: str) -> None:
    with pytest.raises(ClaimError, match="--slug must be"):
        checkout.validate_slug(slug)


@pytest.mark.parametrize(
    ("environ", "prefix"),
    [
        pytest.param(
            {"ACO_AGENT": "Claude head (coordinator)"}, "claude", id="aco-agent-first-word"
        ),
        pytest.param({"GROK_SESSION_ID": "sess-1"}, "grok", id="grok-session"),
        pytest.param({"CLAUDE_SESSION_ID": "sess-1"}, "claude", id="claude-session"),
        pytest.param(
            {"ACO_AGENT": "Grok", "CLAUDE_SESSION_ID": "sess-1"},
            "grok",
            id="aco-agent-wins-over-a-session-id",
        ),
    ],
)
def test_branch_prefix_for_identity_reads_the_same_precedence_as_resolved_agent(
    monkeypatch: pytest.MonkeyPatch, environ: dict[str, str], prefix: str
) -> None:
    _set_agent_identity_env(monkeypatch, environ)

    assert checkout.branch_prefix_for_identity() == prefix


def test_branch_prefix_for_identity_refuses_with_no_identity_signal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _set_agent_identity_env(monkeypatch)

    with pytest.raises(ClaimError, match="branch prefix is required"):
        checkout.branch_prefix_for_identity()


def test_refuse_unsafe_start_branch_names_the_identity_word(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ClaimError, match="agent identity '-bad' is not usable"):
        checkout.refuse_unsafe_start_branch("-bad/issue-1-widget", prefix="-bad")


def test_refuse_unsafe_start_branch_accepts_a_safe_branch() -> None:
    checkout.refuse_unsafe_start_branch("codex/issue-1-widget", prefix="codex")


def test_refuse_unsafe_start_branch_refuses_an_overlong_identity_prefix() -> None:
    """Issue #322 review/gate finding 1: a syntactically safe but overlong
    `ACO_AGENT` first word must still be refused before any git write,
    exactly as the claim machinery's own `BRANCH_NAME_MAX_LENGTH` bound on a
    stored claim marker refuses it -- one predicate, not a shape check that
    happens to cap length only by coincidence of an unrelated regex
    quantifier."""
    prefix = "a" * 300
    branch = f"{prefix}/issue-1-widget"

    with pytest.raises(ClaimError, match=f"agent identity {prefix!r} is not usable"):
        checkout.refuse_unsafe_start_branch(branch, prefix=prefix)


def test_branch_exists_reads_real_local_refs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _scratch_git_repository(tmp_path)
    _real_git(repository, "branch", "feature")
    monkeypatch.chdir(repository)

    assert checkout.branch_exists("feature") is True
    assert checkout.branch_exists("no-such-branch") is False


def _bare_remote_repository_with_one_commit(tmp_path: Path) -> Path:
    repo, _remote = _real_repository_with_bare_remote(tmp_path)
    (repo / "base.txt").write_text("base\n")
    _real_git(repo, "add", "base.txt")
    _real_git(repo, "commit", "-q", "-m", "initial")
    _push_repository_trunk(repo, "origin")
    return repo


def test_create_linked_worktree_fetches_and_builds_from_the_remote_trunk(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    worktree = tmp_path / "repo-worktrees" / "issue-9-widget"
    monkeypatch.chdir(repo)

    checkout.create_linked_worktree(worktree, branch="codex/issue-9-widget", remote="origin")

    assert (worktree / "base.txt").read_text() == "base\n"
    assert _real_git(worktree, "branch", "--show-current").stdout.strip() == "codex/issue-9-widget"


def test_resolve_or_create_worktree_builds_once_and_resumes_on_a_second_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    worktree = tmp_path / "repo-worktrees" / "issue-9-widget"
    branch = "codex/issue-9-widget"
    monkeypatch.chdir(repo)

    checkout.resolve_or_create_worktree(worktree, branch, remote="origin")
    checkout.resolve_or_create_worktree(worktree, branch, remote="origin")

    resolved = checkout.resolve_path_checkout(worktree)
    assert resolved is not None
    assert resolved.branch == branch


def test_resolve_or_create_worktree_refuses_a_dirty_resume(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    worktree = tmp_path / "repo-worktrees" / "issue-9-widget"
    branch = "codex/issue-9-widget"
    monkeypatch.chdir(repo)
    checkout.resolve_or_create_worktree(worktree, branch, remote="origin")
    (worktree / "scratch.txt").write_text("uncommitted\n")

    with pytest.raises(ClaimError, match="is dirty"):
        checkout.resolve_or_create_worktree(worktree, branch, remote="origin")


def test_resolve_or_create_worktree_refuses_a_branch_taken_by_no_worktree_of_this_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    branch = "codex/issue-9-widget"
    _real_git(repo, "branch", branch)
    worktree = tmp_path / "repo-worktrees" / "issue-9-widget"
    monkeypatch.chdir(repo)

    with pytest.raises(ClaimError, match="already exists and is not this item's worktree"):
        checkout.resolve_or_create_worktree(worktree, branch, remote="origin")


def test_resolve_or_create_worktree_refuses_an_existing_non_worktree_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #322 review/gate finding: an ordinary directory already sitting
    at the target path -- empty or not -- is refused outright rather than
    left for `git worktree add` to adopt."""
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    branch = "codex/issue-9-widget"
    worktree = tmp_path / "repo-worktrees" / "issue-9-widget"
    worktree.mkdir(parents=True)
    monkeypatch.chdir(repo)

    with pytest.raises(ClaimError, match=re.escape(checkout.NOT_A_WORKTREE_REFUSAL)):
        checkout.resolve_or_create_worktree(worktree, branch, remote="origin")


def test_resolve_or_create_worktree_refuses_a_worktree_on_a_different_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    worktree = tmp_path / "repo-worktrees" / "issue-9-widget"
    worktree.parent.mkdir(parents=True)
    _real_git(repo, "worktree", "add", "-q", str(worktree), "-b", "codex/issue-9-old-slug")
    monkeypatch.chdir(repo)

    with pytest.raises(ClaimError, match="exists on branch 'codex/issue-9-old-slug'"):
        checkout.resolve_or_create_worktree(worktree, "codex/issue-9-widget", remote="origin")


def test_resolve_or_create_worktree_refuses_a_worktree_from_a_different_repository(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #322 review finding 2: a clean linked worktree on the exact
    same branch name, but belonging to an entirely different repository,
    must never be adopted as this item's own -- only its own repository's
    common git directory earns reuse."""
    caller = _scratch_git_repository(tmp_path)
    foreign_root = tmp_path / "foreign"
    foreign_root.mkdir()
    _foreign_main, foreign_worktree = _repo_with_linked_worktree(foreign_root)
    monkeypatch.chdir(caller)

    with pytest.raises(ClaimError, match="belongs to a different repository"):
        checkout.resolve_or_create_worktree(
            foreign_worktree, "codex/issue-1-widget", remote="origin"
        )


def test_resolve_or_create_worktree_refuses_a_path_that_is_not_a_checkout_root_itself(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #322 review finding 2: a path resolving into the middle of some
    repository's own working tree -- rather than a checkout root by itself --
    must never be adopted as `start`'s own worktree, even when a branch of
    the same name happens to exist elsewhere in that repository."""
    caller = _scratch_git_repository(tmp_path)
    nested = caller / "nested"
    nested.mkdir()
    monkeypatch.chdir(caller)

    with pytest.raises(ClaimError, match="is not a checkout root by itself"):
        checkout.resolve_or_create_worktree(nested, "codex/issue-1-widget", remote="origin")


def test_resolve_or_create_worktree_refuses_the_repositorys_own_main_checkout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #322 review finding 2: `path` resolving to this repository's own
    main checkout, not a linked worktree, must never be adopted -- only a
    linked worktree is ever safe to treat as one of `start`'s own lanes."""
    caller = _scratch_git_repository(tmp_path)
    monkeypatch.chdir(caller)

    with pytest.raises(
        ClaimError, match="is a repository's own main checkout, not a linked worktree"
    ):
        checkout.resolve_or_create_worktree(caller, "codex/issue-1-widget", remote="origin")


def test_worktree_on_branch_finds_the_one_matching_path(tmp_path: Path) -> None:
    main, worktree = _repo_with_linked_worktree(tmp_path)

    assert checkout.worktree_on_branch((main, worktree), "codex/issue-1-widget") == worktree
    assert checkout.worktree_on_branch((main,), "codex/issue-1-widget") is None


def test_worktree_on_branch_surfaces_a_git_failure_resolving_a_registered_worktree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #322 review/gate finding: `paths` names worktrees git's own
    registry already vouches for, so a failure resolving one of them (a
    moved or deleted directory, most often) must surface as a refusal --
    never a silently skipped "not on this branch"."""
    main, worktree = _repo_with_linked_worktree(tmp_path)
    monkeypatch.chdir(main)
    _stub_one_git_call(
        monkeypatch,
        [
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-dir",
            "--git-common-dir",
        ],
        exit_status=128,
        stderr="fatal: cannot change to 'gone': No such file or directory",
    )

    with pytest.raises(ClaimError, match="cannot change to"):
        checkout.worktree_on_branch((worktree,), "codex/issue-1-widget")


def test_resolve_path_checkout_fails_loud_on_a_malformed_rev_parse_reply(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #322 review/gate finding: a successful `git rev-parse` exit
    whose combined toplevel/git-dir/git-common-dir reply does not split into
    exactly three lines is a real git misbehavior -- `worktree_on_branch`'s
    own callers must see it as a refusal, never as a silently swallowed
    `None`."""
    repository = _scratch_git_repository(tmp_path)
    monkeypatch.chdir(repository)
    _stub_one_git_call(
        monkeypatch,
        [
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-dir",
            "--git-common-dir",
        ],
        exit_status=0,
        stderr="",
    )

    with pytest.raises(ClaimError, match="git returned a malformed checkout description"):
        checkout.worktree_on_branch((repository,), "main")


def test_remove_linked_worktree_deletes_the_directory_and_the_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, worktree = _repo_with_linked_worktree(tmp_path)
    monkeypatch.chdir(main)

    outcome = checkout.remove_linked_worktree(worktree, branch="codex/issue-1-widget")

    assert not worktree.exists()
    assert checkout.branch_exists("codex/issue-1-widget") is False
    assert outcome.worktree.removed is True
    assert outcome.branch.removed is True


def test_branch_merged_into_default_is_true_only_after_a_real_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    _real_git(repo, "checkout", "-q", "-b", "feature")
    (repo / "feature.txt").write_text("feature\n")
    _real_git(repo, "add", "feature.txt")
    _real_git(repo, "commit", "-q", "-m", "feature work")
    _real_git(repo, "checkout", "-q", "main")
    monkeypatch.chdir(repo)

    assert checkout.branch_merged_into_default("feature", remote="origin") is False

    _real_git(repo, "merge", "-q", "--no-ff", "-m", "Merge feature", "feature")
    _push_repository_trunk(repo, "origin")

    assert checkout.branch_merged_into_default("feature", remote="origin") is True


def test_branch_exists_fails_loud_on_an_unexpected_git_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repository = _scratch_git_repository(tmp_path)
    monkeypatch.chdir(repository)
    _stub_one_git_call(
        monkeypatch,
        ["show-ref", "--verify", "--quiet", "refs/heads/x"],
        exit_status=128,
        stderr="fatal: bad object refs/heads/x",
    )

    with pytest.raises(ClaimError, match="fatal: bad object"):
        checkout.branch_exists("x")


def test_create_linked_worktree_fails_loud_when_the_fetch_itself_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    monkeypatch.chdir(repo)
    _stub_one_git_call(
        monkeypatch, ["fetch", "origin"], exit_status=1, stderr="fatal: could not read from remote"
    )

    with pytest.raises(ClaimError, match="could not read from remote"):
        checkout.create_linked_worktree(
            tmp_path / "repo-worktrees" / "issue-9-widget",
            branch="codex/issue-9-widget",
            remote="origin",
        )


def test_create_linked_worktree_fails_loud_when_worktree_add_itself_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    monkeypatch.chdir(repo)
    worktree = tmp_path / "repo-worktrees" / "issue-9-widget"
    _stub_one_git_call(
        monkeypatch,
        [
            "worktree",
            "add",
            str(worktree),
            "-b",
            "codex/issue-9-widget",
            "refs/remotes/origin/main",
        ],
        exit_status=128,
        stderr="fatal: already exists",
    )

    with pytest.raises(ClaimError, match="fatal: already exists"):
        checkout.create_linked_worktree(worktree, branch="codex/issue-9-widget", remote="origin")


def test_remove_linked_worktree_fails_loud_when_worktree_remove_itself_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    main, worktree = _repo_with_linked_worktree(tmp_path)
    monkeypatch.chdir(main)
    _stub_one_git_call(
        monkeypatch,
        ["worktree", "remove", str(worktree)],
        exit_status=1,
        stderr="fatal: contains modified or untracked files",
    )

    with pytest.raises(ClaimError, match="contains modified or untracked files"):
        checkout.remove_linked_worktree(worktree, branch="codex/issue-1-widget")


def test_remove_linked_worktree_reports_the_worktree_removed_and_the_branch_kept(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Issue #322 review/gate finding 4: a branch-deletion failure after the
    worktree is already gone must never read as a bare `kept` -- the typed
    outcome names both halves apart."""
    main, worktree = _repo_with_linked_worktree(tmp_path)
    monkeypatch.chdir(main)
    _stub_one_git_call(
        monkeypatch,
        ["branch", "-d", "codex/issue-1-widget"],
        exit_status=1,
        stderr="error: branch not fully merged",
    )

    outcome = checkout.remove_linked_worktree(worktree, branch="codex/issue-1-widget")

    assert not worktree.exists()
    assert outcome.worktree.removed is True
    assert outcome.branch.removed is False
    assert outcome.branch.reason is not None
    assert "not fully merged" in outcome.branch.reason


def test_branch_merged_into_default_fails_loud_when_the_fetch_itself_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    monkeypatch.chdir(repo)
    _stub_one_git_call(
        monkeypatch, ["fetch", "origin"], exit_status=1, stderr="fatal: could not read from remote"
    )

    with pytest.raises(ClaimError, match="could not read from remote"):
        checkout.branch_merged_into_default("feature", remote="origin")


def test_branch_merged_into_default_fails_loud_on_an_unexpected_merge_base_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = _bare_remote_repository_with_one_commit(tmp_path)
    monkeypatch.chdir(repo)
    _stub_one_git_call(
        monkeypatch,
        ["merge-base", "--is-ancestor", "no-such-branch", "refs/remotes/origin/main"],
        exit_status=128,
        stderr="fatal: not a valid object name no-such-branch",
    )

    with pytest.raises(ClaimError, match="not a valid object name"):
        checkout.branch_merged_into_default("no-such-branch", remote="origin")
