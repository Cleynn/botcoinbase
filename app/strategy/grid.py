"""Geometric grid builder with fee-aware feasibility and hard capital limits.

A grid of N price lines (3 to 5) has N-1 cells. Each cell buys at line i and sells at line i+1.
Buy prices round DOWN and sell prices round UP to the price increment, so rounding can only make a
cell more conservative. A cell's quantity is sized so that notional plus a stress-fee allowance fits
its share of the deployment cap; quantities round DOWN to the base increment.

Hard limits (the named capital profile; config may only tighten them): the pilot profile is total
capital 50, protected reserve 15, deployment cap 35; 3 to 5 levels; one active pair.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from app import constants
from app.capital.profiles import DEFAULT_PROFILE, PROFILES
from app.domain.money import ONE, ZERO, geometric_ratio, quantize_down, quantize_up, ratio
from app.domain.pairs import ProductMetadata


@dataclass(frozen=True)
class MarketRules:
    """Exchange rules taken from a frozen product-metadata snapshot."""

    price_increment: Decimal
    base_increment: Decimal
    quote_increment: Decimal
    base_min_size: Decimal
    base_max_size: Decimal | None
    quote_min_size: Decimal | None


@dataclass(frozen=True)
class CapitalPolicy:
    total: Decimal
    reserve: Decimal
    cap: Decimal
    profile: str = DEFAULT_PROFILE

    def validate(self) -> None:
        limits = PROFILES.get(self.profile)
        within_profile = self.profile == "custom" or (
            limits is not None
            and self.total <= limits.allocation_cap
            and self.reserve >= limits.protected_reserve
            and self.cap <= limits.max_deployment
        )
        if not (within_profile and self.cap + self.reserve <= self.total):
            raise GridRejected("CAPITAL_POLICY")


@dataclass(frozen=True)
class CostModel:
    """Everything a cell has to earn back, as fractions of price."""

    maker_fee: Decimal
    stress_fee: Decimal
    spread: Decimal
    slippage: Decimal  # per leg
    safety_margin: Decimal

    def required(self, fee: Decimal) -> Decimal:
        """Round-trip cost: two maker fees, one spread, two legs of slippage, plus the margin."""
        return 2 * fee + self.spread + 2 * self.slippage + self.safety_margin


class GridRejected(Exception):
    """The grid cannot be built safely. `code` is a fixed reason."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class Cell:
    index: int
    buy_price: Decimal
    sell_price: Decimal
    base_qty: Decimal
    reserve: Decimal  # quote reserved while the buy is open (notional plus stress-fee allowance)
    gross_return: Decimal


@dataclass(frozen=True)
class GridPlan:
    levels: int
    lower: Decimal
    upper: Decimal
    lines: tuple[Decimal, ...]
    cells: tuple[Cell, ...]
    cell_budget: Decimal
    min_return: Decimal
    required_operator: Decimal
    required_stress: Decimal

    @property
    def max_commitment(self) -> Decimal:
        return sum((c.reserve for c in self.cells), ZERO)


def build_grid(
    *,
    lower: Decimal,
    upper: Decimal,
    levels: int,
    rules: MarketRules,
    capital: CapitalPolicy,
    costs: CostModel,
) -> GridPlan:
    """Build the plan or raise `GridRejected`. Never returns a grid that breaks a limit."""
    capital.validate()
    if not constants.GRID_MIN_LEVELS <= levels <= constants.GRID_MAX_LEVELS:
        raise GridRejected("LEVELS_OUT_OF_RANGE")
    if lower <= 0 or upper <= lower:
        raise GridRejected("BAD_RANGE")
    cells_n = levels - 1
    r = geometric_ratio(lower, upper, cells_n)

    lines = [lower]
    for _ in range(cells_n - 1):
        lines.append(lines[-1] * r)
    lines.append(upper)
    buys = [quantize_down(p, rules.price_increment) for p in lines[:-1]]
    sells = [quantize_up(p, rules.price_increment) for p in lines[1:]]
    if buys[0] <= 0 or any(b >= a for a, b in zip(buys[1:], buys, strict=False)):
        raise GridRejected("PRICE_STEP_TOO_COARSE")  # lines collapse onto the same increment

    cell_budget = quantize_down(capital.cap / cells_n, rules.quote_increment)
    cells: list[Cell] = []
    for i in range(cells_n):
        buy, sell = buys[i], sells[i]
        if sell <= buy:
            raise GridRejected("PRICE_STEP_TOO_COARSE")
        usable = cell_budget - rules.quote_increment  # keep one increment for fee rounding
        qty = quantize_down(usable / (buy * (ONE + costs.stress_fee)), rules.base_increment)
        if qty < rules.base_min_size:
            raise GridRejected("BELOW_BASE_MINIMUM")
        if rules.base_max_size is not None and qty > rules.base_max_size:
            raise GridRejected("ABOVE_BASE_MAXIMUM")
        notional = qty * buy
        if rules.quote_min_size is not None and notional < rules.quote_min_size:
            raise GridRejected("BELOW_QUOTE_MINIMUM")
        # one increment of headroom covers fee rounding: a fill never spends more than reserved
        reserve = (
            quantize_up(notional * (ONE + costs.stress_fee), rules.quote_increment)
            + rules.quote_increment
        )
        if reserve > cell_budget:  # rounding pushed the reserve over the cell's share
            raise GridRejected("CELL_BUDGET_EXCEEDED")
        cells.append(Cell(i, buy, sell, qty, reserve, ratio(sell, buy) - ONE))

    plan = GridPlan(
        levels=levels,
        lower=lower,
        upper=upper,
        lines=tuple(lines),
        cells=tuple(cells),
        cell_budget=cell_budget,
        min_return=min(c.gross_return for c in cells),
        required_operator=costs.required(costs.maker_fee),
        required_stress=costs.required(costs.stress_fee),
    )
    if plan.max_commitment > capital.cap or capital.total - plan.max_commitment < capital.reserve:
        raise GridRejected("CAP_OR_RESERVE")
    if plan.min_return <= plan.required_operator:
        raise GridRejected("FEES_INFEASIBLE")
    if plan.min_return <= plan.required_stress:
        raise GridRejected("STRESS_FEES_INFEASIBLE")
    return plan


def rules_from_metadata(meta: ProductMetadata) -> MarketRules:
    """Exchange rules from a frozen metadata snapshot; anything missing means no trading."""
    needed = (meta.price_increment, meta.base_increment, meta.quote_increment, meta.base_min_size)
    if any(v is None or v <= 0 for v in needed):
        raise GridRejected("METADATA_INCOMPLETE")
    return MarketRules(
        price_increment=meta.price_increment,  # type: ignore[arg-type]
        base_increment=meta.base_increment,  # type: ignore[arg-type]
        quote_increment=meta.quote_increment,  # type: ignore[arg-type]
        base_min_size=meta.base_min_size,  # type: ignore[arg-type]
        base_max_size=meta.base_max_size,
        quote_min_size=meta.quote_min_size,
    )
