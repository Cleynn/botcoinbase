"""SQL repositories for candles, ingestion, dataset snapshots, backtests, reports and the paper venue.

Statements are parameterised. Numeric values move as Decimal end to end (psycopg maps numeric to
Decimal), never through float.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from psycopg.types.json import Jsonb

from app.market.candles import Candle, QualityEvent
from app.storage.repositories import Conn

_PAIR_KEY = 7_272_050


def _candle(row: dict[str, Any]) -> Candle:
    return Candle(
        int(row["start_ts"]), row["open"], row["high"], row["low"], row["close"], row["volume"]
    )


@dataclass(frozen=True)
class IngestRunRow:
    id: int
    product_uuid: UUID
    granularity: str
    window_start: int
    window_end: int
    status: str
    requests: int
    retries: int
    fetched: int
    inserted: int
    duplicates: int
    conflicts: int
    malformed: int
    gaps: int
    missing: int
    finished_at: datetime


@dataclass(frozen=True)
class SnapshotRow:
    id: UUID
    product_uuid: UUID
    granularity: str
    range_start: int
    range_end: int
    candle_count: int
    gap_count: int
    missing_count: int
    max_ingest_run_id: int
    file_sha256: str
    content_sha256: str
    file_name: str
    size_bytes: int
    metadata_snapshot_id: UUID | None
    engine_version: str
    created_at: datetime


@dataclass(frozen=True)
class ReportRow:
    id: UUID
    kind: str
    mode_label: str
    source_id: UUID | None
    title: str
    body_json: str
    body_md: str
    sha256: str
    created_at: datetime


@dataclass(frozen=True)
class BacktestRow:
    id: UUID
    snapshot_id: UUID
    kind: str
    fee_scenario: str
    config_sha256: str
    engine_version: str
    params: dict[str, Any]
    summary: dict[str, Any]
    created_at: datetime


class MarketRepository:
    """Candles, ingestion runs, data-quality events and the ingestion cursor."""

    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def lock_product(self, product_uuid: UUID) -> None:
        self._conn.execute(
            "SELECT pg_advisory_xact_lock(%s, hashtext(%s))", (_PAIR_KEY, str(product_uuid))
        )

    def candles(
        self, product_uuid: UUID, granularity: str, start: int, end: int
    ) -> tuple[Candle, ...]:
        rows = self._conn.execute(
            "SELECT start_ts, open, high, low, close, volume FROM candles "
            "WHERE product_uuid = %s AND granularity = %s AND start_ts >= %s AND start_ts < %s "
            "ORDER BY start_ts",
            (product_uuid, granularity, start, end),
        ).fetchall()
        return tuple(_candle(r) for r in rows)

    def candle_count(self, product_uuid: UUID, granularity: str) -> int:
        row = self._conn.execute(
            "SELECT count(*) AS n FROM candles WHERE product_uuid = %s AND granularity = %s",
            (product_uuid, granularity),
        ).fetchone()
        return int(row["n"]) if row else 0

    def insert_run(
        self,
        *,
        product_uuid: UUID,
        granularity: str,
        window_start: int,
        window_end: int,
        status: str,
        counts: dict[str, int],
        offset_ms: int | None,
        started_at: datetime,
        finished_at: datetime,
    ) -> int:
        row = self._conn.execute(
            "INSERT INTO ingest_runs (product_uuid, granularity, window_start, window_end, status, "
            "requests, retries, fetched, inserted, duplicates, conflicts, malformed, gaps, missing, "
            "server_time_offset_ms, started_at, finished_at) VALUES (%s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id",
            (
                product_uuid,
                granularity,
                window_start,
                window_end,
                status,
                counts["requests"],
                counts["retries"],
                counts["fetched"],
                counts["inserted"],
                counts["duplicates"],
                counts["conflicts"],
                counts["malformed"],
                counts["gaps"],
                counts["missing"],
                offset_ms,
                started_at,
                finished_at,
            ),
        ).fetchone()
        if row is None:  # pragma: no cover  (INSERT ... RETURNING always returns a row)
            raise RuntimeError("ingest run was not recorded")
        return int(row["id"])

    def insert_candles(
        self, product_uuid: UUID, granularity: str, run_id: int, candles: list[Candle]
    ) -> None:
        with self._conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO candles (product_uuid, granularity, start_ts, open, high, low, close, "
                "volume, ingest_run_id) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [
                    (
                        product_uuid,
                        granularity,
                        c.start,
                        c.open,
                        c.high,
                        c.low,
                        c.close,
                        c.volume,
                        run_id,
                    )
                    for c in candles
                ],
            )

    def insert_conflicts(
        self,
        product_uuid: UUID,
        granularity: str,
        run_id: int,
        rows: list[tuple[int, Candle, Candle]],
    ) -> None:
        for start, stored, incoming in rows:
            self._conn.execute(
                "INSERT INTO candle_conflicts (product_uuid, granularity, start_ts, stored, "
                "incoming, ingest_run_id) VALUES (%s, %s, %s, %s, %s, %s)",
                (product_uuid, granularity, start, _text(stored), _text(incoming), run_id),
            )

    def insert_events(self, run_id: int, events: list[QualityEvent]) -> None:
        with self._conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO data_quality_events (ingest_run_id, code, severity, start_ts, end_ts, "
                "count, detail) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                [(run_id, e.code, e.severity, e.start, e.end, e.count, e.detail) for e in events],
            )

    def cursor(self, product_uuid: UUID, granularity: str) -> int | None:
        row = self._conn.execute(
            "SELECT covered_until FROM ingest_cursors WHERE product_uuid = %s AND granularity = %s",
            (product_uuid, granularity),
        ).fetchone()
        return int(row["covered_until"]) if row else None

    def advance_cursor(
        self, product_uuid: UUID, granularity: str, covered_until: int, run_id: int, now: datetime
    ) -> None:
        if self.cursor(product_uuid, granularity) is None:
            self._conn.execute(
                "INSERT INTO ingest_cursors (product_uuid, granularity, covered_until, last_run_id, "
                "updated_at) VALUES (%s, %s, %s, %s, %s)",
                (product_uuid, granularity, covered_until, run_id, now),
            )
        else:
            self._conn.execute(
                "UPDATE ingest_cursors SET covered_until = %s, last_run_id = %s, updated_at = %s "
                "WHERE product_uuid = %s AND granularity = %s",
                (covered_until, run_id, now, product_uuid, granularity),
            )

    def last_run(self, product_uuid: UUID, granularity: str) -> IngestRunRow | None:
        row = self._conn.execute(
            "SELECT * FROM ingest_runs WHERE product_uuid = %s AND granularity = %s "
            "ORDER BY id DESC LIMIT 1",
            (product_uuid, granularity),
        ).fetchone()
        return (
            IngestRunRow(**{k: row[k] for k in IngestRunRow.__dataclass_fields__}) if row else None
        )

    def max_run_id(self, product_uuid: UUID, granularity: str) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(max(id), 0) AS m FROM ingest_runs "
            "WHERE product_uuid = %s AND granularity = %s",
            (product_uuid, granularity),
        ).fetchone()
        return int(row["m"]) if row else 0

    def event_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT code, COALESCE(sum(count), 0) AS n FROM data_quality_events GROUP BY code"
        ).fetchall()
        return {r["code"]: int(r["n"]) for r in rows}

    def last_success_at(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT max(finished_at) AS t FROM ingest_runs WHERE status = 'COMPLETE'"
        ).fetchone()
        return row["t"] if row else None

    def newest_candle_start(self, product_uuid: UUID, granularity: str) -> int | None:
        row = self._conn.execute(
            "SELECT max(start_ts) AS t FROM candles WHERE product_uuid = %s AND granularity = %s",
            (product_uuid, granularity),
        ).fetchone()
        return int(row["t"]) if row and row["t"] is not None else None


def _text(c: Candle) -> str:
    return "|".join(format(x, "f") for x in (c.open, c.high, c.low, c.close, c.volume))


class ResultRepository:
    """Dataset snapshots, backtests, reports and grid plans (all immutable)."""

    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    # ------------------------------------------------------------------ snapshots
    def insert_snapshot(self, row: SnapshotRow) -> bool:
        cur = self._conn.execute(
            "INSERT INTO dataset_snapshots (id, product_uuid, granularity, range_start, range_end, "
            "candle_count, gap_count, missing_count, max_ingest_run_id, file_sha256, content_sha256, "
            "file_name, size_bytes, metadata_snapshot_id, engine_version, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT ON CONSTRAINT dataset_snapshots_identity DO NOTHING",
            tuple(getattr(row, f) for f in SnapshotRow.__dataclass_fields__),
        )
        return cur.rowcount == 1

    def snapshot(self, snapshot_id: UUID) -> SnapshotRow | None:
        row = self._conn.execute(
            "SELECT * FROM dataset_snapshots WHERE id = %s", (snapshot_id,)
        ).fetchone()
        return _snapshot(row) if row else None

    def snapshot_by_identity(
        self, product_uuid: UUID, granularity: str, start: int, end: int, run_id: int, content: str
    ) -> SnapshotRow | None:
        row = self._conn.execute(
            "SELECT * FROM dataset_snapshots WHERE product_uuid = %s AND granularity = %s "
            "AND range_start = %s AND range_end = %s AND max_ingest_run_id = %s "
            "AND content_sha256 = %s",
            (product_uuid, granularity, start, end, run_id, content),
        ).fetchone()
        return _snapshot(row) if row else None

    def snapshots(self, limit: int = 50) -> list[SnapshotRow]:
        rows = self._conn.execute(
            "SELECT * FROM dataset_snapshots ORDER BY created_at DESC, id LIMIT %s", (limit,)
        ).fetchall()
        return [_snapshot(r) for r in rows]

    def snapshot_count(self) -> int:
        row = self._conn.execute("SELECT count(*) AS n FROM dataset_snapshots").fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ backtests
    def insert_backtest(self, row: BacktestRow) -> bool:
        cur = self._conn.execute(
            "INSERT INTO backtest_runs (id, snapshot_id, kind, fee_scenario, config_sha256, "
            "engine_version, params, summary, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT ON CONSTRAINT backtest_runs_identity DO NOTHING",
            (
                row.id,
                row.snapshot_id,
                row.kind,
                row.fee_scenario,
                row.config_sha256,
                row.engine_version,
                Jsonb(row.params),
                Jsonb(row.summary),
                row.created_at,
            ),
        )
        return cur.rowcount == 1

    def backtest_by_identity(
        self, snapshot_id: UUID, kind: str, scenario: str, config: str, engine: str
    ) -> BacktestRow | None:
        row = self._conn.execute(
            "SELECT * FROM backtest_runs WHERE snapshot_id = %s AND kind = %s AND fee_scenario = %s "
            "AND config_sha256 = %s AND engine_version = %s",
            (snapshot_id, kind, scenario, config, engine),
        ).fetchone()
        return _backtest(row) if row else None

    def backtest(self, run_id: UUID) -> BacktestRow | None:
        row = self._conn.execute("SELECT * FROM backtest_runs WHERE id = %s", (run_id,)).fetchone()
        return _backtest(row) if row else None

    def backtest_stats(self) -> tuple[int, datetime | None]:
        row = self._conn.execute(
            "SELECT count(*) AS n, max(created_at) AS t FROM backtest_runs"
        ).fetchone()
        return (int(row["n"]), row["t"]) if row else (0, None)

    # ------------------------------------------------------------------ reports
    def insert_report(self, row: ReportRow) -> bool:
        cur = self._conn.execute(
            "INSERT INTO reports (id, kind, mode_label, source_id, title, body_json, body_md, "
            "sha256, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT ON CONSTRAINT reports_identity DO NOTHING",
            (
                row.id,
                row.kind,
                row.mode_label,
                row.source_id,
                row.title,
                row.body_json,
                row.body_md,
                row.sha256,
                row.created_at,
            ),
        )
        return cur.rowcount == 1

    def report_by_hash(self, kind: str, sha256: str) -> ReportRow | None:
        row = self._conn.execute(
            "SELECT * FROM reports WHERE kind = %s AND sha256 = %s", (kind, sha256)
        ).fetchone()
        return _report(row) if row else None

    def report(self, report_id: UUID) -> ReportRow | None:
        row = self._conn.execute("SELECT * FROM reports WHERE id = %s", (report_id,)).fetchone()
        return _report(row) if row else None

    def reports(self, limit: int = 50, offset: int = 0) -> list[ReportRow]:
        rows = self._conn.execute(
            "SELECT * FROM reports ORDER BY created_at DESC, id LIMIT %s OFFSET %s", (limit, offset)
        ).fetchall()
        return [_report(r) for r in rows]

    def report_count(self) -> int:
        row = self._conn.execute("SELECT count(*) AS n FROM reports").fetchone()
        return int(row["n"]) if row else 0

    # ------------------------------------------------------------------ grid plans
    def insert_grid_plan(
        self,
        *,
        plan_id: UUID,
        pair_id: UUID,
        levels: int,
        lower: Decimal,
        upper: Decimal,
        prices: list[Decimal],
        cell_budget: Decimal,
        config_sha256: str,
        details: dict[str, Any],
        now: datetime,
    ) -> None:
        self._conn.execute(
            "INSERT INTO grid_plans (id, pair_id, levels, lower_price, upper_price, prices, "
            "cell_budget, config_sha256, details, created_at) VALUES (%s, %s, %s, %s, %s, %s, %s, "
            "%s, %s, %s)",
            (
                plan_id,
                pair_id,
                levels,
                lower,
                upper,
                prices,
                cell_budget,
                config_sha256,
                Jsonb(details),
                now,
            ),
        )

    def grid_plan(self, plan_id: UUID) -> dict[str, Any] | None:
        row = self._conn.execute("SELECT * FROM grid_plans WHERE id = %s", (plan_id,)).fetchone()
        return dict(row) if row else None


def _snapshot(row: dict[str, Any]) -> SnapshotRow:
    return SnapshotRow(**{k: row[k] for k in SnapshotRow.__dataclass_fields__})


def _report(row: dict[str, Any]) -> ReportRow:
    return ReportRow(**{k: row[k] for k in ReportRow.__dataclass_fields__})


def _backtest(row: dict[str, Any]) -> BacktestRow:
    def obj(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else json.loads(value)

    return BacktestRow(
        row["id"],
        row["snapshot_id"],
        row["kind"],
        row["fee_scenario"],
        row["config_sha256"],
        row["engine_version"],
        obj(row["params"]),
        obj(row["summary"]),
        row["created_at"],
    )
