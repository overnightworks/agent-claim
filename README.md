# agent-coordination

`agent-coordination` is a small installable CLI that gives coding agents one
claim state per repository: a compare-and-swap git ref, `refs/aco/state`, on
the repository's own canonical remote. It is provider-neutral: Codex, Claude,
Grok, people, and future agents use the same contract. Its command is `aco`.

This file is the operator's view — what aco is, how to install and run it,
and how each workflow fits together. Every command's exact flags, outputs,
exit codes, and refusal sentences belong to one file under `specs/`; the
table at the end names which file owns which command.

## What belongs in aco

A capability belongs in `aco` only if it clears three tests (ruling
19.09.2026): it helps a coding agent coordinate in a shared repository; it
holds what an agent would forget or cannot know alone -- who holds what, what
a landing freed, what's disjoint; and no native tool does it better -- git,
`gh`, and GitLab own commits, branches, pull requests, and issue text. What
fails is ballast: unbuilt, or removed once it fails. aco is meant for every
repository: GitHub today, forges next. Workspace-recovery (`register`, `run`,
`login`) stays: it restores the operator's project heads after a restart.

## Install and maintain

```bash
uv tool install git+https://github.com/overnightworks/agent-claim.git@v3.0.0
# or: pipx install git+https://github.com/overnightworks/agent-claim.git@v3.0.0
uv tool upgrade agent-coordination
uv tool uninstall agent-coordination
```

Local proofs run under the pinned interpreter named in `.python-version`
(currently 3.12); `uv sync` creates the development venv from that file.

Claim state lives as a git tree under `refs/aco/state`, read and written
whole: an unknown key or a malformed record refuses the whole read rather
than patching around it (operator ruling 16.09.2026: no backwards
compatibility). The exact tree shape, versioning, and transport contract are
`specs/ref-store-cas.spec.md`'s own facts.

## Quick start

```bash
aco bootstrap
aco status
aco claim 42 --agent "Ada" --scope src/widget.py
aco release 42 --merged 57
```

`bootstrap` creates the state ref once per repository; every other command
here reads or writes it. `specs/bootstrap.spec.md` owns `bootstrap`;
`specs/claim.spec.md` and `specs/release.spec.md` own `claim` and `release`.

## A GitHub workflow: claim, build, land

```bash
git worktree add ../repo-worktrees/issue-42-widget -b Ada/issue-42-widget
cd ../repo-worktrees/issue-42-widget
aco claim 42 --agent "Ada" --scope src/widget.py
# edit, commit, push, open a pull request naming Closes #42 and Work-Item: #42
aco land 57
```

`claim` refuses before the first edit unless the checkout is already a
linked, isolated worktree on a non-main branch -- create it first, exactly
as shown, naming the issue and agent in both the directory and the branch;
`claim` then opens one live claim there and refuses out-of-order or blocked
work by name unless overridden with `--out-of-order REASON`. `aco land <pull
request>`, from a clean default-branch checkout, verifies it against GitHub
-- mergeable, checks green, body carrying Closes and Work-Item -- merges it
with a merge commit, deletes the branch, removes the lane's worktree, closes
the item, releases the claim, and reports what that landing freed and what
to pull next. The exact preconditions, identity resolution, and refusals are
`specs/claim.spec.md`, `specs/land.spec.md`, and `specs/release.spec.md`'s
own; the claim record itself -- scope, roles, resources, overlap -- is
`specs/claim-record.spec.md`'s.

## Issueless lane claims

A `docs/`- or `fix/`-prefixed branch claims and releases the same way
without a GitHub issue: the branch name is the lane's identity, so `claim`
and `release` take no positional number in this mode.

```bash
git worktree add ../repo-worktrees/docs-tidy-readme -b docs/tidy-readme
cd ../repo-worktrees/docs-tidy-readme
aco claim --agent "Ada" --scope README.md
# edit, commit, push, open a pull request whose body declares No-Item: docs
aco release --merged 58
```

This lane branch must land within the session it was claimed in; it never
appears on `board`, `rulings`, or `next` since it owns no issue. Its pull
request carries `No-Item: docs` or `No-Item: fix` in place of a `Work-Item:`
line -- the one classification `release --merged` accepts for a lane with no
issue to close. `specs/claim.spec.md` owns the exact branch-name grammar and
its refusal when a checkout is not on a matching branch;
`specs/landing-grammar.spec.md` owns the `No-Item:` classification and its
refusals.

## A workflow without a forge

A repository with a `file://` remote and no GitHub coordinates entirely out
of `refs/aco/state`: pin `storage = "state-ref"`, and items live as files
instead of issues.

