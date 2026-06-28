"""Layer 1 — raw warehouse queries.

Each function takes a DuckDB connection and returns a list of
frozen row dataclasses. No windowing, no decisions: this layer
only fetches. `flowmetrics.charts` (Layer 2) windows and decides.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

import duckdb


@dataclass(frozen=True)
class CompletedItem:
    """One completed work item, straight from `work_items`.

    `completed_at` is non-null by construction (the query filters
    on it). `cycle_time_days` can still be null — a data-quality
    gap the model layer decides how to treat.
    """

    item_id: str
    title: str | None
    url: str | None
    completed_at: datetime
    cycle_time_days: float | None


@dataclass(frozen=True)
class InFlightItem:
    """One in-flight work item at a snapshot date, with its current
    workflow state resolved — the latest transition at or before
    the snapshot, or `"Unknown"` if it has never transitioned."""

    item_id: str
    title: str | None
    url: str | None
    created_at: datetime
    current_state: str


def completed_items(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    issuetypes: list[str] | tuple[str, ...] | None = None,
) -> list[CompletedItem]:
    """Every completed item for `contract_name`, oldest completion
    first. In-flight items (no `completed_at`) are excluded."""
    if issuetypes is not None:
        if not issuetypes:
            return []
        placeholders = ",".join("?" for _ in issuetypes)
        query = f"""
            SELECT item_id, title, url, completed_at, cycle_time_days
            FROM work_items
            WHERE contract_id = ? AND completed_at IS NOT NULL
              AND issuetype IN ({placeholders})
            ORDER BY completed_at
            """
        params = [contract_name, *issuetypes]
    else:
        query = """
            SELECT item_id, title, url, completed_at, cycle_time_days
            FROM work_items
            WHERE contract_id = ? AND completed_at IS NOT NULL
            ORDER BY completed_at
            """
        params = [contract_name]

    rows = con.execute(query, params).fetchall()
    return [
        CompletedItem(
            item_id=str(item_id),
            title=str(title) if title is not None else None,
            url=str(url) if url is not None else None,
            completed_at=completed_at,
            cycle_time_days=(
                float(cycle_time_days)
                if cycle_time_days is not None
                else None
            ),
        )
        for (item_id, title, url, completed_at, cycle_time_days) in rows
    ]


def in_flight_snapshot(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    asof: date,
    issuetypes: list[str] | tuple[str, ...] | None = None,
) -> list[InFlightItem]:
    """Items in flight at `asof` — created on or before it and not
    yet completed by it — each tagged with its current state (the
    latest transition at or before `asof`). One window-function
    query, not N+1: per-item state resolution is a single pass.
    """
    if issuetypes is not None:
        if not issuetypes:
            return []
        placeholders = ",".join("?" for _ in issuetypes)
        query = f"""
            WITH latest_state AS (
                SELECT t.item_id, t.stage,
                       ROW_NUMBER() OVER (
                           PARTITION BY t.item_id ORDER BY t.entered_at DESC
                       ) AS rn
                FROM transitions t
                INNER JOIN work_items w ON t.item_id = w.item_id AND t.contract_id = w.contract_id
                WHERE t.contract_id = ?
                  AND CAST(t.entered_at AS DATE) <= CAST(? AS DATE)
                  AND w.issuetype IN ({placeholders})
            )
            SELECT w.item_id, w.title, w.url, w.created_at,
                   COALESCE(ls.stage, 'Unknown') AS current_state
            FROM work_items w
            LEFT JOIN latest_state ls
              ON ls.item_id = w.item_id AND ls.rn = 1
            WHERE w.contract_id = ?
              AND w.created_at IS NOT NULL
              AND CAST(w.created_at AS DATE) <= CAST(? AS DATE)
              AND (w.completed_at IS NULL
                   OR CAST(w.completed_at AS DATE) > CAST(? AS DATE))
              AND w.issuetype IN ({placeholders})
            ORDER BY w.created_at ASC
            """
        params = [contract_name, asof, *issuetypes, contract_name, asof, asof, *issuetypes]
    else:
        query = """
            WITH latest_state AS (
                SELECT item_id, stage,
                       ROW_NUMBER() OVER (
                           PARTITION BY item_id ORDER BY entered_at DESC
                       ) AS rn
                FROM transitions
                WHERE contract_id = ?
                  AND CAST(entered_at AS DATE) <= CAST(? AS DATE)
            )
            SELECT w.item_id, w.title, w.url, w.created_at,
                   COALESCE(ls.stage, 'Unknown') AS current_state
            FROM work_items w
            LEFT JOIN latest_state ls
              ON ls.item_id = w.item_id AND ls.rn = 1
            WHERE w.contract_id = ?
              AND w.created_at IS NOT NULL
              AND CAST(w.created_at AS DATE) <= CAST(? AS DATE)
              AND (w.completed_at IS NULL
                   OR CAST(w.completed_at AS DATE) > CAST(? AS DATE))
            ORDER BY w.created_at ASC
            """
        params = [contract_name, asof, contract_name, asof, asof]

    rows = con.execute(query, params).fetchall()
    return [
        InFlightItem(
            item_id=str(item_id),
            title=str(title) if title is not None else None,
            url=str(url) if url is not None else None,
            created_at=created_at,
            current_state=str(current_state),
        )
        for (item_id, title, url, created_at, current_state) in rows
    ]


@dataclass(frozen=True)
class StageEntry:
    """An item's first entry into a stage, by calendar date."""

    item_id: str
    stage: str
    entered_date: date


