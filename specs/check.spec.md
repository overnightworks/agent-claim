# `aco check`

`aco check <n>`: one read that dispatches a bare number to one of three
answers, since GitHub gives issues and pull requests the same number space.
A pull request reads `specs/landing-grammar.spec.md`'s own classification,
closing, and parent grammar (LAND-04..LAND-28); this file cites those IDs
rather than restating them and owns only the command's own three-way
dispatch, its issue-mode read of an item's own body contract, its `--json`
envelope, and the checkout it needs to run at all. `specs/output.spec.md`
owns the `--json` envelope itself (OUT-nn: key order, `ok`, `message`);
this file names only `check`'s own `reason` values. `specs/body-block.spec.md`
owns the exact `body malformed:`/`body incomplete:` sentences an issue's own
shape can carry (BODY-01..BODY-52); `specs/storage-pin.spec.md` owns the
`'<value>' is not an item reference` refusal for `<n>` itself (PIN-08) and
gates this command like any other resolving the item forge (PIN-04, PIN-05)
under `storage = "state-ref"`. `<n>` is the argument as given, `<repository>`
the checked repository's own `owner/repo` path. A refusal that never even
reaches this command's own dispatch (no checkout, an untracked storage pin,
an unreachable forge) prints `ERROR: <sentence>` on stderr and, with
`--json`, the envelope with `reason: "unavailable"`; exit `2` either way.
`aco check <sha>` (a trunk commit, not a bare number) is a distinct mode
this file does not own: its own exit and `--json` shape are
`specs/landing-grammar.spec.md`'s own contract (LAND-48, LAND-60).

## Behavior table

| state \ trigger | `aco check <n>` (text) | `aco check <n> --json` |
|---|---|---|
| `<n>` is a pull request | LAND-04..LAND-28 (cited) | CHECK-01 |
| `<n>` exists in neither number space | CHECK-02 | CHECK-03 |
| `<n>` is an issue, sound and unblocked | CHECK-04 | CHECK-05 |
| `<n>` is an issue, malformed body | CHECK-06 | CHECK-05 |
| `<n>` is an issue, incomplete body | CHECK-07 | CHECK-05 |
| `<n>` is an issue, blocked | CHECK-08, CHECK-09 | CHECK-05 |
| no checkout, or another pre-dispatch failure | CHECK-10 | CHECK-10 |
| `--repo` given, `storage = "state-ref"` | CHECK-11 | CHECK-11 |

## `--json`'s own envelope

- [ ] [CHECK-01] `aco check <n> --json` against a pull request prints the envelope, `"kind": "pull_request", "number": <n>`, `reason`/`message` from the table below (see E-CHECK-01).
- [ ] [CHECK-05] `aco check <n> --json` against an issue prints the envelope, `"kind": "issue", "number": <n>`, `reason`/`message` below, `"blocked_by"` only with `reason: "blocked"` (see E-CHECK-05).

`reason`, by which outcome fired:

