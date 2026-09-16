"""Board and claim domain scenario builders shared by `tests/test_cli.py`
(CLI wiring behavior), `tests/test_board.py` (pure `board.py` behavior), and
`tests/test_github.py` (the GitHub adapter's `REPOSITORY`). All three import
this module directly; pytest's rootless collection puts `tests/` on
`sys.path`, so a plain `import board_fixtures` resolves here."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from types import MappingProxyType

from agent_coordination import board, protocol
from agent_coordination.protocol import ClaimRequest

BASE = "a" * 40
REPOSITORY = "example/agent-claim"


def request(
    claim_id: str = "claim-a",
    agent: str = "Codex Sol",
    *,
    issue: int | None = 71,
    lane: bool = False,
    role: str = "builder",
    branch: str | None = None,
    scope: tuple[str, ...] = ("docs/COORDINATION.md", "scripts/issue_claim.py"),
    resource: str | None = None,
    resource_value: int | None = None,
    whole_reason: str | None = None,
) -> ClaimRequest:
    """Build a `ClaimRequest`, issue-identified by default or lane-identified via `lane=True`.

    `issue=None` implies `lane=True` (mirrors the CLI's own "omitted issue number
    means lane mode" rule) so parametrized tables can drive both identity kinds
    from one `issue`/`lane` axis without hand-building identities at every call site.
    """
    lane = lane or issue is None
    identity: protocol.ClaimIdentity
    if lane:
        identity = protocol.LaneIdentity()
    else:
        assert issue is not None, "lane is False only when the caller passed an issue"
        identity = protocol.IssueIdentity(issue)
    default_branch = f"docs/lane-{claim_id}" if lane else f"codex/issue-{issue}-claims"
    return ClaimRequest(
        identity=identity,
        agent=agent,
        role=role,
        base=BASE,
        branch=branch or default_branch,
        scope=scope,
        claim_id=claim_id,
        resource=resource,
        resource_value=resource_value,
        whole_reason=whole_reason,
    )


def projected_board(
    issues: tuple[board.Issue, ...],
    open_pull_requests: tuple[board.PullRequest, ...],
    recent_merged_pull_requests: tuple[board.PullRequest, ...],
    claims: tuple[protocol.ScopedClaim, ...],
    config: board.BoardConfig,
    *,
    repository: str = REPOSITORY,
    now: datetime | None = None,
    trunk_landings: tuple[datetime, ...] = (),
    children: Mapping[int, tuple[board.ChildItem, ...]] = MappingProxyType({}),
    dependencies: Mapping[int, tuple[board.IssueDependency, ...]] = MappingProxyType({}),
) -> board.Board:
    """`board.build_board` for scenarios that do not turn on which repository is projected."""
    observed_at = now or datetime(2026, 8, 21, tzinfo=UTC)
    return board.build_board(
        board.BoardBuildInputs(
            issues=issues,
            open_pull_requests=open_pull_requests,
            recent_merged_pull_requests=recent_merged_pull_requests,
            claims=claims,
            config=config,
            repository=repository,
            now=now,
            trunk_landings=trunk_landings,
            children=children,
            dependencies=dependencies,
            claim_ages={claim.claim_id: observed_at for claim in claims},
        )
    )


def _store_claim_from_request(
    claimed: ClaimRequest, *, opened_commit: str = BASE
) -> protocol.ActiveClaim:
    resource = None
    if claimed.resource is not None and claimed.resource_value is not None:
        resource = protocol.ResourceHold(claimed.resource, claimed.resource_value)
    return protocol.ActiveClaim(
        identity=claimed.identity,
        claim_id=protocol.ClaimId(claimed.claim_id),
        agent=claimed.agent,
        role=claimed.role,
        base=protocol.ObjectId(claimed.base),
        branch=claimed.branch,
        scope=claimed.scope,
        opened_commit=protocol.ObjectId(opened_commit),
        resource=resource,
        whole_reason=claimed.whole_reason,
    )


def board_issue(
    number: int,
    title: str,
    body: str,
    *,
    labels: tuple[str, ...] = (),
    blocked_by_count: int = 0,
    kind: board.ItemKind | None = None,
) -> board.Issue:
    return board.Issue(
        number,
        title,
        labels,
        body,
        "2026-08-20T00:00:00Z",
        "2026-08-20T00:00:00Z",
        kind=kind,
        blocked_by_count=blocked_by_count,
    )


def block_dependency(
    number: int,
    *,
    repository: str = REPOSITORY,
    state: board.BlockerState = board.BlockerState.OPEN,
    is_pull_request: bool = False,
    closed_at: datetime | None = None,
) -> board.IssueDependency:
    return board.IssueDependency(
        board.IssueReference(repository, number), state, is_pull_request, closed_at
    )


def blocked_issue(
    number: int,
    title: str,
    *dependencies: board.IssueDependency,
    next_step: str | None = None,
    labels: tuple[str, ...] = (),
) -> tuple[board.Issue, dict[int, tuple[board.IssueDependency, ...]]]:
    """One item and the `blocked_by` dependencies GitHub records for it, with
    the listing count the forge would report for exactly those."""
    issue = board_issue(
        number,
        title,
        complete_contract(next_step or f"Claim #{number}."),
        labels=labels,
        blocked_by_count=len(dependencies),
    )
    return issue, {number: dependencies}


def agent_claim_body(toml_text: str, *, fence: str = "```") -> str:
    """A body carrying one recognized `agent-claim` fence around `toml_text`,
    with ordinary prose before and after it (issue #150 §4)."""
    return f"Prose before.\n\n{fence}agent-claim\n{toml_text}\n{fence}\n\nProse after.\n"


MINIMAL_BLOCK_TOML = 'version = 1\nnow = "N"\nnext = "X"\ndone_when = "D"\n'
RULED_ON = date(2026, 8, 28)


def complete_contract(
    next_step: str,
    *,
    now: str = "Work is ready.",
    done_when: str = "The work is merged.",
    **block_entries: object,
) -> str:
    """A body whose one `agent-claim` block carries every projection key
    filled, plus whatever `[[expectation]]`/`[[slice]]`/`frozen_until`
    entries the scenario needs -- serialized by the production writer, so no
    test hand-writes the block's TOML escaping."""
    data: dict[str, object] = {
        "version": 1,
        "now": now,
        "next": next_step,
        "done_when": done_when,
        **block_entries,
    }
    return agent_claim_body(board.render_block(data).rstrip("\n"))


FROZEN_TRIGGER = "eine zweite Maschine bekommt einen Grund"
FROZEN_UNTIL = {"trigger": FROZEN_TRIGGER, "ruled_on": date(2026, 8, 31)}


def proposed_expectation(text: str, *, default: str = "later") -> dict[str, object]:
    return {"text": text, "default": default}


def ruled_expectation(
    text: str, *, ruling: str = "yes", ruled_on: date = RULED_ON
) -> dict[str, object]:
    return {"text": text, "ruling": ruling, "ruled_on": ruled_on}


def idea_body(wish: str) -> str:
    """An operator's idea: the wish in their own prose, above a block whose
    projection keys are all still empty -- what `projectionless` reads."""
    return f"## Wunsch\n{wish}\n\n" + complete_contract("", now="", done_when="")


def slice_entries(*titles: str, first_index: int = 1) -> list[dict[str, object]]:
    """`[[slice]]` entries numbered from `first_index`, in order."""
    return [{"index": first_index + offset, "title": title} for offset, title in enumerate(titles)]
