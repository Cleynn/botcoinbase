"""The pre-trade risk engine: every block reason, fail-closed on unknowns, no ceiling can be raised.

All numbers are SYNTHETIC. The engine is pure: nothing here touches a database or an exchange.
"""

from __future__ import annotations

from dataclasses import fields, replace
from decimal import Decimal
from typing import Any

import pytest
from pydantic import ValidationError

from app import constants
from app.config import SafetySettings
from app.safety import risk_engine as engine
from app.safety.risk_engine import FeeView, RiskInputs
from app.safety.types import (
    BLOCK_REASONS,
    CapitalView,
    Decision,
    EquityView,
    OrderProposal,
    ProductRules,
    ReconView,
)

D = Decimal
LIMITS = SafetySettings()
ORDER = OrderProposal(
    "FAKE", "pair-1", "BTC-USDC", "BUY", D("100"), D("0.1"), expected_cycle_return=D("0.03")
)
RULES = ProductRules(True, D("0.01"), D("0.00000001"), D("0.00000001"), D("3400"), D("1"), 60)
CAPITAL = CapitalView(D("50"), D("0"), D("0"), D("0"), D("0"))
EQUITY = EquityView(D("50"), D("50"), D("50"))
FEE = FeeView("OK", D("0.002"), D("0.006"))
GOOD = RiskInputs(
    gate_ok=True,
    gate_reason=None,
    kill_active=False,
    breaker_open=False,
    bot_running=True,
    recovery_complete=True,
    reconciliation=ReconView("OK", 10),
    unknown_attempts=0,
    unknown_orders=0,
    balance_unexpected=False,
    duplicate_client_id=False,
    duplicate_intent=False,
    api_failures_recent=0,
    pair_active=True,
    product=RULES,
    market_data_age_seconds=60,
    last_price=D("100"),
    spread_bps=D("10"),
    book_age_seconds=5,
    fee=FEE,
    capital=CAPITAL,
    equity=EQUITY,
)


def decide(
    order: OrderProposal = ORDER, facts: RiskInputs = GOOD, limits: SafetySettings = LIMITS
) -> Decision:
    return engine.evaluate(order, facts, limits)


def with_(**changes: Any) -> RiskInputs:
    return replace(GOOD, **changes)


def rules_with(**changes: Any) -> ProductRules:
    return replace(RULES, **changes)


def test_a_clean_order_is_allowed_with_no_reasons() -> None:
    result = decide()
    assert result.allowed and result.reasons == ()


# reason -> (order, facts) that must produce exactly that reason
CASES: dict[str, tuple[OrderProposal, RiskInputs]] = {
    "LIVE_GATE_BLOCKED": (ORDER, with_(gate_ok=False, gate_reason="LIVE_GATE_BLOCKED")),
    "KILL_SWITCH_ACTIVE": (ORDER, with_(kill_active=True)),
    "BREAKER_OPEN": (ORDER, with_(breaker_open=True)),
    "BOT_NOT_RUNNING": (ORDER, with_(bot_running=False)),
    "RECOVERY_INCOMPLETE": (ORDER, with_(recovery_complete=False)),
    "RECONCILIATION_MISSING": (ORDER, with_(reconciliation=None)),
    "RECONCILIATION_STALE": (ORDER, with_(reconciliation=ReconView("OK", 301))),
    "RECONCILIATION_FAILED": (ORDER, with_(reconciliation=ReconView("MISMATCH", 5))),
    "UNKNOWN_ORDER": (ORDER, with_(unknown_orders=1)),
    "UNKNOWN_ATTEMPT": (ORDER, with_(unknown_attempts=1)),
    "DUPLICATE_CLIENT_ID": (ORDER, with_(duplicate_client_id=True)),
    "DUPLICATE_INTENT": (ORDER, with_(duplicate_intent=True)),
    "UNEXPECTED_BALANCE": (ORDER, with_(balance_unexpected=True)),
    "API_FAILURES": (ORDER, with_(api_failures_recent=LIMITS.api_failure_threshold)),
    "STALE_MARKET_DATA": (
        ORDER,
        with_(market_data_age_seconds=LIMITS.market_data_max_age_seconds + 1),
    ),
    "STALE_METADATA": (
        ORDER,
        with_(product=rules_with(metadata_age_seconds=LIMITS.metadata_max_age_seconds + 1)),
    ),
    "PRODUCT_NOT_TRADABLE": (ORDER, with_(product=rules_with(status_ok=False))),
    "PAIR_NOT_ACTIVE": (ORDER, with_(pair_active=False)),
    "PRICE_PRECISION": (replace(ORDER, price=D("100.005")), GOOD),
    "SIZE_PRECISION": (replace(ORDER, base_qty=D("0.100000001")), GOOD),
    "BELOW_MIN_SIZE": (ORDER, with_(product=rules_with(base_min_size=D("0.5")))),
    "ABOVE_MAX_SIZE": (ORDER, with_(product=rules_with(base_max_size=D("0.05")))),
    "BELOW_MIN_NOTIONAL": (ORDER, with_(product=rules_with(quote_min_size=D("20")))),
    "FEE_UNATTESTED": (ORDER, with_(fee=FeeView("UNATTESTED", None, D("0.006")))),
    "FEE_EXPIRED": (ORDER, with_(fee=FeeView("EXPIRED", D("0.002"), D("0.006")))),
    "EDGE_BELOW_COSTS": (replace(ORDER, expected_cycle_return=D("0.01")), GOOD),
    "RESERVE_BREACH": (ORDER, with_(capital=replace(CAPITAL, cash_total=D("20")))),
    "DEPLOYMENT_CAP_BREACH": (ORDER, with_(capital=replace(CAPITAL, inventory_cost=D("30")))),
    "ORDER_CAP_BREACH": (replace(ORDER, base_qty=D("0.2")), GOOD),
    "SELL_EXCEEDS_INVENTORY": (
        replace(ORDER, side="SELL"),
        with_(capital=replace(CAPITAL, inventory_qty=D("0.05"))),
    ),
    "LOSS_LIMIT": (ORDER, with_(equity=EquityView(D("46.9"), D("50"), D("50")))),
    "DRAWDOWN_LIMIT": (ORDER, with_(equity=EquityView(D("44.9"), D("50"), D("44.9")))),
    "EQUITY_UNKNOWN": (ORDER, with_(equity=None)),
    "SPREAD_ABNORMAL": (ORDER, with_(spread_bps=D("35"))),
    "SPREAD_UNKNOWN": (ORDER, with_(spread_bps=None, book_age_seconds=None)),
    "PRICE_DEVIATION": (replace(ORDER, price=D("106")), GOOD),
    "ORDER_SHAPE": (replace(ORDER, post_only=False), GOOD),
}
# codes the engine never emits by itself: the pipeline uses them when a fact could not be read
PIPELINE_ONLY = {"INPUTS_UNAVAILABLE"}


