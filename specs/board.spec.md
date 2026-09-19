# `aco board`

`aco board` projects the open work board read-only, in four output modes:
the fixed-width text table (default), `--json`, `--html` (issue #276, a
static page), and `--serve` (issue #280, that same page over a loopback
HTTP server with a one-click `POST /rule` form). This file owns those four
modes' own shapes -- the text sections, the `--json` keys, the HTML page's
section order and empty states, and the serve loop's URL line and its
`GET`/`POST` wire contract -- and the one forge-resolution precondition
`aco next`/`aco rulings` share with it before either ever reads an issue
(`## The shared forge precondition`, cited rather than restated by
`specs/next.spec.md`/`specs/rulings.spec.md`). It never restates a fact
another file already owns: `<label>`'s two forms and the Landungen
pairing rule are `specs/landing-grammar.spec.md`'s (LAND-41..54); the
untracked-pin refusal is `specs/storage-pin.spec.md`'s (PIN-01); a ruled
line's own write path is `specs/rule.spec.md`'s (RULE-01..09), which
`board --serve`'s `POST /rule` form calls verbatim. `board`'s own ranking,
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
| board empty of ready/stale/recovery rows | BOARD-05 | — | — | — |
| landed-but-open items exist | BOARD-06 | — | — | — |
| the board source cannot list merged pull requests | BOARD-07 | BOARD-13 | BOARD-21 | BOARD-21 |
| a container, with or without an open child | BOARD-08 | — | — | — |
| an item with no recognized `kind` | BOARD-09 | BOARD-09 | — | — |
| an uncut `[[slice]]` row | BOARD-10 | BOARD-15 | — | — |
| a completed run | BOARD-11 | BOARD-12, BOARD-14 | — | — |
| `--html`/`--json`/`--serve` combined | BOARD-17 | BOARD-17 | BOARD-17 | BOARD-17 |
| `--html PATH` given, or omitted | — | — | BOARD-16 | — |
| open expectation lines, cards | — | — | BOARD-19, BOARD-20, BOARD-32 | BOARD-25 |
| a live claim on an item | — | — | BOARD-19 | — |
| a fresh `--serve` start | — | — | — | BOARD-22, BOARD-23 |
| `GET /` with a missing/wrong/valid token | — | — | — | BOARD-24, BOARD-25 |
| `POST /rule` valid, wrong token, malformed, bad length | — | — | — | BOARD-26..29 |
| an unknown path | — | — | — | BOARD-31 |

## The shared forge precondition

`aco next` and `aco rulings` reach this exact check the same way `board`
does, before either reads a single issue -- cited there, not restated.

- [ ] [BOARD-02] A canonical remote whose host is not GitHub refuses `ERROR: no forge adapter for host <host>`, exit `2`, before `discover_repository`/`gh` ever runs (see E-BOARD-01).
- [ ] [BOARD-01] `board` reaches the same untracked-`.agent-claim/board.toml` refusal `specs/storage-pin.spec.md` owns (PIN-01), exit `2`, before the host check above ever runs.

## Text output

