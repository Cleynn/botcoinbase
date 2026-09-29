"""Circuit breaker trip policy. Pure: signals in, at most one trip reason out.

The breaker OPENs on a trip and stays open through a cooldown; it closes only inside the ADMIN
RESUME chain after a current successful reconciliation (enforced by the database, not here).
The bot state becomes PAUSED whenever the breaker opens. Nothing here sells anything.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Final

from app.config import SafetySettings
from app.safety.anomaly import Anomaly

TRIP_REASONS: Final[dict[str, str]] = {
    "API_FAILURES": "too many exchange API failures",
    "RECONCILIATION_FAILURES": "repeated reconciliation failures",
    "UNKNOWN_ORDER_FOUND": "an unexplained order exists on the exchange",
    "BALANCE_MISMATCH": "the exchange balance differs from the ledger",
    "TAKER_FILL_ON_POST_ONLY": "a taker fill happened on a post-only order",
    "FILL_PRICE_WORSE_THAN_LIMIT": "an order filled at a price worse than its limit",
    "CANDLE_JUMP": "an abnormal price jump was observed",
    "ORDER_RATE": "orders were proposed faster than the limit",
    "REJECT_STORM": "the exchange keeps rejecting orders",
    "LOSS_LIMIT": "the daily loss limit was reached",
    "DRAWDOWN_LIMIT": "the drawdown limit was reached",
}
# fixed priority: the first matching reason is recorded
_PRIORITY: Final = tuple(TRIP_REASONS)


@dataclass(frozen=True)
class BreakerSignals:
    recent_api_failures: int = 0
    consecutive_reconcile_failures: int = 0
    unknown_order_found: bool = False
    loss_limit_hit: bool = False
    drawdown_hit: bool = False
    anomalies: tuple[Anomaly, ...] = field(default_factory=tuple)


def evaluate(signals: BreakerSignals, limits: SafetySettings) -> str | None:
    """The reason to open the breaker, or None."""
    hit: set[str] = set()
    if signals.recent_api_failures >= limits.api_failure_threshold:
        hit.add("API_FAILURES")
    if signals.consecutive_reconcile_failures >= 2:
        hit.add("RECONCILIATION_FAILURES")
    if signals.unknown_order_found:
        hit.add("UNKNOWN_ORDER_FOUND")
    if signals.loss_limit_hit:
        hit.add("LOSS_LIMIT")
    if signals.drawdown_hit:
        hit.add("DRAWDOWN_LIMIT")
    for anomaly in signals.anomalies:
        if anomaly.severity == "TRIP":
            hit.add(anomaly.code if anomaly.code in TRIP_REASONS else "BALANCE_MISMATCH")
    for reason in _PRIORITY:
        if reason in hit:
            return reason
    return None
