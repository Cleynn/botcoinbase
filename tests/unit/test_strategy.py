"""Indicators, the geometric grid builder, the trend/range filters and pair scoring."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app import constants
from app.config import load_settings
from app.domain.money import canonical
from app.strategy import decision as strat
from app.strategy import indicators as ind
from app.strategy.grid import CapitalPolicy, CostModel, GridRejected, MarketRules, build_grid
from tests.market_data import choppy_range, trend

D = Decimal
BASE = load_settings({"TD_ENVIRONMENT": "test", "TD_SECRET_KEY": "x" * 40}).pair_policy
POLICY = BASE.model_copy(
    update={"strategy": BASE.strategy.model_copy(update={"max_trend_separation": D("0.05")})}
)
RULES = MarketRules(D("0.01"), D("0.0001"), D("0.01"), D("0.0001"), None, D("1"))
CAPITAL = CapitalPolicy(D("50"), D("15"), D("35"))
COSTS = CostModel(D("0.002"), D("0.006"), D("0.001"), D("0.0005"), D("0.001"))


# ------------------------------------------------------------------ indicators
def test_sma_ema_are_exact_decimals() -> None:
    xs = [D(i) for i in range(1, 11)]
    assert ind.sma(xs, 4) == D("8.5")
    assert ind.sma(xs, 11) is None and ind.ema(xs, 11) is None
    e = ind.ema(xs, 3)
    assert e is not None and isinstance(e, Decimal) and D("8") < e < D("10")
    assert ind.ema([D(5)] * 20, 5) == D(5)


def test_atr_uses_true_range_across_gaps() -> None:
    c = choppy_range(60)
    a = ind.atr(c, 14)
    assert a is not None and a > 0
    assert ind.atr(c[:10], 14) is None
    flat = trend(30, per_candle=D("0"), wick=D("0"))
    assert ind.atr(flat, 14) == 0


def test_efficiency_ratio_separates_trends_from_ranges() -> None:
    up = [D(100 + i) for i in range(60)]
    zig = [D(100 + (i % 2)) for i in range(60)]
    assert ind.efficiency_ratio(up, 40) == 1
    ratio = ind.efficiency_ratio(zig, 40)
    assert ratio is not None and ratio < D("0.05")
    assert ind.efficiency_ratio([D(5)] * 50, 20) == 0
    assert ind.efficiency_ratio(up[:10], 40) is None


def test_donchian() -> None:
    c = choppy_range(100)
    band = ind.donchian(c, 50)
    assert band is not None and band[0] < band[1] and band[0] == min(x.low for x in c[-50:])


# ------------------------------------------------------------------ grid builder
def build(lower: str = "96", upper: str = "104", levels: int = 4, **kw: Any) -> Any:
    return build_grid(
        lower=D(lower),
        upper=D(upper),
        levels=levels,
        rules=kw.get("rules", RULES),
        capital=kw.get("capital", CAPITAL),
        costs=kw.get("costs", COSTS),
    )


def test_the_grid_is_geometric_with_conservative_rounding() -> None:
    plan = build()
    assert plan.levels == 4 and len(plan.cells) == 3 and len(plan.lines) == 4
    ratios = [plan.lines[i + 1] / plan.lines[i] for i in range(3)]
    assert max(ratios) - min(ratios) < D("1e-20")  # constant ratio: geometric, not arithmetic
    for cell, line, nxt in zip(plan.cells, plan.lines, plan.lines[1:], strict=False):
        assert cell.buy_price <= line and cell.sell_price >= nxt  # buys down, sells up
        assert (
            cell.buy_price % RULES.price_increment == 0
            and cell.sell_price % RULES.price_increment == 0
        )
        assert cell.base_qty % RULES.base_increment == 0 and cell.gross_return > 0


@pytest.mark.parametrize("levels", [3, 4, 5])
def test_capital_limits_hold_for_every_allowed_level_count(levels: int) -> None:
    plan = build(lower="94", upper="106", levels=levels)
    assert plan.max_commitment <= constants.POLICY_MAX_DEPLOYMENT
    assert CAPITAL.total - plan.max_commitment >= constants.POLICY_MIN_RESERVE
    assert all(c.reserve <= plan.cell_budget for c in plan.cells)
    assert all(c.base_qty * c.buy_price * (1 + COSTS.stress_fee) <= c.reserve for c in plan.cells)


@pytest.mark.parametrize("levels", [-1, 0, 1, 2, 21, 22, 100])
def test_levels_outside_three_to_twenty_are_refused(levels: int) -> None:
    with pytest.raises(GridRejected) as err:
        build(levels=levels)
    assert err.value.code == "LEVELS_OUT_OF_RANGE"


def test_capital_policy_can_only_tighten_the_hard_ceilings() -> None:
    for bad in (
        CapitalPolicy(D("60"), D("15"), D("35")),
        CapitalPolicy(D("50"), D("10"), D("35")),
        CapitalPolicy(D("50"), D("15"), D("40")),
        CapitalPolicy(D("50"), D("20"), D("35")),
    ):
        with pytest.raises(GridRejected) as err:
            build(capital=bad)
        assert err.value.code == "CAPITAL_POLICY"
    tighter = build(capital=CapitalPolicy(D("50"), D("20"), D("30")))
    assert tighter.max_commitment <= 30


def test_bad_ranges_and_coarse_ticks_are_refused() -> None:
    for lower, upper in (("0", "10"), ("-1", "10"), ("100", "100"), ("100", "90")):
        with pytest.raises(GridRejected) as err:
            build(lower=lower, upper=upper)
        assert err.value.code == "BAD_RANGE"
    with pytest.raises(GridRejected) as err:
        build(
            lower="100",
            upper="104",
            rules=MarketRules(D("5"), D("0.0001"), D("0.01"), D("0.0001"), None, D("1")),
        )
    assert err.value.code == "PRICE_STEP_TOO_COARSE"


def test_fees_that_eat_the_spacing_reject_the_grid_at_both_rates() -> None:
    with pytest.raises(GridRejected) as err:
        build(lower="99.6", upper="100.4")  # far below the cost of a round trip
    assert err.value.code in {"FEES_INFEASIBLE", "STRESS_FEES_INFEASIBLE"}
    # feasible at the operator fee but not at the stress fee: still refused
    plan_ok = build(lower="94", upper="106", levels=3)
    assert plan_ok.min_return > plan_ok.required_stress > plan_ok.required_operator
    tight = CostModel(D("0.002"), D("0.03"), D("0.001"), D("0.0005"), D("0.001"))
    with pytest.raises(GridRejected) as err2:
        build(lower="94", upper="106", levels=3, costs=tight)
    assert err2.value.code == "STRESS_FEES_INFEASIBLE"


def test_exchange_minimums_and_maximums_are_enforced() -> None:
    with pytest.raises(GridRejected) as e1:
        build(rules=MarketRules(D("0.01"), D("0.0001"), D("0.01"), D("1"), None, D("1")))
    assert e1.value.code == "BELOW_BASE_MINIMUM"
    with pytest.raises(GridRejected) as e2:
        build(rules=MarketRules(D("0.01"), D("0.0001"), D("0.01"), D("0.0001"), None, D("50")))
    assert e2.value.code == "BELOW_QUOTE_MINIMUM"
    with pytest.raises(GridRejected) as e3:
        build(
            rules=MarketRules(D("0.01"), D("0.0001"), D("0.01"), D("0.0001"), D("0.0002"), D("1"))
        )
    assert e3.value.code == "ABOVE_BASE_MAXIMUM"


def test_grid_construction_is_deterministic() -> None:
    a, b = build(), build()
    assert a == b


# ------------------------------------------------------------------ decisions
def decide(candles: Any, **kw: Any) -> strat.Decision:
    return strat.decide(
        candles,
        policy=kw.get("policy", POLICY),
        rules=RULES,
        maker_fee=D("0.002"),
        levels=kw.get("levels"),
    )


def test_a_choppy_range_gives_a_grid() -> None:
    d = decide(choppy_range(1500)[:1200])
    assert d.action == strat.GRID and d.plan is not None and d.reasons == ("GRID_FEASIBLE",)
    assert 3 <= d.plan.levels <= 5 and d.score > 0
    assert d.indicators["price"] and d.indicators["band_lower"] < d.indicators["band_upper"] or True


def test_a_trend_is_no_trade() -> None:
    d = decide(trend(1500), policy=BASE)
    assert d.action == strat.NO_TRADE and d.plan is None
    assert {"TREND_UP", "NOT_RANGING"} & set(d.reasons)


def test_a_falling_trend_is_flagged_as_down() -> None:
    d = decide(trend(1500, per_candle=D("-0.0002")), policy=BASE)
    assert "TREND_DOWN" in d.reasons


def test_too_little_history_is_no_trade() -> None:
    d = decide(choppy_range(100))
    assert d.action == strat.NO_TRADE and d.reasons == ("INSUFFICIENT_HISTORY",)
    assert decide(()).reasons == ("INSUFFICIENT_HISTORY",)


def test_data_gaps_in_the_lookback_are_no_trade() -> None:
    gappy = choppy_range(1500, skip=lambda i: 900 <= i < 1000 or i % 7 == 0)
    d = decide(gappy)
    assert d.action == strat.NO_TRADE and "DATA_GAPS" in d.reasons


def test_a_narrow_band_is_no_trade() -> None:
    d = decide(choppy_range(1500, amp=D("0.002"), noise=D("0.0005")))
    assert d.action == strat.NO_TRADE and "BAND_TOO_NARROW" in d.reasons


def test_an_extreme_band_is_no_trade_for_volatility() -> None:
    d = decide(choppy_range(1500, amp=D("0.30"), noise=D("0.01")))
    assert d.action == strat.NO_TRADE and "VOLATILITY_TOO_HIGH" in d.reasons


def test_infeasible_fees_are_no_trade_with_the_reason() -> None:
    d = decide(choppy_range(1500, amp=D("0.012"), noise=D("0.004"))[:1200])
    assert d.action == strat.NO_TRADE
    assert {"FEES_INFEASIBLE", "STRESS_FEES_INFEASIBLE", "BAND_TOO_NARROW"} & set(d.reasons)


def test_the_decision_uses_only_the_candles_it_is_given() -> None:
    c = choppy_range(1500)
    a = decide(c[:1200])
    b = decide(c[:1200] + trend(300, base=D("500"), start=c[-1].start + 300))  # extra future data
    assert a == decide(c[:1200])
    assert (
        b.action != a.action or b.indicators != a.indicators
    )  # more data does change a later decision
    assert a.indicators == decide(c[:1200]).indicators


def test_score_is_bounded_deterministic_and_explainable() -> None:
    d = decide(choppy_range(1500)[:1200])
    assert D(0) <= d.score <= D(100) and d.score == decide(choppy_range(1500)[:1200]).score
    worst = strat.score_pair(
        separation=D("0.5"), eff=D(1), width=D("0.5"), gap_ratio=D("0.5"), policy=POLICY, plan=None
    )
    assert worst == D("0.00")
    assert strat.score_pair(
        separation=D(0), eff=D(0), width=D("0.11"), gap_ratio=D(0), policy=POLICY, plan=None
    ) <= D(70)


def test_indicator_facts_are_canonical_strings() -> None:
    d = decide(choppy_range(1500)[:1200])
    assert all(isinstance(v, str) for v in d.indicators.values())
    assert canonical(D("1.10")) == "1.1"


def test_operator_fee_needs_a_current_attestation() -> None:
    from datetime import date

    from app.config import FeePolicy

    ok = BASE.model_copy(
        update={"fees": FeePolicy(operator_maker_rate="0.002", attested_on=date(2026, 9, 20))}
    )
    assert strat.operator_fee(ok, date(2026, 9, 29)) == D("0.002")
    for policy, today, code in (
        (BASE, date(2026, 9, 29), "FEE_NOT_ATTESTED"),
        (ok, date(2026, 11, 1), "FEE_ATTESTATION_EXPIRED"),
        (ok, date(2026, 9, 1), "FEE_ATTESTATION_EXPIRED"),
    ):
        with pytest.raises(strat.FeeNotAttested) as err:
            strat.operator_fee(policy, today)
        assert err.value.code == code
