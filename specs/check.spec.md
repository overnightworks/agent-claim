# `aco check`

`aco check <n>`: one read that dispatches a bare number to one of three
answers, since GitHub gives issues and pull requests the same number space.
A pull request reads `specs/landing-grammar.spec.md`'s own classification,
closing, and parent grammar (LAND-04..LAND-28); this file cites those IDs
rather than restating them and owns only the command's own three-way
dispatch, its issue-mode read of an item's own body contract, its `--json`
envelope, and the checkout it needs to run at all. `specs/body-block.spec.md`
owns the exact `body malformed:`/`body incomplete:` sentences an issue's own
shape can carry (BODY-01..BODY-52); `specs/storage-pin.spec.md` owns the
`'<value>' is not an item reference` refusal for `<n>` itself (PIN-08) and
gates this command like any other resolving the item forge (PIN-04, PIN-05)
under `storage = "state-ref"`. `<n>` is the argument as given, `<repository>`
the checked repository's own `owner/repo` path. A refusal that never even
reaches this command's own dispatch (no checkout, an unreachable forge)
prints `ERROR: <sentence>` on stderr, exit `2`, `--json` also printing
`{"ok": false, "error": "<sentence>"}`, exactly as
`specs/ref-store-cas.spec.md`'s own preamble already documents.

## Behavior table

| state \ trigger | `aco check <n>` (text) | `aco check <n> --json` |
|---|---|---|
| `<n>` is a pull request | LAND-04..LAND-28 (cited) | CHECK-01 |
| `<n>` exists in neither number space | CHECK-02 | CHECK-03 |
| `<n>` is an issue, sound and unblocked | CHECK-04 | CHECK-05 |
| `<n>` is an issue, malformed body | CHECK-06 | CHECK-05 |
| `<n>` is an issue, incomplete body | CHECK-07 | CHECK-05 |
| `<n>` is an issue, blocked | CHECK-08, CHECK-09 | CHECK-05 |
| no checkout in reach | CHECK-10 | CHECK-10 |

## `--json`'s own envelope

`<sentence>` mirrors the text form's own refusal; `"refused"` appears only
then.

- [ ] [CHECK-01] `aco check <n> --json` against a pull request prints `{"ok": <bool>, "kind": "pull_request", "number": <n>}`, `"refused": "<sentence>"` added only when refused.
- [ ] [CHECK-05] `aco check <n> --json` against an issue prints `{"ok": <bool>, "kind": "issue", "number": <n>}`, `<sentence>` CHECK-06/07/08's own text, without the `ISSUE #<n> ` prefix.

## A number in neither space

- [ ] [CHECK-02] `<n>` matching no issue and no pull request prints `REFUSED: #<n> does not exist in <repository>` on stderr, exit `1`, naming no kind word (see E-CHECK-02).
- [ ] [CHECK-03] The same read with `--json` prints `{"ok": false, "kind": "missing", "number": <n>, "refused": "does not exist in <repository>"}`, exit `1`.

## Issue mode: a body's own contract

`<body defect sentence>` and `<sections>` are `specs/body-block.spec.md`'s
own text (BODY-01..BODY-50, BODY-12).

- [ ] [CHECK-04] `<n>` naming a valid, complete, unblocked issue prints `ISSUE #<n> body ok` on stdout, exit `0` (see E-CHECK-03).
- [ ] [CHECK-06] A malformed body prints `ISSUE #<n> <body defect sentence>` on stderr, exit `1` (see E-CHECK-04).
- [ ] [CHECK-07] A structurally valid but unfilled body prints `ISSUE #<n> body incomplete: <sections>` on stderr, exit `1`.
- [ ] [CHECK-08] An issue with open `blocked_by` dependencies prints `ISSUE #<n> blocked by <label>, <label>` on stderr, exit `1`, local blockers first (see E-CHECK-05).
- [ ] [CHECK-09] Under `storage = "state-ref"`, a local blocker's own label is `specs/landing-grammar.spec.md`'s `<label>` (`aco-xxxxxx`); a foreign one stays `owner/repo#n` regardless of either pin.

## No checkout, no read

`<no-checkout sentence>` is `this command reads the repository's body
contract from .agent-claim/board.toml and needs a checkout (a shallow one
is enough): <git detail>`.

- [ ] [CHECK-10] `aco check <n>` outside a git checkout refuses `ERROR: <no-checkout sentence>`, exit `2` (see E-CHECK-06).

## Never

- `aco check` never writes: neither mode ever calls `update_item_body`, and the pull-request mode's own claims read is a pure fetch of the already-observed state ref.
- The issue mode of `check` never fetches the state ref: only the pull-request mode reads the live claims LAND-15/LAND-16 need.
- Under `storage = "state-ref"`, `check <n>` never reaches the pull-request path: that adapter never reports a number as a landing, so every `<n>` resolves to ISSUE or MISSING.
- `check`'s own subject line is always the bare `#<n>`, in every mode, never the storage-aware `<label>` form `specs/landing-grammar.spec.md` defines for `aco next`/`release`'s own narrative lines.
- A refused classification, or a blocked/malformed/incomplete issue, is never exit `2`: only a refusal that never reaches the dispatch (CHECK-10, an unreachable forge) is.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`,
and `ACO_AGENT` set to `Ada`; `<owner>/<repo>` is the runner's own
repository path. A session reading a pull request or an issue also names a
fixed, deterministic fake `gh` as a setup precondition (the shape
`specs/landing-grammar.spec.md` already uses); the checkout-less session
needs neither a fake `gh` nor a checkout at all.

### E-CHECK-01 — a pull request accepted, text and `--json`

Setup: bare-remote, fake `gh`, pull request `#57` by `Ada`, body `Work-Item: #42\n\nCloses #42`, an active claim on issue `#42` matching the pull request's head branch

```console
$ aco check 57
PR #57 by Ada declares Work-Item: <owner>/<repo>#42
exit 0
$ aco check 57 --json
{"ok": true, "kind": "pull_request", "number": 57}
exit 0
```

### E-CHECK-02 — a number in neither space

Setup: bare-remote, fake `gh`, no issue or pull request `#81` exists in this repository

```console
$ aco check 81
2> REFUSED: #81 does not exist in <owner>/<repo>
exit 1
$ aco check 81 --json
{"ok": false, "kind": "missing", "number": 81, "refused": "does not exist in <owner>/<repo>"}
exit 1
```

### E-CHECK-03 — a sound, unblocked issue

Setup: bare-remote, fake `gh`, issue `#81` open, body a complete `agent-claim` block, no open `blocked_by` dependencies

```console
$ aco check 81
ISSUE #81 body ok
exit 0
```

### E-CHECK-04 — a malformed issue body

Setup: bare-remote, fake `gh`, issue `#81` open, body carrying no `agent-claim` block

```console
$ aco check 81
2> ISSUE #81 body malformed: agent-claim: no agent-claim block
exit 1
```

### E-CHECK-05 — an issue blocked by a local and a foreign item

Setup: bare-remote, fake `gh`, issue `#81` open with a complete body, open `blocked_by` dependencies on `#7` (same repository) and `other/repo#9`

```console
$ aco check 81
2> ISSUE #81 blocked by #7, other/repo#9
exit 1
```

### E-CHECK-06 — no checkout, no read

Setup: a fresh repository outside any git checkout

```console
$ aco check 12
2> ERROR: this command reads the repository's body contract from .agent-claim/board.toml and needs a checkout (a shallow one is enough): fatal: not a git repository (or any of the parent directories): .git
exit 2
```