def first_stage_entries(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    *,
    only_stages: tuple[str, ...] | None = None,
    issuetypes: list[str] | tuple[str, ...] | None = None,
) -> list[StageEntry]:
    """First-entry date per (item, stage) for `contract_name`.
    Collapses ping-pong transitions to the first visit. When
    `only_stages` is set, transitions for other states are
    filtered out at the SQL layer — that's how backlog states get
    excluded from the CFD without round-tripping through Python.
    """
    if only_stages is not None and not only_stages:
        return []
    if issuetypes is not None and not issuetypes:
        return []

    where_clauses = ["t.contract_id = ?"]
    params = [contract_name]

    if only_stages is not None:
        placeholders = ",".join("?" for _ in only_stages)
        where_clauses.append(f"t.stage IN ({placeholders})")
        params.extend(only_stages)

    if issuetypes is not None:
        join_clause = "INNER JOIN work_items w ON t.item_id = w.item_id AND t.contract_id = w.contract_id"
        placeholders_issue = ",".join("?" for _ in issuetypes)
        where_clauses.append(f"w.issuetype IN ({placeholders_issue})")
        params.extend(issuetypes)
        query = f"""
            SELECT t.item_id, t.stage,
                   CAST(min(t.entered_at) AS DATE) AS entered_date
            FROM transitions t
            {join_clause}
            WHERE {" AND ".join(where_clauses)}
            GROUP BY t.item_id, t.stage
        """
    else:
        query = f"""
            SELECT t.item_id, t.stage,
                   CAST(min(t.entered_at) AS DATE) AS entered_date
            FROM transitions t
            WHERE {" AND ".join(where_clauses)}
            GROUP BY t.item_id, t.stage
        """

    rows = con.execute(query, params).fetchall()
    return [
        StageEntry(
            item_id=str(item_id),
            stage=str(stage),
            entered_date=entered_date,
        )
        for (item_id, stage, entered_date) in rows
    ]


def observed_stages(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    issuetypes: list[str] | tuple[str, ...] | None = None,
) -> list[str]:
    """Every distinct stage that has appeared in `transitions` for
    the workflow, sorted alphabetically."""
    if issuetypes is not None:
        if not issuetypes:
            return []
        placeholders = ",".join("?" for _ in issuetypes)
        rows = con.execute(
            f"""
            SELECT DISTINCT t.stage
            FROM transitions t
            INNER JOIN work_items w ON t.item_id = w.item_id AND t.contract_id = w.contract_id
            WHERE t.contract_id = ? AND w.issuetype IN ({placeholders})
            """,
            [contract_name, *issuetypes],
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT DISTINCT stage FROM transitions WHERE contract_id = ?",
            [contract_name],
        ).fetchall()
    return sorted(str(s) for (s,) in rows)


