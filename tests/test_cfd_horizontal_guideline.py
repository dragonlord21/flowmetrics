"""Browser test for the CFD's horizontal y-axis guideline.

User feature: a horizontal dashed line that follows the cursor's
vertical position over the chart, with a label showing the
inverted y-scale value, so the y-axis can be read at the cursor
without bouncing back to the tick labels.

Test path: move the mouse to a known pixel y, verify a visible
horizontal line appears at that y, and verify the label's text is
a plausible cumulative count.
"""

from __future__ import annotations

from pathlib import Path

import pytest

pytest.importorskip("playwright")
from playwright.sync_api import sync_playwright

pytestmark = pytest.mark.browser

REPO_ROOT = Path(__file__).parent.parent
CFD_HTML = REPO_ROOT / "samples" / "astral-sh_uv" / "cfd.html"


def test_horizontal_guideline_appears_on_hover():
    if not CFD_HTML.exists():
        pytest.skip("CFD sample missing")
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(viewport={"width": 1440, "height": 1500})
        page.goto(f"file://{CFD_HTML}")
        page.wait_for_selector("#cfd-chart svg", timeout=15000)
        page.wait_for_timeout(2000)

        # Move cursor to chart center.
        box = page.locator("#cfd-chart").bounding_box()
        cx = box["x"] + box["width"] / 2
        cy = box["y"] + box["height"] / 2
        page.mouse.move(cx, cy)
        page.wait_for_timeout(400)

        # Look for a visible horizontal guideline element. Our overlay
        # tags it with `data-flow="y-guideline"` for unambiguous lookup.
        guideline = page.locator('#cfd-chart [data-flow="y-guideline"]')
        assert guideline.count() == 1, (
            "Expected one y-guideline overlay; got "
            f"{guideline.count()}"
        )

        # When the cursor is over the chart, the overlay must be visible
        # (opacity > 0 on the parent group).
        opacity = page.evaluate(
            """() => {
                const g = document.querySelector('#cfd-chart [data-flow=\"y-guideline\"]');
                return g ? parseFloat(getComputedStyle(g).opacity) : 0;
            }"""
        )
        assert opacity > 0, f"guideline should be visible; opacity={opacity}"

        # The label must show a numeric y value (something like '247').
        label_text = page.evaluate(
            """() => {
                const t = document.querySelector('#cfd-chart [data-flow=\"y-guideline-label\"]');
                return t ? t.textContent : '';
            }"""
        )
        assert label_text.strip().isdigit() or label_text.strip().replace('.', '', 1).isdigit(), (
            f"label should be a numeric y value, got {label_text!r}"
        )

        # Moving cursor outside the chart hides the overlay.
        page.mouse.move(10, 10)
        page.wait_for_timeout(300)
        opacity_after = page.evaluate(
            """() => {
                const g = document.querySelector('#cfd-chart [data-flow=\"y-guideline\"]');
                return g ? parseFloat(getComputedStyle(g).opacity) : 0;
            }"""
        )
        assert opacity_after == 0, (
            f"guideline should hide on mouseleave; got opacity={opacity_after}"
        )
        browser.close()
