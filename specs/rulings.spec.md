# `aco rulings`

`aco rulings` lists every open board item that still carries an open
`[[expectation]]` line, read-only. This file owns its row and per-line
text, its ordering, its `--json` shape, and its empty-board sentence. It
cites the forge-resolution precondition `specs/board.spec.md` owns
(BOARD-01/BOARD-02) rather than restating it, and the `RULE-01`/`ASK-01`
write paths that fill the lines this command only reads. `<n>` is an
item number, `<label>` an item as `specs/landing-grammar.spec.md` prints
it, `<k>` an expectation line's 1-based index.

## Behavior table

| state \ trigger | text | `--json` |
|---|---|---|
| unsupported forge host / untracked pin | BOARD-01/02 (cited) | BOARD-01/02 (cited) |
| an item with an open expectation line | RUL-01, RUL-02 | RUL-05 |
| several such items | RUL-03 | RUL-05 |
| an item whose every line is already ruled | RUL-04 | RUL-04 |
| a ruled line | RUL-02 | RUL-05 |
| a line's `question`/`example`/`picture` | — | RUL-06 |
| no open expectation line on the whole board | RUL-07 | RUL-08 |

## Rows and lines

- [ ] [RUL-01] Each open-line item prints one header, `<label> <open>/<total>: <title>`, then one line per expectation entry -- ruled ones too -- in block order (see E-RUL-01).
- [ ] [RUL-02] A still-open line prints `  <k> open: <summary>`; a ruled one prints `  <k> ruled <outcome> <date>: <summary>`, `<summary>` capped at 100 characters total, its last char `…` when cut (see E-RUL-01).
- [ ] [RUL-03] Rows are ordered by priority category and score first, then fewer open lines, then ascending `<n>` -- never `board --json`'s own raw `items` order (see E-RUL-02).
- [ ] [RUL-04] An item whose every expectation line is already ruled is left off the list entirely, in both text and `--json` (see E-RUL-02).

## `--json`

- [ ] [RUL-05] Each row is `{"number", "title", "open", "total", "lines"}`; each line is `{"index", "text", "state"}`, `state` `"open"` or `"ruled <outcome> <date>"`, text untruncated (see E-RUL-03).
- [ ] [RUL-06] A line's `question`/`example`/`picture` (`aco ask`, ASK-03) each add their own key, present only when that field was given.

## Empty board

- [ ] [RUL-07] With no open expectation line anywhere, text output is exactly `No open expectation lines.`, exit `0` (see E-RUL-04).
- [ ] [RUL-08] The same board's `--json` prints `[]`, exit `0` (see E-RUL-04).

## Never

- `aco rulings` never writes: it reads the same open-board projection `board`/`next` already build, and nothing else.
- `aco rulings` never re-scans an item's stale prose for its progress counts: a `## Erwartungen` heading left beside the block plays no part -- only the block's own `[[expectation]]` entries are read.
- A row never shows an item with zero open lines, even briefly, however many total lines its block carries.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`, a
tracked `.agent-claim/board.toml`, and `ACO_AGENT` set to `Ada`. A session
reading GitHub issues also names a fixed, deterministic fake `gh` as a
setup precondition (the shape `specs/landing-grammar.spec.md` already
uses).

### E-RUL-01 — one item, an open line and a ruled line

Setup: bare-remote, fake `gh`, issue `#10` open, one open and one ruled `[[expectation]]` line

```console
$ aco rulings
#10 1/2: Open expectation
  1 open: Open decision 0.
  2 ruled yes 2026-09-19: Settled decision 0.
exit 0
```

### E-RUL-02 — ranked rows, a fully-ruled item left off the list

Setup: bare-remote, fake `gh`, issue `#50` in-flight with two open lines, issue `#60` a lower-priority item with one open line, issue `#70` fully ruled

```console
$ aco rulings
#50 2/3: In-flight security work
  1 open: Name it.
  2 open: Name it too.
  3 ruled yes 2026-09-19: Settled.
#60 1/1: Lower-priority product work
  1 open: Ship it?
exit 0
```

### E-RUL-03 — `--json`

Setup: bare-remote, fake `gh`, issue `#10` as in E-RUL-01

```console
$ aco rulings --json
[{"number": 10, "title": "Open expectation", "open": 1, "total": 2, "lines": [{"index": 1, "text": "Open decision 0.", "state": "open"}, {"index": 2, "text": "Settled decision 0.", "state": "ruled yes 2026-09-19"}]}]
exit 0
```

### E-RUL-04 — no open board item carries an open line

Setup: bare-remote, fake `gh`, every open issue's expectations fully ruled (or none at all)

```console
$ aco rulings
No open expectation lines.
exit 0
$ aco rulings --json
[]
exit 0
```
