"""SQL repositories for products, pairs, validation evidence and history.

Lifecycle rules live in the database (migration 0002): these methods only issue statements. A
rejected transition surfaces as `psycopg.errors.IntegrityConstraintViolation`, which the service
layer turns into a denial.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from psycopg.types.json import Jsonb

from app.adapters.coinbase_parse import metadata_sha256
from app.domain.models import AuditRecord
from app.domain.pairs import (
    CheckResult,
    HistoryRow,
    PairRecord,
    PairState,
    ProductMetadata,
    ProductRecord,
    ValidationRun,
)
from app.storage.models import audit_from_row
from app.storage.repositories import Conn

_PAIR_LOCK = 7_272_040  # serialises adding pairs and activating one (cap and single-active)
SNAPSHOT_COLUMNS = (
    "s.status, s.is_disabled, s.trading_disabled, s.cancel_only, s.limit_only, s.post_only, "
    "s.auction_mode, s.base_increment, s.quote_increment, s.price_increment, s.base_min_size, "
    "s.base_max_size, s.quote_min_size, s.quote_max_size, s.alias, s.alias_to, s.malformed"
)


def _metadata(row: dict[str, Any]) -> ProductMetadata:
    return ProductMetadata(
        product_id=row["product_id"],
        base_currency=row["base_currency"],
        quote_currency=row["quote_currency"],
        product_type=row["product_type"],
        venue=row["venue"],
        status=row["status"],
        is_disabled=row["is_disabled"],
        trading_disabled=row["trading_disabled"],
        cancel_only=row["cancel_only"],
        limit_only=row["limit_only"],
        post_only=row["post_only"],
        auction_mode=row["auction_mode"],
        base_increment=row["base_increment"],
        quote_increment=row["quote_increment"],
        price_increment=row["price_increment"],
        base_min_size=row["base_min_size"],
        base_max_size=row["base_max_size"],
        quote_min_size=row["quote_min_size"],
        quote_max_size=row["quote_max_size"],
        alias=row["alias"],
        alias_to=tuple(row["alias_to"] or ()),
        malformed=tuple(row["malformed"] or ()),
    )


def _product(row: dict[str, Any]) -> ProductRecord:
    return ProductRecord(
        id=row["id"],
        metadata=_metadata(row),
        snapshot_id=row["snapshot_id"],
        snapshot_sha256=row["sha256"],
        discovered_rank=row["discovered_rank"],
        first_seen_at=row["first_seen_at"],
        last_seen_at=row["last_seen_at"],
        last_verified_at=row["last_verified_at"],
        server_time_offset_ms=row["server_time_offset_ms"],
    )


def _pair(row: dict[str, Any]) -> PairRecord:
    return PairRecord(
        id=row["id"],
        product_uuid=row["product_uuid"],
        product_id=row["product_id"],
        state=PairState(row["state"]),
        version=row["version"],
        order_product_id=row["order_product_id"],
        data_product_id=row["data_product_id"],
        data_basis=row["data_basis"],
        proposed_via=row["proposed_via"],
        proposed_by=row["proposed_by"],
        proposed_at=row["proposed_at"],
        state_changed_at=row["state_changed_at"],
        ever_active=row["ever_active"],
        eligible_run_id=row["eligible_run_id"],
        successor_of=row["successor_of"],
    )


def _run(row: dict[str, Any]) -> ValidationRun:
    checks = row["checks"] if isinstance(row["checks"], list) else json.loads(row["checks"])
    return ValidationRun(
        id=row["id"],
        pair_id=row["pair_id"],
        pair_version=row["pair_version"],
        snapshot_id=row["snapshot_id"],
        started_at=row["started_at"],
        finished_at=row["finished_at"],
        outcome=row["outcome"],
        checks=tuple(
            CheckResult(
                c["code"], c["status"], c["reason"], c["message"], dict(c.get("observed") or {})
            )
            for c in checks
        ),
        thresholds_sha256=row["thresholds_sha256"],
        expires_at=row["expires_at"],
    )


class ProductRepository:
    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    _SELECT = (
        "SELECT p.id, p.product_id, p.base_currency, p.quote_currency, p.product_type, p.venue, "
        "p.discovered_rank, p.first_seen_at, p.last_seen_at, "
        f"{SNAPSHOT_COLUMNS}, s.sha256, c.snapshot_id, c.last_verified_at, c.server_time_offset_ms "
        "FROM products p JOIN product_metadata_current c ON c.product_uuid = p.id "
        "JOIN product_metadata_snapshots s ON s.id = c.snapshot_id "
    )

    def get(self, product_uuid: UUID) -> ProductRecord | None:
        row = self._conn.execute(self._SELECT + "WHERE p.id = %s", (product_uuid,)).fetchone()
        return _product(row) if row else None

    def get_by_product_id(self, product_id: str) -> ProductRecord | None:
        row = self._conn.execute(self._SELECT + "WHERE p.product_id = %s", (product_id,)).fetchone()
        return _product(row) if row else None

    def list_all(self) -> list[ProductRecord]:
        rows = self._conn.execute(
            self._SELECT + "ORDER BY p.discovered_rank NULLS LAST, p.product_id"
        ).fetchall()
        return [_product(r) for r in rows]

    def count(self) -> int:
        row = self._conn.execute("SELECT count(*) AS n FROM products").fetchone()
        return int(row["n"]) if row else 0

    def metadata_snapshot(self, snapshot_id: UUID) -> ProductMetadata | None:
        """The frozen rules of one metadata snapshot (what a dataset snapshot points at)."""
        row = self._conn.execute(
            "SELECT p.product_id, p.base_currency, p.quote_currency, p.product_type, p.venue, "
            f"{SNAPSHOT_COLUMNS} FROM product_metadata_snapshots s "
            "JOIN products p ON p.id = s.product_uuid WHERE s.id = %s",
            (snapshot_id,),
        ).fetchone()
        return _metadata(row) if row else None

    def record_seen(
        self,
        meta: ProductMetadata,
        *,
        rank: int | None,
        now: datetime,
        offset_ms: int | None,
    ) -> tuple[UUID, bool, bool]:
        """Upsert a product and its metadata. Returns (product uuid, is_new, content_changed)."""
        existing = self._conn.execute(
            "SELECT id FROM products WHERE product_id = %s", (meta.product_id,)
        ).fetchone()
        is_new = existing is None
        if existing is None:
            product_uuid = uuid4()
            self._conn.execute(
                "INSERT INTO products (id, product_id, base_currency, quote_currency, "
                "product_type, venue, discovered_rank, first_seen_at, last_seen_at) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    product_uuid,
                    meta.product_id,
                    meta.base_currency,
                    meta.quote_currency,
                    meta.product_type,
                    meta.venue,
                    rank,
                    now,
                    now,
                ),
            )
        else:
            product_uuid = existing["id"]
            self._conn.execute(
                "UPDATE products SET discovered_rank = COALESCE(%s, discovered_rank), "
                "last_seen_at = %s WHERE id = %s",
                (rank, now, product_uuid),
            )
        digest = metadata_sha256(meta)
        inserted = self._conn.execute(
            "INSERT INTO product_metadata_snapshots (id, product_uuid, sha256, status, "
            "is_disabled, trading_disabled, cancel_only, limit_only, post_only, auction_mode, "
            "base_increment, quote_increment, price_increment, base_min_size, base_max_size, "
            "quote_min_size, quote_max_size, alias, alias_to, malformed, first_seen_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, "
            "%s, %s) ON CONFLICT (product_uuid, sha256) DO NOTHING RETURNING id",
            (
                uuid4(),
                product_uuid,
                digest,
                meta.status,
                meta.is_disabled,
                meta.trading_disabled,
                meta.cancel_only,
                meta.limit_only,
                meta.post_only,
                meta.auction_mode,
                meta.base_increment,
                meta.quote_increment,
                meta.price_increment,
                meta.base_min_size,
                meta.base_max_size,
                meta.quote_min_size,
                meta.quote_max_size,
                meta.alias,
                list(meta.alias_to),
                list(meta.malformed),
                now,
            ),
        ).fetchone()
        if inserted is None:
            snapshot_id = self._conn.execute(
                "SELECT id FROM product_metadata_snapshots WHERE product_uuid = %s AND sha256 = %s",
                (product_uuid, digest),
            ).fetchone()["id"]  # type: ignore[index]
        else:
            snapshot_id = inserted["id"]
        self._conn.execute(
            "INSERT INTO product_metadata_current (product_uuid, snapshot_id, last_verified_at, "
            "server_time_offset_ms) VALUES (%s, %s, %s, %s) ON CONFLICT (product_uuid) DO UPDATE "
            "SET snapshot_id = EXCLUDED.snapshot_id, last_verified_at = EXCLUDED.last_verified_at, "
            "server_time_offset_ms = EXCLUDED.server_time_offset_ms",
            (product_uuid, snapshot_id, now, offset_ms),
        )
        changed = inserted is not None and not is_new
        return product_uuid, is_new, changed


class PairRepository:
    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    _SELECT = "SELECT p.*, pr.product_id FROM pairs p JOIN products pr ON pr.id = p.product_uuid "

    def lock_capacity(self) -> None:
        """Serialise proposing and activating (transaction-scoped advisory lock)."""
        self._conn.execute("SELECT pg_advisory_xact_lock(%s)", (_PAIR_LOCK,))

    def get(self, pair_id: UUID, *, for_update: bool = False) -> PairRecord | None:
        lock = " FOR UPDATE OF p" if for_update else ""
        row = self._conn.execute(self._SELECT + "WHERE p.id = %s" + lock, (pair_id,)).fetchone()
        return _pair(row) if row else None

    def list_all(self) -> list[PairRecord]:
        rows = self._conn.execute(self._SELECT + "ORDER BY p.proposed_at, p.id").fetchall()
        return [_pair(r) for r in rows]

    def in_states(self, states: tuple[PairState, ...]) -> list[PairRecord]:
        rows = self._conn.execute(
            self._SELECT + "WHERE p.state = ANY(%s) ORDER BY p.proposed_at, p.id",
            ([s.value for s in states],),
        ).fetchall()
        return [_pair(r) for r in rows]

    def open_for_product(self, product_uuid: UUID) -> PairRecord | None:
        row = self._conn.execute(
            self._SELECT + "WHERE p.product_uuid = %s AND p.state <> 'ARCHIVED'", (product_uuid,)
        ).fetchone()
        return _pair(row) if row else None

    def latest_archived_for_product(self, product_uuid: UUID) -> UUID | None:
        row = self._conn.execute(
            "SELECT id FROM pairs WHERE product_uuid = %s AND state = 'ARCHIVED' "
            "ORDER BY state_changed_at DESC, id LIMIT 1",
            (product_uuid,),
        ).fetchone()
        return row["id"] if row else None

    def count_open(self) -> int:
        row = self._conn.execute(
            "SELECT count(*) AS n FROM pairs WHERE state <> 'ARCHIVED'"
        ).fetchone()
        return int(row["n"]) if row else 0

    def others_in_states(self, pair_id: UUID, states: frozenset[PairState]) -> list[PairRecord]:
        rows = self._conn.execute(
            self._SELECT + "WHERE p.id <> %s AND p.state = ANY(%s) ORDER BY p.proposed_at",
            (pair_id, [s.value for s in states]),
        ).fetchall()
        return [_pair(r) for r in rows]

    def state_counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT state, count(*) AS n FROM pairs GROUP BY state"
        ).fetchall()
        return {r["state"]: int(r["n"]) for r in rows}

    def insert(
        self,
        *,
        pair_id: UUID,
        product_uuid: UUID,
        order_product_id: str,
        data_product_id: str | None,
        data_basis: str,
        proposed_via: str,
        proposed_by: UUID | None,
        now: datetime,
        successor_of: UUID | None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO pairs (id, product_uuid, state, version, order_product_id, "
            "data_product_id, data_basis, proposed_via, proposed_by, proposed_at, "
            "state_changed_at, ever_active, eligible_run_id, successor_of) "
            "VALUES (%s, %s, 'PROPOSED', 1, %s, %s, %s, %s, %s, %s, %s, false, NULL, %s)",
            (
                pair_id,
                product_uuid,
                order_product_id,
                data_product_id,
                data_basis,
                proposed_via,
                proposed_by,
                now,
                now,
                successor_of,
            ),
        )

    def transition(
        self,
        pair: PairRecord,
        new_state: PairState,
        now: datetime,
        *,
        ever_active: bool | None = None,
        eligible_run_id: UUID | None = None,
    ) -> bool:
        """Compare-and-set on the version. False means someone else changed the pair first."""
        cur = self._conn.execute(
            "UPDATE pairs SET state = %s, version = version + 1, state_changed_at = %s, "
            "ever_active = %s, eligible_run_id = %s WHERE id = %s AND version = %s",
            (
                new_state.value,
                now,
                pair.ever_active if ever_active is None else ever_active,
                eligible_run_id if eligible_run_id is not None else pair.eligible_run_id,
                pair.id,
                pair.version,
            ),
        )
        return cur.rowcount == 1

    def add_history(
        self,
        *,
        pair_id: UUID,
        version_after: int,
        state_before: str | None,
        state_after: str,
        actor_class: str,
        actor_user_id: UUID | None,
        transition_no: int,
        reason_code: str | None,
        occurred_at: datetime,
        request_id: str | None,
        audit_seq: int | None,
    ) -> None:
        self._conn.execute(
            "INSERT INTO pair_state_history (pair_id, version_after, state_before, state_after, "
            "actor_class, actor_user_id, transition_no, reason_code, occurred_at, request_id, "
            "audit_seq) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                pair_id,
                version_after,
                state_before,
                state_after,
                actor_class,
                actor_user_id,
                transition_no,
                reason_code,
                occurred_at,
                request_id,
                audit_seq,
            ),
        )

    def history(self, pair_id: UUID) -> list[HistoryRow]:
        rows = self._conn.execute(
            "SELECT h.version_after, h.state_before, h.state_after, h.actor_class, "
            "u.username AS actor_username, h.transition_no, h.reason_code, h.occurred_at, "
            "h.audit_seq FROM pair_state_history h LEFT JOIN users u ON u.id = h.actor_user_id "
            "WHERE h.pair_id = %s ORDER BY h.version_after",
            (pair_id,),
        ).fetchall()
        return [
            HistoryRow(
                r["version_after"],
                r["state_before"],
                r["state_after"],
                r["actor_class"],
                r["actor_username"],
                r["transition_no"],
                r["reason_code"],
                r["occurred_at"],
                r["audit_seq"],
            )
            for r in rows
        ]

    # ------------------------------------------------------------------ validation evidence
    def insert_run(self, run: ValidationRun) -> None:
        checks = [
            {
                "code": c.code,
                "status": c.status,
                "reason": c.reason,
                "message": c.message,
                "observed": c.observed,
            }
            for c in run.checks
        ]
        self._conn.execute(
            "INSERT INTO pair_validation_runs (id, pair_id, pair_version, snapshot_id, "
            "started_at, finished_at, outcome, checks, thresholds_sha256, expires_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                run.id,
                run.pair_id,
                run.pair_version,
                run.snapshot_id,
                run.started_at,
                run.finished_at,
                run.outcome,
                Jsonb(checks),
                run.thresholds_sha256,
                run.expires_at,
            ),
        )

    def get_run(self, run_id: UUID) -> ValidationRun | None:
        row = self._conn.execute(
            "SELECT * FROM pair_validation_runs WHERE id = %s", (run_id,)
        ).fetchone()
        return _run(row) if row else None

    def latest_run(self, pair_id: UUID) -> ValidationRun | None:
        row = self._conn.execute(
            "SELECT * FROM pair_validation_runs WHERE pair_id = %s "
            "ORDER BY started_at DESC, finished_at DESC LIMIT 1",
            (pair_id,),
        ).fetchone()
        return _run(row) if row else None

    def runs(self, pair_id: UUID, limit: int = 10) -> list[ValidationRun]:
        rows = self._conn.execute(
            "SELECT * FROM pair_validation_runs WHERE pair_id = %s "
            "ORDER BY started_at DESC, finished_at DESC LIMIT %s",
            (pair_id, limit),
        ).fetchall()
        return [_run(r) for r in rows]

    # ------------------------------------------------------------------ audit timeline
    def audit_timeline(self, pair_id: UUID, limit: int = 100) -> list[AuditRecord]:
        rows = self._conn.execute(
            "SELECT e.*, u.username AS actor_username FROM audit_events e "
            "LEFT JOIN users u ON u.id = e.actor_user_id "
            "WHERE e.target_type = 'pair' AND e.target_id = %s ORDER BY e.seq DESC LIMIT %s",
            (str(pair_id), limit),
        ).fetchall()
        return [audit_from_row(r) for r in rows]

    # ------------------------------------------------------------------ monitoring aggregates
    def oldest_metadata_age_basis(self) -> datetime | None:
        """Oldest `last_verified_at` among products that have a non-archived pair."""
        row = self._conn.execute(
            "SELECT min(c.last_verified_at) AS t FROM product_metadata_current c "
            "JOIN pairs p ON p.product_uuid = c.product_uuid WHERE p.state <> 'ARCHIVED'"
        ).fetchone()
        return row["t"] if row else None
