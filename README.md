# agent-coordination

`agent-coordination` is a small installable CLI that gives coding agents one
claim state per repository: a compare-and-swap git ref, `refs/aco/state`, on
the repository's own canonical remote. It is provider-neutral: Codex, Claude,
Grok, people, and future agents use the same contract. Its command is `aco`.

## Install and maintain

```bash
uv tool install git+https://github.com/overnightworks/agent-claim.git@v1.0.0
# or: pipx install git+https://github.com/overnightworks/agent-claim.git@v1.0.0
uv tool upgrade agent-coordination
uv tool uninstall agent-coordination
```

To roll back, force-install the previous tag with `uv tool install --force
git+https://github.com/overnightworks/agent-claim.git@v0.13.1`; that build
installs the old `agent-claim` command, not `aco`.

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
operator to upgrade. The one-time import ran on 07.09.2026 and its code has
been deleted: this release reads and writes the state ref only. A repository
that never migrated still runs its old ledger under an installation pinned to
0.12.x or earlier, and has no upgrade path through this release.

## Five-command quick start

```bash
aco bootstrap
aco status
aco claim 42 --agent "Ada" --scope src/widget.py
aco release 42 --merged 57
```

Omitted `--base`/`--branch` bind the current checkout; explicit values must match it.
Omitted `--agent` on `claim` and `release` is filled from non-empty
`ACO_AGENT`, else non-empty `GROK_SESSION_ID` as `Grok {session}`, else
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
base commit, branch, issue, and repository-relative scope. Each `--scope` is
exactly one path, comma and all; a claim with more than one path repeats the
flag (`--scope a --scope b`), never a comma-joined value. A scope is wide when
it declares more than three paths, any directory,
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

`bootstrap` has one job: it creates or reports the state ref. A present ref
is a pure read (prints the ref's commit id, writes nothing); an absent ref
(proven by `git ls-remote --exit-code`, never inferred from a fetch failure)
gets one commit holding an empty state tree (`schema.toml`, `version = 1`)
pushed as a plain fast-forward; an unreachable remote (auth or transport
failure) fails loud instead of either printing or writing. It refuses when
the forge target does not match the canonical remote's own repository.

## Checking one number

`aco check <n>` reads one number of the current checkout's repository (or
`--repo OWNER/REPOSITORY`) and says whether it is sound. It claims nothing,
labels nothing, comments nothing and writes nothing. One forge request
answers whether the number is a pull request, an issue, or in neither number
space, and each answer asks a different question. `--json` prints it as
`{"ok": …, "kind": "pull_request"|"issue"|"missing", "number": n}`, with a
`refused` reason added when `ok` is false. The command needs a working tree of the
repository (a shallow checkout is enough) to read `.agent-claim/board.toml`,
and refuses outright without one.

For a **pull request** it answers: which item does this landing close? It
prints `PR #<n> by <author> declares <classification>` and exits 0, or prints
one `REFUSED: pull request #<n> ...` line and exits 1. Run it as a required
check on every pull request that targets the default branch.

For an **issue** it answers: can a builder start from this body? It prints one
`ISSUE #<n> ...` line — `body ok` and exit 0, or one of `body legacy`,
`body malformed: <reason>`, `body incomplete: <sections>`, or
`blocked by #<a>, #<b>` and exit 1. The blockers are GitHub's own `blocked_by`
dependencies, so a foreign one renders as `owner/repo#n`. A body carries no
dependency key at all, so a body-incomplete line never asks for one. The issue
mode reads nothing else: it repeats none of `claim`'s working-tree, identity,
or ordering checks.

A number that is in **neither** number space prints `REFUSED: #<n> does not
exist in <owner>/<repo>` and exits 1. It names no kind: GitHub gives issues
and pull requests one number space, so an absent number was never proven to
be either.

A pull request body carries exactly one classification line:

- `Work-Item: OWNER/REPO#n` (or `Work-Item: #n` for this repository) together
  with a closing reference for that same item — `Closes #n`, or any other
  keyword GitHub itself closes on, optionally qualified as `OWNER/REPO#n`; or
- `No-Item: docs` or `No-Item: fix` for a lane that owns no issue.

