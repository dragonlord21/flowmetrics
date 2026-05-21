"""Forecast component — Monte Carlo over historical throughput.

Two views, both running M=10,000 simulations against the daily
throughput distribution captured by the warehouse:

  `render_when_done(con, contract, *, items, start_date)`
      "When will it be done?" Distribution of completion DATES
      for a given backlog of `items`.

  `render_how_many(con, contract, *, start_date, end_date)`
      "How many will be done by date X?" Distribution of
      COUNTS over a window from start_date..end_date.

Both delegate to `flowmetrics.forecast.monte_carlo_when_done` /
`monte_carlo_how_many` for the simulation itself. Those primitives
are stdlib-only (rng.choices + accumulate + bisect) and complete
10K runs in 10-25ms for typical inputs — well under the 200ms
real-time target.

The components add: warehouse access (pull daily throughput),
percentile extraction (P50/P85/P95), and a Vega-Lite spec
(histogram + percentile rules).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from random import Random

import duckdb

from ...forecast import (
    backward_percentile,
    build_histogram,
    forward_percentile,
    monte_carlo_how_many,
    monte_carlo_when_done,
)
from ...utc_dates import to_utc_display_date
from ...windows import Window

# Number of simulations. 10K is the standard Vacanti recommendation —
# enough for stable P95s, fast enough for interactive sliders.
DEFAULT_RUNS = 10_000

# Colour tokens — same neutrals + P85 accent the cycle-time chart
# uses. Plum is reserved for the headline commitment threshold.
_BAR_COLOR = "__theme:border__"
_PCT_COLOR_P50 = "__theme:muted__"
_PCT_COLOR_P85 = "__theme:p-500__"
_PCT_COLOR_P95 = "__theme:fg__"


def _daily_throughput(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    reference: Window | None = None,
) -> list[int]:
    """Read the historical daily throughput as a list of integers.

    One entry per calendar date from the earliest to latest
    completion (inclusive of zero-count days, per the Vacanti
    rule the throughput component pins). When `reference` is
    supplied, the window is clamped to that inclusive range —
    that's the operator's "MCS over a specific period" control
    (the sample distribution the simulation draws from).
    """
    where = ["contract_id = ?", "created_at IS NOT NULL", "completed_at IS NOT NULL"]
    params: list = [contract_name]
    if reference is not None:
        where.append("CAST(completed_at AS DATE) BETWEEN ? AND ?")
        params.extend([reference.from_, reference.to])
    rows = con.execute(
        f"SELECT CAST(completed_at AS DATE) AS d, count(*) AS n "
        f"FROM work_items "
        f"WHERE {' AND '.join(where)} "
        f"GROUP BY 1 ORDER BY 1 ASC",
        params,
    ).fetchall()
    if not rows:
        return []
    by_date = {d: int(n) for d, n in rows}
    # When the reference window is explicit, walk THAT range so
    # zero-count days inside the reference window are included
    # (otherwise we'd implicitly trim leading/trailing zeros).
    if reference is not None:
        cur = reference.from_
        last = reference.to
    else:
        cur = min(by_date)
        last = max(by_date)
    out: list[int] = []
    while cur <= last:
        out.append(by_date.get(cur, 0))
        cur += timedelta(days=1)
    return out


@dataclass(frozen=True)
class WhenDoneData:
    """Payload for the WWIBD-Date chart."""

    items: int
    start_date_iso: str
    runs: int
    histogram: tuple[dict, ...]
    histogram_total: int
    p50_iso: str
    p85_iso: str
    p95_iso: str
    p50_display: str
    p85_display: str
    p95_display: str
    headline: str
    daily_throughput_n_days: int

    @property
    def percentile_rows(self) -> tuple[dict, ...]:
        """Structured rows for the small (percentile, value)
        table rendered next to the chart. Ordered P50 → P95.
        Each row carries `label`, `value_display`, and `color`
        so the template can paint a matching swatch."""
        return (
            {"label": "P50", "value_display": self.p50_display,
             "color": _PCT_COLOR_P50},
            {"label": "P85", "value_display": self.p85_display,
             "color": _PCT_COLOR_P85},
            {"label": "P95", "value_display": self.p95_display,
             "color": _PCT_COLOR_P95},
        )

    def vega_spec_json(self) -> str:
        # Pre-thin x-axis labels when there are many bars —
        # nominal axes don't auto-thin in Vega-Lite. Target ~10
        # visible labels; use CEILING division so the step
        # actually grows past 1 for 11-19 bars (floor division
        # would keep step=1 there, leaving labels overlapping).
        date_isos = [b["date_iso"] for b in self.histogram]
        target = 10
        if len(date_isos) > target:
            step = (len(date_isos) + target - 1) // target  # ceil(n/target)
            axis_values = date_isos[::step]
        else:
            axis_values = None
        return _histogram_spec(
            values=[{"date_iso": b["date_iso"], "count": b["count"]} for b in self.histogram],
            x_field="date_iso",
            x_type="nominal",
            x_title="Completion date (UTC)",
            x_format_expr="utcFormat(datetime(datum.value), '%b %d')",
            x_axis_values=axis_values,
            pcts=[
                {"label": "P50", "anchor": self.p50_iso, "color": _PCT_COLOR_P50},
                {"label": "P85", "anchor": self.p85_iso, "color": _PCT_COLOR_P85},
                {"label": "P95", "anchor": self.p95_iso, "color": _PCT_COLOR_P95},
            ],
            pct_field="anchor",
            pct_field_type="nominal",
        )


@dataclass(frozen=True)
class HowManyData:
    """Payload for the WWIBD-How-Many chart."""

    days: int
    start_date_iso: str
    end_date_iso: str
    runs: int
    histogram: tuple[dict, ...]
    histogram_total: int
    p50: int
    p85: int
    p95: int
    headline: str
    daily_throughput_n_days: int

    @property
    def percentile_rows(self) -> tuple[dict, ...]:
        """Structured rows for the small percentile table. For
        How-Many the value is a count, displayed as a
        high-confidence floor ('≥ N items')."""
        return (
            {"label": "P50", "value_display": f"≥ {self.p50} items",
             "color": _PCT_COLOR_P50},
            {"label": "P85", "value_display": f"≥ {self.p85} items",
             "color": _PCT_COLOR_P85},
            {"label": "P95", "value_display": f"≥ {self.p95} items",
             "color": _PCT_COLOR_P95},
        )

    def vega_spec_json(self) -> str:
        return _histogram_spec(
            values=[{"count": b["count"], "runs": b["runs"]} for b in self.histogram],
            x_field="count",
            x_type="quantitative",
            x_title="Items completed in window",
            x_format_expr=None,
            pcts=[
                {"label": "P50", "anchor": self.p50, "color": _PCT_COLOR_P50},
                {"label": "P85", "anchor": self.p85, "color": _PCT_COLOR_P85},
                {"label": "P95", "anchor": self.p95, "color": _PCT_COLOR_P95},
            ],
            pct_field="anchor",
            pct_field_type="quantitative",
            y_field="runs",
        )


def render_when_done(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    items: int,
    start_date: date,
    runs: int = DEFAULT_RUNS,
    seed: int = 0,
    reference: Window | None = None,
) -> WhenDoneData:
    """Run a Monte Carlo simulation: when will `items` be done?

    `start_date` is required — the caller (the one window model)
    supplies it; the component never invents a date. `reference`
    clamps the historical-throughput sample to that inclusive
    window. Returns a payload with the full histogram + the
    P50/P85/P95 dates.
    """
    samples = _daily_throughput(con, contract_name, reference=reference)
    rng = Random(seed)
    if not samples:
        # Defensive: no data yet — surface a degenerate but
        # well-formed payload rather than crashing.
        return WhenDoneData(
            items=items,
            start_date_iso=start_date.isoformat(),
            runs=0,
            histogram=(),
            histogram_total=0,
            p50_iso="",
            p85_iso="",
            p95_iso="",
            p50_display="",
            p85_display="",
            p95_display="",
            headline="No throughput data yet.",
            daily_throughput_n_days=0,
        )
    results = monte_carlo_when_done(
        samples, items, start_date, runs=runs, rng=rng
    )
    hist = build_histogram(results)
    # `forward_percentile` expects p in (0, 100] — percentages,
    # NOT probabilities. Passing 0.50/0.85/0.95 here would make
    # threshold = total * p / 100 = a 50/85/95-count threshold,
    # crossed by the first bin, collapsing all three percentile
    # dates onto the same earliest date.
    p50 = forward_percentile(hist, 50)
    p85 = forward_percentile(hist, 85)
    p95 = forward_percentile(hist, 95)
    p50_disp = to_utc_display_date(
        datetime(p50.year, p50.month, p50.day, tzinfo=UTC)
    )
    p85_disp = to_utc_display_date(
        datetime(p85.year, p85.month, p85.day, tzinfo=UTC)
    )
    p95_disp = to_utc_display_date(
        datetime(p95.year, p95.month, p95.day, tzinfo=UTC)
    )
    histogram = tuple(
        {"date_iso": d.isoformat(), "count": hist.counts[d]}
        for d in hist.sorted_keys
    )
    headline = (
        f"{items} items from {to_utc_display_date(datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC))} "
        f"· P50 by {p50_disp} · P85 by {p85_disp} · P95 by {p95_disp} "
        f"({runs:,} runs over {len(samples)} days of throughput history)"
    )
    return WhenDoneData(
        items=items,
        start_date_iso=start_date.isoformat(),
        runs=runs,
        histogram=histogram,
        histogram_total=hist.total,
        p50_iso=p50.isoformat(),
        p85_iso=p85.isoformat(),
        p95_iso=p95.isoformat(),
        p50_display=p50_disp,
        p85_display=p85_disp,
        p95_display=p95_disp,
        headline=headline,
        daily_throughput_n_days=len(samples),
    )


def render_how_many(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    start_date: date,
    end_date: date,
    runs: int = DEFAULT_RUNS,
    seed: int = 0,
    reference: Window | None = None,
) -> HowManyData:
    """Run a Monte Carlo simulation: how many items will be done
    in [start_date, end_date]?

    For counts the percentile convention is inverted relative to
    dates: P85 is the count you'd hit AT LEAST 85% of the time,
    which is LOWER than P50. Use `backward_percentile` so the
    "high-confidence-floor" interpretation holds. `reference`
    clamps the historical-throughput sample to that inclusive
    window.
    """
    samples = _daily_throughput(con, contract_name, reference=reference)
    rng = Random(seed)
    days = (end_date - start_date).days + 1
    if not samples or days <= 0:
        return HowManyData(
            days=max(0, days),
            start_date_iso=start_date.isoformat(),
            end_date_iso=end_date.isoformat(),
            runs=0,
            histogram=(),
            histogram_total=0,
            p50=0,
            p85=0,
            p95=0,
            headline="No throughput data yet.",
            daily_throughput_n_days=0,
        )
    results = monte_carlo_how_many(
        samples,
        start_date=start_date,
        end_date=end_date,
        runs=runs,
        rng=rng,
    )
    hist = build_histogram(results)
    # Counts: high-confidence floor → use backward_percentile so
    # P85 means "at least this many, 85% of the time".
    # Same unit caveat as render_when_done — `backward_percentile`
    # takes a percentage in (0, 100], not a probability.
    p50 = int(backward_percentile(hist, 50))
    p85 = int(backward_percentile(hist, 85))
    p95 = int(backward_percentile(hist, 95))
    histogram = tuple(
        {"count": k, "runs": hist.counts[k]} for k in hist.sorted_keys
    )
    headline = (
        f"{days} days from "
        f"{to_utc_display_date(datetime(start_date.year, start_date.month, start_date.day, tzinfo=UTC))} "
        f"· P50 ≥ {p50} items · P85 ≥ {p85} · P95 ≥ {p95} "
        f"({runs:,} runs over {len(samples)} days of throughput history)"
    )
    return HowManyData(
        days=days,
        start_date_iso=start_date.isoformat(),
        end_date_iso=end_date.isoformat(),
        runs=runs,
        histogram=histogram,
        histogram_total=hist.total,
        p50=p50,
        p85=p85,
        p95=p95,
        headline=headline,
        daily_throughput_n_days=len(samples),
    )


def _histogram_spec(
    *,
    values: list[dict],
    x_field: str,
    x_type: str,
    x_title: str,
    x_format_expr: str | None,
    pcts: list[dict],
    pct_field: str,
    pct_field_type: str,
    y_field: str = "count",
    x_axis_values: list | None = None,
) -> str:
    """Shared Vega-Lite spec: histogram bars with P50/P85/P95
    threshold rules. Used by both forecast charts.

    `x_axis_values`: explicit list of tick values to show. For
    nominal axes with many entries, callers pre-thin this list
    (Vega-Lite doesn't auto-thin nominal axes)."""
    x_axis: dict = {
        "title": x_title,
        "labelAngle": 0,
        "grid": False,
    }
    if x_format_expr:
        x_axis["labelExpr"] = x_format_expr
    if x_axis_values is not None:
        x_axis["values"] = x_axis_values

    spec: dict = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "background": "transparent",
        "padding": 12,
        "width": "container",
        "layer": [
            {
                "data": {"values": values},
                "mark": {"type": "bar", "color": _BAR_COLOR, "cornerRadius": 1},
                "encoding": {
                    "x": {"field": x_field, "type": x_type, "axis": x_axis},
                    "y": {
                        "field": y_field,
                        "type": "quantitative",
                        "axis": {"title": "Simulations", "format": "d"},
                    },
                    "tooltip": [
                        {"field": x_field, "type": x_type, "title": x_title},
                        {
                            "field": y_field,
                            "type": "quantitative",
                            "title": "Simulations",
                        },
                    ],
                },
            },
            {
                "data": {"values": pcts},
                "mark": {
                    "type": "rule",
                    "size": 2.5,
                },
                "encoding": {
                    "x": {
                        "field": pct_field,
                        "type": pct_field_type,
                        # Anchor at band CENTER for nominal/ordinal
                        # axes. Without this, the rule lands at the
                        # band's left edge — which on the first bar
                        # means jammed against the y-axis. `band`
                        # is a no-op for quantitative scales, so
                        # safe to set unconditionally.
                        "band": 0.5,
                    },
                    "color": {
                        "field": "label",
                        "type": "nominal",
                        "scale": {
                            "domain": ["P50", "P85", "P95"],
                            "range": [
                                _PCT_COLOR_P50,
                                _PCT_COLOR_P85,
                                _PCT_COLOR_P95,
                            ],
                        },
                        # Legend hidden — the percentile values
                        # are surfaced in a structured table next
                        # to the chart (`percentile_rows`), which
                        # carries the actual forecast values too.
                        "legend": None,
                    },
                    # Distinct dash patterns per percentile so even
                    # when two rules coincide on the same x value
                    # (a common case with small backlogs — P85 and
                    # P95 often land on the same date) each line
                    # is still visually identifiable.
                    "strokeDash": {
                        "field": "label",
                        "type": "nominal",
                        "scale": {
                            "domain": ["P50", "P85", "P95"],
                            "range": [
                                [2, 3],     # P50: short
                                [6, 4],     # P85: medium
                                [12, 5],    # P95: long
                            ],
                        },
                        "legend": None,
                    },
                    # Pixel-level horizontal offset per percentile.
                    # When two rules share the same x value they'd
                    # otherwise overlap pixel-perfectly and one
                    # disappears behind the other. Nudge each to
                    # its own pixel column.
                    "xOffset": {
                        "field": "label",
                        "type": "nominal",
                        "scale": {
                            "domain": ["P50", "P85", "P95"],
                            "range": [-4, 0, 4],
                        },
                        "legend": None,
                    },
                    "tooltip": [
                        {"field": "label", "type": "nominal", "title": "Percentile"},
                        {"field": pct_field, "type": pct_field_type, "title": "At"},
                    ],
                },
            },
            # Inline labels for each percentile rule so the chart
            # itself names what each line is — without relying on
            # a legend or the structured table elsewhere. Text
            # sits just above the chart's plot area (y=0 with
            # baseline=bottom) at the same x as its rule.
            {
                "data": {"values": pcts},
                "mark": {
                    "type": "text",
                    "baseline": "bottom",
                    "dy": -4,
                    "fontSize": 11,
                    "fontWeight": 600,
                },
                "encoding": {
                    "x": {
                        "field": pct_field,
                        "type": pct_field_type,
                        "band": 0.5,
                    },
                    "y": {"value": 0},
                    "text": {"field": "label", "type": "nominal"},
                    "color": {
                        "field": "label",
                        "type": "nominal",
                        "scale": {
                            "domain": ["P50", "P85", "P95"],
                            "range": [
                                _PCT_COLOR_P50,
                                _PCT_COLOR_P85,
                                _PCT_COLOR_P95,
                            ],
                        },
                        "legend": None,
                    },
                    "xOffset": {
                        "field": "label",
                        "type": "nominal",
                        "scale": {
                            "domain": ["P50", "P85", "P95"],
                            "range": [-12, 0, 12],
                        },
                        "legend": None,
                    },
                },
            },
        ],
        "config": {
            "view": {"stroke": None},
            "axis": {
                "labelFont": "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif",
                "titleFont": "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif",
                "labelColor": "__theme:fg__",
                "titleColor": "__theme:muted__",
            },
        },
    }
    return json.dumps(spec)
