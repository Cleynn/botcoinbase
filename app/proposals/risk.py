"""Deterministic risk assessment of a proposal. Advisory information for the reviewing ADMIN only.

It does not gate, apply or schedule anything: nothing in this system applies a proposal. The level
tells the reviewer how much scrutiny the (manual) follow-up work deserves.
"""

from __future__ import annotations

import re
from typing import Any, Final

from app.proposals.policy import normalise
from app.proposals.schema import Parsed

FACTOR_TEXT: Final[dict[str, str]] = {
    "CATEGORY_HIGH_IMPACT": "the category (strategy, risk or paper execution) affects trading behaviour",
    "CATEGORY_MEDIUM_IMPACT": "the category touches data, backtest or pair research",
    "MENTIONS_MONEY_PARAMETERS": "the text names grid, fee, band, level, capital or order parameters",
    "MENTIONS_MANY_PARAMETERS": "the text names five or more distinct trading parameters",
    "MENTIONS_EXCHANGE_OR_LIVE": "the text mentions the exchange, private access or live use",
    "THIN_EVIDENCE": "fewer than two evidence references",
    "NO_BACKTEST_EVIDENCE": "a trading-behaviour proposal cites no report or backtest evidence",
    "THIN_VALIDATION_PLAN": "the validation plan is very short",
    "THIN_ROLLBACK_PLAN": "the rollback plan is very short",
    "POLICY_BLOCKED": "the policy triage found violations",
}
REVIEW_DEPTH: Final = {
    "LOW": "standard review",
    "MEDIUM": "review by a second person and a backtest before any further step",
    "HIGH": "full change-review gates, a walk-forward backtest and paper validation",
    "BLOCKED": "not eligible: fix the policy violations and import a new proposal",
}
_PARAMETERS: Final = re.compile(
    r"\b(grid|fee|fees|spread|slippage|band|levels?|capital|reserve|deployment|breakout|drawdown|"
    r"position|order|orders|fill|fills|atr|ema|trend|range|stop|threshold|ratio|size)\b"
)
_EXCHANGE: Final = re.compile(r"\b(exchange|coinbase|private|live|api|websocket|withdraw)\b")
_HIGH: Final = {"strategy", "risk", "paper_execution"}
_MEDIUM: Final = {"data", "backtest", "pair_research"}


def assess(parsed: Parsed, *, blocked: bool) -> dict[str, Any]:
    factors: list[str] = []
    score = 0
    if parsed.category in _HIGH:
        factors.append("CATEGORY_HIGH_IMPACT")
        score += 3
    elif parsed.category in _MEDIUM:
        factors.append("CATEGORY_MEDIUM_IMPACT")
        score += 1
    text = normalise(" ".join([*parsed.texts.values(), *parsed.assumptions]))
    names = set(_PARAMETERS.findall(text))
    if names:
        factors.append("MENTIONS_MONEY_PARAMETERS")
        score += 2
    if len(names) >= 5:
        factors.append("MENTIONS_MANY_PARAMETERS")
        score += 1
    if _EXCHANGE.search(text):
        factors.append("MENTIONS_EXCHANGE_OR_LIVE")
        score += 3
    if len(parsed.evidence) < 2:
        factors.append("THIN_EVIDENCE")
        score += 1
    if parsed.category in _HIGH and not any(
        e.startswith(("report:", "backtest_run:")) or "backtest" in e for e in parsed.evidence
    ):
        factors.append("NO_BACKTEST_EVIDENCE")
        score += 2
    if len(parsed.texts["required_validation"]) < 40:
        factors.append("THIN_VALIDATION_PLAN")
        score += 1
    if len(parsed.texts["rollback_plan"]) < 40:
        factors.append("THIN_ROLLBACK_PLAN")
        score += 1
    if blocked:
        factors.append("POLICY_BLOCKED")
        level = "BLOCKED"
    else:
        level = "HIGH" if score >= 7 else "MEDIUM" if score >= 4 else "LOW"
    return {
        "level": level,
        "score": score,
        "factors": factors,
        "review_depth": REVIEW_DEPTH[level],
        "advisory_only": True,
    }
