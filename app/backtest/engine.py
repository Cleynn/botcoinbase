"""Backtest engine: runs the shared grid trader over a frozen candle series. Decimal only.

`run_backtest` is a pure function of (candles, config). It reads no clock, database or network,
so the same snapshot and config always give byte-identical results (a test asserts it). Callers load
candles only from a verified dataset snapshot (`app.market.snapshots.load_snapshot`).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

from app.backtest.trader import HALTED, STEP, SimConfig, Trader, TraderState, initial_state
from app.domain.money import ONE, ZERO, canonical, fee_for, pct, quantize_down, ratio
from app.market.candles import Candle, series_gaps
from app.market.snapshots import ENGINE_VERSION
from app.strategy import decision as strat

SCENARIOS: Final = ("OPERATOR", "STRESS")


@dataclass(frozen=True)
class BacktestResult:
    data: dict[str, Any]  # canonical strings only: safe to store as JSON and to hash

    def sha256(self) -> str:
        text = json.dumps(self.data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        return hashlib.sha256(text.encode("ascii")).hexdigest()


def config_payload(cfg: SimConfig, scenario: str) -> dict[str, Any]:
    p = cfg.policy
    return {
        "engine": ENGINE_VERSION,
        "scenario": scenario,
        "levels": cfg.levels,
        "sim_fee": canonical(cfg.sim_fee),
        "decision_fee": canonical(cfg.decision_fee),
        "capital": [
            canonical(p.total_capital),
            canonical(p.min_reserve),
            canonical(p.max_deployment),
        ],
        "growth_enabled": p.capital_growth_enabled,
        "regridding_enabled": p.regridding_enabled,
        "strategy": {k: str(v) for k, v in sorted(p.strategy.model_dump().items())},
        "backtest": {k: str(v) for k, v in sorted(p.backtest.model_dump().items())},
        "validation": {
            "max_gap_ratio": str(p.validation.max_gap_ratio),
            "stress_fee": canonical(p.fees.stress_maker_rate),
        },
        "rules": {
            "price_increment": canonical(cfg.rules.price_increment),
            "base_increment": canonical(cfg.rules.base_increment),
            "quote_increment": canonical(cfg.rules.quote_increment),
            "base_min_size": canonical(cfg.rules.base_min_size),
            "base_max_size": canonical(cfg.rules.base_max_size)
            if cfg.rules.base_max_size
            else None,
            "quote_min_size": canonical(cfg.rules.quote_min_size)
            if cfg.rules.quote_min_size
            else None,
        },
    }


def config_sha256(cfg: SimConfig, scenario: str) -> str:
    text = json.dumps(config_payload(cfg, scenario), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(text.encode("ascii")).hexdigest()


def run_backtest(
    candles: Sequence[Candle], cfg: SimConfig, *, scenario: str, start_index: int = 0
) -> BacktestResult:
    """Trade candles[start_index:] using candles[:start_index] only as history."""
    if scenario not in SCENARIOS:
        raise ValueError("unknown fee scenario")
    if not candles or not 0 <= start_index < len(candles):
        raise ValueError("no candles to trade")
    need = strat.lookback_needed(cfg.policy)
    state: TraderState = initial_state(cfg.policy)
    position = {"i": 0}

    def window(n: int) -> Sequence[Candle]:
        i = position["i"]
        return candles[max(0, i + 1 - n) : i + 1]

    trader = Trader(cfg, state, window)
    equity_curve: list[tuple[int, Decimal]] = []
    for i in range(start_index, len(candles)):
        position["i"] = i
        trader.on_candle(candles[i])
        if (i - start_index) % 288 == 0 or i == len(candles) - 1:
            equity_curve.append((candles[i].start, state.equity(candles[i].close)))
    return BacktestResult(
        _summarise(candles, start_index, cfg, scenario, state, equity_curve, need)
    )


def _summarise(
    candles: Sequence[Candle],
    start_index: int,
    cfg: SimConfig,
    scenario: str,
    s: TraderState,
    curve: list[tuple[int, Decimal]],
    need: int,
) -> dict[str, Any]:
    traded = candles[start_index:]
    last = traded[-1]
    c = s.counters
    capital = cfg.policy.total_capital
    final_equity = s.equity(last.close)
    unrealized = s.inventory * last.close - s.cost_basis
    gaps = series_gaps(tuple(traded), STEP)
    missing = sum((b - a) // STEP for a, b in gaps)
    steps = max(c.candles, 1)
    hold = _buy_and_hold(traded, cfg)
    open_buys = sum(1 for o in s.orders if o.side == "BUY")
    return {
        "label": "BACKTEST",
        "engine_version": ENGINE_VERSION,
        "fee_scenario": scenario,
        "fee_rate": canonical(cfg.sim_fee),
        "levels": cfg.levels,
        "period": {
            "first_candle": traded[0].start,
            "last_candle": last.start,
            "candles": len(traded),
            "history_candles_before": start_index,
            "missing_intervals": missing,
            "gap_count": len(gaps),
        },
        "capital": {
            "total": canonical(capital),
            "protected_reserve": canonical(cfg.policy.min_reserve),
            "deployment_cap": canonical(cfg.policy.max_deployment),
            "growth_enabled": cfg.policy.capital_growth_enabled,
        },
        "performance": {
            "final_equity": canonical(final_equity),
            "net_pnl": canonical(final_equity - capital),
            "return": canonical(ratio(final_equity - capital, capital)),
            "return_display": pct(ratio(final_equity - capital, capital)),
            "realized_pnl": canonical(c.realized),
            "unrealized_pnl": canonical(unrealized),
            "fees_paid": canonical(c.fees_paid),
            "max_drawdown": canonical(c.max_drawdown),
            "max_drawdown_display": pct(c.max_drawdown),
            "buy_and_hold_return": hold,
        },
        "activity": {
            "decisions_grid": c.decisions_grid,
            "decisions_no_trade": c.decisions_no_trade,
            "no_trade_reasons": dict(sorted(c.no_trade_reasons.items())),
            "orders_placed": c.orders_placed,
            "fills": c.fills,
            "partial_fills": c.partial_fills,
            "touched_no_fill": c.touched_no_fill,
            "cycles_completed": c.cycles,
            "stale_age_cancels": c.stale_age_cancels,
            "stale_gap_cancels": c.stale_gap_cancels,
            "post_only_rejects": c.post_only_rejects,
            "capital_blocked": c.capital_blocked,
            "breakout_stops": c.breakout_stops,
            "halts": c.halts,
            "gap_events": c.gap_events,
            "candles_in_market": c.candles_in_market,
            "average_deployed": canonical((c.deployed_sum / steps).quantize(Decimal("0.0001"))),
        },
        "end_state": {
            "phase": s.phase,
            "halted": s.phase == HALTED,
            "cash": canonical(s.cash),
            "inventory": canonical(s.inventory),
            "cost_basis": canonical(s.cost_basis),
            "open_buys": open_buys,
            "open_sells": len(s.orders) - open_buys,
            "reserved": canonical(s.reserved()),
            "free_cash": canonical(s.free_cash()),
        },
        "equity_curve": [[t, canonical(v.quantize(Decimal("0.0001")))] for t, v in curve],
        "invariants": "checked after every candle: reserve, cap, levels 3-5, non-negative holdings",
        "warmup_candles_required": need,
    }


def _buy_and_hold(traded: Sequence[Candle], cfg: SimConfig) -> str:
    """Context only: buy the cap at the first close, value at the last (fees both ways)."""
    first, last = traded[0].close, traded[-1].close
    r = cfg.rules
    qty = quantize_down(cfg.policy.max_deployment / (first * (ONE + cfg.sim_fee)), r.base_increment)
    if qty <= ZERO:
        return "0"
    entry = qty * first + fee_for(qty * first, cfg.sim_fee, r.quote_increment)
    exit_ = qty * last - fee_for(qty * last, cfg.sim_fee, r.quote_increment)
    return canonical(ratio(exit_ - entry, entry))
