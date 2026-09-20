# Body block

The `agent-claim` fenced TOML block inside a work item's body: the one grammar
`aco board`, `aco next`, issue-mode `aco claim`, `aco cut`, `aco rulings` and
`aco check` read a work item from. This file owns the block's shape, its
defect sentences, and the two verdicts a body can carry (`malformed`,
`incomplete`). The commands appear here only as far as they make the contract
visible; each command's own spec owns its flags and its other output and cites
these IDs instead of restating them.

Every defect sentence below is printed by `aco body --check` on stderr, one
line per defect, and by a reader as the item's own reason (BODY-50..BODY-52).
`<n>` is an item number, `<path>` a repository-relative path.

## Behavior table

| body state \ trigger | `aco body --check` | `aco board` | `aco claim <n>` |
|---|---|---|---|
| no `agent-claim` fence | BODY-01 | BODY-50 | BODY-52 |
| fence opened, never closed | BODY-02 | BODY-50 | BODY-52 |
| two `agent-claim` fences | BODY-03 | BODY-50 | BODY-52 |
| fence content is not TOML | BODY-04 | BODY-50 | BODY-52 |
| block quoted inside a documentation fence | BODY-05 | BODY-50 | BODY-52 |
| valid block, prose around it | BODY-06, BODY-07 | — | — |
| `version` missing or not `1` | BODY-08, BODY-09 | BODY-50 | BODY-52 |
| `now`/`next`/`done_when` missing or not a string | BODY-10, BODY-11 | BODY-50 | BODY-52 |
| every projection key present and empty | BODY-12 | BODY-51 | BODY-52 |
| unknown top-level key | BODY-13 | BODY-50 | BODY-52 |
| `[record]` under `storage = "github"` | BODY-15 | BODY-50 | BODY-52 |
| `[record]` under `storage = "state-ref"` | BODY-16..BODY-20 | — | — |
| `frozen_until` defective | BODY-21..BODY-24 | BODY-50 | BODY-52 |
| `[[expectation]]` defective | BODY-25..BODY-33 | BODY-50 | BODY-52 |
| card field defective | BODY-34, BODY-36..BODY-42 | BODY-50 | BODY-52 |
| `question` of exactly 160 characters | BODY-35 | — | — |
| `[[slice]]` defective | BODY-43..BODY-48 | BODY-50 | BODY-52 |
| `slice = []` | BODY-49 | — | — |
| `scope` defective | BODY-53..BODY-56 | BODY-50 | BODY-52 |
| `size` valid or defective | BODY-57..BODY-59 | BODY-50 (defective only) | BODY-52 (defective only) |
| `whole` valid or defective | BODY-60..BODY-62 | BODY-50 (defective only) | BODY-52 (defective only) |
| complete valid block | BODY-14 | — | — |

## Fence and surroundings

- [ ] [BODY-01] A body with no `agent-claim` fence makes `aco body --check` print `body malformed: agent-claim: no agent-claim block` on stderr, exit `1` — never a sentence about another grammar.
- [ ] [BODY-02] A body whose `agent-claim` fence is opened and never closed makes `aco body --check` print `body malformed: agent-claim: unclosed agent-claim block` on stderr, exit `1`.
- [ ] [BODY-03] A body with two `agent-claim` fences makes `aco body --check` print `body malformed: agent-claim: multiple agent-claim blocks; exactly one is allowed` on stderr, exit `1`.
- [ ] [BODY-04] A fence whose content is not TOML makes `aco body --check` print `body malformed: agent-claim: agent-claim block is not valid TOML: <reason>` on stderr, exit `1`.
- [ ] [BODY-05] An `agent-claim` fence quoted inside a longer documentation fence is not the item's block, so a body carrying only that one prints `body malformed: agent-claim: no agent-claim block`, exit `1`.
- [ ] [BODY-06] Prose beside the block — headings, a `Blocked by: nichts` line, another tool's own section — carries no contract, so a body of prose plus one complete block prints `body ok`, exit `0`.
- [ ] [BODY-07] A block whose lines end in CRLF is read like any other: a complete CRLF body prints `body ok`, exit `0`; a rewrite keeps CRLF but re-renders the block's fields canonically, not byte-for-byte.

## Projection keys

