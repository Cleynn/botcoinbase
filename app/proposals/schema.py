"""Strict proposal schema and parser. Runs only in the host validator.

The parse is plain `json.loads` with hooks: duplicate keys, NaN/Infinity and excessive nesting are
refused before and during decoding; the result is checked against a closed schema (unknown keys are
rejected) and every string is checked for hidden or control characters. Nothing is evaluated,
templated, converted to another format or passed to anything that interprets it.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any, Final

CATEGORIES: Final = (
    "strategy",
    "risk",
    "pair_research",
    "data",
    "backtest",
    "paper_execution",
    "monitoring",
    "documentation",
    "code_quality",
)
FIELDS: Final = (
    "proposal_version",
    "proposal_id",
    "linked_review_package_id",
    "linked_review_package_sha256",
    "category",
    "summary",
    "evidence_references",
    "assumptions",
    "suggested_change",
    "expected_benefit",
    "risk_tradeoffs",
    "required_validation",
    "rollback_plan",
    "should_remain_no_trade_until_validated",
    "no_profit_guarantee",
)
TEXT_FIELDS: Final = (
    "summary",
    "suggested_change",
    "expected_benefit",
    "risk_tradeoffs",
    "required_validation",
    "rollback_plan",
)
_LIMITS: Final = {
    "summary": (10, 500),
    "suggested_change": (20, 2000),
    "expected_benefit": (10, 1000),
    "risk_tradeoffs": (10, 1000),
    "required_validation": (10, 1000),
    "rollback_plan": (10, 1000),
}
MAX_EVIDENCE: Final = 20
MAX_ASSUMPTIONS: Final = 20
MAX_ASSUMPTION_CHARS: Final = 300

_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{2,63}")
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_SHA = re.compile(r"[0-9a-f]{64}")
EVIDENCE_PATH: Final = re.compile(
    r"(?P<path>data/[a-z_]{3,40}\.jsonl|csv/[a-z_]{3,40}\.csv|efficiency_summary\.json"
    r"|manifest\.json)(?:#L[1-9][0-9]{0,5})?"
)
EVIDENCE_ID: Final = re.compile(
    r"(?P<kind>report|snapshot|backtest_run):(?P<id>[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}"
    r"-[0-9a-f]{4}-[0-9a-f]{12})"
)


class ProposalRejected(Exception):
    """Schema or parse failure. `rules` are fixed codes, one per problem."""

    def __init__(self, rules: list[str]) -> None:
        super().__init__(",".join(rules[:5]))
        self.rules = rules


@dataclass(frozen=True)
class Parsed:
    proposal_id: str
    linked_package_id: str
    linked_package_sha256: str
    category: str
    evidence: tuple[str, ...]
    assumptions: tuple[str, ...]
    texts: dict[str, str]
    should_remain_no_trade: bool
    no_profit_guarantee: bool

    def as_json(self) -> dict[str, Any]:
        return {
            "proposal_version": 1,
            "proposal_id": self.proposal_id,
            "linked_review_package_id": self.linked_package_id,
            "linked_review_package_sha256": self.linked_package_sha256,
            "category": self.category,
            "evidence_references": list(self.evidence),
            "assumptions": list(self.assumptions),
            **self.texts,
            "should_remain_no_trade_until_validated": self.should_remain_no_trade,
            "no_profit_guarantee": self.no_profit_guarantee,
        }


# ------------------------------------------------------------------ safe JSON decoding
def max_depth(text: str, limit: int) -> int:
    """Nesting depth of `text` without recursion; stops early once `limit` is exceeded."""
    depth = deepest = 0
    in_string = escaped = False
    for ch in text:
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
            deepest = max(deepest, depth)
            if deepest > limit:
                return deepest
        elif ch in "}]":
            depth -= 1
    return deepest


def _no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError("duplicate key")
        out[key] = value
    return out


def _refuse_constant(_name: str) -> Any:
    raise ValueError("non-finite number")


def decode_json(text: str, depth_limit: int) -> Any:
    if max_depth(text, depth_limit) > depth_limit:
        raise ProposalRejected(["JSON_TOO_DEEP"])
    try:
        return json.loads(text, object_pairs_hook=_no_duplicates, parse_constant=_refuse_constant)
    except RecursionError as exc:  # pragma: no cover  (the depth pre-check prevents it)
        raise ProposalRejected(["JSON_TOO_DEEP"]) from exc
    except ValueError as exc:
        message = str(exc)
        if "duplicate key" in message:
            raise ProposalRejected(["JSON_DUPLICATE_KEY"]) from exc
        if "non-finite" in message:
            raise ProposalRejected(["JSON_NON_FINITE_NUMBER"]) from exc
        raise ProposalRejected(["JSON_INVALID"]) from exc


# ------------------------------------------------------------------ schema
def _bad_chars(value: str, *, multiline: bool) -> str | None:
    for ch in value:
        category = unicodedata.category(ch)
        if category == "Cc" and not (multiline and ch in "\n\t"):
            return "CONTROL_CHARACTERS"
        if category in ("Cf", "Cs", "Co", "Cn", "Zl", "Zp"):
            return "HIDDEN_CHARACTERS"
    return None


def _string(
    obj: dict[str, Any], name: str, errors: list[str], lo: int, hi: int, *, multiline: bool
) -> str:
    value = obj.get(name)
    if not isinstance(value, str):
        errors.append(f"BAD_TYPE:{name}")
        return ""
    if len(value) > hi:
        errors.append(f"TOO_LONG:{name}")
        return ""
    if len(value) < lo or not value.strip():
        errors.append(f"TOO_SHORT:{name}")
    problem = _bad_chars(value, multiline=multiline)
    if problem:
        errors.append(f"{problem}:{name}")
    return value


def _list(obj: dict[str, Any], name: str, errors: list[str], lo: int, hi: int) -> list[Any]:
    value = obj.get(name)
    if not isinstance(value, list):
        errors.append(f"BAD_TYPE:{name}")
        return []
    if len(value) < lo:
        errors.append(f"TOO_FEW:{name}")
    if len(value) > hi:
        errors.append(f"TOO_MANY:{name}")
        return []
    return value


def validate(obj: Any) -> Parsed:
    """Check the decoded document against the closed schema. Raises `ProposalRejected`."""
    if not isinstance(obj, dict):
        raise ProposalRejected(["NOT_AN_OBJECT"])
    errors: list[str] = []
    if set(obj) - set(FIELDS):
        errors.append("UNKNOWN_FIELD")
    missing = [f"MISSING_FIELD:{name}" for name in FIELDS if name not in obj]
    if missing:
        raise ProposalRejected((errors + missing)[:20])
    version = obj["proposal_version"]
    if isinstance(version, bool) or not isinstance(version, int) or version != 1:
        errors.append("BAD_VERSION")
    ref, package_id, package_sha, category = (
        obj["proposal_id"],
        obj["linked_review_package_id"],
        obj["linked_review_package_sha256"],
        obj["category"],
    )
    if not isinstance(ref, str) or not _REF.fullmatch(ref):
        errors.append("BAD_VALUE:proposal_id")
    if not isinstance(package_id, str) or not _UUID.fullmatch(package_id):
        errors.append("BAD_VALUE:linked_review_package_id")
    if not isinstance(package_sha, str) or not _SHA.fullmatch(package_sha):
        errors.append("BAD_VALUE:linked_review_package_sha256")
    if not isinstance(category, str) or category not in CATEGORIES:
        errors.append("BAD_VALUE:category")
    texts = {
        name: _string(obj, name, errors, *_LIMITS[name], multiline=True) for name in TEXT_FIELDS
    }
    evidence = _list(obj, "evidence_references", errors, 1, MAX_EVIDENCE)
    if not all(
        isinstance(e, str) and (EVIDENCE_PATH.fullmatch(e) or EVIDENCE_ID.fullmatch(e))
        for e in evidence
    ):
        errors.append("BAD_EVIDENCE_REFERENCE")
    elif len(set(evidence)) != len(evidence):
        errors.append("DUPLICATE_EVIDENCE_REFERENCE")
    assumptions = _list(obj, "assumptions", errors, 0, MAX_ASSUMPTIONS)
    for item in assumptions:
        if not isinstance(item, str) or not 1 <= len(item) <= MAX_ASSUMPTION_CHARS:
            errors.append("BAD_VALUE:assumptions")
            break
        problem = _bad_chars(item, multiline=False)
        if problem:
            errors.append(f"{problem}:assumptions")
            break
    for name in ("should_remain_no_trade_until_validated", "no_profit_guarantee"):
        if not isinstance(obj[name], bool):
            errors.append(f"BAD_TYPE:{name}")
    if errors:
        raise ProposalRejected(list(dict.fromkeys(errors))[:20])
    return Parsed(
        proposal_id=ref,
        linked_package_id=package_id,
        linked_package_sha256=package_sha,
        category=category,
        evidence=tuple(evidence),
        assumptions=tuple(assumptions),
        texts=texts,
        should_remain_no_trade=bool(obj["should_remain_no_trade_until_validated"]),
        no_profit_guarantee=bool(obj["no_profit_guarantee"]),
    )


def parse_text(text: str, depth_limit: int) -> Parsed:
    return validate(decode_json(text, depth_limit))
