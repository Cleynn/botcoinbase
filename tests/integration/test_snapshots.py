"""Frozen Parquet snapshots: checksums, provenance, determinism and refusals (synthetic data)."""

from __future__ import annotations

import json
import os
import stat
from collections.abc import Callable
from typing import Any

import pytest

from app.market.candles import last_closed_end
from app.market.ingest import STEP
from app.market.snapshots import (
    SnapshotBuilder,
    SnapshotError,
    decode_parquet,
    load_snapshot,
    snapshot_dir,
)
from app.storage.market_repositories import SnapshotRow
from tests.integration.conftest import Market

Sql = Callable[..., list[dict[str, Any]]]


def cursor_end(m: Market) -> int:
    return last_closed_end(int(m.clock.now().timestamp()), STEP)


def builder(m: Market) -> SnapshotBuilder:
    return SnapshotBuilder(storage=m.storage, clock=m.clock, settings=m.settings)


def freeze(m: Market, days: int = 7) -> tuple[SnapshotRow, bool]:
    end = cursor_end(m)
    return builder(m).build("BTC-USDC", start=end - days * 288 * STEP, end=end)


def test_a_snapshot_records_checksums_provenance_a_manifest_and_an_audit_event(
    mkt: Market, sql: Sql
) -> None:
    mkt.imported()
    row, created = freeze(mkt)
    assert created and row.candle_count == 7 * 288 and row.gap_count == 0
    assert len(row.file_sha256) == 64 and len(row.content_sha256) == 64
    assert row.engine_version and row.metadata_snapshot_id is not None
    directory = snapshot_dir(mkt.settings)
    parquet = directory / row.file_name
    manifest = json.loads((directory / f"{row.id}.manifest.json").read_text())
    assert (
        manifest["file_sha256"]
        == row.file_sha256
        == __import__("hashlib").sha256(parquet.read_bytes()).hexdigest()
    )
    assert manifest["product"] == "BTC-USDC" and manifest["candles"] == row.candle_count
    assert stat.S_IMODE(os.stat(parquet).st_mode) == 0o444  # read-only on disk
    events = sql("SELECT * FROM audit_events WHERE event_code = 'market.snapshot_created'")
    assert len(events) == 1 and events[0]["actor_role"] == "HOST_CLI"


def test_the_same_data_gives_the_same_snapshot_and_no_second_row(mkt: Market, sql: Sql) -> None:
    mkt.imported()
    first, created_first = freeze(mkt)
    second, created_second = freeze(mkt)
    assert created_first and not created_second and first.id == second.id
    assert sql("SELECT count(*) AS n FROM dataset_snapshots")[0]["n"] == 1
    assert first.file_sha256 == second.file_sha256


def test_the_parquet_bytes_are_deterministic(mkt: Market, tmp_path: Any) -> None:
    mkt.imported()
    row, _ = freeze(mkt)
    candles = load_snapshot(row, mkt.settings)
    from app.market.snapshots import encode_parquet

    assert encode_parquet(candles) == encode_parquet(decode_parquet(encode_parquet(candles)))
    import hashlib

    assert hashlib.sha256(encode_parquet(candles)).hexdigest() == row.file_sha256


def test_a_verified_snapshot_loads_back_identically(mkt: Market, sql: Sql) -> None:
    mkt.imported()
    row, _ = freeze(mkt)
    candles = load_snapshot(row, mkt.settings)
    stored = sql("SELECT start_ts, open, high, low, close, volume FROM candles ORDER BY start_ts")
    assert len(candles) == len(stored)
    assert all(
        c.start == r["start_ts"] and c.close == r["close"] and c.volume == r["volume"]
        for c, r in zip(candles, stored, strict=True)
    )
    assert [c.start for c in candles] == sorted(c.start for c in candles)


def test_a_tampered_file_is_refused(mkt: Market) -> None:
    mkt.imported()
    row, _ = freeze(mkt)
    path = snapshot_dir(mkt.settings) / row.file_name
    path.chmod(0o644)
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    with pytest.raises(SnapshotError, match="CHECKSUM_MISMATCH"):
        load_snapshot(row, mkt.settings)


