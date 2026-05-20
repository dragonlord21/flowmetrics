"""Aging Work In Progress component, per Vacanti.

In-flight items only (started ≤ asof but not yet completed by asof),
plotted by current workflow state (x-axis nominal) and elapsed age in
days (y-axis). Percentile lines from completed-item cycle times serve
as commitment thresholds — an item aging past P85 is likely to miss
the forecast.

The component takes an `asof` UTC date parameter (default = today) so
historical aging views work. The fixture for this codebase has all
items completed within a bounded window, so the default render is
empty against it — pass `asof=2026-05-06` for a non-empty demo.
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass
from datetime import UTC, date, datetime

import duckdb

from ...contract import WorkflowStates
from ...utc_dates import attach_utc, to_utc_display_date


# Chart colors are CSS-theme-driven; see _base.html.jinja's
# `flowmetricsTheme` for resolved values. Same neutrals + P85
# accent as the cycle-time chart so the commitment line is the
# single thing that pops on both charts.
_PCT_COLOR_P50 = "__theme:border__"
_PCT_COLOR_P85 = "__theme:p-500__"
_PCT_COLOR_P95 = "__theme:muted__"
_POINT_COLOR = "__theme:muted__"


@dataclass(frozen=True)
class AgingItem:
    """One in-flight item at the asof date."""

    item_id: str
    title: str
    url: str | None
    current_state: str
    age_days: int


@dataclass(frozen=True)
class PercentileProvenance:
    """The percentile thresholds drawn on the Aging chart, plus
    the provenance the operator needs to read them honestly.

    The chart can lie if the percentiles come from a 7-day
    completed sample but the visible in-flight items have been
    aging for 30+ days. Surfacing the source counts + window +
    smell flag lets the UI flag the disparity instead of
    silently treating the P-lines as gospel.
    """

    p50: float
    p85: float
    p95: float
    # Number of completed items the thresholds were computed from.
    source_count: int
    # Date range of those completions (for the "P-lines from
    # May 4 – May 10" provenance line in the UI).
    source_window_earliest_iso: str | None
    source_window_latest_iso: str | None
    source_window_display: str
    # Smell signal: when in-flight ages dwarf the historical
    # window, the UI surfaces a callout. Empty string when not
    # triggered.
    smell: bool
    smell_text: str


@dataclass(frozen=True)
class WarehouseCoverage:
    """What date range the warehouse covers and when it was last
    refreshed. Used by the empty-state UI to name the actual
    gap when the chart can't render ('data is from May 4 to
    May 10; you asked about May 19')."""

    # Earliest / latest completion dates we have on hand.
    earliest_iso: str | None
    latest_iso: str | None
    earliest_display: str | None
    latest_display: str | None
    # Last materialise timestamp. Distinct from `latest_iso`:
    # the warehouse may have been materialised recently even
    # though no items completed recently.
    last_materialised_iso: str | None


