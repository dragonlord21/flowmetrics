"""SQL table rename: `contracts` → `workflows` on first open.

Users with an existing `workflows.db` (which still contains a
`contracts` table — that's what we created before this rename)
must not lose their data. WorkflowsDB's constructor performs a
one-time `ALTER TABLE contracts RENAME TO workflows` if it sees
the legacy table name. Idempotent: subsequent opens are no-ops.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from flowmetrics.workflows_db import WorkflowsDB


def _write_legacy_db(path: Path, *, rows: list[tuple[str, str]]) -> None:
    """Create a SQLite file with the old `contracts` table shape and
    populate it. Mirrors what every user's existing workflows.db
    looks like at the moment of upgrade."""
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.executescript("""
            CREATE TABLE contracts (
              id TEXT PRIMARY KEY,
              yaml TEXT NOT NULL,
              archived_at TEXT,
              archived_reason TEXT,
              created_at TEXT NOT NULL,
              updated_at TEXT NOT NULL
            );
            CREATE INDEX contracts_archived_at_idx ON contracts(archived_at);
        """)
        for id_, yaml_ in rows:
            con.execute(
                "INSERT INTO contracts(id, yaml, created_at, updated_at) "
                "VALUES (?, ?, ?, ?)",
                (id_, yaml_, "2026-05-01T00:00:00Z", "2026-05-01T00:00:00Z"),
            )
        con.commit()
    finally:
        con.close()


def _table_names(path: Path) -> set[str]:
    con = sqlite3.connect(path)
    try:
        rows = con.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
    finally:
        con.close()
    return {r[0] for r in rows}


class TestLegacyTableRename:
    def test_legacy_contracts_table_is_renamed_on_open(self, tmp_path):
        db_path = tmp_path / "workflows.db"
        _write_legacy_db(db_path, rows=[("astral-uv", "workflow:\n  name: astral-uv\n")])

        # Open through the wrapper — this is the upgrade moment.
        WorkflowsDB(db_path)

        tables = _table_names(db_path)
        assert "workflows" in tables, (
            f"expected legacy `contracts` table to be renamed to "
            f"`workflows`; got tables={tables}"
        )
        assert "contracts" not in tables, (
            f"expected legacy `contracts` table to be gone after rename; "
            f"got tables={tables}"
        )

    def test_rows_survive_the_rename(self, tmp_path):
        db_path = tmp_path / "workflows.db"
        _write_legacy_db(db_path, rows=[
            ("astral-uv", "workflow:\n  name: astral-uv\n"),
            ("kno", "workflow:\n  name: kno\n"),
        ])

        WorkflowsDB(db_path)

        # The user's two configured workflows are still queryable.
        con = sqlite3.connect(db_path)
        try:
            ids = sorted(r[0] for r in con.execute(
                "SELECT id FROM workflows"
            ).fetchall())
        finally:
            con.close()
        assert ids == ["astral-uv", "kno"]

    def test_idempotent_no_op_on_second_open(self, tmp_path):
        db_path = tmp_path / "workflows.db"
        _write_legacy_db(db_path, rows=[("astral-uv", "workflow:\n  name: astral-uv\n")])

        WorkflowsDB(db_path)  # first: renames
        WorkflowsDB(db_path)  # second: no-op, must not raise

        tables = _table_names(db_path)
        assert "workflows" in tables
        assert "contracts" not in tables


class TestAlreadyRenamedDb:
    def test_workflows_table_alone_is_a_no_op(self, tmp_path):
        """A fresh install creates the `workflows` table directly.
        Re-opening it must not try to rename anything (the legacy
        table doesn't exist)."""
        db_path = tmp_path / "workflows.db"
        # Wrapper creates the schema with `workflows` directly.
        WorkflowsDB(db_path)
        WorkflowsDB(db_path)  # idempotent

        tables = _table_names(db_path)
        assert "workflows" in tables
        assert "contracts" not in tables


class TestBothTablesPresent:
    def test_raises_when_both_contracts_and_workflows_exist(self, tmp_path):
        """If both tables somehow exist (operator-induced ambiguity:
        e.g., copied an old DB on top of a new one), surface the
        problem instead of silently picking one."""
        db_path = tmp_path / "workflows.db"
        con = sqlite3.connect(db_path)
        try:
            con.executescript("""
                CREATE TABLE contracts (id TEXT PRIMARY KEY, yaml TEXT NOT NULL,
                  archived_at TEXT, archived_reason TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
                CREATE TABLE workflows (id TEXT PRIMARY KEY, yaml TEXT NOT NULL,
                  archived_at TEXT, archived_reason TEXT,
                  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
            """)
            con.commit()
        finally:
            con.close()

        with pytest.raises(Exception) as exc:
            WorkflowsDB(db_path)
        msg = str(exc.value).lower()
        assert "contracts" in msg and "workflows" in msg
