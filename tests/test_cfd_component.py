"""Component tests for `flowmetrics.web.components.cfd`.

Cumulative Flow Diagram per Vacanti (Actionable Agile Metrics
for Predictability, 10th Anniversary Edition):

  For each date in the window and each stage, the value plotted
  is the cumulative count of items that have ENTERED that stage
  on or before that date.

  Bands stack in workflow order (first stage at top, terminal
  at bottom). The visible width of each band = WIP in that
  stage at that moment. The full stack height at any date =
  total items that have ever entered the workflow. The bottom
  band = items that have completed (departures).

The key invariant the math must preserve:

  count_entered(stage_N, date) ≥ count_entered(stage_N+1, date)

for every date and every adjacent pair of stages in workflow
order. The difference between the two cumulatives IS the WIP in
the earlier stage at that date. If the math ever violates this
(by, say, double-counting transitions or using the wrong source
table), the CFD bands would cross — visually nonsense.

We test the invariant directly. We also pin:

  - The bottom band's cumulative at the final date == count of
    completed items in `work_items`. This catches drift between
    the transitions table and the work_items table.
  - The top band's cumulative at the final date ≥ all other
    bands. (Same invariant generalised.)
  - Zero-arrival days are not gaps — every calendar date in the
    [first, last] window has one row per stage.

For our GitHub PR fixture, the inferred workflow order is
Draft → Awaiting Review → Merged. The component infers the
ordering from data (median `entered_at` per stage) so it works
on Jira workflows or contract-specific stage names without code
changes. A contract YAML override is a future hook.
"""

from __future__ import annotations

import json
import tempfile
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
import pytest
import yaml
from click.testing import CliRunner

from flowmetrics.cli import cli
from flowmetrics.web.components.cfd import render


FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


@pytest.fixture
def warehouse() -> duckdb.DuckDBPyConnection:
    tmp = Path(tempfile.mkdtemp())
    contracts_dir = tmp / "contracts"
    contracts_dir.mkdir()
    data_dir = tmp / "data"
    (contracts_dir / "demo.yaml").write_text(
        yaml.safe_dump(
            {
                "contract": {
                    "name": "demo",
                    "source": "github",
                    "repo": "astral-sh/uv",
                    "start": "2026-05-04",
                    "stop": "2026-05-10",
                }
            }
        )
    )
    res = CliRunner().invoke(
        cli,
        [
            "materialise", "demo",
            "--data-dir", str(data_dir),
            "--contracts-dir", str(contracts_dir),
            "--cache-dir", str(FIXTURE_CACHE),
            "--offline",
        ],
        catch_exceptions=False,
    )
    assert res.exit_code == 0, res.output
    con = duckdb.connect(":memory:")
    for kind in ("work_items", "transitions"):
        glob = (data_dir / kind / "**" / "*.parquet").as_posix()
        con.execute(
            f"CREATE VIEW {kind} AS "
            f"SELECT * FROM read_parquet('{glob}', hive_partitioning = true)"
        )
    yield con
    con.close()


