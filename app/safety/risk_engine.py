"""The pre-trade risk engine: a pure function from facts to ALLOW or BLOCK with every reason.

It reads nothing and writes nothing. `RiskInputs` carries facts gathered elsewhere; a fact that is
missing (`None`) is a reason to block, never a reason to allow. The engine returns ALL reasons it
finds, not the first, so an operator sees the full picture. It cannot raise a ceiling: reserve,
deployment cap and per-order cap come from `app.constants` and config may only tighten them.

Money rules that the database also enforces (reserve, cap) are checked here as well as in SQL:
this check is an early refusal with a reason, the database CHECKs are the backstop.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from app import constants
from app.config import SafetySettings
from app.safety.types import (
    CapitalView,
    Decision,
    EquityView,
    OrderProposal,
    ProductRules,
    ReconView,
)

_BPS: Final = Decimal("10000")
ZERO: Final = Decimal(0)


@dataclass(frozen=True)
class FeeView:
    state: str  # OK | UNATTESTED | EXPIRED
    operator_rate: Decimal | None
    stress_rate: Decimal


@dataclass(frozen=True)
class RiskInputs:
    """Facts. `None` means unknown and blocks."""

    gate_ok: bool
    gate_reason: str | None
    kill_active: bool | None
    breaker_open: bool | None
    bot_running: bool | None
    recovery_complete: bool | None
    reconciliation: ReconView | None
    unknown_attempts: int | None
    unknown_orders: int | None
    balance_unexpected: bool | None
    duplicate_client_id: bool
    duplicate_intent: bool
    api_failures_recent: int | None
    pair_active: bool | None
    product: ProductRules | None
    market_data_age_seconds: int | None
    last_price: Decimal | None
    spread_bps: Decimal | None
    book_age_seconds: int | None
    fee: FeeView | None
    capital: CapitalView | None
    equity: EquityView | None
    assumed_slippage_bps: Decimal = Decimal("5")
    safety_margin: Decimal = Decimal("0.001")


def _canonical(value: object) -> object:
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, dict):
        return {k: _canonical(v) for k, v in sorted(value.items())}
    if isinstance(value, list | tuple):
        return [_canonical(v) for v in value]
    if hasattr(value, "__dataclass_fields__"):
        return _canonical({k: getattr(value, k) for k in value.__dataclass_fields__})
    return value


def inputs_hash(order: OrderProposal, facts: RiskInputs) -> str:
    """A digest of exactly what was decided on, so a stale decision cannot be reused."""
    blob = json.dumps(
        {"order": _canonical(order), "facts": _canonical(facts)},
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(blob.encode()).hexdigest()


def _multiple_of(value: Decimal, step: Decimal) -> bool:
    return step > ZERO and value % step == ZERO


def evaluate(order: OrderProposal, facts: RiskInputs, limits: SafetySettings) -> Decision:
    reasons: set[str] = set()

    def block(code: str) -> None:
        reasons.add(code)

    # ---- authority: gate, kill switch, breaker, bot state, recovery, reconciliation
    if not facts.gate_ok:
        block("LIVE_GATE_BLOCKED")
    if facts.kill_active is not False:
        block("KILL_SWITCH_ACTIVE")
    if facts.breaker_open is not False:
        block("BREAKER_OPEN")
    if facts.bot_running is not True:
        block("BOT_NOT_RUNNING")
    if facts.recovery_complete is not True:
        block("RECOVERY_INCOMPLETE")
    recon = facts.reconciliation
    if recon is None:
        block("RECONCILIATION_MISSING")
    elif recon.outcome != "OK":
        block("RECONCILIATION_FAILED")
    elif recon.age_seconds > limits.reconcile_max_age_seconds or recon.age_seconds < 0:
        block("RECONCILIATION_STALE")
    if facts.unknown_orders is None or facts.unknown_orders > 0:
        block("UNKNOWN_ORDER")
    if facts.unknown_attempts is None or facts.unknown_attempts > 0:
        block("UNKNOWN_ATTEMPT")
    if facts.balance_unexpected is not False:
        block("UNEXPECTED_BALANCE")
    if facts.duplicate_client_id:
        block("DUPLICATE_CLIENT_ID")
    if facts.duplicate_intent:
        block("DUPLICATE_INTENT")
    failures = facts.api_failures_recent
    if failures is None or failures >= limits.api_failure_threshold:
        block("API_FAILURES")

    # ---- order shape: only post-only limit GTC, positive numbers
    if (
        order.order_type != "limit_limit_gtc"
        or order.post_only is not True
        or order.side not in ("BUY", "SELL")
        or order.price <= ZERO
        or order.base_qty <= ZERO
    ):
        block("ORDER_SHAPE")

    # ---- pair, product, metadata
    if facts.pair_active is not True:
        block("PAIR_NOT_ACTIVE")
    product = facts.product
    if product is None:
        block("STALE_METADATA")
        block("PRODUCT_NOT_TRADABLE")
    else:
        if not product.status_ok:
            block("PRODUCT_NOT_TRADABLE")
        age = product.metadata_age_seconds
        if age is None or age < 0 or age > limits.metadata_max_age_seconds:
            block("STALE_METADATA")
        if order.price > ZERO and not _multiple_of(order.price, product.price_increment):
            block("PRICE_PRECISION")
        if order.base_qty > ZERO and not _multiple_of(order.base_qty, product.base_increment):
            block("SIZE_PRECISION")
        if order.base_qty < product.base_min_size:
            block("BELOW_MIN_SIZE")
        if product.base_max_size > ZERO and order.base_qty > product.base_max_size:
            block("ABOVE_MAX_SIZE")
        if order.price * order.base_qty < product.quote_min_size:
            block("BELOW_MIN_NOTIONAL")

    # ---- market data, spread, price sanity
    data_age = facts.market_data_age_seconds
    if data_age is None or data_age < 0 or data_age > limits.market_data_max_age_seconds:
        block("STALE_MARKET_DATA")
    if facts.spread_bps is None or facts.book_age_seconds is None:
        block("SPREAD_UNKNOWN")
    elif facts.book_age_seconds < 0 or facts.book_age_seconds > limits.book_max_age_seconds:
        block("SPREAD_UNKNOWN")
    elif facts.spread_bps > limits.max_spread_bps:
        block("SPREAD_ABNORMAL")
    if facts.last_price is None or facts.last_price <= ZERO:
        block("STALE_MARKET_DATA")
    elif abs(order.price - facts.last_price) / facts.last_price > limits.max_price_deviation_ratio:
        block("PRICE_DEVIATION")

    # ---- fees and expected edge
    fee = facts.fee
    if fee is None or fee.state == "UNATTESTED" or fee.operator_rate is None:
        block("FEE_UNATTESTED")
    elif fee.state == "EXPIRED":
        block("FEE_EXPIRED")
    else:
        spread = (facts.spread_bps or ZERO) / _BPS
        required = (
            2 * max(fee.operator_rate, fee.stress_rate)
            + spread
            + facts.assumed_slippage_bps / _BPS
            + facts.safety_margin
        )
        if order.expected_cycle_return is None or order.expected_cycle_return <= required:
            block("EDGE_BELOW_COSTS")

    # ---- capital: reserve, deployment cap, per-order cap, inventory
    notional = order.price * order.base_qty
    cap = min(limits.per_order_cap, constants.POLICY_MAX_ORDER_NOTIONAL)
    if notional > cap:
        block("ORDER_CAP_BREACH")
    capital = facts.capital
    if capital is None:
        block("RESERVE_BREACH")
        block("DEPLOYMENT_CAP_BREACH")
    elif order.side == "BUY":
        stress = fee.stress_rate if fee is not None else Decimal("0.006")
        outlay = notional * (1 + stress)
        free = capital.cash_total - capital.reserved_open_buys - outlay
        if free < constants.POLICY_MIN_RESERVE:
            block("RESERVE_BREACH")
        deployed = capital.reserved_open_buys + capital.inventory_cost + notional
        if deployed > constants.POLICY_MAX_DEPLOYMENT:
            block("DEPLOYMENT_CAP_BREACH")
    elif order.side == "SELL":
        if order.base_qty > capital.inventory_qty - capital.reserved_open_sells_qty:
            block("SELL_EXCEEDS_INVENTORY")

    # ---- loss and drawdown
    eq = facts.equity
    if eq is None:
        block("EQUITY_UNKNOWN")
    else:
        if eq.day_start_equity - eq.equity >= limits.daily_loss_limit:
            block("LOSS_LIMIT")
        if eq.peak_equity > ZERO and (eq.peak_equity - eq.equity) / eq.peak_equity >= (
            limits.max_drawdown_ratio
        ):
            block("DRAWDOWN_LIMIT")

    digest = inputs_hash(order, facts)
    if reasons:
        return Decision(False, tuple(sorted(reasons)), digest)
    return Decision(True, (), digest)
