"""The live gate. Live trading is BLOCKED unless the host has armed it; this module cannot arm it.

Two questions are answered here:

* `panel()`: what the dashboard shows. Status BLOCKED with every unmet blocker listed (baseline
  5.10), unless the caller passes facts read from the database that show an active arming made on
  the host (`live_arming`, written only by the host CLI, at most 24 hours, undone by any later kill
  switch, breaker trip, recovery reset or LIVE profile change). There is no unconditional unblock:
  `LIVE_UNBLOCK_PRESENT` is a constant and nothing in this module or the web tier can write an arming.
* `order_gate()`: the gate check an order must pass. PAPER, and FAKE outside production, pass in a
  representable mode. COINBASE passes only when the caller reports an active arming (the database
  attempt guard re-checks the same thing). Anything else, and any failure to evaluate, blocks.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Final

from app import constants
from app.config import Settings
from app.safety.types import VENUES, GateResult

LIVE_UNBLOCK_PRESENT: Final = False  # no unconditional unblock exists: live needs a host arming
STATUS_TEXT: Final = "LIVE TRADING BLOCKED"
ARMED_TEXT: Final = "LIVE TRADING ARMED"

# Each entry is a condition that must become true before live could even be considered. None is
# evaluated as "true" by this build; the text is what the operator sees.
BLOCKERS: Final[dict[str, str]] = {
    "NO_UNBLOCK_CHANGE": "no separately approved unblock change exists",
    "LIVE_NOT_REPRESENTABLE": "no LIVE mode or LIVE pair state exists; live runs only as a host arming",
    "NO_LIVE_GATEWAY": "no live execution gateway is armed (it is built only on the host, when armed)",
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


# Each human attestation clears exactly one blocker; the rest are facts the database knows.
ATTESTATION_BLOCKERS: Final[dict[str, str]] = {
    "INDEPENDENT_REVIEW_DONE": "INDEPENDENT_REVIEW_NOT_DONE",
    "EXCHANGE_CONTRACT_REVIEWED": "EXCHANGE_CONTRACT_UNVERIFIED",
    "SIGNED_PILOT_REVIEW": "NO_SIGNED_REVIEW",
    "SOAK_COMPLETE": "SOAK_INCOMPLETE",
    "DEC000_ACKNOWLEDGED": "CONTRACT_INTERPRETATIONS_UNACKNOWLEDGED",
    "TRADABILITY_CONFIRMED": "TRADABILITY_UNPROVEN",
    "KEY_SCOPE_CONFIRMED": "KEY_PERMISSIONS_UNVERIFIED",
}
FACT_BLOCKERS: Final[dict[str, str]] = {
    "NOT_ARMED": "the host has not armed live trading (or the arming expired or was undone)",
    "NO_COINBASE_BASELINE": "no USDC baseline is recorded for the COINBASE venue",
    "COINBASE_RECONCILIATION_STALE": "there is no current successful COINBASE reconciliation",
    "CONTROL_NOT_CLEAR": "the kill switch, breaker or startup recovery is not clear",
}


@dataclass(frozen=True)
class LiveFacts:
    """What the database knows about live readiness, gathered by the caller."""

    armed_until: datetime | None
    attested: frozenset[str]
    baseline: bool
    reconciled: bool
    clear: bool


def panel(settings: Settings | None = None, facts: LiveFacts | None = None) -> GateResult:
    """What the dashboard shows; never raises. Without facts: BLOCKED with every blocker listed."""
    if facts is None:
        return GateResult(status="BLOCKED", reasons=tuple(BLOCKERS))
    if facts.armed_until is not None:
        return GateResult(status="ARMED", reasons=(), checked_at=facts.armed_until)
    unmet = ["NOT_ARMED"]
    unmet += [b for code, b in ATTESTATION_BLOCKERS.items() if code not in facts.attested]
    if not facts.baseline:
        unmet.append("NO_COINBASE_BASELINE")
    if not facts.reconciled:
        unmet.append("COINBASE_RECONCILIATION_STALE")
    if not facts.clear:
        unmet.append("CONTROL_NOT_CLEAR")
    return GateResult(status="BLOCKED", reasons=tuple(unmet))


def order_gate(
    venue: str, settings: Settings, *, live_armed: bool = False
) -> tuple[bool, str | None]:
    """(passes, reason). Fails closed on any error."""
    try:
        if LIVE_UNBLOCK_PRESENT:  # pragma: no cover  (constant; kept so a future change is visible)
            return False, "LIVE_GATE_BLOCKED"
        if settings.mode not in constants.ALLOWED_MODES:
            return False, "LIVE_GATE_BLOCKED"
        if venue not in VENUES:
            return False, "LIVE_GATE_BLOCKED"
        if venue == "COINBASE" and live_armed is not True:
            return False, "LIVE_GATE_BLOCKED"
        if venue == "FAKE" and settings.environment == "production":
            return False, "LIVE_GATE_BLOCKED"
        return True, None
    except Exception:  # noqa: BLE001  (an unreadable gate is a blocked gate)
        return False, "LIVE_GATE_BLOCKED"
