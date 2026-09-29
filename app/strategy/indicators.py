"""Deterministic indicators over validated candles. Decimal only; no lookahead; no randomness."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal

from app.domain.money import ONE, ZERO, ratio
from app.market.candles import Candle


def sma(values: Sequence[Decimal], n: int) -> Decimal | None:
    if n < 1 or len(values) < n:
        return None
    return sum(values[-n:], ZERO) / n


def ema(values: Sequence[Decimal], n: int) -> Decimal | None:
    """Exponential moving average seeded with the SMA of the first n values."""
    if n < 1 or len(values) < n:
        return None
    alpha = Decimal(2) / (n + 1)
    value = sum(values[:n], ZERO) / n
    for x in values[n:]:
        value = alpha * x + (ONE - alpha) * value
    return value


def true_range(candle: Candle, previous_close: Decimal | None) -> Decimal:
    if previous_close is None:
        return candle.high - candle.low
    return max(
        candle.high - candle.low,
        abs(candle.high - previous_close),
        abs(candle.low - previous_close),
    )


def atr(candles: Sequence[Candle], n: int) -> Decimal | None:
    """Wilder's average true range. Across a data gap the true range uses the last known close."""
    if n < 1 or len(candles) < n + 1:
        return None
    trs = [true_range(c, p.close) for p, c in zip(candles, candles[1:], strict=False)]
    value = sum(trs[:n], ZERO) / n
    for tr in trs[n:]:
        value = (value * (n - 1) + tr) / n
    return value


def efficiency_ratio(closes: Sequence[Decimal], n: int) -> Decimal | None:
    """Kaufman efficiency ratio in [0, 1]: net move over total path. Low means ranging."""
    if n < 2 or len(closes) < n + 1:
        return None
    window = closes[-(n + 1) :]
    path = sum((abs(b - a) for a, b in zip(window, window[1:], strict=False)), ZERO)
    if path == 0:
        return ZERO
    return ratio(abs(window[-1] - window[0]), path)


def donchian(candles: Sequence[Candle], n: int) -> tuple[Decimal, Decimal] | None:
    if n < 1 or len(candles) < n:
        return None
    window = candles[-n:]
    return min(c.low for c in window), max(c.high for c in window)
