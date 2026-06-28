"""Layer 1 (data access) — tests for `flowmetrics.warehouse.queries`.

These functions are pure SQL: a DuckDB connection in, raw typed
rows out. No windowing, no percentiles, no chart decisions — the
chart-model layer (`flowmetrics.charts`) does the deciding; this
layer only fetches. The tests build a tiny in-memory `work_items`
table directly — no warehouse fixture, no CLI.
"""

from __future__ import annotations

from datetime import date, datetime

import duckdb

from flowmetrics.warehouse.queries import (
    CompletedItem,
    InFlightItem,
    completed_items,
    completion_date_range,
    count_open_items,
    first_stage_entries,
    in_flight_snapshot,
    latest_materialized_at,
    observed_stages,
    observed_issuetypes,
    pairwise_stage_precedence,
    creations_by_day,
)


def _warehouse() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(
        """CREATE TABLE work_items (
            contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
            title VARCHAR, url VARCHAR,
            created_at TIMESTAMP, completed_at TIMESTAMP,
            cycle_time_days DOUBLE, issuetype VARCHAR
        )"""
    )
    rows = [
        # workflow "c" — three completed, completion-ascending
        ("c", "github", "#1", "first", "http://x/1",
         datetime(2026, 1, 1), datetime(2026, 1, 4), 3.0, "Story"),
        ("c", "github", "#2", "second", None,
         datetime(2026, 1, 2), datetime(2026, 1, 9), 7.0, "Bug"),
        ("c", "github", "#3", "third", "http://x/3",
         datetime(2026, 1, 1), datetime(2026, 2, 1), 31.0, "Story"),
        # workflow "c" — two in flight (completed_at NULL)
        ("c", "github", "#4", "open", None,
         datetime(2026, 1, 5), None, None, "Bug"),
        ("c", "github", "#5", "open2", None,
         datetime(2026, 1, 6), None, None, "Task"),
        # a different workflow — must be excluded
        ("other", "github", "#9", "elsewhere", None,
         datetime(2026, 1, 1), datetime(2026, 1, 2), 1.0, "Story"),
    ]
    con.executemany("INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?)", rows)
    return con


class TestCompletedItems:
    def test_returns_completed_items_for_the_contract(self):
        items = completed_items(_warehouse(), "c")
        assert {i.item_id for i in items} == {"#1", "#2", "#3"}

    def test_excludes_in_flight_items(self):
        items = completed_items(_warehouse(), "c")
        assert "#4" not in {i.item_id for i in items}
        assert all(i.completed_at is not None for i in items)

    def test_excludes_other_contracts(self):
        items = completed_items(_warehouse(), "c")
        assert "#9" not in {i.item_id for i in items}

    def test_maps_every_column_onto_the_row_type(self):
        items = completed_items(_warehouse(), "c")
        first = next(i for i in items if i.item_id == "#1")
        assert first == CompletedItem(
            item_id="#1", title="first", url="http://x/1",
            completed_at=datetime(2026, 1, 4), cycle_time_days=3.0,
        )

    def test_null_url_survives_as_none(self):
        items = completed_items(_warehouse(), "c")
        second = next(i for i in items if i.item_id == "#2")
        assert second.url is None

    def test_rows_are_ordered_oldest_completion_first(self):
        items = completed_items(_warehouse(), "c")
        assert items == sorted(items, key=lambda i: i.completed_at)

    def test_unknown_contract_returns_empty(self):
        assert completed_items(_warehouse(), "nope") == []

    def test_filters_by_issuetypes(self):
        items = completed_items(_warehouse(), "c", issuetypes=["Story"])
        assert {i.item_id for i in items} == {"#1", "#3"}

        items_bug = completed_items(_warehouse(), "c", issuetypes=["Bug"])
        assert {i.item_id for i in items_bug} == {"#2"}

        items_both = completed_items(_warehouse(), "c", issuetypes=["Story", "Bug"])
        assert {i.item_id for i in items_both} == {"#1", "#2", "#3"}

    def test_empty_issuetypes_returns_empty(self):
        assert completed_items(_warehouse(), "c", issuetypes=[]) == []


