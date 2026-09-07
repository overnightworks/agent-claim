# agent-claim

`agent-claim` is a small installable CLI that gives coding agents one claim
state per repository: a compare-and-swap git ref, `refs/aco/state`, on the
repository's own canonical remote. It is provider-neutral: Codex, Claude,
Grok, people, and future agents use the same contract.

## Install and maintain

```bash
uv tool install git+https://github.com/FlexOr2/agent-claim.git@v0.10.0
# or: pipx install git+https://github.com/FlexOr2/agent-claim.git@v0.10.0
uv tool upgrade agent-claim
uv tool uninstall agent-claim
```

To roll back, force-install the previous tag with `uv tool install --force
git+https://github.com/FlexOr2/agent-claim.git@v0.8.0`.

Local proofs run under the pinned interpreter named in `.python-version`
(currently 3.12); `uv sync` creates the development venv from that file.

### Reader/writer compatibility

Claim state is a git tree, `claims/<key>.toml` / `ids/<claim_id>` /
`resources/<name>.toml` under a `schema.toml` `version`, read and written
whole: an unknown key or a malformed record fails the whole read loud, never
a single quarantinable claim -- a commit is the unit a writer writes, so a
broken tree is corrupt state. `version = 1` is the compatibility contract
this release actually makes; a later tree version is a new cut, not a silent
patch. Every repository migrated off the old GitHub-issue ledger carries this
tombstone, posted once on that now-closed issue and never edited afterward:

```
<!-- agent-claim:v2 {"action":"state_cut","claim_id":"state-cut","agent":"coordinator","role":"coordinator"} -->

This ledger is closed. Claim state now lives in this repository's state ref; upgrade the tool and re-read its README. Do not post further claim comments.
Agent: coordinator (coordinator)
```

It names no version number and no new command name -- old clients are
"0.12.x and older" in the text, never "the 0.13 client" -- so it never needs
editing to stay accurate; a 0.12.x or older client that still tries to read a
ledger hits its unknown `state_cut` action and fails loud, telling the
operator to upgrade. The migration names three rollback windows. Before the
tombstone, the ledger is still the only truth and nothing about the cut is
yet irreversible. Between the tombstone and the import, the ledger is closed
to writers but still holds the truth the import has not yet moved, so a
stuck import is repaired by deleting the tombstone and reposting it, never by
editing it in place. After the import lands, the state ref is the truth and
the ledger issue closes only once that repository's import is proven against
the `status --json` snapshot taken before its tombstone. A repository not yet
migrated still runs its old ledger under an installation pinned to 0.12.x or
earlier.

## Five-command quick start

```bash
agent-claim bootstrap
agent-claim status
agent-claim claim 42 --agent "Ada" --scope src/widget.py
agent-claim release 42 --merged 57
```

Omitted `--base`/`--branch` bind the current checkout; explicit values must match it.
Omitted `--agent` on `claim` and `release` is filled from non-empty
`AGENT_CLAIM_AGENT`, else non-empty `GROK_SESSION_ID` as `Grok {session}`, else
non-empty `CLAUDE_SESSION_ID` as `Claude {session}`. `GROK_AGENT` is not a name.
Missing or present-invalid identity fails closed before any write. Omitted
`--role` on `claim` is `builder`; an explicit `--role` wins. Repeating an
interrupted `claim` for the same active item, agent, role, branch, and scope,
with the same claim id, returns that active claim instead of writing a second
one. A different live claim still fails; a released claim id remains terminal.

