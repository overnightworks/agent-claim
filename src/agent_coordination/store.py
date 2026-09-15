"""`refs/aco/state` store: the fast-forward CAS transport for the claim state tree.

Sibling of `checkout`, not below it. `checkout`'s owner is this working tree --
current branch, isolation, cleanliness, agent identity -- and its git calls are
local (`rev-parse`, `ls-files`, `status`, `branch`). This module's owner is
repository-global state: a remote compare-and-swap ref, reached through
`ls-remote`, `FETCH_HEAD`, plumbing, push, and retry. Folding the two would
let a worktree-local module own repository-global state -- exactly the
linked-worktree stamp collision `_lineage_stamp_path` exists to avoid.

`cli` (issue #176, slice C2) is this module's production caller: `bootstrap`
and `commit_transition`. It never checks the state ref out: every read goes
through plumbing (`ls-remote`, `fetch` to `FETCH_HEAD`, one recursive
`ls-tree` and one `archive`, never a `cat-file` per entry), and every write
builds a tree with `hash-object`/`mktree` -- reusing whatever a
transition's already-committed parent tree still carries unchanged (issue
#241) -- and a commit with `commit-tree`.
"""

from __future__ import annotations

import os
import tarfile
import tempfile
import uuid
from collections.abc import Callable, Iterable, Mapping
from contextlib import suppress
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, TypeVar

from . import process
from .protocol import (
    CLAIM_ID_PATTERN,
    EMPTY_STATE,
    MISSING_STATE_REF,
    ActiveClaim,
    ClaimError,
    ClaimId,
    ClaimIntent,
    ClaimState,
    ClaimTransitionIntent,
    ClaimUnavailableError,
    MalformedStateTreeError,
    ObjectId,
    OperationAlreadyApplied,
    PushRejectedError,
    ReleaseIntent,
    RescopeIntent,
    ResourceRecord,
    StateLineageError,
    apply,
    parse_claim_toml,
    parse_resource_toml,
    parse_schema_toml,
    serialize_claim_toml,
    serialize_empty_schema_toml,
    serialize_resource_toml,
)

STATE_REF = "refs/aco/state"
DEFAULT_CANONICAL_REMOTE = "origin"
CLAIMS_DIRECTORY = "claims"
IDS_DIRECTORY = "ids"
RESOURCES_DIRECTORY = "resources"
SCHEMA_TOML_FILENAME = "schema.toml"
TOML_SUFFIX = ".toml"
_STATE_TOP_LEVEL_NAMES = frozenset(
    {SCHEMA_TOML_FILENAME, CLAIMS_DIRECTORY, IDS_DIRECTORY, RESOURCES_DIRECTORY}
)

# The transition each intent type carries, for the commit message trailer
# (§1 "Commit message"): `intent: claim` / `rescope` / `release`.
_INTENT_LABELS: dict[type[ClaimTransitionIntent], str] = {
    ClaimIntent: "claim",
    RescopeIntent: "rescope",
    ReleaseIntent: "release",
}

# `git ls-remote --exit-code` (git(1)): 2 is "no matching refs" -- the only
# outcome this store ever reads as `EMPTY_STATE` (criterion 6). 128 is the
# generic auth/transport failure and must never be read as empty.
_LS_REMOTE_EXIT_NO_MATCH = 2

# Internal bound on the push-retry loop below (criterion 3's seam). Distinct
# from `_MAX_TRANSITION_ATTEMPTS`: this loop only ever contends over
# `bootstrap`'s fixed empty-tree commit, a narrower race than a live claim.
_MAX_PUSH_ATTEMPTS = 8

# Retry exhaustion for a live claim/rescope/release transition (criterion 5):
# 32 attempts, then `ClaimUnavailableError("... moved 32 times; retry the
# command")` -- never "held by X" for a different-key loser.
_MAX_TRANSITION_ATTEMPTS = 32

# The fallback detail every git-transport failure message falls back to when
# git's own stderr/stdout carried nothing readable.
_UNKNOWN_GIT_FAILURE = "unknown git failure"

_LINEAGE_STAMP_DIRECTORY = "aco"
_LINEAGE_STAMP_FILENAME = "last-oid"


class PushTransport(Protocol):
    """The store's injectable push boundary (criterion 3's seam).

    A production implementation performs an ordinary `git push`. A test fake
    can additionally advance the observed remote state and *then* raise, to
    reproduce a lost response after the remote actually accepted the push --
    the retry loop below treats every raise identically: re-fetch and look
    for this attempt's `operation_id` before assuming nothing landed.
    """

    def push(self, *, worktree: Path, remote: str, ref: str, new_oid: ObjectId) -> None:
        """Fast-forward `ref` to `new_oid` on `remote`. Raise `PushRejectedError`
        when the push did not observably land."""
        ...


