# `aco start`

`aco start <item> [--scope PATH]... [--slug SLUG] [--whole REASON] [--out-of-order REASON]`
replaces the four-line dance a dispatcher used to type by hand -- `git fetch`, `git worktree
add ../<repo>-worktrees/issue-<n>-<slug> -b <prefix>/issue-<n>-<slug>`, `cd`, `aco claim <n>` --
with one command. This file owns the worktree/branch it builds or resumes, the slug and prefix
it derives, its own refusals, and how it mints or reuses the claim id underneath. It never
restates `aco claim`'s own preconditions, checks, or `CLAIMED ...`/cost-line grammar
(`specs/claim.spec.md`, `specs/body-block.spec.md`): `start` acquires that same claim, inside the
worktree, through the unchanged claim machinery, and cites those files' IDs rather than repeating
them. `<n>` is the claimed item number, `<slug>` the derived or given slug, `<prefix>` the derived
branch prefix, `<claim-id>` the acquired claim's own id.

## Behavior table

| state \ trigger | `aco start <n>` |
|---|---|
| target closed or missing | START-07 |
| no worktree yet at the computed path | START-01, START-03 |
| `--slug` given, matching the derived shape | START-01 |
| `--slug` given, not matching the derived shape | START-12 |
| no usable slug can be derived and `--slug` is omitted | START-02 |
| no identity signal resolves a prefix | START-03 |
| `--scope`/`--whole`/`--out-of-order` given or omitted | START-04 |
| a worktree already sits at the computed path, clean, same branch, a live claim already on it | START-06 |
| a worktree already sits at the computed path, clean, same branch, no live claim on it | START-11 |
| a worktree already sits at the computed path, dirty | START-10 |
| the computed branch name is already taken elsewhere | START-08 |
| a worktree at the computed path sits on a different branch | START-09 |
| a worktree at the computed path belongs to a foreign checkout | START-13 |
| the built branch name is unsafe as a git ref | START-14 |
| a non-worktree directory already sits at the computed path | START-15 |
| a live claim on the target is held by a different agent or branch | START-16 |
| a clean resume's own `--scope` differs from the live claim's stored scope | START-17 |
| every case | START-05 |

## Creating or resuming the worktree

The slug is `--slug` when given, else the item's own title lowercased with every run of
non-`[a-z0-9]` characters collapsed to one `-`, at most 40 characters, no leading or trailing
`-`. The branch prefix is the acting identity, read the same way the acting agent's own identity
is read elsewhere, but rendered short: the first word of `ACO_AGENT`, lowercased, when set; else
`grok` from a non-empty `GROK_SESSION_ID`; else `claude` from a non-empty `CLAUDE_SESSION_ID`.

The claim id is never derived from the item number: a fresh build always mints one exactly as a
bare `aco claim` would, and a clean resume looks the live claim up by identity and branch, the
same lookup `status`/`release` already use, and reprints that same id rather than minting or
computing a second one. A prior claim that ended -- released, abandoned, or the item's own
merge -- leaves no live claim behind, so the next `start` on that same clean worktree mints a
brand-new id, never a stale or deterministic per-item one.

- [ ] [START-01] No worktree yet at `../<repo>-worktrees/issue-<n>-<slug>`: fetch, create it on `<prefix>/issue-<n>-<slug>` from the trunk, claim it, print `worktree:`/`branch:` (see E-START-01).
- [ ] [START-02] A title with no usable slug, `--slug` omitted, refuses `no usable slug in this item's title: pass --slug explicitly`, exit 2.
- [ ] [START-03] No identity signal resolves a prefix: refuses `branch prefix is required: set ACO_AGENT, GROK_SESSION_ID, or CLAUDE_SESSION_ID`, exit 2.
- [ ] [START-04] `--scope`/`--whole`/`--out-of-order` pass through verbatim to the claim acquired inside the worktree, exactly as `aco claim <n>` reads them (CLM-06..CLM-18, CLAIM-53..CLAIM-55).
- [ ] [START-05] `start` never changes the caller's own working directory: it stands wherever it started once `start` returns, whatever worktree it just built or claimed in.
- [ ] [START-06] A worktree already at the computed path, clean, same branch, with a live claim already on it: looks it up by identity/branch and reprints it verbatim, never minting a second id (see E-START-02).
- [ ] [START-11] The same clean resume with no live claim (released, abandoned, or reopened after merge) mints a fresh id through the ordinary claim path, exactly as a first build would (see E-START-06).
- [ ] [START-12] An explicit `--slug` not matching the shape a derived slug would always produce refuses `--slug must be <rule>`, exit 2, before any worktree or branch is touched (see E-START-04).

## Refusing a target, a collision, or a dirty resume

- [ ] [START-07] A closed target refuses `issue #<n> is closed`; a missing one refuses `issue #<n> does not exist here`; exit 2, before any worktree or branch (see E-START-03).
- [ ] [START-08] The branch name already taken elsewhere refuses `branch '<branch>' already exists and is not this item's worktree; remove it, or pass --slug to choose a different worktree`, exit 2.
- [ ] [START-09] A worktree at the computed path on a different branch refuses `worktree <path> exists on branch '<other>', not '<branch>'; remove it, or pass --slug to choose a different worktree`, exit 2.
- [ ] [START-10] A worktree at the computed path with uncommitted changes refuses `worktree <path> is dirty: <paths>; commit or clean it before resuming`, exit 2 (paths named as CLM-05 names them).
- [ ] [START-13] A worktree at the computed path that is not this repository's own -- a foreign root, this repository's own main checkout, or a different repository's worktree -- refuses by name (see E-START-05).
- [ ] [START-14] An unsafe branch prefix refuses `agent identity '<prefix>' is not usable in a branch name: '<branch>' is not a safe Git ref`, exit `2`, before any git write (see E-START-07).
- [ ] [START-15] Something other than a git worktree already sitting at the computed path refuses `path exists and is not a worktree of this repository`, exit `2` (see E-START-08).
- [ ] [START-16] A live claim on the target held by a different agent or branch is never silently resumed: it falls through to the ordinary claim path, refused by CLAIM-11's own sentence (see E-START-09).
- [ ] [START-17] A resume's own explicit `--scope` disagreeing with the live claim's stored scope refuses `live claim scope differs; release it first`, exit `2` (see E-START-10).

