# `aco board`

`aco board` projects the open work board read-only, in four output modes:
the fixed-width text table (default), `--json`, `--html` (issue #276, a
static page), and `--serve` (issue #280, a loopback HTTP page an operator
opens with the printed link). This file owns those four modes' own shapes
-- the text sections, the `--json` keys, the HTML page's section order and
empty states, and the one line `--serve` prints -- and the one
forge-resolution precondition `aco next`/`aco rulings` share with it before
either ever reads an issue (`## The shared forge precondition`, cited
rather than restated by `specs/next.spec.md`/`specs/rulings.spec.md`). It
never restates a fact another file already owns: `<label>`'s two forms and
the Landungen pairing rule are `specs/landing-grammar.spec.md`'s
(LAND-41..54); the untracked-pin refusal is `specs/storage-pin.spec.md`'s
(PIN-01); the ruling a click on that page writes is `specs/rule.spec.md`'s
(RULE-01..09). `--serve`'s own request/response wire contract is not this
lane's to invent and is not specified here. `board`'s own ranking,
scoring, and per-column cell semantics (`SCORE`, `PRIORITY`, `AGE`, ...)
are pre-existing, untouched behaviour this lane does not re-derive into
criteria; the table's own presence and its `ACTIONABLE`/`no: <reason>`
cell are the one column this file grades, since `next`'s own SKIPPED list
(`specs/next.spec.md`) reuses that exact reason text. `<n>` is an item
number, `<label>` an item as `specs/landing-grammar.spec.md` prints it.

## Behavior table

| state \ trigger | text (default) | `--json` | `--html` | `--serve` |
|---|---|---|---|---|
| unsupported canonical-remote host | BOARD-02 | BOARD-02 | BOARD-02 | BOARD-02 |
| untracked `.agent-claim/board.toml` | PIN-01 (cited) | PIN-01 (cited) | PIN-01 (cited) | PIN-01 (cited) |
| an item claimed or malformed | BOARD-03 | — | — | — |
| board empty of ready/stale/recovery rows | BOARD-04 | — | — | — |
| landed-but-open items exist | BOARD-05 | — | — | — |
| the Landungen view's own rows | BOARD-06 | BOARD-12 | BOARD-20 | BOARD-20 |
| a container, with or without an open child | BOARD-07 | — | — | — |
| an item with no recognized `kind` | BOARD-08 | BOARD-08 | — | — |
| an uncut `[[slice]]` row | BOARD-09 | BOARD-14 | — | — |
| a completed run | BOARD-10 | BOARD-11, BOARD-13 | — | — |
| `--html`/`--json`/`--serve` combined | BOARD-16 | BOARD-16 | BOARD-16 | BOARD-16 |
| `--html PATH` given, or omitted | — | — | BOARD-15 | — |
| open expectation lines, cards | — | — | BOARD-18, BOARD-19, BOARD-21 | — |
| a live claim on an item | — | — | BOARD-17 | — |
| an item with a `size`, measured or not | BOARD-24, BOARD-25, BOARD-29, BOARD-30 | BOARD-26, BOARD-27, BOARD-31 | BOARD-18, BOARD-28 | — |
| a fresh `--serve` start | — | — | — | BOARD-22, BOARD-23 |

## The shared forge precondition

`aco next` and `aco rulings` reach this exact check the same way `board`
does, before either reads a single issue -- cited there, not restated.

- [ ] [BOARD-02] A canonical remote whose host is not GitHub refuses `ERROR: no forge adapter for host <host>`, exit `2`, before any GitHub read is made (see E-BOARD-01).
- [ ] [BOARD-01] `board` reaches the same untracked-`.agent-claim/board.toml` refusal `specs/storage-pin.spec.md` owns (PIN-01), exit `2`, before the host check above ever runs.

## Text output

