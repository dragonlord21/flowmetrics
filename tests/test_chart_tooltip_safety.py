"""Codebase-wide safety net: no chart tooltip uses `type: temporal`
on a date-display field.

Why this test exists
--------------------

`type: temporal` on a Vega-Lite tooltip field tells Vega to parse
the value as a timestamp AND format it with the browser-local
formatter. That shifts UTC dates by the viewer's TZ offset, so the
same data renders differently for different viewers. We hit this
bug once on the cycle-time tooltip (May 03 in PT, May 04 in UTC);
this test prevents the bug from recurring in any future chart.

Pair with `flowmetrics.utc_dates` — the runtime utility that
formats dates UTC-anchored before they reach Vega.

How to add coverage for a new chart
-----------------------------------

When a new component renders a Vega-Lite spec, add it to the
`_collect_component_specs` walker below. The test then enforces
the no-temporal-tooltip rule for that component too.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import duckdb
import pytest
import yaml
from click.testing import CliRunner

from flowmetrics.cli import cli
from flowmetrics.web.components.cycle_time import render as render_cycle_time

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "cache"


@pytest.fixture
def warehouse() -> duckdb.DuckDBPyConnection:
    """Materialise the pinned fixture data into a tmp warehouse so
    every component renderer has data to read. Independent of the
    other component-test fixtures so this file stands alone."""
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


def _collect_component_specs(warehouse) -> list[tuple[str, dict]]:
    """Render every chart component and return (component_name,
    parsed Vega-Lite spec) tuples. Add new components here when
    they ship."""
    return [
        (
            "cycle_time",
            json.loads(
                render_cycle_time(warehouse, "astral-uv-week").vega_spec_json()
            ),
        ),
        # Future: add aging, cfd, forecast, etc. as they land.
    ]


def _walk_tooltip_entries(spec: dict):
    """Yield every tooltip entry across all layers of a Vega-Lite
    spec. Returns dicts each describing one field bound to a
    tooltip."""
    for layer in spec.get("layer", [spec]):
        tooltip = layer.get("encoding", {}).get("tooltip")
        if tooltip is None:
            continue
        entries = tooltip if isinstance(tooltip, list) else [tooltip]
        for entry in entries:
            if isinstance(entry, dict):
                yield entry


class TestNoTemporalTooltips:
    """The single hard rule. Use the runtime utility
    `flowmetrics.utc_dates.to_utc_display_date` to pre-format any
    date you want in a tooltip; bind that string with
    `type: nominal`.
    """

    def test_no_chart_tooltip_uses_type_temporal(self, warehouse):
        for component_name, spec in _collect_component_specs(warehouse):
            for entry in _walk_tooltip_entries(spec):
                assert entry.get("type") != "temporal", (
                    f"chart {component_name!r} has a tooltip entry with "
                    f"type:temporal, which Vega-Lite formats in BROWSER-"
                    f"LOCAL time and produces TZ-shifted display strings "
                    f"for different viewers. Pre-format the date in "
                    f"Python via flowmetrics.utc_dates.to_utc_display_date "
                    f"and bind with type:nominal instead. Offending "
                    f"entry: {entry!r}"
                )
