# Release

`aco release` ends one live claim, `--merged <pr|sha|empty>` or `--abandoned
REASON`, and reports what that ending changed. This file owns the command's
own flags, its identity/branch resolution, which live claim it selects, its
`RELEASED ...` line and `--json` shape, and when the shared `freed`/`next`/
`hint` facts appear at all. It never restates what a `--merged` release
verifies against the pull request or, under `storage = "state-ref"`, the
trunk walk (`specs/landing-grammar.spec.md`, `## What release --merged
requires`), the exact `freed`/`next` line and `--json` shapes
(`specs/landing-grammar.spec.md` LAND-49), the missing-state-ref sentence
(`specs/ref-store-cas.spec.md` CAS-03), or a claimant refusal
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

| state \ trigger | `aco release` (either outcome) | `--merged <pr\|sha\|empty>` | `--abandoned REASON` |
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
| pull request verification | — | LAND-29..39, 49, 50, 55 | — |
| `storage = "state-ref"` trunk verification | — | REL-17 (LAND-47, LAND-52, LAND-56, LAND-59) | — |
| released item's own body contract | REL-23 | REL-23 | REL-23 |
| successful release, text output | REL-18 | REL-18 | REL-18 |
| successful release, `--json` | REL-19 | REL-19, REL-35 | REL-19 |
| landing board read resolves | — | REL-20 | — |
| landing board read hits an unreachable forge | — | REL-22 | — |
| no landing to report | — | — | REL-21 |
| any refusal past the parser, with `--json` | REL-24 | REL-24 | REL-24 |
| a successful outcome's own worktree/branch cleanup | — | REL-25..REL-32, REL-34 | REL-33 |

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

- [ ] [REL-16] `release --merged <pr>`'s forge verification runs before anything is written; it is `specs/landing-grammar.spec.md`'s grammar (LAND-29..39, LAND-49, LAND-50, LAND-55), nothing added here.
- [ ] [REL-17] `release --merged` under `storage = "state-ref"` reads `<sha|empty>` against the local trunk walk, never a pull request or forge (LAND-47, LAND-52, LAND-56, LAND-59, `specs/landing-grammar.spec.md`).
- [ ] [REL-23] `release` never reads the released item's own body contract, unlike issue-mode `claim` (`specs/body-block.spec.md` BODY-52).

## The `RELEASED` line and `--json`

`specs/landing-grammar.spec.md` LAND-49 owns `freed`/`next`'s exact shape;
this file owns only when they appear at all.

- [ ] [REL-18] Without `--json`, a successful release always prints `RELEASED <subject>: <claim-id>` first; `--abandoned`, that line is the whole output (see E-REL-01).
- [ ] [REL-19] `--json` on a successful release prints one object: `issue`, `lane`, `branch`, `claim_id`, `agent`, `role`, `reason` (`"merged #<n>"` or `"abandoned: <explanation>"`) (see E-REL-05).
- [ ] [REL-35] A `--merged` release's `--json` object also carries `worktree`, the identical text its printed `worktree:` line shows (REL-25..REL-34), present only for `--merged` (see E-REL-17).
- [ ] [REL-20] A resolved `--merged` landing adds LAND-49's `freed:`/`next:` lines after `RELEASED` in text, or its keys to `--json`, present only then (see E-REL-02).
- [ ] [REL-21] `--abandoned` never resolves the forge, reads the board, or prints `freed`/`next`/`hint` (LAND-39); its `--json` object carries neither key.
- [ ] [REL-22] A `--merged` release whose post-commit board read fails prints LAND-38's `hint:` line, on stdout in text or stderr with `--json`; `freed`/`next` omitted (LAND-50) (see E-REL-06).
- [ ] [REL-24] A release refusal past the parser (every ID but REL-01) with `--json` also prints `{"ok": false, "error": "<sentence>"}`, exit `2` (see E-REL-07).

## `--merged`'s own worktree/branch cleanup