- [ ] [BOARD-03] Every row's `ACTIONABLE` cell reads `yes`, or `no: <reason>` -- `no: claimed`, or any other `actionable_reason` verbatim (see E-BOARD-02).
- [ ] [BOARD-04] `READY NOW`, `STALE`, and `RECOVERY (close or re-project)` each list comma-joined `<label>`s in board order, or `none` when empty (see E-BOARD-02).
- [ ] [BOARD-05] `RECOVERY (close or re-project)` names every item a merged pull request's own `Work-Item:` line declares, that item itself still open (`specs/landing-grammar.spec.md`, LAND-43).
- [ ] [BOARD-06] `LANDUNGEN` follows `RECOVERY`: one line per row, `<label> <date> <sha7>` or `<label> <date> PR #<n>` (github's own supplement, LAND-44/45), or `none` when empty.
- [ ] [BOARD-07] `CONTAINERS` lists one line per container item, open child or not: `<label> <closed>/<total> closed; open: <children or none>`, or `none` when there is none (see E-BOARD-02).
- [ ] [BOARD-08] An item the forge reports no `kind` for, or a non-`container` `kind`, is left out of `CONTAINERS` even with child counts of its own (#309): never guessed at (see E-BOARD-03).
- [ ] [BOARD-09] `UNCUT` lists one line per item with an undispatched `[[slice]]` row, `<label>: rows <indices> uncut`, or `none` when every slice table is either empty or fully cut.
- [ ] [BOARD-10] The table and every section are followed by one closing line, `requests: <n>`, `<n>` the exact count of forge calls this run made (see E-BOARD-02).
- [ ] [BOARD-24] `ESTIMATE` reads `~<n>h (<S|M|L>, n=<k>)` when measured, else `schwach` or `keine Größe` (see E-BOARD-02).

  ```
  ~5h (M, n=4)   # class has 3+ measured lanes
  schwach        # fewer than 3, including 0
  keine Größe    # the item names no size
  ```
- [ ] [BOARD-25] `MESSUNGEN` follows `UNCUT`: measured, `Messungen (Stand <date>, seit <date>)` then one line per class; else only `keine Messungen seit <date>` (see E-BOARD-02).
- [ ] [BOARD-29] Each `MESSUNGEN` class line reads `<S|M|L>: n=<k>, median <h>h, p80 <h>h`, `(schwach)` appended under three, then `<first>..<last>` dates.
- [ ] [BOARD-30] `MESSUNGEN` appends `<n> Lanes ohne Ende` when any claim is still open, and `<n> Commits ohne lesbaren Item-Trailer` when any transition commit could not be read.

## `--json`

- [ ] [BOARD-11] The top-level object carries exactly `items`, `ready_now`, `stale`, `recovery`, `landings`, `uncut`, `requests`, `measurements` -- never a `repository` key (see E-BOARD-04).
- [ ] [BOARD-12] Each `landings` row carries `{"item", "committed_at", "sha", "pull_request"}`, exactly one of `sha`/`pull_request` non-`null` (LAND-54).
- [ ] [BOARD-13] Each item's `open_blockers` is split into a same-repository `int` list plus a sibling `foreign_blockers` list of `"<repository>#<n>"` strings, never one mixed list.
- [ ] [BOARD-14] An `uncut` row's own `scope` key is present, canonical and non-empty only when that row carries a `scope` of its own; a scopeless row's object carries no `scope` key at all, never `"scope": null`.
- [ ] [BOARD-26] Each item object carries `size` (`"S"`/`"M"`/`"L"`/`null`) and `estimate` (`null`, or the fields below) alongside its other fields.

  ```
  {"size": "M", "estimate": {"item": "aco-1", "size": "M", "median_hours": 5, "n": 4, "weak": false}}
  {"size": null, "estimate": null}
  ```
- [ ] [BOARD-27] The top-level `measurements` object carries exactly `{"classes", "unfinished", "unparsed", "since", "as_of"}` (see E-BOARD-04).
- [ ] [BOARD-31] Each `classes` entry carries `{"stats": {"size", "n", "median_hours", "p80_hours", "weak"}, "first_event_at", "last_event_at"}`; `since` is `null` only with no lane event read.

## `--html`

- [ ] [BOARD-15] `--html` with no `PATH` writes the page to stdout; `--html PATH` writes it to that file and stdout stays empty (see E-BOARD-05).
- [ ] [BOARD-16] `--html`, `--json`, and `--serve` are mutually exclusive: combining two refuses `aco board: error: argument <second>: not allowed with argument <first>`, exit `2`, before any read (see E-BOARD-06).
- [ ] [BOARD-17] Lanes lists one card per live claim, `<label> <title>` heading, Agent/Branch/Alter always, and Now/Next/Blocked by/Done when only when the contract carries them (see E-BOARD-05).
- [ ] [BOARD-18] The page carries exactly five `<h2>` sections in order: `Wartet auf dich <N>`, `Lanes <N>`, `Themen` (uncounted), `Landungen <N>`, `Messungen` (uncounted, issue #357) (see E-BOARD-05).
- [ ] [BOARD-19] An empty `Wartet auf dich`/`Lanes` list renders `<p class="empty">nichts</p>`; an empty `Themen` list renders `<li class="empty">nichts</li>`.
- [ ] [BOARD-20] `Landungen` renders each row `<label> <date> <sha7>` or `<label> <date> PR #<n>` (LAND-45/46); empty renders plain `nichts`, never a capability-gated line.
- [ ] [BOARD-21] The static page (no `--serve`) shows each open card's three outcomes as copyable `aco rule <n> --line <k> --<outcome>` lines, never a live form.
- [ ] [BOARD-28] `Messungen` renders BOARD-25's own first line as a `<p>`, every further line as one `<li>`; empty, it renders `<p class="empty">…</p>` instead, never an empty `<ul>`.

## `--serve`

- [ ] [BOARD-22] `--serve` prints exactly one line, `http://127.0.0.1:<port>/?t=<token>`, flushed to stdout before the process ever blocks on the request loop (see E-BOARD-07).
- [ ] [BOARD-23] A Ctrl-C during `--serve` exits `0` with only that one URL line ever printed and nothing on stderr.

## Never

- `board --html` never performs a `gh` call beyond what plain `board` already made for the same fixture: the Landungen section reuses `board`'s own merged-pull-request read rather than asking a second time.
- A container whose own child-count summary disagrees with its open-children list (a stale summary, a lost paginated row) is never silently reconciled into a `CONTAINERS`/`--json` row: the board build refuses by name instead of rendering a guess.
- `board`'s `--json` never carries a `read_state` key on any item, and never a bare `null` in place of an absent `foreign_blockers`/`uncut` `scope` entry.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`, a
tracked `.agent-claim/board.toml`, and `ACO_AGENT` set to `Ada`. A session
that reads GitHub issues also names a fixed, deterministic fake `gh` as a
setup precondition (the shape `specs/landing-grammar.spec.md` already
uses).

### E-BOARD-01 — an unsupported canonical remote refuses before any read

Setup: bare-remote, `origin` repointed at `file:///srv/git/agent-claim.git`

```console
$ aco board
2> ERROR: no forge adapter for host file
exit 2
```

### E-BOARD-02 — the text board's own sections

Setup: bare-remote, fake `gh`, issue `#10` open and claimed, issue `#11` a
container `2/3` closed with one open child `#12`

```console
$ aco board
SCORE  ISSUE  KIND             ...  ACTIONABLE     ...  ESTIMATE      TITLE
...    #10    -                ...  no: claimed    ...  keine Größe   Ship #10.
...    #11    container 2/3    ...  no: container; claim a child  ...  keine Größe  Container epic

READY NOW
none

STALE
none

RECOVERY (close or re-project)
none

LANDUNGEN
none

CONTAINERS
#11 2/3 closed; open: #12

UNCUT
none

MESSUNGEN
keine Messungen seit <date>

requests: 4
exit 0
```

### E-BOARD-03 — a kindless container is never listed under `CONTAINERS`

Setup: bare-remote, fake `gh`, issue `#20` open with `children_closed`/
`children_total` set on the forge but no recognized `kind`

```console
$ aco board
...
CONTAINERS
none
...
exit 0
```

### E-BOARD-04 — `--json`'s top-level keys, no `repository`

Setup: bare-remote, fake `gh`, one open issue `#10`

```console
$ aco board --json
{"items": [...], "ready_now": [...], "stale": [], "recovery": [], "landings": [], "uncut": [], "requests": 3, "measurements": {"classes": [], "unfinished": 0, "unparsed": 0, "since": null, "as_of": "<date>"}}
exit 0
```

### E-BOARD-05 — `--html` to a file, and its five sections

Setup: bare-remote, fake `gh`, one open issue `#10` with a live claim

```console
$ aco board --html board.html
exit 0
$ grep -o '<h2 id="[a-z]*"' board.html
<h2 id="you"
<h2 id="lanes"
<h2 id="topics"
<h2 id="landed"
<h2 id="measurements"
exit 0
```

### E-BOARD-06 — `--html` and `--json` refuse together

Setup: bare-remote, fake `gh`

```console
$ aco board --html --json
2> aco board: error: argument --json: not allowed with argument --html
exit 2
```

### E-BOARD-07 — `--serve` prints one URL line

Setup: bare-remote, fake `gh`, `--port 0`

```console
$ aco board --serve
http://127.0.0.1:<port>/?t=<token>
```

The line above is the whole of this transcript: `--serve` then blocks in
its request loop, so no further line is printed until it is stopped.
`--serve`'s own request handling beyond that one line is untouched,
pre-existing behaviour outside this file's scope; `tests/test_board_serve.py`
proves it directly.
