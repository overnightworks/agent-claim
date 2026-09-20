# `aco rule`

`aco rule ITEM --line N (--yes|--no|--later) [--note TEXT]`: moves one
proposed `[[expectation]]` line to ruled, transcribing the operator's own
word (`aco ask` proposes; this command decides). This command and
`board --serve`'s own `POST /rule` form (issue #280) share the one write
path; a refusal reads the identical sentence through either caller. This
file owns the ruling's own refusals -- an already-ruled
line, an out-of-range one -- the `RULED` line, its own `reason` vocabulary,
and every refusal `aco ask` shares only by substituting `command` ("rule"
where ask's own reads "ask"): a missing item, a pull-request target, a
forge that cannot write the body. `specs/output.spec.md` owns the `--json`
envelope itself (OUT-nn: key order, `ok`, `message`); this file names only
`rule`'s own `reason` values. `specs/ask.spec.md` owns the
malformed-body-target refusal the two commands share verbatim (ASK-05);
this file cites it rather than restating it. `<n>` is the item's own
number; `<k>` the 1-based line index `--line` names, the same index
`aco ask`'s own `ASKED` line and `rulings` print.

## Behavior table

| state \ trigger | `aco rule ITEM --line N OUTCOME` | `--json` | `--note TEXT` |
|---|---|---|---|
| a still-open line | RULE-01 | RULE-02 | RULE-03 |
| that line already ruled | RULE-04 | RULE-09 | — |
| `N` out of range | RULE-05 | RULE-09 | — |
| this forge cannot write the body | RULE-06 | RULE-09 | — |
| item does not exist | RULE-07 | RULE-09 | — |
| item is a pull request | RULE-08 | RULE-09 | — |
| item body malformed | ASK-05 (cited) | RULE-09 | — |

## Ruling a line

- [ ] [RULE-01] A still-open line makes `aco rule ITEM --line N` with `--yes`/`--no`/`--later` print `RULED #<n> line <k> <ruling>; <m> line(s) still open` on stdout, exit `0` (see E-RULE-01).
- [ ] [RULE-02] `--json` on the same call prints `specs/output.spec.md`'s envelope with `reason: "ruled"`, then `"item": <n>, "index": <k>, "ruling": "<ruling>", "ruled_on": "<date>", "open": <m>`.
- [ ] [RULE-03] `--note TEXT` appends ` Anmerkung: TEXT` to the ruled line's own `text`, never a separate field (see E-RULE-02).

## Refusals this command owns

`aco ask`'s equivalent of RULE-06/RULE-08 substitutes `ask` for `rule` in
the same sentence; RULE-07's sentence is identical for both commands.

- [ ] [RULE-04] `--line N` naming an already-ruled entry refuses `line <k> is already ruled; a changed ruling is a new line`, exit `2`, before any write (see E-RULE-03).
- [ ] [RULE-05] `--line N` outside the item's own expectation lines refuses `line <k> out of range: this item has <m> expectation line(s)`, exit `2`, before any write (see E-RULE-04).
- [ ] [RULE-06] A forge whose `update_item_body` capability is not `READ_WRITE` refuses `this forge cannot update_item_body; rule by hand`, exit `2`, before any write.
- [ ] [RULE-07] An item no reference resolves refuses `#<n> does not exist`, exit `2`, before any write.
- [ ] [RULE-08] An item that is a pull request refuses `#<n> is a pull request, not an issue; rule needs an issue`, exit `2`, before any write.
- [ ] [RULE-09] `--json` on a dispatched refusal (RULE-04..RULE-08, ASK-05) prints `specs/output.spec.md`'s envelope with the sentence as `message` and `reason` from the table below, exit `2` (see E-RULE-05).

`reason`, by which refusal fired:

| refusal | `reason` |
|---|---|
| RULE-04 (already ruled) | `already_ruled` |
| RULE-05 (out of range) | `line_out_of_range` |
| RULE-07 (missing item), RULE-08 (pull-request target), ASK-05 (malformed body, cited) | `invalid_item` |
| RULE-06 (forge cannot write) | `unavailable` |

## Never

- `aco rule` never accepts two outcome flags, or none: `--yes`/`--no`/`--later` are one mutually exclusive, required group.
- `aco rule` never changes a ruled line's own `ruling`: RULE-04 refuses before any write reaches it.
- `aco rule` never rewrites a byte outside the ruled entry: the body's surrounding bytes stay exactly as written.
- The `RULED` line is always the bare `#<n>`, never the storage-aware `<label>` form `specs/landing-grammar.spec.md` defines for `aco next`/`release`'s own narrative lines.
- `aco rule` reaches the same checkout-less refusal `specs/check.spec.md` owns (CHECK-10) before it ever resolves the forge or the item.
- `board --serve`'s own `POST /rule` form is transport only: it calls this exact write, never a second one, so a line already ruled through the served board refuses the same RULE-04 sentence a CLI `aco rule` would.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`,
and `ACO_AGENT` set to `Ada`; `<item-id>` is the state-ref id a session
itself minted, `<n>` that same item's own bare number, `<ruled-on>` the
runner's own today's date. Writing to a GitHub-pinned item instead needs a
fixed, deterministic fake `gh` (`specs/landing-grammar.spec.md`'s own
shape); these sessions use `storage = "state-ref"` instead, needing
neither.

### E-RULE-01 — ruling a still-open line, text and `--json`

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `<item-id>` open with two proposed lines (`aco ask <item-id> --text "Ship it?"`, `aco ask <item-id> --text "Ship it too?"`)

```console
$ aco rule <item-id> --line 1 --yes
RULED #<n> line 1 yes; 1 line(s) still open
exit 0
$ aco rule <item-id> --line 2 --later --json
{"ok": true, "reason": "ruled", "item": <n>, "index": 2, "ruling": "later", "ruled_on": "<ruled-on>", "open": 0}
exit 0
```

### E-RULE-02 — a note appended to the ruled line

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `<item-id>` open with one proposed line, text `Ship it?`

```console
$ aco rule <item-id> --line 1 --yes --note "Ja, sofort."
RULED #<n> line 1 yes; 0 line(s) still open
exit 0
```

### E-RULE-03 — an already-ruled line refuses before any write

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `<item-id>` open with one already-ruled line, `--line 1`

```console
$ aco rule <item-id> --line 1 --no
2> ERROR: line 1 is already ruled; a changed ruling is a new line
exit 2
```

### E-RULE-04 — an out-of-range line refuses before any write

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `<item-id>` open with one proposed line

```console
$ aco rule <item-id> --line 2 --yes
2> ERROR: line 2 out of range: this item has 1 expectation line(s)
exit 2
```

### E-RULE-05 — a refusal's own `--json` envelope

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `<item-id>` open with one already-ruled line, `--line 1`

```console
$ aco rule <item-id> --line 1 --no --json
2> ERROR: line 1 is already ruled; a changed ruling is a new line
{"ok": false, "reason": "already_ruled", "message": "line 1 is already ruled; a changed ruling is a new line"}
exit 2
```
