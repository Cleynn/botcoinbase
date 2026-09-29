"""Public OHLCV importer. Dry-run by default: nothing is written unless `commit=True`.

Guarantees:
- Only read-only GETs of public market data, through the fixed public client. Bounded retry: at most
  `data.max_attempts` attempts per request, only for transient failures, with a fixed exponential
  backoff (no jitter, no unbounded loop).
- Every fetched span is validated (`app.market.candles`). Candles are never invented, filled or
  altered. Bad candles are excluded and recorded as data-quality events; gaps stay gaps.
- Existing stored candles are never overwritten: an identical repeat is counted, a different repeat
  is a recorded conflict and the stored value stands.
- The cursor moves forward only over a contiguous, successfully fetched and processed span.
- Product metadata is refreshed first (and its snapshot recorded on commit); a product that is not
  tradable is not imported.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import UUID

from app.adapters import coinbase_parse as parse
from app.adapters.coinbase_public import (
    MAX_CANDLES_PER_REQUEST,
    CoinbasePublicClient,
    PublicClientError,
)
from app.auth.audit import HOST_CLI_ACTOR, AuditWriter
from app.config import Settings
from app.domain.enums import AuditEventType, AuditResult
from app.domain.models import Clock
from app.market import candles as cd
from app.pairs import policy as pair_policy
from app.pairs.runner import HOST_ACTOR, RunnerError
from app.storage.database import Storage

GRANULARITY = "FIVE_MINUTE"
STEP = cd.GRANULARITY_SECONDS[GRANULARITY]
CHUNK_SECONDS = MAX_CANDLES_PER_REQUEST * STEP
MAX_DAYS = 365


@dataclass(frozen=True)
class ImportResult:
    product_id: str
    mode: str  # DRY_RUN | COMMIT
    status: str  # COMPLETE | PARTIAL | FAILED | NOTHING_TO_DO
    window_start: int
    window_end: int
    requests: int = 0
    retries: int = 0
    fetched: int = 0
    to_insert: int = 0  # what a commit would (or did) insert
    inserted: int = 0  # 0 in a dry run
    duplicates: int = 0
    conflicts: int = 0
    malformed: int = 0
    gaps: int = 0
    missing: int = 0
    cursor_before: int | None = None
    cursor_after: int | None = None
    run_id: int | None = None
    events: tuple[cd.QualityEvent, ...] = field(default_factory=tuple)


class Importer:
    def __init__(
        self,
        *,
        storage: Storage,
        clock: Clock,
        settings: Settings,
        client: CoinbasePublicClient,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._storage, self._clock, self._client = storage, clock, client
        self._data = settings.data
        self._audit = AuditWriter(clock)
        self._sleep = sleep
        self._requests = 0
        self._retries = 0

    # ------------------------------------------------------------------ bounded safe-read retry
    def _get(self, call: Callable[[], bytes]) -> bytes:
        attempt = 0
        while True:
            attempt += 1
            self._requests += 1
            try:
                return call()
            except PublicClientError as exc:
                if not exc.retryable or attempt >= self._data.max_attempts:
                    raise
                self._retries += 1
                self._sleep(float(self._data.backoff_seconds * (2 ** (attempt - 1))))

    # ------------------------------------------------------------------ the run
    def run(
        self, product_id: str, *, commit: bool = False, days: int | None = None
    ) -> ImportResult:
        self._requests = self._retries = 0
        days = self._data.history_days if days is None else days
        if not 1 <= days <= MAX_DAYS:
            raise RunnerError("BAD_DAYS")
        started = self._clock.now()
        with self._storage.tx() as repos:
            product = repos.products.get_by_product_id(product_id)
            if product is None:
                raise RunnerError("UNKNOWN_PRODUCT")
            cursor = repos.market.cursor(product.id, GRANULARITY)
        try:
            server = parse.parse_server_time(self._get(self._client.server_time))
            fresh = parse.parse_product_response(
                self._get(lambda: self._client.get_product(product_id))
            )
        except (PublicClientError, parse.ParseError) as exc:
            raise RunnerError(f"SETUP_{getattr(exc, 'code', 'ERROR')}") from exc
        if fresh.product_id != product_id or not pair_policy.assess_product(fresh).ok:
            raise RunnerError("PRODUCT_NOT_OK")
        offset_ms = int((started - server.as_datetime()).total_seconds() * 1000)

        end = cd.last_closed_end(server.epoch_seconds, STEP)
        start = cursor if cursor is not None else (end - days * 86400) // STEP * STEP
        mode = "COMMIT" if commit else "DRY_RUN"
        if start >= end:
            return ImportResult(
                product_id,
                mode,
                "NOTHING_TO_DO",
                start,
                max(end, start),
                cursor_before=cursor,
                cursor_after=cursor,
            )

        kept: list[cd.Candle] = []
        events: list[cd.QualityEvent] = []
        fetched = malformed = 0
        good_end = start
        failed_at: int | None = None
        for a in range(start, end, CHUNK_SECONDS):
            b = min(a + CHUNK_SECONDS, end)
            try:
                received = parse.parse_candles(self._get(self._chunk_call(product_id, a, b)))
            except (PublicClientError, parse.ParseError) as exc:
                events.append(
                    cd.QualityEvent(
                        cd.FETCH_ERROR, a, b, 1, str(getattr(exc, "code", "ERROR"))[:60]
                    )
                )
                failed_at = a
                break
            report = cd.validate_series(
                received,
                granularity=STEP,
                window_start=a,
                window_end=b,
                server_epoch=server.epoch_seconds,
            )
            fetched += len(received.candles)
            malformed += report.count(cd.MALFORMED)
            kept.extend(report.candles)
            events.extend(
                e for e in report.events if e.code != cd.GAP
            )  # gaps are computed globally
            good_end = b
        status = "COMPLETE" if failed_at is None else ("PARTIAL" if good_end > start else "FAILED")

        gaps = cd.find_gaps(kept, STEP, start, good_end) if good_end > start else []
        events.extend(
            cd.QualityEvent(cd.GAP, a, b, (b - a) // STEP, "no candle for these intervals")
            for a, b in gaps
        )
        missing = sum((b - a) // STEP for a, b in gaps)

        if not commit:
            existing = self._read_existing(product.id, start, good_end)
            fresh_candles, dup, conflicts = _classify(kept, existing)
            events.extend(_conflict_events(conflicts))
            return ImportResult(
                product_id,
                mode,
                status,
                start,
                end,
                self._requests,
                self._retries,
                fetched,
                len(fresh_candles),
                0,
                dup,
                len(conflicts),
                malformed,
                len(gaps),
                missing,
                cursor,
                cursor,
                None,
                tuple(_ordered(events)),
            )

        with self._storage.tx() as repos:
            repos.market.lock_product(product.id)
            repos.products.record_seen(fresh, rank=None, now=self._clock.now(), offset_ms=offset_ms)
            existing = {
                c.start: c
                for c in repos.market.candles(
                    product.id, GRANULARITY, start, max(good_end, start + STEP)
                )
            }
            fresh_candles, dup, conflicts = _classify(kept, existing)
            events.extend(_conflict_events(conflicts))
            events = _ordered(events)
            counts = {
                "requests": self._requests,
                "retries": self._retries,
                "fetched": fetched,
                "inserted": len(fresh_candles),
                "duplicates": dup,
                "conflicts": len(conflicts),
                "malformed": malformed,
                "gaps": len(gaps),
                "missing": missing,
            }
            now = self._clock.now()
            run_id = repos.market.insert_run(
                product_uuid=product.id,
                granularity=GRANULARITY,
                window_start=start,
                window_end=max(end, start + STEP),
                status=status,
                counts=counts,
                offset_ms=offset_ms,
                started_at=started,
                finished_at=now,
            )
            repos.market.insert_candles(product.id, GRANULARITY, run_id, fresh_candles)
            repos.market.insert_conflicts(
                product.id, GRANULARITY, run_id, [(c[0], c[1], c[2]) for c in conflicts]
            )
            repos.market.insert_events(run_id, events)
            cursor_after = cursor
            if status != "FAILED" and good_end > (cursor or 0):
                repos.market.advance_cursor(product.id, GRANULARITY, good_end, run_id, now)
                cursor_after = good_end
            self._audit.record(
                repos,
                AuditEventType.MARKET_INGESTED,
                AuditResult.SUCCESS if status == "COMPLETE" else AuditResult.FAILURE,
                actor=HOST_CLI_ACTOR,
                target_type="product",
                target_id=product_id,
                reason=status,
                client_tag=HOST_ACTOR.client_tag,
                request_id=HOST_ACTOR.request_id,
                detail={
                    "run": run_id,
                    "inserted": len(fresh_candles),
                    "duplicates": dup,
                    "conflicts": len(conflicts),
                    "gaps": len(gaps),
                    "missing": missing,
                    "malformed": malformed,
                    "requests": self._requests,
                    "retries": self._retries,
                },
            )
        return ImportResult(
            product_id,
            mode,
            status,
            start,
            end,
            self._requests,
            self._retries,
            fetched,
            len(fresh_candles),
            len(fresh_candles),
            dup,
            len(conflicts),
            malformed,
            len(gaps),
            missing,
            cursor,
            cursor_after,
            run_id,
            tuple(events),
        )

    def _chunk_call(self, product_id: str, a: int, b: int) -> Callable[[], bytes]:
        def call() -> bytes:
            return self._client.get_candles(product_id, GRANULARITY, a, b)

        return call

    def _read_existing(self, product_uuid: UUID, start: int, end: int) -> dict[int, cd.Candle]:
        with self._storage.tx() as repos:
            return {
                c.start: c
                for c in repos.market.candles(
                    product_uuid, GRANULARITY, start, max(end, start + STEP)
                )
            }


def _classify(
    kept: list[cd.Candle], existing: dict[int, cd.Candle]
) -> tuple[list[cd.Candle], int, list[tuple[int, cd.Candle, cd.Candle]]]:
    new: list[cd.Candle] = []
    duplicates = 0
    conflicts: list[tuple[int, cd.Candle, cd.Candle]] = []
    for candle in kept:
        stored = existing.get(candle.start)
        if stored is None:
            new.append(candle)
        elif stored.key() == candle.key():
            duplicates += 1
        else:
            conflicts.append((candle.start, stored, candle))  # the stored value stands
    return new, duplicates, conflicts


def _conflict_events(conflicts: list[tuple[int, cd.Candle, cd.Candle]]) -> list[cd.QualityEvent]:
    return [
        cd.QualityEvent(
            cd.CONFLICT, start, start + STEP, 1, "differs from the stored candle; stored value kept"
        )
        for start, _stored, _incoming in conflicts
    ]


def _ordered(events: list[cd.QualityEvent]) -> list[cd.QualityEvent]:
    return sorted(events, key=lambda e: (e.code, e.start or 0, e.end or 0, e.count, e.detail))


def aligned(epoch: int) -> int:
    return epoch // STEP * STEP
