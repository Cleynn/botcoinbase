"""The exchange boundary: strict parsers, the GET-only private adapter, the feed, the request shape
and the scripted fake. ALL responses here are SYNTHETIC (shapes follow the documented API and are
unverified against the live one, AS-C3/AS-C4). No network is used: httpx.MockTransport only."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx
import pytest

from app.adapters.coinbase_parse import ParseError
from app.config import ExchangeSettings
from app.exchange import coinbase_private as cp
from app.exchange import models as em
from app.exchange import sandbox
from app.exchange.errors import ExchangeError, NoCredentials
from app.exchange.fake import FakeExchange, Fault
from app.exchange.gateway import OrderRequest
from app.exchange.websocket import MAX_MESSAGE_BYTES, UserFeed

D = Decimal
T0 = datetime(2026, 9, 29, 12, 0, tzinfo=UTC)
CLIENT = "0f0f0f0f-1111-4222-8333-444444444444"


def order_json(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "order_id": "ord-0000001",
        "client_order_id": CLIENT,
        "product_id": "BTC-USDC",
        "side": "BUY",
        "status": "OPEN",
        "filled_size": "0",
        "created_time": "2026-09-29T12:00:00Z",
        "order_configuration": {
            "limit_limit_gtc": {"base_size": "0.1", "limit_price": "100", "post_only": True}
        },
    }
    base.update(over)
    return base


def body(**kw: Any) -> bytes:
    return json.dumps(kw).encode()


# ------------------------------------------------------------------ order parsing
@pytest.mark.parametrize(
    ("raw", "local"),
    [
        ("PENDING", "WORKING"), ("OPEN", "WORKING"), ("QUEUED", "WORKING"),
        ("EDIT_QUEUED", "WORKING"), ("CANCEL_QUEUED", "CANCEL_REQUESTED"), ("FILLED", "FILLED"),
        ("CANCELLED", "CANCELLED"), ("EXPIRED", "EXPIRED"), ("FAILED", "FAILED"),
        ("UNKNOWN_ORDER_STATUS", "UNKNOWN"), ("SOMETHING_NEW", "UNKNOWN"), ("", "UNKNOWN"),
    ],
)  # fmt: skip
def test_the_status_map_is_the_documented_one_and_everything_else_is_unknown(
    raw: str, local: str
) -> None:
    page = em.parse_orders(body(orders=[order_json(status=raw)], has_next=False))
    assert page.items[0].status == local


def test_a_good_order_parses_to_exact_decimals() -> None:
    (o,) = em.parse_orders(body(orders=[order_json()], has_next=False)).items
    assert o.price == D("100") and o.base_qty == D("0.1") and o.post_only is True
    assert o.order_type == "limit_limit_gtc" and o.created_time == T0


def test_unknown_extra_fields_are_tolerated_and_counted() -> None:
    page = em.parse_orders(body(orders=[order_json(brand_new="x", another=1)], has_next=False))
    assert page.drift == 2 and len(page.items) == 1


@pytest.mark.parametrize(
    "mutate",
    [
        lambda o: o.pop("order_id"),
        lambda o: o.pop("client_order_id"),
        lambda o: o.pop("product_id"),
        lambda o: o.pop("side"),
        lambda o: o.pop("status"),
        lambda o: o.pop("order_configuration"),
        lambda o: o.update(order_id="x"),
        lambda o: o.update(client_order_id="bad id!"),
        lambda o: o.update(product_id="btc-usdc"),
        lambda o: o.update(side="HOLD"),
        lambda o: o.update(filled_size="1e3"),
        lambda o: o.update(filled_size="-1"),
        lambda o: o.update(filled_size="NaN"),
        lambda o: o.update(order_configuration={}),
        lambda o: o.update(order_configuration={"a": {}, "b": {}}),
        lambda o: o["order_configuration"]["limit_limit_gtc"].update(limit_price="abc"),
        lambda o: o["order_configuration"]["limit_limit_gtc"].pop("base_size"),
    ],
)
def test_a_missing_or_malformed_required_field_fails_the_whole_response(mutate: Any) -> None:
    raw = order_json()
    mutate(raw)
    with pytest.raises(ParseError):
        em.parse_orders(body(orders=[raw], has_next=False))


def test_another_order_kind_is_reported_as_not_ours_never_as_a_limit_order() -> None:
    raw = order_json(order_configuration={"market_market_ioc": {"quote_size": "10"}})
    (o,) = em.parse_orders(body(orders=[raw], has_next=False)).items
    assert o.order_type == "market_market_ioc" and o.post_only is False and o.price == 0


@pytest.mark.parametrize(
    "bad", [b"[]", b"null", b"not json", b'{"orders": "x"}', b'{"orders": {}}', b"{}"]
)
def test_non_object_or_listless_bodies_are_refused(bad: bytes) -> None:
    with pytest.raises(ParseError):
        em.parse_orders(bad)


def test_cursor_rules() -> None:
    with pytest.raises(ParseError):
        em.parse_orders(body(orders=[], has_next=True, cursor=""))
    with pytest.raises(ParseError):
        em.parse_orders(body(orders=[], has_next="yes", cursor="c"))
    with pytest.raises(ParseError):
        em.parse_orders(body(orders=[], has_next=False, cursor="x" * 300))
    assert em.parse_orders(body(orders=[], has_next=True, cursor="abc")).cursor == "abc"


def test_too_many_items_is_refused() -> None:
    with pytest.raises(ParseError):
        em.parse_orders(body(orders=[order_json()] * (em.MAX_ITEMS + 1), has_next=False))


def test_numbers_are_never_floats() -> None:
    with pytest.raises(ParseError):
        em.parse_orders(
            b'{"orders":[{"order_id":"ord-0000001","filled_size":0.1}],"has_next":false}'
        )


# ------------------------------------------------------------------ fills, accounts, key permissions
def fill_json(**over: Any) -> dict[str, Any]:
    base = {
        "entry_id": "fill-0000001", "order_id": "ord-0000001", "product_id": "BTC-USDC",
        "side": "BUY", "price": "100", "size": "0.1", "commission": "0.02",
        "liquidity_indicator": "MAKER", "trade_time": "2026-09-29T12:01:00Z",
    }  # fmt: skip
    base.update(over)
    return base


def test_fills_parse_and_map_liquidity() -> None:
    fills = em.parse_fills(
        body(
            fills=[
                fill_json(),
                fill_json(liquidity_indicator="TAKER", entry_id="fill-0000002"),
                fill_json(
                    liquidity_indicator="UNKNOWN_LIQUIDITY_INDICATOR", entry_id="fill-0000003"
                ),
            ]
        )
    ).items
    assert [f.liquidity for f in fills] == ["MAKER", "TAKER", "UNKNOWN"]
    assert fills[0].fee == D("0.02") and fills[0].size == D("0.1")


@pytest.mark.parametrize(
    "field", ["entry_id", "order_id", "product_id", "side", "price", "size", "commission"]
)
def test_a_fill_missing_a_required_field_is_refused(field: str) -> None:
    raw = fill_json()
    raw.pop(field)
    with pytest.raises(ParseError):
        em.parse_fills(body(fills=[raw]))


def test_accounts_total_is_available_plus_hold() -> None:
    page = em.parse_accounts(
        body(
            accounts=[
                {
                    "currency": "USDC",
                    "available_balance": {"value": "40.5"},
                    "hold": {"value": "9.5"},
                }
            ],
            has_next=False,
        )
    )
    assert page.items[0].total == D("50")


@pytest.mark.parametrize(
    "acct",
    [
        {"currency": "usdc", "available_balance": {"value": "1"}, "hold": {"value": "0"}},
        {"currency": "USDC", "available_balance": {"value": "1"}},
        {"currency": "USDC", "available_balance": "1", "hold": {"value": "0"}},
        {"currency": "USDC", "available_balance": {"value": "-1"}, "hold": {"value": "0"}},
    ],
)
def test_malformed_accounts_are_refused(acct: dict[str, Any]) -> None:
    with pytest.raises(ParseError):
        em.parse_accounts(body(accounts=[acct], has_next=False))


def test_key_permissions() -> None:
    kp = em.parse_key_permissions(
        body(can_view=True, can_trade=True, can_transfer=False, portfolio_type="DEFAULT")
    )
    assert kp.can_trade and not kp.can_transfer
    with pytest.raises(ParseError):
        em.parse_key_permissions(body(can_view=True, can_trade="yes", can_transfer=False))
    with pytest.raises(ParseError):
        em.parse_key_permissions(body(can_view=True, can_trade=True))


# ------------------------------------------------------------------ the order request and the sandbox shape
def request(**over: Any) -> OrderRequest:
    args: dict[str, Any] = {
        "client_order_id": CLIENT, "product_id": "BTC-USDC", "side": "BUY",
        "price": D("100"), "base_qty": D("0.1"),
    }  # fmt: skip
    args.update(over)
    return OrderRequest(**args)


def test_the_request_body_is_a_post_only_limit_gtc_order_and_nothing_else() -> None:
    body_ = request().body()
    assert body_ == {
        "client_order_id": CLIENT,
        "product_id": "BTC-USDC",
        "side": "BUY",
        "order_configuration": {
            "limit_limit_gtc": {"base_size": "0.1", "limit_price": "100", "post_only": True}
        },
    }
    assert sandbox.check_request_shape(body_) == []


@pytest.mark.parametrize(
    "over",
    [
        {"client_order_id": "not-a-uuid"}, {"client_order_id": CLIENT.upper()},
        {"product_id": "BTC-USD"}, {"product_id": "btc-usdc"}, {"side": "HOLD"},
        {"price": D("0")}, {"price": D("-1")}, {"base_qty": D("0")},
    ],
)  # fmt: skip
def test_an_invalid_request_cannot_be_constructed(over: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        request(**over)


def test_there_is_no_way_to_spell_a_market_order() -> None:
    fields = set(OrderRequest.__dataclass_fields__)
    assert fields == {"client_order_id", "product_id", "side", "price", "base_qty"}
    text = json.dumps(request().body())
    for word in (
        "market",
        "stop",
        "ioc",
        "fok",
        "leverage",
        "margin",
        "attached",
        'post_only": false',
    ):
        assert word not in text


@pytest.mark.parametrize(
    ("mutate", "problem"),
    [
        (lambda b: b.update(extra="x"), "FIELDS"),
        (lambda b: b.update(client_order_id="x"), "CLIENT_ORDER_ID"),
        (lambda b: b.update(product_id="BTC-USD"), "PRODUCT_ID"),
        (lambda b: b.update(side="SELL_ALL"), "SIDE"),
        (lambda b: b.update(order_configuration={"market_market_ioc": {}}), "ORDER_CONFIGURATION"),
        (
            lambda b: b["order_configuration"]["limit_limit_gtc"].update(post_only=False),
            "POST_ONLY",
        ),
        (
            lambda b: b["order_configuration"]["limit_limit_gtc"].update(limit_price="0"),
            "LIMIT_PRICE",
        ),
        (
            lambda b: b["order_configuration"]["limit_limit_gtc"].update(base_size="1e3"),
            "BASE_SIZE",
        ),
        (
            lambda b: b["order_configuration"]["limit_limit_gtc"].update(leverage="3"),
            "LIMIT_FIELDS",
        ),
    ],
)
def test_the_sandbox_shape_check_finds_each_problem(mutate: Any, problem: str) -> None:
    b = request().body()
    mutate(b)
    assert problem in sandbox.check_request_shape(b)


def test_recorded_static_responses_are_checked_for_shape_only() -> None:
    assert "RECORDED-STATIC" in sandbox.PROVENANCE
    good = body(orders=[order_json()], has_next=False)
    assert sandbox.check_recorded_response("orders", good) == []
    assert sandbox.check_recorded_response("orders", b"{}") != []
    assert sandbox.check_recorded_response("nonsense", good) == ["UNKNOWN_KIND"]
    assert not hasattr(sandbox, "simulate") and not hasattr(sandbox, "fill")


# ------------------------------------------------------------------ the private adapter
class Recorder:
    def __init__(self, responses: list[httpx.Response] | None = None) -> None:
        self.requests: list[httpx.Request] = []
        self.responses = responses or []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.responses:
            return self.responses.pop(0)
        return httpx.Response(200, json={"accounts": [], "has_next": False})


class TestSigner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, str]] = []

    def bearer(self, method: str, host: str, path: str) -> str:
        self.calls.append((method, host, path))
        return "TEST-TOKEN-NOT-A-CREDENTIAL"


def reader(rec: Recorder, signer: Any | None = None, **kw: Any) -> cp.CoinbasePrivateReader:
    return cp.CoinbasePrivateReader(
        ExchangeSettings(), signer or TestSigner(), transport=httpx.MockTransport(rec),
        sleep=lambda _s: None, **kw,
    )  # fmt: skip


def json_response(payload: Any, status: int = 200, **headers: str) -> httpx.Response:
    return httpx.Response(
        status, json=payload, headers={"content-type": "application/json", **headers}
    )


def test_the_default_signer_refuses_and_no_request_is_ever_made() -> None:
    rec = Recorder()
    r = reader(rec, cp.NullSigner())
    for call in (
        r.list_accounts,
        r.key_permissions,
        lambda: r.get_order("ord-0000001"),
        lambda: r.list_orders(T0, T0 + timedelta(hours=1)),
        lambda: r.list_fills(T0, T0 + timedelta(hours=1)),
    ):
        with pytest.raises(NoCredentials):
            call()
    assert rec.requests == []


def test_reads_are_get_only_signed_per_request_and_hit_only_allowlisted_paths() -> None:
    rec = Recorder([
        json_response({"accounts": [], "has_next": False}),
        json_response({"orders": [], "has_next": False}),
        json_response({"fills": [], "cursor": ""}),
        json_response({"order": order_json()}),
        json_response({"can_view": True, "can_trade": True, "can_transfer": False, "portfolio_type": "DEFAULT"}),
    ])  # fmt: skip
    signer = TestSigner()
    r = reader(rec, signer)
    r.list_accounts()
    r.list_orders(T0, T0 + timedelta(hours=1))
    r.list_fills(T0, T0 + timedelta(hours=1))
    assert r.get_order("ord-0000001") is not None
    r.key_permissions()
    assert {q.method for q in rec.requests} == {"GET"}
    assert [q.url.path for q in rec.requests] == [
        "/api/v3/brokerage/accounts",
        "/api/v3/brokerage/orders/historical/batch",
        "/api/v3/brokerage/orders/historical/fills",
        "/api/v3/brokerage/orders/historical/ord-0000001",
        "/api/v3/brokerage/key_permissions",
    ]
    assert len(signer.calls) == 5 and all(
        c[0] == "GET" and c[1] == "api.coinbase.com" for c in signer.calls
    )
    assert all(
        q.headers["authorization"] == "Bearer TEST-TOKEN-NOT-A-CREDENTIAL" for q in rec.requests
    )
    assert all(q.content == b"" for q in rec.requests)  # no body, ever
    assert all("sort" not in str(q.url) for q in rec.requests)  # default sort only (CB-11)


def test_the_window_is_sent_as_utc_dates_and_pages_are_deduplicated() -> None:
    rec = Recorder([
        json_response({"orders": [order_json()], "has_next": True, "cursor": "c1"}),
        json_response({"orders": [order_json(), order_json(order_id="ord-0000002", client_order_id="0f0f0f0f-1111-4222-8333-444444444445")], "has_next": False}),
    ])  # fmt: skip
    orders = reader(rec).list_orders(T0, T0 + timedelta(hours=1))
    assert [o.order_id for o in orders] == ["ord-0000001", "ord-0000002"]
    first, second = rec.requests
    assert first.url.params["start_date"] == "2026-09-29T12:00:00Z"
    assert first.url.params["end_date"] == "2026-09-29T13:00:00Z"
    assert second.url.params["cursor"] == "c1"


def test_paging_guards() -> None:
    loop = Recorder([json_response({"orders": [], "has_next": True, "cursor": "same"})] * 3)
    with pytest.raises(ExchangeError, match="PAGING_LOOP"):
        reader(loop).list_orders(T0, T0 + timedelta(hours=1))
    endless = Recorder(
        [json_response({"orders": [], "has_next": True, "cursor": f"c{i}"}) for i in range(40)]
    )
    with pytest.raises(ExchangeError, match="PAGING_CAP"):
        reader(endless).list_orders(T0, T0 + timedelta(hours=1))
    assert len(endless.requests) == cp.MAX_PAGES


def test_paging_time_budget() -> None:
    clock = iter([0.0, 1.0, 999.0, 999.0, 999.0, 999.0])
    rec = Recorder(
        [
            json_response({"orders": [], "has_next": True, "cursor": "a"}),
            json_response({"orders": [], "has_next": False}),
        ]
    )
    r = reader(rec, monotonic=lambda: next(clock, 999.0))
    with pytest.raises(ExchangeError, match="PAGING_TIME_BUDGET"):
        r.list_orders(T0, T0 + timedelta(hours=1))


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (httpx.Response(401, json={}), "AUTH_REJECTED"),
        (httpx.Response(403, json={}), "AUTH_REJECTED"),
        (httpx.Response(429, json={}), "RATE_LIMITED"),
        (httpx.Response(500, json={}), "SERVER_ERROR"),
        (httpx.Response(503, json={}), "SERVER_ERROR"),
        (httpx.Response(400, json={}), "HTTP_ERROR"),
        (httpx.Response(302, headers={"location": "https://evil.example/"}), "REDIRECT_REFUSED"),
        (
            httpx.Response(200, text="<html>", headers={"content-type": "text/html"}),
            "BAD_CONTENT_TYPE",
        ),
        (
            httpx.Response(
                200,
                content=b"{}",
                headers={"content-type": "application/json", "content-length": str(10**9)},
            ),
            "TOO_LARGE",
        ),
    ],
)
def test_http_failures_map_to_fixed_codes_and_redirects_are_never_followed(
    response: httpx.Response, code: str
) -> None:
    rec = Recorder([response])
    with pytest.raises(ExchangeError) as info:
        reader(rec).list_accounts()
    assert info.value.code == code
    assert len(rec.requests) == 1  # a redirect is not followed


def test_a_body_over_the_cap_is_refused_while_streaming() -> None:
    big = json_response(
        {"accounts": ["x" * 100] * 10},
    )
    small = cp.CoinbasePrivateReader(
        ExchangeSettings.model_construct(
            **{**ExchangeSettings().model_dump(), "max_response_bytes": 100}
        ),
        TestSigner(),
        transport=httpx.MockTransport(Recorder([big])),
    )
    with pytest.raises(ExchangeError, match="TOO_LARGE"):
        small.list_accounts()


def test_transport_errors_are_mapped_and_carry_no_response_data() -> None:
    def boom(_r: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("secret detail")

    r = cp.CoinbasePrivateReader(
        ExchangeSettings(), TestSigner(), transport=httpx.MockTransport(boom)
    )
    with pytest.raises(ExchangeError) as info:
        r.list_accounts()
    assert info.value.code == "TIMEOUT" and "secret" not in str(info.value)


def test_a_malformed_body_is_a_failure_not_a_partial_result() -> None:
    rec = Recorder([json_response({"accounts": [{"currency": "USDC"}], "has_next": False})])
    with pytest.raises(ExchangeError, match="UNEXPECTED_RESPONSE"):
        reader(rec).list_accounts()


def test_get_order_404_is_none_and_a_bad_id_never_reaches_a_url() -> None:
    rec = Recorder([httpx.Response(404, json={})])
    assert reader(rec).get_order("ord-0000001") is None
    for bad in ("../secret", "a/b", "x", "ord 1", ""):
        with pytest.raises(ExchangeError, match="BAD_REQUEST"):
            reader(Recorder()).get_order(bad)


def test_a_disallowed_path_or_parameter_is_refused_before_signing() -> None:
    signer = TestSigner()
    r = reader(Recorder(), signer)
    for path in (
        "/orders",
        "/accounts/x/y",
        "/portfolios",
        "/orders/batch_cancel",
        "/orders/historical/a/b",
    ):
        with pytest.raises(ExchangeError, match="BAD_REQUEST"):
            r._get(path, {})
    with pytest.raises(ExchangeError, match="BAD_REQUEST"):
        r._get(cp.PATH_ACCOUNTS, {"sort": "x"})
    assert signer.calls == []


def _code_words(path: str) -> set[str]:
    """Identifiers and non-docstring string literals of a module (prose in docstrings excluded)."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(path).read_text())
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef):
            first = node.body[0] if node.body else None
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    words: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            words.add(node.id)
        elif isinstance(node, ast.Attribute):
            words.add(node.attr)
        elif isinstance(node, ast.FunctionDef):
            words.add(node.name)
        elif (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
        ):
            words.add(node.value)
    return words