def test_every_block_reason_has_a_case_and_is_reachable() -> None:
    assert set(CASES) | PIPELINE_ONLY == set(BLOCK_REASONS)


@pytest.mark.parametrize("reason", sorted(CASES))
def test_each_single_fault_blocks_with_its_reason(reason: str) -> None:
    order, facts = CASES[reason]
    result = decide(order, facts)
    assert not result.allowed
    assert reason in result.reasons
    assert set(result.reasons) <= set(BLOCK_REASONS)


@pytest.mark.parametrize("reason", sorted(CASES))
def test_a_single_fault_gives_only_its_own_reason(reason: str) -> None:
    order, facts = CASES[reason]
    assert decide(order, facts).reasons == (reason,)


# ------------------------------------------------------------------ fail closed
NULLABLE = [
    f.name
    for f in fields(RiskInputs)
    if f.name
    in {
        "kill_active", "breaker_open", "bot_running", "recovery_complete", "reconciliation",
        "unknown_attempts", "unknown_orders", "balance_unexpected", "api_failures_recent",
        "pair_active", "product", "market_data_age_seconds", "last_price", "spread_bps",
        "book_age_seconds", "fee", "capital", "equity",
    }
]  # fmt: skip


@pytest.mark.parametrize("name", NULLABLE)
def test_an_unknown_fact_blocks(name: str) -> None:
    result = decide(facts=with_(**{name: None}))
    assert not result.allowed and result.reasons


def test_everything_unknown_blocks_with_many_reasons() -> None:
    blind = RiskInputs(
        gate_ok=False, gate_reason=None, kill_active=None, breaker_open=None, bot_running=None,
        recovery_complete=None, reconciliation=None, unknown_attempts=None, unknown_orders=None,
        balance_unexpected=None, duplicate_client_id=False, duplicate_intent=False,
        api_failures_recent=None, pair_active=None, product=None, market_data_age_seconds=None,
        last_price=None, spread_bps=None, book_age_seconds=None, fee=None, capital=None,
        equity=None,
    )  # fmt: skip
    result = decide(facts=blind)
    assert not result.allowed and len(result.reasons) >= 15


def test_all_reasons_are_reported_not_just_the_first() -> None:
    facts = with_(kill_active=True, breaker_open=True, reconciliation=None, spread_bps=D("99"))
    assert {
        "KILL_SWITCH_ACTIVE",
        "BREAKER_OPEN",
        "RECONCILIATION_MISSING",
        "SPREAD_ABNORMAL",
    } <= set(decide(facts=facts).reasons)


def test_future_timestamps_and_negative_ages_are_unknown_not_fresh() -> None:
    assert "STALE_MARKET_DATA" in decide(facts=with_(market_data_age_seconds=-5)).reasons
    assert "RECONCILIATION_STALE" in decide(facts=with_(reconciliation=ReconView("OK", -1))).reasons
    assert (
        "STALE_METADATA" in decide(facts=with_(product=rules_with(metadata_age_seconds=-1))).reasons
    )
    assert "SPREAD_UNKNOWN" in decide(facts=with_(book_age_seconds=-1)).reasons


@pytest.mark.parametrize(
    "order",
    [
        replace(ORDER, price=D("0")),
        replace(ORDER, price=D("-1")),
        replace(ORDER, base_qty=D("0")),
        replace(ORDER, side="HOLD"),
        replace(ORDER, order_type="market_market_ioc"),
        replace(ORDER, post_only=False),
    ],
)
def test_a_malformed_order_shape_blocks(order: OrderProposal) -> None:
    assert "ORDER_SHAPE" in decide(order).reasons


