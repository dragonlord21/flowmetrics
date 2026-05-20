"""Tests for `flowmetrics.windows` — the (from, to) date-range
types that drive the dashboard's two user-facing windows:

  - **View window**: clamps chart x-axes. Display-only.
  - **Reference period**: the statistical sample. Drives
    percentile thresholds (cycle-time, aging) and the MCS
    throughput sampling distribution (forecast).

Both windows are inclusive on both endpoints. The UI labels
match: "From" and "To" both inclusive — same-day = 1-day window,
not 0.

Defaults are anchored to today UTC:
  - View window: 30 days inclusive (today − 29 .. today)
  - Reference period: 14 days inclusive (today − 13 .. today)
"""

from __future__ import annotations

from datetime import date

import pytest

from flowmetrics.windows import (
    DEFAULT_REFERENCE_DAYS,
    DEFAULT_VIEW_DAYS,
    Window,
    parse_windows,
)


class TestWindow:
    def test_from_and_to_are_inclusive(self):
        w = Window(from_=date(2026, 5, 4), to=date(2026, 5, 10))
        # 7-day window (May 4..10 inclusive)
        assert w.days_inclusive == 7

    def test_same_day_is_one_day_inclusive(self):
        w = Window(from_=date(2026, 5, 4), to=date(2026, 5, 4))
        assert w.days_inclusive == 1, (
            "single-day window must report 1 day (inclusive endpoints)"
        )

    def test_last_n_days_anchors_at_today(self):
        today = date(2026, 5, 20)
        w = Window.last_n_days(30, today=today)
        assert w.to == today
        assert w.from_ == date(2026, 4, 21)  # 30 days inclusive
        assert w.days_inclusive == 30

    def test_last_n_days_with_1_day_is_today_only(self):
        today = date(2026, 5, 20)
        w = Window.last_n_days(1, today=today)
        assert w.from_ == today
        assert w.to == today
        assert w.days_inclusive == 1


class TestParseWindows:
    def test_defaults_when_no_query_params(self):
        today = date(2026, 5, 20)
        view, ref = parse_windows({}, today=today)
        # View defaults to 30 days inclusive ending today.
        assert view.to == today
        assert view.days_inclusive == DEFAULT_VIEW_DAYS
        # Reference defaults to 14 days inclusive ending today.
        assert ref.to == today
        assert ref.days_inclusive == DEFAULT_REFERENCE_DAYS

    def test_view_from_and_to_override_defaults(self):
        today = date(2026, 5, 20)
        view, _ref = parse_windows(
            {"view_from": "2025-01-01", "view_to": "2025-01-31"},
            today=today,
        )
        assert view.from_ == date(2025, 1, 1)
        assert view.to == date(2025, 1, 31)

    def test_ref_from_and_to_override_defaults(self):
        today = date(2026, 5, 20)
        _view, ref = parse_windows(
            {"ref_from": "2025-04-01", "ref_to": "2025-04-07"},
            today=today,
        )
        assert ref.from_ == date(2025, 4, 1)
        assert ref.to == date(2025, 4, 7)

    def test_view_and_ref_are_independent(self):
        today = date(2026, 5, 20)
        view, ref = parse_windows(
            {
                "view_from": "2025-01-01", "view_to": "2025-01-31",
                "ref_from": "2025-02-01", "ref_to": "2025-02-07",
            },
            today=today,
        )
        assert view.from_ == date(2025, 1, 1)
        assert view.to == date(2025, 1, 31)
        assert ref.from_ == date(2025, 2, 1)
        assert ref.to == date(2025, 2, 7)

    def test_only_one_endpoint_falls_back_to_default(self):
        """Partial input (only from, only to) is treated as no
        input. Avoids surprising user with a half-window."""
        today = date(2026, 5, 20)
        view, _ref = parse_windows(
            {"view_from": "2025-01-01"},  # no view_to
            today=today,
        )
        assert view.days_inclusive == DEFAULT_VIEW_DAYS
        assert view.to == today

    def test_invalid_date_falls_back_to_default(self):
        """A malformed date doesn't crash the request — it just
        means the operator typed something wrong and gets the
        default back. UI can re-validate visibly."""
        today = date(2026, 5, 20)
        view, _ref = parse_windows(
            {"view_from": "not-a-date", "view_to": "2025-01-31"},
            today=today,
        )
        assert view.days_inclusive == DEFAULT_VIEW_DAYS

    def test_default_constants_are_reasonable(self):
        """Pin the documented defaults so a future widening
        doesn't go unnoticed."""
        assert DEFAULT_VIEW_DAYS == 30
        assert DEFAULT_REFERENCE_DAYS == 14
