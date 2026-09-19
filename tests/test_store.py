"""`refs/aco/state` store behaviour: the git transport `bootstrap` exercises.

Every test here drives real git subprocesses against a local bare repository
standing in for the canonical remote -- this module's whole job is git
transport, so its tests are the thin integration layer the coding
conventions reserve for exactly that, never a re-implementation of git
semantics in Python.
"""

from __future__ import annotations

import subprocess
import threading
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

import pytest
from cli_fixtures import stub_board_config_tracked
from test_cli import FakeForge

from agent_coordination import cli as issue_claim
from agent_coordination import github, process, protocol, store

# Syntactically valid but locally unresolvable object ids, for tests that
# exercise a failure path where the actual value never reaches an assertion.
_UNRESOLVABLE_OBJECT_ID = protocol.ObjectId("0" * 40)
_PLACEHOLDER_TIP = protocol.ObjectId("1" * 40)
# `schema.toml`'s content one version below `SUPPORTED_STATE_SCHEMA_VERSION`
# (2, issue #248's `items/` directory): unsupported for the same reason
# version 3 is, and reused verbatim wherever a test needs any syntactically
# valid `schema.toml` body that is not the currently supported one.
_SCHEMA_TOML_VERSION_ONE = b"version = 1\n"


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    """A commit identity for every git subprocess this test file spawns,
    including the ones `store` itself runs (it inherits the process
    environment, never overriding it) -- this machine may carry no git
    `user.name`/`user.email` at all.
    """
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")