def test_a_missing_file_is_refused(mkt: Market) -> None:
    mkt.imported()
    row, _ = freeze(mkt)
    path = snapshot_dir(mkt.settings) / row.file_name
    path.chmod(0o644)
    path.unlink()
    with pytest.raises(SnapshotError, match="FILE_MISSING"):
        load_snapshot(row, mkt.settings)


def test_a_file_that_verifies_but_holds_other_candles_is_refused(mkt: Market) -> None:
    """Replacing the file AND matching its checksum still fails the content checksum."""
    import hashlib
    from dataclasses import replace

    from app.market.snapshots import encode_parquet

    mkt.imported()
    row, _ = freeze(mkt)
    candles = list(load_snapshot(row, mkt.settings))
    c = candles[10]
    candles[10] = replace(c, volume=c.volume + 1)
    forged = encode_parquet(tuple(candles))
    path = snapshot_dir(mkt.settings) / row.file_name
    path.chmod(0o644)
    path.write_bytes(forged)
    lie = replace(row, file_sha256=hashlib.sha256(forged).hexdigest(), size_bytes=len(forged))
    with pytest.raises(SnapshotError, match="CONTENT_MISMATCH"):
        load_snapshot(lie, mkt.settings)


def test_a_snapshot_stays_frozen_when_a_gap_is_filled_later(mkt: Market, sql: Sql) -> None:
    from app.pairs.runner import PairRunner

    PairRunner(
        storage=mkt.storage, clock=mkt.clock, settings=mkt.settings, client=mkt.coinbase.client()
    ).discover()
    end = cursor_end(mkt)
    hole = {end - 400 * STEP + i * STEP for i in range(6)}
    mkt.source.missing = hole
    assert mkt.importer().run("BTC-USDC", commit=True).missing == 6
    old, _ = freeze(mkt)
    assert old.gap_count == 1 and old.missing_count == 6
    sql("ALTER TABLE ingest_cursors DISABLE TRIGGER USER")
    sql("DELETE FROM ingest_cursors")
    sql("ALTER TABLE ingest_cursors ENABLE TRIGGER USER")
    mkt.source.missing = set()
    filled = mkt.importer().run("BTC-USDC", commit=True)
    assert filled.inserted == 6
    new, created = freeze(mkt)
    assert created and new.id != old.id and new.missing_count == 0
    again = load_snapshot(old, mkt.settings)  # the old snapshot still verifies, unchanged
    assert len(again) == old.candle_count and old.candle_count == new.candle_count - 6


@pytest.mark.parametrize(
    ("shift", "code"),
    [(STEP, "RANGE_NOT_INGESTED"), (-1, "BAD_RANGE")],
)
def test_ranges_that_are_not_ingested_or_not_aligned_are_refused(
    mkt: Market, shift: int, code: str
) -> None:
    mkt.imported()
    end = cursor_end(mkt) + shift
    with pytest.raises(SnapshotError, match=code):
        builder(mkt).build("BTC-USDC", start=end - 1000 * STEP, end=end)


def test_too_little_history_is_refused(mkt: Market) -> None:
    mkt.imported()
    end = cursor_end(mkt)
    with pytest.raises(SnapshotError, match="TOO_FEW_CANDLES"):
        builder(mkt).build("BTC-USDC", start=end - 50 * STEP, end=end)


def test_too_many_gaps_are_refused(mkt: Market) -> None:
    from app.pairs.runner import PairRunner

    PairRunner(
        storage=mkt.storage, clock=mkt.clock, settings=mkt.settings, client=mkt.coinbase.client()
    ).discover()
    end = cursor_end(mkt)
    mkt.source.missing = {end - 1900 * STEP + i * STEP for i in range(400)}
    mkt.importer().run("BTC-USDC", commit=True)
    with pytest.raises(SnapshotError, match="TOO_MANY_GAPS"):
        freeze(mkt)


def test_stale_product_metadata_is_refused(mkt: Market) -> None:
    mkt.imported()
    end = cursor_end(mkt)  # data is unchanged; only the metadata gets old
    mkt.clock.advance(mkt.settings.pair_policy.validation.max_metadata_age_seconds + 60)
    with pytest.raises(SnapshotError, match="METADATA_STALE"):
        builder(mkt).build("BTC-USDC", start=end - 7 * 288 * STEP, end=end)
