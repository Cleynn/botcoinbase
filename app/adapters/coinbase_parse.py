"""Strict parsing of Coinbase public responses into typed, sanitised records.

Everything from the exchange is untrusted input: numbers become Decimal (never float), identifiers
must match fixed patterns, free strings are reduced to a safe vocabulary or replaced by a sentinel,
and display names and descriptions are never kept. A field that cannot be parsed is recorded by
name in `malformed` so validation reports INCONCLUSIVE instead of guessing.

The response shapes are the ones documented in `docs/exchange-contract.md` (CF-8 to CF-10). They
have NOT been verified against the live API from this repository (AS-C1); the fixtures under
`tests/fixtures/coinbase/public_recorded` are synthetic and labelled as such.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final

from app.domain.pairs import ProductMetadata

PRODUCT_ID_RE: Final = re.compile(r"^[A-Z0-9]+-USDC$")
ANY_PRODUCT_ID_RE: Final = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+$")
CURRENCY_RE: Final = re.compile(r"^[A-Z0-9]{1,20}$")
STATUS_RE: Final = re.compile(r"^[A-Za-z0-9_-]{1,32}$")
MAX_PRODUCT_ID_LENGTH: Final = 24
STATUS_SENTINEL: Final = "UNPARSEABLE"
_DECIMAL_RE: Final = re.compile(r"^[0-9]{1,30}(\.[0-9]{1,30})?$")  # ASCII digits only
_MAX_CANDLES: Final = 350
_MAX_BOOK_LEVELS: Final = 500
_MAX_PRODUCTS: Final = 5000

_DECIMAL_FIELDS: Final = (
    "base_increment",
    "quote_increment",
    "price_increment",
    "base_min_size",
    "base_max_size",
    "quote_min_size",
    "quote_max_size",
)
_FLAG_FIELDS: Final = (
    "is_disabled",
    "trading_disabled",
    "cancel_only",
    "limit_only",
    "post_only",
    "auction_mode",
)


class ParseError(Exception):
    """A response could not be understood. The message is a fixed code, never response content."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def _reject_constant(_name: str) -> Any:
    raise ParseError("NON_FINITE_NUMBER")


def load_json(body: bytes) -> Any:
    try:
        return json.loads(body, parse_float=Decimal, parse_constant=_reject_constant)
    except ParseError:
        raise
    except (ValueError, RecursionError) as exc:
        raise ParseError("INVALID_JSON") from exc


