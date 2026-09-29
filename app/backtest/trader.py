"""The grid trader: one pure state machine used by BOTH the backtest and the paper exchange.

`Trader.on_candle` takes one closed candle and returns the effects it caused. It does no I/O. The
backtest applies it to an in-memory state; the paper exchange rebuilds the same state from the
database, runs the same code and persists the effects. Same rules, same numbers.

Simulation is deliberately pessimistic:
- an order is live only from the candle AFTER the one it was placed on (no same-candle fills);
- a buy fills only if the candle's low trades THROUGH the limit by `adverse_bps` (a touch is not a
  fill); a sell only if the high does;
- fills are capped by a fraction of the candle's volume shared by all orders (partial fills, and no
  fill at all when volume is thin);
- post-only orders that would cross the assumed spread are rejected, not filled;
- fees are charged on every fill, rounded up; the buy reserve includes a stress-fee allowance so a
  fill never spends more than was reserved;
- after a data gap wider than `max_gap_candles`, and for orders older than `max_order_age_candles`,
  all open orders are cancelled as stale;
- a breakout of the band stops the grid (buys cancelled, holdings kept, never sold at market);
- a drawdown beyond `drawdown_stop_ratio` of total capital halts trading.

Invariants checked after every candle (violation raises): free cash >= reserve, deployed <= cap,
3 to 5 levels, inventory and cost never negative.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Final

from app import constants
from app.config import PairPolicy
from app.domain.money import BPS, ONE, ZERO, fee_for, quantize_down, quantize_up
from app.market.candles import Candle
from app.strategy import decision as strat
from app.strategy.grid import GridPlan, MarketRules

STEP: Final = 300
IDLE: Final = "IDLE"
ACTIVE: Final = "ACTIVE"
STOPPED: Final = "STOPPED"
HALTED: Final = "HALTED"
EVAL_EVERY: Final = 12  # candles between decisions while flat


class InvariantViolation(Exception):
    """A capital or state invariant broke. Never caught: the run stops."""


@dataclass
class Order:
    seq: int
    cell: int
    side: str  # BUY | SELL
    price: Decimal
    qty: Decimal
    placed_start: int
    reserve: Decimal = ZERO
    filled: Decimal = ZERO
    state: str = "OPEN"  # OPEN | FILLED | CANCELLED

    @property
    def remaining(self) -> Decimal:
        return self.qty - self.filled


@dataclass(frozen=True)
class Effect:
    kind: str  # PLACE | FILL | CANCEL | REJECT | PLAN | PHASE | DECISION
    order: Order | None = None
    candle_start: int = 0
    qty: Decimal = ZERO
    price: Decimal = ZERO
    notional: Decimal = ZERO
    fee: Decimal = ZERO
    reason: str = ""
    realized: Decimal = ZERO
    plan: GridPlan | None = None


@dataclass
class Counters:
    decisions_grid: int = 0
    decisions_no_trade: int = 0
    no_trade_reasons: dict[str, int] = field(default_factory=dict)
    orders_placed: int = 0
    fills: int = 0
    partial_fills: int = 0
    touched_no_fill: int = 0
    stale_age_cancels: int = 0
    stale_gap_cancels: int = 0
    post_only_rejects: int = 0
    capital_blocked: int = 0
    cycles: int = 0
    breakout_stops: int = 0
    halts: int = 0
    gap_events: int = 0
    candles: int = 0
    candles_in_market: int = 0
    fees_paid: Decimal = ZERO
    realized: Decimal = ZERO
    max_drawdown: Decimal = ZERO
    deployed_sum: Decimal = ZERO


@dataclass
class TraderState:
    cash: Decimal
    inventory: Decimal = ZERO
    cost_basis: Decimal = ZERO
    phase: str = IDLE
    peak_equity: Decimal = ZERO
    plan: GridPlan | None = None
    orders: list[Order] = field(default_factory=list)  # OPEN orders only
    pending: dict[int, Decimal] = field(default_factory=dict)  # bought, not yet covered by a sell
    next_seq: int = 1
    last_start: int | None = None
    counters: Counters = field(default_factory=Counters)

    def reserved(self) -> Decimal:
        return sum((o.reserve for o in self.orders if o.side == "BUY"), ZERO)

    def free_cash(self) -> Decimal:
        return self.cash - self.reserved()

    def deployed(self) -> Decimal:
        return self.reserved() + self.cost_basis

    def equity(self, close: Decimal) -> Decimal:
        return self.cash + self.inventory * close


@dataclass(frozen=True)
class SimConfig:
    policy: PairPolicy
    rules: MarketRules
    sim_fee: Decimal  # fee charged in the simulation (operator or stress scenario)
    decision_fee: Decimal  # operator maker fee used by the strategy's feasibility test
    levels: int

    def check(self) -> None:
        if not constants.GRID_MIN_LEVELS <= self.levels <= constants.GRID_MAX_LEVELS:
            raise InvariantViolation("levels outside 3..5")
        if self.policy.capital_growth_enabled or self.policy.regridding_enabled:
            raise InvariantViolation("growth and regridding must stay disabled")


class Trader:
    def __init__(
        self, cfg: SimConfig, state: TraderState, window: Callable[[int], Sequence[Candle]]
    ) -> None:
        cfg.check()
        self.cfg, self.state, self._window = cfg, state, window
        self._bt = cfg.policy.backtest
        self._adverse = self._bt.adverse_bps / BPS
        self._half_spread = cfg.policy.strategy.assumed_spread_bps / BPS / 2

    # ------------------------------------------------------------------ one candle
    def on_candle(self, candle: Candle) -> list[Effect]:
        s, effects = self.state, []
        s.counters.candles += 1
        if (
            s.last_start is not None
            and candle.start - s.last_start > self._bt.max_gap_candles * STEP
        ):
            s.counters.gap_events += 1
            effects += self._cancel_all("STALE_GAP", candle.start)
        max_age = self._bt.max_order_age_candles * STEP
        for order in [o for o in s.orders if candle.start - o.placed_start > max_age]:
            effects += self._cancel(order, "STALE_AGE", candle.start)
        effects += self._fills(candle)
        effects += self._risk(candle)
        if s.phase == ACTIVE and s.plan is not None:
            effects += self._breakout(candle)
        if s.phase in (IDLE, STOPPED):
            effects += self._maybe_activate(candle)
        if s.phase == ACTIVE and s.plan is not None:
            effects += self._place(candle)
        s.last_start = candle.start
        if s.inventory > 0:
            s.counters.candles_in_market += 1
        s.counters.deployed_sum += s.deployed()
        self.check_invariants()
        return effects

    # ------------------------------------------------------------------ fills
    def _fills(self, candle: Candle) -> list[Effect]:
        s, effects = self.state, []
        pool = self._bt.fill_volume_participation * candle.volume
        live = [o for o in s.orders if o.placed_start < candle.start]
        live.sort(
            key=lambda o: (
                0 if o.side == "BUY" else 1,
                -o.price if o.side == "BUY" else o.price,
                o.seq,
            )
        )
        for order in live:
            if order.side == "BUY":
                touched = candle.low <= order.price
                through = candle.low <= order.price * (ONE - self._adverse)
            else:
                touched = candle.high >= order.price
                through = candle.high >= order.price * (ONE + self._adverse)
            if not through:
                if touched:
                    s.counters.touched_no_fill += 1
                continue
            qty = min(order.remaining, quantize_down(pool, self.cfg.rules.base_increment))
            if qty <= 0:
                s.counters.touched_no_fill += 1
                continue
            pool -= qty
            effects.append(self._apply_fill(order, qty, candle.start))
        return effects

    def _apply_fill(self, order: Order, qty: Decimal, start: int) -> Effect:
        s, r = self.state, self.cfg.rules
        notional = qty * order.price
        fee = fee_for(notional, self.cfg.sim_fee, r.quote_increment)
        s.counters.fills += 1
        s.counters.fees_paid += fee
        if qty < order.remaining:
            s.counters.partial_fills += 1
        realized = ZERO
        if order.side == "BUY":
            cost = notional + fee
            s.cash -= cost
            s.inventory += qty
            s.cost_basis += cost
            spend = min(order.reserve, cost)
            order.reserve -= spend
            s.pending[order.cell] = s.pending.get(order.cell, ZERO) + qty
        else:
            proceeds = notional - fee
            cost_out = quantize_up(s.cost_basis * qty / s.inventory, r.quote_increment)
            cost_out = min(cost_out, s.cost_basis)
            s.cash += proceeds
            s.inventory -= qty
            s.cost_basis = ZERO if s.inventory == 0 else s.cost_basis - cost_out
            realized = proceeds - cost_out
            s.counters.realized += realized
        order.filled += qty
        if order.remaining == 0:
            order.state = "FILLED"
            order.reserve = ZERO  # whatever headroom was not spent is released
            s.orders.remove(order)
            if order.side == "SELL":
                s.counters.cycles += 1
        return Effect("FILL", order, start, qty, order.price, notional, fee, realized=realized)

    # ------------------------------------------------------------------ cancels
    def _cancel(self, order: Order, reason: str, start: int) -> list[Effect]:
        s = self.state
        if order.state != "OPEN":
            return []
        order.state = "CANCELLED"
        s.orders.remove(order)
        if order.side == "SELL":  # the unsold quantity needs a new sell
            s.pending[order.cell] = s.pending.get(order.cell, ZERO) + order.remaining
        order.reserve = ZERO
        if reason == "STALE_AGE":
            s.counters.stale_age_cancels += 1
        elif reason == "STALE_GAP":
            s.counters.stale_gap_cancels += 1
        return [Effect("CANCEL", order, start, reason=reason)]

    def _cancel_all(self, reason: str, start: int, side: str | None = None) -> list[Effect]:
        out: list[Effect] = []
        for order in [o for o in self.state.orders if side is None or o.side == side]:
            out += self._cancel(order, reason, start)
        return out

    # ------------------------------------------------------------------ risk, breakout, activation
    def _risk(self, candle: Candle) -> list[Effect]:
        s, cap = self.state, self.cfg.policy.total_capital
        equity = s.equity(candle.close)
        s.peak_equity = max(s.peak_equity, equity)
        drawdown = (s.peak_equity - equity) / cap
        s.counters.max_drawdown = max(s.counters.max_drawdown, drawdown)
        if s.phase != HALTED and drawdown >= self._bt.drawdown_stop_ratio:
            s.phase = HALTED
            s.counters.halts += 1
            return [
                *self._cancel_all("DRAWDOWN_HALT", candle.start, "BUY"),
                Effect("PHASE", reason="HALTED", candle_start=candle.start),
            ]
        return []

    def _breakout(self, candle: Candle) -> list[Effect]:
        s, plan = self.state, self.state.plan
        if plan is None:
            return []
        buffer = self.cfg.policy.strategy.breakout_buffer
        if candle.close < plan.lower * (ONE - buffer) or candle.close > plan.upper * (ONE + buffer):
            s.phase = STOPPED
            s.counters.breakout_stops += 1
            return [
                *self._cancel_all("BREAKOUT", candle.start, "BUY"),
                Effect("PHASE", reason="STOPPED", candle_start=candle.start),
            ]
        return []

    def _flat(self) -> bool:
        s = self.state
        return not s.orders and s.inventory == 0 and not any(v > 0 for v in s.pending.values())

    def _maybe_activate(self, candle: Candle) -> list[Effect]:
        s = self.state
        if not self._flat() or candle.start % (EVAL_EVERY * STEP) != 0:
            return []
        need = strat.lookback_needed(self.cfg.policy)
        history = self._window(need)
        d = strat.decide(
            history,
            policy=self.cfg.policy,
            rules=self.cfg.rules,
            maker_fee=self.cfg.decision_fee,
            levels=self.cfg.levels,
        )
        if d.action == strat.GRID and d.plan is not None:
            s.counters.decisions_grid += 1
            s.phase, s.plan, s.pending = ACTIVE, d.plan, {}
            return [Effect("PLAN", plan=d.plan, candle_start=candle.start)]
        s.counters.decisions_no_trade += 1
        for reason in d.reasons:
            s.counters.no_trade_reasons[reason] = s.counters.no_trade_reasons.get(reason, 0) + 1
        if s.phase == STOPPED:
            s.phase = IDLE
        return [Effect("DECISION", reason=",".join(d.reasons), candle_start=candle.start)]

    # ------------------------------------------------------------------ placement
    def _place(self, candle: Candle) -> list[Effect]:
        s, plan, r = self.state, self.state.plan, self.cfg.rules
        if plan is None:
            return []
        effects: list[Effect] = []
        sell_floor = quantize_up(candle.close * (ONE + self._half_spread), r.price_increment)
        buy_ceiling = quantize_down(candle.close * (ONE - self._half_spread), r.price_increment)
        for cell in plan.cells:  # sells for bought quantity first
            pend = s.pending.get(cell.index, ZERO)
            if pend < r.base_min_size:
                continue
            qty = quantize_down(pend, r.base_increment)
            if qty < r.base_min_size:
                continue
            if r.quote_min_size is not None and qty * cell.sell_price < r.quote_min_size:
                continue
            if cell.sell_price < sell_floor:
                s.counters.post_only_rejects += 1
                effects.append(
                    Effect("REJECT", reason="POST_ONLY_WOULD_CROSS", candle_start=candle.start)
                )
                continue
            order = self._new(cell.index, "SELL", cell.sell_price, qty, candle.start, ZERO)
            s.pending[cell.index] = pend - qty
            effects.append(Effect("PLACE", order, candle.start, qty, cell.sell_price))
        for cell in sorted(plan.cells, key=lambda c: -c.buy_price):
            busy = any(o.cell == cell.index for o in s.orders)
            if busy or s.pending.get(cell.index, ZERO) > 0 or s.phase != ACTIVE:
                continue
            if cell.buy_price > buy_ceiling:
                s.counters.post_only_rejects += 1
                continue
            cap = self.cfg.policy.max_deployment
            floor = self.cfg.policy.min_reserve
            if s.free_cash() - cell.reserve < floor or s.deployed() + cell.reserve > cap:
                s.counters.capital_blocked += 1
                effects.append(Effect("REJECT", reason="CAPITAL_LIMIT", candle_start=candle.start))
                continue
            order = self._new(
                cell.index, "BUY", cell.buy_price, cell.base_qty, candle.start, cell.reserve
            )
            effects.append(Effect("PLACE", order, candle.start, cell.base_qty, cell.buy_price))
        return effects

    def _new(
        self, cell: int, side: str, price: Decimal, qty: Decimal, start: int, reserve: Decimal
    ) -> Order:
        s = self.state
        order = Order(s.next_seq, cell, side, price, qty, start, reserve)
        s.next_seq += 1
        s.orders.append(order)
        s.counters.orders_placed += 1
        return order

    # ------------------------------------------------------------------ invariants
    def check_invariants(self) -> None:
        s, p = self.state, self.cfg.policy
        if s.inventory < 0 or s.cost_basis < 0 or (s.inventory == 0 and s.cost_basis != 0):
            raise InvariantViolation("inventory or cost basis invalid")
        if s.deployed() > p.max_deployment:
            raise InvariantViolation("deployment cap breached")
        if s.free_cash() < p.min_reserve:
            raise InvariantViolation("protected reserve breached")
        if any(o.remaining <= 0 or o.reserve < 0 for o in s.orders):
            raise InvariantViolation("order state invalid")
        if len({o.seq for o in s.orders}) != len(s.orders):
            raise InvariantViolation("duplicate order")
        if (
            s.plan is not None
            and not constants.GRID_MIN_LEVELS <= s.plan.levels <= constants.GRID_MAX_LEVELS
        ):
            raise InvariantViolation("levels outside 3..5")


def initial_state(policy: PairPolicy) -> TraderState:
    return TraderState(cash=policy.total_capital, peak_equity=policy.total_capital)
