# Claim record

One live claim as it is stored under `refs/aco/state`: its identity, its file
name and fields, the scope grammar and the width gate, roles, resources,
overlap, and age. `aco claim`, `aco rescope`, `aco release`, `aco status` and
`aco protect` all read and write this record; the commands appear here only as
far as they make it visible, and each command's own spec cites these IDs
instead of restating them.

A refusal reaching the shared collection point prints `ERROR: <sentence>` on
stderr and exits `2`. `<claim-id>`, `<sha>`, `<oid>` and `<tip>` are the
runner's own values; `<agent>` and `<role>` are the claimant's.

## Behavior table

| state \ trigger | `aco claim <n> --scope P` | `aco status` | `aco rescope`/`aco release` |
|---|---|---|---|
| no live claim | CLAIM-01, CLAIM-05, CLAIM-57, CLAIM-58 | CLAIM-36 | CLAIM-16, CLAIM-17 |
| issue already claimed | CLAIM-11 | CLAIM-31 | — |
| lane branch already claimed | CLAIM-12 | — | — |
| same claim id, same fields | CLAIM-13 | — | — |
| claim id active or released | CLAIM-14, CLAIM-15 | — | — |
| lane claim beside an issue claim | CLAIM-10 | — | — |
| scope path outside the repository | CLAIM-19 | — | — |
| scope path not canonical | CLAIM-20, CLAIM-21 | — | — |
| scope empty, or over 256 entries | CLAIM-22, CLAIM-23 | — | — |
| comma-bearing scope value | CLAIM-18, CLAIM-24 | — | — |
| four paths, or a directory | CLAIM-25, CLAIM-26 | — | — |
| exactly three paths, no directory | CLAIM-30 | — | — |
| under twelve versioned files | CLAIM-28 | — | — |
| wide scope with `--whole` | CLAIM-29 | CLAIM-29 | — |
| scope overlapping a live claim | CLAIM-31, CLAIM-33, CLAIM-34 | CLAIM-35 | — |
| disjoint sibling prefixes | CLAIM-32 | CLAIM-35 | — |
| resource requested | CLAIM-41, CLAIM-42, CLAIM-43 | CLAIM-41 | CLAIM-44 |
| claim opened over an hour ago | — | CLAIM-47, CLAIM-48 | CLAIM-49 |
| state ref rewritten | — | CLAIM-50 | — |
| foreign claim, coordinator override | CLAIM-51 | — | CLAIM-38, CLAIM-39, CLAIM-40 |
| hand-corrupted claim file | CLAIM-06..CLAIM-08, CLAIM-59..CLAIM-63 | CLAIM-06..CLAIM-08, CLAIM-59..CLAIM-63 | — |
| hand-corrupted claim key | CLAIM-09, CLAIM-56, CLAIM-64..CLAIM-66 | CLAIM-09, CLAIM-56, CLAIM-64..CLAIM-66 | — |
| item naming its own scope | CLAIM-53, CLAIM-54, CLAIM-55 | — | — |

## The record and its key

- [ ] [CLAIM-01] A first claim writes one record and `aco status` prints `CLAIMED issue #42: <agent> (<role>) base=<sha> branch=<branch> claim=<claim-id>` with one indented line per scope path (see E-CLAIM-01).
- [ ] [CLAIM-02] An issue claim is one tree entry named `claims/issue-42.toml` under the state ref, whatever its branch is called.
- [ ] [CLAIM-03] A lane claim's entry name percent-encodes its branch, so the lane `docs/tidy-readme` is stored as `claims/lane-docs%2Ftidy-readme.toml` (see E-CLAIM-02).
- [ ] [CLAIM-04] The two key prefixes never collide: a lane branch literally named `issue-1` is stored as `claims/lane-issue-1.toml`, never as the entry of issue `#1`.
- [ ] [CLAIM-05] A claim file carries `claim_id`, `agent`, `role`, `base`, `branch`, `scope`, `opened_commit`, and only then the optional `whole_reason`, `resource_name`, `resource_value`.
- [ ] [CLAIM-10] A lane claim and an issue claim never conflict by identity, so a `docs/` lane claims cleanly beside a live issue claim and `aco status` lists both.
- [ ] [CLAIM-57] A branch that is not a safe Git ref — a leading `-`, `..`, `//`, `@{`, a `.lock` suffix, a dot-segment — refuses `claim marker branch is not a safe Git ref: '<branch>'`, exit `2`.
- [ ] [CLAIM-58] An agent or role that is blank, padded, carries a control character or exceeds its bound refuses `agent must be one bounded non-empty line`, exit `2`, before anything is written.

