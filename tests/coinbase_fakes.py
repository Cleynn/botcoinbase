"""SYNTHETIC Coinbase public-API responses for tests. NOT recorded from Coinbase.

The shapes follow docs/exchange-contract.md (CF-8 to CF-10) as written in the baseline. They have
not been checked against the live API (open item AS-C1): the sandbox this repository is developed
in cannot reach api.coinbase.com. Treat a green test as "the code handles this documented shape",
never as "Coinbase behaves like this".
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx

from app.adapters.coinbase_public import BASE_URL, CoinbasePublicClient
from app.adapters.ratelimit import RateLimiter
from app.config import ExchangeSettings

DAY = 86400
STEP = 300


def product_json(product_id: str = "BTC-USDC", **overrides: Any) -> dict[str, Any]:
    base = product_id.split("-")[0]
    body: dict[str, Any] = {
        "product_id": product_id,
        "price": "60000.00",
        "base_currency_id": base,
        "quote_currency_id": "USDC",
        "base_increment": "0.00000001",
        "quote_increment": "0.01",
        "price_increment": "0.01",
        "base_min_size": "0.00000001",
        "base_max_size": "3400",
        "quote_min_size": "1",
        "quote_max_size": "150000000",
        "base_name": "Synthetic",  # display strings are never stored
        "display_name": f"{base}-USDC",
        "status": "online",
        "is_disabled": False,
        "trading_disabled": False,
        "cancel_only": False,
        "limit_only": False,
        "post_only": False,
        "auction_mode": False,
        "product_type": "SPOT",
        "product_venue": "CBE",
        "alias": f"{base}-USD",
        "alias_to": [],
        "approximate_quote_24h_volume": "1000000",
    }
    body.update(overrides)
    return body


def daily_candles(
    server_epoch: int, *, days: int = 100, price: str = "60000"
) -> list[dict[str, str]]:
    """Closed daily candles ending yesterday; every day's range is at least 6%."""
    p = Decimal(price)
    today = server_epoch // DAY * DAY
    out = []
    for i in range(days, 0, -1):
        start = today - i * DAY
        wobble = Decimal(i % 5) / 200  # 0 .. 2%
        close = p * (1 + Decimal((i % 7) - 3) / 500)
        high = close * (Decimal("1.03") + wobble / 2)
        low = close * (Decimal("0.97") - wobble / 2)
        out.append(_candle(start, low, high, close * Decimal("0.999"), close))
    return out


def intraday_candles(
    server_epoch: int, *, count: int = 300, price: str = "60000"
) -> list[dict[str, str]]:
    """Five-minute candles; the last one is still open (not closed) at `server_epoch`."""
    p = Decimal(price)
    current = server_epoch // STEP * STEP
    out = []
    for i in range(count, -1, -1):
        start = current - i * STEP
        drift = Decimal((i % 9) - 4) / 10000
        close = p * (1 + drift)
        out.append(_candle(start, close * Decimal("0.9995"), close * Decimal("1.0005"), p, close))
    return out


def _candle(
    start: int, low: Decimal, high: Decimal, open_: Decimal, close: Decimal
) -> dict[str, str]:
    def s(x: Decimal) -> str:
        return format(x.quantize(Decimal("0.01")), "f")

    return {
        "start": str(start),
        "low": s(min(low, open_, close)),
        "high": s(max(high, open_, close)),
        "open": s(open_),
        "close": s(close),
        "volume": "12.5",
    }


def book_json(
    server_time: datetime,
    *,
    mid: str = "60000",
    spread_bps: str = "4",
    levels: int = 20,
    size: str | None = None,
) -> dict[str, Any]:
    m = Decimal(mid)
    if size is None:  # the same notional at every level, whatever the price
        size = format((Decimal(3000) / m).quantize(Decimal("0.00000001")), "f")
    half = m * Decimal(spread_bps) / 20000
    step = m / 10000  # one basis point per level, so every price stays positive
    quantum = Decimal("0.0001")
    bids = [
        {"price": format((m - half - i * step).quantize(quantum), "f"), "size": size}
        for i in range(levels)
    ]
    asks = [
        {"price": format((m + half + i * step).quantize(quantum), "f"), "size": size}
        for i in range(levels)
    ]
    return {
        "pricebook": {
            "product_id": "X",
            "bids": bids,
            "asks": asks,
            "time": server_time.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z"),
        }
    }


