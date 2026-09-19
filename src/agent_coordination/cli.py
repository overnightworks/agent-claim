"""Coordinate coding-agent claims through this repository's own state ref."""

from __future__ import annotations

import argparse
import json
import os
import sys
import uuid
from collections.abc import Callable, Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import UTC, date, datetime
from enum import StrEnum
from pathlib import Path
from typing import cast

from . import (
    __version__,
    board,
    board_html,
    board_serve,
    checkout,
    forge,
    github,
    hook_input,
    items,
    protocol,
    providers,
    state_board,
    store,
    terminal,
    workspace,
)

ACO_AGENT_ENV = checkout.ACO_AGENT_ENV
CLAUDE_SESSION_ID_ENV = checkout.CLAUDE_SESSION_ID_ENV
GROK_SESSION_ID_ENV = checkout.GROK_SESSION_ID_ENV
ClaimError = protocol.ClaimError
ClaimRequest = protocol.ClaimRequest
ClaimUnavailableError = protocol.ClaimUnavailableError
InvalidClaimMarkerError = protocol.InvalidClaimMarkerError
IssueIdentity = protocol.IssueIdentity
LaneIdentity = protocol.LaneIdentity
ISSUELESS_LANE_BRANCH_PREFIXES = protocol.ISSUELESS_LANE_BRANCH_PREFIXES
_git_output = checkout._git_output
_resolved_agent = checkout._resolved_agent
_timestamp = board._timestamp
_validate_checkout = checkout._validate_checkout
claims_conflict = protocol.claims_conflict
claims_holding_path = protocol.claims_holding_path

DEFAULT_CLAIM_ROLE = "builder"
NEXT_PULL_DESCRIPTION = (
    "Pulling is not dispatching: an item whose expectations are still unruled is "
    "named here with refining as its first step, while dispatching a builder onto "
    "it waits for the operator's ruling."
)
CLAIM_DESCRIPTION = (
    "Refuses before the first edit unless the checkout is a linked, isolated "
    "worktree on a non-main branch, the tree is clean, and every --scope value is "
    "a repository-relative path."
)
WHOLE_HELP = (
    "one sentence why this wide scope does not split; required for more than "
    "three paths, any directory, or, once the repository has at least twelve "
    "versioned files, more than a quarter of them"
)


def _resolved_identity(issue: int | None, branch: str) -> protocol.ClaimIdentity:
    """Resolve the CLI's discriminated identity: an explicit issue, or a lane.

    Omitting the positional issue number means lane mode, derived from `branch`
    (the same checkout branch `--base`/`--branch` auto-fill and the release
    branch-matching fallback already use). Lane mode is refused outright unless
    `branch` follows the issueless-lane convention, so a builder who simply forgot
    the issue number never gets a silent, unlabeled, non-projected lane claim.
    """
    if issue is not None:
        return protocol.IssueIdentity(issue)
    if not branch.startswith(protocol.ISSUELESS_LANE_BRANCH_PREFIXES):
        prefixes = " or ".join(repr(prefix) for prefix in protocol.ISSUELESS_LANE_BRANCH_PREFIXES)
        raise protocol.ClaimError(
            f"branch {branch!r} is not an issueless lane; pass an issue number, or "
            f"check out a branch prefixed {prefixes}"
        )
    return protocol.LaneIdentity()


def _claim_subject(
    claim: protocol.ScopedClaim, storage: board.Storage = board.Storage.GITHUB
) -> str:
    return (
        f"lane {claim.branch}"
        if isinstance(claim.identity, protocol.LaneIdentity)
        else f"issue {board.item_label(claim.identity.issue, storage)}"
    )


def _claim_age_fields(opened_at: datetime, now: datetime) -> tuple[str, bool]:
    """The rendered age and old-ness of a claim opened at `opened_at` (a
    commit's committer date -- issue #176, §1 -- not a ledger comment
    timestamp)."""
    age = now.astimezone(UTC) - opened_at.astimezone(UTC)
    return board.format_claim_age(age), board.claim_is_old(age)


def _claim_age_suffix(opened_at: datetime, now: datetime) -> str:
    rendered, old = _claim_age_fields(opened_at, now)
    return f" {rendered} old" if old else f" {rendered}"


def _scope_cost(versioned: tuple[str, ...], scope: tuple[str, ...]) -> tuple[int, int, float]:
    n = len(checkout.paths_under_scope(versioned, scope))
    total = len(versioned)
    share = 0.0 if total == 0 else n / total
    return n, total, share


def _optional_whole_reason(arguments: argparse.Namespace) -> str | None:
    raw = getattr(arguments, "whole", None)
    if raw is None:
        return None
    return protocol._outbound_text(raw, "whole reason", maximum=512)


def _wide_scope_condition(trip: protocol.WideScopeTrip) -> str:
    """The tripped condition in words, with the numbers it was judged
    against -- what the refusal names instead of restating the whole rule."""
    if trip.reason is protocol.WideScopeReason.PATH_COUNT:
        return f"{trip.path_count} paths exceeds three"
    if trip.reason is protocol.WideScopeReason.DIRECTORY:
        noun = "directory" if len(trip.directories) == 1 else "directories"
        return f"{len(trip.directories)} {noun} in scope ({', '.join(trip.directories)})"
    covered, total = trip.covered_file_count, trip.versioned_file_count
    percent = round(100 * covered / total)
    path_word = "path" if covered == 1 else "paths"
    return f"{covered} {path_word} of {total} versioned files ({percent} %) exceeds a quarter"


def _wide_scope_refusal(trip: protocol.WideScopeTrip) -> str:
    return f"scope is wide: {_wide_scope_condition(trip)}; pass --whole REASON"


def _reject_wide_scope(
    scope: tuple[str, ...],
    versioned: tuple[str, ...],
    whole_reason: str | None,
) -> tuple[int, int, float]:
    n, total, share = _scope_cost(versioned, scope)
    directories = checkout._scope_directories(scope)
    trip = protocol.wide_scope_trip(
        scope, directories=directories, covered_file_count=n, versioned_file_count=total
    )
    if trip is not None and whole_reason is None:
        raise protocol.ClaimError(_wide_scope_refusal(trip))
    return n, total, share


def _reject_ungrounded_comma_scope(
    scope: tuple[str, ...], versioned: tuple[str, ...], *, flag: str
) -> None:
    """Refuse a scope entry that contains a comma and matches no versioned
    file -- the shape `{flag} a.py,b.py` takes when passed as one flag from
    habit instead of one path per flag (issue #207). Stored verbatim, that
    single entry names a path nothing tracks, so the claim protects nothing:
    the lane's real files stay uncovered and no overlap check can ever fire
    for them.

    Never call this over `--drop`: a value the live claim already holds is a
    fact about the claim, not a typo about the checkout, and dropping a value
    the claim does not hold is already refused by `_combined_scope` with a
    truer sentence naming the claim rather than the checkout -- adding this
    check there would either block the very repair this refusal exists to
    leave open (dropping an already-claimed ungrounded value), or never fire
    at all (a not-yet-dropped value the claim lacks is refused first).

    Splitting on the comma would be the old, wrong fix (issue #201): it made
    a real comma-bearing filename unrepresentable. So a real comma-bearing
    path that names a versioned file, or a directory holding one, still
    passes here -- `paths_under_scope` matches either. So does a comma-free
    path that does not exist yet, since a lane routinely claims files it is
    about to create; only a comma with no match among versioned files is the
    signature this refuses.
    """
    for entry in scope:
        if "," in entry and not checkout.paths_under_scope(versioned, (entry,)):
            raise protocol.ClaimError(
                f"{entry!r} matches no versioned file; one {flag} path per flag, so its comma "
                f"is read literally -- repeat {flag} for a second path"
            )


def _touch_json(claim: protocol.ScopedClaim) -> dict[str, object]:
    return {
        **_identity_json(claim.identity),
        "claim_id": claim.claim_id,
        "agent": claim.agent,
        "scope": list(claim.scope),
    }


def _touch_line(own_scope: tuple[str, ...], claim: protocol.ScopedClaim) -> str:
    """One overlapping claim, named with the paths where its scope meets
    `own_scope` -- the fact a claimant needs to know they hold both scopes
    at once, not only the other item's name (issue #206)."""
    meeting = protocol.scope_overlap_paths(own_scope, claim.scope)
    return f"{_claim_subject(claim)} on {protocol.named_with_overflow_count(meeting)}"


def _touch_summary(own_scope: tuple[str, ...], touches: tuple[protocol.ScopedClaim, ...]) -> str:
    if not touches:
        return "overlaps no other open claims"
    return "overlaps " + ", ".join(_touch_line(own_scope, claim) for claim in touches)


def _claim_cost_line(
    n: int, total: int, own_scope: tuple[str, ...], touches: tuple[protocol.ScopedClaim, ...]
) -> str:
    percent = 0 if total == 0 else round(100 * n / total)
    return f"{n} of {total} versioned files ({percent}%); {_touch_summary(own_scope, touches)}"


def _request(arguments: argparse.Namespace) -> protocol.ClaimRequest:
    agent = protocol._outbound_text(checkout._resolved_agent(arguments.agent), "agent", maximum=128)
    role = protocol._outbound_text(arguments.role, "role", maximum=64)
    base = checkout._git_output(["rev-parse", "HEAD"]) if arguments.base is None else arguments.base
    if protocol.COMMIT_PATTERN.fullmatch(base) is None:
        raise protocol.ClaimError("base must be a full lowercase commit SHA")
    if arguments.branch is None:
        branch = checkout._git_output(["branch", "--show-current"])
    else:
        branch = arguments.branch
    branch = protocol._valid_branch({"branch": branch})
    issue = _optional_issue_number(arguments.issue)
    identity = _resolved_identity(issue, branch)
    claim_id = arguments.claim_id or uuid.uuid4().hex
    protocol.ClaimId(claim_id)
    whole_reason = _optional_whole_reason(arguments)
    resource = getattr(arguments, "resource", None)
    if resource is not None:
        resource = protocol._outbound_resource_name(resource)
    request = protocol.ClaimRequest(
        identity=identity,
        agent=agent,
        role=role,
        base=base,
        branch=branch,
        scope=protocol._valid_scope(arguments.scope),
        claim_id=claim_id,
        out_of_order_reason=arguments.out_of_order,
        whole_reason=whole_reason,
        resource=resource,
    )
    checkout._validate_checkout(request)
    return request


LANE_ISSUE_HELP = "omit for lane mode, derived from a docs/ or fix/ checkout branch"
JSON_HELP = "print the result as JSON instead of the human lines"
# `--html` with no value: `argparse`'s `nargs="?"` const, distinct from the
# `None` default (flag absent) -- `_cmd_board_html` treats it as "stdout".
STDOUT_HTML_PATH = ""
AGENT_HELP = (
    "the acting agent's name; filled from a non-empty ACO_AGENT, GROK_SESSION_ID or "
    "CLAUDE_SESSION_ID when omitted"
)
EXPECTED_CLAIM_ID_HELP = (
    "assert which claim you are acting on; the issue number or lane branch selects it, and "
    "a differing id is refused rather than redirected"
)
ROLE_ON_LIVE_CLAIM_HELP = (
    "the acting role; the selected claim's own role when omitted, and required to be "
    "coordinator with --coordinator-override"
)
ITEM_REF_HELP = "an item, as aco-xxxxxx, #n, or the bare number n"


def _add_bootstrap_parser(commands: argparse._SubParsersAction) -> None:
    commands.add_parser("bootstrap", help="create refs/aco/state if it does not exist yet")


def _add_reset_parser(commands: argparse._SubParsersAction) -> None:
    reset = commands.add_parser(
        "reset",
        help="export refs/aco/state, delete it remotely and locally, and bootstrap fresh",
    )
    reset.add_argument(
        "--confirm", action="store_true", help="perform the reset; omit for a dry run"
    )
    reset.add_argument(
        "--no-export", action="store_true", help="skip the otherwise-mandatory bundle export"
    )
    reset.add_argument(
        "--export-dir",
        type=Path,
        metavar="DIR",
        help="directory for the export bundle (default: the repository's parent directory)",
    )


def _add_status_parser(commands: argparse._SubParsersAction) -> None:
    status = commands.add_parser("status", help="show repository-wide build claims")
    status.add_argument(
        "issue",
        type=board.parse_item_reference,
        nargs="?",
        help="show only this issue's claims and the ones they overlap",
    )
    status.add_argument(
        "--path", metavar="PATH", help="list holders of this path instead of by issue"
    )
    status.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_board_parser(commands: argparse._SubParsersAction) -> None:
    board_command = commands.add_parser(
        "board", help="project the open work board; only --serve writes"
    )
    output = board_command.add_mutually_exclusive_group()
    output.add_argument("--json", action="store_true", help=JSON_HELP)
    output.add_argument(
        "--html",
        nargs="?",
        const=STDOUT_HTML_PATH,
        default=None,
        metavar="PATH",
        help="write a static HTML board page (stdout when PATH is omitted)",
    )
    output.add_argument(
        "--serve",
        action="store_true",
        help=(
            "serve the board page on 127.0.0.1 with a one-click aco-rule form per "
            "expectation line (issue #280); a write command, so it needs the writer"
        ),
    )
    board_command.add_argument(
        "--port",
        type=int,
        default=0,
        metavar="PORT",
        help="loopback port for --serve; 0 (default) picks an ephemeral one",
    )


def _add_rulings_parser(commands: argparse._SubParsersAction) -> None:
    rulings_command = commands.add_parser(
        "rulings", help="list open expectation lines without writes"
    )
    rulings_command.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_next_parser(commands: argparse._SubParsersAction) -> None:
    next_command = commands.add_parser(
        "next",
        help="name the board's top-priority item to pull",
        description=NEXT_PULL_DESCRIPTION,
    )
    next_command.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_claim_parser(commands: argparse._SubParsersAction) -> None:
    claim = commands.add_parser(
        "claim",
        help="claim an issue and scope before editing",
        description=CLAIM_DESCRIPTION,
    )
    claim.add_argument(
        "issue",
        type=board.parse_item_reference,
        nargs="?",
        help=LANE_ISSUE_HELP,
    )
    claim.add_argument("--agent", help=AGENT_HELP)
    claim.add_argument(
        "--role",
        default=DEFAULT_CLAIM_ROLE,
        help=f"the claiming role; default {DEFAULT_CLAIM_ROLE}",
    )
    claim.add_argument(
        "--base",
        help="the full commit SHA this lane starts from; the current HEAD when omitted",
    )
    claim.add_argument(
        "--branch", help="the lane's branch; the current checkout branch when omitted"
    )
    claim.add_argument(
        "--scope",
        action="append",
        required=True,
        help="a repository-relative path; repeat --scope for more than one path",
    )
    claim.add_argument(
        "--claim-id",
        help=(
            "this claim's own id; generated when omitted, and repeating an identical claim "
            "with it returns the active claim instead of writing a second one"
        ),
    )
    claim.add_argument(
        "--out-of-order",
        metavar="REASON",
        help=(
            "refuses a claim without a reason when a higher-priority actionable item is "
            "free or an open blocker remains; records why"
        ),
    )
    claim.add_argument(
        "--whole",
        metavar="REASON",
        help=WHOLE_HELP,
    )
    claim.add_argument(
        "--resource",
        metavar="NAME",
        help="allocate the next free value of this named scarce resource and hold it",
    )
    claim.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_release_parser(commands: argparse._SubParsersAction) -> None:
    release = commands.add_parser("release", help="release a landed or abandoned claim")
    release.add_argument(
        "issue",
        type=board.parse_item_reference,
        nargs="?",
        help=LANE_ISSUE_HELP,
    )
    release.add_argument("--agent", help=AGENT_HELP)
    release.add_argument("--role", help=ROLE_ON_LIVE_CLAIM_HELP)
    release.add_argument(
        "--branch",
        help=(
            "the claim's lane branch; selects it without checking out that branch, unlike "
            "claim's --branch, and defaults to the current checkout branch when omitted "
            "together with --claim-id"
        ),
    )
    outcome = release.add_mutually_exclusive_group(required=True)
    outcome.add_argument(
        "--merged",
        type=int,
        metavar="PULL_REQUEST",
        help="the pull request that landed this claim's item on the default branch",
    )
    outcome.add_argument(
        "--abandoned",
        metavar="REASON",
        help="why this claim ends without a landing",
    )
    release.add_argument("--claim-id", help=EXPECTED_CLAIM_ID_HELP)
    release.add_argument(
        "--coordinator-override",
        action="store_true",
        help="release another agent's claim as the coordinator; requires --role coordinator",
    )
    release.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_rescope_parser(commands: argparse._SubParsersAction) -> None:
    rescope = commands.add_parser(
        "rescope", help="add or drop paths on a live claim without releasing"
    )
    rescope.add_argument(
        "issue",
        type=board.parse_item_reference,
        nargs="?",
        help=LANE_ISSUE_HELP,
    )
    rescope.add_argument("--agent", help=AGENT_HELP)
    rescope.add_argument(
        "--add",
        action="append",
        help="a repository-relative path to add; repeat --add for more than one path",
    )
    rescope.add_argument(
        "--drop",
        action="append",
        help="a repository-relative path to drop; repeat --drop for more than one path",
    )
    rescope.add_argument("--claim-id", help=EXPECTED_CLAIM_ID_HELP)
    rescope.add_argument(
        "--whole",
        metavar="REASON",
        help=WHOLE_HELP,
    )
    rescope.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_cut_parser(commands: argparse._SubParsersAction) -> None:
    cut = commands.add_parser("cut", help="create a container's next slice as a fresh child issue")
    cut.add_argument("issue", type=board.parse_item_reference, help="the container to cut")
    cut.add_argument("--title", required=True, help="the fresh child issue's title")
    cut.add_argument(
        "--row",
        type=int,
        metavar="N",
        help="the slice table's # column value to cut; default is the first cuttable row",
    )
    cut.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_ask_parser(commands: argparse._SubParsersAction) -> None:
    ask = commands.add_parser("ask", help="append one proposed expectation line to an item's block")
    ask.add_argument(
        "item", type=board.parse_item_reference, help="the item to append the expectation line to"
    )
    ask.add_argument("--text", required=True, help="the expectation line's prose")
    ask.add_argument(
        "--default",
        choices=sorted(board.BLOCK_EXPECTATION_DEFAULTS),
        default="yes",
        help="the proposer's suggested outcome; default yes",
    )
    ask.add_argument(
        "--question",
        help=(
            "one operator-language sentence the card shows as its heading "
            f"instead of --text; at most {board.EXPECTATION_QUESTION_MAXIMUM_CHARACTERS} characters"
        ),
    )
    ask.add_argument("--example", help="one operator-language sentence illustrating the question")
    ask.add_argument(
        "--picture",
        metavar="FILE.svg",
        help=(
            "a path to an inline-SVG file (root <svg>, no <script>, no external "
            f"href, at most {board.EXPECTATION_PICTURE_MAXIMUM_BYTES} bytes) the card shows"
        ),
    )
    ask.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_rule_parser(commands: argparse._SubParsersAction) -> None:
    rule = commands.add_parser(
        "rule", help="rule one proposed expectation line, transcribing the operator's word"
    )
    rule.add_argument(
        "item", type=board.parse_item_reference, help="the item whose expectation line is ruled"
    )
    rule.add_argument(
        "--line",
        type=int,
        required=True,
        metavar="N",
        help="the 1-based expectation line index, as rulings prints it",
    )
    outcome = rule.add_mutually_exclusive_group(required=True)
    outcome.add_argument("--yes", action="store_const", dest="ruling", const="yes")
    outcome.add_argument("--no", action="store_const", dest="ruling", const="no")
    outcome.add_argument("--later", action="store_const", dest="ruling", const="later")
    rule.add_argument("--note", help="appended to the line's own text as ' Anmerkung: TEXT'")
    rule.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_check_parser(commands: argparse._SubParsersAction) -> None:
    check = commands.add_parser(
        "check",
        help=(
            "read one number -- a pull request's work-item classification, or an "
            "issue's body contract; claims, labels and writes nothing"
        ),
    )
    check.add_argument(
        "number",
        type=board.parse_item_reference,
        help="the pull request or issue to read; the forge says which one it is",
    )
    check.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_body_parser(commands: argparse._SubParsersAction) -> None:
    body = commands.add_parser(
        "body",
        help="print a body skeleton, or check one for defects before it reaches the forge",
    )
    mode = body.add_mutually_exclusive_group(required=True)
    mode.add_argument("--template", action="store_true", help="print the skeleton body for --kind")
    mode.add_argument(
        "--check",
        action="store_true",
        help="read a body from stdin and report its defects",
    )
    body.add_argument(
        "--kind",
        choices=BODY_TEMPLATE_KINDS,
        help=f"the fresh item's kind for --template; default {DEFAULT_BODY_TEMPLATE_KIND}",
    )
    body.add_argument(
        "--parent",
        type=board.parse_item_reference,
        metavar="ITEM",
        help="prepend a Parent: #N line to --template's skeleton",
    )
    body.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_brief_parser(commands: argparse._SubParsersAction) -> None:
    brief = commands.add_parser(
        "brief",
        help="print one item's body, live claim, lane tip and touched files for a dispatch",
    )
    brief.add_argument("item", type=board.parse_item_reference, help="the work item to brief")
    brief.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_item_parser(commands: argparse._SubParsersAction) -> None:
    item = commands.add_parser(
        "item", help="create or show one work item straight in refs/aco/state"
    )
    item_commands = item.add_subparsers(dest="item_command", required=True)
    new = item_commands.add_parser(
        "new", help="create a fresh item in refs/aco/state and print its id"
    )
    new.add_argument("--title", required=True, help="the fresh item's title")
    new.add_argument(
        "--kind",
        choices=BODY_TEMPLATE_KINDS,
        default=DEFAULT_BODY_TEMPLATE_KIND,
        help=f"the fresh item's kind; default {DEFAULT_BODY_TEMPLATE_KIND}",
    )
    new.add_argument(
        "--parent",
        type=board.parse_item_reference,
        metavar="ITEM",
        help=f"the fresh item's parent, {ITEM_REF_HELP}",
    )
    new.add_argument(
        "--origin",
        type=items.parse_origin,
        metavar="FORGE#N",
        help="bind this lane to a foreign forge issue, e.g. gitlab#514",
    )
    new.add_argument("--json", action="store_true", help=JSON_HELP)
    show = item_commands.add_parser(
        "show", help="print one item's header and its stored body byte-exact"
    )
    show.add_argument(
        "item", type=board.parse_item_reference, help=f"the item to show, {ITEM_REF_HELP}"
    )
    show.add_argument("--json", action="store_true", help=JSON_HELP)
    edit = item_commands.add_parser(
        "edit", help="replace one item's body from stdin, aco keeping its own record fields"
    )
    edit.add_argument(
        "item", type=board.parse_item_reference, help=f"the item to edit, {ITEM_REF_HELP}"
    )
    edit.add_argument("--json", action="store_true", help=JSON_HELP)
    close = item_commands.add_parser(
        "close", help="close a state-ref item; the file stays, next and board let it go"
    )
    close.add_argument(
        "item", type=board.parse_item_reference, help=f"the item to close, {ITEM_REF_HELP}"
    )
    close.add_argument("--json", action="store_true", help=JSON_HELP)


