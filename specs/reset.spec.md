# `aco reset`

`aco reset` (issue #298, operator-ruled 15.09.2026/16.09.2026) is the one
recovery path over a broken or rewritten `refs/aco/state`, its own five-step
shape owned start to finish by `specs/ref-store-cas.spec.md` (CAS-39..46)
along with the mandatory export and its bundle naming, the live-claim
refusal, `--no-export`, and the bundle's own restore proof; this file owns
the command's own argument shape, forge-freedom, and the exact per-step line
for every state CAS-39..46 do not already spell out: an absent ref, a
failed export, and every shape a rejected remote delete can take. A refusal
reaching this command's own sink prints `ERROR: <sentence>` on stderr, exit
`2`, the shared sink `specs/ref-store-cas.spec.md`'s own preamble already
documents. `<repo>`/`<sha>`/`<tip>`/`<remote>` are the runner's own values;
`<repo>` is the work repository's own directory name (`aco reset`'s own
`repository` argument to its bundle-name builder, the worktree's own
`Path.name`).

## Behavior table

| state \ trigger | `aco reset` | `aco reset --confirm` |
|---|---|---|
| `refs/aco/state` never existed on the remote | RESET-01, RESET-02 | RESET-03 |
| a live claim exists | CAS-40 | CAS-40 |
| ref present, no live claim | CAS-39 | CAS-41, CAS-43, CAS-44 |
| `--no-export` | — | CAS-45 |
| export destination already carries that bundle's name | — | CAS-42 |
| the export write itself fails | — | RESET-04 |
| a `--force-with-lease` delete rejected, ref unchanged | — | RESET-05 |
| a `--force-with-lease` delete rejected, ref moved | — | RESET-06 |
| a `--force-with-lease` delete whose remote is unreachable | — | RESET-07 |
| a rejected push whose commit actually landed | — | RESET-08 |
| a local ref a foreign tool left behind | — | RESET-09 |
| that local ref's own deletion fails | — | RESET-13 |
| an argument this command does not define | RESET-10 | RESET-10 |
| `--repo`, or a non-GitHub canonical remote | RESET-11 | RESET-11 |
| `--export-dir` omitted | RESET-12 | RESET-12 |

## Before any ref exists

- [ ] [RESET-01] `aco reset` against a remote that never carried `refs/aco/state` prints `would: nothing to export: refs/aco/state does not exist on <remote>` (see E-RESET-01).
- [ ] [RESET-02] That same run's second line is `would: nothing to delete on <remote>: refs/aco/state does not exist`, exit `0` (see E-RESET-01).
- [ ] [RESET-03] `aco reset --confirm` in that same state prints RESET-01/02's own two sentences without the `would:` prefix, then CAS-44's own steps still run unmodified (see E-RESET-01).

## A failed export or delete

- [ ] [RESET-04] A bundle write failure -- an unwritable `--export-dir`, or any other write failure -- refuses `cannot export refs/aco/state at <tip> to <destination>: <detail>`, before any deletion (see E-RESET-02).
- [ ] [RESET-05] `--force-with-lease` rejected, tip unchanged, refuses `cannot delete refs/aco/state on <remote> (lease <tip>): <detail>`, naming `still present`, fix `re-run aco reset --confirm` (see E-RESET-05).
- [ ] [RESET-06] The same rejection, remote tip moved, refuses that same prefix, naming `the remote moved to <current>`, fix `re-run aco reset --confirm`, never a manual lease (see E-RESET-03).
- [ ] [RESET-07] The same rejection when the remote cannot be reached at all to confirm the outcome refuses that same prefix, naming `outcome unknown`, fix `verify with git ls-remote` (see E-RESET-04).
- [ ] [RESET-08] A rejected `--force-with-lease` push whose commit actually landed (a lost response) is re-probed as already deleted: `reset` reports no failure for it and continues.
- [ ] [RESET-09] `aco reset --confirm` against a local `refs/aco/state` a foreign tool left behind prints `deleted local refs/aco/state` as its own third line.
- [ ] [RESET-13] That local delete itself failing (a stale `.lock` file, or any other local git failure) refuses `cannot delete local refs/aco/state: <detail>`, after the remote step already succeeded.

## The command's own argument shape

- [ ] [RESET-10] `aco reset` followed by any argument outside `--confirm`/`--no-export`/`--export-dir` is refused by the parser, exactly as `aco bootstrap` refuses an unknown flag (BOOT-02), before any read.
- [ ] [RESET-11] `aco --repo OWNER/REPO reset` behaves exactly as with no `--repo`, forge-free like `aco bootstrap` (`specs/bootstrap.spec.md` BOOT-03): a non-GitHub canonical remote is no error either.
- [ ] [RESET-12] `--export-dir` omitted writes the bundle under the repository's own parent directory, the default its own `--export-dir DIR` help text names.

