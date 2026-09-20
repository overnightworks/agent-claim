# `aco claim`

`aco claim` opens one live claim on an issue or an issueless lane before the
first worktree edit. This file owns the command's own preconditions (an
isolated worktree, a clean tree, `HEAD` matching `--base`), identity
resolution (issue vs. lane), the slice-rule checks it runs against the open
board (out-of-order, blocked, container, closed, missing, body-incomplete,
missing-parent) and their `--out-of-order REASON` downgrade, and the
`CLAIMED ...`/`--json` report. `specs/claim-record.spec.md` owns the record
itself, the scope grammar, the width gate, roles, resources, overlap and the
cost line (CLAIM-*); `specs/body-block.spec.md` owns a malformed body's own
defect sentence (BODY-50) and an incomplete projection's own skip (BODY-51);
`aco claim` reuses either sentence verbatim before any write (BODY-52). This
file cites those IDs rather than restating them. `<n>` is a claimed issue
number, `<path>` a repository-relative path, `<reason>` a free-text sentence.

## Behavior table

| state \ trigger | `aco claim <n> --scope P` | `aco claim <n>` (no `--scope`) | `aco claim` (lane) |
|---|---|---|---|
| shared main checkout, or trunk branch | CLM-01, CLM-02 | CLM-01, CLM-02 | CLM-01, CLM-02 |
| claim branch/base mismatch the checkout | CLM-03, CLM-04, CLM-20 | CLM-03, CLM-04, CLM-20 | CLM-03, CLM-04, CLM-20 |
| dirty working tree | CLM-05 | CLM-05 | CLM-05 |
| no `--scope` given | — | CLAIM-53, CLAIM-55 | CLM-06 |
| branch not `docs/`/`fix/` prefixed | — | — | CLM-07 |
| `--scope` differs from the item's own scope | CLAIM-54 | — | — |
| higher-priority actionable item is free | CLM-08 | CLM-08 | — |
| `--out-of-order REASON` given | CLM-09 | CLM-09 | — |
| target is blocked | CLM-10 | CLM-10 | — |
| target is a container | CLM-11 | CLM-11 | — |
| target is closed or missing | CLM-12 | CLM-12 | — |
| target body is malformed | BODY-50, BODY-52 | BODY-50, BODY-52 | — |
| target body is incomplete | CLM-13 | CLM-13 | — |
| slice-shaped title, no recorded parent | CLM-14 | CLM-14 | — |
| a replayed (interrupted) request | CLM-15, CLAIM-13 | CLM-15, CLAIM-13 | CLM-15, CLAIM-13 |
| every check clears, with `--json` | CLM-16 | CLM-16 | CLM-16 |
| a check refuses, with `--json` | CLM-17 | CLM-17 | CLM-17 |
| `--resource NAME` | CLAIM-41..46 | CLAIM-41..46 | CLAIM-41..46 |
| wide scope (width gate) | CLM-18 | CLM-18 | CLM-18 |
| wide scope, `--whole` omitted, the item's own body names one | CLM-21 | CLM-21 | — |
| wide scope, neither `--whole` nor the item's own body names one | CLM-22 | CLM-22 | CLM-18 |
| higher-priority item names neither `scope` nor a `[[slice]]` row | CLM-23 | CLM-23 | — |

## The checkout precondition

CLM-01 and CLM-02 both end with the same fix pointer, `; run git worktree add
../<repo>-worktrees/issue-<n>-<slug> -b <agent>/issue-<n>-<slug>`, appended
to the clause each names below.

- [ ] [CLM-01] Off an isolated worktree, claim refuses `build claims require an isolated non-main worktree branch` plus the fix pointer above, exit `2`.
- [ ] [CLM-02] Sharing main's git dir, claim refuses `build claims require a linked isolated worktree checkout` plus the fix pointer above, exit `2`.
- [ ] [CLM-03] A `--branch` differing from the checkout's own current branch refuses `claim branch '<branch>' does not match checkout branch '<current>'`, exit `2`.
- [ ] [CLM-04] A `--base` differing from checkout `HEAD` refuses `claim base <base> does not match checkout HEAD <head>; omit --base to use checkout HEAD`, exit `2`.
- [ ] [CLM-20] A `--base` that is not a full lowercase 40-character commit SHA refuses `base must be a full lowercase commit SHA`, exit `2`.
- [ ] [CLM-05] A dirty working tree refuses `claim must be acquired before the first worktree edit: <path>`, naming the changed paths, exit `2`, before any write.

## Identity: issue or lane

- [ ] [CLM-06] Lane mode (the positional issue omitted) with no `--scope` refuses `lane claim requires --scope; a lane names no item to derive it from`, exit `2` (see E-CLM-01).
- [ ] [CLM-07] A lane-mode branch not prefixed `docs/`/`fix/` refuses `branch '<branch>' is not an issueless lane; pass an issue number, or check out a branch prefixed 'docs/' or 'fix/'`, exit `2`.

## Slice-rule checks against the open board

