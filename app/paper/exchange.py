"""The local paper exchange: persisted, restart-safe, and unable to reach any exchange.

There is no network code here. Orders, fills, the cash ledger and the position live in PostgreSQL
(`paper_*` tables, every row CHECKed to the PAPER venue). `step` rebuilds the grid trader's
state from those tables, feeds it the closed candles that arrived since the last step (read from
the candle table), and writes the effects back in ONE transaction. If the process dies
mid-step nothing was written and the next step repeats the same work: steps are idempotent per
candle, client order ids are deterministic, and ledger entries and fills have unique keys.

Rules are the backtest's (`app.backtest.trader`); the database independently enforces the 15 USDC
reserve, the 35 USDC cap, the single 50 USDC deposit and "orders only for the PAPER_ACTIVE pair".
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final
from uuid import UUID, uuid5

from app.auth.audit import HOST_CLI_ACTOR, AuditWriter
from app.backtest.trader import (
    ACTIVE,
    IDLE,
    Counters,
    Effect,
    Order,
    SimConfig,
    Trader,
    TraderState,
)
from app.capital.profiles import PROFILES as CAPITAL_PROFILES
from app.config import PairPolicy, Settings
from app.domain.enums import AuditEventType, AuditResult
from app.domain.models import Clock
from app.domain.money import canonical
from app.domain.pairs import PairState
from app.market.candles import Candle
from app.market.ingest import GRANULARITY, STEP
from app.pairs import policy as pair_policy
from app.pairs.runner import HOST_ACTOR
from app.storage.database import Storage
from app.storage.paper_repositories import OrderRow, SessionRow
from app.storage.repositories import Repos
from app.strategy import decision as strat
from app.strategy.grid import Cell, GridPlan, GridRejected, rules_from_metadata

_NAMESPACE: Final = UUID("0b6c7a3e-2f4d-4c0e-8a55-7d1d9f6a2b02")
MAX_CANDLES_PER_STEP: Final = 2000


class PaperError(Exception):
    """A paper operation was refused. `code` is a fixed reason; nothing was changed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PaperStatus:
    state: str
    phase: str
    pair_id: UUID | None
    product_id: str | None
    cash: Decimal
    reserved: Decimal
    free_cash: Decimal
    inventory: Decimal
    cost_basis: Decimal
    deployed: Decimal
    open_orders: int
    orders_by_state: dict[str, int]
    fills: int
    fees_paid: Decimal
    last_candle_start: int | None
    newest_candle_start: int | None
    data_age_seconds: int | None
    data_stale: bool


@dataclass(frozen=True)
class StepResult:
    candles_processed: int
    placed: int
    fills: int
    cancelled: int
    phase: str
    last_candle_start: int | None


def _plan_details(plan: GridPlan) -> dict[str, Any]:
    return {
        "cells": [
            {
                "index": c.index,
                "buy": canonical(c.buy_price),
                "sell": canonical(c.sell_price),
                "qty": canonical(c.base_qty),
                "reserve": canonical(c.reserve),
                "return": canonical(c.gross_return),
            }
            for c in plan.cells
        ],
        "min_return": canonical(plan.min_return),
        "required_operator": canonical(plan.required_operator),
        "required_stress": canonical(plan.required_stress),
    }


def _plan_from_row(row: dict[str, Any]) -> GridPlan:
    d = row["details"] if isinstance(row["details"], dict) else {}
    cells = tuple(
        Cell(
            int(c["index"]),
            Decimal(c["buy"]),
            Decimal(c["sell"]),
            Decimal(c["qty"]),
            Decimal(c["reserve"]),
            Decimal(c["return"]),
        )
        for c in d["cells"]
    )
    return GridPlan(
        levels=int(row["levels"]),
        lower=row["lower_price"],
        upper=row["upper_price"],
        lines=tuple(row["prices"]),
        cells=cells,
        cell_budget=row["cell_budget"],
        min_return=Decimal(d["min_return"]),
        required_operator=Decimal(d["required_operator"]),
        required_stress=Decimal(d["required_stress"]),
    )


