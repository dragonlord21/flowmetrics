"""The flow-balance chart — daily arrivals vs departures, with two
extra signals layered on:

  * Days where arrivals > departures (WIP growing) are filled
    between the two lines.
  * Saturdays and Sundays are tinted so the reader can sanity-check
    whether a deficit is just the usual weekend arrival bump.

These tests pin the layered Vega-Lite shape (top-level `layer`
with weekend / deficit / lines marks) and the filter expressions
that drive each highlight.
"""

from __future__ import annotations

import json

from flowmetrics.charts.cfd import CfdDailyPoint, CfdModel
from flowmetrics.web.components.flow_balance import flow_balance_spec_json


def _model() -> CfdModel:
    # May 01 = Friday (carry-in), 02 Sat (deficit + weekend),
    # 03 Sun (surplus + weekend), 04 Mon (balanced).
    stages = ("Open", "Done")
    daily = (
        CfdDailyPoint("2026-05-01", "May 01, 2026",
                      {"Open": 5, "Done": 1}),
        CfdDailyPoint("2026-05-02", "May 02, 2026",
                      {"Open": 10, "Done": 3}),
        CfdDailyPoint("2026-05-03", "May 03, 2026",
                      {"Open": 11, "Done": 5}),
        CfdDailyPoint("2026-05-04", "May 04, 2026",
                      {"Open": 12, "Done": 6}),
    )
    return CfdModel(
        daily=daily, stages=stages, headline="",
        first_date_iso="2026-05-01", last_date_iso="2026-05-04", crop=None,
    )


def _layer(spec: dict, mark_type: str) -> dict:
    return next(
        layer for layer in spec["layer"]
        if layer["mark"]["type"] == mark_type
    )


def test_spec_is_layered_with_weekend_deficit_and_line_marks():
    spec = json.loads(flow_balance_spec_json(_model()))
    marks = [layer["mark"]["type"] for layer in spec["layer"]]
    # Order matters — weekend tint paints under everything,
    # the deficit fill sits between lines and ground, lines on top.
    assert marks == ["rect", "bar", "line"]


def test_line_layer_carries_themed_two_series_and_skips_day_zero():
    spec = json.loads(flow_balance_spec_json(_model()))
    lines = _layer(spec, "line")
    assert lines["encoding"]["color"]["scale"]["range"] == [
        "__theme:cfd-1__", "__theme:cfd-3__",
    ]
    dates = {row["date_iso"] for row in lines["data"]["values"]}
    assert "2026-05-01" not in dates
    assert dates == {"2026-05-02", "2026-05-03", "2026-05-04"}


def test_deficit_layer_filters_to_arrivals_above_departures_only():
    spec = json.loads(flow_balance_spec_json(_model()))
    deficit = _layer(spec, "bar")
    assert deficit["transform"] == [{"filter": "datum.delta > 0"}]
    # Sanity: the wide rows carry signed deltas; the filter does the
    # actual subset.
    deltas = {row["date_iso"]: row["delta"] for row in deficit["data"]["values"]}
    assert deltas == {
        # 2026-05-02: arrivals 5 (Open Δ + Done Δ = 5+2=7? no — arrivals = next-cum minus carry-in)
        # daily_flow_metrics treats day 0 cum as the carry-in.
        # arrivals[d2] = top-cum Δ = 10 - 5 = 5. departures = bottom Δ = 3-1 = 2 → delta +3.
        "2026-05-02": 3,
        # 2026-05-03: arrivals 11-10=1, departures 5-3=2 → -1.
        "2026-05-03": -1,
        # 2026-05-04: arrivals 12-11=1, departures 6-5=1 → 0.
        "2026-05-04": 0,
    }


def test_deficit_layer_spans_from_departures_up_to_arrivals():
    spec = json.loads(flow_balance_spec_json(_model()))
    deficit = _layer(spec, "bar")
    enc = deficit["encoding"]
    assert enc["y"]["field"] == "departures"
    assert enc["y2"]["field"] == "arrivals"


def test_weekend_layer_filters_on_day_type_and_carries_only_saturday_sunday():
    spec = json.loads(flow_balance_spec_json(_model()))
    weekend = _layer(spec, "rect")
    assert weekend["transform"] == [
        {"filter": "datum.day_type === 'weekend'"},
    ]
    by_date = {row["date_iso"]: row["day_type"]
               for row in weekend["data"]["values"]}
    # May 02 = Sat, May 03 = Sun; May 04 = Mon must read as weekday.
    assert by_date["2026-05-02"] == "weekend"
    assert by_date["2026-05-03"] == "weekend"
    assert by_date["2026-05-04"] == "weekday"
