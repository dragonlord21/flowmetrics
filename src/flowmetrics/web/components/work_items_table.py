"""Work-items table component.

Renders a sortable, filterable table of completed work items for
the current contract. Server-side sort + filter via HTMX (per the
slice-2 architectural commitment to HTMX for interactivity).

Reusable: the same partial appears on the dashboard and may be
included on detail pages or future scope-narrowed views.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import duckdb

from ...utc_dates import attach_utc, to_utc_display_date, to_utc_iso_date

SortKey = Literal[
    "item_id",
    "title",
    "created_at",
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
    created_at: str            # YYYY-MM-DD (UTC) — start date
    created_at_display: str    # "May 04, 2026"
    completed_at: str          # YYYY-MM-DD (UTC) — end date
    completed_at_display: str  # "May 04, 2026"
    cycle_time_days: float


@dataclass(frozen=True)
class WorkItemsTableData:
    rows: tuple[WorkItemRow, ...]
    count: int
    # The filter / sort echoed back so the partial can render the
    # input value + sort-indicator state. `completed_on` narrows to
    # a single UTC date — used when the viewer clicks a bar on the
    # throughput chart. `None`/`""` means no date filter.
    q: str
    completed_on: str
    # Human-readable form of `completed_on` ("May 04, 2026"). Empty
    # when no date filter is active. Computed in Python so the
    # template doesn't have to dig through the rows to find a
    # matching display string.
    completed_on_display: str
    sort: SortKey
    direction: SortDir


# Whitelist for sort keys so we can safely interpolate into SQL
# without parameter binding (DuckDB doesn't support parameterised
# ORDER BY column names). Keep the list closed.
_SORT_COLUMN_SQL: dict[str, str] = {
    "item_id": "item_id",
    "title": "title",
    "created_at": "created_at",
    "completed_at": "completed_at",
    "cycle_time_days": "cycle_time_days",
}


def render(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    q: str | None = None,
    completed_on: str | None = None,
    sort: SortKey = "completed_at",
    direction: SortDir = "desc",
) -> WorkItemsTableData:
    """Read rows for the contract with optional title filter + sort.

    `q` does a case-insensitive substring filter on `title`.
    `completed_on` is a UTC ISO date (YYYY-MM-DD) — only items
    completed on that exact date are returned. Used by the
    throughput chart's bar-click handler to drill into a day.
    Both filters compose (AND semantics).
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
        "SELECT source, item_id, title, url, "
        "       created_at, completed_at, cycle_time_days "
        "FROM work_items "
        "WHERE contract_id = ? "
        "  AND created_at IS NOT NULL "
        "  AND completed_at IS NOT NULL "
        "  AND (? = '' OR lower(title) LIKE ?) "
        "  AND (? = '' OR CAST(completed_at AS DATE) = CAST(? AS DATE)) "
        f"ORDER BY {column} {order}, item_id ASC"
    )
    pattern = f"%{(q or '').lower()}%"
    completed_on_arg = completed_on or ""
    rows = con.execute(
        sql,
        [
            contract_name,
            q or "", pattern,
            completed_on_arg, completed_on_arg,
        ],
    ).fetchall()

    table_rows = tuple(
        WorkItemRow(
            item_id=str(item_id),
            title=str(title) if title is not None else "",
            url=str(url) if url is not None else None,
            source=str(source),
            created_at=to_utc_iso_date(attach_utc(created_at)) if created_at else "",
            created_at_display=(
                to_utc_display_date(attach_utc(created_at)) if created_at else ""
            ),
            completed_at=to_utc_iso_date(attach_utc(completed_at)) if completed_at else "",
            completed_at_display=(
                to_utc_display_date(attach_utc(completed_at)) if completed_at else ""
            ),
            cycle_time_days=(
                float(cycle_time_days) if cycle_time_days is not None else 0.0
            ),
        )
        for (source, item_id, title, url, created_at, completed_at, cycle_time_days) in rows
    )

    # Pre-format the date filter's display string so the template
    # doesn't have to. Parse the ISO string as a UTC midnight
    # datetime so the shared formatter accepts it.
    if completed_on_arg:
        from datetime import UTC, date, datetime

        try:
            iso_date = date.fromisoformat(completed_on_arg)
            anchor = datetime(
                iso_date.year, iso_date.month, iso_date.day, tzinfo=UTC
            )
            completed_on_display = to_utc_display_date(anchor)
        except ValueError:
            # Malformed filter value — render the ISO form rather
            # than throwing; downstream just sees the empty result
            # set.
            completed_on_display = completed_on_arg
    else:
        completed_on_display = ""

    return WorkItemsTableData(
        rows=table_rows,
        count=len(table_rows),
        q=q or "",
        completed_on=completed_on_arg,
        completed_on_display=completed_on_display,
        sort=sort,
        direction=direction,
    )
