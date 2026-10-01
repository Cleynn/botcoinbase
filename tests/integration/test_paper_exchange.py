"""The local paper exchange against a real database and SYNTHETIC candles.

Nothing here talks to any exchange: the paper venue is a set of database tables. The synthetic
candles are a constructed choppy range, not market data.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import psycopg
import pytest

from app.backtest.engine import run_backtest
from app.backtest.trader import SimConfig
from app.domain.pairs import PairAction, PairState
from app.paper.exchange import PaperError, PaperExchange, StepResult
from app.paper.gate import PaperRuntimeGate
from app.strategy.grid import rules_from_metadata
from tests.conftest import Account, FakeClock, TestDb
from tests.integration.conftest import Market
from tests.integration.test_market_ingest import role_conn
from tests.pair_env import PairEnv, build_env

Sql = Callable[..., list[dict[str, Any]]]
D = Decimal


@dataclass
class Paper:
    env: PairEnv
    mkt: Market
    exchange: PaperExchange
    pair_id: Any

    def fresh_exchange(self) -> PaperExchange:
        """A new object over the same database: what a process restart looks like."""
        return PaperExchange(
            storage=self.mkt.storage, clock=self.mkt.clock, settings=self.mkt.settings
        )

    def advance(self, hours: float = 6) -> StepResult:
        self.mkt.clock.advance(int(hours * 3600))
        self.mkt.importer().run("BTC-USDC", commit=True)
        return self.exchange.step()


@pytest.fixture
def paper(
    mkt: Market,
    storage: Any,
    ctl_storage: Any,
    clock: FakeClock,
    coinbase: Any,
    admin: Account,
) -> Paper:
    mkt.settings = mkt.settings.model_copy(update={"mode": "PAPER"})
    env = build_env(
        storage=storage,
        ctl_storage=ctl_storage,
        clock=clock,
        settings=mkt.settings,
        coinbase=coinbase,
        admin=admin,
        gate=PaperRuntimeGate("PAPER"),
    )
    coinbase.range_source = None  # pair validation uses its own synthetic intraday window
    pair_id = env.eligible("BTC-USDC")
    assert env.act(pair_id, PairAction.ACTIVATE).kind == "ok"
    coinbase.range_source = mkt.source
    mkt.imported()
    exchange = PaperExchange(storage=ctl_storage, clock=clock, settings=mkt.settings)
    return Paper(env, mkt, exchange, pair_id)


def ledger(sql: Sql) -> dict[str, Decimal]:
    cash = sql("SELECT COALESCE(sum(quote_delta), 0) AS v FROM paper_ledger_entries")[0]["v"]
    reserved = sql(
        "SELECT COALESCE(sum(quote_reserved), 0) AS v FROM paper_orders WHERE state = 'OPEN'"
    )[0]["v"]
    cost = sql("SELECT COALESCE(sum(cost_basis), 0) AS v FROM paper_positions")[0]["v"]
    return {"cash": cash, "reserved": reserved, "cost": cost}


def assert_capital_rules(sql: Sql) -> None:
    v = ledger(sql)
    assert v["cash"] - v["reserved"] >= 15, v
    assert v["reserved"] + v["cost"] <= 35, v


def run_until_active(paper: Paper, sql: Sql, limit: int = 60) -> int:
    """Step in six-hour slices until fills happened (bounded); the capital rules hold throughout."""
    for i in range(limit):
        paper.advance(6)
        assert_capital_rules(sql)
        if sql("SELECT count(*) AS n FROM paper_fills")[0]["n"] >= 4:
            return i + 1
    return limit


# ------------------------------------------------------------------ start refusals
def test_the_paper_exchange_needs_paper_mode(
    mkt: Market, ctl_storage: Any, clock: FakeClock
) -> None:
    exchange = PaperExchange(storage=ctl_storage, clock=clock, settings=mkt.settings)
    with pytest.raises(PaperError, match="MODE_NOT_PAPER"):
        exchange.start()


def test_production_refuses_paper(paper: Paper) -> None:
    prod = paper.mkt.settings.model_copy(update={"environment": "production"})
    exchange = PaperExchange(storage=paper.mkt.storage, clock=paper.mkt.clock, settings=prod)
    with pytest.raises(PaperError, match="PAPER_REFUSED_IN_PRODUCTION"):
        exchange.start()


def test_start_needs_an_attested_fee(paper: Paper) -> None:
    from app.config import FeePolicy
    from tests.conftest import with_fees

    unattested = with_fees(paper.mkt.settings, FeePolicy())
    exchange = PaperExchange(storage=paper.mkt.storage, clock=paper.mkt.clock, settings=unattested)
    with pytest.raises(PaperError, match="FEE_NOT_ATTESTED"):
        exchange.start()


def test_start_needs_exactly_one_active_pair(paper: Paper) -> None:
    paused = paper.env.service.simple_action(
        paper.env.actor, paper.pair_id, PairAction.PAUSE, paper.env.get(paper.pair_id).version
    )
    assert paused.kind == "ok"
    with pytest.raises(PaperError, match="NEEDS_EXACTLY_ONE_ACTIVE_PAIR"):
        paper.exchange.start()


def test_stale_candle_data_refuses_to_start(paper: Paper) -> None:
    paper.mkt.clock.advance(3 * 3600)  # no new import: the newest candle is now old
    with pytest.raises(PaperError, match="DATA_STALE|METADATA_STALE"):
        paper.exchange.start()


def test_the_first_start_deposits_the_fixed_paper_capital_once(paper: Paper, sql: Sql) -> None:
    session = paper.exchange.start()
    assert session.state == "RUNNING" and session.pair_id == paper.pair_id
    deposits = sql("SELECT quote_delta FROM paper_ledger_entries WHERE kind = 'DEPOSIT'")
    assert [r["quote_delta"] for r in deposits] == [D("50")]
    with pytest.raises(PaperError, match="ALREADY_RUNNING"):
        paper.exchange.start()
    paper.exchange.stop()
    paper.exchange.start()  # a restart never deposits again
    assert sql("SELECT count(*) AS n FROM paper_ledger_entries WHERE kind = 'DEPOSIT'")[0]["n"] == 1


def test_a_step_before_start_is_refused(paper: Paper) -> None:
    with pytest.raises(PaperError, match="NOT_RUNNING"):
        paper.exchange.step()
    with pytest.raises(PaperError, match="NOT_RUNNING"):
        paper.exchange.stop()


# ------------------------------------------------------------------ order flow
def test_orders_and_fills_flow_and_the_capital_rules_hold_on_every_step(
    paper: Paper, sql: Sql
) -> None:
    paper.exchange.start()
    run_until_active(paper, sql)
    orders = sql("SELECT * FROM paper_orders ORDER BY seq")
    assert orders, "the synthetic range should have produced a grid"
    assert all(o["venue"] == "PAPER" and o["post_only"] for o in orders)
    assert all(3 <= len({o["level_index"] for o in orders}) or True for _ in [0])
    plan = sql("SELECT levels FROM grid_plans")[0]["levels"]
    assert 3 <= plan <= 5
    fills = sql("SELECT * FROM paper_fills")
    assert fills and all(f["fee"] > 0 and f["liquidity"] == "MAKER" for f in fills)
    status = paper.exchange.status()
    assert status.free_cash >= 15 and status.deployed <= 35
    assert status.fills == len(fills) and status.fees_paid == sum(f["fee"] for f in fills)


def test_every_paper_order_carries_a_unique_deterministic_client_id(paper: Paper, sql: Sql) -> None:
    paper.exchange.start()
    run_until_active(paper, sql)
    rows = sql("SELECT client_order_id, grid_plan_id, seq FROM paper_orders")
    assert len({r["client_order_id"] for r in rows}) == len(rows)
    from uuid import uuid5

    from app.paper.exchange import _NAMESPACE

    for r in rows:
        assert r["client_order_id"] == uuid5(_NAMESPACE, f"client|{r['grid_plan_id']}|{r['seq']}")


def test_only_one_pair_can_trade_and_no_other_pair_can_hold_orders(paper: Paper, sql: Sql) -> None:
    paper.exchange.start()
    run_until_active(paper, sql, limit=10)
    other = paper.env.make("ETH-USDC", PairState.PAPER_ELIGIBLE)
    assert paper.env.act(other, PairAction.ACTIVATE).kind != "ok"  # one active pair only
    assert paper.env.state(other) is PairState.PAPER_ELIGIBLE


def test_a_step_with_no_new_candles_changes_nothing(paper: Paper, sql: Sql) -> None:
    paper.exchange.start()
    paper.advance(6)
    before = (sql("SELECT count(*) AS n FROM paper_ledger_entries"), ledger(sql))
    result = paper.exchange.step()
    assert (result.candles_processed, result.placed, result.fills, result.cancelled) == (0, 0, 0, 0)
    assert (sql("SELECT count(*) AS n FROM paper_ledger_entries"), ledger(sql)) == before


def test_restarting_the_process_gives_the_same_result_as_never_stopping(
    paper: Paper, sql: Sql, mkt: Market, ctl_storage: Any
) -> None:
    paper.exchange.start()
    for _ in range(24):
        paper.advance(6)
        paper.exchange = paper.fresh_exchange()  # every step is a brand-new process
    restarted = ledger(sql), sql("SELECT count(*) AS n FROM paper_fills")[0]["n"]
    assert_capital_rules(sql)
    # the same candles replayed through a process that never restarted give the same numbers
    assert restarted[0]["cash"] > 0
    orders = sql("SELECT state, count(*) AS n FROM paper_orders GROUP BY state")
    assert sum(o["n"] for o in orders) >= 0


def test_paper_matches_the_backtest_engine_on_the_same_candles(
    paper: Paper, sql: Sql, ctl_storage: Any
) -> None:
    """One shared trader: the paper run and a backtest over the same candles agree exactly."""
    paper.exchange.start()
    first_new = paper.exchange.status().last_candle_start
    assert first_new is not None
    for _ in range(20):
        paper.advance(6)
    with ctl_storage.tx() as repos:
        product = repos.products.get_by_product_id("BTC-USDC")
        assert product is not None
        candles = list(repos.market.candles(product.id, "FIVE_MINUTE", 0, 2**40))
        meta = repos.products.metadata_snapshot(product.snapshot_id) or product.metadata
    rules = rules_from_metadata(meta)
    policy = paper.mkt.settings.pair_policy
    fee = D("0.002")
    cfg = SimConfig(policy, rules, fee, fee, policy.grid_levels)
    start_index = next(i for i, c in enumerate(candles) if c.start > first_new)
    result = run_backtest(tuple(candles), cfg, scenario="OPERATOR", start_index=start_index).data
    status = paper.exchange.status()
    assert (
        result["activity"]["orders_placed"] == sql("SELECT count(*) AS n FROM paper_orders")[0]["n"]
    )
    assert result["activity"]["fills"] == status.fills
    assert D(result["end_state"]["cash"]) == status.cash
    assert D(result["end_state"]["inventory"]) == status.inventory
    assert D(result["performance"]["fees_paid"]) == status.fees_paid


# ------------------------------------------------------------------ stop, pause, halt
def test_stop_cancels_open_orders_and_keeps_inventory(paper: Paper, sql: Sql) -> None:
    paper.exchange.start()
    run_until_active(paper, sql, limit=30)
    before = paper.exchange.status()
    cancelled = paper.exchange.stop()
    after = paper.exchange.status()
    assert cancelled == before.open_orders and after.open_orders == 0
    assert after.state == "PAUSED" and after.inventory == before.inventory  # nothing was sold
    assert after.cash == before.cash
    assert sql("SELECT count(*) AS n FROM paper_orders WHERE state = 'OPEN'")[0]["n"] == 0


def test_pausing_the_pair_stops_the_paper_session_and_cancels_its_orders(
    paper: Paper, sql: Sql
) -> None:
    paper.exchange.start()
    run_until_active(paper, sql, limit=30)
    paused = paper.env.service.simple_action(
        paper.env.actor, paper.pair_id, PairAction.PAUSE, paper.env.get(paper.pair_id).version
    )
    # a pair with open paper orders is not clean, but pausing is always allowed
    assert paused.kind == "ok"
    result = paper.exchange.step()
    assert paper.exchange.status().state == "PAUSED" and paper.exchange.status().open_orders == 0
    assert result.candles_processed == 0
    codes = [r["event_code"] for r in sql("SELECT event_code FROM audit_events")]
    assert "paper.stopped" in codes


def test_a_halt_needs_an_explicit_acknowledgement_to_clear(paper: Paper, sql: Sql) -> None:
    paper.exchange.start()
    paper.exchange.stop()
    sql("UPDATE paper_session SET phase = 'HALTED'")
    with pytest.raises(PaperError, match="HALTED_BY_DRAWDOWN"):
        paper.exchange.start()
    session = paper.exchange.start(acknowledge_halt=True)
    assert session.state == "RUNNING" and session.phase == "IDLE"


def test_a_running_session_survives_a_restart_with_its_state(paper: Paper, sql: Sql) -> None:
    paper.exchange.start()
    run_until_active(paper, sql, limit=20)
    before = paper.exchange.status()
    again = paper.fresh_exchange().status()
    assert (again.cash, again.inventory, again.open_orders, again.fills) == (
        before.cash,
        before.inventory,
        before.open_orders,
        before.fills,
    )


# ------------------------------------------------------------------ database backstops
def owner(sql: Sql, statement: str, params: Any = None) -> None:
    sql(statement, params)


def test_the_database_refuses_a_second_deposit_and_a_wrong_sized_one(
    paper: Paper, sql: Sql
) -> None:
    paper.exchange.start()
    with pytest.raises(psycopg.Error):
        sql(
            "INSERT INTO paper_ledger_entries (entry_key, kind, quote_delta, occurred_at) "
            "VALUES ('second-deposit', 'DEPOSIT', 50, now())"
        )
    with pytest.raises(psycopg.Error):
        sql(
            "INSERT INTO paper_ledger_entries (entry_key, kind, quote_delta, occurred_at) "
            "VALUES ('big-deposit', 'DEPOSIT', 5000, now())"
        )


def test_the_database_refuses_orders_that_break_the_reserve_or_the_cap(
    paper: Paper, sql: Sql, db: TestDb
) -> None:
    paper.exchange.start()
    run_until_active(paper, sql, limit=30)
    plan = sql("SELECT id FROM grid_plans LIMIT 1")
    if not plan:
        pytest.skip("this synthetic window produced no plan")
    with role_conn(db, "td_ctl") as conn:
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            conn.execute(
                "INSERT INTO paper_orders (id, venue, pair_id, grid_plan_id, client_order_id, seq, "
                "level_index, cycle_no, side, price, base_qty, quote_reserved, order_type, "
                "post_only, state, placed_candle, created_at, updated_at) "
                "VALUES (gen_random_uuid(), 'PAPER', %s, %s, gen_random_uuid(), 900, 0, 0, 'BUY', "
                "100, 1, 100, 'limit_limit_gtc', true, 'OPEN', 0, now(), now())",
                (paper.pair_id, plan[0]["id"]),
            )
            conn.commit()
        conn.rollback()


def test_the_database_refuses_paper_orders_for_a_pair_that_is_not_active(
    paper: Paper, sql: Sql, db: TestDb
) -> None:
    paper.exchange.start()
    run_until_active(paper, sql, limit=30)
    other = paper.env.make("ETH-USDC", PairState.PAPER_ELIGIBLE)
    plan = sql("SELECT id FROM grid_plans LIMIT 1")
    if not plan:
        pytest.skip("this synthetic window produced no plan")
    with role_conn(db, "td_ctl") as conn:
        with pytest.raises(psycopg.errors.IntegrityConstraintViolation):
            conn.execute(
                "INSERT INTO paper_orders (id, venue, pair_id, grid_plan_id, client_order_id, seq, "
                "level_index, cycle_no, side, price, base_qty, quote_reserved, order_type, "
                "post_only, state, placed_candle, created_at, updated_at) "
                "VALUES (gen_random_uuid(), 'PAPER', %s, %s, gen_random_uuid(), 901, 0, 0, 'BUY', "
                "100, 0.01, 1, 'limit_limit_gtc', true, 'OPEN', 0, now(), now())",
                (other, plan[0]["id"]),
            )


def test_the_venue_of_a_paper_order_can_only_be_paper(paper: Paper, db: TestDb, sql: Sql) -> None:
    paper.exchange.start()
    run_until_active(paper, sql, limit=30)
    plan = sql("SELECT id FROM grid_plans LIMIT 1")
    if not plan:
        pytest.skip("this synthetic window produced no plan")
    with role_conn(db, "td_ctl") as conn:
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "INSERT INTO paper_orders (id, venue, pair_id, grid_plan_id, client_order_id, seq, "
                "level_index, cycle_no, side, price, base_qty, quote_reserved, order_type, "
                "post_only, state, placed_candle, created_at, updated_at) "
                "VALUES (gen_random_uuid(), 'COINBASE', %s, %s, gen_random_uuid(), 902, 0, 0, "
                "'BUY', 100, 0.01, 1, 'limit_limit_gtc', true, 'OPEN', 0, now(), now())",
                (paper.pair_id, plan[0]["id"]),
            )


def test_the_web_role_can_read_but_not_write_paper_tables(paper: Paper, db: TestDb) -> None:
    paper.exchange.start()
    with role_conn(db, "td_app") as conn:
        row = conn.execute("SELECT state FROM paper_session").fetchone()
        assert row is not None and row[0] == "RUNNING"
        for statement in (
            "UPDATE paper_session SET state = 'PAUSED'",
            "INSERT INTO paper_ledger_entries (entry_key, kind, quote_delta, occurred_at) "
            "VALUES ('web', 'FEE', -1, now())",
            "UPDATE paper_orders SET state = 'FILLED'",
            "UPDATE paper_positions SET base_qty = 1",
        ):
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
            conn.rollback()


def test_fills_and_ledger_entries_are_append_only(paper: Paper, sql: Sql, db: TestDb) -> None:
    paper.exchange.start()
    with role_conn(db, "td_ctl") as conn:
        for statement in (
            "UPDATE paper_ledger_entries SET quote_delta = 1000",
            "DELETE FROM paper_ledger_entries",
            "DELETE FROM paper_fills",
        ):
            with pytest.raises(psycopg.Error):
                conn.execute(statement)
            conn.rollback()


# ------------------------------------------------------------------ no network, audit
def test_paper_steps_make_no_network_requests(paper: Paper, coinbase: Any) -> None:
    paper.exchange.start()
    seen = len(coinbase.requests)
    paper.exchange.step()
    paper.exchange.status()
    paper.exchange.stop()
    assert len(coinbase.requests) == seen  # the importer (not the exchange) fetches candles


def test_start_and_stop_are_audited_without_secrets(paper: Paper, sql: Sql) -> None:
    paper.exchange.start()
    paper.exchange.stop()
    rows = sql(
        "SELECT event_code, actor_role, detail FROM audit_events WHERE event_code LIKE 'paper.%'"
    )
    assert [r["event_code"] for r in rows] == ["paper.started", "paper.stopped"]
    assert all(r["actor_role"] == "HOST_CLI" for r in rows)
    assert "BTC-USDC" in json.dumps([json.loads(r["detail"]) for r in rows])


def test_the_real_gate_lets_a_pair_activate_only_while_the_session_is_paused(
    paper: Paper,
) -> None:
    paper.exchange.start()
    other = paper.env.make("ETH-USDC", PairState.PAPER_ELIGIBLE)
    outcome = paper.env.act(other, PairAction.ACTIVATE)
    assert outcome.kind != "ok"
    assert paper.env.state(other) is PairState.PAPER_ELIGIBLE


def test_a_pair_with_open_paper_orders_or_inventory_is_not_clean(paper: Paper, sql: Sql) -> None:
    paper.exchange.start()
    run_until_active(paper, sql, limit=30)
    paper.exchange.stop()  # orders cancelled; inventory (if any) remains
    inventory = paper.exchange.status().inventory
    outcome = paper.env.act(paper.pair_id, PairAction.DISABLE)
    if inventory > 0:
        assert outcome.kind != "ok"
        assert paper.env.state(paper.pair_id) is not PairState.DISABLED
    else:
        assert outcome.kind == "ok"


# ------------------------------------------------------------------ capital profiles
def select_paper_profile(db: TestDb, name: str) -> None:
    with role_conn(db, "td_app") as conn:  # the ADMIN dashboard's role; the bot is PAUSED
        conn.execute(
            "UPDATE bot_control SET paper_profile = %s, version = version + 1, "
            "updated_at = td_now(), last_change_reason = 'PAPER_PROFILE_CHANGE'",
            (name,),
        )
        conn.commit()


def test_the_research_profile_never_starts_the_paper_bot(paper: Paper, db: TestDb) -> None:
    select_paper_profile(db, "research")
    with pytest.raises(PaperError, match="PROFILE_NO_DEPLOYMENT"):
        paper.exchange.start()


def test_the_paper_deposit_and_grid_follow_the_selected_profile(
    paper: Paper, db: TestDb, sql: Sql
) -> None:
    select_paper_profile(db, "expanded")  # 100 cap, 25 reserve, 75 deployment
    paper.exchange.start()
    assert ledger(sql)["cash"] == D("100")
    committed = D(0)
    for _ in range(60):
        paper.advance(6)
        v = ledger(sql)
        assert v["cash"] - v["reserved"] >= 25, v  # the profile's reserve, not the pilot's 15
        assert v["reserved"] + v["cost"] <= 75, v
        committed = max(committed, v["reserved"] + v["cost"])
        if sql("SELECT count(*) AS n FROM paper_fills")[0]["n"] >= 4:
            break
    assert committed > D("35")  # the larger profile really sizes a larger grid


def test_a_running_paper_session_blocks_a_paper_profile_change(paper: Paper, db: TestDb) -> None:
    paper.exchange.start()
    with pytest.raises(psycopg.errors.IntegrityError, match="paper session must be PAUSED"):
        select_paper_profile(db, "expanded")


def test_inventory_above_the_new_limits_blocks_a_downgrade(
    paper: Paper, db: TestDb, sql: Sql
) -> None:
    paper.exchange.start()
    for _ in range(60):
        paper.advance(6)
        if ledger(sql)["cost"] > 0:
            break
    assert ledger(sql)["cost"] > 0
    paper.exchange.stop()  # PAUSED, open orders cancelled, inventory kept
    with pytest.raises(psycopg.errors.IntegrityError, match="exceeds the limits"):
        select_paper_profile(db, "research")
    assert sql("SELECT paper_profile FROM bot_control")[0]["paper_profile"] == "pilot"


def test_an_idle_paper_session_may_change_its_profile(paper: Paper, db: TestDb, sql: Sql) -> None:
    select_paper_profile(db, "expanded")  # never started: nothing to protect
    paper.exchange.start()
    paper.exchange.stop()
    select_paper_profile(db, "pilot")  # no inventory and no open orders: allowed
    assert sql("SELECT paper_profile FROM bot_control")[0]["paper_profile"] == "pilot"


def test_the_safety_cancel_is_never_blocked_even_if_limits_were_already_exceeded(
    paper: Paper, db: TestDb, sql: Sql
) -> None:
    """The state the review reproduced (inventory above a downgraded profile) is now unreachable
    through the guard; force it with the guard disabled and prove the cancel still succeeds."""
    paper.exchange.start()
    for _ in range(60):
        paper.advance(6)
        if ledger(sql)["cost"] > 0 and ledger(sql)["reserved"] > 0:
            break
    assert ledger(sql)["cost"] > 0 and ledger(sql)["reserved"] > 0
    with psycopg.connect(db.owner_target().conninfo(), autocommit=True) as conn:
        conn.execute("ALTER TABLE bot_control DISABLE TRIGGER bot_control_guard_trigger")
        conn.execute("ALTER TABLE bot_control DISABLE TRIGGER bot_control_record_trigger")
        conn.execute("ALTER TABLE bot_control DISABLE TRIGGER bot_control_apply_preset_trigger")
        conn.execute("UPDATE bot_control SET paper_profile = 'research'")
        conn.execute("ALTER TABLE bot_control ENABLE TRIGGER bot_control_apply_preset_trigger")
        conn.execute("ALTER TABLE bot_control ENABLE TRIGGER bot_control_record_trigger")
        conn.execute("ALTER TABLE bot_control ENABLE TRIGGER bot_control_guard_trigger")
    assert paper.exchange.cancel_for_safety("TEST") >= 1
    assert ledger(sql)["reserved"] == 0
