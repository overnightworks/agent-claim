from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import pytest

from agent_coordination.metrics import (
    ContainerSum,
    Estimate,
    LaneEvent,
    OpenItem,
    Parallelism,
    Size,
    SizeClassStats,
    measure,
)

BASE = datetime(2026, 1, 1, tzinfo=UTC)


def _lane(
    item: str,
    claimed_at: datetime,
    released_at: datetime | None,
    *,
    size: Size | None = None,
    container: str | None = None,
    landed_at: datetime | None = None,
) -> LaneEvent:
    """One `LaneEvent`, every field defaulted to the value a proof that does
    not care about it can ignore."""
    return LaneEvent(
        item=item,
        size=size,
        container=container,
        claimed_at=claimed_at,
        released_at=released_at,
        landed_at=landed_at,
        rescopes=0,
    )


def _completed_lane(item: str, size: Size, hours: float, container: str | None = None) -> LaneEvent:
    """A lane claimed at `BASE` and released `hours` later -- the one shape
    the size-class and estimate proofs need repeated with different data."""
    return _lane(item, BASE, BASE + timedelta(hours=hours), size=size, container=container)


# Three S-lanes and two M-lanes (issue #299 proof 1), shared by the
# size-class and estimate proofs so both read the same measured world.
_THREE_S_TWO_M_LANES = [
    _completed_lane("s1", Size.SMALL, 1.0),
    _completed_lane("s2", Size.SMALL, 2.0),
    _completed_lane("s3", Size.SMALL, 4.0),
    _completed_lane("m1", Size.MEDIUM, 3.0),
    _completed_lane("m2", Size.MEDIUM, 5.0),
]


@pytest.mark.parametrize(
    ("size", "hours", "expected"),
    [
        (
            Size.SMALL,
            (1.0, 2.0, 4.0),
            SizeClassStats(Size.SMALL, n=3, median_hours=2.0, p80_hours=4.0, weak=False),
        ),
        (
            Size.MEDIUM,
            (3.0, 5.0),
            SizeClassStats(Size.MEDIUM, n=2, median_hours=4.0, p80_hours=5.0, weak=True),
        ),
        (Size.LARGE, (), None),
    ],
)
def test_size_class_stats_from_measured_lanes(
    size: Size, hours: tuple[float, ...], expected: SizeClassStats | None
) -> None:
    """n, median, and nearest-rank p80 per size class; a class with no
    measured lane (L here) produces no `SizeClassStats` at all."""
    lanes = [_completed_lane(f"{size}-{index}", size, hour) for index, hour in enumerate(hours)]
    report = measure(lanes, open_items=())
    matching = next((entry for entry in report.classes if entry.size is size), None)
    assert matching == expected


@pytest.mark.parametrize(
    ("open_item", "expected"),
    [
        (
            OpenItem("open-s", Size.SMALL, None),
            Estimate("open-s", Size.SMALL, median_hours=2.0, n=3, weak=False),
        ),
        (
            OpenItem("open-m", Size.MEDIUM, None),
            Estimate("open-m", Size.MEDIUM, median_hours=4.0, n=2, weak=True),
        ),
        (OpenItem("open-none", None, None), None),
        (OpenItem("open-l", Size.LARGE, None), None),
    ],
)
def test_estimate_only_for_sized_items_with_a_measured_class(
    open_item: OpenItem, expected: Estimate | None
) -> None:
    """An open item gets an estimate only when it carries a size AND that
    size's class has at least one measured lane; `weak` follows the class's
    own `n < 3`. No size, or a size with no measured class (L here), yields
    no estimate -- counted elsewhere, never guessed."""
    report = measure(_THREE_S_TWO_M_LANES, open_items=(open_item,))
    matching = next((entry for entry in report.estimates if entry.item == open_item.item), None)
    assert matching == expected


@pytest.mark.parametrize(
    ("released_at", "landed_at", "expected_incomplete", "expected_wait"),
    [
        (BASE + timedelta(hours=2), None, 0, None),
        (BASE + timedelta(hours=2), BASE + timedelta(hours=5), 0, 3.0),
        (None, None, 1, None),
    ],
)
def test_completion_gates_measurement_and_landing_wait(
    released_at: datetime | None,
    landed_at: datetime | None,
    expected_incomplete: int,
    expected_wait: float | None,
) -> None:
    """A lane without a release is counted in `incomplete`, never measured
    and never sorted into a class; a released lane without a landing reports
    `landing_wait_hours=None` rather than guessing one."""
    lane = _lane("x", BASE, released_at, size=Size.SMALL, landed_at=landed_at)
    report = measure([lane], open_items=())
    assert report.incomplete == expected_incomplete
    assert len(report.measures) == (1 - expected_incomplete)
    if report.measures:
        assert report.measures[0].landing_wait_hours == expected_wait


@pytest.mark.parametrize(
    ("lanes", "open_items", "expected_container_sums", "expected_parallelism"),
    [
        (
            [_completed_lane("s1", Size.SMALL, 2.0)],
            (
                OpenItem("i1", Size.SMALL, "epic-1"),
                OpenItem("i2", None, "epic-1"),
                OpenItem("i3", Size.SMALL, "epic-2"),
            ),
            (
                ContainerSum("epic-1", hours=2.0, n_estimated=1, n_without_size=1),
                ContainerSum("epic-2", hours=2.0, n_estimated=1, n_without_size=0),
            ),
            (Parallelism(date(2026, 1, 1), 1),),
        ),
        (
            [
                _lane("a", BASE.replace(hour=9), BASE.replace(hour=11)),
                _lane("b", BASE.replace(hour=10), BASE.replace(hour=12)),
                _lane("c", BASE.replace(day=2, hour=9), BASE.replace(day=2, hour=10)),
                _lane("d", BASE.replace(day=3, hour=9), BASE.replace(day=3, hour=10)),
                _lane("e", BASE.replace(day=4, hour=9), BASE.replace(day=4, hour=10)),
            ],
            (),
            (),
            (
                Parallelism(date(2026, 1, 1), 2),
                Parallelism(date(2026, 1, 2), 1),
                Parallelism(date(2026, 1, 3), 1),
                Parallelism(date(2026, 1, 4), 1),
            ),
        ),
    ],
)
def test_container_sums_and_daily_parallelism(
    lanes: list[LaneEvent],
    open_items: tuple[OpenItem, ...],
    expected_container_sums: tuple[ContainerSum, ...],
    expected_parallelism: tuple[Parallelism, ...],
) -> None:
    """Container sums add up their open children's estimates and separately
    count children without a size; two lanes overlapping in wall-clock time
    on one day read `overlapping_lanes=2` for that day, three lanes on three
    disjoint days each read `1`."""
    report = measure(lanes, open_items)
    assert report.container_sums == expected_container_sums
    assert report.parallelism == expected_parallelism


@pytest.mark.parametrize(
    "build_lanes",
    [
        lambda: [_completed_lane("s1", Size.SMALL, 2.0)],
        lambda: [
            _completed_lane("m1", Size.MEDIUM, 4.0),
            _lane("stuck", BASE, None, size=Size.LARGE),
        ],
    ],
)
def test_measure_is_deterministic_for_the_same_input(
    build_lanes: Callable[[], list[LaneEvent]],
) -> None:
    """No randomness and no clock inside the module: computing twice from
    freshly built, equal input yields an equal `MetricsReport`."""
    open_items = (OpenItem("open-s", Size.SMALL, "epic-1"),)
    first = measure(build_lanes(), open_items)
    second = measure(build_lanes(), open_items)
    assert first == second
