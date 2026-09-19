# `aco cut`

`aco cut <container> --title T`: create a container's next slice as a fresh
child, remove that slice's own `[[slice]]` row from the container's block,
and recover instead of duplicating when a prior run already got partway.
This file owns the command's own target precondition, row selection,
`--scope` inheritance (issue #337), the fresh child's own body, the printed
`CUT`/`ADOPTED` line and `--json` shape, and the two ways a partial failure
recovers. `specs/body-block.spec.md` owns the `[[slice]]` schema (BODY-43..
BODY-49) and a malformed body's own defect sentences (BODY-01..BODY-52);
`specs/storage-pin.spec.md` owns the id-argument grammar `<container>`
accepts (PIN-08), the fresh child's own state-ref CAS write, the same path
`item new` shares (PIN-21), and `record.parent`'s own existence check
(PIN-16); `specs/ref-store-cas.spec.md` owns the stale-write sentence a
raced state-ref write meets (CAS-20). This file cites those IDs rather than
restating them. `<n>` is the container's own number, `<child>` the fresh or
adopted child's, `<idx>` a `[[slice]]` row's `index`. A refusal reaching the
shared collection point prints `ERROR: <sentence>` on stderr, exit `2`, and
with `--json` also `specs/release.spec.md`'s own `{"ok": false, "error":
"<sentence>"}` object (REL-24).

## Behavior table

| state \ trigger | `cut <n> --title T` | `--row N` | `--scope P` | `--json` |
|---|---|---|---|---|
| `<n>` not open, or not on the board | CUT-01 | CUT-01 | CUT-01 | CUT-01 |
| `<n>` open but not a container | CUT-02 | CUT-02 | CUT-02 | CUT-02 |
| `<n>` itself has a parent | CUT-03 | CUT-03 | CUT-03 | CUT-03 |
| a required forge write is unsupported | CUT-04 | CUT-04 | CUT-04 | CUT-04 |
| `<n>`'s body is malformed | CUT-05 | CUT-05 | — | CUT-05 |
| a slice table, no `--row` | CUT-06, CUT-11 | — | — | CUT-12 |
| a slice table, `--row N` present | — | CUT-06, CUT-11 | — | CUT-12 |
| `--title` mismatches the linked row | CUT-07 | CUT-07 | — | CUT-07 |
| `--row N`, no slice table at all | — | CUT-08 | — | CUT-08 |
| `--row N`, no such row | — | CUT-09 | — | CUT-09 |
| no slice table, or `slice = []` | CUT-10 | — | CUT-23, CUT-24 | CUT-10 |
| an open child already matches the title | CUT-13 | CUT-13 | — | CUT-13 |
| a closed child matches, none open | CUT-14 | — | — | CUT-14 |
| two or more open matches | CUT-15 | — | — | CUT-15 |
| an orphan shares the title, wrong shape | CUT-16 | — | — | — |
| GitHub's own relation write fails | CUT-17 | — | — | — |
| the row-removal write fails, either storage | CUT-18 | — | — | — |
| an identical re-run after either failure | CUT-19 | — | — | CUT-19 |
| a linked row's own `scope` is empty | — | — | CUT-20, CUT-22 | — |
| a linked row already names a `scope` | — | — | CUT-21 | — |
| every successful cut's own child body | CUT-25 | CUT-25 | CUT-25 | — |
| `storage = "state-ref"`, the row removed | CUT-26, CUT-27 | CUT-26 | — | — |

## The target

- [ ] [CUT-01] `aco cut <n> --title T` against an `<n>` that names no open issue on the board refuses `#<n> is not an open container`, exit `2`, before any write.
- [ ] [CUT-02] The same against an open issue that is not a container refuses `#<n> is not a container`, exit `2`.
- [ ] [CUT-03] The same against a container that is itself a child of another item refuses `#<n> is itself a child of <ref>; nested containers are not supported`, exit `2`.

## The forge precondition

- [ ] [CUT-04] `aco cut` refuses `this forge cannot <operation>; cut the slice by hand` for an unsupported `create_child`, `link_child`, or `update_item_body`, exit `2`.

## The body precondition

- [ ] [CUT-05] `<n>` with a malformed body (BODY-50) refuses `#<n> body malformed: <field>: <message>; cut needs a valid agent-claim block`, exit `2`; an incomplete body (BODY-51) is accepted.

## Row selection

- [ ] [CUT-06] With no `--row`, `aco cut <n> --title T` links the first `[[slice]]` entry; `--row N` links entry `N` by its own `index` instead, whichever position it holds.
- [ ] [CUT-07] A linked row whose own `title` differs from `--title` refuses `#<n>'s slice <idx> is titled '<row-title>'; --title must match it exactly`, exit `2`, before any write.
- [ ] [CUT-08] `--row N` against a block with no `slice` key at all refuses `#<n> has no slice table; --row needs one to select a row from`, exit `2`.
- [ ] [CUT-09] `--row N` naming no entry refuses `#<n> has no row N; cuttable rows: <comma-joined indices, or none>`, exit `2` (see E-CUT-03).
- [ ] [CUT-10] A block with no `slice` key, or `slice = []`, links no row: the child gets none, the container's body stays unwritten, the printed line carries no ` row <idx>` suffix (see E-CUT-04).

