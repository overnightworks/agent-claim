"""Behavior of the GitHub adapter (`agent_coordination.github`): `GitHubForge`'s
reader/writer methods, repository discovery, and the `gh` process boundary it
wraps. CLI wiring that merely drives a forge through `cli.py` -- including
tests that build a `GitHubForge` only to hand it to `issue_claim._board`/
`issue_claim.main` -- stays in `tests/test_cli.py`."""

from __future__ import annotations

import dataclasses
import json
import subprocess
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

import pytest
from board_fixtures import REPOSITORY
from github_fixtures import LANDING_BRANCH, MERGE_COMMIT_SHA

from agent_coordination import board, forge, github, process
from agent_coordination.protocol import ClaimError

GitHubForge = github.GitHubForge


def test_forge_operation_exhaustiveness_matches_the_declared_reader_and_writer_methods() -> None:
    """Every `ForgeOperation` member names a `ForgeReader`/`ForgeWriter` method and
    nothing else; the count is pinned so a new operation cannot be added without
    its enum member, its capability entry, and this count bump."""
    declared_methods = {
        name
        for name in dir(forge.ForgeWriter)
        if not name.startswith("_") and name not in {"repository", "capability", "requests"}
    }
    assert {operation.value for operation in forge.ForgeOperation} == declared_methods
    assert len(forge.ForgeOperation) == 12
    assert set(github.GITHUB_CAPABILITIES) == set(forge.ForgeOperation)
    assert forge.Capability.UNSUPPORTED not in github.GITHUB_CAPABILITIES.values()


def test_only_the_github_adapter_speaks_gh_argv() -> None:
    """Source check, not a runtime guarantee: import-linter's module contract
    cannot see an argv string, so "only the adapter speaks `gh`" is proven by
    grepping every other module for the literal command name instead. Every
    module in the package is enumerated, not a fixed list, so a new module
    is covered the day it is added."""
    package = Path("src/agent_coordination")
    other_modules = sorted(p for p in package.glob("*.py") if p.name != "github.py")
    assert len(other_modules) >= 7
    for module in other_modules:
        assert '"gh"' not in module.read_text(), f"{module.name} must not construct a gh argv"


def board_issue_page_client(*rows: dict[str, object]) -> GitHubForge:
    return GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: "\n".join(json.dumps(row) for row in rows),
    )


def raw_board_issue(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "number": 1,
        "title": "Title",
        "labels": [],
        "body": "",
        "createdAt": "2026-08-20T00:00:00Z",
        "updatedAt": "2026-08-20T00:00:00Z",
        "isPullRequest": False,
        "kind": None,
        "childrenClosed": None,
        "childrenTotal": None,
        "blockedByCount": 0,
    }
    base.update(overrides)
    return base


def test_github_adapter_reads_the_native_issue_type_and_sub_issue_counts() -> None:
    client = board_issue_page_client(
        raw_board_issue(kind="Container", childrenClosed=1, childrenTotal=2)
    )

    issues = client.list_open_board_issues()

    assert issues[0].kind is board.ItemKind.CONTAINER
    assert issues[0].children_closed == 1
    assert issues[0].children_total == 2


def test_github_adapter_reads_an_unrecognized_issue_type_as_no_kind() -> None:
    client = board_issue_page_client(raw_board_issue(kind="Epic"))

    issues = client.list_open_board_issues()

    assert issues[0].kind is None


def test_github_adapter_reads_a_container_with_zero_children_as_a_real_state() -> None:
    """`0/0` must survive as a real container state, never as the forge
    saying nothing (the malformed-mixed-presence check would otherwise be
    indistinguishable from a genuinely empty container)."""
    client = board_issue_page_client(
        raw_board_issue(kind="Container", childrenClosed=0, childrenTotal=0)
    )

    issues = client.list_open_board_issues()

    assert issues[0].children_closed == 0
    assert issues[0].children_total == 0


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"childrenClosed": 1}, id="total-missing"),
        pytest.param({"childrenTotal": 2}, id="closed-missing"),
        pytest.param({"childrenClosed": -1, "childrenTotal": 2}, id="closed-negative"),
        pytest.param({"childrenClosed": 3, "childrenTotal": 2}, id="closed-exceeds-total"),
        pytest.param({"childrenClosed": True, "childrenTotal": 2}, id="closed-is-a-bool"),
        pytest.param({"kind": 5}, id="kind-not-a-string"),
        pytest.param({"childrenClosed": 2, "childrenTotal": True}, id="total-is-a-bool"),
        pytest.param({"childrenClosed": 2, "childrenTotal": -1}, id="total-negative"),
    ],
)
def test_github_adapter_fails_loud_on_a_malformed_board_issue(
    overrides: dict[str, object],
) -> None:
    client = board_issue_page_client(raw_board_issue(**overrides))

    with pytest.raises(ClaimError, match="malformed board issue"):
        client.list_open_board_issues()


def test_github_adapter_fails_loud_when_a_board_issue_is_not_an_object() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY), run=lambda arguments, input_data=None: '"not an object"'
    )

    with pytest.raises(ClaimError, match="malformed board issue"):
        client.list_open_board_issues()


def _paged_board_issue_client(page_rows: Callable[[int], list[dict[str, object]]]) -> GitHubForge:
    """An adapter whose `gh` stand-in answers each requested page number from
    `page_rows`, so a test can decide where the listing runs short."""

    def run(arguments: list[str], input_data: bytes | None = None) -> str:
        page = int(arguments[1].rsplit("page=", 1)[1])
        return "\n".join(json.dumps(row) for row in page_rows(page))

    return GitHubForge(github._repository_id(REPOSITORY), run=run)


def test_github_adapter_fetches_board_pages_until_one_comes_back_short(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A full first page means more may exist, so the listing asks for the
    next batch; the short page inside that batch is what ends it, and pages
    past the last one come back empty exactly as GitHub answers them."""
    monkeypatch.setattr(github, "ISSUES_PER_PAGE", 2)
    pages = {
        1: [raw_board_issue(number=1), raw_board_issue(number=2)],
        2: [raw_board_issue(number=3)],
    }
    client = _paged_board_issue_client(lambda page: pages.get(page, []))

    assert [issue.number for issue in client.list_open_board_issues()] == [1, 2, 3]


def test_github_adapter_asks_for_a_second_batch_when_the_first_is_still_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One concurrent batch is not always enough: a board whose whole first
    batch comes back full must ask for another rather than stopping there."""
    monkeypatch.setattr(github, "ISSUES_PER_PAGE", 1)
    monkeypatch.setattr(github, "PARALLEL_FETCH_CONCURRENCY", 1)
    client = _paged_board_issue_client(
        lambda page: [raw_board_issue(number=page)] if page <= 3 else []
    )

    assert [issue.number for issue in client.list_open_board_issues()] == [1, 2, 3]


def test_github_adapter_accepts_pretty_and_ansi_colored_json() -> None:
    """`gh` pretty-prints and colorizes when it believes it writes to a TTY;
    the adapter reads that back as the same values as compact NDJSON."""
    pretty = (
        json.dumps(raw_board_issue(number=1), indent=2)
        + "\n"
        + json.dumps(raw_board_issue(number=2), indent=2)
    )
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: f"\x1b[32m{pretty}\x1b[0m",
    )

    assert [issue.number for issue in client.list_open_board_issues()] == [1, 2]


