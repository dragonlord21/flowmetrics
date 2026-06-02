"""Behavioural spec for the rich text renderer.

We test substring contents — rich-formatting bytes vary by terminal width
and color settings, so we focus on what a human reader needs to see.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from flowmetrics.compute import FlowEfficiency, WindowResult
from flowmetrics.forecast import build_histogram
from flowmetrics.renderers import text_renderer
from flowmetrics.report import (
    EfficiencyInput,
    EfficiencyReport,
    HowManyInput,
    HowManyReport,
    Interpretation,
    SimulationSummary,
    WhenDoneInput,
    WhenDoneReport,
    build_training_summary,
)


def _interp():
    return Interpretation(
        headline="Portfolio FE is 12.3% — typical for knowledge work.",
        key_insight="Slowest PR dominates the ratio.",
        next_actions=["Inspect PR #99.", "Compare to last 4 weeks."],
        caveats=["Per-engineer use is harmful."],
    )


def _efficiency_report() -> EfficiencyReport:
    pr = FlowEfficiency(
        item_id="#99",
        title="Slow PR",
        created_at=datetime(2026, 5, 4, 9, 0, tzinfo=UTC),
        completed_at=datetime(2026, 5, 10, 9, 0, tzinfo=UTC),
        cycle_time=timedelta(days=6),
        active_time=timedelta(hours=12),
        efficiency=0.083,
    )
    return EfficiencyReport(
        input=EfficiencyInput("acme/widget", date(2026, 5, 4), date(2026, 5, 10), 4.0, 30.0, False),
        result=WindowResult(
            pr_count=1,
            portfolio_efficiency=0.083,
            mean_efficiency=0.083,
            median_efficiency=0.083,
            total_cycle=timedelta(days=6),
            total_active=timedelta(hours=12),
            per_pr=[pr],
        ),
        interpretation=_interp(),
    )


def _when_done_report() -> WhenDoneReport:
    return WhenDoneReport(
        input=WhenDoneInput(
            "acme/widget",
            50,
            date(2026, 5, 11),
            date(2026, 4, 11),
            date(2026, 5, 10),
            False,
        ),
        training=build_training_summary([5] * 4, date(2026, 5, 7), date(2026, 5, 10)),
        simulation=SimulationSummary(runs=10_000, seed=42),
        histogram=build_histogram([date(2026, 5, 19), date(2026, 5, 20)]),
        percentiles={
            50: date(2026, 5, 19),
            70: date(2026, 5, 19),
            85: date(2026, 5, 20),
            95: date(2026, 5, 20),
        },
        interpretation=_interp(),
    )


def _how_many_report() -> HowManyReport:
    return HowManyReport(
        input=HowManyInput(
            "acme/widget",
            date(2026, 5, 11),
            date(2026, 5, 25),
            date(2026, 4, 11),
            date(2026, 5, 10),
            False,
        ),
        training=build_training_summary([5] * 4, date(2026, 5, 7), date(2026, 5, 10)),
        simulation=SimulationSummary(runs=10_000, seed=42),
        histogram=build_histogram([50, 60, 70]),
        percentiles={50: 60, 70: 55, 85: 51, 95: 50},
        interpretation=_interp(),
    )


# ---------------------------------------------------------------------------


class TestAsciiSafeOutput:
    """Text output must survive being redirected through a non-UTF-8
    locale, piped into a clipboard, dropped into an email or chat
    that auto-decodes as latin-1, etc. Unicode arrows (→) get
    rendered as `â†'` mojibake under those conditions. HTML is
    safe because the page declares charset=utf-8; text isn't."""

    def test_no_unicode_arrows_in_terse_default(self):
        out = text_renderer.render(_efficiency_report())
        assert "→" not in out

    def test_no_unicode_arrows_in_verbose(self):
        for report_fn in (_efficiency_report, _when_done_report, _how_many_report):
            out = text_renderer.render(report_fn(), verbose=True)
            assert "→" not in out, (
                f"Verbose output of {report_fn.__name__} contains "
                f"a unicode arrow — text mode must use ASCII '->'."
            )

    def test_ascii_arrows_present_where_unicode_arrows_used_to_be(self):
        """Sanity: the date-range / workflow-chain prose still
        carries an arrow — just as ASCII '->' instead of '→'."""
        out = text_renderer.render(_when_done_report(), verbose=True)
        assert "->" in out


