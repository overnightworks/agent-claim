# `aco ask`

`aco ask ITEM --text TEXT`: appends one fresh proposed `[[expectation]]`
line to an item's `agent-claim` block; `--question`/`--example`/`--picture
FILE.svg` (issue #295) attach the card fields a picture-owner's mockup
shows instead of the bare `text`. This file owns the command's own argument
shape, its `ASKED` line, its own `reason` vocabulary, and the two refusals
unique to appending a card (an unreadable or refused `--picture` file),
plus the malformed-body refusal `aco rule` shares verbatim.
`specs/output.spec.md` owns the `--json` envelope itself (OUT-nn: key
order, `ok`, `message`); this file names only `ask`'s own `reason` values.
`specs/rule.spec.md` owns every refusal the two commands share only by
substituting `command` ("ask" where rule reads "rule"): a missing item, a
pull-request target, a forge that cannot write the body
(RULE-06..RULE-08); this file cites those IDs rather than restating them.
`specs/body-block.spec.md` owns the picture's own content grammar
field-by-field (BODY-37..BODY-42) and the question/example length and
non-empty rules (BODY-34..BODY-36): appending a card checks each field by
the same rule the body parser applies. `<n>` is the item's own number,
always the argument given; `<k>` the fresh line's 1-based index.

## Behavior table

| state \ trigger | `aco ask ITEM --text TEXT` | `--json` | `--question`/`--example`/`--picture` |
|---|---|---|---|
| valid item body | ASK-01 | ASK-02 | ASK-03, ASK-04 |
| item body malformed | ASK-05 | ASK-09 | — |
| `--picture FILE.svg` unreadable | ASK-06 | ASK-09 | — |
| `--picture` content refused | ASK-07 | ASK-09 | — |
| `--text` blank or all whitespace | ASK-08 | ASK-09 | — |
| item is a pull request | RULE-08 (cited) | ASK-09 | — |
| item does not exist | RULE-07 (cited) | ASK-09 | — |
| this forge cannot write the body | RULE-06 (cited) | ASK-09 | — |

## Appending a proposed line

- [ ] [ASK-01] A valid item body makes `aco ask ITEM --text TEXT` append a fresh proposed `[[expectation]]` line and print `ASKED #<n> line <k>: <text>` on stdout, exit `0` (see E-ASK-01).
- [ ] [ASK-02] `--json` prints `specs/output.spec.md`'s envelope, `reason: "asked"`, then `"item": <n>, "index": <k>, "text": "<text>", "default": "<default>"` (`yes` unless `--default` given).
- [ ] [ASK-03] `--question`/`--example`/`--picture FILE.svg` attach to the fresh entry, but the `ASKED` line itself never changes: it still names `TEXT` alone, never the question or example (see E-ASK-02).
- [ ] [ASK-04] `--json` on that same call adds `"question"`/`"example"`/`"picture"` keys, one per flag actually given, each the exact string written.

## Refusals this command owns

`<body defect sentence>` is `specs/body-block.spec.md`'s own first-defect
text (BODY-01..BODY-50); a picture's own content rules are BODY-37..BODY-42.

- [ ] [ASK-05] A MALFORMED item body refuses `#<n> <body defect sentence>; ask needs a valid agent-claim block`, exit `2`, before any write (see E-ASK-03).
- [ ] [ASK-06] `--picture FILE.svg` naming an unreadable file refuses `--picture <path> could not be read: <error>`, exit `2`, before the forge or the item body are touched (see E-ASK-04).
- [ ] [ASK-07] A `--picture` failing a BODY-37..42 rule refuses `picture <reason>` (no `expectation[0].` prefix), exit `2`, before any write; `<reason>` is the first-matching row below.
- [ ] [ASK-08] `--text` that is blank or all whitespace refuses `expectation text must be a non-empty string`, exit `2`, before any write.
- [ ] [ASK-09] `--json` on a dispatched refusal (ASK-05..ASK-08, RULE-06..RULE-08) prints `specs/output.spec.md`'s envelope with the sentence as `message` and `reason` from the table below, exit `2` (see E-ASK-05).

`reason`, by which refusal fired:

| refusal | `reason` |
|---|---|
| ASK-05 (malformed body), RULE-07 (missing item, cited), RULE-08 (pull-request target, cited) | `invalid_item` |
| ASK-06 (`--picture` unreadable), ASK-07 (`--picture` content refused) | `invalid_picture` |
| ASK-08 (blank `--text`) | `invalid_expectation` |
| RULE-06 (forge cannot write, cited) | `unavailable` |

`<reason>`, checked in this fixed order:

