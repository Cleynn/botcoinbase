"""Discovery and validation runner. Runs from the host CLI as `td_ctl` (never in the web process).

Duties the baseline gives to `td_worker`, kept here until the worker exists: refresh product
metadata from Coinbase PUBLIC endpoints, seed the initial watchlist as PROPOSED candidates,
validate pairs that an ADMIN queued, and expire stale eligibility. It never activates a pair, never
touches an account and never places or cancels an order: the private Coinbase API does not exist
in this build.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import TypeVar
from uuid import UUID, uuid4

from app.adapters import coinbase_parse as parse
from app.adapters.coinbase_public import (
    GRANULARITY_SECONDS,
    MAX_CANDLES_PER_REQUEST,
    CoinbasePublicClient,
    PublicClientError,
)
from app.auth.audit import HOST_CLI_ACTOR, AuditWriter
from app.config import PairPolicy, Settings
from app.domain.enums import AuditEventType, AuditResult
from app.domain.models import Clock
from app.domain.pairs import ActorClass, PairAction, PairRecord, PairState, ValidationRun
from app.pairs import validation
from app.pairs.policy import QUOTE, SPOT, VENUE
from app.pairs.service import Outcome, PairService, UnavailableRuntimeGate
from app.pairs.transitions import Actor, apply_transition
from app.storage.database import Storage

logger = logging.getLogger("app")
T = TypeVar("T")

HOST_ACTOR = Actor(HOST_CLI_ACTOR, ActorClass.HOST, "host_cli", "cli")
DISCOVERY_CAP = 500  # baseline: top 500 products by volume


class RunnerError(Exception):
    """A whole run could not proceed. The message is a fixed code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class DiscoverySummary:
    seen: int
    candidates: int  # USDC spot products on the expected venue
    stored: int
    new: int
    changed: int
    capped: int


@dataclass(frozen=True)
class SeedResult:
    product_id: str
    outcome: str  # PROPOSED | ALREADY_A_CANDIDATE | NOT_DISCOVERED | CAP_REACHED
    pair_id: UUID | None = None


@dataclass(frozen=True)
class ValidationReport:
    pair_id: UUID
    product_id: str
    result: str  # PAPER_ELIGIBLE | RESEARCH_ONLY | SKIPPED
    outcome: str  # PASS | FAIL | INCONCLUSIVE | -
    failed: tuple[str, ...] = ()
    inconclusive: tuple[str, ...] = ()


