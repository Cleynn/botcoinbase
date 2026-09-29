"""The read side of the exchange boundary: a protocol plus wrappers that add retries and a record
of every call's outcome. Reads only: there is nothing here that can create or cancel an order."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime
from typing import Protocol, TypeVar

from app.exchange.errors import ExchangeError
from app.exchange.models import Balance, ExchangeFill, ExchangeOrder, KeyPermissions
from app.safety.retry import RetryPolicy

T = TypeVar("T")


class ExchangeReader(Protocol):
    venue: str

    def list_accounts(self) -> tuple[Balance, ...]: ...

    def list_orders(self, start: datetime, end: datetime) -> tuple[ExchangeOrder, ...]: ...

    def list_fills(self, start: datetime, end: datetime) -> tuple[ExchangeFill, ...]: ...

    def get_order(self, order_id: str) -> ExchangeOrder | None: ...

    def key_permissions(self) -> KeyPermissions: ...


class RetryingReader:
    """Repeats a read on transient errors (bounded, with backoff). Never used for submits."""

    def __init__(
        self,
        inner: ExchangeReader,
        policy: RetryPolicy,
        *,
        sleep: Callable[[float], None] = time.sleep,
        rng: Callable[[], float] | None = None,
    ) -> None:
        self._inner, self._policy, self._sleep = inner, policy, sleep
        self._rng = rng
        self.venue = inner.venue

    def _run(self, call: Callable[[], T]) -> T:
        if self._rng is None:
            return self._policy.run_read(call, sleep=self._sleep)
        return self._policy.run_read(call, sleep=self._sleep, rng=self._rng)

    def list_accounts(self) -> tuple[Balance, ...]:
        return self._run(self._inner.list_accounts)

    def list_orders(self, start: datetime, end: datetime) -> tuple[ExchangeOrder, ...]:
        return self._run(lambda: self._inner.list_orders(start, end))

    def list_fills(self, start: datetime, end: datetime) -> tuple[ExchangeFill, ...]:
        return self._run(lambda: self._inner.list_fills(start, end))

    def get_order(self, order_id: str) -> ExchangeOrder | None:
        return self._run(lambda: self._inner.get_order(order_id))

    def key_permissions(self) -> KeyPermissions:
        return self._run(self._inner.key_permissions)


Sink = Callable[[str, bool, str | None], None]  # (operation, ok, failure code)


class RecordingReader:
    """Reports the outcome of every read (operation, ok, fixed failure code) to a sink so API
    failures can feed the risk engine and the circuit breaker. Parse failures count as failures."""

    def __init__(self, inner: ExchangeReader, sink: Sink) -> None:
        self._inner, self._sink = inner, sink
        self.venue = inner.venue

    def _run(self, op: str, call: Callable[[], T]) -> T:
        try:
            result = call()
        except ExchangeError as exc:
            self._sink(op, False, exc.code)
            raise
        except Exception as exc:  # parser and programming errors are failures too
            self._sink(op, False, "UNEXPECTED_RESPONSE")
            raise ExchangeError("UNEXPECTED_RESPONSE") from exc
        self._sink(op, True, None)
        return result

    def list_accounts(self) -> tuple[Balance, ...]:
        return self._run("list_accounts", self._inner.list_accounts)

    def list_orders(self, start: datetime, end: datetime) -> tuple[ExchangeOrder, ...]:
        return self._run("list_orders", lambda: self._inner.list_orders(start, end))

    def list_fills(self, start: datetime, end: datetime) -> tuple[ExchangeFill, ...]:
        return self._run("list_fills", lambda: self._inner.list_fills(start, end))

    def get_order(self, order_id: str) -> ExchangeOrder | None:
        return self._run("get_order", lambda: self._inner.get_order(order_id))

    def key_permissions(self) -> KeyPermissions:
        return self._run("key_permissions", self._inner.key_permissions)