| outcome | `reason` | exit |
|---|---|---|
| pull request classified (LAND-04..LAND-28's own accepted case) | `valid` | `0` |
| pull request refused (LAND-04..LAND-28's own defect) | `invalid_classification` | `2` |
| `<n>` in neither number space (CHECK-02/03) | `missing` | `2` |
| issue valid, complete, unblocked (CHECK-04) | `valid` | `0` |
| issue malformed (CHECK-06) | `malformed` | `2` |
| issue structurally valid but unfilled (CHECK-07) | `incomplete` | `2` |
| issue with open `blocked_by` dependencies (CHECK-08) | `blocked` | `3` |
| no checkout, or another pre-dispatch failure (CHECK-10) | `unavailable` | `2` |
| `--repo` given, `storage = "state-ref"` (CHECK-11) | `invalid_usage` | `2` |

## A number in neither space

- [ ] [CHECK-02] `<n>` matching no issue and no pull request prints `REFUSED: #<n> does not exist in <repository>` on stderr, exit `2`, naming no kind word (see E-CHECK-02).
- [ ] [CHECK-03] The same read with `--json` prints the envelope, `"reason": "missing", "kind": "missing", "number": <n>, "message": "does not exist in <repository>"`, exit `2`.

## Issue mode: a body's own contract

`<body defect sentence>` and `<sections>` are `specs/body-block.spec.md`'s
own text (BODY-01..BODY-50, BODY-12).

- [ ] [CHECK-04] `<n>` naming a valid, complete, unblocked issue prints `ISSUE #<n> body ok` on stdout, exit `0` (see E-CHECK-03).
- [ ] [CHECK-06] A malformed body prints `ISSUE #<n> <body defect sentence>` on stderr, exit `2` (see E-CHECK-04).
- [ ] [CHECK-07] A structurally valid but unfilled body prints `ISSUE #<n> body incomplete: <sections>` on stderr, exit `2`.
- [ ] [CHECK-08] An issue with open `blocked_by` dependencies prints `ISSUE #<n> blocked by <label>, <label>` on stderr, exit `3`, local blockers first (see E-CHECK-05).
- [ ] [CHECK-09] Under `storage = "state-ref"`, a local blocker's own label is `specs/landing-grammar.spec.md`'s `<label>` (`aco-xxxxxx`); a foreign one stays `owner/repo#n` regardless of either pin.

## No checkout, no read

`<no-checkout sentence>` is `this command reads the repository's body
contract from .agent-claim/board.toml and needs a checkout (a shallow one
is enough): <git detail>`.

- [ ] [CHECK-10] `aco check <n>` outside a git checkout, or on any other pre-dispatch forge failure, refuses `ERROR: <sentence>`, `--json` `reason: "unavailable"`, exit `2` (see E-CHECK-06).
- [ ] [CHECK-11] Under `storage = "state-ref"`, `--repo` here refuses the same sentence as `specs/storage-pin.spec.md`'s PIN-04, `--json` `reason: "invalid_usage"`, exit `2` (see E-CHECK-07).

## Never

- `aco check` never writes: neither mode ever changes an item's body, and the pull-request mode's own claims read is a pure fetch of the already-observed state ref.
- The issue mode of `check` never fetches the state ref: only the pull-request mode reads the live claims LAND-15/LAND-16 need.
- Under `storage = "state-ref"`, `check <n>` never reaches the pull-request path: no number under that storage is ever reported as a landing, so every `<n>` resolves to ISSUE or MISSING.
- `check`'s own subject line is always the bare `#<n>`, in every mode, never the storage-aware `<label>` form `specs/landing-grammar.spec.md` defines for `aco next`/`release`'s own narrative lines.
- `blocked` (CHECK-08) is the only outcome that exits `3`; every other refusal exits `2` (see the `reason`/exit table above), and no outcome exits `1` any more.
- `check <sha>`'s own exit and `--json` shape are never this file's: `specs/landing-grammar.spec.md` owns LAND-48/LAND-60 unchanged.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`,
and `ACO_AGENT` set to `Ada`; `<owner>/<repo>` is the runner's own
repository path. Every session that reaches this command's own dispatch
also needs `.agent-claim/board.toml` tracked (PIN-01), since that read
comes before pull-request or issue mode ever run; the checkout-less
session (E-CHECK-06) is the one exception, since it never reaches the
dispatch at all. A session reading a pull request or an issue also names a
fixed, deterministic fake `gh` as a setup precondition (the shape
`specs/landing-grammar.spec.md` already uses); the checkout-less session
needs neither a fake `gh` nor a checkout at all.

### E-CHECK-01 — `--json` on an accepted and a refused pull request

The text form's own literal is LAND-04's own fact, already driven by
`specs/landing-grammar.spec.md`'s own E-LAND-02; this session drives only
CHECK-01, this file's own `--json` envelope.

Setup: bare-remote, `.agent-claim/board.toml` tracked, fake `gh`, pull request `#57` by `Ada`, body `Work-Item: #42\n\nCloses #42`, an active claim on issue `#42` matching its head branch

```console
$ aco check 57 --json
{"ok": true, "reason": "valid", "kind": "pull_request", "number": 57}
exit 0
```

Setup: bare-remote, `.agent-claim/board.toml` tracked, fake `gh`, pull request `#57` by `Ada`, body `Tidy the README.` (no classification line)

```console
$ aco check 57 --json
{"ok": false, "reason": "invalid_classification", "kind": "pull_request", "number": 57, "message": "carries no `Work-Item:` or `No-Item:` line"}
exit 2
```

### E-CHECK-02 — a number in neither space

Setup: bare-remote, `.agent-claim/board.toml` tracked, fake `gh`, no issue or pull request `#81` exists in this repository

```console
$ aco check 81
2> REFUSED: #81 does not exist in <owner>/<repo>
exit 2
$ aco check 81 --json
{"ok": false, "reason": "missing", "kind": "missing", "number": 81, "message": "does not exist in <owner>/<repo>"}
exit 2
```

### E-CHECK-03 — a sound, unblocked issue

Setup: bare-remote, `.agent-claim/board.toml` tracked, fake `gh`, issue `#81` open, body a complete `agent-claim` block, no open `blocked_by` dependencies

```console
$ aco check 81
ISSUE #81 body ok
exit 0
```

### E-CHECK-04 — a malformed issue body

The defect text itself is BODY-01's own fact; this session drives only
CHECK-06, this file's own `ISSUE #<n> ` wrapping around it.

Setup: bare-remote, `.agent-claim/board.toml` tracked, fake `gh`, issue `#81` open, BODY-01's own defect (no `agent-claim` fence)

```console
$ aco check 81
2> ISSUE #81 body malformed: agent-claim: no agent-claim block
exit 2
```

### E-CHECK-05 — an issue blocked by a local and a foreign item

Setup: bare-remote, `.agent-claim/board.toml` tracked, fake `gh`, issue `#81` open with a complete body, open `blocked_by` dependencies on `#7` (same repository) and `other/repo#9`

```console
$ aco check 81
2> ISSUE #81 blocked by #7, other/repo#9
exit 3
$ aco check 81 --json
{"ok": false, "reason": "blocked", "kind": "issue", "number": 81, "blocked_by": ["#7", "other/repo#9"], "message": "blocked by #7, other/repo#9"}
exit 3
```

### E-CHECK-06 — no checkout, no read

Setup: a fresh repository outside any git checkout

```console
$ aco check 12
2> ERROR: this command reads the repository's body contract from .agent-claim/board.toml and needs a checkout (a shallow one is enough): fatal: not a git repository (or any of the parent directories): .git
exit 2
```

### E-CHECK-07 — `--repo` under `storage = "state-ref"`

Setup: bare-remote, `.agent-claim/board.toml` tracked with `storage = "state-ref"`

```console
$ aco --repo acme/items check 258 --json
2> ERROR: --repo is meaningless under storage = state-ref
{"ok": false, "reason": "invalid_usage", "message": "--repo is meaningless under storage = state-ref"}
exit 2
```
