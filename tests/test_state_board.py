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
import io
import json
import shutil
import subprocess
import sys
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path

import pytest
from test_cli import FakeForge, projected_board
from test_store import _blob, _push_raw_state_tree, _raw_tree

from agent_coordination import board, checkout, forge, items, process, protocol, store
from agent_coordination import cli as issue_claim
from agent_coordination.body import (
    BLOCK_CHILD_SKELETON,
    BLOCK_CONTAINER_SKELETON,
    ExpectationLine,
    ItemKind,
    Storage,
    expectation_lines,
    locate_agent_claim_block,
    parse_body,
    render_block,
)
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


def _head_sha(cwd: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


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
    return f"Prose.\n\n```agent-claim\n{render_block(projection.block_data())}```\n"


def _state_ref_body(projection: _Projection, record: dict[str, object]) -> str:
    data = {**projection.block_data(), "record": record}
    return f"Prose.\n\n```agent-claim\n{render_block(data)}```\n"


def _record(
    *,
    title: str,
    state: str,
    kind: str,
    parent: str | None = None,
    labels: tuple[str, ...] = (),
    blocked_by: tuple[str, ...] = (),
    closed_at: str | None = None,
) -> dict[str, object]:
    record: dict[str, object] = {
        "title": title,
        "state": state,
        "kind": kind,
        "labels": list(labels),
        "blocked_by": list(blocked_by),
        "parent": parent,
        "created_at": "2026-09-10T00:00:00Z",
        "updated_at": "2026-09-15T00:00:00Z",
    }
    if closed_at is not None:
        record["closed_at"] = closed_at
    return record


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
    ItemKind.CONTAINER,
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
    ItemKind.TASK,
    blocked_by_count=0,
)
CHILD_B_ISSUE = board.Issue(
    CHILD_B_NUMBER,
    "Slice B",
    (),
    CHILD_B_BODY,
    "2026-09-10T00:00:00Z",
    "2026-09-15T00:00:00Z",
    ItemKind.TASK,
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


def _container_body_with_slices(slice_rows: tuple[tuple[int, str], ...]) -> str:
    """`CONTAINER_ID`'s own body, its `[[slice]]` table set to `slice_rows`
    -- the one shape issue #291's `cut` proofs need and the flat
    `_CONTAINER_PROJECTION`/`_record` pair above cannot express (neither
    carries a `slice` array)."""
    data = {
        **_CONTAINER_PROJECTION.block_data(),
        "slice": [{"index": index, "title": title} for index, title in slice_rows],
        "record": _record(title="Epic", state="open", kind="container"),
    }
    return f"Prose.\n\n```agent-claim\n{render_block(data)}```\n"


def _item_files_with_container_slices(slice_rows: tuple[tuple[int, str], ...]) -> dict[str, bytes]:
    """`_item_files`'s own three-item scenario, `CONTAINER_ID`'s body
    replaced by one carrying `slice_rows` -- `CHILD_A`/`CHILD_B` stay
    untouched so a slice-table proof still exercises a container that
    already has real children, not an invented empty one."""
    return {**_item_files(), f"{CONTAINER_ID}.md": _container_body_with_slices(slice_rows).encode()}


def _item_files_with_one_scoped_slice(
    index: int, title: str, scope: tuple[str, ...] | None
) -> dict[str, bytes]:
    """`_item_files_with_container_slices`'s own one-row shape, that row's
    own `scope` set to `scope` (issue #337) -- `None` names a row with no
    scope of its own, matching how `_render_scope` omits the key entirely
    rather than writing an empty one."""
    entry: dict[str, object] = {"index": index, "title": title}
    if scope is not None:
        entry["scope"] = list(scope)
    data = {
        **_CONTAINER_PROJECTION.block_data(),
        "slice": [entry],
        "record": _record(title="Epic", state="open", kind="container"),
    }
    container_body = f"Prose.\n\n```agent-claim\n{render_block(data)}```\n"
    return {**_item_files(), f"{CONTAINER_ID}.md": container_body.encode()}


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


# A flat two-item scenario for `aco item edit`'s own `blocked_by` proof
# (issue #287, proof 3): neither item is a container's own child, so `aco
# next`'s pick between them turns on `blocked_by` alone, never on a
# container's own cut/close recommendation.
EDIT_TARGET_ID = "aco-00000a"
EDIT_TARGET_NUMBER = items.item_number(EDIT_TARGET_ID)
EDIT_BLOCKER_ID = "aco-00000b"
EDIT_BLOCKER_NUMBER = items.item_number(EDIT_BLOCKER_ID)
_EDIT_TARGET_PROJECTION = _Projection("Ship the target.", "Land it.", "Target is done.")
_EDIT_BLOCKER_PROJECTION = _Projection("Ship the blocker.", "Land it.", "Blocker is done.")


def _edit_target_item_files() -> dict[str, bytes]:
    target_body = _state_ref_body(
        _EDIT_TARGET_PROJECTION, _record(title="Target", state="open", kind="task")
    )
    blocker_body = _state_ref_body(
        _EDIT_BLOCKER_PROJECTION, _record(title="Blocker", state="open", kind="task")
    )
    return {
        f"{EDIT_TARGET_ID}.md": target_body.encode(),
        f"{EDIT_BLOCKER_ID}.md": blocker_body.encode(),
    }


# A flat two-item scenario for `aco item close`'s own freed-item proof
# (issue #289, proofs 1-2): `TARGET` starts blocked by `BLOCKER` alone and
# neither is a container's own child, so closing `BLOCKER` frees `TARGET`
# through `blocked_by` alone, never a container's own cut/close
# recommendation.
CLOSE_BLOCKER_ID = "aco-00000c"
CLOSE_BLOCKER_NUMBER = items.item_number(CLOSE_BLOCKER_ID)
CLOSE_TARGET_ID = "aco-00000d"
CLOSE_TARGET_NUMBER = items.item_number(CLOSE_TARGET_ID)
_CLOSE_BLOCKER_PROJECTION = _Projection("Ship the blocker.", "Land it.", "Blocker is done.")
_CLOSE_TARGET_PROJECTION = _Projection("Ship the target.", "Land it.", "Target is done.")


def _close_scenario_item_files() -> dict[str, bytes]:
    blocker_body = _state_ref_body(
        _CLOSE_BLOCKER_PROJECTION, _record(title="Blocker", state="open", kind="task")
    )
    target_body = _state_ref_body(
        _CLOSE_TARGET_PROJECTION,
        _record(title="Target", state="open", kind="task", blocked_by=(CLOSE_BLOCKER_ID,)),
    )
    return {
        f"{CLOSE_BLOCKER_ID}.md": blocker_body.encode(),
        f"{CLOSE_TARGET_ID}.md": target_body.encode(),
    }


# A container-and-only-child scenario for `aco item close`'s own parent hint
# (issue #348, Beweis 4): the parent's block carries no `[[slice]]` row, so
# closing its only child leaves it freshly closable -- `item close`'s own
# version of `release --merged`'s parent hint.
CLOSE_PARENT_ID = "aco-00000e"
CLOSE_PARENT_NUMBER = items.item_number(CLOSE_PARENT_ID)
CLOSE_CHILD_ID = "aco-00000f"
CLOSE_CHILD_NUMBER = items.item_number(CLOSE_CHILD_ID)
_CLOSE_PARENT_PROJECTION = _Projection("Land every slice.", "keiner", "All slices are closed.")
_CLOSE_CHILD_PROJECTION = _Projection("Ship the slice.", "Land it.", "Slice is done.")


def _close_parent_scenario_item_files() -> dict[str, bytes]:
    parent_body = _state_ref_body(
        _CLOSE_PARENT_PROJECTION, _record(title="Parent", state="open", kind="container")
    )
    child_body = _state_ref_body(
        _CLOSE_CHILD_PROJECTION,
        _record(title="Child", state="open", kind="task", parent=CLOSE_PARENT_ID),
    )
    return {
        f"{CLOSE_PARENT_ID}.md": parent_body.encode(),
        f"{CLOSE_CHILD_ID}.md": child_body.encode(),
    }


def _decoded_record(body: str, item_id: str) -> items.ItemRecord:
    """`body`'s `[record]` table, decoded -- the same read `StateRefBoard`
    itself performs, used here to check a write's persisted result straight
    from the state ref, independent of any one adapter instance's view."""
    parsed = parse_body(body, storage=Storage.STATE_REF)
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

    @pytest.mark.parametrize(
        ("unsupported_call", "sentence"),
        [
            pytest.param(
                lambda adapter: adapter.landing(CHILD_A_NUMBER), "not yet derived", id="landing"
            ),
            pytest.param(
                lambda adapter: adapter.create_issue(title="T", body="", kind=ItemKind.TASK),
                "created by aco item new",
                id="create_issue",
            ),
        ],
    )
    def test_unsupported_operations_refuse_by_name(
        self,
        state_ref_board: StateRefBoard,
        unsupported_call: Callable[[StateRefBoard], object],
        sentence: str,
    ) -> None:
        with pytest.raises(forge.ForgeUnsupportedError, match=sentence):
            unsupported_call(state_ref_board)

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

    def test_list_recently_closed_issues_keeps_only_items_closed_at_or_after_the_cutoff(
        self,
    ) -> None:
        """Issue #444's twin search: an item closed before `since` is past the
        window and never listed; one closed exactly at it is."""
        closed_in_window = _record(
            title="In window", state="closed", kind="task", closed_at="2026-09-01T00:00:00Z"
        )
        closed_before = _record(
            title="Before", state="closed", kind="task", closed_at="2026-08-31T23:59:59Z"
        )
        adapter = _state_ref_board(
            {
                f"{CHILD_A_ID}.md": _state_ref_body(_CHILD_A_PROJECTION, closed_in_window).encode(),
                f"{CHILD_B_ID}.md": _state_ref_body(_CHILD_B_PROJECTION, closed_before).encode(),
            }
        )

        closed = adapter.list_recently_closed_issues(datetime(2026, 9, 1, tzinfo=UTC))

        assert closed == (forge.ClosedIssue(CHILD_A_NUMBER, "In window"),)

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
        assert parent.kind is ItemKind.CONTAINER

    def test_a_missing_number_is_missing(self, state_ref_board: StateRefBoard) -> None:
        assert state_ref_board.item_reference(999999).state is forge.ItemState.MISSING

    def test_item_references_read_every_number_like_item_reference(
        self, state_ref_board: StateRefBoard
    ) -> None:
        references = state_ref_board.item_references((CHILD_A_NUMBER, 999999))

        assert references[CHILD_A_NUMBER] == state_ref_board.item_reference(CHILD_A_NUMBER)
        assert references[999999].state is forge.ItemState.MISSING

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
    storage: Storage,
    claims: tuple[protocol.ScopedClaim, ...] = (),
) -> board.Board:
    """`projected_board` fed entirely from `client`'s own read methods --
    the one assembly both parametrized cases below share, mirroring what
    `cli._board` does at the CLI layer without that layer's filesystem and
    network concerns. `open_pull_requests_supported` comes from
    `client.capability` (issue #248), the same read `cli._board` makes,
    never from the storage pin `config.storage` also carries."""
    issues = client.list_open_board_issues()
    children = {
        issue.number: client.list_children(issue.number)
        for issue in issues
        if issue.kind is ItemKind.CONTAINER
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
    )


