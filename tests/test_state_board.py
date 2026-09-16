"""`StateRefBoard` behaviour: the state-ref adapter, reads and writes alike
(issues #248, #283).

The three-item scenario below is built the way #241's archive reader
actually walks a real state tree -- `store`'s own git plumbing
(`hash-object`/`mktree`/`commit-tree`/`push`), never a hand-serialized item
body -- so this module's central claim (the adapter reads what is really
there, and reads it into the exact same shapes the GitHub adapter would)
is proven against a real object database, not an invented one.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_cli import FakeForge, projected_board
from test_store import _blob, _push_raw_state_tree, _raw_tree

from agent_coordination import board, checkout, forge, items, process, protocol, store
from agent_coordination import cli as issue_claim
from agent_coordination.protocol import ClaimUnavailableError, MalformedStateTreeError
from agent_coordination.state_board import ItemWriter, StateRefBoard

REPOSITORY_PATH = "acme/items"
REPOSITORY = forge.RepositoryId("file", ("acme",), "items")
DEFAULT_BRANCH = "main"

CONTAINER_ID = "aco-000001"
CHILD_A_ID = "aco-000002"
CHILD_B_ID = "aco-000003"
CONTAINER_NUMBER = items.item_number(CONTAINER_ID)
CHILD_A_NUMBER = items.item_number(CHILD_A_ID)
CHILD_B_NUMBER = items.item_number(CHILD_B_ID)

EXPECTATION_TEXT = "Does the offline board render without gh?"


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")


def _git(*arguments: str, cwd: Path) -> None:
    subprocess.run(["git", *arguments], cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def bare_remote(tmp_path: Path) -> Path:
    """An empty bare repository standing in for the canonical remote (the
    same shape `test_store.py` builds; duplicated rather than imported so a
    test parameter here is never mistaken by tooling for a redefinition of
    an imported fixture of the same name)."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    _git("init", "--bare", "-b", "main", cwd=remote)
    return remote


@pytest.fixture
def worktree(tmp_path: Path) -> Path:
    """An ordinary git checkout used as this test's client worktree."""
    checkout = tmp_path / "worktree"
    checkout.mkdir()
    _git("init", "-b", "main", cwd=checkout)
    (checkout / "README").write_text("placeholder\n")
    _git("add", "README", cwd=checkout)
    _git("commit", "-m", "initial", cwd=checkout)
    return checkout


@dataclass(frozen=True)
class _Projection:
    """One item's `now`/`next`/`done_when` plus its optional expectation
    lines -- the part of a body two differently-recorded items (a GitHub
    issue, a state-ref item file) still render identically."""

    now: str
    next_step: str
    done_when: str
    expectations: tuple[dict[str, object], ...] = ()

    def block_data(self) -> dict[str, object]:
        data: dict[str, object] = {
            "version": 1,
            "now": self.now,
            "next": self.next_step,
            "done_when": self.done_when,
        }
        if self.expectations:
            data["expectation"] = list(self.expectations)
        return data


def _github_body(projection: _Projection) -> str:
    return f"Prose.\n\n```agent-claim\n{board.render_block(projection.block_data())}```\n"


def _state_ref_body(projection: _Projection, record: dict[str, object]) -> str:
    data = {**projection.block_data(), "record": record}
    return f"Prose.\n\n```agent-claim\n{board.render_block(data)}```\n"


def _record(
    *,
    title: str,
    state: str,
    kind: str,
    parent: str | None = None,
    blocked_by: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "title": title,
        "state": state,
        "kind": kind,
        "labels": [],
        "blocked_by": list(blocked_by),
        "parent": parent,
        "created_at": "2026-09-10T00:00:00Z",
        "updated_at": "2026-09-15T00:00:00Z",
    }


# The one logical scenario every test below reads (issue #248's proof): a
# container with two children, one of the children blocked by its sibling,
# and the other carrying an open expectation line. Built once as plain
# Python values, then rendered two ways -- a GitHub-style body (no record)
# for `FakeForge`, an item-file body (with record) for `StateRefBoard` --
# so "the same data" is a fact this module enforces once, not two
# independently maintained fixtures that could drift apart.
_CONTAINER_PROJECTION = _Projection("Land every slice.", "keiner", "Both slices are closed.")
_CHILD_A_PROJECTION = _Projection(
    "Build slice A.",
    "Ship slice A.",
    "Slice A is merged.",
    expectations=({"text": EXPECTATION_TEXT, "default": "yes"},),
)
_CHILD_B_PROJECTION = _Projection("Build slice B.", "Ship slice B.", "Slice B is merged.")

CONTAINER_BODY = _github_body(_CONTAINER_PROJECTION)
CHILD_A_BODY = _github_body(_CHILD_A_PROJECTION)
CHILD_B_BODY = _github_body(_CHILD_B_PROJECTION)

CONTAINER_ISSUE = board.Issue(
    CONTAINER_NUMBER,
    "Epic",
    (),
    CONTAINER_BODY,
    "2026-09-10T00:00:00Z",
    "2026-09-15T00:00:00Z",
    board.ItemKind.CONTAINER,
    children_closed=0,
    children_total=2,
    blocked_by_count=0,
)
CHILD_A_ISSUE = board.Issue(
    CHILD_A_NUMBER,
    "Slice A",
    (),
    CHILD_A_BODY,
    "2026-09-10T00:00:00Z",
    "2026-09-15T00:00:00Z",
    board.ItemKind.TASK,
    blocked_by_count=0,
)
CHILD_B_ISSUE = board.Issue(
    CHILD_B_NUMBER,
    "Slice B",
    (),
    CHILD_B_BODY,
    "2026-09-10T00:00:00Z",
    "2026-09-15T00:00:00Z",
    board.ItemKind.TASK,
    blocked_by_count=1,
)
GITHUB_ISSUES = (CONTAINER_ISSUE, CHILD_A_ISSUE, CHILD_B_ISSUE)
GITHUB_CHILDREN = {
    CONTAINER_NUMBER: (
        board.ChildItem(CHILD_A_NUMBER, board.ChildState.OPEN),
        board.ChildItem(CHILD_B_NUMBER, board.ChildState.OPEN),
    )
}
GITHUB_DEPENDENCIES = {
    CHILD_B_NUMBER: (
        board.IssueDependency(
            board.IssueReference(REPOSITORY_PATH, CHILD_A_NUMBER), board.BlockerState.OPEN, False
        ),
    )
}


