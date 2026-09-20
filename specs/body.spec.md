# `aco body`

`aco body --template` / `aco body --check`: the one command that composes a
fresh skeleton or checks a piped one, without ever reading a live item. This
file owns its own two mutually exclusive modes and their flag conflicts;
`specs/body-block.spec.md` owns every defect and `body ok`/`body incomplete`
sentence a checked block can carry (BODY-01..BODY-56) and this file cites
those IDs rather than restating them. `specs/storage-pin.spec.md` owns the
tracked-pin precondition `--check` reads (PIN-01); `[record]`'s own
storage-gated validity is `specs/body-block.spec.md`'s own BODY-15/BODY-16.
`specs/output.spec.md` owns the `--json` envelope itself (OUT-nn: key order,
`ok`, `message`); this file names only `--check`'s own `reason` vocabulary
(`valid`, `malformed`, `incomplete`, `invalid_usage`, `unavailable`).
`--template`'s own two refusals (BDY-01, BDY-02) still print
`specs/release.spec.md`'s own `{"ok": false, "error": "<sentence>"}` object
(REL-24), since `--template` names no `--json` output of its own to migrate.

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
| stdin is malformed or incomplete | — | BDY-08 (BODY-01..56) | BDY-09 |
| stdin is not valid UTF-8 | — | BDY-10 | BDY-10 |
| `.agent-claim/board.toml` untracked or absent | — | BDY-11 (PIN-01) | BDY-11 |
| `storage = "state-ref"` vs default `"github"` | — | BDY-12 (BODY-15, BODY-16) | BDY-12 |

## Flags and modes

- [ ] [BDY-01] `aco body` with neither `--template` nor `--check`, or with both, is refused by the parser itself before anything runs, exit `2`.
- [ ] [BDY-02] `aco body --template --json` refuses `--json applies only to --check, not --template`, exit `2`, before any skeleton is composed.
- [ ] [BDY-03] `--check` combined with `--kind K`/`--parent N` refuses `--kind and --parent apply only to --template, not --check`, `--json` `reason: "invalid_usage"`, exit `2`, before stdin is read.

## `--template`, a pure composition

- [ ] [BDY-04] `aco body --template` (default `--kind task`; also `--kind feature`) prints the four-line unfilled `agent-claim` block, exit `0` (see E-BDY-01).
- [ ] [BDY-05] `aco body --template --kind container` prepends one `Blocked by: nichts` line and a blank line ahead of BDY-04's own block, exit `0` (see E-BDY-01).
- [ ] [BDY-06] `--parent N` prepends a `Parent: #N` line and blank line ahead of everything else; combined with `--kind container`, `Parent:` comes first (see E-BDY-01).

## `--check`, reading stdin only

Every sentence below is `specs/body-block.spec.md`'s own text; this file
owns only the CLI-level framing around it.

- [ ] [BDY-07] `aco body --check` reading a valid, complete block from stdin prints and exits exactly as BODY-14 describes.
- [ ] [BDY-08] `aco body --check` prints every defect sentence on stderr, one per line, never truncated like `aco check` (CHECK-06, CHECK-07); exit `2`.
- [ ] [BDY-09] `aco body --check --json` prints the envelope, `reason` below, `"defects": [...]` the same ordered sentences BDY-08 lists; exit `0` on `valid`, exit `2` otherwise (see E-BDY-03).
- [ ] [BDY-10] Stdin that is not valid UTF-8 refuses `stdin is not valid UTF-8: <reason>; pipe the body as UTF-8 text`, `--json` `reason: "unavailable"`, exit `2`, before any parse is attempted.
- [ ] [BDY-11] `aco body --check` against an untracked, absent, or ignored `.agent-claim/board.toml` refuses PIN-01's own sentence, `--json` `reason: "unavailable"`, before stdin is ever read.
- [ ] [BDY-12] `aco body --check` reads the storage pin for `[record]`'s validity: unknown under default `storage = "github"` (BODY-15), field-checked under `storage = "state-ref"` (BODY-16).

`reason`, by stdin's own shape:

| stdin's own shape | `reason` |
|---|---|
| valid, complete | `valid` |
| malformed (BODY-01..56's own schema defect) | `malformed` |
| structurally valid but unfilled | `incomplete` |

## Never

- `aco body --template` never reads `.agent-claim/board.toml`, resolves a forge or the state ref, or needs a git checkout at all: only `--check` touches this repository in any way (see E-BDY-01).
- `aco body --check` never resolves a forge, fetches the state ref, or shells out to `gh`: its only input is stdin, and its only repository read is the storage pin BDY-11/BDY-12 name.
- `aco body --check` never reads a file path, a live issue, or a dependency: a body is always piped in, never named by number (`specs/body-block.spec.md`'s own Never line).
- `aco body --template`'s printed skeleton never carries a `[record]` table, `scope`, or any other optional key: it is always the same four projection lines, whatever `--kind` or `--parent` add around them.
- No `--check` outcome exits `1` any more: `valid` is exit `0`; `malformed`, `incomplete`, `invalid_usage`, and `unavailable` are all exit `2`.
- `aco body --template` never gains a `--json` mode of its own (BDY-02): a composed skeleton is text for a person to paste, not a machine-read payload; only `--check` ever carries `--json`.

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
exit 2
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
{"ok": true, "reason": "valid", "defects": []}
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

### E-BDY-04 -- `--check --json`'s own `malformed`/`incomplete` reasons

Setup: bare-remote

````console
$ aco body --check --json <<'BODY'
no block
BODY
{"ok": false, "reason": "malformed", "defects": ["body malformed: agent-claim: no agent-claim block"]}
exit 2
$ aco body --check --json <<'BODY'
```agent-claim
version = 1
now = ""
next = ""
done_when = ""
```
BODY
{"ok": false, "reason": "incomplete", "defects": ["body incomplete: Now, Next, Done when"]}
exit 2
````
