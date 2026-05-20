"""Component tests for `flowmetrics.web.components.forecast`.

Two forecast charts, both driven by Monte Carlo simulation over
the historical daily-throughput distribution:

  - "When will it be done?" (WWIBD-Date)
        Given N items remaining, what's the distribution of
        completion dates? P50/P85/P95 dates.
  - "How many will be done?" (WWIBD-How-Many)
        Given a window of D days, what's the distribution of
        items completed? P50/P85/P95 counts.

The simulation primitives in `flowmetrics.forecast` are already
fast (10K runs in <25ms for typical inputs). The component layer
wraps them with warehouse access, percentile extraction, and a
Vega-Lite spec.
"""

from __future__ import annotations

import json
import tempfile
import time
from datetime import date
from pathlib import Path

import duckdb
import pytest
import yaml
from click.testing import CliRunner

from flowmetrics.cli import cli
from flowmetrics.web.components.forecast import (
    render_how_many,
    render_when_done,
)

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


class TestWhenDone:
    def test_payload_carries_percentile_dates(self, warehouse):
        """For N items, the result names the P50/P85/P95
        completion dates (ascending — later percentiles are
        later dates)."""
        data = render_when_done(
            warehouse, "demo", items=20, start_date=date(2026, 5, 11)
        )
        assert data.items == 20
        assert data.p50_iso and data.p85_iso and data.p95_iso
        # Ascending — P95 takes longer than P50 in the worst case.
        assert data.p50_iso <= data.p85_iso <= data.p95_iso

    def test_percentile_dates_match_the_histograms_cumulative(
        self, warehouse
    ):
        """The payload's p50/p85/p95 must match what an empirical
        cumulative-distribution calculation produces from the
        same histogram. This catches the percentile-unit-mismatch
        bug where the call site passes 0.50/0.85/0.95 (fractions)
        to a function that expects 50/85/95 (percentages) — in
        that case the threshold becomes total*p/100 = 50 (not
        5000), every percentile crosses on the FIRST bin, and
        P50/P85/P95 all collapse to the same earliest date.

        The previous `p50_iso <= p85_iso <= p95_iso` assertion
        passes when all three are equal — it doesn't catch the
        collapse. This one verifies each percentile against the
        actual cumulative distribution.
        """
        data = render_when_done(
            warehouse, "demo", items=20, start_date=date(2026, 5, 11)
        )
        # Compute expected percentiles from the histogram.
        total = data.histogram_total
        running = 0
        expected = {}
        thresholds = {"p50": total * 0.50, "p85": total * 0.85,
                      "p95": total * 0.95}
        # Walk buckets ascending by date; pop thresholds as crossed.
        remaining = dict(thresholds)
        for bucket in data.histogram:
            running += bucket["count"]
            to_pop = []
            for name, th in remaining.items():
                if running >= th:
                    expected[name] = bucket["date_iso"]
                    to_pop.append(name)
            for n in to_pop:
                remaining.pop(n)
            if not remaining:
                break

        assert data.p50_iso == expected.get("p50"), (
            f"p50_iso ({data.p50_iso}) doesn't match the empirical "
            f"P50 from the histogram ({expected.get('p50')}). "
            f"Histogram: {[(b['date_iso'], b['count']) for b in data.histogram]}"
        )
        assert data.p85_iso == expected.get("p85"), (
            f"p85_iso ({data.p85_iso}) doesn't match empirical P85 "
            f"({expected.get('p85')}). Common cause: percentile "
            f"unit mismatch in the call site (0.85 vs 85)."
        )
        assert data.p95_iso == expected.get("p95"), (
            f"p95_iso ({data.p95_iso}) doesn't match empirical P95 "
            f"({expected.get('p95')})."
        )

    def test_payload_carries_histogram(self, warehouse):
        """The chart visualises the distribution as a histogram —
        count of runs that completed by each date offset."""
        data = render_when_done(
            warehouse, "demo", items=20, start_date=date(2026, 5, 11)
        )
        # At least one bucket; total across buckets equals runs.
        assert data.runs > 0
        assert data.histogram_total == data.runs
        # Each bucket: {date_iso, count}.
        assert all(
            "date_iso" in b and "count" in b for b in data.histogram
        )

    def test_headline_summarises_the_forecast(self, warehouse):
        data = render_when_done(
            warehouse, "demo", items=20, start_date=date(2026, 5, 11)
        )
        # Mentions items + at least P85 (the Vacanti commitment).
        assert "20" in data.headline
        assert "P85" in data.headline or "P50" in data.headline

    def test_chart_spec_uses_bar_marks(self, warehouse):
        data = render_when_done(
            warehouse, "demo", items=20, start_date=date(2026, 5, 11)
        )
        spec = json.loads(data.vega_spec_json())
        # The histogram is rendered as bars; percentile rules
        # layered on top.
        marks: list[str] = []

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
        assert "bar" in marks
        assert "rule" in marks

    def test_latency_under_200ms(self, warehouse):
        """User-pinned target: real-time slider response needs the
        end-to-end render under 200ms. The simulation alone is
        ~10-25ms; we have plenty of headroom for warehouse read +
        spec build. This benchmark guards against future
        regressions."""
        t = time.perf_counter()
        for _ in range(3):
            render_when_done(
                warehouse, "demo", items=50, start_date=date(2026, 5, 11)
            )
        elapsed_ms = (time.perf_counter() - t) / 3 * 1000
        assert elapsed_ms < 200, (
            f"render_when_done averaged {elapsed_ms:.1f}ms — over the "
            f"200ms real-time target. Profile the warehouse query or "
            f"the spec builder."
        )

    def test_chart_labels_each_percentile_line_inline(
        self, warehouse
    ):
        """User-reported: with the chart's legend hidden (legend
        info moved to the structured table above), the lines
        themselves carry no on-chart identifier. Add a text-mark
        layer that paints 'P50' / 'P85' / 'P95' near each rule
        so the chart is readable in isolation."""
        data = render_when_done(
            warehouse, "demo", items=20, start_date=date(2026, 5, 11)
        )
        spec = json.loads(data.vega_spec_json())
        # Find a text layer whose data carries the percentile labels.
        text_layers = []
        for layer in spec.get("layer", []):
            mark = layer.get("mark")
            mark_type = mark if isinstance(mark, str) else (
                mark.get("type") if isinstance(mark, dict) else None
            )
            if mark_type != "text":
                continue
            text_enc = layer.get("encoding", {}).get("text")
            if not text_enc:
                continue
            data_values = layer.get("data", {}).get("values", [])
            labels = [v.get("label") for v in data_values if isinstance(v, dict)]
            if {"P50", "P85", "P95"}.issubset(set(labels)):
                text_layers.append(layer)
        assert text_layers, (
            "expected a text-mark layer painting P50/P85/P95 "
            "labels on the chart so the rules are identifiable "
            "without the legend or external table"
        )

    def test_percentile_rules_anchor_at_band_center_not_edge(
        self, warehouse
    ):
        """User-reported bug: with the nominal x scale (band-based)
        rules default to anchoring at the band's LEFT EDGE, which
        means all three percentile lines land at the start of
        their date column — jammed against the y-axis if P50/P85/
        P95 all land on the first bar.

        Fix: `band: 0.5` on the rule's x encoding positions the
        rule at band CENTER, where the axis label sits and the
        viewer expects the column anchor.

        Only matters for the nominal-x When-Done chart; the
        quantitative-x How-Many chart doesn't have a band concept.
        """
        data = render_when_done(
            warehouse, "demo", items=20, start_date=date(2026, 5, 11)
        )
        spec = json.loads(data.vega_spec_json())
        rule_layer = next(
            layer for layer in spec["layer"]
            if isinstance(layer.get("mark"), dict)
            and layer["mark"].get("type") == "rule"
        )
        x = rule_layer["encoding"]["x"]
        assert x.get("band") == 0.5, (
            f"rule x must declare band:0.5 so it anchors at the "
            f"band CENTER (where the axis label sits); otherwise "
            f"rules cluster at the band's left edge. Got x={x!r}"
        )

    def test_x_axis_thins_labels_when_many_bars(self, warehouse):
        """User-reported bug: with many items the daily x-axis
        becomes a smear of overlapping dates (e.g. 'Jun 02Jun 03
        Jun 04...'). Vega-Lite's nominal axis won't auto-thin;
        we pre-pick a sparse `axis.values` list at spec time.

        The previous thinning used floor division (`n // 12`)
        which gave step=1 for 11-23 bars — i.e. NO thinning at
        the exact sizes the slider produces. Tighten the cap
        and use ceiling division. Pin: ≤ ~11 visible labels for
        any histogram length."""
        data = render_when_done(
            warehouse, "demo", items=200, start_date=date(2026, 5, 11)
        )
        if len(data.histogram) <= 10:
            pytest.skip("fixture didn't produce enough bins to need thinning")
        spec = json.loads(data.vega_spec_json())
        bar_layer = spec["layer"][0]
        x_axis = bar_layer["encoding"]["x"]["axis"]
        assert "values" in x_axis, (
            f"x-axis must declare explicit `axis.values` to thin "
            f"date labels when there are {len(data.histogram)} bars"
        )
        # ceil(n/10) step → at most n / step + 1 ≈ 11 labels
        assert 0 < len(x_axis["values"]) <= 11, (
            f"axis.values must be capped at ~10 visible labels; "
            f"got {len(x_axis['values'])} of {len(data.histogram)} bars"
        )

    def test_percentile_lines_have_distinct_stroke_dashes(
        self, warehouse
    ):
        """User-reported bug: P50/P85/P95 lines look identical and
        when two land on the same date one is invisible behind
        the other. Distinct dash patterns per percentile make
        each line visually distinguishable even when overlapping
        on the x-axis."""
        data = render_when_done(
            warehouse, "demo", items=20, start_date=date(2026, 5, 11)
        )
        spec = json.loads(data.vega_spec_json())
        # Find rule layer.
        rule_layer = next(
            layer for layer in spec["layer"]
            if isinstance(layer.get("mark"), dict)
            and layer["mark"].get("type") == "rule"
        )
        sd = rule_layer["encoding"].get("strokeDash")
        assert isinstance(sd, dict), (
            f"rule encoding must bind strokeDash to label so each "
            f"percentile gets a unique dash pattern; got "
            f"strokeDash={sd!r}"
        )
        scale = sd.get("scale")
        assert isinstance(scale, dict), "strokeDash needs a scale"
        ranges = scale.get("range")
        assert isinstance(ranges, list) and len(ranges) >= 3, (
            f"strokeDash scale.range must list ≥ 3 distinct dash "
            f"patterns (P50/P85/P95); got {ranges!r}"
        )
        # Each pattern must be unique.
        assert len({tuple(r) for r in ranges}) == len(ranges), (
            f"strokeDash patterns must be unique; got {ranges!r}"
        )

    def test_percentile_lines_offset_in_pixels_so_overlaps_visible(
        self, warehouse
    ):
        """If two percentiles land on the same date (common with
        small backlogs — P85 and P95 often coincide), the rules
        overlap pixel-perfectly and only one is visible. A small
        xOffset bound to label nudges each line a few pixels
        sideways so coincident rules separate visually."""
        data = render_when_done(
            warehouse, "demo", items=20, start_date=date(2026, 5, 11)
        )
        spec = json.loads(data.vega_spec_json())
        rule_layer = next(
            layer for layer in spec["layer"]
            if isinstance(layer.get("mark"), dict)
            and layer["mark"].get("type") == "rule"
        )
        offset = rule_layer["encoding"].get("xOffset")
        assert isinstance(offset, dict), (
            f"rule encoding must bind xOffset to label so coincident "
            f"percentile rules separate; got xOffset={offset!r}"
        )
        scale = offset.get("scale", {})
        ranges = scale.get("range")
        assert isinstance(ranges, list) and len(ranges) >= 3, (
            f"xOffset scale.range must give ≥ 3 distinct pixel "
            f"offsets (P50/P85/P95); got {ranges!r}"
        )
        assert len(set(ranges)) == len(ranges), (
            f"xOffset pixel offsets must be unique; got {ranges!r}"
        )

    def test_payload_carries_structured_percentile_rows(self, warehouse):
        """User-asked: a small table near the chart in consistent
        format (percentile column, forecast-value column) so the
        viewer can read P50/P85/P95 values structurally, not
        glued together in a prose headline.

        Pin: `WhenDoneData.percentile_rows` is a tuple of dicts
        with `label` (e.g. 'P50') and `value_display` (e.g.
        'May 29, 2026') for each of P50/P85/P95, in that order.
        """
        data = render_when_done(
            warehouse, "demo", items=20, start_date=date(2026, 5, 11)
        )
        rows = data.percentile_rows
        assert isinstance(rows, tuple) and len(rows) == 3, (
            f"percentile_rows must be a 3-tuple (P50/P85/P95); got {rows!r}"
        )
        labels = [r["label"] for r in rows]
        assert labels == ["P50", "P85", "P95"], (
            f"percentile_rows must be ordered P50→P85→P95; got {labels}"
        )
        for r in rows:
            assert "value_display" in r, f"row missing value_display: {r!r}"
            # For the date chart, value_display contains a month abbrev.
            assert any(
                m in r["value_display"] for m in [
                    "Jan", "Feb", "Mar", "Apr", "May", "Jun",
                    "Jul", "Aug", "Sep", "Oct", "Nov", "Dec",
                ]
            ), f"row {r['label']} value_display should be a date: {r!r}"


