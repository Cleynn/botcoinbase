"""Coinbase Advanced Trade PUBLIC market-data client: exactly five read-only GET paths.

There is no private call, no credential, no signing and no order code in this module (or anywhere in
this build). The base URL is a code constant. Redirects are never followed, response bodies are
size-capped while streaming, product identifiers are pattern-checked before they reach a URL and
requests are rate-limited on the client side.

Paths (CB-2): /time, /market/products, /market/products/{id}, /market/products/{id}/candles,
/market/product_book.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final

import httpx

from app.adapters.ratelimit import RateLimiter
from app.config import ExchangeSettings

BASE_URL: Final = "https://api.coinbase.com/api/v3/brokerage"
ALLOWED_HOST: Final = "api.coinbase.com"
USER_AGENT: Final = "TradingDots-public-client/0.4"

GRANULARITY_SECONDS: Final[Mapping[str, int]] = {
    "ONE_MINUTE": 60,
    "FIVE_MINUTE": 300,
    "FIFTEEN_MINUTE": 900,
    "THIRTY_MINUTE": 1800,
    "ONE_HOUR": 3600,
    "TWO_HOUR": 7200,
    "SIX_HOUR": 21600,
    "ONE_DAY": 86400,
}
MAX_CANDLES_PER_REQUEST: Final = 350

# The complete, fixed path set. Nothing else can be requested through this client.
PATH_TIME: Final = "/time"
PATH_PRODUCTS: Final = "/market/products"
PATH_PRODUCT: Final = "/market/products/{product_id}"
PATH_CANDLES: Final = "/market/products/{product_id}/candles"
PATH_BOOK: Final = "/market/product_book"
PUBLIC_PATHS: Final = (PATH_TIME, PATH_PRODUCTS, PATH_PRODUCT, PATH_CANDLES, PATH_BOOK)


class PublicClientError(Exception):
    """A request failed. `code` is a fixed vocabulary; the message never contains response data."""

    def __init__(self, code: str, status: int | None = None) -> None:
        super().__init__(code)
        self.code = code
        self.status = status  # HTTP status when there was one (never response content)

    @property
    def retryable(self) -> bool:
        """Safe to repeat: every call is a read-only GET; only transient failures qualify."""
        return self.code in {"TIMEOUT", "NETWORK", "RATE_LIMITED"} or (
            self.code == "HTTP_ERROR" and self.status is not None and self.status >= 500
        )


class CoinbasePublicClient:
    def __init__(
        self,
        settings: ExchangeSettings,
        *,
        transport: httpx.BaseTransport | None = None,
        limiter: RateLimiter | None = None,
    ) -> None:
        self._max_bytes = settings.max_response_bytes
        self._limiter = limiter or RateLimiter(settings.requests_per_second)
        kwargs: dict[str, object] = {}
        if transport is not None:
            kwargs["transport"] = transport
        elif settings.egress_proxy:
            kwargs["proxy"] = settings.egress_proxy
        self._http = httpx.Client(
            base_url=BASE_URL,
            timeout=httpx.Timeout(settings.timeout_seconds),
            follow_redirects=False,
            trust_env=False,  # never pick up proxy or credential settings from the environment
            headers={"Accept": "application/json", "User-Agent": USER_AGENT},
            **kwargs,  # type: ignore[arg-type]
        )

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> CoinbasePublicClient:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ the five paths
    def server_time(self) -> bytes:
        return self._get(PATH_TIME, {})

    def list_products(self) -> bytes:
        # Always SPOT; never get_all_products and never user_country_code (CB-2).
        return self._get(PATH_PRODUCTS, {"product_type": "SPOT"})

    def get_product(self, product_id: str) -> bytes:
        return self._get(PATH_PRODUCT.format(product_id=_product_id(product_id)), {})

    def get_candles(self, product_id: str, granularity: str, start: int, end: int) -> bytes:
        if granularity not in GRANULARITY_SECONDS:
            raise PublicClientError("BAD_REQUEST")
        seconds = GRANULARITY_SECONDS[granularity]
        if not (0 < start < end) or (end - start) // seconds > MAX_CANDLES_PER_REQUEST:
            raise PublicClientError("BAD_REQUEST")
        return self._get(
            PATH_CANDLES.format(product_id=_product_id(product_id)),
            {"start": str(start), "end": str(end), "granularity": granularity},
        )

    def get_product_book(self, product_id: str, limit: int = 50) -> bytes:
        if not 1 <= limit <= 500:
            raise PublicClientError("BAD_REQUEST")
        return self._get(PATH_BOOK, {"product_id": _product_id(product_id), "limit": str(limit)})

    # ------------------------------------------------------------------ transport
    def _get(self, path: str, params: Mapping[str, str]) -> bytes:
        self._limiter.acquire()
        try:
            with self._http.stream("GET", BASE_URL + path, params=dict(params)) as response:
                if response.url.host != ALLOWED_HOST:  # defence in depth; redirects are off anyway
                    raise PublicClientError("HOST_NOT_ALLOWED")
                if 300 <= response.status_code < 400:
                    raise PublicClientError("REDIRECT_REFUSED")
                if response.status_code == 429:
                    raise PublicClientError("RATE_LIMITED", 429)
                if response.status_code != 200:
                    raise PublicClientError("HTTP_ERROR", response.status_code)
                if (
                    not response.headers.get("content-type", "")
                    .lower()
                    .startswith("application/json")
                ):
                    raise PublicClientError("BAD_CONTENT_TYPE")
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > self._max_bytes:
                    raise PublicClientError("TOO_LARGE")
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_bytes:
                        raise PublicClientError("TOO_LARGE")
                return bytes(body)
        except PublicClientError:
            raise
        except httpx.TimeoutException as exc:
            raise PublicClientError("TIMEOUT") from exc
        except httpx.HTTPError as exc:
            raise PublicClientError("NETWORK") from exc


def _product_id(value: str) -> str:
    from app.adapters.coinbase_parse import MAX_PRODUCT_ID_LENGTH, PRODUCT_ID_RE

    if not PRODUCT_ID_RE.fullmatch(value) or len(value) > MAX_PRODUCT_ID_LENGTH:
        raise PublicClientError("BAD_REQUEST")
    return value
