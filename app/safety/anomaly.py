"""Anomaly checks: pure functions that turn observed facts into fixed anomaly codes.

A TRIP anomaly opens the circuit breaker; a WARN anomaly is recorded and shown. Nothing here talks
to an exchange or a database.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final, Literal

Severity = Literal["WARN", "TRIP"]
ZERO: Final = Decimal(0)


@dataclass(frozen=True)
class Anomaly:
    code: str
    severity: Severity


CODES: Final = (
    "CANDLE_JUMP",
    "SPREAD_SPIKE",
    "TAKER_FILL_ON_POST_ONLY",
    "FILL_PRICE_WORSE_THAN_LIMIT",
    "BALANCE_MISMATCH",
    "ORDER_RATE",
    "REJECT_STORM",
    "WS_SILENT",
    "WS_SEQUENCE_GAP",
)


def candle_jump(
    prev_close: Decimal | None, close: Decimal | None, max_ratio: Decimal
) -> Anomaly | None:
    if prev_close is None or close is None or prev_close <= ZERO:
        return None
    if abs(close - prev_close) / prev_close > max_ratio:
        return Anomaly("CANDLE_JUMP", "TRIP")
    return None


def spread_spike(
    spread_bps: Decimal | None, baseline_bps: Decimal, factor: Decimal
) -> Anomaly | None:
    if spread_bps is None:
        return None
    if spread_bps > max(baseline_bps, Decimal(1)) * factor:
        return Anomaly("SPREAD_SPIKE", "WARN")
    return None


def fill_liquidity(liquidity: str, post_only: bool) -> Anomaly | None:
    """A taker fill on a post-only order cannot happen; if it does, something is wrong."""
    if post_only and liquidity == "TAKER":
        return Anomaly("TAKER_FILL_ON_POST_ONLY", "TRIP")
    return None


def fill_price(side: str, order_price: Decimal, fill_price: Decimal) -> Anomaly | None:
    """A limit order never fills at a worse price than its limit."""
    if (side == "BUY" and fill_price > order_price) or (
        side == "SELL" and fill_price < order_price
    ):
        return Anomaly("FILL_PRICE_WORSE_THAN_LIMIT", "TRIP")
    return None


def balance_mismatch(expected: Decimal, actual: Decimal) -> Anomaly | None:
    return Anomaly("BALANCE_MISMATCH", "TRIP") if expected != actual else None


def order_rate(intents_last_minute: int, limit: int) -> Anomaly | None:
    return Anomaly("ORDER_RATE", "TRIP") if intents_last_minute > limit else None


def reject_storm(streak: int, limit: int) -> Anomaly | None:
    return Anomaly("REJECT_STORM", "TRIP") if streak >= limit else None


def ws_silence(age_seconds: int | None, limit: int) -> Anomaly | None:
    if age_seconds is None or age_seconds > limit:
        return Anomaly("WS_SILENT", "WARN")
    return None
