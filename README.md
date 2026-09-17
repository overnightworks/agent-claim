# agent-coordination

`agent-coordination` is a small installable CLI that gives coding agents one
claim state per repository: a compare-and-swap git ref, `refs/aco/state`, on
the repository's own canonical remote. It is provider-neutral: Codex, Claude,
Grok, people, and future agents use the same contract. Its command is `aco`.

## Install and maintain

```bash
uv tool install git+https://github.com/overnightworks/agent-claim.git@v2.0.0
# or: pipx install git+https://github.com/overnightworks/agent-claim.git@v2.0.0
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
`resources/<name>.toml` / `items/<id>.md` under a `schema.toml` `version`,
read and written whole: an unknown key or a malformed record fails the whole
read loud, never a single quarantinable claim -- a commit is the unit a
writer writes, so a broken tree is corrupt state. `schema.toml` carries the
one supported version; a tree written under any other version is refused,
never silently patched (operator ruling 16.09.2026: no backwards
compatibility).

## Five-command quick start

```bash
aco bootstrap
aco status
aco claim 42 --agent "Ada" --scope src/widget.py
aco release 42 --merged 57
```

## Recovering a provider workspace

`aco register` is a deliberate, local handoff for a stopped native provider
conversation or an explicitly selected running native Codex or Claude resume. It records the mapping at
`${XDG_CONFIG_HOME:-~/.config}/aco/workspace.toml`; it does not alter the
conversation, repository files, or its live claim. A stopped handoff needs the
acknowledgement because `aco` cannot prove whether an arbitrary unmanaged provider
process is still running; live registration instead validates the selected process.

```bash
aco register my-project --path /work/my-project \
  --session-id 123e4567-e89b-12d3-a456-426614174000 \
  --agent "Codex workspace head" --stopped
aco run my-project
```

Use `--provider claude` or `--provider grok` to register a stopped native UUID;
omitted `--provider` keeps the existing Codex default. For Grok, `--path` must be
the conversation's original native workspace and still exist. `aco` passes its
canonical form as `grok --resume UUID --cwd PATH`, but does not validate, copy, or
edit provider history and cannot relocate that UUID. A different path may retain
Grok's original workspace; that unsupported mismatch is not preflight refused.
`aco` never passes `--restore-code`. To register a running Codex or Claude conversation
without interrupting it, pass its exact native process ID instead of `--stopped`:

```bash
aco register my-project --path /work/my-project \
  --session-id 123e4567-e89b-12d3-a456-426614174000 \
  --agent "Codex workspace head" --live-pid 12345