## Never

- `start` never launches a second, competing way to run git: every worktree it builds, resumes, or refuses goes through the one git launcher every other command in this package already uses.
- `start` never overwrites, deletes, or reuses a worktree sitting on a different branch than the one it computed (START-09), or one that belongs to a foreign checkout (START-13): it refuses by name and leaves that worktree exactly as found.
- `start` never derives the claimed scope independently of `aco claim`'s own body-scope resolution: an explicit `--scope` that disagrees with the item's own body still refuses the same way (CLAIM-54).
- `start` never reads or writes `refs/aco/state` itself: every claim write happens inside the one `aco claim` call it makes from the resolved worktree.
- `start` never reuses a prior, now-terminal claim id for the same item: a clean resume with no live claim mints a fresh one (START-11), exactly as a first build would.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local bare repository with
`main` at one commit, a git identity, `origin/HEAD`, a tracked `.agent-claim/board.toml`, and
`ACO_AGENT` set to `Ada`. A session below also names a fixed, deterministic fake `gh` as a setup
precondition (the shape `specs/landing-grammar.spec.md` already uses) for reading the item's own
title and body.

### E-START-01 -- a fresh item gets a worktree, a branch, and a claim in one call

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` open, title `Fresh Slug`, body `scope = ["src/x.py"]`

```console
$ aco start 314
worktree: /work/agent-claim-worktrees/issue-314-fresh-slug
branch: ada/issue-314-fresh-slug
CLAIMED issue #314: <claim-id>
1 of 6 versioned files (17%); overlaps no other open claims
exit 0
```

### E-START-02 -- a second call resumes the live claim by lookup, same id and all

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` as above, already `aco start 314`

```console
$ aco start 314
worktree: /work/agent-claim-worktrees/issue-314-fresh-slug
branch: ada/issue-314-fresh-slug
CLAIMED issue #314: <claim-id>
1 of 6 versioned files (17%); overlaps no other open claims
exit 0
```

`<claim-id>` is the identical id E-START-01 minted: this call never mints a second one.

### E-START-03 -- a closed item refuses before anything is built

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` closed

```console
$ aco start 314
2> ERROR: issue #314 is closed
exit 2
```

### E-START-04 -- an explicit `--slug` failing the derived shape refuses before any worktree

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` open

```console
$ aco start 314 --slug Bad_Slug
2> ERROR: --slug must be lowercase letters, digits, and single '-' separators only, at most 40 characters, never leading or trailing '-'
exit 2
```

### E-START-05 -- a worktree at the computed path belongs to a different repository

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` open, `/work/agent-claim-worktrees/issue-314-fresh-slug`
already a linked worktree of an unrelated repository, on branch `ada/issue-314-fresh-slug`

```console
$ aco start 314
2> ERROR: worktree /work/agent-claim-worktrees/issue-314-fresh-slug belongs to a different repository; remove it, or pass --slug to choose a different worktree
exit 2
```

### E-START-06 -- a clean resume with no live claim mints a fresh id, never a stale one

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` as E-START-01, already `aco start 314` then
`aco release 314 --abandoned "stopped for the day"` -- the worktree stands, untouched, with no
live claim on it

```console
$ aco start 314
worktree: /work/agent-claim-worktrees/issue-314-fresh-slug
branch: ada/issue-314-fresh-slug
CLAIMED issue #314: <fresh-claim-id>
1 of 6 versioned files (17%); overlaps no other open claims
exit 0
```

`<fresh-claim-id>` differs from any id minted for #314 before it.

### E-START-07 -- an unsafe identity prefix refuses before any worktree

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` open, title `Fresh Slug`, `ACO_AGENT` set to `-bad`

```console
$ aco start 314
2> ERROR: agent identity '-bad' is not usable in a branch name: '-bad/issue-314-fresh-slug' is not a safe Git ref
exit 2
```

### E-START-08 -- something other than a worktree already sits at the computed path

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` open, title `Fresh Slug`,
`/work/agent-claim-worktrees/issue-314-fresh-slug` already a plain directory, not a git worktree

```console
$ aco start 314
2> ERROR: path exists and is not a worktree of this repository
exit 2
```

### E-START-09 -- a live claim held by a different agent is never silently resumed

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` as E-START-01, a live claim on `#314`
held by `Grok sess-9` on branch `grok/issue-314-other`

```console
$ aco start 314
2> ERROR: issue #314 is claimed by Grok sess-9 (builder) on issue #314 branch grok/issue-314-other
exit 2
```

### E-START-10 -- resuming with a `--scope` that disagrees with the live claim refuses

Setup: bare-remote, bootstrapped, fake `gh`, issue `#314` as E-START-01, already `aco start 314`

```console
$ aco start 314 --scope src/other.py
2> ERROR: live claim scope differs; release it first
exit 2
```
