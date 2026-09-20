# Item: new, show, edit, close

`aco item new`/`show`/`edit`/`close`: the one straight-to-`refs/aco/state`
item lifecycle (issues #285, #287, #289, #316, #337, #357). This file owns
each command's own flags (`--title`, `--kind`, `--parent`, `--origin`,
`--scope`, `--size`, `--whole`), its printed and `--json` shapes, and `item
edit`'s record-merge rule. `specs/body-block.spec.md` also owns the stored
`size` field's own schema (BODY-57..BODY-59), cited by ITEM-20 rather than
restated, and the stored `whole` field's own schema (BODY-60..BODY-62),
cited by ITEM-23/24; `specs/claim.spec.md` owns what `claim`/`start` do
with a stored `whole` (CLM-21/22).
`specs/storage-pin.spec.md` already owns the storage-pin refusal under
`storage = "github"` (PIN-09..PIN-11), the id-argument grammar (PIN-08), the
fresh-id mint and its collision refusal (PIN-06, PIN-07, PIN-19), the unknown-
parent and unknown-id refusals (PIN-18, PIN-23, PIN-28), the `--origin` write
and its header read (PIN-20), and `item close`'s live-claim and already-
closed refusals (PIN-25..PIN-28); `specs/body-block.spec.md` owns a stored
`scope` field's own schema (BODY-53..BODY-56) and `item edit`'s malformed-
body refusal (BODY-01..BODY-50, cited by PIN-24); `specs/claim-record.spec.md`
owns the scope canonicalization grammar (CLAIM-19..CLAIM-23) `item new
--scope` reuses -- never CLAIM-18's comma-versioned-file check, which only
`aco claim`/`rescope` apply; `specs/ref-store-cas.spec.md` owns the stale-oid refusal
(CAS-20) a second `item edit`/`item close` from the same snapshot meets;
`specs/release.spec.md` owns the `--json` refusal object's own shape
(REL-24). This file cites those IDs rather than restating them. A refusal
reaching `main`'s own sink prints `ERROR: <sentence>` on stderr, exit `2`.
`<item-id>` is a minted `aco-xxxxxx` id, `<n>` its number, `<oid>`/`<sha>` the
runner's own git object ids.

## Behavior table

| state \ trigger | `item new` | `item show` | `item edit` | `item close` |
|---|---|---|---|---|
| state-ref pin, default `--kind` | ITEM-01 | — | — | — |
| state-ref pin, `--kind container` | ITEM-02 | — | — | — |
| state-ref pin, `--parent` given | ITEM-03 | — | — | — |
| state-ref pin, `--scope` given | ITEM-04 | — | — | — |
| state-ref pin, `--scope` invalid | ITEM-05 | — | — | — |
| state-ref pin, `--origin` given | ITEM-19 | — | — | — |
| `--origin` malformed | ITEM-06 | — | — | — |
| `--size` given, valid or invalid | ITEM-20 | — | ITEM-21, ITEM-22 | — |
| `--whole` given, valid or invalid | ITEM-23 | — | ITEM-24 | — |
| an item, open or closed | — | ITEM-07, ITEM-08 | — | — |
| an unknown id | PIN-18 | ITEM-10 | PIN-23 | PIN-28 |
| `storage = "github"` | PIN-09 | ITEM-11 | PIN-10 | PIN-11 |
| a delivered `[record]`, present or absent | — | — | ITEM-12..ITEM-14 | — |
| `item show`/`edit`/`close --json` | — | ITEM-09 | ITEM-15 | ITEM-16 |
| a refusal reached with `--json` | ITEM-17, ITEM-18 | ITEM-17, ITEM-18 | ITEM-17, ITEM-18 | ITEM-17, ITEM-18 |

## `item new`