`check` refuses a body with no classification line, with more than one, or
naming two work items (split the pull request); a work item that lives in
another repository; a work item with no active claim
on the pull request's head branch; a closing reference naming anything but the
work item; a `No-Item` pull request without an active issue-less lane claim on
that head branch, or carrying any closing reference at all; a pull request
whose head branch lives in another repository; and a pull request that does not
target the default branch. A classification line inside a fenced code block is
documentation, never a declaration. `Advances #n` is read nowhere: a dispatched
slice is its own item, and its pull request closes it.

Parentage is GitHub's own sub-issue relation, not a line in a body. `check`
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

`aco board` reads the open issues, open PRs, PRs merged since the
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
the same open/total expectation progress. Expectations are the block's own
`[[expectation]]` entries (below): an item is proposed while any entry still
carries a `default`, and ruled once every entry carries a `ruling` with its
`ruled_on` date. A ruled item also shows how many default-branch first-parent
landings (`git log --first-parent` committer times) happened after the oldest
of those dates; ten or more mark it `old`. Missing or proposed expectations
have neither fresh nor old. If git cannot name the default branch, that is an
error, never silently fresh. It never writes GitHub.
The target defaults to the repository of the current checkout;
for another GitHub repository run `aco --repo overnightworks/atelier-2 board`.
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
`idea_label`; an item carrying that label with no Now/Next/Done when
projection ranks normally, and `next` tells the head `Problem neu prüfen und
Item verfeinern`. Once it has a complete contract, its own Next takes over;
without the configured label, a projectionless item remains
`body incomplete: <missing sections>` — the same rendering `claim` and
`check` use, detailed below.
The file defines exactly `priority_labels`, `idea_label`, `body_contract` and
`canonical_remote`; any other key is refused by name (`board configuration
<path> has unknown top-level key priorty_labels`) rather than read past, since
a typo would otherwise leave the setting at its default with nothing saying
so. `body_contract` survives as a known key with a single legal value,
`"block"` (below): an absent key means the same thing, and `"prose"` is
refused by name — `board configuration <path> pins body_contract 'prose':
prose bodies are no longer supported`.
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

`board` also shows an `UNCUT` section naming, per item, the `[[slice]]`
entries still waiting to be dispatched, by index, as `#<item>: rows N, N, …
uncut`. In `board --json` the top-level `uncut` list carries `item` and `rows`,
a list of `{"index", "title"}` objects. This is a finding, never a status
column: `cut` removes an entry the moment it dispatches it, so a dispatched
slice simply leaves the list.

`board` prints a `RECOVERY (close or re-project)` section after `STALE`,
followed by the `CONTAINERS` and `UNCUT` sections above; `next` names recovery
items first with that step: open issues that a merged pull request already
declared as its `Work-Item:` — the landing happened, the bookkeeping did not. It
is keyed on that typed line, never on an issue's update time. `next --json`
carries the same items under `recovery`.

`board` ends its text output with a `requests: N` line, counting every read
the command made through the forge port; `board --json` carries the same
count as a top-level `"requests"` field.

`aco rulings` lists only open board items with open expectation lines
as `#NUMBER OPEN/TOTAL: TITLE`; `rulings --json` returns the same `number`,
`title`, `open`, and `total` values. It is read-only and uses the board's
priority category and score first, then fewer open expectation lines and the
issue number. An empty list succeeds.

Use `aco next` (or `aco next --json`) to name the board's
top-ranked qualifying row — the same `board_rank` order `board` shows.
`next --json` always carries an `action` field, naming one of three shapes
or `null` when nothing qualifies. `work_item`: the row is open, free,
unblocked, not frozen, and has a complete Now/Next/Done when
contract, or is a configured projectionless idea; its text form also prints
`Run: aco claim <n> --scope <paths>` (the literal placeholder
`<paths>`, since the scope cannot be derived) and a line pointing at the
item body for the real paths — the `--json` form is unchanged beyond the
always-present `action` field. `cut_slice`: a container with no open child
still names work in its own `Next` line (`{"action": "cut_slice", "number",
"title", "slice", "cut_title"}`); `slice` is the container's own human step,
`cut_title` is the exact title `cut` accepts (#177: the first uncut
`[[slice]]` entry's title when one exists, else `slice`) — the head cuts
that slice (`aco cut <number> --title "<cut_title>"`) and dispatches
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
same order `board` and `next` use — is free. It also refuses an item that has
at least one open GitHub blocked-by dependency, including a foreign
`owner/repo#n`; a closed same-repository dependency does not count. The
message is `#5 is blocked by #3 (open); pass --out-of-order REASON to claim it
anyway` (a foreign entry renders as `owner/repo#n`), naming every open
blocker. Pass `--out-of-order REASON` to proceed deliberately; it remains
visible as a warning and preserves the reason in the claim comment.

Before it writes a claim, `claim` also reads the pulled issue's live contract
from its block. It refuses with `#<n> body incomplete: <missing sections>`
(block order, e.g. `#150 body incomplete: Now, Done when`) when any of the
three projection keys is empty, unless the issue is a configured
projectionless idea (above) — the same rule and the same rendering `board`'s
`actionable` and `next`'s `SKIPPED` reason use, so a freshly `cut` child (its
`board.BLOCK_CHILD_SKELETON` body has `now`/`next`/`done_when` empty) is named
`body incomplete: Now, Next, Done when` everywhere until the head fills it
in. The check does not limit body size or inspect references in `next`, and
`release` stays available even when the body's contract has since become
invalid.