Omitted `--claim-id` on `release` selects the unique active claim on that issue
or lane whose agent is this session and whose branch is the current checkout;
otherwise it fails closed.
Omitted `--role` on `release` uses that selected claim's role; an explicit
`--role` must still match unless `--coordinator-override`, which still requires
`--role coordinator`. `release` takes exactly one outcome, never a free-form
reason: `--merged <pull request>` or `--abandoned "<reason>"`. `--merged` is
verified against GitHub before anything is written — the pull request must be
merged into the default branch, its `Work-Item:` line must name this claim's
item (or it must carry `No-Item:` for an issue-less lane), and that item must be
closed; otherwise the release is refused, naming what is missing. A `--claim-id`
already consumed, active or released, is refused before anything is written;
release the old claim and pass a fresh `--claim-id` instead.
`rescope <issue> --add <path> [--drop <path>]` changes a live claim's scope
without releasing it: the claim id and base stay, added paths are advisory
like `claim`, and a resulting wide scope uses the same `--whole` rule as
`claim`. There is no release window. It does not require HEAD to match
base or a clean tree.

Run commands in the repository being coordinated, or pass `--repo
OWNER/REPOSITORY`. A claim must begin from a clean linked worktree and binds its
base commit, branch, issue, and repository-relative scope. `--scope a,b` is
the same as `--scope a --scope b`; each path is stored and compared
separately. A scope is wide when it declares more than three paths, any directory,
or, once the repository has at least twelve versioned files, more than a
quarter of them; a single named path in a smaller repository is never wide on
share. Named new paths count; children of containers are never exempt. The
refusal names the one condition that tripped, with its numbers, instead of
restating the whole rule: `scope is wide: 4 paths exceeds three; pass --whole
REASON`, `scope is wide: 1 directory in scope (docs); pass --whole REASON`,
or `scope is wide: 4 paths of 12 versioned files (33 %) exceeds a quarter;
pass --whole REASON`. Wide
scopes need `--whole "<one sentence why it does not split>"`; the sentence
lands in the claim record and `status`/`status --path` show it. `--allow-directory` is
removed: pass `--whole` instead. Live claims
are advisory: they say who works where and do not refuse path overlap. Two
lanes may claim the same
directory or the same file; `claim` and `status` print the overlap as a note.
The same issue or the same `docs/`/`fix/` lane branch still holds at most one
live claim. `claim --resource <name>` requests a name-only intent; the held integer is the
next positive value not occupied by an earlier first-occurrence request for that name. An
explicit posted value occupies that integer even after release; a released auto still
occupies the integer it would have been assigned. A second live hold of the same name and
value is refused: only the earliest live claim of that pair is the holder. Sequential
allocations stay unique even after a release. `claim` prints
how many versioned files the scope covers and which open claims it overlaps.
`status --path <path>` prints every live claim that holds a path.
Agents should read `--json` from `status`, `claim`, `release`, and `rescope`.
`status` prints each live claim's age from its `opened_commit`'s committer date
(the state-ref commit that first introduced it) as `Xh Ym`, and marks it `old`
after more than one hour.

`claim`, `rescope`, `release`, and `status` read and write exclusively through
`refs/aco/state` -- a compare-and-swap git ref on the repository's configured
`canonical_remote` (`.agent-claim/board.toml`, default `origin`), invisible in
the GitHub UI and never checked out into a working tree. No command but
`bootstrap` ever creates that ref, so a `claim`/`rescope`/`release`/`protect`
call against a repository that has not been bootstrapped refuses by name
instead of silently creating it. `release --coordinator-override` is for an
explicit coordinator action; a stale takeover is that same override-release
followed by an ordinary `claim` -- two commits, no separate verb, and the new
claim never reuses a resource integer the released claim held.