- [ ] [BOARD-03] Every row's `ACTIONABLE` cell reads `yes`, or `no: <reason>` -- `no: claimed`, or any other `actionable_reason` verbatim (see E-BOARD-02).
- [ ] [BOARD-05] `READY NOW`, `STALE`, and `RECOVERY (close or re-project)` each list comma-joined `<label>`s in board order, or `none` when empty (see E-BOARD-02).
- [ ] [BOARD-06] `RECOVERY (close or re-project)` names every item a merged pull request declared closed while it stayed open (`specs/landing-grammar.spec.md`, LAND-43).
- [ ] [BOARD-07] A board source that cannot list merged pull requests appends `landings are not derivable from this board source: recovery and code-landed stay empty` after `RECOVERY`.
- [ ] [BOARD-08] `CONTAINERS` lists one line per container item, open child or not: `<label> <closed>/<total> closed; open: <children or none>`, or `none` when there is none (see E-BOARD-02).
- [ ] [BOARD-09] An item the forge reports no `kind` for, or a non-`container` `kind`, is left out of `CONTAINERS` even with child counts of its own (#309): never guessed at (see E-BOARD-03).
- [ ] [BOARD-10] `UNCUT` lists one line per item with an undispatched `[[slice]]` row, `<label>: rows <indices> uncut`, or `none` when every slice table is either empty or fully cut.
- [ ] [BOARD-11] The table and every section are followed by one closing line, `requests: <n>`, `<n>` the exact count of forge calls this run made (see E-BOARD-02).

## `--json`

- [ ] [BOARD-12] The top-level object carries exactly `items`, `ready_now`, `stale`, `recovery`, `uncut`, `requests`, `landings_derivable` -- never a `repository` key (see E-BOARD-04).
- [ ] [BOARD-13] `landings_derivable` is `false` for a board source that cannot list merged pull requests, `true` otherwise -- the one field `render`'s BOARD-07 line reports in text.
- [ ] [BOARD-14] Each item's `open_blockers` is split into a same-repository `int` list plus a sibling `foreign_blockers` list of `"<repository>#<n>"` strings, never one mixed list.
- [ ] [BOARD-15] An `uncut` row's own `scope` key is present, canonical and non-empty only when that row carries a `scope` of its own; a scopeless row's object carries no `scope` key at all, never `"scope": null`.

## `--html`

- [ ] [BOARD-16] `--html` with no `PATH` writes the page to stdout; `--html PATH` writes it to that file and stdout stays empty (see E-BOARD-05).
- [ ] [BOARD-17] `--html`, `--json`, and `--serve` are mutually exclusive: combining two refuses `aco board: error: argument <second>: not allowed with argument <first>`, exit `2`, before any read (see E-BOARD-06).
- [ ] [BOARD-19] The page carries exactly four `<h2>` sections in order: `Wartet auf dich <N>`, `Lanes <N>`, `Themen` (uncounted), `Landungen <N>` (see E-BOARD-05).
- [ ] [BOARD-20] An empty `Wartet auf dich`/`Lanes` list renders `<p class="empty">nichts</p>`; an empty `Themen` list renders `<li class="empty">nichts</li>`.
- [ ] [BOARD-21] `Landungen` shows `<p class="empty">nicht ableitbar</p>` only once its list is empty and `landings_derivable` is `false` (LAND-46); a proven-empty list still shows plain `nichts`.
- [ ] [BOARD-32] The static page (no `--serve`) shows each open card's three outcomes as copyable `aco rule <n> --line <k> --<outcome>` lines, never a live form.

## `--serve`

- [ ] [BOARD-22] `--serve` prints exactly one line, `http://127.0.0.1:<port>?t=<token>`, flushed to stdout before the process ever blocks on the request loop (see E-BOARD-07).
- [ ] [BOARD-23] A Ctrl-C during `--serve` exits `0` with only that one URL line ever printed and nothing on stderr.
- [ ] [BOARD-24] `GET /` with a missing or wrong `?t=` token responds `403` with body `forbidden: missing or wrong token`, never rendering the page.
- [ ] [BOARD-25] `GET /` with the valid token renders one `POST /rule` form per open card, carrying the token, item, line, a note field, and `yes`/`no`/`later` as submit buttons.
- [ ] [BOARD-26] `POST /rule` with a valid token writes exactly the ruling `aco rule --line N --<outcome>` would (`specs/rule.spec.md`, RULE-01) and answers `303`, redirecting to `/?t=<token>`.
- [ ] [BOARD-27] `POST /rule` naming an already-ruled line redirects to `/?t=<token>&refused=<sentence>`, `<sentence>` the URL-encoded RULE-04 text; the line is left unchanged.
- [ ] [BOARD-28] `POST /rule` with a missing or wrong token responds `403` with body `forbidden: missing or wrong token` and writes nothing.
- [ ] [BOARD-29] `POST /rule` missing `item`, `line`, or `outcome`, or carrying a non-digit `item`/`line`, responds `400` with body `bad request: item, line, and outcome are required`.
- [ ] [BOARD-30] A missing, non-digit, negative, or oversized `Content-Length` responds `400` with body `bad request: missing, invalid, or oversized Content-Length`, before the request body is ever read.
- [ ] [BOARD-31] Any path but `/` on `GET`, or any path but `/rule` on `POST`, responds `404` with body `not found`.

## Never

- `board --html` never performs a `gh` call beyond what plain `board` already made for the same fixture: the Landungen section reuses `board`'s own merged-pull-request read rather than asking a second time.
- `board --serve`'s per-request access log is silenced: the default HTTP server log, which would otherwise print every request line -- including a `POST /rule`'s `?t=<token>` query string -- to stderr, never runs.
- `board_serve.py` never re-validates a rule request's own outcome or line index itself: every refusal it can show (BOARD-27) is `specs/rule.spec.md`'s own `protocol.ClaimError` text, carried back unchanged.
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
SCORE  ISSUE  KIND             ...  ACTIONABLE     ...  TITLE
...    #10    -                ...  no: claimed    ...  Ship #10.
...    #11    container 2/3    ...  no: container; claim a child  ...  Container epic

READY NOW
none

STALE
none

RECOVERY (close or re-project)
none

CONTAINERS
#11 2/3 closed; open: #12

UNCUT
none

requests: 3
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
{"items": [...], "ready_now": [...], "stale": [], "recovery": [], "uncut": [], "requests": 1, "landings_derivable": true}
exit 0
```

### E-BOARD-05 — `--html` to a file, and its four sections

Setup: bare-remote, fake `gh`, one open issue `#10` with a live claim

```console
$ aco board --html board.html
exit 0
$ grep -o '<h2 id="[a-z]*"' board.html
<h2 id="you"
<h2 id="lanes"
<h2 id="topics"
<h2 id="landed"
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
http://127.0.0.1:54321?t=Xy0AbCdEf...
```

The line above is the whole of this transcript (SPEC-16's shell block):
`--serve` then blocks in its request loop, so no further line is printed
until it is stopped. `board_serve`'s own `GET`/`POST` wire behaviour
(BOARD-24..30) is proven by `tests/test_board_serve.py`'s real HTTP client
against the bound server, never by a shell transcript here.
