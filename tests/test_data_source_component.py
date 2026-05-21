"""Component tests for `flowmetrics.web.components.data_source`.

The Data Source page's coverage view is a GitHub-style calendar
heat-map: per-DAY counts of work items keyed on their creation
date, so the operator sees which days had items created and which
days had none.
"""

from __future__ import annotations

import json
from datetime import datetime

import duckdb

from flowmetrics.web.components.data_source import render


def _warehouse(
    items: list[tuple[datetime, datetime | None]],
) -> duckdb.DuckDBPyConnection:
    """`items` is a list of (created_at, completed_at)."""
    con = duckdb.connect(":memory:")
    con.execute(
        "CREATE TABLE work_items ("
        "contract_id VARCHAR, created_at TIMESTAMP, completed_at TIMESTAMP)"
    )
    if items:
        con.executemany(
            "INSERT INTO work_items VALUES ('c', ?, ?)",
            [(c, d) for c, d in items],
        )
    return con


class TestCoverage:
    def test_counts_every_work_item_including_in_flight(self):
        """The gap map counts ALL records by `created_at` — not
        just completions. A mostly-in-flight workflow must not
        read as empty."""
        con = _warehouse(
            [
                (datetime(2025, 4, 2, 9), None),  # in-flight
                (datetime(2025, 4, 2, 14), None),  # in-flight
                (datetime(2025, 4, 2, 16), datetime(2025, 4, 3)),
            ]
        )
        data = render(con, "c")
        assert data.total_records == 3
        by = {b.day_iso: b.records for b in data.days}
        assert by["2025-04-02"] == 3

    def test_groups_by_day_filling_gap_days_with_zero(self):
        """Each day in the span gets a bucket — days with no
        creations are zero, visible as a 'None' heat-map cell."""
        con = _warehouse(
            [
                (datetime(2025, 4, 2), None),
                (datetime(2025, 4, 2), None),
                (datetime(2025, 4, 5), None),
            ]
        )
        data = render(con, "c")
        by = {b.day_iso: b.records for b in data.days}
        assert by["2025-04-02"] == 2
        assert by["2025-04-03"] == 0  # gap / NODATA
        assert by["2025-04-04"] == 0  # gap / NODATA
        assert by["2025-04-05"] == 1
        assert len(data.days) == 4

    def test_empty_warehouse_has_a_no_data_headline(self):
        data = render(_warehouse([]), "c")
        assert data.total_records == 0
        assert data.days == ()
        assert "no work items" in data.headline.lower()

    def test_headline_names_the_record_count_and_span(self):
        con = _warehouse([(datetime(2025, 3, 10), None)])
        data = render(con, "c")
        assert "1 work item" in data.headline
        assert "Mar 10, 2025" in data.headline

    def test_span_is_capped_at_180_days(self):
        """A long-lived workflow must not produce a thousand daily
        bars — the chart shows the most recent 180 days, but every
        item still counts toward the total."""
        con = _warehouse(
            [(datetime(2024, 1, 5), None), (datetime(2025, 3, 10), None)]
        )
        data = render(con, "c")
        assert len(data.days) <= 180
        assert data.days[-1].day_iso == "2025-03-10"
        assert data.total_records == 2

    def test_vega_spec_is_a_calendar_heatmap(self):
        """The coverage view is a GitHub-style calendar heat-map:
        one rect cell per day, laid out week (x) by weekday (y),
        coloured by a record-count level."""
        con = _warehouse([(datetime(2025, 3, 10), None)])
        data = render(con, "c")
        spec = json.loads(data.vega_spec_json())
        mark = spec["mark"]
        mark_type = mark if isinstance(mark, str) else mark.get("type")
        assert mark_type == "rect"
        enc = spec["encoding"]
        assert enc["x"]["field"] == "week"
        assert enc["y"]["field"] == "weekday"
        assert enc["color"]["field"] == "level"

    def test_every_day_in_span_has_a_cell(self):
        """Unlike the old bar chart, gap days are not omitted —
        every day in the span emits a rect datum."""
        con = _warehouse([
            (datetime(2025, 4, 2), None),
            (datetime(2025, 4, 5), None),
        ])
        data = render(con, "c")
        spec = json.loads(data.vega_spec_json())
        assert len(spec["data"]["values"]) == 4  # Apr 2-5 inclusive

    def test_zero_creation_days_are_a_distinct_colour_level(self):
        """A day with no work items created is its own colour
        level — a visible 'None' cell, not an empty column."""
        con = _warehouse([
            (datetime(2025, 4, 2), None),
            (datetime(2025, 4, 5), None),  # Apr 3-4 had no creations
        ])
        data = render(con, "c")
        spec = json.loads(data.vega_spec_json())
        levels = {v["level"] for v in spec["data"]["values"]}
        assert "None" in levels, f"zero days need a level; got {levels}"
        assert "None" in spec["encoding"]["color"]["scale"]["domain"]

    def test_tooltip_names_the_day_and_record_count(self):
        """Hover shows the exact day and its work-item count."""
        con = _warehouse([(datetime(2025, 3, 10), None)])
        data = render(con, "c")
        spec = json.loads(data.vega_spec_json())
        tip_fields = {t["field"] for t in spec["encoding"]["tooltip"]}
        assert {"label", "records"} <= tip_fields

    def test_chart_is_titled_by_creation_date(self):
        """The chart is explicitly about the work items' creation
        date — both the title and the x-axis say so."""
        con = _warehouse([(datetime(2025, 3, 10), None)])
        data = render(con, "c")
        spec = json.loads(data.vega_spec_json())
        title = spec["title"]
        title_text = title if isinstance(title, str) else title.get("text")
        assert title_text == "Work Items by Creation Date"
        assert spec["encoding"]["x"]["axis"]["title"] == "Created Date"

    def test_chart_subtitle_notes_the_180_day_cap(self):
        """A subtitle warns the chart caps at 180 days so nobody
        expects year-old history to show up."""
        con = _warehouse([(datetime(2025, 3, 10), None)])
        data = render(con, "c")
        spec = json.loads(data.vega_spec_json())
        assert "180" in spec["title"]["subtitle"]
