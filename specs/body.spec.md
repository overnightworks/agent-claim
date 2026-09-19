# `aco body`

`aco body --template` / `aco body --check`: the one command that composes a
fresh skeleton or checks a piped one, without ever reading a live item. This
file owns its own two mutually exclusive modes, their flag conflicts, and the
`--check` `--json` envelope; `specs/body-block.spec.md` owns every defect and
`body ok`/`body incomplete` sentence a checked block can carry (BODY-01..
BODY-56) and this file cites those IDs rather than restating them.
`specs/storage-pin.spec.md` owns the tracked-pin precondition `--check` reads
(PIN-01) and `[record]`'s own storage-gated validity (PIN-09,
BODY-15/BODY-16). A refusal reaching the shared collection point prints
`ERROR: <sentence>` on stderr, exit `2`, and with `--json` also
`specs/release.spec.md`'s own `{"ok": false, "error": "<sentence>"}` object
(REL-24) -- exactly as every other command's runtime refusal does.

## Behavior table

| state \ trigger | `aco body --template[ --kind K][ --parent N]` | `aco body --check` (stdin) | `aco body --check --json` |
|---|---|---|---|
| neither `--template` nor `--check`, or both | BDY-01 | BDY-01 | BDY-01 |
| `--template` given together with `--json` | BDY-02 | — | — |
| `--check` given together with `--kind` or `--parent` | — | BDY-03 | BDY-03 |
| `--template`, no `--kind` (default `task`) | BDY-04 | — | — |
| `--template --kind container` | BDY-05 | — | — |
| `--template ... --parent N` | BDY-06 | — | — |
| stdin is a valid, complete block | — | BDY-07 (BODY-14) | BDY-09 |
| stdin is malformed or incomplete | — | BDY-08 (BODY-01..52) | BDY-09 |
| stdin is not valid UTF-8 | — | BDY-10 | BDY-10 |
| `.agent-claim/board.toml` untracked or absent | — | BDY-11 (PIN-01) | BDY-11 |
| `storage = "state-ref"` vs default `"github"` | — | BDY-12 (BODY-15, BODY-16) | BDY-12 |

## Flags and modes

- [ ] [BDY-01] `aco body` with neither `--template` nor `--check`, or with both, is refused by the parser itself before anything runs, exit `2`.
- [ ] [BDY-02] `aco body --template --json` refuses `--json applies only to --check, not --template`, exit `2`, before any skeleton is composed.
- [ ] [BDY-03] `aco body --check --kind K` or `aco body --check --parent N` refuses `--kind and --parent apply only to --template, not --check`, exit `2`, before stdin is ever read.

## `--template`, a pure composition

- [ ] [BDY-04] `aco body --template` (default `--kind task`, or `--kind feature`) prints a four-line unfilled `agent-claim` block -- `version = 1`, `now`/`next`/`done_when` empty -- exit `0` (see E-BDY-01).
- [ ] [BDY-05] `aco body --template --kind container` prepends one `Blocked by: nichts` line and a blank line ahead of BDY-04's own block, exit `0` (see E-BDY-01).
- [ ] [BDY-06] `--parent N` prepends one `Parent: #N` line and a blank line ahead of everything else; with `--kind container` too, the order is `Parent:`, then `Blocked by: nichts`, then the block (see E-BDY-01).

## `--check`, reading stdin only

Every sentence below is `specs/body-block.spec.md`'s own text; this file
owns only the CLI-level framing around it.

- [ ] [BDY-07] `aco body --check` reading a valid, complete block from stdin prints and exits exactly as BODY-14 describes.
- [ ] [BDY-08] `aco body --check` prints every simultaneous defect sentence on stderr, one per line, in order -- never truncated to the first, unlike `aco check`'s issue-mode read (CHECK-06, CHECK-07) -- exit `1`.
- [ ] [BDY-09] `aco body --check --json` prints `{"ok": <bool>, "defects": [...]}`, `defects` the same ordered sentences BDY-08 lists in text, `[]` only when `"ok"` is `true`, exit `0` or `1` to match.
- [ ] [BDY-10] Stdin that is not valid UTF-8 refuses `stdin is not valid UTF-8: <reason>; pipe the body as UTF-8 text`, exit `2`, before any parse is attempted.
- [ ] [BDY-11] `aco body --check` against an untracked, absent, or ignored `.agent-claim/board.toml` refuses PIN-01's own sentence, before stdin is ever read.
- [ ] [BDY-12] `aco body --check` reads the storage pin for `[record]`'s validity: unknown under the default `storage = "github"` (BODY-15), known and field-checked under `storage = "state-ref"` (BODY-16).

## Never

- `aco body --template` never reads `.agent-claim/board.toml`, resolves a forge or the state ref, or needs a git checkout at all: only `--check` touches this repository in any way (see E-BDY-01).
- `aco body --check` never resolves a forge, fetches the state ref, or shells out to `gh`: its only input is stdin, and its only repository read is the storage pin BDY-11/BDY-12 name.
- `aco body --check` never reads a file path, a live issue, or a dependency: a body is always piped in, never named by number (`specs/body-block.spec.md`'s own Never line).
- `aco body --template`'s printed skeleton never carries a `[record]` table, `scope`, or any other optional key: it is always the same four projection lines, whatever `--kind` or `--parent` add around them.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local bare
repository with `main` at one commit, a git identity, `origin/HEAD`, a
tracked `.agent-claim/board.toml` naming no `storage` key, and `ACO_AGENT` set
to `Ada`. Sessions whose stdin carries a fenced block use a four-backtick
console fence.

### E-BDY-01 -- `--template` needs no checkout, pin, or forge at all

Setup: none -- a directory outside any git repository

````console
$ aco body --template
```agent-claim
version = 1
now = ""
next = ""
done_when = ""
```
exit 0
$ aco body --template --kind container --parent 79
Parent: #79

Blocked by: nichts

```agent-claim
version = 1
now = ""
next = ""
done_when = ""
```
exit 0
````

### E-BDY-02 -- `--check`, ok and malformed

Setup: bare-remote

````console
$ aco body --check <<'BODY'
```agent-claim
version = 1
now = "Cut on 19.09.2026."
next = "Build it."
done_when = "It is built."
```
BODY
body ok
exit 0
$ aco body --check <<'BODY'
```agent-claim
version = 1
next = "X"
```
BODY
2> body malformed: now: now is required
2> body malformed: done_when: done_when is required
exit 1
````

### E-BDY-03 -- `--check --json`, and the mode/flag refusals

Setup: bare-remote

````console
$ aco body --check --json <<'BODY'
```agent-claim
version = 1
now = "Cut on 19.09.2026."
next = "Build it."
done_when = "It is built."
```
BODY
{"ok": true, "defects": []}
exit 0
$ aco body --check --kind task <<'BODY'
```agent-claim
version = 1
now = ""
next = ""
done_when = ""
```
BODY
2> ERROR: --kind and --parent apply only to --template, not --check
exit 2
$ aco body --template --json
2> ERROR: --json applies only to --check, not --template
exit 2
````
