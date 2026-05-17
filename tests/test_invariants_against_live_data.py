"""Run canonical-data invariants against the real cached data.

This is the data-quality smoke test: each source's actual output
must pass every invariant. If something fires here, there's a
real bug in a source adapter — not a chart problem.

Marked `integration` because it depends on `.cache/{github,jira}`
being populated, NOT on any live network call. Run with:

    uv run pytest -m integration tests/test_invariants_against_live_data.py
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from flowmetrics.compute import WorkItem, compute_pr_flow
from flowmetrics.invariants import (
    validate_flow_efficiency,
    validate_work_item,
)

pytestmark = pytest.mark.integration


CACHE_ROOT = Path(__file__).parent.parent / ".cache"
WINDOW_START = date(2026, 4, 17)
WINDOW_STOP = date(2026, 5, 16)


def _all_violations(items: list[WorkItem]) -> list[str]:
    """Return human-readable strings for every invariant violation
    across the work-item set."""
    out: list[str] = []
    for item in items:
        for v in validate_work_item(item):
            out.append(str(v))
    return out


def _format_violations_summary(violations: list[str], cap: int = 20) -> str:
    if not violations:
        return ""
    head = "\n  ".join(violations[:cap])
    tail = (
        f"\n  ... and {len(violations) - cap} more" if len(violations) > cap else ""
    )
    return f"{len(violations)} violation(s):\n  {head}{tail}"


# ---------------------------------------------------------------------------
# CalcMark/go-calcmark — small dataset, easy to inspect manually
# ---------------------------------------------------------------------------


class TestCalcMarkDataQuality:
    """Smoke test that the real CalcMark cache produces well-formed
    WorkItems."""

    def _items(self, *, include_issues: bool) -> list[WorkItem]:
        from flowmetrics.service import (
            fetch_items_active_in_window,
            make_github_source,
        )
        src = make_github_source(
            "CalcMark/go-calcmark",
            cache_dir=CACHE_ROOT / "github",
            read_only=True,
            include_issues=include_issues,
        )
        return fetch_items_active_in_window(src, WINDOW_START, WINDOW_STOP)

    def test_pr_only_path_is_clean(self):
        items = self._items(include_issues=False)
        violations = _all_violations(items)
        assert violations == [], _format_violations_summary(violations)

    def test_pr_plus_issue_path_is_clean(self):
        items = self._items(include_issues=True)
        violations = _all_violations(items)
        assert violations == [], _format_violations_summary(violations)

    def test_efficiency_per_item_is_clean(self):
        """Every per-item FlowEfficiency row must satisfy:
        cycle_time >= 0, active_time in [0, cycle_time], efficiency in [0, 1]."""
        from datetime import timedelta
        items = self._items(include_issues=True)
        completed = [i for i in items if i.completed_at is not None]
        violations: list[str] = []
        for item in completed:
            fe = compute_pr_flow(
                item,
                gap=timedelta(hours=4),
                min_cluster=timedelta(minutes=30),
                active_statuses=frozenset({"In Progress", "In Development"}),
            )
            for v in validate_flow_efficiency(fe):
                violations.append(str(v))
        assert violations == [], _format_violations_summary(violations)


# ---------------------------------------------------------------------------
# astral-sh/uv — larger PR-only dataset, exercises the lifecycle path
# ---------------------------------------------------------------------------


class TestAstralUvDataQuality:
    def test_pr_lifecycle_produces_clean_work_items(self):
        from flowmetrics.service import (
            fetch_items_active_in_window,
            make_github_source,
        )
        src = make_github_source(
            "astral-sh/uv",
            cache_dir=CACHE_ROOT / "github",
            read_only=True,
        )
        items = fetch_items_active_in_window(src, WINDOW_START, WINDOW_STOP)
        violations = _all_violations(items)
        assert violations == [], _format_violations_summary(violations)


# ---------------------------------------------------------------------------
# Cassandra Jira — changelog-driven status_intervals
# ---------------------------------------------------------------------------


class TestCassandraJiraDataQuality:
    def test_jira_changelog_produces_clean_work_items(self):
        from flowmetrics.service import (
            fetch_items_active_in_window,
            make_jira_source,
        )
        src = make_jira_source(
            "https://issues.apache.org/jira", "CASSANDRA",
            cache_dir=CACHE_ROOT / "jira",
            read_only=True,
        )
        items = fetch_items_active_in_window(src, WINDOW_START, WINDOW_STOP)
        # Cap at 200 items so the test stays under a few seconds.
        items = items[:200]
        violations = _all_violations(items)
        assert violations == [], _format_violations_summary(violations)
