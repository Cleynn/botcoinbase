"""The execution gateway protocol and the only order request this build can express.

`OrderRequest` is a post-only limit GTC order and nothing else: there is no market order, no
stop, no leverage, no attached order and no way to spell one, so nothing (including the kill
switch) can market-sell. There is no live gateway class in this build. The implementations are the
deterministic fake used by tests (`app.exchange.fake`) and nothing else.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal, Protocol

CLIENT_ID_RE: Final = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
PRODUCT_RE: Final = re.compile(r"^[A-Z0-9]+-USDC$")


@dataclass(frozen=True)
class OrderRequest:
    client_order_id: str
    product_id: str
    side: Literal["BUY", "SELL"]
    price: Decimal
    base_qty: Decimal

    def __post_init__(self) -> None:
        if not CLIENT_ID_RE.fullmatch(self.client_order_id):
            raise ValueError("client_order_id must be a lowercase UUID")
        if not PRODUCT_RE.fullmatch(self.product_id):
            raise ValueError("product_id must be a USDC spot product")
        if self.side not in ("BUY", "SELL"):
            raise ValueError("side must be BUY or SELL")
        if self.price <= 0 or self.base_qty <= 0:
            raise ValueError("price and size must be positive")

    # The wire shape, fixed by construction: snake_case, post-only limit GTC, spot, no extras.
    def body(self) -> dict[str, object]:
        return {
            "client_order_id": self.client_order_id,
            "product_id": self.product_id,
            "side": self.side,
            "order_configuration": {
                "limit_limit_gtc": {
                    "base_size": format(self.base_qty, "f"),
                    "limit_price": format(self.price, "f"),
                    "post_only": True,
                }
            },
        }


SubmitOutcome = Literal["ACCEPTED", "REJECTED", "EXISTING", "UNKNOWN"]


@dataclass(frozen=True)
class SubmitResult:
    outcome: SubmitOutcome
    exchange_order_id: str | None = None
    reason: str | None = None  # fixed vocabulary


CancelOutcome = Literal["CANCEL_QUEUED", "REJECTED", "UNKNOWN"]


@dataclass(frozen=True)
class CancelResult:
    outcome: CancelOutcome
    reason: str | None = None


class ExecutionGateway(Protocol):
    venue: str

    def submit(self, request: OrderRequest) -> SubmitResult: ...

    def cancel(self, exchange_order_ids: Sequence[str]) -> dict[str, CancelResult]: ...