def test_github_adapter_accepts_concatenated_pretty_json_objects() -> None:
    """Pretty-printed objects can arrive with no separator at all between
    them, which is neither NDJSON nor a JSON array."""
    raw = json.dumps(raw_board_issue(number=1), indent=2) + json.dumps(
        raw_board_issue(number=2), indent=2
    )
    client = GitHubForge(
        github._repository_id(REPOSITORY), run=lambda arguments, input_data=None: raw
    )

    assert [issue.number for issue in client.list_open_board_issues()] == [1, 2]


def raw_board_pull_request(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "number": 62,
        "title": "Fixes #10",
        "body": "Ship it.",
        "headRefName": "codex/issue-10",
        "mergedAt": None,
    }
    base.update(overrides)
    return base


def test_github_adapter_reads_an_open_board_pull_request() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: json.dumps(raw_board_pull_request()),
    )

    assert client.list_open_board_pull_requests() == (
        board.PullRequest(62, "Fixes #10", "Ship it.", "codex/issue-10", None),
    )


def test_github_adapter_reads_a_board_pull_request_with_no_body_as_empty() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: json.dumps(raw_board_pull_request(body=None)),
    )

    assert client.list_open_board_pull_requests()[0].body == ""


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"number": True}, id="number-is-a-bool"),
        pytest.param({"number": 0}, id="number-not-positive"),
        pytest.param({"title": 5}, id="title-not-text"),
        pytest.param({"body": 5}, id="body-not-text"),
        pytest.param({"headRefName": None}, id="head-ref-missing"),
        pytest.param({"mergedAt": 5}, id="merged-at-not-text"),
        pytest.param({"mergedAt": "yesterday"}, id="merged-at-unparsable-shape"),
    ],
)
def test_github_adapter_fails_loud_on_a_malformed_board_pull_request(
    overrides: dict[str, object],
) -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: json.dumps(raw_board_pull_request(**overrides)),
    )

    with pytest.raises(ClaimError, match="malformed board pull request"):
        client.list_open_board_pull_requests()


def test_github_adapter_fails_loud_when_a_board_pull_request_is_not_an_object() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY), run=lambda arguments, input_data=None: '"not an object"'
    )

    with pytest.raises(ClaimError, match="malformed board pull request"):
        client.list_open_board_pull_requests()


def test_github_adapter_creates_a_child_and_links_it_as_a_sub_issue() -> None:
    """Composed from `create_issue` and `link_child` (#260): the create POST,
    the id read `link_child` needs for an issue it did not just create
    itself, and the sub-issue POST."""
    observed: list[tuple[list[str], bytes | None]] = []

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append((arguments, input_data))
        if arguments[2] == "POST" and arguments[3].endswith("/issues"):
            return json.dumps({"id": 555444, "number": 101})
        if arguments[1] == f"repos/{REPOSITORY}/issues/101":
            return "555444"
        return ""

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    child = client.create_child(
        parent=79, title="Scheibe 4", body=board.BLOCK_CHILD_SKELETON, kind=board.ItemKind.TASK
    )

    assert child == 101
    assert observed == [
        (
            ["api", "--method", "POST", f"repos/{REPOSITORY}/issues", "--input", "-"],
            json.dumps(
                {"title": "Scheibe 4", "body": board.BLOCK_CHILD_SKELETON, "type": "Task"}
            ).encode("utf-8"),
        ),
        (["api", f"repos/{REPOSITORY}/issues/101", "--jq", ".id"], None),
        (
            ["api", "--method", "POST", f"repos/{REPOSITORY}/issues/79/sub_issues", "--input", "-"],
            json.dumps({"sub_issue_id": 555444}).encode("utf-8"),
        ),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("not json", id="invalid-json"),
        pytest.param(json.dumps({"id": 1}), id="missing-number"),
        pytest.param(json.dumps({"number": 1}), id="missing-id"),
        pytest.param(json.dumps({"id": True, "number": 1}), id="id-is-a-bool"),
    ],
)
def test_github_adapter_fails_loud_on_a_malformed_created_child(payload: str) -> None:
    client = GitHubForge(github._repository_id(REPOSITORY), run=lambda *_a, **_k: payload)

    with pytest.raises(ClaimError, match=r"created.issue"):
        client.create_child(parent=79, title="Scheibe 4", body="", kind=board.ItemKind.TASK)


def test_github_adapter_names_the_created_child_when_the_relation_post_fails() -> None:
    """The issue exists once `create_child`'s first write returns; a second
    write failing after that must name the surviving child (#112 finding 3),
    never just surface the raw relation-POST error."""

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        if arguments[2] == "POST" and arguments[3].endswith("/issues"):
            return json.dumps({"id": 555444, "number": 101})
        raise forge.ForgeError("HTTP 422 could not create sub-issue relation")

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    with pytest.raises(forge.ForgePartialChildCreationError) as excinfo:
        client.create_child(parent=79, title="Scheibe 4", body="", kind=board.ItemKind.TASK)

    assert excinfo.value.child == 101
    assert excinfo.value.parent == 79


def test_github_adapter_creates_an_issue_without_linking_it_as_a_child() -> None:
    """`_create_issue` is `create_child`'s first write on its own (#260): one
    POST, no sub-issue relation -- `link_child` is the caller's to run,
    later, against a number it may not have yet. Private (no other caller,
    #260 Sonnet finding): exercised directly here rather than through the
    port."""
    observed: list[tuple[list[str], bytes | None]] = []

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append((arguments, input_data))
        return json.dumps({"id": 555444, "number": 101})

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    number = client._create_issue(
        title="Scheibe 4", body=board.BLOCK_CHILD_SKELETON, kind=board.ItemKind.TASK
    )

    assert number == 101
    assert observed == [
        (
            ["api", "--method", "POST", f"repos/{REPOSITORY}/issues", "--input", "-"],
            json.dumps(
                {"title": "Scheibe 4", "body": board.BLOCK_CHILD_SKELETON, "type": "Task"}
            ).encode("utf-8"),
        )
    ]


def test_github_adapter_links_an_existing_child_by_reading_its_internal_id() -> None:
    """`link_child` records an already-existing issue as a sub-issue (#260) --
    the write a repeat `cut` uses to adopt an orphan `create_child` left
    behind. GitHub's sub-issue POST wants the child's internal id, not its
    issue number, so this reads it first."""
    observed: list[tuple[list[str], bytes | None]] = []

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append((arguments, input_data))
        return "555444" if arguments[1].endswith("/issues/101") else ""

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    client.link_child(79, 101)

    assert observed == [
        (["api", f"repos/{REPOSITORY}/issues/101", "--jq", ".id"], None),
        (
            ["api", "--method", "POST", f"repos/{REPOSITORY}/issues/79/sub_issues", "--input", "-"],
            json.dumps({"sub_issue_id": 555444}).encode("utf-8"),
        ),
    ]


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param("not a number", id="not-numeric"),
        pytest.param("0", id="zero"),
    ],
)
def test_github_adapter_fails_loud_on_a_malformed_child_identifier(payload: str) -> None:
    client = GitHubForge(github._repository_id(REPOSITORY), run=lambda *_a, **_k: payload)

    with pytest.raises(ClaimError, match="malformed issue id"):
        client.link_child(79, 101)


