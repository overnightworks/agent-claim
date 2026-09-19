# `aco next`

`aco next` names the one action the board recommends pulling right now,
plus a fixed tail of three further lines every run prints regardless of
which action it recommends: `parallel:`, `scope unknown:`, and `close:`
(issue #348). This file owns every one of those lines' own shape, the
three `NextAction` variants' own text and `--json`, the `RECOVERY`
preamble and `SKIPPED` tail, and exit `0`/`3`. It cites rather than
restates: the forge-resolution precondition (`specs/board.spec.md`,
BOARD-01/BOARD-02), `RECOVERY`'s own content and its always-first
placement (`specs/landing-grammar.spec.md`, LAND-53), the kind
exclusion a container without a recognized `kind` gets from every
container-shaped view (`specs/board.spec.md`, BOARD-08), and the scope
overlap grammar `aco claim`'s own cost line already defines
(`specs/claim-record.spec.md`, CLAIM-31/CLAIM-32). `<n>` is an
item number, `<label>` an item as `specs/landing-grammar.spec.md` prints
it, `<s>` an integer score.

## Behavior table

| state \ trigger | text | `--json` |
|---|---|---|
| unsupported forge host / untracked pin | BOARD-01/02 (cited) | BOARD-01/02 (cited) |
| no actionable item at all | NEXT-01 | NEXT-01 |
| a work item is the top action | NEXT-02, NEXT-03 | NEXT-11 |
| that item's expectations are proposed or old-ruled | NEXT-04 | NEXT-11 |
| a container qualifies to be cut | NEXT-05 | NEXT-12 |
| a childless container has no further `Next` work | NEXT-06 | NEXT-13 |
| that same container still names further work | NEXT-07 | NEXT-13 |
| a landed-but-open item exists | LAND-53 (cited) | NEXT-14 |
| an unworkable item exists beside the top action | NEXT-08 | NEXT-14 |
| further free items exist, disjoint from the first action | NEXT-09, NEXT-22, NEXT-23 | NEXT-15 |
| more than three such candidates | NEXT-10 | NEXT-15 |
| the first action itself names no scope | NEXT-16 | NEXT-15 |
| a candidate names no scope of its own | NEXT-17 | NEXT-15 |
| a closable container or recovery item, any rank | NEXT-18, NEXT-19 | NEXT-14 |
| neither exists | NEXT-20 | NEXT-14 |

## No actionable item

- [ ] [NEXT-01] With nothing left to pull, `aco next` prints `No actionable item.`, still followed by the `parallel:`/`scope unknown:`/`close:` tail, exit `3`; `--json`'s `"action"` is `null`.

## A work item action

- [ ] [NEXT-02] The top-ranked item prints `<label> score <s>: <title>`, then `Next: <item's own Next>`, then `Run: aco claim <n>`, exit `0` (see E-NEXT-01).
- [ ] [NEXT-03] An item naming its own top-level `scope` drops `--scope` from `Run:`; a scopeless item's `Run:` ends `--scope <paths>` and gains a further `scope unknown` line (see E-NEXT-02).
- [ ] [NEXT-04] A still-proposed item adds `expectations unruled: refine before the pull`; a stale ruling adds `ruled <n> landings ago: refine again at the pull` -- never both (see E-NEXT-02).

## A container's own action

- [ ] [NEXT-05] A childless container with an undispatched `[[slice]]` row prints `cut_slice <label>: <next>`, then `Next: aco cut <n> --title "<cut title>"`, exit `0` (see E-NEXT-03).
- [ ] [NEXT-06] A childless container with an empty slice table and a `Next` line naming work prints only `close_container <label>: <next>`, never a guessed `cut` command (see E-NEXT-04).
- [ ] [NEXT-07] The same container with no further `Next` work prints `close_container <label>: <closed>/<total> children closed, no Next work` (see E-NEXT-04).

## `RECOVERY` and `SKIPPED`

- [ ] [NEXT-08] Every other unworkable item is named once under a trailing `SKIPPED` block, `<label>: <reason>`; a container `next` itself recommends cutting or closing is left out of that list.

## `parallel:`

- [ ] [NEXT-09] `parallel:` lists every further free item the walk placed, `<label> (<n> path[s])`, comma-joined, in board order, alongside the first action (see E-NEXT-05).
- [ ] [NEXT-22] `parallel:` occupies live claims' scopes and the first action's scope, then walks further qualifying actions in board order, skipping a close proposal or recovery item outright (see E-NEXT-05).
- [ ] [NEXT-23] A candidate whose `scope` stays disjoint (CLAIM-31/CLAIM-32's grammar) from everything occupied is placed, its `scope` then joining what's occupied for the rest of the walk (see E-NEXT-05).
- [ ] [NEXT-10] Beyond three placed candidates, only the first three are named, followed by `, and <n> more`; `--json`'s own `candidates` array still carries every one (see E-NEXT-05).
- [ ] [NEXT-16] Once the first action names no scope, the tail collapses to `parallel: unknown (first action names no scope)`; `scope unknown:` is skipped, never printed as `none` (see E-NEXT-02).
- [ ] [NEXT-17] `scope unknown:` lists, in board order, every candidate the walk could not place for lacking a scope of its own, or `none`; printed whenever the first action does name a scope.

## `close:`

- [ ] [NEXT-18] `close:` unions closable containers (kind, no open child, no uncut `[[slice]]` row) then recovery items, both in board order, first-seen, regardless of the first action's rank (#310; see E-NEXT-06).
- [ ] [NEXT-19] `close:` never names a container the forge reports no recognized `kind` for (BOARD-08's own exclusion, #309): such an item is read as ordinary, not as one with nothing left to do.
- [ ] [NEXT-20] `close:` prints `none` when neither a closable container nor a recovery item exists.

## `--json`

- [ ] [NEXT-11] A work-item action adds `"action": "work_item"`, `number`, `score`, `title`, `next`, `command`, and `ruling_landings`/`ruling_old`/`ruling_hint` only per NEXT-04 (see E-NEXT-07).
- [ ] [NEXT-12] A cut proposal's object adds `"action": "cut_slice"`, `number`, `title`, `slice`, `cut_title`, `command` (see E-NEXT-07).
- [ ] [NEXT-13] A close proposal adds `"action": "close_container"`, `number`, `closed`, `total`, `next_step` -- never `command`/`cut_title`, since none exists to run (see E-NEXT-07).
- [ ] [NEXT-14] The object always carries `recovery` (`{number, title, step}` each), `skipped` (`{number, reason}` each), and `close` (a bare number array), independent of `action`.
- [ ] [NEXT-15] `parallel` always carries `first_scope_unknown`, `candidates` (`{number, scope}` each, uncapped), and `scope_unknown` (a bare number array).

## Never

- `aco next` never writes anything: it is a pure projection over the same read `board` performs.
- `close:` never fires the cut proposal's own `Next:`/`Run:` lines: a closable container's own action, when it is also the top action, never carries a command -- NEXT-06/NEXT-07 already refuse to invent one.
- `parallel:`/`scope unknown:` never occupy or place a `board.recovery` item: a landed-but-open item is `close:`'s domain alone, never a `parallel:` candidate or an occupant that could crowd out a real free item behind it.
- The first action's own row is never repeated inside `parallel:`'s candidate list, whatever its own scope is.
- Exit `3` never carries any action-specific line: `No actionable item.` alone stands where `Next:`/`Run:` would.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`, a
tracked `.agent-claim/board.toml`, and `ACO_AGENT` set to `Ada`. Every
session below also names a fixed, deterministic fake `gh` as a setup
precondition (the shape `specs/landing-grammar.spec.md` already uses).

### E-NEXT-01 — the top-ranked work item, scoped

Setup: bare-remote, fake `gh`, issue `#10` open, complete, `scope = ["README.md"]`

```console
$ aco next
#10 score -10: Work
Next: Claim #10.
Run: aco claim 10
parallel: none
scope unknown: none
close: none
exit 0
```

### E-NEXT-02 — a scopeless item, proposed expectations, `--scope <paths>`

Setup: bare-remote, fake `gh`, issue `#10` open, no `scope`, one proposed `[[expectation]]`

```console
$ aco next
#10 score -10: Work
Next: Claim #10.
Run: aco claim 10 --scope <paths>
scope unknown
expectations unruled: refine before the pull
parallel: unknown (first action names no scope)
close: none
exit 0
```

### E-NEXT-03 — a container with an uncut slice row

Setup: bare-remote, fake `gh`, container `#181`, no open child, one `[[slice]]` row titled `Scheibe C`

```console
$ aco next
cut_slice #181: Scheibe C
Next: aco cut 181 --title "Scheibe C"
parallel: none
scope unknown: none
close: none
exit 0
```

### E-NEXT-04 — a closable container, with and without further `Next` work

Setup: bare-remote, fake `gh`, container `#182`, no open child, no `[[slice]]` row, `Next` line `keiner`

```console
$ aco next
close_container #182: 3/3 children closed, no Next work
parallel: none
scope unknown: none
close: #182
exit 0
```

### E-NEXT-05 — five disjoint candidates, capped at three

Setup: bare-remote, fake `gh`, six open items each naming its own disjoint scope

```console
$ aco next
#60 score -10: Alpha
Next: Ship Alpha.
Run: aco claim 60
parallel: #61 (1 path), #62 (1 path), #63 (1 path), and 2 more
scope unknown: none
close: none
exit 0
```

### E-NEXT-06 — `close:` unions a closable container and a recovery item

Setup: bare-remote, fake `gh`, a top-ranked item `#70`, a closable container `#71`, a
recovery item `#72` whose merged pull request typed a `Work-Item:` line for it while it stayed open

```console
$ aco next
RECOVERY
#72: close or re-project

#70 score 40: Top ranked work
Next: Ship it.
Run: aco claim 70
parallel: none
scope unknown: none
close: #71, #72
exit 0
```

### E-NEXT-07 — `--json`, the three action shapes

Setup: bare-remote, fake `gh`, container `#181` as in E-NEXT-03

```console
$ aco next --json
{"action": "cut_slice", "number": 181, "title": "Epic", "slice": "Scheibe C", "cut_title": "Scheibe C", "command": "aco cut 181 --title \"Scheibe C\"", "recovery": [], "skipped": [], "parallel": {"first_scope_unknown": false, "candidates": [], "scope_unknown": []}, "close": []}
exit 0
```