```

ACO validates the same-user native executable, exact resumed UUID, canonical working
directory, and stable process birth before writing a receipt. It never stores command
arguments. At `aco run` or login recovery it leaves an exact live original or manual
replacement untouched; uncertain ownership also blocks a launch. Run a manual native
resume and recovery serially: they do not share a provider lock. An unmanaged live
conversation is preserved but cannot be attached to a new ACO console.

The local mapping upgrades additively to version 3 when a new registration succeeds,
preserving every existing project field with an explicit provider. Older installations
refuse version 3 rather than silently dropping the external-process receipt; an older
provider-aware installation refuses an unsupported Grok record rather than
silently dropping provider identity; native provider resume remains available
independently.

`aco run` works outside a Git checkout and never accepts `--repo`. It resumes
the registered UUID in a dedicated local tmux socket and opens one GNOME
Terminal console for the project. Repeating it reuses an attached managed
head, reattaches a detached one, and retries a preserved exited pane only on
that explicit command. The managed process receives the registered `ACO_AGENT`
identity while retaining the current provider authentication and configuration
channels; it does not copy credentials or alter provider permissions. Claude
uses the caller's existing `CLAUDE_CONFIG_DIR` when set and its normal home when
unset; `aco` does not select an account or search provider homes.

When a desktop console later disappears, the next `aco run` reopens the same
detached managed head. If GNOME Terminal never accepts an attachment, `aco run`
reports that failed launch once; the following explicit run can try the same
head again. A still-opening or accepted console remains the sole owner until
its GNOME wrapper exits, so concurrent runs do not create another viewer.

If an older installation reports unowned viewer-pending metadata, do not clear
it while a console may still be attaching. First confirm the named target has
no attached client (`tmux -S "$XDG_RUNTIME_DIR/aco/tmux.sock" display-message
-p -t aco-my-project '#{session_attached}'` prints `0`) and confirm that no
earlier GNOME Terminal launch for that project remains in progress. Only then
clear that stale value with `tmux -S "$XDG_RUNTIME_DIR/aco/tmux.sock"
set-option -t aco-my-project @aco_viewer_pending ""`, then run `aco run
my-project` again.

Claude's existing `claude-revive` SessionStart hook remains a separate recovery
owner. Do not use both recovery paths for the same conversation; its replacement,
enrollment, and retirement remain with #213.

## Restoring the configured workspace at desktop login

`aco login enable` creates one owned desktop-login launcher at
`${XDG_CONFIG_HOME:-~/.config}/autostart/aco-workspace.desktop`. It first reads
the whole workspace mapping and refuses an absent, malformed, or empty mapping;
it never overwrites a launcher it cannot prove it created. Repeating enable is
safe, and refreshes the installed Python path after an upgrade. `aco login disable`
removes only that owned launcher, leaving the mapping, provider history, files,
claims, and surviving tmux heads intact. `aco login status` reports launcher and
mapping state separately, plus the most recent login attempt, without starting a
provider or writing a file.

The installed Python path must not contain `%`. ACO refuses that path before it
creates or replaces a launcher; move the installation to a percent-free path and
enable login again.

Enable also requires `${XDG_CONFIG_HOME:-~/.config}/autostart` to be a normal
directory owned by the user with no group or other permission bits. If an existing
directory you own is more permissive, run `chmod 700 "${XDG_CONFIG_HOME:-$HOME/.config}/autostart"`;
the same user's desktop session can still read its autostart entry.

Login recovery is desired-workspace mode: every configured project is resumed in
configuration order at each desktop login, including one that was previously
exited. It restores the existing native UUID through the normal promptless
provider command and stops at the native input line; it never sends a prompt,
copies authentication, or watches and restarts a same-boot process. Use `aco run`
for an explicit same-boot retry.

The login runner replaces one bounded record at
`${XDG_STATE_HOME:-~/.local/state}/aco/login-attempt.json`. It records only an
aco attempt identifier, times, completion state, project keys, and public outcome
classes. An interrupted run remains visibly unfinished; a failed project does not
hide later configured projects. The record contains no provider UUID, workspace
path, agent, model, configuration, environment, command line, or raw error.

Omitted `--base`/`--branch` on `claim` bind the current checkout; explicit
values must match it. `release --branch` is the one exception: it selects the
claim by that branch name without requiring the checkout to be on it (see
"Issueless lane claims" below).
Omitted `--agent` on `claim` and `release` is filled from non-empty
`ACO_AGENT`, else non-empty `GROK_SESSION_ID` as `Grok {session}`, else
non-empty `CLAUDE_SESSION_ID` as `Claude {session}`. `GROK_AGENT` is not a name.
Missing or present-invalid identity fails closed before any write. Omitted
`--role` on `claim` is `builder`; an explicit `--role` wins. Repeating an
interrupted `claim` for the same active item, agent, role, branch, and scope,
with the same claim id, returns that active claim instead of writing a second
one. A different live claim still fails; a released claim id remains terminal.

Omitted `--claim-id` on `release` selects the unique active claim on that issue
or lane whose agent is this session and whose branch is the current checkout,
or the branch named by an explicit `--branch`; otherwise it fails closed.
`--branch` and `--claim-id` naming different claims are refused, naming both
values, rather than silently preferring one.
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
A successful `--merged` release then reads the board once, lazily, to name
what it freed: `freed: #a, #b` (or `freed: none`) for every open item whose
last open blocker was this landing, and `next: #n score s: <title>` (or
`next: none`) for the same pick `aco next` would make; `--json` carries these
as `freed` (a list of numbers) and `next` (a number or `null`). `--abandoned`
never reads the board and reports neither. A forge failure after the release
has already committed never undoes or fails it -- one hint line stands in for
`freed`/`next` instead, naming the repair.
`rescope <issue> --add <path> [--drop <path>]` changes a live claim's scope
without releasing it: the claim id and base stay, added paths are advisory
like `claim`, and a resulting wide scope uses the same `--whole` rule as
`claim`. There is no release window. It does not require HEAD to match
base or a clean tree.

Run commands in the repository being coordinated, or pass `--repo
OWNER/REPOSITORY`. A claim must begin from a clean linked worktree and binds its
base commit, branch, issue, and repository-relative scope. Each `--scope` is
exactly one path, comma and all; a claim with more than one path repeats the
flag (`--scope a --scope b`), never a comma-joined value. A comma-bearing value
that matches no versioned file is refused before the claim is written, naming
the one-path-per-flag rule -- the signature of a comma-joined value passed by
habit, which would otherwise store a path that guards nothing; a real
comma-bearing filename, and any comma-free path not created yet, still claim
cleanly. `rescope --add` applies the same refusal to its own values; `--drop`
never does, since a value the live claim already holds is a fact about the
claim, not a typo about the checkout, and dropping a value the claim does not
hold is already refused by its own truer reason. A scope is wide when
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
`status --path <path>` prints every live claim that holds a path; it computes no age and so
checks no claim's ancestry (the fetched tip itself stays guarded on every read).
Agents should read `--json` from `status`, `claim`, `release`, and `rescope`.
`status` prints each live claim's age from its `opened_commit`'s committer date
(the state-ref commit that first introduced it) as `Xh Ym`, and marks it `old`
after more than one hour. `status --json` also carries `tip`, the fetched
state ref's own oid (`null` when the repository has no ref yet), so a monitor
can poll a cheap `ls-remote` for whether the fleet moved without re-reading
every claim; the human `status` text is unchanged.