def test_github_adapter_updates_an_item_body() -> None:
    observed: list[tuple[list[str], bytes | None]] = []

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append((arguments, input_data))
        return ""

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    client.update_item_body(79, "new body")

    assert observed == [
        (
            ["api", "--method", "PATCH", f"repos/{REPOSITORY}/issues/79", "--input", "-"],
            json.dumps({"body": "new body"}).encode("utf-8"),
        )
    ]


_LANDING_COMMENTS_PATH = f"repos/{REPOSITORY}/issues/79/comments"
_LANDING_COMMENTS_READ = [
    "api",
    "--paginate",
    f"{_LANDING_COMMENTS_PATH}?per_page=100",
    "--jq",
    ".[] | {body}",
]


def test_github_adapter_closes_a_landed_item_with_a_comment_first() -> None:
    """Issue #359 Card 1/CI, extended by issue #397: `close_landed_item`
    reads its own past comments first (a repeat-safety check that finds
    none here), then posts the comment, then closes -- exactly the order
    its own docstring promises."""
    observed: list[tuple[list[str], bytes | None]] = []

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        observed.append((arguments, input_data))
        return ""

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    client.close_landed_item(79, pull_request=101)

    assert observed == [
        (_LANDING_COMMENTS_READ, None),
        (
            ["api", _LANDING_COMMENTS_PATH, "--input", "-"],
            json.dumps({"body": github.landing_comment(101)}).encode("utf-8"),
        ),
        (
            ["api", "--method", "PATCH", f"repos/{REPOSITORY}/issues/79", "--input", "-"],
            json.dumps({"state": "closed"}).encode("utf-8"),
        ),
    ]


def test_github_adapter_closing_a_landed_item_never_reaches_close_when_the_comment_fails() -> None:
    """The comment lands first (issue #359 Card 1): a transient failure
    there must never reach the close call, leaving the issue open with no
    record of why it is about to close -- worse than leaving it open with
    the comment already explaining the pending landing."""
    calls: list[list[str]] = []

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        calls.append(arguments)
        if input_data is None:
            return ""
        raise forge.ForgeError("HTTP 500 comment failed")

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    with pytest.raises(forge.ForgeError, match="comment failed"):
        client.close_landed_item(79, pull_request=101)

    assert calls == [
        _LANDING_COMMENTS_READ,
        ["api", _LANDING_COMMENTS_PATH, "--input", "-"],
    ]


def test_github_adapter_closing_a_landed_item_skips_a_repeated_comment() -> None:
    """Issue #397: a rerun that finds its own `landed by PR #<n>` comment
    already posted -- the comment landed but a prior run crashed before the
    close -- skips straight to the close instead of posting it twice."""
    calls: list[list[str]] = []
    already_posted = json.dumps({"body": github.landing_comment(101)})

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        calls.append(arguments)
        return already_posted if input_data is None else ""

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    client.close_landed_item(79, pull_request=101)

    assert calls == [
        _LANDING_COMMENTS_READ,
        ["api", "--method", "PATCH", f"repos/{REPOSITORY}/issues/79", "--input", "-"],
    ]


def test_github_adapter_finds_a_landing_comment_past_the_first_page() -> None:
    """Issue #397: a plain `gh api` request serves only its own `per_page`
    (100 here) without `--paginate`. With 150 unrelated comments ahead of
    this run's own landing comment -- posted by a run whose close then
    failed -- a rerun must still find it via `--paginate` and skip straight
    to closing, never posting it a second time; dropping `--paginate` from
    the request would make this fail, since the fake `gh` below then serves
    only the truncated first page, which does not reach the landing
    comment at index 150."""
    unrelated_comments = [
        json.dumps({"body": f"unrelated comment {index}"}) for index in range(150)
    ]
    all_comments = "\n".join(
        [*unrelated_comments, json.dumps({"body": github.landing_comment(101)})]
    )
    first_page_only = "\n".join(unrelated_comments[:100])
    calls: list[list[str]] = []
    close_should_fail = True

    def fake_run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        calls.append(arguments)
        nonlocal close_should_fail
        if input_data is None:
            return all_comments if "--paginate" in arguments else first_page_only
        if close_should_fail:
            close_should_fail = False
            raise forge.ForgeError("HTTP 500 close failed")
        return ""

    client = GitHubForge(github._repository_id(REPOSITORY), run=fake_run)

    with pytest.raises(forge.ForgeError, match="close failed"):
        client.close_landed_item(79, pull_request=101)
    client.close_landed_item(79, pull_request=101)

    assert calls == [
        _LANDING_COMMENTS_READ,
        ["api", "--method", "PATCH", f"repos/{REPOSITORY}/issues/79", "--input", "-"],
        _LANDING_COMMENTS_READ,
        ["api", "--method", "PATCH", f"repos/{REPOSITORY}/issues/79", "--input", "-"],
    ]


def test_github_adapter_fails_loud_on_a_malformed_issue_comment() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: json.dumps({"body": 5}),
    )

    with pytest.raises(ClaimError, match="malformed issue comment"):
        client.close_landed_item(79, pull_request=101)


def test_recent_merged_pull_requests_refuses_a_window_that_ends_before_it_starts() -> None:
    """A fixed far-future `since` -- never `datetime.now(UTC)`-relative -- so
    this stays deterministic regardless of when the suite runs: a real-clock
    window could cross UTC midnight between this line and the production
    code's own `datetime.now(UTC)` call, sometimes closing the window and
    falling through to a real, unfaked `gh` call instead of raising."""
    client = GitHubForge(github._repository_id("example/agent-claim"))
    raised_argument_1 = datetime(2099, 1, 1, tzinfo=UTC)
    with pytest.raises(ClaimError, match="merged pull request window ends before it starts"):
        client.list_recent_merged_board_pull_requests(raised_argument_1)


