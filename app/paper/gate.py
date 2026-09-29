"""The runtime gate that lets the pair lifecycle activate a pair for PAPER trading.

Activation needs the configured mode to be PAPER (production refuses PAPER until the safety
machinery exists, see `production_problems`) and the paper session to be PAUSED. A pair with
open paper orders or paper inventory is not clean, so it cannot be disabled or archived.
"""

from __future__ import annotations

from app.domain.pairs import PairRecord
from app.storage.repositories import Repos


class PaperRuntimeGate:
    def __init__(self, mode: str) -> None:
        self._mode = mode

    def activation_blockers(self, repos: Repos, pair: PairRecord) -> tuple[str, ...]:
        reasons: list[str] = []
        if self._mode != "PAPER":
            reasons.append("MODE_NOT_PAPER")
        session = repos.paper.session()
        if session.state != "PAUSED":
            reasons.append("PAPER_SESSION_NOT_PAUSED")
        return tuple(reasons)

    def is_clean(self, repos: Repos, pair: PairRecord) -> tuple[bool, tuple[str, ...]]:
        reasons: list[str] = []
        if repos.paper.open_orders():
            reasons.append("OPEN_PAPER_ORDERS")
        qty, _cost = repos.paper.position(pair.id)
        if qty > 0:
            reasons.append("PAPER_INVENTORY")
        return (not reasons, tuple(reasons))
