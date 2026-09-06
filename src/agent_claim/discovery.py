"""Discover, adopt, and create the canonical claim ledger issue."""

from __future__ import annotations

from . import forge
from .protocol import (
    LEDGER_BODY_MARKER,
    LEDGER_LABEL,
    ClaimError,
    ClaimUnavailableError,
)


def _issue_first_line(item: forge.LedgerItem) -> str:
    return item.body.partition("\n")[0]


def _foreign_contract(item: forge.LedgerItem) -> bool:
    first = _issue_first_line(item)
    if first == LEDGER_BODY_MARKER:
        return False
    return first.startswith("<!-- ") and ("claim" in first or "ledger" in first)


def _select_ledger(items: tuple[forge.LedgerItem, ...]) -> int | None:
    """Resolve the canonical ledger from an issue-row snapshot, or raise on
    a locked-marker violation or a competing foreign coordination contract."""
    ledgers: list[int] = []
    foreign: list[int] = []
    for item in items:
        if item.is_landing:
            continue
        if not item.author_is_trusted:
            continue
        if item.state is forge.ItemState.OPEN and _foreign_contract(item):
            foreign.append(item.number)
            continue
        if _issue_first_line(item) != LEDGER_BODY_MARKER or item.state is forge.ItemState.CLOSED:
            continue
        if not item.locked:
            raise ClaimUnavailableError(
                f"ledger candidate #{item.number} is not locked; run bootstrap"
            )
        ledgers.append(item.number)
    if foreign:
        raise ClaimError(
            f"another coordination contract exists on issue(s) {foreign}; refusing to compete"
        )
    return min(ledgers) if ledgers else None


def discover_ledger(client: forge.ForgeReader) -> int | None:
    """Find the single open, locked protocol ledger without changing GitHub state.

    Every canonical ledger is labelled `LEDGER_LABEL`, attached and backfilled
    by `reconcile` (`protocol.reconcile_all_labels`) rather than by discovery
    itself, and only the canonical ledger ever carries it. Asking for that
    exact label is genuinely atomic under normal
    operation: the answer is at most one issue, always one response, one
    snapshot — never a fetch spanning multiple page requests that a
    concurrent open/close could shift an issue across.

    Only when the labelled query comes back empty — an unlabelled legacy
    ledger, or a genuine absence — does discovery fall back to scanning every
    open issue. That scan can only report absence (return `None`) when the
    listing's own `pages_fetched` says it was one snapshot; the adapter owns
    what "one page" means for its provider, so this reads only that
    provenance, never a duplicated page-size constant. A fallback the
    listing itself reports as spanning more than one page can never prove
    absence, no matter how the counts line up — an issue that closed on an
    already-consumed page while another opened could leave the item count
    exactly equal to the live open-issue count while still hiding an
    unlabelled legacy ledger that shifted across the page boundary — so that
    case always fails loud instead. Within a listing the adapter reports as
    one page, the open-issue-count comparison is still worth keeping as an
    additional detector: it cannot involve a page-boundary shift, but the
    count itself comes from a separate request that could still have raced
    an open or close between the two calls. Whichever check trips, this must
    fail loud rather than report "no ledger" — reporting that wrongly
    invites `bootstrap`, which would create a second, competing ledger next
    to one that still exists.
    """
    labelled = client.list_items(state=forge.ItemState.OPEN, label=LEDGER_LABEL)
    ledger = _select_ledger(labelled.items)
    if ledger is not None:
        return ledger
    listing = client.list_items(state=forge.ItemState.OPEN)
    ledger = _select_ledger(listing.items)
    if ledger is not None:
        return ledger
    if listing.pages_fetched > 1:
        raise ClaimError(
            f"could not establish ledger absence over {listing.pages_fetched} pages of "
            "open issues; retry, do not bootstrap"
        )
    if len(listing.items) != client.open_item_count():
        raise ClaimError(
            "ledger discovery fetch may be incomplete (the open-issue count "
            "changed mid-fetch); retry rather than bootstrap"
        )
    return None
