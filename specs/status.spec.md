# `aco status`

`aco status`: the one read of every live claim under `refs/aco/state`, plain
text or `--json`, repository-wide, by issue, or by `--path`. This file owns
the command's own argument shape, its `UNCLAIMED`/`CONFLICT` states, its own
overlap note, its `--json` object shape and `reason` vocabulary, `--path`'s
narrower read, and its forge-freedom. `specs/output.spec.md` owns the
`--json` envelope itself (OUT-nn: key order, `ok`, `message`); this file
names only `status`'s own `reason` values. `specs/claim-record.spec.md`
owns one claim's own identity, resource, and `whole:` line (CLAIM-01,
CLAIM-29, CLAIM-36, CLAIM-41, CLAIM-47..50) and its own `--json` field
order (CLAIM-69); `specs/landing-grammar.spec.md` owns the storage-aware
`<label>` convention every command's narrative output shares; this file
cites those IDs rather than restating them. A refusal reaching this
command's own sink prints `ERROR: <sentence>` on stderr, exit `2`, the
shared sink `specs/ref-store-cas.spec.md`'s own preamble already documents.
`<sha>`, `<tip>`, and `<claim-id>` are the runner's own values.

## Behavior table

| state \ trigger | `aco status` (text) | `aco status --json` | `aco status --path P` (text) | `aco status --path P --json` |
|---|---|---|---|---|
| no matching claim | STAT-01, STAT-02 | STAT-03 | STAT-10 | STAT-13 |
| a matching claim, no conflict | — | STAT-17 | — | STAT-13 |
| two claims share one identity | STAT-04 | STAT-05 | — | — |
| a claim overlapping another's scope | STAT-06 | STAT-08 | — | — |
| one holder, no extra fields | — | STAT-07, STAT-09 | STAT-11 | STAT-13 |
| more than one holder of one path | — | — | STAT-12 | STAT-13 |
| `storage = "state-ref"` | STAT-14 | STAT-15 | — | — |
| `--repo`, or a non-GitHub canonical remote | STAT-16 | STAT-16 | STAT-16 | STAT-16 |
| the fetched state ref itself is rewritten or malformed | STAT-18 | STAT-18 | STAT-18 | STAT-18 |

## The empty repository and an unclaimed issue

- [ ] [STAT-01] `aco status` with no issue argument, against no live claims, prints `UNCLAIMED repository`, exit `0` (see E-STAT-02).
- [ ] [STAT-02] `aco status <n>` against an issue with no live claim prints `UNCLAIMED issue <label>`, `<label>` the storage-aware form STAT-14 owns, exit `0`.
- [ ] [STAT-03] `aco status --json` against no matching claims prints the envelope, `reason: "unclaimed"`, then `"issue": <n-or-null>, "tip": <tip-or-null>, "claims": []`.
- [ ] [STAT-17] `aco status --json` against a matching, non-conflicting claim reports `reason: "claimed"`, `ok: true`, exit `0`.

## `CONFLICT`, status's own read of two claims on one identity

Reached only through a `claims/` tree pairing two entries whose decoded
identity coincides -- no command here writes that state; a live claim
already refuses a second one on the same identity (`specs/claim-record.spec.md`,
CLAIM-11/CLAIM-12).

- [ ] [STAT-04] Two live claims recorded under the same issue or lane identity both print `CONFLICT` in place of `CLAIMED` at the head of their own line, exit `2`.
- [ ] [STAT-05] The same pair under `--json` reports `reason: "conflict"`, `ok: false`, and each claim's own `"state": "CONFLICT"`, exit `2`.

## Status's own overlap note

The cost-and-overlap line `aco claim` prints at claim time is that
command's own fact; `aco status` renders overlap separately as one line per
claim block, positioned after any resource or `whole:` line, naming only
the peer and its claim id, never the meeting paths.

- [ ] [STAT-06] A claim with a live peer ends its own block with `overlaps issue <label> (<claim-id>), issue <label> (<claim-id>)`, comma-joining every peer; a claim with no peer prints no such line (see E-STAT-05).
- [ ] [STAT-08] `aco status --json`'s `claims[]` object carries an `"overlaps"` array of `{"issue", "lane", "claim_id", "agent"}` objects, one per peer STAT-06's own line names, `[]` when none.

## `--json`'s claim object, beside the fields `claim-record.spec.md` owns