def _item_files() -> dict[str, bytes]:
    container_body = _state_ref_body(
        _CONTAINER_PROJECTION, _record(title="Epic", state="open", kind="container")
    )
    child_a_body = _state_ref_body(
        _CHILD_A_PROJECTION,
        _record(title="Slice A", state="open", kind="task", parent=CONTAINER_ID),
    )
    child_b_body = _state_ref_body(
        _CHILD_B_PROJECTION,
        _record(
            title="Slice B",
            state="open",
            kind="task",
            parent=CONTAINER_ID,
            blocked_by=(CHILD_A_ID,),
        ),
    )
    return {
        f"{CONTAINER_ID}.md": container_body.encode(),
        f"{CHILD_A_ID}.md": child_a_body.encode(),
        f"{CHILD_B_ID}.md": child_b_body.encode(),
    }


# A second open expectation line beside `EXPECTATION_TEXT` (issue #283): one
# CLI-level `aco rule` proof needs a line still open after the ruled one, so
# `aco rulings` still has something to print for this item -- a fully-ruled
# item drops out of `rulings` entirely (it only lists open lines), which
# would otherwise hide the very ruling this proof exists to show.
RULABLE_ID = "aco-000004"
RULABLE_NUMBER = items.item_number(RULABLE_ID)
_RULABLE_PROJECTION = _Projection(
    "Ship it.",
    "Land it.",
    "Done.",
    expectations=(
        {"text": EXPECTATION_TEXT, "default": "yes"},
        {"text": "A second, still-open question?", "default": "later"},
    ),
)


def _rulable_item_files() -> dict[str, bytes]:
    body = _state_ref_body(_RULABLE_PROJECTION, _record(title="Rulable", state="open", kind="task"))
    return {f"{RULABLE_ID}.md": body.encode()}


def _decoded_record(body: str, item_id: str) -> items.ItemRecord:
    """`body`'s `[record]` table, decoded -- the same read `StateRefBoard`
    itself performs, used here to check a write's persisted result straight
    from the state ref, independent of any one adapter instance's view."""
    parsed = board.parse_body(body, storage=board.Storage.STATE_REF)
    assert parsed.record is not None
    return items.parse_item_record(item_id, parsed.record)


def _push_item_tree(remote: Path, worktree_path: Path, item_files: dict[str, bytes]) -> str:
    """Push a state tree carrying `schema.toml` and `items/` (issue #248),
    built through the exact plumbing `store.py` itself uses -- one blob per
    file, one `items/` subtree, one top-level tree, one commit -- never a
    hand-crafted archive or a bypass of `store.read_item_files`'s own read
    path."""
    item_entries = [
        ("100644", "blob", _blob(worktree_path, content), name)
        for name, content in item_files.items()
    ]
    items_tree = _raw_tree(worktree_path, item_entries)
    schema_blob = _blob(worktree_path, protocol.serialize_empty_schema_toml().encode())
    return _push_raw_state_tree(
        remote,
        worktree_path,
        [
            ("100644", "blob", schema_blob, "schema.toml"),
            ("040000", "tree", items_tree, "items"),
        ],
    )


class _UnusedItemWriter:
    """`ItemWriter` for a test that only ever reads: any write reaching it is
    the test's own defect, not a behaviour under test, so it fails loud by
    name rather than silently succeeding at plumbing nothing asked for."""

    def write_item(
        self, item_id: str, *, expected: protocol.ObjectId | None, content: bytes
    ) -> protocol.ObjectId:
        del expected, content
        raise AssertionError(f"unexpected write to item {item_id}")


def _fake_oid(seed: str) -> protocol.ObjectId:
    """A well-formed 40-character git object id, deterministic in `seed` --
    stands in for a real blob oid in the ad hoc scenarios below that never
    push their bytes through real git (issue #283): only its shape, never
    its actual content-addressing, matters to a test that never writes."""
    return protocol.ObjectId(hashlib.sha1(seed.encode()).hexdigest())


def _item_oids(item_files: Mapping[str, bytes]) -> dict[str, protocol.ObjectId]:
    return {items.item_id_from_filename(filename): _fake_oid(filename) for filename in item_files}


def _state_ref_board(
    item_files: Mapping[str, bytes], *, writer: ItemWriter | None = None
) -> StateRefBoard:
    """A `StateRefBoard` over `item_files` alone, its oids fabricated
    (`_fake_oid`) and its writer refusing any write by default -- the one
    constructor call every read-only scenario in this module shares, so a
    constructor signature change (issue #283: `item_oids`, `writer`) has one
    call site to update, not the dozen ad hoc scenarios below."""
    return StateRefBoard(
        repository=REPOSITORY,
        default_branch=DEFAULT_BRANCH,
        item_files=item_files,
        item_oids=_item_oids(item_files),
        writer=writer or _UnusedItemWriter(),
    )


