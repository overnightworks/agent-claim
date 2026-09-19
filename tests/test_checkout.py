"""Direct `checkout.py` behavior: remote/worktree/branch validation, the
`_git_output` boundary, dirty-path reading, `versioned_paths`, trunk
landings, and remote-location parsing. `_validate_checkout` is exercised
through `issue_claim._validate_checkout`, `cli.py`'s own re-export of the
same function every `claim`/`rescope` command call site uses. Tests that
drive these through `issue_claim.main([...])` stay in `tests/test_cli.py` as
CLI-wiring behavior."""

from __future__ import annotations

import re
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from board_fixtures import BASE, request
from cli_fixtures import _fallback_git_output, _git_checkout, _real_git

from agent_coordination import board, checkout, process, protocol
from agent_coordination import cli as issue_claim
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
    def git(arguments: list[str]) -> str:
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

    def git(arguments: list[str]) -> str:
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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: values[tuple(arguments)])

    issue_claim._validate_checkout(request())


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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: values[tuple(arguments)])

    with pytest.raises(ClaimError, match=message):
        issue_claim._validate_checkout(candidate)


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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: values[tuple(arguments)])
    candidate = request()

    with pytest.raises(ClaimError) as error:
        issue_claim._validate_checkout(candidate)

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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: values[tuple(arguments)])
    candidate = request(branch=branch)

    with pytest.raises(ClaimError) as error:
        issue_claim._validate_checkout(candidate)

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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: values[tuple(arguments)])
    candidate = request()

    with pytest.raises(ClaimError) as error:
        issue_claim._validate_checkout(candidate)

    assert str(error.value) == (
        "build claims require a linked isolated worktree checkout; "
        f"run {checkout.ISOLATED_WORKTREE_RECIPE}"
    )


def test_checkout_validation_return_to_claim_names_no_branch_from_the_trunk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`rescope` shares this check with `claim` (issue #211), but its honest
    repair differs: its claim's worktree already exists, so recommending the
    `git worktree add` recipe builds a second, foreign one. From the trunk
    branch no other branch is known here to name, so `RETURN_TO_CLAIM` points
    back at the claim's own worktree without inventing one."""
    values = {("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main"}
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: values[tuple(arguments)])

    with pytest.raises(ClaimError) as error:
        checkout._validate_worktree_branch("main", repair=checkout.WorktreeRepair.RETURN_TO_CLAIM)

    assert str(error.value) == (
        "build claims require an isolated non-main worktree branch; "
        "run this command from this claim's own worktree, not the primary checkout"
    )


def test_checkout_validation_return_to_claim_names_the_known_branch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Checked out directly on a real branch inside the shared (non-linked)
    checkout, that branch is already known -- it is the same branch the
    caller resolved its identity from -- so `RETURN_TO_CLAIM` names it
    instead of leaving the sentence branch-less."""
    values = {
        ("symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"): "refs/remotes/origin/main",
        ("branch", "--show-current"): "codex/issue-211-worktree-repair-sentence",
        ("rev-parse", "--git-dir"): "/repo/.git",
        ("rev-parse", "--git-common-dir"): "/repo/.git",
    }
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: values[tuple(arguments)])

    with pytest.raises(ClaimError) as error:
        checkout._validate_worktree_branch(
            "codex/issue-211-worktree-repair-sentence",
            repair=checkout.WorktreeRepair.RETURN_TO_CLAIM,
        )

    assert str(error.value) == (
        "build claims require a linked isolated worktree checkout; "
        "run this command from this claim's own worktree on "
        "'codex/issue-211-worktree-repair-sentence', not the primary checkout"
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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: values[tuple(arguments)])
    candidate = request()

    with pytest.raises(ClaimError) as error:
        issue_claim._validate_checkout(candidate)

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
    monkeypatch.setattr(checkout, "_git_output", lambda arguments: values[tuple(arguments)])
    candidate = request()

    with pytest.raises(ClaimError) as error:
        issue_claim._validate_checkout(candidate)

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
        issue_claim._validate_checkout(candidate)
        return

    with pytest.raises(ClaimError, match="isolated non-main worktree branch"):
        issue_claim._validate_checkout(candidate)


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
    """`versioned_paths` and `origin_remote_url` -- both direct `subprocess.run`
    callers (`_git_output` backs the latter) -- must translate a missing
    executable or a timeout to the same `ClaimError` text."""
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


def test_versioned_paths_fails_loud_on_a_nonzero_git_exit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed(arguments, **_kwargs):
        return subprocess.CompletedProcess(
            arguments, 128, stdout=b"", stderr=b"fatal: not a git repository\n"
        )

    monkeypatch.setattr(subprocess, "run", failed)
    with pytest.raises(ClaimError, match="fatal: not a git repository"):
        _LIVE_VERSIONED_PATHS()


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

    def git_output(arguments: list[str]) -> str:
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


def test_trunk_ref_fails_loud_when_no_candidate_branch_resolves(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Neither the symbolic ref nor any of the default-branch-name candidates
    resolving must fail loud rather than silently ruling every candidate's age
    as unknown."""

    def git_output(_arguments: list[str]) -> str:
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

    def git_output(arguments: list[str]) -> str:
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
    def git_output(arguments: list[str]) -> str:
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

    def git_output(arguments: list[str]) -> str:
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

    def git_output(arguments: list[str]) -> str:
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
def test_trunk_landings_reads_a_control_byte_inside_a_trailer_value_as_one_literal_item(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, byte: str
) -> None:
    """Issue #304 review, finding B1: git never escapes `\\x1f`, `\\x1e`, or
    `\\x01` inside a trailer value -- exactly the bytes the historical
    `\\x1f`-separated framing used to split repeated trailer values -- so a
    value that happens to contain one of them must read back as the one
    literal value git actually recorded, never as two fabricated work items.
    The NUL/newline framing reads it as data: `parse_item_reference` then
    refuses that single literal value by name."""
    repo = _minimal_pushed_repository(tmp_path)
    (repo / "f.txt").write_text("content\n")
    _real_git(repo, "add", "f.txt")
    value = f"#12{byte}#13"
    _real_git(repo, "commit", "-q", "-m", "change", "-m", f"Work-Item: {value}")
    _push_to_hub(repo)
    monkeypatch.chdir(repo)

    with pytest.raises(
        protocol.ClaimUnavailableError, match=re.escape(f"{value!r} is not an item reference")
    ):
        checkout.trunk_landings("hub", 20)


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
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _real_git(remote, "init", "-q", "--bare", "-b", "main")

    repo = tmp_path / "repo"
    repo.mkdir()
    _real_git(repo, "init", "-q", "-b", "main")
    _real_git(repo, "config", "user.name", "Test")
    _real_git(repo, "config", "user.email", "test@example.com")
    _real_git(repo, "config", "commit.gpgsign", "false")
    (repo / "base.txt").write_text("base\n")
    _real_git(repo, "add", "base.txt")
    _real_git(repo, "commit", "-q", "-m", "initial")
    _real_git(repo, "remote", "add", "hub", str(remote))

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

    _real_git(repo, "push", "-q", "hub", "main")
    _real_git(repo, "remote", "set-head", "hub", "main")
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