## Creating the child

- [ ] [CUT-11] A successful cut prints `CUT #<n>[ row <idx>] -> #<child>`, the `row <idx>` clause present only when a row was linked, exit `0` (see E-CUT-02, E-CUT-04).
- [ ] [CUT-12] `aco cut ... --json` prints `{"container": <n>, "row": <idx-or-null>, "child": <child>}`, `"adopted": true` appended only when CUT-13 applies (see E-CUT-02).
- [ ] [CUT-25] The fresh child's body is a `Parent: #<n>` line, a blank line, `body --template`'s unfilled `task` skeleton (BDY-04), plus `scope` from CUT-20/CUT-23 (see E-CUT-02, E-CUT-06).

## Adopting instead of duplicating

- [ ] [CUT-13] An open issue titled exactly `--title` -- linked already, or a recovery orphan (CUT-16) -- is adopted: `ADOPTED` replaces `CUT`, `"adopted": true` in `--json` (see E-CUT-05).
- [ ] [CUT-14] A closed child already titled `--title`, with no open match, refuses `#<n> already has a closed child #<child> titled '<title>'; reopen it or remove the row by hand`, exit `2`.
- [ ] [CUT-15] Two or more open matches refuses `#<n>'s row '<title>' matches more than one open issue (#a, #b); adopt the right one by hand and remove the row`, exit `2`, naming every match.
- [ ] [CUT-16] An open issue sharing `--title` is adopted only in CUT-13's recovery shape -- never the container itself, idea-labelled, non-`task`, or naming a different `Parent:` (see E-CUT-05).

## Partial failure and recovery

- [ ] [CUT-17] GitHub's own failed sub-issue relation write refuses `created #<child> but failed to record #<child> as a sub-issue of #<n>: <cause>; re-run the same cut`, exit `2`.
- [ ] [CUT-18] A failed row-removal write, either storage, refuses `created #<child> but failed to remove row <idx> from #<n>'s agent-claim block: <cause>; re-run the same cut`, exit `2`.
- [ ] [CUT-19] An identical re-run after CUT-17 or CUT-18 prints `ADOPTED` (CUT-13) instead of minting a second child, then finishes the row removal when a row was linked (see E-CUT-06).

## `--scope` (issue #337)

- [ ] [CUT-20] `--scope P` with a linked row that carries none becomes the fresh child's own top-level `scope`; the row is removed along with the rest, never rewritten to carry it.
- [ ] [CUT-21] `--scope P` against a linked row that already names one refuses `slice <idx> already names a scope; edit the container instead`, exit `2` -- the row is the one place to change it.
- [ ] [CUT-22] With no `--scope`, a linked row that already names one leaves the child inheriting that scope verbatim; the row itself is unchanged before its removal.
- [ ] [CUT-23] `--scope P` with no linked row at all (CUT-10) becomes the fresh child's own top-level `scope` directly, no row ever touched.
- [ ] [CUT-24] With no linked row and no `--scope`, the fresh child carries no top-level `scope` key at all.

## `storage = "state-ref"`, the same command over one CAS write

- [ ] [CUT-26] Removing a `[[slice]]` row under `storage = "state-ref"` preserves every byte outside the block's interior; the interior re-renders canonically (BODY-07), not byte-for-byte (see E-CUT-07).
- [ ] [CUT-27] Under `storage = "state-ref"`, the fresh child's body carries CUT-25's `Parent:` prose and a `[record]` table whose `parent` names the container's item id (PIN-16) (see E-CUT-07).

## Never

