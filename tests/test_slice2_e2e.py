"""Slice 2 acceptance: `flow serve` renders a working cycle-time
dashboard + matching detail page, in a real browser.

The slice 2 click-path (from docs/SPEC-warehouse-app.md §15 and the
spec-driven session leading up to it):

  > Run `flow materialise astral-uv-week` (Slice 1, already works).
  > Run `flow serve --port 8000 --host 127.0.0.1 --data-dir … --contracts-dir …`.
  > Open http://127.0.0.1:8000/. See:
  >   - Sticky filter bar (decorative in Slice 2).
  >   - Anchored #cycle-time section with a Vega-Lite scatterplot,
  >     P50 + P85 reference lines, "Details →" link.
  >   - 43 data points (one per merged PR from the fixture window).
  >   - Hover shows tooltip with title + cycle-time-days.
  >   - Drag-zoom changes the x domain; double-click resets.
  >   - "Details →" navigates to /metrics/cycle-time with the same
  >     chart full-size + placeholder sections for "How to read",
  >     "Caveats", "Methodology", "Actions".
  >   - `--host 0.0.0.0` without `--password` exits with a clear error.

Per SPEC.md §6 (test credibility rule) Slice 2 acceptance must be
e2e: Playwright drives a real Chromium against a real FastAPI
process; assertions name what the user sees (rendered SVG, axis
labels, on-page text) rather than internal route shapes or JSON
payloads. "The div exists" is not enough — the chart must actually
draw, the data must actually appear, the interaction must actually
work.

Slow tests are opt-in via `-m e2e`. Default pytest run skips this
file. To run: `uv run pytest -m e2e tests/test_slice2_e2e.py`.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from pathlib import Path

import pytest
import uvicorn
import yaml
from click.testing import CliRunner
from playwright.sync_api import Page, expect

from flowmetrics.cli import cli

pytestmark = pytest.mark.e2e

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


# ---------------------------------------------------------------------------
# Server fixture: in-thread uvicorn against pre-materialised Parquet
# ---------------------------------------------------------------------------


def _free_port() -> int:
    """Find an unused TCP port on 127.0.0.1."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ServerThread(threading.Thread):
    """uvicorn server in a daemon thread; supports graceful shutdown.

    pytest-playwright needs a real bound TCP port the browser can hit;
    FastAPI TestClient is in-process only. This fixture starts uvicorn
    on a free port and tears it down at test end.
    """

    def __init__(self, app, port: int):
        super().__init__(daemon=True)
        config = uvicorn.Config(
            app, host="127.0.0.1", port=port, log_level="error", access_log=False
        )
        self.server = uvicorn.Server(config)

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


@pytest.fixture(scope="module")
def server_url(tmp_path_factory):
    """Set up a fresh data dir with materialised fixture data, then
    serve via uvicorn in a daemon thread. Yields the base URL.
    """
    from flowmetrics.app import create_app
    from flowmetrics.cli import cli as _cli

    tmp_path = tmp_path_factory.mktemp("slice2")
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    data_dir = tmp_path / "data"
    name = "astral-uv-week"

    # Materialise once via the public CLI so this also exercises the
    # Slice 1 path end-to-end.
    contract_yaml = {
        "contract": {
            "name": name,
            "source": "github",
            "repo": "astral-sh/uv",
            "start": "2026-05-04",
            "stop": "2026-05-10",
        }
    }
    (contracts_dir / f"{name}.yaml").write_text(yaml.safe_dump(contract_yaml))
    res = CliRunner().invoke(
        _cli,
        [
            "materialise",
            name,
            "--data-dir",
            str(data_dir),
            "--contracts-dir",
            str(contracts_dir),
            "--cache-dir",
            str(FIXTURE_CACHE),
            "--offline",
        ],
        catch_exceptions=False,
    )
    assert res.exit_code == 0, f"fixture materialise failed: {res.output}"

    app = create_app(data_dir=data_dir, contracts_dir=contracts_dir)
    port = _free_port()
    thread = _ServerThread(app, port)
    thread.start()

    # Wait for server to come up
    for _ in range(50):
        with (
            contextlib.suppress(OSError),
            socket.create_connection(("127.0.0.1", port), timeout=0.2),
        ):
            break
        time.sleep(0.1)
    else:
        thread.stop()
        raise RuntimeError("uvicorn did not start in time")

    yield f"http://127.0.0.1:{port}"

    thread.stop()
    thread.join(timeout=3)


# ---------------------------------------------------------------------------
# Tests — drive a real browser, assert what the user sees
# ---------------------------------------------------------------------------


