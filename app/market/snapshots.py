"""Frozen dataset snapshots (Parquet) with checksum and provenance.

A snapshot is a range of validated candles copied out of the database into one immutable Parquet
file. Backtests read snapshots only, never the live candle table, so a result can always be
reproduced from (snapshot id, config, engine version).

Integrity: `file_sha256` covers the exact bytes on disk; `content_sha256` covers the canonical
text of every candle and does not depend on the Parquet library. Both are stored in an immutable
database row and re-verified on every load. A late-filled gap changes nothing here: it is a copy.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
from decimal import Decimal
from pathlib import Path
from typing import Any, Final
from uuid import UUID, uuid5

import pyarrow as pa
import pyarrow.parquet as pq

from app.auth.audit import HOST_CLI_ACTOR, AuditWriter
from app.config import Settings
from app.domain.enums import AuditEventType, AuditResult
from app.domain.models import Clock
from app.domain.money import canonical
from app.market import candles as cd
from app.market.candles import Candle
from app.market.ingest import GRANULARITY, STEP
from app.pairs.runner import HOST_ACTOR, RunnerError
from app.storage.database import Storage
from app.storage.market_repositories import SnapshotRow

ENGINE_VERSION: Final = "5.0.0"
SNAPSHOT_DIR: Final = "snapshots"
_NAMESPACE: Final = UUID("6f0f1c2e-5b7a-4a52-9d55-0d3a1f1e7a01")
_DECIMAL: Final = pa.decimal128(38, 18)
SCHEMA: Final = pa.schema(
    [
        ("start", pa.int64()),
        ("open", _DECIMAL),
        ("high", _DECIMAL),
        ("low", _DECIMAL),
        ("close", _DECIMAL),
        ("volume", _DECIMAL),
    ]
)


class SnapshotError(Exception):
    """A snapshot is missing, corrupt, or cannot be built. The message is a fixed code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def content_sha256(candles: tuple[Candle, ...]) -> str:
    """Hash of the canonical candle text (independent of Parquet, ordering enforced)."""
    digest = hashlib.sha256()
    for c in candles:
        line = "|".join(
            (str(c.start), *(canonical(x) for x in (c.open, c.high, c.low, c.close, c.volume)))
        )
        digest.update(line.encode("ascii") + b"\n")
    return digest.hexdigest()


def encode_parquet(candles: tuple[Candle, ...]) -> bytes:
    """Deterministic Parquet bytes: fixed schema and settings, no pandas metadata, no timestamps."""
    try:
        table = pa.table(
            {
                "start": pa.array([c.start for c in candles], pa.int64()),
                **{
                    name: pa.array([getattr(c, name) for c in candles], _DECIMAL)
                    for name in ("open", "high", "low", "close", "volume")
                },
            },
            schema=SCHEMA,
        )
    except (pa.ArrowInvalid, pa.ArrowTypeError, OverflowError) as exc:
        raise SnapshotError("VALUE_NOT_REPRESENTABLE") from exc
    sink = io.BytesIO()
    pq.write_table(
        table,
        sink,
        compression="zstd",
        use_dictionary=False,
        write_statistics=True,
        store_schema=False,
        data_page_version="2.0",
        row_group_size=100_000,
    )
    return sink.getvalue()


def decode_parquet(data: bytes) -> tuple[Candle, ...]:
    try:
        table = pq.read_table(io.BytesIO(data))
    except (pa.ArrowInvalid, OSError) as exc:
        raise SnapshotError("UNREADABLE") from exc
    if table.schema != SCHEMA:
        raise SnapshotError("SCHEMA_MISMATCH")
    cols: dict[str, list[Any]] = {n: table.column(n).to_pylist() for n in SCHEMA.names}
    out = []
    for i in range(table.num_rows):
        values = [cols[n][i] for n in ("open", "high", "low", "close", "volume")]
        if not all(isinstance(v, Decimal) for v in values) or cols["start"][i] is None:
            raise SnapshotError("NULL_VALUE")
        out.append(Candle(int(cols["start"][i]), *values))
    return tuple(out)


def snapshot_dir(settings: Settings) -> Path:
    return Path(settings.data.data_dir) / SNAPSHOT_DIR


def _write_once(path: Path, data: bytes) -> None:
    """Write atomically and read-only; an existing identical file is left alone."""
    if path.exists():
        if hashlib.sha256(path.read_bytes()).hexdigest() == hashlib.sha256(data).hexdigest():
            return
        raise SnapshotError("FILE_EXISTS_DIFFERENT")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o444)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)