```bash
git init --bare -b main /srv/aco/repo.git
git clone /srv/aco/repo.git repo && cd repo
mkdir .agent-claim
printf 'storage = "state-ref"\n' > .agent-claim/board.toml
git add -f .agent-claim/board.toml && git commit -m "pin state-ref storage"
git push -u origin main
git remote set-head origin main
aco bootstrap
```

Cut the epic and its first slice, fill in each body, then claim, build, and
land the same way a GitHub lane does -- except a landing is verified from
the trunk commit's own `Work-Item:` trailer instead of a pull request:

```bash
aco item new --kind container --title "Ship the widget"
aco item new --title "Build the widget" --parent <container-id>
aco item edit <item-id> < body.md
aco claim <item-id> --scope src/widget.py
# build, then land a commit carrying "Work-Item: <item-id>" on main
aco release <item-id> --merged
```

`specs/storage-pin.spec.md` owns the pin and the two item-id forms
(`aco-xxxxxx` versus `#n`); `specs/item.spec.md` owns `item new`/`show`/
`edit`/`close`; `specs/landing-grammar.spec.md` owns what a trunk trailer
must say to count as a landing.

## Recovering a provider workspace

```bash
aco register widget --path ~/git/widget --session-id 11111111-1111-1111-1111-111111111111 --agent Ada --stopped
aco run widget
```

`aco register` records a local, deliberate mapping from a project to a
stopped or explicitly selected running native provider conversation
(Codex, Claude, or Grok); `aco run` resumes that mapping in a dedicated
tmux session and opens one desktop console for it, reattaching an existing
one rather than duplicating it. Neither command alters the conversation,
repository files, provider credentials, or a live claim -- registration only
records where to find the conversation again. A stopped registration needs
an explicit acknowledgement, since aco cannot otherwise prove an unmanaged
provider process is still running; registering a live process instead
validates that exact process. Claude's own `claude-revive` SessionStart
hook is a separate recovery owner -- do not run both paths for the same
conversation. The exact flags, refusals, and recovery states are
`specs/workspace.spec.md`'s own.

## Desktop-login autostart

`aco login enable` installs one desktop-login launcher that resumes every
registered project, in order, at each login; `aco login disable` removes
only that launcher; `aco login status` reports launcher and mapping state
without starting anything. Login recovery never sends a prompt or copies
authentication -- it restores each conversation to its native input line and
stops there; use `aco run` for an explicit same-boot retry instead. The
exact preconditions and refusals are also `specs/workspace.spec.md`'s own.

## PreToolUse write gate

Copy this hook once into the file the provider actually loads. Skip when a
`PreToolUse` hook already runs `aco protect`.

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "*",
        "hooks": [
          {
            "type": "command",
            "command": "aco protect",
            "timeout": 60
          }
        ]
      }
    ]
  }
}
```

Install this in the settings of the session that actually runs the
subagents -- the orchestrating head's settings, not each worktree's own --
since every dispatched subagent's tool calls share that one session's
process, cwd included (issue #314). `protect` judges a write from the
payload's own path, never from that shared process cwd, and fails closed on
any tool name it does not recognize. The full judgement order, every denial
reason, and the JSON verdict shape are `specs/protect.spec.md`'s own.

## Configuration

`.agent-claim/board.toml` defines exactly five top-level keys; any other key
is refused by name.

- `storage` -- `"github"` (default) or `"state-ref"`; the pin and its
  precondition are `specs/storage-pin.spec.md`'s own.
- `canonical_remote` -- the git remote name claim state and forge reads use
  (default `origin`).
- `priority_labels` -- an ordered list of labels that rank as critical work
  on the board (default `security`, `data`, `ci`, `product`, `ux`,
  `cleanup`).
- `idea_label` -- the label marking a not-yet-refined idea; `aco next` tells
  the head to refine it before dispatch instead of proposing a build.
- `body_contract` -- must be `"block"` (the only work-item body format aco
  reads) when present; absent means the same thing.

## Board

`aco board` projects the open work board: a fixed-width text table by
default, `--json`, or a static `--html` page -- all three read-only.
`--serve` instead runs a live page on 127.0.0.1 with a one-click ruling form
per expectation line, so it is the one form of this command that writes; its
loopback token persists across restarts and reinstalls (`--new-token` mints
a fresh one), so the token is always stable. The full printed URL is stable
too only once the port is fixed with `--port`: the default `--port 0` picks
a fresh ephemeral port on every start.
Every item that carries a top-level `size = "S"|"M"|"L"` shows a measured
estimate (`~4h (M, n=5)`) only once three or more same-size claims have
landed; fewer measurements, including zero, show `schwach` instead. The
exact text sections, JSON keys, HTML layout, and estimate derivation are
`specs/board.spec.md`'s own.

## Scope and boundaries

GitHub through the `gh` CLI is the one forge adapter that exists today. A
second forge attaches at the same port -- a `ForgeReader`/`ForgeWriter` pair
with a `Capability` answer per operation -- and not inside the commands
themselves, so adding one never changes a command's own contract. `status`,
`protect`, `bootstrap`, `reset`, and a lane `claim`/`rescope`/`release` never
resolve a forge at all: they read and write only `refs/aco/state`, so a
canonical remote on any host, including a bare `file://` path, is no error
for them. aco does not allocate work, merge code, or operate a lease server;
it never writes provider configuration, and never touches `~/.claude`,
`~/.codex`, or `~/.grok` except the one workspace mapping described above.

