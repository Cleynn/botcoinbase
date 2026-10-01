"""Coinbase Advanced Trade execution gateway: post-only limit orders, create and cancel only.

* Two POST paths only (`/orders`, `/orders/batch_cancel`) on one host, signed per request by an
  injected `Signer`; redirects are never followed and responses are size-capped while streaming.
* The order body is `OrderRequest.body()`: a post-only limit GTC order with a client id. There is no
  market order, stop, margin or transfer, so nothing (including the kill switch) can market-sell.
* A definite refusal (auth, validation, rate limit, `success: false`) is REJECTED: nothing was
  placed and a later retry is a new attempt. Anything that may have been processed (timeout,
  network loss, 5xx, an unparseable answer, a duplicate client id) is never reported as a success
  or a failure: it is ambiguous, the attempt becomes UNKNOWN and reconciliation decides before
  any retry.

The response shapes follow the documented ones and are NOT verified against the live API from this
repository (AS-C3, AS-C4); the VPS network test is where that happens.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Sequence
from typing import Any, Final

import httpx

from app.adapters.ratelimit import RateLimiter
from app.config import ExchangeSettings
from app.exchange.coinbase_private import ALLOWED_HOST, BASE_URL, Signer
from app.exchange.errors import ExchangeError
from app.exchange.gateway import CancelResult, OrderRequest, SubmitResult
from app.exchange.models import ORDER_ID_RE

USER_AGENT: Final = "TradingDots-gateway/0.10"
PATH_CREATE: Final = "/orders"
PATH_CANCEL: Final = "/orders/batch_cancel"
POST_PATHS: Final = (PATH_CREATE, PATH_CANCEL)
MAX_CANCEL_BATCH: Final = 100
_REASON_RE: Final = re.compile(r"[^A-Z0-9_]")


def _reason(value: object, default: str) -> str:
    """A fixed-vocabulary reason from exchange text: upper-case letters, digits and underscores."""
    text = _REASON_RE.sub("_", str(value or "").upper())[:40].strip("_")
    return text if len(text) >= 3 else default


class CoinbaseLiveGateway:
    venue = "COINBASE"

    def __init__(
        self,
        settings: ExchangeSettings,
        signer: Signer,
        *,
        transport: httpx.BaseTransport | None = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        requests_per_second: int = 5,
    ) -> None:
        self._signer = signer
        self._max_bytes = settings.max_response_bytes
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

    # ------------------------------------------------------------------ create
    def submit(self, request: OrderRequest) -> SubmitResult:
        status, body = self._post(PATH_CREATE, request.body())
        refused = _refusal(status)
        if refused is not None:
            return SubmitResult("REJECTED", None, refused)
        data = _json(body)
        success = data.get("success")
        if success is True:
            detail = data.get("success_response")
            order_id = detail.get("order_id") if isinstance(detail, dict) else None
            if isinstance(order_id, str) and ORDER_ID_RE.fullmatch(order_id):
                return SubmitResult("ACCEPTED", order_id)
            raise ExchangeError("UNEXPECTED_RESPONSE", ambiguous=True)  # it may have been placed
        if success is False:
            err = data.get("error_response")
            code = err.get("error") if isinstance(err, dict) else None
            reason = _reason(code or data.get("failure_reason"), "REJECTED")
            if "DUPLICATE" in reason:  # the id is known to the exchange: reconcile, never guess
                return SubmitResult("UNKNOWN", None, reason)
            return SubmitResult("REJECTED", None, reason)
        raise ExchangeError("UNEXPECTED_RESPONSE", ambiguous=True)

    # ------------------------------------------------------------------ cancel
    def cancel(self, exchange_order_ids: Sequence[str]) -> dict[str, CancelResult]:
        ids = list(exchange_order_ids)
        if not ids or len(ids) > MAX_CANCEL_BATCH or not all(ORDER_ID_RE.fullmatch(i) for i in ids):
            raise ExchangeError("BAD_REQUEST")
        status, body = self._post(PATH_CANCEL, {"order_ids": ids})
        refused = _refusal(status)
        if refused is not None:
            return {i: CancelResult("REJECTED", refused) for i in ids}
        results = _json(body).get("results")
        if not isinstance(results, list):
            raise ExchangeError("UNEXPECTED_RESPONSE", ambiguous=True)
        out: dict[str, CancelResult] = {}
        for item in results:
            if not isinstance(item, dict) or not isinstance(item.get("order_id"), str):
                continue
            order_id = item["order_id"]
            if order_id not in ids:
                continue  # an answer about an order we did not ask about is ignored
            if item.get("success") is True:
                out[order_id] = CancelResult("CANCEL_QUEUED")
            elif item.get("success") is False:
                out[order_id] = CancelResult(
                    "REJECTED", _reason(item.get("failure_reason"), "CANCEL_REJECTED")
                )
        for order_id in ids:  # no answer for an order: its outcome is unknown, not rejected
            out.setdefault(order_id, CancelResult("UNKNOWN", "NO_ANSWER"))
        return out

    # ------------------------------------------------------------------ transport (two POST paths)
    def _post(self, path: str, payload: dict[str, Any]) -> tuple[int, bytes]:
        """(status, body) for a definite answer; ambiguous `ExchangeError` when it may have run."""
        if path not in POST_PATHS:
            raise ExchangeError("BAD_REQUEST")
        content = json.dumps(payload, separators=(",", ":")).encode()
        token = self._signer.bearer("POST", ALLOWED_HOST, "/api/v3/brokerage" + path)
        self._limiter.acquire()
        try:
            with self._http.stream(
                "POST",
                BASE_URL + path,
                content=content,
                headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
            ) as response:
                if response.url.host != ALLOWED_HOST:
                    raise ExchangeError("HOST_NOT_ALLOWED", ambiguous=True)
                status = response.status_code
                if status != 200:
                    if 200 < status < 400:  # a success other than 200, or a redirect: never guess
                        raise ExchangeError("UNEXPECTED_STATUS", status=status, ambiguous=True)
                    if status >= 500:
                        raise ExchangeError("SERVER_ERROR", status=status, ambiguous=True)
                    return status, b""  # a definite 4xx: the request was not processed
                ctype = response.headers.get("content-type", "").lower()
                if not ctype.startswith("application/json"):
                    raise ExchangeError("BAD_CONTENT_TYPE", ambiguous=True)
                declared = response.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > self._max_bytes:
                    raise ExchangeError("TOO_LARGE", ambiguous=True)
                body = bytearray()
                for chunk in response.iter_bytes():
                    body.extend(chunk)
                    if len(body) > self._max_bytes:
                        raise ExchangeError("TOO_LARGE", ambiguous=True)
                return status, bytes(body)
        except ExchangeError:
            raise
        except httpx.TimeoutException as exc:
            raise ExchangeError("TIMEOUT", ambiguous=True) from exc
        except httpx.HTTPError as exc:
            raise ExchangeError("NETWORK", ambiguous=True) from exc


def _refusal(status: int) -> str | None:
    """A definite non-200 answer as a fixed reason (None = 200, go on and read the body)."""
    if status == 200:
        return None
    if status in (401, 403):
        return "AUTH_REJECTED"
    if status == 429:
        return "RATE_LIMITED"
    return f"HTTP_{status}"


def _json(body: bytes) -> dict[str, Any]:
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise ExchangeError("UNEXPECTED_RESPONSE", ambiguous=True) from exc
    if not isinstance(data, dict):
        raise ExchangeError("UNEXPECTED_RESPONSE", ambiguous=True)
    return data
