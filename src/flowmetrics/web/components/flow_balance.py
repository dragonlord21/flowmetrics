"""Layer 3 — the flow-balance chart view.

Daily arrivals vs daily departures. When the two tracks each
other the system is balanced; runs where arrivals stay above
departures are the visible signal that WIP is accumulating. Two
extra signals sit on top of the basic two-line view:

  * `bar` between the lines on days where arrivals > departures
    (deficit fill) — makes the accumulating runs unmistakable.
  * `rect` over Saturday + Sunday columns — so a deficit can be
    read against the usual weekend arrival bump rather than as a
    standalone alarm.

The data is CFD-derived (via `daily_flow_metrics`), so the
component takes a `CfdModel`; everything specific to the
two-series shape, the weekend / deficit decorations and the
spec lives here, not in the CFD module.
"""

from __future__ import annotations

import datetime as dt
import json

from ...charts.cfd import CfdModel, daily_flow_metrics


def _day_type(iso: str) -> str:
    # weekday() returns 5 for Sat, 6 for Sun.
    return "weekend" if dt.date.fromisoformat(iso).weekday() >= 5 else "weekday"


def flow_balance_spec_json(model: CfdModel) -> str:
    """Vega spec for the daily flow-balance view. Skips day 0 — its
    `arrivals` is the window's carry-in rather than a per-day rate
    — so the scale and the signals reflect true daily flow."""
    metrics = daily_flow_metrics(model)
    wide: list[dict] = []
    lines: list[dict] = []
    for m in metrics[1:]:
        wide.append({
            "date_iso": m.date_iso, "date_display": m.date_display,
            "arrivals": m.arrivals, "departures": m.departures,
            "delta": m.arrivals - m.departures,
            "day_type": _day_type(m.date_iso),
        })
        lines.append({
            "date_iso": m.date_iso, "date_display": m.date_display,
            "kind": "Arrivals", "count": m.arrivals,
        })
        lines.append({
            "date_iso": m.date_iso, "date_display": m.date_display,
            "kind": "Departures", "count": m.departures,
        })

    sort_iso = [m.date_iso for m in metrics[1:]]
    step = max(1, (len(metrics) + 9) // 10)
    axis_label_values = [m.date_iso for m in metrics[1::step]]

    # Share the same nominal x scale across all three layers so
    # rect / bar columns align to their line points.
    x_nominal: dict = {
        "field": "date_iso", "type": "nominal",
        "sort": sort_iso,
    }
    x_with_axis: dict = {
        **x_nominal,
        "axis": {
            "title": "Date (UTC)", "labelAngle": 0,
            "values": axis_label_values,
            "labelExpr": "utcFormat(datetime(datum.value), '%b %d')",
        },
    }

    spec: dict = {
        "$schema": "https://vega.github.io/schema/vega-lite/v5.json",
        "background": "transparent",
        "padding": 12,
        "width": "container",
        "height": 200,
        "layer": [
            # 1. Faint rect over weekend columns. Same pattern the
            # throughput chart uses — neutral grey at low opacity so
            # it reads as context, not signal.
            {
                "data": {"values": wide},
                "transform": [{"filter": "datum.day_type === 'weekend'"}],
                "mark": {
                    "type": "rect",
                    "color": "__theme:muted__",
                    "opacity": 0.08,
                },
                "encoding": {"x": x_nominal},
            },
            # 2. Deficit fill: a soft coral band spanning from
            # departures up to arrivals on days where the system is
            # falling behind. Same hue family as the CFD palette so
            # it reads as part of the same chart vocabulary.
            {
                "data": {"values": wide},
                "transform": [{"filter": "datum.delta > 0"}],
                "mark": {
                    "type": "bar",
                    "opacity": 0.4,
                    "color": "__theme:cfd-7__",
                },
                "encoding": {
                    "x": x_nominal,
                    "y": {"field": "departures", "type": "quantitative"},
                    "y2": {"field": "arrivals"},
                    "tooltip": [
                        {"field": "date_display", "type": "nominal", "title": "Date"},
                        {"field": "delta", "type": "quantitative",
                         "title": "Net WIP gain"},
                    ],
                },
            },
            # 3. The two flow lines.
            {
                "data": {"values": lines},
                "mark": {"type": "line", "point": True, "interpolate": "monotone"},
                "encoding": {
                    "x": x_with_axis,
                    "y": {
                        "field": "count", "type": "quantitative",
                        "axis": {"title": "Items / day"},
                    },
                    "color": {
                        "field": "kind", "type": "nominal",
                        "scale": {
                            "domain": ["Arrivals", "Departures"],
                            "range": ["__theme:cfd-1__", "__theme:cfd-3__"],
                        },
                        "legend": {"title": None, "orient": "top-right"},
                    },
                    "tooltip": [
                        {"field": "date_display", "type": "nominal", "title": "Date"},
                        {"field": "kind", "type": "nominal", "title": "Flow"},
                        {"field": "count", "type": "quantitative", "title": "Items"},
                    ],
                },
            },
        ],
        "config": {
            "view": {"fill": "__theme:bg__", "stroke": None},
            "axis": {
                "labelColor": "__theme:fg__",
                "titleColor": "__theme:muted__",
            },
        },
    }
    return json.dumps(spec, separators=(",", ":"))