def _add_protect_parser(commands: argparse._SubParsersAction) -> None:
    commands.add_parser("protect", help="deny PreToolUse writes without this session's live claim")


def _add_register_parser(commands: argparse._SubParsersAction) -> None:
    register = commands.add_parser(
        "register",
        help="record one stopped or validated live provider session for workspace recovery",
    )
    register.add_argument("project", metavar="PROJECT", help="a stable local project key")
    register.add_argument(
        "--path", required=True, type=Path, help="the project's canonical directory"
    )
    register.add_argument("--session-id", required=True, help="the exact provider session UUID")
    register.add_argument("--agent", required=True, help="the inherited logical claim identity")
    register.add_argument(
        "--provider",
        choices=tuple(provider.value for provider in providers.Provider),
        default=providers.Provider.CODEX.value,
        help="the conversation provider (default: codex)",
    )
    register.add_argument("--model", help="optional provider model override")
    handover = register.add_mutually_exclusive_group(required=True)
    handover.add_argument(
        "--stopped",
        action="store_true",
        help="acknowledge that the existing provider session was checkpointed and stopped",
    )
    handover.add_argument(
        "--live-pid", type=int, help="the exact running native Codex or Claude process ID"
    )


def _add_run_parser(commands: argparse._SubParsersAction) -> None:
    run = commands.add_parser("run", help="open the registered provider workspace consoles")
    run.add_argument("project", metavar="PROJECT", nargs="?", help="one registered project")


def _add_login_parser(commands: argparse._SubParsersAction) -> None:
    login = commands.add_parser(
        "login", help="manage configured workspace recovery at desktop login"
    )
    login_commands = login.add_subparsers(dest="login_command", required=True)
    login_commands.add_parser("enable", help="install the owned desktop login launcher")
    login_commands.add_parser("disable", help="remove the owned desktop login launcher")
    login_commands.add_parser(
        "status", help="show launcher, configuration, and latest attempt state"
    )


def _add_run_at_login_parser(commands: argparse._SubParsersAction) -> None:
    commands.add_parser("_run-at-login", help=argparse.SUPPRESS)


_SUBPARSER_BUILDERS: tuple[Callable[[argparse._SubParsersAction], None], ...] = (
    _add_bootstrap_parser,
    _add_reset_parser,
    _add_status_parser,
    _add_board_parser,
    _add_rulings_parser,
    _add_next_parser,
    _add_claim_parser,
    _add_release_parser,
    _add_rescope_parser,
    _add_cut_parser,
    _add_ask_parser,
    _add_rule_parser,
    _add_check_parser,
    _add_body_parser,
    _add_brief_parser,
    _add_item_parser,
    _add_protect_parser,
    _add_register_parser,
    _add_run_parser,
    _add_login_parser,
    _add_run_at_login_parser,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="aco", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--repo", help="GitHub repository as OWNER/REPO")
    commands = parser.add_subparsers(dest="command", required=True)
    for add_subparser in _SUBPARSER_BUILDERS:
        add_subparser(commands)
    return parser


def _identity_json(identity: protocol.ClaimIdentity) -> dict[str, object]:
    """`issue`/`lane` pair for one claim's discriminated identity, for JSON output.

    A lane claim's name lives in the sibling `branch` field of the same JSON
    object, so `lane` stays a bare marker instead of duplicating it.
    """
    if isinstance(identity, protocol.LaneIdentity):
        return {"issue": None, "lane": True}
    return {"issue": identity.issue, "lane": None}


def _status_claims(
    claims: tuple[protocol.ActiveClaim, ...], issue: int | None
) -> tuple[tuple[protocol.ActiveClaim, ...], protocol.ClaimConflictIndex]:
    selected = tuple(
        claim
        for claim in claims
        if issue is None
        or (isinstance(claim.identity, protocol.IssueIdentity) and claim.identity.issue == issue)
    )
    index = protocol._claim_conflict_index(claims)
    if not selected:
        return (), index
    related_ids = (
        {claim.claim_id for claim in claims}
        if issue is None
        else protocol._related_claim_ids(index, selected)
    )
    related = tuple(claim for claim in claims if claim.claim_id in related_ids)
    return related, index


def _resource_fields(resource: protocol.ResourceHold | None) -> dict[str, object]:
    if resource is None:
        return {"resource": None, "resource_value": None}
    return {"resource": resource.name, "resource_value": resource.value}


def _overlap_subjects(
    claims_by_id: Mapping[str, protocol.ActiveClaim], peer_ids: set[str]
) -> list[dict[str, object]]:
    return [
        {
            **_identity_json(peer.identity),
            "claim_id": peer.claim_id,
            "agent": peer.agent,
        }
        for claim_id in sorted(peer_ids)
        if (peer := claims_by_id.get(claim_id)) is not None
    ]


def _overlap_note(
    claims_by_id: Mapping[str, protocol.ActiveClaim], peer_ids: set[str], storage: board.Storage
) -> str | None:
    peers = [claims_by_id[claim_id] for claim_id in sorted(peer_ids) if claim_id in claims_by_id]
    if not peers:
        return None
    return "overlaps " + ", ".join(
        f"{_claim_subject(claim, storage)} ({claim.claim_id})" for claim in peers
    )


@dataclass(frozen=True)
class _ClaimReportContext:
    """`index` and `storage` together (issue #292): the two facts every
    claim in one `status` read shares -- bundled so adding `storage`
    beside the pre-existing `index` never pushes a caller past PLR0913's
    five-argument ceiling."""

    index: protocol.ClaimConflictIndex
    storage: board.Storage


def _print_claim_status_lines(
    claim: protocol.ActiveClaim,
    claims_by_id: Mapping[str, protocol.ActiveClaim],
    context: _ClaimReportContext,
    opened_at: datetime,
    observed_at: datetime,
) -> None:
    state = "CONFLICT" if claim.claim_id in context.index.conflict_ids else "CLAIMED"
    print(
        f"{state} {_claim_subject(claim, context.storage)}: {claim.agent} ({claim.role}) "
        f"base={claim.base} branch={claim.branch} claim={claim.claim_id}"
        f"{_claim_age_suffix(opened_at, observed_at)}"
    )
    for path in claim.scope:
        print(f"  {path}")
    if claim.resource is not None:
        print(f"  resource {claim.resource.name}={claim.resource.value}")
    if claim.whole_reason is not None:
        print(f"  whole: {claim.whole_reason}")
    note = _overlap_note(
        claims_by_id, protocol._overlap_peer_ids(context.index, claim), context.storage
    )
    if note is not None:
        print(f"  {note}")


def _print_related_claims(
    claims: tuple[protocol.ActiveClaim, ...],
    related: tuple[protocol.ActiveClaim, ...],
    context: _ClaimReportContext,
    ages: Mapping[str, datetime],
    observed_at: datetime,
) -> int:
    claims_by_id: dict[str, protocol.ActiveClaim] = {claim.claim_id: claim for claim in claims}
    for claim in related:
        _print_claim_status_lines(claim, claims_by_id, context, ages[claim.claim_id], observed_at)
    return 2 if any(claim.claim_id in context.index.conflict_ids for claim in related) else 0


def _status(
    claims: tuple[protocol.ActiveClaim, ...],
    issue: int | None,
    ages: Mapping[str, datetime],
    storage: board.Storage,
    now: datetime | None = None,
) -> int:
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    related, index = _status_claims(claims, issue)
    if related:
        context = _ClaimReportContext(index, storage)
        return _print_related_claims(claims, related, context, ages, observed_at)
    subject = "repository" if issue is None else f"issue {board.item_label(issue, storage)}"
    print(f"UNCLAIMED {subject}")
    return 0


def _status_json(
    claims: tuple[protocol.ActiveClaim, ...],
    issue: int | None,
    ages: Mapping[str, datetime],
    tip: protocol.ObjectId | None,
    now: datetime | None = None,
) -> int:
    observed_at = (now or datetime.now(UTC)).astimezone(UTC)
    related, index = _status_claims(claims, issue)
    if not related:
        state = "UNCLAIMED"
    elif any(claim.claim_id in index.conflict_ids for claim in related):
        state = "CONFLICT"
    else:
        state = "CLAIMED"
    claims_by_id: dict[str, protocol.ActiveClaim] = {claim.claim_id: claim for claim in claims}
    payload = {
        "issue": issue,
        "state": state,
        "tip": tip,
        "claims": [
            {
                **_identity_json(claim.identity),
                "agent": claim.agent,
                "role": claim.role,
                "base": claim.base,
                "branch": claim.branch,
                "claim_id": claim.claim_id,
                "scope": list(claim.scope),
                **_resource_fields(claim.resource),
                **({"whole": claim.whole_reason} if claim.whole_reason is not None else {}),
                "overlaps": _overlap_subjects(
                    claims_by_id, protocol._overlap_peer_ids(index, claim)
                ),
                "state": "CONFLICT" if claim.claim_id in index.conflict_ids else "CLAIMED",
                "age": _claim_age_fields(ages[claim.claim_id], observed_at)[0],
                "old": _claim_age_fields(ages[claim.claim_id], observed_at)[1],
            }
            for claim in related
        ],
    }
    print(json.dumps(payload))
    return 2 if state == "CONFLICT" else 0


def _status_path(
    claims: tuple[protocol.ActiveClaim, ...], path: str, storage: board.Storage
) -> None:
    holders = protocol.claims_holding_path(claims, path)
    if not holders:
        print(f"UNCLAIMED {path}")
        return
    for claim in holders:
        print(
            f"CLAIMED {path} {_claim_subject(claim, storage)}: {claim.agent} ({claim.role}) "
            f"claim={claim.claim_id}"
        )
        if claim.whole_reason is not None:
            print(f"  whole: {claim.whole_reason}")
    if len(holders) > 1:
        print(
            "overlap: "
            + ", ".join(f"{_claim_subject(claim, storage)} ({claim.claim_id})" for claim in holders)
        )


def _status_path_json(claims: tuple[protocol.ActiveClaim, ...], path: str) -> int:
    holders = protocol.claims_holding_path(claims, path)
    state = "UNCLAIMED" if not holders else "CLAIMED"
    payload = {
        "path": path,
        "state": state,
        "claims": [
            {
                **_identity_json(claim.identity),
                "agent": claim.agent,
                "role": claim.role,
                "base": claim.base,
                "branch": claim.branch,
                "claim_id": claim.claim_id,
                "scope": list(claim.scope),
                **_resource_fields(claim.resource),
                **({"whole": claim.whole_reason} if claim.whole_reason is not None else {}),
                "state": "CLAIMED",
            }
            for claim in holders
        ],
    }
    print(json.dumps(payload))
    return 0


def _rescope_json(claimed: protocol.ActiveClaim) -> None:
    print(
        json.dumps(
            {
                **_identity_json(claimed.identity),
                "claim_id": claimed.claim_id,
                "agent": claimed.agent,
                "role": claimed.role,
                "base": claimed.base,
                "branch": claimed.branch,
                "scope": list(claimed.scope),
            }
        )
    )


@dataclass(frozen=True)
class ScopeVersioning:
    """How much of the claimed scope's Git history the checkout already has,
    for the `--json` claim payload's `versioned_files`/`share` fields."""

    versioned_files: int
    versioned_files_total: int
    share: float


def _claim_json(
    claimed: protocol.ActiveClaim,
    *,
    versioning: ScopeVersioning,
    touches: tuple[protocol.ActiveClaim, ...],
    checks: tuple[SliceCheck, ...],
) -> int:
    print(
        json.dumps(
            {
                **_identity_json(claimed.identity),
                "claim_id": claimed.claim_id,
                "agent": claimed.agent,
                "role": claimed.role,
                "base": claimed.base,
                "branch": claimed.branch,
                "scope": list(claimed.scope),
                **_resource_fields(claimed.resource),
                "versioned_files": versioning.versioned_files,
                "versioned_files_total": versioning.versioned_files_total,
                "share": versioning.share,
                "touches": [_touch_json(claim) for claim in touches],
                "checks": [check.as_json() for check in checks],
            }
        )
    )
    return 0


def _release_json(
    released: protocol.ActiveClaim,
    agent: str,
    role: str | None,
    outcome: protocol.ReleaseOutcome,
    landing: ReleaseLanding | None,
) -> None:
    payload: dict[str, object] = {
        **_identity_json(released.identity),
        "branch": released.branch,
        "claim_id": released.claim_id,
        "agent": agent,
        "role": role if role is not None else released.role,
        "reason": outcome.reason,
    }
    if landing is not None:
        payload["freed"] = list(landing.freed)
        payload["next"] = None if landing.next_item is None else landing.next_item.number
    print(json.dumps(payload))


@dataclass(frozen=True)
class ReleaseLanding:
    """What a merged release's lazy board read (issue #256) adds to its
    report once the release itself has already succeeded: every open item
    this landing fully freed, and the same next pick `aco next` would
    recommend right after it."""

    freed: tuple[int, ...]
    next_item: board.BoardItem | None


def _next_action_item(action: board.NextAction) -> board.BoardItem:
    """The `BoardItem` `action` targets, whichever action kind it is -- a
    plain claim target for `WorkItemAction`, the container itself for
    `CutSliceAction`/`CloseContainerAction` (issue #256)."""
    return action.item if isinstance(action, board.WorkItemAction) else action.container


def _freed_item_numbers(
    dependencies: Mapping[int, tuple[board.IssueDependency, ...]],
    landed: board.IssueReference,
) -> tuple[int, ...]:
    """Open items whose last open blocker was `landed` (issue #256): every
    local `blocked_by` dependency of the item is now closed, `landed` was
    one of them, and nothing else -- local or foreign -- still blocks it.
    Reuses `board.open_dependency_blockers`/`board._dependency_freed_on`,
    the same per-item blocker facts `_issue_check`'s own refusal already
    reads, rather than re-deriving them from `Board`'s aggregate fields:
    those only ever describe the *currently* open issues, so a landed
    item's own `blocked_by` reference disappears from them the moment it
    closes -- exactly the fact this needs to name who it freed. `dependencies`
    is `_release_landing`'s one fetch, shared with `_board` (issue #256
    review), rather than a second `list_board_dependencies` round trip over
    the same candidates.
    """
    repository = landed.repository
    freed: list[int] = []
    for number, local in dependencies.items():
        if board._dependency_freed_on(local, repository) is None:
            continue
        if board.open_dependency_blockers(local, repository):
            continue
        if landed in {dependency.reference for dependency in local}:
            freed.append(number)
    return tuple(sorted(freed))


def _release_landing(
    client: forge.ForgeReader,
    claims: tuple[protocol.ActiveClaim, ...],
    claim_ages: Mapping[str, datetime],
    landed: board.IssueReference | None,
) -> ReleaseLanding:
    """A merged release's own board read (issue #256): fetched once, lazily,
    only after the release transition already committed -- the caller wraps
    this in one broad `forge.ForgeError` catch, so a forge hiccup here can
    never undo or fail a release that already stood. The dependency fetch
    below is the one round trip both `_freed_item_numbers` and `_board` need
    for this same candidate set; passing it into `_board` keeps this a
    single fetch rather than two (issue #256 review)."""
    issues = client.list_open_board_issues()
    candidates = tuple(issue.number for issue in issues if issue.blocked_by_count > 0)
    dependencies = _validated_dependencies(issues, _fetch_dependencies(client, candidates))
    freed = () if landed is None else _freed_item_numbers(dependencies, landed)
    projected = _board(
        client, claims, issues=issues, claim_ages=claim_ages, dependencies=dependencies
    ).board
    action = board.next_action(projected)
    return ReleaseLanding(freed, None if action is None else _next_action_item(action))


def _release_freed_line(freed: tuple[int, ...], storage: board.Storage) -> str:
    return "freed: " + (
        ", ".join(board.item_label(number, storage) for number in freed) if freed else "none"
    )


def _release_next_line(item: board.BoardItem | None, storage: board.Storage) -> str:
    if item is None:
        return "next: none"
    return f"next: {board.item_label(item.number, storage)} score {item.score}: {item.title}"


def _merged_pull_request_floor(issues: tuple[board.Issue, ...], now: datetime) -> datetime:
    """The earliest merge that could still matter to a currently open issue.

    A pull request can only touch or close an issue that already exists, so
    nothing merged before the oldest still-open issue was filed can ever
    change any open item's stage. Anchoring the query here — instead of an
    arbitrary fixed window — is what lets a slice's "Refs #N"/"Part of #N"
    landing keep crediting its still-open epic for as long as the epic stays
    open, rather than for a fixed number of days after which the credit
    silently reverts. Residual: the underlying query is still capped (see
    `GitHubForge.list_recent_merged_board_pull_requests`), so an epic
    old enough to have more merges than that cap between its filing and now
    can still lose credit for an early slice; this floor removes the
    fortnight-sized version of that gap, not every version of it.
    """
    if not issues:
        return now
    return min(_timestamp(issue.created_at) for issue in issues)


# A container's children are their own `gh list_children` subprocess call;
# an unbounded pool would spawn one worker per container on a large board.
# This caps that fan-out -- a stable invariant of this executor, not
# something an operator tunes. `_fetch_children` gives it a dedicated
# executor sized to exactly this constant, so the cap holds regardless of
# whether the three base board reads below have already finished.
BOARD_CHILD_FETCH_CONCURRENCY = 4

# A stable invariant of the board's own ruling-freshness read (issue #304),
# not something an operator tunes: `RULING_OLD_AFTER_LANDINGS` (10) is the
# most any ruling ever needs counted, so this bounds `git log`'s walk deep
# enough that no realistic ruling window is ever truncated.
TRUNK_LANDING_DEPTH = 5000


def _fetch_children(
    client: forge.BoardSource, container_numbers: tuple[int, ...]
) -> dict[int, tuple[board.ChildItem, ...]]:
    """Every container's children, at most `BOARD_CHILD_FETCH_CONCURRENCY`
    `gh` subprocesses at a time; excess containers queue behind it."""
    if not container_numbers:
        return {}
    workers = min(len(container_numbers), BOARD_CHILD_FETCH_CONCURRENCY)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            number: pool.submit(client.list_children, number) for number in container_numbers
        }
        return {number: future.result() for number, future in futures.items()}


def _fetch_dependencies(
    client: forge.BoardSource, issue_numbers: tuple[int, ...]
) -> dict[int, tuple[board.IssueDependency, ...]]:
    """Every named issue's `blocked_by` dependencies (#150), at most
    `BOARD_CHILD_FETCH_CONCURRENCY` `gh` subprocesses at a time -- run only
    after `_board`'s base reads and children wave have already finished, so
    peak concurrent `gh` subprocesses never exceeds today's 3+4."""
    if not issue_numbers:
        return {}
    workers = min(len(issue_numbers), BOARD_CHILD_FETCH_CONCURRENCY)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            number: pool.submit(client.list_board_dependencies, number) for number in issue_numbers
        }
        return {number: future.result() for number, future in futures.items()}


def _validated_dependencies(
    issues: tuple[board.Issue, ...], fetched: dict[int, tuple[board.IssueDependency, ...]]
) -> dict[int, tuple[board.IssueDependency, ...]]:
    """`fetched`, keyed by exactly the positive-`blocked_by_count` issues,
    each list checked against its own listing count (#150 §6): a length
    mismatch or a duplicated dependency is the same class of malformed
    forge response as a disagreeing container summary -- named loud rather
    than guessed through."""
    validated: dict[int, tuple[board.IssueDependency, ...]] = {}
    for issue in issues:
        if issue.blocked_by_count <= 0:
            continue
        dependencies = fetched.get(issue.number, ())
        distinct = {dependency.reference for dependency in dependencies}
        length = len(dependencies)
        if length != issue.blocked_by_count or len(distinct) != length:
            raise forge.ForgeMalformedResponseError(
                f"GitHub returned a malformed board blocked-by list for #{issue.number}: "
                f"listing total_blocked_by={issue.blocked_by_count}, detail length={length}"
            )
        validated[issue.number] = dependencies
    return validated


def _resolve_toplevel() -> Path:
    """The checkout's toplevel, for every command that resolves the
    repository's board configuration (#150) -- its priority ladder, its idea
    label, its canonical remote, and the body pin it is still checked
    against. Without a working tree there is no configuration to read, so
    the command refuses rather than running on guessed defaults (#178)."""
    try:
        return Path(checkout._git_output(["rev-parse", "--show-toplevel"]))
    except protocol.ClaimError as error:
        raise protocol.ClaimUnavailableError(
            "this command reads the repository's body contract from "
            ".agent-claim/board.toml and needs a checkout (a shallow one is "
            f"enough): {error}"
        ) from error