class GitPushTransport:
    """Production push boundary: a plain fast-forward `git push`, never
    `--force`/`--force-with-lease` (a matching lease can still replace
    history; only the documented recovery procedure forces)."""

    def push(self, *, worktree: Path, remote: str, ref: str, new_oid: ObjectId) -> None:
        result = _run_git(worktree, ["push", remote, f"{new_oid}:{ref}"])
        if result.exit_status != 0:
            detail = result.stderr.decode().strip() or result.stdout.decode().strip()
            raise PushRejectedError(detail or f"git push exited {result.exit_status}")


def _run_git(worktree: Path, arguments: list[str]) -> process.CapturedResult:
    try:
        return process.run_captured(["git", "-C", str(worktree), *arguments])
    except process.ExecutableMissingError as error:
        raise ClaimError("git is required for the claim state store") from error
    except process.ProcessTimedOutError as error:
        raise ClaimError("git timed out while reading the claim state store") from error


def _run_git_with_input(worktree: Path, arguments: list[str], *, input_data: bytes) -> str:
    command = ["git", "-C", str(worktree), *arguments]
    try:
        result = process.run_bounded(command, input_data=input_data)
    except process.ExecutableMissingError as error:
        raise ClaimError("git is required for the claim state store") from error
    except process.ProcessTimedOutError as error:
        raise ClaimError("git timed out while writing to the claim state store") from error
    if result.exit_status != 0:
        raise ClaimError(result.output.decode(errors="replace").strip() or "git command failed")
    return result.output.decode().strip()


def _git_dir(worktree: Path) -> Path:
    """This worktree's own git-dir, absolute and per-worktree.

    Never the shared common dir: a linked worktree's `--absolute-git-dir` is
    `.git/worktrees/<name>`, distinct from the main checkout's `.git`, which
    is exactly what keeps the lineage stamp below from colliding across
    worktrees that share one repository.
    """
    result = _run_git(worktree, ["rev-parse", "--absolute-git-dir"])
    if result.exit_status != 0:
        raise ClaimError(result.stderr.decode().strip() or "cannot resolve git-dir")
    return Path(result.stdout.decode().strip())


def _lineage_stamp_path(worktree: Path) -> Path:
    return _git_dir(worktree) / _LINEAGE_STAMP_DIRECTORY / _LINEAGE_STAMP_FILENAME


def _read_lineage_stamp(worktree: Path) -> ObjectId | None:
    try:
        content = _lineage_stamp_path(worktree).read_text().strip()
    except FileNotFoundError:
        return None
    return ObjectId(content) if content else None


def _write_lineage_stamp(worktree: Path, tip: ObjectId) -> None:
    """Record this worktree's last-observed tip via temp file + `os.replace`.

    The write is local to this worktree's own git-dir, so two linked
    worktrees fetching concurrently write two distinct files and never race
    each other's stamp.
    """
    stamp_path = _lineage_stamp_path(worktree)
    stamp_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temp_name = tempfile.mkstemp(dir=stamp_path.parent, prefix=".last-oid-")
    try:
        with os.fdopen(descriptor, "w") as handle:
            handle.write(f"{tip}\n")
        os.replace(temp_name, stamp_path)
    except BaseException:
        with suppress(OSError):
            os.remove(temp_name)
        raise


def _check_lineage(worktree: Path, tip: ObjectId) -> None:
    """Refuse a fetched tip this worktree's own history cannot reach.

    Cannot see: a rewrite that branched before this worktree's first fetch,
    or one this worktree has simply never observed before -- a missing stamp
    is silently accepted as a first observation, not a lineage break.
    """
    previous = _read_lineage_stamp(worktree)
    if previous is None or previous == tip:
        return
    result = _run_git(worktree, ["merge-base", "--is-ancestor", str(previous), str(tip)])
    if result.exit_status != 0:
        raise StateLineageError(
            f"{STATE_REF} moved from {previous} to {tip} without {previous} as an "
            "ancestor of the new tip; the ref may have been rewritten"
        )


# Field separator for the batched `git log` read below: %x09 is git's own
# escape for a literal tab, unambiguous inside a `--format` string.
_LOG_FORMAT = "%H%x09%cI"


