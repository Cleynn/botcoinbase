"""Expected balances and equity, derived only from recorded baselines and fills (pure, Decimal).

This is the bot's own account of what the exchange should hold. Reconciliation compares it with the
exchange; a difference is a BALANCE_MISMATCH and blocks. Equity is sampled at each fill using that
fill's price as the mark (deterministic and needs no external price history); the current mark, when
known, adds a last point.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Final

ZERO: Final = Decimal(0)
QUOTE: Final = "USDC"


@dataclass(frozen=True)
class FillEvent:
    product_id: str
    side: str
    price: Decimal
    size: Decimal
    fee: Decimal  # in the quote currency
    at: datetime


@dataclass
class Position:
    qty: Decimal = ZERO
    cost: Decimal = ZERO


@dataclass
class LedgerState:
    balances: dict[str, Decimal]
    positions: dict[str, Position]
    realized: Decimal
    curve: list[tuple[datetime, Decimal]] = field(default_factory=list)


def base_of(product_id: str) -> str:
    return product_id.split("-", 1)[0]


def replay(baselines: Mapping[str, Decimal], fills: Sequence[FillEvent]) -> LedgerState:
    balances = dict(baselines)
    balances.setdefault(QUOTE, ZERO)
    positions: dict[str, Position] = {}
    marks: dict[str, Decimal] = {}
    realized = ZERO
    curve: list[tuple[datetime, Decimal]] = []
    for f in sorted(fills, key=lambda x: x.at):
        base = base_of(f.product_id)
        pos = positions.setdefault(f.product_id, Position())
        notional = f.price * f.size
        if f.side == "BUY":
            balances[QUOTE] = balances[QUOTE] - notional - f.fee
            balances[base] = balances.get(base, ZERO) + f.size
            pos.qty += f.size
            pos.cost += notional + f.fee
        else:
            balances[QUOTE] = balances[QUOTE] + notional - f.fee
            balances[base] = balances.get(base, ZERO) - f.size
            if pos.qty > ZERO:
                removed = pos.cost * min(f.size, pos.qty) / pos.qty
                pos.cost -= removed
                pos.qty -= min(f.size, pos.qty)
                realized += notional - f.fee - removed
            else:  # a sell with no inventory: nothing to cost against; recorded as proceeds
                realized += notional - f.fee
        marks[f.product_id] = f.price
        curve.append((f.at, equity(balances, positions, marks)))
    return LedgerState(balances, positions, realized, curve)


def equity(
    balances: Mapping[str, Decimal], positions: Mapping[str, Position], marks: Mapping[str, Decimal]
) -> Decimal:
    total = balances.get(QUOTE, ZERO)
    for product_id, pos in positions.items():
        mark = marks.get(product_id)
        if pos.qty > ZERO and mark is not None:
            total += pos.qty * mark
        elif pos.qty > ZERO:
            total += pos.cost  # no mark: value at cost (never invents a gain)
    return total


def equity_view(
    state: LedgerState,
    baseline_equity: Decimal,
    *,
    day_start: datetime,
    current_marks: Mapping[str, Decimal],
) -> tuple[Decimal, Decimal, Decimal]:
    """(current equity, peak equity, equity at the start of the day)."""
    marks = dict(current_marks)
    current = equity(state.balances, state.positions, marks)
    values = [baseline_equity, current, *[v for _, v in state.curve]]
    before = [v for t, v in state.curve if t < day_start]
    day_open = before[-1] if before else baseline_equity
    return current, max(values), day_open