Most commands accept `--json` for a machine-readable form: one object on
stdout, refusals included. Read it in that order -- the exit code, then
`ok` and `reason`, then the payload. The exit code decides first: a refusal
is never `0`, so a script that reads stdout without it eventually parses a
refusal as an answer (one did, 08.09.2026). `ok` and `reason` are always the
object's first two keys, and `reason` is the token to branch on: a stable
word from that command's own vocabulary, never a sentence to match against,
and never a payload key whose presence you test -- a success may carry `ok`
and `reason` alone. A closing `message` is prose for a person: the refusal's own
sentence, the one the text form prints -- behind `ERROR: ` for most
commands, behind `REFUSED: <sha> ` for `aco check <sha>` -- and optional,
because not every refusal names one. It is no promise of a matching
stderr line: under `--json`, `check` writes the object alone. A usage
error the parser itself raises joins the same object, but only for a
command that declares `--json`: `aco bootstrap --json` stays argparse's own
text on stderr, stdout empty. A non-zero exit is not always a refusal --
`next` exits non-zero on an empty board without refusing anything
(`specs/next.spec.md`). No command carves itself out of this shape any
more: `aco check <sha>` answers in the same `ok`/`reason` object every
other command does, naming its commit under `sha` and its defect sentence
under `message` (issue #435). `specs/output.spec.md` owns the envelope;
each command's spec owns its own `reason` values.

## Commands and their specs

Every command's flags, outputs, exit codes, and refusal sentences are owned
by exactly one file below; this table is the map, not a copy.

| Command / contract | Spec | What it covers |
|---|---|---|
| `aco bootstrap` | `specs/bootstrap.spec.md` | creates or reports the state ref |
| `aco claim` | `specs/claim.spec.md` | opens a claim on an issue or an issueless lane |
| `aco rescope` | `specs/rescope.spec.md` | adds or drops paths on a live claim |
| `aco release` | `specs/release.spec.md` | ends a claim as merged or abandoned |
| `aco status` | `specs/status.spec.md` | reads every live claim, repository-wide or by path |
| `aco reset` | `specs/reset.spec.md` | rebuilds a broken or rewritten state ref |
| `aco check` | `specs/check.spec.md` | answers whether a pull request, an issue, or a trunk commit is sound |
| `aco brief` | `specs/brief.spec.md` | composes one item's body, claim, tip, and touched files |
| `aco board` | `specs/board.spec.md` | projects the open board (text, `--json`, `--html`, `--serve`) |
| `aco ask` | `specs/ask.spec.md` | proposes one expectation line on an item |
| `aco rule` | `specs/rule.spec.md` | rules one proposed expectation line |
| `aco rulings` | `specs/rulings.spec.md` | lists every item with an open expectation line |
| `aco next` | `specs/next.spec.md` | names the one action the board recommends pulling now |
| `aco cut` | `specs/cut.spec.md` | dispatches a container's next slice as a fresh child |
| `aco item new/show/edit/close` | `specs/item.spec.md` | the state-ref item lifecycle |
| `aco body --check` | `specs/body.spec.md` | validates a piped body offline |
| `aco protect` | `specs/protect.spec.md` | the `PreToolUse` hook's write verdict |
| `aco register/run/login` | `specs/workspace.spec.md` | records, resumes, and autostarts a provider workspace mapping |
| `agent-claim` block grammar | `specs/body-block.spec.md` | the fenced TOML block every item-reading command parses |
| claim record | `specs/claim-record.spec.md` | the stored claim fields `claim`/`release`/`rescope`/`status`/`protect` share |
| state ref transport | `specs/ref-store-cas.spec.md` | the compare-and-swap `refs/aco/state` every store command reads and writes |
| landing grammar | `specs/landing-grammar.spec.md` | what counts as a landing, read by `check`, `release --merged`, and `board` |
| storage pin | `specs/storage-pin.spec.md` | the `storage` key gating GitHub versus state-ref item storage |

`register`/`run`/`login` are documented above as a workflow; their own spec,
`specs/workspace.spec.md`, is landing separately (issue #358).
