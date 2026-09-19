"""GitHub adapter for the forge port."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from typing import TypeVar, cast

from . import board, forge, process, protocol
from .protocol import REPOSITORY_PATTERN, ClaimError

_Page = TypeVar("_Page")
_T = TypeVar("_T")
TIMESTAMP_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")
# gh 2.45 colorizes --jq output when it believes stdout is a TTY.
ANSI_ESCAPE = re.compile(r"\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")
MAX_RECENT_MERGED_PULL_REQUESTS = 1000
# GitHub's merged-pull-request search accepts an exact-day filter, so a
# board's merged-pull-request date shards are independent, order-agnostic
# fetches. Walking them one `gh` subprocess at a time made shard count the
# dominant cost of a wide `board`/`next` read; fetched in parallel batches
# instead. This bounds how many `gh` subprocesses run at once, comfortably
# under GitHub's secondary rate limit for concurrent requests.
PARALLEL_FETCH_CONCURRENCY = 20
GH_TIMEOUT_SECONDS = 60
GH_QUIET_ENVIRONMENT = {
    "NO_COLOR": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
}
API_ISSUE_STATES: dict[str, board.BlockerState] = {
    "open": board.BlockerState.OPEN,
    "closed": board.BlockerState.CLOSED,
}
_ITEM_REFERENCE_STATES: dict[str, forge.ItemState] = {
    "open": forge.ItemState.OPEN,
    "closed": forge.ItemState.CLOSED,
}
# The organization's native issue types (decision record 0001 ruling D3):
# casefolded so an org's own casing of the type name never matters. An
# unrecognized type name maps to no kind at all -- never guessed from a
# label -- so a repository whose org renames a type loses that item's
# container/bug rules rather than silently misreading them.
_ISSUE_TYPE_KINDS: dict[str, board.ItemKind] = {
    "container": board.ItemKind.CONTAINER,
    "bug": board.ItemKind.BUG,
    "task": board.ItemKind.TASK,
    "feature": board.ItemKind.FEATURE,
}
# The write-side names GitHub's issue-type API expects (`cut`'s
# `create_child`) -- derived from the one read-side mapping above so the
# type name has a single owner, capitalized the way GitHub itself names them.
_ITEM_KIND_TYPE_NAMES: dict[board.ItemKind, str] = {
    kind: name.capitalize() for name, kind in _ISSUE_TYPE_KINDS.items()
}
# GitHub's issues-list pagination fills every page but the last, so a result
# strictly under this count could only have come from one request -- one live
# snapshot a concurrent open/close cannot have shifted an issue across.
ISSUES_PER_PAGE = 100
MALFORMED_PULL_REQUEST = "GitHub returned a malformed pull request"
# `HTTP 5xx` in #4.2's signal table: gh's combined output names the status
# code but never its class, so any 5xx is matched by digit rather than by an
# enumerated list of codes that would need to grow with the API.
_HTTP_SERVER_ERROR_PATTERN = re.compile(r"HTTP 5\d\d")
GITHUB_HOST = "github.com"
# Accepts both pinned remote forms, the SCP one included.
GITHUB_REMOTE_PATTERN = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$")


def github_command_environment() -> dict[str, str]:
    environment = os.environ.copy()
    environment.update(GH_QUIET_ENVIRONMENT)
    return environment


def _repository_id(text: str) -> forge.RepositoryId:
    if re.fullmatch(REPOSITORY_PATTERN, text) is None:
        raise ClaimError("repository must be OWNER/REPO")
    namespace, _, name = text.partition("/")
    return forge.RepositoryId(GITHUB_HOST, (namespace,), name)


def discover_repository(
    explicit: str | None, *, remote_url: Callable[[], str]
) -> forge.RepositoryId:
    """Resolve the repository `--repo` did not name.

    Reads the git remote first (issue #245): almost every checkout's remote
    already names its GitHub repository, and that read is a local `git
    config` lookup, not a network round trip -- so `gh repo view` (a real
    `gh` API call, `GH_TIMEOUT_SECONDS` long) only runs as a fallback, when
    the remote's own URL names no repository at all.
    """
    if explicit:
        return _repository_id(explicit)
    match = GITHUB_REMOTE_PATTERN.search(remote_url())
    if match is not None:
        return _repository_id(f"{match.group(1)}/{match.group(2)}")
    try:
        result = process.run_captured(
            ["gh", "repo", "view", "--json", "nameWithOwner", "--jq", ".nameWithOwner"],
            env=github_command_environment(),
            timeout=GH_TIMEOUT_SECONDS,
        )
    except process.ExecutableMissingError:
        raise ClaimError("gh is required for issue claims") from None
    except process.ProcessTimedOutError:
        raise ClaimError("gh timed out while resolving the repository") from None
    # `gh repo view`'s stdout alone -- a separate-stream result, so a stderr
    # warning can neither corrupt a good answer nor mask a real failure.
    cleaned = strip_ansi(result.stdout.decode("utf-8")).strip()
    if result.exit_status == 0 and cleaned:
        return _repository_id(cleaned)
    raise ClaimError("cannot resolve GitHub repository; pass --repo OWNER/REPO")


def _head_repository(pull_request: dict[str, object]) -> forge.RepositoryId | None:
    """The identity of the repository whose branch a pull request proposes,
    or None when GitHub does not name both halves — a fork deleted after the
    pull request opened, say.
    """
    repository = pull_request.get("headRepository")
    owner = pull_request.get("headRepositoryOwner")
    name = repository.get("name") if isinstance(repository, dict) else None
    login = owner.get("login") if isinstance(owner, dict) else None
    if not isinstance(name, str) or not isinstance(login, str):
        return None
    if re.fullmatch(REPOSITORY_PATTERN, f"{login}/{name}") is None:
        return None
    return forge.RepositoryId(GITHUB_HOST, (login,), name)


def strip_ansi(text: str) -> str:
    return ANSI_ESCAPE.sub("", text)


def _query_days(start: date, end: date) -> tuple[date, ...]:
    """One calendar UTC day per merged-pull-request query shard, `start` through `end` inclusive."""
    if end < start:
        raise ClaimError("merged pull request window ends before it starts")
    return tuple(start + timedelta(days=offset) for offset in range((end - start).days + 1))


def _decoded(result: process.BoundedResult, purpose: str) -> str:
    try:
        return strip_ansi(result.output.decode("utf-8")).strip()
    except UnicodeDecodeError as error:
        raise forge.ForgeMalformedResponseError(f"{purpose} returned non-UTF-8 output") from error


def _forge_failure(error: process.ProcessError, purpose: str) -> forge.ForgeError:
    """Translate a process failure that reached no forge response into a typed one.

    An isinstance chain, not a dict keyed by `type(error)`: only the chain lets
    each branch narrow `error` to the subtype that actually carries `.stage` and
    `.detail`, so the dispatch and the type stay one honest fact instead of two
    that could drift apart.
    """
    if isinstance(error, process.ProcessTimedOutError):
        return forge.ForgeTransientError(f"{purpose} timed out")
    if isinstance(error, process.ProcessIoFailedError):
        return forge.ForgeTransientError(
            f"{purpose} failed while {error.stage.value}: {error.detail}"
        )
    if isinstance(error, process.ProcessDidNotExitError):
        return forge.ForgeTransientError(f"{purpose} did not exit after closing its output")
    if isinstance(error, process.ProcessOutputTooLargeError):
        return forge.ForgeMalformedResponseError(f"{purpose} exceeded its output limit")
    raise AssertionError(f"unhandled process failure type: {type(error).__name__}")


def _is_transient_signal(decoded: str) -> bool:
    return (
        _HTTP_SERVER_ERROR_PATTERN.search(decoded) is not None
        or "connection reset" in decoded
        or "timeout" in decoded
    )


def _nonzero_exit_failure(decoded: str, return_code: int, purpose: str) -> forge.ForgeError:
    """Classify a nonzero `gh` exit from its decoded combined output (#4.2).

    `gh`'s own exit code never carries the HTTP status, so this reads the
    same prose a human would; the fallback stays an unclassified `ForgeError`
    rather than guessing at retry safety.
    """
    if "HTTP 404" in decoded:
        return forge.ForgeNotFoundError(decoded)
    if "HTTP 401" in decoded or "HTTP 403" in decoded:
        return forge.ForgePermissionDeniedError(decoded)
    if _is_transient_signal(decoded):
        return forge.ForgeTransientError(decoded)
    return forge.ForgeError(decoded or f"{purpose} failed with exit {return_code}")


def _bounded_command(command: list[str], *, purpose: str, input_data: bytes | None = None) -> str:
    try:
        result = process.run_bounded(
            command,
            input_data=input_data,
            env=github_command_environment(),
            timeout=GH_TIMEOUT_SECONDS,
        )
    except process.ExecutableMissingError as error:
        raise ClaimError(f"{error.executable} is required for issue claims") from error
    except process.ProcessStartFailedError as error:
        raise ClaimError(f"cannot start {purpose}: {error.detail}") from error
    except process.ProcessError as error:
        raise _forge_failure(error, purpose) from error
    decoded = _decoded(result, purpose)
    if result.exit_status != 0:
        raise _nonzero_exit_failure(decoded, result.exit_status, purpose)
    return decoded


def _require_mapping(value: object, message: str) -> dict[str, object]:
    """The object-shaped precondition every field read below shares: a `gh`
    payload that is not itself a JSON object cannot carry any named field."""
    if not isinstance(value, dict):
        raise forge.ForgeMalformedResponseError(message)
    return value


def _mapped_field(
    mapping: Mapping[str, object], name: str, table: Mapping[str, _T], message: str
) -> _T:
    """A field read through a lookup table -- GitHub's state strings onto this
    adapter's own enums -- malformed the moment the raw value is not a key
    the table recognizes."""
    raw = mapping.get(name)
    parsed = table.get(raw) if isinstance(raw, str) else None
    if parsed is None:
        raise forge.ForgeMalformedResponseError(message)
    return parsed


# The typed field decoder every `gh` JSON read in this adapter goes through
# (issue #312): one function per field shape, each raising the caller's one
# `message` on a type, pattern, or numeric mismatch. An optional variant
# delegates to its required counterpart once `None` is ruled out, so each
# shape's contract has one owner.
def _string(
    raw: object, message: str, *, pattern: re.Pattern[str] | None = None, non_empty: bool = False
) -> str:
    if (
        not isinstance(raw, str)
        or (pattern is not None and pattern.fullmatch(raw) is None)
        or (non_empty and not raw)
    ):
        raise forge.ForgeMalformedResponseError(message)
    return raw


def _string_field(
    mapping: Mapping[str, object],
    name: str,
    message: str,
    *,
    pattern: re.Pattern[str] | None = None,
    non_empty: bool = False,
) -> str:
    return _string(mapping.get(name), message, pattern=pattern, non_empty=non_empty)


def _optional_string_field(
    mapping: Mapping[str, object],
    name: str,
    message: str,
    *,
    pattern: re.Pattern[str] | None = None,
) -> str | None:
    if mapping.get(name) is None:
        return None
    return _string_field(mapping, name, message, pattern=pattern)


def _int_field(
    mapping: Mapping[str, object], name: str, message: str, *, minimum: int | None = None
) -> int:
    # `bool` is never accepted here -- it is an `int` subclass in Python, but
    # no field this adapter reads is boolean where its contract declares it
    # numeric.
    raw = mapping.get(name)
    if isinstance(raw, bool) or not isinstance(raw, int) or (minimum is not None and raw < minimum):
        raise forge.ForgeMalformedResponseError(message)
    return raw


def _optional_int_field(
    mapping: Mapping[str, object], name: str, message: str, *, minimum: int | None = None
) -> int | None:
    if mapping.get(name) is None:
        return None
    return _int_field(mapping, name, message, minimum=minimum)


def _bool_field(mapping: Mapping[str, object], name: str, message: str) -> bool:
    raw = mapping.get(name)
    if not isinstance(raw, bool):
        raise forge.ForgeMalformedResponseError(message)
    return raw


def _string_list_field(mapping: Mapping[str, object], name: str, message: str) -> tuple[str, ...]:
    raw = mapping.get(name)
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):
        raise forge.ForgeMalformedResponseError(message)
    return cast("tuple[str, ...]", tuple(raw))


_READ_ONLY_OPERATIONS = (
    forge.ForgeOperation.ITEM_REFERENCE,
    forge.ForgeOperation.LANDING,
    forge.ForgeOperation.PARENT_ISSUE,
    forge.ForgeOperation.LIST_CHILDREN,
    forge.ForgeOperation.DEFAULT_BRANCH,
    forge.ForgeOperation.LIST_OPEN_BOARD_ISSUES,
    forge.ForgeOperation.LIST_BOARD_DEPENDENCIES,
    forge.ForgeOperation.LIST_OPEN_BOARD_PULL_REQUESTS,
    forge.ForgeOperation.LIST_RECENT_MERGED_BOARD_PULL_REQUESTS,
)
_READ_WRITE_OPERATIONS = (
    forge.ForgeOperation.LINK_CHILD,
    forge.ForgeOperation.CREATE_CHILD,
    forge.ForgeOperation.UPDATE_ITEM_BODY,
)
# The GitHub adapter never refuses an operation: every member answers
# READ_ONLY or READ_WRITE, never UNSUPPORTED (decision record 0001 §2).
GITHUB_CAPABILITIES: Mapping[forge.ForgeOperation, forge.Capability] = MappingProxyType(
    {
        **dict.fromkeys(_READ_ONLY_OPERATIONS, forge.Capability.READ_ONLY),
        **dict.fromkeys(_READ_WRITE_OPERATIONS, forge.Capability.READ_WRITE),
    }
)


class GitHubForge:
    def __init__(
        self,
        repository: forge.RepositoryId,
        *,
        run: Callable[..., str] | None = None,
    ) -> None:
        self.repository = repository
        self._perform = run if run is not None else self._gh
        self.requests = 0
        self._requests_lock = threading.Lock()

    def _gh(self, arguments: list[str], *, input_data: bytes | None = None) -> str:
        return _bounded_command(
            ["gh", *arguments],
            purpose="GitHub issue coordination",
            input_data=input_data,
        )

    def _run(self, arguments: list[str], *, input_data: bytes | None = None) -> str:
        """The one chokepoint every board or claim read/write funnels through
        (issue #168): every one of this class's operations calls `self._run`,
        never `self._gh` or an injected `run` directly, so `requests` counts
        every round trip exactly once regardless of which operation asked for
        it. Locked because `board` fans reads out across worker threads
        (`cli._board`'s pools, and this adapter's own paginated/sharded
        fetches) that call `_run` concurrently -- an unlocked `+=` could lose
        an increment and under-count.

        Forwards `input_data` only when a caller actually passed one: tests
        across this suite inject `run=` callables shaped like `_gh` was
        called before this method existed, most of them taking no
        `input_data` keyword at all, and this preserves that call shape
        exactly rather than widening every fixture's signature for a
        counting concern they have nothing to do with.
        """
        with self._requests_lock:
            self.requests += 1
        if input_data is None:
            return self._perform(arguments)
        return self._perform(arguments, input_data=input_data)

    def capability(self, operation: forge.ForgeOperation) -> forge.Capability:
        return GITHUB_CAPABILITIES[operation]

    def item_reference(self, number: int) -> forge.ItemReference:
        try:
            raw = self._run(
                [
                    "api",
                    f"repos/{self.repository}/issues/{number}",
                    "--jq",
                    # The issues endpoint answers for a pull request too, and
                    # only its `pull_request` member tells the two apart.
                    '{state,title,body,is_landing:has("pull_request")}',
                ]
            )
        except forge.ForgeNotFoundError:
            return forge.ItemReference(forge.ItemState.MISSING)
        message = "GitHub returned a malformed issue reference"
        values = self._json_lines(raw, "issue reference")
        if len(values) != 1:
            raise forge.ForgeMalformedResponseError(message)
        mapping = _require_mapping(values[0], message)
        state = _mapped_field(mapping, "state", _ITEM_REFERENCE_STATES, message)
        title = _string_field(mapping, "title", message)
        body = _optional_string_field(mapping, "body", message) or ""
        is_landing = _bool_field(mapping, "is_landing", message)
        return forge.ItemReference(state, title, body, is_landing)

    def _json_lines(self, raw: str, description: str) -> tuple[object, ...]:
        """Parse compact NDJSON, pretty JSON, or a concatenated JSON sequence."""
        text = strip_ansi(raw).strip()
        if not text:
            return ()
        decoder = json.JSONDecoder()
        values: list[object] = []
        offset = 0
        length = len(text)
        try:
            while offset < length:
                # No "only whitespace remains" exit here: `text` is already
                # `.strip()`ped above, so its last character is never
                # whitespace -- this inner skip can never reach `length`
                # without first landing on a value to decode.
                while offset < length and text[offset].isspace():
                    offset += 1
                value, offset = decoder.raw_decode(text, offset)
                values.append(value)
        except json.JSONDecodeError as error:
            raise forge.ForgeMalformedResponseError(
                f"GitHub returned invalid {description} JSON"
            ) from error
        return tuple(values)

    def _fetch_pages(
        self, page: Callable[[int], tuple[_Page, ...]], *, per_page: int
    ) -> tuple[_Page, ...]:
        """Every page from `page` (1-indexed), the first fetched alone and the
        rest in concurrent batches of `PARALLEL_FETCH_CONCURRENCY`.

        A single-page listing (the common case for a small or fresh
        repository) costs exactly the one round trip it always did. A page
        past the last one returns an empty array rather than erroring, so
        once page 1 comes back full, a batch can ask for the next
        `PARALLEL_FETCH_CONCURRENCY` page numbers at once; the batch's last
        page coming back short of a full page is what ends the fetch, exactly
        as a single `gh api --paginate` call would stop, just without waiting
        for each page's round trip in turn.
        """
        first_page = page(1)
        if len(first_page) < per_page:
            return first_page
        pages: list[_Page] = list(first_page)
        start = 2
        while True:
            batch = range(start, start + PARALLEL_FETCH_CONCURRENCY)
            with ThreadPoolExecutor(max_workers=PARALLEL_FETCH_CONCURRENCY) as pool:
                fetched = list(pool.map(page, batch))
            for page_values in fetched:
                pages.extend(page_values)
            if len(fetched[-1]) < per_page:
                return tuple(pages)
            start += PARALLEL_FETCH_CONCURRENCY

    def _issue_kind(self, value: object) -> board.ItemKind | None:
        return _ISSUE_TYPE_KINDS.get(value.casefold()) if isinstance(value, str) else None

    @staticmethod
    def _valid_children_progress(closed: int | None, total: int | None) -> bool:
        """`childrenClosed`/`childrenTotal` (`sub_issues_summary`) must arrive
        both present or both absent -- `ContainerProgress` has no
        representation for "closed known, total unknown". `None` for both is
        preserved as `None`: `0/0` is a real container state, not a stand-in
        for "the forge said nothing". Each field's own type and
        non-negativity is already checked by `_optional_int_field`."""
        if closed is None and total is None:
            return True
        if closed is None or total is None:
            return False
        return closed <= total

    def _board_issue(self, value: object) -> board.Issue:
        message = "GitHub returned a malformed board issue"
        mapping = _require_mapping(value, message)
        number = _int_field(mapping, "number", message, minimum=1)
        title = _string_field(mapping, "title", message)
        labels = _string_list_field(mapping, "labels", message)
        body = _string_field(mapping, "body", message)
        created_at = _string_field(mapping, "createdAt", message, pattern=TIMESTAMP_PATTERN)
        updated_at = _string_field(mapping, "updatedAt", message, pattern=TIMESTAMP_PATTERN)
        kind_raw = _optional_string_field(mapping, "kind", message)
        children_closed = _optional_int_field(mapping, "childrenClosed", message, minimum=0)
        children_total = _optional_int_field(mapping, "childrenTotal", message, minimum=0)
        blocked_by_count = _int_field(mapping, "blockedByCount", message, minimum=0)
        if not self._valid_children_progress(children_closed, children_total):
            raise forge.ForgeMalformedResponseError(message)
        return board.Issue(
            number,
            title,
            labels,
            body,
            created_at,
            updated_at,
            self._issue_kind(kind_raw),
            children_closed,
            children_total,
            blocked_by_count,
        )

    def _board_pull_request(self, value: object) -> board.PullRequest:
        message = "GitHub returned a malformed board pull request"
        mapping = _require_mapping(value, message)
        number = _int_field(mapping, "number", message, minimum=1)
        title = _string_field(mapping, "title", message)
        body = _optional_string_field(mapping, "body", message) or ""
        head_ref_name = _string_field(mapping, "headRefName", message)
        merged_at = _optional_string_field(mapping, "mergedAt", message, pattern=TIMESTAMP_PATTERN)
        return board.PullRequest(number, title, body, head_ref_name, merged_at)

    def _landing(self, value: object) -> forge.Landing:
        message = MALFORMED_PULL_REQUEST
        mapping = _require_mapping(value, message)
        number = _int_field(mapping, "number", message, minimum=1)
        body = _optional_string_field(mapping, "body", message) or ""
        base_ref_name = _string_field(mapping, "baseRefName", message)
        head_ref_name = _string_field(mapping, "headRefName", message)
        source_repository = _head_repository(mapping)
        author = mapping.get("author")
        login_raw = author.get("login") if isinstance(author, dict) else None
        login = _string(login_raw, message, non_empty=True)
        merged_at = _optional_string_field(mapping, "mergedAt", message, pattern=TIMESTAMP_PATTERN)
        if source_repository is None:
            raise forge.ForgeMalformedResponseError(MALFORMED_PULL_REQUEST)
        return forge.Landing(
            number,
            login,
            body,
            source_repository,
            head_ref_name,
            base_ref_name,
            merged_at is not None,
        )

    def landing(self, number: int) -> forge.Landing:
        raw = self._run(
            [
                "pr",
                "view",
                str(number),
                "--repo",
                self.repository.path,
                "--json",
                "number,body,baseRefName,headRefName,headRepository,"
                "headRepositoryOwner,author,mergedAt",
                "--jq",
                ".",
            ]
        )
        values = self._json_lines(raw, "pull request")
        if len(values) != 1:
            raise forge.ForgeMalformedResponseError(MALFORMED_PULL_REQUEST)
        landing = self._landing(values[0])
        if landing.number != number:
            raise ClaimError(f"GitHub answered for pull request #{landing.number}, not #{number}")
        return landing

    def _issue_reference(self, value: object, description: str) -> board.IssueReference:
        message = f"GitHub returned a malformed {description}"
        mapping = _require_mapping(value, message)
        number = _int_field(mapping, "number", message, minimum=1)
        repository_url = mapping.get("repository")
        # A sub-issue/dependency payload names its repository by URL
        # (`.../repos/OWNER/REPO`), never by the bare name this adapter's
        # own records carry.
        derived = (
            repository_url.rpartition("/repos/")[2]
            if isinstance(repository_url, str)
            else repository_url
        )
        repository = _string(derived, message, pattern=REPOSITORY_PATTERN)
        return board.IssueReference(repository, number)

    def _issue_state(self, value: object, description: str) -> board.BlockerState:
        message = f"GitHub returned a malformed {description}"
        return _mapped_field(_require_mapping(value, message), "state", API_ISSUE_STATES, message)

    def parent_issue(self, number: int) -> board.ParentIssue | None:
        """The issue GitHub records as `number`'s parent, or None when it has none."""
        try:
            raw = self._run(
                [
                    "api",
                    f"repos/{self.repository}/issues/{number}/parent",
                    "--jq",
                    '{number,repository:.repository_url,body:(.body // ""),'
                    "kind:(.type.name // null)}",
                ]
            )
        except forge.ForgeNotFoundError:
            # The sub-issue endpoint answers "no parent" with an HTTP 404,
            # which the nonzero-exit classification (#4.2) reports as
            # `ForgeNotFoundError` -- that is an answer, not a failure.
            return None
        message = "GitHub returned a malformed parent issue"
        values = self._json_lines(raw, "parent issue")
        if len(values) != 1:
            raise forge.ForgeMalformedResponseError(message)
        value = values[0]
        mapping = _require_mapping(value, message)
        body = _string_field(mapping, "body", message)
        kind_raw = _optional_string_field(mapping, "kind", message)
        return board.ParentIssue(
            self._issue_reference(value, "parent issue"), body, self._issue_kind(kind_raw)
        )

    def list_children(self, number: int) -> tuple[board.ChildItem, ...]:
        """Every sub-issue GitHub records under `number`, open or closed.

        Every child's state is read here rather than filtered by `--jq`: a
        state this adapter does not understand would otherwise vanish and make
        a parent look childless, which is exactly the landing this check must
        refuse. A child recorded in another repository is refused outright --
        `board.ChildItem` has no field to hold that fact honestly, and
        containers and their children are same-repository only, for now.
        """
        raw = self._run(
            [
                "api",
                "--paginate",
                f"repos/{self.repository}/issues/{number}/sub_issues?per_page=100",
                "--jq",
                ".[] | {number,repository:.repository_url,state,type:(.type.name // null)}",
            ]
        )
        children: list[board.ChildItem] = []
        for value in self._json_lines(raw, "sub-issue"):
            reference = self._issue_reference(value, "sub-issue")
            if reference.repository != self.repository.path:
                raise forge.ForgeMalformedResponseError(
                    "GitHub returned a sub-issue from another repository"
                )
            state = self._issue_state(value, "sub-issue")
            children.append(board.ChildItem(reference.number, board.ChildState(state.value)))
        return tuple(children)

    def default_branch(self) -> str:
        branch = self._run(["api", f"repos/{self.repository}", "--jq", ".default_branch"])
        if protocol.BRANCH_PATTERN.fullmatch(branch) is None:
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed default branch")
        return branch

    def _open_issue_page(self, page: int) -> tuple[object, ...]:
        raw = self._run(
            [
                "api",
                f"repos/{self.repository}/issues?state=open&per_page={ISSUES_PER_PAGE}&page={page}",
                "--jq",
                (
                    # No `select` here (unlike the old single `--paginate` call):
                    # a page must report its true raw item count so a short page
                    # still correctly signals "no more pages" even when some of
                    # its items are pull requests, filtered out below instead.
                    '.[] | {number,title,labels:(.labels | map(.name)),body:(.body // ""),'
                    "createdAt:.created_at,updatedAt:.updated_at,"
                    'isPullRequest:has("pull_request"),'
                    "kind:(.type.name // null),"
                    "childrenClosed:(.sub_issues_summary.completed // null),"
                    "childrenTotal:(.sub_issues_summary.total // null),"
                    "blockedByCount:(.issue_dependencies_summary.total_blocked_by // 0)}"
                ),
            ]
        )
        return self._json_lines(raw, "board issue")

    def list_open_board_issues(self) -> tuple[board.Issue, ...]:
        values = self._fetch_pages(self._open_issue_page, per_page=ISSUES_PER_PAGE)
        return tuple(
            self._board_issue(value)
            for value in values
            if not (isinstance(value, dict) and value.get("isPullRequest"))
        )

    MALFORMED_BOARD_DEPENDENCY = "GitHub returned a malformed board blocked-by dependency"

    def _board_dependency(self, value: object) -> board.IssueDependency:
        message = self.MALFORMED_BOARD_DEPENDENCY
        mapping = _require_mapping(value, message)
        number = _int_field(mapping, "number", message, minimum=1)
        blocker_state = _mapped_field(mapping, "state", API_ISSUE_STATES, message)
        closed_at = _optional_string_field(mapping, "closedAt", message, pattern=TIMESTAMP_PATTERN)
        repository = _string_field(mapping, "repository", message, pattern=REPOSITORY_PATTERN)
        is_pull_request = _bool_field(mapping, "isPullRequest", message)
        if blocker_state is board.BlockerState.CLOSED and closed_at is None:
            raise forge.ForgeMalformedResponseError(message)
        parsed_closed_at = None
        if closed_at is not None:
            try:
                parsed_closed_at = datetime.fromisoformat(closed_at).astimezone(UTC)
            except ValueError as error:
                raise forge.ForgeMalformedResponseError(message) from error
        return board.IssueDependency(
            board.IssueReference(repository, number),
            blocker_state,
            is_pull_request,
            parsed_closed_at,
        )

    def list_board_dependencies(self, number: int) -> tuple[board.IssueDependency, ...]:
        raw = self._run(
            [
                "api",
                "--paginate",
                f"repos/{self.repository}/issues/{number}/dependencies/blocked_by"
                f"?per_page={ISSUES_PER_PAGE}",
                "--jq",
                ".[] | {number,state,closedAt:.closed_at,repository:.repository.full_name,"
                'isPullRequest:has("pull_request")}',
            ]
        )
        return tuple(
            self._board_dependency(value) for value in self._json_lines(raw, "board dependency")
        )

    def list_open_board_pull_requests(self) -> tuple[board.PullRequest, ...]:
        raw = self._run(
            [
                "pr",
                "list",
                "--repo",
                self.repository.path,
                "--state",
                "open",
                "--limit",
                "1000",
                "--json",
                "number,title,body,headRefName",
                "--jq",
                ".[]",
            ]
        )
        return tuple(
            self._board_pull_request(value)
            for value in self._json_lines(raw, "open board pull request")
        )

    def _merged_pull_requests_for_day(self, day: date) -> tuple[board.PullRequest, ...]:
        raw = self._run(
            [
                "pr",
                "list",
                "--repo",
                self.repository.path,
                "--state",
                "merged",
                "--search",
                f"merged:{day.isoformat()}",
                "--limit",
                str(MAX_RECENT_MERGED_PULL_REQUESTS),
                "--json",
                "number,title,body,headRefName,mergedAt",
                "--jq",
                ".[]",
            ]
        )
        return tuple(
            self._board_pull_request(value)
            for value in self._json_lines(raw, "merged board pull request")
        )

    def list_recent_merged_board_pull_requests(
        self, since: datetime
    ) -> tuple[board.PullRequest, ...]:
        cutoff = since.astimezone(UTC)
        days = _query_days(cutoff.date(), datetime.now(UTC).date())
        with ThreadPoolExecutor(max_workers=min(len(days), PARALLEL_FETCH_CONCURRENCY)) as pool:
            shards = list(pool.map(self._merged_pull_requests_for_day, days))
        # GitHub's search date qualifier is an exact UTC day, so slicing the
        # window this way turns one query that walks `since` to today through
        # GraphQL cursor pagination (measured ~4-9s for a three-week, ~630-PR
        # window) into independent single-page requests fetched in parallel
        # (~1-2s for the same window). A day whose own shard fills its limit
        # is now the only way a merged pull request can go missing (the old
        # single query's cap instead truncated the *whole* window), so that is
        # what the residual warning below watches for.
        saturated_days = tuple(
            day
            for day, shard in zip(days, shards, strict=True)
            if len(shard) >= MAX_RECENT_MERGED_PULL_REQUESTS
        )
        if saturated_days:
            print(
                "WARNING: merged pull request history is capped at "
                f"{MAX_RECENT_MERGED_PULL_REQUESTS} results for "
                f"{', '.join(day.isoformat() for day in saturated_days)}; "
                "an older landing that day could be missing from a board/next stage",
                file=sys.stderr,
            )
        recent: list[board.PullRequest] = []
        for pull_request in (pr for shard in shards for pr in shard):
            if pull_request.merged_at is None:
                continue
            try:
                merged_at = datetime.fromisoformat(pull_request.merged_at)
            except ValueError as error:
                raise forge.ForgeMalformedResponseError(
                    "GitHub returned a malformed merged board pull request"
                ) from error
            if merged_at >= cutoff:
                recent.append(pull_request)
        return tuple(recent)

    def _create_issue(self, *, title: str, body: str, kind: board.ItemKind) -> int:
        """Create a fresh issue of `kind`, linked to no parent.

        Private: `create_child` is the only caller (#260) -- nothing else
        in the package needs an issue with no parent, so this is not a
        port operation.

        Validates the same response shape `create_child` depends on --
        `id` alongside `number` -- even though only the number is returned
        here: GitHub always sends both, and a caller that later runs
        `link_child` against this issue needs that id to already be
        trustworthy rather than fail out of place.
        """
        raw = self._run(
            ["api", "--method", "POST", f"repos/{self.repository}/issues", "--input", "-"],
            input_data=json.dumps(
                {"title": title, "body": body, "type": _ITEM_KIND_TYPE_NAMES[kind]}
            ).encode("utf-8"),
        )
        try:
            created = json.loads(raw)
        except json.JSONDecodeError as error:
            raise forge.ForgeMalformedResponseError(
                "GitHub returned invalid created-issue JSON"
            ) from error
        message = "GitHub did not return a created issue"
        mapping = _require_mapping(created, message)
        _int_field(mapping, "id", message, minimum=1)
        return _int_field(mapping, "number", message, minimum=1)

    def _issue_identifier(self, number: int) -> int:
        """`number`'s internal id, which the sub-issue POST needs and the
        issue-number-only port surface never otherwise carries."""
        raw = self._run(["api", f"repos/{self.repository}/issues/{number}", "--jq", ".id"])
        try:
            identifier = int(strip_ansi(raw).strip())
        except ValueError as error:
            raise forge.ForgeMalformedResponseError(
                "GitHub returned a malformed issue id"
            ) from error
        if identifier < 1:
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed issue id")
        return identifier

    def link_child(self, parent: int, child: int) -> None:
        """Record already-existing issue `child` as `parent`'s sub-issue --
        the write a repeat `cut` uses to adopt an orphan a failed
        `create_child` left behind (#260), instead of creating a second
        issue. GitHub's sub-issue POST wants `child`'s internal id, not its
        issue number, so this reads it first.
        """
        identifier = self._issue_identifier(child)
        self._run(
            [
                "api",
                "--method",
                "POST",
                f"repos/{self.repository}/issues/{parent}/sub_issues",
                "--input",
                "-",
            ],
            input_data=json.dumps({"sub_issue_id": identifier}).encode("utf-8"),
        )

    def create_child(self, *, parent: int, title: str, body: str, kind: board.ItemKind) -> int:
        """Create a fresh issue of `kind` and record it as `parent`'s sub-issue.

        Composed from `_create_issue` and `link_child` (#260): not atomic,
        since GitHub has no transaction across the two writes. A failure in
        the relation POST raises `forge.ForgePartialChildCreationError`
        naming the child that already exists; safe to retry the same `cut`,
        since it then finds this child orphaned -- open, no recorded parent
        -- and adopts it with `link_child` rather than creating a second one.
        """
        child = self._create_issue(title=title, body=body, kind=kind)
        try:
            self.link_child(parent, child)
        except protocol.ClaimError as error:
            raise forge.ForgePartialChildCreationError(
                child=child,
                parent=parent,
                step=f"record #{child} as a sub-issue of #{parent}",
                cause=error,
            ) from error
        return child

    def update_item_body(self, number: int, body: str) -> None:
        self._run(
            [
                "api",
                "--method",
                "PATCH",
                f"repos/{self.repository}/issues/{number}",
                "--input",
                "-",
            ],
            input_data=json.dumps({"body": body}).encode("utf-8"),
        )
