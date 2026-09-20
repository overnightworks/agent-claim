# `aco brief`

`aco brief <item>`: one dispatch brief composed from reads a lane step's body
otherwise gets assembled from by hand -- the item's own body, its live issue
claim, that claim's lane tip, and the files the lane touches against its
base. `aco brief <item> --step <step>` adds two more sections, this
repository's own rules and checks for that lane step, read from the tracked
`.agent-claim/brief.toml`. This file owns the command's own argument, its
printed section shape, when each section carries a value versus stays empty,
its own success payload, and its own `reason` vocabulary;
`specs/output.spec.md` owns the `--json` envelope itself (OUT-nn: key
order, `ok`, `message`). `specs/claim-record.spec.md` owns the claim
record's own fields this command reads (CLAIM-01, CLAIM-05, CLAIM-47), and
`specs/storage-pin.spec.md` owns the item-reference grammar `<item>`
accepts (PIN-08) and the state-ref forge gate (PIN-04, PIN-05). `<item>`
is the argument as given; `<n>` its resolved number; `<step>` is one of
`build`, `review`, `fix`, `land`. A refusal reaching the shared collection
point prints `ERROR: <sentence>` on stderr, exit `2`.

## Behavior table

| state \ trigger | `aco brief <item>` (text) | `aco brief <item> --json` |
|---|---|---|
| a live issue claim, lane branch resolves | BRIEF-01, BRIEF-02, BRIEF-11, BRIEF-05 | BRIEF-06, BRIEF-10 |
| a live issue claim, lane branch gone | BRIEF-04 | BRIEF-06, BRIEF-10 |
| a live issue claim, lane branch read fails outright | BRIEF-18 | BRIEF-18 |
| no live issue claim | BRIEF-03 | BRIEF-06 |
| `<item>` names no item at all | BRIEF-08 | BRIEF-08 |
| a non-GitHub canonical remote | BRIEF-07 | BRIEF-07 |
| `storage = "state-ref"` | BRIEF-09 | BRIEF-09 |

## The four sections, always in this order

- [ ] [BRIEF-01] `aco brief <item>` prints the body, a blank line, `CLAIM`, a blank line, `TIP`, a blank line, `TOUCHED` -- always these four headings in order (see E-BRIEF-01).
- [ ] [BRIEF-02] Under a live issue claim, `CLAIM` is followed by one line `<agent> (<role>) branch=<branch> base=<base>[ <age>]`, branch before base (see E-BRIEF-01).
- [ ] [BRIEF-11] That claim line's indented lines are one per scope path, then `  whole: <reason>` only when the claim carries one (see E-BRIEF-01).
- [ ] [BRIEF-03] With no live issue claim, `CLAIM` prints exactly `no active claim`; `TIP` prints no value line at all; `TOUCHED` lists nothing (see E-BRIEF-02).
- [ ] [BRIEF-04] With a live claim whose branch resolves neither locally nor as `origin/<branch>` -- git itself answering "no such ref" -- `TIP` prints `branch not found` and `TOUCHED` lists nothing (see E-BRIEF-03).
- [ ] [BRIEF-05] With a live claim whose branch resolves, `TIP` prints that branch's own commit id, and `TOUCHED` lists one path per line from a `git diff --name-only <base>..<tip>` (see E-BRIEF-01).
- [ ] [BRIEF-18] When a live claim's branch read fails instead of answering not-found, `aco brief` refuses with git's own failure detail, exit `2`, `reason: unavailable` under `--json` (see E-BRIEF-12).
- [ ] [BRIEF-08] `<item>` naming no item at all prints one empty line for the missing body, then every section exactly as BRIEF-01..06 describe with no live claim -- never a refusal (see E-BRIEF-06).

## `--json`

- [ ] [BRIEF-06] `aco brief <item> --json` prints `specs/output.spec.md`'s envelope with `reason: "composed"`, then `"body", "claim", "tip", "touched"`, `"claim"` `null` with no live claim (see E-BRIEF-04).
- [ ] [BRIEF-10] A non-`null` `"claim"` object is `{"agent", "role", "branch", "base", "scope", "whole", "age"}`, `"whole"` `null` without one (see E-BRIEF-04).

## `--step`

| state \ trigger | `aco brief <item> --step <step>` (text) | `aco brief <item> --step <step> --json` |
|---|---|---|
| a tracked `.agent-claim/brief.toml` | BRIEF-12, BRIEF-13 | BRIEF-14 |
| no tracked `.agent-claim/brief.toml` | BRIEF-15 | BRIEF-15 |

