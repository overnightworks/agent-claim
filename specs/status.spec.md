# `aco status`

`aco status`: the one read of every live claim under `refs/aco/state`, plain
text or `--json`, repository-wide, by issue, or by `--path`. This file owns
the command's own argument shape, its `UNCLAIMED`/`CONFLICT` states, its own
overlap note, its `--json` object shape, `--path`'s narrower read, and its
forge-freedom. `specs/claim-record.spec.md` owns one claim's own identity,
resource, and `whole:` line (CLAIM-01, CLAIM-29, CLAIM-36, CLAIM-41,
CLAIM-47..50), and `specs/landing-grammar.spec.md` owns the storage-aware
`<label>` convention every command's narrative output shares; this file
cites those IDs rather than restating them. A refusal reaching this
command's own sink prints `ERROR: <sentence>` on stderr, exit `2`, the
shared sink `specs/ref-store-cas.spec.md`'s own preamble already documents.
`<sha>`, `<tip>`, and `<claim-id>` are the runner's own values.

## Behavior table

| state \ trigger | `aco status` (text) | `aco status --json` | `aco status --path P` (text) | `aco status --path P --json` |
|---|---|---|---|---|
| no matching claim | STAT-01, STAT-02 | STAT-03 | STAT-10 | STAT-13 |
| two claims share one identity | STAT-04 | STAT-05 | — | — |
| a claim overlapping another's scope | STAT-06 | STAT-08 | — | — |
| one holder, no extra fields | — | STAT-07, STAT-09 | STAT-11 | STAT-13 |
| more than one holder of one path | — | — | STAT-12 | STAT-13 |
| `storage = "state-ref"` | STAT-14 | STAT-15 | — | — |
| `--repo`, or a non-GitHub canonical remote | STAT-16 | STAT-16 | STAT-16 | STAT-16 |

## The empty repository and an unclaimed issue

- [ ] [STAT-01] `aco status` with no issue argument, against no live claims, prints `UNCLAIMED repository`, exit `0` (see E-STAT-02).
- [ ] [STAT-02] `aco status <n>` against an issue with no live claim prints `UNCLAIMED issue <label>`, `<label>` the storage-aware form STAT-14 owns, exit `0`.
- [ ] [STAT-03] `aco status --json`, with or without an issue argument, against no matching claims prints `{"issue": <n-or-null>, "state": "UNCLAIMED", "tip": <tip-or-null>, "claims": []}`.

## `CONFLICT`, status's own read of two claims on one identity

Reached only through a `claims/` tree pairing two entries whose decoded
identity coincides -- no command here writes that state; a live claim
already refuses a second one on the same identity (`specs/claim-record.spec.md`,
CLAIM-11/CLAIM-12).

- [ ] [STAT-04] Two live claims recorded under the same issue or lane identity both print `CONFLICT` in place of `CLAIMED` at the head of their own line, exit `2`.
- [ ] [STAT-05] The same pair under `--json` reports the top-level `"state": "CONFLICT"` and each claim's own `"state": "CONFLICT"`, exit `2`.

## Status's own overlap note

The cost-and-overlap line `aco claim` prints at claim time is that
command's own fact; `aco status` renders overlap separately, one line per
claim, naming only the peer and its claim id, never the meeting paths.

- [ ] [STAT-06] A claim whose scope meets a live peer's ends its own block with `overlaps issue #73 (claim-b)`, one per peer, after any resource or `whole:` line; a claim with no peer prints no such line.
- [ ] [STAT-08] `aco status --json`'s `claims[]` object carries an `"overlaps"` array of `{"issue", "lane", "claim_id", "agent"}` objects, one per peer STAT-06's own line names, `[]` when none.

## `--json`'s claim object, beside the fields `claim-record.spec.md` owns

