"""Harness for market-data integration tests: a SYNTHETIC five-minute candle source.

Nothing here is recorded from Coinbase (open item AS-C1). Prices come from a deterministic
Decimal shape (a choppy range) keyed on the absolute candle index, so every span the importer asks
for is consistent with every other span.
"""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

STEP = 300


def _price(i: int, base: Decimal) -> Decimal:
    x = Decimal(i % 1152) / 1152
    tri = 4 * abs(x - Decimal("0.5")) - 1
    zig = (Decimal(i % 6) - Decimal("2.5")) / Decimal("2.5")
    return base * (1 + Decimal("0.04") * tri + Decimal("0.004") * zig)


class Source:
    """Serves raw candle dicts for [start, end); tweak the knobs to inject faults."""

    def __init__(self, base: str = "100") -> None:
        self.base = Decimal(base)
        self.missing: set[int] = set()  # candle starts that are simply absent
        self.overrides: dict[int, dict[str, str]] = {}  # start -> replacement fields
        self.duplicates: set[int] = set()  # candle starts returned twice
        self.calls: list[tuple[str, int, int]] = []

    def __call__(self, product_id: str, start: int, end: int) -> list[dict[str, str]]:
        self.calls.append((product_id, start, end))
        first = -(-start // STEP) * STEP
        candles = [self._candle(t) for t in range(first, end, STEP)]
        out: list[dict[str, str]] = []
        for row in candles:
            start_ = int(row["start"])
            if start_ in self.missing:
                continue
            row.update(self.overrides.get(start_, {}))
            out.append(row)
            if start_ in self.duplicates:
                out.append(dict(row))
        return out

    def _candle(self, t: int) -> dict[str, str]:
        q = Decimal("0.01")
        prev = _price(t // STEP - 1, self.base).quantize(q)
        close = _price(t // STEP, self.base).quantize(q)
        high = max(prev, close) * Decimal("1.0005")
        low = min(prev, close) * Decimal("0.9995")
        return {
            "start": str(t),
            "low": format(min(low.quantize(q), prev, close), "f"),
            "high": format(max(high.quantize(q), prev, close), "f"),
            "open": format(prev, "f"),
            "close": format(close, "f"),
            "volume": "1000",
        }


def install(coinbase: Any, source: Source | None = None) -> Source:
    source = source or Source()
    coinbase.range_source = source
    return source


def ingest_settings(settings: Any, data_dir: str, days: int = 7) -> Any:
    data = settings.data.model_copy(
        update={"data_dir": data_dir, "history_days": days, "backoff_seconds": Decimal("0")}
    )
    return settings.model_copy(update={"data": data})


Sleeper = Callable[[float], None]