A successful `--merged` outcome (issue #322) removes the lane's own linked worktree and local
branch when both are safe to remove -- after the release's own store transition already
committed, so a cleanup problem never turns a released claim back into a live one. The remote
branch stays the forge merge's own business: only the local worktree and the local branch move
here, never anything on `remote`. Every outcome is loud: exactly one `worktree: <outcome>` line
follows the report in text, and the same text becomes `--json`'s own `worktree` value -- `removed`
when both are gone, `kept -- <reason>` otherwise, one owner for both shapes so they can never
drift apart.

- [ ] [REL-25] A clean linked worktree whose branch is already merged into the canonical remote's own trunk is removed together with that local branch: `worktree: removed` (see E-REL-08).
- [ ] [REL-26] `--keep-worktree` skips that removal outright: `worktree: kept -- --keep-worktree was given`, worktree and branch both left exactly as found (see E-REL-09).
- [ ] [REL-27] A release run from inside the lane's own worktree cannot remove its own cwd: `worktree: kept -- release ran from inside it`, and keeps both (see E-REL-10).
- [ ] [REL-28] A dirty worktree keeps it: `worktree: kept -- dirty`, exit code unaffected (see E-REL-11).
- [ ] [REL-29] A branch not yet provably merged into the default branch keeps it: `worktree: kept -- not merged into the default branch` (see E-REL-12).
- [ ] [REL-30] No linked worktree found on that branch keeps nothing to report: `worktree: kept -- no linked worktree found` (see E-REL-13).
- [ ] [REL-31] The branch checked out on this repository's own shared main checkout, not a linked worktree, keeps it: `worktree: kept -- branch checked out elsewhere` (see E-REL-14).
- [ ] [REL-32] A git failure resolving which worktree matches the lane's branch keeps both and reports it: `worktree: kept -- git failure: <detail>`, the release itself stays committed regardless (see E-REL-15).
- [ ] [REL-33] `--abandoned` never attempts this cleanup at all, the same as it never resolves a forge target (LAND-39).
- [ ] [REL-34] A git failure deleting the local branch after the worktree is already removed reports both halves, never a bare `kept`: `worktree: removed; branch kept -- git failure: <detail>` (see E-REL-16).

## Never

- `release` never reads or checks the released item's own body contract (REL-23).
- `--abandoned` never resolves a forge target, reads the board, or prints `freed`/`next`/`hint`, ever (LAND-39); nor does it ever remove a worktree or branch (REL-33).
- `--claim-id` is never a second selector among several live claims: at most one claim is ever live per identity (`specs/claim-record.spec.md`), so it only ever confirms or refuses the one claim identity/branch resolution already found.
- A `--branch`/`--claim-id` disagreement is never silently resolved by preferring one: REL-11 refuses instead.
- A forge outage discovered after the release's own store transition already committed never undoes or fails that transition (LAND-50): the claim stays released regardless of whether `freed`/`next` could be reported.
- A worktree/branch cleanup problem after that same commit never undoes or fails it either (REL-28..REL-32, REL-34): the claim stays released regardless of whether cleanup removed anything.
- Cleanup never touches the remote branch a forge merge already owns: only the local worktree and local branch are ever removed.

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
`#42` claimed and closed on the forge, no other open board items, run from
inside that same worktree

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: kept -- release ran from inside it
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
`ada/issue-42`, already merged into `main`, run from the main checkout,
issue `#42` claimed

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
hint: could not read the board to report what this landing freed (<error>); run `aco board` once the forge is reachable
worktree: removed
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

### E-REL-08 — a merged release removes its own clean, merged lane worktree

Setup: bare-remote, fake `gh`, a linked worktree `/work/agent-claim-worktrees/issue-42-widget`
on `ada/issue-42`, already merged into `main`, run from the main checkout, issue `#42` claimed

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: removed
exit 0
```

`/work/agent-claim-worktrees/issue-42-widget` and branch `ada/issue-42` are both gone afterward.

### E-REL-09 — `--keep-worktree` skips cleanup outright

Setup: bare-remote, fake `gh`, a linked worktree `/work/agent-claim-worktrees/issue-42-widget`
on `ada/issue-42`, already merged into `main`, run from the main checkout, issue `#42` claimed

```console
$ aco release 42 --merged 57 --keep-worktree
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: kept -- --keep-worktree was given
exit 0
```

`/work/agent-claim-worktrees/issue-42-widget` and branch `ada/issue-42` both remain.

### E-REL-10 — running from inside the lane worktree keeps it, one line and all

Setup: bare-remote, fake `gh`, a linked worktree on `ada/issue-42`, already merged into `main`,
issue `#42` claimed, run from inside that same worktree

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: kept -- release ran from inside it
exit 0
```

### E-REL-11 — a dirty worktree keeps it

Setup: bare-remote, fake `gh`, a linked worktree on `ada/issue-42`, already merged into `main`,
run from the main checkout, an uncommitted file sitting in that worktree, issue `#42` claimed

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: kept -- dirty
exit 0
```

### E-REL-12 — a branch not yet locally merged keeps it

Setup: bare-remote, fake `gh` reporting pull request `#57` merged, a linked worktree on
`ada/issue-42` whose branch this checkout has not itself merged into `main` yet, run from the
main checkout, issue `#42` claimed

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: kept -- not merged into the default branch
exit 0
```

### E-REL-13 — no linked worktree ever built for the branch

Setup: bare-remote, fake `gh`, branch `ada/issue-42` merged into `main` with no linked worktree
of its own (the claim came from a plain checkout), issue `#42` claimed

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: kept -- no linked worktree found
exit 0
```

### E-REL-14 — the branch sits on this repository's own main checkout, not a linked worktree

Setup: bare-remote, fake `gh`, branch `ada/issue-42` checked out on this repository's own main
checkout rather than a linked worktree, run from an unrelated linked worktree, issue `#42` claimed

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: kept -- branch checked out elsewhere
exit 0
```

### E-REL-15 — a git failure resolving the lane's own worktree keeps both

Setup: bare-remote, fake `gh`, a linked worktree on `ada/issue-42`, already merged into `main`,
run from the main checkout, issue `#42` claimed, a git failure while resolving which of the
repository's worktrees sits on `ada/issue-42`

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: kept -- git failure: <detail>
exit 0
```

### E-REL-16 — a branch-deletion failure after the worktree is already gone names both halves

Setup: bare-remote, fake `gh`, a linked worktree on `ada/issue-42`, already merged into `main`,
run from the main checkout, issue `#42` claimed, deleting the local branch fails once the
worktree itself is already removed

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
worktree: removed; branch kept -- git failure: <detail>
exit 0
```

### E-REL-17 — `--json` on a merged release carries the same `worktree` text

Setup: bare-remote, fake `gh`, a linked worktree on `ada/issue-42`, already merged into `main`,
run from the main checkout, issue `#42` claimed

```console
$ aco release 42 --merged 57 --json
{"issue": 42, "lane": null, "branch": "ada/issue-42", "claim_id": "<claim-id>", "agent": "Ada", "role": "builder", "reason": "merged #42", "freed": [], "next": null, "parent_closable": false, "worktree": "removed"}
exit 0
```