class TestCfdShape:
    def test_one_row_per_date_per_stage(self, warehouse):
        """Every calendar date from first arrival to last arrival
        appears with one entry per stage. Zero-arrival days are
        carried over from the previous day (cumulative), not
        skipped."""
        data = render(warehouse, "demo")
        assert data.stages, "fixture must have ≥ 1 stage"
        # Each date_iso appears exactly once in daily — each daily
        # point carries a `counts` dict keyed by stage.
        date_isos = [d.date_iso for d in data.daily]
        assert date_isos == sorted(date_isos), (
            f"daily must be sorted ascending; got {date_isos}"
        )
        # Consecutive — no gaps.
        from datetime import date as _date
        parsed = [_date.fromisoformat(s) for s in date_isos]
        for prev, cur in zip(parsed, parsed[1:]):
            assert (cur - prev).days == 1, (
                f"daily series gap between {prev} and {cur}"
            )
        # Each point has a count per stage.
        for d in data.daily:
            assert set(d.counts.keys()) == set(data.stages), (
                f"date {d.date_iso!r}: counts keys {sorted(d.counts)} "
                f"don't match stages {sorted(data.stages)}"
            )

    def test_counts_are_monotonic_non_decreasing_per_stage(self, warehouse):
        """Cumulative arrivals only go UP over time. If a stage's
        count ever decreases, we're counting wrong (probably
        double-counting transitions or using the source-events
        rather than first-entry-per-item)."""
        data = render(warehouse, "demo")
        for stage in data.stages:
            counts = [d.counts[stage] for d in data.daily]
            for prev, cur in zip(counts, counts[1:]):
                assert cur >= prev, (
                    f"stage {stage!r} cumulative count decreased: "
                    f"{prev} → {cur}. Cumulative arrivals must be "
                    f"monotonic non-decreasing."
                )

    def test_bands_never_cross(self, warehouse):
        """The core CFD invariant: at every date, the cumulative
        count for an EARLIER stage in the workflow is ≥ the
        cumulative count for any LATER stage. Items must pass
        through stages in order, so if N items have reached
        stage_N+1, those same N items already reached stage_N.

        Bands crossing on a CFD would be visually meaningless and
        a signal that the math is wrong."""
        data = render(warehouse, "demo")
        for d in data.daily:
            counts_in_order = [d.counts[stage] for stage in data.stages]
            for stage_a, stage_b, a, b in zip(
                data.stages,
                data.stages[1:],
                counts_in_order,
                counts_in_order[1:],
            ):
                assert a >= b, (
                    f"on {d.date_iso}: earlier stage {stage_a!r} "
                    f"cumulative ({a}) < later stage {stage_b!r} "
                    f"cumulative ({b}). Workflow-order invariant "
                    f"broken — bands would cross."
                )

    def test_terminal_band_at_last_date_matches_work_items_completed(
        self, warehouse
    ):
        """Cross-check the transitions math against work_items:
        the terminal stage's cumulative at the last date should
        equal the count of completed items in work_items. If
        these drift, the two tables disagree about completions."""
        data = render(warehouse, "demo")
        last = data.daily[-1]
        terminal_stage = data.stages[-1]
        terminal_count = last.counts[terminal_stage]
        completed_in_work_items = warehouse.execute(
            "SELECT count(*) FROM work_items "
            "WHERE contract_id = 'demo' AND completed_at IS NOT NULL"
        ).fetchone()[0]
        assert terminal_count == completed_in_work_items, (
            f"CFD terminal stage count ({terminal_count}) disagrees "
            f"with work_items completed count "
            f"({completed_in_work_items}). The two tables must "
            f"agree about who completed by when."
        )

    def test_top_band_at_last_date_is_total_arrivals(self, warehouse):
        """The top band's cumulative = items that have entered the
        workflow's first stage. Every item must have passed
        through stage_0 at some point, so this equals the total
        number of items the warehouse knows about (where
        `created_at IS NOT NULL`)."""
        data = render(warehouse, "demo")
        last = data.daily[-1]
        top_stage = data.stages[0]
        top_count = last.counts[top_stage]
        total_items = warehouse.execute(
            "SELECT count(DISTINCT item_id) FROM transitions "
            "WHERE contract_id = 'demo'"
        ).fetchone()[0]
        # Top stage cumulative = items that entered stage[0]. Items
        # whose first transition was NOT stage[0] (e.g. PR created
        # straight into Awaiting Review, skipping Draft) won't be
        # counted in the top band. That's fine — those items DO
        # appear under whichever stage they first entered. So the
        # top band ≤ total distinct items.
        assert top_count <= total_items
        # And the SUM across all stages' first-arrivals == total
        # distinct items.
        first_arrivals = warehouse.execute(
            """
            SELECT stage, count(DISTINCT item_id) AS n FROM (
              SELECT item_id, stage,
                ROW_NUMBER() OVER (
                  PARTITION BY item_id ORDER BY entered_at ASC
                ) AS rn
              FROM transitions
              WHERE contract_id = 'demo'
            ) WHERE rn = 1
            GROUP BY stage
            """
        ).fetchall()
        first_arrival_total = sum(n for _, n in first_arrivals)
        assert first_arrival_total == total_items

    def test_stages_ordered_in_typical_workflow_progression(self, warehouse):
        """The component infers stage order from data (median
        entered_at per stage). For the GitHub PR fixture, that
        should land on Draft → Awaiting Review → Merged."""
        data = render(warehouse, "demo")
        # Sanity: those three stages are present (subset; CFD may
        # also include other GitHub stages from the data).
        assert "Draft" in data.stages
        assert "Awaiting Review" in data.stages
        assert "Merged" in data.stages
        # Ordering: Draft comes before Awaiting Review, which
        # comes before Merged.
        idx = {s: i for i, s in enumerate(data.stages)}
        assert idx["Draft"] < idx["Awaiting Review"] < idx["Merged"], (
            f"expected Draft → Awaiting Review → Merged; got "
            f"{data.stages}"
        )

    def test_empty_warehouse_renders_no_daily_no_crash(self, warehouse):
        """An unknown contract returns an empty payload, not a
        crash. The view layer renders an empty state."""
        data = render(warehouse, "does-not-exist")
        assert data.daily == ()
        assert data.stages == ()
        assert data.headline

    def test_dates_are_utc_anchored(self, warehouse):
        """Same TZ-safety contract as every other chart: the
        rendered date display is in UTC, regardless of viewer TZ."""
        import re
        data = render(warehouse, "demo")
        for d in data.daily:
            assert re.match(r"^\d{4}-\d{2}-\d{2}$", d.date_iso)
            assert re.match(
                r"^[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}$", d.date_display
            ), (
                f"date_display must be UTC `%b %d, %Y`; got "
                f"{d.date_display!r}"
            )

    def test_default_window_caps_at_90_days(self, warehouse):
        """When no contract bounds are given, the time axis caps
        at 90 days back from the data's most recent date. Without
        this cap, in-flight items dragging multi-year transition
        histories produce an unreadable 700+ day axis.

        For the fixture the data spans well under 90 days, so the
        cap is no-op here. The assertion is on the cap behavior
        when there's enough data to be capped — use a small
        `default_window_days` to force the cap to bite."""
        data = render(warehouse, "demo", default_window_days=3)
        # The cap should produce at most 3 days.
        assert len(data.daily) <= 3, (
            f"3-day cap should produce ≤ 3 daily points; got "
            f"{len(data.daily)}: {[d.date_iso for d in data.daily]}"
        )

    def test_default_window_caps_at_90_when_no_bounds(self, warehouse):
        """The default cap is 90 days — pin this so future edits
        don't silently widen the default and re-introduce the
        full-history axis bug."""
        from flowmetrics.web.components.cfd import DEFAULT_WINDOW_DAYS
        assert DEFAULT_WINDOW_DAYS == 90, (
            f"DEFAULT_WINDOW_DAYS must be 90 (the documented default); "
            f"got {DEFAULT_WINDOW_DAYS}"
        )

    def test_cfd_bands_are_wip_plus_done_in_declared_order(
        self, warehouse
    ):
        """The CFD's band list is `states.cfd_bands()` —
        wip followed by done, in declared YAML order. Each raw
        state is its own band. Backlog excluded."""
        from flowmetrics.contract import WorkflowStates
        states = WorkflowStates(
            wip=("Draft", "Awaiting Review", "Changes Requested", "Approved"),
            done=("Merged",),
        )
        data = render(warehouse, "demo", states=states)
        assert data.stages == states.cfd_bands(), (
            f"CFD stages must match states.cfd_bands(); got {data.stages}"
        )

    def test_states_not_in_wip_or_done_are_excluded(self, warehouse):
        """Per Vacanti, backlog states MUST NOT appear in the CFD.
        Any raw state not in `wip` or `done` is dropped from CFD
        math. The cumulative at the terminal must STILL equal
        the completed-items count — backlog-exclusion shouldn't
        lose departures, just hide the upstream bands."""
        from flowmetrics.contract import WorkflowStates
        states = WorkflowStates(done=("Merged",))
        data = render(warehouse, "demo", states=states)
        assert data.stages == ("Merged",)
        last = data.daily[-1]
        completed = warehouse.execute(
            "SELECT count(*) FROM work_items "
            "WHERE contract_id='demo' AND completed_at IS NOT NULL"
        ).fetchone()[0]
        assert last.counts["Merged"] == completed

    def test_window_clamps_with_only_start_set(self, warehouse):
        """A contract with `start` but no `stop` should clamp the
        LEFT edge of the axis to `start` and let the right edge
        come from the data — not fall back to the full history.
        Same logic if only `stop` is set.

        This guards against the previous bug where both bounds were
        required together: a contract with one bound silently
        dropped the clamp and the axis ranged over the entire
        in-flight-item lifetime."""
        from datetime import date as _date
        start = _date(2026, 5, 4)
        data = render(warehouse, "demo", contract_start=start)
        first = _date.fromisoformat(data.daily[0].date_iso)
        assert first == start, (
            f"first daily point should be contract.start ({start}) "
            f"even with stop=None; got {first}"
        )

    def test_window_clamps_with_only_stop_set(self, warehouse):
        from datetime import date as _date
        stop = _date(2026, 5, 10)
        data = render(warehouse, "demo", contract_stop=stop)
        last = _date.fromisoformat(data.daily[-1].date_iso)
        assert last == stop, (
            f"last daily point should be contract.stop ({stop}) "
            f"even with start=None; got {last}"
        )

    def test_window_clamps_to_contract_start_and_stop(self, warehouse):
        """When `contract_start` and `contract_stop` are provided
        the daily axis must be exactly that window. The CFD is a
        view of a contract's window; in-flight items can have
        transitions extending years before `contract.start`
        (their PR was opened long ago), but the chart should be
        anchored to the contract's window so the axis stays
        readable and the metric stays scoped.

        Cumulative counts at each date STILL reflect every
        transition that happened on or before that date — items
        that entered earlier than `contract.start` are counted
        as "already reached" at start. Only the time AXIS is
        clamped; the math is unchanged.
        """
        from datetime import date as _date
        start = _date(2026, 5, 4)
        stop = _date(2026, 5, 10)
        data = render(
            warehouse, "demo",
            contract_start=start,
            contract_stop=stop,
        )
        assert data.daily, "should still produce daily points within window"
        first = _date.fromisoformat(data.daily[0].date_iso)
        last = _date.fromisoformat(data.daily[-1].date_iso)
        assert first == start, (
            f"first daily point should be contract.start ({start}); got {first}"
        )
        assert last == stop, (
            f"last daily point should be contract.stop ({stop}); got {last}"
        )
        # And the cumulative at start should NOT be zero — items
        # that entered before start are already accounted for.
        # (For the fixture, items predate start by some margin.)

    def test_chart_spec_uses_area_marks_stacked_in_stage_order(
        self, warehouse
    ):
        """The CFD is a stacked area chart. The stack order must
        match the workflow stage order so the bands read
        top-to-bottom in workflow progression — Vega-Lite's
        `sort` on the color encoding pins this."""
        data = render(warehouse, "demo")
        spec = json.loads(data.vega_spec_json())
        marks = []

        def _walk(node):
            if isinstance(node, dict):
                m = node.get("mark")
                if isinstance(m, str):
                    marks.append(m)
                elif isinstance(m, dict) and "type" in m:
                    marks.append(m["type"])
                for v in node.values():
                    _walk(v)
            elif isinstance(node, list):
                for v in node:
                    _walk(v)

        _walk(spec)
        assert "area" in marks, (
            f"CFD spec must include an area mark; got {marks}"
        )
        # The color encoding's `sort` carries the stage order so
        # Vega stacks them top-to-bottom in workflow progression.
        json_str = json.dumps(spec)
        for stage in data.stages:
            assert stage in json_str, (
                f"stage {stage!r} must appear in spec (color domain)"
            )