def _fetch_state_ref_board(
    remote: Path, worktree_path: Path, *, writer: ItemWriter | None = None
) -> StateRefBoard:
    state = store.fetch_state(worktree=worktree_path, remote=str(remote))
    item_files = {} if state.tip is None else store.read_item_files(worktree_path, state.tip)
    return StateRefBoard(
        repository=REPOSITORY,
        default_branch=DEFAULT_BRANCH,
        item_files=item_files,
        item_oids=state.items,
        writer=writer or _UnusedItemWriter(),
    )


@pytest.fixture
def state_ref_board(bare_remote: Path, worktree: Path) -> StateRefBoard:
    _push_item_tree(bare_remote, worktree, _item_files())
    return _fetch_state_ref_board(bare_remote, worktree)


def _github_fake(*, open_pull_requests: tuple[board.PullRequest, ...] = ()) -> FakeForge:
    return FakeForge(
        board_issues=GITHUB_ISSUES,
        board_open_pull_requests=open_pull_requests,
        children=GITHUB_CHILDREN,
        board_dependencies=GITHUB_DEPENDENCIES,
        repository=REPOSITORY,
    )


class TestEmptyStart:
    def test_no_items_directory_reads_as_an_empty_board(self) -> None:
        empty = _state_ref_board({})

        assert empty.list_open_board_issues() == ()

    def test_empty_state_ref_reads_as_an_empty_board(
        self, bare_remote: Path, worktree: Path
    ) -> None:
        adapter = _fetch_state_ref_board(bare_remote, worktree)

        assert adapter.list_open_board_issues() == ()


class TestMalformedItem:
    def test_a_record_missing_its_required_fields_fails_loud(self) -> None:
        body = (
            b'```agent-claim\nversion = 1\nnow = "N"\nnext = "X"\ndone_when = "D"\n\n'
            b'[record]\ntitle = "Bare"\n```\n'
        )

        with pytest.raises(MalformedStateTreeError, match="malformed agent-claim block"):
            _state_ref_board({"aco-000001.md": body})

    def test_a_body_with_no_record_table_fails_loud(self) -> None:
        body = CONTAINER_BODY.encode()

        with pytest.raises(MalformedStateTreeError, match="malformed agent-claim block"):
            _state_ref_board({"aco-000001.md": body})

    def test_a_malformed_filename_fails_loud(self) -> None:
        with pytest.raises(MalformedStateTreeError, match="not a valid item file name"):
            _state_ref_board({"not-an-item.md": b"anything"})

    def test_non_utf8_content_fails_loud(self) -> None:
        with pytest.raises(MalformedStateTreeError, match="is not valid UTF-8"):
            _state_ref_board({"aco-000001.md": b"\xff\xfe not utf-8"})


class TestStateRefBoardMethods:
    """Every `StateRefBoard` method, each checked directly against a small
    scenario built for exactly that behaviour -- the `state_ref_board`
    fixture's three-item scenario where it already fits, a bespoke one
    where it does not (a closed item, a dangling reference)."""

    def test_requests_is_always_zero(self, state_ref_board: StateRefBoard) -> None:
        assert state_ref_board.requests == 0

    def test_capability_matches_the_state_ref_surface(self, state_ref_board: StateRefBoard) -> None:
        assert (
            state_ref_board.capability(forge.ForgeOperation.LIST_OPEN_BOARD_ISSUES)
            is forge.Capability.READ_ONLY
        )
        assert (
            state_ref_board.capability(forge.ForgeOperation.LANDING) is forge.Capability.UNSUPPORTED
        )
        assert (
            state_ref_board.capability(forge.ForgeOperation.CREATE_CHILD)
            is forge.Capability.READ_WRITE
        )
        assert (
            state_ref_board.capability(forge.ForgeOperation.LINK_CHILD)
            is forge.Capability.READ_WRITE
        )
        assert (
            state_ref_board.capability(forge.ForgeOperation.UPDATE_ITEM_BODY)
            is forge.Capability.READ_WRITE
        )

    def test_item_reference_reports_an_open_items_title_and_body(
        self, state_ref_board: StateRefBoard
    ) -> None:
        reference = state_ref_board.item_reference(CHILD_A_NUMBER)
        assert reference.state is forge.ItemState.OPEN
        assert reference.title == "Slice A"
        assert reference.is_landing is False

    def test_item_reference_reports_a_closed_item(self) -> None:
        closed_record = _record(title="Closed", state="closed", kind="task")
        closed_record["closed_at"] = "2026-09-14T00:00:00Z"
        body = _state_ref_body(_CHILD_A_PROJECTION, closed_record)
        adapter = _state_ref_board({f"{CHILD_A_ID}.md": body.encode()})

        assert adapter.item_reference(CHILD_A_NUMBER).state is forge.ItemState.CLOSED

    def test_landing_is_unsupported(self, state_ref_board: StateRefBoard) -> None:
        with pytest.raises(forge.ForgeUnsupportedError, match="not yet derived"):
            state_ref_board.landing(CHILD_A_NUMBER)

    def test_parent_issue_is_none_for_an_item_without_one(
        self, state_ref_board: StateRefBoard
    ) -> None:
        assert state_ref_board.parent_issue(CONTAINER_NUMBER) is None

    def test_parent_issue_fails_loud_for_a_dangling_reference(self) -> None:
        record = _record(title="Orphan", state="open", kind="task", parent="aco-999999")
        body = _state_ref_body(_CHILD_A_PROJECTION, record)
        adapter = _state_ref_board({f"{CHILD_A_ID}.md": body.encode()})

        with pytest.raises(
            MalformedStateTreeError, match="referenced as a parent but does not exist"
        ):
            adapter.parent_issue(CHILD_A_NUMBER)

    def test_list_children_is_empty_for_an_unknown_number(
        self, state_ref_board: StateRefBoard
    ) -> None:
        assert state_ref_board.list_children(999999) == ()

    def test_default_branch_returns_the_configured_value(
        self, state_ref_board: StateRefBoard
    ) -> None:
        assert state_ref_board.default_branch() == DEFAULT_BRANCH

    def test_list_board_dependencies_is_empty_for_an_unblocked_item(
        self, state_ref_board: StateRefBoard
    ) -> None:
        assert state_ref_board.list_board_dependencies(CONTAINER_NUMBER) == ()

    def test_list_board_dependencies_is_empty_for_an_unknown_number(
        self, state_ref_board: StateRefBoard
    ) -> None:
        assert state_ref_board.list_board_dependencies(999999) == ()

    def test_list_board_dependencies_fails_loud_for_a_dangling_blocker(self) -> None:
        record = _record(
            title="Orphan blocker", state="open", kind="task", blocked_by=("aco-999999",)
        )
        body = _state_ref_body(_CHILD_B_PROJECTION, record)
        adapter = _state_ref_board({f"{CHILD_B_ID}.md": body.encode()})

        with pytest.raises(
            MalformedStateTreeError, match="is listed as a blocker but does not exist"
        ):
            adapter.list_board_dependencies(CHILD_B_NUMBER)

    def test_list_board_dependencies_reads_a_closed_blockers_closed_at(self) -> None:
        closed_blocker = _record(title="Closed blocker", state="closed", kind="task")
        closed_blocker["closed_at"] = "2026-09-14T00:00:00Z"
        blocker_body = _state_ref_body(_CHILD_A_PROJECTION, closed_blocker)
        blocked = _record(title="Blocked", state="open", kind="task", blocked_by=(CHILD_A_ID,))
        blocked_body = _state_ref_body(_CHILD_B_PROJECTION, blocked)
        adapter = _state_ref_board(
            {
                f"{CHILD_A_ID}.md": blocker_body.encode(),
                f"{CHILD_B_ID}.md": blocked_body.encode(),
            }
        )

        dependencies = adapter.list_board_dependencies(CHILD_B_NUMBER)

        assert dependencies[0].state is board.BlockerState.CLOSED
        assert dependencies[0].closed_at == datetime(2026, 9, 14, tzinfo=UTC)

    def test_pull_request_listings_are_always_empty(self, state_ref_board: StateRefBoard) -> None:
        assert state_ref_board.list_open_board_pull_requests() == ()
        assert state_ref_board.list_recent_merged_board_pull_requests(OBSERVED_AT) == ()


