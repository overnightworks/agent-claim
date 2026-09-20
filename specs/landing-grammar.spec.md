# Landing grammar

What a landing is, and the two related classification grammars every reader
tells it from: a merge or squash commit's own trailer block on the trunk,
and the `Work-Item:`/`No-Item:` line in a pull request body before it
merges -- the same two keys, but not the same value grammar (`## The trunk's
own trailer block` vs. `## The pull request body's own grammar`, below).
This file owns both grammars, `aco check <pr>`'s classification, the
parent-closing rule, what `aco release --merged` verifies, and the
"Landungen" landing view `aco board`/`aco board --html` derive from them.
Each command's own spec (none exist yet for `check`/`release`/`board`) would
cite these IDs rather than restate them. `aco check <sha>` and, under
`storage = "state-ref"`, `release --merged <sha|empty>` (issue #359) read
the trunk's own trailer block directly and need no forge at all: a commit's
trailer is local history, unlike a pull request's classification. Under
`storage = "github"`, `release --merged <pr>` (issue #397) reads it too,
once it has the merge commit's own sha from the forge: the trunk's trailer
block is that release's authority for which item it closes, never the pull
request's own -- separately mutable -- body.

`<n>` is a bare issue or pull request number, always printed `#<n>`. `<item>`
and `<ref>` are a parsed `Work-Item:` value or closing reference, always
printed fully qualified as `<owner>/<repo>#n` -- even where the line or
argument that named it used a bare `#n` -- because `IssueReference` resolves
a bare number against the reading repository before printing it again.
`<label>` is an item as `aco next`/`release`'s own narrative lines print it:
`#<n>` under `storage = "github"` (this file's default assumption unless a
criterion says otherwise), `aco-xxxxxx` under `storage = "state-ref"`
(README "What is still different under `state-ref`"); `--json` output never
uses `<label>` -- it is always the bare number. `<sha>` is a commit id,
`<branch>` a git branch name, `<author>` a pull request's author, `<kind>`
`docs` or `fix`. A trailer block is read through git's own trailer parsing
(`%(trailers:key=...,valueonly)`), never by scanning the message body for a
matching line.

## Behavior table

| state \ trigger | trunk trailer (`aco board`) | `aco check <pr>` \| `aco check <sha>` | `aco release --merged <pr>` \| `--merged <sha\|empty>` (state-ref) | Landungen (`aco board`/`--html`) |
|---|---|---|---|---|
| single `Work-Item:` trailer | LAND-01 | LAND-48 | LAND-47, LAND-52, LAND-59 | LAND-42 |
| repeated `Work-Item:` trailer | LAND-02 | LAND-48 | LAND-52, LAND-59 | LAND-42 |
| control byte inside a trailer value | LAND-03 | — | — | — |
| valid `Work-Item:` + closing reference | — | LAND-04 | LAND-29, LAND-49 | LAND-41 |
| valid `No-Item:` + lane claim | — | LAND-05 | LAND-37 | — |
| no classification / no trailer | — | LAND-06, LAND-58 | LAND-32, LAND-52, LAND-62 | — |
| contradictory trunk trailer (both, or repeated `No-Item:`) | — (lands nothing, LAND-42) | LAND-60 | LAND-61 | — |
| classification line inside a fenced block | — | LAND-07 | — | — |
| two classification lines | — | LAND-08 | LAND-32 | — |
| two `Work-Item:` lines | — | LAND-09 | LAND-32 | — |
| malformed `Work-Item:` value | — | LAND-10 | LAND-32 | — |
| malformed/unknown `No-Item:` kind | — | LAND-11 | LAND-32 | — |
| cross-repository head branch | — | LAND-12 | — | — |
| wrong target branch | — | LAND-13 | LAND-31 | — |
| foreign-repository work item | — | LAND-14 | LAND-52, LAND-62 | — |
| commit outside the first-parent trunk | — | LAND-57 | LAND-52, LAND-62 | — |
| no active claim on the head branch | — | LAND-15, LAND-16 | — | — |
| `No-Item:` PR carrying a closing reference | — | LAND-17 | — | — |
| missing/extra closing reference | — | LAND-18, LAND-19 | — | — |
| non-closing landing keyword (`Implements #n`) | — | LAND-20 | — | — |
| last open child, parent `Next` says none | — | LAND-21, LAND-23 | — | — |
| last open child, parent `Next` still has work | — | LAND-22 | — | — |
| other open children, no `Next` line | — | LAND-24 | — | — |
| other open children, `Next` names work | — | LAND-25 | — | — |
| malformed/wrong-kind/foreign parent | — | LAND-26, LAND-27, LAND-28 | — | — |
| pull request not merged | — | — | LAND-30 | — |
| PR names a different item / kind mismatch | — | — | LAND-35, LAND-62 | — |
| work item still open | — | — | LAND-55, LAND-63 | — |
| forge unreachable right after the release commits | — | — | LAND-38, LAND-50 | — |
| `--abandoned` outcome | — | — | LAND-39 | — |
| issue-less lane, `storage = "state-ref"` | — | — | LAND-56 | — |
| still-open item a merged PR already declared | — | — | — | LAND-43, LAND-51, LAND-53 |
| the Landungen view's own rows | — | — | — | LAND-44, LAND-45, LAND-46, LAND-54 |

## The trunk's own trailer block

- [ ] [LAND-01] A merge or squash commit whose own trailer block carries `Work-Item: #10` marks #10 `code-landed` in `aco board`'s STAGE column, whether or not any pull request body also names it.
- [ ] [LAND-02] A trailer block repeating `Work-Item:` (a squash commit carrying `Work-Item: #11` and `Work-Item: #12`) marks every named item `code-landed`, unlike a pull request body, which allows only one.
- [ ] [LAND-03] A trailer value with a control byte (`#12\x1f#13`) reads as one literal value: `aco board` refuses `ERROR: '#12\x1f#13' is not an item reference; use aco-xxxxxx, #n, or the bare number n`, exit `2`.
- [ ] [LAND-48] `aco check <sha>` reads `<sha>`'s own trailer: `Work-Item:` prints `<sha> declares Work-Item: #<n>`, `No-Item:` prints `<sha> declares No-Item: <kind>`, exit `0`.
- [ ] [LAND-57] A `<sha>` outside the walked first-parent trunk refuses `<sha> is not on the first-parent trunk`, exit `1`.
- [ ] [LAND-58] A trunk `<sha>` carrying neither trailer refuses `<sha> carries no \`Work-Item:\` or \`No-Item:\` trailer`, exit `1`.
- [ ] [LAND-60] A trunk `<sha>` whose trailer block is contradictory -- both `Work-Item:` and `No-Item:`, or `No-Item:` repeated -- makes `check <sha>` refuse `REFUSED: <sha> <that defect sentence>`, exit `1`.
- [ ] [LAND-61] That same contradictory `<sha>` makes `release --merged <sha>` refuse `ERROR: <sha> <that defect sentence>`, exit `2`, before any write.

## The pull request body's own grammar

A pull request body's `Work-Item:` value reads a narrower, stricter grammar
than the trunk trailer's (above): only `OWNER/REPO#n` or `#n` -- never the
bare `n` or `aco-xxxxxx` forms `parse_item_reference` accepts for a trunk
trailer, and never trimmed by that function either. Here, the `Work-Item`/
`No-Item` key and a `No-Item:` value are matched case-insensitively (`docs`,
`Docs`, and `DOCS` all classify the same body, `No-Item: DOCS` included),
and the value itself has its surrounding spaces and tabs trimmed before it
is read (`parse_pull_request_classification`).

- [ ] [LAND-04] A body carrying `Work-Item: #10` and a closing reference for #10 makes `aco check <pr>` print `PR #<n> by <author> declares Work-Item: <owner>/<repo>#10`, exit `0`.
- [ ] [LAND-05] A body carrying `No-Item: docs` with an active issue-less lane claim on the pull request's head branch prints `PR #<n> by <author> declares No-Item: docs`, exit `0`.
- [ ] [LAND-06] A body carrying neither `Work-Item:` nor `No-Item:` makes `check` print `REFUSED: pull request #<n> carries no \`Work-Item:\` or \`No-Item:\` line`, exit `1`.
- [ ] [LAND-07] A `Work-Item:`/`No-Item:` line inside a fenced code block is documentation, not a declaration: a body carrying one only there refuses the same as LAND-06.
- [ ] [LAND-08] A body carrying two classification lines (a `Work-Item:` and a `No-Item:`, or two `No-Item:` lines) refuses `carries <n> classification lines; exactly one is required`, exit `1`.
- [ ] [LAND-09] A body naming two `Work-Item:` lines refuses `names two work items, #a and #b; split it`, exit `1`.
- [ ] [LAND-10] A `Work-Item:` value that is not `OWNER/REPO#n` or `#n` refuses `carries \`Work-Item: <value>\`; a work item reads OWNER/REPO#n or #n`, exit `1`.
- [ ] [LAND-11] A `No-Item:` value that is not `docs` or `fix` refuses `carries \`No-Item: <value>\`; an issue-less pull request is docs or fix`, exit `1`.
- [ ] [LAND-12] A pull request whose head branch lives in another repository refuses `proposes a branch of <repo>; cross-repository pull requests are not classified`, exit `1`, before the body is even read.
- [ ] [LAND-13] A pull request that does not target the repository's default branch refuses `targets '<branch>', not the default branch '<default>'`, exit `1`.
- [ ] [LAND-14] A `Work-Item:` naming another repository's issue refuses `names work item <ref> of another repository, which holds no claim here`, exit `1`.
- [ ] [LAND-15] A `Work-Item:` item with no active claim on the pull request's own head branch refuses `has no active claim for #<n> on branch '<branch>'`, exit `1`.
- [ ] [LAND-16] A `No-Item:` pull request with no active issue-less lane claim on its head branch refuses `has no active issue-less lane claim on branch '<branch>'`, exit `1`.
- [ ] [LAND-17] A `No-Item:` body that also carries a closing reference refuses `declares no work item but closes <ref>; name it as the work item`, exit `1`.
- [ ] [LAND-18] A `Work-Item:` body with no closing reference for that item refuses `carries no closing reference for its work item <item>`, exit `1`.
- [ ] [LAND-19] A body closing anything besides its own work item (or an unpermitted parent, LAND-21) refuses `closes <ref> besides its work item <item>; a pull request lands one item`, exit `1`.
- [ ] [LAND-20] `Implements #n`/`Lands #n` name work a pull request touched, but GitHub closes on neither word: a body carrying one beside its own closing reference for the declared item still passes, exit `0`.

## Parent closing at the last open child

Parentage is GitHub's own sub-issue relation, read fresh for every `check`;
nothing in a body names a parent to this grammar. "No further work" means the
parent's own `Next` line reads `keiner`, `keine`, `nichts`, `none`, `-`, or
is empty, case-insensitively (`_NO_FURTHER_WORK_VALUES`, which a fresh
skeleton's `next = ""` also matches). A malformed body's own defect sentence
is `specs/body-block.spec.md`'s own fact, cited here as `<body defect
sentence>`, not restated.

- [ ] [LAND-21] Closing a parent's last open child, its `Next` naming no further work, must also close the parent; not doing so refuses `closes the last open child of parent <ref>; close the parent too`, exit `1`.
- [ ] [LAND-22] The same last-child landing, when the parent's `Next` line still names work, may pass without closing the parent: exit `0`.
- [ ] [LAND-23] The same last-child landing may also close the completed parent in the same pull request: a body closing both the item and that parent passes, exit `0`.
- [ ] [LAND-24] A landing leaving other open children behind, with no parent `Next` line, refuses `leaves parent <ref> open with <n> other open child/children, whose body carries no Next line`, exit `1`.
- [ ] [LAND-25] The same landing, when the parent's `Next` line names work, passes without closing the parent: exit `0`.
- [ ] [LAND-26] A parent whose own body reads as malformed refuses `has parent <ref> with a <body defect sentence>`, exit `1`, before the last-child rule runs; a valid but incomplete parent proceeds to it.
- [ ] [LAND-27] A recorded parent whose own kind is not `container` refuses `has parent <ref> of kind <kind>, which is not a container; only a container holds children`, exit `1`.
- [ ] [LAND-28] A recorded parent living in another repository refuses `has parent <ref> in another repository, whose children this check cannot read`, exit `1`.

## What `release --merged` requires

- [ ] [LAND-29] `release <n> --merged <pr>` succeeds once the pull request is merged into default and its merge commit's own `Work-Item:` trailer names this item (issue #397); LAND-55 closes a still-open one first.
- [ ] [LAND-49] A successful `--merged` release prints `freed: <label>, <label>` (or `none`) and `next: <label> score <s>: <title>` (or `none`); `--json` carries `"freed": [n,...]` and `"next": n`/`null`.
- [ ] [LAND-30] A pull request that is not merged refuses `pull request #<n> is not merged`, exit `2`, before anything is written.
- [ ] [LAND-31] A pull request merged into a branch other than the default refuses `pull request #<n> merged into '<branch>', not the default branch '<default>'`, exit `2`.
- [ ] [LAND-32] A pull request body's own classification defect (LAND-06..LAND-11) refuses `pull request #<n> <that same defect sentence>`, exit `2` -- read by `check` and an issue-less lane's own `release --merged`.
- LAND-33 (retired 20.09.2026, issue #397, Befund 41): "pull request #<n> names Work-Item: <ref>, not work item #<n>" no longer exists; the merge commit's own trailer decides instead (LAND-62), never the pull request's mutable body.
- LAND-34 (retired 20.09.2026, issue #397, Befund 41): "pull request #<n> names No-Item: <kind>, not work item #<n>" no longer exists for a numbered item's own release; LAND-62 reads the merge commit instead.
- [ ] [LAND-35] A pull request declaring `Work-Item:` while an issue-less lane is being released refuses `pull request #<n> names <ref>; an issue-less lane needs a No-Item line`, exit `2`.
- [ ] [LAND-62] A merge commit off the walked trunk, with no `Work-Item:` trailer, or naming another item refuses `merge commit <sha> of pull request #<n> <that LAND-52 defect sentence>`, exit `2`, before any write.
- [ ] [LAND-55] A still-open work item, once its landing pull request verifies, is closed by this release: a comment `landed by PR #<n>`, then the close — never a refusal (replaces retired LAND-36).
- [ ] [LAND-63] A rerun of LAND-55's own close that finds its `landed by PR #<n>` comment already posted skips it and closes straight away, never posting it twice (issue #397).
- LAND-36 (retired 19.09.2026, issue #359): "work item #<n> is open, not closed" no longer exists; a still-open item is closed instead (LAND-55).
- [ ] [LAND-37] `release --merged <pr>` for an issue-less lane, against a body carrying only `No-Item: docs`, releases the claim without requiring or reading any closing reference.
- [ ] [LAND-38] A forge outage after the release committed prints `hint: could not read the board to report what this landing freed (<error>); run \`aco board\` once the forge is reachable`.
- [ ] [LAND-50] The release LAND-38 reports on never undoes or fails on that hiccup: its claim stays released and its exit code stays `0`, exactly as a reachable forge would have produced.
- [ ] [LAND-39] `--abandoned "<reason>"` never verifies a pull request or reads the board: it prints `RELEASED ...` alone, with no `freed`/`next` line and no `hint` line, ever.
- [ ] [LAND-47] Under `storage = "state-ref"`, `release --merged <sha|empty>` reads the trunk walk (LAND-01/LAND-02); empty picks the newest commit naming this claim's item.
- [ ] [LAND-52] That commit must sit on the walked trunk and carry a `Work-Item:` trailer naming this item, or the release refuses by name, exit `2`, before any write.
- [ ] [LAND-59] On success, `release --merged <sha>` closes the item and releases the claim in one commit, reporting `freed:`/`next:` as LAND-49 does.
- [ ] [LAND-56] `--merged` requires an issue number under `storage = "state-ref"`, refusing `--merged under storage = state-ref requires an issue number; an issue-less lane has no item to close`, exit `2`.
- LAND-40 (retired 19.09.2026, issue #359): the state-ref `--merged` refusal it named no longer exists; LAND-47/LAND-52 are the real grammar.

## The Landungen view

- [ ] [LAND-41] `aco board` marks an item `code-landed` when a merged pull request carries a closing or landing keyword (`Lands`/`Implements` too) naming it — wider than `check`'s own closing reference.
- [ ] [LAND-42] `aco board`/`--html` also mark an item `code-landed` from the trunk's own trailer block alone (LAND-01/LAND-02), independent of any pull request — one union, never two disagreeing sets.
- [ ] [LAND-43] `aco board`'s `RECOVERY` names a still-open item there from a merged pull request's typed `Work-Item:` line (why one can be open there is CLAIM-52's fact, `specs/claim-record.spec.md`).
- [ ] [LAND-51] `aco board`'s recovery reading is keyed on that typed `Work-Item:` line alone, never on the trunk trailer and never on an issue's update time.
- [ ] [LAND-53] `aco next` names every recovery item first, each as `RECOVERY\n<label>: close or re-project`, ahead of the item it recommends next.
- [ ] [LAND-44] `aco board`'s Landungen view lists one row per trunk-trailer-landed item, sha and date from that commit, under both storages, item open or closed.
- [ ] [LAND-45] Under `github` only, Landungen adds one row per item a merged pull request plainly landed (LAND-41) with no trailer, deduped against trailer rows, which always win.
- [ ] [LAND-46] `board --html`'s Landungen section renders `<label> <date> <sha7>` or `<label> <date> PR #<n>`; empty renders `nichts`, never a capability-gated line.
- [ ] [LAND-54] `board --json`'s `"landings"` rows each carry `{"item", "committed_at", "sha", "pull_request"}`, exactly one of `sha`/`pull_request` non-`null`.

## Never

- A trunk commit's own `No-Item:` trailer never marks any issue `code-landed`; only a `Work-Item:` trailer does.
- A trunk commit's contradictory trailer block — both `Work-Item:` and `No-Item:`, or `No-Item:` repeated — is never read as a landing by the trunk trailer walk itself (LAND-42): it lands nothing there, the same as a commit that carries neither. `check <sha>` and `release --merged <sha>` (issue #359, LAND-60/LAND-61) read that same commit directly, and do refuse it by name — never letting `Work-Item:` win by ordering.
- A `Work-Item:` reference sitting in a commit's ordinary message prose, outside its own trailer block, is never read as a landing.
- The trunk walk never follows a side branch: only the first-parent line a merge or squash commit sits on counts, and it never reads a hardcoded `origin` — only the repository's own configured canonical remote.
- `check`/`release --merged` never read a body's `Advances #n` line as a declaration or a closing reference: a dispatched slice is its own item, and only its own pull request closes it.
- The last-child rule never reads a parent's stale `## Next` prose beside the block; only the block's own `next` field decides whether closing is required.
- `release --abandoned` never verifies a pull request, never closes an item, and never reports `freed`/`next`.
- Under `storage = github`, a numbered item's own `release --merged <pr>` never trusts the pull request's own `body` for which item it closes (issue #397, Befund 41): only its merge commit's own trailer, read after the fact, decides -- a body edited after the merge changes nothing this release verifies.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local bare
repository with `main` at one commit, a git identity, `origin/HEAD`, and
`ACO_AGENT` set to `Ada`; `<remote>`, `<tmp>` and `<home>` are the runner's own
paths. A session for `check <pr>`/`release --merged` also names a fixed,
deterministic fake `gh` as a setup precondition (the shape #320 lays out) —
named here, not built by this spec.

### E-LAND-01 — a trunk trailer lands an item with no pull request in sight

Setup: bare-remote, fake `gh`, a merge commit on `main` carrying `Work-Item: #10` in its own trailer block, issue `#10` open with no pull request naming it

```console
$ set -o pipefail && aco board --json | python3 -c "import json,sys; print(json.load(sys.stdin)['items'][0]['stage'])"
code-landed
exit 0
```

### E-LAND-02 — `check <pr>` accepts a classified, closed landing

Setup: bare-remote, fake `gh`, pull request `#57` merged into `main`, body `Work-Item: #42\n\nCloses #42`, an active claim on issue `#42` matching the pull request's head branch

```console
$ aco check 57
PR #57 by Ada declares Work-Item: <owner>/<repo>#42
exit 0
```

### E-LAND-03 — `check <pr>` refuses an unclassified body

Setup: bare-remote, fake `gh`, pull request `#58` whose body carries `Advances #42` and nothing else

```console
$ aco check 58
2> REFUSED: pull request #58 carries no `Work-Item:` or `No-Item:` line
exit 1
```

### E-LAND-04 — `release --merged` closes a still-open work item itself

Setup: bare-remote, fake `gh`, pull request `#57` merged, body `Work-Item: #42\n\nCloses #42`, its own merge commit's trailer also naming `Work-Item: #42`, issue `#42` claimed and still open on the forge

```console
$ aco release 42 --merged 57
RELEASED issue #42: <claim-id>
freed: none
next: none
exit 0
```

### E-LAND-05 — a last-child landing that must also close its parent

Setup: bare-remote, fake `gh`, parent `#5` a container with one open child `#42` and `Next` line `keiner`, pull request `#57` merged, body `Work-Item: #42\n\nCloses #42`

```console
$ aco check 57
2> REFUSED: pull request #57 closes the last open child of parent <owner>/<repo>#5; close the parent too
exit 1
```

### E-LAND-47 — a merge landing closes its item and releases the claim under state-ref

Setup: bare-remote, `storage = "state-ref"`, `main` carrying a merge commit whose trailer names `aco-00000a` (`#10`), item `#10` open and claimed

```console
$ aco release 10 --merged
RELEASED issue aco-00000a: <claim-id>
freed: none
next: none
exit 0
```

### E-LAND-52 — a commit off the first-parent trunk refuses

Setup: bare-remote, `storage = "state-ref"`, item `#10` open and claimed, `<sha>` a commit on a side branch never merged into `main`

```console
$ aco release 10 --merged <sha>
2> ERROR: <sha> is not on the first-parent trunk
exit 2
```

### E-LAND-62 — a github merge commit with no trailer refuses `release --merged`

Setup: bare-remote, fake `gh`, pull request `#57` merged into `main`, body `Work-Item: #42\n\nCloses #42`, its own merge commit carrying no trailer, issue `#42` claimed and open

```console
$ aco release 42 --merged 57
2> ERROR: merge commit <sha> of pull request #57 carries no `Work-Item:` trailer
exit 2
```

### E-LAND-48 — `check <sha>` reads a trunk commit's own trailer

Setup: bare-remote, `main` carrying a commit whose trailer reads `Work-Item: #20`

```console
$ aco check <sha>
<sha> declares Work-Item: #20
exit 0
```

### E-LAND-56 — `release --merged` with no issue number under state-ref refuses

Setup: bare-remote, `storage = "state-ref"`, a live issue-less lane claim

```console
$ aco release --merged
2> ERROR: --merged under storage = state-ref requires an issue number; an issue-less lane has no item to close
exit 2
```

### E-LAND-60 — a contradictory trunk trailer refuses `check <sha>`

Setup: bare-remote, `main` carrying a commit whose trailer reads `Work-Item: #20` and `No-Item: docs`

```console
$ aco check <sha>
2> REFUSED: <sha> carries both `Work-Item:` and `No-Item:` trailers; a landed commit is one or the other
exit 1
```

### E-LAND-61 — the same contradictory trailer refuses `release --merged <sha>`

Setup: bare-remote, `storage = "state-ref"`, item `#20` open and claimed, `main` carrying a commit whose trailer reads `Work-Item: #20` and `No-Item: docs`

```console
$ aco release 20 --merged <sha>
2> ERROR: <sha> carries both `Work-Item:` and `No-Item:` trailers; a landed commit is one or the other
exit 2
```
