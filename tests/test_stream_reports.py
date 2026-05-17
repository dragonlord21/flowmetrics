"""Behavioural spec for Stream-native report consumers.

All four remaining Vacanti reports - CFD, Scatterplot, Flow
Efficiency, Forecast (throughput sampling) - implemented against
the canonical Stream as a parallel surface to the legacy
WorkItem-based modules. Same numeric outputs; different input
shape.

These tests don't replace the legacy module tests - they prove
the canonical stream carries enough information to reproduce
every metric.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from flowmetrics.canonical import StageTransition, WorkflowDef
from flowmetrics.stream import Stream, StreamItem
from flowmetrics.stream_reports import (
    cfd_daily_counts,
    flow_efficiency_per_item,
    scatterplot_points,
    throughput_per_day,
)


def _ts(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


def _three_item_stream() -> Stream:
    """Toy: three items moving Open→In Progress→Done at staggered times.

    item:1  Open d1, In Progress d2, Done d4   (cycle = 3 days)
    item:2  Open d2, In Progress d3, Done d6   (cycle = 4 days)
    item:3  Open d3, In Progress d4            (in-flight as of d5)
    """
    wf = WorkflowDef(
        stages=("Open", "In Progress", "Done"),
        wip_set=frozenset({"In Progress"}),
    )
    items = [
        StreamItem("i:1", "first",  "https://e/1",
                   _ts(2026, 5, 1), _ts(2026, 5, 4)),
        StreamItem("i:2", "second", "https://e/2",
                   _ts(2026, 5, 2), _ts(2026, 5, 6)),
        StreamItem("i:3", "third",  None,
                   _ts(2026, 5, 3), None),
    ]
    txs = [
        StageTransition("i:1", _ts(2026, 5, 1), "Open",         "s"),
        StageTransition("i:1", _ts(2026, 5, 2), "In Progress",  "s"),
        StageTransition("i:1", _ts(2026, 5, 4), "Done",         "s"),
        StageTransition("i:2", _ts(2026, 5, 2), "Open",         "s"),
        StageTransition("i:2", _ts(2026, 5, 3), "In Progress",  "s"),
        StageTransition("i:2", _ts(2026, 5, 6), "Done",         "s"),
        StageTransition("i:3", _ts(2026, 5, 3), "Open",         "s"),
        StageTransition("i:3", _ts(2026, 5, 4), "In Progress",  "s"),
    ]
    return Stream(items=items, transitions=txs, workflow=wf)


class TestCfdDailyCounts:
    def test_daily_counts_per_stage_match_expected_evolution(self):
        s = _three_item_stream()
        rows = cfd_daily_counts(s, start=date(2026, 5, 1), stop=date(2026, 5, 6))
        # Returns a list of dicts {date, stage, count} OR a nested dict; let's
        # canonicalize on dict[date, dict[stage, count]] for assertions.
        by_date = {r["date"]: r["counts"] for r in rows}
        # May 1: i:1 in Open; nothing else exists.
        assert by_date[date(2026, 5, 1)] == {"Open": 1, "In Progress": 0, "Done": 0}
        # May 4: i:1 Done; i:2 In Progress; i:3 In Progress.
        assert by_date[date(2026, 5, 4)] == {"Open": 0, "In Progress": 2, "Done": 1}
        # May 6: i:1 Done, i:2 Done, i:3 In Progress.
        assert by_date[date(2026, 5, 6)] == {"Open": 0, "In Progress": 1, "Done": 2}

    def test_one_row_per_day_in_inclusive_window(self):
        s = _three_item_stream()
        rows = cfd_daily_counts(s, start=date(2026, 5, 1), stop=date(2026, 5, 3))
        assert [r["date"] for r in rows] == [date(2026, 5, 1), date(2026, 5, 2), date(2026, 5, 3)]


class TestScatterplotPoints:
    def test_one_point_per_completed_item(self):
        s = _three_item_stream()
        points = scatterplot_points(s)
        # i:1 and i:2 completed; i:3 in-flight.
        assert {p.item_id for p in points} == {"i:1", "i:2"}

    def test_cycle_time_in_days_is_calendar_difference(self):
        s = _three_item_stream()
        points = {p.item_id: p for p in scatterplot_points(s)}
        # i:1 created May 1, completed May 4 → 3 days.
        assert points["i:1"].cycle_time_days == 3.0
        assert points["i:2"].cycle_time_days == 4.0

    def test_completed_at_and_url_flow_through(self):
        s = _three_item_stream()
        p = next(p for p in scatterplot_points(s) if p.item_id == "i:1")
        assert p.completed_at == _ts(2026, 5, 4)
        assert p.url == "https://e/1"


class TestThroughputPerDay:
    def test_counts_completion_dates_inclusive(self):
        s = _three_item_stream()
        rows = throughput_per_day(s, start=date(2026, 5, 1), stop=date(2026, 5, 6))
        by_date = {r["date"]: r["completed"] for r in rows}
        assert by_date[date(2026, 5, 4)] == 1   # i:1
        assert by_date[date(2026, 5, 6)] == 1   # i:2
        assert by_date[date(2026, 5, 1)] == 0
        assert by_date[date(2026, 5, 5)] == 0


class TestFlowEfficiencyPerItem:
    def test_efficiency_is_wip_time_over_cycle_time(self):
        s = _three_item_stream()
        rows = {r.item_id: r for r in flow_efficiency_per_item(s)}
        # i:1: WIP set = {In Progress}. In Progress from d2→d4 = 2 days.
        # Cycle = 3 days. Efficiency = 2/3.
        i1 = rows["i:1"]
        assert i1.active_time == timedelta(days=2)
        assert i1.cycle_time == timedelta(days=3)
        assert abs(i1.efficiency - (2 / 3)) < 1e-9
        # i:2: WIP In Progress d3→d6 = 3 days. Cycle = 4 days.
        i2 = rows["i:2"]
        assert i2.active_time == timedelta(days=3)
        assert i2.cycle_time == timedelta(days=4)
        assert abs(i2.efficiency - 0.75) < 1e-9

    def test_excludes_in_flight_items(self):
        s = _three_item_stream()
        rows = list(flow_efficiency_per_item(s))
        assert all(r.item_id != "i:3" for r in rows)
