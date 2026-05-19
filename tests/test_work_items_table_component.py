"""Component tests for `flowmetrics.web.components.work_items_table`.

The table is the second composable component in Slice 2. Same
pattern as cycle_time: a pure render function reads from DuckDB and
returns a typed payload that a Jinja partial renders.

Interaction (filter by title, sort by column) is client-side JS for
v1 — data volumes are small (≤ a few hundred rows per contract per
window) and snappy local sort beats round-tripping through HTMX.
The tests assert the contract at the data and the spec level; the
e2e file tests the in-browser interactivity.
"""

from __future__ import annotations

import re
import tempfile
from pathlib import Path

import duckdb
import pytest
import yaml
from click.testing import CliRunner

from flowmetrics.cli import cli
from flowmetrics.web.components.work_items_table import render

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


@pytest.fixture
def warehouse() -> duckdb.DuckDBPyConnection:
    tmp = Path(tempfile.mkdtemp())
    contracts_dir = tmp / "contracts"
    contracts_dir.mkdir()
    data_dir = tmp / "data"
    (contracts_dir / "astral-uv-week.yaml").write_text(
        yaml.safe_dump(
            {
                "contract": {
                    "name": "astral-uv-week",
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
            "materialise",
            "astral-uv-week",
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
    assert res.exit_code == 0, res.output

    con = duckdb.connect(":memory:")
    glob = (data_dir / "work_items" / "**" / "*.parquet").as_posix()
    con.execute(
        f"CREATE VIEW work_items AS "
        f"SELECT * FROM read_parquet('{glob}', hive_partitioning = true)"
    )
    yield con
    con.close()


class TestWorkItemsTableShape:
    def test_renders_one_row_per_completed_item(self, warehouse):
        data = render(warehouse, "astral-uv-week")
        assert data.count == 43
        assert len(data.rows) == 43

    def test_row_fields_cover_identity_lifecycle_and_link(self, warehouse):
        data = render(warehouse, "astral-uv-week")
        first = data.rows[0]
        # Identity
        assert first.item_id
        assert first.title
        assert first.source in ("github", "jira")
        # Lifecycle
        assert re.match(r"^\d{4}-\d{2}-\d{2}$", first.completed_at)
        assert first.completed_at_display  # pre-formatted UTC display
        assert isinstance(first.cycle_time_days, float)
        # Optional: source URL for "open on GitHub/Jira"
        # (None acceptable; field present)
        assert first.url is None or first.url.startswith("http")

    def test_completed_at_display_is_utc_anchored(self, warehouse):
        """Same TZ-safety contract as the cycle-time chart: the
        date the table shows must not shift by browser TZ. Display
        string comes from `flowmetrics.utc_dates`."""
        data = render(warehouse, "astral-uv-week")
        for r in data.rows:
            assert re.match(
                r"^[A-Z][a-z]{2}\s+\d{1,2},\s+\d{4}$", r.completed_at_display
            ), (
                f"completed_at_display must be the UTC display form "
                f"'%b %d, %Y'; got {r.completed_at_display!r} on "
                f"item {r.item_id!r}"
            )

    def test_rows_default_ordered_by_completed_at_desc(self, warehouse):
        """Most-recent first is the natural default — users
        scanning the table want yesterday's work first."""
        data = render(warehouse, "astral-uv-week")
        dates = [r.completed_at for r in data.rows]
        assert dates == sorted(dates, reverse=True), (
            "table default sort must be completed_at descending"
        )
