"""The live grid runner against the scripted FAKE exchange: a grid starts from the strategy, each
cell places post-only orders through the pipeline, fills turn BUYs into SELLs, leaving the band
cancels BUYs, and every gate (mode, kill switch, funds) stops it. SYNTHETIC data. The strategy's
own decision is replaced by a fixed grid around the last price (the strategy has its own tests)."""

from __future__ import annotations

from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest

from app.capital.profiles import ZERO
from app.live.runner import LiveRunner, TickResult
from app.safety.control import MODE_PHRASES
from app.strategy import decision as strategy_decision
from app.strategy.decision import GRID, Decision, capital_policy
from app.strategy.grid import CostModel, build_grid
from tests.integration.safety_env import SafetyEnv

Sql = Callable[..., list[dict[str, Any]]]
D = Decimal


def make_runner(safe: SafetyEnv, **kw: Any) -> LiveRunner:
    return LiveRunner(
        storage=safe.ctl,
        clock=safe.clock,
        settings=safe.settings,
        pipeline=safe.pipeline,
        gateway=safe.fake,
        list_accounts=safe.fake.list_accounts,
        reconcile=safe.reconciler.run,
        book=lambda _p: safe.book(),
        **kw,
    )


def go_live(safe: SafetyEnv, *, choose: bool = True) -> None:
    safe.reauth.available = True
    assert safe.control.switch_mode(safe.ctx, safe.actor, "live", MODE_PHRASES["live"]).kind == "ok"
    if choose:  # the pair is chosen for LIVE on the Bot page (only while the bot is PAUSED)
        with safe.ctl.tx() as repos:
            repos.safety.set_trading_pairs("LIVE", [safe.pair_id])
    safe.baseline("50")
    assert safe.recover().complete
    assert safe.act("resume").kind == "ok"


