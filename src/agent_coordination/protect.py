"""Owns `aco protect`'s own judgement (issue #394, caller: architecture-audit
distributor #389 finding 2): `judge` reads one already-parsed hook payload
and returns a typed `Verdict` -- allow or deny, with the deny reason -- from
the payload envelope checks (PROT-03..) through the shared Checkout/
Default-Branch/Claim-Scope/Bash-pattern chain every mutating tool call runs.
`cli` keeps only reading stdin, calling `judge`, and printing the verdict's
own JSON envelope under one `except Exception` frame (PROT-17); this module
never touches stdin or stdout itself. `specs/protect.spec.md` owns every
denial reason, the order they are judged in, and the JSON shape and exit
codes `Verdict.to_json`/`Verdict.exit_code` produce -- this file cites those
IDs rather than restating them.

`judge` takes `canonical_remote_for` as an explicit dependency rather than
resolving it itself: reading `.agent-claim/board.toml`'s own storage pin
(PIN-01) is `cli`'s own board-configuration concern, shared by every store
command, not something this lower layer re-implements or reaches upward
for.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from . import checkout, hook_input, protocol, store


class Decision(StrEnum):
    """The two words `protect`'s own JSON envelope prints (PROT-01/PROT-02)."""

    ALLOW = "allow"
    DENY = "deny"


@dataclass(frozen=True, slots=True)
class Verdict:
    """`judge`'s own typed result: `decision` and `reason` are exactly the
    JSON envelope's own two fields (PROT-01/PROT-02) -- `reason` is always
    `None` for `ALLOW` and always a sentence for `DENY`."""

    decision: Decision
    reason: str | None = None

    @classmethod
    def allow(cls) -> Verdict:
        return cls(Decision.ALLOW)

    @classmethod
    def deny(cls, reason: str) -> Verdict:
        return cls(Decision.DENY, reason=reason)

    @property
    def exit_code(self) -> int:
        return 0 if self.decision is Decision.ALLOW else 2

    def to_json(self) -> dict[str, object]:
        if self.decision is Decision.ALLOW:
            return {"decision": "allow"}
        return {"decision": "deny", "reason": self.reason}


_CanonicalRemoteFor = Callable[[Path], str]


class HookToolEffect(StrEnum):
    """What a `PreToolUse` hook name does to files, for `protect`'s verdict.

    `READ` never touches a file's contents, so it clears without a claim
    check. `COMMAND_TEXT` names no path key at all -- its own `command`
    text is scanned for a recognized write pattern instead (issue #380);
    a command naming none allows exactly like `READ`, since `protect`
    cannot judge what its own pattern grammar does not recognize.
    `MUTATING` can write, so it is gated on a live overlapping claim
    exactly as today. Any name in neither set is unproven -- `protect` must
    fail closed on it rather than default it to either bucket (issue #238).
    """

    READ = "read"
    COMMAND_TEXT = "command_text"
    MUTATING = "mutating"


APPLY_PATCH_TOOL_NAME = "apply_patch"
NOTEBOOK_EDIT_TOOL_NAME = "NotebookEdit"