def _rulings_lines(
    client: forge.BoardSource, built: board.Board, *, storage: Storage
) -> tuple[str, ...]:
    bodies = {issue.number: issue.body for issue in client.list_open_board_issues()}
    rows = issue_claim._rulings_rows(built, bodies, storage=storage)
    return tuple(issue_claim._rulings_row_text(row, storage) for row in rows)


_EXPECTED_BOARD = _projected(_github_fake(), storage=Storage.GITHUB)
EXPECTED_BOARD_PAYLOAD = board.board_payload(_EXPECTED_BOARD)
EXPECTED_NEXT_ACTION = board.next_action(_EXPECTED_BOARD)
EXPECTED_RULINGS_LINES = _rulings_lines(_github_fake(), _EXPECTED_BOARD, storage=Storage.GITHUB)
# `rulings`' own header line names the item under the pin (issue #292):
# `aco-xxxxxx` under state-ref, `#n` unchanged under github -- the same
# shared scenario re-rendered under each storage, so the only sanctioned
# difference is that one id-shaped prefix, never a second hand-built
# expectation.
EXPECTED_RULINGS_LINES_BY_STORAGE = {
    Storage.GITHUB: EXPECTED_RULINGS_LINES,
    Storage.STATE_REF: _rulings_lines(_github_fake(), _EXPECTED_BOARD, storage=Storage.STATE_REF),
}
# `state-ref` projects the identical scenario, never `_EXPECTED_BOARD` itself
# `replace`d: `item.actionable_reason` (issue #300 residual 2) is now baked
# in at build time from `config.storage`, exactly like every other field a
# real `state-ref` pin changes, so reusing the `Storage.GITHUB`-built board
# for a different storage's own payload would silently keep its stale `#n`
# blocker text. A fresh `_projected` call under `Storage.STATE_REF` bakes
# that text correctly; the two payloads otherwise agree apart from the
# id-shaped pins, since this scenario carries no merged pull request at all
# (issue #371 retired the one line that used to differ).
EXPECTED_STATE_REF_BOARD_PAYLOAD = board.board_payload(
    _projected(_github_fake(), storage=Storage.STATE_REF)
)
EXPECTED_BOARD_PAYLOAD_BY_STORAGE = {
    Storage.GITHUB: EXPECTED_BOARD_PAYLOAD,
    Storage.STATE_REF: EXPECTED_STATE_REF_BOARD_PAYLOAD,
}