class FakeCoinbase:
    """An `httpx.MockTransport` handler serving synthetic public data and recording requests."""

    def __init__(self, now: Callable[[], datetime]) -> None:
        self._now = now
        self.products: dict[str, dict[str, Any]] = {
            "BTC-USDC": product_json("BTC-USDC"),
            "ETH-USDC": product_json("ETH-USDC", approximate_quote_24h_volume="800000"),
            "SOL-USDC": product_json("SOL-USDC", approximate_quote_24h_volume="500000"),
        }
        self.mids = {"BTC-USDC": "60000", "ETH-USDC": "3000", "SOL-USDC": "150"}
        self.requests: list[httpx.Request] = []
        self.errors: dict[str, int] = {}  # path suffix -> HTTP status to answer with
        self.raw: dict[str, bytes] = {}  # path suffix -> raw body to answer with
        self.drop_daily = 0  # remove this many oldest daily candles
        self.book_overrides: dict[str, dict[str, Any]] = {}
        self.intraday_gap = 0  # remove this many recent five-minute candles
        self.time_offset = 0
        self.server_epoch_override: int | None = None
        # optional: (product, start, end) -> raw candle dicts for FIVE_MINUTE requests
        self.range_source: Callable[[str, int, int], list[dict[str, str]]] | None = None

    # ------------------------------------------------------------------ plumbing
    def epoch(self) -> int:
        return self.server_epoch_override or int(self._now().timestamp()) + self.time_offset

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def client(self, settings: ExchangeSettings | None = None) -> CoinbasePublicClient:
        return CoinbasePublicClient(
            settings or ExchangeSettings(),
            transport=self.transport(),
            limiter=RateLimiter(10_000),
        )

    def json(self, body: Any, status: int = 200) -> httpx.Response:
        return httpx.Response(
            status,
            headers={"content-type": "application/json; charset=utf-8"},
            content=json.dumps(body).encode(),
        )

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        path = request.url.path.removeprefix("/api/v3/brokerage")
        for suffix, status in self.errors.items():
            if path.endswith(suffix):
                return httpx.Response(
                    status, headers={"content-type": "application/json"}, content=b"{}"
                )
        for suffix, body in self.raw.items():
            if path.endswith(suffix):
                return httpx.Response(
                    200, headers={"content-type": "application/json"}, content=body
                )
        epoch = self.epoch()
        if path == "/time":
            return self.json(
                {"iso": "x", "epochSeconds": str(epoch), "epochMillis": str(epoch * 1000)}
            )
        if path == "/market/products":
            return self.json(
                {"products": list(self.products.values()), "num_products": len(self.products)}
            )
        if path == "/market/product_book":
            pid = request.url.params["product_id"]
            if pid in self.book_overrides:
                return self.json(self.book_overrides[pid])
            return self.json(
                book_json(datetime.fromtimestamp(epoch, UTC), mid=self.mids.get(pid, "100"))
            )
        if path.startswith("/market/products/") and path.endswith("/candles"):
            pid = path.split("/")[3]
            price = self.mids.get(pid, "100")
            if request.url.params["granularity"] == "FIVE_MINUTE" and self.range_source is not None:
                start = int(request.url.params["start"])
                end = int(request.url.params["end"])
                return self.json({"candles": list(reversed(self.range_source(pid, start, end)))})
            if request.url.params["granularity"] == "ONE_DAY":
                candles = daily_candles(epoch, price=price)[self.drop_daily :]
            else:
                candles = intraday_candles(epoch, price=price)
                if self.intraday_gap:  # a hole inside the quality window, recent candles intact
                    candles = candles[:100] + candles[100 + self.intraday_gap :]
            return self.json({"candles": list(reversed(candles))})  # newest first, like the API
        if path.startswith("/market/products/"):
            pid = path.split("/")[3]
            if pid in self.products:
                return self.json(self.products[pid])
            return httpx.Response(404, headers={"content-type": "application/json"}, content=b"{}")
        return httpx.Response(404, content=b"{}")


__all__ = [
    "BASE_URL",
    "FakeCoinbase",
    "book_json",
    "daily_candles",
    "intraday_candles",
    "product_json",
]
