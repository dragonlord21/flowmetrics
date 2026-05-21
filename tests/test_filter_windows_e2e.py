"""E2E: the Period filter bar.

The filter bar is a thin input layer — it emits a `period`
choice (a preset, or Custom = anchor + view_days) and nothing
else; `parse_windows` server-side turns it into the windows
every view reads. Pins:

  - The default Period is "Last 30 days".
  - Picking a preset submits `?period=<name>`.
  - "Custom" reveals Period Ending + View; the date input is
    bounded to the data coverage (no picking a date with no
    data behind it).
  - A custom period drives the chart window.
  - Reset clears the query.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from datetime import date
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
            app, host="127.0.0.1", port=port,
            log_level="error", access_log=False,
        )
        self.server = uvicorn.Server(config)

    def run(self) -> None:
        self.server.run()

    def stop(self) -> None:
        self.server.should_exit = True


@pytest.fixture(scope="module")
def server_url(tmp_path_factory):
    from flowmetrics.app import create_app

    tmp_path = tmp_path_factory.mktemp("filter-windows-e2e")
    contracts_dir = tmp_path / "contracts"
    contracts_dir.mkdir()
    data_dir = tmp_path / "data"
    name = "astral-uv-week"
    (contracts_dir / f"{name}.yaml").write_text(
        yaml.safe_dump({
            "contract": {
                "name": name, "source": "github",
                "repo": "astral-sh/uv",
                "start": "2026-05-04", "stop": "2026-05-10",
            }
        })
    )
    res = CliRunner().invoke(
        cli,
        [
            "materialise", name,
            "--data-dir", str(data_dir),
            "--contracts-dir", str(contracts_dir),
            "--cache-dir", str(FIXTURE_CACHE),
            "--offline",
        ],
        catch_exceptions=False,
    )
    assert res.exit_code == 0, res.output

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


class TestPeriodFilterBar:
    def test_default_period_is_last_30_days(
        self, server_url: str, page: Page
    ):
        """No query params → the Period dropdown selects "Last 30
        days" and the Custom fields stay hidden."""
        page.goto(server_url + "/workflows/astral-uv-week")
        page.wait_for_selector("select[name='period']")
        period = page.evaluate(
            "() => document.querySelector(\"select[name='period']\").value"
        )
        assert period == "last-30-days"
        # The Period Ending field is Custom-only.
        expect(page.locator("input[name='anchor']")).to_be_hidden()

    def test_selecting_a_preset_submits_period(
        self, server_url: str, page: Page
    ):
        """Picking a preset auto-submits, emitting just
        `?period=<name>` — no anchor/view_days noise."""
        page.goto(server_url + "/workflows/astral-uv-week/metrics/cfd")
        page.wait_for_selector("select[name='period']")
        page.select_option("select[name='period']", "last-7-days")
        page.wait_for_url("**period=last-7-days**")
        url = page.evaluate("() => location.href")
        assert "anchor=" not in url, f"preset URL should be clean: {url}"
        assert "view_days=" not in url, f"preset URL should be clean: {url}"

    def test_custom_reveals_period_ending_and_view(
        self, server_url: str, page: Page
    ):
        """Choosing "Custom…" reveals the Period Ending date
        field and the View dropdown without submitting."""
        page.goto(server_url + "/workflows/astral-uv-week/metrics/cfd")
        page.wait_for_selector("select[name='period']")
        expect(page.locator("input[name='anchor']")).to_be_hidden()
        page.select_option("select[name='period']", "custom")
        expect(page.locator("input[name='anchor']")).to_be_visible()
        expect(page.locator("select[name='view_days']")).to_be_visible()

    def test_custom_period_drives_the_cfd_window(
        self, server_url: str, page: Page
    ):
        """A custom Period Ending + View resolves to that exact
        window in the CFD headline."""
        page.goto(
            server_url + "/workflows/astral-uv-week/metrics/cfd"
            "?period=custom&anchor=2026-05-10&view_days=7"
        )
        page.wait_for_selector("#cfd-chart svg", timeout=15000)
        body = page.locator("body").inner_text()
        assert "7 days" in body, (
            f"CFD headline should report a 7-day window; got "
            f"{body[:1500]!r}"
        )
        assert "May 10, 2026" in body

    def test_period_ending_is_bounded_to_the_data_coverage(
        self, server_url: str, page: Page
    ):
        """The Period Ending date input carries min/max bounding
        it to the dates that actually have data — you can't pick a
        period with no data behind it."""
        page.goto(server_url + "/workflows/astral-uv-week?period=custom")
        page.wait_for_selector("input[name='anchor']")
        lo = page.get_attribute("input[name='anchor']", "min")
        hi = page.get_attribute("input[name='anchor']", "max")
        assert lo and hi, f"date input must be bounded; min={lo} max={hi}"
        # The fixture's data sits inside May 4-10, 2026.
        assert date(2026, 5, 4) <= date.fromisoformat(lo) <= date(2026, 5, 10)
        assert date(2026, 5, 4) <= date.fromisoformat(hi) <= date(2026, 5, 10)

    def test_aging_ignores_the_period_ending(
        self, server_url: str, page: Page
    ):
        """Aging is a "right now" snapshot pinned to the latest
        materialise — a custom Period anchor in the URL must NOT
        move its as-of date, and the page carries no Period bar."""
        page.goto(
            server_url + "/workflows/astral-uv-week/metrics/aging"
            "?period=custom&anchor=2026-05-06&view_days=30"
        )
        page.wait_for_selector(".metric-strip-headline")
        headline = page.locator(".metric-strip-headline").inner_text()
        # The Period anchor must NOT drive aging's as-of date.
        assert "May 06, 2026" not in headline, (
            f"aging must ignore the Period anchor; got {headline!r}"
        )
        # The aging page has no Period filter bar at all.
        expect(page.locator("select[name='period']")).to_have_count(0)

    def test_reset_clears_the_query(self, server_url: str, page: Page):
        """The Reset link drops all filter params."""
        page.goto(
            server_url + "/workflows/astral-uv-week"
            "?period=custom&anchor=2026-05-08&view_days=7"
        )
        page.wait_for_selector("a.filter-reset")
        page.locator("a.filter-reset").click()
        page.wait_for_url("**/workflows/astral-uv-week")
        url = page.evaluate("() => location.href")
        assert "period=" not in url
        assert "anchor=" not in url