def _first_parent_commit_dates(worktree: Path, tip: ObjectId) -> dict[ObjectId, datetime]:
    """Every commit reachable from `tip` by first-parent descent, mapped to
    its committer date, in one `git log` call.

    `refs/aco/state`'s own history is always linear by construction --
    `commit_transition` never writes a merge, every retry parents its new
    commit onto a freshly observed tip (issue #241, "never a merge") -- so a
    first-parent walk from `tip` reaches every commit the ref has ever held.
    """
    result = _run_git(worktree, ["log", "--first-parent", f"--format={_LOG_FORMAT}", str(tip)])
    if result.exit_status != 0:
        raise ClaimError(f"cannot read the commit history of {tip}")
    dates: dict[ObjectId, datetime] = {}
    for line in result.stdout.decode().splitlines():
        commit_hex, _, raw_date = line.partition("\t")
        try:
            parsed = datetime.fromisoformat(raw_date)
        except ValueError as error:
            raise ClaimError(f"git returned a malformed committer date for {commit_hex}") from error
        dates[ObjectId(commit_hex)] = parsed.astimezone(UTC)
    return dates


def claim_ages(
    *, worktree: Path, tip: ObjectId, claims: Iterable[ActiveClaim]
) -> dict[str, datetime]:
    """Each `claim`'s age -- its `opened_commit`'s committer date -- from one
    walk of `tip`'s history (issue #242, replacing a `merge-base` plus `log`
    pair per claim).

    Refuses with `StateLineageError` for a claim whose `opened_commit` the
    walk never reaches (§1 "Status age..."): a claim's age display reads
    real history, it never guesses across a lineage break.
    """
    claim_list = tuple(claims)
    if not claim_list:
        return {}
    dates = _first_parent_commit_dates(worktree, tip)
    ages: dict[str, datetime] = {}
    for claim in claim_list:
        try:
            ages[claim.claim_id] = dates[claim.opened_commit]
        except KeyError:
            raise StateLineageError(
                f"{claim.opened_commit} is not an ancestor of {tip}; the ref may "
                "have been rewritten"
            ) from None
    return ages


def _ls_remote_state(worktree: Path, remote: str) -> ObjectId | None:
    """Probe `STATE_REF` on `remote` without fetching it.

    Only `_LS_REMOTE_EXIT_NO_MATCH` (2, "no matching refs") is ever read as
    absent (criterion 6): every other nonzero exit -- 128 and any other code
    git might use -- is an auth or transport failure and must fail loud
    instead of being mistaken for emptiness.
    """
    result = _run_git(worktree, ["ls-remote", "--exit-code", remote, STATE_REF])
    if result.exit_status == 0:
        oid, _, _ref = result.stdout.decode().splitlines()[0].partition("\t")
        return ObjectId(oid)
    if result.exit_status == _LS_REMOTE_EXIT_NO_MATCH:
        return None
    detail = (
        result.stderr.decode().strip() or result.stdout.decode().strip() or _UNKNOWN_GIT_FAILURE
    )
    raise ClaimError(
        f"cannot reach {remote} {STATE_REF}: auth or transport failure "
        f"(ls-remote exited {result.exit_status}): {detail}"
    )


def _fetch_to_fetch_head(worktree: Path, remote: str) -> None:
    """Fetch `STATE_REF` into this worktree's own `FETCH_HEAD` only.

    No destination refspec is given, so production never creates
    `refs/aco/state` in the shared local namespace -- the oid comes back
    from `FETCH_HEAD`, read by `_read_fetch_head`.
    """
    result = _run_git(worktree, ["fetch", remote, STATE_REF])
    if result.exit_status != 0:
        detail = result.stderr.decode().strip() or _UNKNOWN_GIT_FAILURE
        raise ClaimError(f"cannot fetch {remote} {STATE_REF}: {detail}")


def _read_fetch_head(worktree: Path) -> ObjectId:
    fetch_head = _git_dir(worktree) / "FETCH_HEAD"
    first_line = fetch_head.read_text().splitlines()[0]
    oid, _, _rest = first_line.partition("\t")
    return ObjectId(oid)


def _tree_oid(worktree: Path, tip: ObjectId) -> ObjectId:
    result = _run_git(worktree, ["rev-parse", f"{tip}^{{tree}}"])
    if result.exit_status != 0:
        raise MalformedStateTreeError(f"cannot resolve the tree for {tip}")
    return ObjectId(result.stdout.decode().strip())


