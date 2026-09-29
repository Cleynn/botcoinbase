"""The strategy decision: GRID or NO_TRADE, with reasons. Deterministic and free of lookahead.

Input is the closed candles up to now (nothing later). The steps:
1. history and data-quality gate (enough candles, few gaps in the lookback);
2. trend filter (EMA separation) and range detector (Kaufman efficiency ratio);
3. band = Donchian range of the lookback, which must be wide enough (in ATRs) and within limits,
   with the price inside it;
4. build a geometric grid; it is only accepted if a cell's return beats fees, spread, slippage and a
   safety margin at BOTH the operator and the stress maker fee.
Any doubt is NO_TRADE. Pair scoring is a separate, explainable 0-100 number over the same inputs.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from app.config import PairPolicy
from app.domain.money import BPS, ONE, ZERO, canonical, ratio
from app.market.candles import Candle, series_gaps
from app.strategy import indicators as ind
from app.strategy.grid import (
    CapitalPolicy,
    CostModel,
    GridPlan,
    GridRejected,
    MarketRules,
    build_grid,
)

GRID = "GRID"
NO_TRADE = "NO_TRADE"
STEP = 300


@dataclass(frozen=True)
class Decision:
    action: str
    reasons: tuple[str, ...]
    plan: GridPlan | None
    indicators: dict[str, str]
    score: Decimal


def cost_model(policy: PairPolicy, *, maker_fee: Decimal) -> CostModel:
    s = policy.strategy
    return CostModel(
        maker_fee=maker_fee,
        stress_fee=policy.fees.stress_maker_rate,
        spread=s.assumed_spread_bps / BPS,
        slippage=s.assumed_slippage_bps / BPS,
        safety_margin=s.safety_margin,
    )


class FeeNotAttested(Exception):
    """No usable operator-attested maker fee: no trading decision may be made."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def operator_fee(policy: PairPolicy, today: date) -> Decimal:
    """The attested maker fee, or FeeNotAttested (missing, in the future, or past its review)."""
    fees = policy.fees
    if fees.operator_maker_rate is None or fees.attested_on is None:
        raise FeeNotAttested("FEE_NOT_ATTESTED")
    if fees.attested_on > today or (today - fees.attested_on).days > fees.review_interval_days:
        raise FeeNotAttested("FEE_ATTESTATION_EXPIRED")
    return fees.operator_maker_rate


def capital_policy(policy: PairPolicy) -> CapitalPolicy:
    return CapitalPolicy(policy.total_capital, policy.min_reserve, policy.max_deployment)


def lookback_needed(policy: PairPolicy) -> int:
    s = policy.strategy
    return max(s.ema_slow * 3, s.efficiency_lookback + 1, s.atr_period + 1, s.min_history_candles)


def decide(
    candles: Sequence[Candle],
    *,
    policy: PairPolicy,
    rules: MarketRules,
    maker_fee: Decimal,
    levels: int | None = None,
) -> Decision:
    """`candles` must be closed, validated, ascending and end at the decision time."""
    s = policy.strategy
    levels = levels or policy.grid_levels
    reasons: list[str] = []
    facts: dict[str, str] = {}
    need = lookback_needed(policy)
    if len(candles) < s.min_history_candles or len(candles) < need:
        return Decision(
            NO_TRADE, ("INSUFFICIENT_HISTORY",), None, {"candles": str(len(candles))}, ZERO
        )
    window = list(candles[-need:])
    closes = [c.close for c in window]
    price = closes[-1]

    gaps = series_gaps(tuple(window), STEP)
    missing = sum((b - a) // STEP for a, b in gaps)
    gap_ratio = ratio(Decimal(missing), Decimal(need + missing))
    facts["gap_ratio"] = canonical(gap_ratio)
    if gap_ratio > policy.validation.max_gap_ratio:
        reasons.append("DATA_GAPS")

    fast = ind.ema(closes, s.ema_fast)
    slow = ind.ema(closes, s.ema_slow)
    eff = ind.efficiency_ratio(closes, s.efficiency_lookback)
    atr = ind.atr(window, s.atr_period)
    band = ind.donchian(window, s.efficiency_lookback)
    if fast is None or slow is None or eff is None or atr is None or band is None:
        return Decision(NO_TRADE, ("INSUFFICIENT_HISTORY",), None, facts, ZERO)
    separation = ratio(fast - slow, slow)
    facts.update(
        ema_separation=canonical(separation),
        efficiency_ratio=canonical(eff),
        atr_ratio=canonical(ratio(atr, price)),
        price=canonical(price),
    )
    if abs(separation) > s.max_trend_separation:
        reasons.append("TREND_UP" if separation > 0 else "TREND_DOWN")
    if eff > s.max_efficiency_ratio:
        reasons.append("NOT_RANGING")

    lower, upper = band
    width = ratio(upper - lower, price)
    facts.update(
        band_lower=canonical(lower), band_upper=canonical(upper), band_width=canonical(width)
    )
    if width < s.min_band_ratio or ratio(upper - lower, atr) < s.band_atr_multiple:
        reasons.append("BAND_TOO_NARROW")
    if width > s.max_band_ratio:
        reasons.append("VOLATILITY_TOO_HIGH")
    margin = (upper - lower) * s.breakout_buffer
    if not (lower + margin < price < upper - margin):
        reasons.append("PRICE_AT_BAND_EDGE")

    score = score_pair(
        separation=separation, eff=eff, width=width, gap_ratio=gap_ratio, policy=policy, plan=None
    )
    plan: GridPlan | None = None
    if not reasons:
        try:
            plan = build_grid(
                lower=lower,
                upper=upper,
                levels=levels,
                rules=rules,
                capital=capital_policy(policy),
                costs=cost_model(policy, maker_fee=maker_fee),
            )
        except GridRejected as exc:
            reasons.append(exc.code)
    if plan is not None:
        score = score_pair(
            separation=separation,
            eff=eff,
            width=width,
            gap_ratio=gap_ratio,
            policy=policy,
            plan=plan,
        )
        facts["min_cell_return"] = canonical(plan.min_return)
        facts["required_stress"] = canonical(plan.required_stress)
    if reasons:
        return Decision(NO_TRADE, tuple(reasons), None, facts, score)
    return Decision(GRID, ("GRID_FEASIBLE",), plan, facts, score)


def _clip(value: Decimal) -> Decimal:
    return max(ZERO, min(ONE, value))


def score_pair(
    *,
    separation: Decimal,
    eff: Decimal,
    width: Decimal,
    gap_ratio: Decimal,
    policy: PairPolicy,
    plan: GridPlan | None,
) -> Decimal:
    """0 to 100: range quality 25, trend quality 20, band fit 15, data quality 10, fee margin 30."""
    s = policy.strategy
    range_q = _clip(ONE - ratio(eff, s.max_efficiency_ratio))
    trend_q = _clip(ONE - ratio(abs(separation), s.max_trend_separation))
    span = s.max_band_ratio - s.min_band_ratio
    mid = (s.max_band_ratio + s.min_band_ratio) / 2
    fit_q = _clip(ONE - ratio(abs(width - mid), span / 2))
    data_q = _clip(ONE - ratio(gap_ratio, policy.validation.max_gap_ratio))
    fee_q = ZERO
    if plan is not None:
        fee_q = _clip(ratio(plan.min_return - plan.required_stress, plan.required_stress))
    total = 25 * range_q + 20 * trend_q + 15 * fit_q + 10 * data_q + 30 * fee_q
    return total.quantize(Decimal("0.01"))
