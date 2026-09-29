"""Client-side token bucket shared by the exchange clients."""

from __future__ import annotations

import time
from collections.abc import Callable


class RateLimiter:
    """Token bucket on a monotonic clock; blocks (sleeps) rather than exceeding the rate."""

    def __init__(
        self,
        per_second: int,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._rate = float(per_second)
        self._capacity = float(per_second)
        self._tokens = float(per_second)
        self._monotonic, self._sleep = monotonic, sleep
        self._last = monotonic()

    def acquire(self) -> None:
        while True:
            now = self._monotonic()
            self._tokens = min(self._capacity, self._tokens + (now - self._last) * self._rate)
            self._last = now
            if self._tokens >= 1.0:
                self._tokens -= 1.0
                return
            self._sleep((1.0 - self._tokens) / self._rate)