def _list_tree(
    worktree: Path, tree_ref: ObjectId, *, tip: ObjectId, context: str
) -> dict[str, tuple[str, str]]:
    """`{path: (kind, oid)}` for every entry under `tree_ref`, at every depth.

    `tree_ref` may be a tree oid or a commit (git dereferences a commit to
    its tree); `-t` keeps intermediate tree entries in the recursive listing
    git would otherwise omit, so a caller can validate `claims`/`ids`/
    `resources` themselves as well as their direct children from this one
    call (issue #241) -- the read side's bulk-listing counterpart to
    `_read_state_archive`, and the write side's own lookup of what it may
    reuse unchanged.
    """
    listing = _run_git(worktree, ["ls-tree", "-r", "-t", str(tree_ref)])
    if listing.exit_status != 0:
        raise MalformedStateTreeError(f"cannot list the {context} tree {tree_ref} at {tip}")
    entries: dict[str, tuple[str, str]] = {}
    for line in listing.stdout.decode().splitlines():
        mode_type, _, path = line.partition("\t")
        _mode, kind, oid = mode_type.split(" ")
        entries[path] = (kind, oid)
    return entries


def _direct_children(
    entries: dict[str, tuple[str, str]], directory: str
) -> dict[str, tuple[str, str]]:
    """Only `directory`'s immediate children from a full recursive `_list_tree`
    listing, keyed by their own name -- never a nested descendant: `claims`,
    `ids`, and `resources` are flat directories by contract, so a deeper path
    is exactly the malformed shape the caller must reject, the same shape a
    non-recursive `ls-tree` on the subtree's own oid used to reject.
    """
    prefix = f"{directory}/"
    return {
        path.removeprefix(prefix): value
        for path, value in entries.items()
        if path.startswith(prefix) and "/" not in path.removeprefix(prefix)
    }


def _extract_archive_member(archive: tarfile.TarFile, member: tarfile.TarInfo) -> bytes:
    handle = archive.extractfile(member)
    assert handle is not None  # `member.isfile()` guarantees an extractable stream
    return handle.read()


def _read_state_archive(worktree: Path, tree_oid: ObjectId, *, tip: ObjectId) -> dict[str, bytes]:
    """Every blob's raw bytes under `tree_oid`, read via one `git archive`
    instead of one `cat-file -p` per file (issue #241, audit findings 20-21):
    a state tree with hundreds of claims used to cost one process per file
    to read; this costs one regardless of how many. `tarfile` owns the
    framing, never a hand-rolled parse of git's batch output.

    A missing object fails the whole `git archive` loud, the same doctrine a
    per-blob read used to enforce (ruling 9c): a broken tree is corrupt
    state, never a single quarantinable claim.
    """
    result = _run_git(worktree, ["archive", "--format=tar", str(tree_oid)])
    if result.exit_status != 0:
        detail = result.stderr.decode().strip() or _UNKNOWN_GIT_FAILURE
        raise MalformedStateTreeError(f"cannot read the state tree at {tip}: {detail}")
    try:
        with tarfile.open(fileobj=BytesIO(result.stdout), mode="r:") as archive:
            return {
                member.name: _extract_archive_member(archive, member)
                for member in archive.getmembers()
                if member.isfile()
            }
    except tarfile.TarError as error:
        raise MalformedStateTreeError(
            f"cannot read the state tree at {tip}: malformed archive"
        ) from error


def _read_schema_toml(
    top_level: dict[str, tuple[str, str]], archive: dict[str, bytes], *, tip: ObjectId
) -> str:
    if SCHEMA_TOML_FILENAME not in top_level:
        raise MalformedStateTreeError(f"state tree at {tip} is missing {SCHEMA_TOML_FILENAME}")
    kind, _oid = top_level[SCHEMA_TOML_FILENAME]
    if kind != "blob":
        raise MalformedStateTreeError(f"{SCHEMA_TOML_FILENAME} at {tip} is not a blob")
    return archive[SCHEMA_TOML_FILENAME].decode()


def _subtree_oid(
    top_entries: dict[str, tuple[str, str]], name: str, *, tip: ObjectId
) -> ObjectId | None:
    if name not in top_entries:
        return None
    kind, oid = top_entries[name]
    if kind != "tree":
        raise MalformedStateTreeError(f"{name} at {tip} is not a directory")
    return ObjectId(oid)


def _parse_claims_subtree(
    entries: dict[str, tuple[str, str]], archive: dict[str, bytes], *, present: bool, tip: ObjectId
) -> Mapping[str, ActiveClaim]:
    if not present:
        return MappingProxyType({})
    claims: dict[str, ActiveClaim] = {}
    for name, (kind, _oid) in _direct_children(entries, CLAIMS_DIRECTORY).items():
        if kind != "blob" or not name.endswith(TOML_SUFFIX):
            raise MalformedStateTreeError(f"{CLAIMS_DIRECTORY}/{name} at {tip} is not a claim file")
        key = name.removesuffix(TOML_SUFFIX)
        content = archive[f"{CLAIMS_DIRECTORY}/{name}"].decode()
        claims[key] = parse_claim_toml(content, key=key, tip=tip)
    return MappingProxyType(claims)