# ------------------------------------------------------------------ ceilings
def test_config_may_tighten_but_never_raise_the_per_order_cap() -> None:
    assert SafetySettings().per_order_cap == constants.POLICY_MAX_ORDER_NOTIONAL
    with pytest.raises(ValidationError):
        SafetySettings(per_order_cap=constants.POLICY_MAX_ORDER_NOTIONAL + 1)
    tight = SafetySettings(per_order_cap=D("5"))
    assert "ORDER_CAP_BREACH" in decide(limits=tight).reasons  # 10 USDC order against a 5 cap


def test_the_engine_itself_caps_at_the_policy_constant_even_if_limits_were_forged() -> None:
    forged = SafetySettings.model_construct(
        **{**SafetySettings().model_dump(), "per_order_cap": D("999")}
    )
    big = replace(ORDER, base_qty=D("0.2"))
    assert "ORDER_CAP_BREACH" in decide(big, GOOD, forged).reasons


def test_the_protected_reserve_and_deployment_cap_are_the_policy_constants() -> None:
    assert constants.POLICY_MIN_RESERVE == D("15") and constants.POLICY_MAX_DEPLOYMENT == D("35")
    # exactly at the reserve: cash 50, buy 10 with stress fee -> free 39.94; push cash so free == 15
    edge = with_(capital=replace(CAPITAL, cash_total=D("25.06")))
    assert decide(facts=edge).allowed
    over = with_(capital=replace(CAPITAL, cash_total=D("25.05")))
    assert "RESERVE_BREACH" in decide(facts=over).reasons
    at_cap = with_(capital=replace(CAPITAL, inventory_cost=D("25")))
    assert decide(facts=at_cap).allowed
    assert (
        "DEPLOYMENT_CAP_BREACH"
        in decide(facts=with_(capital=replace(CAPITAL, inventory_cost=D("25.01")))).reasons
    )


def test_open_buy_reserves_count_against_reserve_and_cap() -> None:
    facts = with_(capital=replace(CAPITAL, reserved_open_buys=D("30")))
    reasons = decide(facts=facts).reasons
    assert "DEPLOYMENT_CAP_BREACH" in reasons and "RESERVE_BREACH" in reasons


def test_open_sell_reserves_reduce_the_sellable_inventory() -> None:
    sell = replace(ORDER, side="SELL")
    ok = with_(capital=replace(CAPITAL, inventory_qty=D("0.3"), reserved_open_sells_qty=D("0.2")))
    assert decide(sell, ok).allowed
    short = with_(
        capital=replace(CAPITAL, inventory_qty=D("0.3"), reserved_open_sells_qty=D("0.25"))
    )
    assert "SELL_EXCEEDS_INVENTORY" in decide(sell, short).reasons


def test_the_edge_check_uses_the_higher_of_operator_and_stress_fee_on_both_sides() -> None:
    # required = 2 * 0.006 + 0.0010 spread + 0.0005 slippage + 0.001 margin = 0.0145
    assert decide(replace(ORDER, expected_cycle_return=D("0.0146"))).allowed
    assert "EDGE_BELOW_COSTS" in decide(replace(ORDER, expected_cycle_return=D("0.0145"))).reasons
    assert "EDGE_BELOW_COSTS" in decide(replace(ORDER, expected_cycle_return=None)).reasons
    low_stress = with_(fee=FeeView("OK", D("0.002"), D("0.001")))
    assert decide(
        replace(ORDER, expected_cycle_return=D("0.0071")), low_stress
    ).allowed  # 0.004+...


# ------------------------------------------------------------------ decision integrity
def test_decision_rejects_inconsistent_construction() -> None:
    with pytest.raises(ValueError, match="unknown reason"):
        Decision(False, ("NOT_A_REASON",), "0" * 64)
    with pytest.raises(ValueError, match="no reasons"):
        Decision(True, ("KILL_SWITCH_ACTIVE",), "0" * 64)
    with pytest.raises(ValueError, match="at least one"):
        Decision(False, (), "0" * 64)


def test_inputs_hash_is_deterministic_and_changes_with_any_input() -> None:
    base = decide().inputs_hash
    assert base == decide().inputs_hash and len(base) == 64
    assert decide(replace(ORDER, price=D("100.01"))).inputs_hash != base
    assert decide(facts=with_(spread_bps=D("11"))).inputs_hash != base
    assert decide(facts=with_(capital=replace(CAPITAL, cash_total=D("49")))).inputs_hash != base


def test_the_hash_ignores_decimal_formatting_but_not_value() -> None:
    a = decide(replace(ORDER, base_qty=D("0.1"))).inputs_hash
    b = decide(replace(ORDER, base_qty=D("0.10"))).inputs_hash
    assert a == b


def test_evaluate_is_pure_and_repeatable() -> None:
    assert decide(*CASES["RESERVE_BREACH"]) == decide(*CASES["RESERVE_BREACH"])
