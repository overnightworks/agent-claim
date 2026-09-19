# Release

`aco release` ends one live claim, `--merged <pr>` or `--abandoned REASON`,
and reports what that ending changed. This file owns the command's own
flags, its identity/branch resolution, which live claim it selects, its
`RELEASED ...` line and `--json` shape, and when the shared `freed`/`next`/
`hint` facts appear at all. It never restates what a `--merged` release
verifies against the pull request (`specs/landing-grammar.spec.md`,
`## What release --merged requires`), the exact `freed`/`next` line and
`--json` shapes (`specs/landing-grammar.spec.md` LAND-49), the state-ref pin
refusal (LAND-40, `specs/storage-pin.spec.md` PIN-12), the missing-state-ref
sentence (`specs/ref-store-cas.spec.md` CAS-03), or a claimant refusal
(`specs/claim-record.spec.md` CLAIM-16, CLAIM-17, CLAIM-38..CLAIM-40) --
each is cited by ID.

`<claim-id>` is the released claim's own id. `<subject>` is the same unquoted
grammar `CLAIMED`/`RESCOPED` already print: `issue <label>` or `lane <branch>`
(`<label>` is
`specs/landing-grammar.spec.md`'s own convention -- `#<n>` under
`storage = "github"`, `aco-xxxxxx` under `storage = "state-ref"`).
`<identity>`, printed only by the no-live-claim refusal below, is a
different, always-plain grammar: `issue #<n>` or `lane '<branch>'`, quoted,
never storage-aware. A refusal reaching the shared collection point prints
`ERROR: <sentence>` on stderr and exits `2`, exactly as
`specs/claim-record.spec.md` already documents.

## Behavior table

| state \ trigger | `aco release` (either outcome) | `--merged <pr>` | `--abandoned REASON` |
|---|---|---|---|
| neither or both outcome flags given | REL-01 | REL-01 | REL-01 |
| `REASON` blank, padded, multiline, or over 512 characters | — | — | REL-02 |
| lane mode, branch not `docs/`/`fix/` | REL-03 | REL-03 | REL-03 |
| explicit `--branch` | REL-04 | REL-04 | REL-04 |
| `--branch` omitted, issue + `--claim-id` both given | REL-05 | REL-05 | REL-05 |
| `--branch` omitted, no issue, empty checkout branch | REL-06 | REL-06 | REL-06 |
| `--branch` omitted, issue without `--claim-id`, empty checkout branch | REL-07 | REL-07 | REL-07 |
| `--coordinator-override` without `--role coordinator` | REL-08 | REL-08 | REL-08 |
| identity/branch resolve to no live claim | REL-09 | REL-09 | REL-09 |
| `--claim-id` mismatches the resolved claim | REL-10 | REL-10 | REL-10 |
| `--branch` and `--claim-id` disagree | REL-11 | REL-11 | REL-11 |
| wrong claimant, no override | REL-12 (CLAIM-38) | REL-12 | REL-12 |
| `--coordinator-override --role coordinator` | REL-13 (CLAIM-40) | REL-13 | REL-13 |
| `--role` omitted | REL-14 | REL-14 | REL-14 |
| `refs/aco/state` not yet bootstrapped | REL-15 (CAS-03) | REL-15 | REL-15 |
| pull request verification | — | LAND-29..40, 49, 50 | — |
| `storage = "state-ref"` | — | REL-17 (LAND-40, PIN-12) | — |
| released item's own body contract | REL-23 | REL-23 | REL-23 |
| successful release, text output | REL-18 | REL-18 | REL-18 |
| successful release, `--json` | REL-19 | REL-19 | REL-19 |
| landing board read resolves | — | REL-20 | — |
| landing board read hits an unreachable forge | — | REL-22 | — |
| no landing to report | — | — | REL-21 |
| any refusal past the parser, with `--json` | REL-24 | REL-24 | REL-24 |

## Flags and outcome

- [ ] [REL-01] `aco release` with neither `--merged PULL_REQUEST` nor `--abandoned REASON`, or with both, is refused by the parser itself before anything runs, exit `2`.
- [ ] [REL-02] An `--abandoned` value blank, padded, with a control character, multiline, or over 512 characters refuses `abandoned reason must be one bounded non-empty line`, exit `2`.