| `--picture` content | `<reason>` |
|---|---|
| not a string | `must be a string` |
| over 8192 bytes | `must be at most 8192 bytes` |
| not rooted at `<svg` | `must be inline SVG rooted at <svg>` |
| contains `<script` | `must not contain <script>` |
| contains `<foreignObject` | `must not contain <foreignObject>` |
| carries an event-handler attribute | `must not contain an event-handler attribute` |
| contains `javascript:` | `must not contain a javascript: reference` |
| contains `data:` | `must not contain a data: reference` |
| references an `href`/`xlink:href` outside the document | `must not reference an href outside the document` |
| animates `href` to an external target | `must not animate href to an external target` |
| contains `url(` | `must not contain a url() reference` |
| contains `<iframe` | `must not contain <iframe>` |
| contains `<embed` | `must not contain <embed>` |
| contains `<object` | `must not contain <object>` |
| contains `srcdoc` | `must not contain srcdoc` |

## Never

- `aco ask` never rules a line: `--default` only proposes an outcome; only `aco rule` moves a line from proposed to ruled.
- `aco ask` never overwrites an existing line: every call appends a fresh entry at the next index, even when an identical `text` already exists.
- `aco ask` never rewrites a byte outside the appended entry: the body's surrounding bytes stay exactly as written.
- The `ASKED` line is always the bare `#<n>`, never the storage-aware `<label>` form `specs/landing-grammar.spec.md` defines for `aco next`/`release`'s own narrative lines.
- `aco ask` reaches the same checkout-less refusal `specs/check.spec.md` owns (CHECK-10) before it ever resolves the forge or the item.
- `--picture`'s own file read (ASK-06) never depends on the item, the forge, or a checkout: a missing checkout still lets a missing or invalid picture file refuse first.
- `--default` can never print the "must be one of yes, no, or later" refusal through this CLI: only `yes`, `no`, or `later` ever reach the command; any other value is refused by the parser itself.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`,
and `ACO_AGENT` set to `Ada`; `<item-id>` is the state-ref id a session
itself minted, `<n>` that same item's own bare number, the runner's own
value. Writing to a GitHub-pinned item instead needs a fixed, deterministic
fake `gh` (`specs/landing-grammar.spec.md`'s own shape); these sessions
use `storage = "state-ref"` instead, needing neither.

### E-ASK-01 — a fresh proposed line, text and `--json`

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `<item-id>` open (`aco item new --title "Decide something"`)

```console
$ aco ask <item-id> --text "New question?"
ASKED #<n> line 1: New question?
exit 0
$ aco ask <item-id> --text "Ship on Friday?" --json
{"ok": true, "reason": "asked", "item": <n>, "index": 2, "text": "Ship on Friday?", "default": "yes"}
exit 0
```

### E-ASK-02 — the card fields: question, example, and an inline picture

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `<item-id>` open

```console
$ cat > sketch.svg <<'SVG'
<svg xmlns='http://www.w3.org/2000/svg'><circle cx='5' cy='5' r='4'/></svg>
SVG
$ aco ask <item-id> --text "New question?" --question "Ship it?" --example "Release on Friday." --picture sketch.svg --json
{"ok": true, "reason": "asked", "item": <n>, "index": 1, "text": "New question?", "default": "yes", "question": "Ship it?", "example": "Release on Friday.", "picture": "<svg xmlns='http://www.w3.org/2000/svg'><circle cx='5' cy='5' r='4'/></svg>"}
exit 0
```

### E-ASK-03 — a malformed body refuses before any write

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `items/aco-000001.md` hand-written with no `agent-claim` block

```console
$ aco ask aco-000001 --text "New question?"
2> ERROR: #<n> body malformed: agent-claim: no agent-claim block; ask needs a valid agent-claim block
exit 2
```

### E-ASK-04 — a picture file that cannot be read

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `<item-id>` open, `missing.svg` does not exist

```console
$ aco ask <item-id> --text "New question?" --picture missing.svg
2> ERROR: --picture missing.svg could not be read: <error>
exit 2
```

### E-ASK-05 — a refusal's own `--json` envelope

Setup: bare-remote, bootstrapped, `storage = "state-ref"` tracked, `items/aco-000001.md` hand-written with no `agent-claim` block

```console
$ aco ask aco-000001 --text "New question?" --json
2> ERROR: #<n> body malformed: agent-claim: no agent-claim block; ask needs a valid agent-claim block
{"ok": false, "reason": "invalid_item", "message": "#<n> body malformed: agent-claim: no agent-claim block; ask needs a valid agent-claim block"}
exit 2
```