def test_the_adapter_module_has_no_write_method_or_credential_code() -> None:
    words = _code_words(cp.__file__)
    assert cp.METHOD == "GET"
    for banned in (
        "POST",
        "DELETE",
        "PUT",
        "PATCH",
        "create_order",
        "cancel_orders",
        "cancel",
        "environ",
        "getenv",
        "hmac",
        "private_key",
    ):
        assert banned not in words, banned
    assert not any(
        w.lower().startswith(("post", "delete", "cancel", "submit", "create"))
        for w in words
        if w.isidentifier()
    )


# ------------------------------------------------------------------ the feed
def message(seq: int, orders: list[dict[str, Any]] | None = None, channel: str = "user") -> bytes:
    event = {"type": "update", "orders": orders or []}
    return json.dumps({"channel": channel, "sequence_num": seq, "events": [event]}).encode()


def feed_order(**over: Any) -> dict[str, Any]:
    base = {
        "client_order_id": CLIENT,
        "order_id": "ord-0000001",
        "status": "OPEN",
        "cumulative_quantity": "0",
    }
    base.update(over)
    return base


def test_feed_hints_are_parsed_in_the_local_vocabulary() -> None:
    feed = UserFeed()
    (hint,) = feed.handle(message(0, [feed_order(status="FILLED", cumulative_quantity="0.1")]), T0)
    assert hint.status == "FILLED" and hint.filled_qty == "0.1" and hint.client_order_id == CLIENT
    assert feed.state.hints_seen == 1 and feed.take_reconcile_requests() == []


