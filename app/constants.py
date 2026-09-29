"""Hard policy ceilings and fixed identifiers. Config may only tighten these, never loosen them."""

from decimal import Decimal
from typing import Final

APP_NAME: Final = "TradingDots"

# Modes representable in this build. LIVE is intentionally absent (unrepresentable).
ALLOWED_MODES: Final = ("BACKTEST", "PAPER")
DEFAULT_MODE: Final = "BACKTEST"

# Fixed banner text. Live trading is blocked in every build produced from this code.
LIVE_TRADING_STATUS: Final = "BLOCKED"

# Capital policy ceilings (USDC).
POLICY_TOTAL_CAPITAL: Final = Decimal("50")
POLICY_MIN_RESERVE: Final = Decimal("15")
POLICY_MAX_DEPLOYMENT: Final = Decimal("35")
# One order never exceeds this notional (35 / 3 levels, rounded up). Config may only lower it.
POLICY_MAX_ORDER_NOTIONAL: Final = Decimal("12")
GRID_MIN_LEVELS: Final = 3
GRID_MAX_LEVELS: Final = 5
MAX_ACTIVE_PAIRS: Final = 1

ENVIRONMENTS: Final = ("development", "test", "production")
PLACEHOLDER_MARKERS: Final = ("CHANGE_ME", "changeme", "placeholder", "example", "REPLACE_ME")
MIN_SECRET_LENGTH: Final = 32

INTERNAL_APP_PORT: Final = 8000
