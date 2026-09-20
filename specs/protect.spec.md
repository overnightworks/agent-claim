# `aco protect`

`aco protect` is the `PreToolUse` hook entry point (issue #176, #238, #252,
#314): reading one hook payload from stdin, it judges a single mutating tool
call against this session's own live claim and prints its verdict as one JSON
object, never a second time and never on stderr. This file owns the payload
envelope, every denial reason and the order they are judged in, and the
allow/deny JSON shape and exit codes. `specs/claim-record.spec.md` owns a
claim's own identity, scope grammar and overlap; `specs/ref-store-cas.spec.md`
owns `refs/aco/state`'s own transport failures; `specs/storage-pin.spec.md`
owns the board-configuration precondition (PIN-01) every store command
shares -- this file cites those IDs rather than restating them. `aco rescope`
shares `protect`'s own checkout resolver and relative-path grammar (the
`relative payload path`, `not in a repository`, and `no commit on this
branch` denials) but is otherwise a different lane's own spec; only as far
as those denials are `protect`'s own verdict are they documented here.
`<path>` is the payload's own absolute file path; `<remote>` is the
canonical remote name.

## Behavior table

| state \ trigger | generic mutating tool | `NotebookEdit` | `apply_patch` | `Bash` | a read-effect tool |
|---|---|---|---|---|---|
| malformed or non-object payload | PROT-03 | PROT-03 | PROT-03 | PROT-03 | PROT-03 |
| no string tool name under either key | PROT-04 | PROT-04 | PROT-04 | PROT-04 | PROT-04 |
| tool name in neither table | — | — | — | — | PROT-06 (unknown) |
| tool name is read-only | — | — | — | — | PROT-05 |
| no resolvable path in the payload | PROT-07 | PROT-07 | PROT-07 | PROT-30 (no pattern) | — |
| agent identity cannot be resolved | PROT-08 | PROT-08 | PROT-08 | PROT-08 (last, see below) | — |
| payload path not absolute | PROT-09 | PROT-09 | PROT-09 (each path) | PROT-31 (allow) | — |
| path's directory outside every repository | PROT-10 | PROT-10 | PROT-10 | PROT-32 (allow) | — |
| checkout has no commit yet | PROT-11 | PROT-11 | PROT-11 | PROT-11 | — |
| shared main checkout, or on the default branch | PROT-12 | PROT-12 | PROT-12 | PROT-12 | — |
| default branch cannot be resolved | PROT-13 | PROT-13 | PROT-13 | PROT-13 | — |
| path resolves to exactly the checkout root | PROT-14 | PROT-14 | PROT-14 | PROT-14 | — |
| board-configuration precondition fails | PROT-29 | PROT-29 | PROT-29 | PROT-29 | — |
| a store fetch failure | PROT-15 | PROT-15 | PROT-15 | PROT-15 | — |
| `refs/aco/state` missing | PROT-16 | PROT-16 | PROT-16 | PROT-16 | — |
| an unexpected crash | PROT-17 | PROT-17 | PROT-17 | PROT-17 | — |
| no live claim on this branch at all | PROT-18 | PROT-18 | PROT-18 | PROT-33 (names pattern) | — |
| a live claim whose scope misses the path | PROT-19 | PROT-19 | PROT-20 | PROT-33 (names pattern) | — |
| a live claim covering the path | PROT-21 | PROT-21 | PROT-23 | PROT-21 | — |
| a lane (issueless) claim covering the path | PROT-22 | PROT-22 | PROT-22 | PROT-22 | — |
| several paths, first one outside scope | — | — | PROT-24 | PROT-33 (each pair) | — |
| paths across two linked worktrees | — | — | PROT-25 | PROT-33 (own checkout each) | — |
| a `cd` changes the resolution directory | — | — | — | PROT-34 | — |
| a `cd` target cannot be resolved | — | — | — | PROT-35 (allow) | — |
| a decoy path key the tool never sends | — | PROT-27 | — | — | — |
| `--repo`, or a non-GitHub canonical remote | PROT-28 | PROT-28 | PROT-28 | PROT-28 | PROT-28 |

