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
lands. Neither is speculative: it is the port surface issue #131 declares
today, each with a named future caller.

`_BoardRequestHandler.do_GET`/`do_POST`/`log_message` (issue #280) are
`http.server`'s own dispatch and logging surface: `handle_one_request` calls
`do_GET`/`do_POST` by `getattr(self, "do_" + self.command)`, and every stdlib
logging call reaches the override through `BaseHTTPRequestHandler`'s own
`self.log_message(...)` -- never by a literal call this package writes, so
vulture never sees a caller for any of the three.
"""

from agent_coordination.board import NoItemKind
from agent_coordination.board_serve import _BoardRequestHandler
from agent_coordination.forge import Capability, ForgeUnsupportedError

_referenced_only_for_vulture = (
    NoItemKind.DOCS,
    NoItemKind.FIX,
    ForgeUnsupportedError,
    Capability.UNSUPPORTED,
    _BoardRequestHandler.do_GET,
    _BoardRequestHandler.do_POST,
    _BoardRequestHandler.log_message,
)
