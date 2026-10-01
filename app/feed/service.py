"""The exchange feed: one read of the Coinbase account, stored for the web interface.

Runs on the host (`td_ctl`, `feed` container) with the authenticated reader, which can only send
GET requests. It never uses the order gateway and never writes anything the order path reads: the
web tier shows these rows, and that is all. A failed read keeps the previous rows and records the
failure, so the page can say how old its data is.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import timedelta

from app.adapters.coinbase_parse import ParseError
from app.domain.models import Clock
from app.exchange.errors import ExchangeError
from app.exchange.reader import ExchangeReader
from app.storage.database import Storage
from app.storage.feed_repositories import FeedBalanceRow, FeedFillRow, FeedOrderRow

VENUE = "COINBASE"
LOOKBACK = timedelta(days=7)  # orders and fills shown by the web interface
_PORTFOLIO_RE = re.compile(r"^[A-Z_]{1,40}$")


@dataclass(frozen=True)
class FeedOutcome:
    ok: bool
    error_code: str | None = None
    balances: int = 0
    orders: int = 0
    fills: int = 0


class ExchangeFeed:
    def __init__(self, *, storage: Storage, reader: ExchangeReader, clock: Clock) -> None:
        self._storage = storage
        self._reader = reader
        self._clock = clock

    def refresh(self) -> FeedOutcome:
        now = self._clock.now()
        start, end = now - LOOKBACK, now + timedelta(minutes=1)  # the end is exclusive
        try:
            perms = self._reader.key_permissions()
            accounts = self._reader.list_accounts()
            orders = self._reader.list_orders(start, end)
            fills = self._reader.list_fills(start, end)
        except (ExchangeError, ParseError) as exc:
            # a malformed page cursor raises ParseError before the reader can wrap it
            code = exc.code if isinstance(exc, ExchangeError) else "UNEXPECTED_RESPONSE"
            with self._storage.tx() as repos:
                repos.feed.record_failure(VENUE, code)
            return FeedOutcome(False, code)
        balances = [
            FeedBalanceRow(a.currency, a.available, a.hold)
            for a in accounts
            if a.currency == "USDC" or a.available + a.hold > 0
        ]
        order_rows = [
            FeedOrderRow(
                o.order_id,
                o.product_id,
                o.side,
                o.status,
                o.price,
                o.base_qty,
                o.filled_qty,
                o.created_time,
            )
            for o in orders
        ]
        fill_rows = [
            FeedFillRow(
                f.fill_id,
                f.order_id,
                f.product_id,
                f.side,
                f.price,
                f.size,
                f.fee,
                f.liquidity,
                f.trade_time,
            )
            for f in fills
        ]
        with self._storage.tx() as repos:
            repos.feed.record_success(
                VENUE,
                can_view=perms.can_view,
                can_trade=perms.can_trade,
                can_transfer=perms.can_transfer,
                portfolio_type=perms.portfolio_type
                if _PORTFOLIO_RE.fullmatch(perms.portfolio_type)
                else "UNKNOWN",
                balances=balances,
                orders=order_rows,
                fills=fill_rows,
            )
        return FeedOutcome(True, None, len(balances), len(order_rows), len(fill_rows))
