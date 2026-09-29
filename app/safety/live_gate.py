"""The live gate. Live trading is BLOCKED in this build and this module cannot open it.

Two questions are answered here:

* `panel()`: what the dashboard shows. Always status BLOCKED with every unmet blocker listed
  (baseline 5.10). There is no code path that returns any other status: `LIVE_UNBLOCK_PRESENT` is a
  constant, the mode set has no LIVE, the venue set has no LIVE venue and no live gateway class,
  credential or signer exists in this image.
* `order_gate()`: the gate check an order must pass. It passes only for a venue that is not live
  (PAPER, or FAKE outside production) in a mode this build can represent. Anything else, and any
  failure to evaluate, blocks.
"""

from __future__ import annotations

from typing import Final

from app import constants
from app.config import Settings
from app.safety.types import VENUES, GateResult

LIVE_UNBLOCK_PRESENT: Final = (
    False  # a separately approved change with its own test suite (5.10 #25)
)
STATUS_TEXT: Final = "LIVE TRADING BLOCKED"

# Each entry is a condition that must become true before live could even be considered. None is
# evaluated as "true" by this build; the text is what the operator sees.
BLOCKERS: Final[dict[str, str]] = {
    "NO_UNBLOCK_CHANGE": "no separately approved unblock change exists",
    "LIVE_NOT_REPRESENTABLE": "this build has no LIVE mode, LIVE venue or LIVE pair state",
    "NO_LIVE_GATEWAY": "no live execution gateway exists in this build",
    "NO_CREDENTIALS": "no exchange credential or signer is present or loadable",
    "KEY_PERMISSIONS_UNVERIFIED": "key permissions (trade only, no transfer, dedicated portfolio) are unverified",
    "TRADABILITY_UNPROVEN": "account, jurisdiction and product tradability are unproven",
    "EXCHANGE_CONTRACT_UNVERIFIED": "client-id scope, list fields and fill shapes are unverified (AS-C3, AS-C4)",
    "ABSENCE_PROOF_MISSING": "no absence proof exists for retrying an order of unknown outcome",
    "NO_SIGNED_REVIEW": "no signed live-pilot review exists",
    "INDEPENDENT_REVIEW_NOT_DONE": "no independent security review has been performed",
    "CONTRACT_INTERPRETATIONS_UNACKNOWLEDGED": "DEC-000 is not acknowledged",
    "SOAK_INCOMPLETE": "the 30-day paper soak after the safety machinery is incomplete",
    "STEP_UP_MISSING": "no step-up approval outside the web process exists",
}


def panel(settings: Settings | None = None) -> GateResult:
    """What the dashboard shows. Always BLOCKED; never raises."""
    return GateResult(status="BLOCKED", reasons=tuple(BLOCKERS))


def order_gate(venue: str, settings: Settings) -> tuple[bool, str | None]:
    """(passes, reason). Fails closed on any error."""
    try:
        if LIVE_UNBLOCK_PRESENT:  # pragma: no cover  (constant; kept so a future change is visible)
            return False, "LIVE_GATE_BLOCKED"
        if settings.mode not in constants.ALLOWED_MODES:
            return False, "LIVE_GATE_BLOCKED"
        if venue not in VENUES:
            return False, "LIVE_GATE_BLOCKED"
        if venue == "FAKE" and settings.environment == "production":
            return False, "LIVE_GATE_BLOCKED"
        return True, None
    except Exception:  # noqa: BLE001  (an unreadable gate is a blocked gate)
        return False, "LIVE_GATE_BLOCKED"