def test_github_adapter_runs_gh_when_no_fake_run_is_given(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every other adapter test injects `run=` to avoid a real subprocess;
    this proves the adapter's own default (`_gh`, via `_bounded_command`)
    actually builds a `gh` command and runs it through the real bounded-I/O
    machinery -- substituting the child process itself so the test needs no
    real `gh` executable."""
    observed: list[list[str]] = []
    original_popen = subprocess.Popen

    def start(
        command: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        observed.append(command)
        substituted = [sys.executable, "-c", "print('main')"]
        return original_popen(substituted, stdin=stdin, stdout=stdout, stderr=stderr, env=env)

    monkeypatch.setattr(subprocess, "Popen", start)
    client = GitHubForge(github._repository_id("example/agent-claim"))

    assert client.default_branch() == "main"
    assert observed == [["gh", "api", "repos/example/agent-claim", "--jq", ".default_branch"]]


def test_github_adapter_capability_reads_the_declared_table() -> None:
    client = GitHubForge(github._repository_id("example/agent-claim"))

    assert client.capability(forge.ForgeOperation.ITEM_REFERENCE) is forge.Capability.READ_ONLY
    assert client.capability(forge.ForgeOperation.CREATE_CHILD) is forge.Capability.READ_WRITE


def test_github_adapter_item_reference_reads_state_title_and_body() -> None:
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda _arguments: json.dumps(
            {"state": "open", "title": "Work", "body": "Do it.", "is_landing": False}
        ),
    )

    assert client.item_reference(10) == forge.ItemReference(forge.ItemState.OPEN, "Work", "Do it.")


def test_github_adapter_item_reference_reports_a_pull_request_as_a_landing() -> None:
    """The one read `check` distributes on: GitHub answers for a pull request
    at the issues endpoint too, and only this flag tells the two apart."""
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda _arguments: json.dumps(
            {"state": "open", "title": "Land it", "body": "Work-Item: #7", "is_landing": True}
        ),
    )

    assert client.item_reference(12) == forge.ItemReference(
        forge.ItemState.OPEN, "Land it", "Work-Item: #7", True
    )


def test_github_adapter_item_reference_reads_a_closed_issue_with_no_body() -> None:
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda _arguments: json.dumps(
            {"state": "closed", "title": "Work", "body": None, "is_landing": False}
        ),
    )

    assert client.item_reference(10) == forge.ItemReference(forge.ItemState.CLOSED, "Work", "")


def test_github_adapter_item_reference_is_missing_after_a_404() -> None:
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda _arguments: (_ for _ in ()).throw(
            forge.ForgeNotFoundError("GitHub API failed: HTTP 404")
        ),
    )

    assert client.item_reference(10) == forge.ItemReference(forge.ItemState.MISSING)


@pytest.mark.parametrize(
    ("raw", "match"),
    [
        pytest.param("not-json", "invalid issue reference JSON", id="not-json"),
        pytest.param(json.dumps([]), "malformed issue reference", id="no-values"),
        pytest.param(
            json.dumps([{"a": 1}, {"b": 2}]), "malformed issue reference", id="two-values"
        ),
        pytest.param(json.dumps(["not-a-dict"]), "malformed issue reference", id="not-a-dict"),
        pytest.param(
            json.dumps({"state": "unknown", "title": "x", "body": None}),
            "malformed issue reference",
            id="unknown-state",
        ),
        pytest.param(
            json.dumps({"state": "open", "title": 5, "body": None}),
            "malformed issue reference",
            id="title-not-text",
        ),
        pytest.param(
            json.dumps({"state": "open", "title": "x", "body": 5}),
            "malformed issue reference",
            id="body-not-text",
        ),
    ],
)
def test_github_adapter_item_reference_fails_loud_on_a_malformed_response(
    raw: str, match: str
) -> None:
    client = GitHubForge(github._repository_id("example/agent-claim"), run=lambda _arguments: raw)

    with pytest.raises(ClaimError, match=match):
        client.item_reference(10)


def test_github_reads_board_dependencies_local_and_foreign(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[list[str]] = []

    def run(arguments: list[str]) -> str:
        observed.append(arguments)
        rows = [
            {
                "number": 151,
                "state": "closed",
                "closedAt": "2026-09-05T00:00:00Z",
                "repository": "example/agent-claim",
                "isPullRequest": False,
            },
            {
                "number": 7,
                "state": "open",
                "closedAt": None,
                "repository": "overnightworks/other-repo",
                "isPullRequest": False,
            },
        ]
        return "\n".join(json.dumps(row) for row in rows)

    client = GitHubForge(github._repository_id("example/agent-claim"), run=run)

    assert client.list_board_dependencies(150) == (
        board.IssueDependency(
            board.IssueReference("example/agent-claim", 151),
            board.BlockerState.CLOSED,
            False,
            datetime(2026, 9, 5, tzinfo=UTC),
        ),
        board.IssueDependency(
            board.IssueReference("overnightworks/other-repo", 7),
            board.BlockerState.OPEN,
            False,
            None,
        ),
    )
    assert observed == [
        [
            "api",
            "--paginate",
            "repos/example/agent-claim/issues/150/dependencies/blocked_by?per_page=100",
            "--jq",
            ".[] | {number,state,closedAt:.closed_at,repository:.repository.full_name,"
            'isPullRequest:has("pull_request")}',
        ]
    ]


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(json.dumps(["not-a-dict"]), id="not-a-dict"),
        pytest.param(
            json.dumps(
                {
                    "number": 151,
                    "state": "open",
                    "closedAt": None,
                    "repository": "not a repo",
                    "isPullRequest": False,
                }
            ),
            id="malformed-repository",
        ),
        pytest.param(
            json.dumps(
                {
                    "number": 151,
                    "state": "closed",
                    "closedAt": None,
                    "repository": "example/agent-claim",
                    "isPullRequest": False,
                }
            ),
            id="closed-without-timestamp",
        ),
    ],
)
def test_github_board_dependency_fails_loud_on_a_malformed_shape(raw: str) -> None:
    client = GitHubForge(github._repository_id("example/agent-claim"), run=lambda _arguments: raw)

    with pytest.raises(ClaimError, match="malformed board blocked-by dependency"):
        client.list_board_dependencies(150)


def test_github_board_dependency_fails_loud_on_an_uncalendared_closed_timestamp() -> None:
    """`closedAt` can pass the timestamp-shape check (digits in the right
    places) while still naming no real calendar date; `datetime.fromisoformat`
    itself is the second, calendar-aware check that catches that."""
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda _arguments: json.dumps(
            {
                "number": 151,
                "state": "closed",
                "closedAt": "9999-99-99T00:00:00Z",
                "repository": "example/agent-claim",
                "isPullRequest": False,
            }
        ),
    )

    with pytest.raises(ClaimError, match="malformed board blocked-by dependency"):
        client.list_board_dependencies(150)


def test_bounded_command_sets_github_quiet_environment() -> None:
    observed = github._bounded_command(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['NO_COLOR']); print(os.environ['GH_NO_UPDATE_NOTIFIER'])",
        ],
        purpose="env probe",
    )

    assert observed.splitlines() == ["1", "1"]


def _non_github_remote_url() -> str:
    """A remote URL `discover_repository` reads first (issue #245) and finds
    no repository in, so it falls through to asking `gh` -- every test using
    this in place of a matching GitHub remote is exercising that fallback,
    never the cheap local-URL read."""
    return "https://example.com/owner/repo"


def test_repository_resolution_uses_github_quiet_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}

    def fake_run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        observed["command"] = command
        observed["env"] = kwargs.get("env")
        return subprocess.CompletedProcess(command, 0, b"\x1b[32mowner/repository\x1b[0m\n", b"")

    monkeypatch.setattr(subprocess, "run", fake_run)

    resolved = github.discover_repository(None, remote_url=_non_github_remote_url)

    assert resolved == forge.RepositoryId(github.GITHUB_HOST, ("owner",), "repository")
    command = observed["command"]
    assert isinstance(command, list)
    assert command[0] == "gh"
    env = observed["env"]
    assert isinstance(env, dict)
    assert env["NO_COLOR"] == "1"
    assert env["GH_NO_UPDATE_NOTIFIER"] == "1"


def test_merged_pull_request_history_warns_when_it_reaches_the_result_cap(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A full result page for a day's shard means more merged pull requests
    could exist that day beyond the cap, so the board must say that day's
    history may be incomplete rather than silently compute stages from a
    truncated query. `since` and "now" are pinned to the same day so the
    fetch is exactly one shard, matching the fixture below.
    """
    since = datetime(2026, 8, 1, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return since

    monkeypatch.setattr(github, "datetime", FixedDateTime)
    saturated_rows = [
        {
            "number": index,
            "title": f"Fixes #{index}",
            "body": "",
            "headRefName": f"codex/issue-{index}",
            "mergedAt": "2026-08-01T00:00:00Z",
        }
        for index in range(1, github.MAX_RECENT_MERGED_PULL_REQUESTS + 1)
    ]
    client = GitHubForge(
        github._repository_id("example/agent-claim"),
        run=lambda arguments: "\n".join(json.dumps(row) for row in saturated_rows),
    )

    pull_requests = client.list_recent_merged_board_pull_requests(since)

    assert len(pull_requests) == github.MAX_RECENT_MERGED_PULL_REQUESTS
    error = capsys.readouterr().err
    assert "WARNING" in error
    assert str(github.MAX_RECENT_MERGED_PULL_REQUESTS) in error
    assert since.date().isoformat() in error


def test_merged_pull_request_history_below_the_cap_warns_of_nothing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    since = datetime(2026, 8, 1, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return since

    monkeypatch.setattr(github, "datetime", FixedDateTime)
    row = {
        "number": 1,
        "title": "Fixes #1",
        "body": "",
        "headRefName": "codex/issue-1",
        "mergedAt": "2026-08-01T00:00:00Z",
    }
    client = GitHubForge(
        github._repository_id("example/agent-claim"), run=lambda arguments: json.dumps(row)
    )

    client.list_recent_merged_board_pull_requests(since)

    assert capsys.readouterr().err == ""


def test_recent_merged_pull_requests_skips_an_entry_with_no_merge_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`mergedAt` can be `None` on an otherwise well-shaped merged-search row
    (a defensive read, not a documented GitHub behavior); such a row cannot
    be dated against the window, so it is skipped rather than crashing the
    whole fetch."""
    since = datetime(2026, 8, 1, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return since

    monkeypatch.setattr(github, "datetime", FixedDateTime)
    row = {
        "number": 1,
        "title": "Fixes #1",
        "body": "",
        "headRefName": "codex/issue-1",
        "mergedAt": None,
    }
    client = GitHubForge(
        github._repository_id("example/agent-claim"), run=lambda arguments: json.dumps(row)
    )

    assert client.list_recent_merged_board_pull_requests(since) == ()


def test_recent_merged_pull_requests_fails_loud_on_an_uncalendared_merge_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    since = datetime(2026, 8, 1, tzinfo=UTC)

    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return since

    monkeypatch.setattr(github, "datetime", FixedDateTime)
    row = {
        "number": 1,
        "title": "Fixes #1",
        "body": "",
        "headRefName": "codex/issue-1",
        "mergedAt": "9999-99-99T00:00:00Z",
    }
    client = GitHubForge(
        github._repository_id("example/agent-claim"), run=lambda arguments: json.dumps(row)
    )

    with pytest.raises(ClaimError, match="malformed merged board pull request"):
        client.list_recent_merged_board_pull_requests(since)


def test_missing_gh_repository_resolution_is_a_controlled_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(*args, **kwargs):
        raise FileNotFoundError

    monkeypatch.setattr(subprocess, "run", missing)

    with pytest.raises(ClaimError, match="gh is required"):
        github.discover_repository(None, remote_url=_non_github_remote_url)


def test_repository_resolution_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    def timed_out(*args, **kwargs):
        raise subprocess.TimeoutExpired(["gh"], process.DEFAULT_TIMEOUT_SECONDS)

    monkeypatch.setattr(subprocess, "run", timed_out)

    with pytest.raises(ClaimError, match="gh timed out while resolving the repository"):
        github.discover_repository(None, remote_url=_non_github_remote_url)


def test_repository_resolution_refuses_when_no_remote_matches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def failed_gh(*arguments, **kwargs):
        return subprocess.CompletedProcess(arguments[0], 1, b"", b"not a gh repo")

    monkeypatch.setattr(subprocess, "run", failed_gh)

    with pytest.raises(ClaimError, match="cannot resolve GitHub repository"):
        github.discover_repository(None, remote_url=lambda: "https://example.com/owner/repo")


def test_discover_repository_requires_owner_slash_repo_shape_for_an_explicit_repo() -> None:
    with pytest.raises(ClaimError, match="repository must be OWNER/REPO"):
        github.discover_repository("not-a-repository-shape", remote_url=lambda: "")


@pytest.mark.parametrize(
    "remote",
    [
        "https://github.com/owner/repository.git",
        "git@github.com:owner/repository.git",
    ],
)
def test_repository_resolves_from_a_standard_github_remote_without_asking_gh(
    monkeypatch: pytest.MonkeyPatch, remote: str
) -> None:
    """The git remote is `discover_repository`'s first read (issue #245): a
    remote that already names a GitHub repository resolves from it alone,
    with `gh` never invoked at all."""

    def unused(*args, **kwargs):
        pytest.fail("a matching GitHub remote must resolve without ever asking gh")

    monkeypatch.setattr(subprocess, "run", unused)

    resolved = github.discover_repository(None, remote_url=lambda: remote)

    assert resolved == forge.RepositoryId(github.GITHUB_HOST, ("owner",), "repository")


def test_bounded_command_stops_before_unbounded_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(process, "MAX_COMMAND_OUTPUT_BYTES", 32)

    with pytest.raises(ClaimError, match="output limit"):
        github._bounded_command(
            [sys.executable, "-c", "print('x' * 1000)"],
            purpose="test command",
        )


def test_bounded_command_disables_github_update_notifications() -> None:
    observed = github._bounded_command(
        [
            sys.executable,
            "-c",
            "import os; print(os.environ['GH_NO_UPDATE_NOTIFIER'])",
        ],
        purpose="update notifier probe",
    )

    assert observed == "1"


def test_bounded_command_streams_stdin_without_putting_it_in_argv() -> None:
    observed = github._bounded_command(
        [sys.executable, "-c", "import sys; print(sys.stdin.buffer.read().decode())"],
        purpose="stdin probe",
        input_data=b"bounded body",
    )

    assert observed == "bounded body"


def test_bounded_command_wraps_process_argument_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cannot_start(*args, **kwargs):
        raise OSError(7, "Argument list too long")

    monkeypatch.setattr(subprocess, "Popen", cannot_start)

    with pytest.raises(ClaimError, match="cannot start test command"):
        github._bounded_command(["gh", "issue"], purpose="test command")


def test_bounded_command_wraps_stdin_write_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def cannot_write(*args, **kwargs):
        raise OSError(5, "Input/output error")

    monkeypatch.setattr(github.os, "write", cannot_write)

    with pytest.raises(ClaimError, match="failed while sending bounded input"):
        github._bounded_command(
            [sys.executable, "-c", "import sys; sys.stdin.buffer.read()"],
            purpose="stdin write probe",
            input_data=b"body",
        )


def test_bounded_command_reaps_child_when_selector_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, subprocess.Popen[bytes]] = {}
    original_popen = subprocess.Popen

    def start(
        command: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        process = original_popen(command, stdin=stdin, stdout=stdout, stderr=stderr, env=env)
        observed["process"] = process
        return process

    def cannot_select():
        raise OSError(5, "selector failed")

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(process.selectors, "DefaultSelector", cannot_select)

    with pytest.raises(ClaimError, match="failed while coordinating I/O"):
        github._bounded_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            purpose="selector setup probe",
        )

    assert observed["process"].poll() is not None
    assert observed["process"].stdout is not None
    assert observed["process"].stdout.closed


def test_bounded_command_reaps_child_when_select_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, subprocess.Popen[bytes]] = {}
    original_popen = subprocess.Popen

    def start(
        command: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        process = original_popen(command, stdin=stdin, stdout=stdout, stderr=stderr, env=env)
        observed["process"] = process
        return process

    class FailingSelector:
        instance: FailingSelector | None = None

        def __init__(self) -> None:
            self.closed = False
            FailingSelector.instance = self

        def register(self, fileobj, events, data) -> None:
            pass

        def get_map(self):
            return {"stdout": object()}

        def select(self, timeout):
            raise OSError(5, "select failed")

        def close(self) -> None:
            self.closed = True

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(process.selectors, "DefaultSelector", FailingSelector)

    with pytest.raises(ClaimError, match="failed while waiting for I/O"):
        github._bounded_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            purpose="select probe",
        )

    spawned = observed["process"]
    assert spawned.poll() is not None
    assert spawned.stdout is not None
    assert spawned.stdout.closed
    assert FailingSelector.instance is not None
    assert FailingSelector.instance.closed


def test_bounded_command_reaps_child_when_output_read_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, subprocess.Popen[bytes]] = {}
    original_popen = subprocess.Popen
    original_read = github.os.read

    def start(
        command: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        process = original_popen(command, stdin=stdin, stdout=stdout, stderr=stderr, env=env)
        observed["process"] = process
        return process

    def cannot_read(file_descriptor: int, count: int) -> bytes:
        process = observed.get("process")
        if (
            process is not None
            and process.stdout is not None
            and file_descriptor == process.stdout.fileno()
        ):
            raise OSError(5, "read failed")
        return original_read(file_descriptor, count)

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(github.os, "read", cannot_read)

    with pytest.raises(ClaimError, match="failed while reading output"):
        github._bounded_command(
            [
                sys.executable,
                "-u",
                "-c",
                "import time; print('ready'); time.sleep(30)",
            ],
            purpose="read probe",
        )

    assert observed["process"].poll() is not None
    assert observed["process"].stdout is not None
    assert observed["process"].stdout.closed


def test_bounded_command_reaps_child_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, subprocess.Popen[bytes]] = {}
    original_popen = subprocess.Popen

    def start(
        command: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        process = original_popen(command, stdin=stdin, stdout=stdout, stderr=stderr, env=env)
        observed["process"] = process
        return process

    class CancellationSentinel(BaseException):
        pass

    class CancellingSelector:
        def register(self, fileobj, events, data) -> None:
            pass

        def get_map(self):
            return {"stdout": object()}

        def select(self, timeout):
            raise CancellationSentinel

        def close(self) -> None:
            pass

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(process.selectors, "DefaultSelector", CancellingSelector)

    with pytest.raises(CancellationSentinel):
        github._bounded_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            purpose="cancellation probe",
        )

    assert observed["process"].poll() is not None
    assert observed["process"].stdout is not None
    assert observed["process"].stdout.closed


def test_bounded_command_requires_the_named_executable() -> None:
    with pytest.raises(ClaimError) as excinfo:
        github._bounded_command(
            ["missing-claim-command"],
            purpose="missing executable probe",
        )
    assert str(excinfo.value) == "missing-claim-command is required for issue claims"


def test_bounded_command_times_out(monkeypatch: pytest.MonkeyPatch) -> None:
    recorded: dict[str, subprocess.Popen[bytes]] = {}
    original_popen = subprocess.Popen

    def start(
        command: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        process = original_popen(command, stdin=stdin, stdout=stdout, stderr=stderr, env=env)
        recorded["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", start)
    monkeypatch.setattr(github, "GH_TIMEOUT_SECONDS", 0.05)
    with pytest.raises(ClaimError) as excinfo:
        github._bounded_command(
            [sys.executable, "-c", "import time; time.sleep(30)"],
            purpose="timeout probe",
        )
    assert str(excinfo.value) == "timeout probe timed out"
    assert recorded["process"].poll() is not None


def test_bounded_command_stops_a_child_that_hangs_after_closing_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorded: dict[str, subprocess.Popen[bytes]] = {}
    original_popen = subprocess.Popen

    def start(
        command: list[str],
        *,
        stdin: int | None = None,
        stdout: int | None = None,
        stderr: int | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.Popen[bytes]:
        process = original_popen(command, stdin=stdin, stdout=stdout, stderr=stderr, env=env)
        recorded["process"] = process
        return process

    monkeypatch.setattr(subprocess, "Popen", start)
    with pytest.raises(ClaimError) as excinfo:
        github._bounded_command(
            [
                sys.executable,
                "-c",
                (
                    "import os, sys, time\n"
                    "for stream in (sys.stdout, sys.stderr):\n"
                    "    try:\n"
                    "        stream.close()\n"
                    "    except OSError:\n"
                    "        pass\n"
                    "for fd in (1, 2):\n"
                    "    try:\n"
                    "        os.close(fd)\n"
                    "    except OSError:\n"
                    "        pass\n"
                    "time.sleep(30)"
                ),
            ],
            purpose="hang probe",
        )
    assert str(excinfo.value) == "hang probe did not exit after closing its output"
    assert recorded["process"].poll() is not None


def test_bounded_command_uses_combined_output_as_nonzero_exit_message() -> None:
    with pytest.raises(ClaimError) as excinfo:
        github._bounded_command(
            [sys.executable, "-c", "raise SystemExit('boom')"],
            purpose="exit probe",
        )
    assert str(excinfo.value) == "boom"


def test_bounded_command_names_the_exit_code_when_nonzero_output_is_empty() -> None:
    with pytest.raises(ClaimError) as excinfo:
        github._bounded_command(
            [sys.executable, "-c", "raise SystemExit(7)"],
            purpose="empty exit probe",
        )
    assert str(excinfo.value) == "empty exit probe failed with exit 7"


def test_bounded_command_rejects_non_utf8_output() -> None:
    with pytest.raises(ClaimError) as excinfo:
        github._bounded_command(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(bytes((255,))); sys.stdout.buffer.flush()",
            ],
            purpose="decode probe",
        )
    assert str(excinfo.value) == "decode probe returned non-UTF-8 output"


@pytest.mark.parametrize(
    ("outcome", "expected_type", "expected_message"),
    [
        pytest.param(
            process.BoundedResult(0, bytes((255,))),
            forge.ForgeMalformedResponseError,
            "GitHub issue coordination returned non-UTF-8 output",
            id="malformed-non-utf8-output",
        ),
        pytest.param(
            process.BoundedResult(1, b"HTTP 404 Not Found"),
            forge.ForgeNotFoundError,
            "HTTP 404 Not Found",
            id="http-404",
        ),
        pytest.param(
            process.BoundedResult(1, b"HTTP 401 Unauthorized"),
            forge.ForgePermissionDeniedError,
            "HTTP 401 Unauthorized",
            id="http-401",
        ),
        pytest.param(
            process.BoundedResult(1, b"HTTP 403 Forbidden"),
            forge.ForgePermissionDeniedError,
            "HTTP 403 Forbidden",
            id="http-403",
        ),
        pytest.param(
            process.BoundedResult(1, b"HTTP 502 Bad Gateway"),
            forge.ForgeTransientError,
            "HTTP 502 Bad Gateway",
            id="http-5xx",
        ),
        pytest.param(
            process.BoundedResult(1, b"connection reset by peer"),
            forge.ForgeTransientError,
            "connection reset by peer",
            id="connection-reset-signal",
        ),
        pytest.param(
            process.BoundedResult(1, b"request timeout"),
            forge.ForgeTransientError,
            "request timeout",
            id="decoded-timeout-signal",
        ),
        pytest.param(
            process.BoundedResult(1, b""),
            forge.ForgeError,
            "GitHub issue coordination failed with exit 1",
            id="unclassified-empty-exit",
        ),
        pytest.param(
            process.BoundedResult(1, b"some other prose"),
            forge.ForgeError,
            "some other prose",
            id="unclassified-nonzero-exit",
        ),
        pytest.param(
            process.ProcessIoFailedError(process.IoStage.WAITING, "boom"),
            forge.ForgeTransientError,
            "GitHub issue coordination failed while waiting for I/O: boom",
            id="io-stage-waiting",
        ),
        pytest.param(
            process.ProcessIoFailedError(process.IoStage.SENDING, "boom"),
            forge.ForgeTransientError,
            "GitHub issue coordination failed while sending bounded input: boom",
            id="io-stage-sending",
        ),
        pytest.param(
            process.ProcessIoFailedError(process.IoStage.READING, "boom"),
            forge.ForgeTransientError,
            "GitHub issue coordination failed while reading output: boom",
            id="io-stage-reading",
        ),
        pytest.param(
            process.ProcessIoFailedError(process.IoStage.COORDINATING, "boom"),
            forge.ForgeTransientError,
            "GitHub issue coordination failed while coordinating I/O: boom",
            id="io-stage-coordinating",
        ),
        pytest.param(
            process.ProcessDidNotExitError(),
            forge.ForgeTransientError,
            "GitHub issue coordination did not exit after closing its output",
            id="did-not-exit",
        ),
        pytest.param(
            process.ProcessTimedOutError(),
            forge.ForgeTransientError,
            "GitHub issue coordination timed out",
            id="process-timed-out",
        ),
        pytest.param(
            process.ProcessOutputTooLargeError(),
            forge.ForgeMalformedResponseError,
            "GitHub issue coordination exceeded its output limit",
            id="output-too-large",
        ),
    ],
)
def test_bounded_command_classifies_every_forge_failure_signal(
    monkeypatch: pytest.MonkeyPatch,
    outcome: process.BoundedResult | process.ProcessError,
    expected_type: type[forge.ForgeError],
    expected_message: str,
) -> None:
    """Adapter-boundary proof, driven through `GitHubForge.default_branch`
    (the real caller of `_gh` -> `_bounded_command`) with `process.run_bounded`
    faked: every nonzero-exit signal (#4.2) and every process failure that
    reaches no forge response (#4.1) becomes the exact typed `ForgeError`
    subclass with the exact message -- not just some `ClaimError`."""

    def fake_run_bounded(*args, **kwargs):
        if isinstance(outcome, process.ProcessError):
            raise outcome
        return outcome

    monkeypatch.setattr(process, "run_bounded", fake_run_bounded)
    client = GitHubForge(github._repository_id("example/agent-claim"))

    with pytest.raises(expected_type) as excinfo:
        client.default_branch()

    assert type(excinfo.value) is expected_type
    assert str(excinfo.value) == expected_message


def test_bounded_command_refuses_a_process_error_type_it_does_not_classify(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The isinstance chain the GitHub adapter's failure classifier walks names
    every concrete `process.ProcessError` subclass; a hypothetical new one added
    there without updating this dispatch must fail loud as a defect, not
    silently fall through as some generic `ForgeError` -- proven through
    `GitHubForge.default_branch`, the real caller, with `process.run_bounded`
    faked to raise the unclassified type."""

    class _UnknownProcessError(process.ProcessError):
        pass

    def fake_run_bounded(*args, **kwargs):
        raise _UnknownProcessError

    monkeypatch.setattr(process, "run_bounded", fake_run_bounded)
    client = GitHubForge(github._repository_id("example/agent-claim"))

    with pytest.raises(AssertionError, match="unhandled process failure type"):
        client.default_branch()


def api_pull_request(**overrides: object) -> dict[str, object]:
    owner, _, name = REPOSITORY.partition("/")
    payload: dict[str, object] = {
        "number": 12,
        "body": "Work-Item: #72",
        "baseRefName": "main",
        "headRefName": LANDING_BRANCH,
        "headRepository": {"name": name},
        "headRepositoryOwner": {"login": owner},
        "author": {"login": "ada"},
        "mergedAt": "2026-09-05T10:00:00Z",
        "mergeCommit": {"oid": MERGE_COMMIT_SHA},
    }
    return payload | overrides


def test_github_adapter_reads_a_pull_request_and_the_default_branch() -> None:
    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        if arguments[:2] == ["pr", "view"]:
            return json.dumps(api_pull_request())
        return "main"

    client = GitHubForge(github._repository_id(REPOSITORY), run=run)

    expected = forge.Landing(
        12,
        "ada",
        "Work-Item: #72",
        github._repository_id(REPOSITORY),
        LANDING_BRANCH,
        "main",
        True,
        MERGE_COMMIT_SHA,
    )
    assert dataclasses.astuple(client.landing(12)) == dataclasses.astuple(expected)
    assert client.default_branch() == "main"


def test_github_adapter_reads_a_landing_with_no_body_as_empty() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: json.dumps(api_pull_request(body=None)),
    )

    assert client.landing(12).body == ""


def test_github_adapter_reads_a_fork_branch_as_its_own_repository() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: json.dumps(
            api_pull_request(
                headRepository={"name": "agent-claim"},
                headRepositoryOwner={"login": "fork"},
            )
        ),
    )

    assert client.landing(12).source_repository == github._repository_id("fork/agent-claim")


def test_github_adapter_fails_loud_when_github_answers_for_another_pull_request() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: json.dumps(api_pull_request(number=13)),
    )

    with pytest.raises(ClaimError, match="answered for pull request #13, not #12"):
        client.landing(12)


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param({"number": 12, "body": "b"}, id="missing-refs"),
        pytest.param(api_pull_request(author={}), id="author-without-login"),
        pytest.param(api_pull_request(mergedAt="yesterday"), id="malformed-merge-time"),
        pytest.param(api_pull_request(headRepository={}), id="head-repository-without-name"),
        pytest.param(
            api_pull_request(headRepositoryOwner=None), id="head-repository-without-owner"
        ),
        pytest.param(
            api_pull_request(
                headRepository={"name": "repo/extra"}, headRepositoryOwner={"login": "owner"}
            ),
            id="head-repository-invalid-shape",
        ),
        pytest.param(api_pull_request(mergeCommit=None), id="merged-with-no-merge-commit"),
        pytest.param(
            api_pull_request(mergeCommit={"oid": "not-a-sha"}), id="malformed-merge-commit-oid"
        ),
        pytest.param(
            api_pull_request(mergeCommit={"oid": 1234567}), id="merge-commit-oid-not-a-string"
        ),
        pytest.param(
            api_pull_request(mergeCommit=MERGE_COMMIT_SHA), id="merge-commit-not-an-object"
        ),
        pytest.param(api_pull_request(mergeCommit={}), id="merge-commit-object-with-no-oid"),
        pytest.param(
            api_pull_request(mergeCommit={"oid": None}), id="merge-commit-oid-explicitly-null"
        ),
        pytest.param(
            api_pull_request(mergedAt=None, mergeCommit={"oid": MERGE_COMMIT_SHA}),
            id="unmerged-with-a-merge-commit",
        ),
    ],
)
def test_github_adapter_fails_loud_on_a_malformed_pull_request(payload: dict[str, object]) -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: json.dumps(payload),
    )

    with pytest.raises(ClaimError) as excinfo:
        client.landing(12)

    assert str(excinfo.value) == github.MALFORMED_PULL_REQUEST


