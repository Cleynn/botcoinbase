"""Typed, sanitised records for private exchange reads, and strict parsers for them.

Required fields are strict (a missing or malformed one fails the whole response, which then counts
as an API failure and blocks trading); unknown extra fields are tolerated but counted as drift.
Numbers become Decimal, never float. The response shapes follow the documented Advanced Trade
shapes and are NOT verified against the live API from this repository (AS-C3, AS-C4): every fixture
in the tests is synthetic and labelled so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Final

from app.adapters.coinbase_parse import ParseError, load_json, parse_decimal

ORDER_ID_RE: Final = re.compile(r"^[A-Za-z0-9-]{8,64}$")
PRODUCT_RE: Final = re.compile(r"^[A-Z0-9]+-[A-Z0-9]+$")
CURRENCY_RE: Final = re.compile(r"^[A-Z0-9]{1,20}$")
MAX_ITEMS: Final = 1000

# CB-11 status map. Anything not listed, including UNKNOWN_ORDER_STATUS, is UNKNOWN.
STATUS_MAP: Final[dict[str, str]] = {
    "PENDING": "WORKING",
    "OPEN": "WORKING",
    "QUEUED": "WORKING",
    "EDIT_QUEUED": "WORKING",
    "CANCEL_QUEUED": "CANCEL_REQUESTED",
    "FILLED": "FILLED",
    "CANCELLED": "CANCELLED",
    "EXPIRED": "EXPIRED",
    "FAILED": "FAILED",
}
LOCAL_STATES: Final = (
    "WORKING",
    "CANCEL_REQUESTED",
    "FILLED",
    "CANCELLED",
    "EXPIRED",
    "FAILED",
    "UNKNOWN",
)
TERMINAL: Final = ("FILLED", "CANCELLED", "EXPIRED", "FAILED")


@dataclass(frozen=True)
class ExchangeOrder:
    order_id: str
    client_order_id: str
    product_id: str
    side: str
    status: str  # one of LOCAL_STATES
    order_type: str
    post_only: bool
    price: Decimal
    base_qty: Decimal
    filled_qty: Decimal
    created_time: datetime | None


@dataclass(frozen=True)
class ExchangeFill:
    fill_id: str
    order_id: str
    product_id: str
    side: str
    price: Decimal
    size: Decimal
    fee: Decimal
    liquidity: str  # MAKER | TAKER | UNKNOWN
    trade_time: datetime | None


@dataclass(frozen=True)
class Balance:
    currency: str
    available: Decimal
    hold: Decimal

    @property
    def total(self) -> Decimal:
        return self.available + self.hold


@dataclass(frozen=True)
class KeyPermissions:
    can_view: bool
    can_trade: bool
    can_transfer: bool
    portfolio_type: str


@dataclass(frozen=True)
class Page[T]:
    items: tuple[T, ...]
    has_next: bool
    cursor: str
    drift: int  # count of unknown extra fields seen (tolerated, counted)


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _need(raw: dict[str, Any], key: str, code: str) -> Any:
    if key not in raw:
        raise ParseError(code)
    return raw[key]


def _dec(value: Any, code: str) -> Decimal:
    out = parse_decimal(value)
    if out is None:
        raise ParseError(code)
    return out


def _id(value: Any, code: str) -> str:
    if not isinstance(value, str) or not ORDER_ID_RE.fullmatch(value):
        raise ParseError(code)
    return value


def _body(body: bytes, key: str) -> tuple[dict[str, Any], list[Any]]:
    data = load_json(body)
    if not isinstance(data, dict):
        raise ParseError("NOT_AN_OBJECT")
    items = data.get(key)
    if not isinstance(items, list) or len(items) > MAX_ITEMS:
        raise ParseError("BAD_LIST")
    return data, items


def _cursor(data: dict[str, Any]) -> tuple[bool, str]:
    has_next = data.get("has_next", False)
    cursor = data.get("cursor", "")
    if not isinstance(has_next, bool) or not isinstance(cursor, str) or len(cursor) > 256:
        raise ParseError("BAD_CURSOR")
    if has_next and not cursor:
        raise ParseError("BAD_CURSOR")
    return has_next, cursor


_ORDER_KNOWN: Final = frozenset(
    {
        "order_id",
        "product_id",
        "side",
        "client_order_id",
        "status",
        "order_configuration",
        "filled_size",
        "created_time",
        "order_type",
        "average_filled_price",
        "completion_percentage",
        "number_of_fills",
        "filled_value",
        "total_fees",
        "total_value_after_fees",
        "time_in_force",
        "settled",
        "product_type",
        "user_id",
        "size_in_quote",
        "size_inclusive_of_fees",
        "pending_cancel",
        "cancel_message",
        "reject_reason",
        "reject_message",
        "outstanding_hold_amount",
        "is_liquidation",
        "last_fill_time",
        "edit_history",
        "leverage",
        "margin_type",
        "retail_portfolio_id",
        "originating_order_id",
        "attached_order_id",
        "attached_order_configuration",
    }
)


def _parse_order(raw: Any) -> tuple[ExchangeOrder, int]:
    if not isinstance(raw, dict):
        raise ParseError("ORDER_MALFORMED")
    order_id = _id(_need(raw, "order_id", "ORDER_MALFORMED"), "ORDER_MALFORMED")
    client_id = _id(_need(raw, "client_order_id", "ORDER_MALFORMED"), "ORDER_MALFORMED")
    product = _need(raw, "product_id", "ORDER_MALFORMED")
    if not isinstance(product, str) or not PRODUCT_RE.fullmatch(product):
        raise ParseError("ORDER_MALFORMED")
    side = _need(raw, "side", "ORDER_MALFORMED")
    if side not in ("BUY", "SELL"):
        raise ParseError("ORDER_MALFORMED")
    status_raw = _need(raw, "status", "ORDER_MALFORMED")
    status = STATUS_MAP.get(status_raw, "UNKNOWN") if isinstance(status_raw, str) else "UNKNOWN"
    config = _need(raw, "order_configuration", "ORDER_MALFORMED")
    if not isinstance(config, dict) or len(config) != 1:
        raise ParseError("ORDER_MALFORMED")
    (kind, body), *_ = config.items()
    if kind != "limit_limit_gtc" or not isinstance(body, dict):
        # any other order kind is not ours; it is reported as unmodelled, which blocks
        order_type, post_only, price, qty = str(kind)[:32], False, Decimal(0), Decimal(0)
    else:
        order_type = "limit_limit_gtc"
        post_only = bool(body.get("post_only") is True)
        price = _dec(body.get("limit_price"), "ORDER_MALFORMED")
        qty = _dec(body.get("base_size"), "ORDER_MALFORMED")
    filled = _dec(raw.get("filled_size", "0"), "ORDER_MALFORMED")
    drift = len(set(raw) - _ORDER_KNOWN)
    return (
        ExchangeOrder(
            order_id, client_id, product, side, status, order_type, post_only, price, qty,
            filled, _time(raw.get("created_time")),
        ),
        drift,
    )  # fmt: skip


def parse_orders(body: bytes) -> Page[ExchangeOrder]:
    data, items = _body(body, "orders")
    parsed = [_parse_order(x) for x in items]
    has_next, cursor = _cursor(data)
    return Page(tuple(o for o, _ in parsed), has_next, cursor, sum(d for _, d in parsed))


def parse_order(body: bytes) -> ExchangeOrder:
    data = load_json(body)
    if not isinstance(data, dict) or "order" not in data:
        raise ParseError("ORDER_MALFORMED")
    return _parse_order(data["order"])[0]


_LIQUIDITY: Final = {"MAKER": "MAKER", "TAKER": "TAKER"}


def _parse_fill(raw: Any) -> ExchangeFill:
    if not isinstance(raw, dict):
        raise ParseError("FILL_MALFORMED")
    fill_id = _id(_need(raw, "entry_id", "FILL_MALFORMED"), "FILL_MALFORMED")
    order_id = _id(_need(raw, "order_id", "FILL_MALFORMED"), "FILL_MALFORMED")
    product = _need(raw, "product_id", "FILL_MALFORMED")
    if not isinstance(product, str) or not PRODUCT_RE.fullmatch(product):
        raise ParseError("FILL_MALFORMED")
    side = _need(raw, "side", "FILL_MALFORMED")
    if side not in ("BUY", "SELL"):
        raise ParseError("FILL_MALFORMED")
    liquidity = _LIQUIDITY.get(str(raw.get("liquidity_indicator", "")), "UNKNOWN")
    return ExchangeFill(
        fill_id,
        order_id,
        product,
        side,
        _dec(_need(raw, "price", "FILL_MALFORMED"), "FILL_MALFORMED"),
        _dec(_need(raw, "size", "FILL_MALFORMED"), "FILL_MALFORMED"),
        _dec(_need(raw, "commission", "FILL_MALFORMED"), "FILL_MALFORMED"),
        liquidity,
        _time(raw.get("trade_time")),
    )


def parse_fills(body: bytes) -> Page[ExchangeFill]:
    data, items = _body(body, "fills")
    fills = tuple(_parse_fill(x) for x in items)
    cursor = data.get("cursor", "")
    if not isinstance(cursor, str) or len(cursor) > 256:
        raise ParseError("BAD_CURSOR")
    return Page(fills, bool(cursor), cursor, 0)


def _money(raw: Any, code: str) -> Decimal:
    if not isinstance(raw, dict):
        raise ParseError(code)
    return _dec(raw.get("value"), code)


def parse_accounts(body: bytes) -> Page[Balance]:
    data, items = _body(body, "accounts")
    out: list[Balance] = []
    for raw in items:
        if not isinstance(raw, dict):
            raise ParseError("ACCOUNT_MALFORMED")
        currency = _need(raw, "currency", "ACCOUNT_MALFORMED")
        if not isinstance(currency, str) or not CURRENCY_RE.fullmatch(currency):
            raise ParseError("ACCOUNT_MALFORMED")
        out.append(
            Balance(
                currency,
                _money(_need(raw, "available_balance", "ACCOUNT_MALFORMED"), "ACCOUNT_MALFORMED"),
                _money(_need(raw, "hold", "ACCOUNT_MALFORMED"), "ACCOUNT_MALFORMED"),
            )
        )
    has_next, cursor = _cursor(data)
    return Page(tuple(out), has_next, cursor, 0)


def parse_key_permissions(body: bytes) -> KeyPermissions:
    data = load_json(body)
    if not isinstance(data, dict):
        raise ParseError("KEYPERM_MALFORMED")
    flags = []
    for key in ("can_view", "can_trade", "can_transfer"):
        value = data.get(key)
        if not isinstance(value, bool):
            raise ParseError("KEYPERM_MALFORMED")
        flags.append(value)
    ptype = data.get("portfolio_type", "")
    if not isinstance(ptype, str) or not re.fullmatch(r"[A-Z_]{0,32}", ptype):
        raise ParseError("KEYPERM_MALFORMED")
    return KeyPermissions(flags[0], flags[1], flags[2], ptype)