@dataclass(frozen=True)
class AgingData:
    """Payload for the aging tile partial."""

    items: tuple[AgingItem, ...]
    count: int
    asof_iso: str
    asof_display: str
    headline: str
    percentiles: PercentileProvenance
    coverage: WarehouseCoverage
    # Empty-state classification for the view layer. None when
    # `items` is non-empty. Otherwise one of:
    #
    #   "asof_after_coverage"   — warehouse has data up to
    #       `coverage.latest_*` but not through `asof`. Action:
    #       backfill the gap.
    #   "asof_before_coverage"  — symmetric: asof predates the
    #       earliest data on hand. Action: backfill backwards.
    #   "no_work_in_flight"     — warehouse covers asof, no items
    #       were in flight. The real answer; no fetch would help.
    empty_state: str | None

    def vega_spec_json(self) -> str:
        """Vega-Lite layered spec: point marks per in-flight item +
        rule lines for percentile thresholds.

        Y-axis quantitative (age_days). X-axis nominal (current_state).
        Forward jitter on x so dots within the same column don't
        collapse on a single line."""
        item_values = [
            {
                "item_id": i.item_id,
                "title": i.title,
                "url": i.url,
                "current_state": i.current_state,
                "age_days": i.age_days,
            }
            for i in self.items
        ]

        # Percentile reference rows: drawn as horizontal rules
        # spanning the full x-range with right-aligned labels.
        pct_values = [
            {"label": "P50", "age_days": self.percentiles.p50, "color": _PCT_COLOR_P50},
            {"label": "P85", "age_days": self.percentiles.p85, "color": _PCT_COLOR_P85},
            {"label": "P95", "age_days": self.percentiles.p95, "color": _PCT_COLOR_P95},
        ]

        # Canonical Vega-Lite jitter pattern (`point_offset_random`):
        # a quantitative xOffset field with values drawn from
        # `random()` ∈ [0, 1). Combined with a band-scale x, Vega
        # auto-fits the offset to the BAND'S actual width at
        # render time — no pixel range baked in, so the dot
        # cloud fills whatever width each band ends up with.
        rng = random.Random(0)
        for v in item_values:
            v["_jitter"] = rng.random()

        # Compute y-axis headroom for the per-column 'N WIP'
        # badge. The badge sits ABOVE the highest dot in each
        # band (Vega text mark with `baseline: bottom, dy: -8`).
        # If the y domain is exactly max(age_days), the badge
        # gets clipped against the top edge of the plot area.
        # 10% headroom is enough for the text + dy offset at
        # typical chart heights without making the dots feel
        # squashed. Percentile threshold lines (P95) may also
        # extend slightly above the max-aged dot; this also
        # ensures their right-edge label sits inside the plot.
        max_age = max((i.age_days for i in self.items), default=1.0)
        y_domain_max = max_age * 1.10
        # Round up to a nicer tick value when ages are large so
        # the axis labels stay legible (avoids "5,973" instead
        # of "6,000" as the top tick).
        if y_domain_max > 100:
            step = 10 ** (len(str(int(y_domain_max))) - 2)
            y_domain_max = (int(y_domain_max / step) + 1) * step

        spec: dict = {
            "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
            "background": "transparent",
            "padding": 12,
            "width": "container",
            "layer": [
                # In-flight item dots — painted FIRST so the
                # threshold rules above sit on top and stay
                # visible against the dot cloud.
                {
                    # Zoom + pan via mouse wheel / click-drag,
                    # bound to scales. Per the layered-chart
                    # cycle-time precedent, `params` lives on
                    # this (the data-bearing) layer rather than
                    # at top level — top-level params on a
                    # layered spec produce per-layer copies of
                    # the selection and Vega complains about
                    # duplicate signal names.
                    "params": [
                        {
                            "name": "aging_zoom",
                            "select": {
                                "type": "interval",
                                "encodings": ["x", "y"],
                            },
                            "bind": "scales",
                        }
                    ],
                    "data": {"values": item_values},
                    "mark": {
                        "type": "point",
                        "filled": True,
                        "size": 90,
                        "color": _POINT_COLOR,
                        "opacity": 0.85,
                        # Signal clickability (the fragment script
                        # navigates to the item's lifecycle page on
                        # click).
                        "cursor": "pointer",
                    },
                    "encoding": {
                        "x": {
                            "field": "current_state",
                            "type": "nominal",
                            # Explicit `band` scale (Vega-Lite's
                            # default for nominal+xOffset is `point`,
                            # which sticks each label AT its tick and
                            # gives no horizontal room for jitter).
                            # Band gives each category a real width;
                            # the axis label sits at the band center.
                            "scale": {
                                "type": "band",
                                "paddingInner": 0.1,
                                "paddingOuter": 0.1,
                            },
                            "axis": {"title": "Current state", "labelAngle": 0},
                            "sort": None,
                        },
                        "xOffset": {
                            # Canonical `point_offset_random` —
                            # quantitative offset with NO explicit
                            # scale.range. Vega auto-fits the offset
                            # to the band's actual width at render
                            # time, so the dot cloud adapts to any
                            # viewport without pixel constants
                            # baked into the spec.
                            "field": "_jitter",
                            "type": "quantitative",
                        },
                        "y": {
                            "field": "age_days",
                            "type": "quantitative",
                            # Floor at 0 — age can't go negative.
                            # Ceil with headroom for the per-band
                            # 'N WIP' badge that paints above the
                            # tallest dot in each column.
                            "scale": {
                                "domainMin": 0,
                                "domainMax": y_domain_max,
                            },
                            "axis": {"title": "Age (days)"},
                        },
                        "tooltip": [
                            {"field": "item_id", "type": "nominal", "title": "#"},
                            {"field": "title", "type": "nominal", "title": "Title"},
                            {
                                "field": "current_state",
                                "type": "nominal",
                                "title": "State",
                            },
                            {
                                "field": "age_days",
                                "type": "quantitative",
                                "title": "Age (d)",
                            },
                        ],
                    },
                },
                # Per-state count badge — "n WIP" above the column.
                # Aggregated client-side by Vega-Lite so it stays
                # in sync with the visible dot cloud (and with
                # zoom/filter selections, when those land). Y is
                # the max age per band + a small offset upward so
                # the badge sits just above the highest dot.
                {
                    "data": {"values": item_values},
                    "mark": {
                        "type": "text",
                        "baseline": "bottom",
                        "dy": -8,
                        "fontSize": 12,
                        "fontWeight": 600,
                        "color": "__theme:muted__",
                    },
                    "encoding": {
                        "x": {
                            "field": "current_state",
                            "type": "nominal",
                            "sort": None,
                        },
                        "y": {
                            "aggregate": "max",
                            "field": "age_days",
                            "type": "quantitative",
                        },
                        "text": {"aggregate": "count"},
                    },
                },
                # Percentile threshold rules — painted AFTER the
                # dots + count badge so they sit on top and stay
                # visible against the dot cloud (Vega-Lite paints
                # layers in order).
                # The color-with-legend encoding labels P50/P85/P95
                # without us having to anchor text marks at the
                # chart's right edge (which was unreliable across
                # Vega versions on a layered nominal-x chart).
                {
                    "data": {"values": pct_values},
                    "mark": {
                        "type": "rule",
                        "size": 2.5,
                        "strokeDash": [5, 3],
                    },
                    "encoding": {
                        "y": {"field": "age_days", "type": "quantitative"},
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
                            "legend": {
                                "title": None,
                                "orient": "top-right",
                                "symbolType": "stroke",
                                "symbolStrokeWidth": 2.5,
                            },
                        },
                        "tooltip": [
                            {"field": "label", "type": "nominal", "title": "Threshold"},
                            {
                                "field": "age_days",
                                "type": "quantitative",
                                "title": "Days",
                                "format": ".1f",
                            },
                        ],
                    },
                },
            ],
            "config": {
                "view": {"stroke": None},
                "axis": {
                    "labelFont": (
                        "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif"
                    ),
                    "titleFont": (
                        "-apple-system,BlinkMacSystemFont,Segoe UI,Roboto,sans-serif"
                    ),
                    "labelColor": "__theme:fg__",
                    "titleColor": "__theme:muted__",
                },
            },
        }
        return json.dumps(spec)