## A hand-corrupted claim file

- [ ] [CLAIM-06] A claim file carrying a key outside those ten makes the next state read refuse `claim file issue-42.toml at <oid> has unknown keys: ['extra']`, exit `2`.
- [ ] [CLAIM-07] A claim file missing a required key refuses `claim file issue-42.toml at <oid> is missing ['branch']`, exit `2`.
- [ ] [CLAIM-08] A claim file carrying `resource_value` without `resource_name` refuses `claim file issue-42.toml at <oid> has resource_value without resource_name`, exit `2`.
- [ ] [CLAIM-59] A claim file whose `agent`, `role`, `base`, `branch` or `opened_commit` is empty or not text refuses `claim file issue-42.toml at <oid> field 'agent' must be non-empty text`, exit `2`.
- [ ] [CLAIM-60] A claim file whose `scope` is empty or holds a non-text entry refuses `claim file issue-42.toml at <oid> field 'scope' must be a non-empty list of text`, exit `2`.
- [ ] [CLAIM-61] A claim file whose `claim_id` is not a claim id refuses `claim file issue-42.toml at <oid> has an invalid claim id`, exit `2`.
- [ ] [CLAIM-62] A claim file whose `base` or `opened_commit` is not a 40-character lowercase commit id refuses `claim file issue-42.toml at <oid> has a malformed commit id`, exit `2`.
- [ ] [CLAIM-63] A claim file that is not valid TOML refuses `malformed claim file issue-42.toml at <oid>: <reason>`, exit `2`; a `whole_reason` that is not text refuses `field 'whole_reason' must be text`.

## A hand-corrupted claim key

- [ ] [CLAIM-09] A claims entry whose name carries neither prefix refuses `claim key has neither the issue nor lane prefix: '<key>'`, exit `2`.
- [ ] [CLAIM-56] A lane entry name whose escaped bytes are not UTF-8 refuses `claim key does not decode as utf-8: '<key>'`, exit `2`.
- [ ] [CLAIM-64] An issue entry name carrying `0`, a leading zero or a non-number refuses `claim key has a malformed issue number: '<key>'`, exit `2`.
- [ ] [CLAIM-65] A flat lane entry with a non-ASCII byte refuses `claim key has an unescaped reserved character: '<key>'`, exit `2`; a literal `/` is a nested path silently dropped instead.
- [ ] [CLAIM-66] A lane entry name carrying an incomplete or non-hexadecimal escape refuses `claim key has a malformed percent-escape: '<key>'`, exit `2`.

## Identity exclusivity

- [ ] [CLAIM-11] A second claim on an already-claimed issue refuses `issue #42 is claimed by Ada (builder) on issue #42 branch ada/issue-42`, exit `2`, before anything is written (see E-CLAIM-03).
- [ ] [CLAIM-12] A second claim on an already-claimed lane branch refuses `lane 'docs/tidy-readme' is claimed by Ada (builder) on lane 'docs/tidy-readme' branch docs/tidy-readme`, exit `2`.
- [ ] [CLAIM-13] Repeating an interrupted claim with the same claim id, agent, role, branch and scope returns that same live claim and writes no second record, exit `0`.
- [ ] [CLAIM-14] A claim id already on the ledger with different fields refuses `claim id '<claim-id>' is already on this ledger, active or released; release it, then claim again with a fresh claim id`.
- [ ] [CLAIM-15] A released claim id stays terminal: claiming with it again refuses with that same `already on this ledger, active or released` sentence, exit `2`.
- [ ] [CLAIM-16] `aco rescope` against a claim id with no live claim refuses `claim id '<claim-id>' has no active claim to rescope`, exit `2`.
- [ ] [CLAIM-17] `aco release` against a claim id with no live claim refuses `claim id '<claim-id>' has no active claim to release`, exit `2`.

## Scope grammar

- [ ] [CLAIM-18] Each `--scope` value is exactly one path, comma and all, and a comma-bearing value matching no versioned file refuses `matches no versioned file; one --scope path per flag`, exit `2`.
- [ ] [CLAIM-19] A scope value that is absolute, starts with `~`, carries `..`, is `.`, or opens with `.git` refuses `claim scope must be repository-relative: '<path>'`, exit `2`.
- [ ] [CLAIM-20] A scope value with surrounding whitespace, a backslash, a control character, or over 512 characters refuses `claim scope entries must be canonical bounded paths`, exit `2`.
- [ ] [CLAIM-21] A scope naming the same path twice refuses `claim scope contains duplicate paths`, exit `2`.
- [ ] [CLAIM-22] An empty scope refuses `claim marker scope must be a non-empty list`, exit `2`.
- [ ] [CLAIM-23] A scope of more than 256 entries refuses `claim marker scope exceeds 256 entries`, exit `2`; 256 entries still claim.
- [ ] [CLAIM-24] A comma-free path that no file carries yet claims cleanly, since a lane routinely claims files it is about to create; `aco status` shows it verbatim.