HOOK_TOOL_EFFECTS: Mapping[str, HookToolEffect] = {
    # Read-only: cannot mutate a file, so no claim check is needed.
    # `shell` and the snake_case terminal names below are here too -- the
    # hook payload carries no file path for those, so `protect` cannot gate
    # what it cannot see; this is a named limit (README, "PreToolUse write
    # gate"), not an oversight. `Bash` left this bucket for its own
    # command-text one below (issue #380).
    "Read": HookToolEffect.READ,
    "Glob": HookToolEffect.READ,
    "Grep": HookToolEffect.READ,
    "LS": HookToolEffect.READ,
    "WebFetch": HookToolEffect.READ,
    "WebSearch": HookToolEffect.READ,
    "TodoWrite": HookToolEffect.READ,
    "Task": HookToolEffect.READ,
    "Agent": HookToolEffect.READ,
    "shell": HookToolEffect.READ,
    # Other providers' names for the same read-only or path-blind operations
    # (Grok, Codex): a snake_case terminal command is the same blind spot as
    # `shell` above, and the rest never write a file.
    "read_file": HookToolEffect.READ,
    "grep": HookToolEffect.READ,
    "list_dir": HookToolEffect.READ,
    "run_terminal_command": HookToolEffect.READ,
    "spawn_subagent": HookToolEffect.READ,
    # Command-text: no path key at all -- `hook_input.hook_command_paths`
    # scans the call's own `command` for a recognized write pattern (issue
    # #380); each recognized path then runs the same judgement chain as a
    # mutating tool's own path.
    "Bash": HookToolEffect.COMMAND_TEXT,
    # Mutating: gated on a live claim whose scope overlaps the written path.
    "Edit": HookToolEffect.MUTATING,
    "MultiEdit": HookToolEffect.MUTATING,
    "Write": HookToolEffect.MUTATING,
    "search_replace": HookToolEffect.MUTATING,
    "write": HookToolEffect.MUTATING,
    NOTEBOOK_EDIT_TOOL_NAME: HookToolEffect.MUTATING,
    APPLY_PATCH_TOOL_NAME: HookToolEffect.MUTATING,
    "create_file": HookToolEffect.MUTATING,
    "str_replace_editor": HookToolEffect.MUTATING,
}


def _unknown_hook_tool_reason(tool_name: str) -> str:
    return (
        f"{tool_name!r} is not in aco's hook tool table (HOOK_TOOL_EFFECTS, "
        "issue #238); add it there as read-only or mutating before use"
    )


def _hook_field(payload: dict[str, object], *keys: str) -> object:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


_GENERIC_PATH_KEYS = ("path", "file_path", "filePath")


