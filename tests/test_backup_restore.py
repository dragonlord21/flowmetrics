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
