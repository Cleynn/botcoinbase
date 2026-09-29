"""FAKE-EXCHANGE: a scripted test double for the exchange boundary. PROVENANCE: TEST-DOUBLE.

It is not a market and not a fill simulator: it never matches orders, moves prices or fills
anything by itself. Tests state exactly what the "exchange" holds (orders, fills, balances) and
which faults the next calls raise; that is what makes fault injection precise. Nothing here talks
to a network. The venue is `FAKE`, which the database accepts but the live gate refuses in
production.
"""

from __future__ import annotations

import itertools
from collections import defaultdict, deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import Final

from app.adapters.coinbase_parse import ParseError
from app.exchange.errors import ExchangeError
from app.exchange.gateway import CancelResult, OrderRequest, SubmitResult
from app.exchange.models import (
    TERMINAL,
    Balance,
    ExchangeFill,
    ExchangeOrder,
    KeyPermissions,
)

PROVENANCE: Final = "TEST-DOUBLE (scripted; no market, no matching, no fill simulation)"


@dataclass(frozen=True)
class Fault:
    """What the next call of an operation does instead of working."""

    kind: str  # timeout | network | server_error | rate_limited | reject | malformed | empty
    processed: bool = False  # submit only: the exchange DID take the order before failing
    reason: str = "INVALID_LIMIT_PRICE_POST_ONLY"  # reject only


