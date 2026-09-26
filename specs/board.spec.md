# `aco board`

`aco board` projects the open work board read-only, in three output modes:
`--json`, `--html` (issue #276, a static page), and `--serve` (issue #280, a
loopback HTTP page an operator opens with the printed link); a bare
`aco board` naming none of them refuses instead (BOARD-44, issue #420) --
the retired fourth mode was a fixed-width text table (#390 Befund 13). This
file owns those three modes' own shapes -- the `--json` keys, the HTML
page's section order and empty states, and the one line `--serve` prints --
and the one forge-resolution precondition `aco next`/`aco rulings` share
with it before either ever reads an issue (`## The shared forge
precondition`, cited rather than restated by
`specs/next.spec.md`/`specs/rulings.spec.md`). It never restates a fact
another file already owns: `<label>`'s two forms and the Landungen pairing
rule are `specs/landing-grammar.spec.md`'s (LAND-41..54); the untracked-pin
refusal is `specs/storage-pin.spec.md`'s (PIN-01); the ruling a click on
that page writes is `specs/rule.spec.md`'s (RULE-01..09);
`specs/output.spec.md` owns the `--json` envelope itself (OUT-nn: key
order, `ok`, `message`) that wraps BOARD-11's own top-level keys.
`--serve`'s own request/response wire contract is not specified here beyond
the held page, its age, and its rebuilds (BOARD-46..50). `board`'s own ranking, scoring, and per-item
field semantics (`score`, `priority_bucket`, `age_days`, ...) are
pre-existing, untouched behaviour this lane does not re-derive into
criteria; each item's own `actionable`/`actionable_reason` fields are the
one pair this file grades, since `next`'s own SKIPPED list
(`specs/next.spec.md`) reuses that exact reason text. `<n>` is an item
number, `<label>` an item as `specs/landing-grammar.spec.md` prints it.
`<token-path>` is the served board's own token file,
`${XDG_CONFIG_HOME:-~/.config}/aco/boards/<board>/token`: one directory per
repository, named `<owner>-<repo>` on a forge or after the checkout's own last
two path parts otherwise, followed by a digest of the whole identity, its host
included.

## Behavior table

| state \ trigger | `--json` | `--html` | `--serve` |
|---|---|---|---|
| no `--json`, `--html`, or `--serve` given | BOARD-44 | BOARD-44 | BOARD-44 |
| unsupported canonical-remote host | BOARD-02, BOARD-43 | BOARD-02 | BOARD-02 |
| untracked `.agent-claim/board.toml` | PIN-01 (cited) | PIN-01 (cited) | PIN-01 (cited) |
| `--repo` under `storage = "state-ref"` | BOARD-42, BOARD-43 | BOARD-42 | — |
| the Landungen view's own rows | BOARD-12 | BOARD-20 | BOARD-20 |
| an item with no recognized `kind` | BOARD-08 | — | — |
| an uncut `[[slice]]` row | BOARD-14 | — | — |
| a completed run | BOARD-11, BOARD-13 | — | — |
| `--json`'s success/refusal envelope | BOARD-11, BOARD-43 (OUT-nn cited) | — | — |
| `--html`/`--json`/`--serve` combined | BOARD-16 | BOARD-16 | BOARD-16 |
| `--html PATH` given, or omitted | — | BOARD-15 | — |
| open expectation lines, cards | — | BOARD-18, BOARD-19, BOARD-21 | — |
| the page's own origin | — | BOARD-45 | BOARD-45 |
| a live claim on an item | — | BOARD-17 | — |
| an item with a `size`, measured or not | BOARD-26, BOARD-27, BOARD-31 | BOARD-18, BOARD-28 | — |
| a fresh `--serve` start | — | — | BOARD-22, BOARD-23 |
| `--serve`'s persistent loopback token, minted or read | — | — | BOARD-32, BOARD-33, BOARD-34 |
| `--serve` naming a port another process already holds | — | — | BOARD-35 |
| an already-ruled `[[expectation]]` line | — | BOARD-36, BOARD-37, BOARD-38 | BOARD-36, BOARD-37, BOARD-38 |
| `--new-token` given without `--serve` | BOARD-39, BOARD-43 | BOARD-39 | — |
| the token file's own content, or its directory's mode | — | — | BOARD-40, BOARD-41 |
| a repeated page request, a ruling click, or the reload link | — | — | BOARD-46, BOARD-47, BOARD-48, BOARD-50 |
| a client hanging up mid-response | — | — | BOARD-49 |

## No output mode

- [ ] [BOARD-44] `aco board` naming none of `--json`, `--html`, or `--serve` refuses `aco board requires --json, --html, or --serve`, exit `2`, before any forge, pin, or repository read (see E-BOARD-16).

## The shared forge precondition

`aco next` and `aco rulings` reach this exact check the same way `board`
does, before either reads a single issue -- cited there, not restated.

- [ ] [BOARD-02] A canonical remote whose host is not GitHub refuses `ERROR: no forge adapter for host <host>`, exit `2`, before any GitHub read is made (see E-BOARD-01).
- [ ] [BOARD-01] `board` reaches the same untracked-`.agent-claim/board.toml` refusal `specs/storage-pin.spec.md` owns (PIN-01), exit `2`, before the host check above ever runs.
- [ ] [BOARD-42] Under `storage = "state-ref"`, `board`/`next`/`rulings` resolve the state-ref forge like `item show`/`edit`/`close`; `--repo` there refuses the same as those (PIN-04, PIN-05).

- BOARD-03, BOARD-04, BOARD-05, BOARD-06, BOARD-07, BOARD-09, BOARD-10, BOARD-24, BOARD-25, BOARD-29, BOARD-30 (retired 20.09.2026, issue #420, #390 Befund 13): the fixed-width text table (`board.render`) they described no longer exists; `--json`'s own item fields, `landings` array, `uncut` array, and `measurements` object (BOARD-11..14, BOARD-26, BOARD-27, BOARD-31) and `--html`'s own sections (BOARD-15..21, BOARD-28) carry the equivalent facts.

## `--json`

- [ ] [BOARD-11] `board --json` wraps OUT-nn (`reason: "projected"`) around `items`, `ready_now`, `stale`, `recovery`, `landings`, `uncut`, `requests`, `measurements` only, never `repository` (E-BOARD-04).
- [ ] [BOARD-43] `--json` on a dispatched refusal (BOARD-02, BOARD-39, BOARD-42) prints that envelope with the sentence as `message` and `reason` below, exit `2` (see E-BOARD-15).
- [ ] [BOARD-08] An item the forge reports no `kind` for, or a non-`container` `kind`, carries `"container": null` even with child counts of its own (#309): never guessed at (see E-BOARD-03).

`reason`, by which refusal fired:

| refusal | `reason` |
|---|---|
| PIN-04 (`--repo` under `storage = state-ref`), BOARD-39 (`--new-token` without `--serve`) | `invalid_usage` |
| BOARD-02 (no forge adapter for host), PIN-05 (no resolvable default branch) | `unavailable` |
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
- [ ] [BOARD-16] `--html`, `--json`, `--serve` are exclusive: the pair refuses `argument <second>: not allowed with argument <first>`, exit `2`, before any read; `--json` per OUT-06 (see E-BOARD-06).
- [ ] [BOARD-17] Lanes lists one card per live claim, `<label> <title>` heading, Agent/Branch/Alter always, and Now/Next/Blocked by/Done when only when the contract carries them (see E-BOARD-05).
- [ ] [BOARD-18] The page carries exactly five `<h2>` sections in order: `Wartet auf dich <N>`, `Lanes <N>`, `Themen` (uncounted), `Landungen <N>`, `Messungen` (uncounted, issue #357) (see E-BOARD-05).
- [ ] [BOARD-19] An empty `Wartet auf dich`/`Lanes` list renders `<p class="empty">nichts</p>`; an empty `Themen` list renders `<li class="empty">nichts</li>`.
- [ ] [BOARD-20] `Landungen` renders each row `<label> <date> <sha7>` or `<label> <date> PR #<n>` (LAND-45/46); empty renders plain `nichts`, never a capability-gated line.
- [ ] [BOARD-21] The static page (no `--serve`) shows each open card's three outcomes as copyable `aco rule <n> --line <k> --<outcome>` lines, never a live form.
- [ ] [BOARD-28] `Messungen` renders BOARD-25's own first line as a `<p>`, every further line as one `<li>`; empty, it renders `<p class="empty">…</p>` instead, never an empty `<ul>`.
- [ ] [BOARD-36] An already-ruled `[[expectation]]` line never renders as a "Wartet auf dich" card: it moves into its own item's `Themen` entry instead (see E-BOARD-08).
- [ ] [BOARD-37] That history renders `<li class="ruled"><span>TEXT</span><span class="ruled-state">ruled OUTCOME DATE</span></li>`, the same wording `aco rulings` prints for a ruled line (RUL-02).
- [ ] [BOARD-38] The history is followed by one sentence naming the ruling's immutability and the way to a new one, `<p class="ruled-hint">` pointing at `aco ask <n> --text`, never a button.
- [ ] [BOARD-45] The page's `<title>` and its first masthead line both name the repository this board belongs to and the checkout it was rendered from (see E-BOARD-17).

## `--serve`

- [ ] [BOARD-22] `--serve` prints exactly one line, `http://127.0.0.1:<port>/?t=<token>`, flushed to stdout before the process ever blocks on the request loop (see E-BOARD-07).
- [ ] [BOARD-23] A Ctrl-C during `--serve` exits `0` with only that one URL line ever printed and nothing on stderr.
- [ ] [BOARD-32] BOARD-22's own token is read from `<token-path>`, minted (0600) only when missing, so two starts on one port print the identical URL (see E-BOARD-09).
- [ ] [BOARD-33] `--new-token` mints a fresh token into that same file before printing BOARD-22's URL, replacing the one a prior start minted.
- [ ] [BOARD-34] A token file whose mode is not `0600` refuses `board token file <path> must be private (mode 0600, found <mode>)`, exit `2` (see E-BOARD-10).
- [ ] [BOARD-35] A port another process holds refuses `port <port> is already in use by PID <pid>`, else `port <port> is already in use; the owning process could not be identified`, exit `2` (see E-BOARD-11).
- [ ] [BOARD-39] `--new-token` without `--serve` refuses `--new-token requires --serve`, exit `2`, before any read (see E-BOARD-14).
- [ ] [BOARD-40] A token file's content that is not one `secrets.token_urlsafe(32)` value refuses `board token at <path> is not a valid token; pass --new-token`, exit `2` (see E-BOARD-13).
- [ ] [BOARD-41] A symlinked or others-writable level of `<token-path>` refuses `board token directory <path> must be private and owned by this user (found mode <mode>)`, exit `2` (see E-BOARD-12).
- [ ] [BOARD-46] A repeated page request answers from the page the first one built; a forge change shows only after a ruling click or the reload link (see E-BOARD-18).
- [ ] [BOARD-47] Every served page shows its age and a reload link, `Stand` `vor <h>h <m>m` `neu laden`; the `--html` page carries neither (see E-BOARD-18).
- [ ] [BOARD-48] Every ruling click, written or refused, rebuilds the page, so the page it redirects to shows the line as the forge now holds it.
- [ ] [BOARD-49] A client that hangs up mid-response leaves stderr empty; any other request error still prints its traceback.
- [ ] [BOARD-50] The reload link's request rebuilds, then redirects (`303`) to the plain URL, no `reload` field, so a later plain refresh serves the held page without rebuilding (see E-BOARD-18).

## Never

- `board --html` never performs a `gh` call beyond what `board --json` already made for the same fixture: the Landungen section reuses `board`'s own merged-pull-request read rather than asking a second time.
- A container whose own child-count summary disagrees with its open-children list (a stale summary, a lost paginated row) is never silently reconciled into its own `container`/`--json` field: the board build refuses by name instead of rendering a guess.
- `board`'s `--json` never carries a `read_state` key on any item, and never a bare `null` in place of an absent `foreign_blockers`/`uncut` `scope` entry.
- `--serve` never accepts a `--restart` flag: a stable token (BOARD-32) makes an ordinary `kill` and a fresh start enough, and BOARD-35's own refusal, naming the PID, is the tool an operator needs to do that by hand.
- BOARD-35's refusal never names a PID it could not verify against `/proc`: unable to identify the occupant, it says so instead of guessing one.
- A busy port (BOARD-35) never reaches the token file: the socket is bound first, so `--new-token` (BOARD-33) against a busy port mints or replaces nothing on disk.
- A directory of `<token-path>` (BOARD-41) is never trusted only at creation: one a prior run already made is checked the same way a freshly created one is.
- A token minted for one repository never opens another repository's served board: each board keeps its own token file (BOARD-32), and a foreign token is refused exactly like a wrong one.
- The token BOARD-32 reads or mints is never written to any log line or error message: it appears only in the start URL line BOARD-22 prints, the served page's own rule-form hidden field, and the `POST /rule` redirect back to that page -- issue #234's own contract that every request must carry it -- never in `board --html`'s static page or a `--json` field.
- A ruled `[[expectation]]` line (BOARD-36) never keeps its three `aco rule`/form outcomes once ruled, and BOARD-38's own sentence is never a button that would open a fresh line on a click.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`, a
tracked `.agent-claim/board.toml`, and `ACO_AGENT` set to `Ada`. A session
that reads GitHub issues also names a fixed, deterministic fake `gh` as a
setup precondition (the shape `specs/landing-grammar.spec.md` already
uses).

### E-BOARD-01 — an unsupported canonical remote refuses before any read

Setup: bare-remote, `origin` repointed at `file:///srv/git/agent-coordination.git`

```console
$ aco board --json
2> ERROR: no forge adapter for host file
{"ok": false, "reason": "unavailable", "message": "no forge adapter for host file"}
exit 2
```

### E-BOARD-03 — a kindless container's own `--json` item carries `"container": null`

Setup: bare-remote, fake `gh`, issue `#20` open with `children_closed`/
`children_total` set on the forge but no recognized `kind`

```console
$ aco board --json
{"ok": true, "reason": "projected", "items": [{"number": 20, "kind": null, "container": null, ...}], ...}
exit 0
```

### E-BOARD-04 — `--json`'s top-level keys, no `repository`

Setup: bare-remote, fake `gh`, one open issue `#10`

```console
$ aco board --json
{"ok": true, "reason": "projected", "items": [...], "ready_now": [...], "stale": [], "recovery": [], "landings": [], "uncut": [], "requests": 3, "measurements": {"classes": [], "unfinished": 0, "unparsed": 0, "since": null, "as_of": "<date>"}}
exit 0
```

### E-BOARD-15 — `--json` refusal envelope, `--repo` under `storage = state-ref`

Setup: bare-remote, `storage = "state-ref"` tracked

```console
$ aco --repo acme/items board --json
2> ERROR: --repo is meaningless under storage = state-ref
{"ok": false, "reason": "invalid_usage", "message": "--repo is meaningless under storage = state-ref"}
exit 2
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

### E-BOARD-17 — the page names the repository and the checkout it came from

Setup: bare-remote, fake `gh`, the checkout at `/home/ada/git/agent-coordination`

```console
$ aco board --html board.html
exit 0
$ head -1 board.html
<title>acme/agent-coordination &middot; /home/ada/git/agent-coordination &middot; Board</title>
exit 0
$ grep -o '<p class="eyebrow">[^<]*</p>' board.html
<p class="eyebrow">acme/agent-coordination &middot; /home/ada/git/agent-coordination</p>
exit 0
```

### E-BOARD-06 — `--html` and `--json` refuse together

Setup: bare-remote, fake `gh`

```console
$ aco board --html --json
2> ERROR: argument --json: not allowed with argument --html
{"ok": false, "reason": "invalid_usage", "message": "argument --json: not allowed with argument --html"}
exit 2
```

A pair without `--json` — `--html --serve` — prints no object at all, only
argparse's own usage block and `aco board: error: argument --serve: not
allowed with argument --html` on stderr, exit `2`.

### E-BOARD-07 — `--serve` prints one URL line

Setup: bare-remote, fake `gh`, `--port 0`

```console
$ aco board --serve
http://127.0.0.1:<port>/?t=<token>
```

The line above is the whole of this transcript: `--serve` then blocks in
its request loop, so no further line is printed until it is stopped.
What a request to that URL returns is BOARD-46..50's (see E-BOARD-18).

### E-BOARD-08 — a ruled line moves into its item's `Themen` entry

Setup: bare-remote, fake `gh`, issue `#10` open with one already-ruled `[[expectation]]` line (`ruling = "yes"`, `ruled_on = 2026-08-28`, `text = "Ship it?"`)

```console
$ aco board --html board.html
exit 0
$ grep -c '<div class="cards"><p class="empty">nichts</p></div>' board.html
1
$ grep -o 'class="ruled-state">[^<]*' board.html
class="ruled-state">ruled yes 2026-08-28
$ grep -o 'aco ask 10 --text "…"' board.html
aco ask 10 --text "…"
exit 0
```

### E-BOARD-09 — two starts on the same port print the identical URL; `--new-token` mints a different one

Setup: bare-remote, fake `gh`, an empty `${XDG_CONFIG_HOME}`, a free `<port>`

```console
$ aco board --serve --port <port>
http://127.0.0.1:<port>/?t=<token>
$ aco board --serve --port <port>
http://127.0.0.1:<port>/?t=<token>
$ aco board --serve --port <port> --new-token
http://127.0.0.1:<port>/?t=<other-token>
```

### E-BOARD-10 — a token file the operator left group/other-readable refuses

Setup: bare-remote, fake `gh`, `<token-path>` already minted, then `chmod 0644` by hand

```console
$ aco board --serve
2> ERROR: board token file <path> must be private (mode 0600, found 0644)
exit 2
```

### E-BOARD-11 — a port another process holds refuses, naming its PID

Setup: bare-remote, fake `gh`, a second process already bound and listening on loopback `<port>`

```console
$ aco board --serve --port <port>
2> ERROR: port <port> is already in use by PID <pid>
exit 2
```

### E-BOARD-12 — a group-writable token directory refuses, naming the mode

Setup: bare-remote, fake `gh`, `${XDG_CONFIG_HOME}/aco` already created, then `chmod 0770` by hand

```console
$ aco board --serve
2> ERROR: board token directory <path> must be private and owned by this user (found mode 0770)
exit 2
```

### E-BOARD-13 — a hand-edited token file refuses

Setup: bare-remote, fake `gh`, `<token-path>` (mode `0600`) overwritten with `not-a-token`

```console
$ aco board --serve
2> ERROR: board token at <path> is not a valid token; pass --new-token
exit 2
```

### E-BOARD-14 — `--new-token` without `--serve` refuses

Setup: bare-remote, fake `gh`

```console
$ aco board --new-token
2> ERROR: --new-token requires --serve
exit 2
```

### E-BOARD-16 — `aco board` naming no output mode refuses before any read

Setup: none -- a directory outside any git repository

```console
$ aco board
2> ERROR: aco board requires --json, --html, or --serve
exit 2
```

### E-BOARD-18 — the served page shows its age and a reload link

Setup: bare-remote, fake `gh`, a running `aco board --serve`; the page's
facts list, two minutes after the first request built it

```console
$ curl -s 'http://127.0.0.1:<port>/?t=<token>'
<div><dt>Stand</dt><dd>vor 0h 2m <a class="reload" href="/?t=<token>&amp;reload=1">neu laden</a></dd></div>
```

A request to the link's URL rebuilds the page, then answers `303` with
`Location: /?t=<token>` -- no `reload` field, so a later plain refresh of
that address does not rebuild -- and the redirected `GET` shows `vor 0h 0m`.
