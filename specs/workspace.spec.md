# `aco register`, `aco run`, `aco login` (workspace recovery)

The operator's own local recovery tool for a stopped or interrupted provider
conversation (ruled to stay in `aco`, 19.09.2026; feature rulings owned by
#213). `register` records one project's native session in a local mapping
file; `run` opens or reattaches its console; `login enable`/`disable`/
`_run-at-login` restore every registered project at desktop login. This file
owns the mapping's own shape and refusals, the three commands' printed lines
and exit codes, and the login-attempt record. It never restates the pin or
forge grammar `specs/storage-pin.spec.md` owns (PIN-01): none of these
commands ever resolves an item forge or a `storage` pin, and REL-24's
`{"ok": false, "error": ...}` `--json` refusal shape never applies here
either, since none of the three ever accepts `--json` (see `## Never`).
`<path>` is the resolved `${XDG_CONFIG_HOME:-~/.config}/aco/workspace.toml`.

## Behavior table

| state \ trigger | `register` | `run` | `login enable`/`disable` | `login status` | `_run-at-login` |
|---|---|---|---|---|---|
| `--repo` given | WS-01 | WS-01 | WS-02 | WS-02 | n/a (WS-*, `## Never`) |
| mapping missing entirely | n/a (writes fresh) | WS-20 | WS-21 | WS-57 (`missing`) | WS-64 |
| mapping wrong version | WS-22 | WS-22 | WS-22 | WS-57 (`malformed`) | WS-64 |
| mapping project record malformed | WS-25..WS-28 | WS-25..WS-28 | WS-25..WS-28 | WS-57 (`malformed`) | WS-64 |
| mapping present, no projects | n/a | n/a | WS-23 | WS-57 (`empty`) | WS-65 |
| mapping valid, has projects | n/a | WS-29/30 | WS-48/49/53/54 | WS-57 (`valid`) | WS-61..63 |
| invalid field (path/uuid/agent/model/key) | WS-05..WS-12 | — | — | — | — |
| identity, directory, or session collision | WS-14..WS-17 | — | — | — | — |
| successful handoff | WS-18, WS-19 | — | — | — | — |
| unknown project name | — | WS-31 | — | — | — |
| `XDG_RUNTIME_DIR` unsafe | — | WS-33..WS-36 | — | — | — |
| console-dependent run outcome | — | WS-37..WS-47 | — | — | — |
| launcher conflict/unsafe directory | — | — | WS-50, WS-51, WS-55 | WS-56 | — |
| desktop-notified outcome | — | — | — | — | WS-61..66 |

## `--repo` is meaningless here

- [ ] [WS-01] `aco --repo <owner/repo> register ...` or `... run ...` refuses `--repo is meaningless for workspace operations`, exit `2`, before any mapping read (see E-WS-01).
- [ ] [WS-02] `aco --repo <owner/repo> login <enable|disable|status>` refuses `--repo is meaningless for login recovery operations`, exit `2`, before any mapping read (see E-WS-01).

## `register`: validating one project record

- [ ] [WS-03] `aco register KEY ...` with neither `--stopped` nor `--live-pid` refuses `aco register: error: one of the arguments --stopped --live-pid is required`, exit `2` (see E-WS-02).
- [ ] [WS-04] `aco register KEY ... --stopped --live-pid N` refuses `aco register: error: argument --live-pid: not allowed with argument --stopped`, exit `2` (see E-WS-02).
- [ ] [WS-05] `--path` naming a directory that does not exist refuses `project 'KEY' directory does not exist: <path>`, exit `2`.
- [ ] [WS-06] `--path` naming a file, not a directory, refuses `project 'KEY' directory is not a directory: <path>`, exit `2`.
- [ ] [WS-07] `--session-id` that is not an exact UUID refuses `project 'KEY' session_id must be an exact UUID`, exit `2`.
- [ ] [WS-08] `--agent` empty or made only of whitespace refuses `project 'KEY' agent must not be empty`, exit `2`.
- [ ] [WS-09] `--agent` or `--model` containing a line break or NUL refuses `project 'KEY' agent must not contain line breaks or NUL` (`model` names the same sentence for itself), exit `2`.
- [ ] [WS-10] `--model ""` (given but empty) refuses `project 'KEY' model must not be empty`, exit `2`.
- [ ] [WS-11] `KEY` outside letters, digits, underscores, or hyphens refuses `project key must use letters, numbers, underscores, or hyphens`, exit `2` (see E-WS-03).
- [ ] [WS-12] `--provider` outside `codex`/`claude`/`grok` refuses `aco register: error: argument --provider: invalid choice: '<value>' (choose from codex, claude, grok)`, exit `2` (see E-WS-02).
- [ ] [WS-13] `--live-pid` zero or negative refuses `project 'KEY' live_pid must be a positive integer`, exit `2`, before any process is observed.
- [ ] [WS-14] A `--path` canonical directory another registered project already owns refuses `directory <path> is already registered`, exit `2`.
- [ ] [WS-15] A `--provider`/`--session-id` pair another registered project already owns refuses `<provider> session <uuid> is already registered`, exit `2`.
- [ ] [WS-16] Registering the same `KEY` again with an identical directory, session, agent, model, and provider changes nothing and prints `KEY: already registered`, exit `0` (see E-WS-03).
- [ ] [WS-17] A changed identity for an already-registered `KEY` refuses `project 'KEY' is already registered; native session and claim identity can only change through an explicit future handoff`, exit `2`.

## `register`: a fresh, successful handoff

A `--live-pid` handoff needs a same-user, exactly-matching, stable native
process to validate against, so its success is test evidence only
(`test_register_live_pid_passes_only_the_selected_process_to_workspace`,
`test_live_registration_persists_only_a_validated_process_receipt`), never a
transcript in this file.

- [ ] [WS-18] `aco register KEY --path P --session-id U --agent A --stopped` writes `KEY` into the mapping and prints `KEY: registered`, exit `0` (see E-WS-03).
- [ ] [WS-19] `aco register KEY ... --live-pid N` for a validated matching process writes the same `KEY: registered` line and a process receipt, exit `0`.

## The mapping file: path, version, and existence

- [ ] [WS-20] `aco run` with no mapping file at all refuses `workspace configuration does not exist: <path>`, exit `2` (see E-WS-01).
- [ ] [WS-21] `aco login enable` with no mapping file at all refuses the same `workspace configuration does not exist: <path>`, exit `2`.
- [ ] [WS-22] `register`/`run`/`login enable` refuse any mapping version but `3`: `workspace configuration <path> must contain only version = 3 and projects; found version <n>`, exit `2` (see E-WS-07).
- [ ] [WS-23] `aco login enable` against a mapping with no registered projects refuses `workspace configuration has no registered projects`, exit `2` (see E-WS-08).
- [ ] [WS-24] `${XDG_CONFIG_HOME:-~/.config}/aco/workspace.toml` is the one mapping path every command but `login disable` reads or writes; an explicit `XDG_CONFIG_HOME` relocates it (see E-WS-09).

## The mapping file: one stored project record's own shape

`register`, `run`, and `login enable` all load the whole mapping before
acting, so one malformed record refuses the same way for each (see the
behavior table); the refusal is always by path and record, never by command.

- [ ] [WS-25] A stored record that is not a table, or a project key that is not a string, refuses `workspace projects must use project keys and table records`, exit `2` (see E-WS-21).
- [ ] [WS-26] A stored record missing or carrying an unsupported field, or a non-string value, refuses `... unsupported or missing fields` or `... must be a string`, exit `2` (see E-WS-21).
- [ ] [WS-27] A stored `path`, relative, missing, or symlinked, refuses `... an absolute canonical directory`, `... does not exist: <path>`, or `... a canonical directory`, exit `2` (see E-WS-11, E-WS-21).
- [ ] [WS-28] Two stored records sharing one canonical directory or one provider/session pair refuse `... contains duplicate canonical paths` or `... duplicate native provider UUIDs`, exit `2` (see E-WS-22).

## `run`: selecting projects

- [ ] [WS-29] `aco run` with no `PROJECT` argument selects every registered project, in the mapping's own order.
- [ ] [WS-30] `aco run PROJECT` selects only that one project.
- [ ] [WS-31] `aco run PROJECT` naming a key the mapping does not carry refuses `project 'PROJECT' is not registered`, exit `2` (see E-WS-10).
- [ ] [WS-32] `aco run PROJECT` validates every other project first; one broken record refuses its own sentence before the named project runs (see E-WS-11).

## `run`: the local runtime directory

WS-36 needs a pre-existing shared or symlinked runtime subdirectory, so it
is test evidence only (`test_run_refuses_a_shared_or_symlinked_workspace_runtime`),
never a transcript.

- [ ] [WS-33] A relative or unset `XDG_RUNTIME_DIR` refuses `XDG_RUNTIME_DIR must be an absolute user-owned directory`, exit `2`, before any console is touched (see E-WS-12).
- [ ] [WS-34] An `XDG_RUNTIME_DIR` naming a path that cannot be inspected (e.g. it does not exist) refuses `XDG_RUNTIME_DIR is unavailable: <path>`, exit `2` (see E-WS-12).
- [ ] [WS-35] A non-private, group- or other-readable `XDG_RUNTIME_DIR` refuses `XDG_RUNTIME_DIR must be a private directory owned by this user`, exit `2` (see E-WS-12).
- [ ] [WS-36] A symlinked or shared runtime subdirectory refuses `workspace runtime directory must be private and must not be a symlink`, exit `2`.

## `run`: one console per project (test evidence, not transcripts)

Every outcome below needs a real or faked console and is therefore proven by
its own named test in `tests/test_workspace.py` (task scope: "no real
terminal or tmux"), never by a transcript in this file. `aco run` always
prints `KEY: <state>` or, with a detail, `KEY: <state>: <detail>`, one line
per selected project, and exits `1` if any outcome is `failed` or `ownership
unknown`, else `0`.

- [ ] [WS-37] No existing console, and no still-running external process from its own live registration, prints `KEY: started` after opening a fresh console.
- [ ] [WS-38] An already-attached console prints `KEY: reused`, opening no second viewer.
- [ ] [WS-39] A detached console matching this project's own identity prints `KEY: reattached`.
- [ ] [WS-40] A console whose pane exited, with no still-running external process from its own live registration, prints `KEY: retried`, only on this explicit command.
- [ ] [WS-41] A console awaiting its own launched viewer prints `KEY: viewer pending: waiting for the previously launched console` or `: console launch is pending`.
- [ ] [WS-42] For a project registered with `--live-pid`, a stable, same-user native process still running its conversation prints `KEY: external live` in place of `started`/`retried`, opening no console.
- [ ] [WS-43] For a project registered with `--live-pid`, an ambiguous or multiply-matching native process prints `KEY: ownership unknown` in place of `started`/`retried`, retrying nothing.
- [ ] [WS-44] A fresh or retried console resumes the stored provider session and model, replacing the inherited session identity with the registered agent.
- [ ] [WS-45] A console opened but never observed attached, with no recorded viewer failure, prints `KEY: failed: project console is not attached`.
- [ ] [WS-46] A console whose viewer attempt was recorded as failed prints `KEY: failed: project console failed to attach`.
- [ ] [WS-47] A foreign target prints `KEY: failed: tmux target 'aco-KEY' has foreign metadata`.

## `login enable`/`disable`: the one owned launcher

- [ ] [WS-48] `login enable`, the mapping valid and non-empty, writes the desktop entry (mode `600`) and prints `login launcher enabled`, exit `0` (see E-WS-14).
- [ ] [WS-49] Repeating `login enable` unchanged prints `login launcher already enabled`, exit `0`, without rewriting the file.
- [ ] [WS-50] `login enable` against a desktop-entry path this program did not write refuses `login launcher conflicts with an unowned filesystem object`, exit `2` (see E-WS-15).
- [ ] [WS-51] `login enable` with an unsafe `autostart` directory refuses `login directory must be private and owned by this user`, exit `2`.
- [ ] [WS-52] An interpreter path containing `%` refuses `login executable must be an absolute safe path`, exit `2` (test evidence, `test_login_enable_refuses_a_percent_executable_path`).
- [ ] [WS-53] `login disable` with no launcher present prints `login launcher already disabled`, exit `0`, creating nothing.
- [ ] [WS-54] `login disable` removes only its own owned launcher and prints `login launcher disabled`, exit `0` (see E-WS-14).
- [ ] [WS-55] `login disable` against an unowned launcher refuses the same `login launcher conflicts with an unowned filesystem object`, exit `2`, unchanged.

`login enable`'s `autostart` parent is created mode `700` only the first
time; an already-existing private parent (WS-51 rejects an unsafe one) is
accepted unchanged, never chmodded.

## `login status`: three independent state lines

- [ ] [WS-56] `aco login status` always prints `launcher: <disabled|enabled|stale|conflict>` first, from the installed interpreter and the desktop entry alone, writing nothing (see E-WS-17).
- [ ] [WS-57] The second line, `configuration: <missing|malformed|empty|valid>`, reads `missing` with no file, `malformed` if it fails to parse, `empty` if valid with no projects, else `valid` (see E-WS-17).
- [ ] [WS-58] With no login-attempt record at all, the third line is `attempt: no login attempt recorded`, exit `0` (see E-WS-17).
- [ ] [WS-59] A well-formed attempt prints `attempt: <id> <started_at> <state>`, one `KEY: <outcome>` line per project, `workspace: <failure>` and `completed: <t>` only if present, exit `0` (see E-WS-18).
- [ ] [WS-60] A login-attempt record that fails to parse prints `attempt: malformed` and exits `1`, echoing none of its own bytes (see E-WS-19).

## `_run-at-login`: one serialized recovery attempt (hidden command)

`_run-at-login` prints nothing to stdout or stderr on any outcome; its only
visible trace is the best-effort desktop notification (`## Never`) and the
login-attempt record `login status` later reads.

- [ ] [WS-61] A run whose selected projects all recover, none already externally live, exits `0` and notifies `Workspace recovery completed for <n> project(s).` (see E-WS-20).
- [ ] [WS-62] A run notifies `Workspace recovery completed for <n> project(s). <k> project(s) already had live owners; no console was opened.` when `k` of the outcomes are `external live`.
- [ ] [WS-63] A run notifies `Workspace recovery completed for <n> project(s). <k> project(s) failed recovery.` and exits `1` when `k` of the outcomes are `failed` or `ownership unknown`.
- [ ] [WS-64] A mapping this attempt cannot even read notifies `Workspace recovery failed.`, exit `1`, recording `failure = "workspace failure"` (see E-WS-20).
- [ ] [WS-65] A mapping that validates but names no projects to run is treated the same as WS-64: `workspace failure`, exit `1` (see E-WS-20).
- [ ] [WS-66] A failure to even write the running attempt record notifies `Workspace recovery could not record its attempt.` and exits `2`, before any project runs.
- [ ] [WS-67] An interrupted attempt leaves `state = "running"`, no outcomes or completion (test evidence, `test_login_recovery_keeps_unknown_outcomes_after_interruption`).

## Never

- None of `register`, `run`, `login enable`, `login disable`, or `login status` accepts `--json`: the flag does not exist on their parsers, so a refusal never gets `{"ok": false, ...}` (REL-24's shape belongs to commands that do carry `--json`; contrast, not shared).
- `_run-at-login` never checks `--repo` at all, even given: it is dispatched before any workspace `--repo` guard runs.
- Beyond `${XDG_CONFIG_HOME:-~/.config}/aco/workspace.toml`, `${XDG_STATE_HOME:-~/.local/state}/aco/login-attempt.json`, and, only after `login enable`, `${XDG_CONFIG_HOME:-~/.config}/autostart/aco-workspace.desktop`, `register` also leaves the mapping's own `.lock` beside it, `run` leaves a `workspace.lock` inside the runtime directory, and `_run-at-login` leaves the login-attempt record's own `.lock`; none of the three is ever removed once created.
- The login-attempt record never carries a provider UUID, workspace path, agent, model, configuration, environment, command line, or raw error text — only an attempt id, timestamps, completion state, project keys, and the public outcome names in `## Behavior table`.
- `login disable` never deletes the mapping file, a provider's own history, claims, or a live console; it removes only the one desktop-entry file it owns.
- `register`/`run`/`login` never migrate an old-version mapping into the current one; WS-22 refuses it outright, by path and version, every time.
- A live-registered project's console launch never scans for an external native process a second time once its own recorded receipt still matches; scanning happens only when that receipt no longer validates.
- `_run-at-login`'s desktop notification is best-effort: a failure to notify (no notifier installed, no display) never changes the attempt's own recorded outcome or exit code.

## Examples

`Setup: bare-remote` is a fresh work repository whose `origin` is a local
bare repository with `main` at one commit, a git identity, `origin/HEAD`,
and `ACO_AGENT` set to `Ada` (`specs/board.spec.md`'s own fixture); every
session below also runs with a temporary `XDG_CONFIG_HOME` (and
`XDG_STATE_HOME` for the login examples), so the mapping and login-attempt
files never touch the operator's own. `<tmp>` is that temporary root;
`<uuid>` is a fixed native session UUID. None of these commands need a git
repository to run; `bare-remote` is used only for a consistent fixture hub.

### E-WS-01 -- `--repo` and a missing mapping refuse before anything else

Setup: bare-remote, temporary `XDG_CONFIG_HOME`, no mapping file

```console
$ aco --repo example/repository run
2> ERROR: --repo is meaningless for workspace operations
exit 2
$ aco --repo example/repository login status
2> ERROR: --repo is meaningless for login recovery operations
exit 2
$ aco run
2> ERROR: workspace configuration does not exist: <tmp>/config/aco/workspace.toml
exit 2
```

### E-WS-02 -- `register`'s own argument grammar refuses before any mapping read

Setup: bare-remote, temporary `XDG_CONFIG_HOME`, `<tmp>/project` exists

```console
$ aco register alpha --path <tmp>/project --session-id <uuid> --agent head
2> aco register: error: one of the arguments --stopped --live-pid is required
exit 2
$ aco register alpha --path <tmp>/project --session-id <uuid> --agent head --stopped --live-pid 5
2> aco register: error: argument --live-pid: not allowed with argument --stopped
exit 2
$ aco register alpha --path <tmp>/project --session-id <uuid> --agent head --stopped --provider bogus
2> aco register: error: argument --provider: invalid choice: 'bogus' (choose from codex, claude, grok)
exit 2
```

### E-WS-03 -- a fresh stopped handoff, idempotent repetition, and an invalid key

Setup: bare-remote, temporary `XDG_CONFIG_HOME`, `<tmp>/project` exists

```console
$ aco register alpha --path <tmp>/project --session-id <uuid> --agent "workspace head" --stopped
alpha: registered
exit 0
$ aco register alpha --path <tmp>/project --session-id <uuid> --agent "workspace head" --stopped
alpha: already registered
exit 0
$ aco register "bad key!" --path <tmp>/project --session-id <uuid> --agent head --stopped
2> ERROR: project key must use letters, numbers, underscores, or hyphens
exit 2
```

### E-WS-07 -- an older mapping is refused, never migrated

Setup: bare-remote, temporary `XDG_CONFIG_HOME`, `workspace.toml` hand-written with `version = 2`

```console
$ aco run
2> ERROR: workspace configuration <tmp>/config/aco/workspace.toml must contain only version = 3 and projects; found version 2
exit 2
```

### E-WS-08 -- `login enable` refuses an empty mapping

Setup: bare-remote, temporary `XDG_CONFIG_HOME`, `workspace.toml` valid with an empty `[projects]` table

```console
$ aco login enable
2> ERROR: workspace configuration has no registered projects
exit 2
```

### E-WS-09 -- `XDG_CONFIG_HOME` relocates the mapping

Setup: bare-remote, `XDG_CONFIG_HOME=<tmp>/config`, `<tmp>/project` exists

```console
$ aco register alpha --path <tmp>/project --session-id <uuid> --agent head --stopped
alpha: registered
exit 0
$ test -f <tmp>/config/aco/workspace.toml && echo found
found
exit 0
```

### E-WS-10 -- `run` on an unregistered name

Setup: bare-remote, temporary `XDG_CONFIG_HOME`, `alpha` registered, `beta` is not

```console
$ aco run beta
2> ERROR: project 'beta' is not registered
exit 2
```

### E-WS-11 -- one broken record blocks even an unrelated `run`

Setup: bare-remote, temporary `XDG_CONFIG_HOME`, `alpha` registered normally, `beta`'s record hand-edited to a directory that no longer exists

```console
$ aco run alpha
2> ERROR: workspace project directory does not exist: /does/not/exist
exit 2
```

### E-WS-12 -- an unsafe `XDG_RUNTIME_DIR`, relative, unavailable, then shared

Setup: bare-remote, temporary `XDG_CONFIG_HOME`, `alpha` registered

```console
$ XDG_RUNTIME_DIR=relative/dir aco run
2> ERROR: XDG_RUNTIME_DIR must be an absolute user-owned directory
exit 2
$ XDG_RUNTIME_DIR=<tmp>/no-such-directory aco run
2> ERROR: XDG_RUNTIME_DIR is unavailable: <tmp>/no-such-directory
exit 2
$ XDG_RUNTIME_DIR=<tmp>/world-writable aco run
2> ERROR: XDG_RUNTIME_DIR must be a private directory owned by this user
exit 2
```

### E-WS-14 -- enabling, then disabling, the launcher

Setup: bare-remote, temporary `XDG_CONFIG_HOME`, `alpha` registered

```console
$ aco login enable
login launcher enabled
exit 0
$ aco login enable
login launcher already enabled
exit 0
$ aco login disable
login launcher disabled
exit 0
$ aco login disable
login launcher already disabled
exit 0
```

### E-WS-15 -- a foreign file at the launcher path

Setup: bare-remote, temporary `XDG_CONFIG_HOME`, `alpha` registered, private `autostart/` (mode `700`) with `aco-workspace.desktop` hand-written with no `X-Aco-Owner` line

```console
$ aco login enable
2> ERROR: login launcher conflicts with an unowned filesystem object
exit 2
```

### E-WS-17 -- `login status` before anything has ever run

Setup: bare-remote, temporary `XDG_CONFIG_HOME`/`XDG_STATE_HOME`, no mapping file

```console
$ aco login status
launcher: disabled
configuration: missing
attempt: no login attempt recorded
exit 0
```

### E-WS-18 -- `login status` after one completed attempt

Setup: bare-remote, temporary `XDG_STATE_HOME`, a hand-written completed attempt record naming `alpha: started`

```console
$ aco login status
launcher: disabled
configuration: missing
attempt: <id> <started_at> completed
alpha: started
completed: <completed_at>
exit 0
```

### E-WS-19 -- a malformed attempt record hides its own bytes

Setup: bare-remote, temporary `XDG_STATE_HOME`, `login-attempt.json` hand-written with an invalid `attempt_id`

```console
$ aco login status
launcher: disabled
configuration: missing
attempt: malformed
exit 1
```

### E-WS-20 -- `_run-at-login` on nothing but console-free preconditions

`_run-at-login` prints nothing itself (`## Never`); this session instead
reads back what it recorded, the only observable trace without a console.

Setup: bare-remote, temporary `XDG_CONFIG_HOME`/`XDG_STATE_HOME`, mapping present with an empty `[projects]` table

```console
$ aco _run-at-login; echo "exit $?"
exit 1
$ aco login status
launcher: disabled
configuration: empty
attempt: <id> <started_at> completed
workspace: workspace failure
completed: <completed_at>
exit 0
```

### E-WS-21 -- a malformed project record, in two of its own shapes

Setup: bare-remote, temporary `XDG_CONFIG_HOME`

```console
$ mkdir -p <tmp>/config/aco
$ printf 'version = 3\nprojects = { alpha = "not-a-table" }\n' > <tmp>/config/aco/workspace.toml
$ aco run
2> ERROR: workspace projects must use project keys and table records
exit 2
$ printf 'version = 3\n[projects.alpha]\npath = "/tmp"\nsession_id = "x"\n' > <tmp>/config/aco/workspace.toml
$ aco run
2> ERROR: project 'alpha' has unsupported or missing fields
exit 2
```

### E-WS-22 -- two records claiming one duplicate identity

Setup: bare-remote, temporary `XDG_CONFIG_HOME`, `<tmp>/alpha` and `<tmp>/beta` exist

```console
$ mkdir -p <tmp>/config/aco
$ printf 'version = 3\n[projects.alpha]\npath = "<tmp>/alpha"\nsession_id = "<uuid>"\nagent = "head"\nprovider = "codex"\n\n[projects.beta]\npath = "<tmp>/alpha"\nsession_id = "123e4567-e89b-12d3-a456-426614174001"\nagent = "head"\nprovider = "codex"\n' > <tmp>/config/aco/workspace.toml
$ aco run
2> ERROR: workspace configuration contains duplicate canonical paths
exit 2
$ printf 'version = 3\n[projects.alpha]\npath = "<tmp>/alpha"\nsession_id = "<uuid>"\nagent = "head"\nprovider = "codex"\n\n[projects.beta]\npath = "<tmp>/beta"\nsession_id = "<uuid>"\nagent = "head"\nprovider = "codex"\n' > <tmp>/config/aco/workspace.toml
$ aco run
2> ERROR: workspace configuration contains duplicate native provider UUIDs
exit 2
```