`bootstrap` does two jobs, never both in the same invocation. Without
`--ledger`, it creates or reports the state ref: a present ref is a pure
read (prints the ref's commit id, writes nothing); an absent ref (proven by
`git ls-remote --exit-code`, never inferred from a fetch failure) gets one
commit holding an empty state tree (`schema.toml`, `version = 1`) pushed as
a plain fast-forward; an unreachable remote (auth or transport failure)
fails loud instead of either printing or writing. `bootstrap --ledger N`
is the one-time, head-only import of a repository's closed GitHub-issue
ledger (issue `N`) into a freshly created state ref, run from that
repository's own checkout after every writer has drained and a `state_cut`
tombstone comment has been posted on the ledger issue: it refuses when the
forge target does not match the canonical remote's own repository, when the
tombstone is missing, edited, or not the ledger's last protocol comment,
when the ledger was itself superseded by another, and when the ref already
carries an import of a *different* run (an empty ref created by some other
command in error is named and left for manual repair, never silently
overwritten); on success it carries every active claim, every claim id ever
consumed, and every occupied resource value forward, and pushes the result
as the ref's very first commit.

## Landing classification

`agent-claim pr-check --pr <n>` reads one pull request of the current
checkout's repository (or `--repo OWNER/REPOSITORY`) and answers one question:
which item does this landing close? It prints `PR #<n> by <author> declares
<classification>` and exits 0, or prints one `REFUSED: pull request #<n> ...`
line and exits 1. Run it as a required check on every pull request that targets
the default branch. It needs a working tree of the repository (a shallow
checkout is enough) to read the repository's `body_contract` pin from
`.agent-claim/board.toml`, and refuses outright without one.

A pull request body carries exactly one classification line:

- `Work-Item: OWNER/REPO#n` (or `Work-Item: #n` for this repository) together
  with a closing reference for that same item — `Closes #n`, or any other
  keyword GitHub itself closes on, optionally qualified as `OWNER/REPO#n`; or
- `No-Item: docs` or `No-Item: fix` for a lane that owns no issue.

`pr-check` refuses a body with no classification line, with more than one, or
naming two work items (split the pull request); a work item that lives in
another repository; a work item with no active claim
on the pull request's head branch; a closing reference naming anything but the
work item; a `No-Item` pull request without an active issue-less lane claim on
that head branch, or carrying any closing reference at all; a pull request
whose head branch lives in another repository; and a pull request that does not
target the default branch. A classification line inside a fenced code block is
documentation, never a declaration. `Advances #n` is read nowhere: a dispatched
slice is its own item, and its pull request closes it.

Parentage is GitHub's own sub-issue relation, not a line in a body. `pr-check`
reads the work item's recorded parent and that parent's open sub-issues. The
parent must be kind `container` (its own native issue type); any other kind is
refused by name, since only a container holds children. Closing the parent's
last open child *permits* closing the parent in the same landing but
*requires* it only when the parent's own `Next` line names no further work
(`keiner`/`keine`/`nichts`/`none`/`-`, case-insensitively, all count as none);
with further `Next` work the container keeps dispatching slices and the
landing may pass without closing it. A parent that keeps other open children
must stay open and carry a `Next` line in its body. A parent recorded in
another repository is refused by name, never skipped silently. `claim` warns
when a slice-shaped title such as `Schema (#79 Scheibe 21)` names a parent
that GitHub does not record as one, and refuses outright when the target
itself is a container (`claim a child`).

## Read-only board projection

`agent-claim board` reads the open issues, open PRs, PRs merged since the
oldest open issue was filed, and the live claim state ref, then prints a
ranked projection with `READY NOW` and `STALE` sections. A pull request that
advances an issue without closing it — an epic's dispatched slice, typically
— credits that issue when the pull request names it a second time outside a
dedicated `Refs #N`/`Part of #N` line; that is a syntactic marker, not a
verified relation, so an unrelated pull request naming the same issue twice
by coincidence would still credit it.