- [ ] [STAT-07] `aco status --json`'s `claims[]` object carries `"resource"`/`"resource_value"`, both `null` without a hold, beside CLAIM-41's own printed `resource <name>=<value>` line.
- [ ] [STAT-09] `aco status --json`'s `claims[]` object carries `"age"` (CLAIM-47's own rendered text) and `"old"` (CLAIM-48's own boolean), so a caller never re-derives either from a timestamp.

## `--path`, a narrower read that never touches claim age

- [ ] [STAT-10] `aco status --path P` against no holder prints `UNCLAIMED P`, exit `0` (see E-STAT-03).
- [ ] [STAT-11] `aco status --path P` against one holder prints `CLAIMED P issue #n: <agent> (<role>) claim=<claim-id>`, with no `base=`, `branch=`, or age field, exit `0` (see E-STAT-03).
- [ ] [STAT-12] `aco status --path P` against more than one holder appends one line, `overlap: issue #a (id-a), issue #b (id-b)`, after every holder's own line.
- [ ] [STAT-13] `aco status --path P --json` prints `{"path": P, "state": ..., "claims": [...]}`, one object per holder, none carrying an `"overlaps"` key (see E-STAT-03).

## Storage-aware labels

- [ ] [STAT-14] Under `storage = "state-ref"`, `aco status <item-id>`'s subject reads the `<label>` form `specs/landing-grammar.spec.md` owns: `issue aco-xxxxxx`, never `issue #<n>`.
- [ ] [STAT-15] `aco status --json`'s `"issue"` field is always the bare item number under either storage pin, never `aco-xxxxxx`, the convention `specs/landing-grammar.spec.md` states.

## Forge-free

- [ ] [STAT-16] `aco status` never resolves an item forge or reads a remote's own URL beyond its board-config precondition: `--repo` and a non-GitHub remote are no error, `--json`/`--path` alike (see E-STAT-04).

## Never

- `aco status --path` never reads a claim's committer-date age: a lineage break in an unrelated claim's `opened_commit` never stops its answer, unlike the plain (non-`--path`) read, which surfaces that break by CLAIM-50's own sentence.
- `aco status` never writes: it is a pure read of the fetched state, never a compare-and-swap transition -- only `aco bootstrap` ever creates `refs/aco/state` (`specs/ref-store-cas.spec.md`'s own Never line), and no other command here writes it either.
- `aco status --json`'s `"issue"` field is never the state-ref item id, even under that pin (STAT-15).
- `aco status --path`'s per-holder object never carries an `"overlaps"` key: the caller reads every holder from the one `"claims"` list instead (STAT-13).
- `aco status`'s own overlap note never names the meeting paths: that detail stays `aco claim`'s own cost line, never duplicated here (STAT-06).

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`, a
tracked `.agent-claim/board.toml` naming no `storage` key, and `ACO_AGENT`
set to `Ada`; `<sha>`, `<tip>`, and `<claim-id>` are the runner's own values.

### E-STAT-01 -- a live claim, text and `--json`

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, already `aco claim 42 --scope README.md`

```console
$ aco status
CLAIMED issue #42: Ada (builder) base=<sha> branch=ada/issue-42 claim=<claim-id> 0h 0m
  README.md
exit 0
$ aco status --json
{"issue": null, "state": "CLAIMED", "tip": "<tip>", "claims": [{"issue": 42, "lane": null, "agent": "Ada", "role": "builder", "base": "<sha>", "branch": "ada/issue-42", "claim_id": "<claim-id>", "scope": ["README.md"], "resource": null, "resource_value": null, "overlaps": [], "state": "CLAIMED", "age": "0h 0m", "old": false}]}
exit 0
```

### E-STAT-02 -- an unclaimed repository and issue

Setup: bare-remote, bootstrapped, no live claim

```console
$ aco status
UNCLAIMED repository
exit 0
$ aco status 42
UNCLAIMED issue #42
exit 0
$ aco status --json
{"issue": null, "state": "UNCLAIMED", "tip": "<tip>", "claims": []}
exit 0
```

### E-STAT-03 -- `--path`, claimed and unclaimed

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, already `aco claim 42 --scope docs/PRODUCT.md`

```console
$ aco status --path docs/PRODUCT.md
CLAIMED docs/PRODUCT.md issue #42: Ada (builder) claim=<claim-id>
exit 0
$ aco status --path README.md
UNCLAIMED README.md
exit 0
$ aco status --path docs/PRODUCT.md --json
{"path": "docs/PRODUCT.md", "state": "CLAIMED", "claims": [{"issue": 42, "lane": null, "agent": "Ada", "role": "builder", "base": "<sha>", "branch": "ada/issue-42", "claim_id": "<claim-id>", "scope": ["docs/PRODUCT.md"], "resource": null, "resource_value": null, "state": "CLAIMED"}]}
exit 0
```

### E-STAT-04 -- forge-free against a non-GitHub remote

Setup: bare-remote except `origin` points at `git@gitlab.com:other/repo.git`, bootstrapped, no live claim

```console
$ aco status
UNCLAIMED repository
exit 0
```