- [ ] [CLM-08] A higher-ranked actionable item free elsewhere refuses `higher-priority actionable item #<n> (score <n>) is free: <title>; use --out-of-order REASON to proceed`, exit `2` (see E-CLM-02).
- [ ] [CLM-09] `--out-of-order <reason>` turns CLM-08's/CLM-10's refusal into `WARNING: <same sentence>` beside `CLAIMED`, exit `0`; the reason is never stored, only downgrading the check (see E-CLM-03).
- [ ] [CLM-10] A target with an open blocker refuses `#<n> is blocked by <blockers> (open); pass --out-of-order REASON to claim it anyway`, exit `2`; `--out-of-order` downgrades this to a warning too.
- [ ] [CLM-11] A target that is a container refuses `#<n> is a container; claim a child`, exit `2`, unaffected by `--out-of-order`.
- [ ] [CLM-12] A closed or missing target refuses `issue #<n> is closed` or `issue #<n> does not exist here`, exit `2`.
- [ ] [CLM-13] A target whose body is complete but empty on its projection keys refuses `#<n> body incomplete: <fields>`, exit `2`.
- [ ] [CLM-14] A slice-shaped title with no recorded parent prints `WARNING: looks like slice <n> of #<parent> but is no sub-issue of #<parent>; the parent inherits nothing`, still claims, exit `0`.

## Replay and JSON

- [ ] [CLM-15] A replayed request (CLAIM-13) skips every slice-rule check and still prints `CLAIMED issue #<n>: <claim-id>`, even against a now-blocked, lower-ranked target (see E-CLM-04).
- [ ] [CLM-16] With `--json`, a clean claim's warnings print to stderr not stdout, and land in the payload's `checks` array; without `--json` they print to stdout, ahead of `CLAIMED`.
- [ ] [CLM-17] With `--json`, any error-level check refuses `{"refused": true, "issue": <n>, "checks": [...]}` on stdout, exit `2`, never the `{"ok": false, "error": ...}` REL-24 shape.
- [ ] [CLM-18] A scope tripping the width gate refuses before any write, in the wording `claim-record.spec.md` owns (CLAIM-25, CLAIM-26, CLAIM-29), exit `2`.
- [ ] [CLM-19] A successful `--json` claim's object carries `versioned_files`, `versioned_files_total`, `share`, `touches` and `checks`, beside the fields CLAIM-* already owns.
- [ ] [CLM-21] With `--whole` omitted, a target naming its own top-level `whole` admits a wide scope exactly as `--whole REASON` would; that sentence lands on the claim (see E-CLM-05).
- [ ] [CLM-22] Neither `--whole` nor the target's own body `whole` present, the width gate's refusal ends `; pass --whole REASON or set whole in the body`, exit `2` (see E-CLM-05).
- [ ] [CLM-23] A higher-ranked item naming neither `scope` nor a `[[slice]]` row is skipped by CLM-08's own walk (`specs/next.spec.md` NEXT-23); claiming past it costs no `--out-of-order`.

## Never

- `aco claim` never writes the state ref when any slice-rule check is error-level: the refusal reaches the collection point before the transition runs.
- `--out-of-order` never downgrades CLM-11 (container), CLM-12 (closed/missing), or a body-contract/body-incomplete refusal: only the out-of-order and blocked checks read it.
- Lane mode never runs a slice-rule check at all: there is no target issue to weigh against the board.
- `aco claim` never re-derives a replayed claim's scope from the item's body: it takes the live claim's own stored scope outright (CLAIM-53).
- Lane mode never reads a body `whole` field either: a lane names no item to read one from, so its width gate keeps CLM-18's own wording, never CLM-21/22's.
- `aco rescope` never reads a target's body `whole` field: only `--whole` or the live claim's own already-stored reason justifies a wide rescope.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`, a
tracked `.agent-claim/board.toml`, and `ACO_AGENT` set to `Ada`.

### E-CLM-01 — lane mode without `--scope` refuses by name

Setup: bare-remote, bootstrapped, a linked worktree on `docs/tidy-readme`

```console
$ aco claim
2> ERROR: lane claim requires --scope; a lane names no item to derive it from
exit 2
```

### E-CLM-02 — a higher-priority item free elsewhere refuses

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-10`, issue `#11` free and higher-ranked

```console
$ aco claim 10 --scope src/lower.py
2> ERROR: higher-priority actionable item #11 (score 40) is free: Top work; use --out-of-order REASON to proceed
exit 2
```

### E-CLM-03 — `--out-of-order` downgrades the refusal to a warning

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-10`, issue `#11` free and higher-ranked

```console
$ aco claim 10 --scope src/lower.py --out-of-order "Urgent customer incident."
WARNING: higher-priority actionable item #11 (score 40) is free: Top work; use --out-of-order REASON to proceed
CLAIMED issue #10: <claim-id>
1 of 6 versioned files (17%); overlaps no other open claims
exit 0
```

### E-CLM-04 — a replay skips the board entirely

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-10`, already `aco claim 10 --scope src/lower.py --claim-id fixed`, issue `#11` now free and higher-ranked

```console
$ aco claim 10 --scope src/lower.py --claim-id fixed
CLAIMED issue #10: fixed
1 of 6 versioned files (17%); overlaps no other open claims
exit 0
```

### E-CLM-05 — a wide scope justified from the item's own body, and the refusal when neither names one

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, issue `#42` with `scope = [4 paths]` and `whole = "the four adapters share one lock"` in its body

```console
$ aco claim 42
CLAIMED issue #42: <claim-id>
0 of 6 versioned files (0%); overlaps no other open claims
exit 0
```

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-43`, issue `#43` with `scope = [4 paths]` and no `whole` in its body

```console
$ aco claim 43
2> ERROR: scope is wide: 4 paths exceeds three; pass --whole REASON or set whole in the body
exit 2
```
