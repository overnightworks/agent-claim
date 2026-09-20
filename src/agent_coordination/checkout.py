"""Local git checkout validation and agent identity."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path

from . import board, process
from .protocol import (
    ClaimError,
    ClaimRequest,
    _outbound_text,
    is_safe_branch_name,
    named_with_overflow_count,
)

ACO_AGENT_ENV = "ACO_AGENT"
GROK_SESSION_ID_ENV = "GROK_SESSION_ID"
CLAUDE_SESSION_ID_ENV = "CLAUDE_SESSION_ID"


# One owner for every git-subprocess failure sentence: `_git_run` launches
# every `git` subprocess this module runs and translates the same three
# launch-failure shapes -- a missing executable, a timeout, and any other
# OS-level launch failure -- to the same `ClaimError` text (issue #315 Sonar
# S1192; issue #314 gate G's follow-up folds `versioned_paths` and
# `path_is_tracked` into this one owner too, F1). `process.run_git` and
# `process.git_failure_detail` (issue #372) own the argv shape and the
# fallback reading `store.py` needs too; the `ClaimError` translation stays
# here since `checkout`/`store` share one import-linter layer. `_git_output`,
# `versioned_paths`, and `path_is_tracked` each interpret a successful
# launch's exit status their own way.
_GIT_MISSING_EXECUTABLE_ERROR = "git is required for issue claims"
_GIT_TIMED_OUT_ERROR = "git timed out while validating the build checkout"


def _git_run(arguments: list[str], *, directory: Path | None = None) -> process.CapturedResult:
    """Launch `git arguments`, in `directory` when given via `-C` (issue
    #314) -- every caller that must judge a specific checkout rather than
    the calling process's own cwd names `directory` explicitly, so the
    checkout a security decision reads is never an accident of where the
    process happens to run.

    Every OS-level launch failure -- a missing executable, a timeout, or
    anything else (permission denied, out of file descriptors, `-C` naming a
    non-directory, ...) -- fails closed as a `ClaimError`, never an
    uncaught traceback out of `protect`'s hook boundary. Interpreting a
    successful launch's exit status is each caller's own job.
    """
    try:
        return process.run_git(arguments, directory=directory)
    except process.ExecutableMissingError as error:
        raise ClaimError(_GIT_MISSING_EXECUTABLE_ERROR) from error
    except process.ProcessTimedOutError as error:
        raise ClaimError(_GIT_TIMED_OUT_ERROR) from error
    except OSError as error:
        raise ClaimError(f"git failed to launch: {error}") from error


def _git_output(arguments: list[str], *, directory: Path | None = None) -> str:
    """`git arguments`'s stdout, in `directory` when given via `-C` (issue
    #314) or the calling process's own cwd otherwise; a nonzero exit fails
    closed."""
    result = _git_run(arguments, directory=directory)
    if result.exit_status != 0:
        raise ClaimError(process.git_failure_detail(result))
    # Trailing-only: every caller wants the one newline `git` appends after its
    # output trimmed, but `git status --porcelain`'s short format is
    # significant in its *leading* column (` M path` names a modified file by
    # a leading space before the path) -- a leading strip silently turned that
    # into `M path` and `_dirty_paths` then sliced into the filename itself.
    return result.stdout.decode().rstrip("\n")


def remote_url(remote: str) -> str:
    """One named remote's URL.

    Generalizes `origin_remote_url` (issue #176, §2): the store's
    `canonical_remote` is a separate, independently configured axis from
    `origin` (the GitHub-repository-discovery fallback below) -- almost
    always the same remote in practice, but not the same concept, so a
    caller comparing a forge target against the canonical remote's own URL
    needs to name that remote explicitly rather than assuming `origin`.
    """
    return _git_output(["config", "--get", f"remote.{remote}.url"])


def origin_remote_url() -> str:
    """The checkout's `origin` remote: `github.discover_repository`'s first,
    cheap read, before it ever falls back to asking `gh`."""
    return remote_url("origin")


@dataclass(frozen=True)
class RemoteLocation:
    """A git remote URL's host and repository path, independent of any forge
    adapter's own URL syntax (issue #245).

    The one owner comparing a forge target against the checkout's canonical
    remote (Erwartung 6, issue #176 §2): a GitHub adapter target and a
    `RemoteLocation` agree exactly when their `host` and `path` do, whether
    the canonical remote is GitHub, another forge host entirely, or a local
    `file://` path.
    """

    host: str
    path: str


_GIT_SUFFIX = ".git"
# A scheme-form remote: `ssh://[user@]host[:port]/path`, `https://host/path`,
# `file:///path` -- the one shape every non-scp remote URL shares.
_SCHEME_REMOTE_PATTERN = re.compile(
    r"^(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*)://(?:[^@/]*@)?(?P<rest>.+)$"
)
# The scp-like shorthand `ssh` alone accepts: `[user@]host:path`, no scheme.
_SCP_REMOTE_PATTERN = re.compile(r"^(?:[^@/]+@)?(?P<host>[^:/]+):(?P<path>.+)$")


def _without_git_suffix(path: str) -> str:
    # Trailing only: a `file://` path's leading `/` is significant (it is
    # what makes the path absolute), while a trailing one is never part of a
    # repository's name.
    return path.rstrip("/").removesuffix(_GIT_SUFFIX)


def parse_remote_location(url: str) -> RemoteLocation:
    """`url`'s host and repository path (issue #245): every remote shape
    `aco` accepts -- SSH scp-like (`git@host:o/r.git`), SSH URL
    (`ssh://git@host/o/r`), HTTPS (`https://host/o/r(.git)`), and
    `file:///...` (host `"file"`, its filesystem path) -- normalizes to one
    shape here, so no caller keeps its own copy of this parsing.
    """
    scheme_match = _SCHEME_REMOTE_PATTERN.match(url)
    if scheme_match is not None:
        scheme = scheme_match.group("scheme").lower()
        rest = scheme_match.group("rest")
        if scheme == "file":
            return RemoteLocation("file", _without_git_suffix(rest))
        host, _, path = rest.partition("/")
        host = host.partition(":")[0]  # drop an explicit port, e.g. `host:2222`
        return RemoteLocation(host, _without_git_suffix(path))
    scp_match = _SCP_REMOTE_PATTERN.match(url)
    if scp_match is not None:
        return RemoteLocation(scp_match.group("host"), _without_git_suffix(scp_match.group("path")))
    raise ClaimError(f"remote url {url!r} names no recognized host")


def versioned_paths(*, directory: Path | None = None) -> tuple[str, ...]:
    """Every versioned path git tracks, from `directory` via `-C` when given
    (issue #314: `rescope`'s own resolved checkout, never the calling
    process's cwd) or the process's own checkout otherwise (`claim`'s own
    precondition, unaffected by #314)."""
    result = _git_run(["ls-files", "-z", "--full-name"], directory=directory)
    if result.exit_status != 0:
        raise ClaimError(process.git_failure_detail(result))
    return tuple(dict.fromkeys(path for path in result.stdout.decode().split("\0") if path))


def path_is_tracked(path: str, *, directory: Path | None = None) -> bool:
    """Whether `path` (repo-relative, forward slashes) is tracked in git's
    index right now, read from `directory` via `-C` when given (issue #314:
    `_board_config`'s own resolved checkout, never the calling process's
    cwd) or the process's own checkout otherwise (issue #315) -- absent,
    untracked, and ignored all read as `False`, since
    `git ls-files --error-unmatch` exits 1, and only 1, for a path it does
    not track. A dedicated call, not `path in versioned_paths()`: that
    listing's exact membership and count are a different concern
    (scope-width math over every tracked file), so a test fixing one axis
    never has to carry the other.

    Exit 1 is the one status `--error-unmatch` defines for "not tracked";
    any other nonzero exit (e.g. 128 outside a git repository) is a real git
    failure, matching `versioned_paths`'s handling in this module -- it must
    not read as an untrusted pin instead of a git error."""
    result = _git_run(["ls-files", "--error-unmatch", "--", path], directory=directory)
    if result.exit_status == 0:
        return True
    if result.exit_status == 1:
        return False
    raise ClaimError(process.git_failure_detail(result))


def paths_under_scope(paths: tuple[str, ...], scope: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            path
            for path in paths
            if any(path == entry or path.startswith(f"{entry}/") for entry in scope)
        )
    )


def _scope_directories(paths: tuple[str, ...], *, directory: Path | None = None) -> tuple[str, ...]:
    """Return the scope entries that name a git tree or on-disk directory,
    read from `directory` via `-C` when given (issue #314 gate B4:
    `rescope`'s own resolved checkout, never the calling process's cwd) or
    the process's own checkout otherwise (`claim`'s own precondition,
    unaffected by #314)."""
    directories: list[str] = []
    toplevel: str | None = None
    for path in paths:
        try:
            kind = _git_output(["cat-file", "-t", f"HEAD:{path}"], directory=directory)
        except ClaimError:
            kind = ""
        if kind == "tree":
            directories.append(path)
            continue
        if toplevel is None:
            try:
                toplevel = _git_output(["rev-parse", "--show-toplevel"], directory=directory)
            except ClaimError:
                toplevel = ""
        if toplevel and (Path(toplevel) / path).is_dir():
            directories.append(path)
    return tuple(directories)


ISOLATED_WORKTREE_RECIPE = (
    "git worktree add ../<repo>-worktrees/issue-<n>-<slug> -b <agent>/issue-<n>-<slug>"
)

# One owner for both worktree-isolation refusal sentences (issue #314 gate
# B6, Sonar S1192): `claim`'s own cwd-based precondition
# (`_validate_worktree_branch`) and `rescope`'s path-resolved one
# (`_refuse_shared_checkout`) raise the identical two sentences, so each is
# spelled once here instead of twice across the two functions.
ISOLATED_NON_MAIN_BRANCH_REFUSAL = "build claims require an isolated non-main worktree branch; "
LINKED_ISOLATED_WORKTREE_REFUSAL = "build claims require a linked isolated worktree checkout; "


class WorktreeRepair(StrEnum):
    """Which repair a worktree-isolation refusal should name.

    `claim` has no worktree yet, so it needs one built (`CREATE`, the `git
    worktree add` recipe). `rescope` and `release` act on a claim that was
    taken from a worktree that therefore already exists, so naming the same
    create recipe sends an agent to build a second, foreign one -- exactly
    the worktree the state then sees as an unrelated lane. Their honest
    repair is `RETURN_TO_CLAIM`: go back to the worktree this claim already
    has.
    """

    CREATE = "create"
    RETURN_TO_CLAIM = "return_to_claim"


def _worktree_repair_instruction(repair: WorktreeRepair, *, branch: str | None) -> str:
    """The actionable clause a worktree-isolation refusal ends with.

    `branch` is the checkout's own already-known branch, never one looked up
    for the occasion: at the trunk-branch check the checkout is on `main` or
    `master`, so no other branch is available to name without guessing, and
    `None` says so; at the shared-checkout check the checkout's branch is
    already known (it is `branch` below), so `RETURN_TO_CLAIM` names it.
    """
    if repair is WorktreeRepair.CREATE:
        return f"run {ISOLATED_WORKTREE_RECIPE}"
    if branch is None:
        return "run this command from this claim's own worktree, not the primary checkout"
    return (
        f"run this command from this claim's own worktree on {branch!r}, not the primary checkout"
    )


def _validate_worktree_branch(
    branch: str,
    *,
    repair: WorktreeRepair = WorktreeRepair.CREATE,
    directory: Path | None = None,
) -> None:
    """Require an isolated non-main worktree checked out on `branch`, read
    from `directory` via `-C` when given (issue #322: `start`'s own resolved
    worktree, never a process-wide `os.chdir`) or the calling process's own
    cwd otherwise -- `claim`'s own precondition, since a fresh claim is
    created by literally standing in the worktree it claims. `rescope` no
    longer shares this (issue #314): it judges an already-resolved
    `PathCheckout` instead, via `_refuse_shared_checkout` below, so a
    rescope invoked from a foreign cwd is not silently judged by the wrong
    checkout.
    """
    if is_default_branch(branch, directory=directory):
        raise ClaimError(
            f"{ISOLATED_NON_MAIN_BRANCH_REFUSAL}{_worktree_repair_instruction(repair, branch=None)}"
        )
    current = _git_output(["branch", "--show-current"], directory=directory)
    git_directory = Path(_git_output(["rev-parse", "--git-dir"], directory=directory)).resolve()
    common_directory = Path(
        _git_output(["rev-parse", "--git-common-dir"], directory=directory)
    ).resolve()
    if current != branch:
        raise ClaimError(f"claim branch {branch!r} does not match checkout branch {current!r}")
    if git_directory == common_directory:
        raise ClaimError(
            f"{LINKED_ISOLATED_WORKTREE_REFUSAL}"
            f"{_worktree_repair_instruction(repair, branch=branch)}"
        )


class CheckoutKind(StrEnum):
    """Whether a resolved checkout is the shared main checkout or a linked,
    isolated worktree (issue #314) -- the one structural fact `protect` and
    `rescope` judge a write or a rescope by, read from the checkout itself
    rather than from a branch name."""

    MAIN = "main"
    LINKED_WORKTREE = "linked_worktree"


@dataclass(frozen=True)
class PathCheckout:
    """The git checkout that owns a directory, resolved directly from that
    directory (issue #314) -- never from the calling process's own cwd, so
    the same directory yields the same checkout regardless of where the
    process runs. `protect` resolves this from a hook payload path's own
    parent directory; `rescope` resolves it from the first absolute path it
    is given, falling back to its own process cwd when none is (its one
    other legitimate location signal). `common_directory` is the one fact
    shared by every worktree of the same repository -- the key a caller
    fetching store state once per repository, not once per worktree, caches
    on (issue #314 gate G5). `has_commit` is `False` for an unborn branch
    (a symbolic `HEAD` naming a branch with no commit yet): a resolved
    checkout with no commit must never itself authorize a write (issue #314
    gate G3), since its branch name can coincidentally match a still-live
    claim's."""

    toplevel: Path
    branch: str
    kind: CheckoutKind
    common_directory: Path
    has_commit: bool


NO_COMMIT_CHECKOUT_REASON = "no commit on this branch"
NOT_IN_A_REPOSITORY_REASON = "not in a repository"


def resolve_path_checkout(directory: Path) -> PathCheckout | None:
    """The checkout owning `directory`, or `None` when `directory` sits
    outside every git repository ("not in a repository", issue #314).

    Every git read runs `git -C directory`, so the result is the same
    regardless of the calling process's own cwd -- unlike the ad hoc,
    cwd-implicit `_git_output` calls this replaces in `protect` and
    `rescope`, which silently read the *process's* checkout instead of the
    one the caller actually means. `--path-format=absolute` makes the
    toplevel/git-dir/common-dir comparison below meaningful: git's default,
    relative-to-`-C`-directory paths would otherwise have to be re-resolved
    against `directory` itself, not the caller's own cwd.
    """
    try:
        return _resolve_checkout(directory)
    except ClaimError:
        return None


def _resolve_checkout(directory: Path) -> PathCheckout:
    """`resolve_path_checkout`'s own git reads, without swallowing a failure
    into `None` (issue #322 review/gate finding: `worktree_on_branch` below
    resolves paths git's own worktree registry already vouches for as
    worktrees of this repository, so any failure resolving one of them is
    real -- a moved or deleted worktree directory, most often -- never a
    legitimate "nothing here"; `resolve_path_checkout` is the one caller
    that still wants that bare `None` reading)."""
    combined = _git_output(
        [
            "rev-parse",
            "--path-format=absolute",
            "--show-toplevel",
            "--git-dir",
            "--git-common-dir",
        ],
        directory=directory,
    )
    try:
        toplevel, git_directory, common_directory = combined.splitlines()
    except ValueError as error:
        raise ClaimError(f"git returned a malformed checkout description: {combined!r}") from error
    branch = _git_output(["branch", "--show-current"], directory=directory)
    kind = CheckoutKind.MAIN if git_directory == common_directory else CheckoutKind.LINKED_WORKTREE
    try:
        _git_output(["rev-parse", "--verify", "HEAD"], directory=directory)
        has_commit = True
    except ClaimError:
        has_commit = False
    return PathCheckout(
        toplevel=Path(toplevel),
        branch=branch,
        kind=kind,
        common_directory=Path(common_directory),
        has_commit=has_commit,
    )


def _refuse_shared_checkout(path_checkout: PathCheckout, *, repair: WorktreeRepair) -> None:
    """`rescope`'s own worktree-isolation refusal (issue #314): the same
    invariant `_validate_worktree_branch` enforces for `claim`, judged from
    an already path-resolved checkout's own `directory` instead of a fresh
    git read in the calling process's own cwd (gate G4).

    Unlike `claim`'s own `is_default_branch`, which falls back to guessing
    `{main, master}` when `origin/HEAD` cannot be resolved, this denies
    outright: `rescope` judges an attacker-reachable payload location, so a
    repository whose default branch is `trunk`, read from a checkout with no
    recorded `origin/HEAD` yet, must never slip through unnoticed as "not
    the default branch".
    """
    default_branch = default_branch_name(directory=path_checkout.toplevel)
    if default_branch is None:
        raise ClaimError(DEFAULT_BRANCH_UNKNOWN_REASON)
    if path_checkout.branch == default_branch:
        raise ClaimError(
            f"{ISOLATED_NON_MAIN_BRANCH_REFUSAL}{_worktree_repair_instruction(repair, branch=None)}"
        )
    if path_checkout.kind is CheckoutKind.MAIN:
        raise ClaimError(
            f"{LINKED_ISOLATED_WORKTREE_REFUSAL}"
            f"{_worktree_repair_instruction(repair, branch=path_checkout.branch)}"
        )


def _dirty_paths(status: str) -> tuple[str, ...]:
    """The changed paths named by `git status --porcelain`'s short format:
    each line is two status characters, a space, then the path (or, for a
    rename, `old -> new`), so dropping the first three characters leaves the
    path a dirty-tree refusal names."""
    return tuple(line[3:] for line in status.splitlines() if line)


def _validate_checkout(request: ClaimRequest, *, directory: Path | None = None) -> None:
    """`claim`'s own preconditions against `directory` via `-C` when given
    (issue #322: `start`'s own resolved worktree, never a process-wide
    `os.chdir`) or the calling process's own cwd otherwise."""
    head = _git_output(["rev-parse", "HEAD"], directory=directory)
    if head != request.base:
        raise ClaimError(
            f"claim base {request.base} does not match checkout HEAD {head}; "
            "omit --base to use checkout HEAD"
        )
    _validate_worktree_branch(request.branch, directory=directory)
    dirty = _git_output(["status", "--porcelain"], directory=directory)
    if dirty:
        named = named_with_overflow_count(_dirty_paths(dirty))
        raise ClaimError(f"claim must be acquired before the first worktree edit: {named}")


DEFAULT_BRANCH_FALLBACK = frozenset({"main", "master"})

# `protect`'s and `rescope`'s own denial when a resolved checkout's default
# branch cannot be determined at all (issue #314 gate G4): unlike `claim`'s
# `is_default_branch` fallback below, they never guess -- see
# `_refuse_shared_checkout`'s and `_protect_basic_checkout_denial`'s own
# docstrings for why the two callers of the same `default_branch_name`
# resolver accept different risk here.
DEFAULT_BRANCH_UNKNOWN_REASON = "default branch unknown"

# One owner for `protect`'s "not main" denial (issue #314 repeat gate,
# finding 4, Sonar S1192): `_protect_not_main_denial` in `cli.py` returns
# this for both a shared main checkout and a linked worktree that sits on
# the resolved default branch, so the one production spelling lives here
# instead of twice in that function.
PROTECT_NOT_MAIN_REASON = "not main"


def _origin_head_ref(*, directory: Path | None = None) -> str | None:
    """The `origin/HEAD` symbolic ref (e.g. `refs/remotes/origin/trunk`),
    read from `directory` via `-C` when given (issue #314: a resolved
    checkout's own default-branch lookup, never the calling process's cwd)
    or the process's own checkout otherwise (`claim`'s own precondition) --
    `None` when a clone or `git remote set-head` never recorded one -- the
    two-name fallback below is the caller's job (issue #238), since `claim`
    and `protect` word their refusals differently."""
    try:
        symbolic = _git_output(
            ["symbolic-ref", "--quiet", "refs/remotes/origin/HEAD"], directory=directory
        )
    except ClaimError:
        return None
    return symbolic or None


def default_branch_name(*, directory: Path | None = None) -> str | None:
    """The repository's default branch name, read from `directory`'s own
    `origin/HEAD` when given (issue #314) or the process's own checkout
    otherwise, or `None` when git cannot resolve it."""
    ref = _origin_head_ref(directory=directory)
    if ref is None:
        return None
    return ref.removeprefix("refs/remotes/origin/")


def is_default_branch(branch: str, *, directory: Path | None = None) -> bool:
    """Whether `branch` is the repository's default branch (issue #238):
    the name `origin/HEAD` resolves to, or the historical `{"main", "master"}`
    guess when a repository has no recorded `origin/HEAD`.

    `claim`'s own worktree precondition (`_validate_worktree_branch`) alone:
    read from `directory` via `-C` when given (issue #322: `start`'s own
    resolved worktree) or the calling process's own cwd otherwise, since a
    fresh claim is created by literally standing in the worktree it claims
    -- there is no attacker-reachable payload location to spoof here, so the
    historical guess stays an accepted risk (issue #238) this function keeps
    unchanged. `protect` and `rescope` judge a resolved checkout's default
    branch directly through `default_branch_name(directory=...)` instead
    (issue #314 gate G4) and deny outright when it cannot be resolved,
    rather than share this guess.
    """
    resolved = default_branch_name(directory=directory)
    if resolved is not None:
        return branch == resolved
    return branch in DEFAULT_BRANCH_FALLBACK


def _trunk_ref(remote: str) -> str:
    """`remote`'s trunk ref: its recorded `HEAD` symbolic ref, or the
    historical `{main, master}` guess when `remote` never recorded one
    (issue #304, generalizing `_origin_head_ref`'s `origin`-only read to the
    caller's own canonical remote -- `default_branch_name`/`is_default_branch`
    keep reading `origin` specifically, since GitHub-repository discovery is
    a separate axis from a repository's configured canonical remote)."""
    try:
        symbolic = _git_output(["symbolic-ref", "--quiet", f"refs/remotes/{remote}/HEAD"])
    except ClaimError:
        symbolic = ""
    if symbolic:
        return symbolic
    for candidate in (
        f"refs/remotes/{remote}/main",
        f"refs/remotes/{remote}/master",
        "main",
        "master",
    ):
        try:
            _git_output(["rev-parse", "--verify", candidate])
            return candidate
        except ClaimError:
            continue
    raise ClaimError("cannot determine the main branch for ruling age")


def _git_hex_placeholder(character: str) -> str:
    """`character`, as the git `--format=`/`%(trailers:...)` `%xHH` escape
    that makes git itself emit the raw byte at run time -- the one owner for
    every separator `_TRUNK_LANDING_LOG_FORMAT` embeds, so each byte is
    spelled once in Python and turned into git's own placeholder text here,
    never typed a second time as a literal `%x..` string. (The raw byte
    itself can never sit directly in the `--format=` argument: an argv
    string is a C string, so a literal NUL there is illegal.)"""
    return f"%x{ord(character):02x}"


# Field separator between a trunk-landing record's `sha`/`committed_at`/
# `Work-Item` trailer values/`No-Item` trailer values. NUL is also `git log
# -z`'s own record terminator, so splitting the whole raw stream on it
# (`trunk_landings`, below) reads every record's four fields *and* the
# boundary between records through the one byte git forbids inside a commit
# message -- unlike the historical `\x1f` separator this replaces, which git
# never escapes inside a trailer *value* (issue #304 review, finding B1: a
# probed value `#12\x1f#13` survived verbatim and silently split into two
# fabricated work items).
#
# Repeated trailer values within one field join on a real newline instead:
# git's own `unfold` guarantees one physical line per logical trailer value
# (a folded/wrapped continuation line is joined back into it) before that
# separator ever runs, so a value can never itself contain the byte the
# split relies on.
_TRUNK_LANDING_FIELD_SEPARATOR = "\x00"
_TRUNK_LANDING_TRAILER_VALUE_SEPARATOR = "\n"
_TRUNK_LANDING_LOG_FORMAT = (
    "%H"
    f"{_git_hex_placeholder(_TRUNK_LANDING_FIELD_SEPARATOR)}%cI"
    f"{_git_hex_placeholder(_TRUNK_LANDING_FIELD_SEPARATOR)}"
    "%(trailers:key=Work-Item,valueonly,"
    f"separator={_git_hex_placeholder(_TRUNK_LANDING_TRAILER_VALUE_SEPARATOR)},unfold)"
    f"{_git_hex_placeholder(_TRUNK_LANDING_FIELD_SEPARATOR)}"
    "%(trailers:key=No-Item,valueonly,"
    f"separator={_git_hex_placeholder(_TRUNK_LANDING_TRAILER_VALUE_SEPARATOR)},unfold)"
)


@dataclass(frozen=True)
class TrunkLanding:
    """One first-parent commit on the trunk (issue #304): its identity, when
    it landed, and -- when its own trailer block names one -- what it
    landed. `classification` is read solely from the trailer block git's own
    parsing recognizes; a `Work-Item:`/`No-Item:` line anywhere else in the
    body is prose, not evidence, so most trunk commits (not every landing is
    a dispatched slice's own merge or squash) carry `None`."""

    sha: str
    committed_at: datetime
    classification: board.TrunkClassification | board.ClassificationDefect | None


def _trailer_values(field: str) -> tuple[str, ...]:
    return tuple(field.split(_TRUNK_LANDING_TRAILER_VALUE_SEPARATOR)) if field else ()


def _parsed_trunk_landing(fields: tuple[str, str, str, str]) -> TrunkLanding:
    sha, raw_committed_at, work_item_field, no_item_field = fields
    try:
        committed_at = datetime.fromisoformat(raw_committed_at)
    except ValueError as error:
        raise ClaimError("git returned a malformed trunk landing timestamp") from error
    if committed_at.tzinfo is None:
        raise ClaimError("git returned a malformed trunk landing timestamp")
    classification = board.trunk_commit_classification(
        _trailer_values(work_item_field), _trailer_values(no_item_field)
    )
    return TrunkLanding(sha, committed_at.astimezone(UTC), classification)


def trunk_landings(remote: str, depth: int) -> tuple[TrunkLanding, ...]:
    """The most recent `depth` first-parent landings on `remote`'s trunk,
    oldest first, each classified from its own trailer block alone
    (issue #304).

    A merge counts once. Reading `remote`'s trunk — never the work branch —
    is the contract: a ruling ages with trunk, not with local commits, and
    `remote` is the caller's own canonical remote, never a hardcoded
    `origin`, so a repository configured with a different canonical remote
    ages rulings against the trunk it actually lands on.
    """
    raw = _git_output(
        [
            "log",
            "-z",
            "--first-parent",
            "--reverse",
            f"--format={_TRUNK_LANDING_LOG_FORMAT}",
            "-n",
            str(depth),
            _trunk_ref(remote),
        ]
    )
    if not raw:
        return ()
    # `-z` terminates every record -- including the last -- with the same
    # byte that separates that record's own four fields, so splitting the
    # whole stream on it leaves exactly one trailing empty token; `sha` and
    # `committed_at` are never empty, so any other shape is git misbehaving.
    fields = raw.split(_TRUNK_LANDING_FIELD_SEPARATOR)
    if fields[-1] != "" or len(fields) % 4 != 1:
        raise ClaimError("git returned a malformed trunk landing log")
    fields = fields[:-1]
    return tuple(
        _parsed_trunk_landing(
            (fields[index], fields[index + 1], fields[index + 2], fields[index + 3])
        )
        for index in range(0, len(fields), 4)
    )


def _resolved_agent(explicit: str | None) -> str:
    if explicit is not None:
        return _outbound_text(explicit, "agent", maximum=128)
    configured = os.environ.get(ACO_AGENT_ENV)
    if configured:
        return _outbound_text(configured, "agent", maximum=128)
    grok_session = os.environ.get(GROK_SESSION_ID_ENV)
    if grok_session:
        return _outbound_text(f"Grok {grok_session}", "agent", maximum=128)
    claude_session = os.environ.get(CLAUDE_SESSION_ID_ENV)
    if claude_session:
        return _outbound_text(f"Claude {claude_session}", "agent", maximum=128)
    raise ClaimError(
        "agent identity is required: pass --agent or set "
        f"{ACO_AGENT_ENV}, {GROK_SESSION_ID_ENV}, or {CLAUDE_SESSION_ID_ENV}"
    )


# One owner for `start`'s own path/branch naming scheme (issue #322): the
# same `<repo>-worktrees/issue-<n>-<slug>` / `<prefix>/issue-<n>-<slug>`
# shape `ISOLATED_WORKTREE_RECIPE` already spells out by hand, so a lane
# directory `start` creates looks exactly like one a person typed.
_SLUG_MAX_LENGTH = 40
_SLUG_COLLAPSE_PATTERN = re.compile(r"[^a-z0-9]+")


def slug_from_title(title: str) -> str:
    """`start`'s own worktree/branch slug, derived from an item's title when
    `--slug` is not given (issue #322): lowercased, every run of characters
    outside `[a-z0-9]` collapsed to one hyphen, at most 40 characters, with
    no leading or trailing hyphen. Refuses when nothing usable survives (a
    title that is empty or pure punctuation), rather than emitting a
    trailing-bare `issue-<n>-` path silently."""
    collapsed = _SLUG_COLLAPSE_PATTERN.sub("-", title.strip().lower()).strip("-")
    slug = collapsed[:_SLUG_MAX_LENGTH].rstrip("-")
    if not slug:
        raise ClaimError("no usable slug in this item's title: pass --slug explicitly")
    return slug


# The shape `slug_from_title` always produces (issue #322 review finding 3):
# an explicit `--slug` value is held to the identical rule rather than used
# verbatim, since it lands unescaped in a worktree path and a branch name.
_SLUG_SHAPE_RULE = (
    "lowercase letters, digits, and single '-' separators only, at most 40 characters, "
    "never leading or trailing '-'"
)
_SLUG_SHAPE_PATTERN = re.compile(r"[a-z0-9]+(-[a-z0-9]+)*")


def validate_slug(slug: str) -> str:
    """`slug`, refused by name when it does not match the shape
    `slug_from_title` itself always produces (issue #322 review finding 3):
    `--slug` is never sanitized the way a derived slug is, so a value with
    uppercase letters, punctuation, or a leading/trailing/doubled `-` is
    refused outright rather than silently accepted into a worktree path and
    branch name a derived slug could never produce."""
    if len(slug) > _SLUG_MAX_LENGTH or _SLUG_SHAPE_PATTERN.fullmatch(slug) is None:
        raise ClaimError(f"--slug must be {_SLUG_SHAPE_RULE}")
    return slug


def branch_prefix_for_identity() -> str:
    """`start`'s own branch prefix (issue #322) -- my inclination, not a
    settled decision: read from the same identity signals `_resolved_agent`
    reads, in the same precedence, since it is the same acting identity,
    only rendered as a short git-branch-safe token instead of a full agent
    name. The first word of `ACO_AGENT`, lowercased, when set (`"Claude head
    (coordinator)"` -> `"claude"`); otherwise `"grok"` from
    `GROK_SESSION_ID`, or `"claude"` from `CLAUDE_SESSION_ID`; refused when
    none resolves, since a worktree/branch scheme needs a real name, never a
    guess."""
    configured = os.environ.get(ACO_AGENT_ENV, "").strip()
    if configured:
        return configured.split()[0].lower()
    if os.environ.get(GROK_SESSION_ID_ENV):
        return "grok"
    if os.environ.get(CLAUDE_SESSION_ID_ENV):
        return "claude"
    raise ClaimError(
        "branch prefix is required: set ACO_AGENT, GROK_SESSION_ID, or CLAUDE_SESSION_ID"
    )


def refuse_unsafe_start_branch(branch: str, *, prefix: str) -> None:
    """Refuse `start`'s own built branch name before any git write (issue
    #322 review/gate: an unsafe `ACO_AGENT` first word used to reach `git
    worktree add` before the claim machinery ever validated the resulting
    branch, so an invalid name could leave an orphan worktree and branch
    behind). `slug_from_title`/`validate_slug` already bind the slug's own
    shape and `number` is always an int, so the one unsanitized ingredient
    left in `branch` is `prefix` -- the identity word this names as the
    refusal's cause.
    """
    if not is_safe_branch_name(branch):
        raise ClaimError(
            f"agent identity {prefix!r} is not usable in a branch name: "
            f"{branch!r} is not a safe Git ref"
        )


def branch_exists(branch: str) -> bool:
    """Whether the calling process's own checkout already has a local
    branch named `branch` (issue #322) -- `start`'s naming-collision guard,
    read the same `-C`-free way `_validate_worktree_branch` reads the
    checkout's own current branch, since `start` always runs from the
    repository whose sibling worktree it is about to create, never from an
    arbitrary resolved directory."""
    result = _git_run(["show-ref", "--verify", "--quiet", f"refs/heads/{branch}"])
    if result.exit_status == 0:
        return True
    if result.exit_status == 1:
        return False
    raise ClaimError(process.git_failure_detail(result))


def create_linked_worktree(path: Path, *, branch: str, remote: str) -> None:
    """Fetch `remote` and create a linked worktree at `path` on a fresh
    `branch`, from `remote`'s own trunk (issue #322): the two-step
    `git fetch`/`git worktree add` dance `ISOLATED_WORKTREE_RECIPE` used to
    spell out for a person to type by hand, run through this module's own
    `_git_run` chokepoint so `start` opens no new subprocess call site."""
    fetch = _git_run(["fetch", remote])
    if fetch.exit_status != 0:
        raise ClaimError(process.git_failure_detail(fetch))
    start_point = _trunk_ref(remote)
    result = _git_run(["worktree", "add", str(path), "-b", branch, start_point])
    if result.exit_status != 0:
        raise ClaimError(process.git_failure_detail(result))


# One owner for `resolve_or_create_worktree`'s two naming-collision repair
# clauses (Sonar S1192): a stray branch with no worktree of its own and an
# existing worktree checked out on the wrong branch share the identical fix.
_CHOOSE_A_DIFFERENT_WORKTREE_REPAIR = "remove it, or pass --slug to choose a different worktree"


def _refuse_foreign_worktree(path: Path, existing: PathCheckout) -> None:
    """Refuse to resume `existing` -- already resolved at `path` -- unless
    it is a linked worktree of this same repository (issue #322 review
    finding 2): the caller's own common git directory, read from its own
    cwd since `resolve_or_create_worktree` always runs from the repository
    it is building a sibling worktree for, must equal `existing`'s; a clean
    linked worktree from a different repository that merely happens to sit
    on the same branch name must never be adopted as this item's own.
    """
    if existing.toplevel != path.resolve():
        raise ClaimError(
            f"worktree {path} is not a checkout root by itself (its own toplevel is "
            f"{existing.toplevel}); {_CHOOSE_A_DIFFERENT_WORKTREE_REPAIR}"
        )
    if existing.kind is not CheckoutKind.LINKED_WORKTREE:
        raise ClaimError(
            f"worktree {path} is a repository's own main checkout, not a linked worktree; "
            f"{_CHOOSE_A_DIFFERENT_WORKTREE_REPAIR}"
        )
    own_common_directory = Path(
        _git_output(["rev-parse", "--path-format=absolute", "--git-common-dir"])
    ).resolve()
    if existing.common_directory != own_common_directory:
        raise ClaimError(
            f"worktree {path} belongs to a different repository; "
            f"{_CHOOSE_A_DIFFERENT_WORKTREE_REPAIR}"
        )


NOT_A_WORKTREE_REFUSAL = "path exists and is not a worktree of this repository"


def resolve_or_create_worktree(path: Path, branch: str, *, remote: str) -> None:
    """Create `path`'s linked worktree and `branch` when nothing sits there
    yet, or validate a prior `start`'s own worktree for resume (issue #322):
    refuses by name when `branch` is already taken by something that is not
    this worktree, when `path` resolves to a checkout this repository does
    not own (`_refuse_foreign_worktree`), when a worktree already at `path`
    is dirty, or when something -- empty or not -- already sits at `path`
    without being a worktree of this repository at all (issue #322
    review/gate finding: `git worktree add` must never be left to adopt, and
    potentially remove, an existing directory nobody offered up for this)."""
    existing = resolve_path_checkout(path)
    if existing is None:
        if path.exists():
            raise ClaimError(NOT_A_WORKTREE_REFUSAL)
        if branch_exists(branch):
            raise ClaimError(
                f"branch {branch!r} already exists and is not this item's worktree; "
                f"{_CHOOSE_A_DIFFERENT_WORKTREE_REPAIR}"
            )
        create_linked_worktree(path, branch=branch, remote=remote)
        return
    _refuse_foreign_worktree(path, existing)
    if existing.branch != branch:
        raise ClaimError(
            f"worktree {path} exists on branch {existing.branch!r}, not {branch!r}; "
            f"{_CHOOSE_A_DIFFERENT_WORKTREE_REPAIR}"
        )
    dirty = _git_output(["status", "--porcelain"], directory=path)
    if dirty:
        named = named_with_overflow_count(_dirty_paths(dirty))
        raise ClaimError(f"worktree {path} is dirty: {named}; commit or clean it before resuming")


def worktree_on_branch(paths: tuple[Path, ...], branch: str) -> Path | None:
    """The one path among `paths` (`store.list_worktrees`'s own listing)
    whose own checked-out branch is `branch`, or `None` when none matches
    (issue #322) -- `release --merged`'s own cleanup finds the lane's
    linked worktree by the branch its claim already names, not by `start`'s
    naming scheme, since a claim's worktree may predate `start` or have
    been resumed under a different `--slug`. `paths` are entries git's own
    worktree registry already vouches for, so a failure resolving one of
    them (issue #322 review/gate finding: a moved or deleted worktree
    directory, most often) is a real refusal, via `_resolve_checkout`,
    never a silently skipped "not on this branch"."""
    for candidate in paths:
        found = _resolve_checkout(candidate)
        if found.branch == branch:
            return candidate
    return None


@dataclass(frozen=True)
class WorktreeRemoval:
    """Whether `release --merged`'s own cleanup removed the lane's linked
    worktree, and why not when it did not (issue #322 review/gate finding
    4)."""

    removed: bool
    reason: str | None


@dataclass(frozen=True)
class BranchRemoval:
    """Whether the same cleanup also removed the lane's own local branch,
    tracked apart from `WorktreeRemoval` (issue #322 review/gate finding 4):
    `git worktree remove` and `git branch -d` are two separate git writes,
    so the first can succeed while the second fails, and that must never
    read as a bare `kept` that hides the worktree's own removal."""

    removed: bool
    reason: str | None


@dataclass(frozen=True)
class WorktreeCleanupOutcome:
    """`release --merged`'s own worktree/branch cleanup result, in two parts
    (issue #322 review/gate finding 4): this module owns the policy that
    decides it, `cli` only renders it."""

    worktree: WorktreeRemoval
    branch: BranchRemoval


_WORKTREE_REMOVED = WorktreeRemoval(removed=True, reason=None)
_BRANCH_REMOVED = BranchRemoval(removed=True, reason=None)


def worktree_cleanup_kept(reason: str) -> WorktreeCleanupOutcome:
    """The outcome for every reason cleanup declines before ever touching
    disk: a flag, a dirty tree, an unmerged or elsewhere-checked-out branch,
    no matching worktree, running from inside it, or a git failure resolving
    any of these -- `branch` is never separately attempted once `worktree`
    itself is kept."""
    return WorktreeCleanupOutcome(
        worktree=WorktreeRemoval(removed=False, reason=reason),
        branch=BranchRemoval(removed=False, reason=None),
    )


WORKTREE_KEPT_NOT_MERGED_REASON = "not merged into the default branch"
WORKTREE_KEPT_DIRTY_REASON = "dirty"
WORKTREE_KEPT_ELSEWHERE_REASON = "branch checked out elsewhere"


def cleanup_landed_worktree(matching: Path, branch: str, *, remote: str) -> WorktreeCleanupOutcome:
    """`release --merged`'s own cleanup policy once a lane's linked worktree
    is already found (issue #322 review/gate finding 4): merged check, then
    checked-out-elsewhere check, then a dirty check, then the worktree
    removal itself, then the branch deletion -- in that order, since
    removing a dirty or still-needed worktree is unsafe and the two git
    writes below it are each worth reporting apart. `release`'s own
    cwd-equality guard and its "no worktree matches this branch" decision
    run before this and stay the caller's own job (they need the process's
    own cwd and worktree listing, neither of which this function reads)."""
    if not branch_merged_into_default(branch, remote=remote):
        return worktree_cleanup_kept(WORKTREE_KEPT_NOT_MERGED_REASON)
    matching_checkout = resolve_path_checkout(matching)
    if matching_checkout is not None and matching_checkout.kind is CheckoutKind.MAIN:
        return worktree_cleanup_kept(WORKTREE_KEPT_ELSEWHERE_REASON)
    dirty = _git_output(["status", "--porcelain"], directory=matching)
    if dirty:
        return worktree_cleanup_kept(WORKTREE_KEPT_DIRTY_REASON)
    return remove_linked_worktree(matching, branch=branch)


def remove_linked_worktree(path: Path, *, branch: str) -> WorktreeCleanupOutcome:
    """Remove a landed lane's linked worktree and its own local branch
    (issue #322): `git worktree remove` first -- git refuses to delete a
    branch still checked out anywhere -- then `git branch -d`, both through
    this module's own `_git_run` chokepoint. Never called on the calling
    process's own checkout: `release`'s own cwd-equality guard runs first,
    since a worktree cannot remove its own cwd. A worktree-removal failure
    still raises loud (nothing on disk has changed yet); a branch-deletion
    failure once the worktree is already gone returns a typed outcome
    instead (issue #322 review/gate finding 4), so that success is never
    lost behind a bare `kept`."""
    result = _git_run(["worktree", "remove", str(path)])
    if result.exit_status != 0:
        raise ClaimError(process.git_failure_detail(result))
    result = _git_run(["branch", "-d", branch])
    if result.exit_status != 0:
        return WorktreeCleanupOutcome(
            worktree=_WORKTREE_REMOVED,
            branch=BranchRemoval(
                removed=False, reason=f"git failure: {process.git_failure_detail(result)}"
            ),
        )
    return WorktreeCleanupOutcome(worktree=_WORKTREE_REMOVED, branch=_BRANCH_REMOVED)


def branch_merged_into_default(branch: str, *, remote: str) -> bool:
    """Whether `branch`'s tip is already an ancestor of `remote`'s own
    trunk (issue #322): `release --merged`'s own cleanup precondition,
    fetching `remote` first so a landing this same process just verified
    through the forge is visible locally even when nothing else in this
    checkout has fetched since. A squash or rebase landing's trunk commit is
    never a literal descendant of the lane branch's own tip, so this reads
    `False` for one -- a safe, conservative "not merged" that only ever
    skips cleanup, never removes a branch git cannot itself prove is in."""
    fetch = _git_run(["fetch", remote])
    if fetch.exit_status != 0:
        raise ClaimError(process.git_failure_detail(fetch))
    trunk = _trunk_ref(remote)
    result = _git_run(["merge-base", "--is-ancestor", branch, trunk])
    if result.exit_status == 0:
        return True
    if result.exit_status == 1:
        return False
    raise ClaimError(process.git_failure_detail(result))
