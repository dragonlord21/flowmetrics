"""E2E: CFD floating day-detail panel — position invariants and
pin/release state machine.

Two claims that the existing unit tests can't verify, because
they live in the rendered SVG + DOM:

  1. The floating panel never occludes the vertical date rule.
     For any cursor x in the chart's plot area, the rule's x-coord
     falls OUTSIDE the panel's horizontal extent. Hovering near
     the left edge parks the panel right; hovering near the right
     edge flips it left.

  2. Click pins the panel — the rule loses its dashed stroke, the
     panel gains an `is-pinned` class and a "PINNED" banner, and
     subsequent mousemove / mouseleave is ignored. Click the same
     day again to release.

Per `feedback_test_credibility_rule`, these UI claims need
browser-asserted evidence, not screenshot eyeballing. Drive
Playwright against a real uvicorn process and assert positions,
classes, and text content directly.

Default pytest run skips e2e. Run:
  uv run pytest -m e2e tests/test_cfd_floating_panel_e2e.py
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


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _ServerThread(threading.Thread):
    def __init__(self, app, port: int):
        super().__init__(daemon=True)
        config = uvicorn.Config(
            app,
            host="127.0.0.1",
            port=port,
            log_level="error",
            access_log=False,
        )
        self.server = uvicorn.Server(config)

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


@pytest.fixture(scope="module")
def server_url(tmp_path_factory):
    """Materialize the astral-uv-week fixture (the same one
    test_cfd_window_e2e uses — 7-day window, cached responses)
    and serve it via a real uvicorn so Playwright sees the
    real chart + the real hover script."""
    from flowmetrics.app import create_app

    tmp_path = tmp_path_factory.mktemp("cfd-floating-panel-e2e")
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    data_dir = tmp_path / "data"
    name = "astral-uv-week"

    (contracts_dir / f"{name}.yaml").write_text(
        yaml.safe_dump(
            {
                "workflow": {
                    "name": name,
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
            "materialize",
            name,
            "--data-dir",
            str(data_dir),
            "--workflows-dir",
            str(contracts_dir),
            "--cache-dir",
            str(FIXTURE_CACHE),
            "--offline",
        ],
        catch_exceptions=False,
    )
    assert res.exit_code == 0, f"fixture materialize failed: {res.output}"

    app = create_app(data_dir=data_dir, contracts_dir=contracts_dir)
    port = _free_port()
    thread = _ServerThread(app, port)
    thread.start()
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


# Read the rule line + the panel in one round-trip so the
# comparison is over a single layout snapshot. Returns None if
# either is missing.
_GEOM_JS = """
() => {
  const rule = document.querySelector('[data-flow="x-guideline"]');
  const panel = document.getElementById('cfd-hover-panel');
  if (!rule || !panel) return null;
  const r = rule.getBoundingClientRect();
  const p = panel.getBoundingClientRect();
  const cs = window.getComputedStyle(rule);
  return {
    rule: {
      x: r.left + r.width / 2,
      opacity: parseFloat(cs.opacity),
      dasharray: rule.getAttribute('stroke-dasharray'),
    },
    panel: {
      left: p.left, right: p.right, width: p.width,
      pinned: panel.classList.contains('is-pinned'),
    },
  };
}
"""


def _goto_cfd(page: Page, server_url: str) -> None:
    page.set_viewport_size({"width": 1400, "height": 1000})
    page.goto(server_url + "/workflows/astral-uv-week/metrics/cfd")
    page.wait_for_selector("#cfd-chart svg", timeout=15000)
    # The hover script defers its first paint two animation frames
    # so the panel can measure its width before placing itself.
    # Wait for the panel to actually have content.
    page.wait_for_function(
        "() => { const p = document.getElementById('cfd-hover-panel');"
        " return p && p.innerHTML.length > 50; }",
        timeout=5000,
    )
    page.wait_for_timeout(200)


def _svg_box(page: Page) -> dict:
    return page.evaluate(
        "() => { const r = document.querySelector('#cfd-chart svg')"
        ".getBoundingClientRect(); return"
        " {x: r.x, y: r.y, width: r.width, height: r.height}; }"
    )


class TestFloatingPanelPosition:
    """The floating panel must never sit over the vertical date
    rule. Whichever side of the rule has more horizontal room
    is the side that gets the panel."""

    def test_default_state_panel_is_absolutely_positioned_and_shows_latest_day(
        self, server_url: str, page: Page
    ):
        _goto_cfd(page, server_url)
        panel = page.locator("#cfd-hover-panel")
        expect(panel).to_be_visible()
        position = panel.evaluate("el => getComputedStyle(el).position")
        assert position == "absolute", (
            f"panel should be position: absolute at 1400px viewport,"
            f" got {position!r}"
        )
        # Default snaps to the last day in the window — May 10 2026.
        expect(panel).to_contain_text("May 10")
        # On first paint nothing is pinned.
        is_pinned = panel.evaluate("el => el.classList.contains('is-pinned')")
        assert is_pinned is False

    def test_hover_left_half_parks_panel_to_the_right_of_the_rule(
        self, server_url: str, page: Page
    ):
        _goto_cfd(page, server_url)
        sb = _svg_box(page)
        page.mouse.move(
            sb["x"] + sb["width"] * 0.20,
            sb["y"] + sb["height"] * 0.5,
        )
        page.wait_for_timeout(150)
        g = page.evaluate(_GEOM_JS)
        assert g is not None
        assert g["rule"]["opacity"] > 0.5, "rule should be visible on hover"
        assert g["panel"]["left"] > g["rule"]["x"], (
            f"panel.left={g['panel']['left']:.1f} should be"
            f" > rule.x={g['rule']['x']:.1f} when hovering on the left half"
        )

    def test_hover_right_half_flips_panel_to_the_left_of_the_rule(
        self, server_url: str, page: Page
    ):
        _goto_cfd(page, server_url)
        sb = _svg_box(page)
        page.mouse.move(
            sb["x"] + sb["width"] * 0.85,
            sb["y"] + sb["height"] * 0.5,
        )
        page.wait_for_timeout(150)
        g = page.evaluate(_GEOM_JS)
        assert g["panel"]["right"] < g["rule"]["x"], (
            f"panel.right={g['panel']['right']:.1f} should be"
            f" < rule.x={g['rule']['x']:.1f} when hovering on the right half"
        )

    def test_panel_never_overlaps_the_rule_across_a_full_scrub(
        self, server_url: str, page: Page
    ):
        """The core invariant — the rule's x-coordinate never falls
        inside the panel's horizontal extent, no matter where the
        cursor is."""
        _goto_cfd(page, server_url)
        sb = _svg_box(page)
        violations = []
        for frac in (0.10, 0.25, 0.40, 0.55, 0.70, 0.85, 0.95):
            page.mouse.move(
                sb["x"] + sb["width"] * frac,
                sb["y"] + sb["height"] * 0.5,
            )
            page.wait_for_timeout(120)
            g = page.evaluate(_GEOM_JS)
            rx = g["rule"]["x"]
            if g["panel"]["left"] <= rx <= g["panel"]["right"]:
                violations.append(
                    f"frac={frac}: rule.x={rx:.1f} inside"
                    f" panel [{g['panel']['left']:.1f},"
                    f" {g['panel']['right']:.1f}]"
                )
        assert not violations, "overlap at: " + "; ".join(violations)


# Read the rule + every visible per-band item-count label in one
# round-trip, so the comparison is against a single layout snapshot.
_LABEL_GEOM_JS = """
() => {
  const rule = document.querySelector('[data-flow="x-guideline"]');
  const labels = Array.from(document.querySelectorAll('text'))
    .filter(t => /\\bitems$/.test(t.textContent || '')
                 && parseFloat(t.style.opacity || '0') > 0);
  if (!rule) return null;
  const r = rule.getBoundingClientRect();
  return {
    ruleX: r.left + r.width / 2,
    labels: labels.map(t => {
      const b = t.getBoundingClientRect();
      return {text: t.textContent, left: b.left, right: b.right};
    }),
  };
}
"""


class TestLabelsParkOppositeThePanel:
    """The per-band item-count labels at the snap have to live on
    the OPPOSITE side of the rule from the floating panel — the
    panel sits on top of anything in its column, so labels on the
    same side disappear under it."""

    def test_hover_left_puts_labels_left_of_rule_panel_right(
        self, server_url: str, page: Page
    ):
        _goto_cfd(page, server_url)
        sb = _svg_box(page)
        page.mouse.move(
            sb["x"] + sb["width"] * 0.20,
            sb["y"] + sb["height"] * 0.5,
        )
        page.wait_for_timeout(200)
        # Confirm the panel parked right (precondition).
        g = page.evaluate(_GEOM_JS)
        assert g["panel"]["left"] > g["rule"]["x"], (
            "expected panel right of rule on left-half hover"
        )
        # Every visible band-count label is entirely LEFT of the rule.
        lg = page.evaluate(_LABEL_GEOM_JS)
        assert lg is not None and lg["labels"], "no labels rendered"
        overlaps = [b for b in lg["labels"] if b["right"] > lg["ruleX"]]
        assert not overlaps, (
            f"with panel on the right, labels must sit left of"
            f" rule.x={lg['ruleX']:.1f}; violations: {overlaps}"
        )

    def test_hover_right_puts_labels_right_of_rule_panel_left(
        self, server_url: str, page: Page
    ):
        _goto_cfd(page, server_url)
        sb = _svg_box(page)
        page.mouse.move(
            sb["x"] + sb["width"] * 0.85,
            sb["y"] + sb["height"] * 0.5,
        )
        page.wait_for_timeout(200)
        # Confirm the panel parked left (precondition).
        g = page.evaluate(_GEOM_JS)
        assert g["panel"]["right"] < g["rule"]["x"], (
            "expected panel left of rule on right-half hover"
        )
        # Every visible band-count label is entirely RIGHT of the rule.
        lg = page.evaluate(_LABEL_GEOM_JS)
        assert lg is not None and lg["labels"], "no labels rendered"
        overlaps = [b for b in lg["labels"] if b["left"] < lg["ruleX"]]
        assert not overlaps, (
            f"with panel on the left, labels must sit right of"
            f" rule.x={lg['ruleX']:.1f}; violations: {overlaps}"
        )


class TestPinAndRelease:
    """Click pins the panel — solid rule + .is-pinned class +
    a 'PINNED' banner. Clicking the same day releases."""

    def test_click_pins_panel_and_makes_the_rule_solid(
        self, server_url: str, page: Page
    ):
        _goto_cfd(page, server_url)
        sb = _svg_box(page)
        page.mouse.click(
            sb["x"] + sb["width"] * 0.5,
            sb["y"] + sb["height"] * 0.5,
        )
        page.wait_for_timeout(150)
        g = page.evaluate(_GEOM_JS)
        assert g["panel"]["pinned"] is True
        # Solid rule = empty (or null) dasharray.
        assert g["rule"]["dasharray"] in ("", None), (
            f"expected solid stroke when pinned,"
            f" got dasharray={g['rule']['dasharray']!r}"
        )
        # The visible banner is "📌 Pinned · click to release".
        # CSS text-transform makes it look uppercase, but the DOM
        # text is mixed-case, so assert the underlying string.
        expect(page.locator("#cfd-hover-panel")).to_contain_text("Pinned")

    def test_pinned_panel_survives_mouseleave(
        self, server_url: str, page: Page
    ):
        _goto_cfd(page, server_url)
        sb = _svg_box(page)
        page.mouse.click(
            sb["x"] + sb["width"] * 0.5,
            sb["y"] + sb["height"] * 0.5,
        )
        page.wait_for_timeout(150)
        pinned_date = page.locator(".cfd-panel-date").inner_text()
        # Leave the chart entirely (well above the SVG).
        page.mouse.move(
            sb["x"] + sb["width"] * 0.5,
            max(0, sb["y"] - 80),
        )
        page.wait_for_timeout(200)
        # Date display unchanged.
        assert page.locator(".cfd-panel-date").inner_text() == pinned_date
        # Rule still visible.
        g = page.evaluate(_GEOM_JS)
        assert g["rule"]["opacity"] > 0.5
        assert g["panel"]["pinned"] is True

    def test_click_same_day_again_releases_the_pin(
        self, server_url: str, page: Page
    ):
        _goto_cfd(page, server_url)
        sb = _svg_box(page)
        x = sb["x"] + sb["width"] * 0.5
        y = sb["y"] + sb["height"] * 0.5
        page.mouse.click(x, y)
        page.wait_for_timeout(150)
        # Same coords → same day → release.
        page.mouse.click(x, y)
        page.wait_for_timeout(150)
        g = page.evaluate(_GEOM_JS)
        assert g["panel"]["pinned"] is False
        assert g["rule"]["dasharray"] == "3 3"
        expect(page.locator("#cfd-hover-panel")).not_to_contain_text("Pinned")

    def test_click_a_different_day_moves_the_pin_without_releasing(
        self, server_url: str, page: Page
    ):
        _goto_cfd(page, server_url)
        sb = _svg_box(page)
        # Click left half first.
        page.mouse.click(
            sb["x"] + sb["width"] * 0.20,
            sb["y"] + sb["height"] * 0.5,
        )
        page.wait_for_timeout(150)
        date_a = page.locator(".cfd-panel-date").inner_text()
        # Now click right half — a clearly different day.
        page.mouse.click(
            sb["x"] + sb["width"] * 0.85,
            sb["y"] + sb["height"] * 0.5,
        )
        page.wait_for_timeout(150)
        date_b = page.locator(".cfd-panel-date").inner_text()
        assert date_a != date_b, (
            f"pin should jump to a new day; both clicks"
            f" landed on {date_a!r}"
        )
        # Still pinned (not released).
        g = page.evaluate(_GEOM_JS)
        assert g["panel"]["pinned"] is True


class TestPinAcrossZoomAndPan:
    """The chart's `bind: scales` zoom captures mouse drags for
    panning. Without compensation, the synthesised click that
    browsers fire at the end of every drag-pan re-fires onClick
    and jerks the pin to wherever the cursor happened to land.
    Pin gestures still work; drag-pan gestures don't disturb the
    pin."""

    def test_pin_works_after_wheel_zoom(
        self, server_url: str, page: Page
    ):
        _goto_cfd(page, server_url)
        sb = _svg_box(page)
        cx = sb["x"] + sb["width"] * 0.5
        cy = sb["y"] + sb["height"] * 0.5
        page.mouse.move(cx, cy)
        for _ in range(3):
            page.mouse.wheel(0, -200)
            page.wait_for_timeout(120)
        page.wait_for_timeout(300)
        # Pin after zoom — wait long enough that the
        # zoom-signal debounce window has elapsed.
        page.wait_for_timeout(250)
        page.mouse.click(cx + 80, cy)
        page.wait_for_timeout(200)
        g = page.evaluate(_GEOM_JS)
        assert g["panel"]["pinned"] is True, (
            "click after wheel-zoom should pin the panel"
        )

    def test_unpin_works_after_wheel_zoom(
        self, server_url: str, page: Page
    ):
        _goto_cfd(page, server_url)
        sb = _svg_box(page)
        cx = sb["x"] + sb["width"] * 0.5
        cy = sb["y"] + sb["height"] * 0.5
        page.mouse.move(cx, cy)
        for _ in range(2):
            page.mouse.wheel(0, -200)
            page.wait_for_timeout(120)
        page.wait_for_timeout(300)
        # Pin, then click same coords to release.
        page.wait_for_timeout(250)
        page.mouse.click(cx, cy)
        page.wait_for_timeout(200)
        page.mouse.click(cx, cy)
        page.wait_for_timeout(200)
        g = page.evaluate(_GEOM_JS)
        assert g["panel"]["pinned"] is False, (
            "click same day after wheel-zoom should release the pin"
        )

    def test_drag_pan_does_not_disturb_an_existing_pin(
        self, server_url: str, page: Page
    ):
        _goto_cfd(page, server_url)
        sb = _svg_box(page)
        cx = sb["x"] + sb["width"] * 0.55
        cy = sb["y"] + sb["height"] * 0.5
        # Zoom in so there's room to pan.
        page.mouse.move(cx, cy)
        for _ in range(3):
            page.mouse.wheel(0, -200)
            page.wait_for_timeout(120)
        page.wait_for_timeout(300)
        # Pin.
        page.wait_for_timeout(250)
        page.mouse.click(cx, cy)
        page.wait_for_timeout(200)
        date_before = page.locator(".cfd-panel-date").inner_text()
        # Drag-pan to the right.
        page.mouse.move(sb["x"] + sb["width"] * 0.30, cy)
        page.mouse.down()
        page.mouse.move(sb["x"] + sb["width"] * 0.55, cy, steps=8)
        page.mouse.up()
        page.wait_for_timeout(300)
        date_after = page.locator(".cfd-panel-date").inner_text()
        g = page.evaluate(_GEOM_JS)
        assert g["panel"]["pinned"] is True, "pan should not unpin"
        assert date_before == date_after, (
            f"pan should not move the pin; before={date_before!r}"
            f" after={date_after!r}"
        )
