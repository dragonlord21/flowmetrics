"""Layer 3 — tests for the throughput view
(`flowmetrics.web.components.throughput`).

Decisions — daily series shape, weekday/weekend tagging,
warehouse/missing coverage, the headline math — are tested at
Layer 2 in `test_charts_throughput.py`. This file covers the view
only: `render()` wires query → model, and `to_vega()` faithfully
translates a model into a Vega-Lite spec.
"""

from __future__ import annotations

from datetime import date, datetime

import duckdb

from flowmetrics.charts.throughput import build_throughput_model
from flowmetrics.warehouse.queries import CompletedItem
from flowmetrics.web.components.throughput import render, to_vega
from flowmetrics.windows import Window


def _completed(n: int, completed: date) -> CompletedItem:
    return CompletedItem(
        item_id=f"#{n}", title=f"item {n}", url=None,
        completed_at=datetime(completed.year, completed.month, completed.day, 12),
        cycle_time_days=3.0,
    )


def _model():
    """A non-empty throughput model with a clean coverage span."""
    items = [
        _completed(1, date(2026, 1, 5)),
        _completed(2, date(2026, 1, 5)),
        _completed(3, date(2026, 1, 7)),
    ]
    return build_throughput_model(items)


def _warehouse() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(
        """CREATE TABLE work_items (
            contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
            title VARCHAR, url VARCHAR,
            created_at TIMESTAMP, completed_at TIMESTAMP,
            cycle_time_days DOUBLE)"""
    )
    con.executemany(
        "INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?)",
        [
            ("c", "github", f"#{i}", f"t{i}", None,
             datetime(2026, 1, 1), datetime(2026, 1, 5 + (i % 3)), float(i))
            for i in range(1, 7)
        ],
    )
    return con


def _mark_types(spec: dict) -> list[str]:
    out = []
    for lyr in spec["layer"]:
        m = lyr["mark"]
        out.append(m["type"] if isinstance(m, dict) else m)
    return out


class TestRenderWiresQueryToModel:
    def test_render_returns_a_model_from_the_warehouse(self):
        model = render(_warehouse(), "c")
        assert not model.is_empty

    def test_render_passes_the_view_window_through(self):
        view = Window(from_=date(2026, 1, 1), to=date(2026, 1, 10))
        model = render(_warehouse(), "c", view=view)
        assert model.daily[0].date_iso == "2026-01-01"
        assert model.daily[-1].date_iso == "2026-01-10"

    def test_unknown_contract_yields_an_empty_model(self):
        assert render(_warehouse(), "absent").is_empty


class TestToVegaStructure:
    def test_spec_has_four_layers(self):
        # weekend rect, missing rect, bars, em-dash marker.
        assert _mark_types(to_vega(_model())) == ["rect", "rect", "bar", "text"]

    def test_x_axis_is_pinned_to_the_date_order(self):
        model = _model()
        expected = [d.date_iso for d in model.daily]
        for layer in to_vega(model)["layer"]:
            assert layer["encoding"]["x"]["sort"] == expected

    def test_axis_labels_are_thinned_for_long_windows(self):
        # 31-day window → ≤ 11 labels.
        items = [_completed(1, date(2026, 1, 1))]
        model = build_throughput_model(
            items,
            view=Window(from_=date(2026, 1, 1), to=date(2026, 1, 31)),
        )
        bars = next(
            lyr for lyr in to_vega(model)["layer"]
            if isinstance(lyr["mark"], dict) and lyr["mark"]["type"] == "bar"
        )
        labels = bars["encoding"]["x"]["axis"]["values"]
        assert 0 < len(labels) <= 11

    def test_bar_layer_filters_to_covered_days(self):
        bars = next(
            lyr for lyr in to_vega(_model())["layer"]
            if isinstance(lyr["mark"], dict) and lyr["mark"]["type"] == "bar"
        )
        filters = [t.get("filter") for t in bars.get("transform", [])]
        assert any("warehouse" in str(f) for f in filters)

    def test_tooltip_reads_preformatted_date_display(self):
        bars = next(
            lyr for lyr in to_vega(_model())["layer"]
            if isinstance(lyr["mark"], dict) and lyr["mark"]["type"] == "bar"
        )
        tooltip = bars["encoding"]["tooltip"]
        completed = next(t for t in tooltip if t.get("title") == "Completed")
        # Pre-formatted, nominal — Vega's temporal formatter would
        # shift the UTC date to browser-local.
        assert completed["field"] == "date_display"
        assert completed["type"] == "nominal"