class PairRunner:
    def __init__(
        self,
        *,
        storage: Storage,
        clock: Clock,
        settings: Settings,
        client: CoinbasePublicClient,
    ) -> None:
        self._storage, self._clock = storage, clock
        self._policy: PairPolicy = settings.pair_policy
        self._client = client
        self._audit = AuditWriter(clock)
        # The host CLI only proposes and queues; activation needs a real bot state (not built).
        self._service = PairService(
            storage=storage,
            clock=clock,
            policy=self._policy,
            audit=self._audit,
            gate=UnavailableRuntimeGate(settings.mode),
            consume_reauth=lambda _repos, _ctx: False,
            reauth_active=lambda _ctx: False,
        )

    # ------------------------------------------------------------------ time
    def _server_time(self) -> tuple[parse.ServerTime, int]:
        try:
            server = parse.parse_server_time(self._client.server_time())
        except (PublicClientError, parse.ParseError) as exc:
            raise RunnerError(f"TIME_{getattr(exc, 'code', 'ERROR')}") from exc
        offset_ms = int((self._clock.now() - server.as_datetime()).total_seconds() * 1000)
        return server, offset_ms

    # ------------------------------------------------------------------ discovery
    def discover(self) -> DiscoverySummary:
        """Refresh the product catalogue (USDC spot only) from the public product list."""
        _server, offset_ms = self._server_time()
        try:
            parsed = parse.parse_product_list(self._client.list_products())
        except (PublicClientError, parse.ParseError) as exc:
            raise RunnerError(f"PRODUCTS_{getattr(exc, 'code', 'ERROR')}") from exc
        candidates = [
            listing
            for listing in parsed.listings
            if listing.metadata.product_type == SPOT
            and listing.metadata.venue == VENUE
            and listing.metadata.quote_currency == QUOTE
        ]
        candidates.sort(key=lambda listing: (-listing.volume_key, listing.metadata.product_id))
        kept = candidates[:DISCOVERY_CAP]
        now = self._clock.now()
        new = changed = 0
        with self._storage.tx() as repos:
            for rank, listing in enumerate(kept, start=1):
                _uuid, is_new, is_changed = repos.products.record_seen(
                    listing.metadata, rank=rank, now=now, offset_ms=offset_ms
                )
                new += is_new
                changed += is_changed
            summary = DiscoverySummary(
                seen=parsed.seen,
                candidates=len(candidates),
                stored=len(kept),
                new=new,
                changed=changed,
                capped=len(candidates) - len(kept),
            )
            self._audit.record(
                repos,
                AuditEventType.PRODUCT_DISCOVERED,
                AuditResult.SUCCESS,
                actor=HOST_CLI_ACTOR,
                target_type="catalogue",
                target_id="products",
                reason="DISCOVERY_RUN",
                client_tag=HOST_ACTOR.client_tag,
                request_id=HOST_ACTOR.request_id,
                detail={
                    "seen": summary.seen,
                    "candidates": summary.candidates,
                    "stored": summary.stored,
                    "new": summary.new,
                    "changed": summary.changed,
                    "capped": summary.capped,
                },
            )
        return summary

    # ------------------------------------------------------------------ seeding
    def seed_watchlist(self, *, queue_validation: bool = False) -> list[SeedResult]:
        """Propose the configured watchlist (BTC, ETH, SOL). Candidates only; nothing activates."""
        results: list[SeedResult] = []
        for product_id in self._policy.watchlist:
            with self._storage.tx() as repos:
                product = repos.products.get_by_product_id(product_id)
            if product is None:
                results.append(SeedResult(product_id, "NOT_DISCOVERED"))
                continue
            outcome: Outcome = self._service.propose(HOST_ACTOR, product.id)
            label = {
                "ok": "PROPOSED",
                "duplicate": "ALREADY_A_CANDIDATE",
                "cap_reached": "CAP_REACHED",
            }.get(outcome.kind, "REFUSED")
            results.append(SeedResult(product_id, label, outcome.pair_id))
            if queue_validation and outcome.kind == "ok" and outcome.pair_id:
                self._service.simple_action(HOST_ACTOR, outcome.pair_id, PairAction.VALIDATE, 1)
        return results

    def queue_validation(self, pair_id: UUID) -> Outcome:
        with self._storage.tx() as repos:
            pair = repos.pairs.get(pair_id)
        if pair is None:
            return Outcome("not_found")
        return self._service.simple_action(HOST_ACTOR, pair_id, PairAction.VALIDATE, pair.version)

    # ------------------------------------------------------------------ expiry and validation
    def expire_eligibility(self) -> int:
        """PAPER_ELIGIBLE -> VALIDATING when the evidence expired or the metadata changed."""
        now = self._clock.now()
        moved = 0
        with self._storage.tx() as repos:
            for pair in repos.pairs.in_states((PairState.PAPER_ELIGIBLE,)):
                locked = repos.pairs.get(pair.id, for_update=True)
                if locked is None or locked.state is not PairState.PAPER_ELIGIBLE:
                    continue
                product = repos.products.get(locked.product_uuid)
                run = (
                    repos.pairs.get_run(locked.eligible_run_id) if locked.eligible_run_id else None
                )
                if run is None or product is None:
                    reason = "EVIDENCE_MISSING"
                elif run.expires_at <= now:
                    reason = "VALIDATION_EXPIRED"
                elif run.snapshot_id != product.snapshot_id:
                    reason = "METADATA_CHANGED"
                else:
                    continue
                apply_transition(
                    repos,
                    self._audit,
                    pair=locked,
                    to_state=PairState.VALIDATING,
                    actor=HOST_ACTOR,
                    transition_no=6,
                    event=AuditEventType.PAIR_ELIGIBILITY_EXPIRED,
                    reason=reason,
                    now=now,
                )
                moved += 1
        return moved

    def validate_pending(self, *, only: UUID | None = None) -> list[ValidationReport]:
        """Validate every pair in VALIDATING (or just `only`)."""
        with self._storage.tx() as repos:
            pending = repos.pairs.in_states((PairState.VALIDATING,))
        return [self._validate_one(p) for p in pending if only is None or p.id == only]

    def _fetch(
        self,
        errors: dict[str, str],
        dataset: str,
        call: Callable[[], bytes],
        parser: Callable[[bytes], T],
    ) -> T | None:
        try:
            return parser(call())
        except (PublicClientError, parse.ParseError) as exc:
            errors[dataset] = getattr(exc, "code", "ERROR")
            return None

    def _validate_one(self, pair: PairRecord) -> ValidationReport:
        started = self._clock.now()
        errors: dict[str, str] = {}
        server = self._fetch(errors, "time", self._client.server_time, parse.parse_server_time)
        offset_ms = int((started - server.as_datetime()).total_seconds() * 1000) if server else None
        fresh = self._fetch(
            errors,
            "product",
            lambda: self._client.get_product(pair.order_product_id),
            parse.parse_product_response,
        )
        daily = intraday = book = None
        if server is not None:
            v = self._policy.validation
            day_end = (server.epoch_seconds // 86400 + 1) * 86400
            day_start = day_end - (v.min_history_days + v.max_missing_history_days + 3) * 86400
            daily = self._fetch(
                errors,
                "daily",
                lambda: self._client.get_candles(
                    pair.order_product_id, "ONE_DAY", day_start, day_end
                ),
                parse.parse_candles,
            )
            n = min(v.quality_window_candles + 6, MAX_CANDLES_PER_REQUEST)
            step = GRANULARITY_SECONDS["FIVE_MINUTE"]
            end = (server.epoch_seconds // step + 1) * step
            intraday = self._fetch(
                errors,
                "intraday",
                lambda: self._client.get_candles(
                    pair.order_product_id, "FIVE_MINUTE", end - n * step, end
                ),
                parse.parse_candles,
            )
        book = self._fetch(
            errors,
            "book",
            lambda: self._client.get_product_book(pair.order_product_id, 50),
            parse.parse_product_book,
        )

        now = self._clock.now()
        with self._storage.tx() as repos:
            locked = repos.pairs.get(pair.id, for_update=True)
            if (
                locked is None
                or locked.state is not PairState.VALIDATING
                or locked.version != pair.version
            ):
                return ValidationReport(pair.id, pair.product_id, "SKIPPED", "-")
            stored = repos.products.get(locked.product_uuid)
            if stored is None:  # pragma: no cover  (foreign key guarantees it)
                return ValidationReport(pair.id, pair.product_id, "SKIPPED", "-")
            meta = stored.metadata
            snapshot_id, verified_at = stored.snapshot_id, stored.last_verified_at
            if fresh is not None and fresh.product_id == pair.order_product_id:
                repos.products.record_seen(fresh, rank=None, now=now, offset_ms=offset_ms)
                refreshed = repos.products.get(locked.product_uuid)
                if refreshed is not None:
                    snapshot_id, verified_at = refreshed.snapshot_id, refreshed.last_verified_at
                meta = fresh  # fresh rules, including type and venue: drift must be visible
            inputs = validation.ValidationInputs(
                product=meta,
                verified_at=verified_at,
                pair_data_basis=locked.data_basis,
                now=now,
                server_time=server,
                daily=daily,
                intraday=intraday,
                book=book,
                fetch_errors=errors,
            )
            checks = validation.run_checks(inputs, self._policy)
            outcome = validation.overall_outcome(checks)
            run = ValidationRun(
                id=uuid4(),
                pair_id=locked.id,
                pair_version=locked.version,
                snapshot_id=snapshot_id,
                started_at=started,
                finished_at=now,
                outcome=outcome,
                checks=checks,
                thresholds_sha256=validation.thresholds_sha256(self._policy),
                expires_at=now + timedelta(hours=self._policy.validation.ttl_hours),
            )
            repos.pairs.insert_run(run)
            eligible = outcome == validation.PASS
            failed = tuple(c.code for c in checks if c.status == validation.FAIL)
            unknown = tuple(c.code for c in checks if c.status == validation.INCONCLUSIVE)
            apply_transition(
                repos,
                self._audit,
                pair=locked,
                to_state=PairState.PAPER_ELIGIBLE if eligible else PairState.RESEARCH_ONLY,
                actor=HOST_ACTOR,
                transition_no=4 if eligible else 3,
                event=AuditEventType.PAIR_PAPER_ELIGIBLE
                if eligible
                else AuditEventType.PAIR_RESEARCH_ONLY,
                reason=f"VALIDATION_{outcome}",
                now=now,
                detail={
                    "outcome": outcome,
                    "run": str(run.id),
                    "failed": ",".join(failed),
                    "inconclusive": ",".join(unknown),
                },
                eligible_run_id=run.id if eligible else None,
            )
        return ValidationReport(
            pair.id,
            pair.product_id,
            "PAPER_ELIGIBLE" if eligible else "RESEARCH_ONLY",
            outcome,
            failed,
            unknown,
        )


__all__ = ["DiscoverySummary", "PairRunner", "RunnerError", "SeedResult", "ValidationReport"]