def _board_config(toplevel: Path) -> board.BoardConfig:
    """The repository's board configuration, refused before
    `board.load_config` ever runs when `.agent-claim/board.toml` is not
    actually tracked by git (#315): a `.gitignore` that ignores every
    dot-directory keeps a freshly written pin off every worktree unless it
    is force-added, and the prior silent `storage = github` default then
    surfaced as the unrelated "no forge adapter for host ..." the moment a
    forge command resolved a non-GitHub canonical remote. `board.load_config`
    itself stays a pure filesystem reader (Layers contract) -- this is the
    one place, reached by every store command, that can see whether git
    actually tracks the pin."""
    if not checkout.path_is_tracked(board.CONFIG_PATH.as_posix()):
        raise protocol.ClaimUnavailableError(
            f"{board.CONFIG_PATH} is not tracked in this checkout, so its "
            f"storage pin cannot be trusted: git add -f {board.CONFIG_PATH}"
        )
    return board.load_config(toplevel / board.CONFIG_PATH)


def _load_board_config(client: forge.BoardSource, toplevel: Path) -> board.BoardConfig:
    """The repository's board configuration, validated against what `client`
    can actually do (#150 §3): reading a body's dependencies requires
    `list_board_dependencies` at read-only or better, and the typed block is
    the one body grammar, so every repository needs it."""
    config = _board_config(toplevel)
    if (
        client.capability(forge.ForgeOperation.LIST_BOARD_DEPENDENCIES)
        is forge.Capability.UNSUPPORTED
    ):
        raise protocol.ClaimError(
            "reading work-item bodies requires forge operation list_board_dependencies"
        )
    return config


@dataclass(frozen=True)
class _BoardFetch:
    """`_board`'s own result, plus the recently-merged pull requests it reads
    to classify each item's `Stage.CODE_LANDED` (#276) -- carried alongside
    `board` so `board --html`'s Landungen section can pair a landed item
    with its pull request without a second `gh` read `board` did not
    already perform."""

    board: board.Board
    recent_merged_pull_requests: tuple[board.PullRequest, ...]


def _board(
    client: forge.BoardSource,
    claims: tuple[protocol.ActiveClaim, ...],
    *,
    issues: tuple[board.Issue, ...] | None = None,
    claim_ages: Mapping[str, datetime] | None = None,
    dependencies: dict[int, tuple[board.IssueDependency, ...]] | None = None,
) -> _BoardFetch:
    now = datetime.now(UTC)
    config = _load_board_config(client, _resolve_toplevel())
    if issues is None:
        issues = client.list_open_board_issues()
    since = _merged_pull_request_floor(issues, now)
    # A container whose own summary already says 0 (or carries no summary at
    # all, `children_total is None`) can never own an open child either way:
    # `_container_progress` returns no progress at all without both numbers,
    # and returns an empty open-children list when they're both 0 -- exactly
    # what an absent `children` entry already defaults to. Fetching its
    # detail list would cost a request `board` never needed (issue #168).
    container_numbers = tuple(
        issue.number
        for issue in issues
        if issue.kind is board.ItemKind.CONTAINER and issue.children_total
    )
    # Open and recently-merged pull requests and each container's children
    # are independent reads once `since` is known, so fetching them on
    # separate threads instead of one after another overlaps their `gh`
    # subprocess wait time. Children get their own executor
    # (`_fetch_children`) so their concurrency stays capped at
    # `BOARD_CHILD_FETCH_CONCURRENCY` even once these two base reads finish
    # and free their own pool's workers. The dependency wave runs afterward
    # instead (below), so peak concurrent `gh` subprocesses stays at 2+4,
    # then at most 4.
    with ThreadPoolExecutor(max_workers=2) as pool:
        open_pull_requests = pool.submit(client.list_open_board_pull_requests)
        merged_pull_requests = pool.submit(client.list_recent_merged_board_pull_requests, since)
        children = _fetch_children(client, container_numbers)
        pull_requests = (open_pull_requests.result(), merged_pull_requests.result())
    if dependencies is None:
        # A caller that already fetched and validated this exact candidate
        # set -- `_release_landing`'s own `list_board_dependencies` wave,
        # issue #256 review -- passes it in above instead of paying for a
        # second round trip here.
        dependencies = _validated_dependencies(
            issues,
            _fetch_dependencies(
                client, tuple(issue.number for issue in issues if issue.blocked_by_count > 0)
            ),
        )
    trunk_landings = checkout.trunk_landings(config.canonical_remote, TRUNK_LANDING_DEPTH)
    return _BoardFetch(
        board.build_board(
            board.BoardBuildInputs(
                issues=issues,
                open_pull_requests=pull_requests[0],
                recent_merged_pull_requests=pull_requests[1],
                claims=claims,
                config=config,
                repository=client.repository.path,
                now=now,
                trunk_landings=tuple(landing.committed_at for landing in trunk_landings),
                trunk_landed_work_items=board.trunk_landed_work_items(
                    landing.classification for landing in trunk_landings
                ),
                children=children,
                dependencies=dependencies,
                requests=client.requests,
                claim_ages=claim_ages or {},
                open_pull_requests_supported=(
                    client.capability(forge.ForgeOperation.LIST_OPEN_BOARD_PULL_REQUESTS)
                    is not forge.Capability.UNSUPPORTED
                ),
                landings_derivable=(
                    client.capability(forge.ForgeOperation.LIST_RECENT_MERGED_BOARD_PULL_REQUESTS)
                    is not forge.Capability.UNSUPPORTED
                ),
            )
        ),
        pull_requests[1],
    )


@dataclass(frozen=True)
class _RulingsRow:
    """One `rulings` row: the item, its open/total counts, and every one of
    its `[[expectation]]` lines (open and already-ruled alike) in block
    order -- the detail `rulings` prints beneath the item's own summary."""

    item: board.BoardItem
    progress: board.ExpectationProgress
    lines: tuple[board.ExpectationLine, ...]


def _rulings_rows(
    projected: board.Board, bodies: Mapping[int, str], *, storage: board.Storage
) -> tuple[_RulingsRow, ...]:
    """Every open board item that still carries an open expectation line,
    board-ranked then by fewer open lines then issue number (unchanged from
    before #240), each paired with its lines read fresh from `bodies` --
    `projected.items` itself carries only the open/total counters. `storage`
    is forwarded to `expectation_lines` unchanged (issue #248): a state-ref
    body's `[record]` table must read as a known key, not a malformed one."""
    ranked = sorted(
        (
            (item, item.expectation_progress)
            for item in projected.items
            if item.expectation_progress.open > 0
        ),
        key=lambda entry: (*board.board_rank(entry[0])[:2], entry[1].open, entry[0].number),
    )
    return tuple(
        _RulingsRow(
            item, progress, board.expectation_lines(bodies.get(item.number, ""), storage=storage)
        )
        for item, progress in ranked
    )


def _rulings_line_json(line: board.ExpectationLine) -> dict[str, object]:
    payload: dict[str, object] = {
        "index": line.index,
        "text": line.text,
        "state": board.expectation_line_state(line),
    }
    payload.update(
        (key, value)
        for key, value in (
            ("question", line.question),
            ("example", line.example),
            ("picture", line.picture),
        )
        if value is not None
    )
    return payload


def _rulings_line_text(line: board.ExpectationLine) -> str:
    state = board.expectation_line_state(line)
    return f"  {line.index} {state}: {board.expectation_line_summary(line)}"


def _rulings_row_text(row: _RulingsRow, storage: board.Storage) -> str:
    label = board.item_label(row.item.number, storage)
    header = f"{label} {row.progress.open}/{row.progress.total}: {row.item.title}"
    return "\n".join((header, *(_rulings_line_text(line) for line in row.lines)))


def _rulings(
    projected: board.Board, bodies: Mapping[int, str], *, as_json: bool, storage: board.Storage
) -> None:
    rows = _rulings_rows(projected, bodies, storage=storage)
    if as_json:
        print(
            json.dumps(
                [
                    {
                        "number": row.item.number,
                        "title": row.item.title,
                        "open": row.progress.open,
                        "total": row.progress.total,
                        "lines": [_rulings_line_json(line) for line in row.lines],
                    }
                    for row in rows
                ]
            )
        )
        return
    if not rows:
        print("No open expectation lines.")
        return
    print("\n".join(_rulings_row_text(row, storage) for row in rows))


def _ruling_pull_hint(item: board.BoardItem) -> str | None:
    if item.expectation_state is board.ExpectationState.PROPOSED:
        return "expectations unruled: refine before the pull"
    if not item.ruling_old:
        return None
    return f"ruled {item.ruling_landings} landings ago: refine again at the pull"


def _next_action_item_argument(number: int, storage: board.Storage) -> str:
    """The item reference `_parse_item_ref` accepts back as `_next_action_command`'s
    printed `aco` invocation's positional argument: the bare number under
    `Storage.GITHUB` -- unchanged, byte-identical to every command printed
    before the state-ref pin existed -- and `board.item_label`'s own id under
    `Storage.STATE_REF`, so a printed command is one a person can paste back
    in (issue #292, residual of #300)."""
    if storage is board.Storage.STATE_REF:
        return board.item_label(number, storage)
    return str(number)


def _next_action_command(
    action: board.WorkItemAction | board.CutSliceAction, storage: board.Storage
) -> str:
    """The exact `aco` invocation `_next` prints and `_next --json` carries
    as `command` -- one owner so text and JSON never name a different
    command for the same action. `close_container` has none: there is no
    command to run, and neither grammar invents one."""
    if isinstance(action, board.WorkItemAction):
        item_argument = _next_action_item_argument(action.item.number, storage)
        return f"aco claim {item_argument} --scope <paths>"
    container_argument = _next_action_item_argument(action.container.number, storage)
    return f'aco cut {container_argument} --title "{action.cut_title}"'


def _next_action_payload(action: board.NextAction, storage: board.Storage) -> dict[str, object]:
    """The action-specific fields `_next_json` adds beyond `recovery`/`skipped`."""
    if isinstance(action, board.WorkItemAction):
        item = action.item
        payload: dict[str, object] = {
            "action": "work_item",
            "number": item.number,
            "score": item.score,
            "title": item.title,
            "next": item.next_step,
            "command": _next_action_command(action, storage),
            "ruling_landings": item.ruling_landings,
            "ruling_old": item.ruling_old,
        }
        hint = _ruling_pull_hint(item)
        if hint is not None:
            payload["ruling_hint"] = hint
        return payload
    if isinstance(action, board.CutSliceAction):
        return {
            "action": "cut_slice",
            "number": action.container.number,
            "title": action.container.title,
            "slice": action.next_step,
            "cut_title": action.cut_title,
            "command": _next_action_command(action, storage),
        }
    return {
        "action": "close_container",
        "number": action.container.number,
        "closed": action.container_progress.closed,
        "total": action.container_progress.total,
        "next_step": action.next_step,
    }


def _next_json(
    action: board.NextAction | None,
    skipped: tuple[board.BoardItem, ...],
    recovery: tuple[board.BoardItem, ...],
    storage: board.Storage,
) -> int:
    payload: dict[str, object] = {
        "action": None,
        "recovery": [
            {
                "number": recovery_item.number,
                "title": recovery_item.title,
                "step": board.RECOVERY_STEP,
            }
            for recovery_item in recovery
        ],
        "skipped": [
            {"number": skipped_item.number, "reason": skipped_item.actionable_reason}
            for skipped_item in skipped
        ],
    }
    if action is not None:
        payload.update(_next_action_payload(action, storage))
    print(json.dumps(payload))
    return 0


def _next_action_lines(action: board.NextAction, storage: board.Storage) -> list[str]:
    """The action-specific lines `_next` prints before `SKIPPED`."""
    if isinstance(action, board.WorkItemAction):
        item = action.item
        lines = [
            f"{board.item_label(item.number, storage)} score {item.score}: {item.title}",
            f"Next: {item.next_step}",
            f"Run: {_next_action_command(action, storage)}",
            "<paths> cannot be derived; take the files to claim from the item body.",
        ]
        hint = _ruling_pull_hint(item)
        if hint is not None:
            lines.append(hint)
        return lines
    container_label = board.item_label(action.container.number, storage)
    if isinstance(action, board.CutSliceAction):
        return [
            f"cut_slice {container_label}: {action.next_step}",
            f"Next: {_next_action_command(action, storage)}",
        ]
    if action.next_step is not None:
        return [f"close_container {container_label}: {action.next_step}"]
    progress = action.container_progress
    return [
        f"close_container {container_label}: "
        f"{progress.closed}/{progress.total} children closed, no Next work"
    ]


def _next(
    action: board.NextAction | None,
    skipped: tuple[board.BoardItem, ...],
    recovery: tuple[board.BoardItem, ...],
    storage: board.Storage,
) -> int:
    """A landed-but-open item is named before anything new is pulled."""
    lines: list[str] = []
    if recovery:
        lines.append("RECOVERY")
        lines.extend(
            f"{board.item_label(recovery_item.number, storage)}: {board.RECOVERY_STEP}"
            for recovery_item in recovery
        )
        lines.append("")
    lines.extend(
        _next_action_lines(action, storage) if action is not None else ["No actionable item."]
    )
    if skipped:
        skipped_lines = (
            f"{board.item_label(skipped_item.number, storage)}: {skipped_item.actionable_reason}"
            for skipped_item in skipped
        )
        lines.extend(("", "SKIPPED", *skipped_lines))
    print("\n".join(lines))
    return 0


def _unworkable(projected: board.Board) -> tuple[board.BoardItem, ...]:
    return tuple(item for item in projected.items if not item.actionable)


@dataclass(frozen=True)
class SliceCheck:
    """One slice-rule finding — the `check` table `#79` rules.

    `slice`/`issue` carry whichever numbers the message names, so a `--json`
    caller can act on the finding without re-parsing `text`; either is
    `None` when the check has nothing of that kind to name.
    """

    level: str
    check: str
    text: str
    slice: int | None = None
    issue: int | None = None

    def render(self) -> str:
        prefix = "ERROR" if self.level == "error" else "WARNING"
        return f"{prefix}: {self.text}"

    def as_json(self) -> dict[str, object]:
        return {
            "level": self.level,
            "check": self.check,
            "text": self.text,
            "slice": self.slice,
            "issue": self.issue,
        }


def _fetch_issue_reference(client: forge.ForgeReader, number: int) -> forge.ItemReference:
    """The live state, title, and body of issue `number` from `client`.

    Called only for a claim target that the already-fetched open board
    didn't resolve as OPEN — a closed or missing issue never appears in
    `list_open_board_issues`, so those two states need their own targeted
    lookup; this is that lookup, kept to one issue at a time rather than a
    repository-wide query.
    """
    return client.item_reference(number)


def _issue_reference_state(
    client: forge.ForgeReader,
    open_by_number: dict[int, board.Issue],
    number: int,
) -> tuple[forge.ItemState, str | None, str | None]:
    open_issue = open_by_number.get(number)
    if open_issue is not None:
        return forge.ItemState.OPEN, open_issue.title, open_issue.body
    reference = _fetch_issue_reference(client, number)
    return reference.state, reference.title, reference.body


def _out_of_order_check(
    projected: board.Board, issue: int | None, out_of_order_reason: str | None
) -> SliceCheck | None:
    highest = board.highest_scored_actionable(projected)
    if highest is None or issue is None:
        return None
    claimed_item = next((item for item in projected.items if item.number == issue), None)
    if claimed_item is None or board.board_rank(highest) >= board.board_rank(claimed_item):
        return None
    return SliceCheck(
        "warning" if out_of_order_reason is not None else "error",
        "out-of-order",
        f"higher-priority actionable item #{highest.number} "
        f"(score {highest.score}) is free: {highest.title}; "
        "use --out-of-order REASON to proceed",
        issue=highest.number,
    )


def _blocked_check(
    item: board.BoardItem | None,
    out_of_order_reason: str | None,
    repository: str,
    storage: board.Storage,
) -> SliceCheck | None:
    if item is None or not item.open_blockers:
        return None
    blockers = ", ".join(
        board.open_blocker_label(reference, repository, storage) for reference in item.open_blockers
    )
    return SliceCheck(
        "warning" if out_of_order_reason is not None else "error",
        "blocked",
        f"#{item.number} is blocked by {blockers} (open); "
        "pass --out-of-order REASON to claim it anyway",
        issue=item.number,
    )


def _parent_checks(
    client: forge.ForgeReader, repository: str, issue: int, title: str
) -> SliceCheck | None:
    """Warn when a slice-shaped title names a parent GitHub does not record as one."""
    match = board.slice_title_match(title)
    if match is None:
        return None
    slice_number, parent_issue = match
    parent = client.parent_issue(issue)
    if parent is not None and parent.reference == board.IssueReference(repository, parent_issue):
        return None
    return SliceCheck(
        "warning",
        "missing-parent",
        f"looks like slice {slice_number} of #{parent_issue} but is no sub-issue "
        f"of #{parent_issue}; the parent inherits nothing",
        slice=slice_number,
        issue=parent_issue,
    )


def _malformed_checks(item: board.BoardItem) -> tuple[SliceCheck, ...] | None:
    """The one refusal a malformed block body gets (#150) -- every other
    body-contract check (blocker state, completeness) never runs, since
    neither the parsed projections nor the blocker set can be trusted once
    the body itself failed to read."""
    if item.read_state is board.BodyReadState.MALFORMED:
        return tuple(
            SliceCheck("error", "body-contract", board.body_defect_text(defect))
            for defect in item.contract.defects
        )
    return None


def _body_contract_checks(item: board.BoardItem) -> tuple[SliceCheck, ...]:
    malformed = _malformed_checks(item)
    if malformed is not None:
        return malformed
    contract = item.contract
    checks = [SliceCheck("error", "body-contract", defect.message) for defect in contract.defects]
    # Read the two atomic facts directly rather than `item.actionable_reason`:
    # that reason is the *first* one `_actionable_reason` finds (frozen,
    # claimed, blocked, then incomplete), so an item that is both blocked and
    # incomplete would report only "blocked" there -- masking the incomplete
    # body this check exists to name. A freshly `cut` child (an incomplete
    # but defect-free skeleton) is refused here exactly as it is invisible to
    # `next`, regardless of what else may also be true of it.
    if not item.contract_complete and not item.projectionless_idea:
        missing = ", ".join(board.missing_or_empty_sections(contract))
        checks.append(
            SliceCheck(
                "error",
                "body-incomplete",
                f"#{item.number} body incomplete: {missing}",
                issue=item.number,
            )
        )
    return tuple(checks)


@dataclass(frozen=True)
class BoardReferenceLookup:
    """The board client, its repository, and the currently open issues it can
    resolve `#reference`s against — what every cross-issue slice/parent check
    below needs to look a referenced issue up."""

    client: forge.ForgeReader
    repository: str
    open_by_number: dict[int, board.Issue]


def _slice_rule_checks(
    lookup: BoardReferenceLookup,
    issue: int,
    projected: board.Board,
    out_of_order_reason: str | None,
    storage: board.Storage,
) -> tuple[SliceCheck, ...]:
    checks: list[SliceCheck] = []
    out_of_order = _out_of_order_check(projected, issue, out_of_order_reason)
    if out_of_order is not None:
        checks.append(out_of_order)
    item = next((item for item in projected.items if item.number == issue), None)
    if item is not None and item.kind is board.ItemKind.CONTAINER:
        checks.append(
            SliceCheck("error", "container", f"#{issue} is a container; claim a child", issue=issue)
        )
    blocked = _blocked_check(item, out_of_order_reason, lookup.repository, storage)
    if blocked is not None:
        checks.append(blocked)
    state, title, _body = _issue_reference_state(lookup.client, lookup.open_by_number, issue)
    if state is forge.ItemState.CLOSED:
        checks.append(SliceCheck("error", "closed-issue", f"issue #{issue} is closed", issue=issue))
    elif state is forge.ItemState.MISSING:
        checks.append(
            SliceCheck("error", "missing-issue", f"issue #{issue} does not exist here", issue=issue)
        )
    if item is not None:
        checks.extend(_body_contract_checks(item))
    if title is not None:
        parent_check = _parent_checks(lookup.client, lookup.repository, issue, title)
        if parent_check is not None:
            checks.append(parent_check)
    return tuple(checks)


def _refuse_claim(json_mode: bool, issue: int | None, checks: tuple[SliceCheck, ...]) -> None:
    if json_mode:
        payload = {"refused": True, "issue": issue, "checks": [c.as_json() for c in checks]}
        print(json.dumps(payload))
        return
    for check in checks:
        print(check.render(), file=sys.stderr)


def _claim_defect(
    claims: tuple[protocol.ActiveClaim, ...],
    detail: forge.Landing,
    identity: protocol.ClaimIdentity,
) -> board.ClassificationDefect | None:
    """A landing declares only what its own head branch holds a live store claim on."""
    matching = next(
        (
            claim
            for claim in claims
            if claim.identity == identity and claim.branch == detail.source_branch
        ),
        None,
    )
    if matching is None:
        subject = (
            f"claim for #{identity.issue}"
            if isinstance(identity, protocol.IssueIdentity)
            else "issue-less lane claim"
        )
        return board.ClassificationDefect(
            f"has no active {subject} on branch {detail.source_branch!r}"
        )
    return None


def _no_item_defect(
    claims: tuple[protocol.ActiveClaim, ...],
    repository: str,
    detail: forge.Landing,
) -> board.ClassificationDefect | None:
    """Why this repository does not accept an issue-less landing as declared.

    A `No-Item` lane owns no issue, so it needs its own lane claim and may
    retire nothing: a closing reference here would close an item no claim and
    no `Work-Item:` line ever named.
    """
    claim_defect = _claim_defect(claims, detail, protocol.LaneIdentity())
    if claim_defect is not None:
        return claim_defect
    closing = board.closing_references(detail.body, repository)
    if closing:
        named = ", ".join(str(reference) for reference in sorted(closing, key=str))
        return board.ClassificationDefect(
            f"declares no work item but closes {named}; name it as the work item"
        )
    return None


@dataclass(frozen=True)
class _LandingCheckContext:
    """What every landing-classification helper needs beyond the pull
    request's own detail and the store's live claims: which forge to read
    and which repository owns the check -- grouped so the cluster of helpers
    below stays under the five-argument limit as claims moved from a
    `client`-read ledger walk to a separately threaded store snapshot."""

    client: forge.ForgeReader
    repository: str


