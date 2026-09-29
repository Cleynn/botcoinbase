"""SQL for the local paper venue (orders, fills, ledger, position, session). PAPER only."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from app.storage.repositories import Conn


@dataclass(frozen=True)
class SessionRow:
    state: str
    pair_id: UUID | None
    grid_plan_id: UUID | None
    last_candle_start: int | None
    phase: str
    peak_equity: Decimal
    updated_at: datetime


@dataclass(frozen=True)
class OrderRow:
    id: UUID
    pair_id: UUID
    grid_plan_id: UUID
    client_order_id: UUID
    seq: int
    level_index: int
    cycle_no: int
    side: str
    price: Decimal
    base_qty: Decimal
    filled_qty: Decimal
    quote_reserved: Decimal
    state: str
    placed_candle: int
    created_at: datetime
    updated_at: datetime


_ORDER_FIELDS = tuple(OrderRow.__dataclass_fields__)


class PaperRepository:
    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    # ------------------------------------------------------------------ session
    def session(self, *, for_update: bool = False) -> SessionRow:
        row = self._conn.execute(
            "SELECT state, pair_id, grid_plan_id, last_candle_start, phase, peak_equity, updated_at "
            "FROM paper_session WHERE id" + (" FOR UPDATE" if for_update else "")
        ).fetchone()
        if row is None:  # pragma: no cover  (the migration inserts the single row)
            raise RuntimeError("paper session row is missing")
        return SessionRow(**row)

    def update_session(self, now: datetime, **fields: Any) -> None:
        allowed = {"state", "pair_id", "grid_plan_id", "last_candle_start", "phase", "peak_equity"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("unsupported session field")
        assignments = ", ".join(f"{name} = %s" for name in fields)
        self._conn.execute(
            f"UPDATE paper_session SET {assignments}, updated_at = %s WHERE id",
            (*fields.values(), now),
        )

    # ------------------------------------------------------------------ ledger and position
    def cash(self) -> Decimal:
        row = self._conn.execute(
            "SELECT COALESCE(sum(quote_delta), 0) AS c FROM paper_ledger_entries"
        ).fetchone()
        return row["c"] if row else Decimal(0)

    def has_deposit(self) -> bool:
        return (
            self._conn.execute(
                "SELECT 1 FROM paper_ledger_entries WHERE kind = 'DEPOSIT'"
            ).fetchone()
            is not None
        )

    def add_entry(
        self,
        key: str,
        kind: str,
        quote_delta: Decimal,
        base_delta: Decimal,
        order_id: UUID | None,
        now: datetime,
    ) -> bool:
        cur = self._conn.execute(
            "INSERT INTO paper_ledger_entries (entry_key, kind, quote_delta, base_delta, order_id, "
            "occurred_at) VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (entry_key) DO NOTHING",
            (key, kind, quote_delta, base_delta, order_id, now),
        )
        return cur.rowcount == 1

    def position(self, pair_id: UUID) -> tuple[Decimal, Decimal]:
        row = self._conn.execute(
            "SELECT base_qty, cost_basis FROM paper_positions WHERE pair_id = %s", (pair_id,)
        ).fetchone()
        return (row["base_qty"], row["cost_basis"]) if row else (Decimal(0), Decimal(0))

    def total_position(self) -> tuple[Decimal, Decimal]:
        row = self._conn.execute(
            "SELECT COALESCE(sum(base_qty), 0) AS q, COALESCE(sum(cost_basis), 0) AS c FROM paper_positions"
        ).fetchone()
        return (row["q"], row["c"]) if row else (Decimal(0), Decimal(0))

    def set_position(self, pair_id: UUID, qty: Decimal, cost: Decimal, now: datetime) -> None:
        self._conn.execute(
            "INSERT INTO paper_positions (pair_id, base_qty, cost_basis, updated_at) VALUES (%s, %s, %s, %s) "
            "ON CONFLICT (pair_id) DO UPDATE SET base_qty = EXCLUDED.base_qty, "
            "cost_basis = EXCLUDED.cost_basis, updated_at = EXCLUDED.updated_at",
            (pair_id, qty, cost, now),
        )

    # ------------------------------------------------------------------ orders and fills
    def insert_order(self, o: OrderRow) -> bool:
        cur = self._conn.execute(
            "INSERT INTO paper_orders (id, venue, pair_id, grid_plan_id, client_order_id, seq, "
            "level_index, cycle_no, side, price, base_qty, filled_qty, quote_reserved, order_type, "
            "post_only, state, placed_candle, created_at, updated_at) VALUES (%s, 'PAPER', %s, %s, %s, "
            "%s, %s, %s, %s, %s, %s, %s, %s, 'limit_limit_gtc', true, %s, %s, %s, %s) "
            "ON CONFLICT (client_order_id) DO NOTHING",
            (o.id, o.pair_id, o.grid_plan_id, o.client_order_id, o.seq, o.level_index, o.cycle_no,
             o.side, o.price, o.base_qty, o.filled_qty, o.quote_reserved, o.state, o.placed_candle,
             o.created_at, o.updated_at),
        )  # fmt: skip
        return cur.rowcount == 1

    def update_order(
        self, order_id: UUID, *, state: str, filled: Decimal, reserved: Decimal, now: datetime
    ) -> None:
        self._conn.execute(
            "UPDATE paper_orders SET state = %s, filled_qty = %s, quote_reserved = %s, updated_at = %s WHERE id = %s",
            (state, filled, reserved, now, order_id),
        )

    def open_orders(self) -> list[OrderRow]:
        rows = self._conn.execute(
            "SELECT * FROM paper_orders WHERE state = 'OPEN' ORDER BY seq"
        ).fetchall()
        return [OrderRow(**{k: r[k] for k in _ORDER_FIELDS}) for r in rows]

    def orders(self, limit: int = 50) -> list[OrderRow]:
        rows = self._conn.execute(
            "SELECT * FROM paper_orders ORDER BY created_at DESC, seq DESC LIMIT %s", (limit,)
        ).fetchall()
        return [OrderRow(**{k: r[k] for k in _ORDER_FIELDS}) for r in rows]

    def max_seq(self, plan_id: UUID) -> int:
        row = self._conn.execute(
            "SELECT COALESCE(max(seq), 0) AS m FROM paper_orders WHERE grid_plan_id = %s",
            (plan_id,),
        ).fetchone()
        return int(row["m"]) if row else 0

    def cell_side_count(self, plan_id: UUID, cell: int, side: str) -> int:
        row = self._conn.execute(
            "SELECT count(*) AS n FROM paper_orders WHERE grid_plan_id = %s AND level_index = %s AND side = %s",
            (plan_id, cell, side),
        ).fetchone()
        return int(row["n"]) if row else 0

    def pending_by_cell(self, plan_id: UUID) -> dict[int, Decimal]:
        """Bought quantity not yet covered by a sell: filled buys minus sells that still count."""
        rows = self._conn.execute(
            "SELECT level_index, "
            "COALESCE(sum(CASE WHEN side = 'BUY' THEN filled_qty ELSE 0 END), 0) AS bought, "
            "COALESCE(sum(CASE WHEN side = 'SELL' THEN CASE WHEN state = 'CANCELLED' THEN filled_qty "
            "ELSE base_qty END ELSE 0 END), 0) AS covered "
            "FROM paper_orders WHERE grid_plan_id = %s GROUP BY level_index",
            (plan_id,),
        ).fetchall()
        return {r["level_index"]: max(r["bought"] - r["covered"], Decimal(0)) for r in rows}

    def insert_fill(
        self,
        order_id: UUID,
        candle: int,
        price: Decimal,
        qty: Decimal,
        notional: Decimal,
        fee: Decimal,
    ) -> bool:
        cur = self._conn.execute(
            "INSERT INTO paper_fills (order_id, candle_start, price, base_qty, notional, fee, liquidity) "
            "VALUES (%s, %s, %s, %s, %s, %s, 'MAKER') ON CONFLICT ON CONSTRAINT paper_fills_once_per_candle DO NOTHING",
            (order_id, candle, price, qty, notional, fee),
        )
        return cur.rowcount == 1

    # ------------------------------------------------------------------ read models
    def counts(self) -> dict[str, int]:
        rows = self._conn.execute(
            "SELECT state, count(*) AS n FROM paper_orders GROUP BY state"
        ).fetchall()
        return {r["state"]: int(r["n"]) for r in rows}

    def fill_stats(self) -> tuple[int, Decimal]:
        row = self._conn.execute(
            "SELECT count(*) AS n, COALESCE(sum(fee), 0) AS f FROM paper_fills"
        ).fetchone()
        return (int(row["n"]), row["f"]) if row else (0, Decimal(0))

    def reserved(self) -> Decimal:
        row = self._conn.execute(
            "SELECT COALESCE(sum(quote_reserved), 0) AS r FROM paper_orders WHERE state = 'OPEN'"
        ).fetchone()
        return row["r"] if row else Decimal(0)
