"""E2E: the contract builder's Steps editor (Alpine component).

Drives the real Alpine component in Chromium with the source probes
stubbed via `app.state` — no network. Pins the chip-binding contract
that bit users in the UX pass:

  - Typing a name into the add-step field and then clicking a label
    chip must commit *one* step with the typed name (not spawn a
    second phantom step named after the chip), and clear the add-step
    field. The leftover text in a step-shaped input was the confusing
    "two steps" the user reported.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time

import pytest
import uvicorn
from playwright.sync_api import Page, expect

pytestmark = pytest.mark.e2e


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

    tmp = tmp_path_factory.mktemp("builder-steps-e2e")
    contracts_dir = tmp / "contracts"
    contracts_dir.mkdir()
    data_dir = tmp / "data"

    app = create_app(data_dir=data_dir, contracts_dir=contracts_dir)
    # Stub the probes so the builder verifies the source + loads label
    # chips without touching the network.
    app.state.probe_source = lambda source, target: {
        "ok": True, "label": "stub repo",
    }
    app.state.probe_source_vocab = lambda source, target: {
        "labels": [{"name": "ready"}, {"name": "in-review"}],
        "lifecycle_events": [{"name": "Issue opened", "wip": False}],
        "warehouse_stages": [],
    }

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


def _verify_source(page: Page) -> None:
    page.fill("#f-repo", "owner/name")
    page.eval_on_selector("#f-repo", "el => el.blur()")
    page.wait_for_selector(".probe-status--ok", timeout=8000)
    page.wait_for_selector("#add-step:visible", timeout=4000)


class TestChipCommitsTypedStep:
    def test_typed_name_plus_chip_makes_one_named_step(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/admin/contracts/new")
        _verify_source(page)

        # Type a step name but DON'T press "+ Add step".
        page.click("#new-step-name")
        page.fill("#new-step-name", "Ready")

        # Click the "ready" label chip.
        page.click("#sugg-labels .sugg-chip >> text=ready")
        page.wait_for_timeout(300)

        rows = page.query_selector_all("#steps-list .step-row")
        assert len(rows) == 1, (
            f"expected exactly one step, got {len(rows)} — a phantom "
            "step was spawned from the chip"
        )
        name = rows[0].query_selector(".step-name-input").input_value()
        assert name == "Ready", (
            f"step should keep the typed name 'Ready', got {name!r}"
        )
        pills = rows[0].query_selector_all(".match-pill")
        assert len(pills) == 1
        assert "ready" in pills[0].inner_text()

        # The add-step field is consumed — no leftover step-shaped input.
        assert page.input_value("#new-step-name") == "", (
            "the add-step field must clear after committing the step"
        )

    def test_chip_binds_to_active_step_without_new_row(
        self, server_url: str, page: Page
    ):
        page.goto(server_url + "/admin/contracts/new")
        _verify_source(page)
        # Commit a step explicitly.
        page.fill("#new-step-name", "Ready")
        page.click("#add-step")
        page.wait_for_selector("#steps-list .step-row", timeout=3000)
        # Now click a chip — it should bind to the active step, not add a row.
        page.click("#sugg-labels .sugg-chip >> text=in-review")
        page.wait_for_timeout(300)
        rows = page.query_selector_all("#steps-list .step-row")
        assert len(rows) == 1
        pills = rows[0].query_selector_all(".match-pill")
        assert any("in-review" in pl.inner_text() for pl in pills)


class TestAddStepIsVisuallyDistinct:
    def test_add_step_control_is_labelled(
        self, server_url: str, page: Page
    ):
        """The add-step control must not read as just another step
        row — it carries a distinguishing label so users can tell the
        'create a step' field apart from committed steps."""
        page.goto(server_url + "/admin/contracts/new")
        _verify_source(page)
        expect(page.locator(".add-step-panel")).to_be_visible()
        expect(page.locator(".add-step-panel")).to_contain_text("Add a step")
