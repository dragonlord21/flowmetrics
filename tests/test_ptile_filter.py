"""Unit tests for `flowmetrics.charts.ptile_filter` — the
shared percentile-rank filter used by the cycle-time and aging
chart renders.

The single contract this module pins: rank assignment matches
DuckDB's `PERCENT_RANK()` semantics, so the chart's filter
result lines up with the SQL filter the table runs. Without
this, chart-vs-table counts drift whenever the underlying
data has ties (which they routinely do — `cycle_time = 1d` is
the dominant value for small PRs).
"""

from __future__ import annotations

from flowmetrics.charts.ptile_filter import PTILE_STOPS, filter_by_rank


class TestPtileStops:
    def test_ladder_is_zero_then_fives_from_fifty_to_hundred(self):
        # The two-handle slider snaps to these stops; PERCENTILE_CONT
        # in SQL targets the same set so the readout shows the
        # value at each ladder point.
        assert PTILE_STOPS == (
            0, 50, 55, 60, 65, 70, 75, 80, 85, 90, 95, 100,
        )


class TestFilterByRank:
    def test_empty_input_returns_empty_list(self):
        kept = filter_by_rank(
            [], key=lambda x: x, ptile_min=0, ptile_max=50,
        )
        assert kept == []

    def test_full_bound_keeps_every_item(self):
        items = [3, 1, 4, 1, 5, 9, 2, 6]
        kept = filter_by_rank(
            items, key=lambda x: x, ptile_min=0, ptile_max=100,
        )
        assert sorted(kept) == sorted(items)

    def test_ties_share_a_rank_so_a_zero_zero_bound_keeps_them_all(self):
        # Three tied lowest values + four distinct higher values.
        # SQL PERCENT_RANK puts all three ties at rank 0 → all three
        # qualify for ptile_min=0, ptile_max=0.
        items = ["a", "a", "a", "b", "c", "d", "e"]
        kept = filter_by_rank(
            items, key=lambda x: x, ptile_min=0, ptile_max=0,
        )
        assert kept == ["a", "a", "a"]

    def test_ties_share_a_rank_even_when_bound_excludes_them(self):
        # The three tied 'a's share rank 0; ptile_min=50 excludes
        # all three because their rank is 0, not 50.
        items = ["a", "a", "a", "b", "c", "d", "e"]
        kept = filter_by_rank(
            items, key=lambda x: x, ptile_min=50, ptile_max=100,
        )
        # Only b, c, d, e (rank 50, 67, 83, 100) qualify.
        assert kept == ["b", "c", "d", "e"]

    def test_upper_bound_drops_the_largest_values(self):
        # Without ties: 7 distinct values → ranks 0, 17, 33, 50,
        # 67, 83, 100. ptile_max=50 keeps the smaller four.
        items = list(range(7))
        kept = filter_by_rank(
            items, key=lambda x: x, ptile_min=0, ptile_max=50,
        )
        # Smallest four = 0, 1, 2, 3 (ranks 0, 17, 33, 50).
        assert sorted(kept) == [0, 1, 2, 3]

    def test_key_function_is_used_for_ranking(self):
        # Items with the SAME key value share a rank — even when
        # the items themselves are distinct. (Aging-style example:
        # two in-flight items with age=5 share a rank.)
        items = [
            ("itemA", 5), ("itemB", 5), ("itemC", 7), ("itemD", 9),
        ]
        kept = filter_by_rank(
            items, key=lambda t: t[1], ptile_min=0, ptile_max=0,
        )
        assert kept == [("itemA", 5), ("itemB", 5)]

    def test_single_item_assigns_rank_zero(self):
        # Edge case: n=1 → division-by-zero risk. The function
        # should assign rank 0 and keep the item under any bound
        # that includes 0.
        kept = filter_by_rank(
            [42], key=lambda x: x, ptile_min=0, ptile_max=100,
        )
        assert kept == [42]
        kept = filter_by_rank(
            [42], key=lambda x: x, ptile_min=50, ptile_max=100,
        )
        assert kept == []