class TestEfficiencyText:
    """Verbose efficiency mirrors the Aging text workflow: headline +
    actionable numbers + slowest-PR table + reproduce. No more Key
    insight panel, no numbered Next actions list, no Vocabulary."""

    def test_contains_headline(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "Portfolio FE is 12.3%" in out

    def test_contains_repo_and_window(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "acme/widget" in out
        assert "2026-05-04" in out
        assert "2026-05-10" in out

    def test_contains_portfolio_fe_number(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "8.3%" in out

    def test_contains_per_pr_breakdown(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "#99" in out
        assert "Slow PR" in out

    def test_no_key_insight_panel(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "Key insight" not in out

    def test_no_next_actions_header(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "Next actions" not in out

    def test_no_vocabulary_block(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "Vocabulary used" not in out

    def test_no_what_this_shows_panel(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "What this shows" not in out

    def test_reproduce_command_present(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "uv run flow efficiency" in out


class TestWhenDoneText:
    def test_contains_headline(self):
        out = text_renderer.render(_when_done_report(), verbose=True)
        assert "Portfolio FE is 12.3%" in out  # the headline we passed in

    def test_contains_percentiles(self):
        out = text_renderer.render(_when_done_report(), verbose=True)
        assert "50" in out and "85" in out and "95" in out
        assert "2026-05-19" in out
        assert "2026-05-20" in out

    def test_contains_training_summary(self):
        out = text_renderer.render(_when_done_report(), verbose=True)
        assert "Training" in out or "training" in out
        # 5/day * 4 days = 20 total
        assert "20" in out

    def test_does_not_include_ascii_histogram(self):
        out = text_renderer.render(_when_done_report(), verbose=True)
        assert "########" not in out

    def test_no_key_insight_panel(self):
        out = text_renderer.render(_when_done_report(), verbose=True)
        assert "Key insight" not in out

    def test_no_next_actions_header(self):
        out = text_renderer.render(_when_done_report(), verbose=True)
        assert "Next actions" not in out

    def test_no_vocabulary_block(self):
        out = text_renderer.render(_when_done_report(), verbose=True)
        assert "Vocabulary used" not in out


class TestHowManyText:
    def test_contains_percentiles_with_items(self):
        out = text_renderer.render(_how_many_report(), verbose=True)
        # 85% confidence should show 51 items (backward percentile)
        assert "51" in out
        assert "50" in out

    def test_no_key_insight_panel(self):
        out = text_renderer.render(_how_many_report(), verbose=True)
        assert "Key insight" not in out

    def test_no_next_actions_header(self):
        out = text_renderer.render(_how_many_report(), verbose=True)
        assert "Next actions" not in out

    def test_no_vocabulary_block(self):
        out = text_renderer.render(_how_many_report(), verbose=True)
        assert "Vocabulary used" not in out


class TestTerseDefault:
    """Default text output is one-line: just the headline answer.
    Full report is opt-in via verbose=True."""

    def test_default_render_is_a_single_line(self):
        out = text_renderer.render(_efficiency_report())
        lines = [line for line in out.strip().splitlines() if line.strip()]
        assert len(lines) == 1, f"expected 1 line, got {len(lines)}: {lines}"
        assert "Portfolio FE" in lines[0] or "flow efficiency" in lines[0].lower()

    def test_default_render_does_not_include_input_block(self):
        out = text_renderer.render(_efficiency_report())
        assert "Repo" not in out
        assert "Reproduce" not in out
        assert "Vocabulary" not in out

    def test_verbose_render_includes_full_detail(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert "Repo" in out
        assert "Reproduce" in out
        # Key insight is now carried by the headline panel — no separate
        # "Key insight" sub-panel any more.
        assert "Portfolio FE" in out

    def test_terse_when_done_is_one_line(self):
        out = text_renderer.render(_when_done_report())
        lines = [line for line in out.strip().splitlines() if line.strip()]
        assert len(lines) == 1
        # Terse output IS the report's headline string, modulo ASCII-
        # safe substitution of unicode arrows / dashes (text mode
        # writes mojibake-resistant output).
        expected = _interp().headline.replace("→", "->").replace("—", "--")
        assert lines[0] == expected


class TestAnswerFirstOrdering:
    """Text output mirrors HTML: headline → definition → key numbers →
    key insight → next actions → caveats — then detail (input + repro)."""

    def test_definition_appears_before_input(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        # The definition mentions "active" (efficiency) or "Monte Carlo" (forecasts)
        assert "active" in out.lower()
        # And it appears before the input/parameters block, not after
        i_def = out.lower().index("active")
        # "Repo" appears in the input table at the bottom (per new layout)
        assert "Repo" in out
        i_repo = out.index("Repo")
        assert i_def < i_repo

    def test_headline_appears_before_input(self):
        # The headline carries the insight — it must come before the
        # detail block. (No more separate "Key insight" panel.)
        out = text_renderer.render(_efficiency_report(), verbose=True)
        i_headline = out.index("Portfolio FE")
        i_repo = out.index("Repo")
        assert i_headline < i_repo


class TestNoEmptyOutput:
    def test_efficiency_render_is_substantial(self):
        out = text_renderer.render(_efficiency_report(), verbose=True)
        assert len(out) > 200  # not just a header

    def test_when_done_render_is_substantial(self):
        out = text_renderer.render(_when_done_report(), verbose=True)
        assert len(out) > 200

