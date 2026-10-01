"""SQL for the exchange feed: Coinbase account data shown by the web interface. The host replaces
the rows after each successful read; the web role can only select them (migration 0013). Values are
always bound parameters."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from app.storage.repositories import Conn


@dataclass(frozen=True)
class FeedStatusRow:
    venue: str
    state: str  # OK | FAILED
    error_code: str | None
    attempted_at: datetime
    succeeded_at: datetime | None
    can_view: bool | None
    can_trade: bool | None
    can_transfer: bool | None
    portfolio_type: str | None


@dataclass(frozen=True)
class FeedBalanceRow:
    currency: str
    available: Decimal
    hold: Decimal


@dataclass(frozen=True)
class FeedOrderRow:
    order_id: str
    product_id: str
    side: str
    status: str
    price: Decimal
    base_qty: Decimal
    filled_qty: Decimal
    created_time: datetime | None


@dataclass(frozen=True)
class FeedFillRow:
    fill_id: str
    order_id: str
    product_id: str
    side: str
    price: Decimal
    size: Decimal
    fee: Decimal
    liquidity: str
    trade_time: datetime | None


class FeedRepository:
    def __init__(self, conn: Conn) -> None:
        self._conn = conn

    # ------------------------------------------------------------------ host writes
    def record_success(
        self,
        venue: str,
        *,
        can_view: bool,
        can_trade: bool,
        can_transfer: bool,
        portfolio_type: str,
        balances: Sequence[FeedBalanceRow],
        orders: Sequence[FeedOrderRow],
        fills: Sequence[FeedFillRow],
    ) -> None:
        """Replace everything shown for `venue` in the caller's transaction (all or nothing)."""
        self._conn.execute(
            # one reading of the clock (materialized, or the planner would read it twice): the
            # two times of a successful read must be equal
            "WITH t AS MATERIALIZED (SELECT td_now() AS now) "
            "INSERT INTO exchange_feed (venue, state, error_code, attempted_at, succeeded_at, "
            "can_view, can_trade, can_transfer, portfolio_type) "
            "SELECT %s, 'OK', NULL, t.now, t.now, %s, %s, %s, %s FROM t "
            "ON CONFLICT (venue) DO UPDATE SET state = 'OK', error_code = NULL, "
            "attempted_at = EXCLUDED.attempted_at, succeeded_at = EXCLUDED.succeeded_at, "
            "can_view = EXCLUDED.can_view, can_trade = EXCLUDED.can_trade, "
            "can_transfer = EXCLUDED.can_transfer, portfolio_type = EXCLUDED.portfolio_type",
            (venue, can_view, can_trade, can_transfer, portfolio_type),
        )
        for table in ("exchange_feed_balances", "exchange_feed_orders", "exchange_feed_fills"):
            self._conn.execute(f"DELETE FROM {table} WHERE venue = %s", (venue,))  # noqa: S608
        for b in balances:
            self._conn.execute(
                "INSERT INTO exchange_feed_balances (venue, currency, available, hold) "
                "VALUES (%s, %s, %s, %s)",
                (venue, b.currency, b.available, b.hold),
            )
        for o in orders:
            self._conn.execute(
                "INSERT INTO exchange_feed_orders (venue, order_id, product_id, side, status, "
                "price, base_qty, filled_qty, created_time) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    venue,
                    o.order_id,
                    o.product_id,
                    o.side,
                    o.status,
                    o.price,
                    o.base_qty,
                    o.filled_qty,
                    o.created_time,
                ),
            )
        for f in fills:
            self._conn.execute(
                "INSERT INTO exchange_feed_fills (venue, fill_id, order_id, product_id, side, "
                "price, size, fee, liquidity, trade_time) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                (
                    venue,
                    f.fill_id,
                    f.order_id,
                    f.product_id,
                    f.side,
                    f.price,
                    f.size,
                    f.fee,
                    f.liquidity,
                    f.trade_time,
                ),
            )

    def record_failure(self, venue: str, error_code: str) -> None:
        """Note a failed read. The rows of the last successful read stay, with their time."""
        self._conn.execute(
            "INSERT INTO exchange_feed (venue, state, error_code, attempted_at) "
            "VALUES (%s, 'FAILED', %s, td_now()) "
            "ON CONFLICT (venue) DO UPDATE SET state = 'FAILED', "
            "error_code = EXCLUDED.error_code, attempted_at = EXCLUDED.attempted_at",
            (venue, error_code),
        )

    # ------------------------------------------------------------------ reads (web and host)
    def status(self, venue: str) -> FeedStatusRow | None:
        row = self._conn.execute(
            "SELECT venue, state, error_code, attempted_at, succeeded_at, can_view, can_trade, "
            "can_transfer, portfolio_type FROM exchange_feed WHERE venue = %s",
            (venue,),
        ).fetchone()
        return FeedStatusRow(**row) if row else None

    def balances(self, venue: str) -> list[FeedBalanceRow]:
        rows = self._conn.execute(
            "SELECT currency, available, hold FROM exchange_feed_balances WHERE venue = %s "
            "ORDER BY (currency = 'USDC') DESC, currency",
            (venue,),
        ).fetchall()
        return [FeedBalanceRow(**r) for r in rows]

    def orders(self, venue: str, limit: int) -> list[FeedOrderRow]:
        rows = self._conn.execute(
            "SELECT order_id, product_id, side, status, price, base_qty, filled_qty, created_time "
            "FROM exchange_feed_orders WHERE venue = %s "
            "ORDER BY created_time DESC NULLS LAST, order_id LIMIT %s",
            (venue, limit),
        ).fetchall()
        return [FeedOrderRow(**r) for r in rows]

    def fills(self, venue: str, limit: int) -> list[FeedFillRow]:
        rows = self._conn.execute(
            "SELECT fill_id, order_id, product_id, side, price, size, fee, liquidity, trade_time "
            "FROM exchange_feed_fills WHERE venue = %s "
            "ORDER BY trade_time DESC NULLS LAST, fill_id LIMIT %s",
            (venue, limit),
        ).fetchall()
        return [FeedFillRow(**r) for r in rows]

    def counts(self, venue: str) -> tuple[int, int, int]:
        row = self._conn.execute(
            "SELECT (SELECT count(*) FROM exchange_feed_balances WHERE venue = %s) AS balances, "
            "(SELECT count(*) FROM exchange_feed_orders WHERE venue = %s) AS orders, "
            "(SELECT count(*) FROM exchange_feed_fills WHERE venue = %s) AS fills",
            (venue, venue, venue),
        ).fetchone()
        if row is None:  # an aggregate always returns one row
            return 0, 0, 0
        return int(row["balances"]), int(row["orders"]), int(row["fills"])
