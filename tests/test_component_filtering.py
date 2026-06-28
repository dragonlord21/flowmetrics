"""Tests for component filtering by issuetype."""

from __future__ import annotations

from datetime import date, datetime
import duckdb

from flowmetrics.web.components import (
    cycle_time,
    throughput,
    aging,
    forecast,
    work_items_table,
)


def _warehouse() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(
        """CREATE TABLE work_items (
            contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
            title VARCHAR, url VARCHAR,
            created_at TIMESTAMP, completed_at TIMESTAMP,
            cycle_time_days DOUBLE, issuetype VARCHAR)"""
    )
    con.execute(
        """CREATE TABLE transitions (
            contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
            entered_at TIMESTAMP, stage VARCHAR, signal VARCHAR)"""
    )

    # Insert items
    # Contract 'c':
    # 2 completed Stories, 1 completed Bug, 1 open Story, 1 open Bug
    con.executemany(
        "INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("c", "jira", "#1", "Story 1", None,
             datetime(2026, 1, 1), datetime(2026, 1, 5), 4.0, "Story"),
            ("c", "jira", "#2", "Story 2", None,
             datetime(2026, 1, 1), datetime(2026, 1, 10), 9.0, "Story"),
            ("c", "jira", "#3", "Bug 1", None,
             datetime(2026, 1, 2), datetime(2026, 1, 12), 10.0, "Bug"),
            ("c", "jira", "#4", "In-Flight Story", None,
             datetime(2026, 1, 15), None, None, "Story"),
            ("c", "jira", "#5", "In-Flight Bug", None,
             datetime(2026, 1, 15), None, None, "Bug"),
        ],
    )

    # Transitions for in-flight items
    con.executemany(
        "INSERT INTO transitions VALUES (?,?,?,?,?,?)",
        [
            ("c", "jira", "#4", datetime(2026, 1, 16), "WIP", "open"),
            ("c", "jira", "#5", datetime(2026, 1, 16), "WIP", "open"),
        ],
    )
    return con


def test_cycle_time_filtering():
    con = _warehouse()

    # Unfiltered (None) should return all completed items (3 items)
    model_all = cycle_time.render(con, "c")
    assert len(model_all.points) == 3

    # Filtered by "Story"
    model_story = cycle_time.render(con, "c", issuetypes=["Story"])
    assert len(model_story.points) == 2
    assert {p.item_id for p in model_story.points} == {"#1", "#2"}

    # Filtered by "Bug"
    model_bug = cycle_time.render(con, "c", issuetypes=["Bug"])
    assert len(model_bug.points) == 1
    assert {p.item_id for p in model_bug.points} == {"#3"}

    # Empty filter should return no completed items
    model_empty = cycle_time.render(con, "c", issuetypes=[])
    assert len(model_empty.points) == 0


def test_throughput_filtering():
    con = _warehouse()

    # Unfiltered (None) should return throughput based on all completed items
    model_all = throughput.render(con, "c")
    # Sum up daily counts to get total throughput
    total_all = sum(d.count for d in model_all.daily)
    assert total_all == 3

    # Filtered by "Story"
    model_story = throughput.render(con, "c", issuetypes=["Story"])
    total_story = sum(d.count for d in model_story.daily)
    assert total_story == 2

    # Filtered by "Bug"
    model_bug = throughput.render(con, "c", issuetypes=["Bug"])
    total_bug = sum(d.count for d in model_bug.daily)
    assert total_bug == 1

    # Empty filter
    model_empty = throughput.render(con, "c", issuetypes=[])
    total_empty = sum(d.count for d in model_empty.daily)
    assert total_empty == 0


def test_aging_filtering():
    con = _warehouse()
    asof = date(2026, 1, 20)

    # Unfiltered (None) should return both in-flight items
    model_all = aging.render(con, "c", asof=asof)
    assert model_all.count == 2

    # Filtered by "Story"
    model_story = aging.render(con, "c", asof=asof, issuetypes=["Story"])
    assert model_story.count == 1
    assert model_story.items[0].item_id == "#4"

    # Filtered by "Bug"
    model_bug = aging.render(con, "c", asof=asof, issuetypes=["Bug"])
    assert model_bug.count == 1
    assert model_bug.items[0].item_id == "#5"

    # Empty filter
    model_empty = aging.render(con, "c", asof=asof, issuetypes=[])
    assert model_empty.count == 0


def test_forecast_filtering():
    con = _warehouse()
    start_date = date(2026, 1, 20)
    end_date = date(2026, 2, 20)

    # Unfiltered (None) should use all completions as the sample history
    # Story completions: 2, Bug completions: 1
    model_all = forecast.render_when_done(con, "c", items=10, start_date=start_date)
    # Filtered by Story
    model_story = forecast.render_when_done(con, "c", items=10, start_date=start_date, issuetypes=["Story"])
    # Filtered by Bug
    model_bug = forecast.render_when_done(con, "c", items=10, start_date=start_date, issuetypes=["Bug"])

    assert model_all is not None
    assert model_story is not None
    assert model_bug is not None

    # Do the same for render_how_many
    model_hm_all = forecast.render_how_many(con, "c", start_date=start_date, end_date=end_date)
    model_hm_story = forecast.render_how_many(con, "c", start_date=start_date, end_date=end_date, issuetypes=["Story"])
    model_hm_bug = forecast.render_how_many(con, "c", start_date=start_date, end_date=end_date, issuetypes=["Bug"])

    assert model_hm_all is not None
    assert model_hm_story is not None
    assert model_hm_bug is not None


def test_work_items_table_filtering():
    con = _warehouse()

    # Unfiltered
    data_all = work_items_table.render(con, "c")
    assert data_all.count == 3  # completed only by default

    # Filtered by Story
    data_story = work_items_table.render(con, "c", issuetypes=["Story"])
    assert data_story.count == 2
    assert {r.item_id for r in data_story.rows} == {"#1", "#2"}

    # Filtered by Bug
    data_bug = work_items_table.render(con, "c", issuetypes=["Bug"])
    assert data_bug.count == 1
    assert data_bug.rows[0].item_id == "#3"

    # Empty filter
    data_empty = work_items_table.render(con, "c", issuetypes=[])
    assert data_empty.count == 0

    # In-flight scope, unfiltered
    data_inf_all = work_items_table.render(con, "c", in_flight_at="2026-01-20")
    assert data_inf_all.count == 2

    # In-flight scope, filtered by Story
    data_inf_story = work_items_table.render(con, "c", in_flight_at="2026-01-20", issuetypes=["Story"])
    assert data_inf_story.count == 1
    assert data_inf_story.rows[0].item_id == "#4"
