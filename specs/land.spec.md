# `aco land`

`aco land <pr>` merges one green pull request under `storage = "github"`
with a pinned head sha and its own composed commit message, then runs the
existing `release --merged <pr>` path (`specs/release.spec.md`,
`specs/landing-grammar.spec.md` LAND-29..39, 49, 50, 55, 62/63/64) unchanged
-- one command instead of "merge, delete the branch, fetch, fast-forward,
release" by hand. This file owns the command's own preflight order and
sentences, the commit message it composes, its idempotent post-merge steps,
and its recovery line; it never restates what `release --merged`'s own
classification, claim, parent, or closing rules require (cited by ID) or
what a successful release prints (`freed:`/`next:`, LAND-49). `<n>` is the
pull request number as given, `<sha>` its merge commit, `<state>` GitHub's
own `mergeable_state`, `<name>`/`<conclusion>` one check's own name and
conclusion. "Checks" is every check run GitHub reports for the head sha
(every page of `check-runs`) plus every combined-status context
(`commits/<sha>/status`; an external context such as SonarCloud counts);
"no checks" means both are empty. A name list past three entries prints
only the first three, then `and N more` (`protocol.named_with_overflow_count`),
so no refusal line exceeds 200 characters. A refusal before any write
prints `ERROR: <sentence>` on stderr, exit `2`, exactly as
`specs/ref-store-cas.spec.md`'s own preamble documents; `aco land` has no
`--json` mode.

## Behavior table

| state \ trigger | `aco land <n>` |
|---|---|
| `storage = "state-ref"` | LANDCMD-01 |
| pull request not open | LANDCMD-02 |
| not mergeable | LANDCMD-03 |
| no CI checks at all | LANDCMD-04 |
| a check still running | LANDCMD-05 |
| a check finished without success | LANDCMD-06 |
| classification/claim/parent/closing defect | LANDCMD-07 (LAND-06..28, 32, cited) |
| named work item not open | LANDCMD-08 |
| checkout unclean or off the default branch | LANDCMD-09 |
| every precondition holds | LANDCMD-10, LANDCMD-11 |
| the pull request changed since it was read | LANDCMD-12 |
| a step after the merge fails | LANDCMD-13, LANDCMD-14 |
| this repository's own pull request | LANDCMD-15 |
| a pull request already merged (rerun) | LANDCMD-16 |

## Preflight, read-only, in order

- [ ] [LANDCMD-01] Under `storage = "state-ref"`, `aco land <n>` refuses `aco land is a github command; storage = state-ref has no pull requests to land`, exit `2`, before any read.
- [ ] [LANDCMD-02] A pull request that is not open refuses `pull request #<n> is not open; it cannot be landed`, exit `2`.
- [ ] [LANDCMD-03] A pull request whose own `mergeable_state` is not `clean` refuses `pull request #<n> is not mergeable (<state>)`, exit `2`.
- [ ] [LANDCMD-04] A pull request exposing no CI checks against its own head commit refuses `pull request #<n> exposes no CI checks; cannot verify green CI`, exit `2`.
- [ ] [LANDCMD-05] A pull request with one or more checks not yet completed refuses `pull request #<n> has checks still running: <name>, <name>; wait for every check to succeed`, exit `2`.
- [ ] [LANDCMD-06] A pull request whose checks all completed, at least one without success, refuses `pull request #<n> has non-successful checks: <name> (<conclusion>); land only after every check succeeds`, exit `2`.
- [ ] [LANDCMD-07] Once every check succeeds, `check <pr>`'s own classification/claim/parent/closing rules apply (LAND-04..LAND-28, LAND-32): a defect refuses `pull request #<n> <that same defect sentence>`, exit `2`.
- [ ] [LANDCMD-08] A classified work item that is not open refuses `work item #<n> is not open; it cannot be landed`, exit `2`; an issue-less pull request skips this check.
- [ ] [LANDCMD-09] This checkout must sit on the default branch with nothing uncommitted, or `aco land` refuses `land must run from a clean checkout of the default branch '<branch>'`, exit `2`.

## Merge, composed by `aco land`