@dataclass(frozen=True)
class _ParentRequirement:
    """What an item's parent demands of the pull request that lands the item.

    `last_child` says closing the parent is *permitted* -- this landing
    closes the parent's one remaining open child; `closing_required` narrows
    that to *required*, which holds only when the parent's own `Next` line
    names no further work.
    """

    reference: board.IssueReference
    closing_required: bool
    last_child: bool


def _parent_body_finding(reference: board.IssueReference, parsed: board.ParsedBody) -> str:
    """Why a malformed parent body refuses the last-child rule before its
    `Next` line is ever consulted (#150)."""
    defect = parsed.contract.defects[0]
    return f"has parent {reference} with a {board.body_defect_text(defect)}"


def _parent_reference_defect(
    parent: board.ParentIssue, repository: str
) -> board.ClassificationDefect | None:
    """Whether the parent itself is even a same-repository container --
    checked before its body is read at all."""
    if parent.reference.repository != repository:
        return board.ClassificationDefect(
            f"has parent {parent.reference} in another repository, "
            "whose children this check cannot read"
        )
    if parent.kind is not board.ItemKind.CONTAINER:
        kind_text = parent.kind.value if parent.kind is not None else "unknown"
        return board.ClassificationDefect(
            f"has parent {parent.reference} of kind {kind_text}, which is not a "
            "container; only a container holds children"
        )
    return None


def _parent_requirement(
    context: _LandingCheckContext,
    item: board.IssueReference,
) -> _ParentRequirement | board.ClassificationDefect | None:
    """The parent's demand, read from GitHub's sub-issue relation and its
    body under the repository's own pin (#150).

    Closing a parent's last open child completes the parent only when the
    parent's own `Next` line names no further work; otherwise the container
    keeps dispatching slices, and the landing may close the parent (its one
    remaining child) but need not. A parent keeping other open children
    stays open, and must say what happens next.
    """
    parent = context.client.parent_issue(item.number)
    if parent is None:
        return None
    reference_defect = _parent_reference_defect(parent, context.repository)
    if reference_defect is not None:
        return reference_defect
    parsed_parent = board.parse_body(parent.body)
    if parsed_parent.read_state is not board.BodyReadState.VALID:
        return board.ClassificationDefect(_parent_body_finding(parent.reference, parsed_parent))
    remaining = tuple(
        child
        for child in context.client.list_children(parent.reference.number)
        if child.state is board.ChildState.OPEN and child.number != item.number
    )
    if not remaining:
        return _ParentRequirement(
            parent.reference,
            not board.has_further_work(parsed_parent.contract.next),
            True,
        )
    if not parsed_parent.contract.next:
        children = "child" if len(remaining) == 1 else "children"
        return board.ClassificationDefect(
            f"leaves parent {parent.reference} open with {len(remaining)} other open "
            f"{children}, whose body carries no Next line"
        )
    return _ParentRequirement(parent.reference, False, False)


def _closing_defect(
    detail: forge.Landing,
    repository: str,
    item: board.IssueReference,
    requirement: _ParentRequirement | None,
) -> board.ClassificationDefect | None:
    """Which issues this landing must close, and that it closes nothing else."""
    closing = board.closing_references(detail.body, repository)
    if item not in closing:
        return board.ClassificationDefect(f"carries no closing reference for its work item {item}")
    if (
        requirement is not None
        and requirement.closing_required
        and requirement.reference not in closing
    ):
        return board.ClassificationDefect(
            f"closes the last open child of parent {requirement.reference}; close the parent too"
        )
    permitted_parent = (
        {requirement.reference} if requirement is not None and requirement.last_child else set()
    )
    besides = tuple(sorted(closing - {item} - permitted_parent, key=str))
    if besides:
        named = ", ".join(str(reference) for reference in besides)
        return board.ClassificationDefect(
            f"closes {named} besides its work item {item}; a pull request lands one item"
        )
    return None


def _work_item_defect(
    context: _LandingCheckContext,
    claims: tuple[protocol.ActiveClaim, ...],
    detail: forge.Landing,
    item: board.IssueReference,
) -> board.ClassificationDefect | None:
    """Why this repository does not accept `item` as the landing pull request's work item."""
    if item.repository != context.repository:
        return board.ClassificationDefect(
            f"names work item {item} of another repository, which holds no claim here"
        )
    claim_defect = _claim_defect(claims, detail, protocol.IssueIdentity(item.number))
    if claim_defect is not None:
        return claim_defect
    requirement = _parent_requirement(context, item)
    if isinstance(requirement, board.ClassificationDefect):
        return requirement
    return _closing_defect(detail, context.repository, item, requirement)


def _checked_classification(
    context: _LandingCheckContext,
    claims: tuple[protocol.ActiveClaim, ...],
    detail: forge.Landing,
) -> board.Classification | board.ClassificationDefect:
    if detail.source_repository.path != context.repository:
        return board.ClassificationDefect(
            f"proposes a branch of {detail.source_repository}; cross-repository pull "
            "requests are not classified"
        )
    classification = board.parse_pull_request_classification(detail.body, context.repository)
    if isinstance(classification, board.ClassificationDefect):
        return classification
    default_branch = context.client.default_branch()
    if detail.target_branch != default_branch:
        return board.ClassificationDefect(
            f"targets {detail.target_branch!r}, not the default branch {default_branch!r}"
        )
    defect = (
        _no_item_defect(claims, context.repository, detail)
        if isinstance(classification, board.NoItemClassification)
        else _work_item_defect(context, claims, detail, classification.item)
    )
    return classification if defect is None else defect


class CheckKind(StrEnum):
    """Which subject one `check` run read -- the `--json` discriminator.

    Three values, because that is what the one dispatch request actually
    distinguishes: GitHub gives issues and pull requests a single number
    space, so a number that is not there was never proven to be either.
    """

    PULL_REQUEST = "pull_request"
    ISSUE = "issue"
    MISSING = "missing"


@dataclass(frozen=True)
class CheckOutcome:
    """One `check` answer: the line a human reads, and the reason a caller
    acts on -- `None` when the subject passed."""

    kind: CheckKind
    number: int
    line: str
    refused: str | None = None

    def as_json(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "ok": self.refused is None,
            "kind": self.kind.value,
            "number": self.number,
        }
        if self.refused is not None:
            payload["refused"] = self.refused
        return payload

    def report(self, *, as_json: bool) -> int:
        if as_json:
            print(json.dumps(self.as_json()))
        else:
            print(self.line, file=sys.stderr if self.refused is not None else sys.stdout)
        return 1 if self.refused is not None else 0


def _pull_request_check(
    client: forge.ForgeReader,
    claims: tuple[protocol.ActiveClaim, ...],
    repository: str,
    number: int,
) -> CheckOutcome:
    detail = client.landing(number)
    context = _LandingCheckContext(client, repository)
    checked = _checked_classification(context, claims, detail)
    if isinstance(checked, board.ClassificationDefect):
        return CheckOutcome(
            CheckKind.PULL_REQUEST,
            detail.number,
            f"REFUSED: pull request #{detail.number} {checked.message}",
            checked.message,
        )
    return CheckOutcome(
        CheckKind.PULL_REQUEST,
        detail.number,
        f"PR #{detail.number} by {detail.author} declares {checked}",
    )


def _missing_number(repository: str, number: int) -> CheckOutcome:
    """A number neither mode can read, named without claiming which of the
    two it would have been."""
    finding = f"does not exist in {repository}"
    return CheckOutcome(CheckKind.MISSING, number, f"REFUSED: #{number} {finding}", finding)


def _issue_line(number: int, finding: str) -> str:
    """The one shape every issue-mode answer takes."""
    return f"ISSUE #{number} {finding}"


def _refused_issue(number: int, finding: str) -> CheckOutcome:
    return CheckOutcome(CheckKind.ISSUE, number, _issue_line(number, finding), finding)


def _body_shape_defects(
    body: str, *, storage: board.Storage = board.Storage.GITHUB
) -> tuple[str, ...]:
    """Every finding a body's own shape can carry without asking a forge
    anything -- malformed (one sentence per schema defect) or incomplete
    (one joined sentence, matching `check <item>`'s own wording).
    `_issue_check` and `body --check` (issue #262) both read this; neither
    writes a second rendering of these sentences. `storage` gates the one
    storage-specific extension, `[record]` (issue #248)."""
    parsed = board.parse_body(body, storage=storage)
    if parsed.read_state is board.BodyReadState.MALFORMED:
        return tuple(board.body_defect_text(defect) for defect in parsed.contract.defects)
    missing = board.missing_or_empty_sections(parsed.contract)
    return (f"body incomplete: {', '.join(missing)}",) if missing else ()


def _issue_check(
    client: forge.ForgeReader,
    repository: str,
    body: str,
    number: int,
    *,
    storage: board.Storage,
) -> CheckOutcome:
    """Whether this issue's body is the contract a builder can start from:
    readable, complete, and unblocked. Its dependencies come from GitHub's
    own `blocked_by` relation, or the state-ref item's own `[record]` table
    under `storage = "state-ref"` -- a body never states them itself."""
    shape_defects = _body_shape_defects(body, storage=storage)
    if shape_defects:
        return _refused_issue(number, shape_defects[0])
    blockers = board.open_dependency_blockers(client.list_board_dependencies(number), repository)
    if blockers:
        named = ", ".join(
            board.open_blocker_label(blocker, repository, storage) for blocker in blockers
        )
        return _refused_issue(number, f"blocked by {named}")
    return CheckOutcome(CheckKind.ISSUE, number, _issue_line(number, "body ok"))


BODY_TEMPLATE_KINDS = ("task", "feature", "container")
DEFAULT_BODY_TEMPLATE_KIND = "task"


def _body_template(kind: str, parent: int | None) -> str:
    """The skeleton `body --template` prints for `kind` (issue #262): the
    same `board.BLOCK_CHILD_SKELETON` `cut` writes for a task or feature
    child, or `board.BLOCK_CONTAINER_SKELETON` for a container -- with
    `parent`'s own `Parent:` line ahead of it when given, exactly as `cut`
    composes one for its own fresh child (`_body_with_parent`)."""
    skeleton = board.BLOCK_CONTAINER_SKELETON if kind == "container" else board.BLOCK_CHILD_SKELETON
    return _body_with_parent(skeleton, parent)


def _read_body_check_input() -> str:
    """`body --check`'s body text, read from stdin only (issue #262 Sonar
    S8707): an agent pipes the file in (`aco body --check < body.md`)
    rather than naming a path the CLI would have to trust."""
    try:
        return sys.stdin.read()
    except UnicodeDecodeError as error:
        raise protocol.ClaimError(
            f"stdin is not valid UTF-8: {error}; pipe the body as UTF-8 text"
        ) from error


def _body_check_report(defects: tuple[str, ...], *, as_json: bool) -> int:
    """The one rendering of a `body --check` answer: `check <item>`'s own
    sentences (`_body_shape_defects`), never truncated to the first, since
    there is no live item here to refuse a single verdict about."""
    if as_json:
        print(json.dumps({"ok": not defects, "defects": list(defects)}))
    elif defects:
        for defect in defects:
            print(defect, file=sys.stderr)
    else:
        print("body ok")
    return 1 if defects else 0


def _cmd_body(parsed: argparse.Namespace) -> int:
    """`body` is forge-free (issue #262), the same way `status` and
    `bootstrap` are (issue #245): a template is composed from this
    repository's own skeleton owners, and a check reads stdin only --
    never an issue, a live claim, a filesystem path, or the forge's own
    `blocked_by` relation, so it never resolves a `_LazyForge` at all
    (`main` dispatches it outside `_dispatch`, exactly like `status`).
    `--check` still reads the repository's own storage pin (issue #287):
    `[record]` is a known key under `storage = "state-ref"` and an unknown
    one under `storage = "github"`, the same gate `_state_ref_forge`'s own
    items already read through `_decode_item`."""
    if parsed.check:
        if parsed.kind is not None or parsed.parent is not None:
            raise protocol.ClaimUnavailableError(
                "--kind and --parent apply only to --template, not --check"
            )
        config = _board_config(_resolve_toplevel())
        defects = _body_shape_defects(_read_body_check_input(), storage=config.storage)
        return _body_check_report(defects, as_json=parsed.json)
    if parsed.json:
        raise protocol.ClaimUnavailableError("--json applies only to --check, not --template")
    print(_body_template(parsed.kind or DEFAULT_BODY_TEMPLATE_KIND, parsed.parent), end="")
    return 0


def _release_outcome(merged: int | None, abandoned: str | None) -> protocol.ReleaseOutcome:
    if merged is not None:
        return protocol.MergedRelease(merged)
    return protocol.AbandonedRelease(
        protocol._outbound_text(abandoned, "abandoned reason", maximum=512)
    )


def _verify_merged_release(
    client: forge.ForgeReader,
    repository: str,
    identity: protocol.ClaimIdentity,
    merged: protocol.MergedRelease,
) -> None:
    """Refuse a `--merged` release the landing itself does not support."""
    detail = client.landing(merged.pull_request)
    if not detail.merged:
        raise protocol.ClaimUnavailableError(f"pull request #{detail.number} is not merged")
    default_branch = client.default_branch()
    if detail.target_branch != default_branch:
        raise protocol.ClaimUnavailableError(
            f"pull request #{detail.number} merged into {detail.target_branch!r}, "
            f"not the default branch {default_branch!r}"
        )
    classification = board.parse_pull_request_classification(detail.body, repository)
    if isinstance(classification, board.ClassificationDefect):
        raise protocol.ClaimUnavailableError(
            f"pull request #{detail.number} {classification.message}"
        )
    if isinstance(identity, protocol.LaneIdentity):
        if isinstance(classification, board.WorkItemClassification):
            raise protocol.ClaimUnavailableError(
                f"pull request #{detail.number} names {classification.item}; "
                "an issue-less lane needs a No-Item line"
            )
        return
    item = board.IssueReference(repository, identity.issue)
    if not isinstance(classification, board.WorkItemClassification) or classification.item != item:
        raise protocol.ClaimUnavailableError(
            f"pull request #{detail.number} names {classification}, not work item #{identity.issue}"
        )
    reference = _fetch_issue_reference(client, identity.issue)
    if reference.state is not forge.ItemState.CLOSED:
        raise protocol.ClaimUnavailableError(
            f"work item #{identity.issue} is {reference.state.value}, not closed"
        )


def _canonical_remote_name(toplevel: Path) -> str:
    """This repository's configured `canonical_remote` name (issue #176,
    Erwartung 7) -- every store command's own precondition, forge or not.
    A forge command additionally resolves and Erwartung-6-checks a forge
    target against it (`_resolved_forge_target` below); a forge-free command
    (`status`, `protect`, `bootstrap`, a lane `claim`/`rescope`/`release`)
    never does, so this alone is all it ever reads."""
    return _board_config(toplevel).canonical_remote


def _canonical_remote_location(canonical_remote: str) -> checkout.RemoteLocation:
    """`canonical_remote`'s configured URL, parsed host-neutrally (issue
    #245) -- read only from a forge command's own precondition, never from a
    forge-free one, so a non-GitHub canonical remote is no error there."""
    return checkout.parse_remote_location(checkout.remote_url(canonical_remote))


def _refuse_unsupported_forge_host(location: checkout.RemoteLocation) -> None:
    """The GitHub-storage forge command's precondition beyond Erwartung 6:
    GitHub is the one forge adapter reached by host, so a canonical remote
    on any other host refuses by name here, before ever asking `gh`, rather
    than failing deep inside `discover_repository` with GitHub's own "not a
    GitHub repository" text. Never reached under `storage = "state-ref"`
    (issue #248): that pin routes to `state_board.StateRefBoard` before
    this host check would even run, so a non-GitHub canonical remote is no
    error there.
    """
    if location.host != github.GITHUB_HOST:
        raise protocol.ClaimUnavailableError(f"no forge adapter for host {location.host}")


def _refuse_canonical_remote_mismatch(
    forge_target: forge.RepositoryId, location: checkout.RemoteLocation
) -> None:
    """Every forge command's shared refusal (Erwartung 6): the forge target
    (`--repo`, or whatever `discover_repository` resolved) must name the same
    repository the canonical remote's own URL points at, or nothing is read
    or written. Reached only when a forge target exists at all -- a
    forge-free command never resolves one, so this never runs for it.
    """
    if (forge_target.host, forge_target.path) != (location.host, location.path):
        raise protocol.ClaimUnavailableError(
            f"forge target {forge_target.path} does not match canonical remote "
            f"{location.path}; run aco from that repository's checkout"
        )


def _resolved_forge_target(repo: str | None, canonical_remote: str) -> forge.RepositoryId:
    """A forge command's own precondition (issue #176 Erwartung 6, issue
    #245): the repository this run's forge talks to, refused by name before
    it is ever built -- either this host has no adapter, or it names a
    different repository than the canonical remote's own URL does.
    """
    location = _canonical_remote_location(canonical_remote)
    _refuse_unsupported_forge_host(location)
    forge_target = github.discover_repository(repo, remote_url=checkout.origin_remote_url)
    _refuse_canonical_remote_mismatch(forge_target, location)
    return forge_target


def _refuse_repo_under_state_ref(repo: str | None) -> None:
    """`--repo` names a GitHub target; under `storage = "state-ref"` there
    is no host-based target to override (issue #248)."""
    if repo is not None:
        raise protocol.ClaimUnavailableError("--repo is meaningless under storage = state-ref")


# `release --merged`'s own residual under `storage = "state-ref"` (issue
# #283): `LANDING` stays `forge.Capability.UNSUPPORTED` through #230 slice
# 6, so a merged release still cannot verify its own pull request there --
# named by the offline path that works today, rather than leaking
# `state_board.py`'s own unsupported-capability wording.
STATE_REF_MERGED_LANDING_NOT_YET = (
    "state-ref cannot verify a merged pull request yet (#230 slice 6); land "
    'offline with `item close` and `release --abandoned "landed as <sha>"` until then'
)


def _refuse_state_ref_merged_release(toplevel: Path) -> None:
    """`release --merged`'s own precondition under `storage = "state-ref"`
    (issue #283): refused by name before `_verify_merged_release` ever calls
    `client.landing`, which `state_board.StateRefBoard` has no data for at
    all -- `claim`'s issue-scoped body check needs no equivalent gate
    anymore, since it only ever reads and `state_board.StateRefBoard` has
    read `board`/`next`/`check` since #248.
    """
    if _board_config(toplevel).storage is board.Storage.STATE_REF:
        raise protocol.ClaimUnavailableError(STATE_REF_MERGED_LANDING_NOT_YET)


@dataclass(frozen=True)
class _StoreItemWriter:
    """`state_board.ItemWriter`, implemented over `store` (issue #283): the
    one place this tool hashes an item's finished bytes into a blob and
    writes it through one `ItemWriteIntent` CAS transition (issue #279).
    `state_board.py` itself may not import `store` (Layers contract), so
    every actual git call a state-ref item write makes funnels through this
    one method.
    """

    worktree: Path
    canonical_remote: str

    def write_item(
        self, item_id: str, *, expected: protocol.ObjectId | None, content: bytes
    ) -> protocol.ObjectId:
        new_oid = store.hash_blob(self.worktree, content)
        intent = protocol.ItemWriteIntent(
            item_id=item_id,
            expected=expected,
            new_oid=new_oid,
            operation_id=uuid.uuid4().hex,
        )
        new_state = store.commit_transition(
            worktree=self.worktree,
            remote=self.canonical_remote,
            subject=f"write item {item_id}",
            intent=intent,
        )
        return new_state.items[item_id]


def _state_ref_forge(repo: str | None, canonical_remote: str) -> state_board.StateRefBoard:
    """The `state-ref` storage pin's forge (issues #248, #283): repository
    identity read host-neutrally from the canonical remote's own URL (issue
    #245's `RemoteLocation`, never GitHub's syntax), the default branch read
    from git, item content and blob oids read once through
    `store.read_item_files`/`ClaimState.items`, and a write port over that
    same `store` (`_StoreItemWriter`) -- this is the one place
    `state_board.StateRefBoard` is ever handed live data or a way to write
    it, since the Layers contract keeps that module from reaching `store`
    itself.

    `checkout.default_branch_name()` reads `origin/HEAD` specifically, not
    whatever `canonical_remote` names (a named residual: every repository
    piloting this pin today also names its canonical remote `origin`).
    """
    _refuse_repo_under_state_ref(repo)
    location = _canonical_remote_location(canonical_remote)
    repository = forge.RepositoryId(location.host, (), location.path)
    default_branch = checkout.default_branch_name()
    if default_branch is None:
        raise protocol.ClaimUnavailableError(
            "cannot resolve the default branch; run aco from a checkout with origin/HEAD set"
        )
    worktree = Path.cwd()
    state = store.fetch_state(worktree=worktree, remote=canonical_remote)
    item_files = {} if state.tip is None else store.read_item_files(worktree, state.tip)
    return state_board.StateRefBoard(
        repository=repository,
        default_branch=default_branch,
        item_files=item_files,
        item_oids=state.items,
        writer=_StoreItemWriter(worktree, canonical_remote),
    )


ITEM_NEW_GITHUB_REFUSAL = "items live on the forge; open the issue there"