def test_a_sequence_gap_asks_for_a_reconciliation_and_never_changes_anything() -> None:
    feed = UserFeed()
    feed.handle(message(1), T0)
    feed.handle(message(3), T0)
    assert feed.state.gaps == 1 and feed.take_reconcile_requests() == ["WS_SEQUENCE_GAP"]
    assert feed.take_reconcile_requests() == []  # taken once


@pytest.mark.parametrize(
    "raw",
    [
        b"not json",
        b"[]",
        json.dumps({"channel": "market_data", "sequence_num": 1, "events": []}).encode(),
        json.dumps({"channel": "user", "sequence_num": -1, "events": []}).encode(),
        json.dumps({"channel": "user", "sequence_num": True, "events": []}).encode(),
        json.dumps({"channel": "user", "sequence_num": 1, "events": "x"}).encode(),
        json.dumps(
            {
                "channel": "user",
                "sequence_num": 1,
                "events": [{"orders": [{"client_order_id": "x"}]}],
            }
        ).encode(),
        json.dumps(
            {
                "channel": "user",
                "sequence_num": 1,
                "events": [{"orders": [feed_order(cumulative_quantity="1e9")]}],
            }
        ).encode(),
        b"x" * (MAX_MESSAGE_BYTES + 1),
    ],
)
def test_malformed_messages_never_raise_and_ask_for_a_reconciliation(raw: bytes) -> None:
    feed = UserFeed()
    assert feed.handle(raw, T0) == []
    assert feed.state.malformed == 1 and "WS_MALFORMED" in feed.take_reconcile_requests()