- [ ] [BODY-08] A block without `version` prints `body malformed: version: version is required and must be 1` on stderr, exit `1`.
- [ ] [BODY-09] A block whose `version` is any value but the integer `1` prints `body malformed: version: version must be exactly 1` on stderr, exit `1`.
- [ ] [BODY-10] A block missing `now`, `next` or `done_when` prints one `body malformed: <key>: <key> is required` line per missing key on stderr, exit `1`.
- [ ] [BODY-11] A block whose `now`, `next` or `done_when` is not a string prints `body malformed: <key>: <key> must be a string` on stderr, exit `1`.
- [ ] [BODY-12] A block with all three projection keys present and empty is valid but unfilled: `aco body --check` prints `body incomplete: Now, Next, Done when` on stderr, exit `1`.
- [ ] [BODY-13] A top-level key outside `version`, `now`, `next`, `done_when`, `frozen_until`, `scope`, `size`, `whole`, `expectation`, `slice` prints `body malformed: <key>: unknown top-level key <key>`, exit `1`.
- [ ] [BODY-14] A block whose three projection keys are all non-empty and whose optional tables are valid prints `body ok` on stdout, exit `0`, with nothing on stderr (see E-BODY-01).

## `[record]`, the state-ref item identity

- [ ] [BODY-15] Under `storage = "github"` a `[record]` table is an unknown key: `aco body --check` prints `body malformed: record: unknown top-level key record` on stderr, exit `1`.
- [ ] [BODY-16] Under `storage = "state-ref"` a `[record]` carrying `title`, `state`, `created_at`, `updated_at` is valid: `aco body --check` prints `body ok`, exit `0` (see E-BODY-05).
- [ ] [BODY-17] A `[record]` whose `state` is neither `open` nor `closed` prints `body malformed: record.state: record.state must be open or closed` on stderr, exit `1`.
- [ ] [BODY-18] A `[record]` with `state = "closed"` and no `closed_at` prints `body malformed: record.closed_at: record.closed_at is required when record.state is closed`, exit `1`.
- [ ] [BODY-19] A `[record]` timestamp that is not an RFC 3339 UTC instant prints `body malformed: record.created_at: record.created_at must be an RFC 3339 UTC timestamp`, exit `1`.
- [ ] [BODY-20] A `[record]` key outside the ten allowed keys prints `body malformed: record.<key>: unknown key record.<key>`, exit `1`.

## `frozen_until`

- [ ] [BODY-21] A `frozen_until` that is not a table prints `body malformed: frozen_until.trigger: frozen_until must be a table with trigger and ruled_on` on stderr, exit `1`.
- [ ] [BODY-22] A `frozen_until` whose `trigger` is missing, blank or not a string prints `body malformed: frozen_until.trigger: frozen_until.trigger must be a non-empty string`, exit `1`.
- [ ] [BODY-23] A `frozen_until` whose `ruled_on` is not a TOML local date prints `body malformed: frozen_until.ruled_on: frozen_until.ruled_on must be a TOML local date`, exit `1`.
- [ ] [BODY-24] A `frozen_until` key outside `trigger` and `ruled_on` prints `body malformed: frozen_until.<key>: unknown key frozen_until.<key>` on stderr, exit `1`.

## `[[expectation]]`

- [ ] [BODY-25] An `expectation` key that is not an array of tables prints `body malformed: expectation: expectation must be an array of tables` on stderr, exit `1`.
- [ ] [BODY-26] An `[[expectation]]` entry that is not a table prints `body malformed: expectation[0]: expectation[0] must be a table` on stderr, exit `1`.
- [ ] [BODY-27] An entry whose `text` is missing, blank or not a string prints `body malformed: expectation[0].text: expectation[0].text must be a non-empty string`, exit `1`.
- [ ] [BODY-28] An entry carrying both `default` and a ruling prints `body malformed: expectation[0].default: expectation[0] must be proposed (default) or ruled (ruling, ruled_on), not both` on stderr, exit `1`.
- [ ] [BODY-29] An entry carrying neither prints `body malformed: expectation[0].default: expectation[0] must carry default, or both ruling and ruled_on`, exit `1`.
- [ ] [BODY-30] An entry whose `default` is not `yes`, `no` or `later` prints `body malformed: expectation[0].default: expectation[0].default must be yes, no, or later`, exit `1`.
- [ ] [BODY-31] An entry whose `ruling` is not `yes`, `no` or `later` prints `body malformed: expectation[0].ruling: expectation[0].ruling must be yes, no, or later`, exit `1`.
- [ ] [BODY-32] A ruled entry whose `ruled_on` is not a TOML local date prints `body malformed: expectation[0].ruled_on: expectation[0].ruled_on must be a TOML local date`, exit `1`.
- [ ] [BODY-33] An entry key outside `text`, `default`, `ruling`, `ruled_on`, `question`, `example`, `picture` prints `body malformed: expectation[0].<key>: unknown key expectation[0].<key>` on stderr, exit `1`.