## Width

- [ ] [CLAIM-25] A scope of four paths refuses `scope is wide: 4 paths exceeds three; pass --whole REASON`, exit `2`.
- [ ] [CLAIM-26] A scope naming a directory refuses `scope is wide: 1 directory in scope (docs); pass --whole REASON`, exit `2`, whatever the path count is.
- [ ] [CLAIM-28] Under twelve versioned files a single named path is never wide on share: `aco claim 42 --scope README.md` prints its `CLAIMED` line, exit `0`.
- [ ] [CLAIM-29] `--whole "<one sentence>"` admits a wide scope, lands in the record, and `aco status` prints it as an indented `whole: <one sentence>` line (see E-CLAIM-04).
- [ ] [CLAIM-30] Exactly three named files with no directory are not wide: `aco claim 42 --scope README.md --scope AGENTS.md --scope CLAUDE.md` claims, exit `0`.

## Overlap, advisory

- [ ] [CLAIM-31] A claim prints its cost and its overlaps as one line, `2 of 40 versioned files (5%); overlaps issue #7 on docs/guide.md`, and claims anyway, exit `0`.
- [ ] [CLAIM-32] A scope meeting no live claim prints `overlaps no other open claims` on that same line, exit `0`.
- [ ] [CLAIM-33] An overlap names the deeper path, so a live claim on `docs` met by a claim on `docs/guide.md` is named `on docs/guide.md`, never `on docs`.
- [ ] [CLAIM-34] An overlap of more than three paths names the first three and counts the rest, `docs/a.md, docs/b.md, docs/c.md, and 2 more`.
- [ ] [CLAIM-35] Sibling prefixes never meet: `docs` and `docs2/guide.md` are disjoint, so `aco status --path docs2/guide.md` prints `UNCLAIMED docs2/guide.md`, exit `0`.

## Roles

- [ ] [CLAIM-36] An omitted `--role` makes the record a `builder`, and `aco status` prints `<agent> (builder)`.
- [ ] [CLAIM-37] A rescope by a different agent refuses `only the original claimant may rescope (holder='Ada (builder)', this session='Bob (builder)')`, exit `2`; `rescope` has no `--role` flag.
- [ ] [CLAIM-38] A release by a different agent or role refuses `only the original claimant may release; use an explicit coordinator override`, exit `2`, naming holder and session.
- [ ] [CLAIM-39] `--coordinator-override` without `--role coordinator` refuses `a coordinator override requires role coordinator`, exit `2`.
- [ ] [CLAIM-40] `--coordinator-override --role coordinator` releases a foreign live claim and removes its record, so `aco status` prints `UNCLAIMED issue #42`, exit `0`.

## Resources

- [ ] [CLAIM-41] `--resource <name>` holds the lowest positive integer that name never gave out, and `aco status` prints an indented `resource port=1` line, exit `0`.
- [ ] [CLAIM-42] An explicit resource value another live claim holds refuses `port 1 is held by Ada (builder) on issue #42`, exit `2`.
- [ ] [CLAIM-43] A resource value a released claim once held refuses `port 1 was already consumed and cannot be reused`, exit `2`.
- [ ] [CLAIM-44] A released resource value is never reassigned: after a release of `port 1`, the next `--resource port` claim succeeds and `aco status` prints its indented `resource port=2` line, exit `0`.
- [ ] [CLAIM-45] A resource value without a resource name refuses `resource value requires a resource name`, exit `2`.
- [ ] [CLAIM-46] A resource value that is zero, negative or not an integer refuses `resource value must be a positive integer`, exit `2`.

## Age and takeover