def test_a_reconnect_resets_sequences_and_asks_for_a_reconciliation() -> None:
    feed = UserFeed()
    feed.handle(message(5), T0)
    feed.reset_connection()
    feed.handle(message(0), T0)
    assert feed.state.gaps == 0 and feed.take_reconcile_requests() == ["WS_RECONNECT"]


def test_silence_is_measured_from_the_last_good_message() -> None:
    feed = UserFeed()
    assert feed.silence_seconds(T0) is None
    feed.handle(message(1, channel="heartbeats"), T0)
    assert feed.silence_seconds(T0 + timedelta(seconds=42)) == 42


def test_the_feed_module_has_no_socket_or_client_code() -> None:
    from pathlib import Path

    from app.exchange import websocket

    source = Path(websocket.__file__).read_text()
    for word in ("import socket", "websockets", "aiohttp", "httpx", "connect(", "ssl"):
        assert word not in source, word


# ------------------------------------------------------------------ the scripted fake
def fake() -> FakeExchange:
    return FakeExchange(lambda: T0)


def test_the_fake_never_fills_or_moves_anything_by_itself() -> None:
    fx = fake()
    result = fx.submit(request())
    assert result.outcome == "ACCEPTED" and len(fx.orders) == 1
    assert fx.fills == [] and next(iter(fx.orders.values())).filled_qty == 0
    assert (
        "TEST-DOUBLE" in FakeExchange.__module__
        or "TEST-DOUBLE" in __import__("app.exchange.fake", fromlist=["x"]).PROVENANCE
    )