## The card fields `question`, `example`, `picture`

- [ ] [BODY-34] A `question` longer than 160 characters prints `body malformed: expectation[0].question: expectation[0].question must be at most 160 characters` on stderr, exit `1`.
- [ ] [BODY-35] A `question` of exactly 160 characters is inside the bound: `aco body --check` prints `body ok` on stdout, exit `0`.
- [ ] [BODY-36] An `example` that is blank or not a string prints `body malformed: expectation[0].example: expectation[0].example must be a non-empty string` on stderr, exit `1`.
- [ ] [BODY-37] A `picture` whose text does not start with `<svg` prints `body malformed: expectation[0].picture: expectation[0].picture must be inline SVG rooted at <svg>`, exit `1`.
- [ ] [BODY-38] A `picture` over 8192 bytes prints `body malformed: expectation[0].picture: expectation[0].picture must be at most 8192 bytes` on stderr, exit `1`.
- [ ] [BODY-39] A `picture` containing `<script` in any casing prints `body malformed: expectation[0].picture: expectation[0].picture must not contain <script>`, exit `1`.
- [ ] [BODY-40] A `picture` carrying an event-handler attribute prints `body malformed: expectation[0].picture: expectation[0].picture must not contain an event-handler attribute`, exit `1`.
- [ ] [BODY-41] A `picture` whose `href` or `xlink:href` does not start with `#` prints `body malformed: expectation[0].picture: expectation[0].picture must not reference an href outside the document`, exit `1`.
- [ ] [BODY-42] A `picture` matching two refused contents names the earlier one in the order `<script>, <foreignObject>, on…=, javascript:, data:, href, animated href, url(), <iframe>, <embed>, <object>, srcdoc`.

## `[[slice]]`

- [ ] [BODY-43] A `slice` key that is not an array of tables prints `body malformed: slice: slice must be an array of tables` on stderr, exit `1`.
- [ ] [BODY-44] A `[[slice]]` entry that is not a table prints `body malformed: slice[0]: slice[0] must be a table` on stderr, exit `1`.
- [ ] [BODY-45] An entry whose `index` is missing, zero, negative or not an integer prints `body malformed: slice[0].index: slice[0].index must be a positive integer`, exit `1`.
- [ ] [BODY-46] A second entry repeating an earlier `index` prints `body malformed: slice[1].index: slice[1].index duplicates slice index 4` on stderr, exit `1`.
- [ ] [BODY-47] An entry whose `title` is missing, blank or not a string prints `body malformed: slice[0].title: slice[0].title must be a non-empty string`, exit `1`.
- [ ] [BODY-48] An entry key outside `index`, `title` and `scope` prints `body malformed: slice[0].<key>: unknown key slice[0].<key>`, exit `1`; per-slice done-when and dependencies stay in the prose.
- [ ] [BODY-49] A block carrying `slice = []` is valid with nothing left to cut: `aco body --check` prints `body ok`, exit `0`, and the empty table stays present in the body.

## What a reader does with a defect

- [ ] [BODY-50] An item whose body carries any defect above is named by `aco board` with its first defect sentence as its reason, `body malformed: <field>: <message>`, and is never proposed as a cut or a close.
- [ ] [BODY-51] An item whose block is valid but whose projection keys are empty is skipped by `aco next`, which names it `SKIPPED` with `body incomplete: Now, Next, Done when`.
- [ ] [BODY-52] Issue-mode `aco claim <n>` against such an item refuses before any write with the same sentence as a `body-contract` check, exit `2` (see E-BODY-03).

## `scope`, the item's own files

- [ ] [BODY-53] A block carrying `scope = ["README.md", "docs"]` is valid, and a `[[slice]]` row may carry its own `scope = ["docs"]` under the same grammar: `aco body --check` prints `body ok`, exit `0`.
- [ ] [BODY-54] `scope = []` prints `body malformed: scope: scope must name at least one path`, exit `1`; an empty `[[slice]]` row `scope` prints the `slice[0].scope` form. Absent `scope` means unknown, never empty.
- [ ] [BODY-55] A `scope = ["/etc/passwd"]` entry fails the same way `claim --scope` would: `body malformed: scope: claim scope must be repository-relative: '/etc/passwd'`.
- [ ] [BODY-56] A valid `scope` renders sorted, deduplicated, ahead of the first `[[expectation]]`/`[[slice]]` table — else TOML binds a bare key to the prior table — so a re-read names the same canonical `scope`.