`claim`, `rescope`, `release`, and `status` read and write exclusively through
`refs/aco/state` -- a compare-and-swap git ref on the repository's configured
`canonical_remote` (`.agent-claim/board.toml`, default `origin`), invisible in
the GitHub UI and never checked out into a working tree. No command but
`bootstrap` ever creates that ref, so a `claim`/`rescope`/`release`/`protect`
call against a repository that has not been bootstrapped refuses by name
instead of silently creating it. `release --coordinator-override` is for an
explicit coordinator action; a stale takeover is that same override-release
followed by an ordinary `claim` -- two commits, no separate verb, and the new
claim never reuses a resource integer the released claim held. The number of
git invocations a transition or a state read costs is fixed, independent of
how many claims the state tree holds.

`bootstrap` has one job: it creates or reports the state ref. A present ref
is a pure read (prints the ref's commit id, writes nothing); an absent ref
(proven by `git ls-remote --exit-code`, never inferred from a fetch failure)
gets one commit holding an empty state tree (`schema.toml`, `version = 2`)
pushed as a plain fast-forward; an unreachable remote (auth or transport
failure) fails loud instead of either printing or writing. It is forge-free
(see "Scope and boundaries" below), so `--repo` and the canonical remote's
own repository never enter into it.

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
`ISSUE #<n> ...` line — `body ok` and exit 0, or one of
`body malformed: <reason>`, `body incomplete: <sections>`, or
`blocked by #<a>, #<b>` and exit 1. A body with no recognized `agent-claim`
block renders as `body malformed: agent-claim: no agent-claim block`. The
blockers are GitHub's own `blocked_by` dependencies, so a foreign one renders
as `owner/repo#n`. A body carries no dependency key at all, so a
body-incomplete line never asks for one. The issue mode reads nothing else: it
repeats none of `claim`'s working-tree, identity, or ordering checks.

`body malformed` and `body incomplete` are the same sentences a body can
already earn before it ever becomes an issue: run `aco body --check` ("The
work-item body contract" below) as the forge-free pre-flight, so a draft fails
here instead of after `gh issue create` has already written it.

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

## Briefing a lane step's dispatch

`aco brief <item>` composes one item's own body, live claim, lane tip, and
touched files into the one artifact a dispatch brief is built from -- the
reads `status`, `check`, and `checkout` already make, never a new data source,
and never a write. It prints, in this fixed order: the item's body, read
whole from the forge; its live claim (agent, role, branch, base, scope,
`whole`, and age) or `no active claim`; the claim branch's current tip
(`git rev-parse`, local then `origin/`, or `branch not found`); and the files
the lane branch touches against its base (`git diff --name-only base..tip`).
Without a live claim the tip and touched-files sections are empty; with a
claim whose branch resolves to neither a local nor an `origin/` ref, the tip
section prints `branch not found` and touched files stays empty. `--json`
prints one object, `{"body", "claim", "tip", "touched"}`, with `claim` `null`
when the item carries none.

`brief` is a forge command like `check` and `board`: without a GitHub remote
under the `canonical_remote` pin it refuses the same way `board` does, before
ever calling `gh`. The head pastes its output straight into a dispatched lane
step's brief -- the body, the lane tip, and the touched files a fresh
delegated agent needs -- instead of assembling it by hand from `gh issue
view`, `git rev-parse`, and `aco status`.

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
The file defines exactly `priority_labels`, `idea_label`, `body_contract`,
`canonical_remote`, and `storage`; any other key is refused by name (`board
configuration <path> has unknown top-level key priorty_labels`) rather than
read past, since a typo would otherwise leave the setting at its default with
nothing saying so. `body_contract` survives as a known key with a single
legal value, `"block"` (below): an absent key means the same thing, and
`"prose"` is refused by name — `board configuration <path> pins body_contract
'prose': prose bodies are no longer supported`.

### HTML board page

`aco board --html [PATH]` (issue #276, parent #234) writes a static HTML
page from exactly the reads `board` already performs — no second `gh` call,
no clock, no randomness — to `PATH`, or to stdout when `PATH` is omitted.
`--json` and `--html` are mutually exclusive. Four sections in fixed order:
"Wartet auf dich" (every still-open `[[expectation]]` line as a card, in the
operator's own words when `aco ask` gave them (issue #295) — `question` as
the heading, the inline-SVG `picture`, `example` under a "Beispiel" tag,
else `text` alone as the heading, as before — then a copyable
`aco rule <item> --line N --yes` / `--no` / `--later` command per outcome,
with a copy button, the page's only JavaScript, and the full `text`
disclosed under "Der volle Satz" whenever a `question` shortened the
heading), "Lanes" (active claims — agent, role, branch, age — with the
item's own Now/Next/Blocked by/Done when verbatim from the body), "Themen"
(containers with their open children and closed/total progress, then
standalone items), and "Landungen" (items `board` already classified
`Stage.CODE_LANDED`, paired with the merged pull request that closes or
declares them when `board.closing_references`/`declared_work_items`
resolves one — or the one "nicht ableitbar" line when `landings_derivable`
is false). An empty section still renders its heading and "nichts". The
page carries the mockup's design tokens for light and dark and stays
narrow-width safe; the same board state renders the identical page
byte-for-byte regardless of which head writes it.

### Served board page

`aco board --serve [--port PORT]` (issue #280, parent #234) serves the same
page on a stdlib `http.server.ThreadingHTTPServer` bound to `127.0.0.1`
only — never `0.0.0.0` — on `PORT` (default `0`, an ephemeral port). It is a
write command, so it goes through the same writer the rest of this document
calls the writer session; `--serve` refuses together with `--html` or
`--json`. On start it prints exactly one line, the page's URL with a fresh
`secrets.token_urlsafe(32)` token in the query string
(`http://127.0.0.1:<port>/?t=<token>`), and keeps running until Ctrl-C.

Exactly two routes exist. `GET /?t=<token>` renders the page fresh for every
request — the same reads and the same renderer `--html` uses, so nothing
caches — with `Cache-Control: no-store`; every open `[[expectation]]` card
carries exactly one `POST /rule` form (issue #295) with one note field that
goes to `aco rule`'s own `--note`, and three `yes`/`no`/`later` submit
buttons inside it (the item's own default marked). `POST /rule` (fields `t`,
`item`, `line`, `outcome`, `note`) rules exactly one line through the same
write path `aco rule` uses and answers `303` back to `/?t=<token>`; a refusal
(an already-ruled line, an out-of-range one, ...) writes nothing and shows
as a sentence on the reloaded page instead of a stack trace. A missing or
wrong token — compared with `hmac.compare_digest` — answers `403` with no
page and no write, on either route; every other path answers `404`. Static
`--html` is unchanged: it keeps the copyable `aco rule` command lines, no
server, no token.

### Storage pin

`storage = "github" | "state-ref"` (default `github`) names which adapter
owns this repository's board and item data (issue #248, parent #230). Under
`github` (every repository today), items are GitHub issues, exactly as
described throughout this document. Under `state-ref`, an item is instead a
file `items/<id>.md` in the tree of `refs/aco/state` — its id the file name,
`aco-` plus six lowercase hex characters, never a block key — and blockers
and parentage live in that file's own nested `[record]` table
(`title`/`state`/`kind`/`labels`/`blocked_by`/`parent`/`origin`/
`created_at`/`updated_at`/`closed_at`) instead of GitHub's native relations;
`record` is refused as an unknown key under `storage = "github"`, so the two
storages never both claim the same fact. `board`, `next`, `rulings`, `check`,
and `status` all read a `state-ref` repository fully, including one without
any GitHub remote at all, and `default_branch` is read from the checkout's
own `origin/HEAD` rather than an API call. `--repo` is meaningless under
`state-ref` and is refused by name.

`cut`, `rule`, and `ask` write straight into `items/<id>.md` (issue #283):
one compare-and-swap `commit_transition` per write, no `gh`, no forge — a
second writer starting from the same already-read item refuses by name
("written since it was read") rather than overwriting it. `cut`'s fresh
child gets a minted id (`aco-` plus six lowercase hex characters, refused
after three collisions rather than silently widened) with its
`record.parent` set in that same write; `link_child` is a no-op, since
state-ref parentage has exactly one owner. The container's own cut
`[[slice]]` row is then removed byte-exact through that same
`update_item_body` CAS (issue #291): a container changed since it was read
refuses with the same "written since it was read" sentence, the just-created
child stays, and an identical re-run adopts it — matched by `record.parent`,
never the `Parent:` prose line GitHub's own orphan recovery reads — instead
of minting a second one. An issue-scoped `claim`'s body
check reads a `state-ref` item the same way `board`/`next` already do.
`release --merged` still refuses — state-ref cannot yet verify a merged pull
request (#230 slice 6) — naming the offline path instead: land with
`item close` plus `release --abandoned "landed as <sha>"`.

`aco item new --title TITLE [--kind task|feature|container] [--parent ITEM]`
creates a fresh item straight in `refs/aco/state` — a task/feature skeleton
(`--kind container` writes the container skeleton instead) plus a `[record]`
naming its kind and, with `--parent`, its parent — through the same one CAS
write `cut`'s own `create_child` performs, and prints exactly one line, the
minted id (`--json`: `{"item": "aco-xxxxxx", "number": n}`). It refuses under
`storage = "github"` by name ("items live on the forge; open the issue
there") — aco is pulled from the forge, never governs it, so it never opens a
GitHub issue itself. `aco item show ITEM [--json]` prints one header line —
`aco-xxxxxx · #n · open|closed · parent aco-…|none` — followed by the stored
body byte-exact; it reads through the ordinary forge port, so it works under
both storages (a state-ref item's own file, or a GitHub issue's body), shows
a closed item exactly like an open one (closing never deletes), and refuses
an unknown id by name.

`aco item edit ITEM` reads the whole new body from stdin only (`aco item edit
ITEM < body.md`; no `--file`, no editor) and refuses before any write when it
carries no valid `agent-claim` block — the same sentences `aco body --check`
reports (issue #287). The stored body is replaced with the piped one; only
`[record]` is composed by aco itself: `parent`, `state`, `origin`,
`created_at`, and `closed_at` come from the item's own already-stored record —
a value the piped body's `[record]` names for one of them is silently
overwritten, never refused — `updated_at` always moves to now, while `title`,
`labels`, and `blocked_by` come from the piped record when it carries one (an
omitted key keeps the stored value). The compare-and-swap `expected` oid is
this process's own already-read snapshot, never a re-read; a second worktree
writing from that same snapshot refuses with the same "written since it was
read" sentence `cut`/`rule`/`ask` already use. Prints one line, `EDITED
aco-xxxxxx` (`--json`: `{"item", "number", "oid"}`). It refuses under `storage =
"github"` by name ("forge issues are edited on the forge; aco never governs
them") — a forge issue is edited on the forge, never through aco.

`aco item close ITEM [--json]` closes a state-ref item: `state` moves to
`"closed"` and `closed_at`/`updated_at` move to now, the item file itself and
every other byte stay untouched (issue #289) — closing never deletes, exactly
like `item show`'s own closed-item proof. The compare-and-swap `expected` oid
is this process's own already-read snapshot, the same discipline `item edit`
uses; a second worktree writing from that same snapshot refuses with the same
"written since it was read" sentence. Prints one line, `CLOSED aco-xxxxxx`
(`--json`: `{"item", "number", "closed_at"}`), then a `freed:` line in
`release --merged`'s own form naming every open item whose only open local
blocker was this one. It refuses: a second close on an already-closed item,
naming the date it closed on; an unknown id; an item still carrying a live
claim ("release the claim first" — a closed item with a live claim would be
the `RECOVERY` anomaly the board already guards against); and, under `storage
= "github"`, by name ("the forge closes its issues; aco never governs
them") — the forge closes its own issues, never aco.

Every command that takes an item — `claim`, `cut`, `ask`, `rule`, `check`,
`brief`, `body --parent`, `status`, `rescope`, and `release` — accepts it as
`aco-xxxxxx`, `#n`, or the bare number `n`: an id is identity, not only
display, so the id `item new` prints is something every other command can
claim right back, on a `state-ref` repository or a `github` one alike.
Landings are not yet derived from the state ref either (#230 slice 6), so
every `state-ref` item's stage is either `IN_FLIGHT` (an active claim on an
open branch) or `TEXT_ONLY`; `CODE_LANDED` and the board's recovery section
stay empty until that slice lands.
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

`aco ask <item> --text TEXT [--default yes|no|later] [--question TEXT]
[--example TEXT] [--picture FILE.svg]` appends one fresh *proposed*
`[[expectation]]` entry to `<item>`'s block (default `yes`). `--question`,
`--example`, and `--picture` (issue #295) are optional card fields a card
renderer shows in place of `text`: `--question` one operator-language
sentence, at most 160 characters; `--example` one operator-language
sentence; `--picture` a path to an inline-SVG file, read and validated
before any write -- at most 8 KiB, rooted at `<svg`, no `<script`, no
external `href="http` reference -- refused by name otherwise. It refuses by
name when `<item>` has no valid `agent-claim` block to append to (`aco
check <item>` shows the exact defect) and when `--text` is empty. `--json`
returns `item`, `index` (the new line's 1-based, block-order position),
`text`, `default`, and whichever of `question`/`example`/`picture` were
given.

`aco rule <item> --line N (--yes | --no | --later) [--note TEXT]` rules the
`N`-th (1-based, block order — the same index `rulings` prints) *proposed*
`[[expectation]]` entry: its `default` falls, `ruling` and today's UTC date
(`ruled_on`) take its place — `--later` is a genuine ruling, an explicit
operator decision to defer, not only a proposer's guessed default.
`--note TEXT`, when given, is appended to the line's own text as
` Anmerkung: TEXT` — the schema has no separate note field. It refuses an
already-ruled line by name before any write (a changed ruling is a new
line, never an overwrite) and a `--line` outside the item's expectation
lines, naming the range. `--json` returns `item`, `index`, `ruling`,
`ruled_on`, and `open` (how many of the item's lines are still open).

`aco rulings` lists every open board item that still carries an open
expectation line, and under it every one of that item's `[[expectation]]`
lines by index, state (`open`, or `ruled <ruling> <ruled_on>`), and text
(truncated to one line for the human form; `--json` carries the full text).
`rulings --json` returns the same `number`, `title`, `open`, and `total`
values as before, plus a `lines` array of `{index, text, state}` objects,
each carrying `question`/`example`/`picture` too when the line has them
(issue #295); the human form keeps printing only `text`.
It is read-only and uses the board's priority category and score first,
then fewer open expectation lines and the issue number. An empty list
succeeds.

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
still carries an undispatched `[[slice]]` row (`{"action": "cut_slice",
"number", "title", "slice", "cut_title"}`); `slice` is the container's own
human step — its `Next` line when that still names work, else the row's own
title — `cut_title` is always the first uncut row's title, the exact title
`cut` accepts (#177) — the head cuts that slice
(`aco cut <number> --title "<cut_title>"`) and dispatches it. `close_container`:
a container with no open child and no uncut slice row, so there is nothing
to cut (`{"action": "close_container", "number", "closed", "total",
"next_step"}`); `next_step` is the container's own `Next` sentence when it
still names real work that is not a slice, or `null` when it names none —
either way no command is proposed, because a container's prose is not a
slice title (#208). The head closes the container only when `next_step` is
`null`; otherwise it reads that sentence. A container is never
itself the `work_item` target. Pulling is not dispatching, so unruled
expectations never withhold a `work_item`; the pulled item carries
`expectations unruled: refine before the pull` instead, and an item
ruled long ago carries `ruled N landings ago: refine again at the pull`
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

### What is still different under `state-ref`

Under `storage = "state-ref"`, `board` (its plain-text table, including the
`READY NOW`/`STALE`/`RECOVERY`/`CONTAINERS` lines), `next`, `status`,
`rulings`, `release`'s `freed:`/`next:` lines, `item close`'s `freed:` line,
and `board --html`'s cards/topics/lanes print an item as `aco-xxxxxx`
(`items.format_item_id`) instead of GitHub's `#n` — the same id `item new`
mints and every item-taking command already accepts back (`aco-xxxxxx`,
`#n`, or the bare number). What stays `#n` even where that chooser
applies (named residuals, issue #292): the number in a refusal sentence
(`protocol`/`cli`) — it names the number the caller typed, not a display
choice; the branch and worktree naming scheme (`issue-<n>-<slug>`), which the
coordination contract itself keys by number — renaming it is a rule change
under the Rule-Gate; `next`'s own tie-break on the numeric id, stable but
arbitrary, judged again only after a week of real use; and a merged
release's own board read, which `state-ref` cannot perform yet (#230 slice
6) — land offline with `item close` plus `release --abandoned "landed as
<sha>"` until then.

### A week without a forge

A repository with a `file://` remote and no GitHub coordinates a whole week
of work out of `refs/aco/state` alone. The bare remote is named `origin`,
with `origin/HEAD` set — aco refuses without it — and
`.agent-claim/board.toml` carries exactly `storage = "state-ref"`; `aco
bootstrap` then creates `refs/aco/state` once, at an empty state tree (a
second run is a pure read of the tip already there):

```bash
git init --bare -b main /srv/aco/repo.git
git remote add origin file:///srv/aco/repo.git
git push origin main
git remote set-head origin main
printf 'storage = "state-ref"\n' > .agent-claim/board.toml
git add .agent-claim && git commit -m "pin state-ref storage"
aco bootstrap
```

Day one cuts the epic and its first slice. `aco item new --kind container
--title "…"` mints the container and prints its id alone; `aco body
--template --kind container` prints the skeleton body, `aco body --check`
verifies a filled-in copy before it is piped in, and `aco item edit
<container-id> < body.md` writes it with `Now`/`Next`/`Done when` filled.
A first child comes from `aco item new --title "…" --parent <container-id>`
(an untied slice); once the container's own body later carries `[[slice]]`
rows, later ones come from `aco cut <container-id> --title "<row title>"`
instead. A freshly minted child's body is still the bare skeleton, so it
needs its own `aco item edit <child-id> < body.md` before anything can claim
it — `claim` refuses an incomplete `Now`/`Next`/`Done when` by name, the
same check `board`/`next` already report.

Every following day repeats one loop: `aco board` or `aco next` names the
next item, and `aco claim <item> --scope <paths>` opens the build from a
linked isolated worktree. Once a build lands offline, there is no pull
request for `release --merged` to verify (see above), so the claim is
released first — `aco release --abandoned "landed as <sha>"` — and only then
does `aco item close <item>` close it: a still-claimed item refuses `item
close` by name ("release the claim first"), so closing an item is always the
loop's last step, never its first. `aco status` shows the claim gone.

```bash
aco claim aco-yyyyyy --scope src/widget.py
# build, push, merge by hand
aco release --abandoned "landed as <sha>"
aco item close aco-yyyyyy
aco status
```

An expectation line rides the same items: `aco ask <item> --text "…"`
proposes one, `aco rule <item> --line N (--yes|--no|--later)` records the
operator's word, and `aco rulings` lists every item that still carries an
open one. Nothing above ever reaches a forge — the week runs entirely
against this repository's own `refs/aco/state`.

## The work-item body contract

`board`, `next`, issue-mode `claim`, `cut`, `rulings`, and the parent-body
part of `check` read a work item's `Now`/`Next`/`Done when`, freeze,
expectations, and undispatched slices from one typed `agent-claim` fenced TOML
block. That block is the whole grammar: the human prose around it — including
another tool's own section headings in the same body — carries no board
contract and is never read by `board`, `next`, `claim`, or `rulings`. The one
exception is `cut`'s own `Parent: #<n>` line (below): a recovery marker only
`cut`'s orphan adoption reads back, so a failed relation write can be
finished by re-running the same `cut`.

A fresh, unfilled item looks like this — the same four skeleton lines `cut`
writes inside the fence for a dispatched child (ahead of which `cut` also
writes that `Parent: #<n>` line, never part of this grammar), and what a
human pastes by hand into a `gh issue create` / operator-opened item:

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
(`default = "yes" | "no" | "later"`) or *ruled* (`ruling = "yes" | "no" | "later"`
with a TOML date `ruled_on`) — never both, never neither; a ruled `"later"`
transcribes an explicit operator decision to defer, not merely a proposer's
guessed default. It may also carry three optional card fields (issue #295,
written by `aco ask`, read by every card renderer): `question`, one
operator-language sentence of at most 160 characters shown in place of
`text`; `example`, one operator-language illustration sentence; and
`picture`, an inline SVG (a multi-line TOML string, at most 8 KiB, rooted at
`<svg`, no `<script`, no external `href="http` reference — anything else is
refused with a sentence, both on write and on a stored body that already
carries one). Absent, a card falls back to `text` as before. Per-slice
files, done-when, and dependencies stay in the human prose beside the block;
only a slice's `index` and `title` are typed. Schema and version tokens, and
an expectation's `default`/`ruling` values, are protocol — always this exact
English spelling; every other value (`now`/`next`/`done_when`,
`frozen_until.trigger`, expectation `text`/`question`/`example`/`picture`,
slice `title`) is the operator's own prose and is never parsed. `next`'s own non-parsed vocabulary
(`keiner | keine | nichts | none | -` for "no further work", plus `tbd | todo
| unknown` for "not yet concrete") still applies to a block's `next` value.

An item with no recognized `agent-claim` fence at all is **body malformed:
agent-claim: no agent-claim block** — "no block was found", never "some other
grammar was found instead"; one with a recognized fence that is unclosed,
duplicated, invalid TOML, or a schema violation is **body malformed:
`<path>: <reason>`** (e.g. `body malformed: version: version must be exactly
1`). Both fail loud, by name, on `board`, `next` (`SKIPPED`), and `claim`
(`body-contract` checks) — never a guess through the missing or broken block,
and a malformed container is never proposed as `cut_slice` or
`close_container`.

**Blockers** come from GitHub's own issue-dependency relations, never a body
line — `Blocked by:` prose beside the block is documentation only and changes
nothing. A foreign `owner/repo#n` blocks exactly like a same-repository
dependency and is named the same way (`blocked by owner/repo#n`, or `#3,
owner/repo#n` mixed with a local one). A same-repository *closed* dependency
does not block and lets `board`'s `FREED` column and `claim` proceed; a closed
*foreign* dependency does not free an item on its own (foreign relations can
only block, never free). A pull-request dependency blocks and frees exactly
like any other dependency. **Parentage stays on sub-issues**: no reader ever
derives a parent from the body. `cut` writes a `Parent: #<n>` line as a
recovery marker (below) that everything but `cut`'s own orphan adoption
ignores.

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
carry a valid block — the four-line skeleton above — or it is `body
malformed: agent-claim: no agent-claim block`; only `cut` writes that
skeleton automatically.

Validate a hand-written body before it ever reaches the forge with
`aco body`, forge-free like `status`:

- `aco body --template [--kind task|feature|container] [--parent N]` prints
  the skeleton to compose an issue from — the same block `cut` writes for a
  task or feature child, or the `Blocked by: nichts` line plus that block for
  a container — with a `Parent: #N` line ahead of it when `--parent` is
  given. Pipe it into `gh issue create --body-file -` (or `gh issue edit
  --body-file -`) and fill in `now`/`next`/`done_when` before dispatch.
- `aco body --check` reads a body from stdin (`aco body --check < body.md`,
  or piped from `--template`) and parses it exactly as `check <item>` reads
  a live body — malformed (one sentence per schema defect) or incomplete —
  and prints every defect it finds, never truncated to the
  first since there is no live item to refuse a single verdict about; exit 1
  with a defect, 0 without one. `--json` prints `{"ok": …, "defects": […]}`.
  It takes no file path, reads no dependency, since a body carries no
  dependency key at all, and touches no forge, store, or `gh` call. It still
  reads the repository's own [storage pin](#storage-pin): `[record]` is a
  known key, and validated, only under `storage = "state-ref"` — an unknown
  top-level key under the default `storage = "github"`.

## Cutting a container's next slice

`aco cut <container> --title "…"` dispatches a container's next slice
as a fresh child issue in one step: it creates the issue (native type `Task`),
records it as the container's sub-issue, and, when there is a `[[slice]]`
entry to link, removes that entry from the container's block. The fresh
child's body opens with a `Parent: #<container>` line (#260 — the signal a
repeat `cut` reads back to adopt its own orphan, never another container's)
ahead of `board.BLOCK_CHILD_SKELETON` — every projection key present and
empty — so it is named `body incomplete: Now, Next, Done when` (invisible to
`next`, refused by `claim`) until the head fills it in.

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
create a child issue, link one as a sub-issue, or update an item body
(`capability()` answers anything but `read_write` for any of the three);
when the target is not an open container, or is itself a child of another
issue (nested containers are not supported); when the target's own body is
malformed; and, for `--row N`, when the block names no such entry.
None of the three writes are atomic with each other — nor is the child
issue's own creation atomic with its sub-issue relation write, since
`create_child` is composed from GitHub's own issue-creation POST and the
separate `link_child` port operation — so a failure at any point after the
child issue exists names the created child and the step that failed. Re-run
the same cut; it adopts the child (#260): before creating anything, `cut`
looks for one titled exactly the row's title, among the container's
already-recorded children and among orphans. A title match alone never
adopts an orphan — any unrelated open issue could share it — so an orphan
is adoptable only when it is also a `Task`, is not the container itself, is
not idea-labelled, and its body still opens with the `Parent: #<container>`
line `cut` wrote for it, exactly the shape a failed relation write leaves
behind. Exactly one open match is adopted (linking an orphan first if that
is where it was found) — no second issue, the remaining steps (row removal,
output) finish for that child instead. More than one open match refuses by
name rather than guess; a *closed* match refuses too, instead of reopening
it; no match at all
takes today's create-a-child path.

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

A lane's only name is its branch. `release --branch <lane-branch>` names one
explicitly, exactly like `claim --branch`, but never requires the checkout to
be on it (issue #250): a worktree that was deleted, or is held by another
session, no longer has to be rebuilt just to release, so `aco release
--branch <lane-branch> --claim-id <id> --coordinator-override --role
coordinator --abandoned "..."` runs from any checkout of the repository.
`--branch` together with `--claim-id` for a different claim is refused,
naming both values. Without `--branch`, releasing a lane — including a
coordinator override — still runs from a checkout of that same lane branch,
as before: re-create a worktree on it if needed (`git worktree add <path>
<lane-branch>`) and run `aco release --claim-id <id> --coordinator-override
--role coordinator --abandoned "..."` from inside it, where `<id>` comes from
`aco status` (omitting `--claim-id` still filters by the releasing agent,
coordinator override or not, so a foreign stuck claim needs the id).

## PreToolUse write gate

Copy this hook once into the file the provider actually loads. Skip when a
`PreToolUse` hook already runs `aco protect`. Never overwrite an existing
hook file. The CLI does not write `~/.grok`.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
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

The matcher is `*` (every tool call), not a write-tool name list: a name the
matcher itself skipped would never reach `protect` at all. `protect` is the
real allowlist (issue #238) -- it fails closed on the tool name, denying
outright any name it does not recognize as read-only or mutating, rather than
letting an unlisted write tool default to allowed. `Bash`/`shell` are the one
named exception within that table -- the hook payload carries no file path
for a shell command, so `protect` cannot gate what it cannot see, and both
stay allowed.

A mutating tool's path reaches `protect` in one of three shapes (issue #252):
a `path`/`file_path`/`filePath` key most tools' `tool_input` carries directly;
`notebook_path`, Claude Code's `NotebookEdit`-specific key for the same single
path; or Codex's `apply_patch`, whose `tool_input.command` is a whole patch
text with no path key at all, so `protect` parses every `*** Update File:`,
`*** Add File:`, `*** Delete File:`, and `*** Move to:` line out of it and
checks each one against the claim scope, denying on the first path outside
it. A patch with none of that grammar denies `path required`, the same as any
other mutating call with no path.

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
does not invent an identity. Claim commands write no file outside the repository's
own git directory: no provider configuration, and never `~/.claude`, `~/.codex`, or
`~/.grok`. Workspace registration is the one exception: it writes its local
project-to-session mapping under the XDG configuration path described above;
it does not change provider configuration.

`status`, `protect`, `bootstrap`, and a lane `claim`/`rescope`/`release`
(the issueless `docs/`/`fix/` kind) are forge-free: they read and write only
`refs/aco/state` on the checkout's `canonical_remote`, never resolving a
repository or invoking `gh`, so a canonical remote on any host -- GitHub,
another forge, or a bare `file://` path with no forge at all -- is no error
for them. `board`, `rulings`, `next`, `check`, `cut`, `rule`, `ask`, an issue
`claim`, and a `release --merged` are forge commands: the first time one of
them actually needs its forge, it resolves a target (`--repo`, or the git
remote GitHub's own adapter reads) and refuses by name rather than reading or
writing anything -- `no forge adapter for host <host>` when the canonical
remote's own URL names a host with no adapter yet, or `forge target ... does
not match canonical remote ...` when it names a different repository on a
host this adapter does serve (Erwartung 6, issue #176).
