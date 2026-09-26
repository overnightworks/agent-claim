"""GitHub adapter for the forge port."""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from collections.abc import Callable, Iterable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType
from typing import TypeVar

from . import board, forge, process, protocol
from .body import ItemKind
from .protocol import REPOSITORY_PATTERN, ClaimError

_Page = TypeVar("_Page")
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
# GraphQL aliases every number into one query field, so a block this size
# stays one round trip; GitHub's own guidance keeps a query's alias count in
# the 50-100 range rather than one huge query per repository (issue #440).
GRAPHQL_ITEM_REFERENCE_BATCH_SIZE = 100
# `Repository.issueOrPullRequest`'s own `state` enum spans both `Issue`
# (`OPEN`/`CLOSED`) and `PullRequest` (`OPEN`/`CLOSED`/`MERGED`); `forge.
# ItemReference.state` only ever distinguishes open from not, matching the
# REST issues endpoint `item_reference` reads a single number through, so a
# merged pull request reads exactly like a closed one here too.
_GRAPHQL_ITEM_STATES: dict[str, forge.ItemState] = {
    "OPEN": forge.ItemState.OPEN,
    "CLOSED": forge.ItemState.CLOSED,
    "MERGED": forge.ItemState.CLOSED,
}
_GRAPHQL_ITEM_TYPENAMES = frozenset({"Issue", "PullRequest"})
# GraphQL's own schema: `Issue.state` is `OPEN`/`CLOSED` only -- `MERGED`
# exists solely on `PullRequestState`, so an `Issue` node claiming `MERGED`
# is impossible on the wire and a malformed response, never a state to
# normalize (issue #440 review).
_GRAPHQL_ITEM_STATES_BY_TYPENAME: dict[str, frozenset[str]] = {
    "Issue": frozenset({"OPEN", "CLOSED"}),
    "PullRequest": frozenset({"OPEN", "CLOSED", "MERGED"}),
}
_MALFORMED_BATCHED_ITEM_REFERENCE = "GitHub returned a malformed batched item reference"
GH_TIMEOUT_SECONDS = 60
GH_QUIET_ENVIRONMENT = {
    "NO_COLOR": "1",
    "GH_NO_UPDATE_NOTIFIER": "1",
}
API_ISSUE_STATES: dict[str, board.BlockerState] = {
    "open": board.BlockerState.OPEN,
    "closed": board.BlockerState.CLOSED,
}
# The organization's native issue types (decision record 0001 ruling D3):
# casefolded so an org's own casing of the type name never matters. An
# unrecognized type name maps to no kind at all -- never guessed from a
# label -- so a repository whose org renames a type loses that item's
# container/bug rules rather than silently misreading them.
_ISSUE_TYPE_KINDS: dict[str, ItemKind] = {
    "container": ItemKind.CONTAINER,
    "bug": ItemKind.BUG,
    "task": ItemKind.TASK,
    "feature": ItemKind.FEATURE,
}
# The write-side names GitHub's issue-type API expects (`cut`'s
# `create_child`) -- derived from the one read-side mapping above so the
# type name has a single owner, capitalized the way GitHub itself names them.
_ITEM_KIND_TYPE_NAMES: dict[ItemKind, str] = {
    kind: name.capitalize() for name, kind in _ISSUE_TYPE_KINDS.items()
}
# GitHub's issues-list pagination fills every page but the last, so a result
# strictly under this count could only have come from one request -- one live
# snapshot a concurrent open/close cannot have shifted an issue across.
ISSUES_PER_PAGE = 100
MALFORMED_PULL_REQUEST = "GitHub returned a malformed pull request"
# The combined-status endpoint's own aggregate `state` can be `pending`,
# `failure`, or `error` with a `statuses` page that, this instant, names no
# context at all -- a status posted after this read, say (issue #405
# review/gate finding). A refusal still needs one name to print, so an
# unnamed non-passing verdict prints this stand-in rather than reading as
# "no checks" and passing silently.
EXTERNAL_STATUS_FALLBACK_NAME = "external status checks"
# `HTTP 5xx` in #4.2's signal table: gh's combined output names the status
# code but never its class, so any 5xx is matched by digit rather than by an
# enumerated list of codes that would need to grow with the API.
_HTTP_SERVER_ERROR_PATTERN = re.compile(r"HTTP 5\d\d")
# `merge_landing`'s own conflict signal (issue #405): GitHub answers a
# pinned merge whose `sha` no longer names the pull request's real head with
# HTTP 405 (closed/not mergeable) or 409 (head moved) -- neither is a 4xx
# `_nonzero_exit_failure` above already classifies, so `merge_landing`
# matches this pattern itself and raises `ForgeMergeConflictError`.
_MERGE_CONFLICT_PATTERN = re.compile(r"HTTP 40[59]")