# The same scenario plus `LIVE_CLAIM`, GitHub-side, as the shared expectation
# for `test_a_live_claim_is_in_flight_identically_on_both_adapters` below:
# GitHub reaches `Stage.IN_FLIGHT` via `LIVE_CLAIM_OPEN_PULL_REQUEST`'s
# matching branch, never via the state-ref-only capability fallback.
# `state-ref`'s own expectation is a fresh build under `Storage.STATE_REF`
# (same reasoning as `EXPECTED_STATE_REF_BOARD_PAYLOAD` above), so the only
# sanctioned difference stays the id-shaped pins.
_EXPECTED_BOARD_WITH_LIVE_CLAIM = _projected(
    _github_fake(open_pull_requests=(LIVE_CLAIM_OPEN_PULL_REQUEST,)),
    storage=Storage.GITHUB,
    claims=(LIVE_CLAIM,),
)
EXPECTED_BOARD_WITH_LIVE_CLAIM_PAYLOAD_BY_STORAGE = {
    Storage.GITHUB: board.board_payload(_EXPECTED_BOARD_WITH_LIVE_CLAIM),
    Storage.STATE_REF: board.board_payload(
        _projected(
            _github_fake(open_pull_requests=(LIVE_CLAIM_OPEN_PULL_REQUEST,)),
            storage=Storage.STATE_REF,
            claims=(LIVE_CLAIM,),
        )
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
        built = _projected(state_ref_board, storage=Storage.STATE_REF, claims=(LIVE_CLAIM,))

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
            pytest.param("github", Storage.GITHUB, id="github"),
            pytest.param("state-ref", Storage.STATE_REF, id="state-ref"),
        ],
    )
    def test_board_next_and_rulings_match_the_shared_expectation(
        self,
        request: pytest.FixtureRequest,
        client_kind: str,
        storage: Storage,
    ) -> None:
        client: forge.BoardSource = (
            _github_fake()
            if client_kind == "github"
            else request.getfixturevalue("state_ref_board")
        )

        built = _projected(client, storage=storage)

        assert board.board_payload(built) == EXPECTED_BOARD_PAYLOAD_BY_STORAGE[storage]
        assert board.next_action(built) == EXPECTED_NEXT_ACTION
        assert (
            _rulings_lines(client, built, storage=storage)
            == EXPECTED_RULINGS_LINES_BY_STORAGE[storage]
        )

    @pytest.mark.parametrize(
        ("client_kind", "storage"),
        [
            pytest.param("github", Storage.GITHUB, id="github"),
            pytest.param("state-ref", Storage.STATE_REF, id="state-ref"),
        ],
    )
    def test_a_live_claim_is_in_flight_identically_on_both_adapters(
        self,
        request: pytest.FixtureRequest,
        client_kind: str,
        storage: Storage,
    ) -> None:
        """`LIVE_CLAIM` on Slice A reaches `Stage.IN_FLIGHT` on both
        adapters (issue #248, Grok final gate blocking 2) -- GitHub from
        `LIVE_CLAIM_OPEN_PULL_REQUEST`'s matching branch, `state-ref` from
        the claim alone, since it cannot list pull requests at all. Two
        different signals, the identical projected payload: the state-ref
        fallback never drifts from what a real in-flight lane looks like on
        GitHub.
        """
        client: forge.BoardSource = (
            _github_fake(open_pull_requests=(LIVE_CLAIM_OPEN_PULL_REQUEST,))
            if client_kind == "github"
            else request.getfixturevalue("state_ref_board")
        )

        built = _projected(client, storage=storage, claims=(LIVE_CLAIM,))

        assert (
            board.board_payload(built) == EXPECTED_BOARD_WITH_LIVE_CLAIM_PAYLOAD_BY_STORAGE[storage]
        )


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
        assert protocol.RFC3339_TIMESTAMP_PATTERN.fullmatch(after_record.updated_at)

    def _full_delivered_record_body(self) -> str:
        return _state_ref_body(
            _CHILD_B_PROJECTION,
            _record(
                title="Renamed B",
                state="closed",
                kind="task",
                parent=CHILD_A_ID,
                labels=("urgent",),
                blocked_by=(),
                closed_at="2026-09-16T00:00:00Z",
            ),
        )

    def _partial_delivered_record_body(self) -> str:
        """A delivered body whose `[record]` table genuinely omits
        `labels`/`blocked_by` -- spliced in as raw TOML rather than routed
        through `_record`/`render_block`'s own `_render_record`, which
        always fills both in (defaulting to `[]`) even when the source
        dict never set them. The one way a test can tell "the key was
        never delivered" apart from "the key was delivered empty"."""
        projection_block = render_block(_CHILD_B_PROJECTION.block_data())
        record_lines = (
            "\n[record]\n"
            'title = "Renamed B"\n'
            'state = "open"\n'
            'created_at = "2026-09-10T00:00:00Z"\n'
            'updated_at = "2026-09-15T00:00:00Z"\n'
        )
        return f"Prose.\n\n```agent-claim\n{projection_block}{record_lines}```\n"

    def _no_delivered_record_body(self) -> str:
        return _github_body(_CHILD_B_PROJECTION)

    @pytest.mark.parametrize(
        ("delivered_body_factory", "expected_title", "expected_labels", "expected_blocked_by"),
        [
            pytest.param(
                "_full_delivered_record_body",
                "Renamed B",
                ("urgent",),
                (),
                id="a-full-delivered-record-overrides-title-labels-and-blocked-by",
            ),
            pytest.param(
                "_partial_delivered_record_body",
                "Renamed B",
                (),
                (CHILD_A_ID,),
                id="a-delivered-record-omitting-labels-and-blocked-by-keeps-them-stored",
            ),
            pytest.param(
                "_no_delivered_record_body",
                "Slice B",
                (),
                (CHILD_A_ID,),
                id="no-delivered-record-keeps-everything-stored",
            ),
        ],
    )
    def test_update_item_body_takes_title_labels_and_blocked_by_from_a_delivered_record(
        self,
        bare_remote: Path,
        worktree: Path,
        delivered_body_factory: str,
        expected_title: str,
        expected_labels: tuple[str, ...],
        expected_blocked_by: tuple[str, ...],
    ) -> None:
        """Issue #287's owner split for `update_item_body`'s own record
        merge: `title`, `labels`, `blocked_by` come from a delivered
        `[record]` table when the key is present -- a hostile `state` or
        `parent` in that same table never takes, an omitted key (or no
        `[record]` at all, every `rule`/`ask`/`cut` write) keeps this
        item's own stored value. `updated_at` always moves regardless."""
        _push_item_tree(bare_remote, worktree, _item_files())
        adapter = _fetch_state_ref_board(
            bare_remote, worktree, writer=self._writer(bare_remote, worktree)
        )
        before_body = adapter.item_reference(CHILD_B_NUMBER).body
        assert before_body is not None
        before_record = _decoded_record(before_body, CHILD_B_ID)
        delivered_body = getattr(self, delivered_body_factory)()

        adapter.update_item_body(CHILD_B_NUMBER, delivered_body)

        after = adapter.item_reference(CHILD_B_NUMBER)
        assert after.body is not None
        after_record = _decoded_record(after.body, CHILD_B_ID)
        assert after_record.title == expected_title
        assert after_record.labels == expected_labels
        assert after_record.blocked_by == expected_blocked_by
        assert after_record.state == before_record.state
        assert after_record.parent == before_record.parent
        assert after_record.created_at == before_record.created_at
        assert after_record.updated_at != before_record.updated_at
        if delivered_body_factory == "_full_delivered_record_body":
            assert (
                replace(
                    after_record,
                    title=before_record.title,
                    labels=before_record.labels,
                    blocked_by=before_record.blocked_by,
                    updated_at=before_record.updated_at,
                )
                == before_record
            )

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

    def test_close_item_sets_state_and_closed_at_keeps_the_rest_and_refuses_a_second_close(
        self, bare_remote: Path, worktree: Path
    ) -> None:
        """Issue #289: `close_item` moves `state` to `CLOSED` and sets
        `closed_at`/`updated_at` to the timestamp it returns, every other
        record field and the body untouched; a second `close_item` on the
        same already-closed instance refuses by name, naming the date,
        before ever reaching the writer (no second write reaches the
        remote -- the oid this proof reads back is still the first
        close's)."""
        _push_item_tree(bare_remote, worktree, _item_files())
        adapter = _fetch_state_ref_board(
            bare_remote, worktree, writer=self._writer(bare_remote, worktree)
        )
        before_body = adapter.item_reference(CHILD_A_NUMBER).body
        assert before_body is not None
        before_record = _decoded_record(before_body, CHILD_A_ID)

        closed_at = adapter.close_item(CHILD_A_NUMBER)

        after = adapter.item_reference(CHILD_A_NUMBER)
        assert after.state is forge.ItemState.CLOSED
        assert after.body is not None
        after_record = _decoded_record(after.body, CHILD_A_ID)
        assert after_record.state is items.RecordState.CLOSED
        assert after_record.closed_at == closed_at
        assert after_record.updated_at == closed_at
        assert (
            replace(
                after_record,
                state=before_record.state,
                closed_at=None,
                updated_at=before_record.updated_at,
            )
            == before_record
        )
        remote_oid_after_close = adapter.item_oid(CHILD_A_NUMBER)

        with pytest.raises(
            ClaimUnavailableError, match=f"already closed \\(closed on {closed_at}\\)"
        ):
            adapter.close_item(CHILD_A_NUMBER)

        assert adapter.item_oid(CHILD_A_NUMBER) == remote_oid_after_close

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
        body = f"Parent: #{CONTAINER_NUMBER}\n\n{BLOCK_CHILD_SKELETON}"

        child_number = adapter.create_child(
            parent=CONTAINER_NUMBER, title="Slice C", body=body, kind=ItemKind.TASK
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


class TestStateRefBoardAtomicLanding:
    """`prepare_landing`/`mark_landed` (issue #359): the staged half of
    `close_item`'s own composition, reused by `release --merged <sha>`'s
    atomic `protocol.LandingIntent` instead of `close_item`'s own immediate
    write -- proven directly against the fake-oid fixture (`state_ref_board`),
    never `self._writer`, since neither method ever calls it."""

    def test_prepare_landing_composes_the_same_closing_write_as_close_item_but_writes_nothing(
        self, state_ref_board: StateRefBoard
    ) -> None:
        before = state_ref_board.item_reference(CHILD_B_NUMBER)
        assert before.state is forge.ItemState.OPEN
        expected_oid = state_ref_board.item_oid(CHILD_B_NUMBER)

        write = state_ref_board.prepare_landing(CHILD_B_NUMBER)

        assert write.item_id == CHILD_B_ID
        assert write.expected == expected_oid
        record = _decoded_record(write.content.decode("utf-8"), CHILD_B_ID)
        assert record.state is items.RecordState.CLOSED
        assert record.closed_at is not None
        assert record.closed_at == record.updated_at
        # Nothing was written: this same instance still reports the item open,
        # and its own oid is unchanged.
        assert state_ref_board.item_reference(CHILD_B_NUMBER).state is forge.ItemState.OPEN
        assert state_ref_board.item_oid(CHILD_B_NUMBER) == expected_oid

    def test_prepare_landing_refuses_an_already_closed_item(self) -> None:
        closed_at = "2026-09-01T00:00:00Z"
        closed_body = _state_ref_body(
            _CHILD_A_PROJECTION,
            _record(title="Slice A", state="closed", kind="task", closed_at=closed_at),
        )
        adapter = _state_ref_board({f"{CHILD_A_ID}.md": closed_body.encode()})

        with pytest.raises(
            ClaimUnavailableError, match=f"already closed \\(closed on {closed_at}\\)"
        ):
            adapter.prepare_landing(CHILD_A_NUMBER)

    def test_mark_landed_folds_the_committed_write_into_the_in_memory_view(
        self, state_ref_board: StateRefBoard
    ) -> None:
        write = state_ref_board.prepare_landing(CHILD_B_NUMBER)
        committed_oid = _fake_oid("landed")

        state_ref_board.mark_landed(write, committed_oid)

        after = state_ref_board.item_reference(CHILD_B_NUMBER)
        assert after.state is forge.ItemState.CLOSED
        assert state_ref_board.item_oid(CHILD_B_NUMBER) == committed_oid
        assert CHILD_B_NUMBER not in {
            issue.number for issue in state_ref_board.list_open_board_issues()
        }


def _run_ok(arguments: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    """Run `arguments` through `main`, asserting success, and return the
    captured stdout -- the one shape every README-sequence step in
    `TestCliStateRefForge` shares (issue #292 proof 4)."""
    status = issue_claim.main(arguments)
    out = capsys.readouterr().out
    assert status == 0, out
    return out


def _run_refused(arguments: list[str], capsys: pytest.CaptureFixture[str]) -> str:
    """`_run_ok`'s own counterpart for a step the README names as a
    refusal: run `arguments`, assert the CLI's own refusal exit code, and
    return the captured stderr."""
    status = issue_claim.main(arguments)
    err = capsys.readouterr().err
    assert status == 2, err
    return err


def _first_ruling_date(capsys: pytest.CaptureFixture[str]) -> str:
    """`rulings --json`'s first listed line's own `ruled_on` (issue #412's
    envelope: the array now sits under the `rulings` key), read back so a
    README-sequence assertion never has to guess the wall clock."""
    payload = json.loads(_run_ok(["rulings", "--json"], capsys))
    return str(payload["rulings"][0]["lines"][0]["ruled_on"])


def _filled_body(template: str, *, now: str, next_step: str, done_when: str) -> str:
    """A fresh `BLOCK_CHILD_SKELETON`/`BLOCK_CONTAINER_SKELETON` body
    with its three blank projection keys filled -- the one substitution the
    README's "fill Now/Next/Done when" step performs before
    `body --check`/`item edit`."""
    return (
        template.replace('now = ""', f'now = "{now}"')
        .replace('next = ""', f'next = "{next_step}"')
        .replace('done_when = ""', f'done_when = "{done_when}"')
    )


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

    def _pin_state_ref(self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        """Writes the state-ref pin, and stubs `path_is_tracked` to report it
        tracked (#315): this fixture's `board.toml` sits beside `worktree`'s
        real `.git`, never actually `git add`ed to it, so a real
        `git ls-files` check would otherwise never see it."""
        config_dir = tmp_path / ".agent-claim"
        config_dir.mkdir()
        (config_dir / "board.toml").write_text('storage = "state-ref"\n')
        monkeypatch.setattr(checkout, "path_is_tracked", lambda _path, **_kwargs: True)

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
        self._pin_state_ref(monkeypatch, tmp_path)
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

    def test_next_status_and_rulings_print_state_ref_ids_where_github_prints_hash_n(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #292 proofs 1-2: `next`'s own pick, its `Run:` command
        (issue #300 residual 3), `rulings`' row header, and `status`'s
        claimed-issue line print `aco-xxxxxx` under `storage = "state-ref"`
        -- the same id `board.parse_item_reference` already accepts right
        back -- never `#n`. `board --json`'s own payload carries the same
        id-shaped pin too (issue #300 residual 2):
        `EXPECTED_STATE_REF_BOARD_PAYLOAD` above proves it against
        this module's own shared scenario, so this test does not repeat that
        proof against a second one."""
        self._live_state_ref_checkout(
            monkeypatch, tmp_path, bare_remote, worktree, _rulable_item_files()
        )

        next_status = issue_claim.main(["next"])
        assert next_status == 0
        next_out = capsys.readouterr().out
        assert RULABLE_ID in next_out
        assert f"#{RULABLE_NUMBER}" not in next_out
        assert f"Run: aco claim {RULABLE_ID} --scope <paths>" in next_out

        rulings_status = issue_claim.main(["rulings"])
        assert rulings_status == 0
        rulings_out = capsys.readouterr().out
        assert rulings_out.splitlines()[0].startswith(f"{RULABLE_ID} ")
        assert f"#{RULABLE_NUMBER}" not in rulings_out

        monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
        monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
        monkeypatch.setattr(checkout, "versioned_paths", lambda **_kwargs: ("README",))
        claimed = issue_claim.main(
            [
                "claim",
                str(RULABLE_NUMBER),
                "--agent",
                "Codex Sol",
                "--role",
                "builder",
                "--base",
                "a" * 40,
                "--branch",
                f"codex/issue-{RULABLE_NUMBER}-rulable",
                "--scope",
                "README",
                "--claim-id",
                "state-ref-claim",
            ]
        )
        assert claimed == 0
        capsys.readouterr()

        status = issue_claim.main(["status", str(RULABLE_NUMBER)])
        assert status == 0
        status_out = capsys.readouterr().out
        assert f"CLAIMED issue {RULABLE_ID}" in status_out
        assert f"issue #{RULABLE_NUMBER}" not in status_out

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

    def test_ask_with_a_picture_writes_the_card_fields_into_the_state_ref_item(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #295 against issue #283's own write path: `aco ask --text
        ... --question ... --example ... --picture FILE.svg` under `storage
        = state-ref` lands all three optional card fields in the same
        `items/<id>.md` write -- a combination `--picture` alone was never
        proved against, since every other `--picture` proof (`test_cli.py`)
        drives the `FakeForge`, never the real `file://` remote this module
        owns."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())
        picture_file = tmp_path / "sketch.svg"
        picture_svg = '<svg xmlns="http://www.w3.org/2000/svg"><circle cx="5" cy="5" r="4"/></svg>'
        picture_file.write_text(picture_svg, encoding="utf-8")
        asked_text = "Brauchen wir Admin-Rechte?"

        asked = issue_claim.main(
            [
                "ask",
                str(CHILD_A_NUMBER),
                "--text",
                asked_text,
                "--question",
                "Admin-Rechte nötig?",
                "--example",
                "Wie beim letzten Import.",
                "--picture",
                str(picture_file),
            ]
        )
        assert asked == 0
        capsys.readouterr()

        remote_url = f"file://{bare_remote}"
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        stored = store.read_item_files(worktree, state.tip)[f"{CHILD_A_ID}.md"].decode()
        lines = expectation_lines(stored, storage=Storage.STATE_REF)
        assert lines[1] == ExpectationLine(
            2,
            asked_text,
            None,
            None,
            default="yes",
            question="Admin-Rechte nötig?",
            example="Wie beim letzten Import.",
            picture=picture_svg,
        )

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
        monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
        monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
        monkeypatch.setattr(checkout, "versioned_paths", lambda **_kwargs: ("README",))

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
        and no refusal; this container's own `[record]` carries no `slice`
        rows, so it creates an untied child exactly like GitHub does for the
        same shape (`test_cut_creates_an_untied_child_with_no_slice_table`).
        The byte-exact `[[slice]]` row removal itself is issue #291's own
        proof, below."""
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

    def test_cut_removes_the_slice_row_byte_exact_and_a_fresh_board_shows_the_child(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #291 proof 1: `aco cut` on a state-ref container with a
        `[[slice]]` table creates the child under `record.parent` and
        removes the cut row from the container's own body byte-exact --
        every byte outside the removed `[[slice]]` entry, `now`/`next`/
        `done_when` and the fence lines included, stays identical -- and a
        fresh `aco board` shows the new child."""
        item_files = _item_files_with_container_slices(((1, "Slice C"), (2, "Slice D")))
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, item_files)
        remote_url = f"file://{bare_remote}"
        before_state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert before_state.tip is not None
        before_container = store.read_item_files(worktree, before_state.tip)[
            f"{CONTAINER_ID}.md"
        ].decode()
        before_located = locate_agent_claim_block(before_container)

        status = issue_claim.main(["cut", str(CONTAINER_NUMBER), "--title", "Slice C"])

        assert status == 0
        out = capsys.readouterr().out.strip()
        assert out.startswith(f"CUT #{CONTAINER_NUMBER} row 1 -> #")
        child_number = int(out.rsplit("#", 1)[1])
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        item_files_after = store.read_item_files(worktree, state.tip)
        [child_id] = [
            items.item_id_from_filename(name)
            for name in item_files_after
            if items.item_number(items.item_id_from_filename(name)) == child_number
        ]
        child_record = _decoded_record(item_files_after[f"{child_id}.md"].decode(), child_id)
        assert child_record.parent == CONTAINER_ID
        after_container = item_files_after[f"{CONTAINER_ID}.md"].decode()
        after_located = locate_agent_claim_block(after_container)
        assert (
            before_container[: before_located.content_start]
            == (after_container[: after_located.content_start])
        )
        assert (
            before_container[before_located.content_end :]
            == (after_container[after_located.content_end :])
        )
        assert after_located.data["slice"] == [{"index": 2, "title": "Slice D"}]
        assert after_located.data["now"] == before_located.data["now"]
        assert after_located.data["next"] == before_located.data["next"]
        assert after_located.data["done_when"] == before_located.data["done_when"]
        before_record = _decoded_record(before_container, CONTAINER_ID)
        after_record = _decoded_record(after_container, CONTAINER_ID)
        assert replace(after_record, updated_at=before_record.updated_at) == before_record
        assert after_record.updated_at != before_record.updated_at

        board_status = issue_claim.main(["board", "--json"])
        assert board_status == 0
        payload = json.loads(capsys.readouterr().out)
        board_items = {item["number"]: item for item in payload["items"]}
        assert board_items[child_number]["title"] == "Slice C"
        container_children = {
            child["number"] for child in board_items[CONTAINER_NUMBER]["container"]["open_children"]
        }
        assert child_number in container_children

    def test_cut_row_selects_the_named_slice_entry_under_state_ref(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #291 proof 2 (selection): `--row 2` cuts the container's
        second `[[slice]]` entry, leaving the first untouched -- the same
        `[[slice]]` table `aco board` already renders, no second parser."""
        item_files = _item_files_with_container_slices(((1, "Slice C"), (2, "Slice D")))
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, item_files)

        status = issue_claim.main(
            ["cut", str(CONTAINER_NUMBER), "--title", "Slice D", "--row", "2", "--json"]
        )

        assert status == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["row"] == 2
        remote_url = f"file://{bare_remote}"
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        container_body = store.read_item_files(worktree, state.tip)[f"{CONTAINER_ID}.md"].decode()
        remaining = locate_agent_claim_block(container_body).data
        assert remaining["slice"] == [{"index": 1, "title": "Slice C"}]

    def test_cut_row_refuses_a_missing_row_under_state_ref(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #291 proof 2 (refusal): `--row 9` names no entry while row 1
        is still cuttable -- the same by-name refusal GitHub's own
        `test_cut_refuses_a_row_with_no_cuttable_row` proves, and nothing
        reaches the remote."""
        item_files = _item_files_with_container_slices(((1, "Slice C"),))
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, item_files)
        remote_url = f"file://{bare_remote}"
        before = store.fetch_state(worktree=worktree, remote=remote_url)

        status = issue_claim.main(["cut", str(CONTAINER_NUMBER), "--title", "X", "--row", "9"])

        assert status == 2
        assert capsys.readouterr().err == (
            f"ERROR: #{CONTAINER_NUMBER} has no row 9; cuttable rows: 1\n"
        )
        after = store.fetch_state(worktree=worktree, remote=remote_url)
        assert after.tip == before.tip

    def _cut_child_scope(
        self, worktree: Path, bare_remote: Path, capsys: pytest.CaptureFixture[str]
    ) -> tuple[str, ...] | None:
        """The just-cut child's own top-level `scope`, `None` when the block
        carries no `scope` key at all -- shared by every scope-inheritance
        case below so each states only its own arrangement and expectation."""
        out = capsys.readouterr().out.strip()
        child_number = int(out.rsplit("#", 1)[1])
        remote_url = f"file://{bare_remote}"
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        item_files_after = store.read_item_files(worktree, state.tip)
        [child_id] = [
            items.item_id_from_filename(name)
            for name in item_files_after
            if items.item_number(items.item_id_from_filename(name)) == child_number
        ]
        data = locate_agent_claim_block(item_files_after[f"{child_id}.md"].decode()).data
        scope = data.get("scope")
        return None if scope is None else protocol.valid_scope(scope)

    @pytest.mark.parametrize(
        (
            "has_row",
            "row_scope",
            "requested_scope",
            "title",
            "expected_status",
            "expected_child_scope",
            "expected_err",
        ),
        [
            pytest.param(
                True,
                None,
                ("src/c.py",),
                "Slice C",
                0,
                ("src/c.py",),
                None,
                id="fills-an-empty-row",
            ),
            pytest.param(
                True,
                ("src/c.py",),
                None,
                "Slice C",
                0,
                ("src/c.py",),
                None,
                id="inherits-without-the-flag",
            ),
            pytest.param(
                True,
                ("src/c.py",),
                ("src/other.py",),
                "Slice C",
                2,
                None,
                issue_claim.CUT_ROW_SCOPE_ALREADY_SET.format(index=1),
                id="refuses-a-row-that-already-names-one",
            ),
            pytest.param(
                False,
                None,
                ("src/loose.py",),
                "Loose Cut",
                0,
                ("src/loose.py",),
                None,
                id="no-slice-table-becomes-the-childs-scope-directly",
            ),
        ],
    )
    def test_cut_scope_fills_inherits_or_refuses_against_a_slice_rows_scope(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
        has_row: bool,
        row_scope: tuple[str, ...] | None,
        requested_scope: tuple[str, ...] | None,
        title: str,
        expected_status: int,
        expected_child_scope: tuple[str, ...] | None,
        expected_err: str | None,
    ) -> None:
        """Issue #337 proof 2: `cut --scope` fills a linked row's own scope
        only when the row carries none -- a row that already names one
        refuses by name instead, since the row is the one place to change
        it -- and with no linked row at all, `--scope` becomes the fresh
        child's own top-level scope directly; either way a successful cut's
        child inherits exactly the row's own scope."""
        item_files = (
            _item_files_with_one_scoped_slice(1, title, row_scope) if has_row else _item_files()
        )
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, item_files)
        remote_url = f"file://{bare_remote}"
        before = store.fetch_state(worktree=worktree, remote=remote_url)
        argv = ["cut", str(CONTAINER_NUMBER), "--title", title]
        for path in requested_scope or ():
            argv.extend(["--scope", path])

        status = issue_claim.main(argv)

        assert status == expected_status
        if expected_status == 0:
            assert self._cut_child_scope(worktree, bare_remote, capsys) == expected_child_scope
            return
        assert capsys.readouterr().err == f"ERROR: {expected_err}\n"
        after = store.fetch_state(worktree=worktree, remote=remote_url)
        assert after.tip == before.tip

    def test_cut_adopts_the_child_after_a_partial_failure_from_a_competing_write(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #291 proof 3: a state-ref `cut` can never leave an orphan
        the way GitHub's two-write `create_child` can (`link_child` is a
        no-op, #283) -- but the row-removal write that follows it is a
        second, separate CAS write, and that one can still lose a race. This
        drives the real race directly: a competing write lands on the
        container between `create_child`'s own commit and the row-removal
        commit, so the row removal refuses with issue #279's own CAS
        sentence (via `forge.ForgePartialChildCreationError`) while the
        child stays created and already carries `record.parent`. An
        identical re-run finds that child through `list_children` (typed
        `record.parent`, never the `Parent:` prose line `_cut_child_body`
        would write for GitHub) and adopts it instead of minting a second
        one, then finishes the row removal against the now-current state."""
        item_files = _item_files_with_container_slices(((1, "Slice C"),))
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, item_files)
        remote_url = f"file://{bare_remote}"
        before_state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert before_state.tip is not None
        container_oid_before = before_state.items[CONTAINER_ID]
        before_ids = set(before_state.items)

        competing_write_done = {"done": False}
        original_write_item = issue_claim._StoreItemWriter.write_item

        def _write_item_then_race_the_container(
            self: issue_claim._StoreItemWriter,
            item_id: str,
            *,
            expected: protocol.ObjectId | None,
            content: bytes,
        ) -> protocol.ObjectId:
            result = original_write_item(self, item_id, expected=expected, content=content)
            if not competing_write_done["done"]:
                competing_write_done["done"] = True
                competing_data = {
                    **_CONTAINER_PROJECTION.block_data(),
                    "now": "Competing edit landed mid-cut.",
                    "slice": [{"index": 1, "title": "Slice C"}],
                    "record": _record(title="Epic", state="open", kind="container"),
                }
                competing_body = (
                    f"Prose.\n\n```agent-claim\n{render_block(competing_data)}```\n"
                ).encode()
                competing_intent = protocol.ItemWriteIntent(
                    item_id=CONTAINER_ID,
                    expected=container_oid_before,
                    new_oid=store.hash_blob(worktree, competing_body),
                    operation_id=uuid.uuid4().hex,
                )
                store.commit_transition(
                    worktree=worktree,
                    remote=remote_url,
                    subject=store.TransitionSubject("competing container write"),
                    intent=competing_intent,
                )
            return result

        monkeypatch.setattr(
            issue_claim._StoreItemWriter, "write_item", _write_item_then_race_the_container
        )

        first_status = issue_claim.main(["cut", str(CONTAINER_NUMBER), "--title", "Slice C"])

        assert first_status == 2
        after_first = store.fetch_state(worktree=worktree, remote=remote_url)
        assert after_first.tip is not None
        [child_id] = set(after_first.items) - before_ids
        child_number = items.item_number(child_id)
        child_record = _decoded_record(
            store.read_item_files(worktree, after_first.tip)[f"{child_id}.md"].decode(), child_id
        )
        assert child_record.parent == CONTAINER_ID
        err = capsys.readouterr().err
        assert (
            f"created #{child_number} but failed to remove row 1 "
            f"from #{CONTAINER_NUMBER}'s agent-claim block" in err
        )
        assert "re-run the same cut -- it adopts the child" in err
        assert "written since it was read" in err
        raced_container = store.read_item_files(worktree, after_first.tip)[
            f"{CONTAINER_ID}.md"
        ].decode()
        raced_data = locate_agent_claim_block(raced_container).data
        assert raced_data["now"] == "Competing edit landed mid-cut."
        assert raced_data["slice"] == [{"index": 1, "title": "Slice C"}]

        second_status = issue_claim.main(["cut", str(CONTAINER_NUMBER), "--title", "Slice C"])

        assert second_status == 0
        assert capsys.readouterr().out.strip() == (
            f"ADOPTED #{CONTAINER_NUMBER} row 1 -> #{child_number}"
        )
        after_second = store.fetch_state(worktree=worktree, remote=remote_url)
        assert after_second.tip is not None
        assert set(after_second.items) == set(after_first.items)
        assert after_second.items[child_id] == after_first.items[child_id]
        second_item_files = store.read_item_files(worktree, after_second.tip)
        first_item_files = store.read_item_files(worktree, after_first.tip)
        assert len(second_item_files) == len(first_item_files)
        final_container = second_item_files[f"{CONTAINER_ID}.md"].decode()
        assert locate_agent_claim_block(final_container).data["slice"] == []

    def test_cut_adopts_a_child_created_by_item_new_with_the_matching_title(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #291 proof 4: `aco item new --parent` writes `record.parent`
        straight into the fresh child's `[record]` table and no `Parent:`
        prose line at all (issue #285) -- unlike `_cut_child_body`'s own
        skeleton. `cut`'s adoption key is `record.parent` alone
        (`list_children`, never `_orphan_names_container`'s prose-line
        match), so it still adopts this child instead of minting a second
        one for the same slice row."""
        item_files = _item_files_with_container_slices(((1, "Slice C"),))
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, item_files)

        new_status = issue_claim.main(
            ["item", "new", "--title", "Slice C", "--parent", CONTAINER_ID]
        )
        assert new_status == 0
        created_id = capsys.readouterr().out.strip()
        created_number = items.item_number(created_id)

        status = issue_claim.main(["cut", str(CONTAINER_NUMBER), "--title", "Slice C"])

        assert status == 0
        assert capsys.readouterr().out.strip() == (
            f"ADOPTED #{CONTAINER_NUMBER} row 1 -> #{created_number}"
        )
        remote_url = f"file://{bare_remote}"
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        item_files_after = store.read_item_files(worktree, state.tip)
        assert {items.item_id_from_filename(name) for name in item_files_after} == {
            CONTAINER_ID,
            CHILD_A_ID,
            CHILD_B_ID,
            created_id,
        }
        container_body = item_files_after[f"{CONTAINER_ID}.md"].decode()
        assert locate_agent_claim_block(container_body).data["slice"] == []
        adopted_body = item_files_after[f"{created_id}.md"].decode()
        adopted_record = _decoded_record(adopted_body, created_id)
        assert adopted_record.parent == CONTAINER_ID
        assert "Parent:" not in adopted_body

    def test_cut_refuses_a_container_whose_body_is_malformed_before_any_write(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #291 proof 5: GitHub's own equivalent
        (`test_cut_refuses_a_blockless_container_before_any_write`) hits
        `_located_block_or_refuse`'s refusal only because a fetched GitHub
        issue can carry any body at all. A state-ref item cannot: every
        item's `[record]` table is validated once, at read time, by
        `StateRefBoard`'s own decode (issue #283) -- so a container without
        a working `[[slice]]` table (no agent-claim block to hold one) is
        refused earlier and louder, before `cut` -- before any command --
        ever reaches it, with the decode's own existing sentence rather than
        a second, cut-specific one. `next` never recommends `cut` on such a
        container for the same reason: it cannot exist on a live board.
        Nothing reaches the remote."""
        item_files = {**_item_files(), f"{CONTAINER_ID}.md": b"Just prose, no block at all.\n"}
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, item_files)
        remote_url = f"file://{bare_remote}"
        before = store.fetch_state(worktree=worktree, remote=remote_url)

        status = issue_claim.main(["cut", str(CONTAINER_NUMBER), "--title", "X"])

        assert status == 2
        assert capsys.readouterr().err == (
            f"ERROR: item {CONTAINER_ID} has a malformed agent-claim block\n"
        )
        after = store.fetch_state(worktree=worktree, remote=remote_url)
        assert after.tip == before.tip

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
        self._pin_state_ref(monkeypatch, tmp_path)
        monkeypatch.setenv("PATH", _path_without_gh(tmp_path))
        monkeypatch.chdir(worktree)

        status = issue_claim.main(["board", "--json"])

        assert status == 0
        assert json.loads(capsys.readouterr().out)["requests"] == 0

    def test_board_marks_a_state_ref_item_landed_by_a_trailer_carrying_trunk_commit(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #304: the trunk-derived landed set applies under
        `storage = state-ref` exactly as under GitHub --
        `checkout.trunk_landings` reads the real `main` history independent
        of which forge names the item, so a trailer-carrying commit lands a
        state-ref item with no merged pull request in the picture at all."""
        item_id = "aco-000005"
        item_number = items.item_number(item_id)
        item_files = {
            f"{item_id}.md": _state_ref_body(
                _Projection("Ship it.", "Land it.", "It is done."),
                _record(title="Trailer-landed", state="open", kind="task"),
            ).encode()
        }
        _git("commit", "--allow-empty", "-m", f"Land it.\n\nWork-Item: {item_id}", cwd=worktree)
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, item_files)

        assert issue_claim.main(["board", "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        item = next(row for row in payload["items"] if row["number"] == item_number)
        assert item["stage"] == "code-landed"

    def test_board_reads_two_trunk_trailer_landings_as_landing_rows_under_state_ref(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #371, Beweis 1: two trailer-carrying trunk commits each land
        their own item under `storage = state-ref` -- `board --json`'s
        `landings` array and `board --html`'s Landungen section both read
        `checkout.trunk_landings` directly, one row per item with its own
        sha and no `pull_request`, independent of any pull-request listing
        this storage can never perform."""
        first_id, second_id = "aco-000005", "aco-000006"
        first_number = items.item_number(first_id)
        second_number = items.item_number(second_id)
        item_files = {
            f"{item_id}.md": _state_ref_body(
                _Projection("Ship it.", "Land it.", "It is done."),
                _record(title=title, state="open", kind="task"),
            ).encode()
            for item_id, title in ((first_id, "First"), (second_id, "Second"))
        }
        _git("commit", "--allow-empty", "-m", f"Land it.\n\nWork-Item: {first_id}", cwd=worktree)
        first_sha = _head_sha(worktree)
        _git(
            "commit", "--allow-empty", "-m", f"Land it too.\n\nWork-Item: {second_id}", cwd=worktree
        )
        second_sha = _head_sha(worktree)
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, item_files)
        board_command = ["board"]

        assert issue_claim.main([*board_command, "--json"]) == 0
        payload = json.loads(capsys.readouterr().out)
        landings = {row["item"]: row for row in payload["landings"]}
        assert landings.keys() == {first_number, second_number}
        assert landings[first_number]["sha"] == first_sha
        assert landings[first_number]["pull_request"] is None
        assert landings[second_number]["sha"] == second_sha
        assert landings[second_number]["pull_request"] is None
        first_date = datetime.fromisoformat(landings[first_number]["committed_at"]).date()
        second_date = datetime.fromisoformat(landings[second_number]["committed_at"]).date()

        assert issue_claim.main([*board_command, "--html"]) == 0
        rendered_html = capsys.readouterr().out
        assert f"<li>{first_id} {first_date} <code>{first_sha[:7]}</code></li>" in rendered_html
        assert f"<li>{second_id} {second_date} <code>{second_sha[:7]}</code></li>" in rendered_html

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
        self._pin_state_ref(monkeypatch, tmp_path)
        monkeypatch.setenv("PATH", _path_without_gh(tmp_path))
        monkeypatch.chdir(worktree)

        status = issue_claim.main(["board", "--json"])

        assert status == 2
        assert capsys.readouterr().err == (
            "ERROR: cannot resolve the default branch; "
            "run aco from a checkout with origin/HEAD set\n"
        )

    def test_item_new_creates_a_task_and_a_fresh_board_shows_it_open(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #285 proof 1: `aco item new --title X` writes a fresh task
        skeleton plus `[record]` straight into `items/<id>.md` and prints
        exactly the minted id; a second `aco board` invocation (its own
        fresh fetch, standing in for a second process) shows the new item
        open."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())

        status = issue_claim.main(["item", "new", "--title", "Fresh Item"])

        assert status == 0
        printed = capsys.readouterr().out.strip()
        assert items.ITEM_ID_PATTERN.fullmatch(printed)

        remote_url = f"file://{bare_remote}"
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        stored = store.read_item_files(worktree, state.tip)[f"{printed}.md"].decode()
        record = _decoded_record(stored, printed)
        assert record.title == "Fresh Item"
        assert record.kind == "task"
        assert record.parent is None
        assert record.state is items.RecordState.OPEN

        board_status = issue_claim.main(["board", "--json"])
        assert board_status == 0
        titles = {item["title"] for item in json.loads(capsys.readouterr().out)["items"]}
        assert "Fresh Item" in titles

    def test_item_new_scope_writes_the_field_canonically(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #337 proof 1: repeated `--scope` flags write the block's
        own top-level `scope = [...]`, canonicalized (sorted, deduplicated)
        by the same `protocol.valid_scope` a live claim's own scope passes
        through -- never a second grammar."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())

        status = issue_claim.main(
            ["item", "new", "--title", "Scoped Item", "--scope", "src/b.py", "--scope", "src/a.py"]
        )

        assert status == 0
        printed = capsys.readouterr().out.strip()
        remote_url = f"file://{bare_remote}"
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        stored = store.read_item_files(worktree, state.tip)[f"{printed}.md"].decode()
        assert locate_agent_claim_block(stored).data["scope"] == ["src/a.py", "src/b.py"]

    def test_item_new_size_writes_the_top_level_field(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #357 proof 2: `item new --size M` writes the block's own
        top-level `size = "M"` -- a plain block field, never nested under
        `[record]` (a `state-ref`-only table BODY-15 refuses under
        `github`), so the same write reaches a `github`-stored item too."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())

        status = issue_claim.main(["item", "new", "--title", "Sized Item", "--size", "M"])

        assert status == 0
        printed = capsys.readouterr().out.strip()
        remote_url = f"file://{bare_remote}"
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        stored = store.read_item_files(worktree, state.tip)[f"{printed}.md"].decode()
        assert locate_agent_claim_block(stored).data["size"] == "M"

    def test_item_new_whole_writes_the_top_level_field(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #399: `item new --whole REASON` writes the block's own
        top-level `whole = "REASON"`, mirroring
        `test_item_new_size_writes_the_top_level_field`."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())
        reason = "one lane owns every adapter"

        status = issue_claim.main(["item", "new", "--title", "Whole Item", "--whole", reason])

        assert status == 0
        printed = capsys.readouterr().out.strip()
        remote_url = f"file://{bare_remote}"
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        stored = store.read_item_files(worktree, state.tip)[f"{printed}.md"].decode()
        assert locate_agent_claim_block(stored).data["whole"] == reason

    def test_item_new_size_refuses_an_invalid_value_before_any_write(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())

        with pytest.raises(SystemExit) as exited:
            issue_claim.main(["item", "new", "--title", "Bad Size", "--size", "XL"])

        assert exited.value.code == 2
        assert "invalid choice" in capsys.readouterr().err
        remote_url = f"file://{bare_remote}"
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        assert set(store.read_item_files(worktree, state.tip)) == set(_item_files())

    def test_item_new_kind_container_writes_the_container_skeleton(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #285 proof 2 (kind): `--kind container` writes the
        container skeleton (a `Blocked by:` line ahead of the block) and
        `kind = "container"` in the record."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())

        status = issue_claim.main(
            ["item", "new", "--title", "Fresh Epic", "--kind", "container", "--json"]
        )

        assert status == 0
        payload = json.loads(capsys.readouterr().out)
        printed = payload["item"]
        assert payload["number"] == items.item_number(printed)

        remote_url = f"file://{bare_remote}"
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        stored = store.read_item_files(worktree, state.tip)[f"{printed}.md"].decode()
        assert stored.startswith("Blocked by: nichts")
        record = _decoded_record(stored, printed)
        assert record.kind == "container"

    def test_item_new_with_parent_sets_the_record_and_a_fresh_board_shows_the_child(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #285 proof 2 (parent): `--parent aco-…` sets `record.parent`,
        and a fresh `aco board --json` shows the new child under the
        container."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())

        status = issue_claim.main(
            ["item", "new", "--title", "Fresh Child", "--parent", CONTAINER_ID]
        )

        assert status == 0
        printed = capsys.readouterr().out.strip()

        remote_url = f"file://{bare_remote}"
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        stored = store.read_item_files(worktree, state.tip)[f"{printed}.md"].decode()
        record = _decoded_record(stored, printed)
        assert record.parent == CONTAINER_ID

        board_status = issue_claim.main(["board", "--json"])
        assert board_status == 0
        payload = json.loads(capsys.readouterr().out)
        child = next(item for item in payload["items"] if item["title"] == "Fresh Child")
        assert child["container_parent"] == CONTAINER_NUMBER

    def test_item_new_refuses_an_unknown_parent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())

        status = issue_claim.main(["item", "new", "--title", "Orphan", "--parent", "aco-abcdef"])

        assert status == 2
        parent_number = items.item_number("aco-abcdef")
        assert capsys.readouterr().err == f"ERROR: #{parent_number} does not exist\n"

    @pytest.mark.parametrize(
        ("cli_args", "expected_err_substring", "patch_minting_collision"),
        [
            pytest.param(
                ["item", "new", "--title", "Collides"],
                "could not mint a fresh item id",
                True,
                id="three-minting-collisions",
            ),
            pytest.param(
                ["item", "new", "--title", "Bound to nothing", "--origin", "not-an-origin"],
                "is not an origin",
                False,
                id="malformed-origin",
            ),
            pytest.param(
                ["item", "new", "--title", "Escapes", "--scope", "../outside"],
                "claim scope must be repository-relative",
                False,
                id="scope-escapes-the-repository",
            ),
        ],
    )
    def test_item_new_refuses_and_writes_nothing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
        cli_args: list[str],
        expected_err_substring: str,
        patch_minting_collision: bool,
    ) -> None:
        """Issue #285 proof 3 / issue #316 proof 2 / issue #337 proof 1:
        `item new` refuses -- on an injected minting collision against every
        already-known id after three attempts, on an `--origin` that does
        not match `FORGE#N` (refused before `argparse` even reaches `item
        new`'s own body), or on a `--scope` value `protocol.valid_scope`
        refuses -- with a diagnostic naming the cause, and nothing reaches
        the remote."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())
        if patch_minting_collision:
            monkeypatch.setattr(items.secrets, "token_hex", lambda _size: "000001")
        remote_url = f"file://{bare_remote}"
        before = store.fetch_state(worktree=worktree, remote=remote_url)

        status = issue_claim.main(cli_args)

        assert status == 2
        assert expected_err_substring in capsys.readouterr().err
        after = store.fetch_state(worktree=worktree, remote=remote_url)
        assert after.tip == before.tip

    def test_item_new_with_origin_writes_the_record_and_a_fresh_show_prints_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #316 proof 1/2: `--origin gitlab#514` writes `record.origin`
        straight into `items/<id>.md` over a real `file://` remote through
        `main`, and a fresh `item show` (its own fetch, standing in for a
        second process) prints it in the header; `claim` then runs on that
        same item exactly as on any other (proof: `aco-xxxxxx` form, a live
        claim record)."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())

        status = issue_claim.main(
            ["item", "new", "--title", "Bound to GitLab", "--origin", "gitlab#514"]
        )

        assert status == 0
        printed = capsys.readouterr().out.strip()
        assert items.ITEM_ID_PATTERN.fullmatch(printed)

        remote_url = f"file://{bare_remote}"
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        stored = store.read_item_files(worktree, state.tip)[f"{printed}.md"].decode()
        assert _decoded_record(stored, printed).origin == "gitlab#514"

        shown = issue_claim.main(["item", "show", printed])
        assert shown == 0
        header_line = capsys.readouterr().out.splitlines()[0]
        assert header_line.endswith("· origin gitlab#514")

        shown_json = issue_claim.main(["item", "show", printed, "--json"])
        assert shown_json == 0
        assert json.loads(capsys.readouterr().out)["origin"] == "gitlab#514"

        filled_body = _github_body(_Projection("Build it.", "Ship it.", "It ships."))
        monkeypatch.setattr(sys, "stdin", io.StringIO(filled_body))
        edited = issue_claim.main(["item", "edit", printed])
        assert edited == 0
        capsys.readouterr()

        monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
        monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
        monkeypatch.setattr(checkout, "versioned_paths", lambda **_kwargs: ("README",))
        claimed = issue_claim.main(
            [
                "claim",
                printed,
                "--agent",
                "Codex Sol",
                "--role",
                "builder",
                "--base",
                "a" * 40,
                "--branch",
                f"codex/issue-{items.item_number(printed)}-bound-to-gitlab",
                "--scope",
                "README",
                "--claim-id",
                "origin-claim",
                "--out-of-order",
                "proving claim works on an item carrying an origin",
            ]
        )
        assert claimed == 0

    def test_item_show_prints_the_header_and_body_byte_exact_for_a_closed_child(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #285 proof 4: the header names id, number, state, and
        parent; a closed item is shown exactly like an open one (closing
        never deletes it); the body is printed byte-exact."""
        closed_id = "aco-000005"
        closed_number = items.item_number(closed_id)
        closed_record = _record(
            title="Shipped",
            state="closed",
            kind="task",
            parent=CONTAINER_ID,
            closed_at="2026-09-15T00:00:00Z",
        )
        closed_body = _state_ref_body(_Projection("Done.", "keiner", "Shipped."), closed_record)
        item_files = {**_item_files(), f"{closed_id}.md": closed_body.encode()}
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, item_files)

        status = issue_claim.main(["item", "show", closed_id])

        assert status == 0
        out = capsys.readouterr().out
        header_line = (
            f"{closed_id} · #{closed_number} · closed · parent {CONTAINER_ID} · origin none"
        )
        assert out == f"{header_line}\n{closed_body}"

    def test_item_show_refuses_an_unknown_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())

        status = issue_claim.main(["item", "show", "aco-abcdef"])

        assert status == 2
        assert "does not exist" in capsys.readouterr().err

    def test_claim_rule_and_check_accept_the_aco_id_form_under_the_pin(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #285 proof 5: `claim`, `rule`, and `check` all run straight
        through to the same state-ref item when given the `aco-xxxxxx`
        reference form, not only the bare number `#n`/`n` every other test
        in this class already exercises."""
        item_files = {**_item_files(), **_rulable_item_files()}
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, item_files)
        monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
        monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
        monkeypatch.setattr(checkout, "versioned_paths", lambda **_kwargs: ("README",))

        claimed = issue_claim.main(
            [
                "claim",
                CHILD_A_ID,
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

        checked = issue_claim.main(["check", CHILD_A_ID])
        assert checked == 0
        assert "body ok" in capsys.readouterr().out

        ruled = issue_claim.main(["rule", RULABLE_ID, "--line", "1", "--yes"])
        assert ruled == 0

    def test_item_edit_replaces_the_body_and_a_fresh_process_reads_it_back(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #287 proof 1: `aco item edit` replaces a state-ref item's
        body from stdin, byte-exact outside `[record]`, and a fresh `aco
        item show` (its own fetch, standing in for a second process) reads
        it back; `updated_at` moves to today, `created_at`/`parent`/`state`
        stay this item's own stored values."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())
        shown = issue_claim.main(["item", "show", str(CHILD_A_NUMBER), "--json"])
        assert shown == 0
        stored_body = json.loads(capsys.readouterr().out)["body"]
        before_record = _decoded_record(stored_body, CHILD_A_ID)
        edited_body = stored_body.replace("Prose.", "Edited prose.", 1)
        monkeypatch.setattr(sys, "stdin", io.StringIO(edited_body))

        edited = issue_claim.main(["item", "edit", str(CHILD_A_NUMBER)])

        assert edited == 0
        assert capsys.readouterr().out.strip() == f"EDITED {CHILD_A_ID}"
        fresh = issue_claim.main(["item", "show", str(CHILD_A_NUMBER), "--json"])
        assert fresh == 0
        after_body = json.loads(capsys.readouterr().out)["body"]
        assert after_body.split("```agent-claim", 1)[0] == edited_body.split("```agent-claim", 1)[0]
        after_record = _decoded_record(after_body, CHILD_A_ID)
        assert after_record.created_at == before_record.created_at
        assert after_record.parent == before_record.parent
        assert after_record.state == before_record.state
        assert after_record.updated_at != before_record.updated_at
        assert after_record.updated_at.startswith(datetime.now(UTC).date().isoformat())

    def test_item_edit_size_writes_only_the_top_level_field(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #357 proof 2: `item edit --size L` writes only the block's
        own top-level `size = "L"`, no stdin read, every other byte
        (including `[record]`) untouched -- unlike the whole-body `item
        edit` above."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())
        shown = issue_claim.main(["item", "show", str(CHILD_A_NUMBER), "--json"])
        assert shown == 0
        before_body = json.loads(capsys.readouterr().out)["body"]

        edited = issue_claim.main(["item", "edit", str(CHILD_A_NUMBER), "--size", "L"])

        assert edited == 0
        assert capsys.readouterr().out.strip() == f"EDITED #{CHILD_A_NUMBER} size=L"
        fresh = issue_claim.main(["item", "show", str(CHILD_A_NUMBER), "--json"])
        assert fresh == 0
        after_body = json.loads(capsys.readouterr().out)["body"]
        assert locate_agent_claim_block(after_body).data["size"] == "L"
        before_record = _decoded_record(before_body, CHILD_A_ID)
        after_record = _decoded_record(after_body, CHILD_A_ID)
        assert replace(after_record, updated_at=before_record.updated_at) == before_record

    def test_item_edit_whole_writes_only_the_top_level_field(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #399: `item edit --whole REASON` writes only the block's
        own top-level `whole = "REASON"`, no stdin read, every other byte
        (including `[record]`) untouched, mirroring
        `test_item_edit_size_writes_only_the_top_level_field`."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())
        reason = "the four adapters share one lock"
        shown = issue_claim.main(["item", "show", str(CHILD_A_NUMBER), "--json"])
        assert shown == 0
        before_body = json.loads(capsys.readouterr().out)["body"]

        edited = issue_claim.main(["item", "edit", str(CHILD_A_NUMBER), "--whole", reason])

        assert edited == 0
        assert capsys.readouterr().out.strip() == f"EDITED #{CHILD_A_NUMBER} whole={reason}"
        fresh = issue_claim.main(["item", "show", str(CHILD_A_NUMBER), "--json"])
        assert fresh == 0
        after_body = json.loads(capsys.readouterr().out)["body"]
        assert locate_agent_claim_block(after_body).data["whole"] == reason
        before_record = _decoded_record(before_body, CHILD_A_ID)
        after_record = _decoded_record(after_body, CHILD_A_ID)
        assert replace(after_record, updated_at=before_record.updated_at) == before_record

    def test_item_edit_json_prints_the_item_number_and_fresh_oid(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """`aco item edit --json` prints `{item, number, oid}` -- the
        freshly written blob's own oid, straight off `StateRefBoard.item_oid`,
        never a re-read."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())
        shown = issue_claim.main(["item", "show", str(CHILD_A_NUMBER), "--json"])
        assert shown == 0
        stored_body = json.loads(capsys.readouterr().out)["body"]
        monkeypatch.setattr(sys, "stdin", io.StringIO(stored_body.replace("Prose.", "Edited.", 1)))

        edited = issue_claim.main(["item", "edit", str(CHILD_A_NUMBER), "--json"])

        assert edited == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["item"] == CHILD_A_ID
        assert payload["number"] == CHILD_A_NUMBER
        remote_url = f"file://{bare_remote}"
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        assert payload["oid"] == state.items[CHILD_A_ID]

    def test_item_edit_refuses_an_unknown_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())
        monkeypatch.setattr(sys, "stdin", io.StringIO(_github_body(_CHILD_A_PROJECTION)))

        status = issue_claim.main(["item", "edit", "aco-abcdef"])

        assert status == 2
        assert "does not exist" in capsys.readouterr().err

    def test_item_edit_takes_title_labels_blocked_by_and_keeps_aco_owned_record_fields(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #287 proof 2: a piped `[record]` naming a foreign `parent`,
        `state = "closed"`, and a different `created_at` is silently
        overwritten by this item's own stored values; the piped `title`,
        `labels`, and `blocked_by` are taken as given."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())
        remote_url = f"file://{bare_remote}"
        before_state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert before_state.tip is not None
        before = _decoded_record(
            store.read_item_files(worktree, before_state.tip)[f"{CHILD_B_ID}.md"].decode(),
            CHILD_B_ID,
        )
        hostile_record = _record(
            title="Renamed via edit",
            state="closed",
            kind="task",
            parent=CHILD_A_ID,
            labels=("urgent",),
            blocked_by=(),
            closed_at="2020-01-01T00:00:00Z",
        )
        hostile_record["created_at"] = "2020-01-01T00:00:00Z"
        delivered_body = _state_ref_body(_CHILD_B_PROJECTION, hostile_record)
        monkeypatch.setattr(sys, "stdin", io.StringIO(delivered_body))

        edited = issue_claim.main(["item", "edit", str(CHILD_B_NUMBER)])

        assert edited == 0
        capsys.readouterr()
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        after = _decoded_record(
            store.read_item_files(worktree, state.tip)[f"{CHILD_B_ID}.md"].decode(), CHILD_B_ID
        )
        assert after.title == "Renamed via edit"
        assert after.labels == ("urgent",)
        assert after.blocked_by == ()
        assert after.state == before.state
        assert after.parent == before.parent
        assert after.created_at == before.created_at

    def test_item_edit_sets_blocked_by_and_aco_next_skips_then_frees_it(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #287 proof 3: `blocked_by` set through `aco item edit`
        makes `aco next` skip the item as blocked; removing it again
        through a second `item edit` frees it."""
        self._live_state_ref_checkout(
            monkeypatch, tmp_path, bare_remote, worktree, _edit_target_item_files()
        )
        blocked_body = _state_ref_body(
            _EDIT_TARGET_PROJECTION,
            _record(title="Target", state="open", kind="task", blocked_by=(EDIT_BLOCKER_ID,)),
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO(blocked_body))
        assert issue_claim.main(["item", "edit", str(EDIT_TARGET_NUMBER)]) == 0
        capsys.readouterr()

        assert issue_claim.main(["next"]) == 0
        blocked_out = capsys.readouterr().out
        # The item's own prefix and the blocker it names inside the reason
        # both print the state-ref id (issue #292, #300 residual 2:
        # `open_blocker_label` now takes `storage`) -- never `#n`.
        assert f"{EDIT_TARGET_ID}: blocked by {EDIT_BLOCKER_ID}" in blocked_out
        assert f"#{EDIT_BLOCKER_NUMBER}" not in blocked_out

        freed_body = _state_ref_body(
            _EDIT_TARGET_PROJECTION, _record(title="Target", state="open", kind="task")
        )
        monkeypatch.setattr(sys, "stdin", io.StringIO(freed_body))
        assert issue_claim.main(["item", "edit", str(EDIT_TARGET_NUMBER)]) == 0
        capsys.readouterr()

        assert issue_claim.main(["next"]) == 0
        freed_out = capsys.readouterr().out
        assert f"{EDIT_TARGET_ID}: blocked by" not in freed_out

    def test_item_edit_two_processes_from_the_same_snapshot_the_second_refuses(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #287 proof 4: two worktrees read the same item oid -- the
        first `aco item edit` (this CLI's own write) lands, and a second
        writer still holding that now-stale oid (`_fetch_state_ref_board`'s
        own read, the same technique issue #283's own CAS test uses to
        stand in for an independent process) refuses with issue #279's own
        sentence; the remote keeps the first writer's body."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())
        second = _fetch_state_ref_board(
            bare_remote, worktree, writer=issue_claim._StoreItemWriter(worktree, str(bare_remote))
        )
        second_body = second.item_reference(CHILD_A_NUMBER).body
        assert second_body is not None

        shown = issue_claim.main(["item", "show", str(CHILD_A_NUMBER), "--json"])
        assert shown == 0
        stored_body = json.loads(capsys.readouterr().out)["body"]
        first_body = stored_body.replace("Prose.", "First writer.", 1)
        monkeypatch.setattr(sys, "stdin", io.StringIO(first_body))

        edited = issue_claim.main(["item", "edit", str(CHILD_A_NUMBER)])
        assert edited == 0
        capsys.readouterr()

        second_writer_body = second_body.replace("Prose.", "Second writer.", 1)
        with pytest.raises(ClaimUnavailableError, match="written since it was read"):
            second.update_item_body(CHILD_A_NUMBER, second_writer_body)

        remote_url = f"file://{bare_remote}"
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        stored = store.read_item_files(worktree, state.tip)[f"{CHILD_A_ID}.md"]
        assert stored.startswith(b"First writer.")

    def test_item_edit_refuses_a_body_with_no_block_before_any_write(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #287 proof 5: a piped body with no recognized `agent-claim`
        block refuses with `body --check`'s own sentence, before any write
        -- the remote's tip stays exactly what it was."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, _item_files())
        remote_url = f"file://{bare_remote}"
        before = store.fetch_state(worktree=worktree, remote=remote_url)
        monkeypatch.setattr(sys, "stdin", io.StringIO("no block\n"))

        status = issue_claim.main(["item", "edit", str(CHILD_A_NUMBER)])

        assert status == 2
        assert capsys.readouterr().err == (
            "ERROR: body malformed: agent-claim: no agent-claim block\n"
        )
        after = store.fetch_state(worktree=worktree, remote=remote_url)
        assert after.tip == before.tip

    def test_item_new_refuses_a_title_twinning_a_just_closed_item(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #444: the twin search reads state-ref items too -- an item
        closed a moment ago still twins a fresh `item new` of its title,
        leaving the remote's tip untouched; `--not-a-twin` creates anyway."""
        self._live_state_ref_checkout(
            monkeypatch, tmp_path, bare_remote, worktree, _close_scenario_item_files()
        )
        assert issue_claim.main(["item", "close", str(CLOSE_BLOCKER_NUMBER)]) == 0
        capsys.readouterr()
        remote_url = f"file://{bare_remote}"
        before = store.fetch_state(worktree=worktree, remote=remote_url)

        refused = issue_claim.main(["item", "new", "--title", "blocker"])

        assert (refused, capsys.readouterr().err) == (
            2,
            f"ERROR: possible twin #{CLOSE_BLOCKER_NUMBER}; pass --not-a-twin\n",
        )
        assert store.fetch_state(worktree=worktree, remote=remote_url).tip == before.tip
        assert issue_claim.main(["item", "new", "--title", "blocker", "--not-a-twin"]) == 0

    def test_item_close_sets_state_and_closed_at_leaves_board_and_next_and_names_the_freed_item(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #289 proofs 1-2: `aco item close` sets `state = "closed"`
        and `closed_at` (today) in the record, the item file stays and a
        fresh `item show` (its own fetch) reads it back closed; the closed
        item leaves `board` and `next`, and `TARGET`, the item it alone
        blocked, is freed -- named in `close`'s own `freed:` line and no
        longer reported blocked by a fresh `next`."""
        self._live_state_ref_checkout(
            monkeypatch, tmp_path, bare_remote, worktree, _close_scenario_item_files()
        )

        status = issue_claim.main(["item", "close", str(CLOSE_BLOCKER_NUMBER)])

        assert status == 0
        assert capsys.readouterr().out.splitlines() == [
            f"CLOSED {CLOSE_BLOCKER_ID}",
            f"freed: {CLOSE_TARGET_ID}",
        ]

        shown = issue_claim.main(["item", "show", str(CLOSE_BLOCKER_NUMBER), "--json"])
        assert shown == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["state"] == "closed"
        record = _decoded_record(payload["body"], CLOSE_BLOCKER_ID)
        assert record.state is items.RecordState.CLOSED
        assert record.closed_at is not None
        assert record.closed_at.startswith(datetime.now(UTC).date().isoformat())
        assert record.updated_at == record.closed_at

        board_status = issue_claim.main(["board", "--json"])
        assert board_status == 0
        payload = json.loads(capsys.readouterr().out)
        assert CLOSE_BLOCKER_NUMBER not in {item["number"] for item in payload["items"]}
        assert CLOSE_TARGET_NUMBER in {item["number"] for item in payload["items"]}

        next_status = issue_claim.main(["next"])
        assert next_status == 0
        next_out = capsys.readouterr().out
        assert CLOSE_TARGET_ID in next_out
        assert f"#{CLOSE_TARGET_NUMBER}" not in next_out
        assert "blocked by" not in next_out

    def test_item_close_prints_the_parent_hint_for_the_last_open_child(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #348, Beweis 4: closing a container's only open child
        prints the same parent hint `release --merged` prints, naming the
        container `item close`'s own way (`freed:`'s own id form)."""
        self._live_state_ref_checkout(
            monkeypatch, tmp_path, bare_remote, worktree, _close_parent_scenario_item_files()
        )

        status = issue_claim.main(["item", "close", str(CLOSE_CHILD_NUMBER)])

        assert status == 0
        assert capsys.readouterr().out.splitlines() == [
            f"CLOSED {CLOSE_CHILD_ID}",
            "freed: none",
            f"parent {CLOSE_PARENT_ID}: no open children — close it",
        ]

    def test_item_close_omits_the_parent_hint_for_an_already_closed_parent(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #348 review (G2): a parent already closed by some other
        landing before this close even runs is never named freshly
        closable -- a childless, uncut container that is not open must not
        surface the hint, since a second close would only refuse. The
        parent's own close runs through the real command, not a seeded
        `state = "closed"` record -- the same command-path proof every
        other closed-item scenario in this module gives."""
        self._live_state_ref_checkout(
            monkeypatch, tmp_path, bare_remote, worktree, _close_parent_scenario_item_files()
        )
        assert issue_claim.main(["item", "close", str(CLOSE_PARENT_NUMBER)]) == 0
        capsys.readouterr()

        status = issue_claim.main(["item", "close", str(CLOSE_CHILD_NUMBER)])

        assert status == 0
        assert capsys.readouterr().out.splitlines() == [
            f"CLOSED {CLOSE_CHILD_ID}",
            "freed: none",
        ]

    def test_item_close_json_carries_the_parent_closable_number(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #348, Beweis 4 (JSON): `parent_closable` carries the same
        number the text form's parent hint names."""
        self._live_state_ref_checkout(
            monkeypatch, tmp_path, bare_remote, worktree, _close_parent_scenario_item_files()
        )

        status = issue_claim.main(["item", "close", str(CLOSE_CHILD_NUMBER), "--json"])

        assert status == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["parent_closable"] == CLOSE_PARENT_NUMBER

    def test_item_close_refuses_a_second_close_with_the_closed_date_and_leaves_the_oid_unchanged(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #289 proof 3: a second `item close` on an already-closed
        item refuses, naming the date it closed on, without writing --
        the remote's item oid stays exactly what the first close left."""
        self._live_state_ref_checkout(
            monkeypatch, tmp_path, bare_remote, worktree, _close_scenario_item_files()
        )
        assert issue_claim.main(["item", "close", str(CLOSE_BLOCKER_NUMBER)]) == 0
        capsys.readouterr()
        remote_url = f"file://{bare_remote}"
        state_after_first_close = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state_after_first_close.tip is not None
        oid_after_first_close = state_after_first_close.items[CLOSE_BLOCKER_ID]

        status = issue_claim.main(["item", "close", str(CLOSE_BLOCKER_NUMBER)])

        assert status == 2
        assert "is already closed (closed on" in capsys.readouterr().err
        state_after_second_close = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state_after_second_close.tip == state_after_first_close.tip
        assert state_after_second_close.items[CLOSE_BLOCKER_ID] == oid_after_first_close

    def test_item_close_refuses_a_live_claim_then_succeeds_after_release_abandoned(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #289 proof 4: an item with a live claim refuses `close` --
        release the claim first -- a closed item with a live claim still on
        it would be the `RECOVERY` anomaly the board already guards
        against. `release --abandoned` frees it, and `close` then
        succeeds."""
        self._live_state_ref_checkout(
            monkeypatch, tmp_path, bare_remote, worktree, _close_scenario_item_files()
        )
        monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
        monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
        monkeypatch.setattr(checkout, "versioned_paths", lambda **_kwargs: ("README",))
        claimed = issue_claim.main(
            [
                "claim",
                str(CLOSE_BLOCKER_NUMBER),
                "--agent",
                "Codex Sol",
                "--role",
                "builder",
                "--base",
                "a" * 40,
                "--branch",
                f"codex/issue-{CLOSE_BLOCKER_NUMBER}-close-target",
                "--scope",
                "README",
                "--claim-id",
                "state-ref-claim",
            ]
        )
        assert claimed == 0
        capsys.readouterr()

        refused = issue_claim.main(["item", "close", str(CLOSE_BLOCKER_NUMBER)])
        assert refused == 2
        assert "release the claim first" in capsys.readouterr().err

        released = issue_claim.main(
            [
                "release",
                str(CLOSE_BLOCKER_NUMBER),
                "--agent",
                "Codex Sol",
                "--claim-id",
                "state-ref-claim",
                "--abandoned",
                "stopped",
            ]
        )
        assert released == 0
        capsys.readouterr()

        status = issue_claim.main(["item", "close", str(CLOSE_BLOCKER_NUMBER)])
        assert status == 0
        assert capsys.readouterr().out.splitlines()[0] == f"CLOSED {CLOSE_BLOCKER_ID}"

    def test_item_close_two_processes_from_the_same_snapshot_the_second_refuses(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #289 proof 5: two worktrees read the same item oid -- the
        first `aco item close` (this CLI's own write) lands, and a second
        writer still holding that now-stale oid (`_fetch_state_ref_board`'s
        own read, the same technique issue #283's own CAS test uses to
        stand in for an independent process) refuses with issue #279's own
        sentence; the remote keeps the first close's record."""
        self._live_state_ref_checkout(
            monkeypatch, tmp_path, bare_remote, worktree, _close_scenario_item_files()
        )
        second = _fetch_state_ref_board(
            bare_remote, worktree, writer=issue_claim._StoreItemWriter(worktree, str(bare_remote))
        )

        closed = issue_claim.main(["item", "close", str(CLOSE_BLOCKER_NUMBER)])
        assert closed == 0
        capsys.readouterr()

        with pytest.raises(ClaimUnavailableError, match="written since it was read"):
            second.close_item(CLOSE_BLOCKER_NUMBER)

        remote_url = f"file://{bare_remote}"
        state = store.fetch_state(worktree=worktree, remote=remote_url)
        assert state.tip is not None
        stored = store.read_item_files(worktree, state.tip)[f"{CLOSE_BLOCKER_ID}.md"].decode()
        assert _decoded_record(stored, CLOSE_BLOCKER_ID).state is items.RecordState.CLOSED

    def test_item_close_refuses_an_unknown_id(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        self._live_state_ref_checkout(
            monkeypatch, tmp_path, bare_remote, worktree, _close_scenario_item_files()
        )

        status = issue_claim.main(["item", "close", "aco-abcdef"])

        assert status == 2
        assert "does not exist" in capsys.readouterr().err

    def test_readme_week_without_a_forge_runs_end_to_end_against_a_fresh_bare_remote(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
        tmp_path: Path,
        bare_remote: Path,
        worktree: Path,
    ) -> None:
        """Issue #292 proof 4: the README's "A week without a forge" runs
        end to end, one `aco` invocation per README sentence, against a
        fresh bare `file://` remote carrying no items yet -- bootstrap, cut
        the epic and its first child, the daily claim/release/close loop,
        and an expectation ruled -- each step asserted by the exact
        sentence README says it prints."""
        self._live_state_ref_checkout(monkeypatch, tmp_path, bare_remote, worktree, {})
        monkeypatch.setattr(checkout, "_validate_checkout", lambda request, directory=None: None)
        monkeypatch.setattr(checkout, "_scope_directories", lambda paths, **_kwargs: ())
        monkeypatch.setattr(checkout, "versioned_paths", lambda **_kwargs: ("README",))

        bootstrap_out = _run_ok(["bootstrap"], capsys).strip()
        assert protocol.COMMIT_PATTERN.fullmatch(bootstrap_out)

        container_id = _run_ok(
            ["item", "new", "--kind", "container", "--title", "A week without a forge"], capsys
        ).strip()
        assert items.ITEM_ID_PATTERN.fullmatch(container_id)
        container_number = items.item_number(container_id)

        container_body = _filled_body(
            BLOCK_CONTAINER_SKELETON,
            now="Land every slice.",
            next_step="Cut the first slice.",
            done_when="Both slices are closed.",
        )

        monkeypatch.setattr(sys, "stdin", io.StringIO(container_body))
        assert _run_ok(["body", "--check"], capsys) == "body ok\n"

        monkeypatch.setattr(sys, "stdin", io.StringIO(container_body))
        assert _run_ok(["item", "edit", container_id], capsys) == f"EDITED {container_id}\n"

        child_id = _run_ok(
            ["item", "new", "--title", "Ship slice one", "--parent", container_id], capsys
        ).strip()
        assert items.ITEM_ID_PATTERN.fullmatch(child_id)
        child_number = items.item_number(child_id)

        child_body = _filled_body(
            BLOCK_CHILD_SKELETON,
            now="Build slice one.",
            next_step="Ship slice one.",
            done_when="Slice one is merged.",
        )

        monkeypatch.setattr(sys, "stdin", io.StringIO(child_body))
        assert _run_ok(["body", "--check"], capsys) == "body ok\n"

        monkeypatch.setattr(sys, "stdin", io.StringIO(child_body))
        assert _run_ok(["item", "edit", child_id], capsys) == f"EDITED {child_id}\n"

        board_payload = json.loads(_run_ok(["board", "--json"], capsys))
        assert "Ship slice one" in {item["title"] for item in board_payload["items"]}

        next_out = _run_ok(["next"], capsys)
        assert child_id in next_out
        assert f"#{child_number}" not in next_out

        claim_out = _run_ok(
            [
                "claim",
                child_id,
                "--agent",
                "Codex Sol",
                "--role",
                "builder",
                "--base",
                "a" * 40,
                "--branch",
                f"codex/issue-{child_number}-slice-one",
                "--scope",
                "README",
                "--claim-id",
                "state-ref-claim",
            ],
            capsys,
        )
        # `claim`'s own success line now prints the state-ref id too
        # (issue #300 residual 1: `_claim_subject` takes `config.storage`).
        assert claim_out.splitlines()[0] == f"CLAIMED issue {child_id}: state-ref-claim"

        assert f"CLAIMED issue {child_id}" in _run_ok(["status", child_id], capsys)

        assert "release the claim first" in _run_refused(["item", "close", child_id], capsys)

        release_out = _run_ok(
            [
                "release",
                child_id,
                "--agent",
                "Codex Sol",
                "--claim-id",
                "state-ref-claim",
                "--abandoned",
                "landed as 1234567890123456789012345678901234567890",
            ],
            capsys,
        )
        # `release`'s own `RELEASED ...` line prints the state-ref id too
        # (issue #300 residual 1); an abandoned release prints neither
        # `freed:` nor `next:`.
        assert release_out.strip() == f"RELEASED issue {child_id}: state-ref-claim"

        assert _run_ok(["status", child_id], capsys).strip() == f"UNCLAIMED issue {child_id}"

        close_out = _run_ok(["item", "close", child_id], capsys)
        # `child_id` was `container_id`'s only child and the container's own
        # block carries no `[[slice]]` row, so this close leaves it freshly
        # closable (issue #348) -- named by the same parent hint `release
        # --merged` prints.
        assert close_out.splitlines() == [
            f"CLOSED {child_id}",
            "freed: none",
            f"parent {container_id}: no open children — close it",
        ]

        asked_text = "Does the runbook still hold without a forge?"
        asked_out = _run_ok(["ask", container_id, "--text", asked_text], capsys)
        assert asked_out.strip() == f"ASKED #{container_number} line 1: {asked_text}"

        ruled_out = _run_ok(["rule", container_id, "--line", "1", "--yes"], capsys)
        assert ruled_out.strip() == f"RULED #{container_number} line 1 yes; 0 line(s) still open"

        # `container_id`'s only expectation line is now ruled, so `rulings`
        # lists the item fully ruled rather than printing the empty
        # sentence (issue #379). Read `ruled_on` back from the CLI's own
        # `--json` line rather than the wall clock, so the assertion cannot
        # flake across a UTC midnight between the `rule` call above and here.
        assert _run_ok(["rulings"], capsys).strip() == (
            f"{container_id} 0/1: A week without a forge\n  1 ruled yes "
            f"{_first_ruling_date(capsys)}: {asked_text}"
        )

        child_state = json.loads(_run_ok(["item", "show", child_id, "--json"], capsys))["state"]
        assert child_state == "closed"
        container_state = json.loads(_run_ok(["item", "show", container_id, "--json"], capsys))[
            "state"
        ]
        assert container_state == "open"