- [ ] [BRIEF-12] `--step <step>` prints the four sections, then a blank line, `RULES`, one line per `[<step>].rules` entry, empty when it names none (see E-BRIEF-07).
- [ ] [BRIEF-13] `RULES`' own lines are followed by a blank line, `CHECKS`, one line per `[<step>].checks` entry, empty when it names none (see E-BRIEF-07).
- [ ] [BRIEF-14] `aco brief <item> --step <step> --json` adds `"rules"` and `"checks"` string-list keys to BRIEF-06/BRIEF-10's own object (see E-BRIEF-08).
- [ ] [BRIEF-15] `--step <step>` refuses `no .agent-claim/brief.toml in the repository`, exit `2`, before reading the body or claim, when the repository tracks no such file (see E-BRIEF-09).

## Forge resolution

- [ ] [BRIEF-07] `aco brief <item>` on a canonical remote whose host has no forge adapter refuses `no forge adapter for host <host>`, exit `2`, before any forge resolution (see E-BRIEF-05).
- [ ] [BRIEF-09] Under `storage = "state-ref"`, `aco brief <item>` resolves the state-ref forge like `item show`/`edit`/`close`; `--repo` there refuses the same as those (PIN-04, PIN-05).
- [ ] [BRIEF-17] `--json` on a dispatched refusal (BRIEF-07, BRIEF-09, BRIEF-15) prints `specs/output.spec.md`'s envelope with the sentence as `message` and `reason` from the table below, exit `2` (see E-BRIEF-11).

`reason`, by which refusal fired:

| refusal | `reason` |
|---|---|
| PIN-04 (`--repo` under `storage = state-ref`) | `invalid_usage` |
| BRIEF-07 (no forge adapter for host), PIN-05 (no resolvable default branch), BRIEF-15 (no tracked `.agent-claim/brief.toml`) | `unavailable` |

## Never

- `aco brief` never refuses for an `<item>` naming no item at all: the forge's own `MISSING` reference carries no body, so `aco brief` prints one empty first line and proceeds through every other section exactly as BRIEF-01..06 describe (see E-BRIEF-06).
- `aco brief` never matches a lane claim, only a live issue claim on the same number -- an unrelated lane branch claimed by someone else never appears in its `CLAIM` section.
- `aco brief` never writes: it is a pure composition of the item's body, the store's live claims, and one local `git diff` -- plus, only with `--step`, the tracked brief configuration -- never a new data source and never a transition against the state ref.
- `aco brief`'s claim line is never `aco status`'s own `CLAIMED`/`CONFLICT` line (CLAIM-01): it carries no verb, no identity prefix, no `claim=` field, and orders `branch=` before `base=`, the reverse of `status`'s own order.
- `aco brief --json`'s claim object is never `aco status --json`'s own claim object (STAT-07, STAT-09): no `claim_id`, `resource`, `resource_value`, `overlaps`, or `old` key.
- [ ] [BRIEF-16] Without `--step`, `.agent-claim/brief.toml`'s presence or content changes nothing: `brief` prints exactly BRIEF-01..06's sections either way (see E-BRIEF-10).
- `aco brief --step` never writes: `.agent-claim/brief.toml` is one more existing read, never a write, and never a new claim or state-ref transition.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local bare
repository with `main` at one commit, a git identity, `origin/HEAD`, a
tracked `.agent-claim/board.toml` naming no `storage` key, and `ACO_AGENT` set
to `Ada`; `<base>` and `<tip>` are the runner's own commit ids. A session
reading an item's body also names a fixed, deterministic fake `gh` as a
setup precondition.

### E-BRIEF-01 -- a live claim, its lane tip, and the files it touches

Setup: bare-remote, fake `gh`, issue `#42` body `The item's own body.`, a
linked worktree on `ada/issue-42` already `aco claim 42 --scope README.md
--whole "lane touches too much to split"`, one commit on `ada/issue-42`
past `<base>` touching `README.md`, pushed to `origin`

```console
$ aco brief 42
The item's own body.

CLAIM
Ada (builder) branch=ada/issue-42 base=<base> 0h 0m
  README.md
  whole: lane touches too much to split

TIP
<tip>

TOUCHED
README.md
exit 0
```

### E-BRIEF-02 -- no live claim

Setup: bare-remote, fake `gh`, issue `#42` body `No claim yet.`, no live claim

```console
$ aco brief 42
No claim yet.

CLAIM
no active claim

TIP

TOUCHED
exit 0
```