## The work-item body contract

`board`, `next`, issue-mode `claim`, `cut`, `rulings`, and the parent-body
part of `check` read a work item's `Now`/`Next`/`Done when`, freeze,
expectations, and undispatched slices from one typed `agent-claim` fenced TOML
block. That block is the whole grammar: the human prose around it — including
another tool's own section headings in the same body — is never parsed.

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
expectation `text`, slice `title`) is the operator's own prose and is never
parsed. `next`'s own non-parsed vocabulary
(`keiner | keine | nichts | none | -` for "no further work", plus `tbd | todo
| unknown` for "not yet concrete") still applies to a block's `next` value.

An item with no recognized `agent-claim` fence at all is **body legacy** —
"no block was found", never "some other grammar was found instead"; one
with a recognized fence that is unclosed, duplicated, invalid TOML, or a
schema violation is **body malformed: `<path>: <reason>`** (e.g. `body
malformed: version: version must be exactly 1`). Both fail loud, by name, on
`board`, `next` (`SKIPPED`), and `claim` (`body-legacy` / `body-contract`
checks) — never a guess through the missing or broken block, and a container
in either state is never proposed as `cut_slice` or `close_container`.

**Blockers** come from GitHub's own issue-dependency relations, never a body
line — `Blocked by:` prose beside the block is documentation only and changes
nothing. A foreign `owner/repo#n` blocks exactly like a same-repository
dependency and is named the same way (`blocked by owner/repo#n`, or `#3,
owner/repo#n` mixed with a local one). A same-repository *closed* dependency
does not block and lets `board`'s `FREED` column and `claim` proceed; a closed
*foreign* dependency does not free an item on its own (foreign relations can
only block, never free). A pull-request dependency blocks and frees exactly
like any other dependency. **Parentage stays on sub-issues**; it never passes
through the body.

**`cut`** reads and rewrites the block: without `--row` it links the first
`[[slice]]` entry when one exists and otherwise creates an untied child;
`--row N` selects entry `N` and requires `--title` to equal that entry's own
`title` exactly, refusing before any write on a mismatch. `cut` removes only
the selected entry (`slice = []` after removing the last one) and preserves
every other byte of the body, including CRLF line endings, exactly. A `--row`
against a block with no `slice` key at all refuses `#N has no slice table;
--row needs one to select a row from`; a `--row N` that names no entry refuses
`#N has no row <N>; cuttable rows: <list-or-none>` — a linked entry is removed
from the block the moment it is cut, so every entry still in `[[slice]]` is
cuttable, and the refusal lists them all.

Every hand-created issue (`gh issue create`, an operator-opened item) must
carry a valid block — the four-line skeleton above — or it is `body legacy`;
only `cut` writes that skeleton automatically.

## Cutting a container's next slice

`aco cut <container> --title "…"` dispatches a container's next slice
as a fresh child issue in one step: it creates the issue (native type `Task`),
records it as the container's sub-issue, and, when there is a `[[slice]]`
entry to link, removes that entry from the container's block. The fresh
child's body is `board.BLOCK_CHILD_SKELETON` — every projection key present
and empty — so it is named `body incomplete: Now, Next, Done when` (invisible
to `next`, refused by `claim`) until the head fills it in.