@pytest.fixture
def fixed_grid(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace the strategy decision by a 3-line grid from 3% below to 3% above the last price."""
    state: dict[str, Any] = {"calls": 0, "action": GRID}

    def fake_decide(
        candles: Any, *, policy: Any, rules: Any, maker_fee: Any, funds: Any, **_: Any
    ) -> Decision:
        state["calls"] += 1
        state["funds"] = funds
        state["policy"] = policy
        if state["action"] != GRID:
            return Decision("NO_TRADE", ("TREND_DOWN",), None, {}, ZERO)
        price = candles[-1].close
        plan = build_grid(
            lower=price * D("0.97"),
            upper=price * D("1.03"),
            levels=3,
            rules=rules,
            capital=capital_policy(policy, funds),
            costs=CostModel(D("0.001"), D("0.002"), D("0.0005"), D("0.0005"), D("0.002")),
        )
        state["plan"] = plan
        return Decision(GRID, ("GRID_FEASIBLE",), plan, {}, D(80))

    monkeypatch.setattr(strategy_decision, "decide", fake_decide)
    return state


def open_orders(safe: SafetyEnv) -> list[Any]:
    return [o for o in safe.fake.orders.values() if o.status == "WORKING"]


def test_the_runner_stays_idle_unless_the_mode_is_live(safe: SafetyEnv) -> None:
    safe.baseline("50")
    assert safe.recover().complete
    assert safe.act("resume").kind == "ok"
    result = make_runner(safe).tick()
    assert result == TickResult(False, "MODE_NOT_LIVE") and not safe.fake.orders


def test_the_runner_stays_idle_while_the_bot_is_paused_or_killed(safe: SafetyEnv) -> None:
    go_live(safe)
    assert safe.act("pause").kind == "ok"
    assert make_runner(safe).tick().reason == "BOT_NOT_RUNNING"
    assert not safe.fake.orders


def test_an_active_pair_that_is_not_chosen_is_not_traded(
    safe: SafetyEnv, fixed_grid: dict[str, Any], sql: Sql
) -> None:
    go_live(safe, choose=False)
    result = make_runner(safe).tick()
    assert result.ran and result.pairs == ()
    assert not safe.fake.orders and sql("SELECT count(*) AS n FROM live_grids")[0]["n"] == 0


def test_a_no_trade_decision_starts_nothing(safe: SafetyEnv, fixed_grid: dict[str, Any]) -> None:
    go_live(safe)
    fixed_grid["action"] = "NO_TRADE"
    result = make_runner(safe).tick()
    assert result.ran and result.pairs[0].action == "NO_TRADE" and not safe.fake.orders


def test_a_grid_starts_then_the_cell_below_the_price_places_a_post_only_buy(
    safe: SafetyEnv, fixed_grid: dict[str, Any], sql: Sql
) -> None:
    go_live(safe)
    runner = make_runner(safe)
    first = runner.tick()
    assert first.pairs[0].action == "GRID_STARTED"
    assert sql("SELECT state FROM live_grids")[0]["state"] == "ACTIVE"
    assert not safe.fake.orders  # starting a grid places nothing by itself
    second = runner.tick()
    assert second.pairs[0].action == "MANAGED" and second.pairs[0].placed == 2
    orders = open_orders(safe)
    assert {o.side for o in orders} == {"BUY"} and len(orders) == 2
    # the funds the strategy sized from are the venue's USDC, with nothing counted twice
    assert fixed_grid["funds"].available == D(50) and fixed_grid["funds"].committed == 0
    # a third tick adds nothing: the cells are working
    assert runner.tick().pairs[0].placed == 0 and len(open_orders(safe)) == 2


def test_a_filled_buy_becomes_a_sell_one_line_up(
    safe: SafetyEnv, fixed_grid: dict[str, Any]
) -> None:
    go_live(safe)
    runner = make_runner(safe)
    runner.tick()
    runner.tick()
    buy = min(open_orders(safe), key=lambda o: o.price)
    safe.fake.inject_fill(buy.order_id, str(buy.price), str(buy.base_qty))
    safe.fake.set_balance("USDC", str(D(50) - buy.price * buy.base_qty))
    safe.fake.set_balance("BTC", str(buy.base_qty))
    safe.clock.advance(1)
    result = runner.tick()
    sells = [o for o in safe.fake.orders.values() if o.side == "SELL"]
    assert result.pairs[0].placed == 1 and len(sells) == 1
    assert 0 < sells[0].base_qty <= buy.base_qty and sells[0].post_only
    assert sells[0].price * sells[0].base_qty <= D(12)  # the per-order cap holds for the SELL too
    plan = fixed_grid["plan"]
    assert sells[0].price >= plan.cells[0].sell_price


def test_leaving_the_band_cancels_the_buys_and_stops_the_empty_grid(
    safe: SafetyEnv, fixed_grid: dict[str, Any], sql: Sql
) -> None:
    go_live(safe)
    runner = make_runner(safe)
    runner.tick()
    runner.tick()
    ids = [o.order_id for o in open_orders(safe)]
    assert len(ids) == 2
    # a newer candle far above the band
    row = sql(
        "SELECT product_uuid, max(start_ts) AS t, max(ingest_run_id) AS r FROM candles GROUP BY 1"
    )[0]
    sql(
        "INSERT INTO candles (product_uuid, granularity, start_ts, open, high, low, close, volume, "
        "ingest_run_id) VALUES (%s, %s, %s, 120, 121, 119, 120, 10, %s)",
        (row["product_uuid"], "FIVE_MINUTE", row["t"] + 300, row["r"]),
    )
    safe.clock.advance(1)
    result = runner.tick()
    assert result.pairs[0].cancelled == 2
    assert not [o for o in safe.fake.orders.values() if o.side == "SELL"]  # never a market sell
    for order_id in ids:
        safe.fake.finish_cancel(order_id)
    safe.clock.advance(1)
    stopped = runner.tick()
    assert stopped.pairs[0].action == "GRID_STOPPED" and "BREAKOUT" in stopped.pairs[0].notes
    assert sql("SELECT state, stop_reason FROM live_grids")[0] == {
        "state": "STOPPED",
        "stop_reason": "BREAKOUT",
    }


def test_a_pair_removed_from_the_choice_has_its_grid_wound_down(
    safe: SafetyEnv, fixed_grid: dict[str, Any], sql: Sql
) -> None:
    go_live(safe)
    runner = make_runner(safe)
    runner.tick()
    runner.tick()
    ids = [o.order_id for o in open_orders(safe)]
    assert len(ids) == 2
    # the operator unticks the pair (bot PAUSED), then resumes
    assert safe.act("pause").kind == "ok"
    with safe.ctl.tx() as repos:
        repos.safety.set_trading_pairs("LIVE", [])
    safe.clock.advance(1)
    assert safe.reconciler.run("MANUAL").outcome == "OK"
    assert safe.act("resume").kind == "ok"
    safe.clock.advance(1)
    result = runner.tick()
    assert result.pairs[0].cancelled == 2  # the grid is not abandoned: its BUYs are cancelled
    assert not [o for o in safe.fake.orders.values() if o.side == "SELL"]  # never a market sell
    for order_id in ids:
        safe.fake.finish_cancel(order_id)
    safe.clock.advance(1)
    stopped = runner.tick()
    assert stopped.pairs[0].action == "GRID_STOPPED" and "PAIR_NOT_CHOSEN" in stopped.pairs[0].notes
    assert sql("SELECT state, stop_reason FROM live_grids")[0] == {
        "state": "STOPPED",
        "stop_reason": "PAIR_NOT_CHOSEN",
    }
    safe.clock.advance(1)
    assert runner.tick().pairs == () and len(safe.fake.orders) == 2  # and no new grid starts


def test_a_failed_funds_read_places_nothing_new(
    safe: SafetyEnv, fixed_grid: dict[str, Any]
) -> None:
    from app.exchange.fake import Fault

    go_live(safe)
    safe.fake.fail("list_accounts", *[Fault("timeout")] * 20)
    runner = make_runner(safe)
    result = runner.tick()
    assert result.pairs[0].action == "SKIPPED" and "FUNDS_UNAVAILABLE" in result.pairs[0].notes
    assert not safe.fake.orders


def test_the_invested_cap_limits_how_many_grids_start(
    safe: SafetyEnv, fixed_grid: dict[str, Any], sql: Sql
) -> None:
    go_live(safe)
    runner = make_runner(safe)
    runner.tick()
    plan = fixed_grid["plan"]
    assert sql("SELECT commitment FROM live_grids")[0]["commitment"] == plan.max_commitment