## Identity and branch resolution

Omitting the positional issue number and an explicit `--branch` both select
lane mode the same way `aco claim`'s own lane mode does; a future `claim`
spec would cite REL-03 rather than restate it.

- [ ] [REL-03] A lane-mode branch not prefixed `docs/` or `fix/` refuses `branch '<branch>' is not an issueless lane; pass an issue number, or check out a branch prefixed 'docs/' or 'fix/'`, exit `2`.
- [ ] [REL-04] An explicit `--branch <branch>` selects that claim by name and never reads the checkout's current branch at all (see E-REL-01).
- [ ] [REL-05] `--branch` omitted together with an issue number and `--claim-id` skips reading the checkout branch entirely, the same as an explicit `--branch` would.
- [ ] [REL-06] `--branch` omitted, no issue, empty branch refuses `lane release requires a non-empty current branch; check out the docs/ or fix/ lane branch, or pass an issue number`, exit `2`.
- [ ] [REL-07] `--branch` omitted, issue given without `--claim-id`, empty current branch refuses `release without --claim-id requires a non-empty current branch; pass --claim-id`, exit `2`.
- [ ] [REL-08] `--coordinator-override` without `--role coordinator` refuses (CLAIM-39's sentence) before the checkout branch, git, or the forge are ever read.

## Claim selection

- [ ] [REL-09] An identity/branch pair with no matching live claim refuses `<identity> has no active build claim`, exit `2`.
- [ ] [REL-10] A `--claim-id` mismatching the one claim already resolved refuses that same `has no active build claim` sentence: never a second selector among several claims.
- [ ] [REL-11] `--branch` and `--claim-id` naming different branches refuses, quoting both and the claim's own branch, exit `2` (see E-REL-04).
- [ ] [REL-12] A `release` by the wrong agent/role, no coordinator override, refuses (CLAIM-38's sentence), before any write.
- [ ] [REL-13] `--coordinator-override --role coordinator` releases a foreign claim with no agent/role match (CLAIM-40's outcome, for release specifically).
- [ ] [REL-14] Omitting `--role` -- unlike `claim`'s own default `builder` -- reports the claim's own stored role, in text and in `--json` alike.
- [ ] [REL-15] A release before `aco bootstrap` has created `refs/aco/state` refuses (CAS-03's sentence), before any transition is attempted.

## What a `--merged` release verifies and never checks

- [ ] [REL-16] `release --merged <pr>`'s forge verification runs before anything is written; it is `specs/landing-grammar.spec.md`'s grammar (LAND-29..40, LAND-49, LAND-50), nothing added here.
- [ ] [REL-17] `release --merged` under `storage = "state-ref"` refuses without ever contacting the forge (LAND-40, PIN-12's shared sentence; see `specs/storage-pin.spec.md` E-PIN-06).
- [ ] [REL-23] `release` never reads the released item's own body contract, unlike issue-mode `claim` (`specs/body-block.spec.md` BODY-52).

Ruled but not yet built: a future `release --merged <sha|empty>` under
`storage = "state-ref"` is LAND-47/LAND-52 (`specs/landing-grammar.spec.md`,
owned by #297) -- REL-17 stands until that lands.

## The `RELEASED` line and `--json`

`specs/landing-grammar.spec.md` LAND-49 owns `freed`/`next`'s exact shape;
this file owns only when they appear at all.

- [ ] [REL-18] Without `--json`, a successful release always prints `RELEASED <subject>: <claim-id>` first; `--abandoned`, that line is the whole output (see E-REL-01).
- [ ] [REL-19] `--json` on a successful release prints one object: `issue`, `lane`, `branch`, `claim_id`, `agent`, `role`, `reason` (`"merged #<n>"` or `"abandoned: <explanation>"`) (see E-REL-05).
- [ ] [REL-20] A resolved `--merged` landing adds LAND-49's `freed:`/`next:` lines after `RELEASED` in text, or its keys to `--json`, present only then (see E-REL-02).
- [ ] [REL-21] `--abandoned` never resolves the forge, reads the board, or prints `freed`/`next`/`hint` (LAND-39); its `--json` object carries neither key.
- [ ] [REL-22] A `--merged` release whose post-commit board read fails prints LAND-38's `hint:` line, on stdout in text or stderr with `--json`; `freed`/`next` omitted (LAND-50) (see E-REL-06).
- [ ] [REL-24] A release refusal past the parser (every ID but REL-01) with `--json` also prints `{"ok": false, "error": "<sentence>"}`, exit `2` (see E-REL-07).

## Never

- `release` never reads or checks the released item's own body contract (REL-23).
- `--abandoned` never resolves a forge target, reads the board, or prints `freed`/`next`/`hint`, ever (LAND-39).
- `--claim-id` is never a second selector among several live claims: at most one claim is ever live per identity (`specs/claim-record.spec.md`), so it only ever confirms or refuses the one claim identity/branch resolution already found.
- A `--branch`/`--claim-id` disagreement is never silently resolved by preferring one: REL-11 refuses instead.
- A forge outage discovered after the release's own store transition already committed never undoes or fails that transition (LAND-50): the claim stays released regardless of whether `freed`/`next` could be reported.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`,
and `ACO_AGENT` set to `Ada`; `<remote>`, `<tmp>` and `<home>` are the
runner's own paths. A session for `--merged` also names a fixed,
deterministic fake `gh` as a setup precondition (the shape
`specs/landing-grammar.spec.md` already uses); an `--abandoned` session
needs no fake `gh` at all (REL-21).

### E-REL-01 — an abandoned lane release, no forge in sight

Setup: bare-remote, bootstrapped, a linked worktree on `docs/tidy-readme`

```console
$ aco claim --scope README.md
CLAIMED lane docs/tidy-readme: <claim-id>
1 of 6 versioned files (17%); overlaps no other open claims
exit 0
$ aco release --abandoned "stopped for the day"
RELEASED lane docs/tidy-readme: <claim-id>
exit 0
```

### E-REL-02 — a merged issue release, closed and unblocking nothing else

Setup: bare-remote, fake `gh`, a linked worktree on `ada/issue-42`, pull
request `#57` merged into `main`, body `Work-Item: #42\n\nCloses #42`, issue
`#42` claimed and closed on the forge, no other open board items

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
exit 0
```

### E-REL-03 — no live claim to release

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, no live claim on issue `#42`

```console
$ aco release 42 --abandoned stopped
2> ERROR: issue #42 has no active build claim
exit 2
```

### E-REL-04 — `--branch` and `--claim-id` disagree

Setup: bare-remote, bootstrapped, a live claim on issue `#42`, claim id
`mine`, branch `ada/issue-42`

```console
$ aco release 42 --branch ada/issue-99 --claim-id mine --abandoned stopped
2> ERROR: --branch 'ada/issue-99' and --claim-id 'mine' disagree: the claim's own branch is 'ada/issue-42'; drop --branch or pass its own value
exit 2
```

### E-REL-05 — `--json` on an abandoned release

Setup: bare-remote, bootstrapped, a live claim on issue `#42`, claim id
`mine`, agent `Ada`, role `builder`, branch `ada/issue-42`

```console
$ aco release 42 --claim-id mine --abandoned "stopped for the day" --json
{"issue": 42, "lane": null, "branch": "ada/issue-42", "claim_id": "mine", "agent": "Ada", "role": "builder", "reason": "abandoned: stopped for the day"}
exit 0
```

### E-REL-06 — a merged release whose landing board read cannot reach the forge

Setup: bare-remote, a fake `gh` that returns the merged pull request but
fails the landing-board read that follows it, a linked worktree on
`ada/issue-42`, issue `#42` claimed

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
hint: could not read the board to report what this landing freed (<error>); run `aco board` once the forge is reachable
exit 0
```

### E-REL-07 — `--json` on a refused release

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, no
live claim on issue `#42`

```console
$ aco release 42 --abandoned stopped --json
{"ok": false, "error": "issue #42 has no active build claim"}
2> ERROR: issue #42 has no active build claim
exit 2
```
