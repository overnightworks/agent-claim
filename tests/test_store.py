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
from pathlib import Path

import pytest
from test_cli import FakeForge

from agent_coordination import cli as issue_claim
from agent_coordination import github, process, protocol, store

# Syntactically valid but locally unresolvable object ids, for tests that
# exercise a failure path where the actual value never reaches an assertion.
_UNRESOLVABLE_OBJECT_ID = protocol.ObjectId("0" * 40)
_PLACEHOLDER_TIP = protocol.ObjectId("1" * 40)


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
    """A `PushTransport` that never lands a push -- exhausts the retry loop."""

    def push(self, *, worktree: Path, remote: str, ref: str, new_oid: protocol.ObjectId) -> None:
        raise protocol.PushRejectedError("simulated permanent rejection")


def test_bootstrap_creates_the_empty_state_tree_on_a_proven_empty_remote(
    bare_remote: Path, worktree: Path
) -> None:
    assert _state_ref_oid(bare_remote) is None

    tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))

    assert _state_ref_oid(bare_remote) == tip
    assert _tree_entries(bare_remote, tip) == {"schema.toml": "version = 1\n"}


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


def test_fetch_state_reads_via_fetch_head_without_creating_a_local_ref(
    bare_remote: Path, worktree: Path, tmp_path: Path
) -> None:
    created = store.bootstrap(worktree=worktree, remote=str(bare_remote))
    reader = tmp_path / "reader"
    reader.mkdir()
    _git("init", "-b", "main", cwd=reader)

    state = store.fetch_state(worktree=reader, remote=str(bare_remote))

    assert state.tip == created
    assert _git("for-each-ref", store.STATE_REF, cwd=reader).stdout == ""
    fetch_head = (reader / ".git" / "FETCH_HEAD").read_text()
    assert fetch_head.startswith(created)


@pytest.mark.parametrize(
    ("files", "expected_error", "match"),
    [
        pytest.param(
            {"schema.toml": b"version = 1\n", "extra.txt": b"stray\n"},
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
            {"schema.toml": b"version = 2\n"},
            protocol.UnsupportedStateSchemaError,
            "unsupported state schema version 2",
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
    schema_blob = _blob(worktree, b"version = 1\n")
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
    schema_blob = _blob(worktree, b"version = 1\n")
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
    schema_blob = _blob(worktree, b"version = 1\n")
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


def test_fetch_state_rejects_a_non_toml_entry_in_resources(
    bare_remote: Path, worktree: Path
) -> None:
    schema_blob = _blob(worktree, b"version = 1\n")
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


def test_lineage_error_when_the_ref_is_rewritten_without_this_worktrees_stamp_as_an_ancestor(
    bare_remote: Path, worktree: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))  # first observation, stamps it

    _push_custom_tree(
        bare_remote, worktree, parent=None, files={"schema.toml": b"version = 1\n"}
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
        tree_oid=store._write_empty_state_tree(worktree),
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


def test_push_retry_exhausts_and_fails_loud_when_the_ref_never_stops_moving(
    bare_remote: Path, worktree: Path
) -> None:
    # Bootstrapping first gives `observed.tip` a real value, so every retry's
    # `_commit_tree` builds onto a non-None parent -- the ordinary case once
    # the ref already exists, not just the from-empty case slice C1 mostly
    # exercises elsewhere.
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    observed = store.fetch_state(worktree=worktree, remote=str(bare_remote))
    pending = store.PendingCommit(
        tree_oid=store._write_empty_state_tree(worktree),
        message="bootstrap empty claim state\n\noperation_id: never-applied\n",
        operation_id="never-applied",
    )
    transport = _AlwaysRejectingTransport()

    with pytest.raises(protocol.ClaimUnavailableError, match="moved 8 times"):
        store.push_tree(
            worktree=worktree,
            remote=str(bare_remote),
            observed=observed,
            pending=pending,
            transport=transport,
        )


def test_git_push_transport_raises_on_a_non_fast_forward_push(
    bare_remote: Path, worktree: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    orphan_tree = store._write_empty_state_tree(worktree)
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
    top_entries = store._list_tree(worktree, outer_tree_oid, tip=_PLACEHOLDER_TIP, context="state")

    with pytest.raises(protocol.MalformedStateTreeError, match="is not a blob"):
        store._read_schema_toml(worktree, top_entries, tip=_PLACEHOLDER_TIP)


def test_read_schema_toml_fails_loud_when_the_blob_is_unresolvable(worktree: Path) -> None:
    # `git mktree` itself refuses a fabricated oid, so the dangling reference
    # this exercises is built the only way one can occur against a real
    # object database: reference a real blob, then remove its loose object,
    # simulating a corrupted or incomplete local store.
    blob_oid = (
        subprocess.run(
            ["git", "-C", str(worktree), "hash-object", "-w", "--stdin"],
            input=b"version = 1\n",
            check=True,
            capture_output=True,
        )
        .stdout.decode()
        .strip()
    )
    tree = protocol.ObjectId(_raw_tree(worktree, [("100644", "blob", blob_oid, "schema.toml")]))
    loose_object = worktree / ".git" / "objects" / blob_oid[:2] / blob_oid[2:]
    loose_object.unlink()
    top_entries = store._list_tree(worktree, tree, tip=_PLACEHOLDER_TIP, context="state")

    with pytest.raises(protocol.MalformedStateTreeError, match=r"cannot read schema\.toml blob"):
        store._read_schema_toml(worktree, top_entries, tip=_PLACEHOLDER_TIP)


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
        tree_oid=store._write_empty_state_tree(worktree),
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


def test_committer_date_reads_the_commit_that_introduced_a_claim(
    bare_remote: Path, worktree: Path
) -> None:
    tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))

    date = store.committer_date(worktree=worktree, tip=tip, commit=tip)

    assert date.tzinfo is not None


def test_committer_date_refuses_a_commit_that_is_not_an_ancestor_of_the_tip(
    bare_remote: Path, worktree: Path
) -> None:
    tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))
    orphan_tree = store._write_empty_state_tree(worktree)
    orphan_commit = store._commit_tree(
        worktree, tree_oid=orphan_tree, parent=None, message="unrelated root commit\n"
    )

    with pytest.raises(protocol.StateLineageError, match="is not an ancestor"):
        store.committer_date(worktree=worktree, tip=tip, commit=orphan_commit)