- [ ] [ITEM-01] `aco item new --title TITLE` with no `--kind` mints the id, then writes the skeleton body once with `kind = "task"` in its `[record]`; PIN-06/PIN-07 own the id and `--json` shape (see E-ITEM-01).
- [ ] [ITEM-02] `--kind container` writes `Blocked by: nichts` ahead of the block and `kind = "container"` in the stored `[record]` (see E-ITEM-01).
- [ ] [ITEM-03] `--parent PARENT` sets `record.parent` to `PARENT`'s id; unlike `cut`'s own child body, it never writes a `Parent: #<n>` prose line.
- [ ] [ITEM-04] Repeated `--scope` values write a sorted, deduplicated top-level `scope = [...]` ahead of the `[record]` table, CLAIM-19..CLAIM-23's own canonical form (see E-ITEM-01).
- [ ] [ITEM-05] A `--scope` value that is absolute, `..`, or `~`-prefixed refuses with CLAIM-19's own sentence; a duplicate refuses with CLAIM-21's `claim scope contains duplicate paths`, before any write.
- [ ] [ITEM-06] `--origin FORGE#N` failing its grammar refuses `'<value>' is not an origin; use forge#n or host/owner/repo#n, e.g. gitlab#514`, exit `2`, before `item new`'s own body ever runs.
- [ ] [ITEM-19] `--origin`'s grammar is ASCII-only, case-insensitive `forge#n`/`host/owner/repo#n` tokens; an accepted value is stored in `record.origin` with its original case (PIN-20).
- [ ] [ITEM-20] `--size S|M|L` writes the item's own top-level `size` (BODY-57..BODY-59); argparse refuses an invalid value first. `id`/`--json` stay ITEM-01's shape; `size` is never printed.

  ```
  $ aco item new --title "Ship it" --size M
  writes size = "M" at the block's top level, ahead of [record]
  ```
- [ ] [ITEM-23] `--whole REASON` writes the item's own top-level `whole` (BODY-60..BODY-62), the same bound `claim`'s own `--whole` enforces; `claim`/`start` read it back when their own call names none (CLM-21).

## `item show`

- [ ] [ITEM-07] `aco item show ITEM` prints one header, `<id> · #<n> · <state> · parent <parent-or-none> · origin <origin-or-none>`, then the stored body byte-exact, exit `0` (see E-ITEM-02).
- [ ] [ITEM-08] A closed item prints ITEM-07's same header, `state closed`; closing rewrites the body's `[record]` with `state = "closed"`, `closed_at`, and `updated_at`, the rest byte-identical.
- [ ] [ITEM-09] `aco item show ITEM --json` prints `{"item", "number", "state", "parent", "origin", "body"}`, `parent`/`origin` `null` when unset (see E-ITEM-02).
- [ ] [ITEM-10] `aco item show ITEM` against an unknown id refuses `#<n> does not exist in <owner/repo>`, exit `2`.
- [ ] [ITEM-11] `aco item show ITEM` under `storage = "github"` reads the forge issue's own body through ITEM-07/ITEM-09's same header and `--json` shape.

## `item edit`

- [ ] [ITEM-12] `aco item edit ITEM < BODY` takes `title`, `labels`, `blocked_by` from a delivered `[record]` when the piped body carries one valid (see E-ITEM-03).
- [ ] [ITEM-13] `aco item edit ITEM`'s every other field — `parent`, `state`, `origin`, `kind`, `created_at`, `closed_at` — stays this item's own stored value; `updated_at` always moves to now.
- [ ] [ITEM-14] A delivered body carrying no `[record]` table at all leaves `title`, `labels`, `blocked_by` unchanged too, exactly `item edit`'s own pre-#287 behaviour.
- [ ] [ITEM-15] `aco item edit ITEM --json` prints `{"item", "number", "oid"}`, `oid` the freshly written blob's own oid (see E-ITEM-03).
- [ ] [ITEM-21] `item edit --size S|M|L` patches only the top-level `size`, reads no stdin, works under both storages; state-ref also bumps `record.updated_at`.

  ```
  $ aco item edit <item-id> --size L
  patches size = "L" only; every other field, including the body outside it, is untouched
  ```
- [ ] [ITEM-22] `item edit --size` prints `EDITED #<n> size=<S|M|L>` (`--json`: `{"item", "size"}`); an invalid value is refused by argparse before any write.
- [ ] [ITEM-24] `item edit --whole REASON` patches only the top-level `whole`, reads no stdin, works under both storages, prints `EDITED #<n> whole=<reason>` (`--json`: `{"item", "whole"}`).

## `item close`

- [ ] [ITEM-16] `aco item close ITEM --json` prints `{"item", "number", "closed_at"}`; only `item`/`number` overlap ITEM-09's `{"item", "number", "state", "parent", "origin", "body"}` (see E-ITEM-04).

## `--json` and the refusal object