- [ ] [CLAIM-47] `aco status` prints each live claim's age from the committer date of its `opened_commit`, the state-ref tip that preceded the commit which added it, as `Xh Ym`.
- [ ] [CLAIM-48] A claim older than one hour is marked, so its `aco status` line ends `3h 12m old`; at or under an hour it ends `0h 59m`.
- [ ] [CLAIM-49] `aco rescope` replaces only the scope: `claim=<claim-id>`, `base=<sha>` and the printed age keep counting from the first claim (see E-CLAIM-05).
- [ ] [CLAIM-50] A claim whose `opened_commit` is not an ancestor of the fetched tip refuses `<oid> is not an ancestor of <tip>; the ref may have been rewritten`, exit `2`.
- [ ] [CLAIM-51] A stale takeover is an override release and an ordinary claim: two state-ref commits, a fresh `claim=<claim-id>`, and no reuse of the released claim's resource integer.
- [ ] [CLAIM-52] Closing an item never removes its claim: it stays `CLAIMED` in `aco status`, but drops from `aco board`, which reads open issues only; `RECOVERY (close or re-project)` lists an open landed item.

## The item's own scope

- [ ] [CLAIM-53] Issue-mode `aco claim 42` without `--scope` takes the item's own `scope` into the record and prints the same `CLAIMED issue #42` line as an explicit scope would; the same set given in a different order is accepted too, and a live claim already on #42 takes its own stored scope outright, without reading the body again.
- [ ] [CLAIM-54] A `--scope` set differing from the item's own `scope` refuses `claim scope differs from the item's scope; correct the item first`, exit `2`, so no reader claims a false disjointness.
- [ ] [CLAIM-55] An item naming no `scope` refuses issue-mode `aco claim 42` without `--scope` with `item names no scope; pass --scope`, exit `2`.

## Never

- A claim never refuses on overlapping scope: two lanes may hold the same file, and the overlap is a printed note, never a gate.
- No claim, rescope or release ever writes a file outside the repository's own git directory, and none of them creates the state ref: only `aco bootstrap` does.
- A released or rewritten claim record is never reused: neither its claim id nor a resource integer it held ever returns to a later claim.
- A record never carries the item's title, body, blockers or parent: the board owns those, and the claim owns only who works where.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local bare
repository with `main` at one commit, a git identity, `origin/HEAD`, and
`ACO_AGENT` set to `Ada`; `<remote>`, `<tmp>` and `<home>` are the runner's own
paths, `<sha>`, `<oid>` and `<claim-id>` the values the session itself produced.

### E-CLAIM-01 — the golden claim, seen in the state ref

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`

```console
$ aco claim 42 --scope README.md
CLAIMED issue #42: <claim-id>
1 of 6 versioned files (17%); overlaps no other open claims
exit 0
$ aco status
CLAIMED issue #42: Ada (builder) base=<sha> branch=ada/issue-42 claim=<claim-id> 0h 0m
  README.md
exit 0
$ git fetch --quiet origin refs/aco/state && git ls-tree --name-only FETCH_HEAD claims/
claims/issue-42.toml
exit 0
```

### E-CLAIM-02 — a lane claim's key percent-encodes its branch

Setup: bare-remote, bootstrapped, a linked worktree on `docs/tidy-readme`

```console
$ aco claim --scope README.md
CLAIMED lane docs/tidy-readme: <claim-id>
1 of 6 versioned files (17%); overlaps no other open claims
exit 0
$ git fetch --quiet origin refs/aco/state && git ls-tree --name-only FETCH_HEAD claims/
claims/lane-docs%2Ftidy-readme.toml
exit 0
```

### E-CLAIM-03 — the issue is already claimed

Setup: bare-remote, bootstrapped, issue `#42` held by `Ada (builder)` on `ada/issue-42`

```console
$ aco claim 42 --scope README.md
2> ERROR: issue #42 is claimed by Ada (builder) on issue #42 branch ada/issue-42
exit 2
```

### E-CLAIM-04 — a wide scope, refused and then admitted

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`

```console
$ aco claim 42 --scope docs
2> ERROR: scope is wide: 1 directory in scope (docs); pass --whole REASON
exit 2
$ aco claim 42 --scope docs --whole "the guide and its pages move together"
CLAIMED issue #42: <claim-id>
3 of 6 versioned files (50%); overlaps no other open claims
exit 0
$ aco status
CLAIMED issue #42: Ada (builder) base=<sha> branch=ada/issue-42 claim=<claim-id> 0h 0m
  docs
  whole: the guide and its pages move together
exit 0
```

### E-CLAIM-05 — a rescope keeps the identity and the age

Setup: bare-remote, bootstrapped, a live claim on issue `#42` scoped to `README.md`

```console
$ aco rescope 42 --add AGENTS.md
RESCOPED issue #42: <claim-id>
exit 0
$ aco status
CLAIMED issue #42: Ada (builder) base=<sha> branch=ada/issue-42 claim=<claim-id> 0h 0m
  README.md
  AGENTS.md
exit 0
```
