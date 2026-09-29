"""The public Coinbase client: five read-only GET paths, fixed host, no credentials, bounded I/O."""

from __future__ import annotations

import ast
import inspect
import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from app.adapters import coinbase_public as cp
from app.config import ExchangeSettings
from tests.coinbase_fakes import FakeCoinbase

FIXED = "https://api.coinbase.com/api/v3/brokerage"


@pytest.fixture
def fake() -> FakeCoinbase:
    from datetime import UTC, datetime

    return FakeCoinbase(lambda: datetime(2026, 9, 29, 12, tzinfo=UTC))


def test_base_url_and_host_are_code_constants() -> None:
    assert cp.BASE_URL == FIXED and cp.ALLOWED_HOST == "api.coinbase.com"
    assert "base_url" not in ExchangeSettings.model_fields
    assert not any("url" in f and f != "egress_proxy" for f in ExchangeSettings.model_fields)


def test_exactly_five_public_paths_exist() -> None:
    assert cp.PUBLIC_PATHS == (
        "/time",
        "/market/products",
        "/market/products/{product_id}",
        "/market/products/{product_id}/candles",
        "/market/product_book",
    )
    methods = {
        n
        for n, _ in inspect.getmembers(cp.CoinbasePublicClient, inspect.isfunction)
        if not n.startswith("_") and n not in ("close",)
    }
    assert methods == {
        "server_time",
        "list_products",
        "get_product",
        "get_candles",
        "get_product_book",
    }


def test_every_request_is_a_get_to_the_fixed_host_with_no_credentials(fake: FakeCoinbase) -> None:
    with fake.client() as client:
        client.server_time()
        client.list_products()
        client.get_product("BTC-USDC")
        client.get_candles("BTC-USDC", "FIVE_MINUTE", 1_790_000_000 - 3000, 1_790_000_000)
        client.get_product_book("BTC-USDC", 25)
    assert len(fake.requests) == 5
    for request in fake.requests:
        assert request.method == "GET"
        assert request.url.scheme == "https" and request.url.host == "api.coinbase.com"
        assert request.url.path.startswith("/api/v3/brokerage/")
        assert request.content == b""
        lowered = {k.lower() for k in request.headers}
        assert not lowered & {
            "authorization",
            "cookie",
            "x-cb-access-key",
            "cb-access-key",
            "cb-access-sign",
            "x-api-key",
            "proxy-authorization",
        }
    paths = [r.url.path.removeprefix("/api/v3/brokerage") for r in fake.requests]
    assert paths == [
        "/time",
        "/market/products",
        "/market/products/BTC-USDC",
        "/market/products/BTC-USDC/candles",
        "/market/product_book",
    ]


def test_product_list_always_asks_for_spot_only_and_never_get_all_products(
    fake: FakeCoinbase,
) -> None:
    fake_client = fake.client()
    fake_client.list_products()
    params = dict(fake.requests[0].url.params)
    assert params == {"product_type": "SPOT"}
    assert "get_all_products" not in str(fake.requests[0].url)
    assert "user_country_code" not in str(fake.requests[0].url)


@pytest.mark.parametrize(
    "product_id",
    [
        "BTC-USD",
        "btc-usdc",
        "BTC-USDC/../x",
        "BTC-USDC?x=1",
        "BTC-USDC#",
        "",
        "A" * 30 + "-USDC",
        "BTC-USDC\r\nHost: evil",
        "%2e%2e",
        "BTC-USDC%00",
    ],
)
def test_product_ids_are_pattern_checked_before_they_reach_a_url(
    fake: FakeCoinbase, product_id: str
) -> None:
    client = fake.client()
    calls: list[Callable[[], bytes]] = [
        lambda: client.get_product(product_id),
        lambda: client.get_candles(product_id, "ONE_DAY", 1_000_000_000, 1_000_086_400),
        lambda: client.get_product_book(product_id),
    ]
    for call in calls:
        with pytest.raises(cp.PublicClientError) as err:
            call()
        assert err.value.code == "BAD_REQUEST"
    assert fake.requests == []