def test_github_adapter_fails_loud_when_the_pull_request_payload_is_not_a_dict() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: json.dumps("not a pull request"),
    )

    with pytest.raises(ClaimError) as excinfo:
        client.landing(12)

    assert str(excinfo.value) == github.MALFORMED_PULL_REQUEST


def test_github_adapter_fails_loud_when_github_answers_with_more_than_one_pull_request() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: (
            f"{json.dumps(api_pull_request())}\n{json.dumps(api_pull_request())}"
        ),
    )

    with pytest.raises(ClaimError) as excinfo:
        client.landing(12)

    assert str(excinfo.value) == github.MALFORMED_PULL_REQUEST


def test_github_adapter_fails_loud_when_github_answers_with_no_pull_request() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY), run=lambda arguments, input_data=None: ""
    )

    with pytest.raises(ClaimError) as excinfo:
        client.landing(12)

    assert str(excinfo.value) == github.MALFORMED_PULL_REQUEST


def test_github_adapter_fails_loud_on_a_malformed_default_branch() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY), run=lambda arguments, input_data=None: "not a branch"
    )

    with pytest.raises(ClaimError, match="malformed default branch"):
        client.default_branch()


def test_github_adapter_refuses_a_default_branch_over_the_length_bound() -> None:
    overlong = "a" * 300
    client = GitHubForge(
        github._repository_id(REPOSITORY), run=lambda arguments, input_data=None: overlong
    )

    with pytest.raises(ClaimError, match="malformed default branch"):
        client.default_branch()


