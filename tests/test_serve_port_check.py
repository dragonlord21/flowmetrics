"""`flow serve` port-availability check.

Without this, an in-use port surfaced as uvicorn's raw `[Errno 48]
Address already in use` traceback — nothing actionable. The check
runs BEFORE uvicorn boots so the user gets a clean message naming
the port and suggesting `--port N`.
"""

from __future__ import annotations

import socket

import click
import pytest

from flowmetrics.cli import _assert_port_available


def _occupy_port() -> tuple[socket.socket, int]:
    """Bind a socket to a free port; return (socket, port) — caller
    closes when done. Holds the port so a subsequent bind fails."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    s.listen(1)
    return s, s.getsockname()[1]


def _free_port() -> int:
    """Pick a free port and immediately release it. Best-effort —
    a different process could grab it in the race window."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class TestAssertPortAvailable:
    def test_free_port_passes_silently(self):
        port = _free_port()
        # Returns None; no exception.
        assert _assert_port_available("127.0.0.1", port) is None

    def test_busy_port_raises_a_click_usage_error(self):
        sock, port = _occupy_port()
        try:
            with pytest.raises(click.ClickException) as excinfo:
                _assert_port_available("127.0.0.1", port)
            msg = str(excinfo.value.message)
            # Message must name the port and suggest --port.
            assert str(port) in msg
            assert "--port" in msg
        finally:
            sock.close()