class FakeExchange:
    venue = "FAKE"

    def __init__(self, now: Callable[[], datetime]) -> None:
        self._now = now
        self._ids = itertools.count(1)
        self._fill_ids = itertools.count(1)
        self.orders: dict[str, ExchangeOrder] = {}
        self.fills: list[ExchangeFill] = []
        self.balances: dict[str, Balance] = {}
        self.permissions = KeyPermissions(True, True, False, "DEFAULT")
        self._faults: dict[str, deque[Fault]] = defaultdict(deque)
        self.calls: list[str] = []  # every operation, in order (asserted by tests)
        self.hidden: set[str] = set()  # client ids the listing omits (lag)

    # ------------------------------------------------------------------ scripting
    def fail(self, operation: str, *faults: Fault) -> None:
        self._faults[operation].extend(faults)

    def set_balance(self, currency: str, available: str, hold: str = "0") -> None:
        self.balances[currency] = Balance(currency, Decimal(available), Decimal(hold))

    def add_foreign_order(self, product_id: str = "BTC-USDC", side: str = "BUY") -> str:
        order_id = f"fx-{next(self._ids):08d}"
        self.orders[order_id] = ExchangeOrder(
            order_id, f"foreign-{order_id}", product_id, side, "WORKING", "limit_limit_gtc",
            True, Decimal("100"), Decimal("1"), Decimal(0), self._now(),
        )  # fmt: skip
        return order_id

    def inject_fill(
        self,
        order_id: str,
        price: str,
        size: str,
        fee: str = "0",
        liquidity: str = "MAKER",
    ) -> ExchangeFill:
        order = self.orders[order_id]
        fill = ExchangeFill(
            f"fill-{next(self._fill_ids):08d}", order_id, order.product_id, order.side,
            Decimal(price), Decimal(size), Decimal(fee), liquidity, self._now(),
        )  # fmt: skip
        self.fills.append(fill)
        filled = order.filled_qty + fill.size
        status = "FILLED" if filled >= order.base_qty else order.status
        self.orders[order_id] = replace(order, filled_qty=filled, status=status)
        return fill

    def set_status(self, order_id: str, status: str) -> None:
        self.orders[order_id] = replace(self.orders[order_id], status=status)

    def by_client_id(self, client_order_id: str) -> ExchangeOrder | None:
        return next((o for o in self.orders.values() if o.client_order_id == client_order_id), None)

    # ------------------------------------------------------------------ faults
    def _next_fault(self, operation: str) -> Fault | None:
        self.calls.append(operation)
        queue = self._faults[operation]
        return queue.popleft() if queue else None

    @staticmethod
    def _raise(fault: Fault, *, ambiguous: bool) -> None:
        if fault.kind == "timeout":
            raise ExchangeError("TIMEOUT", ambiguous=ambiguous)
        if fault.kind == "network":
            raise ExchangeError("NETWORK", ambiguous=ambiguous)
        if fault.kind == "server_error":
            raise ExchangeError("SERVER_ERROR", status=500, ambiguous=ambiguous)
        if fault.kind == "rate_limited":
            raise ExchangeError("RATE_LIMITED", status=429)
        if fault.kind == "malformed":
            raise ParseError("ORDER_MALFORMED")

    # ------------------------------------------------------------------ reader
    def _read(self, operation: str) -> Fault | None:
        fault = self._next_fault(operation)
        if fault is not None and fault.kind != "empty":
            self._raise(fault, ambiguous=False)
        return fault

    def list_accounts(self) -> tuple[Balance, ...]:
        self._read("list_accounts")
        return tuple(self.balances.values())

    def list_orders(self, start: datetime, end: datetime) -> tuple[ExchangeOrder, ...]:
        fault = self._read("list_orders")
        if fault is not None:  # "empty": the listing lags and shows nothing
            return ()
        return tuple(
            o
            for o in self.orders.values()
            if o.client_order_id not in self.hidden
            and o.created_time is not None
            and start <= o.created_time < end
        )

    def list_fills(self, start: datetime, end: datetime) -> tuple[ExchangeFill, ...]:
        fault = self._read("list_fills")
        if fault is not None:
            return ()
        return tuple(
            f for f in self.fills if f.trade_time is not None and start <= f.trade_time < end
        )

    def get_order(self, order_id: str) -> ExchangeOrder | None:
        self._read("get_order")
        return self.orders.get(order_id)

    def key_permissions(self) -> KeyPermissions:
        self._read("key_permissions")
        return self.permissions

    # ------------------------------------------------------------------ gateway
    def submit(self, request: OrderRequest) -> SubmitResult:
        fault = self._next_fault("submit")
        existing = self.by_client_id(request.client_order_id)
        if existing is not None and (fault is None or fault.kind != "timeout"):
            same = (
                existing.product_id == request.product_id
                and existing.side == request.side
                and existing.price == request.price
                and existing.base_qty == request.base_qty
            )
            if not same:
                return SubmitResult("REJECTED", None, "DUPLICATE_CLIENT_ORDER_ID_MISMATCH")
            return SubmitResult("EXISTING", existing.order_id, "DUPLICATE_CLIENT_ORDER_ID")
        if fault is not None and fault.kind == "reject":
            return SubmitResult("REJECTED", None, fault.reason)
        if fault is not None and fault.processed:
            self._accept(request)  # the exchange took it, then the call failed
        if fault is not None:
            self._raise(fault, ambiguous=True)
        order = self._accept(request)
        return SubmitResult("ACCEPTED", order.order_id)

    def _accept(self, request: OrderRequest) -> ExchangeOrder:
        existing = self.by_client_id(request.client_order_id)
        if existing is not None:
            return existing
        order = ExchangeOrder(
            f"fx-{next(self._ids):08d}", request.client_order_id, request.product_id,
            request.side, "WORKING", "limit_limit_gtc", True, request.price,
            request.base_qty, Decimal(0), self._now(),
        )  # fmt: skip
        self.orders[order.order_id] = order
        return order

    def cancel(self, exchange_order_ids: Sequence[str]) -> dict[str, CancelResult]:
        fault = self._next_fault("cancel")
        if fault is not None:
            self._raise(fault, ambiguous=True)
        out: dict[str, CancelResult] = {}
        for order_id in exchange_order_ids:
            order = self.orders.get(order_id)
            if order is None:
                out[order_id] = CancelResult("REJECTED", "UNKNOWN_ORDER")
            elif order.status in TERMINAL:
                out[order_id] = CancelResult("REJECTED", "ORDER_ALREADY_TERMINAL")
            else:
                self.orders[order_id] = replace(order, status="CANCEL_REQUESTED")
                out[order_id] = CancelResult("CANCEL_QUEUED")
        return out

    def finish_cancel(self, order_id: str) -> None:
        """Tests decide when a queued cancel completes (the fake never advances by itself)."""
        self.set_status(order_id, "CANCELLED")
