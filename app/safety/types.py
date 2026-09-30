"""Shared vocabulary for the safety machinery: fixed reason codes and small value types.

Every block reason is a member of BLOCK_REASONS. Anything unknown, missing or unreadable is a
reason to block, never a reason to allow (prefer NO_TRADE).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Final

VENUES: Final = ("PAPER", "FAKE", "COINBASE")  # COINBASE orders also need a host arming (DB + gate)

BLOCK_REASONS: Final[dict[str, str]] = {
    "LIVE_GATE_BLOCKED": "the live gate is not open for this venue",
    "KILL_SWITCH_ACTIVE": "the kill switch is active",
    "BREAKER_OPEN": "the circuit breaker is open",
    "BOT_NOT_RUNNING": "the bot is not RUNNING",
    "RECOVERY_INCOMPLETE": "startup recovery has not completed",
    "RECONCILIATION_MISSING": "there is no reconciliation result",
    "RECONCILIATION_STALE": "the last successful reconciliation is too old",
    "RECONCILIATION_FAILED": "the last reconciliation failed or found a mismatch",
    "UNKNOWN_ORDER": "an order exists on the exchange that the bot cannot explain",
    "UNKNOWN_ATTEMPT": "an order attempt has an unknown outcome",
    "DUPLICATE_CLIENT_ID": "the client order id is already used",
    "DUPLICATE_INTENT": "an identical intent already exists",
    "UNEXPECTED_BALANCE": "the exchange balance differs from the ledger",
    "API_FAILURES": "too many recent exchange API failures",
    "STALE_MARKET_DATA": "market data is missing or too old",
    "STALE_METADATA": "product metadata is missing or too old",
    "PRODUCT_NOT_TRADABLE": "the product is not in a tradable state",
    "PAIR_NOT_ACTIVE": "the pair is not active for trading",
    "PRICE_PRECISION": "the price does not fit the price increment",
    "SIZE_PRECISION": "the size does not fit the size increment",
    "BELOW_MIN_SIZE": "the size is below the product minimum",
    "ABOVE_MAX_SIZE": "the size is above the product maximum",
    "BELOW_MIN_NOTIONAL": "the notional is below the product minimum",
    "FEE_UNATTESTED": "there is no attested fee configuration",
    "FEE_EXPIRED": "the fee attestation has expired",
    "EDGE_BELOW_COSTS": "the expected cycle return does not exceed fees, spread, slippage and margin",
    "RESERVE_BREACH": "the order would use the protected reserve",
    "DEPLOYMENT_CAP_BREACH": "the order would exceed the deployment cap",
    "ORDER_CAP_BREACH": "the order exceeds the per-order cap",
    "PROFILE_UNAVAILABLE": "no valid capital profile is selected",
    "FUNDS_UNAVAILABLE": "the USDC funds at the venue could not be established",
    "INSUFFICIENT_FUNDS": "the venue's available USDC does not cover the order and the protected reserve",
    "SELL_EXCEEDS_INVENTORY": "the sell is larger than the inventory held",
    "LOSS_LIMIT": "the daily loss limit is reached",
    "DRAWDOWN_LIMIT": "the drawdown limit is reached",
    "EQUITY_UNKNOWN": "equity cannot be computed",
    "SPREAD_ABNORMAL": "the spread is abnormally wide",
    "SPREAD_UNKNOWN": "the spread cannot be measured",
    "PRICE_DEVIATION": "the order price is far from the market price",
    "ORDER_SHAPE": "the order is not a post-only limit GTC order",
    "INPUTS_UNAVAILABLE": "a safety input could not be read",
}


@dataclass(frozen=True)
class Decision:
    allowed: bool
    reasons: tuple[str, ...]
    inputs_hash: str

    def __post_init__(self) -> None:
        unknown = set(self.reasons) - set(BLOCK_REASONS)
        if unknown:  # a typo in a reason must never silently weaken a block
            raise ValueError(f"unknown reason codes: {sorted(unknown)}")
        if self.allowed and self.reasons:
            raise ValueError("an allowed decision has no reasons")
        if not self.allowed and not self.reasons:
            raise ValueError("a block decision needs at least one reason")


@dataclass(frozen=True)
class OrderProposal:
    """The content of an OrderIntent as the risk engine sees it."""

    venue: str
    pair_id: str
    product_id: str
    side: str  # BUY | SELL
    price: Decimal
    base_qty: Decimal
    order_type: str = "limit_limit_gtc"
    post_only: bool = True
    expected_cycle_return: Decimal | None = None  # fraction, conservative


@dataclass(frozen=True)
class ProductRules:
    status_ok: bool
    price_increment: Decimal
    base_increment: Decimal
    base_min_size: Decimal
    base_max_size: Decimal
    quote_min_size: Decimal
    metadata_age_seconds: int | None


@dataclass(frozen=True)
class CapitalView:
    cash_total: Decimal
    reserved_open_buys: Decimal
    inventory_cost: Decimal
    inventory_qty: Decimal
    reserved_open_sells_qty: Decimal


@dataclass(frozen=True)
class EquityView:
    equity: Decimal
    peak_equity: Decimal
    day_start_equity: Decimal


@dataclass(frozen=True)
class ReconView:
    outcome: str  # OK | MISMATCH | FAILED
    age_seconds: int


@dataclass(frozen=True)
class GateResult:
    status: str  # always "BLOCKED" in this build
    reasons: tuple[str, ...]
    checked_at: datetime | None = None