## Never

- `aco reset` never accepts `--json`: its own parser defines only `--confirm`, `--no-export`, and `--export-dir` (README, "Refusals and `--json`").
- A rejected `--force-with-lease` delete never leaves the local ref deleted while the remote one survives: RESET-05..07 all raise before the local-delete step runs.
- A remote-delete repair sentence never names a manual `--force-with-lease` command: RESET-05 and RESET-06 both name only re-running `aco reset --confirm` itself.
- An export failure never leaves a bundle file at the destination path: the temporary file it was staged to is always removed on the way out.
- `aco reset` never force-pushes without a lease matched to the tip it just read (`specs/ref-store-cas.spec.md` CAS-43): every delete is `--force-with-lease`, never plain `--force`.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`, a
tracked `.agent-claim/board.toml` naming no `storage` key, and `ACO_AGENT`
set to `Ada`; `<remote>`, `<tmp>`, and `<repo>` are the runner's own paths.

### E-RESET-01 -- nothing yet to export or delete

Setup: bare-remote, no `refs/aco/state` yet

```console
$ aco reset --export-dir <tmp>
would: nothing to export: refs/aco/state does not exist on origin
would: nothing to delete on origin: refs/aco/state does not exist
would: no local refs/aco/state to delete
would: clear lineage stamps and fetch anchors in 1 worktree
would: bootstrap a fresh empty state
exit 0
$ aco reset --confirm --export-dir <tmp>
nothing to export: refs/aco/state does not exist on origin
nothing to delete on origin: refs/aco/state does not exist
no local refs/aco/state to delete
cleared lineage stamps and fetch anchors in 1 worktree
bootstrapped a fresh empty state at <sha>
exit 0
```

### E-RESET-02 -- an export failure leaves the ref untouched

Setup: bare-remote, bootstrapped, no live claim, `--export-dir` an unwritable directory

```console
$ aco reset --confirm --export-dir <tmp>
2> ERROR: cannot export refs/aco/state at <tip> to <tmp>/aco-state-<repo>-<date>-<sha>.bundle: <detail>
exit 2
```

### E-RESET-03 -- a moved remote tip names the re-run repair, never a manual lease

Setup: bare-remote, bootstrapped, no live claim, `refs/aco/state` on `origin` is force-pushed to a new tip after `reset` reads it but before its own delete runs

```console
$ aco reset --confirm --export-dir <tmp>
exported refs/aco/state at <tip> to <tmp>/aco-state-<repo>-<date>-<sha>.bundle (restore with: git fetch <tmp>/aco-state-<repo>-<date>-<sha>.bundle refs/worktree/aco/reset-export:refs/aco/state)
2> ERROR: cannot delete refs/aco/state on origin (lease <tip>): <detail>; the remote moved to <moved-tip> -- re-run `aco reset --confirm`, which re-observes origin, re-validates against every live claim, and re-exports before deleting again; a manual lease against <moved-tip> would skip both checks
exit 2
```

### E-RESET-04 -- an unreachable remote leaves the outcome unknown

Setup: bare-remote, bootstrapped, no live claim, `origin` becomes unreachable after `reset` reads its tip

```console
$ aco reset --confirm --export-dir <tmp>
exported refs/aco/state at <tip> to <tmp>/aco-state-<repo>-<date>-<sha>.bundle (restore with: git fetch <tmp>/aco-state-<repo>-<date>-<sha>.bundle refs/worktree/aco/reset-export:refs/aco/state)
2> ERROR: cannot delete refs/aco/state on origin (lease <tip>): <detail>; outcome unknown -- verify with `git ls-remote origin refs/aco/state` before retrying `aco reset --confirm`
exit 2
```

### E-RESET-05 -- an unchanged remote tip names the re-run repair

Setup: bare-remote, bootstrapped, no live claim, the `--force-with-lease` delete is rejected for a reason other than a moved tip -- `refs/aco/state` on `origin` is still at the tip `reset` read

```console
$ aco reset --confirm --export-dir <tmp>
exported refs/aco/state at <tip> to <tmp>/aco-state-<repo>-<date>-<sha>.bundle (restore with: git fetch <tmp>/aco-state-<repo>-<date>-<sha>.bundle refs/worktree/aco/reset-export:refs/aco/state)
2> ERROR: cannot delete refs/aco/state on origin (lease <tip>): <detail>; refs/aco/state is still present at <tip>, unchanged from the lease -- re-run `aco reset --confirm` once the cause is fixed
exit 2
```
