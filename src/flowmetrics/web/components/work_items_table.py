"""Work-items table component.

Renders a sortable, filterable table of completed work items for
the current contract. Server-side sort + filter via HTMX (per the
slice-2 architectural commitment to HTMX for interactivity).

Reusable: the same partial appears on the dashboard and may be
included on detail pages or future scope-narrowed views.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC
from typing import Literal

import duckdb

from ...utc_dates import to_utc_display_date, to_utc_iso_date

SortKey = Literal[
    "item_id",
    "title",
    "completed_at",
    "cycle_time_days",
]
SortDir = Literal["asc", "desc"]


@dataclass(frozen=True)
class WorkItemRow:
    item_id: str
    title: str
    url: str | None
    source: str  # 'github' or 'jira'
    completed_at: str          # YYYY-MM-DD (UTC)
    completed_at_display: str  # "May 04, 2026"
    cycle_time_days: float


@dataclass(frozen=True)
class WorkItemsTableData:
    rows: tuple[WorkItemRow, ...]
    count: int
    # The filter / sort echoed back so the partial can render the
    # input value + sort-indicator state.
    q: str
    sort: SortKey
    direction: SortDir


# Whitelist for sort keys so we can safely interpolate into SQL
# without parameter binding (DuckDB doesn't support parameterised
# ORDER BY column names). Keep the list closed.
_SORT_COLUMN_SQL: dict[str, str] = {
    "item_id": "item_id",
    "title": "title",
    "completed_at": "completed_at",
    "cycle_time_days": "cycle_time_days",
}


def render(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    q: str | None = None,
    sort: SortKey = "completed_at",
    direction: SortDir = "desc",
) -> WorkItemsTableData:
    """Read rows for the contract with optional title filter + sort.

    `q` does a case-insensitive substring filter on `title`.
    `sort` must be a member of SortKey (whitelist); `direction`
    is asc/desc. Defaults: completed_at DESC (most-recent first).
    """
    if sort not in _SORT_COLUMN_SQL:
        sort = "completed_at"
    if direction not in ("asc", "desc"):
        direction = "desc"
    column = _SORT_COLUMN_SQL[sort]
    order = "ASC" if direction == "asc" else "DESC"
    # Stable secondary order by item_id so equal-key rows don't
    # flicker between requests.
    sql = (
        "SELECT source, item_id, title, url, completed_at, cycle_time_days "
        "FROM work_items "
        "WHERE contract_id = ? AND completed_at IS NOT NULL "
        "  AND (? = '' OR lower(title) LIKE ?) "
        f"ORDER BY {column} {order}, item_id ASC"
    )
    pattern = f"%{(q or '').lower()}%"
    rows = con.execute(sql, [contract_name, q or "", pattern]).fetchall()

    def _aware(d):
        """DuckDB strips TZ on Parquet read — re-attach UTC per the
        warehouse-storage contract. See cycle_time component for the
        same idiom."""
        return d.replace(tzinfo=UTC) if (d and d.tzinfo is None) else d

    table_rows = tuple(
        WorkItemRow(
            item_id=str(item_id),
            title=str(title) if title is not None else "",
            url=str(url) if url is not None else None,
            source=str(source),
            completed_at=to_utc_iso_date(_aware(completed_at)) if completed_at else "",
            completed_at_display=(
                to_utc_display_date(_aware(completed_at)) if completed_at else ""
            ),
            cycle_time_days=(
                float(cycle_time_days) if cycle_time_days is not None else 0.0
            ),
        )
        for (source, item_id, title, url, completed_at, cycle_time_days) in rows
    )

    return WorkItemsTableData(
        rows=table_rows,
        count=len(table_rows),
        q=q or "",
        sort=sort,
        direction=direction,
    )
