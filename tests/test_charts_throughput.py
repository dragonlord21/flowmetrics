"""Layer 2 (chart model) — tests for `flowmetrics.charts.throughput`.

`build_throughput_model` is pure: completed-item rows + a view
window in, a fully-resolved `ThroughputModel` out. Decisions —
window enumeration, weekday/weekend classification, warehouse vs.
missing coverage, the headline math — are asserted here.
"""

from __future__ import annotations

from datetime import date, datetime

from flowmetrics.charts.throughput import build_throughput_model
from flowmetrics.warehouse.queries import CompletedItem
from flowmetrics.windows import Window


def _completed(n: int, completed: date) -> CompletedItem:
    return CompletedItem(
        item_id=f"#{n}", title=f"item {n}", url=None,
        completed_at=datetime(completed.year, completed.month, completed.day, 12),
        cycle_time_days=3.0,
    )


class TestShape:
    def test_empty_for_no_items(self):
        assert build_throughput_model([]).is_empty

    def test_one_row_per_calendar_date_in_the_window(self):
        items = [_completed(1, date(2026, 1, 1)), _completed(2, date(2026, 1, 3))]
        m = build_throughput_model(items)
        # data span Jan 1..3 → 3 days, zero-completion day Jan 2.
        assert [d.date_iso for d in m.daily] == [
            "2026-01-01", "2026-01-02", "2026-01-03",
        ]
        assert [d.count for d in m.daily] == [1, 0, 1]

    def test_counts_completions_per_date(self):
        items = [
            _completed(1, date(2026, 1, 1)),
            _completed(2, date(2026, 1, 1)),
            _completed(3, date(2026, 1, 2)),
        ]
        m = build_throughput_model(items)
        by_iso = {d.date_iso: d.count for d in m.daily}
        assert by_iso == {"2026-01-01": 2, "2026-01-02": 1}


class TestWindowing:
    def test_view_window_clamps_the_chart(self):
        items = [_completed(1, date(2026, 1, 5)), _completed(2, date(2026, 6, 5))]
        m = build_throughput_model(
            items,
            view=Window(from_=date(2026, 1, 1), to=date(2026, 1, 31)),
        )
        # 31 days enumerated; only Jan 5 has a completion.
        assert len(m.daily) == 31
        nonzero = [d for d in m.daily if d.count > 0]
        assert [d.date_iso for d in nonzero] == ["2026-01-05"]

    def test_view_outside_data_yields_empty(self):
        items = [_completed(1, date(2026, 1, 1))]
        m = build_throughput_model(
            items,
            view=Window(from_=date(2027, 1, 1), to=date(2027, 12, 31)),
        )
        assert m.is_empty


class TestDayClassification:
    def test_weekdays_and_weekends_tagged(self):
        # 2026-01-03 is a Saturday, 2026-01-05 is a Monday.
        items = [_completed(1, date(2026, 1, 3)), _completed(2, date(2026, 1, 5))]
        m = build_throughput_model(items)
        by_iso = {d.date_iso: d.day_type for d in m.daily}
        assert by_iso["2026-01-03"] == "weekend"
        assert by_iso["2026-01-05"] == "weekday"


class TestCoverage:
    def test_days_inside_completion_span_are_warehouse(self):
        items = [
            _completed(1, date(2026, 1, 5)),
            _completed(2, date(2026, 1, 10)),
        ]
        m = build_throughput_model(items)
        assert all(d.data_coverage == "warehouse" for d in m.daily)

    def test_days_outside_completion_span_are_missing(self):
        """A view extending past the warehouse's data tags the
        extra days as 'missing' — a gap, not a real zero."""
        items = [_completed(1, date(2026, 1, 10))]
        m = build_throughput_model(
            items,
            view=Window(from_=date(2026, 1, 1), to=date(2026, 1, 20)),
        )
        by_iso = {d.date_iso: d.data_coverage for d in m.daily}
        assert by_iso["2026-01-01"] == "missing"
        assert by_iso["2026-01-10"] == "warehouse"
        assert by_iso["2026-01-20"] == "missing"


class TestHeadline:
    def test_full_coverage_headline_states_total_and_rate(self):
        items = [
            _completed(1, date(2026, 1, 1)),
            _completed(2, date(2026, 1, 2)),
        ]
        m = build_throughput_model(items)
        # 2 items over 2 days · 1.0/day
        assert "2 items" in m.headline
        assert "2 days" in m.headline
        assert "1.0/day" in m.headline

    def test_gappy_window_headline_names_both_numbers(self):
        items = [_completed(1, date(2026, 1, 10))]
        m = build_throughput_model(
            items,
            view=Window(from_=date(2026, 1, 1), to=date(2026, 1, 20)),
        )
        # 1 covered day; 20-day window → both numbers in headline.
        assert "with data" in m.headline
        assert "20-day window" in m.headline

    def test_empty_window_says_so(self):
        items = [_completed(1, date(2026, 1, 1))]
        m = build_throughput_model(
            items,
            view=Window(from_=date(2027, 1, 1), to=date(2027, 12, 31)),
        )
        assert "No completed items" in m.headline
