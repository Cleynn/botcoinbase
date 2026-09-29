"""Candle validation and data-quality events. Candles are never invented, filled or repaired.

Input is what the exchange sent, already parsed to Decimal. Output is the set of candles that passed
every check plus one event per problem found. A problem never changes a value: the offending
candles are excluded and reported, and gaps are simply gaps.

Closed-candle rule (CB-5): a candle is closed only when `start + granularity <= server time - lag`.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from app.adapters.coinbase_parse import Candle as ParsedCandle
from app.adapters.coinbase_parse import CandleSet
from app.domain.money import ZERO

GRANULARITY_SECONDS: Final = {"FIVE_MINUTE": 300}
CLOSE_LAG_SECONDS: Final = 3

# Event codes (a fixed vocabulary: they become database values and metric label values).
GAP: Final = "GAP"
DUPLICATE: Final = "DUPLICATE"
CONFLICT: Final = "CONFLICT"
ORDER: Final = "ORDER"
MALFORMED: Final = "MALFORMED"
INVALID_OHLC: Final = "INVALID_OHLC"
MISALIGNED: Final = "MISALIGNED"
FUTURE: Final = "FUTURE"
OUT_OF_WINDOW: Final = "OUT_OF_WINDOW"
OPEN_CANDLE: Final = "OPEN_CANDLE"
FETCH_ERROR: Final = "FETCH_ERROR"
EVENT_CODES: Final = (
    GAP,
    DUPLICATE,
    CONFLICT,
    ORDER,
    MALFORMED,
    INVALID_OHLC,
    MISALIGNED,
    FUTURE,
    OUT_OF_WINDOW,
    OPEN_CANDLE,
    FETCH_ERROR,
)
SEVERITY: Final = {
    GAP: "WARN",
    DUPLICATE: "INFO",
    CONFLICT: "ERROR",
    ORDER: "WARN",
    MALFORMED: "ERROR",
    INVALID_OHLC: "ERROR",
    MISALIGNED: "ERROR",
    FUTURE: "ERROR",
    OUT_OF_WINDOW: "INFO",
    OPEN_CANDLE: "INFO",
    FETCH_ERROR: "ERROR",
}


@dataclass(frozen=True)
class Candle:
    """A validated candle. Times are UNIX seconds (UTC) of the interval start."""

    start: int
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def key(self) -> tuple[int, Decimal, Decimal, Decimal, Decimal, Decimal]:
        return (self.start, self.open, self.high, self.low, self.close, self.volume)


@dataclass(frozen=True)
class QualityEvent:
    code: str
    start: int | None  # first affected interval start
    end: int | None  # end (exclusive) of the affected span
    count: int
    detail: str = ""  # fixed text only, never exchange content

    @property
    def severity(self) -> str:
        return SEVERITY[self.code]


@dataclass(frozen=True)
class QualityStats:
    expected: int  # closed intervals in the effective window
    present: int
    missing: int
    gap_count: int
    max_gap: int  # longest run of missing intervals
    gap_ratio: Decimal


@dataclass(frozen=True)
class QualityReport:
    candles: tuple[Candle, ...]  # valid, unique, ascending, closed, inside the window
    events: tuple[QualityEvent, ...]
    stats: QualityStats
    effective_end: int  # window end clamped to the last closed boundary

    def count(self, code: str) -> int:
        return sum(e.count for e in self.events if e.code == code)


def convert(candle: ParsedCandle) -> Candle:
    return Candle(candle.start, candle.open, candle.high, candle.low, candle.close, candle.volume)


def ohlc_valid(c: Candle) -> bool:
    if min(c.open, c.high, c.low, c.close) <= ZERO or c.volume < ZERO:
        return False
    return c.low <= min(c.open, c.close) and c.high >= max(c.open, c.close) and c.low <= c.high


def last_closed_end(server_epoch: int, granularity: int) -> int:
    """End (exclusive) of the newest interval that is closed at `server_epoch`."""
    return (server_epoch - CLOSE_LAG_SECONDS) // granularity * granularity


def validate_series(
    received: CandleSet | tuple[ParsedCandle, ...],
    *,
    granularity: int,
    window_start: int,
    window_end: int,
    server_epoch: int,
) -> QualityReport:
    """Validate one fetched span. `window_*` are aligned interval boundaries, end exclusive."""
    items = received.candles if isinstance(received, CandleSet) else received
    malformed = received.malformed if isinstance(received, CandleSet) else 0
    unordered = received.unordered if isinstance(received, CandleSet) else False
    events: list[QualityEvent] = []
    if malformed:
        events.append(QualityEvent(MALFORMED, None, None, malformed, "entries could not be parsed"))
    if unordered:
        events.append(QualityEvent(ORDER, None, None, 1, "candles arrived in mixed order"))

    effective_end = min(window_end, last_closed_end(server_epoch, granularity))
    groups: dict[int, list[Candle]] = {}
    for parsed in items:
        groups.setdefault(parsed.start, []).append(convert(parsed))

    kept: list[Candle] = []
    for start in sorted(groups):
        versions = groups[start]
        if len(versions) > 1:
            if len({v.key() for v in versions}) > 1:
                events.append(
                    QualityEvent(
                        CONFLICT,
                        start,
                        start + granularity,
                        len(versions),
                        "same interval with different values; excluded",
                    )
                )
                continue
            events.append(
                QualityEvent(
                    DUPLICATE,
                    start,
                    start + granularity,
                    len(versions) - 1,
                    "identical duplicate ignored",
                )
            )
        candle = versions[0]
        if start % granularity != 0:
            events.append(
                QualityEvent(MISALIGNED, start, None, 1, "start is not on an interval boundary")
            )
        elif start > server_epoch:
            events.append(QualityEvent(FUTURE, start, None, 1, "interval starts in the future"))
        elif not ohlc_valid(candle):
            events.append(
                QualityEvent(
                    INVALID_OHLC,
                    start,
                    start + granularity,
                    1,
                    "prices or volume are not a valid candle",
                )
            )
        elif start + granularity > last_closed_end(server_epoch, granularity):
            events.append(
                QualityEvent(
                    OPEN_CANDLE, start, start + granularity, 1, "interval is not closed yet"
                )
            )
        elif start < window_start or start + granularity > window_end:
            events.append(
                QualityEvent(
                    OUT_OF_WINDOW, start, start + granularity, 1, "outside the requested window"
                )
            )
        else:
            kept.append(candle)

    gaps = find_gaps(kept, granularity, window_start, effective_end)
    events.extend(
        QualityEvent(GAP, a, b, (b - a) // granularity, "no candle for these intervals")
        for a, b in gaps
    )
    expected = max((effective_end - window_start) // granularity, 0)
    missing = sum((b - a) // granularity for a, b in gaps)
    stats = QualityStats(
        expected=expected,
        present=len(kept),
        missing=missing,
        gap_count=len(gaps),
        max_gap=max(((b - a) // granularity for a, b in gaps), default=0),
        gap_ratio=(Decimal(missing) / Decimal(expected)) if expected else ZERO,
    )
    return QualityReport(tuple(kept), tuple(events), stats, effective_end)


def find_gaps(
    candles: list[Candle], granularity: int, start: int, end: int
) -> list[tuple[int, int]]:
    """Spans (start, end) of missing intervals, including a leading and a trailing gap."""
    gaps: list[tuple[int, int]] = []
    cursor = start
    for candle in candles:
        if candle.start > cursor:
            gaps.append((cursor, candle.start))
        cursor = candle.start + granularity
    if cursor < end:
        gaps.append((cursor, end))
    return gaps


def series_gaps(candles: tuple[Candle, ...], granularity: int) -> list[tuple[int, int]]:
    """Internal gaps of an already validated series (used by the backtest and the scorer)."""
    out: list[tuple[int, int]] = []
    for prev, nxt in zip(candles, candles[1:], strict=False):
        if nxt.start - prev.start > granularity:
            out.append((prev.start + granularity, nxt.start))
    return out