def test_committer_date_fails_loud_when_the_commit_is_unresolvable(worktree: Path) -> None:
    with pytest.raises(protocol.StateLineageError):
        store.committer_date(
            worktree=worktree, tip=_PLACEHOLDER_TIP, commit=_UNRESOLVABLE_OBJECT_ID
        )


def _fake_git_log_result(
    monkeypatch: pytest.MonkeyPatch, *, exit_status: int, stdout: bytes
) -> None:
    """Let every real git subprocess run except `log`, which returns a fixed
    result -- isolates `committer_date`'s date-read/parse steps from its
    ancestry check, which a real `merge-base` call still proves."""
    real_run_captured = process.run_captured

    def fake_run_captured(arguments: list[str]) -> process.CapturedResult:
        if "log" in arguments:
            return process.CapturedResult(exit_status=exit_status, stdout=stdout, stderr=b"")
        return real_run_captured(arguments)

    monkeypatch.setattr(store.process, "run_captured", fake_run_captured)


def test_committer_date_fails_loud_when_the_log_read_fails(
    monkeypatch: pytest.MonkeyPatch, bare_remote: Path, worktree: Path
) -> None:
    tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))
    _fake_git_log_result(monkeypatch, exit_status=1, stdout=b"")

    with pytest.raises(protocol.ClaimError, match="cannot read the committer date"):
        store.committer_date(worktree=worktree, tip=tip, commit=tip)


def test_committer_date_fails_loud_on_a_malformed_date(
    monkeypatch: pytest.MonkeyPatch, bare_remote: Path, worktree: Path
) -> None:
    tip = store.bootstrap(worktree=worktree, remote=str(bare_remote))
    _fake_git_log_result(monkeypatch, exit_status=0, stdout=b"not-a-date\n")

    with pytest.raises(protocol.ClaimError, match="malformed committer date"):
        store.committer_date(worktree=worktree, tip=tip, commit=tip)


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


def test_commit_transition_a_different_key_loser_that_exhausts_retries_names_no_holder(
    bare_remote: Path, worktree: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    transport = _AlwaysRejectingTransport()

    intent = _issue_claim_intent(42)
    with pytest.raises(protocol.ClaimUnavailableError, match="moved 32 times; retry the command"):
        store.commit_transition(
            worktree=worktree,
            remote=str(bare_remote),
            subject="claim issue 42",
            intent=intent,
            transport=transport,
        )


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