def _parse_ids_subtree(
    entries: dict[str, tuple[str, str]], *, present: bool, tip: ObjectId
) -> frozenset[ClaimId]:
    if not present:
        return frozenset()
    consumed: set[ClaimId] = set()
    for name, (kind, _oid) in _direct_children(entries, IDS_DIRECTORY).items():
        if kind != "blob" or CLAIM_ID_PATTERN.fullmatch(name) is None:
            raise MalformedStateTreeError(f"{IDS_DIRECTORY}/{name} at {tip} is not a claim id")
        consumed.add(ClaimId(name))
    return frozenset(consumed)


def _parse_resources_subtree(
    entries: dict[str, tuple[str, str]], archive: dict[str, bytes], *, present: bool, tip: ObjectId
) -> Mapping[str, ResourceRecord]:
    if not present:
        return MappingProxyType({})
    resources: dict[str, ResourceRecord] = {}
    for name, (kind, _oid) in _direct_children(entries, RESOURCES_DIRECTORY).items():
        if kind != "blob" or not name.endswith(TOML_SUFFIX):
            raise MalformedStateTreeError(
                f"{RESOURCES_DIRECTORY}/{name} at {tip} is not a resource file"
            )
        resource_name = name.removesuffix(TOML_SUFFIX)
        content = archive[f"{RESOURCES_DIRECTORY}/{name}"].decode()
        resources[resource_name] = parse_resource_toml(content, name=resource_name, tip=tip)
    return MappingProxyType(resources)


def _parse_state_tree(worktree: Path, tip: ObjectId) -> ClaimState:
    """Parse the full state tree at `tip`: `schema.toml` plus whichever of
    `claims/`, `ids/`, `resources/` are present (issue #176, slice C2).

    One recursive `ls-tree` for structure and one `git archive` for every
    blob's bytes (issue #241) replace what used to be one `ls-tree` and one
    `cat-file -p` per entry -- the process count this function pays is fixed
    regardless of how many claims, ids, or resources the tree holds.

    A defect anywhere fails the whole read loud (ruling 9c): a commit is the
    unit a writer writes, so a broken tree is corrupt state, never a single
    quarantinable claim.
    """
    tree_oid = _tree_oid(worktree, tip)
    entries = _list_tree(worktree, tree_oid, tip=tip, context="state")
    top_level = {name: value for name, value in entries.items() if "/" not in name}
    unknown = set(top_level) - _STATE_TOP_LEVEL_NAMES
    if unknown:
        raise MalformedStateTreeError(f"state tree at {tip} has unknown entries: {sorted(unknown)}")
    archive = _read_state_archive(worktree, tree_oid, tip=tip)
    parse_schema_toml(_read_schema_toml(top_level, archive, tip=tip), tip=tip)
    return ClaimState(
        tip=tip,
        claims=_parse_claims_subtree(
            entries,
            archive,
            present=_subtree_oid(top_level, CLAIMS_DIRECTORY, tip=tip) is not None,
            tip=tip,
        ),
        consumed_ids=_parse_ids_subtree(
            entries,
            present=_subtree_oid(top_level, IDS_DIRECTORY, tip=tip) is not None,
            tip=tip,
        ),
        resources=_parse_resources_subtree(
            entries,
            archive,
            present=_subtree_oid(top_level, RESOURCES_DIRECTORY, tip=tip) is not None,
            tip=tip,
        ),
    )


def fetch_state(*, worktree: Path, remote: str = DEFAULT_CANONICAL_REMOTE) -> ClaimState:
    """Read `refs/aco/state` from `remote` without ever checking it out.

    `EmptyState` only for a proven-absent ref (`ls-remote` exit 2). A present
    ref is fetched to this worktree's own `FETCH_HEAD` (never a local ref),
    parsed via plumbing, lineage-checked against this worktree's own last
    observation, and re-stamped.
    """
    probed = _ls_remote_state(worktree, remote)
    if probed is None:
        stamp = _read_lineage_stamp(worktree)
        if stamp is not None:
            raise StateLineageError(
                f"{STATE_REF} was previously observed at {stamp} but is now absent; "
                "the ref may have been deleted"
            )
        return EMPTY_STATE
    _fetch_to_fetch_head(worktree, remote)
    tip = _read_fetch_head(worktree)
    state = _parse_state_tree(worktree, tip)
    _check_lineage(worktree, tip)
    _write_lineage_stamp(worktree, tip)
    return state