- [ ] [LANDCMD-10] `aco land` merges with a real merge commit pinned to the head sha read during preflight, never a squash and never `gh pr merge`'s own unpinned re-read.
- [ ] [LANDCMD-11] The merge commit's own message is the pull request body with its classification line removed, a blank line, then that classification as the message's own last paragraph, nothing after it.
- [ ] [LANDCMD-12] A pull request whose head sha changed since preflight refuses the pinned merge with `pull request #<n> changed while it was checked; re-run land`, exit `2`; nothing merges.

## After the merge

- [ ] [LANDCMD-13] A failure deleting the branch, fast-forwarding, or in the delegated `release --merged` prints `MERGED pull request #<n> as <sha>; follow-up incomplete: <step>; re-run aco land <n>`, exit `2`.
- [ ] [LANDCMD-14] Deleting the merged branch is idempotent: a forge already reporting it absent is success, not a refusal.
- [ ] [LANDCMD-15] In this package's own repository, a successful landing's last line is `reinstall: uv tool install --force --from . agent-coordination`; any other repository prints nothing further.
- [ ] [LANDCMD-16] A rerun against an already-merged pull request skips preflight and the merge, verifies the trailer as `release --merged` does (LAND-62, LAND-64), and resumes -- never a second merge.

## Never

- `aco land` never calls `gh pr merge`: every merge is `merge_landing`'s own pinned REST call, never a re-read of the pull request's current head at merge time.
- `aco land` never runs `release`'s own close, store transition, `freed`/`next` report, or worktree cleanup a second time; it delegates to the one existing `release --merged` path (`specs/release.spec.md`).
- A step after the merge never re-merges: recovery always resumes from the pull request's own already-merged state, read fresh on every rerun.
- `aco land` never writes when any preflight check (LANDCMD-01..09) refuses.

## Examples

`Setup: bare-remote` is `specs/landing-grammar.spec.md`'s own fixture: a
fresh work repository whose `origin` is a local bare repository with `main`
at one commit, a git identity, `origin/HEAD`, and `ACO_AGENT` set to `Ada`,
plus a fixed, deterministic fake `gh`.

### E-LANDCMD-02 — a closed pull request refuses

Setup: bare-remote, fake `gh`, pull request `#57` closed without merging

```console
$ aco land 57
2> ERROR: pull request #57 is not open; it cannot be landed
exit 2
```

### E-LANDCMD-05 — a pull request with a check still running refuses

Setup: bare-remote, fake `gh`, pull request `#57` open, `mergeable_state` `clean`, one check `build` still running

```console
$ aco land 57
2> ERROR: pull request #57 has checks still running: build; wait for every check to succeed
exit 2
```

### E-LANDCMD-05a — a name list past three checks is capped

Setup: bare-remote, fake `gh`, pull request `#57` open, `mergeable_state` `clean`, five checks still running: `check-0` through `check-4`

```console
$ aco land 57
2> ERROR: pull request #57 has checks still running: check-0, check-1, check-2, and 2 more; wait for every check to succeed
exit 2
```

### E-LANDCMD-09 — an unclean checkout refuses before any write

Setup: bare-remote, fake `gh`, pull request `#57` open, mergeable, every check green, this checkout on `main` with an uncommitted change

```console
$ aco land 57
2> ERROR: land must run from a clean checkout of the default branch 'main'
exit 2
```

### E-LANDCMD-12 — the pull request changed since preflight refuses the merge

Setup: bare-remote, fake `gh`, pull request `#57` open and green during preflight, its head moved before the pinned merge request lands

```console
$ aco land 57
2> ERROR: pull request #57 changed while it was checked; re-run land
exit 2
```

### E-LANDCMD-13 — a failed post-merge step names its own recovery line

Setup: bare-remote, fake `gh`, pull request `#57` merges cleanly, the delegated `release --merged` path then fails

```console
$ aco land 57
2> ERROR: MERGED pull request #57 as <sha>; follow-up incomplete: release; re-run aco land 57
exit 2
```

### E-LANDCMD-16 — a rerun resumes without a second merge

Setup: bare-remote, fake `gh`, pull request `#57` already merged (LANDCMD-13's own scenario, resumed)

```console
$ aco land 57
RELEASED issue #42: <claim-id>
freed: none
next: none
exit 0
```
