"""The order path: strategy proposal -> immutable OrderIntent -> risk decision -> authorization ->
execution gateway -> reconciliation. Host side only; nothing on the web tier can reach it.

Rules this module enforces (and the database backs up):

* The intent is persisted, immutable, BEFORE any decision or I/O; its key is deterministic, so the
  same proposal in the same slot is the same intent (no duplicates).
* The decision is computed from fresh facts at authorization time and stored (ALLOW and BLOCK).
  An ALLOW is consumed by exactly one attempt; the database re-checks kill switch, breaker, bot
  state, recovery, reconciliation freshness and unknown attempts at that moment.
* The client order id is a deterministic UUID of (intent, attempt number) and unique.
* The SUBMITTING mark is committed BEFORE the exchange call.
* An ambiguous or unexpected submit result leaves the attempt UNKNOWN; the next thing that happens
  is reconciliation. A retry is a NEW attempt, allowed only after the earlier attempt was proven
  ABSENT (or definitively REJECTED).
* With no gateway (the state of every deployment of this build) nothing is ever submitted.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Final, Literal
from uuid import UUID, uuid4, uuid5

import psycopg

from app.auth.audit import HOST_CLI_ACTOR, AuditWriter
from app.config import Settings
from app.domain.enums import AuditEventType as Evt
from app.domain.enums import AuditResult as Res
from app.domain.models import Clock
from app.exchange.errors import ExchangeError
from app.exchange.gateway import ExecutionGateway, OrderRequest
from app.pairs.runner import HOST_ACTOR
from app.safety import risk_engine
from app.safety.context import BookFacts, RiskContextBuilder
from app.safety.types import OrderProposal
from app.storage.database import Storage
from app.storage.repositories import Repos
from app.storage.safety_repositories import DecisionRow, IntentRow

NAMESPACE: Final = UUID("6f0c2d64-5b1e-4b9e-9d3a-3a7f0f0e9c11")
DECISION_TTL: Final = timedelta(seconds=30)
Kind = Literal["blocked", "no_gateway", "submitted", "rejected", "unknown", "adopted", "invalid"]


@dataclass(frozen=True)
class PipelineResult:
    kind: Kind
    intent_id: UUID | None = None
    attempt_id: UUID | None = None
    reasons: tuple[str, ...] = ()
    state: str | None = None


def intent_key(order: OrderProposal, source: str, slot: str) -> str:
    material = "|".join(
        [
            order.venue,
            order.pair_id,
            order.product_id,
            order.side,
            format(order.price.normalize(), "f"),
            format(order.base_qty.normalize(), "f"),
            source,
            slot,
        ]
    )
    return hashlib.sha256(material.encode()).hexdigest()


def client_order_id(intent_id: UUID, attempt_no: int) -> UUID:
    return uuid5(NAMESPACE, f"{intent_id}:{attempt_no}")


def _reject_code(reason: str | None) -> str:
    text = "".join(c if c.isalpha() else "_" for c in (reason or "REJECTED").upper())[:40]
    return text if len(text) >= 3 else "REJECTED"


class OrderPipeline:
    def __init__(
        self,
        *,
        storage: Storage,
        clock: Clock,
        settings: Settings,
        gateway: ExecutionGateway | None,
        boot_id: str,
        reconcile: Callable[[str], object] | None = None,
    ) -> None:
        self._storage, self._clock, self._settings = storage, clock, settings
        self._gateway, self._boot_id = gateway, boot_id
        self._reconcile = reconcile
        self._audit = AuditWriter(clock)
        self._builder = RiskContextBuilder(settings)

    # ------------------------------------------------------------------ audit
    def _record(
        self,
        repos: Repos,
        event: Evt,
        result: Res,
        target: UUID | None,
        reason: str | None = None,
        detail: dict[str, str | int | bool | None] | None = None,
    ) -> None:
        self._audit.record(
            repos,
            event,
            result,
            actor=HOST_CLI_ACTOR,
            target_type="order",
            target_id=target,
            reason=reason,
            client_tag=HOST_ACTOR.client_tag,
            request_id=HOST_ACTOR.request_id,
            detail=detail,
        )

    # ------------------------------------------------------------------ 1. the intent
    def create_intent(self, order: OrderProposal, *, source: str, slot: str) -> IntentRow:
        """Persist the intent (idempotent on its deterministic key). Nothing else happens."""
        key = intent_key(order, source, slot)
        with self._storage.tx() as repos:
            existing = repos.safety.intent_by_key(key)
            if existing is not None:
                return existing
            row = IntentRow(
                id=uuid4(),
                intent_key=key,
                venue=order.venue,
                pair_id=UUID(order.pair_id),
                product_id=order.product_id,
                side=order.side,
                order_type=order.order_type,
                post_only=order.post_only,
                price=order.price,
                base_qty=order.base_qty,
                expected_cycle_return=order.expected_cycle_return,
                source=source,
                created_at=self._clock.now(),
            )
            repos.safety.add_intent(row)
            self._record(
                repos,
                Evt.ORDER_INTENT_CREATED,
                Res.SUCCESS,
                row.id,
                detail={"side": row.side, "venue": row.venue},
            )
            return row

    # ------------------------------------------------------ decide, authorize, send
    def process(self, intent_id: UUID, *, book: BookFacts | None) -> PipelineResult:
        """Run one attempt for an existing intent."""
        if self._gateway is None:
            return PipelineResult("no_gateway", intent_id, reasons=("NO_GATEWAY",))
        authorized = self._authorize(intent_id, book)
        if isinstance(authorized, PipelineResult):
            return authorized
        attempt_id, order = authorized
        return self._send(intent_id, attempt_id, order)

    def submit(
        self, order: OrderProposal, *, source: str, slot: str, book: BookFacts | None
    ) -> PipelineResult:
        intent = self.create_intent(order, source=source, slot=slot)
        return self.process(intent.id, book=book)

    def _authorize(
        self, intent_id: UUID, book: BookFacts | None
    ) -> PipelineResult | tuple[UUID, OrderRequest]:
        now = self._clock.now()
        with self._storage.tx() as repos:
            intent = repos.safety.intent(intent_id)
            if intent is None:
                return PipelineResult("invalid", intent_id, reasons=("INTENT_MISSING",))
            attempts = repos.safety.attempts_for_intent(intent_id)
            attempt_no = len(attempts) + 1
            client_id = client_order_id(intent_id, attempt_no)
            facts = self._builder.build(
                repos, intent, now=now, book=book, client_order_id=str(client_id)
            )
            decision = risk_engine.evaluate(_proposal(intent), facts, self._settings.safety)
            row = DecisionRow(
                id=uuid4(),
                intent_id=intent_id,
                decision="ALLOW" if decision.allowed else "BLOCK",
                reasons=decision.reasons,
                inputs_hash=decision.inputs_hash,
                decided_at=now,
                expires_at=now + DECISION_TTL,
                consumed_at=None,
            )
            repos.safety.add_decision(row)
            if not decision.allowed:
                self._record(
                    repos,
                    Evt.ORDER_RISK_BLOCKED,
                    Res.DENIED,
                    intent_id,
                    reason=decision.reasons[0],
                    detail={"reasons": ",".join(decision.reasons)[:190]},
                )
                return PipelineResult("blocked", intent_id, reasons=decision.reasons)
            self._record(repos, Evt.ORDER_RISK_ALLOWED, Res.SUCCESS, intent_id)
        # authorization is a separate transaction so a refusal by the database guard (a race with a
        # kill switch or breaker) leaves the ALLOW decision unconsumed and expiring
        try:
            with self._storage.tx() as repos:
                attempt_id = uuid4()
                repos.safety.add_attempt(
                    attempt_id=attempt_id,
                    intent_id=intent_id,
                    attempt_no=attempt_no,
                    client_order_id=client_id,
                    decision_id=row.id,
                    boot_id=self._boot_id,
                    now=self._clock.now(),
                )
                self._record(
                    repos,
                    Evt.ORDER_ATTEMPT_AUTHORIZED,
                    Res.SUCCESS,
                    attempt_id,
                    detail={"attempt_no": attempt_no},
                )
        except psycopg.errors.IntegrityConstraintViolation:
            with self._storage.tx() as repos:
                self._record(
                    repos,
                    Evt.ORDER_RISK_BLOCKED,
                    Res.DENIED,
                    intent_id,
                    reason="AUTHORIZATION_REFUSED",
                )
            return PipelineResult("blocked", intent_id, reasons=("INPUTS_UNAVAILABLE",))
        request = OrderRequest(
            client_order_id=str(client_id),
            product_id=intent.product_id,
            side="BUY" if intent.side == "BUY" else "SELL",
            price=intent.price,
            base_qty=intent.base_qty,
        )
        return attempt_id, request

    def _send(self, intent_id: UUID, attempt_id: UUID, request: OrderRequest) -> PipelineResult:
        gateway = self._gateway
        if gateway is None:  # pragma: no cover  (process() refuses earlier)
            return PipelineResult("no_gateway", intent_id, attempt_id, ("NO_GATEWAY",))
        # the SUBMITTING mark is committed BEFORE any I/O (BI-20)
        with self._storage.tx() as repos:
            if not repos.safety.mark_submitting(attempt_id, self._clock.now()):
                return PipelineResult("invalid", intent_id, attempt_id, ("NOT_AUTHORIZED",))
            self._record(repos, Evt.ORDER_SUBMITTING, Res.SUCCESS, attempt_id)
        outcome, order_id, reason = "UNKNOWN", None, None
        try:
            result = gateway.submit(request)
            outcome, order_id, reason = result.outcome, result.exchange_order_id, result.reason
            ok, code = True, None
        except ExchangeError as exc:
            ok, code = False, exc.code
        except Exception:  # noqa: BLE001  (anything unexpected after the mark is ambiguous)
            ok, code = False, "UNEXPECTED_RESPONSE"
        now = self._clock.now()
        with self._storage.tx() as repos:
            repos.safety.add_api_event(gateway.venue, "submit", ok, code, now)
            if outcome in ("ACCEPTED", "EXISTING") and order_id:
                repos.safety.transition(
                    attempt_id, ("SUBMITTING",), "WORKING", now, exchange_order_id=order_id
                )
                self._record(
                    repos,
                    Evt.ORDER_SUBMITTED,
                    Res.SUCCESS,
                    attempt_id,
                    reason="DUPLICATE_CLIENT_ID_RETURNED" if outcome == "EXISTING" else None,
                )
                kind: Kind = "adopted" if outcome == "EXISTING" else "submitted"
                state = "WORKING"
            elif outcome == "REJECTED":
                repos.safety.transition(
                    attempt_id, ("SUBMITTING",), "REJECTED", now, failure_code=_reject_code(reason)
                )
                self._record(
                    repos, Evt.ORDER_REJECTED, Res.FAILURE, attempt_id, reason=_reject_code(reason)
                )
                return PipelineResult(
                    "rejected", intent_id, attempt_id, (_reject_code(reason),), "REJECTED"
                )
            else:
                repos.safety.transition(
                    attempt_id,
                    ("SUBMITTING",),
                    "UNKNOWN",
                    now,
                    failure_code=code or "UNKNOWN_RESULT",
                )
                self._record(
                    repos,
                    Evt.ORDER_UNKNOWN,
                    Res.FAILURE,
                    attempt_id,
                    reason=code or "UNKNOWN_RESULT",
                )
                kind, state = "unknown", "UNKNOWN"
        if kind == "unknown" or kind == "adopted":
            # reconcile before anything else, including any retry (invariant 5)
            if self._reconcile is not None:
                self._reconcile("AFTER_AMBIGUITY")
            with self._storage.tx() as repos:
                row = repos.safety.attempt(attempt_id)
                state = row.state if row else state
        return PipelineResult(kind, intent_id, attempt_id, (), state)

    # ------------------------------------------------------------------ retry
    def retry(self, intent_id: UUID, *, book: BookFacts | None) -> PipelineResult:
        """A new attempt for an intent whose earlier attempt is proven absent or was rejected.
        Reconciles first; the database refuses a retry while any earlier attempt is not ABSENT or
        REJECTED."""
        if self._reconcile is not None:
            self._reconcile("AFTER_AMBIGUITY")
        with self._storage.tx() as repos:
            previous = repos.safety.attempts_for_intent(intent_id)
        if not previous or any(a.state not in ("ABSENT", "REJECTED") for a in previous):
            return PipelineResult("blocked", intent_id, reasons=("UNKNOWN_ATTEMPT",))
        return self.process(intent_id, book=book)


def _proposal(intent: IntentRow) -> OrderProposal:
    from app.safety.context import proposal_of

    return proposal_of(intent)