- [ ] [ITEM-17] Every runtime refusal from `item new`/`show`/`edit`/`close`, reached with `--json`, also prints REL-24's own `{"ok": false, "error": "<sentence>"}` object, exit `2`.
- [ ] [ITEM-18] An argparse-level refusal — a malformed `--origin`, or an item argument PIN-08 refuses — prints only the `ERROR:` line, exit `2`; `--json` never adds ITEM-17's object there.

## Never

- `aco item new --scope` never applies CLAIM-25/CLAIM-26's wide-scope gate: a bare directory or four-plus paths write cleanly into the item's own `scope`; only a later `aco claim` on that item enforces width.
- `aco item new` under `storage = "github"` never creates a GitHub issue with a type, a twin search, or a sub-issue link: that path is ruled but unbuilt with no owning item (#310 finding 28), so today it only refuses by name (`items live on the forge; open the issue there`, PIN-09).
- `aco item show` never refuses merely for its storage value itself (ITEM-11 covers both); it does resolve the state-ref forge like `item new`/`edit`/`close`, so `--repo` there refuses same as those (PIN-04, PIN-05).
- `aco item edit`/`close` never reach ITEM-17's refusal object ahead of PIN-08's own argparse-level check: the item argument is parsed before either command body ever runs.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local bare
repository with `main` at one commit, a git identity, `origin/HEAD`, and
`ACO_AGENT` set to `Ada`; `<remote>`, `<tmp>`, and `<home>` are the runner's
own paths, `<item-id>` an id the session itself minted. Sessions whose stdin
carries a fenced block use a four-backtick console fence.

### E-ITEM-01 — a fresh task, then a scoped container

Setup: bare-remote, `storage = "state-ref"` tracked, bootstrapped

```console
$ aco item new --title "Ship it"
<item-id>
exit 0
$ aco item new --title "Docs pass" --kind container --scope docs/README.md --scope docs/PRODUCT.md --json
{"item": "<item-id-2>", "number": <n2>}
exit 0
```

### E-ITEM-02 — item show, text and `--json`

Setup: bare-remote, `storage = "state-ref"` tracked, bootstrapped, `<item-id>`
already `aco item new --title "Ship it"`

````console
$ aco item show <item-id>
<item-id> · #<n> · open · parent none · origin none
```agent-claim
version = 1
now = ""
next = ""
done_when = ""

[record]
title = "Ship it"
state = "open"
kind = "task"
labels = []
blocked_by = []
created_at = "<created_at>"
updated_at = "<updated_at>"
```
exit 0
$ aco item show <item-id> --json
{"item": "<item-id>", "number": <n>, "state": "open", "parent": null, "origin": null, "body": "```agent-claim\nversion = 1\nnow = \"\"\nnext = \"\"\ndone_when = \"\"\n\n[record]\ntitle = \"Ship it\"\nstate = \"open\"\nkind = \"task\"\nlabels = []\nblocked_by = []\ncreated_at = \"<created_at>\"\nupdated_at = \"<updated_at>\"\n```\n"}
exit 0
````

### E-ITEM-03 — item edit, text and `--json`

Setup: bare-remote, `storage = "state-ref"` tracked, bootstrapped, `<item-id>`
already open

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
$ aco item edit <item-id> --json <<'BODY'
```agent-claim
version = 1
now = "Cut on 19.09.2026."
next = "Ship it."
done_when = "It is built."
```
BODY
{"item": "<item-id>", "number": <n>, "oid": "<oid>"}
exit 0
````

### E-ITEM-04 — item close, text and `--json`

Setup: bare-remote, `storage = "state-ref"` tracked, bootstrapped, `<item-a>`
open with no live claim, `<item-b>` open and blocked only by `<item-a>`,
`<item-c>` open with no live claim

```console
$ aco item close <item-a>
CLOSED <item-a>
freed: <item-b>
exit 0
$ aco item close <item-c> --json
{"item": "<item-c>", "number": <n-c>, "closed_at": "<closed_at>"}
exit 0
```

### E-ITEM-05 — the `storage = "github"` refusal

Setup: bare-remote, `.agent-claim/board.toml` tracked with no `storage` key
(the default)

```console
$ aco item new --title "Should not open"
2> ERROR: items live on the forge; open the issue there
exit 2
$ aco item edit 42
2> ERROR: forge issues are edited on the forge; aco never governs them
exit 2
$ aco item close 42
2> ERROR: the forge closes its issues; aco never governs them
exit 2
```
