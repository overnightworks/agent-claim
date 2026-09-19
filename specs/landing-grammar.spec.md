# Landing grammar

What a landing is, and the one grammar every reader classifies it from: a
merge or squash commit's own trailer block on the trunk, and the matching
`Work-Item:`/`No-Item:` line in a pull request body before it merges. This
file owns that grammar, `aco check <pr>`'s classification, the parent-closing
rule, what `aco release --merged` verifies, and the "Landungen" landing view
`aco board`/`aco board --html` derive from it. Each command's own spec (none
exist yet for `check`/`release`/`board`) would cite these IDs rather than
restate them.

`<n>`/`<item>`/`<ref>` are issue or pull request numbers, `<sha>` a commit id,
`<branch>` a git branch name, `<author>` a pull request's author, `<kind>`
`docs` or `fix`. A trailer block is read through git's own trailer parsing
(`%(trailers:key=...,valueonly)`), never by scanning the message body for a
matching line.

## Behavior table

| state \ trigger | trunk trailer (`aco board`) | `aco check <pr>` | `aco release --merged <pr>` | Landungen (`aco board`/`--html`) |
|---|---|---|---|---|
| single `Work-Item:` trailer | LAND-01 | — | — | LAND-42 |
| repeated `Work-Item:` trailer | LAND-02 | — | — | LAND-42 |
| control byte inside a trailer value | LAND-03 | — | — | — |
| valid `Work-Item:` + closing reference | — | LAND-04 | LAND-29, LAND-49 | LAND-41 |
| valid `No-Item:` + lane claim | — | LAND-05 | LAND-37 | — |
| no classification line | — | LAND-06 | LAND-32 | — |
| classification line inside a fenced block | — | LAND-07 | — | — |
| two classification lines | — | LAND-08 | LAND-32 | — |
| two `Work-Item:` lines | — | LAND-09 | LAND-32 | — |
| malformed `Work-Item:` value | — | LAND-10 | LAND-32 | — |
| malformed/unknown `No-Item:` kind | — | LAND-11 | LAND-32 | — |
| cross-repository head branch | — | LAND-12 | — | — |
| wrong target branch | — | LAND-13 | LAND-31 | — |
| foreign-repository work item | — | LAND-14 | — | — |
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
| PR names a different item / kind mismatch | — | — | LAND-33, LAND-34, LAND-35 | — |
| work item still open | — | — | LAND-36 | — |
| forge unreachable right after the release commits | — | — | LAND-38, LAND-50 | — |
| `--abandoned` outcome | — | — | LAND-39 | — |
| under `storage = "state-ref"` (today) | — | — | LAND-40 | — |
| still-open item a merged PR already declared | — | — | — | LAND-43, LAND-51, LAND-53 |
| `landings_derivable` is false | — | — | — | LAND-44, LAND-46, LAND-54 |
| a `code-landed` row with no resolved pull request | — | — | — | LAND-45 |
| ruled but not built (`storage = "state-ref"`, #297) | LAND-47, LAND-52 | LAND-48 | LAND-47, LAND-52 | — |

## The trunk's own trailer block

- [ ] [LAND-01] A merge or squash commit whose own trailer block carries `Work-Item: #10` marks #10 `code-landed` in `aco board`'s STAGE column, whether or not any pull request body also names it.
- [ ] [LAND-02] A trailer block repeating `Work-Item:` (a squash commit carrying `Work-Item: #11` and `Work-Item: #12`) marks every named item `code-landed`, unlike a pull request body, which allows only one.
- [ ] [LAND-03] A trailer value with a control byte (`#12\x1f#13`) reads as one literal value: `aco board` refuses `ERROR: '#12\x1f#13' is not an item reference; use aco-xxxxxx, #n, or the bare number n`, exit `2`.

## The same grammar in a pull request body

- [ ] [LAND-04] A body carrying `Work-Item: #10` and a closing reference for #10 makes `aco check <pr>` print `PR #<n> by <author> declares Work-Item: #10`, exit `0`.
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
parent's own `Next` line reads `keiner`, `keine`, `nichts`, `none`, or `-`,
case-insensitively.

- [ ] [LAND-21] Closing a parent's last open child, its `Next` naming no further work, must also close the parent; not doing so refuses `closes the last open child of parent <ref>; close the parent too`, exit `1`.
- [ ] [LAND-22] The same last-child landing, when the parent's `Next` line still names work, may pass without closing the parent: exit `0`.
- [ ] [LAND-23] The same last-child landing may also close the completed parent in the same pull request: a body closing both the item and that parent passes, exit `0`.
- [ ] [LAND-24] A landing leaving other open children behind, with no parent `Next` line, refuses `leaves parent <ref> open with <n> other open child/children, whose body carries no Next line`, exit `1`.
- [ ] [LAND-25] The same landing, when the parent's `Next` line names work, passes without closing the parent: exit `0`.
- [ ] [LAND-26] A parent whose own body is malformed or incomplete refuses `has parent <ref> with a <body defect sentence>`, exit `1`, before the last-child rule is ever evaluated (see `specs/body-block.spec.md`).
- [ ] [LAND-27] A recorded parent whose own kind is not `container` refuses `has parent <ref> of kind <kind>, which is not a container; only a container holds children`, exit `1`.
- [ ] [LAND-28] A recorded parent living in another repository refuses `has parent <ref> in another repository, whose children this check cannot read`, exit `1`.

## What `release --merged` requires

- [ ] [LAND-29] `release <n> --merged <pr>` succeeds only when the pull request is merged into the default branch, its classification names this claim's own item, and (for an issue) that item is already closed.
- [ ] [LAND-49] A successful `--merged` release prints `freed: #a, #b` (or `freed: none`) and `next: #n score <s>: <title>` (or `next: none`); `--json` carries `freed`/`next` the same way.
- [ ] [LAND-30] A pull request that is not merged refuses `pull request #<n> is not merged`, exit `2`, before anything is written.
- [ ] [LAND-31] A pull request merged into a branch other than the default refuses `pull request #<n> merged into '<branch>', not the default branch '<default>'`, exit `2`.
- [ ] [LAND-32] A pull request body carrying any classification defect (LAND-06..LAND-11) refuses `pull request #<n> <that same defect sentence>`, exit `2` — one grammar, read by both `check` and `release --merged`.
- [ ] [LAND-33] A pull request naming a different work item than the one being released refuses `pull request #<n> names Work-Item: <ref>, not work item #<n>`, exit `2`.
- [ ] [LAND-34] A pull request declaring `No-Item:` while an issue is being released refuses `pull request #<n> names No-Item: <kind>, not work item #<n>`, exit `2`.
- [ ] [LAND-35] A pull request declaring `Work-Item:` while an issue-less lane is being released refuses `pull request #<n> names <ref>; an issue-less lane needs a No-Item line`, exit `2`.
- [ ] [LAND-36] An issue whose work item is not yet closed on the forge refuses `work item #<n> is open, not closed`, exit `2`, even once its landing pull request is fully verified.
- [ ] [LAND-37] `release --merged <pr>` for an issue-less lane, against a body carrying only `No-Item: docs`, releases the claim without requiring or reading any closing reference.
- [ ] [LAND-38] A forge outage after the release committed prints `hint: could not read the board to report what this landing freed (<error>); run \`aco board\` once the forge is reachable`.
- [ ] [LAND-50] The release LAND-38 reports on never undoes or fails on that hiccup: its claim stays released and its exit code stays `0`, exactly as a reachable forge would have produced.
- [ ] [LAND-39] `--abandoned "<reason>"` never verifies a pull request or reads the board: it prints `RELEASED ...` alone, with no `freed`/`next` line and no `hint` line, ever.
- [ ] [LAND-40] `release --merged` under state-ref refuses ``state-ref cannot verify a merged pull request yet (#230 slice 6); land offline with `item close` and `release --abandoned "landed as <sha>"` until then``.

## The Landungen view

- [ ] [LAND-41] `aco board` marks an item `code-landed` when a merged pull request carries a closing or landing keyword (`Lands`/`Implements` too) naming it — wider than `check`'s own closing reference.
- [ ] [LAND-42] `aco board`/`--html` also mark an item `code-landed` from the trunk's own trailer block alone (LAND-01/LAND-02), independent of any pull request — one union, never two disagreeing sets.
- [ ] [LAND-43] `aco board`'s `RECOVERY (close or re-project)` section lists every still-open issue a merged pull request's typed `Work-Item:` line already named.
- [ ] [LAND-51] `aco board`'s recovery reading is keyed on that typed `Work-Item:` line alone, never on the trunk trailer and never on an issue's update time.
- [ ] [LAND-53] `aco next` names every recovery item first, each as `RECOVERY\n#<n>: close or re-project`, ahead of the item it recommends next.
- [ ] [LAND-44] A board source that cannot list merged pull requests at all prints ``landings are not derivable from this board source: recovery and code-landed stay empty`` after `RECOVERY` in text output.
- [ ] [LAND-54] That same board source's `--json` carries `"landings_derivable": false` as a top-level field.
- [ ] [LAND-45] `board --html`'s "Landungen" section pairs each `code-landed` item with the naming pull request (`PR #<n>: <title>`), else the trunk commit (`<date> <sha7>`), else `PR nicht zugeordnet`.
- [ ] [LAND-46] `board --html` shows `nicht ableitbar` in place of the "Landungen" list only once neither a pull request nor a trunk trailer resolves any row.

## Landing without a forge (ruled, not yet built — #297)

- [ ] [LAND-47] Under state-ref, `release --merged <sha|empty>` reads the trunk walk of LAND-01/LAND-02; an empty value picks the newest trunk commit whose own trailer names this item.
- [ ] [LAND-52] That commit must sit on the trunk and carry a `Work-Item:` trailer naming this item, or the release refuses by name; on success it closes the item and reports `freed:`/`next:` as LAND-49 does today.
- [ ] [LAND-48] Under state-ref, `check <sha>` reads a commit's own trailer through the same grammar `check <pr>` reads a body with (LAND-04/LAND-06), printing the same `work-item`/`no-item` answers.

## Never

- A trunk commit's own `No-Item:` trailer never marks any issue `code-landed`; only a `Work-Item:` trailer does.
- A trunk commit's contradictory trailer block — both `Work-Item:` and `No-Item:`, or `No-Item:` repeated — is never read as a landing and never refuses any command; it simply lands nothing, the same as a commit that carries neither.
- A `Work-Item:` reference sitting in a commit's ordinary message prose, outside its own trailer block, is never read as a landing.
- The trunk walk never follows a side branch: only the first-parent line a merge or squash commit sits on counts, and it never reads a hardcoded `origin` — only the repository's own configured canonical remote.
- `check`/`release --merged` never read a body's `Advances #n` line as a declaration or a closing reference: a dispatched slice is its own item, and only its own pull request closes it.
- The last-child rule never reads a parent's stale `## Next` prose beside the block; only the block's own `next` field decides whether closing is required.
- `release --abandoned` never verifies a pull request, never closes an item, and never reports `freed`/`next`.

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
$ aco board --json | python3 -c "import json,sys; print(json.load(sys.stdin)['items'][0]['stage'])"
code-landed
exit 0
```

### E-LAND-02 — `check <pr>` accepts a classified, closed landing

Setup: bare-remote, fake `gh`, pull request `#57` merged into `main`, body `Work-Item: #42\n\nCloses #42`, an active claim on issue `#42` matching the pull request's head branch

```console
$ aco check 57
PR #57 by Ada declares Work-Item: #42
exit 0
```

### E-LAND-03 — `check <pr>` refuses an unclassified body

Setup: bare-remote, fake `gh`, pull request `#58` whose body carries `Advances #42` and nothing else

```console
$ aco check 58
2> REFUSED: pull request #58 carries no `Work-Item:` or `No-Item:` line
exit 1
```

### E-LAND-04 — `release --merged` requires the item closed first

Setup: bare-remote, fake `gh`, pull request `#57` merged, body `Work-Item: #42\n\nCloses #42`, issue `#42` claimed and still open on the forge

```console
$ aco release 42 --merged 57
2> ERROR: work item #42 is open, not closed
exit 2
```

### E-LAND-05 — a last-child landing that must also close its parent

Setup: bare-remote, fake `gh`, parent `#5` a container with one open child `#42` and `Next` line `keiner`, pull request `#57` merged, body `Work-Item: #42\n\nCloses #42`

```console
$ aco check 57
2> REFUSED: pull request #57 closes the last open child of parent #5; close the parent too
exit 1
```
