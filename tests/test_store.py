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
from collections.abc import Iterator
from pathlib import Path

import pytest

# `test_cli.py`'s own ledger-onboarding double, reused for the ledger half of
# `bootstrap` rather than duplicated, so both test files exercise
# `bootstrap_ledger` against one maintained fake.
from test_cli import FakeForge

from agent_claim import cli as issue_claim
from agent_claim import github, process, protocol, store

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


@pytest.fixture(autouse=True)
def _restore_ledger_global() -> Iterator[None]:
    """`protocol.configure_ledger` binds a process-wide global; the CLI
    `bootstrap` tests below call it through `issue_claim.main` with whatever
    ledger number the fake forge assigned, which would otherwise leak past
    this test and corrupt another test file's assumption about
    `protocol.LEDGER_ISSUE` for the rest of the session.
    """
    previous = protocol.LEDGER_ISSUE
    yield
    protocol.LEDGER_ISSUE = previous


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
            "must contain exactly schema.toml",
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

    with pytest.raises(protocol.ClaimUnavailableError, match="moved 8 times"):
        store.push_tree(
            worktree=worktree,
            remote=str(bare_remote),
            observed=observed,
            pending=pending,
            transport=_AlwaysRejectingTransport(),
        )


def test_git_push_transport_raises_on_a_non_fast_forward_push(
    bare_remote: Path, worktree: Path
) -> None:
    store.bootstrap(worktree=worktree, remote=str(bare_remote))
    orphan_tree = store._write_empty_state_tree(worktree)
    orphan_commit = store._commit_tree(
        worktree, tree_oid=orphan_tree, parent=None, message="unrelated root commit\n"
    )

    with pytest.raises(protocol.PushRejectedError):
        store.GitPushTransport().push(
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


def test_cli_bootstrap_creates_both_a_ledger_and_a_state_ref_from_scratch(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bare_remote: Path,
    worktree: Path,
) -> None:
    """A fresh repository has neither a ledger nor `refs/aco/state`.
    `bootstrap` must still create both, in that order, until the state-ref
    cut (issue #164 slice C2) retires the ledger half for good."""
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    _git("remote", "add", "origin", str(bare_remote), cwd=worktree)
    monkeypatch.chdir(worktree)

    status = issue_claim.main(["--repo", "example/agent-claim", "bootstrap"])

    assert status == 0
    created_ledger = next(iter(client.ledger_items)).number
    lines = capsys.readouterr().out.splitlines()
    assert lines == [f"LEDGER #{created_ledger}", _state_ref_oid(bare_remote)]


def test_cli_bootstrap_is_idempotent_on_a_second_run(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    bare_remote: Path,
    worktree: Path,
) -> None:
    client = FakeForge()
    monkeypatch.setattr(github, "GitHubForge", lambda repository: client)
    _git("remote", "add", "origin", str(bare_remote), cwd=worktree)
    monkeypatch.chdir(worktree)
    issue_claim.main(["--repo", "example/agent-claim", "bootstrap"])
    first_output = capsys.readouterr().out

    status = issue_claim.main(["--repo", "example/agent-claim", "bootstrap"])

    assert status == 0
    assert capsys.readouterr().out == first_output


def test_read_schema_blob_fails_loud_when_the_tree_is_unresolvable(worktree: Path) -> None:
    with pytest.raises(protocol.MalformedStateTreeError, match="cannot list the state tree"):
        store._read_schema_blob(worktree, _UNRESOLVABLE_OBJECT_ID, tip=_PLACEHOLDER_TIP)


def test_read_schema_blob_fails_loud_when_schema_toml_is_not_a_blob(worktree: Path) -> None:
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
    outer_tree = _raw_tree(worktree, [("040000", "tree", inner_tree, "schema.toml")])

    with pytest.raises(protocol.MalformedStateTreeError, match="is not a blob"):
        store._read_schema_blob(worktree, protocol.ObjectId(outer_tree), tip=_PLACEHOLDER_TIP)


def test_read_schema_blob_fails_loud_when_the_blob_is_unresolvable(worktree: Path) -> None:
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
    tree = _raw_tree(worktree, [("100644", "blob", blob_oid, "schema.toml")])
    loose_object = worktree / ".git" / "objects" / blob_oid[:2] / blob_oid[2:]
    loose_object.unlink()

    with pytest.raises(protocol.MalformedStateTreeError, match=r"cannot read schema\.toml blob"):
        store._read_schema_blob(worktree, protocol.ObjectId(tree), tip=_PLACEHOLDER_TIP)


def test_write_lineage_stamp_cleans_up_its_temp_file_on_failure(
    monkeypatch: pytest.MonkeyPatch, worktree: Path
) -> None:
    def fail_replace(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(store.os, "replace", fail_replace)

    with pytest.raises(OSError, match="simulated replace failure"):
        store._write_lineage_stamp(worktree, protocol.ObjectId("a" * 40))

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
