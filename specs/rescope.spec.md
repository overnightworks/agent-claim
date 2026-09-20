# `aco rescope`

`aco rescope` adds or drops paths on a live claim without releasing it. This
file owns the command's own location resolution (`--add`/`--drop` absolute
paths only, issue #314), its checkout preconditions, the scope-combining
refusals, its own `_selected_store_claim` lookup (RESC-14), the
`RESCOPED ...`/`--json` report, and its own `--json` `reason` vocabulary.
`specs/output.spec.md` owns the `--json` envelope itself (OUT-nn: key
order, `ok`, `message`); this file names only `rescope`'s own `reason`
values. `specs/claim-record.spec.md` owns the record itself, the width
gate's literal and `--whole`'s own field (CLAIM-25..30), a foreign-claimant
refusal (CLAIM-37), that a rescope replaces only the scope while
`claim_id`, `base` and age keep counting (CLAIM-49), and one claim's own
`--json` field order (CLAIM-69); `specs/protect.spec.md` owns the checkout
resolver's `relative payload path`, `not in a repository`, `no commit on
this branch` and `default branch unknown` sentences (PROT-09..11, PROT-13)
that `rescope` shares verbatim, `protect`'s own docstring names the
sharing; `specs/claim.spec.md` owns the issueless-lane branch refusal
(CLM-07) `_resolved_identity` shares with `rescope` too. This file cites
those IDs rather than restating them. `<flag>` is `--add` or `--drop`,
`<path>` a path, `<claim-id>` the selected claim's own id.

## Behavior table

| state \ trigger | `--add PATH` | `--drop PATH` | neither given | `--whole REASON` |
|---|---|---|---|---|
| a relative entry, anywhere in either list | RESC-01 | RESC-01 | — | — |
| resolved checkout is outside every repository | PROT-10 | PROT-10 | PROT-10 | — |
| resolved checkout has no commit yet | PROT-11 | PROT-11 | PROT-11 | — |
| current branch is empty | RESC-02 | RESC-02 | RESC-02 | — |
| shared main checkout, branch known | RESC-03 | RESC-03 | RESC-03 | — |
| on the repository's own trunk branch | RESC-04 | RESC-04 | RESC-04 | — |
| default branch cannot be resolved | PROT-13 | PROT-13 | PROT-13 | — |
| the path resolves outside the checkout | RESC-05 | RESC-05 | — | — |
| no live claim on this identity/branch | RESC-14 | RESC-14 | RESC-14 | — |
| a different agent than the claimant | CLAIM-37 | CLAIM-37 | CLAIM-37 | — |
| a comma-bearing value matching nothing | RESC-06 | — (never checked) | — | — |
| dropping a path not in the claim's scope | — | RESC-07 | — | — |
| combined scope equals the current scope | RESC-08 | RESC-08 | RESC-08 | — |
| drop leaves nothing and nothing is added | — | RESC-09 | — | — |
| combined scope is wide | RESC-10 | RESC-10 | — | RESC-11 |
| a clean combine | RESC-12, RESC-13 | RESC-12, RESC-13 | — | RESC-12, RESC-13 |
| a malformed `--add`/`--drop` value | RESC-15 | RESC-15 | RESC-15 | — |
| this claim's own state disallows the rescope | RESC-16 | RESC-16 | RESC-16 | — |
| every other refusal | RESC-17 | RESC-17 | RESC-17 | — |

## `--add`/`--drop` and their own checkout

- [ ] [RESC-01] A `--add`/`--drop` entry that is not itself absolute, anywhere in either list, refuses PROT-09's own `relative payload path`, exit `2`, before the checkout is even resolved.
- [ ] [RESC-02] A resolved checkout with an empty current branch refuses `rescope requires a non-empty current branch; check out the claim branch, or pass an issue number`, exit `2`.
- [ ] [RESC-03] Sharing main's git dir, rescope refuses `build claims require a linked isolated worktree checkout; run this command from this claim's own worktree on '<branch>', not the primary checkout`, exit `2`.
- [ ] [RESC-04] On the repository's own trunk branch, rescope refuses `build claims require an isolated non-main worktree branch; run this command from this claim's own worktree, not the primary checkout`, exit `2`.
- [ ] [RESC-05] A `--add`/`--drop` path resolving outside the resolved checkout refuses `<flag> path '<path>' is outside the resolved checkout <toplevel>`, exit `2`.

## Selecting the live claim

- [ ] [RESC-14] No live claim on this identity/branch, or a `--claim-id` not matching the one live claim, refuses `<subject> has no active build claim`, exit `2`; `rescope` and `release` share this lookup.

## Combining the scope

- [ ] [RESC-06] An `--add` value with a comma matching no versioned file refuses `'<path>' matches no versioned file; one --add path per flag`, exit `2` (its comma is read literally).
- [ ] [RESC-07] A `--drop` value the live claim's own scope does not hold refuses `cannot drop '<path>'; it is not in this claim's scope`, exit `2`.
- [ ] [RESC-08] An `--add`/`--drop` combination leaving the scope set unchanged refuses `rescope does not change the claim scope`, exit `2`; omitting both flags refuses the same way.
- [ ] [RESC-09] Dropping every scoped path with no `--add` to replace them refuses `rescope must leave a non-empty scope`, exit `2`.
- [ ] [RESC-10] A combined scope tripping the width gate refuses, in the wording `claim-record.spec.md` owns (CLAIM-25, CLAIM-26, CLAIM-29; see CLM-18), exit `2`.
- [ ] [RESC-11] `--whole "<reason>"` admits a wide combined scope and replaces the stored `whole_reason`; an omitted `--whole` keeps a prior reason instead of clearing it.
- [ ] [RESC-12] A clean combine prints `RESCOPED <subject>: <claim-id>`, exit `0` (see E-RESC-01).
- [ ] [RESC-13] With `--json`, a clean combine prints the envelope, `reason: "rescoped"`, then `issue`/`lane`, `claim_id`, `agent`, `role`, `base`, `branch`, `scope` (`42`/`null` issue, `null`/`true` lane).

