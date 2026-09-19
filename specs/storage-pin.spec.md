# Storage pin

`storage = "github" | "state-ref"` in `.agent-claim/board.toml` (default
`github`): which adapter owns this repository's board and item data. This
file owns the pin's own values and precondition, which commands the pin
gates by name, the two item id forms it chooses between, and the
state-ref item file's own defects one layer above the `agent-claim` block
grammar. `specs/body-block.spec.md` owns the block's own schema, including
`[record]`'s field-by-field validity (BODY-15..BODY-20) once the pin has
already gated it open; this file never restates those sentences. `<path>`
is `.agent-claim/board.toml`, the pin's own file. Every refusal below
reaches `ERROR: <sentence>` on stderr, exit `2`, unless noted otherwise.

## Behavior table

| pin state \ trigger | any store command | `item new` | `item edit` / `item close` | `release --merged` | an id argument |
|---|---|---|---|---|---|
| `.agent-claim/board.toml` untracked, absent, or ignored | PIN-01 | PIN-01 | PIN-01 | PIN-01 | — |
| `storage` unset (default `github`) | PIN-02 | PIN-09 | PIN-10, PIN-11 | — | PIN-08 |
| `storage` names an unrecognized value | PIN-03 | PIN-03 | PIN-03 | PIN-03 | — |
| `storage = "state-ref"` | PIN-04\*, PIN-05\* | PIN-18..21 | PIN-22..28 | PIN-12 | PIN-08 |
| a state-ref item file itself is malformed | PIN-13..17 | — | — | — | — |
| a fresh item id, minted | PIN-06, PIN-07 | PIN-06, PIN-07 | — | — | — |

\* PIN-04/PIN-05 gate only a command that resolves this repository's item
forge, a narrower set than "any store command" -- see `## Never` for the
exact commands that never do.

## The pin and its precondition

- [ ] [PIN-01] Any store command with an untracked, absent, or ignored `<path>` refuses `<path> is not tracked in this checkout, so its storage pin cannot be trusted: git add -f <path>` (E-PIN-01).
- [ ] [PIN-02] A tracked `.agent-claim/board.toml` naming no `storage` key pins `storage = "github"`, the default every existing repository already reads.
- [ ] [PIN-03] A tracked `.agent-claim/board.toml` naming a `storage` value outside `github`/`state-ref` refuses `board configuration <path> storage must be 'github' or 'state-ref'` (see E-PIN-02).

## `storage = "state-ref"` is forge-free

- [ ] [PIN-04] Under `storage = "state-ref"`, a command resolving the item forge refuses `--repo` with `--repo is meaningless under storage = state-ref`.
- [ ] [PIN-05] Under `storage = "state-ref"`, that same command with no `origin/HEAD` set refuses `cannot resolve the default branch; run aco from a checkout with origin/HEAD set`.

## Item identity: `aco-xxxxxx` versus `#n`

- [ ] [PIN-06] `aco item new --title TITLE` under `storage = "state-ref"` prints exactly one line, the minted id `aco-` plus six lowercase hex characters, exit `0` (see E-PIN-03).
- [ ] [PIN-07] `aco item new --title TITLE --json` prints `{"item": "aco-xxxxxx", "number": n}`.
- [ ] [PIN-08] An id argument matching none of `aco-xxxxxx`, `#n`, or the bare number `n` refuses `'<value>' is not an item reference; use aco-xxxxxx, #n, or the bare number n` (see E-PIN-04).

## Commands refused by the wrong pin

- [ ] [PIN-09] `aco item new` under `storage = "github"` refuses `items live on the forge; open the issue there` (see E-PIN-05).
- [ ] [PIN-10] `aco item edit ITEM` under `storage = "github"` refuses `forge issues are edited on the forge; aco never governs them`.
- [ ] [PIN-11] `aco item close ITEM` under `storage = "github"` refuses `the forge closes its issues; aco never governs them`.
- [ ] [PIN-12] `aco release --merged` under `storage = "state-ref"` refuses, naming the offline path `item close` and `release --abandoned "landed as <sha>"` (see E-PIN-06).

## The state-ref item file, one layer above the block

- [ ] [PIN-13] An `items/<id>.md` entry whose filename is not `aco-` plus six lowercase hex characters plus `.md` makes a state-ref read refuse `items/<name> is not a valid item file name`.
- [ ] [PIN-14] An `items/<id>.md` entry whose bytes are not valid UTF-8 refuses `item <id> is not valid UTF-8`.
- [ ] [PIN-15] An `items/<id>.md` entry whose text carries no valid `agent-claim` block with a `[record]` table refuses `item <id> has a malformed agent-claim block` (see E-PIN-07).
- [ ] [PIN-16] An item whose own `record.parent` names an id no `items/` entry carries refuses `item <parent-id> is referenced as a parent but does not exist`.
- [ ] [PIN-17] An item whose own `record.blocked_by` names an id no `items/` entry carries refuses `item <blocker-id> is listed as a blocker but does not exist`.

## Writing a fresh state-ref item

- [ ] [PIN-18] `aco item new --title TITLE --parent PARENT` against a `PARENT` no `items/` entry carries refuses `#<parent> does not exist`, before any write.
- [ ] [PIN-19] Three failed random-hex mint attempts against an already-full six-character neighbourhood refuse `could not mint a fresh item id in 3 attempts; retry`, before any write.
- [ ] [PIN-20] `aco item new --title TITLE --origin gitlab#514` stores that reference in `record.origin`, so `aco item show <item-id>` ends its header `origin gitlab#514`, never `origin none`.
- [ ] [PIN-21] `aco item new` under `storage = "state-ref"` writes through the same one CAS write path `aco cut`'s own child creation uses (`specs/ref-store-cas.spec.md`, CAS-19..CAS-21).