- `aco cut` never creates a second child for a row already linked to an open issue: CUT-13's adoption always runs before a fresh child is ever considered.
- `aco cut` never reaches CUT-17's own relation-write failure under `storage = "state-ref"`: the child's own mint is one CAS write that can fail before any child exists -- a plain re-run then starts over as a fresh cut, nothing to adopt -- and CUT-18's row-removal step can fail once the child exists.
- `aco cut` never touches the container's own `now`, `next`, or `done_when` fields, or any `[[slice]]` row but the one linked: a row removal's own rewrite carries every other field forward unchanged (CUT-26).
- `aco cut` never re-parents an issue by title alone: an orphan is adopted only through CUT-16's own recovery-shape check, never a bare string match.
- `aco cut`'s own printed `CUT`/`ADOPTED` line and `--json` object are never the storage-aware `<label>` form (`specs/landing-grammar.spec.md`): `<n>` and `<child>` are always the bare number, under either storage pin.
- CUT-25's own body shape is identical under either storage pin; only `storage = "state-ref"` additionally sets `[record].parent` (CUT-27) -- GitHub carries no such field, so its own retry (CUT-19) reads the `Parent:` prose line instead, while a state-ref retry reads `record.parent` alone.
- Under `storage = "state-ref"`, a CUT-19 retry caused by a competing write names CAS-20's own "written since it was read" sentence as CUT-18's own `<cause>`.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`,
and `ACO_AGENT` set to `Ada`. A session under `storage = "github"` (the
default, no `storage` key tracked) also names a fixed, deterministic fake
`gh` as a setup precondition; a session under `storage = "state-ref"` names
no `gh` at all -- `PATH` carries none.

### E-CUT-01 -- refused before any write: not open, not a container, already parented

Setup: bare-remote, `storage = "github"`, fake `gh`

```console
$ aco cut 90 --title "Slice A"
2> ERROR: #90 is not an open container
exit 2
$ aco cut 91 --title "Slice A"
2> ERROR: #91 is not a container
exit 2
$ aco cut 92 --title "Slice A"
2> ERROR: #92 is itself a child of example/agent-claim#80; nested containers are not supported
exit 2
```

### E-CUT-02 -- a tied cut, text and `--json`, then row selection by number

Setup: bare-remote, `storage = "github"`, fake `gh`, container `#90` with two
`[[slice]]` rows, `index = 1` titled `Slice A`, `index = 2` titled `Slice B`

```console
$ aco cut 90 --title "Slice A"
CUT #90 row 1 -> #<child-a>
exit 0
$ aco cut 90 --title "Slice B" --row 2 --json
{"container": 90, "row": 2, "child": <child-b>}
exit 0
```

### E-CUT-03 -- row refusals

Setup: bare-remote, `storage = "github"`, fake `gh`, container `#90`, one
`[[slice]]` row `index = 1` titled `Slice A`

```console
$ aco cut 90 --title "Slice A" --row 9
2> ERROR: #90 has no row 9; cuttable rows: 1
exit 2
$ aco cut 90 --title "Wrong title"
2> ERROR: #90's slice 1 is titled 'Slice A'; --title must match it exactly
exit 2
```

### E-CUT-04 -- an untied container, no slice table

Setup: bare-remote, `storage = "github"`, fake `gh`, container `#90` with no
`slice` key at all

```console
$ aco cut 90 --title "Loose work" --row 1
2> ERROR: #90 has no slice table; --row needs one to select a row from
exit 2
$ aco cut 90 --title "Loose work"
CUT #90 -> #<child>
exit 0
```

### E-CUT-05 -- adoption after a partial failure, and a look-alike that is not adopted

Setup: bare-remote, `storage = "github"`, fake `gh`, container `#90` with one
`[[slice]]` row `index = 1` titled `Slice A`; a first `aco cut 90 --title
"Slice A"` already left an open, unlinked orphan `#<child>` (its relation
write failed)

```console
$ aco cut 90 --title "Slice A"
ADOPTED #90 row 1 -> #<child>
exit 0
```

Setup: bare-remote, `storage = "github"`, fake `gh`, container `#90` with one
`[[slice]]` row `index = 1` titled `Slice A`, and an unrelated open issue
`#<lookalike>` also titled `Slice A` but labelled `idea`

```console
$ aco cut 90 --title "Slice A"
CUT #90 row 1 -> #<child>
exit 0
```

### E-CUT-06 -- `--scope`, filled, inherited, and refused

Setup: bare-remote, `storage = "github"`, fake `gh`, container `#90` with one
empty `[[slice]]` row `index = 1` titled `Slice A`

```console
$ aco cut 90 --title "Slice A" --scope src/a.py
CUT #90 row 1 -> #<child>
exit 0
```

A second container `#91`'s row already names `scope = ["src/b.py"]`:

```console
$ aco cut 91 --title "Slice B" --scope src/other.py
2> ERROR: slice 1 already names a scope; edit the container instead
exit 2
$ aco cut 91 --title "Slice B"
CUT #91 row 1 -> #<child>
exit 0
```

### E-CUT-07 -- `storage = "state-ref"`, byte-exact removal and `record.parent`

Setup: bare-remote, `storage = "state-ref"` tracked, bootstrapped, container
`aco-000001` (`#<n>`) with two `[[slice]]` rows, `index = 1` titled `Slice
C`, `index = 2` titled `Slice D`

````console
$ aco cut <n> --title "Slice C"
CUT #<n> row 1 -> #<child>
exit 0
$ aco item show <child>
<item-id> · #<child> · open · parent aco-000001 · origin none
Parent: #<n>

```agent-claim
version = 1
now = ""
next = ""
done_when = ""

[record]
title = "Slice C"
state = "open"
kind = "task"
labels = []
blocked_by = []
parent = "aco-000001"
created_at = "<created_at>"
updated_at = "<updated_at>"
```
exit 0
````