def _commit_tree(
    worktree: Path, *, tree_oid: ObjectId, parent: ObjectId | None, message: str
) -> ObjectId:
    arguments = ["commit-tree", str(tree_oid), "-m", message]
    if parent is not None:
        arguments += ["-p", str(parent)]
    result = _run_git(worktree, arguments)
    if result.exit_status != 0:
        raise ClaimError(result.stderr.decode().strip() or "commit-tree failed")
    return ObjectId(result.stdout.decode().strip())


def _find_operation_id(
    worktree: Path, *, since: ObjectId | None, until: ObjectId, operation_id: str
) -> ObjectId | None:
    """Search new commits on `refs/aco/state` for one carrying `operation_id`.

    One `git log -1` per candidate commit rather than a single delimited
    dump: the range this ever searches is a handful of commits contending
    over one push, not a hot path, so the simplest correct parse wins.

    Both ends of the range are already-resolved commits by the time the
    retry loop calls this (its own just-built commit, and a tip
    `fetch_state` just parsed), so a failing walk is a broken invariant, not
    an absent id: reading it as "not found" would let a lost response whose
    commit already landed be pushed a second time.
    """
    range_argument = f"{since}..{until}" if since is not None else str(until)
    listing = _run_git(worktree, ["log", "--format=%H", range_argument])
    if listing.exit_status != 0:
        detail = listing.stderr.decode().strip() or _UNKNOWN_GIT_FAILURE
        raise ClaimError(
            f"cannot search {range_argument} for operation_id {operation_id}: {detail}"
        )
    needle = f"operation_id: {operation_id}"
    for candidate in listing.stdout.decode().split():
        message = _run_git(worktree, ["log", "-1", "--format=%B", candidate])
        if message.exit_status == 0 and needle in message.stdout.decode():
            return ObjectId(candidate)
    return None


@dataclass(frozen=True)
class PendingCommit:
    """One not-yet-landed transition: the tree it writes and the commit
    message carrying its `operation_id`, kept together so a retry re-applies
    the same write rather than drifting from it."""

    tree_oid: ObjectId
    message: str
    operation_id: str


def push_tree(
    *,
    worktree: Path,
    remote: str,
    observed: ClaimState,
    pending: PendingCommit,
    transport: PushTransport,
) -> ObjectId | OperationAlreadyApplied:
    """Commit `pending` onto `observed.tip` and push it as the new state tip.

    Retries against a moved tip (non-fast-forward, or a lost response after
    the remote actually advanced) until the push lands or `pending`'s
    `operation_id` is found already applied by a concurrent writer
    (criterion 3) -- never re-applying it a second time.
    """
    parent = observed.tip
    for _attempt in range(_MAX_PUSH_ATTEMPTS):
        new_commit = _commit_tree(
            worktree, tree_oid=pending.tree_oid, parent=parent, message=pending.message
        )
        try:
            transport.push(worktree=worktree, remote=remote, ref=STATE_REF, new_oid=new_commit)
        except PushRejectedError:
            refreshed = fetch_state(worktree=worktree, remote=remote)
            if refreshed.tip is not None:
                found = _find_operation_id(
                    worktree, since=parent, until=refreshed.tip, operation_id=pending.operation_id
                )
                if found is not None:
                    return OperationAlreadyApplied(tip=refreshed.tip)
            parent = refreshed.tip
            continue
        _write_lineage_stamp(worktree, new_commit)
        return new_commit
    raise ClaimUnavailableError(f"{STATE_REF} moved {_MAX_PUSH_ATTEMPTS} times; retry the command")


def _write_blob(worktree: Path, content: str) -> ObjectId:
    return ObjectId(
        _run_git_with_input(worktree, ["hash-object", "-w", "--stdin"], input_data=content.encode())
    )


def _empty_blob_oid(worktree: Path) -> ObjectId:
    return ObjectId(_run_git_with_input(worktree, ["hash-object", "-w", "--stdin"], input_data=b""))


# `(mode, kind, oid, name)`, git's own `ls-tree`/`mktree` entry shape.
_TreeEntry = tuple[str, str, ObjectId, str]


def _mktree(worktree: Path, entries: list[_TreeEntry]) -> ObjectId:
    ordered = sorted(entries, key=lambda entry: entry[3])
    mktree_input = "".join(f"{mode} {kind} {oid}\t{name}\n" for mode, kind, oid, name in ordered)
    return ObjectId(_run_git_with_input(worktree, ["mktree"], input_data=mktree_input.encode()))


def _write_bootstrap_tree(worktree: Path) -> ObjectId:
    """The one tree `bootstrap` ever writes: `schema.toml` alone (issue #176
    slice C1). Every later transition's tree comes from
    `_write_incremental_state_tree` instead (issue #241), which diffs
    against an already-committed tree -- exactly what bootstrap's own first
    commit has none of yet.
    """
    schema_blob = _write_blob(worktree, serialize_empty_schema_toml())
    return _mktree(worktree, [("100644", "blob", schema_blob, SCHEMA_TOML_FILENAME)])


