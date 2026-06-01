"""`flow backup` + `flow restore` — warehouse portability.

A backup is a single timestamped `.tar.gz` carrying the whole
warehouse (Parquet tables + run manifests) plus a header that
records schema version, flowmetrics version, DuckDB version, and a
SHA-256 of every file. Restore verifies the header + checksums
before touching disk; refuses to clobber a non-empty target unless
--force.

The contract is: backup → restore → DuckDB read returns identical
rows. That's what the round-trip test pins; nothing else matters.
"""

from __future__ import annotations

import gzip
import io
import json
import tarfile
from pathlib import Path

import pytest
from click.testing import CliRunner

from flowmetrics.cli import cli


def _make_tiny_warehouse(root: Path) -> None:
    """Synthesise the minimum directory structure `flow restore`
    needs to round-trip: a couple of Parquet-ish leaves under
    `work_items/` and a tiny run manifest."""
    work = root / "work_items" / "contract_id=demo" / "year=2026" / "month=05" / "day=10"
    work.mkdir(parents=True)
    (work / "items-run1.parquet").write_bytes(b"PAR1\x00\x00\x00FAKE")
    runs = root / "runs" / "demo" / "run_id=run1"
    runs.mkdir(parents=True)
    (runs / "manifest.json").write_text(json.dumps({"items_fetched": 7}))


