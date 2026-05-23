"""Tests for converting PR records into daily throughput samples."""

from datetime import UTC, date, datetime

from flowmetrics.compute import WorkItem
from flowmetrics.throughput import daily_counts, daily_throughput


def pr(number: int, completed_at: datetime) -> WorkItem:
    return WorkItem(
        item_id=f"#{number}",
        title=f"PR {number}",
        created_at=completed_at,  # not used by daily_throughput
        completed_at=completed_at,
        activity=[],
    )


def dt(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


class TestDailyThroughput:
    def test_counts_per_day_including_zero_days(self):
        prs = [
            pr(1, dt(2026, 5, 4, 10, 0)),
            pr(2, dt(2026, 5, 4, 15, 0)),
            pr(3, dt(2026, 5, 6, 9, 0)),
        ]
        samples = daily_throughput(prs, date(2026, 5, 4), date(2026, 5, 7))
        # Mon=2, Tue=0, Wed=1, Thu=0
        assert samples == [2, 0, 1, 0]

    def test_empty_window_returns_zeros(self):
        samples = daily_throughput([], date(2026, 5, 4), date(2026, 5, 10))
        assert samples == [0] * 7

    def test_prs_outside_window_are_ignored(self):
        prs = [
            pr(1, dt(2026, 5, 3, 23, 0)),  # day before window
            pr(2, dt(2026, 5, 5, 10, 0)),  # inside
            pr(3, dt(2026, 5, 11, 1, 0)),  # day after window
        ]
        samples = daily_throughput(prs, date(2026, 5, 4), date(2026, 5, 10))
        assert sum(samples) == 1
        # Day index 1 (Tue 5/5) should have the one merge
        assert samples[1] == 1

    def test_single_day_window(self):
        prs = [pr(1, dt(2026, 5, 4, 10, 0))]
        assert daily_throughput(prs, date(2026, 5, 4), date(2026, 5, 4)) == [1]

    def test_unmerged_prs_skipped(self):
        unmerged = WorkItem(
            item_id="#99",
            title="open",
            created_at=dt(2026, 5, 4, 9, 0),
            completed_at=None,
            activity=[],
        )
        samples = daily_throughput(
            [unmerged, pr(1, dt(2026, 5, 4, 10, 0))],
            date(2026, 5, 4),
            date(2026, 5, 5),
        )
        assert samples == [1, 0]


class TestDailyCounts:
    """The shared zero-filled-daily-counts primitive both the CLI
    `daily_throughput` and the web chart-model layer call into.
    Pure: dates in, counts out — no `WorkItem`, no warehouse."""

    def test_counts_per_day_in_inclusive_window(self):
        dates = [date(2026, 5, 4), date(2026, 5, 4), date(2026, 5, 6)]
        assert daily_counts(dates, date(2026, 5, 4), date(2026, 5, 7)) == [2, 0, 1, 0]

    def test_dates_outside_window_are_ignored(self):
        dates = [date(2026, 5, 3), date(2026, 5, 5), date(2026, 5, 11)]
        assert daily_counts(dates, date(2026, 5, 4), date(2026, 5, 10)) == [
            0, 1, 0, 0, 0, 0, 0,
        ]

    def test_empty_dates_yields_all_zeros(self):
        assert daily_counts([], date(2026, 5, 4), date(2026, 5, 10)) == [0] * 7

    def test_single_day_window(self):
        assert daily_counts([date(2026, 5, 4)], date(2026, 5, 4), date(2026, 5, 4)) == [1]

    def test_stop_before_start_raises(self):
        import pytest
        with pytest.raises(ValueError):
            daily_counts([], date(2026, 5, 10), date(2026, 5, 4))