`cut` without `--row` links the first `[[slice]]` entry when one exists and
otherwise creates an untied child (#151): a container with no `slice` key at
all — only a numbered `Next` line, as #122 carried on 06.09.2026 — and one
whose list has been emptied but whose own `Next` line still names further work
both cut this way, the container's body left exactly as it was. `next` never
prints `--row`, so a command it prints for a container is always one `cut`
accepts. `--row N` requires a block carrying entry `N` and refuses by name
otherwise: no `slice` key at all (`#122 has no slice table; --row needs one to
select a row from`), or no such entry left (`#79 has no row 9; cuttable rows:
1, 2`).

Every refusal precedes every write. `cut` refuses when the forge cannot
create a child issue or update an item body (`capability()` answers anything
but `read_write` for either); when the target is not an open container, or is
itself a child of another issue (nested containers are not supported); when
the target's own body is legacy or malformed; and, for `--row N`, when the
block names no such entry. None of the three writes are atomic with each
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
aco claim --agent "Ada" --scope README.md
aco release --merged 58
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
`aco release --claim-id <id> --coordinator-override --role coordinator
--abandoned "..."` from inside it, where `<id>` comes from `aco status`
(omitting `--claim-id` still filters by the releasing agent, coordinator
override or not, so a foreign stuck claim needs the id).

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
            "command": "aco protect",
            "timeout": 60
          }
        ]
      }
    ]
  }
}
```

## Refusals and `--json`

Every command's own refusal ends the same way: a `ClaimError` (a forge
failure, a claim conflict, a missing state ref, too wide a scope, a `cut`/
`release`/`rescope` refusal, a `check` without a checkout, and more) reaches
one collection point that prints `ERROR: <sentence>` to stderr and exits 2.
For a command whose parsed arguments carry a requested `--json`, that same
point additionally prints `{"ok": false, "error": "<the same sentence>"}` to
stdout -- so a scripted `--json` caller reads a machine-readable refusal on
the stream it already parses, without a script watching stderr too. No new
vocabulary: this is the same `ok`/`refused` discriminator `check` already
uses, not an error code, a retryable flag, or a mutation-state field. A
command without a `--json` option (`bootstrap`, `protect`) is unaffected;
`protect` already prints JSON on every outcome through its own error path
and this collection point never runs for it. **Deliberate boundary**: an
argparse failure (a missing issue number, an unknown flag) happens before any
command -- and therefore any `--json` -- is known, and still only prints
argparse's usage text to stderr with nothing on stdout.

Read the exit status first and parse second: this object shares stdout with
whatever ordinary payload the command would otherwise have printed, so a
reader that parses stdout without looking at the status reads a refusal as a
successful answer. The rule that holds even for a case this paragraph forgets:
**on a non-zero status, expect an object whose shape belongs to the command
that produced it**, and read the sentence out of `error` only when this
collection point is what produced it. Every other refusal object has its own
keys — `claim --json` refuses with `{"refused": true, "issue": …, "checks":
[…]}`, `check --json` with `{"ok": false, "kind": …, "number": …, "refused":
…}`, `protect` denies with `{"decision": "deny", "reason": …}`, and `status`
reports a claim conflict with its ordinary payload. A non-zero status need not
mean a refusal at all: `next` exits 3 with its ordinary `{"action": null, …}`
payload when nothing is actionable. The codes: 0 succeeded; 1 is `check`'s own
refusal; 2 is this collection point, plus `claim`'s refusal, `protect`'s deny,
`status`'s conflict, and argparse's usage failure (which writes nothing to
stdout at all); 3 means only that `next` had nothing to name.

## Scope and boundaries

GitHub through the `gh` CLI is the one adapter that exists. A second forge
attaches at the port — `ForgeReader`/`ForgeWriter`, with a `Capability` answer
per operation — and not in the commands; the GitHub adapter itself refuses no
operation. Invocations set `NO_COLOR=1` and `GH_NO_UPDATE_NOTIFIER=1`, strip
ANSI from output, and parse pretty or compact JSON, so a wrapping `gh` shim is
not required. The tool does not automatically allocate work, merge code, or
operate a lease server. Omitted `--agent` follows the documented else-chain; it
does not invent an identity. It writes no file outside the repository's own git
directory: no provider configuration, and never `~/.claude`, `~/.codex`, or
`~/.grok`.