def _branch_already_absent(error_text: str) -> bool:
    """`delete_branch`'s own idempotent-absence signal (issue #405 review
    finding; S8786): `gh api`'s own error text puts the message before the
    code -- "Reference does not exist (HTTP 422)" -- so this checks both
    substrings independent of order, rather than the two-lookahead regex
    that made an order-agnostic match super-linear to backtrack; any other
    422 (a protected branch, a malformed ref name) is a real failure this
    adapter must still surface."""
    lowered = error_text.casefold()
    return "reference does not exist" in lowered and "http 422" in lowered


GITHUB_HOST = "github.com"
# Accepts both pinned remote forms, the SCP one included.
GITHUB_REMOTE_PATTERN = re.compile(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?$")


def landing_comment(pull_request: int) -> str:
    """The one comment body `close_landed_item` posts naming the pull
    request that landed an item (issue #359 Card 1) -- named once so a
    test can assert the exact text without a second copy of it."""
    return f"landed by PR #{pull_request}"


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


_ITEM_REFERENCE_FIELDS = (
    "__typename ... on Issue { state title body } ... on PullRequest { state title body }"
)


def _item_reference_query(numbers: Sequence[int]) -> str:
    """One GraphQL query reading every one of `numbers` through its own
    alias (issue #440): `issueOrPullRequest` -- unlike the narrower `issue`
    field -- answers for a pull request number too, matching `item_reference`'s
    own REST read of a single number. GraphQL's own schema answers `null` in
    `data` for a number that exists in neither space, but `gh api graphql`
    still exits nonzero whenever any alias produced a `NOT_FOUND` entry in
    `errors` -- `_item_reference_block`/`_not_found_batch_nodes` read that
    exit back as the partial success it actually carries."""
    aliases = "\n".join(
        f"n{index}: issueOrPullRequest(number: {number}) {{ {_ITEM_REFERENCE_FIELDS} }}"
        for index, number in enumerate(numbers)
    )
    return (
        "query($owner: String!, $name: String!) { "
        f"repository(owner: $owner, name: $name) {{ {aliases} }} }}"
    )


def _parsed_item_reference_node(node: object) -> forge.ItemReference:
    """One alias's own value from `_item_reference_query`'s response: `None`
    for a number GitHub could resolve to neither an issue nor a pull request
    (`item_reference`'s own `ForgeNotFoundError` case), a malformed shape
    raised loud, never guessed at."""
    if node is None:
        return forge.ItemReference(forge.ItemState.MISSING)
    if not isinstance(node, dict):
        raise forge.ForgeMalformedResponseError(_MALFORMED_BATCHED_ITEM_REFERENCE)
    typename = node.get("__typename")
    state = node.get("state")
    title = node.get("title")
    body = node.get("body")
    if (
        typename not in _GRAPHQL_ITEM_TYPENAMES
        or state not in _GRAPHQL_ITEM_STATES
        or state not in _GRAPHQL_ITEM_STATES_BY_TYPENAME.get(typename, frozenset())
        or not isinstance(title, str)
        or (body is not None and not isinstance(body, str))
    ):
        raise forge.ForgeMalformedResponseError(_MALFORMED_BATCHED_ITEM_REFERENCE)
    return forge.ItemReference(
        _GRAPHQL_ITEM_STATES[state], title, body or "", typename == "PullRequest"
    )


def _aliased_node(nodes: Mapping[str, object], index: int) -> object:
    """One alias's own raw node out of a batch's `nodes` mapping (issue #440
    review): an omitted key is a malformed response -- GitHub always answers
    every alias a query names, `null` included for one it cannot resolve --
    never silently read the same as that explicit `null` (`_parsed_item_
    reference_node`'s own `MISSING` case)."""
    alias = f"n{index}"
    if alias not in nodes:
        raise forge.ForgeMalformedResponseError(_MALFORMED_BATCHED_ITEM_REFERENCE)
    return nodes[alias]


def _item_references_from_nodes(
    nodes: Mapping[str, object], numbers: Sequence[int]
) -> dict[int, forge.ItemReference]:
    """`_item_reference_block`'s own mapping from a batch response's raw
    `nodes` to every requested number's parsed reference -- the one shape
    both a clean response and a recovered `_not_found_batch_nodes` response
    are read through."""
    return {
        number: _parsed_item_reference_node(_aliased_node(nodes, index))
        for index, number in enumerate(numbers)
    }


# A NOT_FOUND error's own `path` (issue #440 review): `["repository", "nN"]`,
# the field GraphQL walked to reach the alias it could not resolve.
_ITEM_REFERENCE_ERROR_PATH_LENGTH = 2


def _is_recovered_not_found_error(
    error: object, aliases: frozenset[str], repository: Mapping[str, object]
) -> bool:
    """Whether `error` is one `_not_found_batch_nodes` can recover: `NOT_
    FOUND` on one of this call's own aliases, at the alias `repository`
    already answers `null` for."""
    if not isinstance(error, dict) or error.get("type") != "NOT_FOUND":
        return False
    path = error.get("path")
    if not isinstance(path, list) or len(path) != _ITEM_REFERENCE_ERROR_PATH_LENGTH:
        return False
    section, alias = path
    return section == "repository" and alias in aliases and repository.get(alias) is None


def _not_found_batch_nodes(
    message: str, numbers: Sequence[int]
) -> Mapping[str, object] | None:
    """`_item_reference_block`'s own recovery (issue #440 review): `gh api
    graphql` exits nonzero whenever any alias in `_item_reference_query`
    resolved to neither an issue nor a pull request, even though GraphQL's
    own response already answers `null` for it in `data` rather than ending
    the query. `_bounded_command` has no way to tell that apart from a real
    failure, so it folds `message` through as an unclassified `ForgeError`;
    this reads `message` back as the GraphQL response it actually is.

    Returns the batch's `nodes` mapping -- ready for `_item_references_from_
    nodes`, `null` exactly at every not-found alias -- only when `message`
    parses as JSON carrying that response shape, every one of `errors` is a
    `NOT_FOUND` on one of this call's own aliases (`_is_recovered_not_found_
    error`), and `data.repository` already holds every alias this call asked
    for. Any other shape returns `None` so the caller re-raises its original
    error instead of guessing at recovery.
    """
    try:
        decoded, _ = json.JSONDecoder().raw_decode(message)
    except json.JSONDecodeError:
        return None
    if not isinstance(decoded, dict):
        return None
    data = decoded.get("data")
    repository = data.get("repository") if isinstance(data, dict) else None
    errors = decoded.get("errors")
    if not isinstance(repository, dict) or not isinstance(errors, list) or not errors:
        return None
    aliases = frozenset(f"n{index}" for index in range(len(numbers)))
    if not aliases.issubset(repository):
        return None
    recovered = all(
        _is_recovered_not_found_error(error, aliases, repository) for error in errors
    )
    return repository if recovered else None


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


_READ_ONLY_OPERATIONS = (
    forge.ForgeOperation.ITEM_REFERENCE,
    forge.ForgeOperation.ITEM_REFERENCES,
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
        values = self._json_lines(raw, "issue reference")
        if len(values) != 1 or not isinstance(values[0], dict):
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed issue reference")
        value = values[0]
        state = value.get("state")
        title = value.get("title")
        body = value.get("body")
        is_landing = value.get("is_landing")
        if (
            state not in {"open", "closed"}
            or not isinstance(title, str)
            or (body is not None and not isinstance(body, str))
            or not isinstance(is_landing, bool)
        ):
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed issue reference")
        return forge.ItemReference(
            forge.ItemState.OPEN if state == "open" else forge.ItemState.CLOSED,
            title,
            body or "",
            is_landing,
        )

    def _item_reference_block(self, numbers: tuple[int, ...]) -> dict[int, forge.ItemReference]:
        """One `numbers`-sized GraphQL round trip (issue #440): `item_
        references`' own block, never called with more than `GRAPHQL_ITEM_
        REFERENCE_BATCH_SIZE` numbers. A number that resolves to neither an
        issue nor a pull request makes `gh` exit nonzero even though GraphQL
        itself already answered the rest -- `_not_found_batch_nodes` reads
        that failure back as the partial success it is (issue #440 review)
        rather than this call failing the whole block loud."""
        try:
            raw = self._run(
                [
                    "api",
                    "graphql",
                    "-f",
                    f"query={_item_reference_query(numbers)}",
                    "-f",
                    f"owner={self.repository.namespace[0]}",
                    "-f",
                    f"name={self.repository.name}",
                    "--jq",
                    ".data.repository",
                ]
            )
        except forge.ForgeError as error:
            nodes = _not_found_batch_nodes(str(error), numbers)
            if nodes is None:
                raise
            return _item_references_from_nodes(nodes, numbers)
        values = self._json_lines(raw, "batched item reference")
        if len(values) != 1 or not isinstance(values[0], dict):
            raise forge.ForgeMalformedResponseError(_MALFORMED_BATCHED_ITEM_REFERENCE)
        return _item_references_from_nodes(values[0], numbers)

    def item_references(self, numbers: Iterable[int]) -> Mapping[int, forge.ItemReference]:
        """Every one of `numbers`, batched into GraphQL blocks of
        `GRAPHQL_ITEM_REFERENCE_BATCH_SIZE` (issue #440): the 32-call, fully
        serial `gh api .../issues/N` walk `cli._closed_item_sizes` used to pay
        (12.8 of an 18s board build) collapses to one round trip for any
        repository whose closed-item history still fits one block, and stays
        flat as that history grows -- a repository large enough to need more
        than one block fetches them concurrently, capped the same way every
        other sharded board read already is (`PARALLEL_FETCH_CONCURRENCY`)."""
        ordered = tuple(dict.fromkeys(numbers))
        if not ordered:
            return {}
        blocks = tuple(
            ordered[start : start + GRAPHQL_ITEM_REFERENCE_BATCH_SIZE]
            for start in range(0, len(ordered), GRAPHQL_ITEM_REFERENCE_BATCH_SIZE)
        )
        with ThreadPoolExecutor(max_workers=min(len(blocks), PARALLEL_FETCH_CONCURRENCY)) as pool:
            results = list(pool.map(self._item_reference_block, blocks))
        references: dict[int, forge.ItemReference] = {}
        for result in results:
            references.update(result)
        return references

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

    def _issue_kind(self, value: object) -> ItemKind | None:
        return _ISSUE_TYPE_KINDS.get(value.casefold()) if isinstance(value, str) else None

    def _valid_children_progress(self, closed: object, total: object) -> bool:
        """`childrenClosed`/`childrenTotal` (`sub_issues_summary`) must arrive
        both present or both absent -- `ContainerProgress` has no
        representation for "closed known, total unknown", and inventing one
        would let the board show a progress figure the forge never sent.
        `None` for both is preserved as `None`: `0/0` is a real container
        state, never a stand-in for "the forge said nothing"."""
        if closed is None and total is None:
            return True
        if closed is None or total is None:
            return False
        if isinstance(closed, bool) or not isinstance(closed, int) or closed < 0:
            return False
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            return False
        return closed <= total

    def _board_issue(self, value: object) -> board.Issue:
        if not isinstance(value, dict):
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed board issue")
        number = value.get("number")
        title = value.get("title")
        labels = value.get("labels")
        body = value.get("body")
        created_at = value.get("createdAt")
        updated_at = value.get("updatedAt")
        kind_raw = value.get("kind")
        children_closed = value.get("childrenClosed")
        children_total = value.get("childrenTotal")
        blocked_by_count = value.get("blockedByCount")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or not isinstance(title, str)
            or not isinstance(labels, list)
            or not all(isinstance(label, str) for label in labels)
            or not isinstance(body, str)
            or not isinstance(created_at, str)
            or protocol.RFC3339_TIMESTAMP_PATTERN.fullmatch(created_at) is None
            or not isinstance(updated_at, str)
            or protocol.RFC3339_TIMESTAMP_PATTERN.fullmatch(updated_at) is None
            or (kind_raw is not None and not isinstance(kind_raw, str))
            or not self._valid_children_progress(children_closed, children_total)
            or isinstance(blocked_by_count, bool)
            or not isinstance(blocked_by_count, int)
            or blocked_by_count < 0
        ):
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed board issue")
        return board.Issue(
            number,
            title,
            tuple(labels),
            body,
            created_at,
            updated_at,
            self._issue_kind(kind_raw),
            children_closed,
            children_total,
            blocked_by_count,
        )

    def _board_pull_request(self, value: object) -> board.PullRequest:
        if not isinstance(value, dict):
            raise forge.ForgeMalformedResponseError(
                "GitHub returned a malformed board pull request"
            )
        number = value.get("number")
        title = value.get("title")
        body = value.get("body")
        if body is None:
            body = ""
        head_ref_name = value.get("headRefName")
        merged_at = value.get("mergedAt")
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or not isinstance(title, str)
            or not isinstance(body, str)
            or not isinstance(head_ref_name, str)
            or (merged_at is not None and not isinstance(merged_at, str))
            or (
                isinstance(merged_at, str)
                and protocol.RFC3339_TIMESTAMP_PATTERN.fullmatch(merged_at) is None
            )
        ):
            raise forge.ForgeMalformedResponseError(
                "GitHub returned a malformed board pull request"
            )
        return board.PullRequest(number, title, body, head_ref_name, merged_at)

    def _landing(self, value: object) -> forge.Landing:
        if not isinstance(value, dict):
            raise forge.ForgeMalformedResponseError(MALFORMED_PULL_REQUEST)
        number = value.get("number")
        body = value.get("body")
        if body is None:
            body = ""
        base_ref_name = value.get("baseRefName")
        head_ref_name = value.get("headRefName")
        source_repository = _head_repository(value)
        author = value.get("author")
        login = author.get("login") if isinstance(author, dict) else None
        merged_at = value.get("mergedAt")
        merged = merged_at is not None
        merge_commit_field = value.get("mergeCommit")
        if merge_commit_field is None:
            merge_commit = None
        elif isinstance(merge_commit_field, dict):
            oid = merge_commit_field.get("oid")
            if not isinstance(oid, str):
                raise forge.ForgeMalformedResponseError(MALFORMED_PULL_REQUEST)
            merge_commit = oid
        else:
            raise forge.ForgeMalformedResponseError(MALFORMED_PULL_REQUEST)
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or not isinstance(body, str)
            or not isinstance(base_ref_name, str)
            or not isinstance(head_ref_name, str)
            or source_repository is None
            or not isinstance(login, str)
            or not login
            or (merged_at is not None and not isinstance(merged_at, str))
            or (
                isinstance(merged_at, str)
                and protocol.RFC3339_TIMESTAMP_PATTERN.fullmatch(merged_at) is None
            )
            or (merged and protocol.COMMIT_PATTERN.fullmatch(merge_commit or "") is None)
            or (not merged and merge_commit is not None)
        ):
            raise forge.ForgeMalformedResponseError(MALFORMED_PULL_REQUEST)
        return forge.Landing(
            number,
            login,
            body,
            source_repository,
            head_ref_name,
            base_ref_name,
            merged,
            merge_commit,
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
                "headRepositoryOwner,author,mergedAt,mergeCommit",
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

    def _check_run(self, value: object) -> forge.CheckRun:
        if not isinstance(value, dict):
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed check run")
        name = value.get("name")
        conclusion = value.get("conclusion")
        if (
            not isinstance(name, str)
            or not name
            or (conclusion is not None and (not isinstance(conclusion, str) or not conclusion))
        ):
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed check run")
        return forge.CheckRun(name, conclusion)

    def _check_run_page(self, sha: str, page: int) -> tuple[object, ...]:
        raw = self._run(
            [
                "api",
                f"repos/{self.repository}/commits/{sha}/check-runs"
                f"?per_page={ISSUES_PER_PAGE}&page={page}",
                "--jq",
                '.check_runs[] | {name,conclusion:(if .status == "completed" '
                "then .conclusion else null end)}",
            ]
        )
        return self._json_lines(raw, "check run")

    def _check_runs(self, sha: str) -> tuple[forge.CheckRun, ...]:
        """Every check run against `sha`, not merely its first page (issue
        #405 review finding): GitHub's own `check-runs` listing paginates
        like every other collection this adapter reads, so a run reported
        only on a later page must count toward readiness exactly as one on
        the first page does."""
        values = self._fetch_pages(
            lambda page: self._check_run_page(sha, page), per_page=ISSUES_PER_PAGE
        )
        return tuple(self._check_run(value) for value in values)

    def _combined_status_summary(self, sha: str) -> tuple[str, int]:
        """`sha`'s combined-status verdict and true total (issue #405
        review/gate finding): GitHub computes `state` -- `success`,
        `pending`, `failure`, or `error` -- as the aggregate over every
        status context on `sha`, and `total_count` as their true count,
        both independent of pagination; one cheap call answers both,
        without ever paging `statuses` for a verdict a per-context
        reconstruction could get wrong."""
        raw = self._run(
            [
                "api",
                f"repos/{self.repository}/commits/{sha}/status",
                "--jq",
                "{state:.state,total:.total_count}",
            ]
        )
        values = self._json_lines(raw, "commit status summary")
        if len(values) != 1 or not isinstance(values[0], dict):
            raise forge.ForgeMalformedResponseError(
                "GitHub returned a malformed commit status summary"
            )
        value = values[0]
        state = value.get("state")
        total = value.get("total")
        if (
            not isinstance(state, str)
            or not state
            or isinstance(total, bool)
            or not isinstance(total, int)
            or total < 0
        ):
            raise forge.ForgeMalformedResponseError(
                "GitHub returned a malformed commit status summary"
            )
        return state, total

    def _combined_status_name_page(self, sha: str, page: int) -> tuple[object, ...]:
        raw = self._run(
            [
                "api",
                f"repos/{self.repository}/commits/{sha}/status"
                f"?per_page={ISSUES_PER_PAGE}&page={page}",
                "--jq",
                ".statuses[] | {name:.context}",
            ]
        )
        return self._json_lines(raw, "commit status")

    def _combined_status_names(self, sha: str) -> tuple[str, ...]:
        """Every combined-status context's own name, paginated (issue #405
        gate finding, round-4 finding 2): named to report each context as a
        successful check once `_combined_status_summary` reads `success`, or
        to fill in a refusal sentence once it reads anything else -- only
        `total_count` zero (no statuses at all) never pays for this
        pagination at all."""
        values = self._fetch_pages(
            lambda page: self._combined_status_name_page(sha, page), per_page=ISSUES_PER_PAGE
        )
        names: list[str] = []
        for value in values:
            name = value.get("name") if isinstance(value, dict) else None
            if not isinstance(name, str) or not name:
                raise forge.ForgeMalformedResponseError("GitHub returned a malformed commit status")
            names.append(name)
        return tuple(names)

    def _combined_status_checks(self, sha: str) -> tuple[forge.CheckRun, ...]:
        """`sha`'s combined commit status, read as one verdict (issue #405
        review/gate finding, round-4 finding 2): the external checks
        GitHub's own check-runs listing never carries -- a SonarCloud
        quality gate, say -- posted through the separate legacy status API.
        The endpoint's own aggregate `state` decides pending/failure/
        error/success, GitHub's own semantics this tool never recomputes
        from individual contexts; `total_count` zero passes regardless of
        that `state` (GitHub's own default state for no statuses at all is
        `pending`, which would otherwise misread a pull request with no
        external checks as blocked). A `success` verdict with statuses
        present is itself a named, successful check, not nothing: a pull
        request whose only checks are combined statuses must still expose
        them, or `landing_readiness` reads it as exposing no CI checks at
        all and refuses a green pull request."""
        state, total = self._combined_status_summary(sha)
        if total == 0:
            return ()
        names = self._combined_status_names(sha) or (EXTERNAL_STATUS_FALLBACK_NAME,)
        conclusion = None if state == "pending" else state
        return tuple(forge.CheckRun(name, conclusion) for name in names)

    def landing_readiness(self, number: int) -> forge.LandingReadiness:
        """Whether pull request `number` is safe to merge with its own
        pinned head sha (`aco land`'s preflight, issue #405): read from the
        pull request itself (open state, head sha, mergeable state), every
        page of the check-runs endpoint, and the combined commit status
        (external contexts such as SonarCloud) against that same head sha --
        GitHub answers all three from separate resources, and a merge is
        safe only once every one of them agrees."""
        raw = self._run(
            [
                "api",
                f"repos/{self.repository}/pulls/{number}",
                "--jq",
                "{state,headSha:.head.sha,mergeableState:.mergeable_state}",
            ]
        )
        values = self._json_lines(raw, "pull request readiness")
        if len(values) != 1 or not isinstance(values[0], dict):
            raise forge.ForgeMalformedResponseError(MALFORMED_PULL_REQUEST)
        value = values[0]
        state = value.get("state")
        head_sha = value.get("headSha")
        mergeable_state = value.get("mergeableState")
        if (
            state not in {"open", "closed"}
            or not isinstance(head_sha, str)
            or protocol.COMMIT_PATTERN.fullmatch(head_sha) is None
            or not isinstance(mergeable_state, str)
            or not mergeable_state
        ):
            raise forge.ForgeMalformedResponseError(MALFORMED_PULL_REQUEST)
        checks = self._check_runs(head_sha) + self._combined_status_checks(head_sha)
        return forge.LandingReadiness(number, state == "open", head_sha, mergeable_state, checks)

    def merge_landing(self, number: int, *, head_sha: str, title: str, body: str) -> str:
        """Merge pull request `number` with GitHub's real-merge-commit
        strategy, pinned to `head_sha` (issue #405): never `gh pr merge`,
        which re-reads the pull request's current head itself rather than
        merging the exact commit `landing_readiness` already proved green.
        A 405 or 409 means the pull request changed since that read --
        translated to `ForgeMergeConflictError` so `aco land` can name the
        one recovery that ever applies: re-run.
        """
        try:
            raw = self._run(
                [
                    "api",
                    "--method",
                    "PUT",
                    f"repos/{self.repository}/pulls/{number}/merge",
                    "--input",
                    "-",
                ],
                input_data=json.dumps(
                    {
                        "sha": head_sha,
                        "merge_method": "merge",
                        "commit_title": title,
                        "commit_message": body,
                    }
                ).encode("utf-8"),
            )
        except forge.ForgeError as error:
            if _MERGE_CONFLICT_PATTERN.search(str(error)) is not None:
                raise forge.ForgeMergeConflictError(str(error)) from error
            raise
        values = self._json_lines(raw, "merge result")
        if len(values) != 1 or not isinstance(values[0], dict):
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed merge result")
        merged = values[0].get("merged")
        sha = values[0].get("sha")
        if (
            merged is not True
            or not isinstance(sha, str)
            or protocol.COMMIT_PATTERN.fullmatch(sha) is None
        ):
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed merge result")
        return sha

    def delete_branch(self, branch: str) -> None:
        """Delete `branch` from this repository once its pull request has
        merged (`aco land`, issue #405): idempotent -- GitHub answering that
        the ref already does not exist (branch protection auto-deleted it,
        or a previous `land` run already deleted it before a later step
        failed) is success, not a failure to surface. Any other 422 -- a
        protected branch refusing the delete, say -- is this adapter's
        normal error family, not an absence to swallow (issue #405 review
        finding)."""
        try:
            self._run(
                ["api", "--method", "DELETE", f"repos/{self.repository}/git/refs/heads/{branch}"]
            )
        except forge.ForgeNotFoundError:
            return
        except forge.ForgeError as error:
            if _branch_already_absent(str(error)):
                return
            raise

    def _issue_reference(self, value: object, description: str) -> board.IssueReference:
        if not isinstance(value, dict):
            raise forge.ForgeMalformedResponseError(f"GitHub returned a malformed {description}")
        number = value.get("number")
        repository_url = value.get("repository")
        repository = (
            repository_url.rpartition("/repos/")[2] if isinstance(repository_url, str) else None
        )
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or repository is None
            or re.fullmatch(REPOSITORY_PATTERN, repository) is None
        ):
            raise forge.ForgeMalformedResponseError(f"GitHub returned a malformed {description}")
        return board.IssueReference(repository, number)

    def _issue_state(self, value: object, description: str) -> board.BlockerState:
        state = value.get("state") if isinstance(value, dict) else None
        parsed = API_ISSUE_STATES.get(state) if isinstance(state, str) else None
        if parsed is None:
            raise forge.ForgeMalformedResponseError(f"GitHub returned a malformed {description}")
        return parsed

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
        values = self._json_lines(raw, "parent issue")
        if len(values) != 1 or not isinstance(values[0], dict):
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed parent issue")
        value = values[0]
        body = value.get("body")
        kind_raw = value.get("kind")
        if not isinstance(body, str) or (kind_raw is not None and not isinstance(kind_raw, str)):
            raise forge.ForgeMalformedResponseError("GitHub returned a malformed parent issue")
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
        if not protocol.is_safe_branch_name(branch):
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
        if not isinstance(value, dict):
            raise forge.ForgeMalformedResponseError(self.MALFORMED_BOARD_DEPENDENCY)
        number = value.get("number")
        state = value.get("state")
        closed_at = value.get("closedAt")
        repository = value.get("repository")
        is_pull_request = value.get("isPullRequest")
        blocker_state = API_ISSUE_STATES.get(state) if isinstance(state, str) else None
        if (
            isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
            or blocker_state is None
            or not isinstance(repository, str)
            or re.fullmatch(REPOSITORY_PATTERN, repository) is None
            or not isinstance(is_pull_request, bool)
            or (closed_at is not None and not isinstance(closed_at, str))
            or (
                isinstance(closed_at, str)
                and protocol.RFC3339_TIMESTAMP_PATTERN.fullmatch(closed_at) is None
            )
            or (blocker_state is board.BlockerState.CLOSED and closed_at is None)
        ):
            raise forge.ForgeMalformedResponseError(self.MALFORMED_BOARD_DEPENDENCY)
        parsed_closed_at = None
        if closed_at is not None:
            try:
                parsed_closed_at = datetime.fromisoformat(closed_at)
            except ValueError as error:
                raise forge.ForgeMalformedResponseError(self.MALFORMED_BOARD_DEPENDENCY) from error
            parsed_closed_at = parsed_closed_at.astimezone(UTC)
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

    def _create_issue(self, *, title: str, body: str, kind: ItemKind) -> int:
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
        identifier = created.get("id") if isinstance(created, dict) else None
        number = created.get("number") if isinstance(created, dict) else None
        if (
            isinstance(identifier, bool)
            or not isinstance(identifier, int)
            or identifier < 1
            or isinstance(number, bool)
            or not isinstance(number, int)
            or number < 1
        ):
            raise forge.ForgeMalformedResponseError("GitHub did not return a created issue")
        return number

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

    def create_child(self, *, parent: int, title: str, body: str, kind: ItemKind) -> int:
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

    def _has_landing_comment(self, number: int, comment: str) -> bool:
        """Whether `number` already carries `close_landed_item`'s own
        deterministic comment (issue #397): a repeat run after a comment
        that landed but a close that did not (a crash or a transient
        failure between the two `_run` calls below) must read this before
        posting the same comment a second time."""
        raw = self._run(
            [
                "api",
                "--paginate",
                f"repos/{self.repository}/issues/{number}/comments?per_page=100",
                "--jq",
                ".[] | {body}",
            ]
        )
        for value in self._json_lines(raw, "issue comment"):
            if not isinstance(value, dict) or not isinstance(value.get("body"), str):
                raise forge.ForgeMalformedResponseError("GitHub returned a malformed issue comment")
            if value["body"] == comment:
                return True
        return False

    def close_landed_item(self, number: int, *, pull_request: int) -> None:
        """Closes `number` itself instead of refusing (issue #359 Card 1):
        `release --merged <pr>` used to require the item already closed on
        the forge; now it closes a still-open one here -- not part of the
        generic `ForgeWriter` port, since `storage = "state-ref"` closes its
        own item through `state_board.StateRefBoard.prepare_landing`'s
        atomic `protocol.LandingIntent` instead, never through a forge
        write at all. The comment lands first: a transient failure between
        the two calls then leaves an open issue explaining the pull request
        that is about to close it, never a closed issue with no record of
        why -- and repeat-safe (issue #397): a rerun that finds its own
        comment already posted skips straight to the close, never doubling
        it.
        """
        comment = landing_comment(pull_request)
        if not self._has_landing_comment(number, comment):
            self._run(
                ["api", f"repos/{self.repository}/issues/{number}/comments", "--input", "-"],
                input_data=json.dumps({"body": comment}).encode("utf-8"),
            )
        self._run(
            [
                "api",
                "--method",
                "PATCH",
                f"repos/{self.repository}/issues/{number}",
                "--input",
                "-",
            ],
            input_data=json.dumps({"state": "closed"}).encode("utf-8"),
        )