- [ ] [STAT-07] `aco status --json`'s `claims[]` object carries `"resource"`/`"resource_value"`, both `null` without a hold, beside CLAIM-41's own printed `resource <name>=<value>` line.
- [ ] [STAT-09] `aco status --json`'s `claims[]` object carries `"age"` (CLAIM-47's own rendered text) and `"old"` (CLAIM-48's own boolean), so a caller never re-derives either from a timestamp.

## `--path`, a narrower read that never touches claim age

- [ ] [STAT-10] `aco status --path P` against no holder prints `UNCLAIMED P`, exit `0` (see E-STAT-03).
- [ ] [STAT-11] `aco status --path P` against one holder prints `CLAIMED P issue <label>: <agent> (<role>) claim=<claim-id>`, with no `base=`, `branch=`, or age field, exit `0` (see E-STAT-03, E-STAT-05).
- [ ] [STAT-12] `aco status --path P` against more than one holder appends one line, `overlap: issue <label> (<claim-id>), issue <label> (<claim-id>)`, after every holder's own line (see E-STAT-05).
- [ ] [STAT-13] `aco status --path P --json` prints the envelope, `reason: "unclaimed"`/`"claimed"`, then `"path": P, "claims": [...]`, none carrying an `"overlaps"` key (see E-STAT-03).

## Storage-aware labels

- [ ] [STAT-14] Under `storage = "state-ref"`, `aco status <item-id>`'s subject reads the `<label>` form `specs/landing-grammar.spec.md` owns: `issue aco-xxxxxx`, never `issue #<n>`.
- [ ] [STAT-15] `aco status --json`'s `"issue"` field is always the bare item number under either storage pin, never `aco-xxxxxx`, the convention `specs/landing-grammar.spec.md` states.

## Forge-free

- [ ] [STAT-16] `aco status` never resolves an item forge or reads a remote's own URL beyond its board-config check; `--repo` and a non-GitHub remote are no error, in text, `--json`, or `--path` (see E-STAT-04).

## Every other refusal

- [ ] [STAT-18] A rewritten `refs/aco/state` (CLAIM-50) or a malformed claim record reports through the shared sink and, under `--json`, `reason: "unavailable"`, exit `2`; `status` names no `invalid_usage` (STAT-16).

## Never

- `aco status --path` never reads a claim's committer-date age: a lineage break in an unrelated claim's `opened_commit` never stops its answer, unlike the plain (non-`--path`) read, which surfaces that break by CLAIM-50's own sentence.
- `aco status` never writes: it is a pure read of the fetched state, never a compare-and-swap transition -- only `aco bootstrap`'s ordinary path creates `refs/aco/state` (`specs/ref-store-cas.spec.md`'s own Never line; `aco reset --confirm` reaches the same creation, CAS-44/CAS-46), and no other command here writes it either.
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
{"ok": true, "reason": "claimed", "issue": null, "tip": "<tip>", "claims": [{"issue": 42, "lane": null, "claim_id": "<claim-id>", "agent": "Ada", "role": "builder", "base": "<sha>", "branch": "ada/issue-42", "scope": ["README.md"], "resource": null, "resource_value": null, "overlaps": [], "state": "CLAIMED", "age": "0h 0m", "old": false}]}
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
{"ok": true, "reason": "unclaimed", "issue": null, "tip": "<tip>", "claims": []}
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
{"ok": true, "reason": "claimed", "path": "docs/PRODUCT.md", "claims": [{"issue": 42, "lane": null, "claim_id": "<claim-id>", "agent": "Ada", "role": "builder", "base": "<sha>", "branch": "ada/issue-42", "scope": ["docs/PRODUCT.md"], "resource": null, "resource_value": null, "state": "CLAIMED"}]}
exit 0
```

### E-STAT-04 -- forge-free against a non-GitHub remote

Setup: bare-remote except `origin` points at `git@gitlab.com:other/repo.git`, bootstrapped, no live claim

```console
$ aco status
UNCLAIMED repository
exit 0
```

### E-STAT-05 -- overlap and `--path`, under `storage = "state-ref"`

Setup: bare-remote, `storage = "state-ref"` tracked, bootstrapped, two items `<item-a>`
and `<item-b>` open (`aco item new`), a linked worktree on `ada/issue-a` already
`aco claim <item-a> --scope README.md --scope docs/PRODUCT.md`, a second linked
worktree on `ada/issue-b` already `aco claim <item-b> --scope docs/PRODUCT.md`

```console
$ aco status
CLAIMED issue <label>: Ada (builder) base=<sha> branch=ada/issue-a claim=<claim-id> 0h 0m
  README.md
  docs/PRODUCT.md
  overlaps issue <label> (<claim-id>)
CLAIMED issue <label>: Ada (builder) base=<sha> branch=ada/issue-b claim=<claim-id> 0h 0m
  docs/PRODUCT.md
  overlaps issue <label> (<claim-id>)
exit 0
$ aco status --path README.md
CLAIMED README.md issue <label>: Ada (builder) claim=<claim-id>
exit 0
$ aco status --path docs/PRODUCT.md
CLAIMED docs/PRODUCT.md issue <label>: Ada (builder) claim=<claim-id>
CLAIMED docs/PRODUCT.md issue <label>: Ada (builder) claim=<claim-id>
overlap: issue <label> (<claim-id>), issue <label> (<claim-id>)
exit 0
```
