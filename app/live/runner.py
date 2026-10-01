"""The live grid runner: automated position making on up to N pairs in parallel.

One `tick` does, in order: check the gates, reconcile, then for each active pair refresh market
data, start a grid when the strategy says GRID (sized from the USDC the venue reports), and keep
every cell of the active grid working:

* an idle cell places a post-only BUY at its line while the price is above that line and inside
  the band;
* a bought cell places a post-only SELL one line up for what it holds;
* a sold cell is idle again (the cycle repeats);
* the price leaving the band cancels the open BUYs (never a market sell) and, once nothing is open
  or held, stops the grid. Holdings keep their SELL orders.

Every order goes through `OrderPipeline` (intent -> risk decision -> database authorization ->
gateway), so the kill switch, the protected reserve, the caps and the fund checks apply to each one
exactly as everywhere else. The runner holds no state of its own: the grid plan is a database row
and the progress of every cell is derived from its intents and attempts, so a restart resumes where
it stopped. Anything uncertain (an UNKNOWN attempt, a failed read) means "do nothing this tick".
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal
from typing import Any, Final
from uuid import UUID, uuid4

from app.auth.audit import HOST_CLI_ACTOR, AuditWriter
from app.capital.funds import BalanceLike, Funds
from app.capital.trading import TradingConfig, problems
from app.config import Settings
from app.domain.enums import AuditEventType as Evt
from app.domain.enums import AuditResult as Res
from app.domain.models import Clock
from app.domain.money import ZERO, quantize_down, quantize_up
from app.domain.pairs import PairState
from app.exchange.errors import ExchangeError
from app.exchange.gateway import ExecutionGateway
from app.market.candles import Candle
from app.market.ingest import GRANULARITY, STEP
from app.pairs.runner import HOST_ACTOR
from app.safety.context import BookFacts
from app.safety.pipeline import OrderPipeline
from app.safety.types import OrderProposal
from app.storage.database import Storage
from app.storage.repositories import Repos
from app.storage.safety_repositories import (
    LIVE_ATTEMPT_STATES,
    AttemptRow,
    IntentRow,
    LiveGridRow,
)
from app.strategy import decision as strat
from app.strategy.grid import GridRejected, MarketRules, rules_from_metadata

GRID_SOURCE: Final = "live_grid_"  # + one letter per cell (a..t): the database allows letters only


def cell_source(index: int) -> str:
    return GRID_SOURCE + chr(ord("a") + index)


FRESH_INTENT: Final = timedelta(seconds=60)  # an intent without attempts is retried only this long
REJECT_COOLDOWN: Final = timedelta(minutes=15)  # after the exchange refused an order
BLOCK_COOLDOWN: Final = timedelta(minutes=5)  # after a risk block (no attempt was made)
MAX_STALE_SECONDS_FACTOR: Final = 3  # candles older than this many steps are "stale data"


@dataclass(frozen=True)
class PairOutcome:
    product_id: str
    action: str  # GRID_STARTED | MANAGED | NO_TRADE | SKIPPED | GRID_STOPPED
    placed: int = 0
    cancelled: int = 0
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class TickResult:
    ran: bool
    reason: str | None = None
    pairs: tuple[PairOutcome, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class CellView:
    index: int
    buy: Decimal
    sell: Decimal
    qty: Decimal
    buys: tuple[tuple[IntentRow, AttemptRow | None], ...]
    sells: tuple[tuple[IntentRow, AttemptRow | None], ...]

    @property
    def pending(self) -> bool:
        """Something in flight: an open order, or an attempt whose outcome is not known yet."""
        return any(
            a is not None and a.state in LIVE_ATTEMPT_STATES for _, a in (*self.buys, *self.sells)
        )

    @property
    def held(self) -> Decimal:
        bought = sum((a.filled_qty for _, a in self.buys if a is not None), ZERO)
        sold = sum((a.filled_qty for _, a in self.sells if a is not None), ZERO)
        return bought - sold


def _latest(attempts: list[AttemptRow]) -> AttemptRow | None:
    return attempts[-1] if attempts else None


class LiveRunner:
    def __init__(
        self,
        *,
        storage: Storage,
        clock: Clock,
        settings: Settings,
        pipeline: OrderPipeline,
        gateway: ExecutionGateway,
        list_accounts: Callable[[], Iterable[BalanceLike]],
        reconcile: Callable[[str], object] | None = None,
        refresh: Callable[[str], None] | None = None,
        book: Callable[[str], BookFacts | None] = lambda _p: None,
    ) -> None:
        self._storage, self._clock, self._settings = storage, clock, settings
        self._pipeline, self._gateway = pipeline, gateway
        self._list_accounts, self._reconcile = list_accounts, reconcile
        self._refresh, self._book = refresh, book
        self._audit = AuditWriter(clock)
        self._venue = gateway.venue
        self._per_order_cap = ZERO  # set from the LIVE configuration at the start of every tick

    # ------------------------------------------------------------------ gates
    def _gate(self, repos: Repos) -> str | None:
        now = self._clock.now()
        mode, _ = repos.safety.trading_state()
        if mode != "LIVE":
            return "MODE_NOT_LIVE"
        control = repos.safety.control()
        if control.kill_switch == "ACTIVE":
            return "KILL_SWITCH_ACTIVE"
        if control.bot_state != "RUNNING":
            return "BOT_NOT_RUNNING"
        if self._venue == "COINBASE" and not repos.safety.live_armed(now):
            return "LIVE_NOT_ARMED"
        return None

    # ------------------------------------------------------------------ one tick
    def tick(self) -> TickResult:
        with self._storage.tx() as repos:
            refused = self._gate(repos)
            cfg = repos.safety.trading_config("LIVE")
        if refused:
            return TickResult(False, refused)
        bad = problems(cfg)
        if bad:
            return TickResult(False, "CONFIG_INVALID:" + ",".join(bad))
        self._per_order_cap = cfg.per_order_cap
        if self._reconcile is not None:
            self._reconcile("SCHEDULED")
        try:
            fee = strat.operator_fee(self._settings.pair_policy, self._clock.now().date())
        except strat.FeeNotAttested as exc:
            return TickResult(False, exc.code)
        accounts = self._read_accounts()
        with self._storage.tx() as repos:
            pairs = [
                p
                for p in repos.pairs.in_states((PairState.PAPER_ACTIVE,))
                if p.state is PairState.PAPER_ACTIVE
            ][: cfg.max_pairs]
        outcomes: list[PairOutcome] = []
        for pair in pairs:
            outcomes.append(self._pair(pair.id, pair.product_id, cfg, fee, accounts))
        return TickResult(True, None, tuple(outcomes))

    def _read_accounts(self) -> tuple[BalanceLike, ...] | None:
        try:
            return tuple(self._list_accounts())
        except Exception:  # noqa: BLE001  (unknown funds: no new grid, no new BUY this tick)
            return None

    # ------------------------------------------------------------------ one pair
    def _pair(
        self,
        pair_id: UUID,
        product_id: str,
        cfg: TradingConfig,
        fee: Decimal,
        accounts: tuple[BalanceLike, ...] | None,
    ) -> PairOutcome:
        notes: list[str] = []
        if self._refresh is not None:
            try:
                self._refresh(product_id)
            except Exception:  # noqa: BLE001
                notes.append("REFRESH_FAILED")  # stale data blocks orders further down
        now = self._clock.now()
        with self._storage.tx() as repos:
            product = repos.products.get_by_product_id(product_id)
            if product is None:
                return PairOutcome(product_id, "SKIPPED", notes=("PRODUCT_MISSING",))
            newest = repos.market.newest_candle_start(product.id, GRANULARITY)
            policy = self._settings.pair_policy
            need = strat.lookback_needed(policy)
            candles: list[Candle] = []
            if newest is not None:
                candles = list(
                    repos.market.candles(
                        product.id, GRANULARITY, newest - need * 2 * STEP, newest + STEP
                    )
                )
            grids = repos.safety.live_grids(pair_id=pair_id)
            committed = sum(
                (g.commitment for g in repos.safety.live_grids() if g.pair_id != pair_id), ZERO
            )
            metadata = product.metadata
        if not candles or newest is None:
            return PairOutcome(product_id, "SKIPPED", notes=(*notes, "NO_CANDLES"))
        if now.timestamp() - (newest + STEP) > STEP * MAX_STALE_SECONDS_FACTOR:
            return PairOutcome(product_id, "SKIPPED", notes=(*notes, "STALE_DATA"))
        try:
            rules = rules_from_metadata(metadata)
        except GridRejected as exc:
            return PairOutcome(product_id, "SKIPPED", notes=(*notes, exc.code))
        price = candles[-1].close
        if not grids:
            return self._start(
                pair_id, product_id, cfg, fee, accounts, candles, rules, committed, tuple(notes)
            )
        return self._manage(pair_id, product_id, grids[-1], rules, price, accounts, tuple(notes))

    # ------------------------------------------------------------------ start a grid
    def _start(
        self,
        pair_id: UUID,
        product_id: str,
        cfg: TradingConfig,
        fee: Decimal,
        accounts: tuple[BalanceLike, ...] | None,
        candles: list[Candle],
        rules: MarketRules,
        committed: Decimal,
        notes: tuple[str, ...],
    ) -> PairOutcome:
        if accounts is None:
            return PairOutcome(product_id, "SKIPPED", notes=(*notes, "FUNDS_UNAVAILABLE"))
        usdc = [a for a in accounts if a.currency == "USDC"]
        if len(usdc) != 1:
            return PairOutcome(product_id, "SKIPPED", notes=(*notes, "FUNDS_UNAVAILABLE"))
        room = cfg.invested_cap - committed
        per_grid = min(cfg.quote_per_grid, room)
        if per_grid <= 0:
            return PairOutcome(product_id, "NO_TRADE", notes=(*notes, "INVESTED_CAP_REACHED"))
        sized = TradingConfig(
            cfg.mode,
            cfg.max_pairs,
            cfg.levels_per_grid,
            per_grid,
            cfg.invested_cap,
            cfg.reserve,
            min(cfg.per_order_cap, per_grid),
            cfg.version,
        )
        policy = self._settings.pair_policy.for_trading(sized)
        # `available` is already net of the holds on open orders, so nothing is committed twice
        funds = Funds(usdc[0].available, ZERO, ZERO, "EXCHANGE", self._clock.now())
        decided = strat.decide(candles, policy=policy, rules=rules, maker_fee=fee, funds=funds)
        if decided.action != strat.GRID or decided.plan is None:
            return PairOutcome(product_id, "NO_TRADE", notes=(*notes, *decided.reasons))
        plan = decided.plan
        if len({c.sell_price for c in plan.cells}) != len(plan.cells):
            return PairOutcome(product_id, "NO_TRADE", notes=(*notes, "PRICE_STEP_TOO_COARSE"))
        row = LiveGridRow(
            id=uuid4(),
            pair_id=pair_id,
            product_id=product_id,
            state="ACTIVE",
            levels=plan.levels,
            lower=plan.lower,
            upper=plan.upper,
            cells=[
                {
                    "index": c.index,
                    "buy": format(c.buy_price, "f"),
                    "sell": format(c.sell_price, "f"),
                    "qty": format(c.base_qty, "f"),
                    "ret": format(c.gross_return, "f"),
                }
                for c in plan.cells
            ],
            commitment=plan.max_commitment,
            created_at=self._clock.now(),
        )
        with self._storage.tx() as repos:
            repos.safety.add_live_grid(row)
            self._audit.record(
                repos,
                Evt.LIVE_GRID_STARTED,
                Res.SUCCESS,
                actor=HOST_CLI_ACTOR,
                target_type="pair",
                target_id=pair_id,
                client_tag=HOST_ACTOR.client_tag,
                request_id=HOST_ACTOR.request_id,
                detail={"levels": plan.levels, "commitment": format(plan.max_commitment, "f")},
            )
        return PairOutcome(product_id, "GRID_STARTED", notes=notes)

    # ------------------------------------------------------------------ manage the active grid
    def _cells(self, repos: Repos, grid: LiveGridRow) -> list[CellView]:
        intents = repos.safety.intents_for_pair(grid.pair_id, GRID_SOURCE, grid.created_at)
        views: list[CellView] = []
        for c in grid.cells:
            index = int(c["index"])
            mine = [
                (i, _latest(repos.safety.attempts_for_intent(i.id)))
                for i in intents
                if i.source == cell_source(index)
            ]
            views.append(
                CellView(
                    index,
                    Decimal(c["buy"]),
                    Decimal(c["sell"]),
                    Decimal(c["qty"]),
                    tuple(x for x in mine if x[0].side == "BUY"),
                    tuple(x for x in mine if x[0].side == "SELL"),
                )
            )
        return views

    def _manage(
        self,
        pair_id: UUID,
        product_id: str,
        grid: LiveGridRow,
        rules: MarketRules,
        price: Decimal,
        accounts: tuple[BalanceLike, ...] | None,
        notes: tuple[str, ...],
    ) -> PairOutcome:
        now = self._clock.now()
        with self._storage.tx() as repos:
            cells = self._cells(repos, grid)
            pair = repos.pairs.get(pair_id)
            active = pair is not None and pair.state is PairState.PAPER_ACTIVE
        buffer = self._settings.pair_policy.strategy.breakout_buffer
        margin = (grid.upper - grid.lower) * buffer
        inside = active and (grid.lower + margin < price < grid.upper - margin)
        placed = cancelled = 0
        book = self._book(product_id)
        # 1. an intent that was persisted but never sent (a crash): send it if it is still fresh
        for cell in cells:
            for intent, attempt in (*cell.buys, *cell.sells):
                if attempt is None and now - intent.created_at <= FRESH_INTENT:
                    with self._storage.tx() as repos:
                        undecided = not repos.safety.decisions_for(intent.id)
                    if undecided:
                        self._pipeline.process(intent.id, book=book)
        # 2. leaving the band: cancel the open BUYs, never sell at market
        if not inside:
            cancelled = self._cancel_buys(cells)
        # 3. each idle or bought cell
        for cell in cells:
            if cell.pending:
                continue
            if cell.held >= rules.base_min_size:
                if self._waited(cell.sells, now, BLOCK_COOLDOWN / 2):
                    placed += self._place_sell(grid, cell, rules, price, book)
            elif inside and accounts is not None and self._buy_ready(cell, rules, price, now):
                placed += self._place_buy(grid, cell, book)
        # 4. a grid that left the band and holds and has nothing open is finished
        if not inside:
            with self._storage.tx() as repos:
                fresh = self._cells(repos, grid)
                if all(not c.pending and c.held < rules.base_min_size for c in fresh):
                    reason = "BREAKOUT" if active else "PAIR_NOT_ACTIVE"
                    if repos.safety.stop_live_grid(grid.id, reason, self._clock.now()):
                        self._audit.record(
                            repos,
                            Evt.LIVE_GRID_STOPPED,
                            Res.SUCCESS,
                            actor=HOST_CLI_ACTOR,
                            target_type="pair",
                            target_id=pair_id,
                            reason=reason,
                            client_tag=HOST_ACTOR.client_tag,
                            request_id=HOST_ACTOR.request_id,
                        )
                        return PairOutcome(
                            product_id, "GRID_STOPPED", placed, cancelled, (*notes, reason)
                        )
        return PairOutcome(product_id, "MANAGED", placed, cancelled, notes)

    def _buy_ready(self, cell: CellView, rules: MarketRules, price: Decimal, now: Any) -> bool:
        if cell.buy > price - rules.price_increment:  # a post-only buy at or above the market
            return False
        limit = self._settings.safety.max_price_deviation_ratio
        if (
            price - cell.buy
        ) / price > limit:  # too far from the market: the risk engine would block
            return False
        return self._waited(cell.buys, now, BLOCK_COOLDOWN)

    @staticmethod
    def _waited(
        entries: tuple[tuple[IntentRow, AttemptRow | None], ...], now: Any, blocked_wait: Any
    ) -> bool:
        """False while the latest intent of this cell was refused or blocked a moment ago, so a
        persistent block does not create a new intent on every tick."""
        if not entries:
            return True
        intent, attempt = entries[-1]
        if attempt is None:
            return bool(now - intent.created_at >= blocked_wait)
        if attempt.state == "REJECTED":
            return bool(now - attempt.updated_at >= REJECT_COOLDOWN)
        return True

    def _slot(self, grid: LiveGridRow, cell: CellView, side: str) -> str:
        n = len(cell.buys if side == "BUY" else cell.sells)
        return f"{grid.id}:{cell.index}:{side}:{n}"

    def _place_buy(self, grid: LiveGridRow, cell: CellView, book: BookFacts | None) -> int:
        planned = next(c for c in grid.cells if int(c["index"]) == cell.index)
        order = OrderProposal(
            venue=self._venue,
            pair_id=str(grid.pair_id),
            product_id=grid.product_id,
            side="BUY",
            price=cell.buy,
            base_qty=cell.qty,
            expected_cycle_return=Decimal(planned["ret"]),
        )
        result = self._pipeline.submit(
            order, source=cell_source(cell.index), slot=self._slot(grid, cell, "BUY"), book=book
        )
        return 1 if result.kind in ("submitted", "adopted") else 0

    def _place_sell(
        self,
        grid: LiveGridRow,
        cell: CellView,
        rules: MarketRules,
        price: Decimal,
        book: BookFacts | None,
    ) -> int:
        qty = quantize_down(cell.held, rules.base_increment)
        if qty < rules.base_min_size:
            return 0
        sell = cell.sell
        if sell <= price:  # the market already passed the line: a post-only sell goes just above it
            sell = quantize_up(price + rules.price_increment, rules.price_increment)
            qty = min(qty, quantize_down(self._per_order_cap / sell, rules.base_increment))
            if qty < rules.base_min_size:
                return 0
        planned = next(c for c in grid.cells if int(c["index"]) == cell.index)
        order = OrderProposal(
            venue=self._venue,
            pair_id=str(grid.pair_id),
            product_id=grid.product_id,
            side="SELL",
            price=sell,
            base_qty=qty,
            expected_cycle_return=Decimal(planned["ret"]),
        )
        result = self._pipeline.submit(
            order, source=cell_source(cell.index), slot=self._slot(grid, cell, "SELL"), book=book
        )
        return 1 if result.kind in ("submitted", "adopted") else 0

    # ------------------------------------------------------------------ cancel (BUYs only)
    def _cancel_buys(self, cells: list[CellView]) -> int:
        targets = [
            (a.id, a.exchange_order_id)
            for cell in cells
            for _, a in cell.buys
            if a is not None and a.state == "WORKING" and a.exchange_order_id
        ]
        done = 0
        for i in range(0, len(targets), 100):
            chunk = targets[i : i + 100]
            ids = [order_id for _, order_id in chunk if order_id]
            try:
                results = self._gateway.cancel(ids)
                ok, code = True, None
            except ExchangeError as exc:
                results, ok, code = {}, False, exc.code
            except Exception:  # noqa: BLE001
                results, ok, code = {}, False, "UNEXPECTED_RESPONSE"
            now = self._clock.now()
            with self._storage.tx() as repos:
                repos.safety.add_api_event(self._venue, "cancel", ok, code, now)
                for attempt_id, order_id in chunk:
                    result = results.get(order_id or "")
                    if result is not None and result.outcome == "CANCEL_QUEUED":
                        repos.safety.transition(attempt_id, ("WORKING",), "CANCEL_REQUESTED", now)
                        done += 1
        return done


def grid_summary(grid: LiveGridRow) -> str:
    return json.dumps(
        {
            "id": str(grid.id),
            "product": grid.product_id,
            "state": grid.state,
            "levels": grid.levels,
            "lower": format(grid.lower, "f"),
            "upper": format(grid.upper, "f"),
        }
    )
