"""Coinbase Advanced Trade PRIVATE READ adapter: five GET paths, no write of any kind.

This is the only place private data is read, and it is a boundary, not a feature switch:

* There is no POST, PUT, PATCH or DELETE here, no order body, no create or cancel call. The
  execution side has no real-exchange implementation anywhere in this build.
* Requests are signed through an injected `Signer`. This build ships only `NullSigner`, which
  refuses with NO_CREDENTIALS: no key is read, loaded, stored or logged by any code here, and the
  default deployment configures no signer, so the adapter cannot make a call.
* Paths, query parameters, host and content type are fixed allowlists; redirects are never
  followed; responses are size-capped while streaming; cursor loops have a page cap, duplicate-page
  detection and a time budget; list calls use the default sort only (CB-11).
* Responses are parsed by `app.exchange.models` (strict required fields, tolerant extras).

The response shapes are the documented ones and are NOT verified against the live API from this
repository (AS-C3, AS-C4). Tests use synthetic responses and say so.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Final, Protocol, TypeVar
from urllib.parse import quote

import httpx

from app.adapters.coinbase_parse import ParseError
from app.adapters.ratelimit import RateLimiter
from app.config import ExchangeSettings
from app.exchange.errors import ExchangeError, NoCredentials
from app.exchange.models import (
    ORDER_ID_RE,
    Balance,
    ExchangeFill,
    ExchangeOrder,
    KeyPermissions,
    parse_accounts,
    parse_fills,
    parse_key_permissions,
    parse_order,
    parse_orders,
)

T = TypeVar("T")
BASE_URL: Final = "https://api.coinbase.com/api/v3/brokerage"
ALLOWED_HOST: Final = "api.coinbase.com"
USER_AGENT: Final = "TradingDots-private-read/0.5"
METHOD: Final = "GET"  # the only method this module can use

PATH_ACCOUNTS: Final = "/accounts"
PATH_ORDERS: Final = "/orders/historical/batch"
PATH_ORDER: Final = "/orders/historical/{order_id}"
PATH_FILLS: Final = "/orders/historical/fills"
PATH_KEY_PERMISSIONS: Final = "/key_permissions"
READ_PATHS: Final = (PATH_ACCOUNTS, PATH_ORDERS, PATH_ORDER, PATH_FILLS, PATH_KEY_PERMISSIONS)
ALLOWED_PARAMS: Final = frozenset({"start_date", "end_date", "cursor", "limit"})
MAX_PAGES: Final = 20
PAGE_LIMIT: Final = 250
TIME_BUDGET_SECONDS: Final = 30.0


class Signer(Protocol):
    """Produces the per-request bearer credential. It is never stored, logged or reused."""

    def bearer(self, method: str, host: str, path: str) -> str: ...


class NullSigner:
    """The only signer this build ships: there is no credential, so every call is refused."""

    def bearer(self, method: str, host: str, path: str) -> str:
        raise NoCredentials


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class CoinbasePrivateReader:
    venue = "COINBASE"  # the venue whose data it reads; it can still only GET

    def __init__(
        self,
        settings: ExchangeSettings,
        signer: Signer,
        *,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        requests_per_second: int = 10,
    ) -> None:
        self._signer = signer
        self._max_bytes = settings.max_response_bytes
        self._monotonic = monotonic
        self._limiter = RateLimiter(requests_per_second, monotonic=monotonic, sleep=sleep)
        kwargs: dict[str, object] = {}
        if transport is not None:
            kwargs["transport"] = transport
        elif settings.egress_proxy:
            kwargs["proxy"] = settings.egress_proxy
        self._http = httpx.Client(
            base_url=BASE_URL,
            timeout=httpx.Timeout(settings.timeout_seconds),
            follow_redirects=False,
            trust_env=False,
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            **kwargs,  # type: ignore[arg-type]
        )

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ the reads
    def key_permissions(self) -> KeyPermissions:
        return self._parsed(parse_key_permissions, self._get(PATH_KEY_PERMISSIONS, {}))

    def list_accounts(self) -> tuple[Balance, ...]:
        found: dict[str, Balance] = {}
        for body in self._pages(PATH_ACCOUNTS, {}):
            for balance in self._parsed(parse_accounts, body).items:
                found[balance.currency] = balance
        return tuple(found.values())

    def list_orders(self, start: datetime, end: datetime) -> tuple[ExchangeOrder, ...]:
        params = {"start_date": _iso(start), "end_date": _iso(end), "limit": str(PAGE_LIMIT)}
        found: dict[str, ExchangeOrder] = {}
        for body in self._pages(PATH_ORDERS, params):
            for order in self._parsed(parse_orders, body).items:
                found[order.order_id] = order  # dedupe by order_id (CB-11)
        return tuple(found.values())

    def list_fills(self, start: datetime, end: datetime) -> tuple[ExchangeFill, ...]:
        params = {"start_date": _iso(start), "end_date": _iso(end), "limit": str(PAGE_LIMIT)}
        found: dict[str, ExchangeFill] = {}
        for body in self._pages(PATH_FILLS, params):
            for fill in self._parsed(parse_fills, body).items:
                found[fill.fill_id] = fill
        return tuple(found.values())

    def get_order(self, order_id: str) -> ExchangeOrder | None:
        if not ORDER_ID_RE.fullmatch(order_id):
            raise ExchangeError("BAD_REQUEST")
        try:
            body = self._get(PATH_ORDER.format(order_id=quote(order_id, safe="")), {})
        except ExchangeError as exc:
            if exc.status == 404:
                return None
            raise
        return self._parsed(parse_order, body)

    # ------------------------------------------------------------------ paging
    def _pages(self, path: str, params: Mapping[str, str]) -> list[bytes]:
        deadline = self._monotonic() + TIME_BUDGET_SECONDS
        bodies: list[bytes] = []
        seen: set[str] = set()
        cursor = ""
        for _ in range(MAX_PAGES):
            if self._monotonic() > deadline:
                raise ExchangeError("PAGING_TIME_BUDGET")
            query = dict(params)
            if cursor:
                query["cursor"] = cursor
            body = self._get(path, query)
            bodies.append(body)
            next_cursor = _next_cursor(body)
            if not next_cursor:
                return bodies
            if next_cursor in seen or next_cursor == cursor:
                raise ExchangeError("PAGING_LOOP")
            seen.add(next_cursor)
            cursor = next_cursor
        raise ExchangeError("PAGING_CAP")

    @staticmethod
    def _parsed(parser: Callable[[bytes], T], body: bytes) -> T:
        try:
            return parser(body)
        except ParseError as exc:
            raise ExchangeError("UNEXPECTED_RESPONSE") from exc

    # ------------------------------------------------------------------ transport (GET only)
    def _get(self, path: str, params: Mapping[str, str]) -> bytes:
        if set(params) - ALLOWED_PARAMS:
            raise ExchangeError("BAD_REQUEST")
        template = path if path in READ_PATHS else _order_template(path)
        if template is None:
            raise ExchangeError("BAD_REQUEST")
        token = self._signer.bearer(METHOD, ALLOWED_HOST, "/api/v3/brokerage" + path)
        self._limiter.acquire()
        try:
            with self._http.stream(
                METHOD,
                BASE_URL + path,
                params=dict(params),
                headers={"Authorization": "Bearer " + token},
            ) as response:
                if response.url.host != ALLOWED_HOST:
                    raise ExchangeError("HOST_NOT_ALLOWED")
                status = response.status_code
                if 300 <= status < 400:
                    raise ExchangeError("REDIRECT_REFUSED", status=status)
                if status in (401, 403):
                    raise ExchangeError("AUTH_REJECTED", status=status)
                if status == 429:
                    raise ExchangeError("RATE_LIMITED", status=429)
                if status >= 500:
                    raise ExchangeError("SERVER_ERROR", status=status)
                if status != 200:
                    raise ExchangeError("HTTP_ERROR", status=status)
                ctype = response.headers.get("content-type", "").lower()
                if not ctype.startswith("application/json"):
                    raise ExchangeError("BAD_CONTENT_TYPE")
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > self._max_bytes:
                    raise ExchangeError("TOO_LARGE")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_bytes:
                        raise ExchangeError("TOO_LARGE")
                return bytes(body)
        except ExchangeError:
            raise
        except httpx.TimeoutException as exc:
            raise ExchangeError("TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise ExchangeError("NETWORK") from exc


def _order_template(path: str) -> str | None:
    prefix = "/orders/historical/"
    if not path.startswith(prefix):
        return None
    tail = path[len(prefix) :]
    return PATH_ORDER if tail and "/" not in tail and tail != "batch" and tail != "fills" else None


def _next_cursor(body: bytes) -> str:
    from app.adapters.coinbase_parse import load_json

    data = load_json(body)
    if not isinstance(data, dict):
        raise ExchangeError("UNEXPECTED_RESPONSE")
    cursor = data.get("cursor", "")
    has_next = data.get("has_next")
    if has_next is False or (has_next is None and not cursor):
        return ""
    if not isinstance(cursor, str) or len(cursor) > 256:
        raise ExchangeError("UNEXPECTED_RESPONSE")
    return cursor