def _hook_path(tool_input: dict[str, object], *, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


PATH_REQUIRED = "path required"


class _HookPathSource(StrEnum):
    """Where a mutating tool's path lives in its `tool_input` -- one owner
    per tool name (issue #252) so a tool can only be read from a key it
    actually sends; a decoy value under a key it does not send (an in-scope
    `path` next to `NotebookEdit`'s real, out-of-scope `notebook_path`) is
    never looked at."""

    GENERIC_KEYS = "generic_keys"
    NOTEBOOK_PATH = "notebook_path"
    PATCH_TEXT = "patch_text"


_HOOK_PATH_SOURCES: dict[str, _HookPathSource] = {
    APPLY_PATCH_TOOL_NAME: _HookPathSource.PATCH_TEXT,
    NOTEBOOK_EDIT_TOOL_NAME: _HookPathSource.NOTEBOOK_PATH,
}


def _protect_hook_paths(tool_name: str, payload: dict[str, object]) -> tuple[str, ...]:
    """Every path this hook call's `tool_input` names, read only from the
    key(s) this specific tool sends.

    `apply_patch` (Codex) carries no path key at all -- its patch text sits
    under `command` and can touch several files in one call, so it is parsed
    by the dedicated patch grammar instead. `NotebookEdit` (Claude Code)
    carries only `notebook_path`. Every other tool still yields at most one
    path, from the shared `path`/`file_path`/`filePath` keys."""
    tool_input = _hook_field(payload, "toolInput", "tool_input")
    if not isinstance(tool_input, dict):
        return ()
    source = _HOOK_PATH_SOURCES.get(tool_name, _HookPathSource.GENERIC_KEYS)
    if source is _HookPathSource.PATCH_TEXT:
        command = tool_input.get("command")
        if not isinstance(command, str):
            return ()
        return hook_input.hook_patch_paths(command)
    keys = ("notebook_path",) if source is _HookPathSource.NOTEBOOK_PATH else _GENERIC_PATH_KEYS
    single = _hook_path(tool_input, keys=keys)
    return (single,) if single is not None else ()


_ProtectStateOutcome = tuple[protocol.ClaimState | None, str | None]
_ProtectStateCache = dict[Path, _ProtectStateOutcome]


def _protect_claim_state_or_denial(worktree: Path, canonical_remote: str) -> _ProtectStateOutcome:
    """`protect`'s live snapshot (issue #176, §1): one fetch, no positive
    cache (D2). A non-`None` second element names a denial reason for a
    store the hook cannot trust -- unreachable, auth, malformed tree,
    lineage break -- with the same named text (Erwartung 8) instead of the
    generic 'claim first', which would send the agent toward a command that
    cannot fix a transient fetch failure. `worktree` is the payload path's
    own resolved checkout (issue #314), never the hook process's cwd, so a
    subagent editing a linked worktree is fetched against that worktree's
    own per-worktree fetch/lineage state.
    """
    try:
        state = store.fetch_state(worktree=worktree, remote=canonical_remote)
    except protocol.ClaimError as error:
        return None, f"cannot reach {store.STATE_REF}: {error}"
    if state.tip is None:
        return None, f"cannot reach {store.STATE_REF}: {protocol.MISSING_STATE_REF}"
    return state, None


@dataclass(frozen=True, slots=True)
class _ProtectContext:
    """One hook invocation's own shared dependencies, threaded through the
    whole judgement chain together (issue #394): `state_cache` and
    `canonical_remote_for` are always needed by the same callers, so
    bundling them keeps every chain function's own parameter list short
    instead of two parallel threads of the same two values. `canonical_remote_for`
    is `cli`'s own board-configuration reader: resolving
    `.agent-claim/board.toml`'s storage pin is that layer's own concern,
    handed down here rather than re-read from this lower module."""

    state_cache: _ProtectStateCache
    canonical_remote_for: _CanonicalRemoteFor


def _protect_cached_claim_state_or_denial(
    path_checkout: checkout.PathCheckout, *, context: _ProtectContext
) -> _ProtectStateOutcome:
    """`_protect_claim_state_or_denial`, fetched at most once per repository
    per hook invocation (issue #314 gate G5): several payload paths in one
    `apply_patch` call can name the same repository through different
    worktrees, and re-fetching for each would let each path be judged
    against a different snapshot of a store that can move between them --
    passing a rescope that narrowed coverage between fetches, for instance,
    though no single live claim ever covered the whole patch. Cached by
    `common_directory`, the one fact every worktree of one repository
    shares, not by `toplevel`, which differs per worktree."""
    cached = context.state_cache.get(path_checkout.common_directory)
    if cached is not None:
        return cached
    canonical_remote = context.canonical_remote_for(path_checkout.toplevel)
    outcome = _protect_claim_state_or_denial(path_checkout.toplevel, canonical_remote)
    context.state_cache[path_checkout.common_directory] = outcome
    return outcome


def _protect_overlapping_claim_exists(
    state: protocol.ClaimState, *, agent: str, branch: str, relative: str
) -> bool:
    return any(
        claim.agent == agent
        and claim.branch == branch
        and protocol.scopes_overlap(claim.scope, (relative,))
        for claim in state.claims.values()
    )


def _protect_session_claim_exists(state: protocol.ClaimState, *, agent: str, branch: str) -> bool:
    return any(claim.agent == agent and claim.branch == branch for claim in state.claims.values())


def _protect_scope_denial(
    state: protocol.ClaimState, *, agent: str, branch: str, relative: str, miss_denial: str
) -> str | None:
    """Whether a live claim covers `relative`, or `miss_denial` when not.

    The one overlap check `_protect_path_denial` and `_protect_bash_path_denial`
    both share once they have a trustworthy state and a resolved checkout
    (issue #380 delta, gate finding: Bash used to reimplement this same check
    inline instead of sharing it, risking the two drifting apart) -- each
    caller builds its own `miss_denial` text: `_protect_path_denial`
    distinguishes `claim first` from `{relative} outside claim scope` for
    `apply_patch` (issue #252); `_protect_bash_path_denial` always names both
    the recognized pattern and the path (PROT-33), since a command's own
    several paths need telling apart."""
    if _protect_overlapping_claim_exists(state, agent=agent, branch=branch, relative=relative):
        return None
    return miss_denial


def _protect_single_path_scope_miss_denial(
    state: protocol.ClaimState, *, agent: str, branch: str, relative: str, distinguish_scope: bool
) -> str:
    """`_protect_path_denial`'s own scope-miss text (issue #252): `claim
    first` when this session holds no live claim on the branch at all, or
    when `distinguish_scope` is off (every mutating tool but `apply_patch`,
    which cannot name any other path anyway); `{relative} outside claim
    scope` under `apply_patch` when a live claim exists but simply misses
    this one path."""
    if distinguish_scope and _protect_session_claim_exists(state, agent=agent, branch=branch):
        return f"{relative} outside claim scope"
    return "claim first"


def _protect_not_main_denial(path_checkout: checkout.PathCheckout) -> str | None:
    """`None` when `path_checkout` is a linked worktree off the repository's
    default branch; otherwise the "not main" family of denials gate G4
    names: the shared main checkout, a linked worktree that happens to sit
    on the default branch, or -- never `claim`'s own `{main, master}` guess
    -- a checkout whose default branch cannot even be resolved."""
    if path_checkout.kind is checkout.CheckoutKind.MAIN:
        return checkout.PROTECT_NOT_MAIN_REASON
    default_branch = checkout.default_branch_name(directory=path_checkout.toplevel)
    if default_branch is None:
        return checkout.DEFAULT_BRANCH_UNKNOWN_REASON
    if path_checkout.branch == default_branch:
        return checkout.PROTECT_NOT_MAIN_REASON
    return None


def _protect_checkout_denial(
    absolute_path: str, *, outside_repository_denial: str | None
) -> tuple[checkout.PathCheckout | None, str | None]:
    """`absolute_path`'s own resolved checkout, or an early denial reason
    when the checkout it names cannot even be weighed against a live claim
    -- resolved from the path itself (issue #314), never from the hook
    process's cwd, so the same absolute path yields the same verdict from
    any cwd. `outside_repository_denial` is the one difference between a
    generic mutating tool's own path (PROT-10's "not in a repository" deny)
    and a Bash-recognized one (PROT-32's allow, `None` here) -- issue #380
    delta, gate finding: the two used to run separate copies of this gate,
    with a Bash path's symlinks resolved before checkout discovery and a
    payload path's resolved only after (`checkout.relative_scope_entry`), so
    the same symlink could pick a different checkout depending on which tool
    named it; sharing one function keeps every other gate, and now the
    symlink handling too, identical between them. A checkout with no
    commit yet denies (gate G3 -- its branch name could otherwise
    coincidentally match a still-live claim's); a path in the shared main
    checkout, or in a checkout on the repository's default branch at all
    (gate G4 -- a linked worktree can sit on that branch after the
    repository's default branch changes), denies "not main"; a checkout
    whose default branch cannot even be resolved there denies outright
    (gate G4 -- never `claim`'s own `{main, master}` guess, since a
    repository whose default branch is `trunk` would otherwise slip through
    unnoticed as "not the default branch"). `absolute_path`'s own parent is
    tried first -- it need not exist yet for an Edit's new file, but its
    parent always does inside a real checkout -- and `absolute_path` itself
    only as a fallback: a path that names a checkout root exactly (its
    parent sits outside every repository) is still judged by that checkout,
    never silently treated as outside every repository (issue #380 delta,
    gate finding: `rm -rf ../<repo>-worktrees/issue-1-x` must not bypass
    PROT-32 this way); PROT-14 then denies it as the checkout root itself.
    A directory is resolved as itself first, before its parent, when it is
    itself a checkout root: a nested checkout's own root -- one whose parent
    directory happens to sit inside an outer repository, e.g. a worktrees
    directory the outer checkout tracks -- would otherwise have the outer
    checkout's parent-first lookup answer for it, letting that outer
    checkout's own scope silently stand in for the nested root's own PROT-14
    gate (issue #380 delta, gate finding). A non-directory path (an
    ordinary file, existing or not yet written) never names a checkout root,
    so it keeps the cheaper parent-first order. `absolute_path` is
    normalized lexically first (`os.path.normpath`, no symlink resolution):
    a lexically equivalent payload like `nested/../nested` or `nested/.`
    must reach this comparison the same way `nested` does, or a covering
    outer claim could stand in for the nested checkout root's own PROT-14
    gate (issue #380 delta, gate finding)."""
    path = Path(os.path.normpath(absolute_path))
    if path.is_dir():
        self_checkout = checkout.resolve_path_checkout(path)
        if self_checkout is not None and self_checkout.toplevel == path:
            path_checkout = self_checkout
        else:
            path_checkout = checkout.resolve_path_checkout(path.parent) or self_checkout
    else:
        path_checkout = checkout.resolve_path_checkout(
            path.parent
        ) or checkout.resolve_path_checkout(path)
    if path_checkout is None:
        return None, outside_repository_denial
    if not path_checkout.has_commit:
        return None, checkout.NO_COMMIT_CHECKOUT_REASON
    not_main_denial = _protect_not_main_denial(path_checkout)
    if not_main_denial is not None:
        return None, not_main_denial
    return path_checkout, None


_ProtectMissDenialBuilder = Callable[[protocol.ClaimState, checkout.PathCheckout, str, str], str]


def _protect_checkout_scope_denial(
    raw_path: str,
    *,
    outside_repository_denial: str | None,
    context: _ProtectContext,
    resolve_agent: Callable[[], str],
    miss_denial: _ProtectMissDenialBuilder,
) -> str | None:
    """The Checkout/Default-Branch/Claim-Scope chain a payload path's own
    write (`_protect_path_denial`) and a Bash-recognized path
    (`_protect_bash_path_denial`) both run once `raw_path` is already known
    absolute -- checkout, relative scope entry, live state, agent identity,
    then overlap -- parameterised only by `raw_path` and each caller's own
    denial sentence (issue #380 delta, gate finding: the two used to run two
    separately written copies of this same four-step orchestration instead
    of one shared chain).

    `resolve_agent` is called only once a live state is already in hand:
    `_protect_path_denial`'s own caller already resolved it eagerly and
    hands back that value here for free (PROT-08: an unresolvable identity
    must deny before any per-path checkout gate runs, so it cannot wait this
    long); `_protect_bash_path_denial` resolves it only here instead
    (PROT-31: a pattern that never gets this far never needed an identity at
    all). `miss_denial` builds each caller's own scope-miss sentence from
    the state, checkout, agent, and relative scope entry now in hand."""
    path_checkout, denial = _protect_checkout_denial(
        raw_path, outside_repository_denial=outside_repository_denial
    )
    if path_checkout is None:
        return denial
    relative = checkout.relative_scope_entry(raw_path, toplevel=path_checkout.toplevel)
    if relative is None:
        return PATH_REQUIRED
    state, denial = _protect_cached_claim_state_or_denial(path_checkout, context=context)
    if state is None:
        return denial
    agent = resolve_agent()
    return _protect_scope_denial(
        state,
        agent=agent,
        branch=path_checkout.branch,
        relative=relative,
        miss_denial=miss_denial(state, path_checkout, agent, relative),
    )


def _protect_path_denial(
    agent: str, raw_path: str, *, distinguish_scope: bool, context: _ProtectContext
) -> str | None:
    """The deny reason for one payload path's write, or `None` to allow.

    `apply_patch` sets `distinguish_scope` (issue #252): with several paths
    in one call, the payload never told the agent which one was the problem,
    so the repair sentence must -- `claim first` when this session holds no
    live claim on the path's own checkout at all, `{path} outside claim
    scope` when it does but this path is not in it. A single-path tool call
    keeps the simpler `claim first` either way, matching its own payload's
    inability to name any other path.
    """
    if not Path(raw_path).is_absolute():
        return checkout.RELATIVE_PAYLOAD_PATH_DENIAL

    def miss_denial(
        state: protocol.ClaimState, path_checkout: checkout.PathCheckout, agent: str, relative: str
    ) -> str:
        return _protect_single_path_scope_miss_denial(
            state,
            agent=agent,
            branch=path_checkout.branch,
            relative=relative,
            distinguish_scope=distinguish_scope,
        )

    return _protect_checkout_scope_denial(
        raw_path,
        outside_repository_denial=checkout.NOT_IN_A_REPOSITORY_REASON,
        context=context,
        resolve_agent=lambda: agent,
        miss_denial=miss_denial,
    )


_ProtectItem = TypeVar("_ProtectItem")


def _protect_first_denial(
    items: tuple[_ProtectItem, ...],
    *,
    canonical_remote_for: _CanonicalRemoteFor,
    denial_for: Callable[[_ProtectItem, _ProtectContext], str | None],
) -> Verdict:
    """Judges every one of `items` -- a mutating tool's own payload paths, or
    a Bash command's own recognized `(pattern, path)` pairs -- against
    `denial_for`'s own per-item chain, sharing one `_ProtectContext` (and so
    one state-fetch cache) between all of them (gate G5: several items in
    one call can name the same repository through different worktrees, and
    re-fetching per item would let each be judged against a different
    snapshot of a store that can move between them). The first denial wins;
    naming none at all allows (issue #380 delta, gate finding: `_protect_write`
    and `_protect_bash` used to each build their own cache and run their own
    copy of this same loop instead of sharing it, risking the two drifting
    apart)."""
    context = _ProtectContext(state_cache={}, canonical_remote_for=canonical_remote_for)
    for item in items:
        denial = denial_for(item, context)
        if denial is not None:
            return Verdict.deny(denial)
    return Verdict.allow()


def _protect_write(
    tool_name: str, payload: dict[str, object], *, canonical_remote_for: _CanonicalRemoteFor
) -> Verdict:
    """`protect` is forge-free (issue #245): it authorizes a write from the
    live store state alone, never a forge target, so it never resolves a
    repository or calls `gh` -- `--repo` is meaningless here and simply
    unused. Several paths in one `apply_patch` call may each sit in a
    different checkout (issue #314): each is judged in its own via
    `_protect_first_denial`, and the first denial wins."""
    raw_paths = _protect_hook_paths(tool_name, payload)
    if not raw_paths:
        return Verdict.deny(PATH_REQUIRED)
    agent = checkout.resolved_agent(None)
    distinguish_scope = tool_name == APPLY_PATCH_TOOL_NAME
    return _protect_first_denial(
        raw_paths,
        canonical_remote_for=canonical_remote_for,
        denial_for=lambda raw_path, context: _protect_path_denial(
            agent, raw_path, distinguish_scope=distinguish_scope, context=context
        ),
    )


def _protect_bash_cwd(payload: dict[str, object]) -> str | None:
    cwd = _hook_field(payload, "cwd")
    return cwd if isinstance(cwd, str) and cwd else None


def _protect_bash_path_denial(
    pattern: str, raw_path: str, *, context: _ProtectContext
) -> str | None:
    """The deny reason for one Bash-recognized `(pattern, raw_path)` write,
    or `None` to allow. `raw_path` already carries `hook_input`'s own
    `cwd`/`cd` resolution (issue #380 delta): a path still relative here
    means no absolute base was ever known, so this allows outright before
    ever resolving identity, a checkout, or the store (PROT-31) -- the same
    "no identity, no repository lookup at all" order a command naming no
    recognized pattern gets. Every remaining, absolute path runs
    `_protect_checkout_scope_denial`'s own shared chain, except: a path
    outside every repository allows instead of PROT-10's deny (PROT-32);
    agent identity resolves last, only once a live claim state is actually
    in hand, rather than before any path even runs (review finding:
    resolving it eagerly made an unresolvable identity deny a path PROT-31
    should have allowed); and a scope miss denies naming both the
    recognized pattern and the path (PROT-33) rather than a bare `claim
    first`, since a command's own several paths need telling apart."""
    if not Path(raw_path).is_absolute():
        return None
    return _protect_checkout_scope_denial(
        raw_path,
        outside_repository_denial=None,
        context=context,
        resolve_agent=lambda: checkout.resolved_agent(None),
        miss_denial=lambda _state, _path_checkout, _agent, relative: (
            f"{pattern} {relative} outside claim scope"
        ),
    )


def _protect_bash(
    payload: dict[str, object], *, canonical_remote_for: _CanonicalRemoteFor
) -> Verdict:
    """`Bash`'s own command-text judgment (issue #380): every
    `(pattern, path)` pair `hook_input.hook_command_paths` recognizes in
    the call's own `command` runs `_protect_bash_path_denial`'s chain via
    `_protect_first_denial`, the first denial winning. A missing or
    non-string `command`, or one naming no recognized pattern at all,
    allows outright without ever resolving identity, git, or the store --
    `protect` cannot judge what it cannot see (`specs/protect.spec.md`'s
    own `## Never`), and failing closed here would block the overwhelming
    majority of harmless shell calls."""
    tool_input = _hook_field(payload, "toolInput", "tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else None
    if not isinstance(command, str):
        return Verdict.allow()
    pairs = hook_input.hook_command_paths(command, cwd=_protect_bash_cwd(payload))
    if not pairs:
        return Verdict.allow()
    return _protect_first_denial(
        pairs,
        canonical_remote_for=canonical_remote_for,
        denial_for=lambda pair, context: _protect_bash_path_denial(
            pair[0], pair[1], context=context
        ),
    )


def _protect_dispatch(
    effect: HookToolEffect,
    tool_name: str,
    payload: dict[str, object],
    *,
    canonical_remote_for: _CanonicalRemoteFor,
) -> Verdict:
    if effect is HookToolEffect.READ:
        return Verdict.allow()
    if effect is HookToolEffect.COMMAND_TEXT:
        return _protect_bash(payload, canonical_remote_for=canonical_remote_for)
    return _protect_write(tool_name, payload, canonical_remote_for=canonical_remote_for)


def judge(
    payload: dict[str, object] | None, *, canonical_remote_for: _CanonicalRemoteFor
) -> Verdict:
    """`protect`'s own verdict for one already-parsed hook payload (issue
    #394): `payload` is `None` for unreadable stdin or invalid JSON (PROT-03,
    `cli`'s own concern before this ever runs); everything else -- a
    non-object payload reaching here as a plain `dict` already rules that
    out for its caller -- is judged here through to a `Verdict`. Raises
    exactly what `canonical_remote_for` or the store boundary itself raises
    (a board-configuration precondition failure, PROT-29; a store fetch
    failure, PROT-15/PROT-16); `cli`'s own `except Exception` frame (PROT-17)
    is what turns any of those, or any other uncaught exception, into a deny
    with that exception's own bare text."""
    if payload is None:
        return Verdict.deny("invalid hook payload")
    tool_name = _hook_field(payload, "toolName", "tool_name")
    if not isinstance(tool_name, str):
        return Verdict.deny("invalid hook payload")
    effect = HOOK_TOOL_EFFECTS.get(tool_name)
    if effect is None:
        return Verdict.deny(_unknown_hook_tool_reason(tool_name))
    return _protect_dispatch(effect, tool_name, payload, canonical_remote_for=canonical_remote_for)
