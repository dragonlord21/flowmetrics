"""Warehouse schema evolution and union_by_name tests."""

from __future__ import annotations

from datetime import datetime, UTC
from pathlib import Path
import duckdb
import pytest

from flowmetrics.compute import WorkItem
from flowmetrics.workflow import Workflow
from flowmetrics.materialize import _write_work_items_parquet
from flowmetrics.warehouse.connection import open_warehouse


def test_materialize_writes_issuetype_column(tmp_path: Path):
    out_path = tmp_path / "items.parquet"
    workflow = Workflow(
        name="test_workflow",
        source="jira",
        jira_url="https://jira.example.com",
        jira_project="TEST",
    )
    items = [
        WorkItem(
            item_id="TEST-1",
            title="A Test Issue",
            created_at=datetime(2026, 6, 28, 9, 0, tzinfo=UTC),
            completed_at=datetime(2026, 6, 28, 17, 0, tzinfo=UTC),
            author_login="peterl",
            url="https://jira.example.com/browse/TEST-1",
            issuetype="Bug",
        )
    ]

    _write_work_items_parquet(
        items=items,
        workflow=workflow,
        run_id="run_123",
        materialized_at=datetime(2026, 6, 28, 18, 0, tzinfo=UTC),
        out_path=out_path,
    )

    con = duckdb.connect()
    res = con.execute(f"SELECT issuetype FROM read_parquet('{out_path}')").fetchall()
    con.close()
    assert res == [("Bug",)]


def test_warehouse_schema_evolution_union_by_name(tmp_path: Path):
    workflow = "test_evolution"
    base = tmp_path / "work_items" / f"contract_id={workflow}"
    
    # 1. Older style parquet file without the issuetype column (May 2026 schema)
    snap1 = base / "year=2026" / "month=06" / "day=28" / "items-old.parquet"
    snap1.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute(
        """CREATE TEMP TABLE wi (
            source VARCHAR, repo VARCHAR, item_id VARCHAR,
            title VARCHAR, url VARCHAR, author VARCHAR, is_bot BOOLEAN,
            created_at TIMESTAMP, completed_at TIMESTAMP,
            cycle_time_days DOUBLE, contract_id VARCHAR,
            materialized_at TIMESTAMP, run_id VARCHAR
        )"""
    )
    con.execute(
        "INSERT INTO wi VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "jira", "TEST", "TEST-1", "Old Issue", "https://jira.example.com/browse/TEST-1",
            "alice", False,
            datetime(2026, 6, 28, 9, 0),
            None,
            None,
            workflow,
            datetime(2026, 6, 28, 10, 0),
            "run_old",
        ]
    )
    p = str(snap1).replace("'", "''")
    con.execute(f"COPY wi TO '{p}' (FORMAT PARQUET)")
    con.close()

    # 2. New style parquet file with the issuetype column
    snap2 = base / "year=2026" / "month=06" / "day=29" / "items-new.parquet"
    snap2.parent.mkdir(parents=True, exist_ok=True)

    con = duckdb.connect()
    con.execute(
        """CREATE TEMP TABLE wi_new (
            source VARCHAR, repo VARCHAR, item_id VARCHAR,
            title VARCHAR, url VARCHAR, author VARCHAR, is_bot BOOLEAN,
            created_at TIMESTAMP, completed_at TIMESTAMP,
            cycle_time_days DOUBLE, contract_id VARCHAR,
            materialized_at TIMESTAMP, run_id VARCHAR,
            issuetype VARCHAR
        )"""
    )
    con.execute(
        "INSERT INTO wi_new VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            "jira", "TEST", "TEST-2", "New Issue", "https://jira.example.com/browse/TEST-2",
            "bob", False,
            datetime(2026, 6, 29, 9, 0),
            None,
            None,
            workflow,
            datetime(2026, 6, 29, 10, 0),
            "run_new",
            "Task",
        ]
    )
    p2 = str(snap2).replace("'", "''")
    con.execute(f"COPY wi_new TO '{p2}' (FORMAT PARQUET)")
    con.close()

    # 3. Read both back via the warehouse view. It must union them cleanly, 
    # filling NULL for the older file's issuetype column.
    db_con = open_warehouse(tmp_path)
    res = db_con.execute(
        "SELECT item_id, title, issuetype FROM work_items ORDER BY item_id"
    ).fetchall()
    db_con.close()

    assert res == [
        ("TEST-1", "Old Issue", None),
        ("TEST-2", "New Issue", "Task"),
    ]