API_REPOSITORY_URL = f"https://api.github.com/repos/{REPOSITORY}"


def api_sub_issue(number: int, state: str) -> dict[str, object]:
    return {"number": number, "repository": API_REPOSITORY_URL, "state": state}


def sub_issue_client(*children: dict[str, object]) -> GitHubForge:
    return GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: "\n".join(json.dumps(child) for child in children),
    )


def test_github_adapter_reads_a_recorded_parent_and_its_children() -> None:
    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        if arguments[1].endswith("/parent"):
            return json.dumps(
                {"number": 79, "repository": API_REPOSITORY_URL, "body": "## Next\nCut."}
            )
        return "\n".join(json.dumps(api_sub_issue(number, "open")) for number in (72, 73))

    client = GitHubForge(github._repository_id(REPOSITORY), run=run)

    assert client.parent_issue(72) == board.ParentIssue(
        board.IssueReference(REPOSITORY, 79), "## Next\nCut."
    )
    assert client.list_children(79) == (
        board.ChildItem(72, board.ChildState.OPEN),
        board.ChildItem(73, board.ChildState.OPEN),
    )


def test_github_adapter_reports_a_closed_child_alongside_open_ones() -> None:
    client = sub_issue_client(api_sub_issue(72, "closed"), api_sub_issue(73, "open"))

    assert client.list_children(79) == (
        board.ChildItem(72, board.ChildState.CLOSED),
        board.ChildItem(73, board.ChildState.OPEN),
    )