_SubtreeValueT = TypeVar("_SubtreeValueT")


@dataclass(frozen=True)
class _ExistingSubtree:
    """One subtree's already-committed shape from `observed.tip` (issue
    #241): its own oid (`None` when the directory did not exist yet) and its
    direct children's `(kind, oid)` by bare name -- exactly what the
    incremental writer needs to decide what it may reuse, and no more, so
    the writers below stay at one `existing`-shaped parameter each.
    """

    oid: str | None
    children: dict[str, tuple[str, str]]


def _existing_subtree(existing: dict[str, tuple[str, str]], directory: str) -> _ExistingSubtree:
    entry = existing.get(directory)
    return _ExistingSubtree(
        oid=entry[1] if entry is not None else None,
        children=_direct_children(existing, directory),
    )


def _reuse_or_write_mapping_subtree(
    worktree: Path,
    *,
    existing: _ExistingSubtree,
    old_members: Mapping[str, _SubtreeValueT],
    new_members: Mapping[str, _SubtreeValueT],
    serialize: Callable[[_SubtreeValueT], str],
) -> ObjectId:
    """A `claims/`- or `resources/`-shaped subtree's new oid, reusing every
    member unchanged since `old_members` (issue #241): frozen-dataclass
    equality on the parsed record is exactly serialization equality --
    `serialize` is a pure function of the record's fields -- so an identical
    record needs neither a new blob nor a new tree; only `mktree` for a
    directory that actually changed, never once per unchanged entry.
    """
    if old_members == new_members and existing.oid is not None:
        return ObjectId(existing.oid)
    entries: list[_TreeEntry] = []
    for name, value in new_members.items():
        entry_name = f"{name}{TOML_SUFFIX}"
        if old_members.get(name) == value:
            _kind, oid = existing.children[entry_name]
            entries.append(("100644", "blob", ObjectId(oid), entry_name))
        else:
            entries.append(("100644", "blob", _write_blob(worktree, serialize(value)), entry_name))
    return _mktree(worktree, entries)


def _reuse_or_write_ids_subtree(
    worktree: Path,
    *,
    existing: _ExistingSubtree,
    old_ids: frozenset[ClaimId],
    new_ids: frozenset[ClaimId],
) -> ObjectId:
    """`ids/`'s new subtree oid (issue #241): every id's blob is the same
    empty content, so reuse is membership-only -- the empty blob is written
    at most once per call, never once per newly consumed id.
    """
    if old_ids == new_ids and existing.oid is not None:
        return ObjectId(existing.oid)
    empty_blob: ObjectId | None = None
    entries: list[_TreeEntry] = []
    for claim_id in new_ids:
        if claim_id in old_ids:
            _kind, oid = existing.children[claim_id]
            entries.append(("100644", "blob", ObjectId(oid), claim_id))
        else:
            if empty_blob is None:
                empty_blob = _empty_blob_oid(worktree)
            entries.append(("100644", "blob", empty_blob, claim_id))
    return _mktree(worktree, entries)


def _write_incremental_state_tree(
    worktree: Path, *, observed: ClaimState, new_state: ClaimState
) -> ObjectId:
    """`new_state`'s tree, reusing every blob and subtree oid `observed.tip`'s
    already-committed tree still carries unchanged (issue #241): the write
    seam inside `commit_transition`'s retry loop, read fresh every attempt
    against that attempt's own `observed.tip` -- never lifted above the
    loop, never cached on `ClaimState` itself, which stays free of this
    adapter detail.

    `schema.toml` never changes after bootstrap, so its oid is always
    reused; each of `claims/`/`ids/`/`resources/` costs `mktree` only when
    it actually differs from `observed`, plus one `mktree` for the top --
    the process count below is fixed regardless of the tree's size. Unlike
    `_write_bootstrap_tree`, which has no prior commit to diff against on a
    ref's very first write.
    """
    assert observed.tip is not None  # commit_transition already refused a missing ref
    existing = _list_tree(worktree, observed.tip, tip=observed.tip, context="state")
    top_entries: list[_TreeEntry] = [
        ("100644", "blob", ObjectId(existing[SCHEMA_TOML_FILENAME][1]), SCHEMA_TOML_FILENAME)
    ]
    if new_state.claims:
        top_entries.append(
            (
                "040000",
                "tree",
                _reuse_or_write_mapping_subtree(
                    worktree,
                    existing=_existing_subtree(existing, CLAIMS_DIRECTORY),
                    old_members=observed.claims,
                    new_members=new_state.claims,
                    serialize=serialize_claim_toml,
                ),
                CLAIMS_DIRECTORY,
            )
        )
    if new_state.consumed_ids:
        top_entries.append(
            (
                "040000",
                "tree",
                _reuse_or_write_ids_subtree(
                    worktree,
                    existing=_existing_subtree(existing, IDS_DIRECTORY),
                    old_ids=observed.consumed_ids,
                    new_ids=new_state.consumed_ids,
                ),
                IDS_DIRECTORY,
            )
        )
    if new_state.resources:
        top_entries.append(
            (
                "040000",
                "tree",
                _reuse_or_write_mapping_subtree(
                    worktree,
                    existing=_existing_subtree(existing, RESOURCES_DIRECTORY),
                    old_members=observed.resources,
                    new_members=new_state.resources,
                    serialize=serialize_resource_toml,
                ),
                RESOURCES_DIRECTORY,
            )
        )
    return _mktree(worktree, top_entries)