class SnapshotBuilder:
    def __init__(self, *, storage: Storage, clock: Clock, settings: Settings) -> None:
        self._storage, self._clock, self._settings = storage, clock, settings
        self._audit = AuditWriter(clock)

    def build(self, product_id: str, *, start: int, end: int) -> tuple[SnapshotRow, bool]:
        """Freeze [start, end). Returns (row, created). Idempotent for the same content."""
        if start % STEP or end % STEP or end <= start:
            raise SnapshotError("BAD_RANGE")
        policy = self._settings.pair_policy
        with self._storage.tx() as repos:
            product = repos.products.get_by_product_id(product_id)
            if product is None:
                raise RunnerError("UNKNOWN_PRODUCT")
            cursor = repos.market.cursor(product.id, GRANULARITY)
            if cursor is None or end > cursor:
                raise SnapshotError("RANGE_NOT_INGESTED")
            candles = repos.market.candles(product.id, GRANULARITY, start, end)
            max_run = repos.market.max_run_id(product.id, GRANULARITY)
            age = (self._clock.now() - product.last_verified_at).total_seconds()
            metadata_id = product.snapshot_id
        if age > policy.validation.max_metadata_age_seconds:
            raise SnapshotError("METADATA_STALE")
        if len(candles) < policy.strategy.min_history_candles:
            raise SnapshotError("TOO_FEW_CANDLES")
        gaps = cd.find_gaps(list(candles), STEP, start, end)
        missing = sum((b - a) // STEP for a, b in gaps)
        expected = (end - start) // STEP
        if Decimal(missing) / Decimal(expected) > policy.validation.max_gap_ratio:
            raise SnapshotError("TOO_MANY_GAPS")

        content = content_sha256(candles)
        data = encode_parquet(candles)
        file_sha = hashlib.sha256(data).hexdigest()
        snap_id = uuid5(_NAMESPACE, f"{product.id}|{GRANULARITY}|{start}|{end}|{max_run}|{content}")
        name = f"{snap_id}.parquet"
        directory = snapshot_dir(self._settings)
        _write_once(directory / name, data)
        row = SnapshotRow(
            id=snap_id,
            product_uuid=product.id,
            granularity=GRANULARITY,
            range_start=start,
            range_end=end,
            candle_count=len(candles),
            gap_count=len(gaps),
            missing_count=missing,
            max_ingest_run_id=max_run,
            file_sha256=file_sha,
            content_sha256=content,
            file_name=name,
            size_bytes=len(data),
            metadata_snapshot_id=metadata_id,
            engine_version=ENGINE_VERSION,
            created_at=self._clock.now(),
        )
        manifest = {
            "snapshot_id": str(snap_id),
            "product": product_id,
            "granularity": GRANULARITY,
            "range_start": start,
            "range_end": end,
            "candles": len(candles),
            "gaps": len(gaps),
            "missing_intervals": missing,
            "max_ingest_run_id": max_run,
            "metadata_snapshot_id": str(metadata_id),
            "content_sha256": content,
            "file_sha256": file_sha,
            "engine_version": ENGINE_VERSION,
            "source": "Coinbase public REST candles, validated by app.market.candles",
        }
        with self._storage.tx() as repos:
            created = repos.results.insert_snapshot(row)
            stored = repos.results.snapshot(snap_id)
            if stored is None:  # pragma: no cover  (the insert or an identical row exists)
                raise SnapshotError("NOT_RECORDED")
            if created:
                self._audit.record(
                    repos,
                    AuditEventType.MARKET_SNAPSHOT_CREATED,
                    AuditResult.SUCCESS,
                    actor=HOST_CLI_ACTOR,
                    target_type="dataset",
                    target_id=str(snap_id),
                    reason="FROZEN",
                    client_tag=HOST_ACTOR.client_tag,
                    request_id=HOST_ACTOR.request_id,
                    detail={"candles": len(candles), "gaps": len(gaps), "product": product_id},
                )
        _write_once(
            directory / f"{snap_id}.manifest.json",
            (json.dumps(manifest, sort_keys=True, indent=2) + "\n").encode("ascii"),
        )
        return stored, created


def load_snapshot(row: SnapshotRow, settings: Settings) -> tuple[Candle, ...]:
    """Read a snapshot back, verifying checksum, shape and content before returning anything."""
    directory = snapshot_dir(settings).resolve()
    path = (directory / row.file_name).resolve()
    if path.parent != directory or not path.is_file():
        raise SnapshotError("FILE_MISSING")
    data = path.read_bytes()
    if len(data) != row.size_bytes or hashlib.sha256(data).hexdigest() != row.file_sha256:
        raise SnapshotError("CHECKSUM_MISMATCH")
    candles = decode_parquet(data)
    if len(candles) != row.candle_count or content_sha256(candles) != row.content_sha256:
        raise SnapshotError("CONTENT_MISMATCH")
    starts = [c.start for c in candles]
    if starts != sorted(set(starts)) or not all(cd.ohlc_valid(c) for c in candles):
        raise SnapshotError("INVALID_CANDLES")
    if starts[0] < row.range_start or starts[-1] + STEP > row.range_end:
        raise SnapshotError("OUT_OF_RANGE")
    return candles