@pytest.mark.parametrize(
    "child",
    [
        pytest.param({"number": 72, "repository": API_REPOSITORY_URL}, id="state-missing"),
        pytest.param(api_sub_issue(72, "archived"), id="state-unknown"),
    ],
)
def test_github_adapter_fails_loud_on_a_sub_issue_state_it_cannot_read(
    child: dict[str, object],
) -> None:
    client = sub_issue_client(child)

    with pytest.raises(ClaimError, match="malformed sub-issue"):
        client.list_children(79)


def test_github_adapter_refuses_a_sub_issue_from_another_repository() -> None:
    client = sub_issue_client(
        {"number": 72, "repository": "https://api.github.com/repos/other/repo", "state": "open"}
    )

    with pytest.raises(ClaimError, match="sub-issue from another repository"):
        client.list_children(79)


def test_github_adapter_fails_loud_when_a_sub_issue_is_not_an_object() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY), run=lambda arguments, input_data=None: "5"
    )

    with pytest.raises(ClaimError, match="malformed sub-issue"):
        client.list_children(79)


def test_github_adapter_reads_an_issue_without_a_parent_as_parentless() -> None:
    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        raise forge.ForgeNotFoundError("gh: No parent issue found (HTTP 404)")

    client = GitHubForge(github._repository_id(REPOSITORY), run=run)

    assert client.parent_issue(72) is None