### E-BRIEF-03 -- a live claim whose branch is gone

Setup: bare-remote, fake `gh`, issue `#42` body `Gone lane.`, a live claim on
`#42` scoped to `README.md` whose branch `ada/issue-42-gone` was deleted
after the claim opened

```console
$ aco brief 42
Gone lane.

CLAIM
Ada (builder) branch=ada/issue-42-gone base=<base> 0h 0m
  README.md

TIP
branch not found

TOUCHED
exit 0
```

### E-BRIEF-04 -- `--json`

Setup: bare-remote, fake `gh`, issue `#42` body `The item's own body.`, a
linked worktree on `ada/issue-42` already `aco claim 42 --scope README.md`,
one commit on `ada/issue-42` past `<base>` touching `README.md`, pushed to
`origin`

```console
$ aco brief 42 --json
{"ok": true, "reason": "composed", "body": "The item's own body.", "claim": {"agent": "Ada", "role": "builder", "branch": "ada/issue-42", "base": "<base>", "scope": ["README.md"], "whole": null, "age": "0h 0m"}, "tip": "<tip>", "touched": ["README.md"]}
exit 0
```

### E-BRIEF-05 -- forge-free refusal by host

Setup: `origin` points at a non-GitHub remote, no other precondition

```console
$ aco brief 42
2> ERROR: no forge adapter for host <host>
exit 2
```

### E-BRIEF-06 -- an item number nothing carries

Setup: bare-remote, fake `gh`, no issue or pull request `#81` exists, no live claim on `#81`

```console
$ aco brief 81


CLAIM
no active claim

TIP

TOUCHED
exit 0
```

### E-BRIEF-07 -- `--step` prints this repository's own rules and checks

Setup: bare-remote, fake `gh`, issue `#42` body `The item's own body.`, a
linked worktree on `ada/issue-42` already `aco claim 42 --scope README.md`,
one commit on `ada/issue-42` past `<base>` touching `README.md`, pushed to
`origin`, `.agent-claim/brief.toml` tracked with:

```toml
[build]
rules = ["Stay in scope."]
checks = ["ruff check ."]
```

```console
$ aco brief 42 --step build
The item's own body.

CLAIM
Ada (builder) branch=ada/issue-42 base=<base> 0h 0m
  README.md

TIP
<tip>

TOUCHED
README.md

RULES
Stay in scope.

CHECKS
ruff check .
exit 0
```

### E-BRIEF-08 -- `--step --json`

Setup: as E-BRIEF-07

```console
$ aco brief 42 --step build --json
{"ok": true, "reason": "composed", "body": "The item's own body.", "claim": {"agent": "Ada", "role": "builder", "branch": "ada/issue-42", "base": "<base>", "scope": ["README.md"], "whole": null, "age": "0h 0m"}, "tip": "<tip>", "touched": ["README.md"], "rules": ["Stay in scope."], "checks": ["ruff check ."]}
exit 0
```

### E-BRIEF-09 -- `--step` with no tracked `.agent-claim/brief.toml`

Setup: bare-remote, fake `gh`, no `.agent-claim/brief.toml` in the repository
at all

```console
$ aco brief 42 --step build
2> ERROR: no .agent-claim/brief.toml in the repository
exit 2
```

### E-BRIEF-10 -- a tracked `.agent-claim/brief.toml` changes nothing without `--step`

Setup: as E-BRIEF-07

```console
$ aco brief 42
The item's own body.

CLAIM
Ada (builder) branch=ada/issue-42 base=<base> 0h 0m
  README.md

TIP
<tip>

TOUCHED
README.md
exit 0
```

### E-BRIEF-11 -- a refusal's own `--json` envelope

Setup: `origin` points at a non-GitHub remote, no other precondition

```console
$ aco brief 42 --json
2> ERROR: no forge adapter for host <host>
{"ok": false, "reason": "unavailable", "message": "no forge adapter for host <host>"}
exit 2
```

### E-BRIEF-12 -- the claim branch's own git read fails outright

Setup: bare-remote, fake `gh`, issue `#42` body `Broken git.`, a live claim on
`#42` scoped to `README.md` whose branch read fails with a git error before
git ever answers whether `ada/issue-42` resolves

```console
$ aco brief 42
2> ERROR: <detail>
exit 2
$ aco brief 42 --json
2> ERROR: <detail>
{"ok": false, "reason": "unavailable", "message": "<detail>"}
exit 2
```
