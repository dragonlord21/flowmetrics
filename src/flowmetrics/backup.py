"""Warehouse backup / restore.

A backup is a single `.tar.gz` carrying:

  - every file under `data_dir` except (by default) the source-API
    cache, which is regenerable;
  - a `flowmetrics-backup.json` header recording schema version,
    flowmetrics version, DuckDB version, and a SHA-256 of every
    payload file.

Restore verifies the header + every checksum BEFORE writing anything
to the target directory. A corrupted or tampered backup fails before
it can damage a half-restored warehouse. The target must be empty
unless `--force` is given.

The format is intentionally stdlib-only (tarfile + gzip): no external
compressor, no boto3, no zstandard. S3 / cloud targets live behind
an optional dep and are out of scope here.
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

# Sub-directories under `data_dir` whose contents are re-fetchable
# from the source API and so excluded from backups by default.
_CACHE_DIR_NAMES = frozenset({".cache", "cache"})

# Path inside the tarball that holds the integrity header.
_HEADER_NAME = "flowmetrics-backup.json"

# Header schema URI — bumped on any breaking change to the layout.
SCHEMA_URI = "flowmetrics.backup.v1"


def _flowmetrics_version() -> str:
    """Best-effort package version. `importlib.metadata` returns the
    installed dist version; a source checkout without an install
    falls back to `unknown`."""
    try:
        from importlib.metadata import version
        return version("flowmetrics")
    except Exception:
        return "unknown"


def _duckdb_version() -> str:
    try:
        import duckdb
        return duckdb.__version__
    except Exception:
        return "unknown"


@dataclass(frozen=True)
class BackupHeader:
    schema: str
    flowmetrics_version: str
    duckdb_version: str
    created_at: str          # ISO-8601 UTC
    files: dict[str, str]    # relative path → sha256 hex

    def to_json(self) -> bytes:
        return json.dumps(
            {
                "schema": self.schema,
                "flowmetrics_version": self.flowmetrics_version,
                "duckdb_version": self.duckdb_version,
                "created_at": self.created_at,
                "files": self.files,
            },
            indent=2,
            sort_keys=True,
        ).encode("utf-8")

    @classmethod
    def from_bytes(cls, raw: bytes) -> BackupHeader:
        d = json.loads(raw)
        return cls(
            schema=d["schema"],
            flowmetrics_version=d["flowmetrics_version"],
            duckdb_version=d["duckdb_version"],
            created_at=d["created_at"],
            files=d["files"],
        )


def _should_skip(rel: Path, include_cache: bool) -> bool:
    """Skip the cache subtree (by default) and anything inside an
    obvious meta-directory we created (`_backups/` from a previous
    run, `_status/` from the daily manifest)."""
    parts = rel.parts
    if not include_cache and any(p in _CACHE_DIR_NAMES for p in parts):
        return True
    return bool(parts and parts[0] == "_backups")


def _enumerate_payload(data_dir: Path, include_cache: bool) -> list[Path]:
    """All files under `data_dir` that belong in the tarball. Sorted
    for deterministic header ordering (so two backups of the same
    state produce identical archives)."""
    out: list[Path] = []
    for p in sorted(data_dir.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(data_dir)
        if _should_skip(rel, include_cache):
            continue
        out.append(p)
    return out


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def write_backup(
    data_dir: Path,
    output: Path,
    *,
    include_cache: bool = False,
) -> BackupHeader:
    """Write a `.tar.gz` backup of `data_dir` to `output`.
    Returns the header that was embedded."""
    payload = _enumerate_payload(data_dir, include_cache)
    files = {str(p.relative_to(data_dir)): _sha256(p) for p in payload}
    header = BackupHeader(
        schema=SCHEMA_URI,
        flowmetrics_version=_flowmetrics_version(),
        duckdb_version=_duckdb_version(),
        created_at=datetime.now(UTC).isoformat(),
        files=files,
    )

    output.parent.mkdir(parents=True, exist_ok=True)
    with tarfile.open(output, "w:gz") as tar:
        # Header first so a streaming restore can fail fast on schema
        # mismatch before reading any payload.
        header_bytes = header.to_json()
        info = tarfile.TarInfo(name=_HEADER_NAME)
        info.size = len(header_bytes)
        tar.addfile(info, io.BytesIO(header_bytes))
        for p in payload:
            tar.add(p, arcname=str(p.relative_to(data_dir)))
    return header


class BackupError(Exception):
    """Raised when a backup is malformed, corrupted, or tampered."""


def read_header(input_path: Path) -> BackupHeader:
    """Open the tarball, read just the header, close. Used by restore
    to fail fast before allocating restore-target paths."""
    try:
        with tarfile.open(input_path, "r:gz") as tar:
            try:
                member = tar.getmember(_HEADER_NAME)
            except KeyError as exc:
                raise BackupError(
                    f"no {_HEADER_NAME} inside {input_path} — not a "
                    "flowmetrics backup."
                ) from exc
            f = tar.extractfile(member)
            if f is None:
                raise BackupError(
                    f"could not read {_HEADER_NAME} from {input_path}."
                )
            raw = f.read()
    except (tarfile.ReadError, EOFError, OSError) as exc:
        raise BackupError(
            f"{input_path} is not a readable .tar.gz: {exc}"
        ) from exc
    try:
        return BackupHeader.from_bytes(raw)
    except (KeyError, json.JSONDecodeError) as exc:
        raise BackupError(
            f"{_HEADER_NAME} inside {input_path} is malformed: {exc}"
        ) from exc


def _is_target_dirty(target: Path) -> bool:
    if not target.exists():
        return False
    if not target.is_dir():
        # A file at the target path counts as "in the way".
        return True
    return any(target.iterdir())


def restore_backup(
    input_path: Path,
    data_dir: Path,
    *,
    force: bool = False,
) -> BackupHeader:
    """Verify + extract `input_path` into `data_dir`.

    Bails before writing anything when:
      - The tarball isn't a valid gzipped tar.
      - The header is missing or malformed.
      - The schema is from a newer version we don't understand.
      - Any payload file's SHA-256 doesn't match the header.
      - `data_dir` exists and is non-empty AND `force` is False.
    """
    if _is_target_dirty(data_dir) and not force:
        raise BackupError(
            f"target {data_dir} is non-empty. Pass --force to "
            f"overwrite, or pick a fresh directory."
        )

    header = read_header(input_path)
    if header.schema != SCHEMA_URI:
        raise BackupError(
            f"unknown backup schema {header.schema!r}; this build "
            f"understands {SCHEMA_URI!r}."
        )

    # Verify every payload file in-memory before touching disk so a
    # tampered tarball can't ever leave us half-restored.
    try:
        with tarfile.open(input_path, "r:gz") as tar:
            for relpath, expected in header.files.items():
                try:
                    member = tar.getmember(relpath)
                except KeyError as exc:
                    raise BackupError(
                        f"backup is missing {relpath!r} listed in the header."
                    ) from exc
                stream = tar.extractfile(member)
                if stream is None:
                    raise BackupError(
                        f"could not read {relpath!r} from the backup."
                    )
                h = hashlib.sha256()
                while True:
                    chunk = stream.read(1024 * 1024)
                    if not chunk:
                        break
                    h.update(chunk)
                if h.hexdigest() != expected:
                    raise BackupError(
                        f"checksum mismatch for {relpath!r} — backup is "
                        "corrupted or tampered with."
                    )
    except (tarfile.ReadError, EOFError, OSError) as exc:
        raise BackupError(f"could not read {input_path}: {exc}") from exc

    # Now we can extract — every byte will match the header.
    data_dir.mkdir(parents=True, exist_ok=True)
    with tarfile.open(input_path, "r:gz") as tar:
        for relpath in header.files:
            member = tar.getmember(relpath)
            target = data_dir / relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            f = tar.extractfile(member)
            assert f is not None
            target.write_bytes(f.read())
    return header
