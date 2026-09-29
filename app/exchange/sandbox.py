"""Sandbox support: REQUEST SHAPE only. It never simulates a market, a fill or an account.

The exchange sandbox is static (it returns canned responses), so it can only prove that a request
is well formed. `check_request_shape` validates what `OrderRequest.body()` would send against the
fixed post-only limit GTC schema; `check_recorded_response` validates that a RECORDED-STATIC sandbox
response has the shape the parsers expect. Neither function sends anything. Recorded responses are
labelled RECORDED-STATIC and are never behavioural evidence.
"""

from __future__ import annotations

from typing import Any, Final

from app.adapters.coinbase_parse import ParseError, load_json
from app.exchange.gateway import CLIENT_ID_RE, PRODUCT_RE
from app.exchange.models import parse_accounts, parse_orders

PROVENANCE: Final = "RECORDED-STATIC (request/response shape only; not behaviour)"
_TOP: Final = frozenset({"client_order_id", "product_id", "side", "order_configuration"})
_LIMIT: Final = frozenset({"base_size", "limit_price", "post_only"})


def check_request_shape(body: dict[str, Any]) -> list[str]:
    """Problems with a create-order body (empty list = well formed). Extra fields are problems."""
    problems: list[str] = []
    if set(body) != _TOP:
        problems.append("FIELDS")
    if not isinstance(body.get("client_order_id"), str) or not CLIENT_ID_RE.fullmatch(
        str(body.get("client_order_id"))
    ):
        problems.append("CLIENT_ORDER_ID")
    if not isinstance(body.get("product_id"), str) or not PRODUCT_RE.fullmatch(
        str(body.get("product_id"))
    ):
        problems.append("PRODUCT_ID")
    if body.get("side") not in ("BUY", "SELL"):
        problems.append("SIDE")
    config = body.get("order_configuration")
    if not isinstance(config, dict) or set(config) != {"limit_limit_gtc"}:
        problems.append("ORDER_CONFIGURATION")
        return problems
    limit = config["limit_limit_gtc"]
    if not isinstance(limit, dict) or set(limit) != _LIMIT:
        problems.append("LIMIT_FIELDS")
        return problems
    if limit.get("post_only") is not True:
        problems.append("POST_ONLY")
    for key in ("base_size", "limit_price"):
        from app.adapters.coinbase_parse import parse_decimal

        value = parse_decimal(limit.get(key))
        if value is None or value <= 0:
            problems.append(key.upper())
    return problems


def check_recorded_response(kind: str, body: bytes) -> list[str]:
    """Shape problems in a recorded static response (kind: 'orders' or 'accounts')."""
    try:
        if kind == "orders":
            parse_orders(body)
        elif kind == "accounts":
            parse_accounts(body)
        else:
            return ["UNKNOWN_KIND"]
        load_json(body)
    except ParseError as exc:
        return [exc.code]
    return []
