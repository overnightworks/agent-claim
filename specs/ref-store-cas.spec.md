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
- [ ] [CAS-06] Any transition's landed commit carries `operation_id: <uuid>`, `intent: claim|rescope|release|item_write|landing`, and `claim_id`+`item`, `item_id`, or a landing's `item_id`+`claim_id`+`item` (below).

  ```
  operation_id: <uuid>
  claim_id: <id>
  item: <bare identifier>
  intent: claim
  ```
  an item write carries `item_id: <id>` and no `item:` line instead. A
  landing (issue #359) carries both ids and `item:` together:
  ```
  operation_id: <uuid>
  item_id: <id>
  claim_id: <id>
  item: <bare identifier>
  intent: landing
  ```

## Fetch anchor and lineage stamp, one per worktree

- [ ] [CAS-07] A fetch of a present ref never creates the shared local `refs/aco/state`; the tip lands only in `refs/worktree/aco/state`, never read back from `FETCH_HEAD` (see E-CAS-02).
- CAS-08 (retired 20.09.2026, issue #426): the anchor write it named is no longer a step of its own; the fetch that lands the tip in `refs/worktree/aco/state` (CAS-07) fails as one `cannot fetch` refusal.
- [ ] [CAS-50] A fetch that lands the tip but cannot read it back from `refs/worktree/aco/state` refuses `cannot read the fetched tip at refs/worktree/aco/state: <detail>`.
- [ ] [CAS-09] This worktree's own last-observed tip is stamped at `<git-dir>/aco/last-oid` -- private to it, never shared with another linked worktree of the same checkout.
- [ ] [CAS-10] A worktree with no stamp yet at `<git-dir>/aco/last-oid` accepts any tip its first fetch reads, never refusing `... the ref may have been rewritten` (CAS-11).
- [ ] [CAS-11] A fetched tip that is not a descendant of this worktree's own stamp refuses `refs/aco/state moved from <old> to <new> without <old> as an ancestor of the new tip; the ref may have been rewritten`.
- [ ] [CAS-48] A lineage check that cannot run refuses `cannot check whether <old> is an ancestor of <new>: <detail>`, never "the ref may have been rewritten" (see E-CAS-07).
- [ ] [CAS-12] A worktree that observed the ref, then fetches again after it was deleted, refuses `refs/aco/state was previously observed at <old> but is now absent; the ref may have been deleted`.
- [ ] [CAS-49] A `land` claim observation, or `reset`, reads the remote's current tip directly, without touching this worktree's own anchor or lineage stamp; only a fetch that advances local state moves them.

## The compare-and-swap transition and its retries

Every push against `refs/aco/state` -- bootstrap's own first commit, and
every live claim/rescope/release/item-write transition -- retries against a
moving tip through the same three exhaustion sentences below, differing
only in the remote, the attempt count (`8` for bootstrap, `32` for a live
transition), and which of the three causes applies.

- [ ] [CAS-13] A transition whose push is rejected once, but whose commit actually landed (a lost response), is found by its own `operation_id` on retry, never pushed a second time.
- [ ] [CAS-47] A retry's search for a lost response's own `operation_id` that fails to read one candidate commit refuses `cannot read commit <sha> while searching for operation_id <id>: <detail>` (see E-CAS-06).
- [ ] [CAS-14] Two transitions on disjoint identities racing for the same tip both land (except CAS-51): the loser re-fetches, re-applies its own `operation_id`'s intent, and lands -- CLAIM-01 owns the printed line.
- [ ] [CAS-15] 32 stuck pushes refuse `refs/aco/state rejected 32 pushes to <remote> without the ref ever moving: a stale lock or missing push rights`, fix `check <remote>'s refs/aco/state.lock` (see E-CAS-03).
- [ ] [CAS-16] A transition rejected 32 times while the ref keeps moving refuses `refs/aco/state moved 32 times while retrying: another writer on <remote> keeps landing first; retry the command`.
- [ ] [CAS-17] A moved-then-stuck ref refuses `refs/aco/state moved 1 time while retrying, then rejected 31 pushes to <remote> without the ref moving after it last moved`, fix `refs/aco/state.lock` (see E-CAS-04).

### Work budget

- [ ] [CAS-18] `status`'s two store reads (a fetch, then every claim's age) make one `ls-remote`, `fetch`, `ls-tree`, `archive`, `log` call and four `rev-parse` calls, ten live claims or three hundred alike.

## An item write's own compare-and-swap

- [ ] [CAS-19] An item write whose `expected` is `None` (must not exist yet) against an id another writer already created refuses `item '<id>' already exists`.
- [ ] [CAS-20] An item write whose `expected` no longer matches the item's current stored oid refuses `item '<id>' was written since it was read (expected <oid>, found <oid-or-None>); re-read and retry`.
- [ ] [CAS-21] Two item writes on distinct ids racing for the same tip both land (except CAS-51): `items/` is rebuilt from the full id -> oid map on every write, never a copy of the parent tree's own `items/` oid.
- [ ] [CAS-51] An item write holding the whole `items/` it checked (a `board --serve` ruling click) refuses `items/ was written since this write checked it; re-read and retry` once any other item changed.

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
- [ ] [CAS-33] A state-tree content read whose own `git archive` invocation fails refuses `cannot read the state tree at <tip>: <detail>`.

## Resource records

- [ ] [CAS-34] A `resources/<name>.toml` that is not valid TOML refuses `malformed resource file <name>.toml at <tip>: <reason>`.
- [ ] [CAS-35] A `resources/<name>.toml` carrying any key set but exactly `occupied` refuses `resource file <name>.toml at <tip> must contain exactly 'occupied'`.
- [ ] [CAS-36] A `resources/<name>.toml` whose `occupied` is not a list of positive integers refuses `resource file <name>.toml at <tip> field 'occupied' must be positive integers`.

## Claim ages read the ref's own history

- [ ] [CAS-37] A `status` age read whose `git log --first-parent` walk fails refuses `cannot read the commit history of <tip>`.
- [ ] [CAS-38] A commit in that history whose committer date `git` cannot parse refuses `git returned a malformed committer date for <sha>`.

## `aco reset`

`aco reset` (issue #298, operator-ruled 15.09.2026/16.09.2026) is the one
recovery path over a broken or rewritten `refs/aco/state`: a mandatory
export, no silent data loss, and a live claim in a state this aco can read
always refuses it outright, `--confirm` or not; a state whose schema it
cannot read needs `--force-unreadable` besides (`specs/reset.spec.md`).

- [ ] [CAS-39] `aco reset` without `--confirm` prints five `would: ` lines -- export, delete-remote, delete-local, clear-stamps, bootstrap -- exit `0`, and touches nothing.
- [ ] [CAS-40] `aco reset`, confirmed or not, against a readable state with any live claim refuses before anything else runs, printing the same claim lines `status` prints, exit `2`.
- [ ] [CAS-41] `aco reset --confirm`, when the ref exists on the remote, exports its tip to a `git bundle`-verifiable `aco-state-<repo>-<date>-<12-hex>.bundle` under `--export-dir` before any deletion.
- [ ] [CAS-42] `aco reset --confirm` against an export path that already carries that bundle's name refuses `<path> already exists; refusing to overwrite an export`, before anything is deleted.
- [ ] [CAS-43] `aco reset --confirm` deletes `refs/aco/state` on the remote with `--force-with-lease` matched to the tip it read; a rejected or stale-leased push refuses and leaves the local ref untouched.
- [ ] [CAS-44] `aco reset --confirm` deletes this repository's own lineage stamp and `refs/worktree/aco/state` anchor in every reachable worktree, before it bootstraps fresh, so CAS-11 never trips there.
- [ ] [CAS-45] `aco reset --confirm --no-export` skips the bundle -- printing `skipped export (--no-export): refs/aco/state at <tip> not saved` -- and does every other CAS-40..44 step exactly as `--confirm` alone.
- [ ] [CAS-46] A bundle CAS-41 wrote, restored via `git fetch <bundle> refs/worktree/aco/reset-export:refs/aco/state`, reproduces the claim state `aco status` showed before the reset.

## Never

- No command but `aco bootstrap` ever creates `refs/aco/state`; every other write path refuses (CAS-03) instead of creating it as a side effect.
- A push against `refs/aco/state` is never `--force`/`--force-with-lease` outside the documented reset/recovery path (CAS-43): every ordinary transition is a plain fast-forward.
- A worktree's own lineage stamp and fetch anchor are never shared with another linked worktree of the same checkout: each has its own git-dir.
- A malformed fetched tree is never partially trusted: the whole read fails loud (CAS-22..38), never a single quarantined claim or resource; a malformed item file alone is refused only as far as `specs/storage-pin.spec.md` PIN-29 and `specs/item.spec.md` ITEM-37..ITEM-42 allow, and ITEM-42 alone reads its still-valid `record.title`.
- No state-store fetch ever lands a tag or `FETCH_HEAD`: each carries `--no-tags --no-write-fetch-head`, so it writes only objects and the ref its own refspec names (issue #298 finding 2).
- A read that does not advance local state (CAS-49) never writes this worktree's fetch anchor or lineage stamp; only an anchoring fetch (CAS-07) moves `refs/worktree/aco/state` and that stamp.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`, a
tracked `.agent-claim/board.toml` naming no `storage` key (the default
`github` pin, `specs/storage-pin.spec.md` PIN-01/PIN-02), and `ACO_AGENT` set
to `Ada`; `<remote>`, `<tmp>`, and `<home>` are the runner's own paths, and
`<bundle>` is the export path `aco reset` itself prints (its own filename
embeds the repository directory name, the date, and the tip's first 12 hex
characters, CAS-41).

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
$ aco status
UNCLAIMED repository
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

### E-CAS-04 — a race lands once, then the lock sticks

Setup: bare-remote, bootstrapped, a competing writer lands one push to `refs/aco/state`, then `refs/aco/state.lock` stays held on `origin` for the rest of the run

```console
$ aco claim 42 --scope README.md
2> ERROR: refs/aco/state moved 1 time while retrying, then rejected 31 pushes to origin without the ref moving after it last moved: another writer landed first, then a stale lock or missing push rights took over -- check origin's refs/aco/state.lock (delete it if stale) and push permissions; retrying the command only helps once that clears
exit 2
```

### E-CAS-05 — reset: a dry run changes nothing, `--confirm` exports and bootstraps fresh

Setup: bare-remote, bootstrapped, no live claim

```console
$ aco reset --export-dir <tmp>
would: export refs/aco/state at <tip> to <bundle> (restore with: git fetch <bundle> refs/worktree/aco/reset-export:refs/aco/state)
would: delete refs/aco/state on origin (lease <tip>)
would: no local refs/aco/state to delete
would: clear lineage stamps and fetch anchors in 1 worktree
would: bootstrap a fresh empty state
exit 0
$ aco reset --confirm --export-dir <tmp>
exported refs/aco/state at <tip> to <bundle> (restore with: git fetch <bundle> refs/worktree/aco/reset-export:refs/aco/state)
deleted refs/aco/state on origin (lease <tip>)
no local refs/aco/state to delete
cleared lineage stamps and fetch anchors in 1 worktree
bootstrapped a fresh empty state at <sha>
exit 0
```

### E-CAS-06 — a lost-response search that cannot read one of its own candidates

Setup: bare-remote, bootstrapped, a rejected push whose commit actually landed, and one commit in the retry's own search range whose object is missing from the local repository (a corrupted or pruned object store)

```console
$ aco claim 42 --scope README.md
2> ERROR: cannot read commit <sha> while searching for operation_id <id>: <detail>
exit 2
```

### E-CAS-07 — a lineage check that cannot run at all

Setup: bare-remote, bootstrapped, this worktree's own stamped commit no longer resolvable in the local repository (a corrupted or pruned object store)

```console
$ aco status
2> ERROR: cannot check whether <old> is an ancestor of <new>: <detail>
exit 2
```
