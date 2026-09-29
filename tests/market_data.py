"""Deterministic synthetic candle series for tests. Decimal arithmetic only; no randomness.

These are NOT market data. They are constructed shapes (a choppy range, a trend, a crash, gaps) used
to exercise the code paths. A result on them says nothing about real markets.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal

from app.market.candles import Candle

STEP = 300
T0 = 1_780_000_200  # a multiple of 300


def _q(x: Decimal) -> Decimal:
    return x.quantize(Decimal("0.01"))


def series(
    n: int,
    price_at: Callable[[int], Decimal],
    *,
    start: int = T0,
    volume: Decimal = Decimal("1000"),
    wick: Decimal = Decimal("0.0005"),
    skip: Callable[[int], bool] = lambda i: False,
) -> tuple[Candle, ...]:
    out: list[Candle] = []
    prev = _q(price_at(0))
    for i in range(n):
        close = _q(price_at(i))
        if skip(i):
            prev = close
            continue
        hi = _q(max(prev, close) * (1 + wick))
        lo = _q(min(prev, close) * (1 - wick))
        out.append(
            Candle(
                start + i * STEP, prev, max(hi, prev, close), min(lo, prev, close), close, volume
            )
        )
        prev = close
    return tuple(out)


def choppy_range(
    n: int,
    *,
    base: Decimal = Decimal("100"),
    amp: Decimal = Decimal("0.04"),
    period: int = 1152,
    noise: Decimal = Decimal("0.004"),
    **kw: object,
) -> tuple[Candle, ...]:
    def price(i: int) -> Decimal:
        x = Decimal(i % period) / period
        tri = 4 * abs(x - Decimal("0.5")) - 1
        zig = (Decimal(i % 6) - Decimal("2.5")) / Decimal("2.5")
        return base * (1 + amp * tri + noise * zig)

    return series(n, price, **kw)  # type: ignore[arg-type]


def trend(
    n: int, *, base: Decimal = Decimal("100"), per_candle: Decimal = Decimal("0.0002"), **kw: object
) -> tuple[Candle, ...]:
    return series(n, lambda i: base * (1 + per_candle * i), **kw)  # type: ignore[arg-type]


def crash(
    n: int, *, at: int, base: Decimal = Decimal("100"), drop: Decimal = Decimal("0.3"), **kw: object
) -> tuple[Candle, ...]:
    """A choppy range that steps down by `drop` at candle `at` and stays there."""

    def price(i: int) -> Decimal:
        x = Decimal(i % 1152) / 1152
        tri = 4 * abs(x - Decimal("0.5")) - 1
        zig = (Decimal(i % 6) - Decimal("2.5")) / Decimal("2.5")
        level = base if i < at else base * (1 - drop)
        return level * (1 + Decimal("0.04") * tri + Decimal("0.004") * zig)

    return series(n, price, **kw)  # type: ignore[arg-type]
