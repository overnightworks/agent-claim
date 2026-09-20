# `aco body`

`aco body --check`: the one command that checks a piped body for defects
before it reaches the forge, without ever reading a live item.
`specs/body-block.spec.md` owns every defect and `body ok`/`body incomplete`
sentence a checked block can carry (BODY-01..BODY-56) and this file cites
those IDs rather than restating them. `specs/storage-pin.spec.md` owns the
tracked-pin precondition `--check` reads (PIN-01); `[record]`'s own
storage-gated validity is `specs/body-block.spec.md`'s own BODY-15/BODY-16.
`specs/output.spec.md` owns the `--json` envelope itself (OUT-nn: key order,
`ok`, `message`); this file names only `--check`'s own `reason` vocabulary
(`valid`, `malformed`, `incomplete`, `unavailable`).

## Behavior table

| state \ trigger | `aco body` (no `--check`) | `aco body --check` (stdin) | `aco body --check --json` |
|---|---|---|---|
| `--check` not given | BDY-13 | — | — |
| stdin is a valid, complete block | — | BDY-07 (BODY-14) | BDY-09 |
| stdin is malformed or incomplete | — | BDY-08 (BODY-01..56) | BDY-09 |
| stdin is not valid UTF-8 | — | BDY-10 | BDY-10 |
| `.agent-claim/board.toml` untracked or absent | — | BDY-11 (PIN-01) | BDY-11 |
| `storage = "state-ref"` vs default `"github"` | — | BDY-12 (BODY-15, BODY-16) | BDY-12 |

## Flags and modes

- BDY-01, BDY-02, BDY-03 (retired 20.09.2026, issue #420): `--template`'s own mode and its `--kind`/`--parent`/`--json` flag conflicts no longer exist; `--check` is `body`'s only mode (BDY-13).

- [ ] [BDY-13] `aco body` without `--check` is refused by the parser itself before anything runs, exit `2`.

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

- BDY-04, BDY-05, BDY-06 (retired 20.09.2026, issue #420): `--template`'s own skeleton composition no longer exists; `item new` keeps reading its fresh body from stdin (`specs/item.spec.md`), never from a composed skeleton.

## Never

- `aco body --check` never resolves a forge, fetches the state ref, or shells out to `gh`: its only input is stdin, and its only repository read is the storage pin BDY-11/BDY-12 name.
- `aco body --check` never reads a file path, a live issue, or a dependency: a body is always piped in, never named by number (`specs/body-block.spec.md`'s own Never line).
- No `--check` outcome exits `1` any more: `valid` is exit `0`; `malformed`, `incomplete`, and `unavailable` are all exit `2`.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local bare
repository with `main` at one commit, a git identity, `origin/HEAD`, a
tracked `.agent-claim/board.toml` naming no `storage` key, and `ACO_AGENT` set
to `Ada`. Sessions whose stdin carries a fenced block use a four-backtick
console fence.

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

### E-BDY-03 -- `--check --json`, and `--check` missing

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
$ aco body
2> aco body: error: the following arguments are required: --check
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
