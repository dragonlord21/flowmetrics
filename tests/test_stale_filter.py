"""Behavioural spec for the stale-item filter.

OSS pipelines accumulate hundreds of in-flight items that have
zero recent activity — external PRs no maintainer touched,
issues filed years ago and forgotten. They aren't part of the
team's real flow; including them dominates the CFD's leading
band and drags every percentile into the deep tail.

`filter_stale(items, asof, days)` drops items whose most recent
event is more than `days` before `asof`. "Most recent event"
considers:
  - created_at (always present)
  - completed_at (when set)
  - the max activity timestamp
  - the max status_intervals interval.end

Items with `days=None` are returned unchanged (filter disabled).
"""

from __future__ import annotations

from datetime import UTC, date, datetime

from flowmetrics.compute import StatusInterval, WorkItem
from flowmetrics.stale import filter_stale


def _ts(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


class TestFilterStale:
    def test_disabled_when_days_is_none(self):
        item = WorkItem(
            item_id="#1", title="t",
            created_at=_ts(2020, 1, 1),
            completed_at=None,
        )
        assert filter_stale([item], asof=date(2026, 5, 17), days=None) == [item]

    def test_drops_item_with_only_old_created_at(self):
        item = WorkItem(
            item_id="#1", title="t",
            created_at=_ts(2024, 1, 1),
            completed_at=None,
        )
        out = filter_stale([item], asof=date(2026, 5, 17), days=14)
        assert out == []

    def test_keeps_item_with_recent_created_at(self):
        item = WorkItem(
            item_id="#1", title="t",
            created_at=_ts(2026, 5, 10),  # 7 days ago
            completed_at=None,
        )
        out = filter_stale([item], asof=date(2026, 5, 17), days=14)
        assert out == [item]

    def test_recent_activity_keeps_old_created_at_alive(self):
        """An old PR with a recent comment/review is NOT stale."""
        item = WorkItem(
            item_id="#1", title="t",
            created_at=_ts(2024, 1, 1),
            completed_at=None,
            activity=[_ts(2024, 1, 1), _ts(2026, 5, 12)],  # 5 days ago
        )
        out = filter_stale([item], asof=date(2026, 5, 17), days=14)
        assert out == [item]

    def test_completed_recently_counts_as_active(self):
        """A merged PR with old creation but recent merge is active."""
        item = WorkItem(
            item_id="#1", title="t",
            created_at=_ts(2024, 1, 1),
            completed_at=_ts(2026, 5, 15),  # 2 days ago
        )
        out = filter_stale([item], asof=date(2026, 5, 17), days=14)
        assert out == [item]

    def test_recent_status_transition_counts_as_activity(self):
        """A new interval's start is a real state transition (label
        added, status changed) — counts as recent activity even when
        no `activity` timestamps exist.

        Important: the LAST interval.end is deliberately NOT counted,
        because in-flight sources synthesize that boundary to extend
        to `asof` — otherwise every fetched in-flight item would look
        active today. Only interval START timestamps reflect real
        events."""
        item = WorkItem(
            item_id="I#1", title="t",
            created_at=_ts(2024, 1, 1),
            completed_at=None,
            status_intervals=[
                StatusInterval(_ts(2024, 1, 1), _ts(2024, 6, 1), "Open"),
                # The "in-progress" transition starts May 13 — within
                # the 14-day window. Even though this is the LAST
                # interval (so its .end might be synthesized to asof),
                # the .start is real.
                StatusInterval(_ts(2026, 5, 13), _ts(2026, 5, 17), "in-progress"),
            ],
        )
        out = filter_stale([item], asof=date(2026, 5, 17), days=14)
        assert out == [item]

    def test_synthetic_last_interval_end_does_not_keep_stale_item_alive(self):
        """In-flight sources extend status_intervals[-1].end to asof.
        That synthetic timestamp must NOT be counted as recent
        activity — otherwise every fetched in-flight item would
        survive any stale filter regardless of real activity."""
        item = WorkItem(
            item_id="#stale-pr", title="abandoned",
            created_at=_ts(2024, 1, 1),
            completed_at=None,
            activity=[],  # no real activity events
            status_intervals=[
                # One interval whose end is the asof end-of-day (the
                # synthetic boundary).
                StatusInterval(_ts(2024, 1, 1), _ts(2026, 5, 17, 23, 59, 59), "Awaiting Review"),
            ],
        )
        out = filter_stale([item], asof=date(2026, 5, 17), days=14)
        assert out == [], (
            "single-interval item with no real activity must be filtered"
        )

    def test_filters_mixed_population_keeping_only_recent(self):
        recent = WorkItem(
            item_id="#new", title="recent",
            created_at=_ts(2026, 5, 14), completed_at=None,
        )
        stale = WorkItem(
            item_id="#old", title="stale",
            created_at=_ts(2020, 1, 1), completed_at=None,
        )
        out = filter_stale([recent, stale], asof=date(2026, 5, 17), days=14)
        assert out == [recent]

    def test_boundary_kept_when_within_threshold(self):
        """An item that's been silent for 6 days survives a 7-day
        threshold. (Just inside the cutoff.)"""
        item = WorkItem(
            item_id="#boundary", title="t",
            created_at=_ts(2026, 5, 11, 12, 0, 0),  # 6 days before asof
            completed_at=None,
        )
        assert filter_stale([item], asof=date(2026, 5, 17), days=7) == [item]

    def test_just_outside_threshold_dropped(self):
        """8 days silent > 7-day threshold → dropped."""
        item = WorkItem(
            item_id="#stale", title="t",
            created_at=_ts(2026, 5, 9, 12, 0, 0),  # 8 days before asof
            completed_at=None,
        )
        assert filter_stale([item], asof=date(2026, 5, 17), days=7) == []