## `size`, the item's own estimate class

- [ ] [BODY-57] A block with no `size` key is valid: absence means "no estimate", never a default class, and `aco body --check` prints `body ok`, exit `0`.
- [ ] [BODY-58] `size = "S"`, `"M"`, or `"L"` is valid: `aco body --check` prints `body ok`, exit `0`.
- [ ] [BODY-59] A `size` outside `S`/`M`/`L` prints `body malformed: size: size must be S, M, or L`, exit `1`, the same sentence for every invalid shape.

  ```
  size = "XL"    # not one of S, M, L
  size = ["S"]   # a list, not a scalar
  size = {}      # a table, not a scalar
  ```

## `whole`, the item's own wide-scope justification

- [ ] [BODY-60] A block with no `whole` key is valid: absence means no stored reason, never a default one, and `aco body --check` prints `body ok`, exit `0`.
- [ ] [BODY-61] `whole = "<reason>"` with any non-blank text is valid: `aco body --check` prints `body ok`, exit `0`; `specs/claim.spec.md` (CLM-21) owns what `claim`/`start` do with it.
- [ ] [BODY-62] A blank or non-string `whole` prints `body malformed: whole: whole must be a non-empty string`, exit `1`, the same sentence for every invalid shape.

  ```
  whole = "   "  # blank after trimming
  whole = 1      # not a string
  ```

## Never

- `aco body --check` never reads a file path, a live item, a dependency, the forge, or the state ref; it reads stdin and the repository's own storage pin only.
- No reader ever derives a blocker or a parent from the body: `Blocked by:` prose beside the block is documentation, and the `Parent: #<n>` line is read back by `aco cut`'s own orphan adoption alone.
- A rewrite of one block field never changes a byte outside the fence, but it does re-render the whole block interior canonically: schema key order, TOML-safe quoting, the fence's own newline convention.
- No reader guesses through a defect: a malformed body is refused by name, never treated as an empty or partial block.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local bare
repository with `main` at one commit, a git identity, `origin/HEAD`, and
`ACO_AGENT` set; `<remote>`, `<tmp>` and `<home>` are the runner's own paths.
Sessions whose stdin carries a fenced block use a four-backtick console fence.

### E-BODY-01 — the golden body, complete and valid

Setup: bare-remote, `storage = "github"` in a tracked `.agent-claim/board.toml`

````console
$ aco body --check <<'BODY'
Prose a reader may write freely.

```agent-claim
version = 1
now = "Cut on 19.09.2026 from #320."
next = "Build the two contract specs."
done_when = "Both spec files exist and the lint probe is named."
```
BODY
body ok
exit 0
````

### E-BODY-02 — an unfilled skeleton is incomplete, not malformed

Setup: bare-remote, `storage = "github"`

````console
$ aco body --check <<'BODY'
```agent-claim
version = 1
now = ""
next = ""
done_when = ""
```
BODY
2> body incomplete: Now, Next, Done when
exit 1
````

### E-BODY-03 — a body with no block, and a claim against that item

Setup: bare-remote, `storage = "github"`, issue `#42` carrying that same body

````console
$ aco body --check <<'BODY'
## Ziel
Prose only, no typed block.
BODY
2> body malformed: agent-claim: no agent-claim block
exit 1
$ aco claim 42 --scope README.md
2> ERROR: body malformed: agent-claim: no agent-claim block
exit 2
````

### E-BODY-04 — every defect at once, as an object

Setup: bare-remote, `storage = "github"`

````console
$ aco body --check --json <<'BODY'
```agent-claim
version = 2
now = "Something"
next = "Something"
done_when = "Something"
unknown_key = 1
```
BODY
{"ok": false, "defects": ["body malformed: version: version must be exactly 1", "body malformed: unknown_key: unknown top-level key unknown_key"]}
exit 1
````

### E-BODY-05 — `[record]` is known only under the state-ref pin

Setup: bare-remote, `storage = "state-ref"` in a tracked `.agent-claim/board.toml`

````console
$ aco body --check <<'BODY'
```agent-claim
version = 1
now = "Open."
next = "Build it."
done_when = "It is built."

[record]
title = "Body block contract"
state = "open"
labels = []
blocked_by = []
created_at = "2026-09-19T08:00:00Z"
updated_at = "2026-09-19T08:00:00Z"
```
BODY
body ok
exit 0
````
