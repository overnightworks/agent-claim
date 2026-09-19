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

`metrics.measure`, `Size.MEDIUM`/`Size.LARGE`, and the report dataclasses'
presentation-only fields (`LaneEvent.rescopes`, `LaneMeasure.landing_wait_hours`,
`SizeClassStats.p80_hours`, `ContainerSum.n_estimated`/`n_without_size`,
`Parallelism.overlapping_lanes`) have no caller inside `src` yet (issue #308):
`store.claim_lifecycle` and the `aco metrics` command that read this module's
report are Lane 1b of #299, not yet landed. Each is a named future caller,
not speculative surface; tests already exercise every one of these names.

`TrunkWorkItemClassification.numbers` (issue #304) is a frozen dataclass
field read only by the class's own generated `__eq__`/`__repr__`, invisible
to vulture the same way an enum member reached only through dynamic
construction is: `checkout.trunk_landings` already stores every trunk
commit's classification on `TrunkLanding.classification`, and the field
carries the work items a landing names for the still-undispatched consumer
that reads them back (issue #304 review, finding B2's "lane 2").
"""

from datetime import UTC, date, datetime

from agent_coordination.board import NoItemKind, TrunkWorkItemClassification
from agent_coordination.board_serve import _BoardRequestHandler
from agent_coordination.forge import Capability, ForgeUnsupportedError
from agent_coordination.metrics import (
    ContainerSum,
    LaneEvent,
    LaneMeasure,
    Parallelism,
    Size,
    SizeClassStats,
    measure,
)

_lane_event_for_vulture = LaneEvent(
    item="",
    size=None,
    container=None,
    claimed_at=datetime(2000, 1, 1, tzinfo=UTC),
    released_at=None,
    landed_at=None,
    rescopes=0,
)
_lane_measure_for_vulture = LaneMeasure(
    item="", size=None, container=None, wall_hours=0.0, landing_wait_hours=None
)
_size_class_stats_for_vulture = SizeClassStats(
    size=Size.SMALL, n=0, median_hours=0.0, p80_hours=0.0, weak=False
)
_container_sum_for_vulture = ContainerSum(container="", hours=0.0, n_estimated=0, n_without_size=0)
_parallelism_for_vulture = Parallelism(day=date(2000, 1, 1), overlapping_lanes=0)

_referenced_only_for_vulture = (
    NoItemKind.DOCS,
    NoItemKind.FIX,
    ForgeUnsupportedError,
    Capability.UNSUPPORTED,
    _BoardRequestHandler.do_GET,
    _BoardRequestHandler.do_POST,
    _BoardRequestHandler.log_message,
    measure,
    Size.MEDIUM,
    Size.LARGE,
    _lane_event_for_vulture.rescopes,
    _lane_measure_for_vulture.landing_wait_hours,
    _size_class_stats_for_vulture.p80_hours,
    _container_sum_for_vulture.n_estimated,
    _container_sum_for_vulture.n_without_size,
    _parallelism_for_vulture.overlapping_lanes,
    TrunkWorkItemClassification((0,)).numbers,
)