def _make_tiny_contracts_db(contracts_dir: Path) -> None:
    """Synthesise a SQLite contracts.db with the schema flowmetrics
    uses — a single contracts table with one row. Enough for the
    backup helper to copy + the restore round-trip to verify
    byte-for-byte equality."""
    import sqlite3
    contracts_dir.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(contracts_dir / "contracts.db")
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
            INSERT INTO contracts(id, yaml, created_at, updated_at)
            VALUES ('demo', 'contract:\\n  name: demo\\n',
                    '2026-05-31T00:00:00Z',
                    '2026-05-31T00:00:00Z');
        """)
        con.commit()
    finally:
        con.close()


class TestBackupShape:
    def test_writes_a_targz_with_a_header_and_checksums(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        _make_tiny_warehouse(data)
        out = tmp_path / "backup.tar.gz"

        res = CliRunner().invoke(cli, [
            "backup", "--data-dir", str(data), "--output", str(out),
        ], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        assert out.exists()
        assert out.suffix == ".gz"

        with tarfile.open(out, "r:gz") as tar:
            names = tar.getnames()
        # Header lives at a known path inside the tarball.
        assert any(n.endswith("flowmetrics-backup.json") for n in names)
        # The warehouse files are there too.
        assert any("items-run1.parquet" in n for n in names)

    def test_header_lists_schema_and_per_file_sha256(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        _make_tiny_warehouse(data)
        out = tmp_path / "backup.tar.gz"

        CliRunner().invoke(cli, [
            "backup", "--data-dir", str(data), "--output", str(out),
        ], catch_exceptions=False)

        with tarfile.open(out, "r:gz") as tar:
            member = next(
                m for m in tar.getmembers()
                if m.name.endswith("flowmetrics-backup.json")
            )
            f = tar.extractfile(member)
            assert f is not None
            header = json.loads(f.read())
        assert header["schema"] == "flowmetrics.backup.v1"
        assert "flowmetrics_version" in header
        # The checksum table is non-empty and keyed on relative paths.
        assert header["files"]
        for relpath, digest in header["files"].items():
            assert relpath.startswith(("work_items/", "runs/"))
            assert len(digest) == 64  # hex sha256


class TestRoundTrip:
    def test_backup_then_restore_reproduces_the_warehouse_exactly(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        _make_tiny_warehouse(data)
        out = tmp_path / "backup.tar.gz"

        CliRunner().invoke(cli, [
            "backup", "--data-dir", str(data), "--output", str(out),
        ], catch_exceptions=False)

        # Restore into a brand-new directory.
        restored = tmp_path / "restored"
        res = CliRunner().invoke(cli, [
            "restore", "--input", str(out), "--data-dir", str(restored),
        ], catch_exceptions=False)
        assert res.exit_code == 0, res.output

        # Every file the original had, the restored copy has, byte-identical.
        original = {p.relative_to(data): p.read_bytes()
                    for p in data.rglob("*") if p.is_file()}
        new = {p.relative_to(restored): p.read_bytes()
               for p in restored.rglob("*") if p.is_file()}
        assert original == new


class TestRestoreSafety:
    def test_refuses_to_clobber_non_empty_target_without_force(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        _make_tiny_warehouse(data)
        out = tmp_path / "backup.tar.gz"
        CliRunner().invoke(cli, [
            "backup", "--data-dir", str(data), "--output", str(out),
        ], catch_exceptions=False)

        # Target already has files; restore should bail.
        target = tmp_path / "target"
        target.mkdir()
        (target / "existing.txt").write_text("keep me")

        res = CliRunner().invoke(cli, [
            "restore", "--input", str(out), "--data-dir", str(target),
        ], catch_exceptions=False)
        assert res.exit_code != 0
        # File the user already had MUST be untouched.
        assert (target / "existing.txt").read_text() == "keep me"

    def test_force_clobbers(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        _make_tiny_warehouse(data)
        out = tmp_path / "backup.tar.gz"
        CliRunner().invoke(cli, [
            "backup", "--data-dir", str(data), "--output", str(out),
        ], catch_exceptions=False)

        target = tmp_path / "target"
        target.mkdir()
        (target / "stale.txt").write_text("clobber me")

        res = CliRunner().invoke(cli, [
            "restore", "--input", str(out), "--data-dir", str(target),
            "--force",
        ], catch_exceptions=False)
        assert res.exit_code == 0, res.output

    def test_corrupted_tarball_fails_before_extraction(self, tmp_path):
        # A clearly invalid tar.gz: random bytes wrapped in gzip.
        bad = tmp_path / "bad.tar.gz"
        bad.write_bytes(gzip.compress(b"this is not a tarball at all"))
        target = tmp_path / "target"

        res = CliRunner().invoke(cli, [
            "restore", "--input", str(bad), "--data-dir", str(target),
        ], catch_exceptions=False)
        assert res.exit_code != 0
        # We didn't create the target — restore bailed first.
        assert not target.exists() or not list(target.iterdir())

    def test_tampered_payload_fails_checksum(self, tmp_path):
        """Tamper a Parquet file inside an otherwise-valid backup;
        restore must refuse before writing anything to disk."""
        data = tmp_path / "data"
        data.mkdir()
        _make_tiny_warehouse(data)
        out = tmp_path / "backup.tar.gz"
        CliRunner().invoke(cli, [
            "backup", "--data-dir", str(data), "--output", str(out),
        ], catch_exceptions=False)

        # Rewrite the tarball, mutating the Parquet payload but
        # leaving the header (and its checksums) untouched.
        with tarfile.open(out, "r:gz") as src:
            members = src.getmembers()
            payloads = {
                m.name: (src.extractfile(m).read() if not m.isdir() else None)
                for m in members
            }
        for k in list(payloads):
            if k.endswith("items-run1.parquet") and payloads[k] is not None:
                payloads[k] = b"TAMPERED"
        with tarfile.open(out, "w:gz") as dst:
            for m in members:
                if payloads[m.name] is None:
                    dst.addfile(m)
                    continue
                m.size = len(payloads[m.name])
                dst.addfile(m, io.BytesIO(payloads[m.name]))

        target = tmp_path / "target"
        res = CliRunner().invoke(cli, [
            "restore", "--input", str(out), "--data-dir", str(target),
        ], catch_exceptions=False)
        assert res.exit_code != 0
        # Same fail-before-write contract as the corrupted-tar case.
        assert not target.exists() or not list(target.iterdir())


class TestIncludeCache:
    def test_cache_excluded_by_default(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        _make_tiny_warehouse(data)
        # Cache directory inside data/ — should be skipped.
        cache = data / ".cache"
        cache.mkdir()
        (cache / "should-not-ship.txt").write_text("re-fetchable")
        out = tmp_path / "backup.tar.gz"
        CliRunner().invoke(cli, [
            "backup", "--data-dir", str(data), "--output", str(out),
        ], catch_exceptions=False)
        with tarfile.open(out, "r:gz") as tar:
            assert not any(".cache" in n for n in tar.getnames())

    def test_include_cache_flag_ships_it(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        _make_tiny_warehouse(data)
        cache = data / ".cache"
        cache.mkdir()
        (cache / "include-me.txt").write_text("ok")
        out = tmp_path / "backup.tar.gz"
        CliRunner().invoke(cli, [
            "backup", "--data-dir", str(data), "--output", str(out),
            "--include-cache",
        ], catch_exceptions=False)
        with tarfile.open(out, "r:gz") as tar:
            assert any("include-me.txt" in n for n in tar.getnames())


class TestDefaultOutput:
    def test_default_filename_is_dated_under_data_dir(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        _make_tiny_warehouse(data)
        # No --output → land at <data-dir>/_backups/<timestamp>.tar.gz
        res = CliRunner().invoke(cli, [
            "backup", "--data-dir", str(data),
        ], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        backups = list((data / "_backups").glob("flowmetrics-*.tar.gz"))
        assert len(backups) == 1


@pytest.mark.skip(reason="S3 mode covered separately when boto3 is the install path")
class TestS3Target:
    """S3 round-trip lives behind an optional `boto3` dep. Skipped
    from the default suite; exercise manually against MinIO or in a
    dedicated integration matrix entry."""


class TestContractsDbBackup:
    """The config DB (`<workflows-dir>/contracts.db`, SQLite) ships
    inside the same tarball as the data warehouse — namespaced under
    `_config/` so old data-only backups are still restorable. The
    SQLite snapshot uses the online backup API (`con.backup()`) so a
    concurrent writer can't tear the file mid-copy."""

    def test_backup_includes_contracts_db_when_workflows_dir_given(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        _make_tiny_warehouse(data)
        contracts = tmp_path / "contracts"
        _make_tiny_contracts_db(contracts)
        out = tmp_path / "backup.tar.gz"

        res = CliRunner().invoke(cli, [
            "backup",
            "--data-dir", str(data),
            "--workflows-dir", str(contracts),
            "--output", str(out),
        ], catch_exceptions=False)
        assert res.exit_code == 0, res.output

        with tarfile.open(out, "r:gz") as tar:
            names = tar.getnames()
        # Config namespace lives under `_config/` (mirrors the
        # `_backups/` + `_status/` underscore-prefix convention).
        assert any(n == "_config/contracts.db" for n in names), names

    def test_backup_without_workflows_dir_omits_config(self, tmp_path):
        """No --workflows-dir → backup carries only the data warehouse
        (no `_config/`). Preserves the pre-extension behaviour."""
        data = tmp_path / "data"
        data.mkdir()
        _make_tiny_warehouse(data)
        out = tmp_path / "backup.tar.gz"

        CliRunner().invoke(cli, [
            "backup", "--data-dir", str(data), "--output", str(out),
        ], catch_exceptions=False)

        with tarfile.open(out, "r:gz") as tar:
            assert not any(n.startswith("_config/") for n in tar.getnames())

    def test_contracts_db_snapshot_round_trips_byte_for_byte(self, tmp_path):
        """A round-trip through the SQLite online-backup API may not
        reproduce the source DB byte-for-byte (page layout can shift),
        but it MUST reproduce row-level content. Pin on row content,
        not byte equality, so the test reflects what we actually
        guarantee."""
        import sqlite3
        data = tmp_path / "data"
        data.mkdir()
        _make_tiny_warehouse(data)
        contracts = tmp_path / "contracts"
        _make_tiny_contracts_db(contracts)

        out = tmp_path / "backup.tar.gz"
        CliRunner().invoke(cli, [
            "backup",
            "--data-dir", str(data),
            "--workflows-dir", str(contracts),
            "--output", str(out),
        ], catch_exceptions=False)

        restored_data = tmp_path / "restored_data"
        restored_contracts = tmp_path / "restored_contracts"
        res = CliRunner().invoke(cli, [
            "restore",
            "--input", str(out),
            "--data-dir", str(restored_data),
            "--workflows-dir", str(restored_contracts),
        ], catch_exceptions=False)
        assert res.exit_code == 0, res.output

        # Restored contracts.db has the same rows as the source.
        src = sqlite3.connect(contracts / "contracts.db")
        dst = sqlite3.connect(restored_contracts / "contracts.db")
        try:
            src_rows = src.execute(
                "SELECT id, yaml, created_at FROM contracts ORDER BY id"
            ).fetchall()
            dst_rows = dst.execute(
                "SELECT id, yaml, created_at FROM contracts ORDER BY id"
            ).fetchall()
        finally:
            src.close()
            dst.close()
        assert src_rows == dst_rows
        assert src_rows  # sanity — fixture not empty

    def test_contracts_db_snapshot_is_safe_under_concurrent_writer(self, tmp_path):
        """The point of using SQLite's online backup API (instead of
        a raw file copy) is that a concurrent writer holding an
        OPEN connection can't corrupt the snapshot. Exercise that:
        open a write connection on contracts.db, then run `flow
        backup`. The backup must succeed and the snapshot must
        contain the rows committed at the moment the snapshot
        started."""
        import sqlite3
        data = tmp_path / "data"
        data.mkdir()
        _make_tiny_warehouse(data)
        contracts = tmp_path / "contracts"
        _make_tiny_contracts_db(contracts)

        # Open a long-lived writer connection (simulating the live
        # server). With a raw `shutil.copy` this would race; with
        # the online backup API it's safe.
        live = sqlite3.connect(contracts / "contracts.db")
        try:
            live.execute("BEGIN")
            live.execute(
                "INSERT INTO contracts(id, yaml, created_at, updated_at) "
                "VALUES ('uncommitted', 'x', '2026-06-01', '2026-06-01')"
            )
            # NOT committed — the snapshot must not see this row.

            out = tmp_path / "backup.tar.gz"
            res = CliRunner().invoke(cli, [
                "backup",
                "--data-dir", str(data),
                "--workflows-dir", str(contracts),
                "--output", str(out),
            ], catch_exceptions=False)
            assert res.exit_code == 0, res.output
        finally:
            live.rollback()
            live.close()

        # Verify the snapshot
        restored = tmp_path / "restored_contracts"
        CliRunner().invoke(cli, [
            "restore",
            "--input", str(out),
            "--data-dir", str(tmp_path / "restored_data"),
            "--workflows-dir", str(restored),
            "--config-only",
        ], catch_exceptions=False)
        dst = sqlite3.connect(restored / "contracts.db")
        try:
            ids = [r[0] for r in dst.execute("SELECT id FROM contracts").fetchall()]
        finally:
            dst.close()
        assert "demo" in ids
        assert "uncommitted" not in ids


class TestSelectiveRestore:
    """`flow restore` accepts `--data-only` and `--config-only` so
    operators can roll back the warehouse without touching contracts
    (or vice versa). Default is both."""

    def _make_backup(self, tmp_path: Path) -> tuple[Path, Path, Path]:
        data = tmp_path / "data"
        data.mkdir()
        _make_tiny_warehouse(data)
        contracts = tmp_path / "contracts"
        _make_tiny_contracts_db(contracts)
        out = tmp_path / "backup.tar.gz"
        CliRunner().invoke(cli, [
            "backup",
            "--data-dir", str(data),
            "--workflows-dir", str(contracts),
            "--output", str(out),
        ], catch_exceptions=False)
        return out, data, contracts

    def test_data_only_skips_contracts_db(self, tmp_path):
        backup_path, _, _ = self._make_backup(tmp_path)
        target_data = tmp_path / "tdata"
        target_contracts = tmp_path / "tcontracts"

        res = CliRunner().invoke(cli, [
            "restore",
            "--input", str(backup_path),
            "--data-dir", str(target_data),
            "--workflows-dir", str(target_contracts),
            "--data-only",
        ], catch_exceptions=False)
        assert res.exit_code == 0, res.output

        # Warehouse files landed
        assert any(target_data.rglob("items-run1.parquet"))
        # Config file did NOT
        assert not (target_contracts / "contracts.db").exists()

    def test_config_only_skips_data(self, tmp_path):
        backup_path, _, _ = self._make_backup(tmp_path)
        target_data = tmp_path / "tdata"
        target_contracts = tmp_path / "tcontracts"

        res = CliRunner().invoke(cli, [
            "restore",
            "--input", str(backup_path),
            "--data-dir", str(target_data),
            "--workflows-dir", str(target_contracts),
            "--config-only",
        ], catch_exceptions=False)
        assert res.exit_code == 0, res.output

        # Config file landed
        assert (target_contracts / "contracts.db").exists()
        # Warehouse files did NOT
        assert not any(target_data.rglob("*.parquet"))

    def test_default_restores_both(self, tmp_path):
        backup_path, _, _ = self._make_backup(tmp_path)
        target_data = tmp_path / "tdata"
        target_contracts = tmp_path / "tcontracts"

        res = CliRunner().invoke(cli, [
            "restore",
            "--input", str(backup_path),
            "--data-dir", str(target_data),
            "--workflows-dir", str(target_contracts),
        ], catch_exceptions=False)
        assert res.exit_code == 0, res.output

        assert any(target_data.rglob("items-run1.parquet"))
        assert (target_contracts / "contracts.db").exists()

    def test_data_only_and_config_only_are_mutually_exclusive(self, tmp_path):
        backup_path, _, _ = self._make_backup(tmp_path)
        res = CliRunner().invoke(cli, [
            "restore",
            "--input", str(backup_path),
            "--data-dir", str(tmp_path / "td"),
            "--workflows-dir", str(tmp_path / "tc"),
            "--data-only",
            "--config-only",
        ], catch_exceptions=False)
        # Click rejects mutually exclusive flags with non-zero exit.
        assert res.exit_code != 0
        assert "data-only" in res.output.lower() or "config-only" in res.output.lower()

    def test_config_only_against_data_only_backup_is_an_error(self, tmp_path):
        """If the tarball never carried a `_config/contracts.db` (a
        legacy data-only backup), `--config-only` has nothing to
        restore — surface that to the operator instead of silently
        succeeding."""
        data = tmp_path / "data"
        data.mkdir()
        _make_tiny_warehouse(data)
        out = tmp_path / "backup.tar.gz"
        CliRunner().invoke(cli, [
            "backup", "--data-dir", str(data), "--output", str(out),
        ], catch_exceptions=False)

        res = CliRunner().invoke(cli, [
            "restore",
            "--input", str(out),
            "--data-dir", str(tmp_path / "td"),
            "--workflows-dir", str(tmp_path / "tc"),
            "--config-only",
        ], catch_exceptions=False)
        assert res.exit_code != 0


class TestBackwardsCompatRestore:
    """Old (data-only) backups must still restore — the schema URI
    didn't change, just the set of allowed prefixes."""

    def test_restore_old_data_only_backup_still_works(self, tmp_path):
        data = tmp_path / "data"
        data.mkdir()
        _make_tiny_warehouse(data)
        out = tmp_path / "backup.tar.gz"
        # No --workflows-dir → pre-extension shape.
        CliRunner().invoke(cli, [
            "backup", "--data-dir", str(data), "--output", str(out),
        ], catch_exceptions=False)

        restored = tmp_path / "restored"
        res = CliRunner().invoke(cli, [
            "restore", "--input", str(out), "--data-dir", str(restored),
        ], catch_exceptions=False)
        assert res.exit_code == 0, res.output
        assert any(restored.rglob("items-run1.parquet"))
