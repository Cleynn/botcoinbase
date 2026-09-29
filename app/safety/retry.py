"""Retry policy. Reads may be repeated; an order-creating call may never be repeated blindly.

Only `RetryPolicy.run_read` exists. There is deliberately no equivalent for submit: an ambiguous
submit (timeout, connection loss, server error) leaves the attempt UNKNOWN and the executor must
reconcile before any further action (safety invariant 5). Backoff is exponential with jitter
and capped; the sleep and random source are injected so tests never wait.
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Final, TypeVar

from app.config import SafetySettings
from app.exchange.errors import ExchangeError

T = TypeVar("T")
_HALF: Final = Decimal("0.5")


@dataclass(frozen=True)
class RetryPolicy:
    max_attempts: int
    base_seconds: Decimal
    cap_seconds: Decimal

    @classmethod
    def from_settings(cls, s: SafetySettings) -> RetryPolicy:
        return cls(s.retry_max_attempts, s.retry_base_seconds, s.retry_cap_seconds)

    def delay(self, attempt: int, rng: Callable[[], float]) -> float:
        """Seconds to wait after failed attempt `attempt` (1-based)."""
        ceiling = min(self.cap_seconds, self.base_seconds * (2 ** (attempt - 1)))
        return float(ceiling * (_HALF + Decimal(str(rng())) * _HALF))

    def run_read(
        self,
        call: Callable[[], T],
        *,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] = random.random,  # noqa: S311  (jitter, not security)
    ) -> T:
        """Repeat a READ on transient errors only. Non-transient errors propagate at once."""
        last: ExchangeError | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                return call()
            except ExchangeError as exc:
                if not exc.transient:
                    raise
                last = exc
                if attempt < self.max_attempts:
                    sleep(self.delay(attempt, rng))
        if last is None:  # pragma: no cover  (max_attempts >= 1 guarantees one attempt)
            raise ExchangeError("NO_ATTEMPT")
        raise last
