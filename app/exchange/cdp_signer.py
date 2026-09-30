"""ES256 request signing for the Coinbase Developer Platform (CDP) API.

Each request gets its own short-lived JWT: header {alg ES256, kid = key name, nonce}, payload
{sub = key name, iss "cdp", nbf = now, exp = now + 120 s, uri = "<METHOD> <host><path>"}. The token
is bound to ONE method, host and path (no query string), so a captured token cannot be replayed
for another endpoint and expires in two minutes. Only `GET` and `POST`, only `api.coinbase.com`, and
only paths under `/api/v3/brokerage/` can be signed. Nothing here is logged or stored.
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from collections.abc import Callable
from typing import Final

from app.exchange.credentials import Credentials
from app.exchange.errors import ExchangeError

HOST: Final = "api.coinbase.com"
PATH_PREFIX: Final = "/api/v3/brokerage/"
METHODS: Final = frozenset({"GET", "POST"})
LIFETIME_SECONDS: Final = 120


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _new_nonce() -> str:
    return secrets.token_hex(16)


class CdpSigner:
    def __init__(
        self,
        credentials: Credentials,
        *,
        clock: Callable[[], float] = time.time,
        nonce: Callable[[], str] = _new_nonce,
    ) -> None:
        self._credentials, self._clock, self._nonce = credentials, clock, nonce

    def __repr__(self) -> str:
        return "CdpSigner(<redacted>)"

    def bearer(self, method: str, host: str, path: str) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        if method not in METHODS or host != HOST or not path.startswith(PATH_PREFIX):
            raise ExchangeError("BAD_REQUEST")
        if "?" in path or "#" in path or len(path) > 300:
            raise ExchangeError("BAD_REQUEST")
        now = int(self._clock())
        name = self._credentials.key_name
        header = {"alg": "ES256", "kid": name, "nonce": self._nonce(), "typ": "JWT"}
        payload = {
            "sub": name,
            "iss": "cdp",
            "nbf": now,
            "exp": now + LIFETIME_SECONDS,
            "uri": f"{method} {host}{path}",
        }
        signing_input = (
            _b64(json.dumps(header, separators=(",", ":")).encode())
            + "."
            + _b64(json.dumps(payload, separators=(",", ":")).encode())
        )
        der = self._credentials.private_key.sign(
            signing_input.encode("ascii"), ec.ECDSA(hashes.SHA256())
        )
        r, s = decode_dss_signature(der)
        return signing_input + "." + _b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))