def render(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    asof: date | None = None,
    contract_start: date | None = None,
    contract_stop: date | None = None,
    states: WorkflowStates | None = None,
) -> AgingData:
    """Compute the aging-WIP payload for `contract_name` at `asof`.

    `asof` defaults to today (UTC date). Items are in-flight if:
      - created_at.date() ≤ asof
      - completed_at is null, OR completed_at.date() > asof

    Current state is the latest transition with entered_at ≤ asof.
    Items with no transitions yet at asof are tagged `"Unknown"`.

    WIP filter (`states`):
      When provided, only items currently in `states.wip`
      appear on the chart. Backlog AND done both fall out
      (done items are also excluded by the completion filter,
      so this is belt-and-braces for the edge case of a state
      classified as `done` for items still showing as in-flight).
      Surviving items keep their RAW state name so operators
      see where work is stuck (Changes Suggested, Awaiting
      Feedback) rather than aggregated bucket names.

    `contract_start` / `contract_stop` come from the contract YAML
    (set by the caller — the component doesn't touch the
    filesystem). Used only to classify the empty state: an asof
    outside the contract window is "not missing data, just outside
    scope," not the same as a stale warehouse.
    """
    if asof is None:
        asof = datetime.now(UTC).date()
    asof_anchor = datetime(asof.year, asof.month, asof.day, tzinfo=UTC)

    rows = con.execute(
        "SELECT item_id, title, url, created_at "
        "FROM work_items "
        "WHERE contract_id = ? "
        "  AND created_at IS NOT NULL "
        "  AND CAST(created_at AS DATE) <= CAST(? AS DATE) "
        "  AND (completed_at IS NULL "
        "       OR CAST(completed_at AS DATE) > CAST(? AS DATE)) "
        "ORDER BY created_at ASC",
        [contract_name, asof, asof],
    ).fetchall()

    items: list[AgingItem] = []
    for item_id, title, url, created_at in rows:
        created_aware = attach_utc(created_at)
        # Vacanti's Age formula: CD - SD + 1 (same `+1` inclusive
        # rule as cycle time; a same-day item ages as 1d). p. 60,
        # Actionable Agile Metrics 10th Anniversary Edition.
        # Computed at query/view time because asof is a runtime
        # parameter — materialise can't precompute this.
        age = (asof - created_aware.date()).days + 1
        # Latest transition at or before asof — that's the current
        # state from asof's point of view.
        state_row = con.execute(
            "SELECT stage FROM transitions "
            "WHERE contract_id = ? AND item_id = ? "
            "  AND CAST(entered_at AS DATE) <= CAST(? AS DATE) "
            "ORDER BY entered_at DESC LIMIT 1",
            [contract_name, str(item_id), asof],
        ).fetchone()
        current_state = state_row[0] if state_row else "Unknown"
        # WIP filter: drop items whose current_state isn't in
        # `states.wip`. Backlog and done both fall out by being
        # absent from that set. Surviving items keep their raw
        # state name on the chart.
        if states is not None and str(current_state) not in states.wip:
            continue
        items.append(
            AgingItem(
                item_id=str(item_id),
                title=str(title) if title is not None else "",
                url=str(url) if url is not None else None,
                current_state=str(current_state),
                age_days=int(age),
            )
        )

    # Percentile thresholds from completed cycle times — same source
    # the cycle-time chart's reference lines come from. The aging
    # check is "this in-flight item is now older than the typical
    # commitment threshold". `cycle_time_days IS NOT NULL` filters
    # to completions; in-flight items by definition have null.
    pct_row = con.execute(
        "SELECT percentile_cont(0.50) WITHIN GROUP (ORDER BY cycle_time_days), "
        "       percentile_cont(0.85) WITHIN GROUP (ORDER BY cycle_time_days), "
        "       percentile_cont(0.95) WITHIN GROUP (ORDER BY cycle_time_days), "
        "       count(*), "
        "       min(CAST(completed_at AS DATE)), "
        "       max(CAST(completed_at AS DATE)) "
        "FROM work_items "
        "WHERE contract_id = ? AND cycle_time_days IS NOT NULL",
        [contract_name],
    ).fetchone()
    p50 = float(pct_row[0] or 0.0)
    p85 = float(pct_row[1] or 0.0)
    p95 = float(pct_row[2] or 0.0)
    pct_source_count = int(pct_row[3] or 0)
    pct_source_earliest = pct_row[4]
    pct_source_latest = pct_row[5]
    if pct_source_earliest is not None and pct_source_latest is not None:
        anc_e = datetime(
            pct_source_earliest.year,
            pct_source_earliest.month,
            pct_source_earliest.day,
            tzinfo=UTC,
        )
        anc_l = datetime(
            pct_source_latest.year,
            pct_source_latest.month,
            pct_source_latest.day,
            tzinfo=UTC,
        )
        pct_source_window_display = (
            f"{to_utc_display_date(anc_e)} – {to_utc_display_date(anc_l)}"
        )
        pct_source_earliest_iso = pct_source_earliest.isoformat()
        pct_source_latest_iso = pct_source_latest.isoformat()
    else:
        pct_source_window_display = "no completed items yet"
        pct_source_earliest_iso = None
        pct_source_latest_iso = None

    asof_display = to_utc_display_date(asof_anchor)
    headline = (
        f"{len(items)} in-flight item{'' if len(items) == 1 else 's'} "
        f"as of {asof_display} (UTC) · "
        f"P50 {p50:.1f}d · P85 {p85:.1f}d · P95 {p95:.1f}d "
        f"from {pct_source_count} completed item"
        f"{'' if pct_source_count == 1 else 's'}"
        f" ({pct_source_window_display})"
    )

    # Smell ratio: if in-flight ages span far longer than the
    # historical sample window driving the percentiles, the
    # thresholds are statistically shaky. Flag at 3× as a
    # reasonable "consider broadening" trigger (configurable
    # later if teams want different sensitivity).
    SMELL_RATIO_THRESHOLD = 3.0
    smell = False
    smell_text = ""
    if (
        items
        and pct_source_earliest is not None
        and pct_source_latest is not None
    ):
        max_age = max(i.age_days for i in items)
        window_days = (pct_source_latest - pct_source_earliest).days + 1
        if window_days > 0 and max_age / window_days >= SMELL_RATIO_THRESHOLD:
            ratio = max_age / window_days
            smell = True
            smell_text = (
                f"In-flight ages reach {max_age}d but percentiles are "
                f"drawn from a {window_days}d window — that's {ratio:.1f}× "
                f"wider. Consider broadening the historical sample for "
                f"more representative thresholds."
            )

    # Warehouse coverage: what dates of completion data do we have
    # on hand? Surfaced in the empty-state messages so the operator
    # sees the actual gap they're asking us to fill, not a vague
    # "outside window" hand-wave.
    coverage_row = con.execute(
        "SELECT min(CAST(completed_at AS DATE)), "
        "       max(CAST(completed_at AS DATE)), "
        "       max(materialised_at) "
        "FROM work_items "
        "WHERE contract_id = ?",
        [contract_name],
    ).fetchone()
    earliest_data_date = coverage_row[0] if coverage_row else None
    latest_data_date = coverage_row[1] if coverage_row else None
    last_mat_dt = coverage_row[2] if coverage_row else None
    if last_mat_dt is not None:
        last_mat_aware = (
            last_mat_dt.replace(tzinfo=UTC)
            if last_mat_dt.tzinfo is None
            else last_mat_dt
        )
        last_mat_date = last_mat_aware.astimezone(UTC).date()
        warehouse_last_materialised_iso = last_mat_date.isoformat()
    else:
        last_mat_date = None
        warehouse_last_materialised_iso = None

    # Classify empty state. Non-empty → None. Else, action-first:
    # tell the operator what data the warehouse has and what range
    # they'd need to import to answer their question.
    #   "asof_after_coverage"        asof > latest completion on
    #                                hand. Import the gap forward.
    #   "asof_before_coverage"       asof < earliest completion
    #                                (symmetric).
    #   "in_flight_never_captured"   warehouse covers asof in the
    #                                COMPLETED dimension but has
    #                                never recorded an in-flight
    #                                row. The aging answer is
    #                                artificially empty; importing
    #                                will fetch current open work.
    #   "no_work_in_flight"          warehouse has captured in-flight
    #                                rows at some point but none
    #                                are open at asof. The real
    #                                answer; no fetch helps.
    have_any_in_flight_row = con.execute(
        "SELECT count(*) FROM work_items "
        "WHERE contract_id = ? AND completed_at IS NULL",
        [contract_name],
    ).fetchone()[0]
    if items:
        empty_state: str | None = None
    elif latest_data_date is not None and asof > latest_data_date:
        empty_state = "asof_after_coverage"
    elif earliest_data_date is not None and asof < earliest_data_date:
        empty_state = "asof_before_coverage"
    elif have_any_in_flight_row == 0:
        empty_state = "in_flight_never_captured"
    else:
        empty_state = "no_work_in_flight"

    # Pre-format coverage bounds as both ISO + human display
    # strings so the empty-state UI doesn't have to do date math.
    def _both(d: date | None) -> tuple[str | None, str | None]:
        if d is None:
            return None, None
        anchor = datetime(d.year, d.month, d.day, tzinfo=UTC)
        return d.isoformat(), to_utc_display_date(anchor)

    coverage_earliest_iso, coverage_earliest_display = _both(earliest_data_date)
    coverage_latest_iso, coverage_latest_display = _both(latest_data_date)

    return AgingData(
        items=tuple(items),
        count=len(items),
        asof_iso=asof.isoformat(),
        asof_display=asof_display,
        headline=headline,
        empty_state=empty_state,
        percentiles=PercentileProvenance(
            p50=p50, p85=p85, p95=p95,
            source_count=pct_source_count,
            source_window_earliest_iso=pct_source_earliest_iso,
            source_window_latest_iso=pct_source_latest_iso,
            source_window_display=pct_source_window_display,
            smell=smell,
            smell_text=smell_text,
        ),
        coverage=WarehouseCoverage(
            earliest_iso=coverage_earliest_iso,
            latest_iso=coverage_latest_iso,
            earliest_display=coverage_earliest_display,
            latest_display=coverage_latest_display,
            last_materialised_iso=warehouse_last_materialised_iso,
        ),
    )
