"""The editable trading configuration of one mode (PAPER or LIVE).

Six numbers decide how a mode trades: how many pairs may be active in parallel, how many grid lines
(levels) each grid has, how much quote currency each grid may invest, the total that may be invested
across all grids, the reserve that is never invested, and the most in a single order. The database
keeps the same rules as CHECK constraints and enforces the money limits at the moment an order is
authorized; this module validates a proposed change first (same arithmetic, Decimal only).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Final

from app.capital.profiles import ZERO

MODES: Final = ("PAPER", "LIVE")
MAX_PAIRS_LIMIT: Final = 10
MIN_LEVELS: Final = 3
MAX_LEVELS: Final = 20


@dataclass(frozen=True)
class TradingConfig:
    mode: str
    max_pairs: int
    levels_per_grid: int
    quote_per_grid: Decimal
    invested_cap: Decimal
    reserve: Decimal
    per_order_cap: Decimal
    version: int = 1

    @property
    def allocation_cap(self) -> Decimal:
        """What the account must hold for this configuration: invested plus reserve."""
        return self.invested_cap + self.reserve


def problems(cfg: TradingConfig) -> list[str]:
    """Reasons the configuration is refused (empty = valid). Fixed codes, never free text."""
    out: list[str] = []
    if cfg.mode not in MODES:
        out.append("MODE")
    if not 1 <= cfg.max_pairs <= MAX_PAIRS_LIMIT:
        out.append("MAX_PAIRS")
    if not MIN_LEVELS <= cfg.levels_per_grid <= MAX_LEVELS:
        out.append("LEVELS")
    amounts = (cfg.quote_per_grid, cfg.invested_cap, cfg.reserve, cfg.per_order_cap)
    if any(not isinstance(a, Decimal) or not a.is_finite() or a < ZERO for a in amounts):
        return [*out, "AMOUNTS"]
    if cfg.quote_per_grid * cfg.max_pairs > cfg.invested_cap:
        out.append("GRIDS_EXCEED_INVESTED_CAP")
    if cfg.per_order_cap > cfg.quote_per_grid:
        out.append("ORDER_EXCEEDS_GRID")
    return out