class PaperExchange:
    def __init__(self, *, storage: Storage, clock: Clock, settings: Settings) -> None:
        self._storage, self._clock, self._settings = storage, clock, settings
        self._policy = settings.pair_policy
        self._audit = AuditWriter(clock)

    # ------------------------------------------------------------------ status
    def status(self) -> PaperStatus:
        with self._storage.tx() as repos:
            session = repos.paper.session()
            cash = repos.paper.cash()
            reserved = repos.paper.reserved()
            qty, cost = repos.paper.total_position()
            counts = repos.paper.counts()
            fills, fees = repos.paper.fill_stats()
            product_id = newest = None
            if session.pair_id is not None:
                pair = repos.pairs.get(session.pair_id)
                if pair is not None:
                    product_id = pair.product_id
                    newest = repos.market.newest_candle_start(pair.product_uuid, GRANULARITY)
        age = None if newest is None else int(self._clock.now().timestamp()) - (newest + STEP)
        return PaperStatus(
            state=session.state,
            phase=session.phase,
            pair_id=session.pair_id,
            product_id=product_id,
            cash=cash,
            reserved=reserved,
            free_cash=cash - reserved,
            inventory=qty,
            cost_basis=cost,
            deployed=reserved + cost,
            open_orders=counts.get("OPEN", 0),
            orders_by_state=counts,
            fills=fills,
            fees_paid=fees,
            last_candle_start=session.last_candle_start,
            newest_candle_start=newest,
            data_age_seconds=age,
            data_stale=age is None or age > self._policy.validation.max_stale_seconds,
        )

    def _policy_for(self, repos: Repos) -> PairPolicy:
        """The static policy under the capital profile selected for PAPER in the database."""
        profile = CAPITAL_PROFILES.get(repos.safety.control().paper_profile)
        if profile is None:
            raise PaperError("PROFILE_UNKNOWN")
        if not profile.trades:
            raise PaperError("PROFILE_NO_DEPLOYMENT")
        return self._policy.for_profile(profile)

    # ------------------------------------------------------------------ start / stop
    def start(self, *, acknowledge_halt: bool = False) -> SessionRow:
        """PAUSED -> RUNNING for the one PAPER_ACTIVE pair. Refuses on anything doubtful."""
        if self._settings.mode != "PAPER":
            raise PaperError("MODE_NOT_PAPER")
        if self._settings.environment == "production":
            raise PaperError("PAPER_REFUSED_IN_PRODUCTION")
        now = self._clock.now()
        try:
            strat.operator_fee(self._policy, now.date())
        except strat.FeeNotAttested as exc:
            raise PaperError(exc.code) from exc
        with self._storage.tx() as repos:
            session = repos.paper.session(for_update=True)
            if session.state != "PAUSED":
                raise PaperError("ALREADY_RUNNING")
            control = repos.safety.control()
            policy = self._policy_for(repos)
            if control.kill_switch == "ACTIVE":
                raise PaperError("KILL_SWITCH_ACTIVE")
            if control.breaker_state == "OPEN":
                raise PaperError("BREAKER_OPEN")
            if session.phase == "HALTED" and not acknowledge_halt:
                raise PaperError("HALTED_BY_DRAWDOWN")  # a halt is never cleared silently
            active = repos.pairs.in_states((PairState.PAPER_ACTIVE,))
            if len(active) != 1:
                raise PaperError("NEEDS_EXACTLY_ONE_ACTIVE_PAIR")
            pair = active[0]
            product = repos.products.get(pair.product_uuid)
            if product is None or not pair_policy.assess_product(product.metadata).ok:
                raise PaperError("PRODUCT_NOT_OK")
            if (
                now - product.last_verified_at
            ).total_seconds() > self._policy.validation.max_metadata_age_seconds:
                raise PaperError("METADATA_STALE")
            try:
                rules_from_metadata(product.metadata)
            except GridRejected as exc:
                raise PaperError(exc.code) from exc
            newest = repos.market.newest_candle_start(pair.product_uuid, GRANULARITY)
            if newest is None:
                raise PaperError("NO_MARKET_DATA")
            if (int(now.timestamp()) - (newest + STEP)) > self._policy.validation.max_stale_seconds:
                raise PaperError("DATA_STALE")
            if not repos.paper.has_deposit():
                repos.paper.add_entry(
                    "DEPOSIT", "DEPOSIT", policy.total_capital, Decimal(0), None, now
                )
                repos.paper.set_position(pair.id, Decimal(0), Decimal(0), now)
            elif session.pair_id is not None and session.pair_id != pair.id:
                raise PaperError("PAIR_CHANGED_WITH_OPEN_STATE")
            qty, _cost = repos.paper.position(pair.id)
            resume_plan = session.grid_plan_id if session.pair_id == pair.id else None
            phase = (
                ACTIVE if (resume_plan is not None and qty > 0) else IDLE
            )  # HALTED is cleared only by the acknowledgement above
            last = session.last_candle_start if session.pair_id == pair.id else None
            repos.paper.update_session(
                now,
                state="RUNNING",
                pair_id=pair.id,
                grid_plan_id=resume_plan if phase == ACTIVE else None,
                last_candle_start=last if last is not None else newest,
                phase=phase,
                peak_equity=max(session.peak_equity, policy.total_capital),
            )
            self._audit.record(
                repos,
                AuditEventType.PAPER_STARTED,
                AuditResult.SUCCESS,
                actor=HOST_CLI_ACTOR,
                target_type="pair",
                target_id=pair.id,
                reason=phase,
                client_tag=HOST_ACTOR.client_tag,
                request_id=HOST_ACTOR.request_id,
                detail={"product": pair.product_id},
            )
            return repos.paper.session()

    def stop(self, reason: str = "OPERATOR") -> int:
        """RUNNING -> PAUSED. Cancels every open paper order; never sells inventory."""
        now = self._clock.now()
        with self._storage.tx() as repos:
            session = repos.paper.session(for_update=True)
            if session.state != "RUNNING":
                raise PaperError("NOT_RUNNING")
            cancelled = self._cancel_all_open(repos, now)
            repos.paper.update_session(
                now, state="PAUSED", phase=IDLE if session.phase != ACTIVE else ACTIVE
            )
            self._audit.record(
                repos,
                AuditEventType.PAPER_STOPPED,
                AuditResult.SUCCESS,
                actor=HOST_CLI_ACTOR,
                target_type="pair",
                target_id=session.pair_id,
                reason=reason[:60],
                client_tag=HOST_ACTOR.client_tag,
                request_id=HOST_ACTOR.request_id,
                detail={"cancelled": cancelled},
            )
        return cancelled

    def cancel_for_safety(self, reason: str) -> int:
        """Cancel every open paper order and pause the paper session. Never sells inventory."""
        now = self._clock.now()
        with self._storage.tx() as repos:
            session = repos.paper.session(for_update=True)
            cancelled = self._cancel_all_open(repos, now)
            if session.state == "RUNNING":
                repos.paper.update_session(now, state="PAUSED")
            self._audit.record(
                repos,
                AuditEventType.PAPER_STOPPED,
                AuditResult.SUCCESS,
                actor=HOST_CLI_ACTOR,
                target_type="pair",
                target_id=session.pair_id,
                reason=reason[:60],
                client_tag=HOST_ACTOR.client_tag,
                request_id=HOST_ACTOR.request_id,
                detail={"cancelled": cancelled},
            )
        return cancelled

    def _cancel_all_open(self, repos: Repos, now: Any) -> int:
        n = 0
        for row in repos.paper.open_orders():
            repos.paper.update_order(
                row.id, state="CANCELLED", filled=row.filled_qty, reserved=Decimal(0), now=now
            )
            n += 1
        return n

    # ------------------------------------------------------------------ step
    def step(self) -> StepResult:
        """Process the closed candles that arrived since the last step. Idempotent and atomic."""
        now = self._clock.now()
        with self._storage.tx() as repos:
            session = repos.paper.session(for_update=True)
            if session.state != "RUNNING" or session.pair_id is None:
                raise PaperError("NOT_RUNNING")
            control = repos.safety.control()
            if control.kill_switch == "ACTIVE" or control.breaker_state == "OPEN":
                reason = "KILL_SWITCH_ACTIVE" if control.kill_switch == "ACTIVE" else "BREAKER_OPEN"
                cancelled = self._cancel_all_open(repos, now)  # cancels only; never sells
                repos.paper.update_session(now, state="PAUSED")
                self._audit.record(
                    repos,
                    AuditEventType.PAPER_STOPPED,
                    AuditResult.FAILURE,
                    actor=HOST_CLI_ACTOR,
                    target_type="pair",
                    target_id=session.pair_id,
                    reason=reason,
                    client_tag=HOST_ACTOR.client_tag,
                    request_id=HOST_ACTOR.request_id,
                    detail={"cancelled": cancelled},
                )
                return StepResult(0, 0, 0, cancelled, session.phase, session.last_candle_start)
            pair = repos.pairs.get(session.pair_id)
            if pair is None or pair.state is not PairState.PAPER_ACTIVE:
                cancelled = self._cancel_all_open(repos, now)
                repos.paper.update_session(now, state="PAUSED")
                self._audit.record(
                    repos,
                    AuditEventType.PAPER_STOPPED,
                    AuditResult.FAILURE,
                    actor=HOST_CLI_ACTOR,
                    target_type="pair",
                    target_id=session.pair_id,
                    reason="PAIR_NOT_ACTIVE",
                    client_tag=HOST_ACTOR.client_tag,
                    request_id=HOST_ACTOR.request_id,
                    detail={"cancelled": cancelled},
                )
                return StepResult(0, 0, 0, cancelled, "IDLE", session.last_candle_start)
            product = repos.products.get(pair.product_uuid)
            if product is None:  # pragma: no cover  (foreign key guarantees it)
                raise PaperError("PRODUCT_MISSING")
            meta = repos.products.metadata_snapshot(product.snapshot_id) or product.metadata
            try:
                rules = rules_from_metadata(meta)
                fee = strat.operator_fee(self._policy, now.date())
            except (GridRejected, strat.FeeNotAttested) as exc:
                raise PaperError(getattr(exc, "code", "REFUSED")) from exc

            last = session.last_candle_start or 0
            need = strat.lookback_needed(self._policy)
            history_from = last + STEP - need * STEP
            candles = list(
                repos.market.candles(
                    pair.product_uuid,
                    GRANULARITY,
                    history_from,
                    last + STEP + MAX_CANDLES_PER_STEP * STEP,
                )
            )
            new_from = next((i for i, c in enumerate(candles) if c.start > last), len(candles))
            if new_from == len(candles):
                return StepResult(0, 0, 0, 0, session.phase, session.last_candle_start)

            state = self._load_state(repos, session, pair.id, rules, fee)
            policy = self._policy_for(repos)
            cfg = SimConfig(policy, rules, fee, fee, policy.grid_levels)
            position = {"i": 0}

            def window(n: int) -> list[Candle]:
                i = position["i"]
                return candles[max(0, i + 1 - n) : i + 1]

            trader = Trader(cfg, state, window)
            plan_id: UUID | None = session.grid_plan_id
            placed = fills = cancelled = 0
            touched: dict[int, Order] = {}
            seq_to_row: dict[int, UUID] = {o.seq: o.id for o in repos.paper.open_orders()}
            new_candles = candles[new_from:]
            for offset, candle in enumerate(new_candles):
                position["i"] = new_from + offset
                for effect in trader.on_candle(candle):
                    plan_id, p, f, c = self._persist(
                        repos, effect, pair.id, plan_id, seq_to_row, touched, state, now, product.id
                    )
                    placed, fills, cancelled = placed + p, fills + f, cancelled + c
            self._settle(repos, touched, seq_to_row, state, pair.id, now)
            last_start = new_candles[-1].start
            repos.paper.update_session(
                now,
                last_candle_start=last_start,
                phase=state.phase,
                peak_equity=state.peak_equity,
                grid_plan_id=plan_id,
            )
            if placed or fills or cancelled:
                self._audit.record(
                    repos,
                    AuditEventType.PAPER_STEPPED,
                    AuditResult.SUCCESS,
                    actor=HOST_CLI_ACTOR,
                    target_type="pair",
                    target_id=pair.id,
                    reason=state.phase,
                    client_tag=HOST_ACTOR.client_tag,
                    request_id=HOST_ACTOR.request_id,
                    detail={
                        "candles": len(new_candles),
                        "placed": placed,
                        "fills": fills,
                        "cancelled": cancelled,
                    },
                )
            return StepResult(len(new_candles), placed, fills, cancelled, state.phase, last_start)

    # ------------------------------------------------------------------ state <-> database
    def _load_state(
        self, repos: Repos, session: SessionRow, pair_id: UUID, rules: Any, fee: Decimal
    ) -> TraderState:
        qty, cost = repos.paper.position(pair_id)
        plan = None
        pending: dict[int, Decimal] = {}
        next_seq = 1
        if session.grid_plan_id is not None:
            row = repos.results.grid_plan(session.grid_plan_id)
            if row is not None:
                plan = _plan_from_row(row)
                pending = repos.paper.pending_by_cell(session.grid_plan_id)
                next_seq = repos.paper.max_seq(session.grid_plan_id) + 1
        orders = [
            Order(
                r.seq,
                r.level_index,
                r.side,
                r.price,
                r.base_qty,
                r.placed_candle,
                r.quote_reserved,
                r.filled_qty,
            )
            for r in repos.paper.open_orders()
        ]
        return TraderState(
            cash=repos.paper.cash(),
            inventory=qty,
            cost_basis=cost,
            phase=session.phase,
            peak_equity=session.peak_equity,
            plan=plan,
            orders=orders,
            pending=pending,
            next_seq=next_seq,
            last_start=session.last_candle_start,
            counters=Counters(),
        )

    def _persist(
        self,
        repos: Repos,
        effect: Effect,
        pair_id: UUID,
        plan_id: UUID | None,
        seq_to_row: dict[int, UUID],
        touched: dict[int, Order],
        state: TraderState,
        now: Any,
        product_uuid: UUID,
    ) -> tuple[UUID | None, int, int, int]:
        if effect.kind == "PLAN" and effect.plan is not None:
            plan = effect.plan
            new_id = uuid5(
                _NAMESPACE,
                f"plan|{pair_id}|{effect.candle_start}|{plan.lower}|{plan.upper}|{plan.levels}",
            )
            repos.results.insert_grid_plan(
                plan_id=new_id,
                pair_id=pair_id,
                levels=plan.levels,
                lower=plan.lower,
                upper=plan.upper,
                prices=list(plan.lines),
                cell_budget=plan.cell_budget,
                config_sha256=_plan_hash(plan),
                details=_plan_details(plan),
                now=now,
            )
            repos.paper.update_session(now, grid_plan_id=new_id, phase=ACTIVE)
            return new_id, 0, 0, 0
        order = effect.order
        if effect.kind == "PLACE" and order is not None and plan_id is not None:
            oid = uuid5(_NAMESPACE, f"order|{plan_id}|{order.seq}")
            cycle = repos.paper.cell_side_count(plan_id, order.cell, order.side)
            row = OrderRow(
                oid,
                pair_id,
                plan_id,
                uuid5(_NAMESPACE, f"client|{plan_id}|{order.seq}"),
                order.seq,
                order.cell,
                cycle,
                order.side,
                order.price,
                order.qty,
                Decimal(0),
                order.reserve,
                "OPEN",
                order.placed_start,
                now,
                now,
            )
            repos.paper.insert_order(row)
            seq_to_row[order.seq] = oid
            touched[order.seq] = order
            return plan_id, 1, 0, 0
        if effect.kind == "FILL" and order is not None:
            oid = seq_to_row[order.seq]
            repos.paper.insert_fill(
                oid, effect.candle_start, effect.price, effect.qty, effect.notional, effect.fee
            )
            key = f"fill|{oid}|{effect.candle_start}"
            if order.side == "BUY":
                repos.paper.add_entry(
                    f"{key}|principal", "FILL_BUY", -effect.notional, effect.qty, oid, now
                )
            else:
                repos.paper.add_entry(
                    f"{key}|principal", "FILL_SELL", effect.notional, -effect.qty, oid, now
                )
            if effect.fee > 0:
                repos.paper.add_entry(f"{key}|fee", "FEE", -effect.fee, Decimal(0), oid, now)
            touched[order.seq] = order
            return plan_id, 0, 1, 0
        if effect.kind == "CANCEL" and order is not None:
            touched[order.seq] = order
            return plan_id, 0, 0, 1
        return plan_id, 0, 0, 0

    def _settle(
        self,
        repos: Repos,
        touched: dict[int, Order],
        seq_to_row: dict[int, UUID],
        state: TraderState,
        pair_id: UUID,
        now: Any,
    ) -> None:
        """Write the final state of every order the step touched, and the position."""
        for seq, order in touched.items():
            state_name = order.state
            repos.paper.update_order(
                seq_to_row[seq],
                state=state_name,
                filled=order.filled,
                reserved=order.reserve,
                now=now,
            )
        repos.paper.set_position(pair_id, state.inventory, state.cost_basis, now)


def _plan_hash(plan: GridPlan) -> str:
    import hashlib
    import json

    text = json.dumps(
        _plan_details(plan) | {"lower": canonical(plan.lower), "upper": canonical(plan.upper)},
        sort_keys=True,
    )
    return hashlib.sha256(text.encode("ascii")).hexdigest()
