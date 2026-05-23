"""Throughput primitives — daily completion counts.

The web chart-model layer (`flowmetrics.charts.throughput`,
`flowmetrics.charts.forecast`) and the CLI compute path both reach
the same lowest level: count completions per day, zero-fill the
gaps. `daily_counts` is that primitive; `daily_throughput` is the
CLI surface that pulls dates off `WorkItem`s before calling it.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date

from .compute import WorkItem


def daily_counts(dates: Iterable[date], start: date, stop: date) -> list[int]:
    """Daily counts in `[start, stop]` inclusive; zero-fills.

    Pure: takes a sequence of dates (any source — completions,
    arrivals, stage entries) and the inclusive window. Dates
    outside the window are ignored. Caller picks the window — the
    bias-correct choice for the Monte Carlo (per the NODATA-not-
    zero rule) is the observed completion span, not a wider one.
    """
    if stop < start:
        raise ValueError(f"stop ({stop}) must be >= start ({start})")
    span = (stop - start).days + 1
    counts = [0] * span
    for d in dates:
        if start <= d <= stop:
            counts[(d - start).days] += 1
    return counts


def daily_throughput(
    prs: Iterable[WorkItem],
    start: date,
    stop: date,
) -> list[int]:
    """Daily merge counts across `[start, stop]` (inclusive).

    Zero-merge days are included as zero — they are real historical
    observations and the Monte Carlo sampler needs them to represent
    "slow days" in the distribution. Items without a `completed_at`
    are skipped.
    """
    return daily_counts(
        (pr.completed_at.date() for pr in prs if pr.completed_at is not None),
        start,
        stop,
    )