def _warehouse_with_transitions() -> duckdb.DuckDBPyConnection:
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
    con.executemany(
        "INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?)",
        [
            # open at 2026-02-01: created before, not completed
            ("c", "github", "#1", "one", None,
             datetime(2026, 1, 1), None, None, "Story"),
            # open: created before, completed AFTER the snapshot
            ("c", "github", "#2", "two", None,
             datetime(2026, 1, 5), datetime(2026, 3, 1), None, "Bug"),
            # closed: completed before the snapshot
            ("c", "github", "#3", "three", None,
             datetime(2026, 1, 2), datetime(2026, 1, 20), 18.0, "Story"),
            # not yet created at the snapshot
            ("c", "github", "#4", "four", None,
             datetime(2026, 2, 15), None, None, "Bug"),
            # open, never transitioned
            ("c", "github", "#5", "five", None,
             datetime(2026, 1, 10), None, None, "Task"),
        ],
    )
    con.executemany(
        "INSERT INTO transitions VALUES (?,?,?,?,?,?)",
        [
            ("c", "github", "#1", datetime(2026, 1, 2), "Draft", "open"),
            ("c", "github", "#1", datetime(2026, 1, 10), "Review", "ready"),
            # a transition AFTER the snapshot — must be ignored
            ("c", "github", "#1", datetime(2026, 3, 5), "Merged", "merge"),
            ("c", "github", "#2", datetime(2026, 1, 6), "Draft", "open"),
        ],
    )
    return con


def _by_id(items: list[InFlightItem], item_id: str) -> InFlightItem:
    return next(i for i in items if i.item_id == item_id)


class TestInFlightSnapshot:
    ASOF = date(2026, 2, 1)

    def test_includes_items_open_at_the_snapshot(self):
        items = in_flight_snapshot(_warehouse_with_transitions(), "c", self.ASOF)
        assert {i.item_id for i in items} == {"#1", "#2", "#5"}

    def test_excludes_items_completed_by_the_snapshot(self):
        items = in_flight_snapshot(_warehouse_with_transitions(), "c", self.ASOF)
        assert "#3" not in {i.item_id for i in items}

    def test_excludes_items_created_after_the_snapshot(self):
        items = in_flight_snapshot(_warehouse_with_transitions(), "c", self.ASOF)
        assert "#4" not in {i.item_id for i in items}

    def test_item_completed_after_the_snapshot_is_still_in_flight(self):
        items = in_flight_snapshot(_warehouse_with_transitions(), "c", self.ASOF)
        assert "#2" in {i.item_id for i in items}

    def test_current_state_is_the_latest_transition_at_or_before_asof(self):
        items = in_flight_snapshot(_warehouse_with_transitions(), "c", self.ASOF)
        assert _by_id(items, "#1").current_state == "Review"

    def test_transitions_after_asof_do_not_set_the_state(self):
        items = in_flight_snapshot(_warehouse_with_transitions(), "c", self.ASOF)
        assert _by_id(items, "#1").current_state != "Merged"

    def test_item_with_no_transitions_is_unknown(self):
        items = in_flight_snapshot(_warehouse_with_transitions(), "c", self.ASOF)
        assert _by_id(items, "#5").current_state == "Unknown"

    def test_rows_are_ordered_by_creation(self):
        items = in_flight_snapshot(_warehouse_with_transitions(), "c", self.ASOF)
        assert [i.item_id for i in items] == ["#1", "#2", "#5"]

    def test_filters_by_issuetypes(self):
        items = in_flight_snapshot(_warehouse_with_transitions(), "c", self.ASOF, issuetypes=["Story"])
        assert {i.item_id for i in items} == {"#1"}

        items_bug = in_flight_snapshot(_warehouse_with_transitions(), "c", self.ASOF, issuetypes=["Bug"])
        assert {i.item_id for i in items_bug} == {"#2"}

        items_empty = in_flight_snapshot(_warehouse_with_transitions(), "c", self.ASOF, issuetypes=[])
        assert items_empty == []


class TestCountOpenItems:
    def test_counts_items_with_no_completion(self):
        # #1, #4, #5 have completed_at NULL.
        assert count_open_items(_warehouse_with_transitions(), "c") == 3

    def test_zero_for_an_unknown_contract(self):
        assert count_open_items(_warehouse_with_transitions(), "nope") == 0


