# `aco start`

`aco start <item> [--scope PATH]... [--slug SLUG] [--whole REASON] [--out-of-order REASON]`
replaces the four-line dance a dispatcher used to type by hand -- `git fetch`, `git worktree
add ../<repo>-worktrees/issue-<n>-<slug> -b <prefix>/issue-<n>-<slug>`, `cd`, `aco claim <n>` --
with one command. This file owns the worktree/branch it builds or resumes, the slug and prefix
it derives, and its own refusals. It never restates `aco claim`'s own preconditions, checks, or
`CLAIMED ...`/cost-line grammar (`specs/claim.spec.md`, `specs/body-block.spec.md`): `start`
acquires that same claim, inside the worktree, through the unchanged claim machinery, and cites
those files' IDs rather than repeating them. `<n>` is the claimed item number, `<slug>` the
derived or given slug, `<prefix>` the derived branch prefix.

## Behavior table

| state \ trigger | `aco start <n>` |
|---|---|
| target closed or missing | START-07 |
| no worktree yet at the computed path | START-01, START-02, START-03 |
| `--slug` given | START-02 |
| no identity signal resolves a prefix | START-03 |
| `--scope`/`--whole`/`--out-of-order` given or omitted | START-04 |
| a worktree already sits at the computed path, clean, same branch | START-06 |
| a worktree already sits at the computed path, dirty | START-10 |
| the computed branch name is already taken elsewhere | START-08 |
| a worktree at the computed path sits on a different branch | START-09 |
| every case | START-05 |

## Creating or resuming the worktree

The slug is `--slug` when given, else the item's own title lowercased with every run of
non-`[a-z0-9]` characters collapsed to one `-`, at most 40 characters, no leading or trailing
`-`. The branch prefix is the acting identity, read the same way `_resolved_agent` reads it but
rendered short: the first word of `ACO_AGENT`, lowercased, when set; else `grok` from a
non-empty `GROK_SESSION_ID`; else `claude` from a non-empty `CLAUDE_SESSION_ID`.

- [ ] [START-01] No worktree yet at `../<repo>-worktrees/issue-<n>-<slug>`: fetch, create it on `<prefix>/issue-<n>-<slug>` from the trunk, claim it, print `worktree:`/`branch:` (see E-START-01).
- [ ] [START-02] A title with no usable slug refuses `no usable slug in this item's title: pass --slug explicitly`, exit 2.
- [ ] [START-03] No identity signal resolves a prefix: refuses `branch prefix is required: set ACO_AGENT, GROK_SESSION_ID, or CLAUDE_SESSION_ID`, exit 2.
- [ ] [START-04] `--scope`/`--whole`/`--out-of-order` pass through verbatim to the claim acquired inside the worktree, exactly as `aco claim <n>` reads them (CLM-06..CLM-18, CLAIM-53..CLAIM-55).
- [ ] [START-05] `start` never changes the caller's own working directory: it stands wherever it started once `start` returns, whatever worktree it just built or claimed in.
- [ ] [START-06] A worktree already at the computed path, clean, same branch: only claims again (an exact repeat is a CLM-15 replay) and still prints `worktree:`/`branch:`/the claim line (see E-START-02).

## Refusing a target, a collision, or a dirty resume

- [ ] [START-07] A closed target refuses `issue #<n> is closed`; a missing one refuses `issue #<n> does not exist here`; exit 2, before any worktree or branch (see E-START-03).
- [ ] [START-08] The branch name already taken elsewhere refuses `branch '<branch>' already exists and is not this item's worktree; remove it, or pass --slug to choose a different worktree`, exit 2.
- [ ] [START-09] A worktree at the computed path on a different branch refuses `worktree <path> exists on branch '<other>', not '<branch>'; remove it, or pass --slug to choose a different worktree`, exit 2.
- [ ] [START-10] A worktree at the computed path with uncommitted changes refuses `worktree <path> is dirty: <paths>; commit or clean it before resuming`, exit 2 (paths named as CLM-05 names them).

## Never

- `start` never launches a second, competing git chokepoint: creating, resuming, or refusing a worktree runs entirely through `checkout.py`'s own `_git_run`, the one launcher every other command in this package already uses.
- `start` never overwrites, deletes, or reuses a worktree sitting on a different branch than the one it computed (START-09): it refuses by name and leaves that worktree exactly as found.
- `start` never derives the claimed scope independently of `aco claim`'s own body-scope resolution: an explicit `--scope` that disagrees with the item's own body still refuses the same way (CLAIM-54).
- `start` never reads or writes `refs/aco/state` itself: every claim write happens inside the one `aco claim` call it makes from the resolved worktree.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local bare repository with
`main` at one commit, a git identity, `origin/HEAD`, a tracked `.agent-claim/board.toml`, and
`ACO_AGENT` set to `Ada`.

### E-START-01 -- a fresh item gets a worktree, a branch, and a claim in one call

Setup: bare-remote, issue `#314` open, title `Fresh Slug`, body `scope = ["src/x.py"]`

```console
$ aco start 314
worktree: /work/agent-claim-worktrees/issue-314-fresh-slug
branch: ada/issue-314-fresh-slug
CLAIMED issue #314: <claim-id>
1 of 6 versioned files (17%); overlaps no other open claims
exit 0
```

### E-START-02 -- a second call resumes instead of rebuilding

Setup: bare-remote, issue `#314` as above, already `aco start 314`

```console
$ aco start 314
worktree: /work/agent-claim-worktrees/issue-314-fresh-slug
branch: ada/issue-314-fresh-slug
CLAIMED issue #314: <claim-id>
1 of 6 versioned files (17%); overlaps no other open claims
exit 0
```

### E-START-03 -- a closed item refuses before anything is built

Setup: bare-remote, issue `#314` closed

```console
$ aco start 314
2> ERROR: issue #314 is closed
exit 2
```
