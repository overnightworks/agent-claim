"""`refs/aco/state` store: the fast-forward CAS transport for the claim state tree.

Sibling of `checkout`, not below it. `checkout`'s owner is this working tree --
current branch, isolation, cleanliness, agent identity -- and its git calls are
local (`rev-parse`, `ls-files`, `status`, `branch`). This module's owner is
repository-global state: a remote compare-and-swap ref, reached through
`ls-remote`, `FETCH_HEAD`, plumbing, push, and retry. Folding the two would
let a worktree-local module own repository-global state -- exactly the
linked-worktree stamp collision `_lineage_stamp_path` exists to avoid.

`bootstrap` (issue #164, slice C1) is this module's sole production caller.
It never checks the state ref out: every read goes through plumbing
(`ls-remote`, `fetch` to `FETCH_HEAD`, `ls-tree`, `cat-file`), and every write
builds a tree with `hash-object`/`mktree` and a commit with `commit-tree`.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from . import process
from .protocol import (
    EMPTY_STATE,
    ClaimError,
    ClaimState,
    ClaimUnavailableError,
    MalformedStateTreeError,
    ObjectId,
    OperationAlreadyApplied,
    PushRejectedError,
    StateLineageError,
    parse_schema_toml,
    serialize_empty_schema_toml,
)

STATE_REF = "refs/aco/state"
DEFAULT_CANONICAL_REMOTE = "origin"

# `git ls-remote --exit-code` (git(1)): 2 is "no matching refs" -- the only
# outcome this store ever reads as `EMPTY_STATE` (criterion 6). 128 is the
# generic auth/transport failure and must never be read as empty.
_LS_REMOTE_EXIT_NO_MATCH = 2

# Internal bound on the push-retry loop below (criterion 3's seam). Distinct
# from the 32-attempt claim-contention policy that owns `apply`'s retries in
# C2: this loop only ever contends over a bootstrap-empty-tree commit.
_MAX_PUSH_ATTEMPTS = 8

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
        result.stderr.decode().strip() or result.stdout.decode().strip() or "unknown git failure"
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
        detail = result.stderr.decode().strip() or "unknown git failure"
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


def _read_schema_blob(worktree: Path, tree_oid: ObjectId, *, tip: ObjectId) -> str:
    listing = _run_git(worktree, ["ls-tree", str(tree_oid)])
    if listing.exit_status != 0:
        raise MalformedStateTreeError(f"cannot list the state tree {tree_oid} at {tip}")
    entries: dict[str, tuple[str, str]] = {}
    for line in listing.stdout.decode().splitlines():
        mode_type, _, name = line.partition("\t")
        _mode, kind, blob_oid = mode_type.split(" ")
        entries[name] = (kind, blob_oid)
    if set(entries) != {"schema.toml"}:
        raise MalformedStateTreeError(
            f"state tree {tree_oid} at {tip} must contain exactly schema.toml, "
            f"found {sorted(entries)}"
        )
    kind, blob_oid = entries["schema.toml"]
    if kind != "blob":
        raise MalformedStateTreeError(f"schema.toml at {tree_oid} is not a blob")
    content = _run_git(worktree, ["cat-file", "-p", blob_oid])
    if content.exit_status != 0:
        raise MalformedStateTreeError(f"cannot read schema.toml blob {blob_oid} at {tip}")
    return content.stdout.decode()


def _parse_state_tree(worktree: Path, tip: ObjectId) -> ClaimState:
    tree_oid = _tree_oid(worktree, tip)
    raw_schema = _read_schema_blob(worktree, tree_oid, tip=tip)
    return parse_schema_toml(raw_schema, tip=tip)


def fetch_state(*, worktree: Path, remote: str = DEFAULT_CANONICAL_REMOTE) -> ClaimState:
    """Read `refs/aco/state` from `remote` without ever checking it out.

    `EmptyState` only for a proven-absent ref (`ls-remote` exit 2). A present
    ref is fetched to this worktree's own `FETCH_HEAD` (never a local ref),
    parsed via plumbing, lineage-checked against this worktree's own last
    observation, and re-stamped.
    """
    probed = _ls_remote_state(worktree, remote)
    if probed is None:
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
    """
    range_argument = f"{since}..{until}" if since is not None else str(until)
    listing = _run_git(worktree, ["log", "--format=%H", range_argument])
    if listing.exit_status != 0:
        return None
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


def _write_empty_state_tree(worktree: Path) -> ObjectId:
    schema_toml = serialize_empty_schema_toml().encode()
    blob_oid = _run_git_with_input(
        worktree, ["hash-object", "-w", "--stdin"], input_data=schema_toml
    )
    mktree_input = f"100644 blob {blob_oid}\tschema.toml\n".encode()
    return ObjectId(_run_git_with_input(worktree, ["mktree"], input_data=mktree_input))


def bootstrap(
    *,
    worktree: Path,
    remote: str = DEFAULT_CANONICAL_REMOTE,
    transport: PushTransport | None = None,
) -> ObjectId:
    """Create `refs/aco/state` at an empty state tree if it is proven absent;
    otherwise report the existing tip untouched.

    The sole production caller of this module. A present ref is a pure read
    (no write); an absent ref (`ls-remote` exit 2) gets one commit holding
    only `schema.toml`; an unreachable ref (auth/transport, exit 128 and
    friends) fails loud from `fetch_state` before either branch runs.
    """
    observed = fetch_state(worktree=worktree, remote=remote)
    if observed.tip is not None:
        return observed.tip
    operation_id = uuid.uuid4().hex
    pending = PendingCommit(
        tree_oid=_write_empty_state_tree(worktree),
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