class TestFirstStageEntries:
    def test_returns_first_entry_per_item_per_stage(self):
        entries = first_stage_entries(_warehouse_with_transitions(), "c")
        # #1: Draft Jan 2, Review Jan 10, Merged Mar 5; #2: Draft Jan 6.
        keys = {(e.item_id, e.stage, e.entered_date) for e in entries}
        assert keys == {
            ("#1", "Draft", date(2026, 1, 2)),
            ("#1", "Review", date(2026, 1, 10)),
            ("#1", "Merged", date(2026, 3, 5)),
            ("#2", "Draft", date(2026, 1, 6)),
        }

    def test_only_stages_filter_excludes_other_transitions(self):
        entries = first_stage_entries(
            _warehouse_with_transitions(), "c",
            only_stages=("Draft", "Review"),
        )
        assert {e.stage for e in entries} == {"Draft", "Review"}

    def test_empty_only_stages_yields_no_entries(self):
        assert first_stage_entries(
            _warehouse_with_transitions(), "c", only_stages=(),
        ) == []

    def test_filters_by_issuetypes(self):
        entries = first_stage_entries(_warehouse_with_transitions(), "c", issuetypes=["Story"])
        keys = {(e.item_id, e.stage, e.entered_date) for e in entries}
        assert keys == {
            ("#1", "Draft", date(2026, 1, 2)),
            ("#1", "Review", date(2026, 1, 10)),
            ("#1", "Merged", date(2026, 3, 5)),
        }

        entries_bug = first_stage_entries(_warehouse_with_transitions(), "c", issuetypes=["Bug"])
        keys_bug = {(e.item_id, e.stage, e.entered_date) for e in entries_bug}
        assert keys_bug == {
            ("#2", "Draft", date(2026, 1, 6)),
        }

    def test_empty_issuetypes_returns_empty(self):
        assert first_stage_entries(_warehouse_with_transitions(), "c", issuetypes=[]) == []


class TestObservedStages:
    def test_returns_distinct_stages_alphabetically(self):
        assert observed_stages(_warehouse_with_transitions(), "c") == [
            "Draft", "Merged", "Review",
        ]

    def test_unknown_contract_is_empty(self):
        assert observed_stages(_warehouse_with_transitions(), "nope") == []

    def test_filters_by_issuetypes(self):
        assert observed_stages(_warehouse_with_transitions(), "c", issuetypes=["Story"]) == [
            "Draft", "Merged", "Review",
        ]
        assert observed_stages(_warehouse_with_transitions(), "c", issuetypes=["Bug"]) == [
            "Draft"
        ]

    def test_empty_issuetypes_returns_empty(self):
        assert observed_stages(_warehouse_with_transitions(), "c", issuetypes=[]) == []


class TestPairwiseStagePrecedence:
    def test_counts_ordered_pairs(self):
        pairs = pairwise_stage_precedence(_warehouse_with_transitions(), "c")
        # #1 visits Draft → Review → Merged; #2 visits Draft only.
        as_dict = {(a, b): c for a, b, c in pairs}
        assert as_dict == {
            ("Draft", "Review"): 1,
            ("Draft", "Merged"): 1,
            ("Review", "Merged"): 1,
        }

    def test_filters_by_issuetypes(self):
        pairs = pairwise_stage_precedence(_warehouse_with_transitions(), "c", issuetypes=["Story"])
        as_dict = {(a, b): c for a, b, c in pairs}
        assert as_dict == {
            ("Draft", "Review"): 1,
            ("Draft", "Merged"): 1,
            ("Review", "Merged"): 1,
        }

        assert pairwise_stage_precedence(_warehouse_with_transitions(), "c", issuetypes=["Bug"]) == []

    def test_empty_issuetypes_returns_empty(self):
        assert pairwise_stage_precedence(_warehouse_with_transitions(), "c", issuetypes=[]) == []


