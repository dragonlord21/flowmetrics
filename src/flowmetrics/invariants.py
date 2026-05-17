"""Canonical-data invariants — what "valid" data looks like at
the schema level, independent of any chart or metric.

Use these to catch source-adapter bugs (negative cycle times,
overlapping intervals, out-of-window activity) before they
silently propagate into charts.

Each validator returns a list of `InvariantViolation` — empty
when valid. Callers decide how loud to be: tests use plain
`assert validate(...) == []`; the CLI could log them; a future
sanity-check report could surface them as data-quality issues.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import timedelta

from .compute import FlowEfficiency, StatusInterval, WorkItem


@dataclass(frozen=True)
class InvariantViolation:
    item_id: str
    message: str

    def __str__(self) -> str:
        return f"[{self.item_id}] {self.message}"


# Tolerance for activity events that predate created_at. Real-world:
# GitHub PR commits carry `committedDate` from the developer's local
# clock — often before the PR was opened on GitHub. Tolerated up to
# 30 days; longer pre-dates indicate corruption.
#
# NO tolerance ceiling on post-completion activity: legitimate
# post-merge comments / cross-references can fire days or weeks
# later. The metric layer drops events outside [created_at,
# completed_at] when clustering for active-time anyway.
_ACTIVITY_BEFORE_CREATED_TOLERANCE = timedelta(days=30)


def validate_work_item(item: WorkItem) -> list[InvariantViolation]:
    """Schema-level invariants on a single WorkItem.

    Checked (HARD — anything fires = real corruption):
      - completed_at >= created_at when completed_at is set.
      - activity timestamps within 30d before created_at (catches
        truly-wrong data; pre-PR commit drift is normal).
      - status_intervals pass their own validator.

    Deliberately NOT checked:
      - activity timestamps after completed_at — post-merge
        comments are a normal, expected event on long-lived PRs.
        The flow-efficiency calc filters them already.
    """
    out: list[InvariantViolation] = []

    if item.completed_at is not None and item.completed_at < item.created_at:
        out.append(InvariantViolation(
            item.item_id,
            f"completed_at < created_at "
            f"({item.completed_at.isoformat()} < {item.created_at.isoformat()})",
        ))

    created_floor = item.created_at - _ACTIVITY_BEFORE_CREATED_TOLERANCE
    for ts in item.activity:
        if ts < created_floor:
            out.append(InvariantViolation(
                item.item_id,
                f"activity timestamp {ts.isoformat()} more than 30d "
                f"before created_at {item.created_at.isoformat()}",
            ))

    for v in validate_status_intervals(item.status_intervals):
        out.append(InvariantViolation(item.item_id, v.message))

    return out


def validate_status_intervals(
    intervals: Sequence[StatusInterval],
) -> list[InvariantViolation]:
    """Schema-level invariants on a chronological interval list.

    Checked:
      - Each interval: start <= end.
      - Chronological order: intervals[i].start <= intervals[i+1].start.
      - No overlap: intervals[i].end <= intervals[i+1].start.
    """
    out: list[InvariantViolation] = []
    for iv in intervals:
        if iv.end < iv.start:
            out.append(InvariantViolation(
                "<unknown>",
                f"interval.end < interval.start for stage {iv.status!r}: "
                f"{iv.end.isoformat()} < {iv.start.isoformat()}",
            ))
    for i in range(len(intervals) - 1):
        cur, nxt = intervals[i], intervals[i + 1]
        if nxt.start < cur.start:
            out.append(InvariantViolation(
                "<unknown>",
                f"intervals not in chronological order: "
                f"[{i + 1}].start ({nxt.start.isoformat()}) < "
                f"[{i}].start ({cur.start.isoformat()})",
            ))
        elif cur.end > nxt.start:
            out.append(InvariantViolation(
                "<unknown>",
                f"intervals overlap: [{i}] ends {cur.end.isoformat()} "
                f"after [{i + 1}] starts {nxt.start.isoformat()}",
            ))
    return out


def validate_flow_efficiency(fe: FlowEfficiency) -> list[InvariantViolation]:
    """Schema-level invariants on a FlowEfficiency row.

    Checked:
      - cycle_time >= 0
      - active_time >= 0
      - active_time <= cycle_time
      - efficiency in [0, 1]
      - completed_at >= created_at
    """
    out: list[InvariantViolation] = []
    if fe.cycle_time < timedelta(0):
        out.append(InvariantViolation(
            fe.item_id, f"cycle_time < 0 ({fe.cycle_time})",
        ))
    if fe.active_time < timedelta(0):
        out.append(InvariantViolation(
            fe.item_id, f"active_time < 0 ({fe.active_time})",
        ))
    if fe.active_time > fe.cycle_time:
        out.append(InvariantViolation(
            fe.item_id,
            f"active_time > cycle_time ({fe.active_time} > {fe.cycle_time})",
        ))
    if not (0.0 <= fe.efficiency <= 1.0):
        out.append(InvariantViolation(
            fe.item_id, f"efficiency outside [0, 1]: {fe.efficiency}",
        ))
    if fe.completed_at < fe.created_at:
        out.append(InvariantViolation(
            fe.item_id,
            f"completed_at < created_at "
            f"({fe.completed_at.isoformat()} < {fe.created_at.isoformat()})",
        ))
    return out
