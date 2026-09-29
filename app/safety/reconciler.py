"""REST reconciliation: the authority on order and fill state. WebSocket hints are cross-checked
here and never applied.

One run reads orders, fills and balances from the exchange boundary (reads only), compares them with
the bot's own records, adopts or flags every difference, and stores one immutable run row with its
findings. The run is OK only when nothing differs. Anything unexplained blocks (fail closed):

* an exchange order the bot did not create (UNKNOWN_ORDER; foreign orders are never touched);
* a local order missing on the exchange, an unmapped status, a status that does not fit;
* a fill with no attempt, fills that do not add up to the order, a taker fill or a fill worse than
  the limit on a post-only order;
* an exchange balance that differs from baseline plus recorded fills;
* an order that appears after it was declared absent; two exchange orders with one client id;
* a hint from the feed that contradicts REST (REST wins, the hint changes nothing).

Reads that fail (after the retry wrapper) produce a FAILED run, never an OK one. An UNKNOWN attempt
whose order is not on the exchange is not a discrepancy; it is resolved to ABSENT only by the
absence proof (two OK runs, each started after the wait window, none of which ever saw the client
id), which the database also enforces.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID, uuid4

from app.adapters.coinbase_parse import ParseError
from app.auth.audit import HOST_CLI_ACTOR, AuditWriter
from app.config import Settings
from app.domain.enums import AuditEventType as Evt
from app.domain.enums import AuditResult as Res
from app.domain.models import Clock
from app.exchange.errors import ExchangeError
from app.exchange.models import Balance, ExchangeFill, ExchangeOrder
from app.exchange.reader import ExchangeReader
from app.pairs.runner import HOST_ACTOR
from app.safety import anomaly, ledger
from app.safety.context import expected_state
from app.storage.database import Storage
from app.storage.repositories import Repos
from app.storage.safety_repositories import AttemptRow, FillRow, FindingRow, IntentRow

logger = logging.getLogger("app")
LOOKBACK: Final = timedelta(hours=24)
MARGIN: Final = timedelta(minutes=5)
TERMINAL_STATES: Final = ("FILLED", "CANCELLED", "EXPIRED", "REJECTED", "ABSENT")
_ATTEMPT_OF: Final = {
    "WORKING": "WORKING",
    "CANCEL_REQUESTED": "CANCEL_REQUESTED",
    "FILLED": "FILLED",
    "CANCELLED": "CANCELLED",
    "EXPIRED": "EXPIRED",
    "FAILED": "REJECTED",
}
# mirrors the database transition map (the database is the authority)
_ALLOWED: Final = {
    "AUTHORIZED": {"SUBMITTING", "REJECTED"},
    "SUBMITTING": {"WORKING", "REJECTED", "UNKNOWN"},
    "WORKING": {"CANCEL_REQUESTED", "FILLED", "CANCELLED", "EXPIRED", "UNKNOWN"},
    "CANCEL_REQUESTED": {"CANCELLED", "FILLED", "WORKING", "EXPIRED", "UNKNOWN"},
    "UNKNOWN": {"WORKING", "FILLED", "CANCELLED", "EXPIRED", "REJECTED", "ABSENT"},
}
_BLOCKING_CODES: Final = frozenset(
    {
        "UNKNOWN_ORDER", "MISSING_ORDER", "STATUS_MISMATCH", "FILL_MISMATCH", "UNKNOWN_FILL",
        "BALANCE_MISMATCH", "DUPLICATE_CLIENT_ID", "FILL_ANOMALY", "ORDER_MISMATCH",
        "WS_REST_CONFLICT", "UNKNOWN_STATUS", "ORDER_APPEARED_AFTER_ABSENCE",
    }
)  # fmt: skip


@dataclass(frozen=True)
class RunResult:
    run_id: UUID
    outcome: str
    findings: tuple[FindingRow, ...]
    failure_code: str | None
    resolved: int = 0
    absent: int = 0

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(sorted({f.code for f in self.findings}))


def _safe(text: object, limit: int = 64) -> str | None:
    value = str(text)[:limit]
    return value if value and all(c.isalnum() or c in "._:-" for c in value) else None


def _f(code: str, subject: object = None, detail: str | None = None) -> FindingRow:
    detail = None if detail is None else "".join(c for c in detail if c.isprintable())[:200]
    return FindingRow(code, _safe(subject) if subject is not None else None, detail)


def _matches(order: ExchangeOrder, intent: IntentRow) -> bool:
    return (
        order.product_id == intent.product_id
        and order.side == intent.side
        and order.order_type == "limit_limit_gtc"
        and order.post_only is True
        and order.price == intent.price
        and order.base_qty == intent.base_qty
    )


class Reconciler:
    def __init__(
        self,
        *,
        storage: Storage,
        clock: Clock,
        settings: Settings,
        reader: ExchangeReader,
        venue: str,
    ) -> None:
        self._storage, self._clock, self._settings = storage, clock, settings
        self._reader, self._venue = reader, venue
        self._audit = AuditWriter(clock)

    # ------------------------------------------------------------------ audit
    def _record(
        self,
        repos: Repos,
        event: Evt,
        result: Res,
        target: object = None,
        reason: str | None = None,
        detail: dict[str, str | int | bool | None] | None = None,
    ) -> None:
        self._audit.record(
            repos,
            event,
            result,
            actor=HOST_CLI_ACTOR,
            target_type="reconciliation" if target is None else "order",
            target_id=None if target is None else str(target),
            reason=reason,
            client_tag=HOST_ACTOR.client_tag,
            request_id=HOST_ACTOR.request_id,
            detail=detail,
        )

    def _fail(self, run_id: UUID, trigger: str, started: datetime, code: str) -> RunResult:
        finished = self._clock.now()
        with self._storage.tx() as repos:
            repos.safety.add_run(
                run_id=run_id,
                venue=self._venue,
                trigger=trigger,
                started_at=started,
                finished_at=finished,
                outcome="FAILED",
                orders_seen=0,
                fills_seen=0,
                balances_seen=0,
                findings=[],
                failure_code=code,
            )
            self._record(
                repos,
                Evt.RECONCILIATION_FAILED,
                Res.FAILURE,
                reason=code,
                detail={"venue": self._venue, "trigger": trigger},
            )
        return RunResult(run_id, "FAILED", (), code)

    # ------------------------------------------------------------------ the run
    def run(self, trigger: str) -> RunResult:
        started = self._clock.now()
        run_id = uuid4()
        with self._storage.tx() as repos:
            baselines = repos.safety.baselines(self._venue)
            tracked = repos.safety.attempts_all(self._venue)
            previous = repos.safety.latest_run(self._venue)
            hints_from = previous.finished_at if previous else started - timedelta(minutes=10)
        if ledger.QUOTE not in baselines:
            return self._fail(run_id, trigger, started, "NO_BASELINE")
        oldest = min(
            [a.created_at for a in tracked if a.state not in TERMINAL_STATES or a.state == "ABSENT"]
            + [started - LOOKBACK]
        )
        window_start = min(oldest, started - LOOKBACK) - MARGIN
        window_end = started + timedelta(minutes=1)
        try:
            orders = self._reader.list_orders(window_start, window_end)
            fills = self._reader.list_fills(window_start, window_end)
            balances = self._reader.list_accounts()
        except ExchangeError as exc:
            return self._fail(run_id, trigger, started, exc.code)
        except ParseError:
            return self._fail(run_id, trigger, started, "UNEXPECTED_RESPONSE")
        try:
            return self._compare(run_id, trigger, started, orders, fills, balances, hints_from)
        except Exception as exc:  # noqa: BLE001  (an unexpected error must never look clean)
            logger.error("reconciliation failed internally: %s", type(exc).__name__)
            return self._fail(run_id, trigger, started, "INTERNAL_ERROR")

    def _compare(
        self,
        run_id: UUID,
        trigger: str,
        started: datetime,
        orders: tuple[ExchangeOrder, ...],
        fills: tuple[ExchangeFill, ...],
        balances: tuple[Balance, ...],
        hints_from: datetime,
    ) -> RunResult:
        now = self._clock.now()
        findings: list[FindingRow] = []
        resolved = 0
        with self._storage.tx() as repos:
            local = {str(a.client_order_id): a for a in repos.safety.attempts_all(self._venue)}
            by_client: dict[str, ExchangeOrder] = {}
            for listed in orders:
                if listed.client_order_id in by_client:
                    findings.append(_f("DUPLICATE_CLIENT_ID", listed.client_order_id))
                by_client.setdefault(listed.client_order_id, listed)
            by_order_id = {o.order_id: o for o in orders}

            # ---- local attempts against the exchange
            for client_id, attempt in local.items():
                order: ExchangeOrder | None = by_client.get(client_id)
                intent = repos.safety.intent(attempt.intent_id)
                if intent is None:  # pragma: no cover  (foreign key)
                    continue
                if attempt.state == "ABSENT":
                    if order is not None:
                        findings.append(_f("ORDER_APPEARED_AFTER_ABSENCE", client_id))
                    continue
                if attempt.state in TERMINAL_STATES:
                    continue
                if attempt.state == "AUTHORIZED":
                    if order is not None:
                        findings.append(
                            _f("ORDER_MISMATCH", client_id, "sent without a submit mark")
                        )
                    continue
                if order is None:
                    if attempt.state in ("WORKING", "CANCEL_REQUESTED"):
                        findings.append(
                            _f("MISSING_ORDER", client_id, "not listed by the exchange")
                        )
                        self._move(repos, attempt, "UNKNOWN", now, "MISSING_ORDER")
                    elif attempt.state == "SUBMITTING" and attempt.boot_id != self._current_boot(
                        repos
                    ):
                        self._move(repos, attempt, "UNKNOWN", now, "RECOVERED")
                    continue
                if not _matches(order, intent):
                    findings.append(_f("ORDER_MISMATCH", client_id, "differs from the intent"))
                    self._move(repos, attempt, "UNKNOWN", now, "ORDER_MISMATCH")
                    continue
                before = attempt.state
                outcome = self._apply_status(repos, attempt, order, now, findings)
                if outcome and before in ("UNKNOWN", "SUBMITTING"):
                    resolved += 1
                    self._record(repos, Evt.ORDER_RESOLVED, Res.SUCCESS, attempt.id, reason=outcome)

            # ---- exchange orders the bot cannot explain (foreign orders are never touched)
            for client_id, seen in by_client.items():
                if client_id not in local and seen.status in (
                    "WORKING",
                    "CANCEL_REQUESTED",
                    "UNKNOWN",
                ):
                    findings.append(_f("UNKNOWN_ORDER", client_id, "not created by the bot"))

            # ---- fills
            for fill in fills:
                self._apply_fill(repos, fill, by_order_id, local, findings)
            self._check_fill_totals(repos, by_client, local, findings)

            # ---- balances (baseline plus recorded fills, exact)
            expected = expected_state(repos, self._venue).balances
            actual = {b.currency: b.total for b in balances}
            for currency in sorted(set(expected) | set(actual)):
                want, got = expected.get(currency, Decimal(0)), actual.get(currency, Decimal(0))
                if want != got:
                    detail = f"expected {format(want, 'f')} found {format(got, 'f')}"
                    findings.append(_f("BALANCE_MISMATCH", currency, detail))

            # ---- feed hints: cross-checked, never applied
            for hint in repos.safety.hints_since(self._venue, hints_from):
                rest = by_client.get(hint.client_order_id)
                if hint.client_order_id not in local and rest is None:
                    findings.append(
                        _f("UNKNOWN_ORDER", hint.client_order_id, "seen on the feed only")
                    )
                elif rest is not None and (
                    (hint.status in ("FILLED", "CANCELLED", "EXPIRED") and rest.status == "WORKING")
                    or hint.filled_qty > rest.filled_qty
                ):
                    findings.append(_f("WS_REST_CONFLICT", hint.client_order_id))

            # ---- the run row, then absence proofs (the database counts this run)
            blocking = [f for f in findings if f.code in _BLOCKING_CODES]
            outcome = "OK" if not blocking else "MISMATCH"
            repos.safety.add_run(
                run_id=run_id,
                venue=self._venue,
                trigger=trigger,
                started_at=started,
                finished_at=self._clock.now(),
                outcome=outcome,
                orders_seen=len(orders),
                fills_seen=len(fills),
                balances_seen=len(balances),
                findings=blocking,
                failure_code=None,
            )
            absent = 0
            if outcome == "OK":
                absent = self._prove_absent(repos, local, by_client)
            self._record(
                repos,
                Evt.RECONCILIATION_OK if outcome == "OK" else Evt.RECONCILIATION_MISMATCH,
                Res.SUCCESS if outcome == "OK" else Res.FAILURE,
                detail={
                    "venue": self._venue,
                    "trigger": trigger,
                    "findings": len(blocking),
                    "orders": len(orders),
                },
            )
        return RunResult(run_id, outcome, tuple(blocking), None, resolved, absent)

    # ------------------------------------------------------------------ pieces
    @staticmethod
    def _current_boot(repos: Repos) -> str | None:
        return repos.safety.control().boot_id

    def _move(
        self, repos: Repos, attempt: AttemptRow, to: str, now: datetime, code: str | None = None
    ) -> bool:
        if to not in _ALLOWED.get(attempt.state, set()):
            return False
        return repos.safety.transition(
            attempt.id,
            (attempt.state,),
            to,
            now,
            failure_code=code,
        )

    def _apply_status(
        self,
        repos: Repos,
        attempt: AttemptRow,
        order: ExchangeOrder,
        now: datetime,
        findings: list[FindingRow],
    ) -> str | None:
        """Fold an exchange status into the attempt. Returns the resulting state if it changed."""
        target = _ATTEMPT_OF.get(order.status)
        if target is None:
            findings.append(_f("UNKNOWN_STATUS", order.client_order_id))
            self._move(repos, attempt, "UNKNOWN", now, "UNKNOWN_STATUS")
            return None
        state = attempt.state
        if (
            state == "SUBMITTING"
        ):  # never straight to a final state: the mark says it was unresolved
            self._move(repos, attempt, "UNKNOWN", now)
            state = "UNKNOWN"
        if target == "REJECTED" and state == "WORKING":
            findings.append(_f("STATUS_MISMATCH", order.client_order_id, "failed after acceptance"))
            self._move(repos, attempt, "UNKNOWN", now, "STATUS_MISMATCH")
            return None
        if state == target:
            repos.safety.transition(
                attempt.id,
                (state,),
                state,
                now,
                exchange_order_id=order.order_id,
            )
            return None
        if target not in _ALLOWED.get(state, set()):
            findings.append(_f("STATUS_MISMATCH", order.client_order_id, f"{state} to {target}"))
            return None
        code = "EXCHANGE_FAILED" if target == "REJECTED" else None
        repos.safety.transition(
            attempt.id,
            (state,),
            target,
            now,
            exchange_order_id=order.order_id,
            failure_code=code,
        )
        return target

    def _apply_fill(
        self,
        repos: Repos,
        fill: ExchangeFill,
        by_order_id: dict[str, ExchangeOrder],
        local: dict[str, AttemptRow],
        findings: list[FindingRow],
    ) -> None:
        order = by_order_id.get(fill.order_id)
        attempt = local.get(order.client_order_id) if order else None
        if attempt is None:
            attempt = repos.safety.attempt_by_exchange_id(fill.order_id)
        if attempt is None:
            findings.append(_f("UNKNOWN_FILL", fill.fill_id, "no attempt for the order"))
            return
        intent = repos.safety.intent(attempt.intent_id)
        if intent is None:  # pragma: no cover
            return
        for found in (
            anomaly.fill_liquidity(fill.liquidity, intent.post_only),
            anomaly.fill_price(intent.side, intent.price, fill.price),
        ):
            if found is not None:
                findings.append(_f("FILL_ANOMALY", fill.fill_id, found.code))
        repos.safety.add_fill(
            FillRow(
                attempt.id,
                self._venue,
                fill.fill_id,
                fill.side,
                fill.price,
                fill.size,
                fill.fee,
                fill.liquidity,
                fill.trade_time or self._clock.now(),
            )
        )

    def _check_fill_totals(
        self,
        repos: Repos,
        by_client: dict[str, ExchangeOrder],
        local: dict[str, AttemptRow],
        findings: list[FindingRow],
    ) -> None:
        now = self._clock.now()
        for client_id, attempt in local.items():
            order = by_client.get(client_id)
            if order is None or attempt.state in ("AUTHORIZED", "ABSENT"):
                continue
            recorded = sum((f.size for f in repos.safety.fills_for_attempt(attempt.id)), Decimal(0))
            if recorded != order.filled_qty:
                findings.append(
                    _f(
                        "FILL_MISMATCH",
                        client_id,
                        f"fills {format(recorded, 'f')} order {format(order.filled_qty, 'f')}",
                    )
                )
            elif recorded > attempt.filled_qty:
                fresh = repos.safety.attempt(attempt.id)
                if fresh is not None and fresh.state not in ("AUTHORIZED",):
                    repos.safety.transition(
                        attempt.id, (fresh.state,), fresh.state, now, filled_qty=recorded
                    )

    def _prove_absent(
        self, repos: Repos, local: dict[str, AttemptRow], by_client: dict[str, ExchangeOrder]
    ) -> int:
        s = self._settings.safety
        need = max(2, s.absence_min_reconciliations)
        window = timedelta(seconds=max(120, s.absence_window_seconds))
        done = 0
        for client_id, attempt in local.items():
            if (
                attempt.state != "UNKNOWN"
                or client_id in by_client
                or attempt.submitting_at is None
            ):
                continue
            if repos.safety.client_id_ever_named(client_id):
                continue  # a run once saw this id: absence is not proven
            proofs = repos.safety.ok_runs_started_after(attempt.submitting_at + window)
            if proofs >= need:
                if self._move(repos, attempt, "ABSENT", self._clock.now(), None):
                    done += 1
                    self._record(repos, Evt.ORDER_ABSENT, Res.SUCCESS, attempt.id, reason="PROVEN")
        return done
