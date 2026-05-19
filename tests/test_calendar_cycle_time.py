"""Cycle-time definition: Vacanti's "+1 day", applied to the
floating-point datetime duration.

    CT = (completed_at - created_at) + 1 day

Vacanti's argument for the "+1" (Actionable Agile Metrics for
Predictability, 10th Anniversary Edition, p. 59): when a PBI
starts and finishes on the same day, you would never say it
took zero days to complete. The "+1" exists to reflect that.

We preserve sub-day precision in the duration term — a PR
that ran from Tuesday 12:00 to Wednesday 15:00 (27 hours) is
2.125d, not 2.0d. The minimum legal value is therefore 1.0d
(zero-duration work still counts as a day); same-day work
lands in [1.0, 2.0); cross-midnight work lands in [1.0,
∞) depending on the actual time elapsed, with the +1 day
applied on top of the floating-point duration.

Two earlier interpretations have been retired:
  - "floor at 1.0" (clamps everything below 1.0 to 1.0) —
    the +1 added to the duration already guarantees ≥1 for
    valid data, with no information loss.
  - "calendar-day count, integer" (FD.date() - SD.date() + 1) —
    too coarse; collapses sub-day signal Vacanti's percentile
    work benefits from.

These tests pin the contract.
"""

from __future__ import annotations

from datetime import UTC, datetime

from flowmetrics.materialise import cycle_time_days


class TestCycleTimeDays:
    def test_same_day_short_pr_is_just_over_one_day(self):
        """A 64-minute PR is (64/1440)d + 1d ≈ 1.044d.

        Above the 1.0 minimum (so "we would never say zero
        time") but still close to it (because only 64 minutes
        actually elapsed)."""
        created = datetime(2026, 5, 4, 11, 0, tzinfo=UTC)
        completed = datetime(2026, 5, 4, 12, 4, tzinfo=UTC)
        result = cycle_time_days(created, completed)
        assert result is not None
        # 64 minutes = 0.04444…d ; + 1d = 1.04444…d
        assert abs(result - (1.0 + 64 / 1440)) < 1e-9, (
            f"same-day 64-min PR must be ≈ 1.044d (= 1 + 64/1440); "
            f"got {result}"
        )
        # And explicitly: above the 1.0 floor, below 2.0.
        assert 1.0 < result < 2.0

    def test_two_minute_pr_across_midnight_is_just_over_one_day(self):
        """Calendar boundary crossed but only 2 minutes elapsed.
        Duration is 2/1440 ≈ 0.0014d; + 1d ≈ 1.0014d.

        The +1 doesn't double-count the midnight crossing —
        we're adding *one* day, not "one day per date crossed"."""
        created = datetime(2026, 5, 4, 23, 59, tzinfo=UTC)
        completed = datetime(2026, 5, 5, 0, 1, tzinfo=UTC)
        result = cycle_time_days(created, completed)
        assert result is not None
        assert abs(result - (1.0 + 2 / 1440)) < 1e-9

    def test_zero_duration_is_exactly_one_day(self):
        """A PR that opened and merged at the exact same moment
        is the minimum legal value: 0 + 1 = 1.0d."""
        created = datetime(2026, 5, 4, 11, 0, tzinfo=UTC)
        result = cycle_time_days(created, created)
        assert result == 1.0

    def test_week_long_pr_is_fractional(self):
        """8 days 6 hours of duration → 8.25 + 1 = 9.25d.

        The +1 sits on top of the floating-point duration,
        so even multi-day PRs are fractional whenever the
        sub-day component is non-zero."""
        created = datetime(2026, 4, 26, 9, 0, tzinfo=UTC)
        completed = datetime(2026, 5, 4, 15, 0, tzinfo=UTC)
        result = cycle_time_days(created, completed)
        assert result is not None
        assert abs(result - 9.25) < 1e-9

    def test_in_flight_returns_none(self):
        created = datetime(2026, 5, 4, 9, 0, tzinfo=UTC)
        assert cycle_time_days(created, None) is None

    def test_tuesday_noon_to_wednesday_3pm_is_two_and_an_eighth(self):
        """27 hours = 1.125d duration; + 1d = 2.125d.

        The earlier "1.125d" answer dropped the Vacanti +1;
        the earlier "2.0d" answer threw away the sub-day
        precision. This is the synthesis: keep the precision,
        add the day."""
        # 2026-05-05 is a Tuesday.
        created = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)
        completed = datetime(2026, 5, 6, 15, 0, tzinfo=UTC)
        result = cycle_time_days(created, completed)
        assert result is not None
        assert abs(result - 2.125) < 1e-9

    def test_completion_before_creation_surfaces_bad_data(self):
        """If a source-data bug delivers completed < created, the
        +1 still gets applied, but the negative duration term
        produces a value < 1.0 — which is impossible for valid
        data (valid minimum is exactly 1.0). Anything below 1.0
        is the "bad data" zone."""
        created = datetime(2026, 5, 6, 12, 0, tzinfo=UTC)
        completed = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)
        result = cycle_time_days(created, completed)
        assert result is not None
        # -1d + 1d = 0.0
        assert result < 1.0, (
            f"completion before creation should land in the "
            f"sub-1.0 'impossible' zone so the bad data surfaces; "
            f"got {result}"
        )
