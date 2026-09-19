"""Pure lane-metrics computation (issue #308, Lane 1a of #299).

Turns lane events into wall-clock measures, size-class statistics, per-item
estimates, per-container sums, and observed daily parallelism -- with no git,
no clock, and no forge: every timestamp is a `datetime` the caller already
holds. A `SizeClassStats` names its median (`statistics.median`) and p80
(nearest-rank) over the lanes measured for one `Size`; too few lanes
(`n < WEAK_SAMPLE_THRESHOLD`) sets `weak`, but never withholds the value --
withholding a weak number is the caller's presentation choice, not this
module's.

`Size` is defined here, not in `items.py`, because no item record carries a
size yet: `items.py` will import it from here once Lane 1b of #299 adds
`record.size`, since a work item's size is a concept `items.py` will own but
does not yet -- this module is the first and, for now, only owner.
"""

from __future__ import annotations

import statistics
from collections.abc import Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from enum import StrEnum

# The nearest-rank percentile `SizeClassStats.p80_hours` reads (issue #299
# "Form"): the 80th percentile, named once so the one call site never repeats
# the literal.
P80_PERCENTILE = 80
# Below this many measured lanes, a size class's median/p80 rests on too few
# samples to trust; `SizeClassStats.weak`/`Estimate.weak` name that -- they
# never hide the number itself.
WEAK_SAMPLE_THRESHOLD = 3


class Size(StrEnum):
    """A work item's rough size class, small enough to estimate a lane's
    duration from -- the same three classes the board's presentation layer
    will read once `items.py` owns `record.size` (Lane 1b of #299).

    Declared in this order (S, M, L) so any iteration over `Size` -- for
    example when listing the measured classes -- reads in size order rather
    than the alphabetical order of the letters themselves.
    """

    SMALL = "S"
    MEDIUM = "M"
    LARGE = "L"


@dataclass(frozen=True)
class LaneEvent:
    """One claim's lifecycle, as a future event reader (`store.claim_lifecycle`,
    Lane 1b of #299) will read it from the ref history and trunk landings.
    `released_at`/`landed_at` are `None` while the claim has not yet reached
    that stage; `rescopes` is the number of scope changes the claim lived
    through, carried for a future reader of this same event, not read by
    `measure` itself.
    """

    item: str
    size: Size | None
    container: str | None
    claimed_at: datetime
    released_at: datetime | None
    landed_at: datetime | None
    rescopes: int


@dataclass(frozen=True)
class OpenItem:
    """One still-open work item, the unit `Estimate` and `ContainerSum` are
    computed for."""

    item: str
    size: Size | None
    container: str | None


@dataclass(frozen=True)
class LaneMeasure:
    """A completed claim's measured durations, in hours. `landing_wait_hours`
    is `None` when the lane has not landed yet."""

    item: str
    size: Size | None
    container: str | None
    wall_hours: float
    landing_wait_hours: float | None


@dataclass(frozen=True)
class SizeClassStats:
    """The measured lanes of one size, with `weak` naming rather than hiding
    a median/p80 that rests on fewer than `WEAK_SAMPLE_THRESHOLD` samples."""

    size: Size
    n: int
    median_hours: float
    p80_hours: float
    weak: bool


@dataclass(frozen=True)
class Estimate:
    """An open item's projected wall hours, read from its size class's
    median -- only produced when that class has at least one measured
    lane."""

    item: str
    size: Size
    median_hours: float
    n: int
    weak: bool


@dataclass(frozen=True)
class ContainerSum:
    """One container's (epic's) estimated remaining hours, summed from its
    open children's `Estimate`s. `n_without_size` counts children that carry
    no size at all -- distinct from a sized child whose class has no
    measured lane yet, which is neither estimated nor counted here."""

    container: str
    hours: float
    n_estimated: int
    n_without_size: int


@dataclass(frozen=True)
class Parallelism:
    """The most lanes observed overlapping in wall-clock time on one
    calendar day."""

    day: date
    overlapping_lanes: int


@dataclass(frozen=True)
class MetricsReport:
    """Everything `measure` computes from one set of lane events and open
    items."""

    measures: tuple[LaneMeasure, ...]
    classes: tuple[SizeClassStats, ...]
    estimates: tuple[Estimate, ...]
    container_sums: tuple[ContainerSum, ...]
    parallelism: tuple[Parallelism, ...]
    incomplete: int


def measure(lanes: Iterable[LaneEvent], open_items: Iterable[OpenItem]) -> MetricsReport:
    """Compute every measure, class, estimate, sum, and parallelism reading
    from the given lane events and open items. Pure and deterministic: the
    same input always yields the same output."""

    lane_list = list(lanes)
    open_item_list = list(open_items)
    completed = [(lane, lane.released_at) for lane in lane_list if lane.released_at is not None]
    incomplete = len(lane_list) - len(completed)

    measures = tuple(_measure_lane(lane, released_at) for lane, released_at in completed)
    classes = _classes_by_size(measures)
    estimates = tuple(
        estimate
        for estimate in (_estimate_item(item, classes) for item in open_item_list)
        if estimate is not None
    )
    container_sums = _container_sums(open_item_list, estimates)
    intervals = tuple((lane.claimed_at, released_at) for lane, released_at in completed)
    parallelism = _parallelism(intervals)

    return MetricsReport(
        measures=measures,
        classes=classes,
        estimates=estimates,
        container_sums=container_sums,
        parallelism=parallelism,
        incomplete=incomplete,
    )


