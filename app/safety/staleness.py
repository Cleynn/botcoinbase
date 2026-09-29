"""Freshness classification for market data, product metadata, books and reconciliation.

Time comes from an injected clock (exchange server time where available), never from wall-clock
guesses. A missing timestamp is UNKNOWN and blocks exactly like STALE.
"""

from __future__ import annotations

from datetime import datetime
from typing import Final, Literal

Freshness = Literal["FRESH", "STALE", "UNKNOWN"]
CANDLE_SECONDS: Final = 300


def classify(age_seconds: int | None, limit_seconds: int) -> Freshness:
    if age_seconds is None or age_seconds < 0:  # a timestamp from the future is unknown, not fresh
        return "UNKNOWN"
    return "FRESH" if age_seconds <= limit_seconds else "STALE"


def age_seconds(now: datetime, then: datetime | None) -> int | None:
    if then is None:
        return None
    return int((now - then).total_seconds())


def candle_data_age(now_epoch: int, newest_candle_start: int | None) -> int | None:
    """Age of the newest CLOSED candle: how long since it ended."""
    if newest_candle_start is None:
        return None
    return now_epoch - (newest_candle_start + CANDLE_SECONDS)