def test_a_duplicate_client_id_returns_the_existing_order_or_a_mismatch() -> None:
    fx = fake()
    first = fx.submit(request())
    again = fx.submit(request())
    assert (
        again.outcome == "EXISTING"
        and again.exchange_order_id == first.exchange_order_id
        and len(fx.orders) == 1
    )
    clash = fx.submit(request(price=D("101")))
    assert clash.outcome == "REJECTED" and clash.reason == "DUPLICATE_CLIENT_ORDER_ID_MISMATCH"


def test_a_timeout_after_the_exchange_took_the_order_is_ambiguous_and_the_order_exists() -> None:
    fx = fake()
    fx.fail("submit", Fault("timeout", processed=True))
    with pytest.raises(ExchangeError) as info:
        fx.submit(request())
    assert info.value.ambiguous and len(fx.orders) == 1


def test_a_timeout_before_processing_leaves_no_order() -> None:
    fx = fake()
    fx.fail("submit", Fault("timeout", processed=False))
    with pytest.raises(ExchangeError):
        fx.submit(request())
    assert fx.orders == {}


def test_cancel_queues_and_only_a_test_finishes_it() -> None:
    fx = fake()
    oid = fx.submit(request()).exchange_order_id or ""
    assert fx.cancel([oid])[oid].outcome == "CANCEL_QUEUED"
    assert fx.orders[oid].status == "CANCEL_REQUESTED"
    assert fx.cancel(["nope"])["nope"].outcome == "REJECTED"