def test_candle_requests_are_bounded_and_granularity_is_an_allowlist(fake: FakeCoinbase) -> None:
    client = fake.client()
    for args in (
        ("BTC-USDC", "FIVE_MINUTE", 100, 100 + 351 * 300),  # more than 350 candles
        ("BTC-USDC", "FIVE_MINUTE", 500, 100),  # reversed
        ("BTC-USDC", "FIVE_MINUTE", 0, 100),
        ("BTC-USDC", "WEEKLY", 100, 5000),  # not a granularity
        ("BTC-USDC", "FIVE_MINUTE; DROP", 100, 5000),
    ):
        with pytest.raises(cp.PublicClientError):
            client.get_candles(*args)
    assert fake.requests == []
    client.get_candles("BTC-USDC", "FIVE_MINUTE", 100, 100 + 350 * 300)  # exactly 350 is fine
    assert dict(fake.requests[0].url.params)["granularity"] == "FIVE_MINUTE"


def test_book_limit_is_bounded(fake: FakeCoinbase) -> None:
    client = fake.client()
    for limit in (0, -1, 501):
        with pytest.raises(cp.PublicClientError):
            client.get_product_book("BTC-USDC", limit)


@pytest.mark.parametrize("status", [301, 302, 307, 308])
def test_redirects_are_never_followed(status: int) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(status, headers={"location": "https://evil.example/x"})

    client = cp.CoinbasePublicClient(
        ExchangeSettings(), transport=httpx.MockTransport(handler), limiter=cp.RateLimiter(1000)
    )
    with pytest.raises(cp.PublicClientError) as err:
        client.server_time()
    assert err.value.code == "REDIRECT_REFUSED" and len(calls) == 1


@pytest.mark.parametrize(
    ("status", "code"),
    [(429, "RATE_LIMITED"), (500, "HTTP_ERROR"), (404, "HTTP_ERROR"), (403, "HTTP_ERROR")],
)
def test_http_errors_map_to_fixed_codes(fake: FakeCoinbase, status: int, code: str) -> None:
    fake.errors["/time"] = status
    with pytest.raises(cp.PublicClientError) as err:
        fake.client().server_time()
    assert err.value.code == code


def test_non_json_content_is_refused(fake: FakeCoinbase) -> None:
    transport = httpx.MockTransport(
        lambda r: httpx.Response(200, headers={"content-type": "text/html"}, content=b"<html>")
    )
    client = cp.CoinbasePublicClient(
        ExchangeSettings(), transport=transport, limiter=cp.RateLimiter(1000)
    )
    with pytest.raises(cp.PublicClientError) as err:
        client.server_time()
    assert err.value.code == "BAD_CONTENT_TYPE"


def test_oversized_responses_are_cut_off_while_streaming() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b"[" + b"1," * 100_000 + b"1]",
        )

    settings = ExchangeSettings(max_response_bytes=65536)
    client = cp.CoinbasePublicClient(
        settings, transport=httpx.MockTransport(handler), limiter=cp.RateLimiter(1000)
    )
    with pytest.raises(cp.PublicClientError) as err:
        client.list_products()
    assert err.value.code == "TOO_LARGE"


def test_a_declared_oversize_body_is_refused_before_reading() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json", "content-length": "999999999"},
            content=b"{}",
        )

    client = cp.CoinbasePublicClient(
        ExchangeSettings(max_response_bytes=65536),
        transport=httpx.MockTransport(handler),
        limiter=cp.RateLimiter(1000),
    )
    with pytest.raises((cp.PublicClientError, httpx.HTTPError)):
        client.server_time()


