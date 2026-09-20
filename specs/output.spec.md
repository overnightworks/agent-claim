# The `--json` envelope

Every migrated command's `--json` object is built and printed through one
shared, nameless envelope (issue #396). This file owns the envelope's own
shape -- key order, when `ok` is `true`, and what `reason` and `message`
may and may not carry -- and applies to every command whose own spec cites
`OUT-nn`; `ask`, `rule`, and `brief` are its first three
(`specs/ask.spec.md`, `specs/rule.spec.md`, `specs/brief.spec.md`), each
naming its own `reason` vocabulary with examples. A command whose own spec
does not cite this file still prints `specs/release.spec.md`'s REL-24
shape until its own migration lands.

## Behavior table

| state \ trigger | `--json` |
|---|---|
| a migrated command's own success | OUT-01, OUT-02 |
| a migrated command's own refusal | OUT-01, OUT-03 |
| a command whose spec does not cite this file | OUT-04 |

## The envelope

- [ ] [OUT-01] Every migrated command's `--json` object prints `ok` first and `reason` second, in that order, before any of the command's own payload keys.
- [ ] [OUT-02] `ok` is `true` only for that command's own success outcome; `reason` is always one stable token from that command's own enum, documented by its own spec, never a free sentence.
- [ ] [OUT-03] A refusal's `reason` names its enum member, never an `error` object; an optional `message` -- the sentence stderr's `ERROR:` line already printed -- is always the envelope's last key, when given.
- [ ] [OUT-04] A command whose own spec does not cite `OUT-nn` keeps `specs/release.spec.md`'s REL-24 `{"ok": false, "error": "<sentence>"}` shape until its own migration cites this file instead.

## Never

- This file never lists a command's own vocabulary: `ask`, `rule`, and `brief` each document their own `reason` members, with examples, in their own spec.
- `ok`/`reason` never reorder around a command's payload: `reason` is always the second key, never last, never interleaved with structured detail keys.
- `message` never carries structured data: every structured detail (`item`, `index`, `claim`, `checks`, and the like) is its own sibling key, never packed into the prose.
- `protect` never joins this envelope, migrated or not: its hook protocol (`{"decision": …}`, exit `0`/`2`) is a permanent exception (`specs/protect.spec.md`), unlike OUT-04's not-yet-migrated commands.
- `board --serve` never prints this file's own `--json` envelope either: its own request/response wire contract is permanently `specs/board.spec.md`'s own, not this file's.
- `bootstrap`, `reset`, `start`, `body --template`, `register`, `run`, and `login` never gain a `--json` mode of their own (each command's own product decision, not a pending migration): each names it in its own `## Never` (`specs/bootstrap.spec.md`, `specs/reset.spec.md`, `specs/start.spec.md`, `specs/body.spec.md`, `specs/workspace.spec.md`).

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`,
and `ACO_AGENT` set to `Ada`; `<item-id>` is the state-ref id a session
itself minted, `<n>` that same item's own bare number.

### E-OUT-01 -- a success envelope, `ok`/`reason` first

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `<item-id>` open (`aco item new --title "Decide something"`)

```console
$ aco ask <item-id> --text "New question?" --json
{"ok": true, "reason": "asked", "item": <n>, "index": 1, "text": "New question?", "default": "yes"}
exit 0
```

### E-OUT-02 -- a refusal envelope, `reason` and `message`

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `items/aco-000001.md` hand-written with no `agent-claim` block

```console
$ aco ask aco-000001 --text "New question?" --json
2> ERROR: #<n> body malformed: agent-claim: no agent-claim block; ask needs a valid agent-claim block
{"ok": false, "reason": "invalid_item", "message": "#<n> body malformed: agent-claim: no agent-claim block; ask needs a valid agent-claim block"}
exit 2
```
