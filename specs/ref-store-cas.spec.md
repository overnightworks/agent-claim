# State ref, compare-and-swap

`refs/aco/state`: the one compare-and-swap git ref every claim, rescope,
release, and item write lands on, and every read (`status`, `board`,
`next`, `claim`'s own body check) fetches fresh. This file owns the ref's
own transport contract -- its empty tree, bootstrap's idempotency, one
compare-and-swap transition per write, the per-worktree fetch anchor and
lineage stamp, a rewritten or deleted ref, and the malformed-tree shapes a
fetch can meet. `specs/claim-record.spec.md` owns one claim's own record,
key, and the printed lines `status`/`claim`/`rescope`/`release` build from
what this ref holds; this file cites those IDs rather than restating them.
`<sha>`/`<tip>`/`<oid>` are the runner's own git object ids; a refusal
reaching the shared collection point prints `ERROR: <sentence>` on stderr,
exit `2`, exactly as `specs/claim-record.spec.md` already documents.

## Behavior table

| state \ trigger | `aco bootstrap` | a transition (claim/rescope/release/item write) | a fetch (`status`/`board`/`claim`/...) |
|---|---|---|---|
| ref absent, proven (`ls-remote` exit 2) | CAS-02 | CAS-03 | — |
| ref present, valid schema | CAS-01 | CAS-06 | CAS-18 |
| ls-remote auth/transport failure | CAS-04 | CAS-04 | CAS-04 |
| fetch of a present ref fails | CAS-05 | CAS-05 | CAS-05 |
| a rejected push whose commit landed | — | CAS-13 | — |
| two writers race the same tip | — | CAS-14 | — |
| every push rejected, ref never moves | CAS-15* | CAS-15 | — |
| every push rejected, ref keeps moving | CAS-15* | CAS-16 | — |
| push rejected once then sticks | CAS-15* | CAS-17 | — |
| worktree stamp not an ancestor of the fetched tip | — | — | CAS-11 |
| ref previously observed, now absent | CAS-12 | CAS-12 | CAS-12 |
| `schema.toml` malformed or unsupported | — | — | CAS-22..26 |
| a subtree malformed | — | — | CAS-27..33 |
| a resource file malformed | — | — | CAS-34..36 |
| an item write's `expected` is stale or `None` | — | CAS-19, CAS-20 | — |

\* `aco bootstrap`'s own push shares CAS-15's exact three-shaped sentence at 8 attempts instead of a transition's 32; see the section preamble below.

## `refs/aco/state` and bootstrap's idempotency

- [ ] [CAS-01] `aco bootstrap` against a repository whose `refs/aco/state` already exists is a pure read: it prints that ref's own commit id `<sha>`, exit `0`, and pushes no new commit (see E-CAS-01).
- [ ] [CAS-02] `aco bootstrap` against a repository proven to carry no `refs/aco/state` pushes one fast-forward commit holding `schema.toml` with `version = 2`, prints its commit id `<sha>`, exit `0` (E-CAS-01).
- [ ] [CAS-03] A claim, rescope, release, or item write before `aco bootstrap` has created the ref refuses `the claim state ref does not exist yet; run bootstrap before claim, rescope, release, or item write`.
- [ ] [CAS-04] An `ls-remote` exit code that is neither `0` nor `2` (auth/transport failure) refuses `cannot reach <remote> refs/aco/state: auth or transport failure (ls-remote exited <code>): <detail>`.
- [ ] [CAS-05] A present ref whose own `fetch` fails refuses `cannot fetch <remote> refs/aco/state: <detail>`.
- [ ] [CAS-06] Any transition's landed commit carries `operation_id: <uuid>`, either `claim_id: <id>` or `item_id: <id>`, and `intent: claim|rescope|release|item_write`, readable via `git log --format=%B`.

## Fetch anchor and lineage stamp, one per worktree

- [ ] [CAS-07] A fetch of a present ref never creates the shared local `refs/aco/state`; the tip lands only in `FETCH_HEAD`, then `refs/worktree/aco/state`, git's own per-worktree namespace (see E-CAS-02).
- [ ] [CAS-08] An anchor write that itself fails refuses `cannot anchor fetched tip <sha>: <detail>`.
- [ ] [CAS-09] This worktree's own last-observed tip is stamped at `<git-dir>/aco/last-oid` -- private to it, never shared with another linked worktree of the same checkout.
- [ ] [CAS-10] A worktree with no stamp yet at `<git-dir>/aco/last-oid` accepts any tip its first fetch reads, never refusing `... the ref may have been rewritten` (CAS-11).
- [ ] [CAS-11] A fetched tip that is not a descendant of this worktree's own stamp refuses `refs/aco/state moved from <old> to <new> without <old> as an ancestor of the new tip; the ref may have been rewritten`.
- [ ] [CAS-12] A worktree that observed the ref, then fetches again after it was deleted, refuses `refs/aco/state was previously observed at <old> but is now absent; the ref may have been deleted`.

## The compare-and-swap transition and its retries

Every push against `refs/aco/state` -- bootstrap's own first commit, and
every live claim/rescope/release/item-write transition -- retries against a
moving tip through the same three exhaustion sentences below, differing
only in the remote, the attempt count (`8` for bootstrap, `32` for a live
transition), and which of the three causes applies.

- [ ] [CAS-13] A transition whose push is rejected once, but whose commit actually landed (a lost response), is found by its own `operation_id` on retry, never pushed a second time.
- [ ] [CAS-14] Two transitions on disjoint identities racing for the same tip both land: the loser re-fetches, re-applies its intent onto the moved tip, and lands `CLAIMED issue #<n>: <claim-id>` (CLAIM-01).
- [ ] [CAS-15] 32 rejected pushes with the ref never moving refuse `refs/aco/state rejected 32 pushes to <remote> without the ref ever moving`, naming a stale lock via `git update-ref -d refs/aco/state` (E-CAS-03).
- [ ] [CAS-16] A transition rejected 32 times while the ref keeps moving refuses `refs/aco/state moved 32 times while retrying: another writer on <remote> keeps landing first; retry the command`.
- [ ] [CAS-17] A transition whose ref moves once then sticks refuses `refs/aco/state moved 1 time while retrying, then rejected 31 pushes to <remote> without the ref moving`, naming the same repair as CAS-15.

### Work budget

- [ ] [CAS-18] `status`'s two store reads (a fetch, then every claim's age) make exactly one `ls-remote`, `fetch`, `update-ref`, `ls-tree`, `archive`, and `log` call, ten live claims or three hundred alike.

