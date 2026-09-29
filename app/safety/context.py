"""Gathers the facts the risk engine decides on, from the database (host side, read only).

Every read that fails or finds nothing becomes `None`, which the engine treats as a reason to block.
The spread comes from a book observation passed in by the caller; this module fetches nothing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

from app.config import Settings
from app.domain.pairs import PairState
from app.market.ingest import GRANULARITY, STEP
from app.pairs import policy as pair_policy
from app.safety import ledger
from app.safety.live_gate import order_gate
from app.safety.risk_engine import FeeView, RiskInputs
from app.safety.staleness import age_seconds, candle_data_age
from app.safety.types import (
    CapitalView,
    EquityView,
    OrderProposal,
    ProductRules,
    ReconView,
)
from app.storage.repositories import Repos
from app.storage.safety_repositories import LIVE_ATTEMPT_STATES, AttemptRow, IntentRow

ZERO = Decimal(0)


@dataclass(frozen=True)
class BookFacts:
    spread_bps: Decimal
    age_seconds: int


def proposal_of(intent: IntentRow) -> OrderProposal:
    return OrderProposal(
        venue=intent.venue,
        pair_id=str(intent.pair_id),
        product_id=intent.product_id,
        side=intent.side,
        price=intent.price,
        base_qty=intent.base_qty,
        order_type=intent.order_type,
        post_only=intent.post_only,
        expected_cycle_return=intent.expected_cycle_return,
    )


def fee_view(settings: Settings, today: date) -> FeeView:
    fees = settings.pair_policy.fees
    if fees.operator_maker_rate is None or fees.attested_on is None:
        return FeeView("UNATTESTED", None, fees.stress_maker_rate)
    if fees.attested_on > today or (today - fees.attested_on).days > fees.review_interval_days:
        return FeeView("EXPIRED", fees.operator_maker_rate, fees.stress_maker_rate)
    return FeeView("OK", fees.operator_maker_rate, fees.stress_maker_rate)


def fill_events(repos: Repos, venue: str) -> list[ledger.FillEvent]:
    by_attempt: dict[UUID, str] = {}
    events: list[ledger.FillEvent] = []
    for fill in repos.safety.fills(venue):
        product = by_attempt.get(fill.attempt_id)
        if product is None:
            attempt = repos.safety.attempt(fill.attempt_id)
            intent = repos.safety.intent(attempt.intent_id) if attempt else None
            if intent is None:  # pragma: no cover  (foreign keys guarantee both)
                continue
            product = by_attempt[fill.attempt_id] = intent.product_id
        events.append(
            ledger.FillEvent(product, fill.side, fill.price, fill.size, fill.fee, fill.occurred_at)
        )
    return events


def expected_state(repos: Repos, venue: str) -> ledger.LedgerState:
    return ledger.replay(repos.safety.baselines(venue), fill_events(repos, venue))


def _remaining(attempt: AttemptRow, intent: IntentRow) -> Decimal:
    return max(ZERO, intent.base_qty - attempt.filled_qty)


class RiskContextBuilder:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def build(
        self,
        repos: Repos,
        intent: IntentRow,
        *,
        now: datetime,
        book: BookFacts | None,
        client_order_id: str | None = None,
    ) -> RiskInputs:
        s = self._settings.safety
        venue = intent.venue
        control = repos.safety.control()
        run = repos.safety.latest_run()
        recon = None
        unknown_orders = 0
        balance_bad = False
        if run is not None:
            recon = ReconView(run.outcome, int((now - run.finished_at).total_seconds()))
            for finding in repos.safety.findings(run.id):
                if finding.code in ("UNKNOWN_ORDER", "ORDER_APPEARED_AFTER_ABSENCE"):
                    unknown_orders += 1
                if finding.code == "BALANCE_MISMATCH":
                    balance_bad = True
        gate_ok, gate_reason = order_gate(venue, self._settings)
        duplicate_client = bool(client_order_id) and (
            repos.safety.attempt_by_client_id(str(client_order_id)) is not None
        )
        live_attempts = [a for a in repos.safety.attempts_for_intent(intent.id)]
        duplicate_intent = any(
            a.state not in ("ABSENT", "REJECTED") for a in live_attempts
        )  # already being handled or finished

        pair = repos.pairs.get(intent.pair_id)
        pair_active = None if pair is None else pair.state is PairState.PAPER_ACTIVE
        product = repos.products.get_by_product_id(intent.product_id)
        rules: ProductRules | None = None
        market_age = None
        last_price = None
        if product is not None:
            meta = product.metadata
            verdict = pair_policy.assess_product(meta)
            if (
                meta.price_increment is not None
                and meta.base_increment is not None
                and meta.base_min_size is not None
                and meta.quote_min_size is not None
            ):
                rules = ProductRules(
                    status_ok=verdict.ok,
                    price_increment=meta.price_increment,
                    base_increment=meta.base_increment,
                    base_min_size=meta.base_min_size,
                    base_max_size=meta.base_max_size or ZERO,
                    quote_min_size=meta.quote_min_size,
                    metadata_age_seconds=age_seconds(now, product.last_verified_at),
                )
            newest = repos.market.newest_candle_start(product.id, GRANULARITY)
            market_age = candle_data_age(int(now.timestamp()), newest)
            if newest is not None:
                candles = repos.market.candles(product.id, GRANULARITY, newest, newest + STEP)
                last_price = candles[-1].close if candles else None

        capital, equity = self._capital_and_equity(repos, venue, now, last_price, intent.product_id)
        failures = repos.safety.api_failures_since(
            venue, now - timedelta(seconds=s.api_failure_window_seconds)
        )
        return RiskInputs(
            gate_ok=gate_ok,
            gate_reason=gate_reason,
            kill_active=control.kill_switch == "ACTIVE",
            breaker_open=control.breaker_state == "OPEN",
            bot_running=control.bot_state == "RUNNING",
            recovery_complete=control.recovery_state == "COMPLETE",
            reconciliation=recon,
            unknown_attempts=repos.safety.count_state("UNKNOWN"),
            unknown_orders=unknown_orders,
            balance_unexpected=balance_bad,
            duplicate_client_id=duplicate_client,
            duplicate_intent=duplicate_intent,
            api_failures_recent=failures,
            pair_active=pair_active,
            product=rules,
            market_data_age_seconds=market_age,
            last_price=last_price,
            spread_bps=book.spread_bps if book else None,
            book_age_seconds=book.age_seconds if book else None,
            fee=fee_view(self._settings, now.date()),
            capital=capital,
            equity=equity,
            assumed_slippage_bps=self._settings.pair_policy.strategy.assumed_slippage_bps,
            safety_margin=self._settings.pair_policy.strategy.safety_margin,
        )

    def _capital_and_equity(
        self,
        repos: Repos,
        venue: str,
        now: datetime,
        last_price: Decimal | None,
        product_id: str,
    ) -> tuple[CapitalView | None, EquityView | None]:
        baselines = repos.safety.baselines(venue)
        if ledger.QUOTE not in baselines:
            return None, None  # no recorded starting balance: nothing to size against
        state = expected_state(repos, venue)
        reserved_buys = ZERO
        reserved_sell_qty = ZERO
        for attempt in repos.safety.attempts_in(LIVE_ATTEMPT_STATES, venue):
            intent = repos.safety.intent(attempt.intent_id)
            if intent is None:  # pragma: no cover
                continue
            remaining = _remaining(attempt, intent)
            if intent.side == "BUY":
                reserved_buys += remaining * intent.price
            else:
                reserved_sell_qty += remaining
        pos = state.positions.get(product_id, ledger.Position())
        inventory_cost = sum((p.cost for p in state.positions.values()), ZERO)
        capital = CapitalView(
            cash_total=state.balances.get(ledger.QUOTE, ZERO),
            reserved_open_buys=reserved_buys,
            inventory_cost=inventory_cost,
            inventory_qty=pos.qty,
            reserved_open_sells_qty=reserved_sell_qty,
        )
        marks = {product_id: last_price} if last_price is not None else {}
        current, peak, day_open = ledger.equity_view(
            state,
            baselines[ledger.QUOTE],
            day_start=datetime.combine(now.date(), time.min, tzinfo=UTC),
            current_marks=marks,
        )
        return capital, EquityView(current, peak, day_open)
