"""Typed-field sanitizer and text scanner for review packages.

Two layers, deliberately redundant:
1. Every exported row is rebuilt from a schema of typed fields. A value that is not exactly the
   declared type (fixed vocabulary, bounded number, canonical decimal, UUID, product id, timestamp,
   SHA-256) is refused, never "cleaned". Free-text columns have no schema entry, so they cannot
   appear. Unknown keys are refused.
2. The finished bytes are scanned for secrets, private material, network and filesystem details,
   exception traces and action-capable content. Any hit fails the whole package.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Final
from uuid import UUID

from app.domain.money import canonical

Validator = Callable[[Any], Any]


class Rejected(ValueError):
    """A value failed its typed schema or the scanner found forbidden content. `code` is fixed."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


# ------------------------------------------------------------------ typed fields
def enum(*allowed: str) -> Validator:
    def check(v: Any) -> str:
        if not isinstance(v, str) or v not in allowed:
            raise Rejected("BAD_ENUM")
        return v

    return check


def integer(lo: int, hi: int) -> Validator:
    def check(v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, int) or not lo <= v <= hi:
            raise Rejected("BAD_INT")
        return v

    return check


def decimal_str(v: Any) -> str:
    """A finite Decimal (or its canonical string) as a bounded canonical string."""
    try:
        d = v if isinstance(v, Decimal) else Decimal(str(v)) if isinstance(v, str | int) else None
    except ArithmeticError:
        d = None
    if isinstance(v, bool) or d is None or not d.is_finite() or abs(d) >= Decimal("1e24"):
        raise Rejected("BAD_DECIMAL")
    return canonical(d)


def uuid_str(v: Any) -> str:
    try:
        return str(UUID(str(v)))
    except ValueError as exc:
        raise Rejected("BAD_UUID") from exc


_PRODUCT = re.compile(r"^[A-Z0-9]{1,20}-USDC$")
_CODE = re.compile(r"^[A-Z][A-Z0-9_]{1,39}$")
_EVENT = re.compile(r"^[a-z_.]{3,64}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_VERSION = re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}$")


def _pattern(rx: re.Pattern[str], code: str) -> Validator:
    def check(v: Any) -> str:
        if not isinstance(v, str) or not rx.fullmatch(v):
            raise Rejected(code)
        return v

    return check


product = _pattern(_PRODUCT, "BAD_PRODUCT")
code = _pattern(_CODE, "BAD_CODE")
event_code = _pattern(_EVENT, "BAD_EVENT")
sha256 = _pattern(_SHA, "BAD_SHA")
version = _pattern(_VERSION, "BAD_VERSION")


def timestamp(v: Any) -> str:
    if not isinstance(v, datetime) or v.tzinfo is None:
        raise Rejected("BAD_TIMESTAMP")
    return v.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def epoch_ts(v: Any) -> str:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 <= v < 4_102_444_800:
        raise Rejected("BAD_EPOCH")
    return datetime.fromtimestamp(v, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def day(v: Any) -> str:
    if not isinstance(v, date):
        raise Rejected("BAD_DAY")
    return v.isoformat()


def code_or_none(v: Any) -> str | None:
    """A fixed-vocabulary code, or None. Anything else (maybe free text) is dropped, not kept."""
    return v if isinstance(v, str) and _CODE.fullmatch(v) else None


def optional(inner: Validator) -> Validator:
    return lambda v: None if v is None else inner(v)


def decimal_list(v: Any) -> list[str]:
    if not isinstance(v, list | tuple) or not 1 <= len(v) <= 8:
        raise Rejected("BAD_LIST")
    return [decimal_str(x) for x in v]


def sanitize_row(schema: Mapping[str, Validator], raw: Mapping[str, Any]) -> dict[str, Any]:
    """Rebuild a row from its schema. Unknown or missing keys and any invalid value are refused."""
    if set(raw) != set(schema):
        raise Rejected("ROW_KEYS")
    return {name: check(raw[name]) for name, check in schema.items()}


# ------------------------------------------------------------------ text scanner
_SHA_OR_UUID = re.compile(
    r"\b[0-9a-f]{64}\b|\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"
)
_BASE_RULES: Final = (
    ("PRIVATE_KEY", re.compile(r"-----BEGIN|-----END")),
    ("URL", re.compile(r"[a-z][a-z0-9+.-]{1,10}://", re.IGNORECASE)),
    ("EMAIL", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")),
    ("IPV4", re.compile(r"(?<![\w.])\d{1,3}(?:\.\d{1,3}){3}(?![\w.])")),
    ("IPV6", re.compile(r"(?<![\w:])(?:[0-9A-Fa-f]{0,4}:){3,}[0-9A-Fa-f]{0,4}(?![\w:])")),
    (
        "FILE_PATH",
        re.compile(
            r"(?:^|[\s\"'=(,])/(?:home|root|etc|var|usr|tmp|data|app|opt|proc|review|run|mnt)\b"
            r"|[A-Za-z]:\\"
        ),
    ),
    ("TRACEBACK", re.compile(r"Traceback \(most recent|File \".{1,200}\", line \d+")),
    ("JWT", re.compile(r"eyJ[A-Za-z0-9_-]{10,}")),
    ("LONG_TOKEN", re.compile(r"[A-Za-z0-9_+/=-]{40,}")),
    (
        "ACTION_CAPABLE",
        re.compile(
            r"<script|javascript:|rm\s+-rf|\bcurl\s|\bwget\s|\bsudo\s|create_order|cancel_order"
            r"|/api/v3|\beval\(|\bexec\(|subprocess|os\.system|DROP\s+TABLE|<iframe|onerror\s*=",
            re.IGNORECASE,
        ),
    ),
)
_SECRET_WORDS: Final = re.compile(
    r"pass(?:word|wd|phrase)|secret|api[_ -]?key|private[_ -]?key|authorization|bearer\b|cookie"
    r"|session[_ -]?id|csrf|\bjwt\b|token|credential|user[_ -]?agent|x-forwarded",
    re.IGNORECASE,
)


def scan_text(text: str, *, strict: bool = True) -> list[str]:
    """Rule ids of forbidden content in `text` (empty when clean).

    Declared SHA-256 digests and UUIDs are removed first. `strict` also refuses secret-like words;
    it is used for data files, while the fixed documentation files use the base rules only.
    """
    body = _SHA_OR_UUID.sub("", text)
    hits = [rule for rule, rx in _BASE_RULES if rx.search(body)]
    if strict and _SECRET_WORDS.search(body):
        hits.append("SECRET_WORD")
    return hits


def neutralise_cell(value: str) -> str:
    """Stop spreadsheet formula injection; plain numbers (including negatives) are left alone."""
    if value and value[0] in "=+-@\t\r" and not re.fullmatch(r"-?\d+(\.\d+)?", value):
        return "'" + value
    return value
