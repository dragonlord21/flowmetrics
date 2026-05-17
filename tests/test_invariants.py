"""Canonical-data invariants.

These tests codify what "valid" data looks like at the schema
level — independent of any chart or metric. If any of these fire,
data downstream is suspect.

Goals:
1. Test the validators themselves (positive + negative cases).
2. Run validators against tests/fixtures/canonical/*.json so
   regressions in the fixtures get caught.

A separate live-data smoke test (`test_invariants_live.py`,
marked `integration`) will run them against actual cached
GitHub/Jira fetches.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from flowmetrics.compute import (
    FlowEfficiency,
    StatusInterval,
    WorkItem,
)
from flowmetrics.invariants import (
    InvariantViolation,
    validate_flow_efficiency,
    validate_status_intervals,
    validate_work_item,
)


def _ts(*args) -> datetime:
    return datetime(*args, tzinfo=UTC)


# ---------------------------------------------------------------------------
# WorkItem invariants
# ---------------------------------------------------------------------------


class TestWorkItemInvariants:
    def test_completed_before_created_is_invalid(self):
        item = WorkItem(
            item_id="#1", title="t",
            created_at=_ts(2026, 5, 10),
            completed_at=_ts(2026, 5, 5),  # before created!
        )
        violations = validate_work_item(item)
        assert any("completed_at < created_at" in str(v) for v in violations), violations

    def test_completed_equals_created_is_valid(self):
        """Same-instant created+merged is rare but legal (e.g. dependabot
        PR auto-merged in <1ms)."""
        item = WorkItem(
            item_id="#1", title="t",
            created_at=_ts(2026, 5, 5),
            completed_at=_ts(2026, 5, 5),
        )
        assert validate_work_item(item) == []

    def test_activity_within_tolerance_is_valid(self):
        """Real-world: GitHub PR commits have committedDate predating
        the PR's createdAt (developer wrote locally, pushed later).
        Tolerated up to 30 days; metric layer drops out-of-window
        events anyway."""
        item = WorkItem(
            item_id="#1", title="t",
            created_at=_ts(2026, 5, 5),
            completed_at=_ts(2026, 5, 10),
            activity=[
                _ts(2026, 5, 6),
                _ts(2026, 5, 1),  # 4 days before created — within 30d tolerance
                _ts(2026, 5, 10, 0, 30),  # 30 min after completed — within 1h tolerance
            ],
        )
        assert validate_work_item(item) == []

    def test_activity_wildly_out_of_range_invalid(self):
        """Activity 6 months pre-creation is genuine corruption, not
        pre-PR commit drift."""
        item = WorkItem(
            item_id="#1", title="t",
            created_at=_ts(2026, 5, 5),
            completed_at=_ts(2026, 5, 10),
            activity=[_ts(2025, 11, 1)],  # ~6 months before
        )
        violations = validate_work_item(item)
        assert any("30d" in str(v) for v in violations), violations

    def test_activity_after_merge_is_acceptable(self):
        """Post-merge comments/cross-references are a normal real-world
        event. The metric layer filters them when clustering for active
        time. They're NOT a data corruption signal."""
        item = WorkItem(
            item_id="#1", title="t",
            created_at=_ts(2026, 5, 5),
            completed_at=_ts(2026, 5, 10),
            activity=[_ts(2026, 5, 15)],  # 5 days post-merge — fine
        )
        assert validate_work_item(item) == []

    def test_in_flight_item_activity_in_future_ok_if_no_completed_at(self):
        """Without a completed_at, activity timestamps aren't bounded
        above by anything — only created_at is the lower bound."""
        item = WorkItem(
            item_id="#1", title="t",
            created_at=_ts(2026, 5, 5),
            completed_at=None,
            activity=[_ts(2026, 5, 6), _ts(2026, 5, 12)],
        )
        assert validate_work_item(item) == []