def test_timeouts_and_network_errors_become_fixed_codes() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    def broken(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route", request=request)

    for handler, code in ((timeout, "TIMEOUT"), (broken, "NETWORK")):
        client = cp.CoinbasePublicClient(
            ExchangeSettings(), transport=httpx.MockTransport(handler), limiter=cp.RateLimiter(1000)
        )
        with pytest.raises(cp.PublicClientError) as err:
            client.server_time()
        assert err.value.code == code
        assert "no route" not in str(err.value) and "slow" not in str(err.value)


def test_error_messages_never_contain_response_content() -> None:
    secret = "sk-live-should-never-appear"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            500,
            headers={"content-type": "application/json"},
            content=json.dumps({"error": secret}).encode(),
        )

    client = cp.CoinbasePublicClient(
        ExchangeSettings(), transport=httpx.MockTransport(handler), limiter=cp.RateLimiter(1000)
    )
    with pytest.raises(cp.PublicClientError) as err:
        client.server_time()
    assert secret not in str(err.value) and secret not in repr(err.value)


def test_the_client_ignores_proxy_and_credential_environment_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("HTTPS_PROXY", "http://evil.example:3128")
    monkeypatch.setenv("ALL_PROXY", "http://evil.example:3128")
    monkeypatch.setenv("SSL_CERT_FILE", "/nonexistent")
    client = cp.CoinbasePublicClient(ExchangeSettings())
    assert client._http.trust_env is False  # noqa: SLF001
    assert client._http.follow_redirects is False  # noqa: SLF001
    client.close()


def test_the_rate_limiter_never_exceeds_its_rate() -> None:
    clock = {"t": 0.0}
    slept: list[float] = []

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock["t"] += seconds

    limiter = cp.RateLimiter(4, monotonic=lambda: clock["t"], sleep=sleep)
    for _ in range(4):
        limiter.acquire()  # the burst is the bucket capacity
    assert slept == []
    for _ in range(8):
        limiter.acquire()
    assert sum(slept) >= 8 / 4 - 1e-6  # eight more calls need at least two seconds at 4/s


def test_settings_bound_the_client() -> None:
    for bad in (
        {"requests_per_second": 0},
        {"requests_per_second": 50},
        {"timeout_seconds": 1},
        {"max_response_bytes": 1},
        {"egress_proxy": "http://user:pw@proxy:3128"},
        {"egress_proxy": "https://proxy:3128"},
        {"egress_proxy": "http://proxy"},
    ):
        with pytest.raises(ValueError):
            ExchangeSettings(**bad)
    assert (
        ExchangeSettings(egress_proxy="http://egress-proxy:3128").egress_proxy
        == "http://egress-proxy:3128"
    )
    assert ExchangeSettings(egress_proxy="").egress_proxy is None


def _code_words(module: object) -> str:
    """Identifiers and string literals of a module, excluding docstrings and comments."""
    tree = ast.parse(inspect.getsource(module))  # type: ignore[arg-type]
    words: list[str] = []
    docstrings = {
        id(node.body[0].value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.FunctionDef | ast.ClassDef)
        and node.body
        and isinstance(node.body[0], ast.Expr)
        and isinstance(node.body[0].value, ast.Constant)
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                words.append(node.value)
        elif isinstance(node, ast.Name):
            words.append(node.id)
        elif isinstance(node, ast.Attribute):
            words.append(node.attr)
        elif isinstance(node, ast.arg):
            words.append(node.arg)
        elif isinstance(node, ast.FunctionDef):
            words.append(node.name)
    return "\n".join(words).lower()


def test_no_private_endpoint_or_order_path_is_present_in_the_module() -> None:
    code = _code_words(cp)
    for forbidden in (
        "/orders",
        "/accounts",
        "/portfolios",
        "/key_permissions",
        "cancel",
        "create_order",
        "authorization",
        "jwt",
        "sign",
        "secret",
        "api_key",
        "cb-access",
        "passphrase",
        "private",
    ):
        assert forbidden not in code, forbidden


def _unused(_: Any) -> None:  # keep typing imports honest
    return None
