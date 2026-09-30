"""Named capital profiles (policy limits, never balances).

A profile is four numbers: the most of the account the bot may consider (`allocation_cap`), the
USDC that is never deployed (`protected_reserve`), the most that may be committed across the active
grid (`max_deployment`) and the most in a single order (`max_order`). The registry is code, so a
profile cannot be invented at runtime; the database holds an identical copy that the SQL limits
read, and a test keeps the two equal. Selecting a profile never creates, changes or sells an order,
and selecting one for LIVE never opens the live gate.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

ZERO = Decimal(0)
DEFAULT_PROFILE: Final = "pilot"
RESERVE_FLOOR_RATIO: Final = Decimal("0.20")  # every profile keeps at least 20% of its cap aside
MODES: Final = ("PAPER", "LIVE")
_NAME = re.compile(r"[a-z][a-z0-9_]{1,23}")


@dataclass(frozen=True)
class CapitalProfile:
    name: str
    title: str
    allocation_cap: Decimal
    protected_reserve: Decimal
    max_deployment: Decimal
    max_order: Decimal

    def __post_init__(self) -> None:
        if not _NAME.fullmatch(self.name):
            raise ValueError("profile names are short lowercase identifiers")
        values = (self.allocation_cap, self.protected_reserve, self.max_deployment, self.max_order)
        if any(not isinstance(v, Decimal) or v < ZERO for v in values):
            raise ValueError("profile amounts are non-negative Decimals")
        if self.protected_reserve + self.max_deployment > self.allocation_cap:
            raise ValueError("reserve plus deployment exceeds the allocation cap")
        if self.max_order > self.max_deployment:
            raise ValueError("one order may not exceed the deployment cap")
        if self.protected_reserve < self.allocation_cap * RESERVE_FLOOR_RATIO:
            raise ValueError("the protected reserve is below 20% of the allocation cap")

    @property
    def trades(self) -> bool:
        return self.max_deployment > ZERO


def _p(name: str, title: str, cap: str, reserve: str, deploy: str, order: str) -> CapitalProfile:
    return CapitalProfile(
        name, title, Decimal(cap), Decimal(reserve), Decimal(deploy), Decimal(order)
    )


PROFILES: Final[dict[str, CapitalProfile]] = {
    p.name: p
    for p in (
        _p("pilot", "Initial pilot", "50", "15", "35", "12"),
        _p("expanded", "Small expanded test", "100", "25", "75", "25"),
        _p("medium", "Medium profile", "250", "50", "150", "50"),
        _p("research", "Research only (no deployment)", "0", "0", "0", "0"),
    )
}
# The largest value any profile allows: database CHECK constraints use these absolute ceilings and
# the per-profile limit is enforced on top of them.
ABSOLUTE_MAX_ORDER: Final = max(p.max_order for p in PROFILES.values())
ABSOLUTE_MAX_DEPLOYMENT: Final = max(p.max_deployment for p in PROFILES.values())


def get_profile(name: str) -> CapitalProfile:
    """The named profile, or a KeyError: an unknown name never falls back to a larger one."""
    return PROFILES[name]