## An item write's own compare-and-swap

- [ ] [CAS-19] An item write whose `expected` is `None` (must not exist yet) against an id another writer already created refuses `item '<id>' already exists`.
- [ ] [CAS-20] An item write whose `expected` no longer matches the item's current stored oid refuses `item '<id>' was written since it was read (expected <oid>, found <oid-or-None>); re-read and retry`.
- [ ] [CAS-21] Two item writes on distinct ids racing for the same tip both land: `items/` is rebuilt from the full id -> oid map on every write, never a copy of the parent tree's own `items/` oid.

## `schema.toml`

The state tree's own `schema.toml` (`version = 2` today) is a different
version field from the work-item body block's own `version = 1`
(`specs/body-block.spec.md`, BODY-08/BODY-09); the two never share a reader.

- [ ] [CAS-22] A fetched tree with no `schema.toml` refuses `state tree at <tip> is missing schema.toml`.
- [ ] [CAS-23] A `schema.toml` carrying any key set but exactly `version` refuses `schema.toml at <tip> must contain exactly 'version'`.
- [ ] [CAS-24] A `schema.toml` whose `version` is not an integer refuses `schema.toml version must be an integer, got '1'`.
- [ ] [CAS-25] A `schema.toml` whose integer `version` is not `2` refuses `unsupported state schema version <n>`, a distinct error from every other malformed-tree refusal here.
- [ ] [CAS-26] A `schema.toml` that is not valid TOML refuses `malformed schema.toml at <tip>: <reason>`.

## The four subtrees' own shape

`claims/`'s own file content is `specs/claim-record.spec.md`'s territory
(CLAIM-06..CLAIM-09, CLAIM-59..CLAIM-66); this section covers only the
tree's structural shape before that content is ever parsed.

- [ ] [CAS-27] A fetched tree carrying a top-level entry outside `schema.toml`, `claims`, `ids`, `resources`, `items` refuses `state tree at <tip> has unknown entries: ['extra.txt']`.
- [ ] [CAS-28] A `claims`/`ids`/`resources`/`items` top-level entry that is not a directory refuses `<name> at <tip> is not a directory`.
- [ ] [CAS-29] A `claims/` entry that is not a `.toml` blob refuses `claims/<name> at <tip> is not a claim file`.
- [ ] [CAS-30] An `ids/` entry that is not a bare, claim-id-shaped blob refuses `ids/<name> at <tip> is not a claim id`.
- [ ] [CAS-31] A `resources/` entry that is not a `.toml` blob refuses `resources/<name> at <tip> is not a resource file`.
- [ ] [CAS-32] An `items/` entry that is not a blob refuses `items/<name> at <tip> is not a file`.
- [ ] [CAS-33] A blob content read that cannot be decoded as the archive git itself produced refuses `cannot read the state tree at <tip>: <detail>`.