## `--json`'s own `reason` vocabulary

- [ ] [RESC-15] A malformed `--add`/`--drop` value (RESC-01, RESC-05..09) reports `reason: "invalid_usage"` under `--json`.
- [ ] [RESC-16] No live claim to rescope (RESC-14), a foreign claimant (CLAIM-37), or a wide combined scope (RESC-10) reports `reason: "precondition_failed"` under `--json` (see E-RESC-04).
- [ ] [RESC-17] Every other refusal the handler raises -- an unresolved checkout (RESC-02..04, PROT-10, PROT-11, PROT-13) or a corrupted record -- reports `reason: "unavailable"` (a missing identity: OUT-05).

## Never

- `rescope` never reads or requires checkout `HEAD` to match the claim's own `base`, and never refuses on a dirty working tree: only `claim`'s own precondition (CLM-04, CLM-05) checks either.
- `rescope` never runs the comma-ungrounded check over `--drop`: a value the live claim already holds is a fact about the claim, not a checkout typo, so dropping a comma-bearing scoped path always matches it as one whole path.
- `rescope` never falls back to the process's own cwd to interpret a relative `--add`/`--drop` entry: RESC-01 denies outright instead of guessing a location.
- `rescope` has no `--resource` flag (CLAIM-37 already names its missing `--role`): any held resource carries over untouched, and `--json` never prints a `resource` key.
- `rescope`'s checkout falls back to the process's own cwd only when neither `--add` nor `--drop` names a single path at all; a fresh scope is not otherwise inferred.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`, a
tracked `.agent-claim/board.toml`, and `ACO_AGENT` set to `Ada`; `<worktree>`
is the runner's own linked-worktree directory -- every `--add`/`--drop`
example below names a path under it, since only an absolute path is
accepted.

### E-RESC-01 — adding a path, seen in text and JSON

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, already `aco claim 42 --scope README.md`

```console
$ aco rescope 42 --add <worktree>/AGENTS.md
RESCOPED issue #42: <claim-id>
exit 0
$ aco rescope 42 --drop <worktree>/AGENTS.md --json
{"ok": true, "reason": "rescoped", "issue": 42, "lane": null, "claim_id": "<claim-id>", "agent": "Ada", "role": "builder", "base": "<sha>", "branch": "ada/issue-42", "scope": ["README.md"]}
exit 0
```

### E-RESC-02 — a relative entry denies before the checkout resolves

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, already `aco claim 42 --scope README.md`

```console
$ aco rescope 42 --add AGENTS.md
2> ERROR: relative payload path
exit 2
```

### E-RESC-03 — dropping the whole scope with nothing to add

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, already `aco claim 42 --scope README.md`

```console
$ aco rescope 42 --drop <worktree>/README.md
2> ERROR: rescope must leave a non-empty scope
exit 2
```

### E-RESC-04 — no live claim to rescope refuses `precondition_failed`

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, no live claim

```console
$ aco rescope 42 --add <worktree>/AGENTS.md --json
2> ERROR: issue #42 has no active build claim
{"ok": false, "reason": "precondition_failed", "message": "issue #42 has no active build claim"}
exit 2
```