def _cmd_item_new(parsed: argparse.Namespace) -> int:
    """`aco item new` (issues #285, #316): the one write path for a fresh
    state-ref item -- `StateRefBoard.create_item`, the same CAS write
    `cut`'s own `create_child` performs, generalized to an optional
    parent and origin -- so this module never grows a second way to
    create one. `--origin` binds the fresh item to a foreign forge issue
    (`items.parse_origin`'s own grammar, refused by `argparse` before this
    ever runs) without aco governing that forge at all. Refuses under
    `storage = "github"`: the forge is pulled, never governed, so aco
    never opens a GitHub issue on a repository's behalf. Never resolves
    the generic `_LazyForge` (issue #248) -- it calls `_state_ref_forge`
    directly, since `create_item` is not part of the generic `ForgeWriter`
    port every other write command narrows to."""
    toplevel = _resolve_toplevel()
    config = _board_config(toplevel)
    if config.storage is not board.Storage.STATE_REF:
        raise protocol.ClaimUnavailableError(ITEM_NEW_GITHUB_REFUSAL)
    client = _state_ref_forge(parsed.repo, config.canonical_remote)
    parent_missing = (
        parsed.parent is not None
        and client.item_reference(parsed.parent).state is forge.ItemState.MISSING
    )
    if parent_missing:
        raise protocol.ClaimUnavailableError(f"#{parsed.parent} does not exist")
    kind = board.ItemKind(parsed.kind)
    skeleton = (
        board.BLOCK_CONTAINER_SKELETON
        if kind is board.ItemKind.CONTAINER
        else board.BLOCK_CHILD_SKELETON
    )
    item_id = client.create_item(
        title=parsed.title, body=skeleton, kind=kind, parent=parsed.parent, origin=parsed.origin
    )
    _print_item_new_result(item_id, items.item_number(item_id), as_json=parsed.json)
    return 0


def _print_item_new_result(item_id: str, number: int, *, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"item": item_id, "number": number}))
    else:
        print(item_id)


ITEM_EDIT_GITHUB_REFUSAL = "forge issues are edited on the forge; aco never governs them"


def _cmd_item_edit(parsed: argparse.Namespace) -> int:
    """`aco item edit ITEM` (issue #287): the state-ref item's own body,
    replaced from stdin only -- refused before any write when the piped
    body carries no valid `agent-claim` block (`body --check`'s own
    sentences, `_body_shape_defects`). The CAS `expected` oid is this
    process's own already-read snapshot (`StateRefBoard.update_item_body`'s
    `current.oid`, set once at `_state_ref_forge` construction): a second
    process writing from that same snapshot refuses with issue #279's own
    sentence, never merged, never silently overwritten. `parent`, `state`,
    `origin`, `created_at`, and `closed_at` stay this item's own stored
    values regardless of what the piped body's `[record]` names for them --
    `update_item_body`'s own owner rule; `updated_at` always moves to now;
    `title`, `labels`, `blocked_by` come from the piped record when it
    carries one. Refuses under `storage = "github"`: forge issues are edited
    on the forge, never governed by aco -- mirrors `item new`'s own refusal,
    and calls `_state_ref_forge` directly for the same reason (`create_item`/
    `update_item_body` are not part of the generic `ForgeWriter` port every
    other write command narrows to)."""
    toplevel = _resolve_toplevel()
    config = _board_config(toplevel)
    if config.storage is not board.Storage.STATE_REF:
        raise protocol.ClaimUnavailableError(ITEM_EDIT_GITHUB_REFUSAL)
    body = _read_body_check_input()
    defects = _body_shape_defects(body, storage=board.Storage.STATE_REF)
    if defects:
        raise protocol.ClaimUnavailableError(defects[0])
    client = _state_ref_forge(parsed.repo, config.canonical_remote)
    number = parsed.item
    if client.item_reference(number).state is forge.ItemState.MISSING:
        raise protocol.ClaimUnavailableError(
            f"#{number} does not exist in {client.repository.path}"
        )
    client.update_item_body(number, body)
    _print_item_edit_result(
        items.format_item_id(number), number, client.item_oid(number), as_json=parsed.json
    )
    return 0


def _print_item_edit_result(
    item_id: str, number: int, oid: protocol.ObjectId, *, as_json: bool
) -> None:
    if as_json:
        print(json.dumps({"item": item_id, "number": number, "oid": oid}))
    else:
        print(f"EDITED {item_id}")


ITEM_CLOSE_GITHUB_REFUSAL = "the forge closes its issues; aco never governs them"


def _cmd_item_close(parsed: argparse.Namespace) -> int:
    """`aco item close ITEM` (issue #289): the state-ref item's own record,
    closed -- `state` to `CLOSED`, `closed_at`/`updated_at` to now, the item
    file and every other byte untouched (`StateRefBoard.close_item`'s one
    CAS write over this process's own already-read oid, extending #287's
    record-owner rule by one field rather than composing a record here).
    Refuses under `storage = "github"` by name -- the forge closes its own
    issues, aco never governs them -- and refuses a live claim on the item
    before ever writing: a closed item with a live claim still on it is the
    `RECOVERY` anomaly the board already guards against, never a state this
    command creates. Existence is checked through the ordinary
    `item_reference` read before `close_item` is ever called, so an unknown
    id gets this command's own "does not exist" sentence rather than
    `close_item`'s internal `_by_number` lookup failing with the wrong
    shape; `close_item` itself refuses a second close on an already-closed
    item, naming its date. Prints one line, `CLOSED aco-xxxxxx` (`--json`:
    `{"item", "number", "closed_at"}`), then `release --merged`'s own
    `freed:` line -- open items whose only open local blocker was this one
    (`_freed_item_numbers`, issue #256; nothing new)."""
    toplevel = _resolve_toplevel()
    config = _board_config(toplevel)
    if config.storage is not board.Storage.STATE_REF:
        raise protocol.ClaimUnavailableError(ITEM_CLOSE_GITHUB_REFUSAL)
    number = parsed.item
    _worktree, _remote, observed = _store_observation()
    _require_state_ref(observed)
    live_claim = observed.claims.get(protocol.claim_key(protocol.IssueIdentity(number), ""))
    if live_claim is not None:
        raise protocol.ClaimUnavailableError(
            f"#{number} has a live claim "
            f"({protocol._claimant_text(live_claim.agent, live_claim.role)}); "
            "release the claim first"
        )
    client = _state_ref_forge(parsed.repo, config.canonical_remote)
    if client.item_reference(number).state is forge.ItemState.MISSING:
        raise protocol.ClaimUnavailableError(
            f"#{number} does not exist in {client.repository.path}"
        )
    closed_at = client.close_item(number)
    freed = _item_close_freed(client, number)
    _print_item_close_result(
        items.format_item_id(number), number, closed_at, freed, as_json=parsed.json
    )
    return 0


def _item_close_freed(client: forge.ForgeReader, number: int) -> tuple[int, ...]:
    """Every open item `number`'s own close just freed -- issue #256's own
    derivation, reused rather than reinvented, the same base-issues-plus-
    dependency-fetch wave `_release_landing` runs for a merged release, read
    fresh off `client`'s own already-closed view so `number` itself never
    appears among its own candidates."""
    issues = client.list_open_board_issues()
    candidates = tuple(issue.number for issue in issues if issue.blocked_by_count > 0)
    dependencies = _validated_dependencies(issues, _fetch_dependencies(client, candidates))
    landed = board.IssueReference(client.repository.path, number)
    return _freed_item_numbers(dependencies, landed)


def _print_item_close_result(
    item_id: str, number: int, closed_at: str, freed: tuple[int, ...], *, as_json: bool
) -> None:
    if as_json:
        print(json.dumps({"item": item_id, "number": number, "closed_at": closed_at}))
        return
    print(f"CLOSED {item_id}")
    # `item close` only ever runs under `storage = "state-ref"`
    # (`_cmd_item_close`'s own refusal otherwise), so `freed:`'s own id
    # chooser is fixed here rather than threaded as a sixth argument.
    print(_release_freed_line(freed, board.Storage.STATE_REF))


def _item_state_text(state: forge.ItemState) -> str:
    return "open" if state is forge.ItemState.OPEN else "closed"


def _item_parent_id(parent: board.ParentIssue | None) -> str | None:
    return None if parent is None else items.format_item_id(parent.reference.number)


def _item_header(
    number: int, reference: forge.ItemReference, parent: board.ParentIssue | None
) -> str:
    """`item show`'s one header line: id, number, state, parent, and origin
    -- the same shape regardless of which forge answered the reads, since an
    id (`items.format_item_id`) is a pure encoding of `number`, never a
    per-adapter fact. `reference.origin` (issue #316) stays `None` for a
    GitHub-stored item, so this prints `origin none` there exactly like an
    unset parent prints `parent none`."""
    return (
        f"{items.format_item_id(number)} · #{number} · "
        f"{_item_state_text(reference.state)} · parent {_item_parent_id(parent) or 'none'} · "
        f"origin {reference.origin or 'none'}"
    )


def _cmd_item_show(parsed: argparse.Namespace, session: _ReadSession) -> int:
    """`aco item show` (issue #285): the stored body, byte-exact, behind
    one header line -- read through the ordinary forge port, so it works
    identically under `storage = "github"` (the forge's own issue body) and
    `storage = "state-ref"` (the item file's own body); closing an item
    never deletes it, so a closed item is shown exactly like an open one."""
    client = session.forge()
    number = parsed.item
    reference = client.item_reference(number)
    if reference.state is forge.ItemState.MISSING:
        raise protocol.ClaimUnavailableError(
            f"#{number} does not exist in {client.repository.path}"
        )
    parent = client.parent_issue(number)
    body = reference.body or ""
    if parsed.json:
        print(
            json.dumps(
                {
                    "item": items.format_item_id(number),
                    "number": number,
                    "state": _item_state_text(reference.state),
                    "parent": _item_parent_id(parent),
                    "origin": reference.origin,
                    "body": body,
                }
            )
        )
        return 0
    print(_item_header(number, reference, parent))
    print(body, end="")
    return 0


def _claim_ages(worktree: Path, state: protocol.ClaimState) -> dict[str, datetime]:
    """Each live claim's age in an already-fetched state -- one batched read
    of `state.tip`'s history (`store.claim_ages`, issue #242), never a git
    call per claim.
    """
    if state.tip is None:
        return {}
    return store.claim_ages(worktree=worktree, tip=state.tip, claims=state.claims.values())


def _store_observation() -> tuple[Path, str, protocol.ClaimState]:
    """One fetch of `refs/aco/state` for a store command -- forge-free by
    itself (issue #245). A command that also needs a forge resolves and
    Erwartung-6-checks that target separately, the first time its session's
    `forge` is actually asked for."""
    canonical_remote = _canonical_remote_name(_resolve_toplevel())
    worktree = Path.cwd()
    return worktree, canonical_remote, store.fetch_state(worktree=worktree, remote=canonical_remote)


def _require_state_ref(state: protocol.ClaimState) -> None:
    if state.tip is None:
        raise protocol.ClaimError(protocol.MISSING_STATE_REF)


def _transition_subject(action: str, identity: protocol.ClaimIdentity, branch: str) -> str:
    """The commit message's first line (§1 "Commit message"): `claim issue 42`,
    `rescope lane docs/lane-cleanup`, and so on."""
    if isinstance(identity, protocol.LaneIdentity):
        return f"{action} lane {branch}"
    return f"{action} issue {identity.issue}"


def _claim_intent_from_request(
    request: protocol.ClaimRequest, operation_id: str
) -> protocol.ClaimIntent:
    """`_request`'s validated `ClaimRequest`, converted to the store's own
    intent (issue #176, §1: `ClaimRequest` stays the CLI-facing input,
    `ClaimIntent` is what `apply` actually consumes)."""
    resource_name = None
    resource_value = None
    if request.resource is not None:
        resource_name = request.resource
        resource_value = request.resource_value
    return protocol.ClaimIntent(
        identity=request.identity,
        agent=request.agent,
        role=request.role,
        base=protocol.ObjectId(request.base),
        branch=request.branch,
        scope=request.scope,
        claim_id=protocol.ClaimId(request.claim_id),
        operation_id=operation_id,
        whole_reason=request.whole_reason,
        resource_name=resource_name,
        resource_value=resource_value,
    )


def _matching_store_claim(
    observed: protocol.ClaimState, request: protocol.ClaimRequest
) -> protocol.ActiveClaim | None:
    """The live store claim an interrupted, replayed `claim` invocation may
    reuse (criterion 2): same identity, agent, role, branch, scope, *and*
    claim id -- `apply` itself only replays on an exact claim-id match
    (§1 "Retry of an interrupted identical request"), so a CLI-level replay
    check that skips this slice's dispatch rules must use the same test, not
    the ledger's looser field-only match. Issueless lanes keep today's
    one-claim-per-branch contract; only a numbered item gets replay
    detection at all.
    """
    if not isinstance(request.identity, protocol.IssueIdentity):
        return None
    existing = observed.claims.get(protocol.claim_key(request.identity, request.branch))
    if (
        existing is not None
        and existing.claim_id == request.claim_id
        and existing.agent == request.agent
        and existing.role == request.role
        and existing.branch == request.branch
        and existing.scope == request.scope
    ):
        return existing
    return None


def _selected_store_claim(
    observed: protocol.ClaimState,
    identity: protocol.ClaimIdentity,
    branch: str | None,
    claim_id: str | None,
) -> protocol.ActiveClaim:
    """The one live store claim `rescope`/`release` names: at most one claim
    is ever live per identity (the store's own invariant), so this is a
    direct key lookup, never the ledger's filter-then-disambiguate walk.
    `claim_id`, when given, is a safety check against that one claim, not a
    selector among several -- there are never several.
    """
    if isinstance(identity, protocol.LaneIdentity) and not branch:
        raise protocol.ClaimUnavailableError(
            "lane release requires a non-empty current branch; check out the "
            "docs/ or fix/ lane branch, or pass an issue number"
        )
    selected = observed.claims.get(protocol.claim_key(identity, branch or ""))
    if selected is None or (claim_id is not None and selected.claim_id != claim_id):
        raise protocol.ClaimUnavailableError(
            f"{protocol._identity_summary(identity, branch or '')} has no active build claim"
        )
    return selected


class HookToolEffect(StrEnum):
    """What a `PreToolUse` hook name does to files, for `protect`'s verdict.

    `READ` never touches a file's contents, so it clears without a claim
    check. `MUTATING` can write, so it is gated on a live overlapping claim
    exactly as today. Any name in neither set is unproven -- `protect` must
    fail closed on it rather than default it to either bucket (issue #238).
    """

    READ = "read"
    MUTATING = "mutating"


HOOK_TOOL_EFFECTS: Mapping[str, HookToolEffect] = {
    # Read-only: cannot mutate a file, so no claim check is needed.
    # `Bash`/`shell` are here too -- the hook payload carries no file path
    # for a shell command, so `protect` cannot gate what it cannot see; this
    # is a named limit (README, "PreToolUse write gate"), not an oversight.
    "Read": HookToolEffect.READ,
    "Glob": HookToolEffect.READ,
    "Grep": HookToolEffect.READ,
    "LS": HookToolEffect.READ,
    "WebFetch": HookToolEffect.READ,
    "WebSearch": HookToolEffect.READ,
    "TodoWrite": HookToolEffect.READ,
    "Task": HookToolEffect.READ,
    "Agent": HookToolEffect.READ,
    "Bash": HookToolEffect.READ,
    "shell": HookToolEffect.READ,
    # Other providers' names for the same read-only or path-blind operations
    # (Grok, Codex): a snake_case terminal command is the same blind spot as
    # `Bash`/`shell` above, and the rest never write a file.
    "read_file": HookToolEffect.READ,
    "grep": HookToolEffect.READ,
    "list_dir": HookToolEffect.READ,
    "run_terminal_command": HookToolEffect.READ,
    "spawn_subagent": HookToolEffect.READ,
    # Mutating: gated on a live claim whose scope overlaps the written path.
    "Edit": HookToolEffect.MUTATING,
    "MultiEdit": HookToolEffect.MUTATING,
    "Write": HookToolEffect.MUTATING,
    "search_replace": HookToolEffect.MUTATING,
    "write": HookToolEffect.MUTATING,
    "NotebookEdit": HookToolEffect.MUTATING,
    "apply_patch": HookToolEffect.MUTATING,
    "create_file": HookToolEffect.MUTATING,
    "str_replace_editor": HookToolEffect.MUTATING,
}


def _unknown_hook_tool_reason(tool_name: str) -> str:
    return (
        f"{tool_name!r} is not in aco's hook tool table (HOOK_TOOL_EFFECTS in "
        "cli.py, issue #238); add it there as read-only or mutating before use"
    )


def _hook_allow() -> int:
    print(json.dumps({"decision": "allow"}))
    return 0


def _hook_deny(reason: str) -> int:
    print(json.dumps({"decision": "deny", "reason": reason}))
    return 2


def _hook_payload() -> dict[str, object] | None:
    try:
        payload = json.loads(sys.stdin.read())
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _hook_field(payload: dict[str, object], *keys: str) -> object:
    for key in keys:
        if key in payload:
            return payload[key]
    return None


_GENERIC_PATH_KEYS = ("path", "file_path", "filePath")


