"""Shared percentile-rank filter for chart renders.

The page-level Percentile Filter slider narrows BOTH the chart's
scatter and the table's rows. The table uses DuckDB's
`PERCENT_RANK()`; this module gives the chart renders an
equivalent Python filter so the two views agree on the same
rows, even when the underlying data has ties (which it does
routinely — `cycle_time = 1d` is the dominant value for small
PRs).

DuckDB's `PERCENT_RANK()` returns `(rank - 1) / (n - 1)` with
ties sharing the rank of their first occurrence. We multiply
by 100 and round to match the integer-bound slider; this
module's `filter_by_rank` does the same.
"""

from __future__ import annotations

from typing import Callable, TypeVar

# Snap stops on the two-handle slider: 0 then the 5%-step ladder
# from P50 upward. The same ladder feeds `PERCENTILE_CONT` so
# the slider's readout can show "P50 (4d)" etc. without an
# extra round-trip.
PTILE_STOPS: tuple[int, ...] = (
    0, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100,
)

T = TypeVar("T")


def filter_by_rank(
    items: list[T],
    *,
    key: Callable[[T], float],
    ptile_min: int,
    ptile_max: int,
) -> list[T]:
    """Keep `items` whose percentile rank lands in
    `[ptile_min, ptile_max]`. Items with equal `key(item)` share
    the rank of the first occurrence — matching DuckDB's
    `PERCENT_RANK()` so the chart and the table filter to the
    same row set."""
    if not items:
        return []
    ordered = sorted(items, key=key)
    n = len(ordered)
    kept: list[T] = []
    i = 0
    while i < n:
        # Find the run of equal-key items starting at i.
        j = i
        anchor_key = key(ordered[i])
        while j < n and key(ordered[j]) == anchor_key:
            j += 1
        # PERCENT_RANK puts the whole run at the rank of the
        # first occurrence. `n == 1` falls back to rank 0.
        rank = round((i / max(1, n - 1)) * 100) if n > 1 else 0
        if ptile_min <= rank <= ptile_max:
            kept.extend(ordered[i:j])
        i = j
    return kept