The table exposes which exact contract headings were found, an
`EXPECT` cell (`-`, `OPEN/TOTAL`, or `ruled N` / `ruled N old`), a concise `Next`, and a CLAIM
cell with `-` or the agent, role, claim age, and `old` when the claim comment
is older than one hour; JSON includes the complete derived contract state and
the same open/total expectation progress. An `Erwartung`, `Erwartungen`, or
`Erwartungsliste` heading makes the following block an expectation list: a line
with `*(Default: yes|no|later)*` is proposed. A block is ruled only when every
expectation line carries a `*(geregelt: ja)*` or `*(geregelt: NEIN ...)*`
marker; absent or malformed markers remain proposed. A ruled block also shows
how many default-branch first-parent landings (`git log --first-parent`
committer times) happened after its heading date (`DD.MM.YYYY`, preferring
`GEREGELT: Operator …`); ten or more mark it `old`. Missing or proposed
expectations have neither fresh nor old. If a ruled block has no readable date
or git cannot name the default branch, that is an error, never silently fresh.
It never writes GitHub.
The target defaults to the repository of the current checkout;
for another GitHub repository run `agent-claim --repo FlexOr2/atelier-2 board`.
The current checkout may set `priority_labels` as an ordered non-empty list in
`.agent-claim/board.toml`; absent configuration uses `security`, `data`, `ci`,
`product`, `ux`, then `cleanup`. `board_rank` orders every item on five fields:
category, then score, then the critical label's index, then container, then
issue number. Category is critical (the first three configured labels or a
Bug, competing by score among each other — see the `KIND` paragraph below)
first, then a blocker, then a container's completing last child, then the
remaining configured labels, then unlabelled; the label index only ever
tie-breaks inside the critical category, so every other category still
degenerates to plain number order at equal score. The same file may set one
`idea_label`; an item carrying that label with no Now/Next/Blocked by/Done when
projection ranks normally, and `next` tells the head `Problem neu prüfen und
Item verfeinern`. Once it has a complete contract, its own Next takes over;
without the configured label, a projectionless item remains `body incomplete`.
The same file's `body_contract` key pins how a work-item body itself is read
(prose, the default, or the typed block below); `priority_labels` and
`idea_label` mean the same thing in either mode.
The board table's `FREED` column shows `YYYY-MM-DD (N d)` when every listed
issue blocker has closed, using the latest such UTC closing date and whole days
since then; it otherwise shows `-`. Every item in `board --json` carries the
same values as `freed_on` (`YYYY-MM-DD` or `null`) and `freed_days` (a
nonnegative integer or `null`).

An item's `KIND` column (`task`, `bug`, `feature`, `container`, or `-` when the
forge reports no native issue type) comes from GitHub's issue type, never a
label; a Bug counts as critical exactly like the first three configured
labels, per the ranking paragraph above. A `container`-kinded issue shows its
sub-issue progress — `board`'s `KIND` cell (`container 2/3`) and a trailing
`CONTAINERS` section (`#122 2/3 closed; open: #112 (blocked by #136)`);
`board --json` carries the same figures under each item's `container`
(`closed`, `total`, `open_children`) and its parent under `container_parent`.
A container is never itself actionable — its `board`/`next` reason reads
`container; claim a child` — and its own last open child (once at least one
sibling has closed) ranks above ordinary work, though never above a critical
item or a real blocker.