## Resource records

- [ ] [CAS-34] A `resources/<name>.toml` that is not valid TOML refuses `malformed resource file <name>.toml at <tip>: <reason>`.
- [ ] [CAS-35] A `resources/<name>.toml` carrying any key set but exactly `occupied` refuses `resource file <name>.toml at <tip> must contain exactly 'occupied'`.
- [ ] [CAS-36] A `resources/<name>.toml` whose `occupied` is not a list of positive integers refuses `resource file <name>.toml at <tip> field 'occupied' must be positive integers`.

## Claim ages read the ref's own history

- [ ] [CAS-37] A `status` age read whose `git log --first-parent` walk fails refuses `cannot read the commit history of <tip>`.
- [ ] [CAS-38] A commit in that history whose committer date `git` cannot parse refuses `git returned a malformed committer date for <sha>`.

## `aco reset` (ruled, not built -- #298)

Operator-ruled 15.09.2026/16.09.2026: reset is the one recovery path, with
a mandatory export and no silent data loss. Not yet built; every criterion
below is proposed wording from #298's own ruling, ratcheted with owner
`# #298`.

- [ ] [CAS-39] `aco reset` without `--confirm` prints five `would:` lines (export, remote deletion, local deletion, worktree stamps, bootstrap), exit `0`, and touches nothing.
- [ ] [CAS-40] `aco reset --confirm` against a repository with any live claim refuses, naming every live claim, before anything is exported or deleted.
- [ ] [CAS-41] `aco reset --confirm` writes a `git bundle`-verifiable export named `aco-state-<repo>-<date>-<short-sha>.bundle` under `--export-dir` before any deletion.
- [ ] [CAS-42] `aco reset --confirm` against an export path that already carries that bundle's name refuses by name, before anything is exported or deleted.
- [ ] [CAS-43] `aco reset --confirm` deletes `refs/aco/state` on the remote with `--force-with-lease` matched to the tip it read; a ref moved since that read refuses the deletion, nothing local touched.
- [ ] [CAS-44] `aco reset --confirm` deletes this repository's own lineage stamp and `refs/worktree/aco/state` anchor in every reachable worktree before it bootstraps fresh, so CAS-11 never trips there.
- [ ] [CAS-45] `aco reset --confirm --no-export` skips the bundle and performs every other CAS-40..44 step exactly as `--confirm` alone does.
- [ ] [CAS-46] A bundle CAS-41 wrote, restored into a fresh repository (`git bundle unbundle`/`fetch`), reproduces the exact claim state `aco status` showed before the reset.

## Never

- No command but `aco bootstrap` ever creates `refs/aco/state`; every other write path refuses (CAS-03) instead of creating it as a side effect.
- A push against `refs/aco/state` is never `--force`/`--force-with-lease` outside the documented reset/recovery path (CAS-43): every ordinary transition is a plain fast-forward.
- A worktree's own lineage stamp and fetch anchor are never shared with another linked worktree of the same checkout: each has its own git-dir.
- A malformed fetched tree is never partially trusted: the whole read fails loud (CAS-22..38), never a single quarantined claim, resource, or item.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`,
and `ACO_AGENT` set to `Ada`; `<remote>`, `<tmp>`, and `<home>` are the
runner's own paths.

### E-CAS-01 — bootstrap, idempotent, and its own commit trailer

Setup: bare-remote, no `refs/aco/state` yet

```console
$ aco bootstrap
<sha>
exit 0
$ aco bootstrap
<sha>
exit 0
$ git fetch --quiet origin refs/aco/state && git log -1 --format=%B FETCH_HEAD
bootstrap empty claim state

operation_id: <uuid>
exit 0
```

### E-CAS-02 — a fetch anchors the tip, never the shared local ref

Setup: bare-remote, bootstrapped, a second, freshly initialized reader checkout

```console
$ git fetch --quiet origin refs/aco/state
exit 0
$ git for-each-ref refs/aco/state
exit 0
$ git rev-parse refs/worktree/aco/state
<sha>
exit 0
```

### E-CAS-03 — a stuck lock exhausts a live transition's retries

Setup: bare-remote, bootstrapped, `refs/aco/state.lock` held on `origin` for the whole run

```console
$ aco claim 42 --scope README.md
2> ERROR: refs/aco/state rejected 32 pushes to origin without the ref ever moving: a stale lock or missing push rights, not a race -- check origin's refs/aco/state.lock (delete it if stale) and push permissions; if the ref itself is stuck, `git update-ref -d refs/aco/state` on origin clears it
exit 2
```