def _hook_path(tool_input: dict[str, object], *, keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


RELATIVE_PAYLOAD_PATH_DENIAL = "relative payload path"


def _relative_scope_entry(absolute_path: str, *, toplevel: Path) -> str | None:
    """`absolute_path` (already an absolute filesystem path -- a hook
    payload path, or a `rescope --add`/`--drop` entry given that way) as a
    canonical, repository-relative scope entry under `toplevel`, or `None`
    when it resolves outside `toplevel` or is otherwise not a valid scope
    entry. Shared by `protect` (issue #314) and `rescope`'s own absolute-path
    handling (issue #314 delta, finding R1)."""
    try:
        relative = Path(absolute_path).resolve().relative_to(toplevel).as_posix()
        return protocol._valid_scope([relative])[0]
    except (protocol.InvalidClaimMarkerError, OSError, ValueError):
        return None


PATH_REQUIRED = "path required"


APPLY_PATCH_TOOL_NAME = "apply_patch"
NOTEBOOK_EDIT_TOOL_NAME = "NotebookEdit"


class _HookPathSource(StrEnum):
    """Where a mutating tool's path lives in its `tool_input` -- one owner
    per tool name (issue #252) so a tool can only be read from a key it
    actually sends; a decoy value under a key it does not send (an in-scope
    `path` next to `NotebookEdit`'s real, out-of-scope `notebook_path`) is
    never looked at."""

    GENERIC_KEYS = "generic_keys"
    NOTEBOOK_PATH = "notebook_path"
    PATCH_TEXT = "patch_text"


_HOOK_PATH_SOURCES: dict[str, _HookPathSource] = {
    APPLY_PATCH_TOOL_NAME: _HookPathSource.PATCH_TEXT,
    NOTEBOOK_EDIT_TOOL_NAME: _HookPathSource.NOTEBOOK_PATH,
}


def _protect_hook_paths(tool_name: str, payload: dict[str, object]) -> tuple[str, ...]:
    """Every path this hook call's `tool_input` names, read only from the
    key(s) this specific tool sends.

    `apply_patch` (Codex) carries no path key at all -- its patch text sits
    under `command` and can touch several files in one call, so it is parsed
    by the dedicated patch grammar instead. `NotebookEdit` (Claude Code)
    carries only `notebook_path`. Every other tool still yields at most one
    path, from the shared `path`/`file_path`/`filePath` keys."""
    tool_input = _hook_field(payload, "toolInput", "tool_input")
    if not isinstance(tool_input, dict):
        return ()
    source = _HOOK_PATH_SOURCES.get(tool_name, _HookPathSource.GENERIC_KEYS)
    if source is _HookPathSource.PATCH_TEXT:
        command = tool_input.get("command")
        if not isinstance(command, str):
            return ()
        return hook_input.hook_patch_paths(command)
    keys = ("notebook_path",) if source is _HookPathSource.NOTEBOOK_PATH else _GENERIC_PATH_KEYS
    single = _hook_path(tool_input, keys=keys)
    return (single,) if single is not None else ()


_ProtectStateOutcome = tuple[protocol.ClaimState | None, str | None]
_ProtectStateCache = dict[Path, _ProtectStateOutcome]


def _protect_claim_state_or_denial(worktree: Path, canonical_remote: str) -> _ProtectStateOutcome:
    """`protect`'s live snapshot (issue #176, §1): one fetch, no positive
    cache (D2). A non-`None` second element names a denial reason for a
    store the hook cannot trust -- unreachable, auth, malformed tree,
    lineage break -- with the same named text (Erwartung 8) instead of the
    generic 'claim first', which would send the agent toward a command that
    cannot fix a transient fetch failure. `worktree` is the payload path's
    own resolved checkout (issue #314), never the hook process's cwd, so a
    subagent editing a linked worktree is fetched against that worktree's
    own per-worktree fetch/lineage state.
    """
    try:
        state = store.fetch_state(worktree=worktree, remote=canonical_remote)
    except protocol.ClaimError as error:
        return None, f"cannot reach {store.STATE_REF}: {error}"
    if state.tip is None:
        return None, f"cannot reach {store.STATE_REF}: {protocol.MISSING_STATE_REF}"
    return state, None


def _protect_cached_claim_state_or_denial(
    path_checkout: checkout.PathCheckout, *, state_cache: _ProtectStateCache
) -> _ProtectStateOutcome:
    """`_protect_claim_state_or_denial`, fetched at most once per repository
    per hook invocation (issue #314 gate G5): several payload paths in one
    `apply_patch` call can name the same repository through different
    worktrees, and re-fetching for each would let each path be judged
    against a different snapshot of a store that can move between them --
    passing a rescope that narrowed coverage between fetches, for instance,
    though no single live claim ever covered the whole patch. Cached by
    `common_directory`, the one fact every worktree of one repository
    shares, not by `toplevel`, which differs per worktree."""
    cached = state_cache.get(path_checkout.common_directory)
    if cached is not None:
        return cached
    canonical_remote = _canonical_remote_name(path_checkout.toplevel)
    outcome = _protect_claim_state_or_denial(path_checkout.toplevel, canonical_remote)
    state_cache[path_checkout.common_directory] = outcome
    return outcome


def _protect_overlapping_claim_exists(
    state: protocol.ClaimState, *, agent: str, branch: str, relative: str
) -> bool:
    return any(
        claim.agent == agent
        and claim.branch == branch
        and protocol._scopes_overlap(claim.scope, (relative,))
        for claim in state.claims.values()
    )


def _protect_session_claim_exists(state: protocol.ClaimState, *, agent: str, branch: str) -> bool:
    return any(claim.agent == agent and claim.branch == branch for claim in state.claims.values())


def _protect_scope_denial(
    state: protocol.ClaimState,
    *,
    agent: str,
    branch: str,
    relative: str,
    distinguish_scope: bool,
) -> str | None:
    """Whether a live claim covers `relative`, or the deny reason when not --
    the `apply_patch` disambiguation (issue #252) `_protect_path_denial`
    delegates to once it has a trustworthy state and a resolved checkout."""
    if _protect_overlapping_claim_exists(state, agent=agent, branch=branch, relative=relative):
        return None
    if distinguish_scope and _protect_session_claim_exists(state, agent=agent, branch=branch):
        return f"{relative} outside claim scope"
    return "claim first"


def _protect_basic_checkout_denial(
    raw_path: str,
) -> tuple[checkout.PathCheckout | None, str | None]:
    """The payload path's own resolved checkout, or an early denial reason
    when the path itself, or the checkout it names, cannot even be weighed
    against a live claim -- resolved from the path (issue #314), never from
    the hook process's cwd, so the same payload path yields the same
    verdict from any cwd: a relative payload path denies outright (finding
    R2 -- every provider `aco` supports sends an already-absolute
    `file_path`, so a relative one is untrustworthy and never guessed at by
    joining it to the hook process's own cwd, exactly the signal issue #314
    removes); a path outside every repository denies "not in a repository";
    a checkout with no commit yet denies (gate G3 -- its branch name could
    otherwise coincidentally match a still-live claim's); a path in the
    shared main checkout, or in a checkout on the repository's default
    branch at all (gate G4 -- a linked worktree can sit on that branch after
    the repository's default branch changes), denies "not main"."""
    if not Path(raw_path).is_absolute():
        return None, RELATIVE_PAYLOAD_PATH_DENIAL
    path_checkout = checkout.resolve_path_checkout(Path(raw_path).parent)
    if path_checkout is None:
        return None, "not in a repository"
    if not path_checkout.has_commit:
        return None, checkout.NO_COMMIT_CHECKOUT_REASON
    if path_checkout.kind is checkout.CheckoutKind.MAIN or checkout.is_default_branch(
        path_checkout.branch
    ):
        return None, "not main"
    return path_checkout, None


def _protect_path_denial(
    agent: str, raw_path: str, *, distinguish_scope: bool, state_cache: _ProtectStateCache
) -> str | None:
    """The deny reason for one payload path's write, or `None` to allow. A
    path that clears `_protect_basic_checkout_denial`'s gates is judged
    against its own linked worktree's live claim, fetched at most once per
    repository for this hook call (gate G5).

    `apply_patch` sets `distinguish_scope` (issue #252): with several paths
    in one call, the payload never told the agent which one was the problem,
    so the repair sentence must -- `claim first` when this session holds no
    live claim on the path's own checkout at all, `{path} outside claim
    scope` when it does but this path is not in it. A single-path tool call
    keeps the simpler `claim first` either way, matching its own payload's
    inability to name any other path.
    """
    path_checkout, denial = _protect_basic_checkout_denial(raw_path)
    if path_checkout is None:
        return denial
    relative = _relative_scope_entry(raw_path, toplevel=path_checkout.toplevel)
    if relative is None:
        return PATH_REQUIRED
    state, denial = _protect_cached_claim_state_or_denial(path_checkout, state_cache=state_cache)
    if state is None:
        return denial
    return _protect_scope_denial(
        state,
        agent=agent,
        branch=path_checkout.branch,
        relative=relative,
        distinguish_scope=distinguish_scope,
    )


def _protect_write(tool_name: str, payload: dict[str, object]) -> int:
    """`protect` is forge-free (issue #245): it authorizes a write from the
    live store state alone, never a forge target, so it never resolves a
    repository or calls `gh` -- `--repo` is meaningless here and simply
    unused. Several paths in one `apply_patch` call may each sit in a
    different checkout (issue #314): each is judged in its own, and the
    first denial wins. `state_cache` is this one hook call's own state
    snapshot, shared by every path in the same repository (gate G5) --
    never carried between calls, so every invocation still reads live."""
    raw_paths = _protect_hook_paths(tool_name, payload)
    if not raw_paths:
        return _hook_deny(PATH_REQUIRED)
    agent = checkout._resolved_agent(None)
    distinguish_scope = tool_name == APPLY_PATCH_TOOL_NAME
    state_cache: _ProtectStateCache = {}
    for raw_path in raw_paths:
        denial = _protect_path_denial(
            agent, raw_path, distinguish_scope=distinguish_scope, state_cache=state_cache
        )
        if denial is not None:
            return _hook_deny(denial)
    return _hook_allow()


def _protect() -> int:
    # Grok fail-opens on crash or non-JSON hook output; deny instead of raising.
    try:
        payload = _hook_payload()
        if payload is None:
            return _hook_deny("invalid hook payload")
        tool_name = _hook_field(payload, "toolName", "tool_name")
        if not isinstance(tool_name, str):
            return _hook_deny("invalid hook payload")
        effect = HOOK_TOOL_EFFECTS.get(tool_name)
        if effect is None:
            return _hook_deny(_unknown_hook_tool_reason(tool_name))
        if effect is HookToolEffect.READ:
            return _hook_allow()
        return _protect_write(tool_name, payload)
    except Exception as error:
        return _hook_deny(str(error))


def _optional_issue_number(value: int | None) -> int | None:
    return None if value is None else int(value)


class _LazyForge:
    """This command's forge: resolved and Erwartung-6-checked the first time
    anything calls it, cached after that, and never touched at all by a
    command that never calls it (issue #245) -- `status`, `protect`,
    `bootstrap`, and a lane `claim`/`rescope`/`release` never resolve a
    repository or invoke `gh` because none of them ever does.

    Chooses its adapter by the repository's own `storage` pin (issue #248),
    never by the canonical remote's host: `github` (the default) builds
    `github.GitHubForge`; `state-ref` builds `state_board.StateRefBoard`
    from the state ref's own `items/` tree instead.
    """

    def __init__(self, repo: str | None) -> None:
        self._repo = repo
        self._resolved: forge.ForgeReader | None = None

    def __call__(self) -> forge.ForgeReader:
        if self._resolved is None:
            toplevel = _resolve_toplevel()
            config = _board_config(toplevel)
            canonical_remote = config.canonical_remote
            if config.storage is board.Storage.STATE_REF:
                self._resolved = _state_ref_forge(self._repo, canonical_remote)
            else:
                target = _resolved_forge_target(self._repo, canonical_remote)
                self._resolved = github.GitHubForge(target)
        return self._resolved

    def writer(self) -> forge.ForgeWriter:
        """The same resolved forge, narrowed to its writing surface (issue
        #248, #283). The cast is honest, not a suppression: every adapter
        this tool builds -- `github.GitHubForge`, `state_board.StateRefBoard`
        (its `ItemWriter` injected by `_state_ref_forge`), and every test
        fake standing in for either -- already implements the full
        `ForgeWriter` surface, checked at each call site by `capability()`,
        never by `isinstance`.
        """
        return cast(forge.ForgeWriter, self())


@dataclass(frozen=True)
class _ReadSession:
    """What a dispatched read-only subcommand needs beyond its parsed arguments."""

    forge: _LazyForge


@dataclass(frozen=True)
class _WriteSession:
    """What a dispatched write subcommand needs beyond its parsed arguments."""

    forge: _LazyForge
    release_branch: str | None


def _rescope_location(add: list[str] | None, drop: list[str] | None) -> Path:
    """The directory `rescope`'s checkout is resolved from (issue #314
    delta, finding R1): the first `--add`/`--drop` entry that is already an
    absolute path -- the one location signal a dispatcher running in a
    foreign cwd (the head's own shared environment, editing a linked
    worktree through a subagent) can give without knowing that cwd. A
    relative entry carries no location of its own and is never joined to the
    process's cwd to guess one -- finding R2's same principle, applied here
    -- so `rescope` falls back to its own process cwd, its other legitimate
    location signal, only when every given entry is relative or none is
    given: unchanged from before this fix for that ordinary, undispatched
    case."""
    for raw_path in (*(add or ()), *(drop or ())):
        if Path(raw_path).is_absolute():
            return Path(raw_path).parent
    return Path.cwd()


def _rescope_scope_entries(
    raw_paths: list[str] | None, *, toplevel: Path, flag: str
) -> tuple[str, ...]:
    """One `--add`/`--drop` list, canonicalized to repository-relative scope
    entries: an absolute entry (`_rescope_location`'s own signal) is
    resolved against `toplevel`; a relative entry is already the documented
    repository-relative form and is validated as-is, never filesystem
    joined."""
    if not raw_paths:
        return ()
    canonical: list[str] = []
    for raw_path in raw_paths:
        if not Path(raw_path).is_absolute():
            canonical.append(raw_path)
            continue
        relative = _relative_scope_entry(raw_path, toplevel=toplevel)
        if relative is None:
            raise protocol.ClaimUnavailableError(
                f"{flag} path {raw_path!r} is outside the resolved checkout {toplevel}"
            )
        canonical.append(relative)
    return protocol._valid_scope(canonical)


def _rescope_checkout(parsed: argparse.Namespace) -> checkout.PathCheckout:
    """`rescope`'s checkout, resolved from a path it is given whenever one
    names a location (issue #314 delta, finding R1), read through the same
    path-based resolver `protect` uses rather than the ad hoc
    `git branch --show-current` this replaces, so it fails the same way
    regardless of where else in the tree a bare cwd fallback might have
    looked. A checkout with no commit yet denies here too (gate G3), the
    same precondition `protect` enforces on its own resolved checkout."""
    path_checkout = checkout.resolve_path_checkout(_rescope_location(parsed.add, parsed.drop))
    if path_checkout is None:
        raise protocol.ClaimUnavailableError("not in a repository")
    if not path_checkout.has_commit:
        raise protocol.ClaimUnavailableError(checkout.NO_COMMIT_CHECKOUT_REASON)
    return path_checkout


def _rescope_command(
    parsed: argparse.Namespace, path_checkout: checkout.PathCheckout
) -> protocol.RescopeRequest:
    branch = path_checkout.branch
    if not branch:
        raise protocol.ClaimUnavailableError(
            "rescope requires a non-empty current branch; "
            "check out the claim branch, or pass an issue number"
        )
    checkout._refuse_shared_checkout(path_checkout, repair=checkout.WorktreeRepair.RETURN_TO_CLAIM)
    identity = _resolved_identity(_optional_issue_number(parsed.issue), branch)
    return protocol.RescopeRequest(
        identity=identity,
        agent=parsed.agent,
        add=_rescope_scope_entries(parsed.add, toplevel=path_checkout.toplevel, flag="--add"),
        drop=_rescope_scope_entries(parsed.drop, toplevel=path_checkout.toplevel, flag="--drop"),
        claim_id=parsed.claim_id,
        branch=branch,
        whole_reason=_optional_whole_reason(parsed),
    )


def _cmd_check(parsed: argparse.Namespace, session: _ReadSession) -> int:
    """One number, one dispatch request into one of three answers: a pull
    request to classify, an issue whose body contract to read, or a number
    that is in neither number space. Only the pull-request side needs the
    live claims, so the issue side never fetches the state ref."""
    number = int(parsed.number)
    client = session.forge()
    repository = client.repository.path
    # Read for its refusals only: a repository pinned to a grammar this tool
    # no longer reads, or a forge that cannot answer `blocked_by`, must fail
    # here rather than hand back a half-read answer.
    config = _load_board_config(client, _resolve_toplevel())
    reference = client.item_reference(number)
    if reference.state is forge.ItemState.MISSING:
        outcome = _missing_number(repository, number)
    elif reference.is_landing:
        _worktree, _remote, observed = _store_observation()
        outcome = _pull_request_check(client, tuple(observed.claims.values()), repository, number)
    else:
        outcome = _issue_check(
            client, repository, reference.body or "", number, storage=config.storage
        )
    return outcome.report(as_json=parsed.json)


def _brief_claim(
    claims: tuple[protocol.ActiveClaim, ...], item: int
) -> protocol.ActiveClaim | None:
    """This item's own live claim -- one exclusive build claim per issue
    before the first edit (README), so at most one is ever live; the first
    match is it. `None` when the item carries no live claim at all."""
    for claim in claims:
        if isinstance(claim.identity, protocol.IssueIdentity) and claim.identity.issue == item:
            return claim
    return None


@dataclass(frozen=True)
class _BriefClaim:
    """`brief`'s own live-claim reading: the claim paired with the one age it
    needs -- never every live claim's age like `_claim_ages`, so a lineage
    break in an unrelated claim can never stop this item's brief."""

    claim: protocol.ActiveClaim
    opened_at: datetime


def _brief_live_claim(worktree: Path, state: protocol.ClaimState, item: int) -> _BriefClaim | None:
    claim = _brief_claim(tuple(state.claims.values()), item)
    if claim is None:
        return None
    # A live claim cannot exist without the ref it was read from.
    tip = cast(protocol.ObjectId, state.tip)
    opened_at = store.claim_ages(worktree=worktree, tip=tip, claims=(claim,))[claim.claim_id]
    return _BriefClaim(claim, opened_at)


def _lane_tip(branch: str) -> str | None:
    """`branch`'s current commit -- local first, then `origin/` -- or `None`
    when neither ref resolves (a deleted or not-yet-pushed lane branch)."""
    for ref in (branch, f"origin/{branch}"):
        try:
            return checkout._git_output(["rev-parse", "--verify", ref])
        except protocol.ClaimError:
            continue
    return None


def _touched_files(base: str, tip: str) -> tuple[str, ...]:
    diff = checkout._git_output(["diff", "--name-only", f"{base}..{tip}"])
    return tuple(diff.splitlines()) if diff else ()


def _print_brief_claim(
    claim: protocol.ActiveClaim, opened_at: datetime, observed_at: datetime
) -> None:
    print(
        f"{claim.agent} ({claim.role}) branch={claim.branch} base={claim.base}"
        f"{_claim_age_suffix(opened_at, observed_at)}"
    )
    for path in claim.scope:
        print(f"  {path}")
    if claim.whole_reason is not None:
        print(f"  whole: {claim.whole_reason}")


def _print_brief(
    body: str,
    live: _BriefClaim | None,
    observed_at: datetime,
    tip: str | None,
    touched: tuple[str, ...],
) -> None:
    print(body)
    print()
    print("CLAIM")
    if live is None:
        print("no active claim")
    else:
        _print_brief_claim(live.claim, live.opened_at, observed_at)
    print()
    print("TIP")
    if live is not None:
        print(tip if tip is not None else "branch not found")
    print()
    print("TOUCHED")
    for path in touched:
        print(path)


def _brief_claim_json(live: _BriefClaim, observed_at: datetime) -> dict[str, object]:
    claim = live.claim
    return {
        "agent": claim.agent,
        "role": claim.role,
        "branch": claim.branch,
        "base": claim.base,
        "scope": list(claim.scope),
        "whole": claim.whole_reason,
        "age": _claim_age_fields(live.opened_at, observed_at)[0],
    }


def _brief_json(
    body: str,
    live: _BriefClaim | None,
    observed_at: datetime,
    tip: str | None,
    touched: tuple[str, ...],
) -> int:
    print(
        json.dumps(
            {
                "body": body,
                "claim": None if live is None else _brief_claim_json(live, observed_at),
                "tip": tip,
                "touched": list(touched),
            }
        )
    )
    return 0


def _cmd_brief(parsed: argparse.Namespace, session: _ReadSession) -> int:
    """Compose one item's own reads into the one dispatch brief a lane step's
    body otherwise gets assembled from by hand (AGENTS.md "the next brief
    names the body, the lane tip ... and the commands"): the item's body from
    the forge, its live claim from the store, the claim branch's current tip,
    and the files the lane touches against its base. Never a new data
    source, and never a write."""
    item = int(parsed.item)
    client = session.forge()
    body = client.item_reference(item).body or ""
    worktree, _remote, state = _store_observation()
    live = _brief_live_claim(worktree, state, item)
    if live is None:
        tip: str | None = None
        touched: tuple[str, ...] = ()
    else:
        tip = _lane_tip(live.claim.branch)
        touched = _touched_files(live.claim.base, tip) if tip is not None else ()
    observed_at = datetime.now(UTC)
    if parsed.json:
        return _brief_json(body, live, observed_at, tip, touched)
    _print_brief(body, live, observed_at, tip, touched)
    return 0


def _cmd_status(parsed: argparse.Namespace) -> int:
    """`status` reads live claims from the store directly (issue #176),
    dispatched straight from `main` -- it never needs `_dispatch`'s ledger
    resolution (a cut-over repository may have no ledger issue left at all).
    It is forge-free (issue #245): `--repo` is meaningless here and unused.
    """
    canonical_remote = _canonical_remote_name(_resolve_toplevel())
    worktree = Path.cwd()
    state = store.fetch_state(worktree=worktree, remote=canonical_remote)
    claims = tuple(state.claims.values())
    if parsed.path is not None:
        # `--path` prints no age, so it never reads a claim's ancestry: a
        # lineage break in one unrelated claim must not stop this answer
        # (README "status --path").
        if parsed.json:
            return _status_path_json(claims, parsed.path)
        storage = _board_config(_resolve_toplevel()).storage
        _status_path(claims, parsed.path, storage)
        return 0
    ages = _claim_ages(worktree, state)
    issue = _optional_issue_number(parsed.issue)
    now = datetime.now(UTC)
    if parsed.json:
        return _status_json(claims, issue, ages, state.tip, now=now)
    storage = _board_config(_resolve_toplevel()).storage
    return _status(claims, issue, ages, storage, now=now)


def _observed_board(
    session: _ReadSession,
    *,
    issues: tuple[board.Issue, ...] | None = None,
) -> board.Board:
    """`board`/`rulings`/`next` share this: the store's live claims, projected
    onto forge board data (issue #176 -- claims no longer come from the
    ledger; the forge is still the board's own data source)."""
    worktree, _remote, observed = _store_observation()
    return _board(
        session.forge(),
        tuple(observed.claims.values()),
        issues=issues,
        claim_ages=_claim_ages(worktree, observed),
    ).board


def _lane_claimants(observed: protocol.ClaimState) -> dict[int, board_html.LaneClaimant]:
    """Every live claim's agent, role, and branch, keyed by the issue it
    holds -- the one field (`branch`) `board.BoardItem.active_claim` never
    carries, since `board.py` joins it into a display string instead."""
    return {
        claim.identity.issue: board_html.LaneClaimant(claim.agent, claim.role, claim.branch)
        for claim in observed.claims.values()
        if isinstance(claim.identity, protocol.IssueIdentity)
    }


def _board_html_page(
    session: _ReadSession, *, served: board_html.ServedRuleForm | None = None
) -> str:
    """The one state -> page render both `board --html` (issue #276) and
    `board --serve` (issue #280) use -- every `gh` read `board` already
    performs (`_board`'s own merged-pull-request fetch serves the Landungen
    section instead of asking `gh` a second time) plus one extra local
    `checkout.trunk_landings` read for the trunk-landed rows' own commit
    identity (issue #304 review delta), rendered fresh every call so
    `--serve`'s `GET` never reads stale state."""
    client = session.forge()
    issues = client.list_open_board_issues()
    worktree, _remote, observed = _store_observation()
    fetch = _board(
        client,
        tuple(observed.claims.values()),
        issues=issues,
        claim_ages=_claim_ages(worktree, observed),
    )
    bodies = {issue.number: issue.body for issue in issues}
    config = _board_config(_resolve_toplevel())
    # `_board`'s own `checkout.trunk_landings` read (issue #304) stays
    # private to its `BoardBuildInputs` classification; the Landungen
    # section needs each landed item's own commit identity too (issue #304
    # review delta), which that classification discards, so this reads the
    # same local git history a second time rather than widening `_BoardFetch`
    # for the one caller that needs the raw records.
    trunk_landed_items = tuple(
        board_html.TrunkLandedItem(number, landing.sha, landing.committed_at)
        for landing in checkout.trunk_landings(config.canonical_remote, TRUNK_LANDING_DEPTH)
        if isinstance(landing.classification, board.TrunkWorkItemClassification)
        for number in landing.classification.numbers
    )
    sources = board_html.BoardSources(
        bodies=bodies,
        claimants=_lane_claimants(observed),
        recent_merged_pull_requests=fetch.recent_merged_pull_requests,
        state_tip="" if observed.tip is None else str(observed.tip),
        storage=config.storage,
        trunk_landed_items=trunk_landed_items,
    )
    page = board_html.build_page(fetch.board, sources)
    return board_html.render(page, served=served)


def _cmd_board_html(parsed: argparse.Namespace, session: _ReadSession) -> None:
    """`board --html` (issue #276): writes `_board_html_page`'s static
    rendering to `PATH`, or to stdout when `PATH` is omitted."""
    rendered = _board_html_page(session)
    if parsed.html:
        Path(parsed.html).write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")


def _cmd_board(parsed: argparse.Namespace, session: _ReadSession) -> None:
    if parsed.html is not None:
        _cmd_board_html(parsed, session)
        return
    projected = _observed_board(session)
    if parsed.json:
        print(board.board_json(projected))
        return
    storage = _board_config(_resolve_toplevel()).storage
    print(board.render(projected, storage=storage))


def _cmd_rulings(parsed: argparse.Namespace, session: _ReadSession) -> None:
    issues = session.forge().list_open_board_issues()
    projected = _observed_board(session, issues=issues)
    bodies = {issue.number: issue.body for issue in issues}
    storage = _board_config(_resolve_toplevel()).storage
    _rulings(projected, bodies, as_json=parsed.json, storage=storage)


def _next_action_container_number(action: board.NextAction | None) -> int | None:
    """The container `action` targets, when it targets one -- excluded from
    `SKIPPED` below since a container is always non-actionable itself."""
    if isinstance(action, board.CutSliceAction | board.CloseContainerAction):
        return action.container.number
    return None


def _cmd_next(parsed: argparse.Namespace, session: _ReadSession) -> int:
    projected = _observed_board(session)
    action = board.next_action(projected)
    chosen_container = _next_action_container_number(action)
    skipped = tuple(item for item in _unworkable(projected) if item.number != chosen_container)
    recovery = projected.recovery
    storage = _board_config(_resolve_toplevel()).storage
    if parsed.json:
        if action is None:
            _next_json(None, skipped, recovery, storage)
            return 3
        return _next_json(action, skipped, recovery, storage)
    if action is None:
        _next(None, skipped, recovery, storage)
        return 3
    return _next(action, skipped, recovery, storage)


def _cmd_rescope(parsed: argparse.Namespace, _session: _WriteSession) -> None:
    path_checkout = _rescope_checkout(parsed)
    requested = _rescope_command(parsed, path_checkout)
    worktree = path_checkout.toplevel
    canonical_remote = _canonical_remote_name(worktree)
    observed = store.fetch_state(worktree=worktree, remote=canonical_remote)
    _require_state_ref(observed)
    selected = _selected_store_claim(
        observed, requested.identity, requested.branch, requested.claim_id
    )
    if requested.agent != selected.agent:
        raise protocol.ClaimUnavailableError(
            "only the original claimant may rescope "
            f"(holder={protocol._claimant_text(selected.agent, selected.role)!r}, "
            f"this session={protocol._claimant_text(requested.agent, selected.role)!r})"
        )
    versioned = checkout.versioned_paths(directory=worktree)
    _reject_ungrounded_comma_scope(requested.add, versioned, flag="--add")
    combined = protocol._combined_scope(selected.scope, requested.add, requested.drop)
    _reject_wide_scope(combined, versioned, requested.whole_reason or selected.whole_reason)
    intent = protocol.RescopeIntent(
        claim_id=selected.claim_id,
        agent=requested.agent,
        role=selected.role,
        scope=combined,
        operation_id=uuid.uuid4().hex,
        whole_reason=requested.whole_reason,
    )
    new_state = store.commit_transition(
        worktree=worktree,
        remote=canonical_remote,
        subject=_transition_subject("rescope", selected.identity, selected.branch),
        intent=intent,
    )
    rescoped = new_state.claims[protocol.claim_key(selected.identity, selected.branch)]
    if parsed.json:
        _rescope_json(rescoped)
        return
    print(f"RESCOPED {_claim_subject(rescoped)}: {rescoped.claim_id}")


def _cmd_claim(parsed: argparse.Namespace, session: _WriteSession) -> int:
    requested = _request(parsed)
    versioned = checkout.versioned_paths()
    _reject_ungrounded_comma_scope(requested.scope, versioned, flag="--scope")
    n, total, share = _reject_wide_scope(requested.scope, versioned, requested.whole_reason)
    worktree, canonical_remote, observed = _store_observation()
    _require_state_ref(observed)
    checks: tuple[SliceCheck, ...] = ()
    target_issue: int | None = None
    replayed = None
    storage = _board_config(_resolve_toplevel()).storage
    if isinstance(requested.identity, protocol.IssueIdentity):
        target_issue = requested.identity.issue
        replayed = _matching_store_claim(observed, requested)
        if replayed is None:
            # A lane claim never reaches here (only an `IssueIdentity` not
            # already replayed does), so `session.forge()` -- built and
            # Erwartung-6-checked on this first call (issue #245) -- never
            # runs for a lane claim at all.
            client = session.forge()
            open_issues = client.list_open_board_issues()
            open_by_number = {issue.number: issue for issue in open_issues}
            projected = _board(
                client,
                tuple(observed.claims.values()),
                issues=open_issues,
                claim_ages=_claim_ages(worktree, observed),
            ).board
            checks = _slice_rule_checks(
                BoardReferenceLookup(client, client.repository.path, open_by_number),
                target_issue,
                projected,
                requested.out_of_order_reason,
                storage,
            )
    if any(check.level == "error" for check in checks):
        _refuse_claim(parsed.json, target_issue, checks)
        return 2
    for check in checks:
        print(check.render(), file=sys.stderr if parsed.json else sys.stdout)
    if replayed is None:
        intent = _claim_intent_from_request(requested, uuid.uuid4().hex)
        new_state = store.commit_transition(
            worktree=worktree,
            remote=canonical_remote,
            subject=_transition_subject("claim", requested.identity, requested.branch),
            intent=intent,
        )
        claimed = new_state.claims[protocol.claim_key(requested.identity, requested.branch)]
        live = tuple(new_state.claims.values())
    else:
        claimed = replayed
        live = tuple(observed.claims.values())
    touches = protocol.conflicting_claims(live, claimed)
    if parsed.json:
        return _claim_json(
            claimed,
            versioning=ScopeVersioning(n, total, share),
            touches=touches,
            checks=checks,
        )
    print(f"CLAIMED {_claim_subject(claimed, storage)}: {claimed.claim_id}")
    print(_claim_cost_line(n, total, requested.scope, touches))
    return 0


def _cmd_release(parsed: argparse.Namespace, session: _WriteSession) -> None:
    issue = _optional_issue_number(parsed.issue)
    identity = _resolved_identity(issue, session.release_branch or "")
    merged = None if parsed.merged is None else int(parsed.merged)
    outcome = _release_outcome(merged, parsed.abandoned)
    client: forge.ForgeReader | None = None
    if isinstance(outcome, protocol.MergedRelease):
        # Only a merged release verifies its landing pull request against the
        # forge (issue #245); an abandoned release -- lane or issue -- never
        # calls `session.forge()`, so it never resolves a repository or
        # invokes `gh`.
        _refuse_state_ref_merged_release(_resolve_toplevel())
        client = session.forge()
        _verify_merged_release(client, client.repository.path, identity, outcome)
    worktree, canonical_remote, observed = _store_observation()
    _require_state_ref(observed)
    selected = _selected_store_claim(observed, identity, session.release_branch, parsed.claim_id)
    if (
        parsed.branch is not None
        and parsed.claim_id is not None
        and selected.branch != parsed.branch
    ):
        raise protocol.ClaimUnavailableError(
            f"--branch {parsed.branch!r} and --claim-id {parsed.claim_id!r} disagree: the "
            f"claim's own branch is {selected.branch!r}; drop --branch or pass its own value"
        )
    role = parsed.role
    if not parsed.coordinator_override:
        if role is None:
            role = selected.role
        if (parsed.agent, role) != (selected.agent, selected.role):
            raise protocol.ClaimUnavailableError(
                "only the original claimant may release; use an explicit coordinator override "
                f"(holder={protocol._claimant_text(selected.agent, selected.role)!r}, "
                f"this session={protocol._claimant_text(parsed.agent, role)!r})"
            )
    resolved_role = role if role is not None else selected.role
    intent = protocol.ReleaseIntent(
        claim_id=selected.claim_id,
        agent=parsed.agent,
        role=resolved_role,
        outcome=outcome,
        operation_id=uuid.uuid4().hex,
        coordinator_override=parsed.coordinator_override,
    )
    new_state = store.commit_transition(
        worktree=worktree,
        remote=canonical_remote,
        subject=_transition_subject("release", selected.identity, selected.branch),
        intent=intent,
    )
    landing, hint = (
        (None, None) if client is None else _landing_report(client, identity, worktree, new_state)
    )
    storage = _board_config(_resolve_toplevel()).storage
    _print_release_result(
        ReleaseReport(
            selected, parsed.agent, resolved_role, outcome, client, landing, hint, storage
        ),
        as_json=parsed.json,
    )


def _landing_report(
    client: forge.ForgeReader,
    identity: protocol.ClaimIdentity,
    worktree: Path,
    new_state: store.ClaimState,
) -> tuple[ReleaseLanding | None, str | None]:
    """The `(landing, hint)` pair `_cmd_release` prints once its release
    transition already committed (issue #256): a forge hiccup here can only
    ever downgrade the report to `hint`, never undo or fail that release."""
    landed = (
        board.IssueReference(client.repository.path, identity.issue)
        if isinstance(identity, protocol.IssueIdentity)
        else None
    )
    try:
        landing = _release_landing(
            client, tuple(new_state.claims.values()), _claim_ages(worktree, new_state), landed
        )
    except forge.ForgeError as error:
        hint = (
            f"hint: could not read the board to report what this landing freed ({error}); "
            "run `aco board` once the forge is reachable"
        )
        return None, hint
    return landing, None


@dataclass(frozen=True)
class ReleaseReport:
    """Everything `_print_release_result` needs to render one `release`
    outcome (issue #256), bundled so the printer itself takes one argument
    instead of PLR0913's five-scalar ceiling: the just-released claim, the
    caller identity that performed it, the outcome it recorded, and the
    merged-landing board read (`None` for an abandoned or issueless release)
    alongside its `hint` fallback."""

    selected: protocol.ActiveClaim
    agent: str
    role: str | None
    outcome: protocol.ReleaseOutcome
    client: forge.ForgeReader | None
    landing: ReleaseLanding | None
    hint: str | None
    storage: board.Storage


def _print_release_result(report: ReleaseReport, *, as_json: bool) -> None:
    selected, landing, hint = report.selected, report.landing, report.hint
    if as_json:
        _release_json(selected, report.agent, report.role, report.outcome, landing)
        if hint is not None:
            print(hint, file=sys.stderr)
        return
    print(f"RELEASED {_claim_subject(selected, report.storage)}: {selected.claim_id}")
    if report.client is not None:
        if hint is not None:
            print(hint)
        else:
            assert landing is not None
            print(_release_freed_line(landing.freed, report.storage))
            print(_release_next_line(landing.next_item, report.storage))


def _cut_target(client: forge.ForgeWriter, number: int) -> board.Issue:
    """The open container `cut` targets, or why it refuses before any write."""
    open_issues = client.list_open_board_issues()
    target = next((issue for issue in open_issues if issue.number == number), None)
    if target is None:
        raise protocol.ClaimUnavailableError(f"#{number} is not an open container")
    if target.kind is not board.ItemKind.CONTAINER:
        raise protocol.ClaimUnavailableError(f"#{number} is not a container")
    parent = client.parent_issue(number)
    if parent is not None:
        raise protocol.ClaimUnavailableError(
            f"#{number} is itself a child of {parent.reference}; "
            "nested containers are not supported"
        )
    return target


def _link_created_child(
    client: forge.ForgeWriter, container: int, new_body: str, child: int, step: str
) -> None:
    """Write `new_body` (`container`'s `agent-claim` block, the
    just-created `child`'s slice entry already removed from it) back to
    `container`.

    Not atomic with `create_child` -- GitHub has no transaction across the
    two writes. A failure here still leaves the created child behind, so it
    raises the same `forge.ForgePartialChildCreationError` a failed relation
    write inside `create_child` itself would -- one type, so `_cmd_cut`
    renders one recovery message for either, `step` naming what an
    identical re-run finishes.
    """
    try:
        client.update_item_body(container, new_body)
    except protocol.ClaimError as error:
        raise forge.ForgePartialChildCreationError(
            child=child, parent=container, step=step, cause=error
        ) from error


def _print_cut_result(
    number: int, row_index: int | None, child: int, *, as_json: bool, adopted: bool
) -> None:
    """Print `cut`'s result, `adopted` naming whether `child` was an already-open
    child GitHub already recorded rather than one just created (#260). Today's
    (`adopted=False`) shape is exactly what `cut` has always printed -- no
    `adopted` key, `CUT` as the verb -- so a run that hits no matching child
    is byte-identical to before this behaviour existed."""
    if as_json:
        payload: dict[str, object] = {"container": number, "row": row_index, "child": child}
        if adopted:
            payload["adopted"] = True
        print(json.dumps(payload))
        return
    suffix = "" if row_index is None else f" row {row_index}"
    verb = "ADOPTED" if adopted else "CUT"
    print(f"{verb} #{number}{suffix} -> #{child}")


def _body_with_parent(skeleton: str, parent: int | None) -> str:
    """`skeleton`, preceded by one `Parent: #<parent>` line -- the same
    wording issue bodies already use for this fact -- when `parent` is
    given; `skeleton` itself otherwise. The one place that composes a
    parent line onto a body, shared by `cut`'s own child body and `body
    --template` (issue #262)."""
    return skeleton if parent is None else f"Parent: #{parent}\n\n{skeleton}"


def _cut_child_body(container: int) -> str:
    """The body `cut` writes for a fresh child: one `Parent: #<container>`
    line ahead of `board.BLOCK_CHILD_SKELETON`. A repeat `cut` after a
    partial failure reads this line back (`_orphan_names_container`) to
    tell `container`'s own orphan apart from an unrelated open issue that
    merely shares the row's title (#260)."""
    return _body_with_parent(board.BLOCK_CHILD_SKELETON, container)


def _orphan_names_container(body: str, container: int) -> bool:
    """Whether `body`'s first line is the `Parent: #<container>` line
    `_cut_child_body` writes -- the one signal that tells `container`'s own
    orphan apart from another open issue, another container's own failed
    cut, or a human-filed issue that happens to share the row's title."""
    return board.first_line(body) == f"Parent: #{container}"


def _adoptable_child(
    client: forge.ForgeWriter, container: int, title: str, idea_label: str | None
) -> board.ChildItem | None:
    """`container`'s already-open child titled exactly `title`, so a repeat
    `cut` after a partial failure (`forge.ForgePartialChildCreationError`)
    adopts the child GitHub already recorded instead of risking a second one
    (#260). Two sources can carry that child: already linked under
    `container` (`list_children`), or an orphan -- an open issue with no
    recorded parent at all, exactly the shape a failed `link_child` POST
    leaves behind. A title match alone is too weak to adopt an orphan: any
    unrelated open issue anywhere in the repository -- including one a
    human filed -- could share it. An orphan is adoptable only when it is
    also a `TASK` (never the container itself, never an idea-labelled item)
    and its body still names `container` as the parent `_cut_child_body`
    wrote for it; a recovery orphan is always exactly that shape, and
    nothing else can fake it. An orphan match is linked under `container`
    right here before it is returned, so the caller's remaining steps treat
    it exactly like an already-linked child; the container's own issue is
    never created twice for it. More than one open match refuses by name
    rather than guess which one the failed cut actually created. A closed
    linked child with that title refuses too -- adoption finishes an
    interrupted cut, it does not reopen a closed one. `None` when nothing
    matches, so the caller falls through to `create_child`.
    """
    linked = [
        child
        for child in client.list_children(container)
        if client.item_reference(child.number).title == title
    ]
    open_linked = [child.number for child in linked if child.state is board.ChildState.OPEN]
    orphans = [
        issue.number
        for issue in client.list_open_board_issues()
        if issue.title == title
        and issue.number != container
        and issue.kind is board.ItemKind.TASK
        and not board.has_label(issue.labels, idea_label)
        and _orphan_names_container(issue.body, container)
        and client.parent_issue(issue.number) is None
    ]
    open_matches = open_linked + orphans
    if len(open_matches) > 1:
        named = ", ".join(f"#{number}" for number in open_matches)
        raise protocol.ClaimUnavailableError(
            f"#{container}'s row {title!r} matches more than one open issue ({named}); "
            "adopt the right one by hand and remove the row"
        )
    if open_matches:
        [number] = open_matches
        if number in orphans:
            client.link_child(container, number)
        return board.ChildItem(number, board.ChildState.OPEN)
    closed = next((child for child in linked if child.state is board.ChildState.CLOSED), None)
    if closed is None:
        return None
    raise protocol.ClaimUnavailableError(
        f"#{container} already has a closed child #{closed.number} titled {title!r}; "
        "reopen it or remove the row by hand"
    )


def _block_slice_entries(data: Mapping[str, object]) -> list[dict[str, object]]:
    value = data.get("slice")
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _slice_row(entry: dict[str, object]) -> board.SliceRow:
    return board.SliceRow(cast(int, entry["index"]), cast(str, entry["title"]))


def _cut_link(
    number: int, data: Mapping[str, object], row_number: int | None
) -> board.SliceRow | None:
    """Which `[[slice]]` entry `cut` links its fresh child to (#150 §7):
    without `--row`, the first entry when the block carries one; with `--row
    N`, the entry `N` names, refusing by name when the block has no `slice`
    key at all or no such row left (every remaining entry is cuttable: a
    linked entry is removed from `data["slice"]` at the moment it is cut)."""
    entries = _block_slice_entries(data)
    if row_number is None:
        return _slice_row(entries[0]) if entries else None
    if "slice" not in data:
        raise protocol.ClaimUnavailableError(
            f"#{number} has no slice table; --row needs one to select a row from"
        )
    match = next((entry for entry in entries if entry["index"] == row_number), None)
    if match is None:
        cuttable = ", ".join(str(entry["index"]) for entry in entries) or "none"
        raise protocol.ClaimUnavailableError(
            f"#{number} has no row {row_number}; cuttable rows: {cuttable}"
        )
    return _slice_row(match)


def _require_matching_title(number: int, link: board.SliceRow, title: str) -> None:
    if title != link.title:
        raise protocol.ClaimUnavailableError(
            f"#{number}'s slice {link.index} is titled {link.title!r}; "
            "--title must match it exactly"
        )


def _located_block_or_refuse(
    number: int, body: str, *, command: str, storage: board.Storage = board.Storage.GITHUB
) -> board.LocatedBlock:
    """`body`'s located `agent-claim` block, or a by-name refusal before any
    write: `cut`, `rule`, and `ask` all need a body `parse_body` reads as
    VALID before they touch it, and share this one gate so the message is
    the same shape for all three. `storage` is forwarded to `parse_body`
    unchanged (issue #283): a state-ref item's own `[record]` table must
    read as a known key, not a malformed one."""
    parsed = board.parse_body(body, storage=storage)
    if parsed.read_state is board.BodyReadState.MALFORMED:
        defect = parsed.contract.defects[0]
        raise protocol.ClaimUnavailableError(
            f"#{number} {board.body_defect_text(defect)}; {command} needs a valid agent-claim block"
        )
    return board.locate_agent_claim_block(body)


def _cut_slice(
    client: forge.ForgeWriter,
    target: board.Issue,
    parsed: argparse.Namespace,
    idea_label: str | None,
    storage: board.Storage,
) -> int:
    number = target.number
    located = _located_block_or_refuse(number, target.body, command="cut", storage=storage)
    link = _cut_link(number, located.data, parsed.row)
    if link is not None:
        _require_matching_title(number, link, parsed.title)
    adopted = _adoptable_child(client, number, parsed.title, idea_label)
    try:
        child = (
            adopted.number
            if adopted is not None
            else client.create_child(
                parent=number,
                title=parsed.title,
                body=_cut_child_body(number),
                kind=board.ItemKind.TASK,
            )
        )
        if link is not None:
            remaining = [
                entry
                for entry in _block_slice_entries(located.data)
                if entry["index"] != link.index
            ]
            new_data = {**located.data, "slice": remaining}
            new_body = board.replace_agent_claim_block(target.body, located, new_data)
            step = f"remove row {link.index} from #{number}'s agent-claim block"
            _link_created_child(client, number, new_body, child, step)
    except forge.ForgePartialChildCreationError as error:
        raise protocol.ClaimUnavailableError(
            f"created #{error.child} but failed to {error.step}: {error.cause}; "
            "re-run the same cut -- it adopts the child"
        ) from error
    _print_cut_result(
        number,
        None if link is None else link.index,
        child,
        as_json=parsed.json,
        adopted=adopted is not None,
    )
    return 0


def _cmd_cut(parsed: argparse.Namespace, session: _WriteSession) -> int:
    client = session.forge.writer()
    number = int(parsed.issue)
    for operation in (
        forge.ForgeOperation.CREATE_CHILD,
        forge.ForgeOperation.LINK_CHILD,
        forge.ForgeOperation.UPDATE_ITEM_BODY,
    ):
        if client.capability(operation) is not forge.Capability.READ_WRITE:
            raise protocol.ClaimUnavailableError(
                f"this forge cannot {operation.value}; cut the slice by hand"
            )
    config = _load_board_config(client, _resolve_toplevel())
    return _cut_slice(
        client, _cut_target(client, number), parsed, config.idea_label, config.storage
    )


def _item_body_or_refuse(client: forge.ForgeReader, number: int, *, command: str) -> str:
    """The live body of issue `number`, or a by-name refusal before any
    write: `rule` and `ask` both target one existing issue, never a pull
    request."""
    reference = client.item_reference(number)
    if reference.state is forge.ItemState.MISSING:
        raise protocol.ClaimUnavailableError(f"#{number} does not exist")
    if reference.is_landing:
        raise protocol.ClaimUnavailableError(
            f"#{number} is a pull request, not an issue; {command} needs an issue"
        )
    return reference.body or ""


def _require_update_item_body(client: forge.ForgeWriter, *, command: str) -> None:
    if client.capability(forge.ForgeOperation.UPDATE_ITEM_BODY) is not forge.Capability.READ_WRITE:
        raise protocol.ClaimUnavailableError(
            f"this forge cannot update_item_body; {command} by hand"
        )


def _rule_remaining_open(new_body: str, *, storage: board.Storage) -> int:
    return sum(
        1 for line in board.expectation_lines(new_body, storage=storage) if line.ruling is None
    )


def _print_rule_result(
    number: int, line: board.ExpectationLine, open_remaining: int, *, as_json: bool
) -> None:
    ruling = cast(str, line.ruling)
    ruled_on = cast(date, line.ruled_on)
    if as_json:
        print(
            json.dumps(
                {
                    "item": number,
                    "index": line.index,
                    "ruling": ruling,
                    "ruled_on": ruled_on.isoformat(),
                    "open": open_remaining,
                }
            )
        )
        return
    print(f"RULED #{number} line {line.index} {ruling}; {open_remaining} line(s) still open")


def rule_item(
    client: forge.ForgeWriter, number: int, line: int, ruling: str, note: str | None
) -> tuple[board.ExpectationLine, int]:
    """`_cmd_rule`'s own write, extracted (issue #280) so `board --serve`'s
    `POST /rule` calls the exact same path a CLI `aco rule` invocation does
    -- one owner for "click -> ruled line", never a second one behind the
    loopback server. Returns the newly ruled line and how many the item
    still has open; raises `protocol.ClaimError` by name for every refusal
    (already ruled, out of range, a bad outcome, a malformed or missing
    item), which both callers turn into their own by-name response."""
    _require_update_item_body(client, command="rule")
    config = _load_board_config(client, _resolve_toplevel())
    body = _item_body_or_refuse(client, number, command="rule")
    _located_block_or_refuse(number, body, command="rule", storage=config.storage)
    ruled_on = datetime.now(UTC).date()
    new_body = board.rule_expectation(body, line, ruling, ruled_on, note=note)
    client.update_item_body(number, new_body)
    ruled_line = board.expectation_lines(new_body, storage=config.storage)[line - 1]
    return ruled_line, _rule_remaining_open(new_body, storage=config.storage)


def _cmd_rule(parsed: argparse.Namespace, session: _WriteSession) -> int:
    client = session.forge.writer()
    number = int(parsed.item)
    ruled_line, open_remaining = rule_item(client, number, parsed.line, parsed.ruling, parsed.note)
    _print_rule_result(number, ruled_line, open_remaining, as_json=parsed.json)
    return 0


def _board_server(parsed: argparse.Namespace, session: _WriteSession) -> board_serve.BoardServer:
    """`board --serve`'s bound, listening server (issue #280), built but not
    yet run: a loopback page that reads through `_board_html_page` and
    writes through `rule_item`, exactly like `--html` and `aco rule` do
    apart -- `board_serve.py` is transport only, so this function is still
    the one place that resolves the forge, renders a page, and rules a
    line. Split from `_cmd_board_serve`'s own `serve_forever` loop so a test
    can bind a real ephemeral port and drive it without blocking."""
    client = session.forge.writer()
    read_session = _ReadSession(forge=session.forge)
    server: board_serve.BoardServer

    def render_page(refused: str | None) -> str:
        served = board_html.ServedRuleForm(token=server.token, refused=refused)
        return _board_html_page(read_session, served=served)

    def post_rule(item: int, line: int, ruling: str, note: str | None) -> board_serve.RuleOutcome:
        try:
            rule_item(client, item, line, ruling, note)
        except protocol.ClaimError as error:
            return board_serve.RuleOutcome(refusal=str(error))
        return board_serve.RuleOutcome(refusal=None)

    server = board_serve.start(port=parsed.port, render_page=render_page, rule_item=post_rule)
    return server


def _cmd_board_serve(parsed: argparse.Namespace, session: _WriteSession) -> int:
    server = _board_server(parsed, session)
    print(server.url, flush=True)
    try:
        server.httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.httpd.server_close()
    return 0


@dataclass(frozen=True)
class _AskedLine:
    """One `aco ask` write's result (issue #295): the item, the fresh
    line's 1-based index, its `text`/`default`, and whichever card fields
    were given -- bundled so `_print_ask_result` takes one value instead of
    five loose ones."""

    item: int
    index: int
    text: str
    default: str
    card: board.ExpectationCardFields


def _print_ask_result(asked: _AskedLine, *, as_json: bool) -> None:
    if as_json:
        payload = {
            "item": asked.item,
            "index": asked.index,
            "text": asked.text,
            "default": asked.default,
        }
        payload.update(
            (key, value) for key, value in asdict(asked.card).items() if value is not None
        )
        print(json.dumps(payload))
        return
    print(f"ASKED #{asked.item} line {asked.index}: {asked.text}")


def _read_picture_file(path: str) -> str:
    """`--picture FILE.svg`'s own filesystem boundary (issue #295): read
    before any forge call, so a missing file refuses before the item body
    is even fetched. Content validation (size, `<svg>` root, no `<script>`,
    no external `href`) is `board.append_expectation`'s -- one owner, shared
    with the body parser's own defects."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError as error:
        raise protocol.ClaimError(f"--picture {path} could not be read: {error}") from error


def _cmd_ask(parsed: argparse.Namespace, session: _WriteSession) -> int:
    picture = _read_picture_file(parsed.picture) if parsed.picture else None
    card = board.ExpectationCardFields(
        question=parsed.question, example=parsed.example, picture=picture
    )
    client = session.forge.writer()
    _require_update_item_body(client, command="ask")
    config = _load_board_config(client, _resolve_toplevel())
    number = int(parsed.item)
    body = _item_body_or_refuse(client, number, command="ask")
    _located_block_or_refuse(number, body, command="ask", storage=config.storage)
    new_body = board.append_expectation(body, parsed.text, parsed.default, card=card)
    index = len(board.expectation_lines(new_body, storage=config.storage))
    client.update_item_body(number, new_body)
    asked = _AskedLine(
        item=number, index=index, text=parsed.text, default=parsed.default, card=card
    )
    _print_ask_result(asked, as_json=parsed.json)
    return 0


_READ_HANDLERS: dict[str, Callable[[argparse.Namespace, _ReadSession], int | None]] = {
    "check": _cmd_check,
    "brief": _cmd_brief,
    "board": _cmd_board,
    "rulings": _cmd_rulings,
    "next": _cmd_next,
}
_WRITE_HANDLERS: dict[str, Callable[[argparse.Namespace, _WriteSession], int | None]] = {
    "rescope": _cmd_rescope,
    "claim": _cmd_claim,
    "release": _cmd_release,
    "cut": _cmd_cut,
    "ask": _cmd_ask,
    "rule": _cmd_rule,
}


def _release_branch_for(parsed: argparse.Namespace) -> str | None:
    if parsed.coordinator_override:
        protocol._require_coordinator_override(parsed.role)
    if parsed.branch is not None:
        # An explicit --branch selects the lane identity by name, exactly
        # like claim's own --branch, but never requires the checkout to be
        # on it (issue #250): a lane's worktree may be gone, or the release
        # may run from the coordinator's primary checkout.
        return parsed.branch
    if parsed.issue is not None and parsed.claim_id is not None:
        return None
    release_branch = checkout._git_output(["branch", "--show-current"])
    if release_branch:
        return release_branch
    if parsed.issue is None:
        raise protocol.ClaimUnavailableError(
            "lane release requires a non-empty current branch; "
            "check out the docs/ or fix/ lane branch, or pass "
            "an issue number"
        )
    raise protocol.ClaimUnavailableError(
        "release without --claim-id requires a non-empty current branch; pass --claim-id"
    )


def _bootstrap_state() -> int:
    """Create `refs/aco/state` if proven absent; report the existing tip
    untouched when it is already there. Forge-free (issue #245): `--repo` is
    meaningless here and unused. `_canonical_remote_name` reads through
    `_board_config` (#315), so an untracked `board.toml` refuses here too,
    before this command's own first write."""
    canonical_remote = _canonical_remote_name(_resolve_toplevel())
    print(store.bootstrap(worktree=Path.cwd(), remote=canonical_remote))
    return 0


# A short, still-practically-unique prefix of an `ObjectId`'s 40 hex
# characters (git's own `--short` abbreviation depends on the repository's
# object count, which the bundle filename has no reason to vary with).
_RESET_BUNDLE_SHA_LENGTH = 12


class ResetStep(StrEnum):
    """The five lines `reset` prints, in the one order the dry run and the
    real run share (plan review, 19.09.2026): export, delete the remote
    ref, delete the local one if present, clear every worktree's lineage
    stamp and fetch anchor, bootstrap fresh."""

    EXPORT = "export"
    DELETE_REMOTE = "delete_remote"
    DELETE_LOCAL = "delete_local"
    CLEAR_STAMPS = "clear_stamps"
    BOOTSTRAP = "bootstrap"


RESET_STEP_ORDER: tuple[ResetStep, ...] = (
    ResetStep.EXPORT,
    ResetStep.DELETE_REMOTE,
    ResetStep.DELETE_LOCAL,
    ResetStep.CLEAR_STAMPS,
    ResetStep.BOOTSTRAP,
)


@dataclass(frozen=True)
class ResetExportTarget:
    tip: protocol.ObjectId
    destination: Path


@dataclass(frozen=True)
class ResetPlan:
    """Every fact `reset`'s five lines are built from, read once before
    anything is exported or deleted (plan review, 19.09.2026). The dry run
    prints this verbatim with a `would: ` prefix; the real run prints the
    same per-step text as each action completes -- the two share the exact
    same line-building functions below, so they cannot say different things
    about the same reset."""

    remote: str
    remote_tip: protocol.ObjectId | None
    export_target: ResetExportTarget | None
    local_ref_present: bool
    worktree_count: int


def _reset_bundle_name(repository: str, today: date, tip: protocol.ObjectId) -> str:
    return f"aco-state-{repository}-{today.isoformat()}-{tip[:_RESET_BUNDLE_SHA_LENGTH]}.bundle"


def _reset_restore_command(destination: Path) -> str:
    """`export_state_bundle` bundles `store.EXPORT_BUNDLE_REF`, never the
    shared `store.STATE_REF` (issue #298, 19.09.2026 REVISE findings 1+2)
    -- the bundle's own head therefore carries that name (`git bundle
    list-heads` shows it), and the fetch renames it to `STATE_REF` on the
    way in.
    """
    return f"git fetch {destination} {store.EXPORT_BUNDLE_REF}:{store.STATE_REF}"


@dataclass(frozen=True)
class ResetExportConfig:
    """`--no-export`/`--export-dir`, resolved once (issue #298): keeps
    `_build_reset_plan` under the five-argument ceiling without folding an
    unrelated pair of facts into `worktree` or `remote`."""

    enabled: bool
    directory: Path


def _resolved_reset_export_config(parsed: argparse.Namespace, toplevel: Path) -> ResetExportConfig:
    directory = parsed.export_dir if parsed.export_dir is not None else toplevel.parent
    return ResetExportConfig(enabled=not parsed.no_export, directory=directory)


def _build_reset_plan(
    *,
    worktree: Path,
    remote: str,
    state: protocol.ClaimState,
    export: ResetExportConfig,
    today: date,
) -> ResetPlan:
    export_target = None
    if export.enabled and state.tip is not None:
        destination = export.directory / _reset_bundle_name(worktree.name, today, state.tip)
        export_target = ResetExportTarget(tip=state.tip, destination=destination)
    return ResetPlan(
        remote=remote,
        remote_tip=state.tip,
        export_target=export_target,
        local_ref_present=store.local_state_ref_exists(worktree),
        worktree_count=len(store.list_worktrees(worktree)),
    )


def _reset_export_line(plan: ResetPlan, *, done: bool) -> str:
    if plan.export_target is None:
        if plan.remote_tip is None:
            return f"nothing to export: {store.STATE_REF} does not exist on {plan.remote}"
        return f"skipped export (--no-export): {store.STATE_REF} at {plan.remote_tip} not saved"
    verb = "exported" if done else "export"
    restore = _reset_restore_command(plan.export_target.destination)
    return (
        f"{verb} {store.STATE_REF} at {plan.export_target.tip} to "
        f"{plan.export_target.destination} (restore with: {restore})"
    )


def _reset_delete_remote_line(plan: ResetPlan, *, done: bool) -> str:
    if plan.remote_tip is None:
        return f"nothing to delete on {plan.remote}: {store.STATE_REF} does not exist"
    verb = "deleted" if done else "delete"
    return f"{verb} {store.STATE_REF} on {plan.remote} (lease {plan.remote_tip})"


def _reset_delete_local_line(*, present: bool, done: bool) -> str:
    if not present:
        return f"no local {store.STATE_REF} to delete"
    verb = "deleted" if done else "delete"
    return f"{verb} local {store.STATE_REF}"


def _reset_clear_stamps_line(*, worktree_count: int, done: bool) -> str:
    verb = "cleared" if done else "clear"
    plural = "" if worktree_count == 1 else "s"
    return f"{verb} lineage stamps and fetch anchors in {worktree_count} worktree{plural}"


def _reset_bootstrap_line(*, tip: protocol.ObjectId | None) -> str:
    if tip is None:
        return "bootstrap a fresh empty state"
    return f"bootstrapped a fresh empty state at {tip}"


def _print_reset_dry_run(plan: ResetPlan) -> None:
    lines: dict[ResetStep, str] = {
        ResetStep.EXPORT: _reset_export_line(plan, done=False),
        ResetStep.DELETE_REMOTE: _reset_delete_remote_line(plan, done=False),
        ResetStep.DELETE_LOCAL: _reset_delete_local_line(
            present=plan.local_ref_present, done=False
        ),
        ResetStep.CLEAR_STAMPS: _reset_clear_stamps_line(
            worktree_count=plan.worktree_count, done=False
        ),
        ResetStep.BOOTSTRAP: _reset_bootstrap_line(tip=None),
    }
    for step in RESET_STEP_ORDER:
        print(f"would: {lines[step]}")


def _execute_reset(*, worktree: Path, remote: str, plan: ResetPlan) -> None:
    """Export -> delete remote -> delete local -> clear stamps -> bootstrap
    (issue #298), each step printed the moment it completes. An export
    failure raises before anything else runs; a remote-deletion failure
    (rejected, or the lease gone stale) raises with the local ref left
    exactly as it was, and whatever export ran left on disk."""
    if plan.export_target is not None:
        store.export_state_bundle(
            worktree=worktree,
            tip=plan.export_target.tip,
            destination=plan.export_target.destination,
        )
    print(_reset_export_line(plan, done=True))
    local_deleted = store.delete_state_ref(
        worktree=worktree, remote=remote, expected_remote_tip=plan.remote_tip
    )
    print(_reset_delete_remote_line(plan, done=True))
    print(_reset_delete_local_line(present=local_deleted, done=True))
    cleared_worktrees = store.clear_lineage_stamps(worktree=worktree)
    print(_reset_clear_stamps_line(worktree_count=len(cleared_worktrees), done=True))
    fresh_tip = store.bootstrap(worktree=worktree, remote=remote)
    print(_reset_bootstrap_line(tip=fresh_tip))


def _reset_observation() -> tuple[Path, str, protocol.ClaimState]:
    """`reset`'s own state read (issue #298, 19.09.2026 gate finding 1):
    `store.read_state_for_reset` instead of `_store_observation`'s ordinary
    `fetch_state`, so a broken lineage -- exactly what `reset` exists to
    recover from -- never blocks it, and so a dry run, a live-claim
    refusal, or a failed export writes no per-worktree stamp or anchor
    (finding 2)."""
    canonical_remote = _canonical_remote_name(_resolve_toplevel())
    worktree = Path.cwd()
    state = store.read_state_for_reset(worktree=worktree, remote=canonical_remote)
    return worktree, canonical_remote, state


def _reset_state(parsed: argparse.Namespace) -> int:
    """`reset` (issue #298): exports `STATE_REF`, deletes it on the remote
    with a lease and locally if present, clears every worktree's lineage
    stamp and fetch anchor, and bootstraps a fresh empty state. Forge-free,
    like `bootstrap`. A live claim always refuses -- `--confirm` or not --
    printing its claim lines instead of touching anything: a reset over live
    work is data loss with no owner.
    """
    worktree, remote, state = _reset_observation()
    if state.claims:
        storage = board.load_config(_resolve_toplevel() / board.CONFIG_PATH).storage
        ages = _claim_ages(worktree, state)
        _status(tuple(state.claims.values()), None, ages, storage)
        return 2
    export = _resolved_reset_export_config(parsed, _resolve_toplevel())
    plan = _build_reset_plan(
        worktree=worktree,
        remote=remote,
        state=state,
        export=export,
        today=datetime.now(UTC).date(),
    )
    if not parsed.confirm:
        _print_reset_dry_run(plan)
        return 0
    _execute_reset(worktree=worktree, remote=remote, plan=plan)
    return 0


_FORGE_FREE_COMMANDS: dict[str, Callable[[argparse.Namespace], int]] = {
    "bootstrap": lambda _parsed: _bootstrap_state(),
    "reset": _reset_state,
}


def _dispatch(parsed: argparse.Namespace) -> int:
    if parsed.command in {"claim", "release", "rescope"}:
        parsed.agent = checkout._resolved_agent(parsed.agent)
    if parsed.command in _FORGE_FREE_COMMANDS:
        return _FORGE_FREE_COMMANDS[parsed.command](parsed)
    if parsed.command == "item":
        if parsed.item_command == "new":
            return _cmd_item_new(parsed)
        if parsed.item_command == "edit":
            return _cmd_item_edit(parsed)
        if parsed.item_command == "close":
            return _cmd_item_close(parsed)
        return _cmd_item_show(parsed, _ReadSession(forge=_LazyForge(parsed.repo)))
    release_branch = _release_branch_for(parsed) if parsed.command == "release" else None
    forge_accessor = _LazyForge(parsed.repo)
    if parsed.command == "board" and parsed.serve:
        # `board --serve` writes through a click (issue #280), so it needs
        # the writer session even though its own name reads like every
        # other `board` output mode.
        result = _cmd_board_serve(parsed, _WriteSession(forge=forge_accessor, release_branch=None))
    elif parsed.command in _READ_HANDLERS:
        result = _READ_HANDLERS[parsed.command](parsed, _ReadSession(forge=forge_accessor))
    else:
        result = _WRITE_HANDLERS[parsed.command](
            parsed, _WriteSession(forge=forge_accessor, release_branch=release_branch)
        )
    return 0 if result is None else result


def _workspace_config_path() -> Path:
    return workspace.default_config_path(os.environ)


def _register_workspace(parsed: argparse.Namespace) -> int:
    handoff = workspace.WorkspaceRegistration(
        parsed.project,
        parsed.path,
        parsed.session_id,
        parsed.agent,
        parsed.model,
        provider=providers.Provider(parsed.provider),
        live_pid=parsed.live_pid,
    )
    created = workspace.register_project(handoff, _workspace_config_path())
    status = "registered" if created else "already registered"
    print(f"{parsed.project}: {status}")
    return 0


def _run_workspace(parsed: argparse.Namespace) -> int:
    outcomes = workspace.run_projects(_workspace_config_path(), parsed.project)
    for outcome in outcomes:
        suffix = f": {outcome.detail}" if outcome.detail else ""
        print(f"{outcome.project}: {outcome.state}{suffix}")
    return (
        1
        if any(
            outcome.state in {workspace.RunState.FAILED, workspace.RunState.UNKNOWN}
            for outcome in outcomes
        )
        else 0
    )


def _login_summary(result: workspace.LoginRunResult) -> str:
    if result.attempt.failure is not None:
        return "Workspace recovery failed."
    outcomes = result.attempt.outcomes
    failed_states = {workspace.RunState.FAILED, workspace.RunState.UNKNOWN}
    failed = sum(state in failed_states for _, state in outcomes)
    already_live = sum(state is workspace.RunState.EXTERNAL for _, state in outcomes)
    recovered = len(outcomes) - failed - already_live
    summary = f"Workspace recovery completed for {recovered} project(s)."
    if already_live:
        summary += f" {already_live} project(s) already had live owners; no console was opened."
    if failed:
        summary += f" {failed} project(s) failed recovery."
    return summary


def _run_at_login() -> int:
    try:
        result = workspace.run_login_recovery(
            _workspace_config_path(), workspace.login_attempt_path(os.environ)
        )
    except workspace.WorkspaceError:
        terminal.notify_login_recovery("Workspace recovery could not record its attempt.")
        return 2
    terminal.notify_login_recovery(_login_summary(result))
    return result.exit_status


def _login_status() -> int:
    launcher = workspace.login_launcher_state(os.environ, Path(sys.executable))
    configuration = workspace.login_configuration_state(_workspace_config_path())
    print(f"launcher: {launcher}")
    print(f"configuration: {configuration}")
    try:
        attempt = workspace.load_login_attempt(workspace.login_attempt_path(os.environ))
    except FileNotFoundError:
        print("attempt: no login attempt recorded")
        return 0
    except workspace.WorkspaceError:
        print("attempt: malformed")
        return 1
    print(f"attempt: {attempt.attempt_id} {attempt.started_at} {attempt.state}")
    for project, outcome in attempt.outcomes:
        print(f"{project}: {outcome}")
    if attempt.failure is not None:
        print(f"workspace: {attempt.failure}")
    if attempt.completed_at is not None:
        print(f"completed: {attempt.completed_at}")
    return 0


def _login_operation(parsed: argparse.Namespace) -> int:
    if parsed.repo is not None:
        raise protocol.ClaimError("--repo is meaningless for login recovery operations")
    if parsed.login_command == "enable":
        changed = workspace.enable_login(_workspace_config_path(), os.environ, Path(sys.executable))
        print("login launcher enabled" if changed else "login launcher already enabled")
        return 0
    if parsed.login_command == "disable":
        changed = workspace.disable_login(os.environ)
        print("login launcher disabled" if changed else "login launcher already disabled")
        return 0
    return _login_status()


def _local_operation(parsed: argparse.Namespace) -> int:
    if parsed.command == "_run-at-login":
        return _run_at_login()
    if parsed.command == "login":
        return _login_operation(parsed)
    if parsed.repo is not None:
        raise protocol.ClaimError("--repo is meaningless for workspace operations")
    return _register_workspace(parsed) if parsed.command == "register" else _run_workspace(parsed)


def _read_status_body_or_dispatch(parsed: argparse.Namespace) -> int:
    """`status` and `body` are forge-free (issue #245, #262): both are
    resolved here, ahead of `_dispatch`'s own `_LazyForge`, so neither ever
    resolves one."""
    if parsed.command == "status":
        return _cmd_status(parsed)
    if parsed.command == "body":
        return _cmd_body(parsed)
    return _dispatch(parsed)


def main(arguments: list[str] | None = None) -> int:
    try:
        # `board.parse_item_reference` is an argparse `type=`; its own refusal is
        # `protocol.ClaimError`, not the `ValueError` argparse's own
        # conversion-error handling catches, so it needs this same try here
        # rather than reaching the parser unguarded.
        parsed = _parser().parse_args(arguments)
    except protocol.ClaimError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 2
    if parsed.command in {"_run-at-login", "register", "run", "login"}:
        try:
            return _local_operation(parsed)
        except protocol.ClaimError as error:
            print(f"ERROR: {error}", file=sys.stderr)
            return 2
    if parsed.command == "protect":
        return _protect()
    try:
        return _read_status_body_or_dispatch(parsed)
    except protocol.ClaimError as error:
        print(f"ERROR: {error}", file=sys.stderr)
        if getattr(parsed, "json", False):
            print(json.dumps({"ok": False, "error": str(error)}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