@pytest.fixture(autouse=True)
def _stub_board_config_tracked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every store-command test reads a tracked `board.toml` by default
    (issue #315): the `bootstrap` proofs below drive a real `worktree` that
    never `git add`s `.agent-claim/board.toml` -- it has no reason to carry
    one, since `board.load_config` already defaults `canonical_remote` to
    `"origin"`, the remote these tests add -- so a real `git ls-files` check
    would otherwise always read "not tracked" here and refuse before
    `bootstrap` gets to run at all."""
    stub_board_config_tracked(monkeypatch)


@pytest.fixture
def git_call_spy(monkeypatch: pytest.MonkeyPatch) -> Counter[str]:
    """Counts real git subprocess invocations by subcommand, patched at the
    `process` chokepoint both of `store`'s call shapes go through --
    `_run_git` (`process.run_captured`) and `_run_git_with_input`
    (`process.run_bounded`) -- the proof that a transition's or a read's
    git-invocation count is independent of how many claims the state tree
    holds (issue #241).
    """
    counts: Counter[str] = Counter()

    def record(command: list[str]) -> None:
        if command[0] == "git":
            counts[command[3]] += 1

    def spy(real: Callable[..., object]) -> Callable[..., object]:
        def wrapped(command: list[str], **kwargs: object) -> object:
            record(command)
            return real(command, **kwargs)

        return wrapped

    monkeypatch.setattr(store.process, "run_captured", spy(process.run_captured))
    monkeypatch.setattr(store.process, "run_bounded", spy(process.run_bounded))
    return counts


def _git(*arguments: str, cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    """An empty bare repository standing in for the canonical remote."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--bare", "-b", "main", cwd=remote)
    return remote


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """An ordinary git checkout used as this test's client worktree.

    Independent of `bare_remote`: store operations reach the remote by path,
    never by a configured `origin`, so this repo's own history is unrelated
    to the state ref it reads and writes.
    """
    checkout = tmp_path / "worktree"
    checkout.mkdir()
    _git("init", "-b", "main", cwd=checkout)
    (checkout / "README").write_text("placeholder\n")
    _git("add", "README", cwd=checkout)
    _git("commit", "-m", "initial", cwd=checkout)
    return checkout


def _has_ref(worktree: Path, ref: str) -> bool:
    result = subprocess.run(
        ["git", "-C", str(worktree), "show-ref", "--verify", "--quiet", ref],
        check=False,
        capture_output=True,
    )
    return result.returncode == 0


def _state_ref_oid(remote: Path) -> str | None:
    result = subprocess.run(
        ["git", "ls-remote", "--exit-code", str(remote), store.STATE_REF],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    return result.stdout.split("\t", 1)[0]


def _tree_entries(remote: Path, tip: str) -> dict[str, str]:
    """`{name: content}` for every top-level blob in `tip`'s tree, read from `remote`."""
    listing = subprocess.run(
        ["git", "--git-dir", str(remote), "ls-tree", f"{tip}^{{tree}}"],
        check=True,
        capture_output=True,
        text=True,
    )
    entries: dict[str, str] = {}
    for line in listing.stdout.splitlines():
        mode_type, _, name = line.partition("\t")
        _mode, _kind, blob_oid = mode_type.split(" ")
        content = subprocess.run(
            ["git", "--git-dir", str(remote), "cat-file", "-p", blob_oid],
            check=True,
            capture_output=True,
            text=True,
        )
        entries[name] = content.stdout
    return entries


def _push_custom_tree(
    remote: Path, worktree: Path, *, parent: str | None, files: dict[str, bytes]
) -> str:
    """Push an arbitrary tree onto `STATE_REF`, bypassing `store` entirely.

    Test scaffolding for constructing malformed or rewritten remote states
    that `store`'s own write path can never produce -- it never writes
    anything but a well-formed `schema.toml`-only tree.
    """
    blob_oids = {}
    for name, content in files.items():
        hashed = subprocess.run(
            ["git", "-C", str(worktree), "hash-object", "-w", "--stdin"],
            input=content,
            check=True,
            capture_output=True,
        )
        blob_oids[name] = hashed.stdout.decode().strip()
    mktree_input = "".join(f"100644 blob {oid}\t{name}\n" for name, oid in blob_oids.items())
    tree = (
        subprocess.run(
            ["git", "-C", str(worktree), "mktree"],
            input=mktree_input.encode(),
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )
    commit_arguments = ["commit-tree", tree, "-m", "test fixture"]
    if parent is not None:
        commit_arguments += ["-p", parent]
    commit = (
        subprocess.run(
            ["git", "-C", str(worktree), *commit_arguments],
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )
    subprocess.run(
        ["git", "-C", str(worktree), "push", "--force", str(remote), f"{commit}:{store.STATE_REF}"],
        check=True,
        capture_output=True,
    )
    return commit


def _raw_tree(worktree: Path, entries: list[tuple[str, str, str, str]]) -> str:
    """Build a tree object directly from `(mode, kind, oid, name)` entries via
    `git mktree`, which does not itself verify that a referenced oid exists --
    letting tests build the dangling or wrong-kind trees `store`'s own write
    path can never produce."""
    mktree_input = "".join(f"{mode} {kind} {oid}\t{name}\n" for mode, kind, oid, name in entries)
    return (
        subprocess.run(
            ["git", "-C", str(worktree), "mktree"],
            input=mktree_input.encode(),
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )


def _blob(worktree: Path, content: bytes) -> str:
    return (
        subprocess.run(
            ["git", "-C", str(worktree), "hash-object", "-w", "--stdin"],
            input=content,
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )


def _push_raw_state_tree(
    remote: Path, worktree: Path, entries: list[tuple[str, str, str, str]]
) -> str:
    """Push an arbitrary top-level tree (built via `_raw_tree`) onto `STATE_REF`,
    for the malformed shapes `store`'s own write path can never produce."""
    tree = _raw_tree(worktree, entries)
    commit = (
        subprocess.run(
            ["git", "-C", str(worktree), "commit-tree", tree, "-m", "test fixture"],
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )
    subprocess.run(
        ["git", "-C", str(worktree), "push", "--force", str(remote), f"{commit}:{store.STATE_REF}"],
        check=True,
        capture_output=True,
    )
    return commit


class _AcceptThenRaiseTransport:
    """A `PushTransport` that performs the real push once, then raises --
    reproducing a lost response after the remote actually advanced
    (criterion 3's seam)."""

    def __init__(self) -> None:
        self.calls = 0
        self._real = store.GitPushTransport()

    def push(self, *, worktree: Path, remote: str, ref: str, new_oid: protocol.ObjectId) -> None:
        self.calls += 1
        self._real.push(worktree=worktree, remote=remote, ref=ref, new_oid=new_oid)
        raise protocol.PushRejectedError("simulated lost response")


class _AlwaysRejectingTransport:
    """A `PushTransport` that never lands a push -- exhausts the retry loop
    without the ref ever moving (issue #237 finding 22's stuck-lock case)."""

    def push(self, *, worktree: Path, remote: str, ref: str, new_oid: protocol.ObjectId) -> None:
        raise protocol.PushRejectedError("simulated permanent rejection")


class _AlwaysRacingTransport:
    """A `PushTransport` where a concurrent writer always lands first: each
    call pushes one real, distinct commit onto the ref before raising, so
    every retry attempt observes it having genuinely moved (issue #237
    finding 22's race case, as opposed to `_AlwaysRejectingTransport`'s
    stuck ref)."""

    def __init__(self) -> None:
        self._real = store.GitPushTransport()
        self._rivals = 0

    def push(self, *, worktree: Path, remote: str, ref: str, new_oid: protocol.ObjectId) -> None:
        self._rivals += 1
        current = store._ls_remote_state(worktree, remote)
        rival_commit = store._commit_tree(
            worktree,
            tree_oid=store._write_bootstrap_tree(worktree),
            parent=current,
            message=f"rival write\n\noperation_id: rival-{self._rivals}\n",
        )
        self._real.push(worktree=worktree, remote=remote, ref=ref, new_oid=rival_commit)
        raise protocol.PushRejectedError("simulated concurrent writer")


class _MovesOnceThenSticksTransport:
    """A `PushTransport` where a concurrent writer's commit lands first on
    exactly the first attempt, then the ref sits fixed while every further
    push is rejected -- issue #237 finding 22's mixed case, neither
    `_AlwaysRacingTransport`'s pure race nor `_AlwaysRejectingTransport`'s
    pure stuck ref."""

    def __init__(self) -> None:
        self._real = store.GitPushTransport()
        self._calls = 0

    def push(self, *, worktree: Path, remote: str, ref: str, new_oid: protocol.ObjectId) -> None:
        self._calls += 1
        if self._calls == 1:
            current = store._ls_remote_state(worktree, remote)
            rival_commit = store._commit_tree(
                worktree,
                tree_oid=store._write_bootstrap_tree(worktree),
                parent=current,
                message="rival write\n\noperation_id: rival-1\n",
            )
            self._real.push(worktree=worktree, remote=remote, ref=ref, new_oid=rival_commit)
        raise protocol.PushRejectedError("simulated mixed retry")


def test_bootstrap_creates_the_empty_state_tree_on_a_proven_empty_remote(
    bare_remote: Path, worktree: Path
) -> None:
    assert _state_ref_oid(bare_remote) is None

    tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))

    assert _state_ref_oid(bare_remote) == tip
    assert _tree_entries(bare_remote, tip) == {"schema.toml": "version = 2\n"}


def test_bootstrap_is_a_no_op_read_when_the_ref_already_exists(
    bare_remote: Path, worktree: Path
) -> None:
    first = store.bootstrap(worktree=worktree, remote=str(bare_remote))

    second = store.bootstrap(worktree=worktree, remote=str(bare_remote))

    assert second == first
    log = subprocess.run(
        ["git", "--git-dir", str(bare_remote), "rev-list", "--count", store.STATE_REF],
        check=True,
        capture_output=True,
        text=True,
    )
    assert log.stdout.strip() == "1"


def test_bootstrap_fails_loud_on_an_unreachable_remote(tmp_path: Path, worktree: Path) -> None:
    unreachable = tmp_path / "does-not-exist"

    with pytest.raises(protocol.ClaimError, match="auth or transport failure"):
        store.bootstrap(worktree=worktree, remote=str(unreachable))


def test_state_ref_is_never_checked_out(bare_remote: Path, worktree: Path) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))

    assert not (worktree / "schema.toml").exists()
    status = _git("status", "--porcelain", cwd=worktree)
    assert status.stdout == ""
    local_refs = _git("for-each-ref", store.STATE_REF, cwd=worktree)
    assert local_refs.stdout == ""


def test_fetch_state_reads_via_fetch_head_without_creating_the_shared_state_ref(
    bare_remote: Path, worktree: Path, tmp_path: Path
) -> None:
    """`fetch_state` never creates `STATE_REF` itself in the local, shared
    ref namespace (`_fetch_to_fetch_head`'s own contract) -- it anchors the
    fetched tip under `refs/worktree/...` instead (issue #237 finding 25),
    git's own per-worktree namespace, never the one `STATE_REF` reserves."""
    created = store.bootstrap(worktree=worktree, remote=str(bare_remote))
    reader = tmp_path / "reader"
    reader.mkdir()
    _git("init", "-b", "main", cwd=reader)

    state = store.fetch_state(worktree=reader, remote=str(bare_remote))

    assert state.tip == created
    assert _git("for-each-ref", store.STATE_REF, cwd=reader).stdout == ""
    fetch_head = (reader / ".git" / "FETCH_HEAD").read_text()
    assert fetch_head.startswith(created)
    anchor = _git("rev-parse", store._FETCH_ANCHOR_REF, cwd=reader).stdout.strip()
    assert anchor == created


@pytest.mark.parametrize(
    ("files", "expected_error", "match"),
    [
        pytest.param(
            {"schema.toml": _SCHEMA_TOML_VERSION_ONE, "extra.txt": b"stray\n"},
            protocol.MalformedStateTreeError,
            "unknown entries",
            id="extra-file",
        ),
        pytest.param(
            {"schema.toml": b'name = "wrong-key"\n'},
            protocol.MalformedStateTreeError,
            "must contain exactly 'version'",
            id="wrong-key",
        ),
        pytest.param(
            {"schema.toml": b'version = "1"\n'},
            protocol.MalformedStateTreeError,
            "must be an integer",
            id="non-integer-version",
        ),
        pytest.param(
            {"schema.toml": b"version = 1 = broken\n"},
            protocol.MalformedStateTreeError,
            "malformed schema.toml",
            id="unparsable-toml",
        ),
        pytest.param(
            {"schema.toml": b"version = 3\n"},
            protocol.UnsupportedStateSchemaError,
            "unsupported state schema version 3",
            id="unsupported-version",
        ),
    ],
)
def test_fetch_state_rejects_a_malformed_or_unsupported_tree(
    bare_remote: Path,
    worktree: Path,
    files: dict[str, bytes],
    expected_error: type[Exception],
    match: str,
) -> None:
    _push_custom_tree(bare_remote, worktree, parent=None, files=files)

    with pytest.raises(expected_error, match=match):
        store.fetch_state(worktree=worktree, remote=str(bare_remote))


def test_fetch_state_rejects_schema_version_one_without_stamping_its_lineage(
    bare_remote: Path, worktree: Path
) -> None:
    """`version = 1` predates `SUPPORTED_STATE_SCHEMA_VERSION = 2` (issue
    #248's `items/` directory): it is exactly as unsupported as the version-3
    case above, and `_parse_state_tree` must raise before `fetch_state` ever
    reaches `_write_lineage_stamp`, so a first, unsupported observation never
    poisons the lineage check a later, supported fetch relies on.
    """
    _push_custom_tree(
        bare_remote, worktree, parent=None, files={"schema.toml": _SCHEMA_TOML_VERSION_ONE}
    )

    with pytest.raises(
        protocol.UnsupportedStateSchemaError, match="unsupported state schema version 1"
    ):
        store.fetch_state(worktree=worktree, remote=str(bare_remote))

    assert store._read_lineage_stamp(worktree) is None


def test_fetch_state_rejects_a_tree_missing_schema_toml(bare_remote: Path, worktree: Path) -> None:
    empty_tree = _raw_tree(worktree, [])
    _push_raw_state_tree(
        bare_remote, worktree, [("040000", "tree", empty_tree, store.CLAIMS_DIRECTORY)]
    )

    with pytest.raises(protocol.MalformedStateTreeError, match=r"missing schema\.toml"):
        store.fetch_state(worktree=worktree, remote=str(bare_remote))


def test_fetch_state_rejects_a_claims_entry_that_is_not_a_directory(
    bare_remote: Path, worktree: Path
) -> None:
    schema_blob = _blob(worktree, b"version = 2\n")
    claims_blob = _blob(worktree, b"not a tree\n")
    _push_raw_state_tree(
        bare_remote,
        worktree,
        [
            ("100644", "blob", schema_blob, "schema.toml"),
            ("100644", "blob", claims_blob, store.CLAIMS_DIRECTORY),
        ],
    )

    with pytest.raises(protocol.MalformedStateTreeError, match="is not a directory"):
        store.fetch_state(worktree=worktree, remote=str(bare_remote))


def test_fetch_state_rejects_a_non_toml_entry_in_claims(bare_remote: Path, worktree: Path) -> None:
    schema_blob = _blob(worktree, b"version = 2\n")
    stray_blob = _blob(worktree, b"junk\n")
    claims_tree = _raw_tree(worktree, [("100644", "blob", stray_blob, "issue-42.txt")])
    _push_raw_state_tree(
        bare_remote,
        worktree,
        [
            ("100644", "blob", schema_blob, "schema.toml"),
            ("040000", "tree", claims_tree, store.CLAIMS_DIRECTORY),
        ],
    )

    with pytest.raises(protocol.MalformedStateTreeError, match="is not a claim file"):
        store.fetch_state(worktree=worktree, remote=str(bare_remote))


def test_fetch_state_rejects_an_invalid_id_entry(bare_remote: Path, worktree: Path) -> None:
    schema_blob = _blob(worktree, b"version = 2\n")
    empty_blob = _blob(worktree, b"")
    ids_tree = _raw_tree(worktree, [("100644", "blob", empty_blob, "not valid!")])
    _push_raw_state_tree(
        bare_remote,
        worktree,
        [
            ("100644", "blob", schema_blob, "schema.toml"),
            ("040000", "tree", ids_tree, store.IDS_DIRECTORY),
        ],
    )

    with pytest.raises(protocol.MalformedStateTreeError, match="is not a claim id"):
        store.fetch_state(worktree=worktree, remote=str(bare_remote))


def test_fetch_state_canonicalizes_a_stored_unsorted_scope_without_rewriting_the_ref(
    bare_remote: Path, worktree: Path
) -> None:
    """A claim file written before issue #331 sorted every scope at
    creation can still carry its paths in typed order on the real ref:
    `parse_claim_toml` must project it through `protocol._valid_scope` on
    read (REVISE finding 1) rather than expose the stored order verbatim.
    `status`/`status --json` (`cli._print_claim_status_lines`,
    `cli._status_json`) print straight from this same `ActiveClaim.scope`,
    so canonicalizing it here is their canonical-order proof too -- there
    is no second scope-ordering decision downstream for them to get wrong.

    The read must never rewrite the ref, and a replayed claim intent (the
    same identity, claimant, branch, and scope, differently typed) must
    still match the now-canonical stored claim -- criterion 2's idempotent
    replay, which a raw order mismatch used to break -- and a release by
    claim id must still succeed regardless of the stored order.
    """
    schema_blob = _blob(worktree, b"version = 2\n")
    sha = "c" * 40
    claim_content = (
        'claim_id = "a1"\n'
        'agent = "Ada"\n'
        'role = "builder"\n'
        f'base = "{sha}"\n'
        'branch = "claude/issue-42-cut"\n'
        'scope = ["scripts/issue_claim.py", "docs/COORDINATION.md"]\n'
        f'opened_commit = "{sha}"\n'
    )
    claim_blob = _blob(worktree, claim_content.encode())
    claims_tree = _raw_tree(worktree, [("100644", "blob", claim_blob, "issue-42.toml")])
    id_blob = _blob(worktree, b"")
    ids_tree = _raw_tree(worktree, [("100644", "blob", id_blob, "a1")])
    tip = _push_raw_state_tree(
        bare_remote,
        worktree,
        [
            ("100644", "blob", schema_blob, "schema.toml"),
            ("040000", "tree", claims_tree, store.CLAIMS_DIRECTORY),
            ("040000", "tree", ids_tree, store.IDS_DIRECTORY),
        ],
    )

    state = store.fetch_state(worktree=worktree, remote=str(bare_remote))

    canonical_scope = ("docs/COORDINATION.md", "scripts/issue_claim.py")
    assert state.claims["issue-42"].scope == canonical_scope
    assert state.consumed_ids == frozenset({protocol.ClaimId("a1")})
    assert _state_ref_oid(bare_remote) == tip

    replay = protocol.ClaimIntent(
        identity=protocol.IssueIdentity(42),
        agent="Ada",
        role="builder",
        base=protocol.ObjectId(sha),
        branch="claude/issue-42-cut",
        scope=protocol._valid_scope(["docs/COORDINATION.md", "scripts/issue_claim.py"]),
        claim_id=protocol.ClaimId("a1"),
        operation_id="op-replay",
    )

    assert protocol.apply(state, replay) == state

    release = protocol.ReleaseIntent(
        claim_id=protocol.ClaimId("a1"),
        agent="Ada",
        role="builder",
        outcome=protocol.AbandonedRelease("done"),
        operation_id="op-release",
    )

    released = protocol.apply(state, release)

    assert "issue-42" not in released.claims


def test_fetch_state_rejects_a_stored_scope_entry_valid_scope_refuses(
    bare_remote: Path, worktree: Path
) -> None:
    """The other half of `_claim_toml_scope`'s canonicalization (issue #331
    REVISE finding 1): a stored scope only reads as a legacy record when
    `_valid_scope` itself would still accept it, just unsorted -- content
    it would refuse from a fresh request (here an absolute path) fails
    loud on read too, named by the claim key, never silently passed
    through."""
    schema_blob = _blob(worktree, b"version = 2\n")
    sha = "c" * 40
    claim_content = (
        'claim_id = "a1"\n'
        'agent = "Ada"\n'
        'role = "builder"\n'
        f'base = "{sha}"\n'
        'branch = "claude/issue-42-cut"\n'
        'scope = ["/etc/passwd"]\n'
        f'opened_commit = "{sha}"\n'
    )
    claim_blob = _blob(worktree, claim_content.encode())
    claims_tree = _raw_tree(worktree, [("100644", "blob", claim_blob, "issue-42.toml")])
    _push_raw_state_tree(
        bare_remote,
        worktree,
        [
            ("100644", "blob", schema_blob, "schema.toml"),
            ("040000", "tree", claims_tree, store.CLAIMS_DIRECTORY),
        ],
    )

    with pytest.raises(
        protocol.MalformedStateTreeError, match=r"claim file issue-42\.toml .* has an invalid scope"
    ):
        store.fetch_state(worktree=worktree, remote=str(bare_remote))


def test_fetch_state_rejects_a_non_toml_entry_in_resources(
    bare_remote: Path, worktree: Path
) -> None:
    schema_blob = _blob(worktree, b"version = 2\n")
    stray_blob = _blob(worktree, b"junk\n")
    resources_tree = _raw_tree(worktree, [("100644", "blob", stray_blob, "display.txt")])
    _push_raw_state_tree(
        bare_remote,
        worktree,
        [
            ("100644", "blob", schema_blob, "schema.toml"),
            ("040000", "tree", resources_tree, store.RESOURCES_DIRECTORY),
        ],
    )

    with pytest.raises(protocol.MalformedStateTreeError, match="is not a resource file"):
        store.fetch_state(worktree=worktree, remote=str(bare_remote))


def test_fetch_state_never_archives_items_content(
    bare_remote: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`items/` (issue #248) is a recognized top-level entry, but
    `_parse_state_tree`'s own archive excludes it by naming exactly the
    paths it wants (never a bare `git archive <tree>`): reading claim state
    never pays to fetch item content."""
    archive_commands: list[list[str]] = []

    def spy(real: Callable[..., object]) -> Callable[..., object]:
        def wrapped(command: list[str], **kwargs: object) -> object:
            if command[0] == "git" and "archive" in command:
                archive_commands.append(command)
            return real(command, **kwargs)

        return wrapped

    monkeypatch.setattr(store.process, "run_captured", spy(process.run_captured))
    schema_blob = _blob(worktree, protocol.serialize_empty_schema_toml().encode())
    item_blob = _blob(worktree, b"item body\n")
    items_tree = _raw_tree(worktree, [("100644", "blob", item_blob, "aco-000001.md")])
    _push_raw_state_tree(
        bare_remote,
        worktree,
        [
            ("100644", "blob", schema_blob, "schema.toml"),
            ("040000", "tree", items_tree, store.ITEMS_DIRECTORY),
        ],
    )

    state = store.fetch_state(worktree=worktree, remote=str(bare_remote))

    assert state.claims == {}
    assert len(archive_commands) == 1
    assert "items" not in archive_commands[0]
    assert "schema.toml" in archive_commands[0]


def test_read_item_files_is_empty_when_the_items_directory_is_absent(
    bare_remote: Path, worktree: Path
) -> None:
    tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))

    assert store.read_item_files(worktree, tip) == {}


def test_read_item_files_reads_every_blob_under_items(bare_remote: Path, worktree: Path) -> None:
    schema_blob = _blob(worktree, protocol.serialize_empty_schema_toml().encode())
    item_blob = _blob(worktree, b"item body\n")
    items_tree = _raw_tree(worktree, [("100644", "blob", item_blob, "aco-000001.md")])
    tip = protocol.ObjectId(
        _push_raw_state_tree(
            bare_remote,
            worktree,
            [
                ("100644", "blob", schema_blob, "schema.toml"),
                ("040000", "tree", items_tree, store.ITEMS_DIRECTORY),
            ],
        )
    )

    assert store.read_item_files(worktree, tip) == {"aco-000001.md": b"item body\n"}


def test_read_item_files_rejects_a_non_blob_entry(bare_remote: Path, worktree: Path) -> None:
    schema_blob = _blob(worktree, protocol.serialize_empty_schema_toml().encode())
    inner_tree = _raw_tree(worktree, [])
    items_tree = _raw_tree(worktree, [("040000", "tree", inner_tree, "aco-000001.md")])
    tip = protocol.ObjectId(
        _push_raw_state_tree(
            bare_remote,
            worktree,
            [
                ("100644", "blob", schema_blob, "schema.toml"),
                ("040000", "tree", items_tree, store.ITEMS_DIRECTORY),
            ],
        )
    )

    with pytest.raises(protocol.MalformedStateTreeError, match="is not a file"):
        store.read_item_files(worktree, tip)


def test_fetch_state_rejects_a_non_blob_entry_in_items(bare_remote: Path, worktree: Path) -> None:
    """`ClaimState.items` (issue #279) enforces the same "every entry is a
    blob" structural shape `read_item_files` already enforces -- proven here
    through the state-parsing side rather than the lazy content read."""
    schema_blob = _blob(worktree, protocol.serialize_empty_schema_toml().encode())
    inner_tree = _raw_tree(worktree, [])
    items_tree = _raw_tree(worktree, [("040000", "tree", inner_tree, "aco-000001.md")])
    _push_raw_state_tree(
        bare_remote,
        worktree,
        [
            ("100644", "blob", schema_blob, "schema.toml"),
            ("040000", "tree", items_tree, store.ITEMS_DIRECTORY),
        ],
    )

    with pytest.raises(protocol.MalformedStateTreeError, match="is not a file"):
        store.fetch_state(worktree=worktree, remote=str(bare_remote))


def test_read_state_archive_short_circuits_on_an_empty_paths_list(worktree: Path) -> None:
    """An empty `paths` sequence means "nothing to fetch" (issue #248): `git
    archive -- ` with zero path arguments would otherwise archive the whole
    tree, silently defeating the exclusion `_parse_state_tree` relies on."""
    schema_blob = _blob(worktree, protocol.serialize_empty_schema_toml().encode())
    tree = protocol.ObjectId(_raw_tree(worktree, [("100644", "blob", schema_blob, "schema.toml")]))

    assert store._read_state_archive(worktree, tree, tip=_PLACEHOLDER_TIP, paths=()) == {}


def test_lineage_error_when_the_ref_is_rewritten_without_this_worktrees_stamp_as_an_ancestor(
    bare_remote: Path, worktree: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))  # first observation, stamps it

    _push_custom_tree(
        bare_remote, worktree, parent=None, files={"schema.toml": b"version = 2\n"}
    )  # unrelated root commit: not a descendant of the stamped tip

    with pytest.raises(protocol.StateLineageError, match="may have been rewritten"):
        store.fetch_state(worktree=worktree, remote=str(bare_remote))


def test_first_fetch_in_a_worktree_accepts_any_tip_without_a_prior_stamp(
    bare_remote: Path, worktree: Path, tmp_path: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    fresh = tmp_path / "fresh"
    fresh.mkdir()
    _git("init", "-b", "main", cwd=fresh)

    state = store.fetch_state(worktree=fresh, remote=str(bare_remote))

    assert state.tip is not None


def test_linked_worktrees_fetch_concurrently_and_write_distinct_lineage_stamps(
    tmp_path: Path, bare_remote: Path
) -> None:
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    _git("init", "-b", "main", cwd=main_repo)
    (main_repo / "README").write_text("placeholder\n")
    _git("add", "README", cwd=main_repo)
    _git("commit", "-m", "initial", cwd=main_repo)
    linked_a = tmp_path / "linked-a"
    linked_b = tmp_path / "linked-b"
    _git("worktree", "add", "-b", "lane-a", str(linked_a), cwd=main_repo)
    _git("worktree", "add", "-b", "lane-b", str(linked_b), cwd=main_repo)
    tip = store.bootstrap(worktree=main_repo, remote=str(bare_remote))

    barrier = threading.Barrier(2)
    results: dict[Path, protocol.ClaimState] = {}

    def fetch(worktree: Path) -> None:
        barrier.wait()
        results[worktree] = store.fetch_state(worktree=worktree, remote=str(bare_remote))

    threads = [
        threading.Thread(target=fetch, args=(linked_a,)),
        threading.Thread(target=fetch, args=(linked_b,)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert results[linked_a].tip == tip
    assert results[linked_b].tip == tip
    common_dir = main_repo / ".git"
    git_dir_a = store._git_dir(linked_a)
    git_dir_b = store._git_dir(linked_b)
    # Each linked worktree's stamp lives under its own `.git/worktrees/<name>`,
    # never the shared common dir the two worktrees would otherwise collide on.
    assert git_dir_a != git_dir_b
    assert git_dir_a != common_dir
    assert git_dir_b != common_dir
    stamp_a = git_dir_a / "aco" / "last-oid"
    stamp_b = git_dir_b / "aco" / "last-oid"
    assert stamp_a.read_text().strip() == tip
    assert stamp_b.read_text().strip() == tip


def test_push_retry_finds_the_operation_id_after_an_accept_then_raise_and_does_not_apply_twice(
    bare_remote: Path, worktree: Path
) -> None:
    observed = store.fetch_state(worktree=worktree, remote=str(bare_remote))
    transport = _AcceptThenRaiseTransport()
    operation_id = "operation-under-test"
    pending = store.PendingCommit(
        tree_oid=store._write_bootstrap_tree(worktree),
        message=f"bootstrap empty claim state\n\noperation_id: {operation_id}\n",
        operation_id=operation_id,
    )

    result = store.push_tree(
        worktree=worktree,
        remote=str(bare_remote),
        observed=observed,
        pending=pending,
        transport=transport,
    )

    assert transport.calls == 1
    assert isinstance(result, protocol.OperationAlreadyApplied)
    log = subprocess.run(
        ["git", "--git-dir", str(bare_remote), "rev-list", "--count", store.STATE_REF],
        check=True,
        capture_output=True,
        text=True,
    )
    assert log.stdout.strip() == "1"
    assert result.tip == _state_ref_oid(bare_remote)


def test_push_retry_exhausts_and_names_a_stuck_lock_when_the_ref_never_moves(
    bare_remote: Path, worktree: Path
) -> None:
    """Issue #237 finding 22: every attempt was rejected without the ref
    ever advancing (`_AlwaysRejectingTransport` never touches the remote),
    so exhaustion names a stuck lock or missing rights on `remote`, never
    "moved N times" -- that phrase would blame a race that never happened.
    """
    # Bootstrapping first gives `observed.tip` a real value, so every retry's
    # `_commit_tree` builds onto a non-None parent -- the ordinary case once
    # the ref already exists, not just the from-empty case slice C1 mostly
    # exercises elsewhere.
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    observed = store.fetch_state(worktree=worktree, remote=str(bare_remote))
    pending = store.PendingCommit(
        tree_oid=store._write_bootstrap_tree(worktree),
        message="bootstrap empty claim state\n\noperation_id: never-applied\n",
        operation_id="never-applied",
    )
    transport = _AlwaysRejectingTransport()

    with pytest.raises(protocol.ClaimUnavailableError, match="rejected 8 pushes") as raised:
        store.push_tree(
            worktree=worktree,
            remote=str(bare_remote),
            observed=observed,
            pending=pending,
            transport=transport,
        )
    assert "without the ref ever moving" in str(raised.value)


def test_push_retry_exhausts_and_names_a_race_when_the_ref_keeps_moving(
    bare_remote: Path, worktree: Path
) -> None:
    """Issue #237 finding 22's other half: a concurrent writer's commit
    genuinely lands first on every attempt (`_AlwaysRacingTransport`
    actually advances the ref each time), so exhaustion names a real race,
    never the stuck-lock repair sentence that would misdiagnose it.
    """
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    observed = store.fetch_state(worktree=worktree, remote=str(bare_remote))
    pending = store.PendingCommit(
        tree_oid=store._write_bootstrap_tree(worktree),
        message="bootstrap empty claim state\n\noperation_id: never-applied\n",
        operation_id="never-applied",
    )
    transport = _AlwaysRacingTransport()

    with pytest.raises(protocol.ClaimUnavailableError, match="moved 8 times") as raised:
        store.push_tree(
            worktree=worktree,
            remote=str(bare_remote),
            observed=observed,
            pending=pending,
            transport=transport,
        )
    assert "stuck" not in str(raised.value)
    assert "lock" not in str(raised.value)


def test_push_retry_exhausts_and_names_the_true_mix_when_the_ref_moves_once_then_sticks(
    bare_remote: Path, worktree: Path
) -> None:
    """Issue #237 finding 22's mixed case: a concurrent writer's commit
    lands first on exactly the first attempt (`_MovesOnceThenSticksTransport`),
    then the ref sits fixed while every later push is rejected. Reporting
    "moved 8 times" here -- the wrong count the shared builder used to
    print whenever more than one tip was ever observed -- would blame a race
    that stopped after one move; the true count is one move, then seven
    pushes rejected without the ref moving again.
    """
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    observed = store.fetch_state(worktree=worktree, remote=str(bare_remote))
    pending = store.PendingCommit(
        tree_oid=store._write_bootstrap_tree(worktree),
        message="bootstrap empty claim state\n\noperation_id: never-applied\n",
        operation_id="never-applied",
    )
    transport = _MovesOnceThenSticksTransport()

    with pytest.raises(protocol.ClaimUnavailableError, match="moved 1 time") as raised:
        store.push_tree(
            worktree=worktree,
            remote=str(bare_remote),
            observed=observed,
            pending=pending,
            transport=transport,
        )
    assert "rejected 7 pushes" in str(raised.value)
    assert "without the ref moving after it last moved" in str(raised.value)


def test_git_push_transport_raises_on_a_non_fast_forward_push(
    bare_remote: Path, worktree: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    orphan_tree = store._write_bootstrap_tree(worktree)
    orphan_commit = store._commit_tree(
        worktree, tree_oid=orphan_tree, parent=None, message="unrelated root commit\n"
    )
    transport = store.GitPushTransport()

    with pytest.raises(protocol.PushRejectedError):
        transport.push(
            worktree=worktree, remote=str(bare_remote), ref=store.STATE_REF, new_oid=orphan_commit
        )


def test_run_git_with_input_fails_loud_on_a_nonzero_exit(worktree: Path) -> None:
    with pytest.raises(protocol.ClaimError):
        store._run_git_with_input(worktree, ["mktree"], input_data=b"not a valid tree line\n")


@pytest.mark.parametrize(
    "raised",
    [
        pytest.param(process.ExecutableMissingError("git"), id="executable-missing"),
        pytest.param(process.ProcessTimedOutError(), id="timed-out"),
    ],
)
def test_run_git_translates_process_failures_to_claim_error(
    monkeypatch: pytest.MonkeyPatch, worktree: Path, raised: Exception
) -> None:
    def fake_run_captured(*_args: object, **_kwargs: object) -> None:
        raise raised

    monkeypatch.setattr(store.process, "run_captured", fake_run_captured)

    with pytest.raises(protocol.ClaimError):
        store._run_git(worktree, ["status"])


def test_git_dir_fails_loud_when_the_worktree_is_not_a_repository(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()

    with pytest.raises(protocol.ClaimError):
        store._git_dir(not_a_repo)


def test_fetch_to_fetch_head_fails_loud_when_the_ref_is_missing(
    bare_remote: Path, worktree: Path
) -> None:
    with pytest.raises(protocol.ClaimError, match="cannot fetch"):
        store._fetch_to_fetch_head(worktree, str(bare_remote))


def test_anchor_fetched_tip_fails_loud_on_an_unresolvable_tip(worktree: Path) -> None:
    with pytest.raises(protocol.ClaimError, match="cannot anchor fetched tip"):
        store._anchor_fetched_tip(worktree, _PLACEHOLDER_TIP)


def test_tree_oid_fails_loud_on_an_unresolvable_commit(worktree: Path) -> None:
    with pytest.raises(protocol.MalformedStateTreeError):
        store._tree_oid(worktree, _UNRESOLVABLE_OBJECT_ID)


def test_object_id_rejects_a_value_that_is_not_a_git_object_id() -> None:
    with pytest.raises(protocol.MalformedStateTreeError, match="not a git object id"):
        protocol.ObjectId("not-an-oid")


def test_serialize_and_parse_schema_toml_round_trip() -> None:
    tip = protocol.ObjectId("a" * 40)

    parsed = protocol.parse_schema_toml(protocol.serialize_empty_schema_toml(), tip=tip)

    assert parsed == protocol.ClaimState(tip=tip)


def test_cli_bootstrap_creates_the_empty_state_ref_without_a_ledger(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bare_remote: Path,
    worktree: Path,
) -> None:
    """Without `--ledger`, bootstrap only creates `refs/aco/state` (issue #176)."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(
        issue_claim.checkout,
        "remote_url",
        lambda remote: "git@github.com:example/agent-claim.git",
    )
    _git("remote", "add", "origin", str(bare_remote), cwd=worktree)
    monkeypatch.chdir(worktree)

    status = issue_claim.main(["--repo", "example/agent-claim", "bootstrap"])

    assert status == 0
    assert capsys.readouterr().out.splitlines() == [_state_ref_oid(bare_remote)]


def test_cli_bootstrap_is_idempotent_on_a_second_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bare_remote: Path,
    worktree: Path,
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    monkeypatch.setattr(
        issue_claim.checkout,
        "remote_url",
        lambda remote: "git@github.com:example/agent-claim.git",
    )
    _git("remote", "add", "origin", str(bare_remote), cwd=worktree)
    monkeypatch.chdir(worktree)
    issue_claim.main(["--repo", "example/agent-claim", "bootstrap"])
    first_output = capsys.readouterr().out

    status = issue_claim.main(["--repo", "example/agent-claim", "bootstrap"])

    assert status == 0
    assert capsys.readouterr().out == first_output


def test_list_tree_fails_loud_when_the_tree_is_unresolvable(worktree: Path) -> None:
    with pytest.raises(protocol.MalformedStateTreeError, match="cannot list the state tree"):
        store._list_tree(worktree, _UNRESOLVABLE_OBJECT_ID, tip=_PLACEHOLDER_TIP, context="state")


def _top_level(entries: dict[str, tuple[str, str]]) -> dict[str, tuple[str, str]]:
    return {name: value for name, value in entries.items() if "/" not in name}


def test_read_schema_toml_fails_loud_when_schema_toml_is_not_a_blob(worktree: Path) -> None:
    inner_blob = (
        subprocess.run(
            ["git", "-C", str(worktree), "hash-object", "-w", "--stdin"],
            input=b"x\n",
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )
    inner_tree = _raw_tree(worktree, [("100644", "blob", inner_blob, "x")])
    outer_tree_oid = protocol.ObjectId(
        _raw_tree(worktree, [("040000", "tree", inner_tree, "schema.toml")])
    )
    entries = store._list_tree(worktree, outer_tree_oid, tip=_PLACEHOLDER_TIP, context="state")
    top_level_entries = _top_level(entries)

    with pytest.raises(protocol.MalformedStateTreeError, match="is not a blob"):
        store._read_schema_toml(top_level_entries, {}, tip=_PLACEHOLDER_TIP)


def test_parse_state_tree_fails_loud_when_a_blob_is_missing_from_the_object_database(
    worktree: Path,
) -> None:
    # `git mktree`/`ls-tree` never verify a referenced blob exists, so the
    # dangling reference this exercises is built the only way one can occur
    # against a real object database: reference a real blob, then remove its
    # loose object, simulating a corrupted or incomplete local store. A real
    # remote would re-supply the object on the next `fetch`, so this drives
    # `_parse_state_tree` directly against a commit this worktree never
    # pushed or fetched. `git archive` -- the bulk-read boundary (issue
    # #241) -- is what notices, not `ls-tree`, which happily lists a
    # dangling entry.
    blob_oid = (
        subprocess.run(
            ["git", "-C", str(worktree), "hash-object", "-w", "--stdin"],
            input=_SCHEMA_TOML_VERSION_ONE,
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )
    tree = _raw_tree(worktree, [("100644", "blob", blob_oid, "schema.toml")])
    commit = (
        subprocess.run(
            ["git", "-C", str(worktree), "commit-tree", tree, "-m", "test fixture"],
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )
    loose_object = worktree / ".git" / "objects" / blob_oid[:2] / blob_oid[2:]
    loose_object.unlink()
    commit_oid = protocol.ObjectId(commit)

    with pytest.raises(protocol.MalformedStateTreeError, match="cannot read the state tree"):
        store._parse_state_tree(worktree, commit_oid)


def test_write_lineage_stamp_cleans_up_its_temp_file_on_failure(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
) -> None:
    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(store.os, "replace", fail_replace)
    tip = protocol.ObjectId("a" * 40)

    with pytest.raises(OSError, match="simulated replace failure"):
        store._write_lineage_stamp(worktree, tip)

    stamp_directory = store._git_dir(worktree) / "aco"
    assert list(stamp_directory.glob(".last-oid-*")) == []


@pytest.mark.parametrize(
    "raised",
    [
        pytest.param(process.ExecutableMissingError("git"), id="executable-missing"),
        pytest.param(process.ProcessTimedOutError(), id="timed-out"),
    ],
)
def test_run_git_with_input_translates_process_failures_to_claim_error(
    monkeypatch: pytest.MonkeyPatch, worktree: Path, raised: Exception
) -> None:
    def fake_run_bounded(*_args: object, **_kwargs: object) -> None:
        raise raised

    monkeypatch.setattr(store.process, "run_bounded", fake_run_bounded)

    with pytest.raises(protocol.ClaimError):
        store._run_git_with_input(worktree, ["mktree"], input_data=b"")


def test_find_operation_id_fails_loud_when_the_range_is_unresolvable(worktree: Path) -> None:
    with pytest.raises(protocol.ClaimError, match="cannot search"):
        store._find_operation_id(
            worktree,
            since=_UNRESOLVABLE_OBJECT_ID,
            until=_PLACEHOLDER_TIP,
            operation_id="whatever",
        )


def test_push_retry_stops_instead_of_committing_again_when_the_search_fails(
    monkeypatch: pytest.MonkeyPatch, bare_remote: Path, worktree: Path
) -> None:
    """A failing `operation_id` search after a rejected push must stop the
    retry loud, never be read as "not found" -- that would commit a second
    time on top of a lost response whose commit already landed."""
    observed = store.fetch_state(worktree=worktree, remote=str(bare_remote))
    transport = _AcceptThenRaiseTransport()
    operation_id = "operation-under-test"
    pending = store.PendingCommit(
        tree_oid=store._write_bootstrap_tree(worktree),
        message=f"bootstrap empty claim state\n\noperation_id: {operation_id}\n",
        operation_id=operation_id,
    )

    def failing_search(*_args: object, **_kwargs: object) -> None:
        raise protocol.ClaimError("simulated search failure")

    monkeypatch.setattr(store, "_find_operation_id", failing_search)

    with pytest.raises(protocol.ClaimError, match="simulated search failure"):
        store.push_tree(
            worktree=worktree,
            remote=str(bare_remote),
            observed=observed,
            pending=pending,
            transport=transport,
        )

    assert transport.calls == 1
    log = subprocess.run(
        ["git", "--git-dir", str(bare_remote), "rev-list", "--count", store.STATE_REF],
        check=True,
        capture_output=True,
        text=True,
    )
    assert log.stdout.strip() == "1"


def test_commit_tree_fails_loud_on_an_unresolvable_tree(worktree: Path) -> None:
    with pytest.raises(protocol.ClaimError):
        store._commit_tree(
            worktree, tree_oid=_UNRESOLVABLE_OBJECT_ID, parent=None, message="test\n"
        )


# --- `commit_transition`: `apply` wired to the real git transport ----------


def _issue_claim_intent(
    issue: int,
    *,
    claim_id: str = "a1",
    operation_id: str = "op-1",
    agent: str = "Ada",
    role: str = "builder",
    resource_name: str | None = None,
    resource_value: int | None = None,
) -> protocol.ClaimIntent:
    return protocol.ClaimIntent(
        identity=protocol.IssueIdentity(issue),
        agent=agent,
        role=role,
        base=protocol.ObjectId("c" * 40),
        branch=f"claude/issue-{issue}-cut",
        scope=(f"src/issue-{issue}.py",),
        claim_id=protocol.ClaimId(claim_id),
        operation_id=operation_id,
        resource_name=resource_name,
        resource_value=resource_value,
    )


def _committed_claim(bare_remote: Path, worktree: Path, *, issue: int) -> protocol.ActiveClaim:
    """Land one real claim transition and hand back its resulting `ActiveClaim`
    -- its `opened_commit` is the real state-ref commit the transition wrote,
    not a placeholder, so a `claim_ages` walk of that history finds it."""
    state = store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject=f"claim issue {issue}",
        intent=_issue_claim_intent(issue, claim_id=f"c{issue}", operation_id=f"op-{issue}"),
    )
    return next(
        claim for claim in state.claims.values() if claim.identity == protocol.IssueIdentity(issue)
    )


def test_claim_ages_reads_the_committer_date_of_each_claim_from_one_log_walk(
    bare_remote: Path, worktree: Path, git_call_spy: Counter[str]
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    first = _committed_claim(bare_remote, worktree, issue=1)
    second = _committed_claim(bare_remote, worktree, issue=2)
    state = store.fetch_state(worktree=worktree, remote=str(bare_remote))
    assert state.tip is not None
    git_call_spy.clear()

    ages = store.claim_ages(worktree=worktree, tip=state.tip, claims=state.claims.values())

    assert set(ages) == {first.claim_id, second.claim_id}
    assert all(age.tzinfo is not None for age in ages.values())
    assert git_call_spy["log"] == 1


def test_claim_ages_survives_a_gc_prune_of_the_just_fetched_history(
    bare_remote: Path, worktree: Path
) -> None:
    """Issue #237 finding 25, reproduced: `_fetch_to_fetch_head` lands the
    fetched commits only in `FETCH_HEAD`, which is not a ref and roots
    nothing once the fetch subprocess exits -- a `git gc --prune=now` run
    against this same checkout right after `fetch_state` returns collected
    every commit `claim_ages`'s own `git log` walk needs next, in the
    audit's reproduced incident (nothing else in this worktree references
    the state ref's history). `fetch_state` now anchors the fetched tip in
    its own per-worktree ref namespace, so the walk survives the same
    prune.
    """
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    claim = _committed_claim(bare_remote, worktree, issue=1)
    state = store.fetch_state(worktree=worktree, remote=str(bare_remote))
    assert state.tip is not None
    _git("gc", "--prune=now", cwd=worktree)

    ages = store.claim_ages(worktree=worktree, tip=state.tip, claims=(claim,))

    assert ages[claim.claim_id].tzinfo is not None


def test_claim_ages_returns_empty_without_a_git_call_for_no_live_claims(
    bare_remote: Path, worktree: Path, git_call_spy: Counter[str]
) -> None:
    tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))
    git_call_spy.clear()

    ages = store.claim_ages(worktree=worktree, tip=tip, claims=())

    assert ages == {}
    assert git_call_spy["log"] == 0


def test_claim_ages_refuses_a_claim_whose_opened_commit_is_not_an_ancestor_of_the_tip(
    bare_remote: Path, worktree: Path
) -> None:
    tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))
    orphan_tree = store._write_bootstrap_tree(worktree)
    orphan_commit = store._commit_tree(
        worktree, tree_oid=orphan_tree, parent=None, message="unrelated root commit\n"
    )
    stray = protocol.ActiveClaim(
        identity=protocol.IssueIdentity(1),
        claim_id=protocol.ClaimId("c1"),
        agent="Ada",
        role="builder",
        base=_UNRESOLVABLE_OBJECT_ID,
        branch="claude/issue-1-cut",
        scope=("src/issue-1.py",),
        opened_commit=orphan_commit,
    )

    with pytest.raises(protocol.StateLineageError, match="is not an ancestor"):
        store.claim_ages(worktree=worktree, tip=tip, claims=(stray,))


def _fake_git_log_result(
    monkeypatch: pytest.MonkeyPatch, *, exit_status: int, stdout: bytes
) -> None:
    """Let every real git subprocess run except `log`, which returns a fixed
    result -- isolates `claim_ages`'s date-read/parse step from the rest of
    its plumbing."""
    real_run_captured = process.run_captured

    def fake_run_captured(arguments: list[str]) -> process.CapturedResult:
        if "log" in arguments:
            return process.CapturedResult(exit_status=exit_status, stdout=stdout, stderr=b"")
        return real_run_captured(arguments)

    monkeypatch.setattr(store.process, "run_captured", fake_run_captured)


def test_claim_ages_fails_loud_when_the_log_read_fails(
    monkeypatch: pytest.MonkeyPatch, bare_remote: Path, worktree: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    claim = _committed_claim(bare_remote, worktree, issue=1)
    state = store.fetch_state(worktree=worktree, remote=str(bare_remote))
    assert state.tip is not None
    _fake_git_log_result(monkeypatch, exit_status=1, stdout=b"")

    with pytest.raises(protocol.ClaimError, match="cannot read the commit history"):
        store.claim_ages(worktree=worktree, tip=state.tip, claims=(claim,))


def test_claim_ages_fails_loud_on_a_malformed_date(
    monkeypatch: pytest.MonkeyPatch, bare_remote: Path, worktree: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    claim = _committed_claim(bare_remote, worktree, issue=1)
    state = store.fetch_state(worktree=worktree, remote=str(bare_remote))
    assert state.tip is not None
    _fake_git_log_result(
        monkeypatch, exit_status=0, stdout=f"{claim.opened_commit}\tnot-a-date\n".encode()
    )

    with pytest.raises(protocol.ClaimError, match="malformed committer date"):
        store.claim_ages(worktree=worktree, tip=state.tip, claims=(claim,))


def test_commit_transition_and_fetch_state_round_trip_a_claim_with_a_resource(
    bare_remote: Path, worktree: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    intent = _issue_claim_intent(42, resource_name="display")

    result = store.commit_transition(
        worktree=worktree, remote=str(bare_remote), subject="claim issue 42", intent=intent
    )

    refetched = store.fetch_state(worktree=worktree, remote=str(bare_remote))
    assert refetched == result
    assert refetched.claims["issue-42"].agent == "Ada"
    assert refetched.claims["issue-42"].resource == protocol.ResourceHold("display", 1)
    assert refetched.consumed_ids == frozenset({protocol.ClaimId("a1")})
    assert refetched.resources["display"].occupied == (1,)


def test_commit_transition_rescope_and_release_round_trip(
    bare_remote: Path, worktree: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject="claim issue 42",
        intent=_issue_claim_intent(42),
    )
    rescope = protocol.RescopeIntent(
        claim_id=protocol.ClaimId("a1"),
        agent="Ada",
        role="builder",
        scope=("README.md",),
        operation_id="op-2",
    )
    store.commit_transition(
        worktree=worktree, remote=str(bare_remote), subject="rescope issue 42", intent=rescope
    )

    rescoped = store.fetch_state(worktree=worktree, remote=str(bare_remote))
    assert rescoped.claims["issue-42"].scope == ("README.md",)

    release = protocol.ReleaseIntent(
        claim_id=protocol.ClaimId("a1"),
        agent="Ada",
        role="builder",
        outcome=protocol.AbandonedRelease("done"),
        operation_id="op-3",
    )
    store.commit_transition(
        worktree=worktree, remote=str(bare_remote), subject="release issue 42", intent=release
    )

    released = store.fetch_state(worktree=worktree, remote=str(bare_remote))
    assert "issue-42" not in released.claims
    assert protocol.ClaimId("a1") in released.consumed_ids


def test_commit_transition_preserves_items_across_claim_rescope_and_release(
    bare_remote: Path, worktree: Path
) -> None:
    """`items/` (issue #248) is board data that claim, rescope, and release
    intents reuse unchanged -- only `ItemWriteIntent` rewrites it.
    `_write_incremental_state_tree` must carry its oid forward unchanged on
    every claim, rescope, and release -- not silently rebuild the top-level
    tree from `schema.toml` plus the claim-ledger directories alone, which
    would drop the whole board (Grok final gate, blocking 1).
    """
    schema_blob = _blob(worktree, protocol.serialize_empty_schema_toml().encode())
    item_blob = _blob(worktree, b"item body\n")
    items_tree = _raw_tree(worktree, [("100644", "blob", item_blob, "aco-000001.md")])
    _push_raw_state_tree(
        bare_remote,
        worktree,
        [
            ("100644", "blob", schema_blob, "schema.toml"),
            ("040000", "tree", items_tree, store.ITEMS_DIRECTORY),
        ],
    )

    def assert_items_unchanged() -> None:
        tip = store.fetch_state(worktree=worktree, remote=str(bare_remote)).tip
        assert tip is not None
        entries = store._list_tree(worktree, tip, tip=tip, context="state")
        assert entries[store.ITEMS_DIRECTORY] == ("tree", items_tree)
        assert store.read_item_files(worktree, tip) == {"aco-000001.md": b"item body\n"}

    assert_items_unchanged()

    store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject="claim issue 42",
        intent=_issue_claim_intent(42),
    )
    assert_items_unchanged()

    store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject="rescope issue 42",
        intent=protocol.RescopeIntent(
            claim_id=protocol.ClaimId("a1"),
            agent="Ada",
            role="builder",
            scope=("README.md",),
            operation_id="op-2",
        ),
    )
    assert_items_unchanged()

    store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject="release issue 42",
        intent=protocol.ReleaseIntent(
            claim_id=protocol.ClaimId("a1"),
            agent="Ada",
            role="builder",
            outcome=protocol.AbandonedRelease("done"),
            operation_id="op-3",
        ),
    )
    assert_items_unchanged()


def test_commit_transition_a_local_two_racer_claim_on_different_keys_both_land(
    bare_remote: Path, worktree: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))

    store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject="claim issue 1",
        intent=_issue_claim_intent(1),
    )
    store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject="claim issue 2",
        intent=_issue_claim_intent(2, claim_id="a2", operation_id="op-2"),
    )

    state = store.fetch_state(worktree=worktree, remote=str(bare_remote))
    assert set(state.claims) == {"issue-1", "issue-2"}


def test_commit_transition_same_key_second_racer_names_the_holder(
    bare_remote: Path, worktree: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject="claim issue 42",
        intent=_issue_claim_intent(42),
    )

    intent = _issue_claim_intent(42, agent="Grace", claim_id="a2", operation_id="op-2")
    with pytest.raises(protocol.ClaimUnavailableError, match="is claimed by Ada"):
        store.commit_transition(
            worktree=worktree,
            remote=str(bare_remote),
            subject="claim issue 42",
            intent=intent,
        )


def test_commit_transition_a_different_key_loser_that_exhausts_retries_names_a_stuck_lock(
    bare_remote: Path, worktree: Path
) -> None:
    """Issue #237 finding 22: the ref never actually moves under
    `_AlwaysRejectingTransport`, so exhaustion names a stuck lock, never
    "held by X" (there is no other real claim here to be held by) and never
    a race (it never moved)."""
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    transport = _AlwaysRejectingTransport()

    intent = _issue_claim_intent(42)
    with pytest.raises(protocol.ClaimUnavailableError, match="rejected 32 pushes") as raised:
        store.commit_transition(
            worktree=worktree,
            remote=str(bare_remote),
            subject="claim issue 42",
            intent=intent,
            transport=transport,
        )
    assert "without the ref ever moving" in str(raised.value)
    assert "held by" not in str(raised.value)


def test_commit_transition_a_different_key_loser_that_exhausts_retries_names_a_race(
    bare_remote: Path, worktree: Path
) -> None:
    """Issue #237 finding 22's other half: a concurrent writer's commit
    genuinely lands first every attempt, so exhaustion names the race, not
    the stuck-lock repair sentence."""
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    transport = _AlwaysRacingTransport()

    intent = _issue_claim_intent(42)
    with pytest.raises(protocol.ClaimUnavailableError, match="moved 32 times") as raised:
        store.commit_transition(
            worktree=worktree,
            remote=str(bare_remote),
            subject="claim issue 42",
            intent=intent,
            transport=transport,
        )
    assert "stuck" not in str(raised.value)
    assert "lock" not in str(raised.value)


def test_commit_transition_exhaustion_names_the_true_mix_when_the_ref_moves_once_then_sticks(
    bare_remote: Path, worktree: Path
) -> None:
    """Issue #237 finding 22's mixed case for `commit_transition`: a
    concurrent writer's commit lands first on exactly the first attempt,
    then the ref sits fixed while the remaining 31 pushes are all rejected
    -- the true count is one move, not "moved 32 times"."""
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    transport = _MovesOnceThenSticksTransport()

    intent = _issue_claim_intent(42)
    with pytest.raises(protocol.ClaimUnavailableError, match="moved 1 time") as raised:
        store.commit_transition(
            worktree=worktree,
            remote=str(bare_remote),
            subject="claim issue 42",
            intent=intent,
            transport=transport,
        )
    assert "rejected 31 pushes" in str(raised.value)
    assert "without the ref moving after it last moved" in str(raised.value)


def test_commit_transition_lost_response_does_not_apply_twice(
    bare_remote: Path, worktree: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    transport = _AcceptThenRaiseTransport()
    intent = _issue_claim_intent(42)

    result = store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject="claim issue 42",
        intent=intent,
        transport=transport,
    )

    assert transport.calls == 1
    assert result.claims["issue-42"].claim_id == "a1"
    log = subprocess.run(
        ["git", "--git-dir", str(bare_remote), "rev-list", "--count", store.STATE_REF],
        check=True,
        capture_output=True,
        text=True,
    )
    # The bootstrap commit, plus this one claim commit -- never a duplicate
    # second commit for the same operation_id.
    assert log.stdout.strip() == "2"


def test_commit_transition_ten_thread_contention_lands_every_distinct_key(
    tmp_path: Path, bare_remote: Path
) -> None:
    """Criterion 3 contention (C2): ten threads, a barrier, no sleeps,
    against a local bare repo, bound at 30 seconds."""
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    _git("init", "-b", "main", cwd=main_repo)
    (main_repo / "README").write_text("placeholder\n")
    _git("add", "README", cwd=main_repo)
    _git("commit", "-m", "initial", cwd=main_repo)
    store.bootstrap(worktree=main_repo, remote=str(bare_remote))

    issue_numbers = range(1, 11)
    worktrees: dict[int, Path] = {}
    for issue in issue_numbers:
        linked = tmp_path / f"linked-{issue}"
        _git("worktree", "add", "-b", f"lane-{issue}", str(linked), cwd=main_repo)
        worktrees[issue] = linked

    barrier = threading.Barrier(10)
    errors: list[BaseException] = []

    def claim(issue: int, linked_worktree: Path) -> None:
        barrier.wait()
        try:
            store.commit_transition(
                worktree=linked_worktree,
                remote=str(bare_remote),
                subject=f"claim issue {issue}",
                intent=_issue_claim_intent(
                    issue, claim_id=f"a{issue}", operation_id=f"op-{issue:03d}"
                ),
            )
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=claim, args=(issue, linked)) for issue, linked in worktrees.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert errors == []
    state = store.fetch_state(worktree=main_repo, remote=str(bare_remote))
    assert set(state.claims) == {f"issue-{issue}" for issue in issue_numbers}
    # Ten writers land in one linear chain (issue #241, "proven sound -- do
    # not break"): the bootstrap commit plus ten claim commits, never a
    # merge -- every losing racer's retry re-reads its own fresh
    # `observed.tip` and rebuilds against it rather than merging histories.
    commit_count = subprocess.run(
        ["git", "--git-dir", str(bare_remote), "rev-list", "--count", store.STATE_REF],
        check=True,
        capture_output=True,
        text=True,
    )
    assert commit_count.stdout.strip() == str(len(issue_numbers) + 1)
    merge_count = subprocess.run(
        ["git", "--git-dir", str(bare_remote), "rev-list", "--count", "--merges", store.STATE_REF],
        check=True,
        capture_output=True,
        text=True,
    )
    assert merge_count.stdout.strip() == "0"


# --- Item write transitions: `ItemWriteIntent`'s oid CAS through the real --
# --- git transport (issue #279) ---------------------------------------------


def _hashed_item_intent(
    worktree: Path,
    *,
    item_id: str = "aco-000001",
    expected: protocol.ObjectId | None = None,
    content: bytes = b"item body\n",
    operation_id: str = "op-1",
) -> protocol.ItemWriteIntent:
    """An `ItemWriteIntent` whose `new_oid` is a real blob hashed once, up
    front -- the same shape a production caller must use: `commit_transition`
    never hashes content itself, so every retry attempt re-applies the exact
    same `new_oid` (issue #279's "hashed once before the retry loop")."""
    return protocol.ItemWriteIntent(
        item_id=item_id,
        expected=expected,
        new_oid=protocol.ObjectId(_blob(worktree, content)),
        operation_id=operation_id,
    )


def test_commit_transition_item_create_adds_a_file_and_preserves_the_claim_ledger(
    bare_remote: Path, worktree: Path
) -> None:
    """A create lands `items/<id>.md` while `claims/`, `ids/`, and
    `resources/` stay byte-identical -- the same subtree-reuse seam
    `_write_incremental_state_tree` already gives claim/rescope/release
    (issue #241), now proven from the item-write side."""
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject="claim issue 42",
        intent=_issue_claim_intent(42, resource_name="display"),
    )
    before_tip = store.fetch_state(worktree=worktree, remote=str(bare_remote)).tip
    assert before_tip is not None
    before_entries = store._list_tree(worktree, before_tip, tip=before_tip, context="state")

    intent = _hashed_item_intent(worktree, content=b"item body\n")
    result = store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject="create item aco-000001",
        intent=intent,
    )

    assert result.items == {"aco-000001": intent.new_oid}
    assert result.tip is not None
    assert store.read_item_files(worktree, result.tip)["aco-000001.md"] == b"item body\n"
    after_entries = store._list_tree(worktree, result.tip, tip=result.tip, context="state")
    assert after_entries[store.CLAIMS_DIRECTORY] == before_entries[store.CLAIMS_DIRECTORY]
    assert after_entries[store.IDS_DIRECTORY] == before_entries[store.IDS_DIRECTORY]
    assert after_entries[store.RESOURCES_DIRECTORY] == before_entries[store.RESOURCES_DIRECTORY]


def test_commit_transition_item_create_refuses_a_duplicate_id(
    bare_remote: Path, worktree: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject="create item aco-000001",
        intent=_hashed_item_intent(worktree, content=b"first\n", operation_id="op-1"),
    )

    duplicate_intent = _hashed_item_intent(worktree, content=b"second\n", operation_id="op-2")

    with pytest.raises(protocol.ClaimUnavailableError, match="already exists"):
        store.commit_transition(
            worktree=worktree,
            remote=str(bare_remote),
            subject="create item aco-000001 again",
            intent=duplicate_intent,
        )


def test_commit_transition_item_edit_refuses_a_stale_expected_oid_without_clobbering(
    bare_remote: Path, worktree: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    created = store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject="create item aco-000001",
        intent=_hashed_item_intent(worktree, content=b"first\n", operation_id="op-1"),
    )
    stale_expected = protocol.ObjectId(_blob(worktree, b"never written\n"))

    stale_intent = _hashed_item_intent(
        worktree, expected=stale_expected, content=b"second\n", operation_id="op-2"
    )

    with pytest.raises(protocol.ClaimUnavailableError, match="written since it was read"):
        store.commit_transition(
            worktree=worktree,
            remote=str(bare_remote),
            subject="edit item aco-000001",
            intent=stale_intent,
        )

    unchanged = store.fetch_state(worktree=worktree, remote=str(bare_remote))
    assert unchanged.items == created.items
    assert unchanged.tip is not None
    assert store.read_item_files(worktree, unchanged.tip)["aco-000001.md"] == b"first\n"


def test_commit_transition_two_writers_different_item_ids_both_land(
    bare_remote: Path, worktree: Path
) -> None:
    """Same shape as the two-racer claim test on different keys (issue
    #176): two item creates on distinct ids both land, because a retry
    rebuilds `items/` from the full id -> oid map, never a copy of the
    parent tree's `items/` oid (issue #279)."""
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject="create item aco-000001",
        intent=_hashed_item_intent(worktree, item_id="aco-000001", operation_id="op-1"),
    )
    store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject="create item aco-000002",
        intent=_hashed_item_intent(worktree, item_id="aco-000002", operation_id="op-2"),
    )

    state = store.fetch_state(worktree=worktree, remote=str(bare_remote))
    assert set(state.items) == {"aco-000001", "aco-000002"}


def test_commit_transition_item_write_lost_response_does_not_apply_twice(
    bare_remote: Path, worktree: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    transport = _AcceptThenRaiseTransport()
    intent = _hashed_item_intent(worktree, content=b"item body\n")

    result = store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject="create item aco-000001",
        intent=intent,
        transport=transport,
    )

    assert transport.calls == 1
    assert result.items == {"aco-000001": intent.new_oid}
    log = subprocess.run(
        ["git", "--git-dir", str(bare_remote), "rev-list", "--count", store.STATE_REF],
        check=True,
        capture_output=True,
        text=True,
    )
    # The bootstrap commit, plus this one item-write commit -- never a
    # duplicate second commit for the same operation_id.
    assert log.stdout.strip() == "2"


def _linked_worktrees(main_repo: Path, tmp_path: Path, names: tuple[str, ...]) -> dict[str, Path]:
    """One `git worktree add`-linked checkout per name, sharing `main_repo`'s
    object database -- the same real-transport shape the ten-thread claim
    contention test above uses, so a thread's git commands never race another
    thread's inside one shared index."""
    worktrees: dict[str, Path] = {}
    for name in names:
        linked = tmp_path / f"linked-{name}"
        _git("worktree", "add", "-b", f"lane-{name}", str(linked), cwd=main_repo)
        worktrees[name] = linked
    return worktrees


def test_commit_transition_two_threads_creating_different_item_ids_both_land(
    tmp_path: Path, bare_remote: Path
) -> None:
    """Issue #279 Gate F2: the same two-racer shape as the ten-thread claim
    contention test above, now proven for item writes -- two real threads, a
    barrier, no sleeps. Distinct ids never contend, so both creates land
    (the sequential version of this claim is
    `test_commit_transition_two_writers_different_item_ids_both_land`)."""
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    _git("init", "-b", "main", cwd=main_repo)
    (main_repo / "README").write_text("placeholder\n")
    _git("add", "README", cwd=main_repo)
    _git("commit", "-m", "initial", cwd=main_repo)
    store.bootstrap(worktree=main_repo, remote=str(bare_remote))

    item_ids = ("aco-000001", "aco-000002")
    worktrees = _linked_worktrees(main_repo, tmp_path, item_ids)
    barrier = threading.Barrier(len(item_ids))
    errors: list[BaseException] = []

    def create(item_id: str, linked_worktree: Path) -> None:
        barrier.wait()
        try:
            store.commit_transition(
                worktree=linked_worktree,
                remote=str(bare_remote),
                subject=f"create item {item_id}",
                intent=_hashed_item_intent(
                    linked_worktree, item_id=item_id, operation_id=f"op-{item_id}"
                ),
            )
        except BaseException as error:
            errors.append(error)

    threads = [
        threading.Thread(target=create, args=(item_id, linked))
        for item_id, linked in worktrees.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert errors == []
    state = store.fetch_state(worktree=main_repo, remote=str(bare_remote))
    assert set(state.items) == set(item_ids)


def test_commit_transition_two_threads_racing_the_same_item_id_lands_exactly_one(
    tmp_path: Path, bare_remote: Path
) -> None:
    """Issue #279 Gate F2: two real threads race `ItemWriteIntent`'s
    create-only CAS (`expected=None`) against the identical id -- exactly one
    lands, and the loser refuses by the same 'already exists' sentence
    `test_commit_transition_item_create_refuses_a_duplicate_id` proves
    sequentially, never a silent overwrite of the winner's content."""
    main_repo = tmp_path / "main"
    main_repo.mkdir()
    _git("init", "-b", "main", cwd=main_repo)
    (main_repo / "README").write_text("placeholder\n")
    _git("add", "README", cwd=main_repo)
    _git("commit", "-m", "initial", cwd=main_repo)
    store.bootstrap(worktree=main_repo, remote=str(bare_remote))

    racer_contents = {"racer-a": b"from a\n", "racer-b": b"from b\n"}
    worktrees = _linked_worktrees(main_repo, tmp_path, tuple(racer_contents))
    barrier = threading.Barrier(len(racer_contents))
    exceptions: dict[str, BaseException] = {}

    def create(racer: str, linked_worktree: Path, content: bytes) -> None:
        barrier.wait()
        try:
            store.commit_transition(
                worktree=linked_worktree,
                remote=str(bare_remote),
                subject="create item aco-000001",
                intent=_hashed_item_intent(
                    linked_worktree, content=content, operation_id=f"op-{racer}"
                ),
            )
        except BaseException as error:
            exceptions[racer] = error

    threads = [
        threading.Thread(target=create, args=(racer, worktrees[racer], content))
        for racer, content in racer_contents.items()
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
        assert not thread.is_alive()

    assert len(exceptions) == 1
    (loser_error,) = exceptions.values()
    assert isinstance(loser_error, protocol.ClaimUnavailableError)
    assert "already exists" in str(loser_error)
    state = store.fetch_state(worktree=main_repo, remote=str(bare_remote))
    assert set(state.items) == {"aco-000001"}


# --- Incremental write/bulk read: git-invocation count is independent of ---
# --- how many claims the state tree holds (issue #241) ---------------------


def _seeded_claims_state(count: int, *, opened_commit: protocol.ObjectId) -> protocol.ClaimState:
    """`count` distinct live claims and their consumed ids, built by
    `protocol.apply` alone -- pure and git-free, so seeding a state this
    large for the tests below costs no subprocess beyond the raw tree
    `_push_seeded_state` builds from it. Every seeded claim's `opened_commit`
    is `opened_commit` (`apply` always stamps it from the state's own tip),
    so a caller that passes the real bootstrap commit gets claims a
    `claim_ages` walk can resolve.
    """
    state = protocol.ClaimState(tip=opened_commit)
    for n in range(count):
        state = protocol.apply(
            state,
            protocol.ClaimIntent(
                identity=protocol.IssueIdentity(n + 1),
                agent="Ada",
                role="builder",
                base=_UNRESOLVABLE_OBJECT_ID,
                branch=f"claude/issue-{n + 1}-cut",
                scope=(f"src/module_{n}.py",),
                claim_id=protocol.ClaimId(f"c{n}"),
                operation_id=f"seed-op-{n}",
            ),
        )
    return state


def _push_seeded_state(bare_remote: Path, worktree: Path, count: int) -> protocol.ClaimState:
    """Push `count` claims onto `STATE_REF` via raw git plumbing plus the
    real TOML codec, once -- never `count` round trips through
    `commit_transition`, and never `store`'s own incremental writer, which
    has nothing yet to diff against on a tree's first write; `bootstrap`
    itself only ever writes `schema.toml` alone (issue #241)."""
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    parent = store.fetch_state(worktree=worktree, remote=str(bare_remote)).tip
    assert parent is not None
    seeded = _seeded_claims_state(count, opened_commit=parent)
    claims_tree = _raw_tree(
        worktree,
        [
            (
                "100644",
                "blob",
                _blob(worktree, protocol.serialize_claim_toml(claim).encode()),
                f"{key}.toml",
            )
            for key, claim in seeded.claims.items()
        ],
    )
    empty_blob = _blob(worktree, b"")
    ids_tree = _raw_tree(
        worktree, [("100644", "blob", empty_blob, claim_id) for claim_id in seeded.consumed_ids]
    )
    schema_blob = _blob(worktree, protocol.serialize_empty_schema_toml().encode())
    top_tree = protocol.ObjectId(
        _raw_tree(
            worktree,
            [
                ("100644", "blob", schema_blob, store.SCHEMA_TOML_FILENAME),
                ("040000", "tree", claims_tree, store.CLAIMS_DIRECTORY),
                ("040000", "tree", ids_tree, store.IDS_DIRECTORY),
            ],
        )
    )
    commit = store._commit_tree(
        worktree, tree_oid=top_tree, parent=parent, message="seed\n\noperation_id: seed\n"
    )
    store.GitPushTransport().push(
        worktree=worktree, remote=str(bare_remote), ref=store.STATE_REF, new_oid=commit
    )
    return store.fetch_state(worktree=worktree, remote=str(bare_remote))


@pytest.mark.parametrize("claim_count", [10, 300])
def test_fetch_state_git_call_count_is_independent_of_claim_count(
    bare_remote: Path,
    worktree: Path,
    tmp_path: Path,
    git_call_spy: Counter[str],
    claim_count: int,
) -> None:
    """One `ls-tree` and one `archive` read every claim regardless of how
    many there are (issue #241, audit findings 20-21) -- replacing what used
    to be one `ls-tree` and one `cat-file -p` per claim.
    """
    _push_seeded_state(bare_remote, worktree, claim_count)
    reader = tmp_path / "reader"
    reader.mkdir()
    _git("init", "-b", "main", cwd=reader)
    git_call_spy.clear()

    state = store.fetch_state(worktree=reader, remote=str(bare_remote))

    assert len(state.claims) == claim_count
    assert dict(git_call_spy) == {
        "ls-remote": 1,
        "fetch": 1,
        "update-ref": 1,
        "rev-parse": 4,
        "ls-tree": 1,
        "archive": 1,
    }


def test_fetch_state_populates_items_from_the_one_existing_ls_tree_call(
    bare_remote: Path, worktree: Path, tmp_path: Path, git_call_spy: Counter[str]
) -> None:
    """`ClaimState.items` (issue #279) comes free from the same recursive
    `ls-tree` `_parse_state_tree` already pays for its structure -- no
    second `ls-tree` and no `archive` call for `items/`'s own oids."""
    schema_blob = _blob(worktree, protocol.serialize_empty_schema_toml().encode())
    item_blob = _blob(worktree, b"item body\n")
    items_tree = _raw_tree(worktree, [("100644", "blob", item_blob, "aco-000001.md")])
    _push_raw_state_tree(
        bare_remote,
        worktree,
        [
            ("100644", "blob", schema_blob, "schema.toml"),
            ("040000", "tree", items_tree, store.ITEMS_DIRECTORY),
        ],
    )
    reader = tmp_path / "reader"
    reader.mkdir()
    _git("init", "-b", "main", cwd=reader)
    git_call_spy.clear()

    state = store.fetch_state(worktree=reader, remote=str(bare_remote))

    assert state.items == {"aco-000001": protocol.ObjectId(item_blob)}
    assert dict(git_call_spy) == {
        "ls-remote": 1,
        "fetch": 1,
        "update-ref": 1,
        "rev-parse": 4,
        "ls-tree": 1,
        "archive": 1,
    }


@pytest.mark.parametrize("claim_count", [10, 300])
def test_status_git_call_count_is_independent_of_claim_count(
    bare_remote: Path,
    worktree: Path,
    tmp_path: Path,
    git_call_spy: Counter[str],
    claim_count: int,
) -> None:
    """`status`'s two store reads -- `fetch_state` then `claim_ages` -- make a
    fixed number of git calls regardless of how many live claims the state
    tree holds (issue #242): one `ls-tree`/`archive` pair for the claims
    (issue #241) plus one batched `log` walk for their ages, never one
    `merge-base`+`log` per claim.
    """
    _push_seeded_state(bare_remote, worktree, claim_count)
    reader = tmp_path / "reader"
    reader.mkdir()
    _git("init", "-b", "main", cwd=reader)
    git_call_spy.clear()

    state = store.fetch_state(worktree=reader, remote=str(bare_remote))
    assert state.tip is not None
    ages = store.claim_ages(worktree=reader, tip=state.tip, claims=state.claims.values())

    assert len(ages) == claim_count
    assert dict(git_call_spy) == {
        "ls-remote": 1,
        "fetch": 1,
        "update-ref": 1,
        "rev-parse": 4,
        "ls-tree": 1,
        "archive": 1,
        "log": 1,
    }


def _add_one_more_claim(claim_count: int) -> protocol.ClaimIntent:
    """A `claim` intent that both adds a new `claims/` entry and consumes a
    new id, touching two of the three subtrees at once."""
    return protocol.ClaimIntent(
        identity=protocol.IssueIdentity(claim_count + 1000),
        agent="Ada",
        role="builder",
        base=_UNRESOLVABLE_OBJECT_ID,
        branch="claude/issue-new-cut",
        scope=("src/new_module.py",),
        claim_id=protocol.ClaimId("new-claim"),
        operation_id="new-op",
    )


def _release_first_claim(_claim_count: int) -> protocol.ReleaseIntent:
    """A `release` intent that only ever shrinks `claims/`: `ids/` and
    `resources/` are untouched (a released id is never reused)."""
    return protocol.ReleaseIntent(
        claim_id=protocol.ClaimId("c0"),
        agent="Ada",
        role="builder",
        outcome=protocol.AbandonedRelease("test"),
        operation_id="release-op",
    )


@pytest.mark.parametrize("claim_count", [10, 300])
@pytest.mark.parametrize(
    ("build_intent", "expected_hash_object", "expected_mktree"),
    [
        pytest.param(_add_one_more_claim, 2, 3, id="claim"),
        pytest.param(_release_first_claim, 0, 2, id="release"),
    ],
)
def test_commit_transition_git_call_count_is_independent_of_claim_count(
    bare_remote: Path,
    worktree: Path,
    git_call_spy: Counter[str],
    claim_count: int,
    build_intent: Callable[[int], protocol.ClaimTransitionIntent],
    expected_hash_object: int,
    expected_mktree: int,
) -> None:
    """A transition's git-invocation count is fixed by which subtrees the
    intent touches, never by how many claims the tree already holds (issue
    #241): the write seam's own `ls-tree` (inside the retry loop, against
    that attempt's `observed.tip`) plus `hash-object` only for entries that
    actually changed, `mktree` only for subtrees that actually changed plus
    the top, and one `commit-tree`.
    """
    _push_seeded_state(bare_remote, worktree, claim_count)
    git_call_spy.clear()

    store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject="transition under measurement",
        intent=build_intent(claim_count),
    )

    assert git_call_spy["ls-tree"] == 2
    assert git_call_spy["hash-object"] == expected_hash_object
    assert git_call_spy["mktree"] == expected_mktree
    assert git_call_spy["commit-tree"] == 1
    assert git_call_spy["merge-base"] == 0


def test_commit_transition_reuses_an_unchanged_claims_blob_byte_for_byte(
    bare_remote: Path, worktree: Path
) -> None:
    """An entry a transition does not touch keeps its exact bytes and oid
    (issue #241): today's normalisation-on-every-write would still produce
    the same bytes for an unchanged record (the codec is deterministic), so
    this pins the oid, the sharper claim reuse actually makes.
    """
    seeded = _push_seeded_state(bare_remote, worktree, 3)
    tip = seeded.tip
    assert tip is not None
    before = store._list_tree(worktree, tip, tip=tip, context="state")

    result = store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject="release c0",
        intent=_release_first_claim(3),
    )

    assert result.tip is not None
    after = store._list_tree(worktree, result.tip, tip=result.tip, context="state")
    # `c0` is the claim id released, held by tree key `issue-1` (identity
    # `n + 1`, per `_seeded_claims_state`); `issue-2`/`issue-3` are untouched.
    assert after["claims/issue-2.toml"] == before["claims/issue-2.toml"]
    assert after["claims/issue-3.toml"] == before["claims/issue-3.toml"]
    assert after[store.SCHEMA_TOML_FILENAME] == before[store.SCHEMA_TOML_FILENAME]
    assert "claims/issue-1.toml" not in after


def test_commit_transition_reuses_a_whole_unchanged_subtree_by_its_own_oid(
    bare_remote: Path, worktree: Path
) -> None:
    """A subtree no member of which changed is reused by its own oid --
    never rebuilt with `mktree` (issue #241): claiming issue 2 without a
    resource leaves `resources/`, populated by issue 1's claim, byte for
    byte the same tree object.
    """
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject="claim issue 1",
        intent=_issue_claim_intent(1, resource_name="display"),
    )
    before_tip = store.fetch_state(worktree=worktree, remote=str(bare_remote)).tip
    assert before_tip is not None
    before = store._list_tree(worktree, before_tip, tip=before_tip, context="state")

    result = store.commit_transition(
        worktree=worktree,
        remote=str(bare_remote),
        subject="claim issue 2",
        intent=_issue_claim_intent(2, claim_id="a2", operation_id="op-2"),
    )

    assert result.tip is not None
    after = store._list_tree(worktree, result.tip, tip=result.tip, context="state")
    assert after[store.RESOURCES_DIRECTORY] == before[store.RESOURCES_DIRECTORY]
    assert after["resources/display.toml"] == before["resources/display.toml"]


def test_parse_state_tree_fails_loud_on_malformed_archive_framing(
    monkeypatch: pytest.MonkeyPatch, bare_remote: Path, worktree: Path
) -> None:
    """`git archive`'s exit status cannot signal a framing problem in bytes
    it already returned successfully (issue #241) -- `tarfile` owns that
    failure, which a real git repository cannot itself produce, so this
    fabricates one at the `process` chokepoint.
    """
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    real_run_captured = process.run_captured

    def fake_run_captured(
        command: list[str],
        *,
        env: dict[str, str] | None = None,
        timeout: float = process.DEFAULT_TIMEOUT_SECONDS,
    ) -> process.CapturedResult:
        if command[0] == "git" and command[3] == "archive":
            return process.CapturedResult(exit_status=0, stdout=b"not a tar stream", stderr=b"")
        return real_run_captured(command, env=env, timeout=timeout)

    monkeypatch.setattr(store.process, "run_captured", fake_run_captured)

    with pytest.raises(protocol.MalformedStateTreeError, match="malformed archive"):
        store.fetch_state(worktree=worktree, remote=str(bare_remote))


# --- `apply`, the claim-key codec, and the claim/resource TOML codecs ------
#
# Pure logic (issue #176, slice C2): no git subprocess needed, so these
# exercise `protocol.apply` and its codecs directly rather than through a
# bare repository -- the thin git-transport integration layer above stays
# reserved for what actually needs a real repository.

_TIP = protocol.ObjectId("a" * 40)
_OTHER_TIP = protocol.ObjectId("b" * 40)
_BASE = protocol.ObjectId("c" * 40)
_STATE_WITH_TIP = protocol.ClaimState(tip=_TIP)
_DEFAULT_IDENTITY = protocol.IssueIdentity(42)


def _claim_intent(
    *,
    identity: protocol.ClaimIdentity = _DEFAULT_IDENTITY,
    agent: str = "Ada",
    role: str = "builder",
    base: protocol.ObjectId = _BASE,
    branch: str = "claude/issue-42-cut",
    scope: tuple[str, ...] = ("src/agent_coordination/store.py",),
    claim_id: str = "a1",
    operation_id: str = "op-1",
    whole_reason: str | None = None,
    resource_name: str | None = None,
    resource_value: int | None = None,
) -> protocol.ClaimIntent:
    return protocol.ClaimIntent(
        identity=identity,
        agent=agent,
        role=role,
        base=base,
        branch=branch,
        scope=scope,
        claim_id=protocol.ClaimId(claim_id),
        operation_id=operation_id,
        whole_reason=whole_reason,
        resource_name=resource_name,
        resource_value=resource_value,
    )


def test_apply_claim_intent_adds_a_live_claim_and_consumes_its_id() -> None:
    state = protocol.apply(_STATE_WITH_TIP, _claim_intent())

    claim = state.claims["issue-42"]
    assert claim.identity == protocol.IssueIdentity(42)
    assert claim.agent == "Ada"
    assert claim.role == "builder"
    assert claim.base == _BASE
    assert claim.branch == "claude/issue-42-cut"
    assert claim.scope == ("src/agent_coordination/store.py",)
    assert claim.opened_commit == _TIP
    assert claim.resource is None
    assert claim.whole_reason is None
    assert state.consumed_ids == frozenset({protocol.ClaimId("a1")})


def test_apply_claim_intent_refuses_against_a_missing_state_ref() -> None:
    intent = _claim_intent()
    with pytest.raises(protocol.ClaimError, match="does not exist yet"):
        protocol.apply(protocol.EMPTY_STATE, intent)


def test_apply_claim_intent_replays_idempotently_for_the_same_claim_id_and_fields() -> None:
    once = protocol.apply(_STATE_WITH_TIP, _claim_intent())

    replayed = protocol.apply(once, _claim_intent(operation_id="a-different-operation-id"))

    assert replayed == once


def test_apply_claim_intent_replays_idempotently_for_a_lane_identity() -> None:
    """Same criterion 2 replay as above, but for a `LaneIdentity` claim: it
    carries no field of its own (`_same_identity`'s other branch, next to
    `IssueIdentity`'s), so two independently constructed `LaneIdentity()`
    instances must still compare equal for the replay to match."""
    lane_intent = _claim_intent(identity=protocol.LaneIdentity(), branch="docs/tidy-readme")
    once = protocol.apply(_STATE_WITH_TIP, lane_intent)

    replayed = protocol.apply(
        once,
        _claim_intent(
            identity=protocol.LaneIdentity(),
            branch="docs/tidy-readme",
            operation_id="a-different-operation-id",
        ),
    )

    assert replayed == once


def test_apply_claim_intent_refuses_a_reused_claim_id_with_different_fields() -> None:
    once = protocol.apply(_STATE_WITH_TIP, _claim_intent())
    reused = _claim_intent(scope=("README.md",))

    with pytest.raises(protocol.ClaimUnavailableError, match="already on this ledger"):
        protocol.apply(once, reused)


def test_apply_claim_intent_refuses_a_reused_claim_id_after_release() -> None:
    claimed = protocol.apply(_STATE_WITH_TIP, _claim_intent())
    release = protocol.ReleaseIntent(
        claim_id=protocol.ClaimId("a1"),
        agent="Ada",
        role="builder",
        outcome=protocol.AbandonedRelease("done"),
        operation_id="op-2",
    )
    released = protocol.apply(claimed, release)
    reclaim = _claim_intent(operation_id="op-3")

    with pytest.raises(protocol.ClaimUnavailableError, match="already on this ledger"):
        protocol.apply(released, reclaim)


def test_apply_claim_intent_refuses_an_identity_conflict() -> None:
    claimed = protocol.apply(_STATE_WITH_TIP, _claim_intent())
    conflicting = _claim_intent(agent="Grace", claim_id="a2", operation_id="op-2")

    with pytest.raises(protocol.ClaimUnavailableError, match="is claimed by Ada"):
        protocol.apply(claimed, conflicting)


def test_apply_rescope_intent_replaces_scope_and_preserves_opened_commit() -> None:
    claimed = protocol.apply(_STATE_WITH_TIP, _claim_intent())
    rescope = protocol.RescopeIntent(
        claim_id=protocol.ClaimId("a1"),
        agent="Ada",
        role="builder",
        scope=("README.md",),
        operation_id="op-2",
    )

    rescoped = protocol.apply(claimed, rescope)

    claim = rescoped.claims["issue-42"]
    assert claim.scope == ("README.md",)
    assert claim.opened_commit == _TIP
    assert rescoped.consumed_ids == claimed.consumed_ids


def test_apply_rescope_intent_refuses_a_non_claimant() -> None:
    """The refusal names both claimants it compared -- the live claim's
    holder, and the intent's own agent and role -- not just the rule."""
    claimed = protocol.apply(_STATE_WITH_TIP, _claim_intent())
    rescope = protocol.RescopeIntent(
        claim_id=protocol.ClaimId("a1"),
        agent="Grace",
        role="builder",
        scope=("README.md",),
        operation_id="op-2",
    )

    with pytest.raises(protocol.ClaimUnavailableError) as error:
        protocol.apply(claimed, rescope)

    assert str(error.value) == (
        "only the original claimant may rescope "
        "(holder='Ada (builder)', this session='Grace (builder)')"
    )


def test_apply_rescope_intent_refuses_rescoping_a_claim_that_does_not_exist() -> None:
    rescope = protocol.RescopeIntent(
        claim_id=protocol.ClaimId("nonexistent"),
        agent="Ada",
        role="builder",
        scope=("README.md",),
        operation_id="op-1",
    )

    with pytest.raises(protocol.ClaimUnavailableError, match="no active claim"):
        protocol.apply(_STATE_WITH_TIP, rescope)


def test_apply_rescope_intent_keeps_the_whole_reason_when_omitted() -> None:
    claimed = protocol.apply(_STATE_WITH_TIP, _claim_intent(whole_reason="repo-wide rename"))
    rescope = protocol.RescopeIntent(
        claim_id=protocol.ClaimId("a1"),
        agent="Ada",
        role="builder",
        scope=("README.md",),
        operation_id="op-2",
    )

    rescoped = protocol.apply(claimed, rescope)

    assert rescoped.claims["issue-42"].whole_reason == "repo-wide rename"


@pytest.mark.parametrize(
    "outcome",
    [
        pytest.param(protocol.MergedRelease(108), id="merged"),
        pytest.param(protocol.AbandonedRelease("no longer needed"), id="abandoned"),
    ],
)
def test_apply_release_intent_removes_the_claim(outcome: protocol.ReleaseOutcome) -> None:
    claimed = protocol.apply(_STATE_WITH_TIP, _claim_intent())
    release = protocol.ReleaseIntent(
        claim_id=protocol.ClaimId("a1"),
        agent="Ada",
        role="builder",
        outcome=outcome,
        operation_id="op-2",
    )

    released = protocol.apply(claimed, release)

    assert "issue-42" not in released.claims
    assert protocol.ClaimId("a1") in released.consumed_ids


def test_apply_release_intent_refuses_releasing_a_claim_that_does_not_exist() -> None:
    release = protocol.ReleaseIntent(
        claim_id=protocol.ClaimId("nonexistent"),
        agent="Ada",
        role="builder",
        outcome=protocol.AbandonedRelease("done"),
        operation_id="op-1",
    )

    with pytest.raises(protocol.ClaimUnavailableError, match="no active claim"):
        protocol.apply(_STATE_WITH_TIP, release)


def test_apply_release_intent_refuses_a_non_claimant_without_override() -> None:
    """The refusal names both claimants it compared, like rescope's."""
    claimed = protocol.apply(_STATE_WITH_TIP, _claim_intent())
    release = protocol.ReleaseIntent(
        claim_id=protocol.ClaimId("a1"),
        agent="Grace",
        role="builder",
        outcome=protocol.AbandonedRelease("stealing it"),
        operation_id="op-2",
    )

    with pytest.raises(protocol.ClaimUnavailableError) as error:
        protocol.apply(claimed, release)

    assert str(error.value) == (
        "only the original claimant may release; use an explicit coordinator override "
        "(holder='Ada (builder)', this session='Grace (builder)')"
    )


def test_apply_release_intent_allows_a_coordinator_override() -> None:
    claimed = protocol.apply(_STATE_WITH_TIP, _claim_intent())
    release = protocol.ReleaseIntent(
        claim_id=protocol.ClaimId("a1"),
        agent="Coordinator",
        role="coordinator",
        outcome=protocol.AbandonedRelease("stale takeover"),
        operation_id="op-2",
        coordinator_override=True,
    )

    released = protocol.apply(claimed, release)

    assert "issue-42" not in released.claims


def test_apply_release_intent_refuses_a_coordinator_override_without_coordinator_role() -> None:
    claimed = protocol.apply(_STATE_WITH_TIP, _claim_intent())
    release = protocol.ReleaseIntent(
        claim_id=protocol.ClaimId("a1"),
        agent="Ada",
        role="builder",
        outcome=protocol.AbandonedRelease("stale takeover"),
        operation_id="op-2",
        coordinator_override=True,
    )

    with pytest.raises(protocol.ClaimUnavailableError, match="requires role coordinator"):
        protocol.apply(claimed, release)


def test_apply_claim_intent_assigns_the_least_free_auto_resource_value() -> None:
    first = protocol.apply(_STATE_WITH_TIP, _claim_intent(claim_id="a1", resource_name="display"))
    second = protocol.apply(
        first,
        _claim_intent(
            identity=protocol.IssueIdentity(43),
            claim_id="a2",
            operation_id="op-2",
            resource_name="display",
        ),
    )

    assert first.claims["issue-42"].resource == protocol.ResourceHold("display", 1)
    assert second.claims["issue-43"].resource == protocol.ResourceHold("display", 2)
    assert second.resources["display"].occupied == (1, 2)


def test_apply_claim_intent_refuses_an_explicit_resource_value_already_held() -> None:
    held = protocol.apply(
        _STATE_WITH_TIP,
        _claim_intent(claim_id="a1", resource_name="display", resource_value=2),
    )

    conflicting = _claim_intent(
        identity=protocol.IssueIdentity(43),
        claim_id="a2",
        operation_id="op-2",
        agent="Grace",
        resource_name="display",
        resource_value=2,
    )
    with pytest.raises(protocol.ClaimUnavailableError, match="display 2 is held by Ada"):
        protocol.apply(held, conflicting)


def test_apply_claim_intent_never_reuses_a_released_auto_resource_value() -> None:
    """Done-when 2: the import (and every later reader) must be able to
    derive `occupied` from this run's own history, not from active claims
    alone -- a released auto value must stay occupied forever."""
    claimed = protocol.apply(_STATE_WITH_TIP, _claim_intent(claim_id="a1", resource_name="display"))
    release = protocol.ReleaseIntent(
        claim_id=protocol.ClaimId("a1"),
        agent="Ada",
        role="builder",
        outcome=protocol.AbandonedRelease("done"),
        operation_id="op-2",
    )
    released = protocol.apply(claimed, release)

    reclaimed = protocol.apply(
        released, _claim_intent(claim_id="a2", operation_id="op-3", resource_name="display")
    )

    assert reclaimed.claims["issue-42"].resource == protocol.ResourceHold("display", 2)
    assert reclaimed.resources["display"].occupied == (1, 2)


def test_apply_claim_intent_never_reuses_a_released_explicit_resource_value() -> None:
    claimed = protocol.apply(
        _STATE_WITH_TIP,
        _claim_intent(claim_id="a1", resource_name="display", resource_value=1),
    )
    release = protocol.ReleaseIntent(
        claim_id=protocol.ClaimId("a1"),
        agent="Ada",
        role="builder",
        outcome=protocol.AbandonedRelease("done"),
        operation_id="op-2",
    )
    released = protocol.apply(claimed, release)

    reclaim = _claim_intent(
        claim_id="a2", operation_id="op-3", resource_name="display", resource_value=1
    )
    with pytest.raises(protocol.ClaimUnavailableError, match="already consumed"):
        protocol.apply(released, reclaim)


def test_stale_takeover_is_release_then_claim_and_does_not_reuse_the_occupied_integer() -> None:
    """Coordinator stale-takeover: override-release then claim, two `apply`
    calls -- never a `TakeoverIntent`. The freed integer stays retired."""
    claimed = protocol.apply(_STATE_WITH_TIP, _claim_intent(claim_id="a1", resource_name="display"))
    override_release = protocol.ReleaseIntent(
        claim_id=protocol.ClaimId("a1"),
        agent="Coordinator",
        role="coordinator",
        outcome=protocol.AbandonedRelease("stale, no activity for 3 days"),
        operation_id="op-2",
        coordinator_override=True,
    )
    freed = protocol.apply(claimed, override_release)

    retaken = protocol.apply(
        freed,
        _claim_intent(claim_id="a3", operation_id="op-3", agent="Grace", resource_name="display"),
    )

    assert retaken.claims["issue-42"].claim_id == "a3"
    assert retaken.claims["issue-42"].resource == protocol.ResourceHold("display", 2)


def test_apply_resource_value_requires_a_resource_name() -> None:
    intent = _claim_intent(resource_value=3)
    with pytest.raises(protocol.ClaimError, match="resource value requires a resource name"):
        protocol.apply(_STATE_WITH_TIP, intent)


def test_apply_resource_value_must_be_a_positive_integer() -> None:
    intent = _claim_intent(resource_name="display", resource_value=0)
    with pytest.raises(protocol.ClaimError, match="positive integer"):
        protocol.apply(_STATE_WITH_TIP, intent)


# --- `ItemWriteIntent`'s oid CAS (issue #279) -------------------------------

_ITEM_OID = protocol.ObjectId("d" * 40)


def _item_intent(
    *,
    item_id: str = "aco-000001",
    expected: protocol.ObjectId | None = None,
    new_oid: protocol.ObjectId = _ITEM_OID,
    operation_id: str = "op-1",
) -> protocol.ItemWriteIntent:
    return protocol.ItemWriteIntent(
        item_id=item_id, expected=expected, new_oid=new_oid, operation_id=operation_id
    )


def test_apply_item_write_intent_creates_a_file_and_leaves_the_claim_ledger_untouched() -> None:
    claimed = protocol.apply(_STATE_WITH_TIP, _claim_intent())

    created = protocol.apply(claimed, _item_intent())

    assert created.items == {"aco-000001": _ITEM_OID}
    assert created.claims == claimed.claims
    assert created.consumed_ids == claimed.consumed_ids
    assert created.resources == claimed.resources


def test_apply_item_write_intent_refuses_against_a_missing_state_ref() -> None:
    intent = _item_intent()

    with pytest.raises(protocol.ClaimError, match="does not exist yet"):
        protocol.apply(protocol.EMPTY_STATE, intent)


def test_apply_item_write_intent_edits_when_the_expected_oid_matches() -> None:
    created = protocol.apply(_STATE_WITH_TIP, _item_intent())
    new_oid = protocol.ObjectId("e" * 40)

    edited = protocol.apply(
        created, _item_intent(expected=_ITEM_OID, new_oid=new_oid, operation_id="op-2")
    )

    assert edited.items == {"aco-000001": new_oid}


@pytest.mark.parametrize(
    ("write_expected", "match"),
    [
        pytest.param(None, "already exists", id="duplicate-create"),
        pytest.param(protocol.ObjectId("e" * 40), "written since it was read", id="stale-edit"),
    ],
)
def test_apply_item_write_intent_refuses_a_conflicting_expected_oid(
    write_expected: protocol.ObjectId | None, match: str
) -> None:
    created = protocol.apply(_STATE_WITH_TIP, _item_intent())
    conflicting_intent = _item_intent(
        expected=write_expected, new_oid=protocol.ObjectId("f" * 40), operation_id="op-2"
    )

    with pytest.raises(protocol.ClaimUnavailableError, match=match):
        protocol.apply(created, conflicting_intent)


# --- Claim key codec (criterion 10) ----------------------------------------


def test_claim_key_round_trips_an_issue_identity() -> None:
    key = protocol.claim_key(protocol.IssueIdentity(42), "claude/issue-42-cut")

    assert key == "issue-42"
    assert protocol.parse_claim_key(key) == protocol.IssueIdentity(42)


def test_claim_key_round_trips_a_lane_branch_with_slash_and_percent() -> None:
    branch = "docs/rename-100%-done"

    key = protocol.claim_key(protocol.LaneIdentity(), branch)

    assert key == "lane-docs%2Frename-100%25-done"
    assert protocol.parse_claim_key(key) == protocol.LaneIdentity()


def test_claim_key_round_trips_a_255_character_lane_branch() -> None:
    branch = "docs/" + "a" * 248 + "/z"
    assert len(branch) == 255

    key = protocol.claim_key(protocol.LaneIdentity(), branch)

    assert "/" not in key
    assert protocol.parse_claim_key(key) == protocol.LaneIdentity()
    # The tree-entry name is one segment, safe for `hash-object`/`mktree`/`ls-tree`
    # (`--missing`: this placeholder blob need not itself exist).
    entries = subprocess.run(
        ["git", "mktree", "--missing"],
        input=f"100644 blob {'0' * 40}\t{key}\n".encode(),
        check=False,
        capture_output=True,
    )
    assert entries.returncode == 0


def test_claim_key_issue_and_lane_prefixes_never_collide() -> None:
    issue_key = protocol.claim_key(protocol.IssueIdentity(1), "irrelevant")
    lane_key = protocol.claim_key(protocol.LaneIdentity(), "issue-1")

    assert issue_key != lane_key
    assert protocol.parse_claim_key(issue_key) == protocol.IssueIdentity(1)
    assert protocol.parse_claim_key(lane_key) == protocol.LaneIdentity()


@pytest.mark.parametrize(
    ("key", "match"),
    [
        pytest.param("resource-display", "neither the issue nor lane prefix", id="unknown-prefix"),
        pytest.param("issue-0", "malformed issue number", id="issue-zero"),
        pytest.param("issue-01", "malformed issue number", id="issue-leading-zero"),
        pytest.param("issue-abc", "malformed issue number", id="issue-not-a-number"),
        pytest.param("lane-%2", "malformed percent-escape", id="lane-incomplete-escape"),
        pytest.param("lane-%zz", "malformed percent-escape", id="lane-invalid-escape"),
        # Issue #237 finding 23: `_percent_encode_branch` never emits a
        # literal byte outside `_LANE_KEY_UNRESERVED` -- these two hand-
        # corrupted blobs carry one anyway, a shape `claim_key` itself would
        # never produce, so `parse_claim_key` must refuse them rather than
        # silently decoding a key `serialize_claim_toml`'s writer never
        # wrote.
        pytest.param(
            "lane-feature/branch", "unescaped reserved character", id="lane-unescaped-slash"
        ),
        pytest.param("lane-café", "unescaped reserved character", id="lane-unescaped-unicode"),
    ],
)
def test_parse_claim_key_rejects_a_malformed_key(key: str, match: str) -> None:
    with pytest.raises(protocol.MalformedStateTreeError, match=match):
        protocol.parse_claim_key(key)


# --- `claims/<key>.toml` and `resources/<name>.toml` codecs ----------------


def _sample_claim(
    *,
    scope: tuple[str, ...] = ("src/agent_coordination/store.py",),
    resource: protocol.ResourceHold | None = None,
    whole_reason: str | None = None,
) -> protocol.ActiveClaim:
    return protocol.ActiveClaim(
        identity=protocol.IssueIdentity(42),
        claim_id=protocol.ClaimId("a1"),
        agent="Ada",
        role="builder",
        base=_BASE,
        branch="claude/issue-42-cut",
        scope=scope,
        opened_commit=_TIP,
        resource=resource,
        whole_reason=whole_reason,
    )


def test_serialize_and_parse_claim_toml_round_trips_the_minimal_claim() -> None:
    claim = _sample_claim()

    parsed = protocol.parse_claim_toml(
        protocol.serialize_claim_toml(claim), key="issue-42", tip=_OTHER_TIP
    )

    assert parsed == claim


def test_serialize_and_parse_claim_toml_round_trips_resource_and_whole_reason() -> None:
    claim = _sample_claim(
        resource=protocol.ResourceHold("display", 2), whole_reason="repo-wide rename"
    )

    parsed = protocol.parse_claim_toml(
        protocol.serialize_claim_toml(claim), key="issue-42", tip=_OTHER_TIP
    )

    assert parsed == claim


def test_serialize_and_parse_claim_toml_round_trips_a_quote_in_a_scope_path() -> None:
    claim = _sample_claim(scope=('weird "quoted" path.py',))

    parsed = protocol.parse_claim_toml(
        protocol.serialize_claim_toml(claim), key="issue-42", tip=_OTHER_TIP
    )

    assert parsed.scope == ('weird "quoted" path.py',)


def test_parse_claim_toml_rejects_an_unknown_key() -> None:
    content = protocol.serialize_claim_toml(_sample_claim()) + 'comment = "stray"\n'

    with pytest.raises(protocol.MalformedStateTreeError, match="unknown keys"):
        protocol.parse_claim_toml(content, key="issue-42", tip=_OTHER_TIP)


def test_parse_claim_toml_rejects_a_missing_required_key() -> None:
    with pytest.raises(protocol.MalformedStateTreeError, match="is missing"):
        protocol.parse_claim_toml('claim_id = "a1"\n', key="issue-42", tip=_OTHER_TIP)


def test_parse_claim_toml_rejects_resource_value_without_resource_name() -> None:
    content = protocol.serialize_claim_toml(_sample_claim()) + "resource_value = 3\n"

    with pytest.raises(protocol.MalformedStateTreeError, match="resource_value without"):
        protocol.parse_claim_toml(content, key="issue-42", tip=_OTHER_TIP)


def test_parse_claim_toml_rejects_a_malformed_commit_id() -> None:
    content = protocol.serialize_claim_toml(_sample_claim()).replace(str(_BASE), "not-a-sha")

    with pytest.raises(protocol.MalformedStateTreeError, match="malformed commit id"):
        protocol.parse_claim_toml(content, key="issue-42", tip=_OTHER_TIP)


def test_parse_claim_toml_rejects_malformed_toml() -> None:
    with pytest.raises(protocol.MalformedStateTreeError, match="malformed claim file"):
        protocol.parse_claim_toml("not = valid = toml\n", key="issue-42", tip=_OTHER_TIP)


def test_serialize_and_parse_resource_toml_round_trips() -> None:
    record = protocol.ResourceRecord(name="display", occupied=(1, 2, 5))

    parsed = protocol.parse_resource_toml(
        protocol.serialize_resource_toml(record), name="display", tip=_OTHER_TIP
    )

    assert parsed == record


@pytest.mark.parametrize(
    "content",
    [
        pytest.param('name = "display"\n', id="wrong-key"),
        pytest.param("occupied = [1, 0]\n", id="non-positive-value"),
        pytest.param('occupied = ["1"]\n', id="non-integer-value"),
        pytest.param("occupied = 1\n", id="not-a-list"),
    ],
)
def test_parse_resource_toml_rejects_a_malformed_record(content: str) -> None:
    with pytest.raises(protocol.MalformedStateTreeError):
        protocol.parse_resource_toml(content, name="display", tip=_OTHER_TIP)


def test_parse_resource_toml_rejects_malformed_toml() -> None:
    with pytest.raises(protocol.MalformedStateTreeError, match="malformed resource file"):
        protocol.parse_resource_toml("not = valid = toml\n", name="display", tip=_OTHER_TIP)


def test_claim_id_rejects_a_value_that_is_not_a_valid_claim_id() -> None:
    with pytest.raises(protocol.ClaimError, match="not a valid claim id"):
        protocol.ClaimId("not a claim id")


def test_parse_claim_key_rejects_a_lane_key_whose_escape_does_not_decode_as_utf8() -> None:
    # `%FF` is a valid two-hex-digit escape but not a valid standalone UTF-8
    # byte, so the codec's decode step (not its hex-digit syntax check) fails.
    with pytest.raises(protocol.MalformedStateTreeError, match="does not decode as utf-8"):
        protocol.parse_claim_key("lane-%FF")


def test_apply_rescope_intent_can_set_a_new_whole_reason() -> None:
    claimed = protocol.apply(_STATE_WITH_TIP, _claim_intent())
    rescope = protocol.RescopeIntent(
        claim_id=protocol.ClaimId("a1"),
        agent="Ada",
        role="builder",
        scope=("README.md",),
        operation_id="op-2",
        whole_reason="repo-wide rename",
    )

    rescoped = protocol.apply(claimed, rescope)

    assert rescoped.claims["issue-42"].whole_reason == "repo-wide rename"


def _minimal_claim_toml_fields(**overrides: str) -> dict[str, str]:
    fields = {
        "claim_id": '"a1"',
        "agent": '"Ada"',
        "role": '"builder"',
        "base": f'"{_BASE}"',
        "branch": '"claude/issue-42-cut"',
        "scope": '["README.md"]',
        "opened_commit": f'"{_TIP}"',
    }
    fields.update(overrides)
    return fields


def _claim_toml_content(**overrides: str) -> str:
    fields = _minimal_claim_toml_fields(**overrides)
    return "\n".join(f"{key} = {value}" for key, value in fields.items()) + "\n"


@pytest.mark.parametrize(
    ("overrides", "match"),
    [
        pytest.param({"agent": "123"}, "must be non-empty text", id="agent-not-text"),
        pytest.param({"agent": '""'}, "must be non-empty text", id="agent-empty"),
        pytest.param({"scope": "[]"}, "non-empty list of text", id="scope-empty"),
        pytest.param({"scope": "[1]"}, "non-empty list of text", id="scope-not-text"),
        pytest.param({"claim_id": '"not valid!"'}, "invalid claim id", id="claim-id-invalid"),
    ],
)
def test_parse_claim_toml_rejects_a_malformed_field(overrides: dict[str, str], match: str) -> None:
    content = _claim_toml_content(**overrides)
    with pytest.raises(protocol.MalformedStateTreeError, match=match):
        protocol.parse_claim_toml(content, key="issue-42", tip=_OTHER_TIP)


def test_parse_claim_toml_rejects_a_non_positive_resource_value() -> None:
    content = _claim_toml_content() + 'resource_name = "display"\nresource_value = 0\n'

    with pytest.raises(protocol.MalformedStateTreeError, match="positive integer"):
        protocol.parse_claim_toml(content, key="issue-42", tip=_OTHER_TIP)


def test_parse_claim_toml_rejects_a_non_text_whole_reason() -> None:
    content = _claim_toml_content() + "whole_reason = 3\n"

    with pytest.raises(protocol.MalformedStateTreeError, match="must be text"):
        protocol.parse_claim_toml(content, key="issue-42", tip=_OTHER_TIP)


def test_fetch_state_refuses_a_deleted_ref_this_worktree_has_observed(
    bare_remote: Path, worktree: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    _git("update-ref", "-d", store.STATE_REF, cwd=bare_remote)

    with pytest.raises(protocol.StateLineageError, match="now absent"):
        store.fetch_state(worktree=worktree, remote=str(bare_remote))


def test_bootstrap_refuses_a_deleted_ref_this_worktree_has_observed(
    bare_remote: Path, worktree: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    _git("update-ref", "-d", store.STATE_REF, cwd=bare_remote)

    with pytest.raises(protocol.StateLineageError, match="now absent"):
        store.bootstrap(worktree=worktree, remote=str(bare_remote))


def test_commit_transition_refuses_a_missing_state_ref(worktree: Path, tmp_path: Path) -> None:
    empty_remote = tmp_path / "empty.git"
    empty_remote.mkdir()
    _git("init", "--bare", "-b", "main", cwd=empty_remote)

    intent = _claim_intent()
    with pytest.raises(protocol.ClaimError, match="does not exist yet"):
        store.commit_transition(
            worktree=worktree,
            remote=str(empty_remote),
            subject="claim issue 42",
            intent=intent,
        )


# `reset` (issue #298): `export_state_bundle`, `delete_state_ref`, and
# `clear_lineage_stamps`, plus the two small reads (`list_worktrees`,
# `local_state_ref_exists`) `cli.py`'s reset command plans and reports from.


def test_local_state_ref_exists_for_reset_fails_loud_on_a_git_failure_other_than_a_missing_ref(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
) -> None:
    """`git show-ref --verify --quiet`'s own documented "no such ref"
    outcome is exit 1 with empty output (issue #298, 19.09.2026 gate
    finding 4) -- any other nonzero exit, a corrupt ref or a repository
    failure, must fail loud rather than be read as the ref's absence."""
    real_run_captured = process.run_captured

    def fake_run_captured(arguments: list[str]) -> process.CapturedResult:
        if "show-ref" in arguments:
            return process.CapturedResult(
                exit_status=128, stdout=b"", stderr=b"fatal: simulated repository failure"
            )
        return real_run_captured(arguments)

    monkeypatch.setattr(store.process, "run_captured", fake_run_captured)

    with pytest.raises(protocol.ClaimError, match="cannot check"):
        store.local_state_ref_exists(worktree)


@pytest.mark.parametrize(
    "shared_state_ref_before_export",
    ["absent", "pointed-at-a-different-tip"],
)
def test_export_state_bundle_writes_a_verifiable_bundle_and_never_touches_the_shared_ref(
    bare_remote: Path, worktree: Path, tmp_path: Path, shared_state_ref_before_export: str
) -> None:
    """Bundles the private `EXPORT_BUNDLE_REF`, never the shared
    `STATE_REF` (issue #298, 19.09.2026 REVISE findings 1+2). The previous
    implementation pointed `STATE_REF` at `tip` to give the bundle a name --
    `refs/aco/state` is visible from every linked worktree of a repository,
    so a concurrent reset elsewhere could repoint or delete it between that
    write and the lease-guarded remote delete that follows, racing the
    bundle onto whatever tip it found rather than the one the caller
    leased, and a failed export left it mutated with no restore of its
    previous value. Whatever `STATE_REF` holds locally beforehand --
    nothing, or a foreign tip set by something else entirely -- must
    survive byte-for-byte, and the bundle must still carry exactly the
    leased `tip` regardless."""
    tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))
    store.fetch_state(worktree=worktree, remote=str(bare_remote))
    foreign_tip = None
    if shared_state_ref_before_export == "pointed-at-a-different-tip":
        foreign_tip = _push_custom_tree(
            bare_remote,
            worktree,
            parent=tip,
            files={store.SCHEMA_TOML_FILENAME: protocol.serialize_empty_schema_toml().encode()},
        )
        _git("update-ref", store.STATE_REF, foreign_tip, cwd=worktree)
    destination = tmp_path / "export" / "state.bundle"
    destination.parent.mkdir()

    returned = store.export_state_bundle(worktree=worktree, tip=tip, destination=destination)

    assert returned == destination
    _git("bundle", "verify", str(destination), cwd=worktree)
    heads = _git("bundle", "list-heads", str(destination), cwd=worktree).stdout
    assert heads.strip() == f"{tip} {store.EXPORT_BUNDLE_REF}"
    if foreign_tip is None:
        assert not store.local_state_ref_exists(worktree)
    else:
        assert _git("rev-parse", store.STATE_REF, cwd=worktree).stdout.strip() == foreign_tip
    assert (
        store._run_git(
            worktree, ["show-ref", "--verify", "--quiet", store.EXPORT_BUNDLE_REF]
        ).exit_status
        != 0
    )
    assert not list(destination.parent.glob(f".{destination.name}.*"))


def test_export_state_bundle_refuses_to_overwrite_an_existing_destination(
    bare_remote: Path, worktree: Path, tmp_path: Path
) -> None:
    tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))
    destination = tmp_path / "state.bundle"
    destination.write_bytes(b"an earlier export")

    with pytest.raises(protocol.ClaimError, match="already exists"):
        store.export_state_bundle(worktree=worktree, tip=tip, destination=destination)

    assert destination.read_bytes() == b"an earlier export"
    # The temporary file the bundle was actually written to (19.09.2026
    # REVISE finding 4) must not survive a refused publish either.
    assert not list(destination.parent.glob(f".{destination.name}.*"))


def _break_export_via_an_unwritable_destination_directory(
    monkeypatch: pytest.MonkeyPatch, bare_remote: Path, worktree: Path, tmp_path: Path
) -> tuple[protocol.ObjectId, Path, Callable[[], None]]:
    tip = store.fetch_state(worktree=worktree, remote=str(bare_remote)).tip
    assert tip is not None
    readonly_dir = tmp_path / "readonly"
    readonly_dir.mkdir()
    readonly_dir.chmod(0o500)
    return tip, readonly_dir / "state.bundle", lambda: readonly_dir.chmod(0o700)


def _break_export_via_a_failed_bundle_create(
    monkeypatch: pytest.MonkeyPatch, bare_remote: Path, worktree: Path, tmp_path: Path
) -> tuple[protocol.ObjectId, Path, Callable[[], None]]:
    """The subprocess step between the claimed temporary file and its
    publish -- `git bundle create - EXPORT_BUNDLE_REF` -- can fail on its
    own even though the preceding `update-ref` already proved `tip`
    reachable; the claimed temporary file must be removed rather than left
    behind empty, and `destination` itself must never come to exist at all
    (19.09.2026 REVISE finding 4)."""
    tip = store.fetch_state(worktree=worktree, remote=str(bare_remote)).tip
    assert tip is not None
    real_run_captured = process.run_captured

    def fake_run_captured(arguments: list[str]) -> process.CapturedResult:
        if "bundle" in arguments and "create" in arguments:
            return process.CapturedResult(
                exit_status=1, stdout=b"", stderr=b"simulated bundle create failure"
            )
        return real_run_captured(arguments)

    monkeypatch.setattr(store.process, "run_captured", fake_run_captured)
    return tip, tmp_path / "state.bundle", lambda: None


def _break_export_via_a_tip_this_worktree_never_received(
    monkeypatch: pytest.MonkeyPatch, bare_remote: Path, worktree: Path, tmp_path: Path
) -> tuple[protocol.ObjectId, Path, Callable[[], None]]:
    """Pointing `EXPORT_BUNDLE_REF` at `tip` is `export_state_bundle`'s own
    validation that `tip`'s objects actually reached this worktree --
    `update-ref` itself refuses a tip git has never seen, so a caller
    cannot silently bundle the wrong state."""
    return _PLACEHOLDER_TIP, tmp_path / "state.bundle", lambda: None


@pytest.mark.parametrize(
    "break_export",
    [
        _break_export_via_an_unwritable_destination_directory,
        _break_export_via_a_failed_bundle_create,
        _break_export_via_a_tip_this_worktree_never_received,
    ],
    ids=["unwritable-directory", "bundle-create-fails", "unreachable-tip"],
)
def test_export_state_bundle_fails_loud_and_leaves_no_trace(
    monkeypatch: pytest.MonkeyPatch,
    bare_remote: Path,
    worktree: Path,
    tmp_path: Path,
    break_export: Callable[
        [pytest.MonkeyPatch, Path, Path, Path], tuple[protocol.ObjectId, Path, Callable[[], None]]
    ],
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    tip, destination, restore = break_export(monkeypatch, bare_remote, worktree, tmp_path)

    try:
        with pytest.raises(protocol.ClaimError, match="cannot export"):
            store.export_state_bundle(worktree=worktree, tip=tip, destination=destination)
    finally:
        restore()

    assert not destination.exists()
    assert not list(destination.parent.glob(f".{destination.name}.*"))


def _fail_temporary_unlink(
    monkeypatch: pytest.MonkeyPatch, destination: Path, attempted: list[Path]
) -> None:
    """Fail `Path.unlink` for exactly the temporary file
    `_write_and_publish_bundle` claims for `destination` (the
    `tempfile.mkstemp` prefix its own docstring names), leaving every other
    `unlink` call -- including this test's own leftover cleanup once it has
    restored the real function -- untouched. Records the intercepted path in
    `attempted`: an observable stand-in for "the unlink was attempted" that
    counting fake calls would not be."""
    real_unlink = Path.unlink

    def fake_unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name.startswith(f".{destination.name}."):
            attempted.append(self)
            raise OSError("simulated temporary-file cleanup failure")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", fake_unlink)


def _fail_export_ref_deletion(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_bundle_create: bool,
    ref_deletion_raises: bool,
) -> None:
    """Patch `process.run_captured` so the bundle-create subprocess and the
    `EXPORT_BUNDLE_REF` cleanup fail the way each row of the parametrized
    cleanup-independence family below needs -- a nonzero exit for the
    cleanup, matching a real `git update-ref` refusal, or a raised
    `OSError`, matching an invocation that never reached git at all (issue
    #298, the third 19.09.2026 gate REVISE: `_run_git` only wraps
    `ExecutableMissingError`/`ProcessTimedOutError`, so a broader exception
    must be shown to actually reach `_delete_export_ref`). Every other
    invocation, including the real `update-ref` that points
    `EXPORT_BUNDLE_REF` at `tip`, runs for real."""
    real_run_captured = process.run_captured

    def fake_run_captured(arguments: list[str]) -> process.CapturedResult:
        if fail_bundle_create and "bundle" in arguments and "create" in arguments:
            return process.CapturedResult(
                exit_status=1, stdout=b"", stderr=b"simulated bundle create failure"
            )
        if arguments[-3:] == ["update-ref", "-d", store.EXPORT_BUNDLE_REF]:
            if ref_deletion_raises:
                raise OSError("simulated ref-deletion invocation failure")
            return process.CapturedResult(
                exit_status=1, stdout=b"", stderr=b"simulated ref cleanup failure"
            )
        return real_run_captured(arguments)

    monkeypatch.setattr(store.process, "run_captured", fake_run_captured)


def _verify_and_remove_the_leftover_export_ref(worktree: Path) -> None:
    """`EXPORT_BUNDLE_REF` genuinely exists after a cleanup row whose ref
    deletion was made to fail: confirm that before removing it for real, so
    a future regression that quietly drops the real `update-ref` call
    cannot pass this test by accident."""
    assert (
        store._run_git(
            worktree, ["show-ref", "--verify", "--quiet", store.EXPORT_BUNDLE_REF]
        ).exit_status
        == 0
    )
    store._run_git(worktree, ["update-ref", "-d", store.EXPORT_BUNDLE_REF])


class _CleanupFailureCase(NamedTuple):
    """One row of the export-cleanup-independence test family below.

    `arrange` sets up `tip`/`destination` and whichever of the two cleanup
    steps (or the primary write/publish) this row breaks, returning the
    call's `tip` and `destination`. `error_match` filters the outer
    `pytest.raises`; `None` skips the filter for a row whose distinguishing
    text lives only in the body. `cause_fragment` is `None` for a row with
    no primary error (a successful publish whose cleanup still fails).
    `destination_exists_after` and `destination_bytes_after` say what should
    remain at `destination` -- `verify_bundle` additionally asks for a real
    `git bundle verify` where `destination` is a genuine publish rather than
    an untouched pre-existing file. `temp_leftover_expected` says whether the
    temporary file should remain; `postcheck` runs after the real
    `process.run_captured`/`Path.unlink` are restored, for a row that leaves
    a real git-level leftover of its own (a ref cleanup failure) rather than
    only a filesystem one.
    """

    id: str
    arrange: Callable[
        [pytest.MonkeyPatch, Path, Path, Path, list[Path]], tuple[protocol.ObjectId, Path]
    ]
    error_match: str | None
    message_fragments: tuple[str, ...]
    cause_fragment: str | None
    destination_exists_after: bool
    destination_bytes_after: bytes | None
    verify_bundle: bool
    temp_leftover_expected: bool
    postcheck: Callable[[Path], None]


def _arrange_successful_publish_with_a_failed_temp_unlink(
    monkeypatch: pytest.MonkeyPatch,
    bare_remote: Path,
    worktree: Path,
    tmp_path: Path,
    attempted_unlinks: list[Path],
) -> tuple[protocol.ObjectId, Path]:
    tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))
    store.fetch_state(worktree=worktree, remote=str(bare_remote))
    destination = tmp_path / "export" / "state.bundle"
    destination.parent.mkdir()
    _fail_temporary_unlink(monkeypatch, destination, attempted_unlinks)
    return tip, destination


def _arrange_a_failed_bundle_create_with_a_failed_ref_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    bare_remote: Path,
    worktree: Path,
    tmp_path: Path,
    attempted_unlinks: list[Path],
) -> tuple[protocol.ObjectId, Path]:
    tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))
    store.fetch_state(worktree=worktree, remote=str(bare_remote))
    destination = tmp_path / "export" / "state.bundle"
    destination.parent.mkdir()
    _fail_export_ref_deletion(monkeypatch, fail_bundle_create=True, ref_deletion_raises=False)
    return tip, destination


def _arrange_a_refused_publish_with_a_failed_temp_unlink(
    monkeypatch: pytest.MonkeyPatch,
    bare_remote: Path,
    worktree: Path,
    tmp_path: Path,
    attempted_unlinks: list[Path],
) -> tuple[protocol.ObjectId, Path]:
    tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))
    destination = tmp_path / "state.bundle"
    destination.write_bytes(b"an earlier export")
    _fail_temporary_unlink(monkeypatch, destination, attempted_unlinks)
    return tip, destination


def _arrange_a_failed_bundle_create_with_both_cleanup_steps_failing(
    monkeypatch: pytest.MonkeyPatch,
    bare_remote: Path,
    worktree: Path,
    tmp_path: Path,
    attempted_unlinks: list[Path],
) -> tuple[protocol.ObjectId, Path]:
    tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))
    store.fetch_state(worktree=worktree, remote=str(bare_remote))
    destination = tmp_path / "export" / "state.bundle"
    destination.parent.mkdir()
    _fail_export_ref_deletion(monkeypatch, fail_bundle_create=True, ref_deletion_raises=True)
    _fail_temporary_unlink(monkeypatch, destination, attempted_unlinks)
    return tip, destination


@pytest.mark.parametrize(
    "case",
    [
        _CleanupFailureCase(
            id="successful-publish-temp-unlink-fails",
            arrange=_arrange_successful_publish_with_a_failed_temp_unlink,
            error_match="could not remove the now-redundant temporary",
            message_fragments=(),
            cause_fragment=None,
            destination_exists_after=True,
            destination_bytes_after=None,
            verify_bundle=True,
            temp_leftover_expected=True,
            postcheck=lambda worktree: None,
        ),
        _CleanupFailureCase(
            id="bundle-create-fails-and-ref-cleanup-exits-nonzero",
            arrange=_arrange_a_failed_bundle_create_with_a_failed_ref_cleanup,
            error_match=None,
            message_fragments=(
                "simulated bundle create failure",
                store.EXPORT_BUNDLE_REF,
                "simulated ref cleanup failure",
            ),
            cause_fragment="simulated bundle create failure",
            destination_exists_after=False,
            destination_bytes_after=None,
            verify_bundle=False,
            temp_leftover_expected=False,
            postcheck=_verify_and_remove_the_leftover_export_ref,
        ),
        _CleanupFailureCase(
            id="refused-publish-temp-unlink-fails",
            arrange=_arrange_a_refused_publish_with_a_failed_temp_unlink,
            error_match="already exists",
            message_fragments=("simulated temporary-file cleanup failure",),
            cause_fragment="already exists",
            destination_exists_after=True,
            destination_bytes_after=b"an earlier export",
            verify_bundle=False,
            temp_leftover_expected=True,
            postcheck=lambda worktree: None,
        ),
        _CleanupFailureCase(
            id="bundle-create-fails-and-both-cleanup-steps-fail",
            arrange=_arrange_a_failed_bundle_create_with_both_cleanup_steps_failing,
            error_match=None,
            message_fragments=(
                "simulated bundle create failure",
                store.EXPORT_BUNDLE_REF,
                "simulated ref-deletion invocation failure",
                "simulated temporary-file cleanup failure",
            ),
            cause_fragment="simulated bundle create failure",
            destination_exists_after=False,
            destination_bytes_after=None,
            verify_bundle=False,
            temp_leftover_expected=True,
            postcheck=_verify_and_remove_the_leftover_export_ref,
        ),
    ],
    ids=lambda case: case.id,
)
def test_export_state_bundle_names_every_uncleaned_leftover_and_preserves_the_original_cause(
    monkeypatch: pytest.MonkeyPatch,
    bare_remote: Path,
    worktree: Path,
    tmp_path: Path,
    case: _CleanupFailureCase,
) -> None:
    """The two cleanup steps `_clear_export_artifacts` runs -- clearing
    `EXPORT_BUNDLE_REF` and removing the temporary file -- are independent
    of each other and of whatever exception type each raises (issue #298,
    the second and third 19.09.2026 gate REVISEs): whichever one fails,
    the other is still attempted, the raised error names every artifact
    that is actually left behind together with its own failure, and it is
    chained to the write/publish failure that preceded it when there was
    one -- never silently dropped in favour of a cleanup failure, and never
    silently dropping a cleanup failure in favour of it."""
    attempted_unlinks: list[Path] = []
    tip, destination = case.arrange(monkeypatch, bare_remote, worktree, tmp_path, attempted_unlinks)

    with pytest.raises(protocol.ClaimError, match=case.error_match) as excinfo:
        store.export_state_bundle(worktree=worktree, tip=tip, destination=destination)

    message = str(excinfo.value)
    for fragment in case.message_fragments:
        assert fragment in message
    if case.cause_fragment is None:
        assert excinfo.value.__cause__ is None
    else:
        assert excinfo.value.__cause__ is not None
        assert case.cause_fragment in str(excinfo.value.__cause__)
    assert destination.exists() == case.destination_exists_after
    if case.destination_bytes_after is not None:
        assert destination.read_bytes() == case.destination_bytes_after

    monkeypatch.undo()  # restore the real process.run_captured/Path.unlink before cleanup below

    if case.temp_leftover_expected:
        assert attempted_unlinks, "the temporary file's own unlink must still be attempted"
        assert str(attempted_unlinks[0]) in message
        leftover = list(destination.parent.glob(f".{destination.name}.*"))
        assert leftover
        for stray in leftover:
            stray.unlink()
    else:
        assert not list(destination.parent.glob(f".{destination.name}.*"))
    if case.verify_bundle:
        _git("bundle", "verify", str(destination), cwd=worktree)

    case.postcheck(worktree)


@pytest.mark.parametrize(
    ("local_ref_present", "pass_expected_remote_tip", "deleted_local_expected"),
    [
        pytest.param(True, True, True, id="local-present-tip-given"),
        pytest.param(False, True, False, id="local-absent-tip-given"),
        pytest.param(True, False, True, id="local-present-tip-omitted"),
    ],
)
def test_delete_state_ref_reports_whether_a_local_ref_was_deleted(
    bare_remote: Path,
    worktree: Path,
    *,
    local_ref_present: bool,
    pass_expected_remote_tip: bool,
    deleted_local_expected: bool,
) -> None:
    """`deleted_local`'s return value tracks only whether a local ref
    existed to remove -- independent of whether the caller supplied
    `expected_remote_tip` (`None` only when the caller has already proven
    the remote ref absent, which skips the remote push entirely -- the
    remote itself is unchanged and unchecked in that case)."""
    tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))
    if local_ref_present:
        _git("update-ref", store.STATE_REF, tip, cwd=worktree)
    expected_remote_tip = protocol.ObjectId(tip) if pass_expected_remote_tip else None

    deleted_local = store.delete_state_ref(
        worktree=worktree, remote=str(bare_remote), expected_remote_tip=expected_remote_tip
    )

    assert deleted_local is deleted_local_expected
    assert not store.local_state_ref_exists(worktree)
    if pass_expected_remote_tip:
        assert _state_ref_oid(bare_remote) is None


def test_delete_state_ref_fails_loud_when_the_local_ref_cannot_be_deleted(
    bare_remote: Path, worktree: Path
) -> None:
    tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))
    _git("update-ref", store.STATE_REF, tip, cwd=worktree)
    lock_path = store._git_dir(worktree) / "refs" / "aco" / "state.lock"
    lock_path.touch()
    try:
        with pytest.raises(protocol.ClaimError, match="cannot delete local"):
            store.delete_state_ref(
                worktree=worktree, remote=str(bare_remote), expected_remote_tip=None
            )
    finally:
        lock_path.unlink()
    assert store.local_state_ref_exists(worktree)


def test_delete_state_ref_refuses_a_stale_lease_and_leaves_everything_intact(
    bare_remote: Path, worktree: Path
) -> None:
    stale_tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))
    _git("update-ref", store.STATE_REF, stale_tip, cwd=worktree)
    # The remote moves on after `stale_tip` was read but before the lease-guarded
    # delete runs -- the exact race `--force-with-lease` exists to catch.
    _push_custom_tree(
        bare_remote,
        worktree,
        parent=stale_tip,
        files={store.SCHEMA_TOML_FILENAME: protocol.serialize_empty_schema_toml().encode()},
    )
    moved_tip = _state_ref_oid(bare_remote)

    with pytest.raises(protocol.ClaimError, match=r"cannot delete .*lease"):
        store.delete_state_ref(
            worktree=worktree, remote=str(bare_remote), expected_remote_tip=stale_tip
        )

    assert _state_ref_oid(bare_remote) == moved_tip
    assert store.local_state_ref_exists(worktree)


def _fake_failed_remote_delete(
    monkeypatch: pytest.MonkeyPatch, *, also_break_ls_remote: bool = False
) -> None:
    """A `--force-with-lease` push that always reports failure, standing in
    for a lost response after the remote actually applied it (issue #298,
    19.09.2026 gate finding 5) -- every real git subprocess still runs
    except `push`, and, when `also_break_ls_remote` is set, the repair's own
    re-probe too, reproducing an unreachable remote."""
    real_run_captured = process.run_captured

    def fake_run_captured(arguments: list[str]) -> process.CapturedResult:
        if "push" in arguments:
            return process.CapturedResult(
                exit_status=1, stdout=b"", stderr=b"simulated lost push response"
            )
        if also_break_ls_remote and "ls-remote" in arguments:
            return process.CapturedResult(
                exit_status=128, stdout=b"", stderr=b"simulated network failure"
            )
        return real_run_captured(arguments)

    monkeypatch.setattr(store.process, "run_captured", fake_run_captured)


def test_delete_state_ref_treats_a_lost_leased_push_as_success_when_the_ref_is_already_gone(
    monkeypatch: pytest.MonkeyPatch, bare_remote: Path, worktree: Path
) -> None:
    """The exact race finding 5 names: the server accepts the deletion, then
    the push's own response is lost. A re-probe finds the ref honestly
    gone, so `delete_state_ref` must not report failure for it."""
    tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))
    _git("update-ref", "-d", store.STATE_REF, cwd=bare_remote)
    _fake_failed_remote_delete(monkeypatch)

    deleted_local = store.delete_state_ref(
        worktree=worktree, remote=str(bare_remote), expected_remote_tip=protocol.ObjectId(tip)
    )

    assert deleted_local is False
    assert _state_ref_oid(bare_remote) is None


@pytest.mark.parametrize("remote_moved", [True, False], ids=["remote-moved", "remote-unchanged"])
def test_delete_state_ref_repair_never_recommends_a_manual_lease(
    monkeypatch: pytest.MonkeyPatch, bare_remote: Path, worktree: Path, *, remote_moved: bool
) -> None:
    """A re-probed tip that differs from the lease means the remote moved
    since this reset last observed it (issue #298, 19.09.2026 REVISE
    finding 3): the replacement tip was never checked against a live claim
    nor exported, so the only repair the message may name is re-running
    `aco reset --confirm` -- which repeats both checks -- never a manual
    `--force-with-lease` command against a tip neither check has seen. The
    re-probe can also find the lease's own tip still present, unmoved --
    the push failed for some other reason -- which gets the same
    instruction, worded for an unchanged remote."""
    stale_tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))
    if remote_moved:
        _push_custom_tree(
            bare_remote,
            worktree,
            parent=stale_tip,
            files={store.SCHEMA_TOML_FILENAME: protocol.serialize_empty_schema_toml().encode()},
        )
    current_tip = _state_ref_oid(bare_remote)
    assert current_tip is not None
    _fake_failed_remote_delete(monkeypatch)

    with pytest.raises(protocol.ClaimError) as excinfo:
        store.delete_state_ref(
            worktree=worktree, remote=str(bare_remote), expected_remote_tip=stale_tip
        )

    message = str(excinfo.value)
    if remote_moved:
        assert f"the remote moved to {current_tip}" in message
    else:
        assert f"still present at {current_tip}" in message
    assert "re-run `aco reset --confirm`" in message
    assert "--force-with-lease" not in message


def test_delete_state_ref_reports_an_unknown_lease_outcome_when_the_remote_is_unreachable(
    monkeypatch: pytest.MonkeyPatch, bare_remote: Path, worktree: Path
) -> None:
    tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))
    _fake_failed_remote_delete(monkeypatch, also_break_ls_remote=True)

    with pytest.raises(protocol.ClaimError, match="outcome unknown"):
        store.delete_state_ref(worktree=worktree, remote=str(bare_remote), expected_remote_tip=tip)


def _a_repository_with_a_linked_worktree(
    worktree: Path, tmp_path: Path
) -> tuple[Path, frozenset[Path] | None]:
    linked = _linked_worktrees(worktree, tmp_path, ("lane",))["lane"]
    return worktree, frozenset({worktree, linked})


def _a_plain_directory_outside_any_repository(
    worktree: Path, tmp_path: Path
) -> tuple[Path, frozenset[Path] | None]:
    not_a_repo = tmp_path / "not-a-repo"
    not_a_repo.mkdir()
    return not_a_repo, None


@pytest.mark.parametrize(
    "build_target",
    [_a_repository_with_a_linked_worktree, _a_plain_directory_outside_any_repository],
    ids=["repository", "not-a-repository"],
)
def test_list_worktrees_lists_every_linked_worktree_or_fails_loud_outside_one(
    worktree: Path,
    tmp_path: Path,
    build_target: Callable[[Path, Path], tuple[Path, frozenset[Path] | None]],
) -> None:
    """`list_worktrees` either enumerates every worktree `git worktree list`
    reports for a real repository, or fails loud outside one -- the two
    halves of its one documented contract."""
    target, expected_worktrees = build_target(worktree, tmp_path)

    if expected_worktrees is None:
        with pytest.raises(protocol.ClaimError):
            store.list_worktrees(target)
        return

    assert set(store.list_worktrees(target)) == expected_worktrees


def test_clear_lineage_stamps_clears_every_worktrees_stamp_and_anchor(
    worktree: Path, tmp_path: Path, bare_remote: Path
) -> None:
    linked = _linked_worktrees(worktree, tmp_path, ("lane",))["lane"]
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    # `bootstrap`'s own push never anchors (only a `fetch_state` read does);
    # an explicit read in each worktree is what a real `aco reset` run
    # observes both of them with beforehand.
    store.fetch_state(worktree=worktree, remote=str(bare_remote))
    store.fetch_state(worktree=linked, remote=str(bare_remote))
    for worktree_path in (worktree, linked):
        assert store._read_lineage_stamp(worktree_path) is not None
        assert _has_ref(worktree_path, store._FETCH_ANCHOR_REF)

    cleared = store.clear_lineage_stamps(worktree=worktree)

    assert set(cleared) == {worktree, linked}
    for worktree_path in (worktree, linked):
        assert store._read_lineage_stamp(worktree_path) is None
        assert not _has_ref(worktree_path, store._FETCH_ANCHOR_REF)


def test_clear_lineage_stamps_fails_loud_when_an_anchor_cannot_be_cleared(
    bare_remote: Path, worktree: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    store.fetch_state(worktree=worktree, remote=str(bare_remote))
    lock_path = store._git_dir(worktree) / "refs" / "worktree" / "aco" / "state.lock"
    lock_path.touch()
    try:
        with pytest.raises(protocol.ClaimError, match="cannot clear"):
            store.clear_lineage_stamps(worktree=worktree)
    finally:
        lock_path.unlink()
    assert _has_ref(worktree, store._FETCH_ANCHOR_REF)


def test_clear_lineage_stamps_lets_a_bootstrap_after_a_ref_rewrite_succeed_in_every_worktree(
    worktree: Path, tmp_path: Path, bare_remote: Path
) -> None:
    """The one behaviour reset exists to unblock: without clearing every
    worktree's stamp and anchor first, `fetch_state`'s own lineage guard
    refuses a `bootstrap` that recreates `STATE_REF` from nothing (`store.py`
    `_check_lineage`), in every worktree that had ever observed the old ref."""
    linked = _linked_worktrees(worktree, tmp_path, ("lane",))["lane"]
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    store.fetch_state(worktree=linked, remote=str(bare_remote))
    _git("update-ref", "-d", store.STATE_REF, cwd=bare_remote)

    store.clear_lineage_stamps(worktree=worktree)
    fresh_tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))

    assert store.fetch_state(worktree=linked, remote=str(bare_remote)).tip == fresh_tip
