"""SQL for review packages: settings, package rows, and the typed read-only export queries.

The export queries select only allow-listed columns (never free text, display names, actors,
client tags, paths or payloads) and only read: they cannot change any bot, pair, risk, order,
configuration or live state.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any
from uuid import UUID

from app.storage.repositories import Conn


@dataclass(frozen=True)
class ReviewSettingsRow:
    enabled: bool
    retention_days: int
    updated_at: datetime


@dataclass(frozen=True)
class PackageRow:
    id: UUID
    state: str
    period_start: date
    period_end: date
    scope: tuple[str, ...]
    retention_days: int
    requested_by: UUID
    requested_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    expires_at: datetime | None
    storage_name: str | None
    size_bytes: int | None
    file_count: int | None
    package_sha256: str | None
    manifest_sha256: str | None
    config_sha256: str | None
    exporter_version: str | None
    failure_code: str | None
    files: list[dict[str, Any]] | None
    content_removed_at: datetime | None


def _package(row: dict[str, Any]) -> PackageRow:
    data = dict(row)
    data["scope"] = tuple(data["scope"])
    return PackageRow(**data)


class ReviewRepository:
    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    # ------------------------------------------------------------------ settings
    def settings(self, *, for_update: bool = False) -> ReviewSettingsRow:
        row = self._conn.execute(
            "SELECT enabled, retention_days, updated_at FROM review_settings WHERE id"  # noqa: S608
            + (" FOR UPDATE" if for_update else "")
        ).fetchone()
        if row is None:  # pragma: no cover  (the migration inserts the single row)
            raise RuntimeError("review settings row is missing")
        return ReviewSettingsRow(**row)

    def set_settings(self, enabled: bool, retention_days: int, now: datetime) -> None:
        self._conn.execute(
            "UPDATE review_settings SET enabled = %s, retention_days = %s, updated_at = %s "
            "WHERE id",
            (enabled, retention_days, now),
        )

    # ------------------------------------------------------------------ packages
    def insert_request(
        self,
        package_id: UUID,
        period_start: date,
        period_end: date,
        scope: list[str],
        retention_days: int,
        requested_by: UUID,
        now: datetime,
    ) -> None:
        self._conn.execute(
            "INSERT INTO review_packages (id, state, period_start, period_end, scope, "
            "retention_days, requested_by, requested_at) "
            "VALUES (%s, 'REQUESTED', %s, %s, %s, %s, %s, %s)",
            (package_id, period_start, period_end, scope, retention_days, requested_by, now),
        )

    def package(self, package_id: UUID, *, for_update: bool = False) -> PackageRow | None:
        row = self._conn.execute(
            "SELECT * FROM review_packages WHERE id = %s"  # noqa: S608
            + (" FOR UPDATE" if for_update else ""),
            (package_id,),
        ).fetchone()
        return _package(row) if row else None

    def recent(self, limit: int = 25) -> list[PackageRow]:
        rows = self._conn.execute(
            "SELECT * FROM review_packages ORDER BY requested_at DESC, id LIMIT %s", (limit,)
        ).fetchall()
        return [_package(r) for r in rows]

    def in_state(self, *states: str) -> list[PackageRow]:
        rows = self._conn.execute(
            "SELECT * FROM review_packages WHERE state = ANY(%s) ORDER BY requested_at, id",
            (list(states),),
        ).fetchall()
        return [_package(r) for r in rows]

    def counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT state, count(*) AS n FROM review_packages GROUP BY state"
        ).fetchall()
        return {r["state"]: int(r["n"]) for r in rows}

    def last_ready(self) -> datetime | None:
        row = self._conn.execute(
            "SELECT max(finished_at) AS t FROM review_packages WHERE finished_at IS NOT NULL "
            "AND storage_name IS NOT NULL"
        ).fetchone()
        return row["t"] if row else None

    def storage_names(self) -> set[str]:
        rows = self._conn.execute(
            "SELECT storage_name FROM review_packages WHERE storage_name IS NOT NULL "
            "AND content_removed_at IS NULL"
        ).fetchall()
        return {r["storage_name"] for r in rows}

    def mark_generating(self, package_id: UUID, now: datetime) -> None:
        self._conn.execute(
            "UPDATE review_packages SET state = 'GENERATING', started_at = %s WHERE id = %s",
            (now, package_id),
        )

    def mark_failed(self, package_id: UUID, code: str, now: datetime) -> None:
        self._conn.execute(
            "UPDATE review_packages SET state = 'FAILED', failure_code = %s, finished_at = %s "
            "WHERE id = %s",
            (code, now, package_id),
        )

    def mark_ready(
        self,
        package_id: UUID,
        *,
        storage_name: str,
        size_bytes: int,
        package_sha256: str,
        manifest_sha256: str,
        config_sha256: str,
        exporter_version: str,
        files: list[dict[str, Any]],
        now: datetime,
        expires_at: datetime,
    ) -> None:
        self._conn.execute(
            "UPDATE review_packages SET state = 'READY', storage_name = %s, size_bytes = %s, "
            "file_count = %s, package_sha256 = %s, manifest_sha256 = %s, config_sha256 = %s, "
            "exporter_version = %s, files = %s::jsonb, finished_at = %s, expires_at = %s "
            "WHERE id = %s",
            (
                storage_name,
                size_bytes,
                len(files),
                package_sha256,
                manifest_sha256,
                config_sha256,
                exporter_version,
                json.dumps(files, sort_keys=True),
                now,
                expires_at,
                package_id,
            ),
        )

    def mark_corrupt(self, package_id: UUID, now: datetime) -> None:
        self._conn.execute(
            "UPDATE review_packages SET state = 'CORRUPT', finished_at = %s WHERE id = %s",
            (now, package_id),
        )

    def mark_expired(self, package_id: UUID) -> None:
        self._conn.execute(
            "UPDATE review_packages SET state = 'EXPIRED' WHERE id = %s", (package_id,)
        )

    def mark_content_removed(self, package_id: UUID, now: datetime) -> None:
        self._conn.execute(
            "UPDATE review_packages SET content_removed_at = %s WHERE id = %s", (now, package_id)
        )

    def due_for_expiry(self, now: datetime) -> list[PackageRow]:
        rows = self._conn.execute(
            "SELECT * FROM review_packages WHERE state IN ('READY', 'CORRUPT') "
            "AND expires_at <= %s ORDER BY expires_at, id",
            (now,),
        ).fetchall()
        return [_package(r) for r in rows]

    def expired_with_content(self) -> list[PackageRow]:
        rows = self._conn.execute(
            "SELECT * FROM review_packages WHERE state = 'EXPIRED' AND content_removed_at IS NULL "
            "AND storage_name IS NOT NULL ORDER BY requested_at, id"
        ).fetchall()
        return [_package(r) for r in rows]


class ExportRepository:
    """Typed, allow-listed, read-only queries for the package builder."""

    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    def _all(self, query: str, params: tuple[Any, ...]) -> list[dict[str, Any]]:
        return [dict(r) for r in self._conn.execute(query, params).fetchall()]

    def backtests(self, start: date, end: date) -> list[dict[str, Any]]:
        return self._all(
            "SELECT b.id AS run_id, b.snapshot_id, p.product_id AS product, b.kind, "
            "b.fee_scenario, b.config_sha256, b.engine_version, b.created_at, b.summary "
            "FROM backtest_runs b JOIN dataset_snapshots s ON s.id = b.snapshot_id "
            "JOIN products p ON p.id = s.product_uuid "
            "WHERE b.created_at::date BETWEEN %s AND %s ORDER BY b.created_at, b.id",
            (start, end),
        )

    def reports(self, start: date, end: date) -> list[dict[str, Any]]:
        return self._all(
            "SELECT id AS report_id, kind, mode_label, sha256, created_at FROM reports "
            "WHERE created_at::date BETWEEN %s AND %s ORDER BY created_at, id",
            (start, end),
        )

    def snapshots(self, start: date, end: date) -> list[dict[str, Any]]:
        return self._all(
            "SELECT s.id AS snapshot_id, p.product_id AS product, s.range_start, s.range_end, "
            "s.candle_count, s.gap_count, s.missing_count, s.file_sha256, s.content_sha256, "
            "s.engine_version FROM dataset_snapshots s JOIN products p ON p.id = s.product_uuid "
            "WHERE s.created_at::date BETWEEN %s AND %s ORDER BY s.created_at, s.id",
            (start, end),
        )

    def paper_orders(self, start: date, end: date) -> list[dict[str, Any]]:
        return self._all(
            "SELECT o.id AS order_id, o.seq, o.level_index, o.cycle_no, o.side, o.price, "
            "o.base_qty, o.filled_qty, o.state, o.placed_candle FROM paper_orders o "
            "WHERE o.created_at::date BETWEEN %s AND %s ORDER BY o.created_at, o.seq",
            (start, end),
        )

    def paper_fills(self, start: date, end: date) -> list[dict[str, Any]]:
        return self._all(
            "SELECT f.order_id, f.candle_start, f.price, f.base_qty, f.notional, f.fee "
            "FROM paper_fills f JOIN paper_orders o ON o.id = f.order_id "
            "WHERE to_timestamp(f.candle_start)::date BETWEEN %s AND %s "
            "ORDER BY f.candle_start, f.id",
            (start, end),
        )

    def paper_summary(self) -> dict[str, Any]:
        row = self._conn.execute(
            "SELECT s.state, s.phase, "
            "COALESCE((SELECT sum(quote_delta) FROM paper_ledger_entries), 0) AS cash, "
            "COALESCE((SELECT sum(quote_reserved) FROM paper_orders WHERE state = 'OPEN'), 0) "
            "AS reserved, COALESCE((SELECT sum(base_qty) FROM paper_positions), 0) AS inventory, "
            "COALESCE((SELECT sum(cost_basis) FROM paper_positions), 0) AS cost_basis, "
            "(SELECT count(*) FROM paper_fills) AS fills, "
            "COALESCE((SELECT sum(fee) FROM paper_fills), 0) AS fees_paid "
            "FROM paper_session s WHERE s.id"
        ).fetchone()
        return dict(row) if row else {}

    def pair_lifecycle(self, start: date, end: date) -> list[dict[str, Any]]:
        return self._all(
            "SELECT p.product_id AS product, h.version_after, h.state_before, h.state_after, "
            "h.actor_class, h.transition_no, h.reason_code, h.occurred_at "
            "FROM pair_state_history h JOIN pairs x ON x.id = h.pair_id "
            "JOIN products p ON p.id = x.product_uuid "
            "WHERE h.occurred_at::date BETWEEN %s AND %s ORDER BY h.occurred_at, h.id",
            (start, end),
        )

    def data_quality_runs(self, start: date, end: date) -> list[dict[str, Any]]:
        return self._all(
            "SELECT r.id AS run_id, p.product_id AS product, r.status, r.window_start, "
            "r.window_end, r.requests, r.retries, r.fetched, r.inserted, r.duplicates, "
            "r.conflicts, r.malformed, r.gaps, r.missing FROM ingest_runs r "
            "JOIN products p ON p.id = r.product_uuid "
            "WHERE r.started_at::date BETWEEN %s AND %s ORDER BY r.id",
            (start, end),
        )

    def data_quality_events(self, start: date, end: date) -> list[dict[str, Any]]:
        return self._all(
            "SELECT e.ingest_run_id AS run_id, e.code, e.severity, sum(e.count)::int AS count "
            "FROM data_quality_events e JOIN ingest_runs r ON r.id = e.ingest_run_id "
            "WHERE r.started_at::date BETWEEN %s AND %s "
            "GROUP BY e.ingest_run_id, e.code, e.severity ORDER BY 1, 2, 3",
            (start, end),
        )

    def grid_plans(self, start: date, end: date) -> list[dict[str, Any]]:
        return self._all(
            "SELECT g.id AS plan_id, p.product_id AS product, g.levels, g.lower_price, "
            "g.upper_price, g.prices, g.cell_budget, g.config_sha256, g.created_at "
            "FROM grid_plans g JOIN pairs x ON x.id = g.pair_id "
            "JOIN products p ON p.id = x.product_uuid "
            "WHERE g.created_at::date BETWEEN %s AND %s ORDER BY g.created_at, g.id",
            (start, end),
        )

    def audit_summary(self, start: date, end: date) -> list[dict[str, Any]]:
        """Counts only: no actor, target, reason, client tag, request id or detail leaves here."""
        return self._all(
            "SELECT occurred_at::date AS day, event_code, result, count(*)::int AS count "
            "FROM audit_events WHERE occurred_at::date BETWEEN %s AND %s "
            "GROUP BY 1, 2, 3 ORDER BY 1, 2, 3",
            (start, end),
        )