def _transition_message(subject: str, intent: ClaimTransitionIntent) -> str:
    return (
        f"{subject}\n\n"
        f"operation_id: {intent.operation_id}\n"
        f"claim_id: {intent.claim_id}\n"
        f"intent: {_INTENT_LABELS[type(intent)]}\n"
    )


def commit_transition(
    *,
    worktree: Path,
    subject: str,
    intent: ClaimTransitionIntent,
    remote: str = DEFAULT_CANONICAL_REMOTE,
    transport: PushTransport | None = None,
) -> ClaimState:
    """Fetch, apply, and push one claim/rescope/release transition (issue
    #176, slice C2): the production caller of `protocol.apply`.

    Unlike `push_tree`'s fixed bootstrap tree, a transition's result depends
    on the state it is applied to, so every retry attempt re-fetches and
    re-applies `intent` to the fresh observed state instead of reusing a
    stale tree -- the same seam (criterion 3), generalized.
    """
    transport = transport or GitPushTransport()
    observed = fetch_state(worktree=worktree, remote=remote)
    if observed.tip is None:
        raise ClaimError(MISSING_STATE_REF)
    for _attempt in range(_MAX_TRANSITION_ATTEMPTS):
        new_state = apply(observed, intent)
        new_tree = _write_incremental_state_tree(worktree, observed=observed, new_state=new_state)
        new_commit = _commit_tree(
            worktree,
            tree_oid=new_tree,
            parent=observed.tip,
            message=_transition_message(subject, intent),
        )
        try:
            transport.push(worktree=worktree, remote=remote, ref=STATE_REF, new_oid=new_commit)
        except PushRejectedError:
            refreshed = fetch_state(worktree=worktree, remote=remote)
            if refreshed.tip is not None:
                found = _find_operation_id(
                    worktree,
                    since=observed.tip,
                    until=refreshed.tip,
                    operation_id=intent.operation_id,
                )
                if found is not None:
                    return refreshed
            observed = refreshed
            continue
        _write_lineage_stamp(worktree, new_commit)
        return ClaimState(
            tip=new_commit,
            claims=new_state.claims,
            consumed_ids=new_state.consumed_ids,
            resources=new_state.resources,
        )
    raise ClaimUnavailableError(
        f"{STATE_REF} moved {_MAX_TRANSITION_ATTEMPTS} times; retry the command"
    )


def bootstrap(
    *,
    worktree: Path,
    remote: str = DEFAULT_CANONICAL_REMOTE,
    transport: PushTransport | None = None,
) -> ObjectId:
    """Create `refs/aco/state` at an empty state tree if it is proven absent;
    otherwise report the existing tip untouched.

    A present ref is a pure read (no write); an absent ref (`ls-remote` exit
    2) gets one commit holding only `schema.toml`; an unreachable ref
    (auth/transport, exit 128 and friends) fails loud from `fetch_state`
    before either branch runs. A worktree that has observed the ref and
    later finds it absent is a lineage error, not a fresh bootstrap.
    """
    observed = fetch_state(worktree=worktree, remote=remote)
    if observed.tip is not None:
        return observed.tip
    operation_id = uuid.uuid4().hex
    pending = PendingCommit(
        tree_oid=_write_bootstrap_tree(worktree),
        message=f"bootstrap empty claim state\n\noperation_id: {operation_id}\n",
        operation_id=operation_id,
    )
    result = push_tree(
        worktree=worktree,
        remote=remote,
        observed=observed,
        pending=pending,
        transport=transport or GitPushTransport(),
    )
    return result.tip if isinstance(result, OperationAlreadyApplied) else result
