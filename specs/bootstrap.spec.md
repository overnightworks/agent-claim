# `aco bootstrap`

`aco bootstrap`: the ordinary command path that creates `refs/aco/state`
(`specs/ref-store-cas.spec.md`'s own fact, CAS-01/CAS-02); `aco reset --confirm`
reaches the same creation through its own path (CAS-44, CAS-46) -- forge-free,
argument-free beyond the bare command, and the sole owner of the CLI's own
argument shape, its forge-freedom, and how a failure reaches its sink. The
ref's own idempotency, the tree it writes, and every transport and lineage
refusal it can hit are `specs/ref-store-cas.spec.md`'s own facts (CAS-01,
CAS-02, CAS-04, CAS-05, CAS-12); `specs/storage-pin.spec.md` owns the
untracked-pin refusal every store command shares (PIN-01). This file cites
those IDs rather than restating them. `<sha>` is the runner's own commit id.

## Behavior table

| state \ trigger | `aco bootstrap` |
|---|---|
| `.agent-claim/board.toml` untracked, absent, or ignored | PIN-01 |
| ref absent, proven (`ls-remote` exit 2) | CAS-02 |
| ref present, valid schema | CAS-01 |
| ref previously observed by this worktree, now absent | CAS-12 |
| `ls-remote`/`fetch` auth or transport failure | CAS-04, CAS-05 |
| any store-level failure reaching this command's own sink | BOOT-01 |
| an argument this command does not define | BOOT-02 |
| `--repo` set, or the canonical remote names a non-GitHub host | BOOT-03 |

## The command's own argument shape

- [ ] [BOOT-02] `aco bootstrap` followed by any argument it does not define refuses `aco: error: unrecognized arguments: <extra>` on stderr, exit `2`, before any read (see E-BOOT-04).

## Forge-free, like the other repository-level commands

- [ ] [BOOT-03] `aco --repo OWNER/REPO bootstrap` against any canonical remote host, GitHub or not, prints the ref's own commit id `<sha>`, exit `0`, same as with no `--repo` (see E-BOOT-02).

## A store-level failure never invents JSON

This command defines no `--json`, so its own sink never invents an error object from one.

- [ ] [BOOT-01] A store-level refusal reaching `aco bootstrap` prints only `ERROR: <sentence>` on stderr, exit `2`; stdout stays empty (see E-BOOT-03).

## Never

- `aco bootstrap` never writes a second commit once `refs/aco/state` exists: a present ref is a pure read (CAS-01).
- `aco bootstrap` never resolves an item forge, reads `--repo`, or calls out to the forge: `--repo` and a non-GitHub canonical remote are no error (BOOT-03).
- `aco bootstrap` never accepts `--json`, a `--ledger` value, or any other flag: its own parser defines the bare subcommand alone (BOOT-02).
- `aco bootstrap`'s own commit never carries a `claims/`, `ids/`, `resources/`, or `items/` entry: only `schema.toml` (CAS-02's own fact).

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`, a
tracked `.agent-claim/board.toml` naming no `storage` key, and `ACO_AGENT`
set to `Ada`; `<sha>` is the runner's own commit id. Bootstrap's own
idempotency and empty-tree shape are `specs/ref-store-cas.spec.md`'s own
proof (CAS-01, CAS-02, E-CAS-01); the sessions below cover only this file's
own argument shape, forge-freedom, and refusal sink.

### E-BOOT-02 -- `--repo` and a non-GitHub remote are no error

Setup: bare-remote except `origin` points at `git@gitlab.com:other/repo.git`, no `refs/aco/state` yet

```console
$ aco --repo example/agent-coordination bootstrap
<sha>
exit 0
```

### E-BOOT-03 -- an unreachable remote refuses loud, stdout stays empty

Setup: a fresh repository whose `origin` points at a local path that does not exist

```console
$ aco bootstrap
2> ERROR: cannot reach origin refs/aco/state: auth or transport failure (ls-remote exited 128): <detail>
exit 2
```

### E-BOOT-04 -- an argument this command does not define

Setup: bare-remote, bootstrapped

```console
$ aco bootstrap --ledger 5
2> usage: aco [-h] [--version] [--repo REPO]
2>            {bootstrap,reset,status,board,rulings,next,claim,release,rescope,cut,ask,rule,check,body,brief,item,protect,register,run,login,_run-at-login}
2>            ...
2> aco: error: unrecognized arguments: --ledger 5
exit 2
```