class TestDashboardCycleTimeTile:
    def test_dashboard_renders_vega_svg(self, server_url: str, page: Page):
        page.goto(server_url + "/")
        # The Vega-Lite chart embeds inside a div with id cycle-time-tile.
        # Wait for the SVG to actually draw, not just for the container div.
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        expect(page.locator("#cycle-time-tile svg")).to_be_visible()

    def test_dashboard_chart_axes_say_completion_date_and_cycle_time(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        # Vega-Lite labels the axes from spec; assert the labels are
        # the human-meaningful ones the slice promised.
        chart_text = page.locator("#cycle-time-tile").inner_text()
        assert "Cycle time" in chart_text, (
            f"y-axis should label cycle time; chart text was:\n{chart_text}"
        )
        assert "Completion date" in chart_text or "Completed" in chart_text, (
            f"x-axis should label completion date; chart text was:\n{chart_text}"
        )

    def test_dashboard_shows_p50_and_p85_reference_lines_with_values(
        self, server_url: str, page: Page
    ):
        """User-visible signal: the two reference lines labelled with
        their numeric values appear IN the chart SVG, not just in the
        surrounding page chrome.

        The earlier version of this test asserted P50/P85 in the
        component's `inner_text` — which includes the headline
        ("43 items completed · P50 0.1d · P85 1.4d") even when the
        chart itself draws neither line nor label. That was a false
        positive: passing tests, broken chart. Fixed here by reading
        only SVG <text> nodes via page.evaluate (Playwright's
        inner_text doesn't extract SVG text reliably).
        """
        page.goto(server_url + "/")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        page.wait_for_timeout(500)  # let Vega finish drawing
        svg_texts: list[str] = page.evaluate(
            """() => Array.from(
                document.querySelectorAll('#cycle-time-tile svg text')
            ).map(t => t.textContent)"""
        )
        # The text-mark layer renders "P50 (X.Xd)" / "P85 (X.Xd)" as
        # SVG <text> nodes. Assert each appears at least once.
        assert any("P50" in t for t in svg_texts), (
            f"P50 label missing from SVG; svg texts were: {svg_texts}"
        )
        assert any("P85" in t for t in svg_texts), (
            f"P85 label missing from SVG; svg texts were: {svg_texts}"
        )

    def test_dashboard_renders_at_least_one_data_point(
        self, server_url: str, page: Page
    ):
        """Forty-three PRs in the fixture window. The chart MUST render
        marks (not just axes). Test the chart is non-empty by counting
        rendered point marks."""
        page.goto(server_url + "/")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        # Vega-Lite renders point marks as <path> or <circle> under
        # the .mark-symbol class. Empty SVG = bug.
        n_marks = page.locator("#cycle-time-tile .mark-symbol path").count()
        assert n_marks >= 1, (
            "chart rendered 0 data points — Parquet query or Vega data binding broken"
        )

    def test_dashboard_has_details_link_to_detail_page(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/")
        link = page.locator("#cycle-time-tile a:has-text('Details')")
        expect(link).to_be_visible()
        href = link.get_attribute("href")
        assert href is not None and "/metrics/cycle-time" in href, (
            f"Details link href={href!r} — expected /metrics/cycle-time"
        )

    def test_xaxis_date_labels_are_unique(self, server_url: str, page: Page):
        """User-reported bug: x-axis was rendering "May 05 May 05
        May 05 May 05 May 06 May 06 …" — each date label appearing
        four times because Vega-Lite auto-picked sub-day tick
        positions for a 7-day window. The user expects DISTINCT date
        labels along the x-axis.

        Assertion: among SVG <text> nodes that look like date labels
        (match the "%b %d" format), no value appears more than once.
        """
        import re

        page.goto(server_url + "/")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        page.wait_for_timeout(500)
        svg_texts: list[str] = page.evaluate(
            """() => Array.from(
                document.querySelectorAll('#cycle-time-tile svg text')
            ).map(t => t.textContent)"""
        )
        date_pattern = re.compile(r"^[A-Z][a-z]{2}\s+\d{1,2}$")
        date_labels = [t for t in svg_texts if t and date_pattern.match(t)]
        # We expect at least a few unique dates (window is a week).
        assert date_labels, (
            f"no date-shaped labels on x-axis; svg texts: {svg_texts}"
        )
        from collections import Counter
        counts = Counter(date_labels)
        dups = {label: n for label, n in counts.items() if n > 1}
        assert not dups, (
            f"x-axis has duplicated date labels: {dups}. "
            f"All date labels: {date_labels}"
        )


class TestDetailPageCycleTime:
    def test_detail_page_renders_same_chart_full_size(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/contracts/astral-uv-week/metrics/cycle-time")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        # The detail page uses the same partial in 'detail' mode; the
        # underlying SVG must still render.
        expect(page.locator("#cycle-time-tile svg")).to_be_visible()

    def test_detail_page_has_placeholder_sections(
        self, server_url: str, page: Page
    ):
        """Per SPEC §7.3.3 the detail page reserves space for four
        sections below the tile. Stubs in Slice 2; populated later.
        Assert the section headers are present so the slot exists.
        """
        page.goto(server_url + "/contracts/astral-uv-week/metrics/cycle-time")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        # Section H2s render UPPERCASE per Knox eyebrow style;
        # compare case-insensitively so the assertion survives
        # display-case changes.
        text = page.locator("body").inner_text().lower()
        assert "how to read" in text, f"missing 'How to read' section"
        assert "caveats" in text, "missing 'Caveats' section"
        assert "methodology" in text, "missing 'Methodology' section"
        assert "actions" in text, "missing 'Actions' section"

    def test_detail_page_shows_metric_summary_above_tile(
        self, server_url: str, page: Page
    ):
        """The detail page introduces itself with the same
        composable `metric-summary` component the dashboard uses
        (title + headline above the chart tile). Replaces the
        old standalone <h1>page-title pattern with the reusable
        component pattern."""
        page.goto(server_url + "/contracts/astral-uv-week/metrics/cycle-time")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        summary = page.locator(".metric-summary")
        expect(summary).to_be_visible()
        text = summary.inner_text()
        assert "Cycle time" in text, (
            f"detail page metric-summary must name the metric; got {text!r}"
        )
        assert "P50" in text and "P85" in text and "P95" in text, (
            f"detail page metric-summary must show the percentiles; got {text!r}"
        )


class TestContractScopedUrls:
    """User-reported feature gap: the dashboard URL must encode the
    contract id so the system is multi-contract-ready by URL shape.
    `/metrics/cycle-time` (the singular form) is no longer the
    canonical URL; the canonical form is
    `/contracts/{contract_id}/metrics/cycle-time`.

    These tests pin the new URL shape. They will require the routes
    in flowmetrics/app.py to be reshaped.
    """

    def test_contract_scoped_dashboard_url_returns_200_with_chart(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/contracts/astral-uv-week/")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        expect(page.locator("#cycle-time-tile svg")).to_be_visible()

    def test_contract_scoped_metric_detail_url_returns_200_with_chart(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/contracts/astral-uv-week/metrics/cycle-time")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        expect(page.locator("#cycle-time-tile svg")).to_be_visible()

    def test_dashboard_details_link_uses_contract_scoped_url(
        self, server_url: str, page: Page
    ):
        """The Details → link on the dashboard tile must point at
        the contract-scoped URL, not the singular legacy form."""
        page.goto(server_url + "/contracts/astral-uv-week/")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        link = page.locator("#cycle-time-tile a:has-text('Details')")
        href = link.get_attribute("href")
        assert href is not None and href.startswith(
            "/contracts/astral-uv-week/metrics/cycle-time"
        ), (
            f"Details link should point at /contracts/<id>/metrics/cycle-time; "
            f"got {href!r}"
        )

    def test_unknown_contract_returns_404(self, server_url: str, page: Page):
        response = page.request.get(server_url + "/contracts/does-not-exist/")
        assert response.status == 404, (
            f"expected 404 for unknown contract; got {response.status}"
        )


class TestTooltipDateMatchesDataAcrossTimezones:
    """The tooltip's "Completed" value MUST be the same string for
    every viewer regardless of their browser timezone — it shows
    the UTC calendar date that the data carries.

    Vega-Lite's `type: temporal` tooltip with `format: "%b %d, %Y"`
    formats in browser-local time, which shifts UTC dates by the
    viewer's TZ offset. A PT user sees "May 03" for a UTC May 04
    dot — exactly the off-by-one bug reported. Fix: pre-format the
    date in Python and pass as `type: nominal`, so the rendered
    string is TZ-invariant.
    """

    def _tooltip_for_first_dot(self, page: Page, server_url: str) -> str:
        page.goto(server_url + "/contracts/astral-uv-week/")
        page.wait_for_selector("#cycle-time-tile svg")
        page.wait_for_timeout(800)
        dot = page.locator("#cycle-time-tile svg .mark-symbol path").first
        bbox = dot.bounding_box()
        assert bbox is not None
        page.mouse.move(
            bbox["x"] + bbox["width"] / 2,
            bbox["y"] + bbox["height"] / 2,
        )
        page.wait_for_timeout(500)
        return page.evaluate(
            "() => document.querySelector('#vg-tooltip-element')?.innerText || ''"
        )

    def test_tooltip_completion_date_is_utc_invariant(
        self, server_url: str, browser
    ):
        """Open the dashboard in two contexts — one PT (UTC-7), one
        UTC — and hover the same dot. The tooltip's Completed value
        must read identically. Currently it differs by a day."""
        pt_ctx = browser.new_context(timezone_id="America/Los_Angeles")
        utc_ctx = browser.new_context(timezone_id="UTC")
        try:
            pt_tt = self._tooltip_for_first_dot(pt_ctx.new_page(), server_url)
            utc_tt = self._tooltip_for_first_dot(utc_ctx.new_page(), server_url)
        finally:
            pt_ctx.close()
            utc_ctx.close()

        # Pull the date string that follows the "Completed" label.
        # Vega's tooltip renders as "Label<tab><newline>Value<newline>".
        def _completed_date(text: str) -> str:
            lines = text.splitlines()
            for i, line in enumerate(lines):
                if line.startswith("Completed"):
                    return lines[i + 1].strip() if i + 1 < len(lines) else ""
            return ""

        pt_date = _completed_date(pt_tt)
        utc_date = _completed_date(utc_tt)
        assert pt_date and utc_date, (
            f"could not find Completed date in tooltip. "
            f"PT raw: {pt_tt!r}; UTC raw: {utc_tt!r}"
        )
        assert pt_date == utc_date, (
            f"tooltip 'Completed' must be the same date regardless of "
            f"browser TZ. PT: {pt_date!r}; UTC: {utc_date!r}"
        )


class TestDotsClusterInTheirDateColumn:
    """User-stated mental model: a dot labelled "May 04" lives
    BETWEEN the May 04 tick and the May 05 tick — strictly to the
    right of its tick label, in the [tick, tick+1) band. Tests pin
    this convention against any future "let's center the jitter"
    drift.
    """

    def test_dot_x_is_to_the_right_of_its_date_tick(
        self, server_url: str, page: Page
    ):
        """For the leftmost (earliest) dot, its center x must be
        >= the x of the matching axis tick label and < the x of
        the following tick label."""
        page.goto(server_url + "/contracts/astral-uv-week/")
        page.wait_for_selector("#cycle-time-tile svg")
        page.wait_for_timeout(800)

        # Read all date-shaped tick labels with their x positions.
        import re

        labels = page.evaluate(
            """() => Array.from(document.querySelectorAll(
                '#cycle-time-tile svg .role-axis-label text'
            )).map(t => {
                const bb = t.getBoundingClientRect();
                return {text: t.textContent, x: bb.x + bb.width/2};
            })"""
        )
        date_re = re.compile(r"^[A-Z][a-z]{2}\s+\d{1,2}$")
        dated = sorted(
            [
                lbl for lbl in labels
                if date_re.match(lbl["text"])
            ],
            key=lambda lbl: lbl["x"],
        )
        assert len(dated) >= 3, f"expected >= 3 date labels; got {dated}"

        # Hover the leftmost dot, read its tooltip date.
        dot = page.locator("#cycle-time-tile svg .mark-symbol path").first
        bbox = dot.bounding_box()
        assert bbox is not None
        dot_cx = bbox["x"] + bbox["width"] / 2
        page.mouse.move(dot_cx, bbox["y"] + bbox["height"] / 2)
        page.wait_for_timeout(500)
        tooltip = page.evaluate(
            "() => document.querySelector('#vg-tooltip-element')?.innerText || ''"
        )
        # Pull the tooltip date — e.g., "May 04, 2026"
        m = re.search(r"([A-Z][a-z]{2})\s+(\d{1,2}),\s+\d{4}", tooltip)
        assert m, f"tooltip missing Completed date; got {tooltip!r}"
        # Zero-pad day so the string matches axis-label form ("May 03",
        # not "May 3").
        tooltip_label = f"{m.group(1)} {int(m.group(2)):02d}"

        # Find that label's x; assert dot_cx is in [tick_x, next_tick_x).
        idx = next(
            (i for i, lbl in enumerate(dated) if lbl["text"] == tooltip_label),
            None,
        )
        assert idx is not None, (
            f"tooltip date {tooltip_label!r} not found in axis labels "
            f"{[lbl['text'] for lbl in dated]}"
        )
        tick_x = dated[idx]["x"]
        next_tick_x = dated[idx + 1]["x"] if idx + 1 < len(dated) else None
        assert dot_cx >= tick_x - 1, (
            f"dot at x={dot_cx:.1f} is LEFT of its tick label "
            f"{tooltip_label!r} (x={tick_x:.1f}). User's column "
            f"convention: dots for May DD live between May DD and "
            f"May DD+1 tick — strictly to the right of their label."
        )
        if next_tick_x is not None:
            assert dot_cx < next_tick_x, (
                f"dot at x={dot_cx:.1f} reaches or exceeds the NEXT "
                f"tick {dated[idx+1]['text']!r} (x={next_tick_x:.1f}). "
                f"Jitter must keep dots strictly inside their band."
            )


class TestMetricSummaryAboveChart:
    """The headline (count + percentiles) is its own component
    above the chart, not embedded inside the chart tile. Same
    pattern reused on every future metric page.
    """

    def test_dashboard_summary_appears_above_chart_tile(
        self, server_url: str, page: Page
    ):
        """Two assertions: the headline text exists on the page,
        AND its position in the DOM is BEFORE the chart-tile
        section. 'Above' is structural, not just visual."""
        page.goto(server_url + "/contracts/astral-uv-week/")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        summary = page.locator(".metric-summary")
        expect(summary).to_be_visible()
        # The summary's headline text is the count+percentiles.
        text = summary.inner_text()
        assert "43 items completed" in text, (
            f"summary must show the item count; got {text!r}"
        )
        assert "P50" in text and "P85" in text and "P95" in text, (
            f"summary must show P50/P85/P95; got {text!r}"
        )
        # Structural position: summary precedes the chart tile.
        ordering = page.evaluate(
            """() => {
                const summ = document.querySelector('.metric-summary');
                const tile = document.querySelector('#cycle-time-tile');
                if (!summ || !tile) return null;
                return summ.compareDocumentPosition(tile) & Node.DOCUMENT_POSITION_FOLLOWING ? 'summary-first' : 'tile-first';
            }"""
        )
        assert ordering == "summary-first", (
            f"summary must appear BEFORE chart tile in DOM order; got {ordering}"
        )

    def test_headline_is_not_inside_the_chart_tile(
        self, server_url: str, page: Page
    ):
        """The 'metric-headline' (the percentile statement) belongs
        to the summary component above the chart, not to the chart
        tile itself. Tile shows the chart; summary shows the
        numbers."""
        page.goto(server_url + "/contracts/astral-uv-week/")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        # No headline inside the tile.
        inner = page.locator("#cycle-time-tile .metric-headline").count()
        assert inner == 0, (
            "the percentile headline must NOT live inside the chart "
            "tile (#cycle-time-tile). It belongs to the .metric-summary "
            "component above the tile."
        )


class TestDetailPageNoSubtitleNoise:
    """Earlier slice shipped a subtitle "Per-item cycle time over the
    window…" on the detail page. The user reports it's not useful.
    Remove it.
    """

    def test_per_item_subtitle_is_removed(self, server_url: str, page: Page):
        page.goto(server_url + "/contracts/astral-uv-week/metrics/cycle-time")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        body = page.locator("body").inner_text()
        assert "Per-item cycle time over the window" not in body, (
            "the dead 'Per-item cycle time over the window…' subtitle "
            "should be gone — it adds noise without explaining anything."
        )


class TestDetailPageHeader:
    """The metric name lives in the site header on detail pages —
    `flowmetrics · <contract> · <metric>` — so the page identifies
    itself at the top of the viewport, not buried in the metric
    summary section halfway down. Dashboard pages don't show a
    metric name in the header (they're multi-metric).
    """

    def test_detail_page_site_header_contains_metric_name(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/contracts/astral-uv-week/metrics/cycle-time")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        header_text = page.locator(".site-header").inner_text()
        assert "Cycle time" in header_text, (
            f"detail page site header must include the metric name; "
            f"got {header_text!r}"
        )
        # Contract name still present.
        assert "astral-uv-week" in header_text, (
            f"detail page site header must still include the contract "
            f"name; got {header_text!r}"
        )

    def test_dashboard_site_header_does_not_carry_a_metric_name(
        self, server_url: str, page: Page
    ):
        """The dashboard shows multiple metrics; pinning one name to
        the header would be wrong. The contract name is what
        identifies the page."""
        page.goto(server_url + "/contracts/astral-uv-week/")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        header_text = page.locator(".site-header").inner_text()
        assert "Cycle time" not in header_text, (
            f"dashboard site header must NOT carry a metric name "
            f"(the dashboard is multi-metric); got {header_text!r}"
        )


class TestSiteBrandLink:
    """The "flowmetrics" brand text in the site header is a link to
    the home page (`/`). Standard web convention; lets the user
    bounce back to the contract selector / dashboard from any
    detail or lifecycle page without using the browser back button.
    """

    def test_brand_is_an_anchor_to_root(self, server_url: str, page: Page):
        page.goto(server_url + "/contracts/astral-uv-week/")
        page.wait_for_selector(".site-header", timeout=10000)
        brand = page.locator(".site-header .brand")
        # The .brand element must BE an <a> tag (not a span wrapping
        # an <a>); the whole brand mark is the click target.
        tag = page.evaluate(
            "() => document.querySelector('.site-header .brand')?.tagName"
        )
        assert tag == "A", (
            f".site-header .brand must be an <a>; got tag {tag!r}"
        )
        href = brand.get_attribute("href")
        assert href == "/", (
            f"brand link must href='/' so it routes to the contract "
            f"redirect; got {href!r}"
        )
        # Text content unchanged.
        assert "flowmetrics" in brand.inner_text().lower()


class TestResetButton:
    """Each chart tile has a reset button that re-loads only the
    chart fragment via HTMX. Use case: re-shuffle jitter, pick up
    new data after an ETL run, etc.
    """

    def test_tile_has_reset_button_with_hx_get(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/contracts/astral-uv-week/")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        btn = page.locator("#cycle-time-tile button.reset-btn").first
        expect(btn).to_be_visible()
        hx_get = btn.get_attribute("hx-get")
        assert hx_get and "/api/internal/cycle-time" in hx_get, (
            f"reset button must use HTMX hx-get pointing at the "
            f"chart fragment endpoint; got hx-get={hx_get!r}"
        )
        hx_target = btn.get_attribute("hx-target")
        assert hx_target == "#cycle-time-chart", (
            f"reset button must target #cycle-time-chart; got {hx_target!r}"
        )

    def test_fragment_endpoint_returns_chart_only_no_chrome(
        self, server_url: str, page: Page
    ):
        """The fragment URL must return JUST the chart + script —
        no page header, no filter bar, no surrounding navigation.
        That's what makes the HTMX swap safe (no double-headers,
        no broken styling)."""
        response = page.request.get(
            server_url
            + "/api/internal/cycle-time?contract=astral-uv-week"
        )
        assert response.status == 200, (
            f"fragment endpoint returned {response.status}; expected 200"
        )
        html = response.text()
        # Must contain the chart container + script.
        assert "cycle-time-chart" in html, (
            "fragment must include the chart container div"
        )
        assert "vegaEmbed" in html, (
            "fragment must include the Vega-Lite embed script"
        )
        # Must NOT contain page chrome — no <header>, no
        # site-header, no filter bar.
        for forbidden in (
            "<!doctype html",
            "site-header",
            "filter-bar",
        ):
            assert forbidden not in html.lower(), (
                f"fragment must not contain page chrome (found {forbidden!r}); "
                f"got first 200 chars: {html[:200]!r}"
            )

    def test_clicking_reset_swaps_chart_in_place(
        self, server_url: str, page: Page
    ):
        """End-to-end: click reset; assert the chart re-renders
        (a new SVG element is in place after the swap). The jitter
        is random per render, so we can also assert the post-reset
        SVG has the same number of data marks."""
        page.goto(server_url + "/contracts/astral-uv-week/")
        page.wait_for_selector("#cycle-time-tile svg", timeout=10000)
        page.wait_for_timeout(500)
        marks_before = page.locator("#cycle-time-tile .mark-symbol path").count()
        # Click reset; wait for HTMX to swap + Vega to re-embed.
        page.locator("#cycle-time-tile button.reset-btn").first.click()
        page.wait_for_timeout(800)
        page.wait_for_selector("#cycle-time-tile svg", timeout=5000)
        marks_after = page.locator("#cycle-time-tile .mark-symbol path").count()
        assert marks_after == marks_before, (
            f"after reset, chart should re-render with same data: "
            f"before={marks_before} marks, after={marks_after}"
        )
        # Still exactly one chart container after swap (no stacking).
        assert page.locator("#cycle-time-chart").count() == 1, (
            "reset swap produced duplicate chart containers"
        )


class TestWorkItemsTableOnDashboard:
    """The work-items table is a composable component included on the
    dashboard (below the cycle-time tile) and on detail pages. It's a
    sortable, filterable per-item view backed by an HTMX fragment
    endpoint at `/api/internal/work-items`.
    """

    def test_dashboard_renders_work_items_table_with_all_rows(
        self, server_url: str, page: Page
    ):
        """The dashboard shows all 43 completed items in the fixture
        window, one row each. The container has id="work-items" so HTMX
        can swap it on filter/sort interactions."""
        page.goto(server_url + "/contracts/astral-uv-week/")
        page.wait_for_selector("#work-items", timeout=10000)
        rows = page.locator("table.work-items-grid tbody tr")
        assert rows.count() == 43, (
            f"expected 43 work-item rows on dashboard; got {rows.count()}"
        )

    def test_table_columns_show_id_title_completed_cycle(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/contracts/astral-uv-week/")
        page.wait_for_selector("#work-items", timeout=10000)
        headers = page.locator("table.work-items-grid thead th").all_inner_texts()
        joined = " ".join(h.lower() for h in headers)
        for expected in ("#", "title", "completed", "cycle"):
            assert expected in joined, (
                f"missing column header {expected!r}; headers were {headers}"
            )

    def test_filter_by_title_narrows_rows_via_htmx(
        self, server_url: str, page: Page
    ):
        """Typing into the search input narrows the row count via
        HTMX (the wrapper swaps in place)."""
        page.goto(server_url + "/contracts/astral-uv-week/")
        page.wait_for_selector("#work-items", timeout=10000)
        before = page.locator("table.work-items-grid tbody tr").count()
        assert before == 43

        # HTMX's keyup-changed trigger fires on keyup events; Playwright's
        # `fill()` sets the value without dispatching keyup, so use
        # `press_sequentially` to simulate real typing.
        page.locator("#work-items-search").press_sequentially(
            "zzz-impossible-needle", delay=20
        )
        # debounce + HTMX round-trip
        page.wait_for_timeout(800)
        after_empty = page.locator(".work-items-empty").count()
        rows_after = page.locator("table.work-items-grid tbody tr").count()
        assert after_empty == 1 or rows_after == 0, (
            f"filter for an impossible string should produce empty state; "
            f"got {rows_after} rows / {after_empty} empty messages"
        )

    def test_sort_by_cycle_time_reorders_rows(
        self, server_url: str, page: Page
    ):
        """Clicking the 'Cycle (d)' column header asks the server to
        re-sort by cycle_time. Clicking it again toggles direction.
        Server-side sort via HTMX — no client JS."""
        page.goto(server_url + "/contracts/astral-uv-week/")
        page.wait_for_selector("#work-items", timeout=10000)

        def _cycle_values() -> list[float]:
            cells = page.locator(
                "table.work-items-grid tbody td.num"
            ).all_inner_texts()
            return [float(c) for c in cells]

        default_order = _cycle_values()
        # Default is completed_at DESC — values are not necessarily
        # monotonic in cycle_time. Sanity: we have 43 entries.
        assert len(default_order) == 43

        # Click the Cycle (d) sort header. The header text is
        # "Cycle (d)". After click + HTMX swap, rows should be
        # sorted by cycle_time descending (first toggle defaults
        # to desc per the component contract).
        page.locator("table.work-items-grid thead a:has-text('Cycle')").click()
        page.wait_for_timeout(500)
        sorted_desc = _cycle_values()
        assert sorted_desc == sorted(sorted_desc, reverse=True), (
            f"clicking Cycle header should sort by cycle_time desc; "
            f"got {sorted_desc}"
        )

        # Second click toggles to ascending.
        page.locator("table.work-items-grid thead a:has-text('Cycle')").click()
        page.wait_for_timeout(500)
        sorted_asc = _cycle_values()
        assert sorted_asc == sorted(sorted_asc), (
            f"second click should sort by cycle_time asc; got {sorted_asc}"
        )

    def test_filter_does_not_replace_the_search_input_element(
        self, server_url: str, page: Page
    ):
        """User-reported bug: typing in the search box scrolls the
        page. Root cause: the HTMX swap recreates the entire
        wrapper, so the `<input>` is a fresh DOM node after each
        keystroke. The browser's restore-scroll / restore-focus
        machinery then yanks the viewport around (and silently
        drops anything the user might have set on the element —
        IME composition, native selection, etc.).

        Fix is structural: the input must NOT be inside the swap
        target. This test pins that invariant via a marker the
        test plants on the input before filtering — if the element
        survived the swap, the marker is still there.
        """
        page.goto(server_url + "/contracts/astral-uv-week/")
        page.wait_for_selector("#work-items", timeout=10000)

        search = page.locator("#work-items-search")
        # Plant a unique marker on the live element.
        marker = page.evaluate(
            """() => {
                const el = document.getElementById('work-items-search');
                const m = 'marker-' + Math.random().toString(36).slice(2);
                el.dataset.testMarker = m;
                return m;
            }"""
        )
        assert marker.startswith("marker-")

        search.focus()
        search.press_sequentially("url", delay=20)
        page.wait_for_timeout(800)  # debounce + HTMX round-trip

        marker_after = page.evaluate(
            """() => document.getElementById('work-items-search')?.dataset.testMarker || ''"""
        )
        assert marker_after == marker, (
            f"search input was recreated during HTMX swap — marker "
            f"{marker!r} did not survive (got {marker_after!r}). The "
            f"input must live OUTSIDE the swap target so it isn't "
            f"replaced on every keystroke."
        )

    def test_each_row_links_to_item_lifecycle_page(
        self, server_url: str, page: Page
    ):
        """The leftmost cell (item_id) is a link to the per-item
        lifecycle page under /contracts/<id>/items/<source>/<item_id>."""
        page.goto(server_url + "/contracts/astral-uv-week/")
        page.wait_for_selector("#work-items", timeout=10000)
        first_id_link = page.locator(
            "table.work-items-grid tbody tr:first-child td.id a"
        )
        href = first_id_link.get_attribute("href")
        assert href is not None, "first row's id cell must contain a link"
        assert href.startswith(
            "/contracts/astral-uv-week/items/"
        ), (
            f"id-cell link must point at the lifecycle page under "
            f"/contracts/<id>/items/...; got {href!r}"
        )


class TestWorkItemsFragmentEndpoint:
    """`/api/internal/work-items?contract=X&sort=…&direction=…&q=…`
    returns the work-items partial only (no page chrome). It's the
    HTMX swap target for sort/filter interactions.
    """

    def test_endpoint_returns_table_partial_no_page_chrome(
        self, server_url: str, page: Page
    ):
        response = page.request.get(
            server_url
            + "/api/internal/work-items?contract=astral-uv-week"
        )
        assert response.status == 200
        html = response.text()
        # The endpoint returns the body partial: count + table (no
        # search input — that lives outside the swap target).
        assert "work-items-grid" in html, (
            f"fragment must contain the table body; first 300 chars: "
            f"{html[:300]!r}"
        )
        # The search input must NOT be in the fragment — it lives
        # outside the swap target and is never re-rendered.
        assert 'id="work-items-search"' not in html, (
            "fragment must not contain the search input (it lives "
            "outside the swap target and stays put across HTMX requests)"
        )
        # No <!doctype>, no site-header, no filter-bar — fragment only.
        for forbidden in (
            "<!doctype html",
            "site-header",
            "filter-bar",
        ):
            assert forbidden not in html.lower(), (
                f"fragment must not contain page chrome (found {forbidden!r}); "
                f"got first 300 chars: {html[:300]!r}"
            )

    def test_endpoint_supports_sort_param(self, server_url: str, page: Page):
        """Pass sort=cycle_time_days&direction=desc; first numeric
        cell in the returned table should be the largest cycle time."""
        response = page.request.get(
            server_url
            + "/api/internal/work-items"
            "?contract=astral-uv-week&sort=cycle_time_days&direction=desc"
        )
        assert response.status == 200
        html = response.text()
        # Parse out the numeric cells in order. The component renders
        # cycle_time_days inside <td class="num">.
        import re

        nums = [
            float(m.group(1))
            for m in re.finditer(
                r'<td class="num">([0-9]+\.[0-9]+)</td>', html
            )
        ]
        assert nums, "expected at least one numeric cycle-time cell"
        assert nums == sorted(nums, reverse=True), (
            f"sort=cycle_time_days&direction=desc should return rows in "
            f"descending cycle order; got {nums}"
        )

    def test_endpoint_supports_q_filter_param(
        self, server_url: str, page: Page
    ):
        """Pass q=<impossible> — endpoint returns the empty state."""
        response = page.request.get(
            server_url
            + "/api/internal/work-items"
            "?contract=astral-uv-week&q=zzz-impossible-needle"
        )
        assert response.status == 200
        html = response.text()
        assert "work-items-empty" in html, (
            f"q with no matches should render empty state; got first "
            f"300 chars: {html[:300]!r}"
        )

    def test_endpoint_404s_for_unknown_contract(
        self, server_url: str, page: Page
    ):
        response = page.request.get(
            server_url + "/api/internal/work-items?contract=does-not-exist"
        )
        assert response.status == 404


class TestLifecyclePage:
    """`/contracts/<id>/items/<source>/<item_id>` shows a per-item
    lifecycle view. Two presentation modes:

      - **Chartable (≥ 2 stages)**: gantt-style swimlane with one bar
        per stage on the y-axis, time on the x-axis. Labels live on
        the y-axis so they can't overlap.
      - **Trivial (1 stage, 2 events)**: no chart — just a summary
        card with the duration and the two endpoint timestamps. A
        timeline with one bar is noise, not insight.

    Both modes render the same identity header + tabular event list.

    These tests pin specific fixture item ids so the assertion stays
    deterministic regardless of dashboard sort order.
    """

    # 3 events (Draft → Awaiting Review → Merged) — gantt territory.
    _CHARTABLE = "#19342"
    # 2 events (Awaiting Review → Merged) — summary-card territory.
    _NON_CHARTABLE = "#19303"

    @staticmethod
    def _url(item_id: str) -> str:
        from urllib.parse import quote

        return (
            f"/contracts/astral-uv-week/items/github/"
            f"{quote(item_id, safe='')}"
        )

    def test_chartable_lifecycle_renders_gantt_bars(
        self, server_url: str, page: Page
    ):
        """A 3-event lifecycle draws bars on a gantt — one rect per
        stage. With #19342 (Draft, Awaiting Review) that's 2 bars."""
        page.goto(server_url + self._url(self._CHARTABLE))
        page.wait_for_selector("#lifecycle-chart svg", timeout=10000)
        page.wait_for_timeout(400)
        n_bars = page.evaluate(
            """() => document.querySelectorAll(
                '#lifecycle-chart svg .mark-rect path'
            ).length"""
        )
        assert n_bars >= 2, (
            f"expected ≥ 2 stage bars on the gantt for "
            f"{self._CHARTABLE!r}; got {n_bars}"
        )

    def test_chartable_lifecycle_yaxis_carries_stage_names(
        self, server_url: str, page: Page
    ):
        """Stage names belong on the y-axis (not as overlap-prone
        text labels above points). For #19342 the axis should
        contain 'Draft' and 'Awaiting Review'."""
        page.goto(server_url + self._url(self._CHARTABLE))
        page.wait_for_selector("#lifecycle-chart svg", timeout=10000)
        page.wait_for_timeout(400)
        labels = page.evaluate(
            """() => Array.from(document.querySelectorAll(
                '#lifecycle-chart svg .role-axis-label text'
            )).map(t => t.textContent)"""
        )
        assert "Draft" in labels, (
            f"y-axis must list 'Draft' as a stage; labels were {labels}"
        )
        assert "Awaiting Review" in labels, (
            f"y-axis must list 'Awaiting Review' as a stage; "
            f"labels were {labels}"
        )

    def test_lifecycle_header_shows_item_identity(
        self, server_url: str, page: Page
    ):
        """Site header carries item id + contract for either mode."""
        page.goto(server_url + self._url(self._CHARTABLE))
        page.wait_for_selector("#lifecycle-chart svg", timeout=10000)
        header_text = page.locator(".site-header").inner_text()
        assert self._CHARTABLE in header_text, (
            f"site header must include the item id "
            f"{self._CHARTABLE!r}; got {header_text!r}"
        )
        assert "astral-uv-week" in header_text

    def test_non_chartable_lifecycle_renders_summary_not_chart(
        self, server_url: str, page: Page
    ):
        """Trivial 2-event item (#19303): no gantt — a summary
        card instead. The card names the (single) stage, shows its
        duration, and names both the entry timestamp and the
        terminal event."""
        page.goto(server_url + self._url(self._NON_CHARTABLE))
        # Must render — wait for the summary card (NOT a chart).
        page.wait_for_selector(".lifecycle-summary", timeout=10000)
        # No gantt chart container at all in this mode.
        assert page.locator("#lifecycle-chart").count() == 0, (
            f"trivial lifecycle ({self._NON_CHARTABLE!r}) must not "
            f"render the gantt chart container — found one"
        )
        summary = page.locator(".lifecycle-summary").inner_text()
        # The fixture's #19303 has Awaiting Review → Merged.
        assert "Awaiting Review" in summary, (
            f"summary must name the single stage; got {summary!r}"
        )
        assert "Merged" in summary, (
            f"summary must name the terminal event; got {summary!r}"
        )

    def test_non_chartable_lifecycle_still_shows_event_table(
        self, server_url: str, page: Page
    ):
        """Even without a chart, the per-event table at the bottom
        of the page still renders — it's the canonical view of the
        raw transitions for either mode."""
        page.goto(server_url + self._url(self._NON_CHARTABLE))
        page.wait_for_selector(".lifecycle-summary", timeout=10000)
        # The events table is just a .work-items-grid styled table
        # inside the detail-extras section.
        n_event_rows = page.locator(
            "section.detail-extras table.work-items-grid tbody tr"
        ).count()
        assert n_event_rows == 2, (
            f"expected 2 event rows in the table for "
            f"{self._NON_CHARTABLE!r}; got {n_event_rows}"
        )

    def test_unknown_item_returns_404(self, server_url: str, page: Page):
        response = page.request.get(
            server_url
            + "/contracts/astral-uv-week/items/github/%23does-not-exist"
        )
        assert response.status == 404

    def test_unknown_contract_for_lifecycle_returns_404(
        self, server_url: str, page: Page
    ):
        response = page.request.get(
            server_url
            + "/contracts/does-not-exist/items/github/%231"
        )
        assert response.status == 404


class TestPasswordGate:
    """CLI-level: starting bind off-localhost without a password must
    fail loudly. Uses CliRunner — no server actually starts."""

    def test_off_localhost_without_password_exits_nonzero(self):
        result = CliRunner().invoke(
            cli,
            ["serve", "--host", "0.0.0.0", "--port", "12345"],
            catch_exceptions=False,
        )
        assert result.exit_code != 0
        assert "password" in result.output.lower(), (
            f"error message should mention password; got:\n{result.output}"
        )

    def test_localhost_default_does_not_require_password(self, tmp_path):
        """The default `--host 127.0.0.1` should NOT require a password.
        We can't actually start the server in this test (would block),
        but we can verify the validation passes by mocking uvicorn.run.
        """
        # Stand up a minimal --data-dir / --contracts-dir so the loader
        # doesn't fail before the bind check.
        (tmp_path / "contracts").mkdir()
        (tmp_path / "data").mkdir()

        # Replace uvicorn.run with a stub that records and returns.
        from unittest.mock import patch

        with patch("uvicorn.run") as mock_run:
            result = CliRunner().invoke(
                cli,
                [
                    "serve",
                    "--port",
                    "12346",
                    "--data-dir",
                    str(tmp_path / "data"),
                    "--contracts-dir",
                    str(tmp_path / "contracts"),
                ],
                catch_exceptions=False,
            )
        assert result.exit_code == 0, (
            f"localhost default should not require password; output:\n{result.output}"
        )
        assert mock_run.called, "uvicorn.run should have been invoked"