`Bash`'s own precedence differs from every other column's: PROT-30's allow
(no recognized pattern) and PROT-31's allow (a still-relative path) both
fire before agent identity is ever resolved, unlike PROT-08's own position
before every per-path gate for a generic mutating tool -- Bash resolves
identity last, only once a checkout, its live state, and a repository-
relative path are already in hand, since a pattern that never gets that far
never needed an identity at all.

## The JSON envelope

- [ ] [PROT-01] A write `protect` authorizes prints exactly `{"decision": "allow"}` to stdout, nothing to stderr, exit `0` (see E-PROT-01).
- [ ] [PROT-02] A write `protect` refuses prints exactly `{"decision": "deny", "reason": "<sentence>"}` to stdout, nothing to stderr, exit `2` (see E-PROT-02).

## The hook payload

- [ ] [PROT-03] Unreadable stdin, invalid JSON, or a payload that is not a JSON object denies `invalid hook payload` (PROT-02's shape).
- [ ] [PROT-04] A payload naming no string tool name under either `toolName` or `tool_name` denies `invalid hook payload`.
- [ ] [PROT-05] A tool name this table marks read-only allows `{"decision": "allow"}` without reading identity, git, the store, or GitHub (see E-PROT-05).
- [ ] [PROT-06] A tool name in neither the read nor the mutating table denies `'<name>' is not in aco's hook tool table`, fix `add it there as read-only or mutating before use` (see E-PROT-06).

## The payload path and its own checkout

- [ ] [PROT-07] A mutating tool call with no resolvable path -- a missing key, an empty string, or an `apply_patch` command matching no patch-file grammar -- denies `path required`.
- [ ] [PROT-08] A failure resolving this session's own agent identity, before any per-path checkout gate runs, denies that failure's own bare sentence, no `ERROR:` prefix.
- [ ] [PROT-09] A payload path that is not absolute denies `relative payload path`, never guessed against the hook process's own cwd (see E-PROT-07).
- [ ] [PROT-10] A payload path whose directory sits outside every git repository denies `not in a repository`.
- [ ] [PROT-11] A checkout with no commit yet (an unborn branch) denies `no commit on this branch`.
- [ ] [PROT-12] The shared main checkout, or a linked worktree on the repository's own resolved default branch, denies `not main` (see E-PROT-03).
- [ ] [PROT-13] A checkout whose default branch cannot be resolved at all denies `default branch unknown`, never falling back to a `main`/`master` guess.
- [ ] [PROT-14] A payload path that resolves to exactly the checkout root denies `path required`, the same reason as no path at all.

## The live claim state

- [ ] [PROT-29] A board-configuration precondition failure (PIN-01), reached resolving the store's own canonical remote after checkout/root gates clear, denies that failure's own bare sentence, no `ERROR:` prefix.
- [ ] [PROT-15] A store fetch failure -- unreachable, malformed tree, or a lineage break -- denies `cannot reach refs/aco/state: <detail>`.
- [ ] [PROT-16] A fetched state with no `refs/aco/state` at all denies `cannot reach refs/aco/state: <sentence>`, `<sentence>` the one `specs/ref-store-cas.spec.md` CAS-03 already owns.
- [ ] [PROT-17] Any other uncaught exception denies `{"decision": "deny", "reason": "<message>"}`, that exception's own bare text, no traceback.
- [ ] [PROT-18] This session holding no live claim on the checkout's own branch at all denies `claim first`.
- [ ] [PROT-19] A live claim for this session and branch whose scope misses the path denies `claim first`, the same reason as no claim at all, for every tool but `apply_patch`.
- [ ] [PROT-20] The same scope miss under `apply_patch` denies `<path> outside claim scope`, naming the one path the payload's own grammar can name.
- [ ] [PROT-21] A live claim covering the path allows `{"decision": "allow"}` (see E-PROT-01).
- [ ] [PROT-22] A lane (issueless) claim covering the path allows `{"decision": "allow"}` exactly like an issue claim.

## `apply_patch`'s own multi-path payload

`apply_patch` (Codex) carries no path key: its `command` is a whole patch
text, parsed for every `*** Add File:`, `*** Delete File:`, and `*** Update
File:` header line. A `*** Move to:` line is recognized only immediately
after an `*** Update File:` header, before any hunk content; anywhere else
in the patch, that line is not a header the grammar admits, so the whole
patch fails to parse and `protect` denies `path required` (PROT-07)
fail-closed rather than guessing which paths it touches.

- [ ] [PROT-23] Every path an `apply_patch` command touches sitting inside the live claim's own scope allows (see E-PROT-04).
- [ ] [PROT-24] A command touching several paths denies naming the first one outside scope, in the patch's own order, not a generic `claim first` (see E-PROT-04).
- [ ] [PROT-25] Two paths in one command sitting in two different linked worktrees of the same repository are judged in their own checkout each; the first denial, `claim first` or otherwise, wins.
- [ ] [PROT-26] Same-repository `apply_patch` paths share one live snapshot: a claim change made mid-call cannot flip the second path's `claim first`/`outside claim scope`/allow verdict (see E-PROT-04).

## `NotebookEdit`'s own path key

- [ ] [PROT-27] `NotebookEdit` reads its target only from `notebook_path`, ignoring a decoy `path` key sitting beside it that this tool never actually sends.

## `Bash`'s own command-text payload

`Bash` carries no path key at all: its `command` text is scanned for a
short, fixed list of write patterns -- a real, unquoted `>`/`>>` redirection
(a heredoc target such as `cat > path <<EOF` included), `tee`'s own file
operands, `sed -i` (or `-i<suffix>`/`--in-place[=suffix]`, skipping
`-e`/`--expression`/`-f`/`--file` and their own values), `mv` (every
operand -- a source vanishes exactly like its destination is written), `cp`
(a `-t`/`--target-directory` value when given, otherwise its last operand --
either way, the only one it actually writes), `rm`, `git checkout`
(`-f`/`--ours`/`--theirs` before a literal `--`, then its path operands),
and `git restore` (skipping `-s`/`--source`, `--conflict`, and
`--pathspec-from-file` and their own values, and a literal `--`) -- each
occurrence naming a `(pattern, path)` pair, in the order the command names
them (issue #380). A literal `--` ends option parsing the same way Bash's
own coreutils do: every operand after it is a path regardless of a leading
`-` (`rm -- -f` judges `-f`). A quoted or backslash-escaped occurrence of a
character that would otherwise be an operator (`echo '>' > f`) is data,
never the operator it merely reads like -- but a quoted or backslash-escaped
*command name* (`'rm' f`, `r\m f`) still executes exactly as Bash runs it
and is recognized like the plain spelling. An unquoted `#` at the start of a
word is a comment to the end of its own physical line, never scanned for a
pattern of its own, exactly like Bash itself never runs what follows it on
that line; an unquoted, trailing backslash-newline joins the next physical
line first, so a command split that way is judged exactly like the one line
it forms. `git checkout <branch>` (no `--`) and a plain `sed` without
`-i`/`--in-place` name no path at all, since neither writes a file; neither
does a redirect whose own target is exactly `/dev/null`, or a
file-descriptor-duplication form (`2>&1`, `>&2`) -- its own "target" is
another operator, never a real file. Unlike every other tool's own
already-absolute payload path, a Bash pattern's own path is relative to the
shell's own working directory: it resolves against the payload's own `cwd`
field, updated by every literal, resolvable `cd` the command names first
(PROT-34), rather than the hook process's cwd, and PROT-09 does not apply to
it at all. That directory changes for the rest of the enclosing
`;`/`&&`/`||`/newline-separated list, never across a `|` -- a pipeline
segment is its own subshell, so a `cd` on either side of one changes nothing
outside it -- and a parenthesised `( ... )` group keeps its own copy that
reverts at its own closing `)`, exactly like Bash's own subshell scoping.
Every resolved path then runs the same Checkout, Default-Branch, and
Claim-Scope gates a mutating tool's own path runs (PROT-11 no commit yet,
PROT-12/PROT-13 not main, PROT-14 the checkout root -- including a path that
names a linked worktree's own root directory exactly, judged by that
checkout rather than by its parent, the store's own
PROT-29/PROT-15/PROT-16/PROT-17, PROT-21/PROT-22 a covering claim), except a
path outside every repository allows instead of PROT-10's deny, agent
identity resolves only once a checkout and its live state are already in
hand rather than before any path runs, and a scope miss denies naming both
the recognized pattern and the path rather than a bare `claim first`.

- [ ] [PROT-30] A `command` naming none of these patterns -- or no string `command` at all -- allows without resolving identity, git, or the store.
- [ ] [PROT-31] A recognized pattern's relative path resolves against the payload's own `cwd`, as PROT-34 updates it; with no known directory, that path allows outright, before identity resolves.
- [ ] [PROT-32] A recognized pattern's own path outside every git repository allows, unlike PROT-10's deny for every other mutating tool -- except a checkout's own root directory, which PROT-14 still denies.
- [ ] [PROT-33] A recognized pattern's own path outside the live claim's scope denies `<pattern> <path> outside claim scope`, naming both (see E-PROT-08).
- [ ] [PROT-34] A literal, resolvable `cd` changes the directory every later path in its own `;`/`&&`/`||`/newline list resolves against -- never across a `|`, and only inside its own group.
- [ ] [PROT-35] An unresolvable `cd` target -- expandable, `-`, or no operand -- ends recognition for the rest of the command outright, allowing it (see E-PROT-10).

## Forge-free

- [ ] [PROT-28] `protect` never resolves an item forge: allow and deny alike are unaffected by `--repo` or a non-GitHub canonical remote.

## Never

- `protect` never reads the store for a denial the checkout resolves alone: a "not main", "no commit on this branch", "not in a repository", "relative payload path", or "path required" verdict touches `store.fetch_state` zero times.
- `protect` never defaults an unrecognized tool name to allowed: PROT-06 fails closed instead.
- `protect` never trusts a relative payload path by joining it to the hook process's own cwd, even from the one cwd where that guess would happen to be correct.
- `protect` never accepts `--json`: every verdict is already the one JSON object on every outcome (README, "Refusals and --json").
- `protect` never writes a file: every denial and every allow leaves `$HOME` and the checkout untouched.
- `protect` never reads working-tree dirtiness: a dirty checkout still allows a covered write, unlike `claim`'s own precondition.
- `protect` never binds the resolved checkout's `HEAD` to a claim's own `base`: it judges the live claim's branch and scope alone.
- `shell` and their other-provider equivalents never deny for a missing path: the hook payload carries no file path for those, so `protect` cannot gate what it cannot see (README, "PreToolUse write gate"). `Bash` (issue #380) is the one named exception: it judges its own `command` text, but only the fixed pattern list PROT-30 owns -- a `python -c ...` one-liner or an opaque script invocation stays invisible on purpose, so recognizing a pattern is a best-effort aid against forgetting the claim, never a security boundary.
- `protect` never guesses a Bash-recognized relative path's `cwd` from the hook process's own cwd: a payload naming no `cwd` at all allows that one path outright (PROT-31) rather than risk denying legitimate work on a guess `protect` has no way to confirm -- the same "never guess a relative path" principle PROT-09 enforces for every other tool, applied here as an allow instead of a deny since Bash's own path is expected to be relative in the first place.
- A Bash pattern never judges what only running the shell could resolve: an operand containing an unquoted (or, inside double quotes, still-substituting) `$name`, `` `command` ``, `~`, `*`, `?`, or `[` is never judged and never denied, the same allow as naming no pattern at all -- `protect` cannot know what a variable, a glob, or a substitution actually expands to without executing the command.
- Everything between an unquoted `<<WORD`/`<<-WORD`/`<<'WORD'` and its own terminator line is heredoc body, never scanned for a write pattern of its own: only the command line naming the heredoc is judged, so a body that merely reads like `rm docs/file` names nothing.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`, a
tracked `.agent-claim/board.toml` naming no `storage` key, and `ACO_AGENT`
set to `Ada`; `<worktree>` and `<main>` are its own linked-worktree and
shared-main directories. Every session pipes the hook's JSON payload on
stdin, exactly as a `PreToolUse` hook call does.

### E-PROT-01 -- a covered write allows

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, already `aco claim 42 --scope README.md`

```console
$ echo '{"toolName": "Write", "toolInput": {"file_path": "<worktree>/README.md"}}' | aco protect
{"decision": "allow"}
exit 0
```

### E-PROT-02 -- no covering claim denies `claim first`

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, no live claim

```console
$ echo '{"toolName": "Write", "toolInput": {"file_path": "<worktree>/README.md"}}' | aco protect
{"decision": "deny", "reason": "claim first"}
exit 2
```

### E-PROT-03 -- the shared main checkout denies `not main`

Setup: bare-remote, bootstrapped, no live claim

```console
$ echo '{"toolName": "Write", "toolInput": {"file_path": "<main>/README.md"}}' | aco protect
{"decision": "deny", "reason": "not main"}
exit 2
```

### E-PROT-04 -- `apply_patch` names the one path outside scope

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, already `aco claim 42 --scope src`

```console
$ echo '{"toolName": "apply_patch", "toolInput": {"command": "*** Begin Patch\n*** Update File: <worktree>/src/widget.py\n@@\n-old\n+new\n*** Add File: <worktree>/docs/widget.md\n+content\n*** End Patch"}}' | aco protect
{"decision": "deny", "reason": "docs/widget.md outside claim scope"}
exit 2
```

### E-PROT-05 -- a read-effect tool always allows

Setup: bare-remote, no live claim

```console
$ echo '{"toolName": "Read", "toolInput": {"path": "src/secret.py"}}' | aco protect
{"decision": "allow"}
exit 0
```

### E-PROT-06 -- an unrecognized tool name fails closed

Setup: bare-remote, no live claim

```console
$ echo '{"toolName": "invented_tool"}' | aco protect
{"decision": "deny", "reason": "'invented_tool' is not in aco's hook tool table (HOOK_TOOL_EFFECTS in cli.py, issue #238); add it there as read-only or mutating before use"}
exit 2
```

### E-PROT-07 -- a relative payload path denies outright

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, already `aco claim 42 --scope src`, cwd is `<worktree>`

```console
$ echo '{"toolName": "Write", "toolInput": {"path": "src/widget.py"}}' | aco protect
{"decision": "deny", "reason": "relative payload path"}
exit 2
```

### E-PROT-08 -- a Bash write pattern outside claim scope denies naming both

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, already `aco claim 42 --scope src`, cwd is `<worktree>`

```console
$ echo '{"toolName": "Bash", "toolInput": {"command": "sed -i \"s/a/b/\" docs/widget.md"}, "cwd": "<worktree>"}' | aco protect
{"decision": "deny", "reason": "sed -i docs/widget.md outside claim scope"}
exit 2
```

### E-PROT-09 -- a Bash command outside every repository, or naming no pattern, allows

Setup: bare-remote, no live claim

```console
$ echo '{"toolName": "Bash", "toolInput": {"command": "rm /tmp/scratch.txt"}}' | aco protect
{"decision": "allow"}
exit 0
$ echo '{"toolName": "Bash", "toolInput": {"command": "git diff"}}' | aco protect
{"decision": "allow"}
exit 0
```

### E-PROT-10 -- `cd` tracks the directory, an unresolvable one allows

Setup: bare-remote, bootstrapped, a linked worktree on `ada/issue-42`, already `aco claim 42 --scope src`, cwd is `<worktree>`

```console
$ echo '{"toolName": "Bash", "toolInput": {"command": "cd docs && rm widget.md"}, "cwd": "<worktree>"}' | aco protect
{"decision": "deny", "reason": "rm docs/widget.md outside claim scope"}
exit 2
$ echo '{"toolName": "Bash", "toolInput": {"command": "cd $SCRATCH && rm widget.md"}, "cwd": "<worktree>"}' | aco protect
{"decision": "allow"}
exit 0
```