`board` also shows an `UNCUT` section naming, per item, the slice-table
rows (`#79`'s grammar) that are not yet linked to a dispatched child — an
undispatched (`—`) row, or one whose item cell is neither the marker nor a
well-formed `#n` link — by row index, as `#<item>: rows N, N, … uncut`; a row
linking any issue (open or closed) is landed, never uncut. A malformed row
(the wrong column count, or a non-integer `#` cell) is named by its `#` cell
and reason instead, e.g. `row "B": index must be a positive integer`,
appended to the same line. **`board --json`'s `uncut` contract changed**: the
top-level `uncut` list still carries `item`, but `rows` is now a list of
`{"index", "title"}` objects (was a list of bare name strings) and a new
`malformed` list carries each malformed row's `line`, `id_cell`, and
`reason`. This is a finding, never a status column: a landed row simply
leaves the list.

`board` prints a `RECOVERY (close or re-project)` section after `STALE`,
followed by the `CONTAINERS` and `UNCUT` sections above; `next` names recovery
items first with that step: open issues that a merged pull request already
declared as its `Work-Item:` — the landing happened, the bookkeeping did not. It
is keyed on that typed line, never on an issue's update time. `next --json`
carries the same items under `recovery`.

`board` ends its text output with a `requests: N` line, counting every read
the command made through the forge port; `board --json` carries the same
count as a top-level `"requests"` field.

`agent-claim rulings` lists only open board items with open expectation lines
as `#NUMBER OPEN/TOTAL: TITLE`; `rulings --json` returns the same `number`,
`title`, `open`, and `total` values. It is read-only and uses the board's
priority category and score first, then fewer open expectation lines and the
issue number. An empty list succeeds.

Use `agent-claim next` (or `agent-claim next --json`) to name the board's
top-ranked qualifying row — the same `board_rank` order `board` shows.
`next --json` always carries an `action` field, naming one of three shapes
or `null` when nothing qualifies. `work_item`: the row is open, free,
unblocked, not frozen, and has a complete Now/Next/Blocked by/Done when
contract, or is a configured projectionless idea; its text form also prints
`Run: agent-claim claim <n> --scope <paths>` (the literal placeholder
`<paths>`, since the scope cannot be derived) and a line pointing at the
item body for the real paths — the `--json` form is unchanged beyond the
always-present `action` field. `cut_slice`: a container with no open child
still names work in its own `Next` line (`{"action": "cut_slice", "number",
"title", "slice", "cut_title"}`); `slice` is the container's own human step,
`cut_title` is the exact title `cut` accepts (#177: the first uncut
`[[slice]]` entry's title when one exists, else `slice`) — the head cuts
that slice (`agent-claim cut <number> --title "<cut_title>"`) and dispatches
it. `close_container`: a container with no
open child and no further `Next` work (`{"action": "close_container",
"number", "closed", "total"}`); the head closes it. A container is never
itself the `work_item` target. Pulling is not dispatching, so unruled
expectations never withhold a `work_item`; the pulled item carries
`Erwartungen ungeregelt, beim Ziehen zuerst refinen` instead, and an item
ruled long ago carries `vor N Landungen geregelt, beim Ziehen neu refinen`
(both as the JSON `ruling_hint`). Items that genuinely cannot be worked —
claimed, blocked by an open issue, frozen, or without a complete contract
when they are not a configured projectionless idea — are named with that
reason under `SKIPPED` (also in the JSON `skipped` list; a container chosen
as the `next` action is never also listed there). `next` exits 3 when
nothing qualifies, but still prints at least `No actionable item.` (plus any
`SKIPPED`/`RECOVERY` sections) in text, and `--json` still emits an object —
`{"action": null, "recovery": [...], "skipped": [...]}` — never nothing.
`claim` refuses work out of order when a higher-priority actionable item — the
same order `board` and `next` use — is free. It also refuses a blocked item,
one sentence per repository pin, then the same shared override in either mode:

- Prose: `claim` refuses an item whose `Blocked by` still names at least one
  open issue (a pull request, or a closed or missing issue, does not count —
  those stay their own refusals below).
- Block (`body_contract = "block"`, below): `claim` refuses an item that has
  at least one open GitHub blocked-by dependency, including a foreign
  `owner/repo#n`; a pull request or a closed same-repository dependency does
  not count.
- Shared: the message is `#5 is blocked by #3 (open); pass --out-of-order
  REASON to claim it anyway` (a foreign entry renders as `owner/repo#n`),
  naming every open blocker. Pass `--out-of-order REASON` to proceed
  deliberately in either case; it remains visible as a warning and preserves
  the reason in the claim comment.

Before it writes a claim, `claim` also reads the pulled issue's live contract:
`Now`, `Next`, `Blocked by`, and `Done when` each appear at most once outside
fenced code examples. `Blocked by` is exactly `nichts` or a comma-separated
`#N` list such as `#62, #75`; every listed issue must be open. `claim` also
refuses with `#<n> body incomplete: <missing sections>` (body order, e.g.
`#150 body incomplete: Now, Done when`) when any of the four sections is
empty, unless the issue is a configured projectionless idea (above) — the
same rule `board` already applies to `actionable` (whose own short
`body incomplete` form is unchanged), so a freshly `cut` child (its
`board.CHILD_SKELETON` body has every section present but empty) is refused
until the head fills it in. The check does not limit body size or inspect
references in `Next`, and `release` stays available even when the body's
contract has since become invalid.

A body line `Eingefroren bis: <trigger in one sentence> (Operator, DD.MM.YYYY)`
freezes an issue: it drops out of `next` and the higher-priority refusal check
even though its score keeps showing on `board`, and deleting the line thaws it
again. The tool only checks the line's form, never who wrote it — that
authority is the coordination contract's. It reads the body the way GitHub
renders it: a marker inside a fenced code block (` ``` ` or `~~~`, including
one left unclosed to the end of the body) is documentation, never a live
marker — examples belong in a fence. A blockquoted `> Eingefroren bis: …`
still freezes; this repo already quotes operator rulings, so a quoted freeze
line reads as the freeze itself.

## Typed body contract (`body_contract`)

Everything above describes the default, `body_contract = "prose"`: the four
regex-read `## Now`/`## Next`/`## Blocked by`/`## Done when` sections. A
repository may instead set, in `.agent-claim/board.toml`:

```toml
body_contract = "block"
```

Under that pin, `board`, `next`, issue-mode `claim`, `cut`, `rulings`, and the
parent-body part of `pr-check` read a work item's `Now`/`Next`/`Done when`,
freeze, expectations, and undispatched slices from one typed `agent-claim`
fenced TOML block instead — no regex, no German markers, no slice table.

A fresh, unfilled item looks like this — the same four lines `cut` writes
automatically for a dispatched child, and what a human pastes by hand into a
`gh issue create` / operator-opened item:

````
```agent-claim
version = 1
now = ""
next = ""
done_when = ""
```
````

The full schema:

````
```agent-claim
version = 1
now = "Current fact"
next = "One concrete next action"
done_when = "Observable terminal condition"

frozen_until = { trigger = "named trigger", ruled_on = 2026-09-06 }

[[expectation]]
text = "An operator sentence"
default = "later"

[[expectation]]
text = "A ruled operator sentence"
ruling = "yes"
ruled_on = 2026-09-06

[[slice]]
index = 4
title = "Block contract in issue bodies"
```
````

`version`, `now`, `next`, and `done_when` are required; `now`/`next`/`done_when`
may be the empty string (an unfilled skeleton — incomplete, but still a valid
block). `frozen_until`, `expectation`, and `slice` are optional; an explicit
`slice = []` is a table intentionally left present but empty (it still counts
as "has a table" for `cut --row`). Each `[[expectation]]` is either *proposed*
(`default = "yes" | "no" | "later"`) or *ruled* (`ruling = "yes" | "no"` with a
TOML date `ruled_on`) — never both, never neither. Per-slice files, done-when,
and dependencies stay in the human prose beside the block; only a slice's
`index` and `title` are typed. Schema and version tokens, and an expectation's
`default`/`ruling` values, are protocol — always this exact English spelling;
every other value (`now`/`next`/`done_when`, `frozen_until.trigger`,
expectation `text`, slice `title`) is the operator's prose and is never
parsed, exactly like prose mode. `next`'s own retained non-parsed vocabulary
(`keiner | keine | nichts | none | -` for "no further work", plus `tbd | todo
| unknown` for "not yet concrete") still applies to a block's `next` value.

An item with no recognized `agent-claim` fence at all is **body legacy**; one
with a recognized fence that is unclosed, duplicated, invalid TOML, or a
schema violation is **body malformed: `<path>: <reason>`** (e.g. `body
malformed: version: version must be exactly 1`). Both fail loud, by name, on
`board`, `next` (`SKIPPED`), and `claim` (`body-legacy` / `body-contract`
checks) — never a guess through the missing or broken block, and a container
in either state is never proposed as `cut_slice` or `close_container`.

**Blockers** come from GitHub's own issue-dependency relations, not a body
line — `Blocked by:` prose beside the block is documentation only and changes
nothing. An open same-repository dependency blocks exactly like a local
`Blocked by` blocker; a foreign `owner/repo#n` blocks the same way and is
named the same way (`blocked by owner/repo#n`, or `#3, owner/repo#n` mixed
with a local one). A same-repository *closed* dependency does not block and
lets `board`'s `FREED` column and `claim` proceed; a closed *foreign*
dependency does not free an item on its own (foreign relations can only
block, never free). Unlike prose, a same-repository pull-request dependency
blocks or frees exactly like any other dependency — `blocker-is-a-PR` is a
prose-only check. **Parentage stays on sub-issues** in both modes; it never
passes through the body.

**`cut`** writes and reads the block the same way it writes and reads the
prose slice table: without `--row` it links the first `[[slice]]` entry when
one exists and otherwise creates an untied child (table or not); `--row N`
selects entry `N` and requires `--title` to equal that entry's own `title`
exactly, refusing before any write on a mismatch. `cut` removes only the
selected entry (`slice = []` after removing the last one) and preserves every
other byte of the body, including CRLF line endings, exactly. The two refusal
strings are shared with prose/#151: `#N has no slice table; --row needs one
to select a row from`, and `#N has no cuttable slice row; 0 malformed rows
need a hand fix` (block mode cannot itself produce malformed rows; the
string is kept so both modes read the same way).

**Migration is a hand edit, not a command.** There is no migration command,
module, or receipt: one reviewed AI session per repository transcribes each
open item's current prose into a block (refusing, by name, anything it
cannot derive rather than inventing it) and keeps the existing prose in place
— GitHub's own edit history is the undo. Forge dependencies are added to
reproduce existing `Blocked by` relations before the pin lands; parentage
needs no migration since it already lives on sub-issues. **Upgrade every
active `agent-claim` installation to a release containing this contract
before a repository sets `body_contract = "block"`** — an older client either
does not know the key (and keeps reading prose blindly) or, once every open
item carries a block, would otherwise see a repository it cannot coordinate
on correctly. From the pin onward, every hand-created issue (`gh issue
create`, an operator-opened item) must carry a valid block — the four-line
skeleton above — or it is `body legacy`; only `cut` writes that skeleton
automatically.

## Cutting a container's next slice

`agent-claim cut <container> --title "…"` dispatches a container's next slice
as a fresh child issue in one step: it creates the issue (native type `Task`),
records it as the container's sub-issue, and, when there is a slice-table row
to link, rewrites the container's slice table so that row now links
`#<child>` instead of the undispatched `—` marker. The fresh child's body is
`board.CHILD_SKELETON` — every contract section present, `Now`/`Next`/`Done
when` empty and `Blocked by: nichts` — so it is `body incomplete` (invisible
to `next`, refused by `claim`) until the head fills it in.

`cut` without `--row` links into the first still-cuttable row when one exists
and otherwise creates an untied child, table or not (#151): a container with
no slice table at all — only a numbered `Next` line, as #122 carried on
06.09.2026 — and one whose table is fully linked but whose own `Next` line
still names further work both cut this way, the container's body left exactly
as it was. `next` never prints `--row`, so a command it prints for a
container is always one `cut` accepts. `--row N` requires a table containing
an uncut row `N` and refuses by name otherwise: no slice table at all, `N`
already linked (`#122 row 4 is already cut (#150); cuttable rows: 5, 6, 7`),
no row left uncut anywhere in the table (`#122 has no uncut row; rows 4-7
are cut`), or no row `N` left cuttable for any other reason, in which case
any malformed rows are named by their `#` cell and reason instead of only
counted (`#79 has no cuttable slice row; row "B": index must be a positive
integer`).

Every refusal precedes every write. `cut` refuses when the forge cannot
create a child issue or update an item body (`capability()` answers anything
but `read_write` for either); when the target is not an open container, or is
itself a child of another issue (nested containers are not supported); and,
for `--row N`, when the table it names does not exist, or has no row `N` left
cuttable — a row already linked to any issue, or one whose item cell is
malformed, is never a target. None of the three writes are atomic with each
other — nor is the child issue's creation atomic with its own sub-issue
relation write inside `create_child` — so a failure at any point after the
child issue exists names the created child and the step that failed, and
instructs a hand fix rather than a re-run, which would create a second child.

## Issueless lane claims

`docs/`- and `fix/`-prefixed branches land within one session without a GitHub
issue. Omit the positional issue number on `claim`/`release` for this lane mode,
derived from the current checkout branch — no separate `--lane` flag. Lane mode
is refused with the offending branch name and both remedies (pass an issue
number, or check out a `docs/`/`fix/` branch) when the branch does not follow
that convention, so a builder who simply forgot the issue number never gets a
silent, unlabeled claim:

```bash
git worktree add ../repo-worktrees/docs-tidy-readme -b docs/tidy-readme
cd ../repo-worktrees/docs-tidy-readme
agent-claim claim --agent "Ada" --scope README.md
agent-claim release --merged 58
```

Like an issue claim, a lane claim must begin from a clean linked worktree
checked out on that branch — `claim` fails outside one.

A lane claim shares the same identity exclusivity, advisory overlap notes, and
release path as an issue claim: two lane claims collide on the same branch;
overlapping scope with another lane or issue is a visible note, not a refusal.
`status` and `protect` show and authorize it the same way. A lane owns no
GitHub issue, so it never appears on `board`, `rulings`, or `next`.

There is no flag to name a lane explicitly on `release`: a lane's only name is
the checkout branch it was claimed from, so releasing it — including a
coordinator override — always runs from a checkout of that same lane branch.
If the original worktree is gone or held by another session, re-create a
worktree on that branch (`git worktree add <path> <lane-branch>`) and run
`agent-claim release --claim-id <id> --coordinator-override --role coordinator
--abandoned "..."` from inside it, where `<id>` comes from `agent-claim status`
(omitting `--claim-id` still filters by the releasing agent, coordinator
override or not, so a foreign stuck claim needs the id).

## Global loader

Run `agent-claim policy --print` and append the block once into the file the
provider actually loads. Skip the append when `<!-- agent-claim-policy:v1 -->`
is already present. Never overwrite an existing loader. The CLI does not write
`~/.claude`, `~/.codex`, or `~/.grok`.

## PreToolUse write gate

Copy this hook once into the file the provider actually loads. Skip when
`Write|Edit|MultiEdit|write|search_replace` is already present. Never overwrite
an existing hook file. The CLI does not write `~/.grok`.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Write|Edit|MultiEdit|write|search_replace",
        "hooks": [
          {
            "type": "command",
            "command": "agent-claim protect",
            "timeout": 60
          }
        ]
      }
    ]
  }
}
```

## v0.5 boundary

GitHub via the `gh` CLI is supported today. Invocations set `NO_COLOR=1`
and `GH_NO_UPDATE_NOTIFIER=1`, strip ANSI from output, and parse pretty or
compact JSON, so a wrapping `gh` shim is not required. The tool does not
automatically allocate work, merge code, or operate a lease server. Omitted `--agent` follows
the documented else-chain; it does not invent an identity. It intentionally
leaves policy-file generation and non-GitHub adapters for a later release.
