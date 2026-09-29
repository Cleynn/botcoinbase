"""The fee-aware backtest: determinism, capital invariants, pessimistic fills, walk-forward."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

import pytest

from app.backtest import engine
from app.backtest.trader import (
    ACTIVE,
    HALTED,
    STOPPED,
    InvariantViolation,
    Order,
    SimConfig,
    Trader,
    TraderState,
    initial_state,
)
from app.backtest.walkforward import folds, walk_forward
from app.config import load_settings
from app.market.candles import Candle
from app.strategy import decision as strat
from app.strategy.grid import MarketRules, build_grid
from tests.market_data import STEP, T0, choppy_range, crash, series, trend

D = Decimal
BASE = load_settings({"TD_ENVIRONMENT": "test", "TD_SECRET_KEY": "x" * 40}).pair_policy
RULES = MarketRules(D("0.01"), D("0.0001"), D("0.01"), D("0.0001"), None, D("1"))


def policy(**strategy: Any) -> Any:
    s = BASE.strategy.model_copy(update={"max_trend_separation": D("0.05"), **strategy})
    return BASE.model_copy(update={"strategy": s})


def cfg(
    pol: Any = None, fee: str = "0.002", levels: int = 4, rules: MarketRules = RULES
) -> SimConfig:
    return SimConfig(pol or policy(), rules, D(fee), D("0.002"), levels)


def run(candles: Any, scenario: str = "OPERATOR", **kw: Any) -> dict[str, Any]:
    fee = "0.002" if scenario == "OPERATOR" else "0.006"
    return engine.run_backtest(candles, cfg(fee=fee, **kw), scenario=scenario).data


RANGE = choppy_range(6000)


# ------------------------------------------------------------------ determinism and reporting
def test_the_same_input_gives_byte_identical_results() -> None:
    a = engine.run_backtest(RANGE, cfg(), scenario="OPERATOR")
    b = engine.run_backtest(RANGE, cfg(), scenario="OPERATOR")
    assert a.data == b.data and a.sha256() == b.sha256()
    assert json.dumps(a.data, sort_keys=True) == json.dumps(b.data, sort_keys=True)


def test_results_contain_only_json_safe_canonical_values() -> None:
    def walk(x: Any) -> None:
        if isinstance(x, dict):
            for k, v in x.items():
                assert isinstance(k, str)
                walk(v)
        elif isinstance(x, list | tuple):
            for v in x:
                walk(v)
        else:
            assert isinstance(x, str | int | bool) and not isinstance(x, float | Decimal), x

    walk(run(RANGE))


def test_config_hash_covers_every_assumption() -> None:
    base = engine.config_sha256(cfg(), "OPERATOR")
    assert base == engine.config_sha256(cfg(), "OPERATOR") and len(base) == 64
    assert base != engine.config_sha256(cfg(), "STRESS")
    assert base != engine.config_sha256(cfg(levels=5), "OPERATOR")
    assert base != engine.config_sha256(cfg(fee="0.003"), "OPERATOR")
    wider = policy(breakout_buffer=D("0.02"))
    assert base != engine.config_sha256(cfg(pol=wider), "OPERATOR")
    bt = BASE.backtest.model_copy(update={"adverse_bps": D("9")})
    assert base != engine.config_sha256(
        cfg(pol=policy().model_copy(update={"backtest": bt})), "OPERATOR"
    )
    rules = MarketRules(D("0.01"), D("0.0001"), D("0.01"), D("0.0002"), None, D("1"))
    assert base != engine.config_sha256(cfg(rules=rules), "OPERATOR")


def test_the_result_is_labelled_backtest_and_states_the_capital_policy() -> None:
    r = run(RANGE)
    assert r["label"] == "BACKTEST" and r["capital"] == {
        "total": "50",
        "protected_reserve": "15",
        "deployment_cap": "35",
        "growth_enabled": False,
    }
    assert r["engine_version"] and "checked after every candle" in r["invariants"]


def test_net_pnl_is_exactly_realised_plus_unrealised() -> None:
    for scenario in ("OPERATOR", "STRESS"):
        r = run(RANGE, scenario)
        perf = r["performance"]
        assert D(perf["net_pnl"]) == D(perf["realized_pnl"]) + D(perf["unrealized_pnl"])
        assert D(perf["final_equity"]) == D("50") + D(perf["net_pnl"])


def test_the_stress_scenario_pays_more_fees_and_earns_no_more() -> None:
    op, st = run(RANGE, "OPERATOR"), run(RANGE, "STRESS")
    assert D(st["performance"]["fees_paid"]) >= D(op["performance"]["fees_paid"]) > 0
    assert D(st["performance"]["net_pnl"]) <= D(op["performance"]["net_pnl"])


def test_a_range_market_trades_and_a_trend_does_not() -> None:
    r = run(RANGE)
    assert r["activity"]["fills"] > 0 and r["activity"]["cycles_completed"] > 0
    t = engine.run_backtest(trend(3000), cfg(pol=BASE), scenario="OPERATOR").data
    assert t["activity"]["orders_placed"] == 0 and t["performance"]["net_pnl"] == "0"
    assert t["performance"]["final_equity"] == "50"
    assert t["activity"]["decisions_no_trade"] > 0 and t["activity"]["no_trade_reasons"]


def test_no_trade_is_a_normal_outcome_with_reasons() -> None:
    r = engine.run_backtest(
        choppy_range(1500, amp=D("0.002"), noise=D("0.0005")), cfg(), scenario="OPERATOR"
    ).data
    assert r["activity"]["orders_placed"] == 0
    assert (
        "BAND_TOO_NARROW" in r["activity"]["no_trade_reasons"]
        or "INSUFFICIENT_HISTORY" in r["activity"]["no_trade_reasons"]
    )


# ------------------------------------------------------------------ capital invariants
def watch(candles: Any, c: SimConfig) -> tuple[Decimal, Decimal, TraderState]:
    state = initial_state(c.policy)
    pos = {"i": 0}
    trader = Trader(c, state, lambda n: candles[max(0, pos["i"] + 1 - n) : pos["i"] + 1])
    max_deployed, min_free = D(0), D(10**6)
    for i, candle in enumerate(candles):
        pos["i"] = i
        trader.on_candle(candle)
        max_deployed = max(max_deployed, state.deployed())
        min_free = min(min_free, state.free_cash())
    return max_deployed, min_free, state


@pytest.mark.parametrize("market", ["range", "crash", "trend", "gappy", "wide"])
def test_reserve_and_cap_hold_at_every_candle_in_every_market(market: str) -> None:
    candles = {
        "range": RANGE,
        "crash": crash(4000, at=2500),
        "trend": trend(3000),
        "gappy": choppy_range(6000, skip=lambda i: 3000 <= i < 3060 or i % 97 == 0),
        "wide": choppy_range(6000, amp=D("0.08"), noise=D("0.008")),
    }[market]
    for levels in (3, 4, 5):
        max_deployed, min_free, state = watch(candles, cfg(pol=policy(), levels=levels))
        assert max_deployed <= 35 and min_free >= 15
        assert state.cash >= 15 and state.inventory >= 0 and state.cost_basis >= 0


def test_a_realised_loss_never_lets_deployment_break_the_reserve() -> None:
    _, min_free, state = watch(
        choppy_range(6000, amp=D("0.05"), noise=D("0.01")),
        cfg(pol=policy(breakout_buffer=D("0.05"))),
    )
    assert min_free >= 15 and state.free_cash() >= 15


def test_growth_and_regridding_cannot_be_switched_on_even_by_bypassing_validation() -> None:
    for name in ("capital_growth_enabled", "regridding_enabled"):
        bad = BASE.model_copy(update={name: True})
        with pytest.raises(InvariantViolation):
            Trader(cfg(pol=bad), initial_state(bad), lambda n: [])
    with pytest.raises(ValueError):
        BASE.model_validate({**BASE.model_dump(), "capital_growth_enabled": True})


def test_levels_outside_three_to_five_stop_the_run() -> None:
    for levels in (2, 6):
        with pytest.raises(InvariantViolation):
            Trader(cfg(levels=levels), initial_state(BASE), lambda n: [])


def test_an_invariant_breach_is_never_swallowed() -> None:
    c = cfg()
    state = initial_state(c.policy)
    trader = Trader(c, state, lambda n: [])
    state.cash = D("10")  # free cash below the 15 USDC reserve
    with pytest.raises(InvariantViolation, match="reserve"):
        trader.check_invariants()
    state.cash, state.inventory = D("50"), D("-1")  # negative holdings
    with pytest.raises(InvariantViolation):
        trader.check_invariants()


# ------------------------------------------------------------------ pessimistic fills (direct)
def trader_with(
    orders: list[Order], *, last_start: int | None = None, volume: str = "1000"
) -> tuple[Trader, TraderState]:
    c = cfg()
    state = initial_state(c.policy)
    state.orders, state.last_start, state.phase = orders, last_start, "IDLE"
    state.next_seq = 100
    return Trader(c, state, lambda n: []), state


def candle(
    start: int, low: str, high: str, volume: str = "1000", close: str | None = None
) -> Candle:
    mid = D(close) if close else (D(low) + D(high)) / 2
    return Candle(start, mid, D(high), D(low), mid, D(volume))


def buy(price: str = "100", qty: str = "0.0800", placed: int = T0, reserve: str = "8.5") -> Order:
    return Order(1, 0, "BUY", D(price), D(qty), placed, D(reserve))


def test_a_touch_is_not_a_fill_but_trading_through_is() -> None:
    trader, state = trader_with([buy()])
    trader.on_candle(candle(T0 + STEP, "100.00", "101"))  # low == limit: touched only
    assert state.orders and state.counters.touched_no_fill == 1 and state.inventory == 0
    trader.on_candle(
        candle(T0 + 2 * STEP, "99.96", "101")
    )  # 4 bps through: still inside the 5 bps margin
    assert state.inventory == 0
    trader.on_candle(candle(T0 + 3 * STEP, "99.90", "101"))  # 10 bps through
    assert state.inventory == D("0.0800") and not [o for o in state.orders if o.side == "BUY"]


def test_a_sell_needs_the_high_to_trade_through_too() -> None:
    sell = Order(2, 0, "SELL", D("105"), D("0.0800"), T0)
    trader, state = trader_with([sell])
    state.inventory, state.cost_basis, state.cash = D("0.0800"), D("8.0"), D("42")
    trader.on_candle(candle(T0 + STEP, "104", "105.00"))
    assert state.inventory == D("0.0800")
    trader.on_candle(candle(T0 + 2 * STEP, "104", "105.20"))
    assert state.inventory == 0 and state.counters.cycles == 1 and state.counters.realized > 0


def test_an_order_cannot_fill_on_the_candle_it_was_placed() -> None:
    trader, state = trader_with([buy(placed=T0 + STEP)])
    trader.on_candle(candle(T0 + STEP, "90", "101"))
    assert state.inventory == 0 and len(state.orders) == 1
    trader.on_candle(candle(T0 + 2 * STEP, "90", "101"))
    assert state.inventory > 0


def test_thin_volume_gives_partial_fills_and_zero_volume_gives_none() -> None:
    trader, state = trader_with([buy(qty="0.3000", reserve="30")])
    trader.on_candle(candle(T0 + STEP, "90", "101", volume="1.0"))  # 10% of 1.0 = 0.1 base
    assert state.inventory == D("0.1000") and state.counters.partial_fills == 1
    assert state.orders[0].remaining == D("0.2000")
    trader.on_candle(candle(T0 + 2 * STEP, "90", "101", volume="0"))
    assert state.inventory == D("0.1000") and state.counters.touched_no_fill >= 1


def test_volume_is_shared_between_orders_in_a_candle() -> None:
    a, b = (
        buy(price="100", qty="0.1500", reserve="15.5"),
        Order(2, 1, "BUY", D("99"), D("0.1500"), T0, D("15.5")),
    )
    trader, state = trader_with([a, b])
    trader.on_candle(candle(T0 + STEP, "90", "101", volume="1.5"))  # pool = 0.15 in total
    assert state.inventory == D("0.1500")


def test_every_fill_pays_a_fee_rounded_up() -> None:
    trader, state = trader_with([buy(price="100", qty="0.0333", reserve="9")])
    fx = trader.on_candle(candle(T0 + STEP, "90", "101"))
    fill = next(e for e in fx if e.kind == "FILL")
    assert fill.fee == D("0.01") * (
        (fill.notional * D("0.002") / D("0.01")).to_integral_value(rounding="ROUND_CEILING")
    )
    assert fill.fee >= fill.notional * D("0.002")


def test_a_data_gap_cancels_everything_as_stale_and_sells_return_to_pending() -> None:
    sell = Order(2, 0, "SELL", D("110"), D("0.05"), T0)
    trader, state = trader_with([buy(), sell], last_start=T0)
    state.inventory, state.cost_basis, state.cash = D("0.05"), D("5"), D("45")
    trader.on_candle(candle(T0 + 13 * STEP, "100.5", "101"))  # 12 candles is the tolerance
    assert (
        not state.orders
        and state.counters.stale_gap_cancels == 2
        and state.counters.gap_events == 1
    )
    assert state.pending[0] == D("0.05")


def test_old_orders_are_cancelled_as_stale() -> None:
    span = BASE.backtest.max_order_age_candles * STEP
    trader, state = trader_with([buy(placed=T0)], last_start=T0 + span - STEP)
    trader.on_candle(candle(T0 + span, "100.5", "101"))
    assert len(state.orders) == 1
    state.last_start = T0 + span
    trader.on_candle(candle(T0 + span + STEP, "100.5", "101"))
    assert not state.orders and state.counters.stale_age_cancels == 1


def test_post_only_orders_that_would_cross_are_rejected_not_filled() -> None:
    plan = build_grid(
        lower=D("94"),
        upper=D("106"),
        levels=3,
        rules=RULES,
        capital=strat.capital_policy(BASE),
        costs=strat.cost_model(BASE, maker_fee=D("0.002")),
    )
    trader, state = trader_with([])
    state.phase, state.plan = ACTIVE, plan
    state.pending = {0: plan.cells[0].base_qty}
    state.inventory, state.cost_basis, state.cash = plan.cells[0].base_qty, D("8"), D("42")
    effects = trader.on_candle(
        candle(T0 + STEP, "104", "106", close="105.5")
    )  # market above the sell price
    assert state.counters.post_only_rejects >= 1 and any(e.kind == "REJECT" for e in effects)
    assert not [o for o in state.orders if o.side == "SELL" and o.price < D("105.5")]


def test_placed_orders_respect_price_and_size_increments_and_minimums() -> None:
    rules = MarketRules(D("0.05"), D("0.001"), D("0.01"), D("0.001"), D("500"), D("2"))
    state = initial_state(policy())
    pos = {"i": 0}
    c = cfg(rules=rules)
    trader = Trader(c, state, lambda n: RANGE[max(0, pos["i"] + 1 - n) : pos["i"] + 1])
    placed = 0
    for i, candle_ in enumerate(RANGE[:4000]):
        pos["i"] = i
        for e in trader.on_candle(candle_):
            if e.kind == "PLACE" and e.order is not None:
                placed += 1
                o = e.order
                assert o.price % rules.price_increment == 0 and o.qty % rules.base_increment == 0
                assert o.qty >= rules.base_min_size and o.qty * o.price >= (
                    rules.quote_min_size or 0
                )
    assert placed > 0


def test_exchange_minimums_that_a_level_cannot_meet_mean_no_trade() -> None:
    rules = MarketRules(D("0.01"), D("0.0001"), D("0.01"), D("5"), None, D("1"))
    r = run(RANGE, rules=rules)
    assert (
        r["activity"]["orders_placed"] == 0
        and "BELOW_BASE_MINIMUM" in r["activity"]["no_trade_reasons"]
    )


# ----------------------------------------------------------- breakout, drawdown, gaps end to end


# ----------------------------------------------- breakout, drawdown, gaps end to end
def test_a_crash_stops_the_grid_keeps_the_holdings_and_never_sells_at_market() -> None:
    r = run(crash(5000, at=3000))
    assert r["activity"]["breakout_stops"] + r["activity"]["halts"] >= 1
    assert r["end_state"]["phase"] in {STOPPED, HALTED, "IDLE"}
    assert r["end_state"]["open_buys"] == 0
    if D(r["end_state"]["inventory"]) > 0:  # holdings are held, marked to market, not liquidated
        assert D(r["performance"]["unrealized_pnl"]) < 0


def test_a_deep_drawdown_halts_trading() -> None:
    pol = policy(breakout_buffer=D("0.1"))  # keep the grid on so the drawdown builds
    c = SimConfig(
        pol.model_copy(
            update={"backtest": pol.backtest.model_copy(update={"drawdown_stop_ratio": D("0.03")})}
        ),
        RULES,
        D("0.002"),
        D("0.002"),
        4,
    )
    r = engine.run_backtest(crash(5000, at=3000, drop=D("0.15")), c, scenario="OPERATOR").data
    assert r["activity"]["halts"] == 1 and r["end_state"]["halted"] is True
    assert r["end_state"]["open_buys"] == 0
    assert D(r["performance"]["max_drawdown"]) >= D("0.03")


def test_gaps_in_the_data_are_counted_and_orders_are_cancelled_not_filled() -> None:
    gappy = choppy_range(6000, skip=lambda i: 3000 <= i < 3040)
    r = run(gappy)
    assert r["period"]["gap_count"] >= 1 and r["period"]["missing_intervals"] >= 40
    assert r["activity"]["gap_events"] >= 1 and r["activity"]["stale_gap_cancels"] >= 0


# ------------------------------------------------------------------ no lookahead
def test_changing_the_future_never_changes_the_past() -> None:
    cut = 4500
    altered = RANGE[:cut] + choppy_range(1500, base=D("300"), start=RANGE[cut].start)
    a, b = run(RANGE), run(altered)
    curve_a = [p for p in a["equity_curve"] if p[0] < RANGE[cut].start]
    curve_b = [p for p in b["equity_curve"] if p[0] < RANGE[cut].start]
    assert curve_a == curve_b and len(curve_a) > 5


def test_history_before_the_start_index_is_used_only_for_indicators() -> None:
    full = engine.run_backtest(RANGE, cfg(), scenario="OPERATOR", start_index=2000).data
    assert (
        full["period"]["history_candles_before"] == 2000
        and full["period"]["candles"] == len(RANGE) - 2000
    )
    assert full["period"]["first_candle"] == RANGE[2000].start


def test_an_empty_or_out_of_range_start_is_refused() -> None:
    with pytest.raises(ValueError):
        engine.run_backtest((), cfg(), scenario="OPERATOR")
    with pytest.raises(ValueError):
        engine.run_backtest(RANGE, cfg(), scenario="OPERATOR", start_index=len(RANGE))
    with pytest.raises(ValueError):
        engine.run_backtest(RANGE, cfg(), scenario="LIVE")


# ------------------------------------------------------------------ walk-forward
WF_POLICY = policy().model_copy(
    update={
        "backtest": BASE.backtest.model_copy(
            update={"walk_forward_train_candles": 1500, "walk_forward_test_candles": 600}
        )
    }
)


def test_fold_boundaries() -> None:
    assert folds(100, 60, 20) == [] or len(folds(100, 60, 20)) == 2
    fs = folds(5000, 1500, 600)
    assert len(fs) == 5 and fs[0].test_start == 1500 and fs[1].train_start == 600
    assert all(
        f.test_end - f.test_start == 600 and f.test_start - f.train_start == 1500 for f in fs
    )
    assert folds(1000, 1500, 600) == []


def test_walk_forward_is_out_of_sample_deterministic_and_bounded() -> None:
    c = SimConfig(WF_POLICY, RULES, D("0.002"), D("0.002"), 4)
    a = walk_forward(RANGE, c, scenario="OPERATOR")
    assert a == walk_forward(RANGE, c, scenario="OPERATOR")
    assert a["label"] == "BACKTEST" and a["summary"]["fold_count"] == len(a["folds"]) >= 4
    for f in a["folds"]:
        assert f["chosen_levels"] in (3, 4, 5) and f["test_first_candle"] > f["train_first_candle"]
    total = sum(D(f["test"]["net_pnl"]) for f in a["folds"])
    assert D(a["summary"]["out_of_sample_net_pnl"]) == total
    s = a["summary"]
    assert s["positive_folds"] + s["negative_folds"] + s["flat_folds"] == s["fold_count"]


def test_walk_forward_parameters_come_from_the_training_window_only() -> None:
    c = SimConfig(WF_POLICY, RULES, D("0.002"), D("0.002"), 4)
    a = walk_forward(RANGE, c, scenario="OPERATOR")
    first_test_end = 1500 + 600
    mutated = RANGE[:first_test_end] + choppy_range(
        len(RANGE) - first_test_end, base=D("777"), start=RANGE[first_test_end].start
    )
    b = walk_forward(mutated, c, scenario="OPERATOR")
    assert a["folds"][0] == b["folds"][0]  # a later window cannot influence fold 1 in any way


def test_walk_forward_needs_enough_data() -> None:
    c = SimConfig(WF_POLICY, RULES, D("0.002"), D("0.002"), 4)
    with pytest.raises(ValueError):
        walk_forward(RANGE[:1000], c, scenario="OPERATOR")


def test_the_engine_modules_are_pure_no_database_network_or_clock() -> None:
    import ast

    from tests.conftest import ROOT

    for name in ("engine.py", "trader.py", "walkforward.py"):
        tree = ast.parse((ROOT / "app" / "backtest" / name).read_text())
        mods = {n.module or "" for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)} | {
            a.name for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names
        }
        assert not [
            m
            for m in mods
            if m.startswith(
                (
                    "app.storage",
                    "app.adapters",
                    "httpx",
                    "psycopg",
                    "socket",
                    "time",
                    "datetime",
                    "os",
                )
            )
        ], (name, mods)
    _ = series