class TestStateRefBoardAgainstARealStateTree:
    def test_lists_every_open_item(self, state_ref_board: StateRefBoard) -> None:
        # `.body` deliberately differs between the two adapters (one carries
        # a `[record]` table, the other never does); every other field --
        # what actually drives `board`/`next` -- must still agree exactly.
        def _identity_fields(issue: board.Issue) -> tuple[object, ...]:
            return (
                issue.number,
                issue.title,
                issue.labels,
                issue.created_at,
                issue.updated_at,
                issue.kind,
                issue.children_closed,
                issue.children_total,
                issue.blocked_by_count,
            )

        actual = {_identity_fields(issue) for issue in state_ref_board.list_open_board_issues()}
        expected = {_identity_fields(issue) for issue in GITHUB_ISSUES}
        assert actual == expected

    def test_lists_a_containers_children(self, state_ref_board: StateRefBoard) -> None:
        assert state_ref_board.list_children(CONTAINER_NUMBER) == GITHUB_CHILDREN[CONTAINER_NUMBER]

    def test_lists_a_blocked_items_dependency(self, state_ref_board: StateRefBoard) -> None:
        assert (
            state_ref_board.list_board_dependencies(CHILD_B_NUMBER)
            == GITHUB_DEPENDENCIES[CHILD_B_NUMBER]
        )

    def test_reads_a_childs_parent(self, state_ref_board: StateRefBoard) -> None:
        parent = state_ref_board.parent_issue(CHILD_A_NUMBER)
        assert parent is not None
        assert parent.reference == board.IssueReference(REPOSITORY_PATH, CONTAINER_NUMBER)
        assert parent.kind is board.ItemKind.CONTAINER

    def test_a_missing_number_is_missing(self, state_ref_board: StateRefBoard) -> None:
        assert state_ref_board.item_reference(999999).state is forge.ItemState.MISSING

    def test_never_shells_out_to_gh(
        self, bare_remote: Path, worktree: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        commands: list[list[str]] = []

        def spy(real: Callable[..., object]) -> Callable[..., object]:
            def wrapped(command: list[str], **kwargs: object) -> object:
                commands.append(command)
                return real(command, **kwargs)

            return wrapped

        monkeypatch.setattr(store.process, "run_captured", spy(process.run_captured))
        monkeypatch.setattr(store.process, "run_bounded", spy(process.run_bounded))
        _push_item_tree(bare_remote, worktree, _item_files())

        adapter = _fetch_state_ref_board(bare_remote, worktree)
        adapter.list_open_board_issues()

        assert commands, "the fixture itself must have run at least one git command"
        assert all(command[0] == "git" for command in commands)


OBSERVED_AT = datetime(2026, 9, 16, tzinfo=UTC)

# A live claim on Slice A (issue #248, Grok final gate blocking 2): opened at
# `OBSERVED_AT` itself, so its rendered age stays fixed across both adapters'
# runs below rather than drifting with wall-clock time.
LIVE_CLAIM = protocol.ActiveClaim(
    identity=protocol.IssueIdentity(CHILD_A_NUMBER),
    claim_id=protocol.ClaimId("a1"),
    agent="Ada",
    role="builder",
    base=protocol.ObjectId("c" * 40),
    branch=f"claude/issue-{CHILD_A_NUMBER}-cut",
    scope=("docs/child-a.md",),
    opened_commit=protocol.ObjectId("c" * 40),
)
# GitHub's own in-flight signal for `LIVE_CLAIM`: a real open pull request
# whose head matches the claim's branch. `state-ref` never sees this PR --
# it cannot list pull requests at all -- and must still reach the identical
# rendered stage from the claim alone.
LIVE_CLAIM_OPEN_PULL_REQUEST = board.PullRequest(
    number=9001, title="", body="", head_ref_name=LIVE_CLAIM.branch
)


def _projected(
    client: forge.BoardSource,
    *,
    storage: board.Storage,
    claims: tuple[protocol.ScopedClaim, ...] = (),
) -> board.Board:
    """`projected_board` fed entirely from `client`'s own read methods --
    the one assembly both parametrized cases below share, mirroring what
    `cli._board` does at the CLI layer without that layer's filesystem and
    network concerns. `open_pull_requests`/`landings_derivable` come from
    `client.capability` (issue #248), the same two reads `cli._board` makes,
    never from the storage pin `config.storage` also carries."""
    issues = client.list_open_board_issues()
    children = {
        issue.number: client.list_children(issue.number)
        for issue in issues
        if issue.kind is board.ItemKind.CONTAINER
    }
    dependencies = {
        issue.number: client.list_board_dependencies(issue.number)
        for issue in issues
        if issue.blocked_by_count > 0
    }
    return projected_board(
        issues,
        client.list_open_board_pull_requests(),
        client.list_recent_merged_board_pull_requests(OBSERVED_AT),
        claims,
        board.BoardConfig(storage=storage),
        repository=REPOSITORY_PATH,
        now=OBSERVED_AT,
        children=children,
        dependencies=dependencies,
        open_pull_requests_supported=(
            client.capability(forge.ForgeOperation.LIST_OPEN_BOARD_PULL_REQUESTS)
            is not forge.Capability.UNSUPPORTED
        ),
        landings_derivable=(
            client.capability(forge.ForgeOperation.LIST_RECENT_MERGED_BOARD_PULL_REQUESTS)
            is not forge.Capability.UNSUPPORTED
        ),
    )


def _rulings_lines(
    client: forge.BoardSource, built: board.Board, *, storage: board.Storage
) -> tuple[str, ...]:
    bodies = {issue.number: issue.body for issue in client.list_open_board_issues()}
    rows = issue_claim._rulings_rows(built, bodies, storage=storage)
    return tuple(issue_claim._rulings_row_text(row) for row in rows)


_EXPECTED_BOARD = _projected(_github_fake(), storage=board.Storage.GITHUB)
EXPECTED_BOARD_TEXT = board.render(_EXPECTED_BOARD)
EXPECTED_NEXT_ACTION = board.next_action(_EXPECTED_BOARD)
EXPECTED_RULINGS_LINES = _rulings_lines(
    _github_fake(), _EXPECTED_BOARD, storage=board.Storage.GITHUB
)
# `state-ref` renders the identical board plus one honest line (issue #248,
# Sonnet review blocking 3): the same content as `_EXPECTED_BOARD`, only
# `landings_derivable` differs, so `replace` -- never a second hand-built
# scenario -- proves the rendered difference is exactly that one line.
EXPECTED_STATE_REF_BOARD_TEXT = board.render(replace(_EXPECTED_BOARD, landings_derivable=False))
EXPECTED_BOARD_TEXT_BY_STORAGE = {
    board.Storage.GITHUB: EXPECTED_BOARD_TEXT,
    board.Storage.STATE_REF: EXPECTED_STATE_REF_BOARD_TEXT,
}

# The same scenario plus `LIVE_CLAIM`, GitHub-side, as the shared expectation
# for `test_a_live_claim_is_in_flight_identically_on_both_adapters` below:
# GitHub reaches `Stage.IN_FLIGHT` via `LIVE_CLAIM_OPEN_PULL_REQUEST`'s
# matching branch, never via the state-ref-only capability fallback.
# `state-ref`'s own expectation is that same board, `replace`d the same way
# as `EXPECTED_STATE_REF_BOARD_TEXT` above, so the only sanctioned
# difference stays the one landings-capability line.
_EXPECTED_BOARD_WITH_LIVE_CLAIM = _projected(
    _github_fake(open_pull_requests=(LIVE_CLAIM_OPEN_PULL_REQUEST,)),
    storage=board.Storage.GITHUB,
    claims=(LIVE_CLAIM,),
)
EXPECTED_BOARD_WITH_LIVE_CLAIM_TEXT_BY_STORAGE = {
    board.Storage.GITHUB: board.render(_EXPECTED_BOARD_WITH_LIVE_CLAIM),
    board.Storage.STATE_REF: board.render(
        replace(_EXPECTED_BOARD_WITH_LIVE_CLAIM, landings_derivable=False)
    ),
}


class TestStateRefInFlightWithoutPullRequests:
    def test_a_live_claim_with_no_pull_requests_is_in_flight(
        self, state_ref_board: StateRefBoard
    ) -> None:
        """`state-ref` cannot list pull requests at all (Grok final gate
        blocking 2): its own honest in-flight signal for a live claim is the
        claim itself, never a PR-head match `open_branches` can never carry
        under this storage backend."""
        built = _projected(state_ref_board, storage=board.Storage.STATE_REF, claims=(LIVE_CLAIM,))

        item = next(item for item in built.items if item.number == CHILD_A_NUMBER)
        assert item.stage is board.Stage.IN_FLIGHT


class TestTwoAdapterParity:
    """One scenario, driven through both adapters (issue #248's proof): the
    GitHub fake's own `board`/`next`/`rulings` rows, computed once above as
    the expected value, and each adapter's own rows, computed here -- a
    genuine defect in either adapter shows up as a mismatch against that
    one shared expectation, never two fixtures compared only to each other.
    """

    @pytest.mark.parametrize(
        ("client_kind", "storage"),
        [
            pytest.param("github", board.Storage.GITHUB, id="github"),
            pytest.param("state-ref", board.Storage.STATE_REF, id="state-ref"),
        ],
    )
    def test_board_next_and_rulings_match_the_shared_expectation(
        self,
        request: pytest.FixtureRequest,
        client_kind: str,
        storage: board.Storage,
    ) -> None:
        client: forge.BoardSource = (
            _github_fake()
            if client_kind == "github"
            else request.getfixturevalue("state_ref_board")
        )

        built = _projected(client, storage=storage)

        assert board.render(built) == EXPECTED_BOARD_TEXT_BY_STORAGE[storage]
        assert board.next_action(built) == EXPECTED_NEXT_ACTION
        assert _rulings_lines(client, built, storage=storage) == EXPECTED_RULINGS_LINES

    @pytest.mark.parametrize(
        ("client_kind", "storage"),
        [
            pytest.param("github", board.Storage.GITHUB, id="github"),
            pytest.param("state-ref", board.Storage.STATE_REF, id="state-ref"),
        ],
    )
    def test_a_live_claim_is_in_flight_identically_on_both_adapters(
        self,
        request: pytest.FixtureRequest,
        client_kind: str,
        storage: board.Storage,
    ) -> None:
        """`LIVE_CLAIM` on Slice A reaches `Stage.IN_FLIGHT` on both
        adapters (issue #248, Grok final gate blocking 2) -- GitHub from
        `LIVE_CLAIM_OPEN_PULL_REQUEST`'s matching branch, `state-ref` from
        the claim alone, since it cannot list pull requests at all. Two
        different signals, the identical rendered board: the state-ref
        fallback never drifts from what a real in-flight lane looks like on
        GitHub.
        """
        client: forge.BoardSource = (
            _github_fake(open_pull_requests=(LIVE_CLAIM_OPEN_PULL_REQUEST,))
            if client_kind == "github"
            else request.getfixturevalue("state_ref_board")
        )

        built = _projected(client, storage=storage, claims=(LIVE_CLAIM,))

        assert board.render(built) == EXPECTED_BOARD_WITH_LIVE_CLAIM_TEXT_BY_STORAGE[storage]


class TestStateRefBoardWrites:
    """`StateRefBoard`'s three mutating operations (issue #283), driven
    directly against a real bare remote through the production
    `cli._StoreItemWriter` -- the same git plumbing `_state_ref_forge`
    wires in production, never a fake CAS."""

    def _writer(self, remote: Path, worktree_path: Path) -> ItemWriter:
        return issue_claim._StoreItemWriter(worktree_path, str(remote))

    def test_update_item_body_refreshes_updated_at_preserves_the_rest_and_is_visible_immediately(
        self, bare_remote: Path, worktree: Path
    ) -> None:
        _push_item_tree(bare_remote, worktree, _item_files())
        adapter = _fetch_state_ref_board(
            bare_remote, worktree, writer=self._writer(bare_remote, worktree)
        )
        before_body = adapter.item_reference(CHILD_A_NUMBER).body
        assert before_body is not None
        before_record = _decoded_record(before_body, CHILD_A_ID)
        new_body = before_body.replace("Prose.", "Edited prose.", 1)

        adapter.update_item_body(CHILD_A_NUMBER, new_body)

        # Visible on this same instance without a re-fetch (proof 2: "the
        # same process sees it").
        after = adapter.item_reference(CHILD_A_NUMBER)
        assert after.body is not None
        assert after.body.startswith("Edited prose.")
        after_record = _decoded_record(after.body, CHILD_A_ID)
        assert replace(after_record, updated_at=before_record.updated_at) == before_record
        assert after_record.updated_at != before_record.updated_at
        assert board.RECORD_TIMESTAMP_PATTERN.fullmatch(after_record.updated_at)

    def test_update_item_body_overwrites_a_hostile_record_with_the_stored_one(
        self, bare_remote: Path, worktree: Path
    ) -> None:
        """A caller's piped body can carry any `[record]` table it likes --
        `update_item_body` never trusts it. Only `updated_at` moves; every
        other field, `parent` and `state` included, comes from the record
        this adapter already holds for the item, not from the body it was
        handed."""
        _push_item_tree(bare_remote, worktree, _item_files())
        adapter = _fetch_state_ref_board(
            bare_remote, worktree, writer=self._writer(bare_remote, worktree)
        )
        before_body = adapter.item_reference(CHILD_A_NUMBER).body
        assert before_body is not None
        before_record = _decoded_record(before_body, CHILD_A_ID)
        hostile_record = _record(title="Hostile", state="closed", kind="task", parent=CHILD_B_ID)
        hostile_body = _state_ref_body(_CHILD_A_PROJECTION, hostile_record)

        adapter.update_item_body(CHILD_A_NUMBER, hostile_body)

        after = adapter.item_reference(CHILD_A_NUMBER)
        assert after.body is not None
        after_record = _decoded_record(after.body, CHILD_A_ID)
        assert replace(after_record, updated_at=before_record.updated_at) == before_record
        assert after_record.updated_at != before_record.updated_at
        assert after_record.parent != hostile_record["parent"]
        assert after_record.state.value != hostile_record["state"]

    def test_a_second_write_from_the_same_read_state_refuses_and_overwrites_nothing(
        self, bare_remote: Path, worktree: Path
    ) -> None:
        """Proof 3: two writers both read the item at the same oid; the
        first write lands, the second -- still holding that now-stale oid --
        refuses with #279's own sentence, and the remote keeps the first
        writer's content."""
        _push_item_tree(bare_remote, worktree, _item_files())
        writer = self._writer(bare_remote, worktree)
        first = _fetch_state_ref_board(bare_remote, worktree, writer=writer)
        second = _fetch_state_ref_board(bare_remote, worktree, writer=writer)
        first_body = first.item_reference(CHILD_A_NUMBER).body
        second_body = second.item_reference(CHILD_A_NUMBER).body
        assert first_body is not None
        assert second_body is not None

        first.update_item_body(CHILD_A_NUMBER, first_body.replace("Prose.", "First writer.", 1))
        second_writer_body = second_body.replace("Prose.", "Second writer.", 1)

        with pytest.raises(ClaimUnavailableError, match="written since it was read"):
            second.update_item_body(CHILD_A_NUMBER, second_writer_body)
        state = store.fetch_state(worktree=worktree, remote=str(bare_remote))
        assert state.tip is not None
        stored = store.read_item_files(worktree, state.tip)[f"{CHILD_A_ID}.md"]
        assert stored.startswith(b"First writer.")

    def test_create_child_mints_an_id_sets_the_parent_and_appears_in_list_children(
        self, bare_remote: Path, worktree: Path
    ) -> None:
        """Proof 4/6: `create_child` mints an `aco-` id, records `parent`,
        and the new child shows up under the container's `list_children` --
        both on this same instance and on a freshly re-fetched one. A
        follow-up `link_child` call is the no-op #283 rules it as."""
        _push_item_tree(bare_remote, worktree, _item_files())
        writer = self._writer(bare_remote, worktree)
        adapter = _fetch_state_ref_board(bare_remote, worktree, writer=writer)
        body = f"Parent: #{CONTAINER_NUMBER}\n\n{board.BLOCK_CHILD_SKELETON}"

        child_number = adapter.create_child(
            parent=CONTAINER_NUMBER, title="Slice C", body=body, kind=board.ItemKind.TASK
        )

        reference = adapter.item_reference(child_number)
        assert reference.state is forge.ItemState.OPEN
        assert reference.title == "Slice C"
        parent = adapter.parent_issue(child_number)
        assert parent is not None
        assert parent.reference == board.IssueReference(REPOSITORY_PATH, CONTAINER_NUMBER)
        assert child_number in {child.number for child in adapter.list_children(CONTAINER_NUMBER)}

        adapter.link_child(CONTAINER_NUMBER, child_number)  # no-op: must not raise or change state
        assert adapter.item_reference(child_number).title == "Slice C"

        refreshed = _fetch_state_ref_board(bare_remote, worktree, writer=writer)
        assert child_number in {child.number for child in refreshed.list_children(CONTAINER_NUMBER)}


def _path_without_gh(tmp_path: Path) -> str:
    """A `PATH` carrying a real `git` and nothing else -- proof that a run
    never shells out to `gh` under `storage = state-ref` rather than an
    assertion resting on a fake that could never have called it anyway."""
    git_executable = shutil.which("git")
    assert git_executable is not None, "this test needs a real git on PATH to symlink"
    bin_directory = tmp_path / "bin"
    bin_directory.mkdir()
    (bin_directory / "git").symlink_to(git_executable)
    return str(bin_directory)


class TestCliStateRefForge:
    """`cli._state_ref_forge` (issue #248, Sonnet review blocking 4) driven
    through the real CLI entry point against a real bare `file://` remote
    and a real checkout -- never through a fake or a monkeypatch of the
    function itself, unlike every other CLI test, which stands in a stub for
    it (`test_lazy_forge_builds_a_state_ref_board_under_the_state_ref_pin`
    in `test_cli.py`)."""

    def _pin_state_ref(self, tmp_path: Path) -> None:
        config_dir = tmp_path / ".agent-claim"
        config_dir.mkdir()
        (config_dir / "board.toml").write_text('storage = "state-ref"\n')

    def _live_state_ref_checkout(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
        item_files: dict[str, bytes],
    ) -> None:
        """A real checkout pinned to `storage = "state-ref"`, `origin`
        pointed at `bare_remote` with `item_files` already seeded and
        `origin/HEAD` set, and `PATH` carrying no `gh` -- the one setup
        every write proof below shares (issue #283)."""
        remote_url = f"file://{bare_remote}"
        _push_item_tree(bare_remote, worktree, item_files)
        _git("remote", "add", "origin", remote_url, cwd=worktree)
        _git("push", "origin", "main", cwd=worktree)
        _git("remote", "set-head", "origin", "main", cwd=worktree)
        self._pin_state_ref(tmp_path)
        monkeypatch.setenv("PATH", _path_without_gh(tmp_path))
        monkeypatch.chdir(worktree)

    def test_rule_writes_a_state_ref_item_and_a_fresh_process_reads_it_ruled(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #283 proof 1: `aco rule` under `storage = state-ref` writes
        straight into `items/<id>.md` through `_StoreItemWriter` -- no
        `gh`, no forge -- and a second `aco rulings` invocation (its own
        fresh fetch, standing in for a second process) reads the line back
        ruled."""
        self._live_state_ref_checkout(
            monkeypatch, tmp_path, bare_remote, worktree, _rulable_item_files()
        )

        ruled = issue_claim.main(["rule", str(RULABLE_NUMBER), "--line", "1", "--yes"])
        assert ruled == 0
        capsys.readouterr()

        rulings_status = issue_claim.main(["rulings"])
        assert rulings_status == 0
        lines = capsys.readouterr().out.splitlines()
        assert any(line.strip().startswith("1 ruled yes") for line in lines)

        remote_url = f"file://{bare_remote}"
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        stored = store.read_item_files(worktree, state.tip)[f"{RULABLE_ID}.md"].decode()
        assert stored.startswith("Prose.\n\n```agent-claim\n")
        record = _decoded_record(stored, RULABLE_ID)
        assert record.title == "Rulable"
        assert record.updated_at.startswith(datetime.now(UTC).date().isoformat())

    def test_ask_appends_a_state_ref_item_and_a_fresh_process_reads_it_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #283 proof 2: `aco ask --text ...` under `storage =
        state-ref` appends the proposed line straight into `items/<id>.md`
        through `_StoreItemWriter` -- no `gh`, no forge -- and a second `aco
        rulings` invocation (its own fresh fetch, standing in for a second
        process) reads the line back open."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())
        asked_text = "Does a second process see the appended line?"

        asked = issue_claim.main(["ask", str(CHILD_A_NUMBER), "--text", asked_text])
        assert asked == 0
        capsys.readouterr()

        remote_url = f"file://{bare_remote}"
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        stored = store.read_item_files(worktree, state.tip)[f"{CHILD_A_ID}.md"].decode()
        assert asked_text in stored

        rulings_status = issue_claim.main(["rulings"])
        assert rulings_status == 0
        lines = capsys.readouterr().out.splitlines()
        assert any(line.strip() == f"2 open: {asked_text}" for line in lines)

    def test_claim_passes_slice_rules_against_a_state_ref_item_and_check_reads_it_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #283 proof 5: `aco claim` against a state-ref item runs its
        slice rules from `StateRefBoard`'s own reads alone, and `aco check`
        reads the same item back -- neither ever resolves `gh`. The
        checkout's own cleanliness precondition (`_validate_checkout`) is
        unrelated to this proof and stubbed the same way every other
        `claim` test stubs it."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())
        monkeypatch.setattr(checkout, "_validate_checkout", lambda request: None)
        monkeypatch.setattr(checkout, "_scope_directories", lambda paths: ())
        monkeypatch.setattr(checkout, "versioned_paths", lambda: ("README",))

        claimed = issue_claim.main(
            [
                "claim",
                str(CHILD_A_NUMBER),
                "--agent",
                "Codex Sol",
                "--role",
                "builder",
                "--base",
                "a" * 40,
                "--branch",
                f"codex/issue-{CHILD_A_NUMBER}-slice-a",
                "--scope",
                "README",
                "--claim-id",
                "state-ref-claim",
            ]
        )
        assert claimed == 0
        capsys.readouterr()

        checked = issue_claim.main(["check", str(CHILD_A_NUMBER)])

        assert checked == 0
        assert "body ok" in capsys.readouterr().out

    def test_cut_creates_a_child_against_a_state_ref_container(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #283 proof 6: `aco cut` on a state-ref container runs
        through to a freshly minted child -- `create_child`'s single CAS
        write -- with no `KeyError` from a missing `LINK_CHILD` capability
        and no refusal; the byte-exact `[[slice]]` row removal stays #230
        slice 4f (this container's own `[record]` carries no `slice` rows)."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())

        status = issue_claim.main(["cut", str(CONTAINER_NUMBER), "--title", "Slice C"])

        assert status == 0
        out = capsys.readouterr().out.strip()
        assert out.startswith(f"CUT #{CONTAINER_NUMBER} -> #")
        child_number = int(out.rsplit("#", 1)[1])
        remote_url = f"file://{bare_remote}"
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        child_files = {
            items.item_id_from_filename(name): content
            for name, content in store.read_item_files(worktree, state.tip).items()
        }
        [child_record] = [
            _decoded_record(content.decode(), item_id)
            for item_id, content in child_files.items()
            if items.item_number(item_id) == child_number
        ]
        assert child_record.title == "Slice C"
        assert child_record.parent == CONTAINER_ID

    def test_board_reads_a_real_state_ref_without_gh(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        remote_url = f"file://{bare_remote}"
        _git("remote", "add", "origin", remote_url, cwd=worktree)
        _git("push", "origin", "main", cwd=worktree)
        _git("remote", "set-head", "origin", "main", cwd=worktree)
        store.bootstrap(worktree=worktree, remote=remote_url)
        self._pin_state_ref(tmp_path)
        monkeypatch.setenv("PATH", _path_without_gh(tmp_path))
        monkeypatch.chdir(worktree)

        status = issue_claim.main(["board"])

        assert status == 0
        assert capsys.readouterr().out.strip().endswith("requests: 0")

    def test_board_refuses_without_an_origin_head(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """No `git remote set-head` ever ran here, so `origin/HEAD` stays
        unresolved -- the refusal `_state_ref_forge` owns, worded exactly as
        the run tells the operator to fix it."""
        remote_url = f"file://{bare_remote}"
        _git("remote", "add", "origin", remote_url, cwd=worktree)
        _git("push", "origin", "main", cwd=worktree)
        store.bootstrap(worktree=worktree, remote=remote_url)
        self._pin_state_ref(tmp_path)
        monkeypatch.setenv("PATH", _path_without_gh(tmp_path))
        monkeypatch.chdir(worktree)

        status = issue_claim.main(["board"])

        assert status == 2
        assert capsys.readouterr().err == (
            "ERROR: cannot resolve the default branch; "
            "run aco from a checkout with origin/HEAD set\n"
        )