def parse_decimal(value: Any) -> Decimal | None:
    """A plain non-negative decimal, or None. No exponents, signs, NaN or infinity."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        value = str(value)
    elif isinstance(value, Decimal):
        value = format(value, "f")
    if not isinstance(value, str) or not _DECIMAL_RE.fullmatch(value):
        return None
    try:
        return Decimal(value)
    except InvalidOperation:  # pragma: no cover  (the pattern already guarantees a valid number)
        return None


def decimal_text(value: Decimal | None) -> str | None:
    """Canonical text for hashing and display: no exponent, no trailing zeros."""
    if value is None:
        return None
    text = format(value.normalize(), "f")
    return text if "." not in text else text.rstrip("0").rstrip(".")


# ---------------------------------------------------------------------------- products
@dataclass(frozen=True)
class ProductListing:
    metadata: ProductMetadata
    volume_key: Decimal  # ordering only; never stored


def _flag(raw: dict[str, Any], name: str, malformed: list[str]) -> bool | None:
    value = raw.get(name)
    if isinstance(value, bool):
        return value
    malformed.append(name)
    return None


def _clean_id(value: Any) -> str | None:
    if isinstance(value, str) and ANY_PRODUCT_ID_RE.fullmatch(value) and len(value) <= 24:
        return value
    return None


def parse_product(raw: Any) -> ProductMetadata | None:
    """Static rules for one product, or None when its identity cannot be trusted."""
    if not isinstance(raw, dict):
        return None
    product_id = raw.get("product_id")
    if not isinstance(product_id, str) or not PRODUCT_ID_RE.fullmatch(product_id):
        return None
    if len(product_id) > MAX_PRODUCT_ID_LENGTH:
        return None
    base = raw.get("base_currency_id")
    quote = raw.get("quote_currency_id")
    if (
        base is None and quote is None
    ):  # some payloads omit the ids; derive them from the product id
        base, quote = product_id.rsplit("-", 1)
    if not (isinstance(base, str) and isinstance(quote, str)):
        return None
    if not (CURRENCY_RE.fullmatch(base) and CURRENCY_RE.fullmatch(quote)):
        return None
    if f"{base}-{quote}" != product_id:
        return None

    malformed: list[str] = []
    status_raw = raw.get("status")
    if isinstance(status_raw, str) and STATUS_RE.fullmatch(status_raw):
        status = status_raw
    else:
        status = STATUS_SENTINEL
        malformed.append("status")

    flags = {name: _flag(raw, name, malformed) for name in _FLAG_FIELDS}

    numbers: dict[str, Decimal | None] = {}
    for name in _DECIMAL_FIELDS:
        present = raw.get(name)
        if present in (None, ""):
            numbers[name] = None
            if name in ("base_increment", "quote_increment", "base_min_size"):
                malformed.append(name)  # required for validation; the others are optional
            continue
        parsed = parse_decimal(present)
        if parsed is None or parsed <= 0:
            numbers[name] = None
            malformed.append(name)
        else:
            numbers[name] = parsed

    alias_raw = raw.get("alias")
    alias: str | None = None
    if alias_raw not in (None, ""):
        alias = _clean_id(alias_raw)
        if alias is None:
            malformed.append("alias")
    alias_to: list[str] = []
    alias_to_raw = raw.get("alias_to")
    if isinstance(alias_to_raw, list):
        for item in alias_to_raw[:10]:
            cleaned = _clean_id(item)
            if cleaned is None:
                malformed.append("alias_to")
            else:
                alias_to.append(cleaned)
    elif alias_to_raw not in (None, ""):
        malformed.append("alias_to")

    product_type = raw.get("product_type")
    venue = raw.get("product_venue")
    return ProductMetadata(
        product_id=product_id,
        base_currency=base,
        quote_currency=quote,
        product_type=product_type if isinstance(product_type, str) else "",
        venue=venue if isinstance(venue, str) else "",
        status=status,
        alias=alias,
        alias_to=tuple(alias_to),
        malformed=tuple(sorted(set(malformed))),
        **flags,  # type: ignore[arg-type]
        **numbers,  # type: ignore[arg-type]
    )


def parse_product_response(body: bytes) -> ProductMetadata:
    data = load_json(body)
    if not isinstance(data, dict):
        raise ParseError("UNEXPECTED_SHAPE")
    product = parse_product(data)
    if product is None:
        raise ParseError("UNTRUSTED_PRODUCT")
    return product


@dataclass(frozen=True)
class ParsedProducts:
    listings: tuple[ProductListing, ...]  # -USDC products with a trustworthy identity
    seen: int  # entries in the response (capped)


def parse_product_list(body: bytes) -> ParsedProducts:
    data = load_json(body)
    if not isinstance(data, dict) or not isinstance(data.get("products"), list):
        raise ParseError("UNEXPECTED_SHAPE")
    listings: list[ProductListing] = []
    seen = 0
    for raw in data["products"][:_MAX_PRODUCTS]:
        seen += 1
        product = parse_product(raw)
        if product is None:
            continue
        volume = parse_decimal(raw.get("approximate_quote_24h_volume")) or Decimal(0)
        listings.append(ProductListing(product, volume))
    return ParsedProducts(tuple(listings), seen)


def metadata_sha256(meta: ProductMetadata) -> str:
    """Hash of the static rules; a new snapshot exists only when this changes."""
    body = {
        "product_id": meta.product_id,
        "base": meta.base_currency,
        "quote": meta.quote_currency,
        "type": meta.product_type,
        "venue": meta.venue,
        "status": meta.status,
        **{name: getattr(meta, name) for name in _FLAG_FIELDS},
        **{name: decimal_text(getattr(meta, name)) for name in _DECIMAL_FIELDS},
        "alias": meta.alias,
        "alias_to": list(meta.alias_to),
        "malformed": list(meta.malformed),
    }
    text = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("ascii")).hexdigest()


# ---------------------------------------------------------------------------- time
@dataclass(frozen=True)
class ServerTime:
    epoch_seconds: int
    epoch_millis: int | None

    def as_datetime(self) -> datetime:
        return datetime.fromtimestamp(self.epoch_seconds, UTC)


def _epoch(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,13}", value):
        return int(value)
    return None


def parse_server_time(body: bytes) -> ServerTime:
    data = load_json(body)
    if not isinstance(data, dict):
        raise ParseError("UNEXPECTED_SHAPE")
    seconds = _epoch(data.get("epochSeconds"))
    if seconds is None or not 1_500_000_000 <= seconds <= 4_000_000_000:
        raise ParseError("BAD_SERVER_TIME")
    return ServerTime(seconds, _epoch(data.get("epochMillis")))


# ---------------------------------------------------------------------------- candles
@dataclass(frozen=True)
class Candle:
    start: int  # UNIX seconds
    low: Decimal
    high: Decimal
    open: Decimal
    close: Decimal
    volume: Decimal


@dataclass(frozen=True)
class CandleSet:
    candles: tuple[Candle, ...]  # ascending by start, duplicates kept (quality check counts them)
    malformed: int  # entries that could not be parsed


def parse_candles(body: bytes) -> CandleSet:
    data = load_json(body)
    if not isinstance(data, dict) or not isinstance(data.get("candles"), list):
        raise ParseError("UNEXPECTED_SHAPE")
    parsed: list[Candle] = []
    malformed = 0
    for raw in data["candles"][:_MAX_CANDLES]:
        if not isinstance(raw, dict):
            malformed += 1
            continue
        start = _epoch(raw.get("start"))
        low, high = parse_decimal(raw.get("low")), parse_decimal(raw.get("high"))
        open_, close = parse_decimal(raw.get("open")), parse_decimal(raw.get("close"))
        volume = parse_decimal(raw.get("volume"))
        if start is None or None in (low, high, open_, close, volume):
            malformed += 1
            continue
        parsed.append(Candle(start, low, high, open_, close, volume))  # type: ignore[arg-type]
    if isinstance(data["candles"], list) and len(data["candles"]) > _MAX_CANDLES:
        malformed += len(data["candles"]) - _MAX_CANDLES
    parsed.sort(key=lambda c: c.start)
    return CandleSet(tuple(parsed), malformed)


# ---------------------------------------------------------------------------- order book
@dataclass(frozen=True)
class BookLevel:
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class OrderBook:
    bids: tuple[BookLevel, ...]  # best first
    asks: tuple[BookLevel, ...]  # best first
    time: datetime | None
    malformed: int


def _levels(raw: Any) -> tuple[list[BookLevel], int]:
    if not isinstance(raw, list):
        raise ParseError("UNEXPECTED_SHAPE")
    levels: list[BookLevel] = []
    bad = 0
    for item in raw[:_MAX_BOOK_LEVELS]:
        price = parse_decimal(item.get("price")) if isinstance(item, dict) else None
        size = parse_decimal(item.get("size")) if isinstance(item, dict) else None
        if price is None or size is None or price <= 0:
            bad += 1
            continue
        levels.append(BookLevel(price, size))
    return levels, bad


def _book_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not re.fullmatch(r"[0-9T:.\-+Z]{10,40}", value):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def parse_product_book(body: bytes) -> OrderBook:
    data = load_json(body)
    book = data.get("pricebook") if isinstance(data, dict) else None
    if not isinstance(book, dict):
        raise ParseError("UNEXPECTED_SHAPE")
    bids, bad_bids = _levels(book.get("bids"))
    asks, bad_asks = _levels(book.get("asks"))
    bids.sort(key=lambda level: level.price, reverse=True)
    asks.sort(key=lambda level: level.price)
    return OrderBook(tuple(bids), tuple(asks), _book_time(book.get("time")), bad_bids + bad_asks)