class TestStatusIntervalInvariants:
    def test_interval_with_negative_duration_is_invalid(self):
        iv = StatusInterval(_ts(2026, 5, 10), _ts(2026, 5, 5), "Open")
        violations = validate_status_intervals([iv])
        assert any("interval.end < interval.start" in str(v) for v in violations), violations

    def test_chronologically_ordered_contiguous_is_valid(self):
        intervals = [
            StatusInterval(_ts(2026, 5, 1), _ts(2026, 5, 3), "Open"),
            StatusInterval(_ts(2026, 5, 3), _ts(2026, 5, 6), "Review"),
            StatusInterval(_ts(2026, 5, 6), _ts(2026, 5, 10), "Done"),
        ]
        assert validate_status_intervals(intervals) == []

    def test_overlapping_intervals_invalid(self):
        # Interval 2 starts BEFORE interval 1 ends.
        intervals = [
            StatusInterval(_ts(2026, 5, 1), _ts(2026, 5, 5), "Open"),
            StatusInterval(_ts(2026, 5, 3), _ts(2026, 5, 7), "Review"),
        ]
        violations = validate_status_intervals(intervals)
        assert any("overlap" in str(v).lower() for v in violations), violations

    def test_out_of_order_intervals_invalid(self):
        intervals = [
            StatusInterval(_ts(2026, 5, 5), _ts(2026, 5, 7), "Review"),
            StatusInterval(_ts(2026, 5, 1), _ts(2026, 5, 3), "Open"),
        ]
        violations = validate_status_intervals(intervals)
        assert any("order" in str(v).lower() or "chronological" in str(v).lower()
                   for v in violations), violations


# ---------------------------------------------------------------------------
# FlowEfficiency invariants
# ---------------------------------------------------------------------------


class TestFlowEfficiencyInvariants:
    def _fe(self, *, cycle: timedelta, active: timedelta, eff: float) -> FlowEfficiency:
        return FlowEfficiency(
            item_id="#1", title="t",
            created_at=_ts(2026, 5, 5),
            completed_at=_ts(2026, 5, 5) + cycle,
            cycle_time=cycle, active_time=active, efficiency=eff,
        )

    def test_negative_cycle_time_invalid(self):
        fe = self._fe(cycle=timedelta(days=-1), active=timedelta(0), eff=0.0)
        violations = validate_flow_efficiency(fe)
        assert any("cycle_time < 0" in str(v) for v in violations), violations

    def test_negative_active_time_invalid(self):
        fe = self._fe(cycle=timedelta(days=1), active=timedelta(seconds=-10), eff=0.0)
        violations = validate_flow_efficiency(fe)
        assert any("active_time < 0" in str(v) for v in violations), violations

    def test_active_exceeds_cycle_invalid(self):
        fe = self._fe(cycle=timedelta(days=1), active=timedelta(days=2), eff=2.0)
        violations = validate_flow_efficiency(fe)
        assert any("active_time" in str(v) and "cycle_time" in str(v) for v in violations), violations

    def test_efficiency_outside_unit_interval_invalid(self):
        too_low = self._fe(cycle=timedelta(days=1), active=timedelta(0), eff=-0.1)
        too_high = self._fe(cycle=timedelta(days=1), active=timedelta(days=1), eff=1.1)
        assert any("efficiency" in str(v) for v in validate_flow_efficiency(too_low))
        assert any("efficiency" in str(v) for v in validate_flow_efficiency(too_high))

    def test_zero_cycle_with_perfect_efficiency_is_legal(self):
        """A same-instant created+merged item has cycle_time=0; defining
        efficiency = 1.0 in that case (rather than NaN) is the
        documented compute_pr_flow behaviour."""
        fe = self._fe(cycle=timedelta(0), active=timedelta(0), eff=1.0)
        assert validate_flow_efficiency(fe) == []


# ---------------------------------------------------------------------------
# InvariantViolation behavior
# ---------------------------------------------------------------------------


class TestInvariantViolation:
    def test_violation_carries_item_context(self):
        v = InvariantViolation("#42", "cycle_time < 0")
        assert "#42" in str(v)
        assert "cycle_time" in str(v)