def _warehouse_with_materialized() -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(":memory:")
    con.execute(
        """CREATE TABLE work_items (
            contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
            title VARCHAR, url VARCHAR,
            created_at TIMESTAMP, completed_at TIMESTAMP,
            cycle_time_days DOUBLE, materialized_at TIMESTAMP)"""
    )
    con.executemany(
        "INSERT INTO work_items VALUES (?,?,?,?,?,?,?,?,?)",
        [
            ("c", "github", "#1", "a", None,
             datetime(2026, 1, 1), datetime(2026, 1, 4), 3.0,
             datetime(2026, 5, 1, 8, 0)),
            ("c", "github", "#2", "b", None,
             datetime(2026, 1, 2), datetime(2026, 1, 9), 7.0,
             datetime(2026, 5, 3, 9, 0)),
            ("other", "github", "#9", "x", None,
             datetime(2026, 1, 1), datetime(2026, 1, 2), 1.0,
             datetime(2099, 1, 1)),  # other workflow — must be excluded
        ],
    )
    return con


class TestCompletionDateRange:
    def test_returns_min_and_max_completion_dates_for_contract(self):
        from datetime import date as _d
        lo, hi = completion_date_range(_warehouse_with_materialized(), "c")
        assert lo == _d(2026, 1, 4)
        assert hi == _d(2026, 1, 9)

    def test_unknown_contract_yields_none_none(self):
        assert completion_date_range(_warehouse_with_materialized(), "nope") == (None, None)

    def test_no_completions_yields_none_none(self):
        # only in-flight items for the workflow.
        con = duckdb.connect(":memory:")
        con.execute(
            """CREATE TABLE work_items (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                title VARCHAR, url VARCHAR,
                created_at TIMESTAMP, completed_at TIMESTAMP,
                cycle_time_days DOUBLE, materialized_at TIMESTAMP)"""
        )
        con.execute(
            "INSERT INTO work_items VALUES "
            "('c','github','#1','a',NULL,?,NULL,NULL,?)",
            [datetime(2026, 1, 1), datetime(2026, 5, 1)],
        )
        assert completion_date_range(con, "c") == (None, None)


class TestLatestMaterializedAt:
    def test_returns_the_max_materialized_at_date(self):
        from datetime import date as _d
        assert latest_materialized_at(_warehouse_with_materialized(), "c") == _d(2026, 5, 3)

    def test_unknown_contract_is_none(self):
        assert latest_materialized_at(_warehouse_with_materialized(), "nope") is None


class TestObservedIssuetypes:
    def test_returns_distinct_sorted_issuetypes(self):
        # _warehouse has "Story", "Bug", "Task" for contract "c"
        types = observed_issuetypes(_warehouse(), "c")
        assert types == ["Bug", "Story", "Task"]

    def test_excludes_null_issuetypes(self):
        con = duckdb.connect(":memory:")
        con.execute(
            """CREATE TABLE work_items (
                contract_id VARCHAR, source VARCHAR, item_id VARCHAR,
                title VARCHAR, url VARCHAR,
                created_at TIMESTAMP, completed_at TIMESTAMP,
                cycle_time_days DOUBLE, issuetype VARCHAR
            )"""
        )
        con.execute(
            "INSERT INTO work_items VALUES ('c', 'github', '#1', 'a', NULL, NULL, NULL, NULL, 'Story')"
        )
        con.execute(
            "INSERT INTO work_items VALUES ('c', 'github', '#2', 'b', NULL, NULL, NULL, NULL, NULL)"
        )
        assert observed_issuetypes(con, "c") == ["Story"]

    def test_unknown_contract_is_empty(self):
        assert observed_issuetypes(_warehouse(), "nope") == []


class TestCreationsByDay:
    def test_returns_creations_by_day_for_contract(self):
        res = creations_by_day(_warehouse(), "c")
        assert res == [
            (date(2026, 1, 1), 2),
            (date(2026, 1, 2), 1),
            (date(2026, 1, 5), 1),
            (date(2026, 1, 6), 1),
        ]

    def test_filters_by_issuetypes(self):
        assert creations_by_day(_warehouse(), "c", issuetypes=["Story"]) == [
            (date(2026, 1, 1), 2),
        ]
        assert creations_by_day(_warehouse(), "c", issuetypes=["Bug"]) == [
            (date(2026, 1, 2), 1),
            (date(2026, 1, 5), 1),
        ]

    def test_empty_issuetypes_returns_empty(self):
        assert creations_by_day(_warehouse(), "c", issuetypes=[]) == []