## Editing and closing a state-ref item

- [ ] [PIN-22] `aco item edit ITEM < body.md` under `storage = "state-ref"` replaces the item's stored body and prints `EDITED aco-xxxxxx`, exit `0` (see E-PIN-08).
- [ ] [PIN-23] `aco item edit ITEM` against an `ITEM` no `items/` entry carries refuses `#<n> does not exist in <owner/repo>`.
- [ ] [PIN-24] `aco item edit ITEM` piping a body with no valid `agent-claim` block refuses with that body's own first defect sentence (`specs/body-block.spec.md`, BODY-01..BODY-50).
- [ ] [PIN-25] `aco item close ITEM` under `storage = "state-ref"` prints `CLOSED aco-xxxxxx` then a `freed: ` line naming every item `ITEM`'s own close just freed, or `freed: none`, exit `0` (see E-PIN-09).
- [ ] [PIN-26] `aco item close ITEM` against an item still carrying a live claim refuses `#<n> has a live claim (<agent> (<role>)); release the claim first`, before any write.
- [ ] [PIN-27] A second `aco item close ITEM` on an already-closed item refuses `#<n> is already closed (closed on <closed_at>)`.
- [ ] [PIN-28] `aco item close ITEM` against an `ITEM` no `items/` entry carries refuses `#<n> does not exist in <owner/repo>`, the same sentence PIN-23 gives `item edit`.

## Never

- `aco item new`/`edit`/`close` never reach a GitHub API call: every one of them is refused by name under `storage = "github"` before the item forge is ever resolved.
- Under `storage = "state-ref"`, `aco status`, `aco protect`, `aco bootstrap`, `aco rescope`, `aco release`, and a lane or already-observed `aco claim` never resolve the item forge, so PIN-04/PIN-05 never gate them.
- `--repo` never selects a repository under `storage = "state-ref"`: there is no host-based target to override.
- `aco item edit`/`close` never overwrite a concurrent writer's change: a stale expected oid refuses by name instead (`specs/ref-store-cas.spec.md`, CAS-20), and the item's stored bytes stay exactly what the last landed write left.
- `aco item close` never deletes an item file or any of its other bytes: only `state`, `closed_at`, and `updated_at` move.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local bare
repository with `main` at one commit, a git identity, `origin/HEAD`, and
`ACO_AGENT` set to `Ada`; `<remote>`, `<tmp>`, and `<home>` are the runner's
own paths, `<item-id>` the id a session itself minted.

### E-PIN-01 — an untracked pin refuses before anything else

Setup: bare-remote, `.agent-claim/board.toml` present on disk but never `git add`ed

```console
$ aco status
2> ERROR: .agent-claim/board.toml is not tracked in this checkout, so its storage pin cannot be trusted: git add -f .agent-claim/board.toml
exit 2
```

### E-PIN-02 — an unrecognized storage value

Setup: bare-remote, `.agent-claim/board.toml` tracked with `storage = "gitlab"`

```console
$ aco status
2> ERROR: board configuration .agent-claim/board.toml storage must be 'github' or 'state-ref'
exit 2
```

### E-PIN-03 — a fresh state-ref item, minted and shown

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked

````console
$ aco item new --title "Reset export"
<item-id>
exit 0
$ aco item show <item-id>
<item-id> · #n · open · parent none · origin none
```agent-claim
version = 1
now = ""
next = ""
done_when = ""

[record]
title = "Reset export"
state = "open"
kind = "task"
labels = []
blocked_by = []
created_at = "<created_at>"
updated_at = "<updated_at>"
```
exit 0
````

### E-PIN-04 — an id argument in no recognized form

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked

```console
$ aco item show not-an-id
2> ERROR: 'not-an-id' is not an item reference; use aco-xxxxxx, #n, or the bare number n
exit 2
```

### E-PIN-05 — `item new` refused under the default pin

Setup: bare-remote, `.agent-claim/board.toml` tracked with no `storage` key

```console
$ aco item new --title "Reset export"
2> ERROR: items live on the forge; open the issue there
exit 2
```

### E-PIN-06 — `release --merged` refused under state-ref

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, a live lane claim

```console
$ aco release --merged
2> ERROR: state-ref cannot verify a merged pull request yet (#230 slice 6); land offline with `item close` and `release --abandoned "landed as <sha>"` until then
exit 2
```

### E-PIN-07 — a hand-corrupted item file

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `items/aco-000001.md` hand-written with no `agent-claim` block

```console
$ aco board
2> ERROR: item aco-000001 has a malformed agent-claim block
exit 2
```

### E-PIN-08 — editing an item

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `<item-id>` already open

````console
$ aco item edit <item-id> <<'BODY'
```agent-claim
version = 1
now = "Cut on 19.09.2026."
next = "Build it."
done_when = "It is built."
```
BODY
EDITED <item-id>
exit 0
````

### E-PIN-09 — closing an item that freed another

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `<item-id>` open with no live claim, `<other-id>` open and blocked only by `<item-id>`

```console
$ aco item close <item-id>
CLOSED <item-id>
freed: <other-id>
exit 0
$ aco item close <item-id>
2> ERROR: #<n> is already closed (closed on <closed_at>)
exit 2
```
