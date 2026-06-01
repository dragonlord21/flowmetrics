"""Tests for `.tmp` debris cleanup.

An interrupted Parquet or status write leaves a `.tmp` file.
`cleanup_tmp_files` sweeps that debris so the data directory
stays clean and rsync-tidy — while NEVER touching a `.parquet`
snapshot or `.yaml` config. The cumulative work-item history is
upsert-only and must survive every operation.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta

from flowmetrics.materialize import cleanup_tmp_files


def _age(path, delta: timedelta) -> None:
    """Backdate a file's mtime by `delta`."""
    ts = (datetime.now(UTC) - delta).timestamp()
    os.utime(path, (ts, ts))


class TestCleanupTmpFiles:
    def test_deletes_a_stale_tmp(self, tmp_path):
        t = tmp_path / "work_items" / "items-dead.parquet.tmp"
        t.parent.mkdir(parents=True)
        t.write_text("half-written")
        _age(t, timedelta(hours=2))
        n = cleanup_tmp_files(tmp_path, now=datetime.now(UTC))
        assert n == 1
        assert not t.exists()

    def test_keeps_a_fresh_tmp(self, tmp_path):
        """A `.tmp` from a write in flight (recent mtime) must be
        spared — deleting it would corrupt a running materialize."""
        t = tmp_path / "items-live.parquet.tmp"
        t.write_text("write in progress")
        n = cleanup_tmp_files(tmp_path, now=datetime.now(UTC))
        assert n == 0
        assert t.exists()

    def test_never_deletes_parquet_or_yaml(self, tmp_path):
        """The cumulative history is sacred — even an ancient
        `.parquet` or `.yaml` survives cleanup untouched."""
        pq = tmp_path / "work_items" / "items-keep.parquet"
        pq.parent.mkdir(parents=True)
        pq.write_text("snapshot data")
        yml = tmp_path / "contract.yaml"
        yml.write_text("contract: {}")
        _age(pq, timedelta(days=365))
        _age(yml, timedelta(days=365))
        cleanup_tmp_files(tmp_path, now=datetime.now(UTC))
        assert pq.exists(), ".parquet snapshot must never be deleted"
        assert yml.exists(), ".yaml config must never be deleted"

    def test_sweeps_status_tmp_too(self, tmp_path):
        """Status-file `.tmp` debris (`_status/<wf>.json.tmp`) is
        also cleaned."""
        s = tmp_path / "_status" / "wf.json.tmp"
        s.parent.mkdir(parents=True)
        s.write_text("{}")
        _age(s, timedelta(hours=2))
        assert cleanup_tmp_files(tmp_path, now=datetime.now(UTC)) == 1

    def test_missing_data_dir_is_a_no_op(self, tmp_path):
        assert cleanup_tmp_files(tmp_path / "nope", now=datetime.now(UTC)) == 0
