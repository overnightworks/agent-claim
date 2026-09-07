"""Vulture whitelist.

`NoItemKind.DOCS` and `NoItemKind.FIX` are never referenced by a literal
attribute access; the code reaches them only by dynamic construction
(`NoItemKind(value)`) and iteration (`for kind in NoItemKind`), both invisible
to vulture's static analysis. Naming them here is the whole fix -- this file
is never imported by the package itself.

`ForgeUnsupportedError` and `Capability.UNSUPPORTED` are the port's typed
capability-refusal surface (decision record 0001 §2, §4 criterion D3): the
GitHub adapter never refuses an operation, so both stay uncalled/unconstructed
until the first adapter that can refuse one (the GitLab adapter, per #112)
lands. `RepositoryId.host` is likewise part of the port's declared shape with
no production reader yet -- it only matters once a second forge host exists.
None of this is speculative: it is the port surface issue #131 declares today,
each with a named future caller.

`ClaimWriter.post_comment` (the protocol method) and `GitHubForge.post_comment`
(its adapter implementation) have no production caller in this cut (issue #176
slice C2): `POST_COMMENT` is the one forge operation the adopted plan names as
staying uncalled here, for step 6's landing receipt, which has not landed yet.

`LedgerActiveClaim.quarantined_by` (issue #136) has no production reader left:
`release`/`pr-check` moved onto the store in this same slice and stopped
consulting it. The field, its writer (`_quarantine_active_claims`), and the
rest of the ledger-comment aggregation walk are D-scheduled infrastructure the
import reader (`bootstrap --ledger`) still needs whole -- kept intact rather
than picked apart function by function ahead of that deletion.
"""

from agent_claim.board import NoItemKind
from agent_claim.forge import Capability, ForgeUnsupportedError, RepositoryId
from agent_claim.protocol import ClaimWriter, LedgerActiveClaim

_referenced_only_for_vulture = (
    NoItemKind.DOCS,
    NoItemKind.FIX,
    ForgeUnsupportedError,
    Capability.UNSUPPORTED,
    RepositoryId("host", (), "name").host,
    ClaimWriter.post_comment,
    LedgerActiveClaim.quarantined_by,
)