def test_github_adapter_reads_the_parents_native_kind() -> None:
    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        return json.dumps(
            {
                "number": 79,
                "repository": API_REPOSITORY_URL,
                "body": "## Next\nCut.",
                "kind": "Container",
            }
        )

    client = GitHubForge(github._repository_id(REPOSITORY), run=run)

    assert client.parent_issue(72) == board.ParentIssue(
        board.IssueReference(REPOSITORY, 79), "## Next\nCut.", board.ItemKind.CONTAINER
    )


def test_github_adapter_fails_loud_on_a_malformed_parent_kind() -> None:
    def run(arguments: list[str], *, input_data: bytes | None = None) -> str:
        return json.dumps(
            {"number": 79, "repository": API_REPOSITORY_URL, "body": "## Next\nCut.", "kind": 5}
        )

    client = GitHubForge(github._repository_id(REPOSITORY), run=run)

    with pytest.raises(ClaimError, match="malformed parent issue"):
        client.parent_issue(72)


def test_github_adapter_fails_loud_when_the_parent_issue_response_is_not_one_object() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY), run=lambda arguments, input_data=None: json.dumps([])
    )

    with pytest.raises(ClaimError, match="malformed parent issue"):
        client.parent_issue(72)


def test_github_adapter_fails_loud_when_the_parent_issue_body_is_not_text() -> None:
    client = GitHubForge(
        github._repository_id(REPOSITORY),
        run=lambda arguments, input_data=None: json.dumps(
            {"number": 79, "repository": API_REPOSITORY_URL, "body": 5}
        ),
    )

    with pytest.raises(ClaimError, match="malformed parent issue"):
        client.parent_issue(72)


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"number": True}, id="number-is-a-bool"),
        pytest.param({"repository": "not-a-repository-url"}, id="repository-unparsable"),
    ],
)
def test_github_adapter_fails_loud_on_a_malformed_parent_reference(
    overrides: dict[str, object],
) -> None:
    value = {"number": 79, "repository": API_REPOSITORY_URL, "body": "## Next\nCut.", **overrides}
    client = GitHubForge(
        github._repository_id(REPOSITORY), run=lambda arguments, input_data=None: json.dumps(value)
    )

    with pytest.raises(ClaimError, match="malformed parent issue"):
        client.parent_issue(72)