def _hours(start: datetime, end: datetime) -> float:
    return (end - start).total_seconds() / 3600


def _measure_lane(lane: LaneEvent, released_at: datetime) -> LaneMeasure:
    landing_wait = _hours(released_at, lane.landed_at) if lane.landed_at is not None else None
    return LaneMeasure(
        item=lane.item,
        size=lane.size,
        container=lane.container,
        wall_hours=_hours(lane.claimed_at, released_at),
        landing_wait_hours=landing_wait,
    )


def _classes_by_size(measures: Sequence[LaneMeasure]) -> tuple[SizeClassStats, ...]:
    grouped: dict[Size, list[float]] = {}
    for entry in measures:
        if entry.size is None:
            continue
        grouped.setdefault(entry.size, []).append(entry.wall_hours)
    return tuple(_size_class_stats(size, grouped[size]) for size in Size if size in grouped)


def _size_class_stats(size: Size, hours: Sequence[float]) -> SizeClassStats:
    return SizeClassStats(
        size=size,
        n=len(hours),
        median_hours=statistics.median(hours),
        p80_hours=_nearest_rank_percentile(hours, P80_PERCENTILE),
        weak=len(hours) < WEAK_SAMPLE_THRESHOLD,
    )


def _nearest_rank_percentile(values: Sequence[float], percentile: int) -> float:
    """The nearest-rank percentile: the value at ceil(percentile/100 * n),
    1-indexed into the ascending sort. Integer ceiling division keeps this
    exact without importing `math` for the one call site."""

    ordered = sorted(values)
    rank = -(-percentile * len(ordered) // 100)
    return ordered[max(rank, 1) - 1]


def _estimate_item(item: OpenItem, classes: Sequence[SizeClassStats]) -> Estimate | None:
    if item.size is None:
        return None
    stats = next((entry for entry in classes if entry.size == item.size), None)
    if stats is None:
        return None
    return Estimate(
        item=item.item,
        size=item.size,
        median_hours=stats.median_hours,
        n=stats.n,
        weak=stats.weak,
    )


def _container_sums(
    open_items: Sequence[OpenItem], estimates: Sequence[Estimate]
) -> tuple[ContainerSum, ...]:
    estimate_by_item = {estimate.item: estimate for estimate in estimates}
    containers: dict[str, list[OpenItem]] = {}
    for item in open_items:
        if item.container is not None:
            containers.setdefault(item.container, []).append(item)
    return tuple(
        _container_sum(container, members, estimate_by_item)
        for container, members in sorted(containers.items())
    )


def _container_sum(
    container: str,
    members: Sequence[OpenItem],
    estimate_by_item: Mapping[str, Estimate],
) -> ContainerSum:
    estimated = [
        estimate_by_item[member.item] for member in members if member.item in estimate_by_item
    ]
    without_size = [member for member in members if member.size is None]
    return ContainerSum(
        container=container,
        hours=sum(estimate.median_hours for estimate in estimated),
        n_estimated=len(estimated),
        n_without_size=len(without_size),
    )


def _days_touched(start: datetime, end: datetime) -> Iterator[date]:
    current = start.date()
    last = end.date()
    while current <= last:
        yield current
        current += timedelta(days=1)


def _clip_to_day(start: datetime, end: datetime, day: date) -> tuple[datetime, datetime]:
    day_start = datetime.combine(day, time.min, tzinfo=start.tzinfo)
    day_end = day_start + timedelta(days=1)
    return max(start, day_start), min(end, day_end)


def _max_overlap(intervals: Iterable[tuple[datetime, datetime]]) -> int:
    """The most intervals ever simultaneously open, treating a shared
    boundary instant (one ends exactly when another starts) as not
    overlapping: at equal timestamps `-1` sorts before `+1`."""

    boundaries = sorted(
        (point, step) for start, end in intervals for point, step in ((start, 1), (end, -1))
    )
    running = 0
    peak = 0
    for _point, step in boundaries:
        running += step
        peak = max(peak, running)
    return peak


def _clipped_intervals(
    intervals: Sequence[tuple[datetime, datetime]], day: date
) -> list[tuple[datetime, datetime]]:
    return [
        _clip_to_day(start, end, day)
        for start, end in intervals
        if start.date() <= day <= end.date()
    ]


def _parallelism(intervals: Sequence[tuple[datetime, datetime]]) -> tuple[Parallelism, ...]:
    days = sorted({day for start, end in intervals for day in _days_touched(start, end)})
    readings = ((day, _max_overlap(_clipped_intervals(intervals, day))) for day in days)
    return tuple(
        Parallelism(day=day, overlapping_lanes=count) for day, count in readings if count > 0
    )