class TestHowMany:
    def test_payload_carries_percentile_counts(self, warehouse):
        """For a D-day window, the result names the P50/P85/P95
        completion counts (descending — later percentiles are
        smaller counts, since you're less likely to hit the
        higher target)."""
        data = render_how_many(
            warehouse,
            "demo",
            start_date=date(2026, 5, 11),
            end_date=date(2026, 6, 10),
        )
        assert data.days == 31
        # Vacanti convention: P50 ≥ P85 ≥ P95 on the count side
        # ("how many will I get done at LEAST?" — the high-
        # confidence count is lower).
        assert data.p50 >= data.p85 >= data.p95

    def test_percentile_counts_match_the_histograms_cumulative(
        self, warehouse
    ):
        """Same unit-mismatch guard as the When-Done chart, but
        for backward_percentile: with `p` passed as a probability
        (0.50) instead of a percentage (50), the threshold becomes
        total * 0.50 / 100 = 50 simulations and every percentile
        collapses to the highest count in the histogram.

        Pin each percentile against an empirical reverse-cumulative
        computation from the same histogram."""
        data = render_how_many(
            warehouse,
            "demo",
            start_date=date(2026, 5, 11),
            end_date=date(2026, 6, 10),
        )
        # Walk buckets HIGH → LOW; the smallest count where
        # cumulative-from-top crosses p% is the empirical Px.
        sorted_buckets = sorted(
            data.histogram, key=lambda b: b["count"], reverse=True
        )
        total = data.histogram_total
        running = 0
        expected: dict[str, int] = {}
        remaining = {"p50": total * 0.50, "p85": total * 0.85,
                     "p95": total * 0.95}
        for bucket in sorted_buckets:
            running += bucket["runs"]
            to_pop = []
            for name, th in remaining.items():
                if running >= th:
                    expected[name] = int(bucket["count"])
                    to_pop.append(name)
            for n in to_pop:
                remaining.pop(n)
            if not remaining:
                break

        assert data.p50 == expected.get("p50"), (
            f"p50 ({data.p50}) doesn't match empirical P50 floor "
            f"({expected.get('p50')}). Common cause: percentile-"
            f"unit mismatch in the call site (0.50 vs 50)."
        )
        assert data.p85 == expected.get("p85"), (
            f"p85 ({data.p85}) doesn't match empirical P85 floor "
            f"({expected.get('p85')})"
        )
        assert data.p95 == expected.get("p95"), (
            f"p95 ({data.p95}) doesn't match empirical P95 floor "
            f"({expected.get('p95')})"
        )

    def test_payload_carries_histogram(self, warehouse):
        data = render_how_many(
            warehouse,
            "demo",
            start_date=date(2026, 5, 11),
            end_date=date(2026, 6, 10),
        )
        assert data.runs > 0
        assert data.histogram_total == data.runs
        assert all(
            "count" in b and "runs" in b for b in data.histogram
        )

    def test_headline_summarises_the_forecast(self, warehouse):
        data = render_how_many(
            warehouse,
            "demo",
            start_date=date(2026, 5, 11),
            end_date=date(2026, 6, 10),
        )
        assert "31" in data.headline  # days in window
        assert "P85" in data.headline or "P50" in data.headline

    def test_payload_carries_structured_percentile_rows(self, warehouse):
        """Same structure as When-Done: a 3-tuple of (label,
        value_display) rows for the small percentile table. For
        How-Many the value is a count, displayed as '≥ N items'."""
        data = render_how_many(
            warehouse,
            "demo",
            start_date=date(2026, 5, 11),
            end_date=date(2026, 6, 10),
        )
        rows = data.percentile_rows
        assert isinstance(rows, tuple) and len(rows) == 3
        assert [r["label"] for r in rows] == ["P50", "P85", "P95"]
        for r in rows:
            assert "value_display" in r
            # Counts — value_display contains a number.
            assert any(c.isdigit() for c in r["value_display"]), (
                f"How-Many row {r['label']} value_display should "
                f"contain a count; got {r!r}"
            )

    def test_percentile_lines_have_distinct_stroke_dashes(
        self, warehouse
    ):
        """Same fix as When-Done: distinct dash patterns per
        percentile so coincident lines are still tellable apart."""
        data = render_how_many(
            warehouse,
            "demo",
            start_date=date(2026, 5, 11),
            end_date=date(2026, 6, 10),
        )
        spec = json.loads(data.vega_spec_json())
        rule_layer = next(
            layer for layer in spec["layer"]
            if isinstance(layer.get("mark"), dict)
            and layer["mark"].get("type") == "rule"
        )
        sd = rule_layer["encoding"].get("strokeDash")
        assert isinstance(sd, dict), (
            f"How-Many rule encoding must bind strokeDash to "
            f"label; got {sd!r}"
        )

    def test_percentile_lines_offset_in_pixels_so_overlaps_visible(
        self, warehouse
    ):
        data = render_how_many(
            warehouse,
            "demo",
            start_date=date(2026, 5, 11),
            end_date=date(2026, 6, 10),
        )
        spec = json.loads(data.vega_spec_json())
        rule_layer = next(
            layer for layer in spec["layer"]
            if isinstance(layer.get("mark"), dict)
            and layer["mark"].get("type") == "rule"
        )
        offset = rule_layer["encoding"].get("xOffset")
        assert isinstance(offset, dict), (
            f"How-Many rule encoding must bind xOffset to label "
            f"so coincident percentile rules separate; got {offset!r}"
        )

    def test_chart_spec_uses_bar_marks(self, warehouse):
        data = render_how_many(
            warehouse,
            "demo",
            start_date=date(2026, 5, 11),
            end_date=date(2026, 6, 10),
        )
        spec = json.loads(data.vega_spec_json())
        marks: list[str] = []

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
        assert "bar" in marks
        assert "rule" in marks

    def test_latency_under_200ms(self, warehouse):
        t = time.perf_counter()
        for _ in range(3):
            render_how_many(
                warehouse,
                "demo",
                start_date=date(2026, 5, 11),
                end_date=date(2026, 7, 11),  # 60-day window
            )
        elapsed_ms = (time.perf_counter() - t) / 3 * 1000
        assert elapsed_ms < 200, (
            f"render_how_many averaged {elapsed_ms:.1f}ms — over the "
            f"200ms real-time target."
        )