def pairwise_stage_precedence(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    issuetypes: list[str] | tuple[str, ...] | None = None,
) -> list[tuple[str, str, int]]:
    """For every ordered pair `(A, B)` of stages, the count of
    items whose first entry into A preceded their first entry
    into B. The CFD's stage-order inference is built on these
    counts when no workflow YAML pins the workflow."""
    if issuetypes is not None:
        if not issuetypes:
            return []
        placeholders = ",".join("?" for _ in issuetypes)
        rows = con.execute(
            f"""
            WITH item_stages AS (
                SELECT t.item_id, t.stage,
                       min(t.entered_at) AS first_entered
                FROM transitions t
                INNER JOIN work_items w ON t.item_id = w.item_id AND t.contract_id = w.contract_id
                WHERE t.contract_id = ? AND w.issuetype IN ({placeholders})
                GROUP BY t.item_id, t.stage
            )
            SELECT a.stage AS earlier, b.stage AS later, count(*) AS cnt
            FROM item_stages a
            JOIN item_stages b ON a.item_id = b.item_id
            WHERE a.first_entered < b.first_entered
            GROUP BY 1, 2
            """,
            [contract_name, *issuetypes],
        ).fetchall()
    else:
        rows = con.execute(
            """
            WITH item_stages AS (
                SELECT item_id, stage,
                       min(entered_at) AS first_entered
                FROM transitions
                WHERE contract_id = ?
                GROUP BY item_id, stage
            )
            SELECT a.stage AS earlier, b.stage AS later, count(*) AS cnt
            FROM item_stages a
            JOIN item_stages b ON a.item_id = b.item_id
            WHERE a.first_entered < b.first_entered
            GROUP BY 1, 2
            """,
            [contract_name],
        ).fetchall()
    return [(str(a), str(b), int(c)) for (a, b, c) in rows]


def creations_by_day(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    issuetypes: list[str] | tuple[str, ...] | None = None,
) -> list[tuple[date, int]]:
    """Per-day count of work items created. Ascending date order;
    days with no creations are NOT included (the model fills them
    in)."""
    if issuetypes is not None:
        if not issuetypes:
            return []
        placeholders = ",".join("?" for _ in issuetypes)
        rows = con.execute(
            "SELECT CAST(created_at AS DATE), count(*) "
            "FROM work_items "
            f"WHERE contract_id = ? AND created_at IS NOT NULL AND issuetype IN ({placeholders}) "
            "GROUP BY 1 ORDER BY 1",
            [contract_name, *issuetypes],
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT CAST(created_at AS DATE), count(*) "
            "FROM work_items "
            "WHERE contract_id = ? AND created_at IS NOT NULL "
            "GROUP BY 1 ORDER BY 1",
            [contract_name],
        ).fetchall()
    return [(d, int(c)) for d, c in rows]


def count_open_items(
    con: duckdb.DuckDBPyConnection,
    contract_name: str,
    issuetypes: list[str] | tuple[str, ...] | None = None,
) -> int:
    """How many work items have no completion recorded — i.e.
    whether the warehouse has ever captured open work at all
    (distinguishes a never-captured snapshot from a genuinely
    empty one)."""
    if issuetypes is not None:
        if not issuetypes:
            return 0
        placeholders = ",".join("?" for _ in issuetypes)
        return int(
            con.execute(
                "SELECT count(*) FROM work_items "
                "WHERE contract_id = ? AND completed_at IS NULL "
                f"  AND issuetype IN ({placeholders})",
                [contract_name, *issuetypes],
            ).fetchone()[0]
        )
    return int(
        con.execute(
            "SELECT count(*) FROM work_items "
            "WHERE contract_id = ? AND completed_at IS NULL",
            [contract_name],
        ).fetchone()[0]
    )


def completion_date_range(
    con: duckdb.DuckDBPyConnection, contract_name: str
) -> tuple[date | None, date | None]:
    """`(earliest, latest)` completed_at dates the warehouse holds
    for `contract_name`. `(None, None)` when no completions yet —
    drives both the filter-bar date-input bounds and the empty-
    state UIs that name where data actually exists."""
    row = con.execute(
        "SELECT min(CAST(completed_at AS DATE)), "
        "       max(CAST(completed_at AS DATE)) "
        "FROM work_items "
        "WHERE contract_id = ? AND completed_at IS NOT NULL",
        [contract_name],
    ).fetchone()
    if row and row[1] is not None:
        return row[0], row[1]
    return None, None


def latest_materialized_at(
    con: duckdb.DuckDBPyConnection, contract_name: str
) -> date | None:
    """The latest materialize date for `contract_name` — i.e. the
    asof of the warehouse's most recent in-flight snapshot. None
    when the warehouse holds no rows yet."""
    row = con.execute(
        "SELECT max(materialized_at) FROM work_items "
        "WHERE contract_id = ?",
        [contract_name],
    ).fetchone()
    mat = row[0] if row else None
    return mat.date() if mat is not None else None


def observed_issuetypes(
    con: duckdb.DuckDBPyConnection, contract_name: str
) -> list[str]:
    """Every distinct, non-null issuetype in `work_items` for the contract,
    sorted alphabetically."""
    rows = con.execute(
        """
        SELECT DISTINCT issuetype FROM work_items
        WHERE contract_id = ? AND issuetype IS NOT NULL
        """,
        [contract_name],
    ).fetchall()
    return sorted(str(s) for (s,) in rows)
